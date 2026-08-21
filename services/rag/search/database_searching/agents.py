import math
import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from typing import Dict

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.usage import RunUsage, UsageLimits

from common.utils.helper import Helper
from common.utils.logger import log
from search.database_searching.model_provider import REVIEW_MODEL_SETTINGS, bedrock_model, model_id
from search.database_searching.review_agents import (adjudication_prompt,adjudicator_agent,challenge_prompt,challenger_agent,)
from search.database_searching.review_models import (EvidenceRecord,RequirementAssessment,RequirementReview,)
from search.database_searching.search import SearchEngine
from search.database_searching.toc_index import TableOfContents, load_table_of_contents

# Chunks are ~1k characters, so eight of them is a comfortable reading window for
# one requirement without burying the model in near-duplicates.
TOP_K = 8
CANDIDATE_LIMIT = 40

# The analyst gets a couple of follow-up searches for requirements that name a
# provision it has to go and find. More than that and it is usually rephrasing.
MAX_SEARCHES = 3

MAX_CHUNK_CHARS = 1800

# A quote has to reproduce this much of itself inside the chunk it was attributed
# to. Below one, because models normalise punctuation and whitespace when quoting.
QUOTE_MATCH_RATIO = 0.85

# Matching runs shorter than this are thrown away before the ratio is worked out.
# Without a floor, a fabricated sentence scores well on nothing but "the", "of"
# and "must" turning up somewhere in the chunk.
QUOTE_MATCH_MIN_RUN = 8

ANALYST_PROMPT = """You are a CMS reviewer checking whether a state's Medicaid managed care contract \
satisfies one specific requirement from the Contract Review Tool.

You work only from retrieved contract text. You have no knowledge of this contract beyond what the \
search results contain, and federal regulations you happen to know do not tell you what this contract \
says.

Method:
1. Read the retrieved chunks you were given against the exact wording of the requirement.
2. If they do not cover the requirement, call search_contract with the language the contract itself \
would use - the defined term, the statutory cite, the section name - rather than repeating the \
requirement verbatim. A requirement about "provider directory update frequency" is more likely to be \
found by searching "provider directory shall be updated" than by searching the requirement text.
3. Commit to a status and quote the wording that decides it.

Status definitions:
- MET: the retrieved text explicitly establishes what the requirement demands, at the strength it \
demands. A requirement that the contract "must require" something is not met by text that permits it.
- NOT MET: the retrieved text covers this subject and shows the requirement is not satisfied, or \
contradicts it.
- UNCLEAR: the retrieved text does not settle it either way. This includes retrieval coming back with \
nothing relevant. UNCLEAR is the honest answer far more often than reviewers like, and it is much less \
damaging than a wrong MET.

Rules:
- Every quote must be copied verbatim from a chunk and carry that chunk's id. Do not stitch wording \
from two chunks into one quote.
- Cite the operative provision, not a heading, table of contents line, glossary definition, or form.
- argument explains how the quoted wording satisfies or fails the requirement, in the reviewer's terms.
- For NOT MET and UNCLEAR, missing_information names the specific provision or language that would \
settle the question.
- confidence is calibrated: 0.9+ means the quoted text is unambiguous, around 0.5 means it is arguable, \
below 0.3 means you are reviewing on evidence too thin to rely on."""

QUESTION_PROMPT = """You answer questions about a state's Medicaid managed care contract from retrieved \
contract text only.

Always call search_contract before answering. Ground every claim in the retrieved chunks and quote the \
contract wording that supports it. Cite the section and printed page shown in each chunk's header so the \
reader can find it in the document.

If the retrieved text does not answer the question, say what is missing instead of filling the gap. Do \
not describe your searching - answer the question."""


@dataclass
class ContractDeps:
    """Shared retrieval services plus the evidence pool for the run in progress."""

    search_engine: SearchEngine = field(default_factory=SearchEngine)
    toc: TableOfContents = field(default_factory=load_table_of_contents)
    chunks: Dict[int, dict] = field(default_factory=dict)
    top_k: int = TOP_K
    candidate_limit: int = CANDIDATE_LIMIT
    max_searches: int = MAX_SEARCHES
    searches: int = 0
    rerank: bool = False


def build_deps(table_name=None, rerank=False):
    return ContractDeps(search_engine=SearchEngine(table_name=table_name), rerank=rerank)


async def retrieve(deps, query):

    if deps.rerank:
        return await deps.search_engine.reranked_search(
            query, limit=deps.top_k, candidate_limit=deps.candidate_limit)
    return await deps.search_engine.hybrid_search(
        query, limit=deps.top_k,
        dense_limit=deps.candidate_limit, lexical_limit=deps.candidate_limit)



def record_chunks(deps, results):
    """Remember every chunk we showed a model, keyed by the id it will cite."""
    kept = []
    for result in results:
        chunk_id = result.get("id")
        if chunk_id is None:
            continue
        existing = deps.chunks.get(chunk_id)
        if existing is None:
            deps.chunks[chunk_id] = result
            kept.append(result)
        else:
            # A chunk found twice keeps its best distance from either search.
            if existing.get("retrieval_confidence") is None:
                existing["retrieval_confidence"] = result.get("retrieval_confidence")
                existing["distance"] = result.get("distance")
    return kept


def chunk_provenance(deps, chunk):
    metadata = chunk.get("metadata") or {}
    # Text and table chunks store doc_id, image chunks store doc_name. Both are the
    # PDF's file name without its extension, which is what the contents index keys on.
    doc_id = metadata.get("doc_id") or metadata.get("doc_name") or ""
    page = metadata.get("page")
    entry = deps.toc.resolve(doc_id, page) or {}
    return {
        "doc_id": doc_id,
        "page": page,
        "printed_page": entry.get("printed_page") or deps.toc.printed_page(doc_id, page),
        "toc_path": entry.get("path", ""),
        "toc_title": entry.get("title", ""),
        "citation": deps.toc.citation(doc_id, page),
    }


def format_evidence(deps, chunks):
    """Lay the chunks out for a model, header first so citations stay grounded."""
    if not chunks:
        return "No contract text was retrieved for this requirement."

    blocks = []
    for chunk in chunks:
        where = chunk_provenance(deps, chunk)
        section = " ".join(part for part in (where["toc_path"], where["toc_title"]) if part).strip()

        # Deliberately no retrieval confidence here. It is a cosine distance, and a
        # model shown one tends to hand it straight back as its own confidence,
        # which leaves the two scores in the workbook saying the same thing twice.
        header = [f"[chunk {chunk.get('id')}]"]
        if section:
            header.append(f"section: {section}")
        if where["printed_page"]:
            header.append(f"printed page: {where['printed_page']}")
        if where["doc_id"]:
            header.append(f"document: {where['doc_id']}")

        text = (chunk.get("text") or "").strip()
        if len(text) > MAX_CHUNK_CHARS:
            text = text[:MAX_CHUNK_CHARS].rsplit(" ", 1)[0] + " ..."
        blocks.append(f"{' | '.join(header)}\n{text}")

    return "\n\n".join(blocks)


def _normalize(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())).strip()


def _match_ratio(needle, haystack):
    """How much of the needle turns up in the haystack, in order.

    Summed over every long enough matching run rather than taken from the single
    longest one, because BDA leaves list markers like "**a.**" sitting in the
    middle of a sentence and a model quoting that sentence sensibly drops them.
    One artefact in the middle of an otherwise verbatim quote used to halve the
    longest run and fail the check.
    """
    blocks = SequenceMatcher(None, needle, haystack, autojunk=False).get_matching_blocks()
    matched = sum(block.size for block in blocks if block.size >= QUOTE_MATCH_MIN_RUN)
    return matched / len(needle) if needle else 0.0


def quote_supported(quote, chunk_text):
    """Is this quote really in that chunk?

    Compared on normalised text because models tidy up punctuation, hyphenation
    and line breaks when they quote. Quotes joined by an ellipsis are checked
    fragment by fragment, since that is a legitimate way to quote a long clause.
    """
    haystack = _normalize(chunk_text)
    if not haystack:
        return False

    fragments = [part for part in re.split(r"\.{3}|…", quote or "") if len(_normalize(part)) >= 20]
    for fragment in fragments or [quote]:
        needle = _normalize(fragment)
        if not needle:
            return False
        if needle in haystack:
            continue
        if _match_ratio(needle, haystack) < QUOTE_MATCH_RATIO:
            return False
    return True


def evidence_records(deps, cited):
    """Turn the model's citations into rows with provenance we resolved ourselves."""
    records = []
    for item in cited:
        chunk = deps.chunks.get(item.chunk_id)
        if chunk is None:
            records.append(EvidenceRecord(quote=item.quote, chunk_id=item.chunk_id, verified=False))
            continue
        where = chunk_provenance(deps, chunk)
        records.append(EvidenceRecord(
            quote=item.quote.strip(),
            chunk_id=item.chunk_id,
            retrieval_confidence=chunk.get("retrieval_confidence"),
            verified=quote_supported(item.quote, chunk.get("text")),
            **where,
        ))
    return records


def blend_confidence(llm_confidence, retrieval):
    """Geometric mean, so a confident reading of weak retrieval cannot score high."""
    if llm_confidence is None:
        return None
    if retrieval is None:
        return round(float(llm_confidence), 4)
    return round(math.sqrt(max(0.0, float(llm_confidence)) * max(0.0, float(retrieval))), 4)


# ---------------------------------------------------------------------------
# Tools and agents
# ---------------------------------------------------------------------------

async def search_contract(context: RunContext[ContractDeps], query: str) -> str:
    """Search the contract for passages relevant to a query.

    Args:
        query: Wording the contract itself would use - a defined term, a statutory
            cite, a section name, or the operative phrase you expect to find.

    Returns the matching chunks, each headed with its id, contents section, and
    printed page. Cite chunks by the id in the header.
    """
    deps = context.deps
    if deps.searches >= deps.max_searches:
        return ("Search budget for this requirement is used up. Decide from the chunks you already "
                "have, and return UNCLEAR if they do not settle it.")

    deps.searches += 1
    try:
        results = await retrieve(deps, query)
    except Exception as lclEx:
        Helper.print_exception("search_contract", lclEx, f"Retrieval failed for query '{query[:120]}'.")
        raise ModelRetry("The search backend failed. Try once more with a shorter query.")

    log.debug(f"search_contract() call {deps.searches} for '{query[:120]}' returned {len(results)} chunks")
    if not results:
        return f"Nothing in the contract matched '{query}'. Try different wording or a broader phrase."

    record_chunks(deps, results)
    return format_evidence(deps, results)


analyst_agent = Agent(
    bedrock_model(),
    output_type=RequirementAssessment,
    deps_type=ContractDeps,
    system_prompt=ANALYST_PROMPT,
    model_settings=REVIEW_MODEL_SETTINGS,
    tools=[search_contract],
    retries=3,
    name="analyst",
)

question_agent = Agent(
    bedrock_model(),
    deps_type=ContractDeps,
    system_prompt=QUESTION_PROMPT,
    model_settings=REVIEW_MODEL_SETTINGS,
    tools=[search_contract],
    retries=3,
    name="question",
)


@analyst_agent.output_validator
def check_citations(context: RunContext[ContractDeps], assessment: RequirementAssessment):
    """Reject citations to chunks that were never retrieved.

    A fabricated chunk id is the tell that the model is reasoning from memory
    rather than from the document, and it is cheaper to retry than to explain.
    """
    unknown = [item.chunk_id for item in assessment.evidence if item.chunk_id not in context.deps.chunks]
    if unknown:
        available = ", ".join(str(chunk_id) for chunk_id in sorted(context.deps.chunks))
        raise ModelRetry(
            f"Chunk ids {unknown} were not in the search results. Cite only these ids: {available}.")

    if assessment.status == "MET" and not assessment.evidence:
        raise ModelRetry("MET needs at least one quote from the retrieved text. Quote it or return UNCLEAR.")

    return assessment


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def review_requirement(requirement, deps=None, sheet="", item="", legal_cite="",
                             row=None, challenge=True, usage=None):
    """Run one requirement through retrieval, assessment, challenge, and adjudication."""
    review = RequirementReview(
        requirement=requirement, sheet=sheet, item=item, legal_cite=legal_cite,
        row=row, model=model_id(),
    )
    if not (requirement or "").strip():
        review.error = "Empty requirement text."
        return review

    # A fresh evidence pool per requirement, sharing the search engine and the
    # contents index so we are not rebuilding boto3 clients 600 times.
    deps = replace(deps or build_deps(), chunks={}, searches=0)
    run_usage = usage if usage is not None else RunUsage()

    try:
        seed = await retrieve(deps, requirement)
        record_chunks(deps, seed)
        review.chunks_retrieved = len(deps.chunks)

        assessment = (await analyst_agent.run(
            f"Requirement to review:\n{requirement}\n\n"
            f"Retrieved contract text:\n{format_evidence(deps, seed)}\n\n"
            "Assess this requirement. Search again first if this text does not cover it.",
            deps=deps,
            usage=run_usage,
            usage_limits=UsageLimits(request_limit=4 + deps.max_searches),
        )).output

        review.chunks_retrieved = len(deps.chunks)
        review.evidence = evidence_records(deps, assessment.evidence)
        review.missing_information = assessment.missing_information
        review.status = assessment.status
        review.argument = assessment.argument
        review.llm_confidence = assessment.confidence

        if challenge:
            # Both later agents argue over the whole pool, including anything the
            # analyst went and found, so neither side has evidence the other lacks.
            pool = format_evidence(deps, list(deps.chunks.values()))
            unverified = [record.chunk_id for record in review.evidence if not record.verified]

            objection = (await challenger_agent.run(
                challenge_prompt(requirement, pool, assessment), usage=run_usage)).output

            verdict = (await adjudicator_agent.run(
                adjudication_prompt(requirement, pool, assessment, objection, unverified),
                usage=run_usage)).output

            review.status = verdict.status
            review.argument = verdict.argument
            review.counter_argument = verdict.counter_argument
            review.follow_up = verdict.follow_up
            review.llm_confidence = verdict.confidence

            if verdict.status != assessment.status:
                log.info(f"review_requirement() {sheet} item {item}: the challenge changed the call from "
                         f"{assessment.status} to {verdict.status}")

        # Normally the strength of the chunks the verdict actually rests on. With
        # nothing cited - which is the usual shape of an UNCLEAR - it falls back to
        # the pool, so the column says how close retrieval got rather than nothing.
        cited = [record.retrieval_confidence for record in review.evidence
                 if record.retrieval_confidence is not None]
        if not cited:
            cited = [chunk.get("retrieval_confidence") for chunk in deps.chunks.values()
                     if chunk.get("retrieval_confidence") is not None]
        review.retrieval_confidence = max(cited) if cited else None
        review.combined_confidence = blend_confidence(review.llm_confidence, review.retrieval_confidence)
        review.quotes_verified = bool(review.evidence) and all(
            record.verified for record in review.evidence)
        review.sources = review.where_found()
        review.toc_sections = " | ".join(sorted({
            " ".join(part for part in (record.toc_path, record.toc_title) if part).strip()
            for record in review.evidence
            if record.toc_path or record.toc_title
        }))

        log.info(f"review_requirement() {sheet} item {item} = {review.status}, "
                 f"llm confidence {review.llm_confidence}, retrieval confidence {review.retrieval_confidence}, "
                 f"quotes verified {review.quotes_verified}, chunks {review.chunks_retrieved}")
        return review

    except Exception as lclEx:
        # One bad row must not end a 600-row run. The error lands in the workbook
        # so the reviewer can see which requirements still need a pass.
        Helper.print_exception("review_requirement", lclEx,
                               f"Review failed for requirement '{requirement[:120]}'.")
        review.status = "UNCLEAR"
        review.error = f"{type(lclEx).__name__}: {lclEx}"
        review.argument = review.argument or "The automated review did not complete for this requirement."
        return review


async def answer_question(query, deps=None, usage=None):
    """Free-text question answering for the query API."""
    deps = replace(deps or build_deps(), chunks={}, searches=0)
    result = await question_agent.run(query, deps=deps, usage=usage if usage is not None else RunUsage())
    return result.output
