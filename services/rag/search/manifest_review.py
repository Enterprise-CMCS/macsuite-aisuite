import asyncio
import os
import re
import sys

from botocore.config import Config
from botocore.exceptions import ClientError

from common.utils.aws_files_access import list_s3_keys, read_s3_bytes, read_s3_many, write_s3_json
from common.utils.contract_config import validate_embeddings_table_name
from common.utils.helper import Helper
from common.utils.logger import log
from common.utils.manifest import (
    REQUEST_KEY_PATTERN,
    Manifest,
    RequirementResult,
    ReviewResults,
    manifest_object_key,
    results_object_key,
    review_item_prefix,
)
from data_preprocessing.bedrock.bda_results import rag_output_key
from search.database_searching.review_models import page_number, simplify_doc_id

DEFAULT_CONCURRENCY = 4
MAX_FAILED_REVIEW_RATIO = 0.2
NOT_FOUND = "Not found"
PROGRESS_LOG_EVERY = 50
_S3_WORKERS = 16
_VALID_CONFIDENCE = frozenset({"HIGH", "MEDIUM", "LOW"})
_ITEM_KEY_RE = re.compile(r"(\d+)\.json$")


class ReviewIncompleteError(RuntimeError):
    pass


class ReviewStore:
    """Reads the manifest and persists per-item checkpoints and final results in S3."""

    def __init__(self, s3, bucket, request_key):
        self._s3 = s3
        self._bucket = bucket
        self._request_key = request_key
        self._item_prefix = review_item_prefix(request_key)

    def load_manifest(self):
        return Manifest.model_validate_json(read_s3_bytes(self._s3, self._bucket, manifest_object_key(self._request_key)))

    def completed_items(self):
        indexed_keys = []
        for key in list_s3_keys(self._s3, self._bucket, self._item_prefix, suffix=".json"):
            match = _ITEM_KEY_RE.search(key)
            if match:
                indexed_keys.append((int(match.group(1)), key))

        bodies = read_s3_many(self._s3, self._bucket, [key for _, key in indexed_keys], max_workers=_S3_WORKERS)
        return {
            index: RequirementResult.model_validate_json(body)
            for (index, _), body in zip(indexed_keys, bodies)
        }

    def save_item(self, index, result):
        write_s3_json(self._s3, self._bucket, f"{self._item_prefix}{index:06d}.json", result.model_dump_json())

    def save_results(self, results):
        write_s3_json(
            self._s3,
            self._bucket,
            results_object_key(self._request_key),
            results.model_dump_json(exclude_none=True),
        )


def document_labels(manifest):
    """Map each source key, RAG_Output key, and unambiguous basename back to MuleSoft's documentId."""
    by_exact_key = {}
    counts = {}
    by_basename = {}
    for doc in manifest.documents:
        by_exact_key[doc.s3.path] = doc.documentId
        by_exact_key[rag_output_key(doc.s3.path)] = doc.documentId
        by_exact_key[rag_output_key(doc.s3.path, namespace=manifest.requestKey)] = doc.documentId
        base = simplify_doc_id(os.path.basename(doc.s3.path))
        counts[base] = counts.get(base, 0) + 1
        by_basename[base] = doc.documentId
    unambiguous_basenames = {base: doc_id for base, doc_id in by_basename.items() if counts[base] == 1}
    return {**unambiguous_basenames, **by_exact_key}


def found_in(evidence, labels):
    """Format verified page locations (e.g. 'Page 88', 'Pages 41-45', or 'DOC-001: Page 3; DOC-003: Page 7')."""
    pages_by_doc = {}
    for record in evidence:
        number = page_number(record.page, record.printed_page)
        if not number:
            continue
        doc_id = record.doc_id or ""
        source_key = record.source_key or ""
        label = (
            labels.get(source_key)
            or labels.get(doc_id)
            or labels.get(simplify_doc_id(os.path.basename(doc_id)))
            or source_key
            or doc_id
        )
        # Order by physical page index so printed roman numerals stay in document order.
        pages_by_doc.setdefault(label, {}).setdefault(number, record.page or 0)

    if not pages_by_doc:
        return NOT_FOUND

    formatted = []
    for label, positions in pages_by_doc.items():
        ordered_pages = sorted(positions, key=positions.get)
        spans = []
        for number in ordered_pages:
            if spans and number.isdigit() and spans[-1][1].isdigit() and int(number) == int(spans[-1][1]) + 1:
                spans[-1][1] = number
            else:
                spans.append([number, number])
        page_text = ", ".join(start if start == end else f"{start}-{end}" for start, end in spans)
        span = f"{'Page' if len(ordered_pages) == 1 else 'Pages'} {page_text}"
        formatted.append((label, span))

    if len(formatted) == 1:
        return formatted[0][1]
    return "; ".join(f"{label}: {span}" if label else span for label, span in formatted)


def to_result(requirement, review, labels):
    """Convert an agent RequirementReview into MuleSoft's RequirementResult contract."""
    evidence = [record for record in review.evidence if record.verified]
    failed = bool(review.error)

    reasoning = review.in_reviewer_terms(review.argument)
    if review.missing_information:
        reasoning = f"{reasoning}\n\nMissing information: {review.in_reviewer_terms(review.missing_information)}"

    rationale = review.in_reviewer_terms(review.decision_response)
    if failed and not rationale:
        rationale = "The automated review did not complete; this requirement needs manual review."

    confidence = (review.decision_confidence or "").strip().upper()
    if failed or confidence not in _VALID_CONFIDENCE:
        confidence = "LOW"

    return RequirementResult(
        **requirement.model_dump(),
        siRecommendation="MET" if review.status == "MET" and not failed else "UNCLEAR",
        confidence=confidence,
        foundIn=found_in(evidence, labels),
        citedText="\n\n".join(dict.fromkeys(record.quote.strip() for record in evidence if record.quote.strip())),
        rationale=rationale,
        reasoning=reasoning.strip(),
        error=review.error or None,
    )


async def run_review(manifest, store, review, concurrency=DEFAULT_CONCURRENCY):
    """Review every requirement not already checkpointed, then write ordered results to S3."""
    requirements = manifest.legalRequirements
    checkpointed = await asyncio.to_thread(store.completed_items)
    results = {
        index: result
        for index, result in checkpointed.items()
        if index < len(requirements) and result.id == requirements[index].id
    }
    pending = [(index, item) for index, item in enumerate(requirements) if index not in results]
    log.info(
        f"Reviewing {len(pending)} of {len(requirements)} requirement(s) for {manifest.requestKey} "
        f"({len(results)} resumed from checkpoints, concurrency {concurrency})"
    )

    labels = document_labels(manifest)
    semaphore = asyncio.Semaphore(concurrency)

    async def review_one(index, requirement):
        async with semaphore:
            result = to_result(requirement, await review(requirement), labels)
        if not result.error:
            await asyncio.to_thread(store.save_item, index, result)
        results[index] = result
        if len(results) % PROGRESS_LOG_EVERY == 0:
            log.info(f"Reviewed {len(results)} of {len(requirements)} requirement(s) for {manifest.requestKey}")

    await asyncio.gather(*(review_one(index, item) for index, item in pending))

    ordered = [results[index] for index in range(len(requirements))]
    failed = [result for result in ordered if result.error]
    if len(failed) > MAX_FAILED_REVIEW_RATIO * len(ordered):
        raise ReviewIncompleteError(
            f"{len(failed)} of {len(ordered)} requirement review(s) failed; first error: {failed[0].error}"
        )

    review_results = ReviewResults(
        requestKey=manifest.requestKey,
        reviewType=manifest.reviewType,
        state=manifest.state,
        legalRequirements=ordered,
    )
    await asyncio.to_thread(store.save_results, review_results)
    return review_results


def main():
    request_key = os.environ.get("REQUEST_KEY", "")
    try:
        if not re.fullmatch(REQUEST_KEY_PATTERN, request_key):
            raise ValueError(f"REQUEST_KEY '{request_key}' is missing or invalid.")
        table_name = validate_embeddings_table_name(os.environ.get("EMBEDDINGS_TABLE_NAME"))
        concurrency = int(os.environ.get("REVIEW_CONCURRENCY", DEFAULT_CONCURRENCY))
        if concurrency < 1:
            raise ValueError("REVIEW_CONCURRENCY must be at least 1.")
    except ValueError as exc:
        log.error(f"Requirement review is misconfigured: {exc}")
        return 2

    from common.utils.settings import aws_client
    from data_embeddings_storage.database.connection import close_db
    from search.database_searching.agents import build_deps, review_requirement

    s3 = aws_client("s3", config=Config(max_pool_connections=_S3_WORKERS + concurrency))
    store = ReviewStore(s3, Helper.get_property("temp_bucket_name"), request_key)

    async def execute():
        manifest = await asyncio.to_thread(store.load_manifest)
        deps = build_deps(table_name)
        try:
            return await run_review(
                manifest,
                store,
                lambda item: review_requirement(
                    item.requirement,
                    deps=deps,
                    sheet=manifest.requestKey,
                    item=item.itemNo,
                    legal_cite=item.legalCite,
                ),
                concurrency,
            )
        finally:
            await close_db()

    try:
        results = asyncio.run(execute())
    except (ReviewIncompleteError, ClientError) as exc:
        log.error(f"Requirement review for {request_key} failed: {exc}")
        return 1

    log.info(
        f"Requirement review for {request_key} wrote {len(results.legalRequirements)} result(s) "
        f"to {results_object_key(request_key)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
