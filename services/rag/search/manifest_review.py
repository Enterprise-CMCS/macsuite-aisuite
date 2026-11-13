import asyncio
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from botocore.config import Config
from botocore.exceptions import ClientError

from common.utils.manifest import (
    REQUEST_KEY_PATTERN,
    Manifest,
    RequirementResult,
    ReviewResults,
    manifest_object_key,
    results_object_key,
    review_item_prefix,
)
from common.utils.helper import Helper
from common.utils.logger import log
from data_embeddings_storage.database.embedding_versions import validate_versioned_table_name
from search.database_searching.review_models import page_number, simplify_doc_id

DEFAULT_CONCURRENCY = 4

MAX_FAILED_REVIEW_RATIO = 0.2
NOT_FOUND = "Not found"
PROGRESS_LOG_EVERY = 50
_S3_WORKERS = 16
_ITEM_KEY_RE = re.compile(r"(\d+)\.json$")


class ReviewIncompleteError(RuntimeError):
    pass


class ReviewStore:

    def __init__(self, s3, bucket, request_key):
        self._s3 = s3
        self._bucket = bucket
        self._request_key = request_key
        self._item_prefix = review_item_prefix(request_key)

    def _read(self, key):
        return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def _write(self, key, body):
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=body, ContentType="application/json")

    def load_manifest(self):
        return Manifest.model_validate_json(self._read(manifest_object_key(self._request_key)))

    def completed_items(self):
        
        paginator = self._s3.get_paginator("list_objects_v2")
        keys = [
            key
            for key in paginator.paginate(Bucket=self._bucket, Prefix=self._item_prefix).search("Contents[].Key")
            if key and _ITEM_KEY_RE.search(key)
        ]
        with ThreadPoolExecutor(max_workers=_S3_WORKERS) as pool:
            bodies = pool.map(self._read, keys)
            return {
                int(_ITEM_KEY_RE.search(key).group(1)): RequirementResult.model_validate_json(body)
                for key, body in zip(keys, bodies)
            }

    def save_item(self, index, result):
        self._write(f"{self._item_prefix}{index:06d}.json", result.model_dump_json())

    def save_results(self, results):
        self._write(results_object_key(self._request_key), results.model_dump_json(exclude_none=True))


def document_labels(manifest):
    """Maps the simplified file name the chunks carry back to MuleSoft's documentId."""
    return {simplify_doc_id(os.path.basename(doc.s3.path)): doc.documentId for doc in manifest.documents}


def _page_span(numbers):
    spans = []
    for number in numbers:
        previous = spans[-1][1] if spans else ""
        if number.isdigit() and previous.isdigit() and int(number) == int(previous) + 1:
            spans[-1][1] = number
        else:
            spans.append([number, number])
    listed = ", ".join(start if start == end else f"{start}-{end}" for start, end in spans)
    return f"{'Page' if len(numbers) == 1 else 'Pages'} {listed}"


def found_in(evidence, labels):
    """'Page 88', 'Pages 41-45' or 'Pages 3, 7'; prefixed by documentId when evidence spans several documents."""
    pages_by_document = {}
    for record in evidence:
        number = page_number(record.page, record.printed_page)
        if not number:
            continue
        doc_id = record.doc_id or ""
        label = labels.get(simplify_doc_id(os.path.basename(doc_id)), doc_id)
        # Ordered by position in the file: printed numbering (e.g. roman front matter) does not sort as text.
        pages_by_document.setdefault(label, {}).setdefault(number, record.page if record.page is not None else 0)

    if not pages_by_document:
        return NOT_FOUND
    spans = {
        label: _page_span(sorted(positions, key=positions.get))
        for label, positions in pages_by_document.items()
    }
    if len(spans) == 1:
        return next(iter(spans.values()))
    return "; ".join(f"{label}: {span}" if label else span for label, span in spans.items())


def _confidence(label):
    label = (label or "").strip().upper()
    return label if label in ("HIGH", "MEDIUM", "LOW") else "LOW"


def to_result(requirement, review, labels):
    """Map the two-stage agent review onto MuleSoft's output contract, citing verified evidence only."""
    evidence = [record for record in review.evidence if record.verified]
    failed = bool(review.error)

    reasoning = review.in_reviewer_terms(review.argument)
    if review.missing_information:
        reasoning = f"{reasoning}\n\nMissing information: {review.in_reviewer_terms(review.missing_information)}"
    rationale = review.in_reviewer_terms(review.decision_response)
    if failed and not rationale:
        rationale = "The automated review did not complete; this requirement needs manual review."

    return RequirementResult(
        **requirement.model_dump(),
        siRecommendation="MET" if review.status == "MET" and not failed else "UNCLEAR",
        confidence="LOW" if failed else _confidence(review.decision_confidence),
        foundIn=found_in(evidence, labels),
        citedText="\n\n".join(dict.fromkeys(record.quote.strip() for record in evidence if record.quote.strip())),
        rationale=rationale,
        reasoning=reasoning.strip(),
        error=review.error or None,
    )


async def run_review(manifest, store, review, concurrency=DEFAULT_CONCURRENCY):
    """Review every requirement not already checkpointed, then write the ordered results.

    ``review`` is ``async (LegalRequirement) -> RequirementReview``; it is injected so the
    runner can be exercised without Bedrock or Postgres.
    """
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
        # Failed reviews are not checkpointed, so a retried task reviews them again.
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


async def _review_manifest(store, table_name, concurrency):
    # Imported here: these modules read the database secret and create Bedrock clients on import.
    from data_embeddings_storage.database.connection import close_db
    from search.database_searching.agents import build_deps, review_requirement

    manifest = await asyncio.to_thread(store.load_manifest)
    deps = build_deps(table_name)

    async def review(requirement):
        return await review_requirement(
            requirement.requirement,
            deps=deps,
            sheet=manifest.requestKey,
            item=requirement.itemNo,
            legal_cite=requirement.legalCite,
        )

    try:
        return await run_review(manifest, store, review, concurrency)
    finally:
        await close_db()


def main():
    request_key = os.environ.get("REQUEST_KEY", "")
    try:
        if not re.fullmatch(REQUEST_KEY_PATTERN, request_key):
            raise ValueError(f"REQUEST_KEY '{request_key}' is missing or invalid.")
        table_name = validate_versioned_table_name(os.environ.get("EMBEDDINGS_TABLE_NAME"))
        concurrency = int(os.environ.get("REVIEW_CONCURRENCY", DEFAULT_CONCURRENCY))
        if concurrency < 1:
            raise ValueError("REVIEW_CONCURRENCY must be at least 1.")
    except ValueError as exc:
        log.error(f"Requirement review is misconfigured: {exc}")
        return 2

    from common.utils.settings import aws_client

    s3 = aws_client("s3", config=Config(max_pool_connections=_S3_WORKERS + concurrency))
    store = ReviewStore(s3, Helper.get_property("temp_bucket_name"), request_key)
    try:
        results = asyncio.run(_review_manifest(store, table_name, concurrency))
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
