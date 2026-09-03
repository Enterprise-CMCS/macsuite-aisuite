import math
import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from typing import Dict

from pydantic_ai import Agent, ModelRetry, RunContext, ToolDefinition, ToolFailed
from pydantic_ai.capabilities import ValidatedToolArgs
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.usage import RunUsage, UsageLimits

from common.utils.helper import Helper
from common.utils.logger import log
from search.database_searching.model_provider import (
    REVIEW_MODEL_SETTINGS,
    bedrock_hooks,
    bedrock_model,
    model_id,
)
from search.database_searching.review_agents import (
    adjudication_prompt,
    adjudicator_agent,
    challenge_prompt,
    challenger_agent,
)
from search.database_searching.review_models import (
    EvidenceRecord,
    RequirementAssessment,
    RequirementReview,
    page_label,
)
from search.database_searching.search import SearchEngine

TOP_K = 8
CANDIDATE_LIMIT = 40

MAX_SEARCHES = 3

MAX_CHUNK_CHARS = 1800

QUOTE_MATCH_RATIO = 0.85

QUOTE_MATCH_MIN_RUN = 8

ANALYST_PROMPT = """You are a CMS reviewer checking whether a state's Medicaid managed care contract \
satisfies one specific requirement from the Contract Review Tool.

You work only from retrieved contract text. You have no knowledge of this contract beyond what the \
search results contain, and federal regulations you happen to know do not tell you what this contract \
says.

Method:
1. Read the retrieved contract text you were given against the exact wording of the requirement.
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
- Every quote must be copied verbatim from the retrieved text. Do not stitch wording from two passages \
into one quote. The quote is how the passage is identified, so copy it exactly.
- Cite the operative provision, not a heading, table of contents line, glossary definition, or form.
- argument explains how the quoted wording satisfies or fails the requirement, in the reviewer's terms.
- A reviewer reads argument, missing_information and follow_up in a spreadsheet, so refer to the \
contract by the page or section name shown above each passage.
- For NOT MET and UNCLEAR, missing_information names the specific provision or language that would \
settle the question.
- confidence is calibrated: 0.9+ means the quoted text is unambiguous, around 0.5 means it is arguable, \
below 0.3 means you are reviewing on evidence too thin to rely on."""

QUESTION_PROMPT = """You answer questions about a state's Medicaid managed care contract from retrieved \
contract text only.

Always call search_contract before answering. Ground every claim in the retrieved text and quote the \
contract wording that supports it. Cite the document and page number shown above each passage so the \
reader can find it in the document.

If the retrieved text does not answer the question, say what is missing instead of filling the gap. Do \
not describe your searching - answer the question."""


@dataclass
class ContractDeps:
    search_engine: SearchEngine = field(default_factory=SearchEngine)
    chunks: Dict[int, dict] = field(default_factory=dict)
    top_k: int = TOP_K
    candidate_limit: int = CANDIDATE_LIMIT
    max_searches: int = MAX_SEARCHES
    searches: int = 0
    query_cache: Dict[str, list] = field(default_factory=dict)


def build_deps(table_name=None):
    return ContractDeps(search_engine=SearchEngine(table_name=table_name))


async def retrieve(deps, query):
    # A requirement review issues up to MAX_SEARCHES tool calls plus the seed
    # retrieval; the model sometimes repeats a query verbatim (e.g. the analyst
    # re-searching the requirement text it was already seeded with). Cache by
    # normalized query for the lifetime of one review so a repeat doesn't pay a
    # second Bedrock embedding call and DB round trip.
    cache_key = _normalize(query)
    cached = deps.query_cache.get(cache_key)
    if cached is not None:
        return cached

    results = await deps.search_engine.hybrid_search(
        query, limit=deps.top_k,
        dense_limit=deps.candidate_limit, lexical_limit=deps.candidate_limit)
    deps.query_cache[cache_key] = results
    return results


def record_chunks(deps, results):
    kept = []
    for result in results:
        chunk_id = result.get("id")
        if chunk_id is None:
            continue
        existing = deps.chunks.get(chunk_id)
        if existing is None:
            deps.chunks[chunk_id] = result
            kept.append(result)
        elif existing.get("retrieval_confidence") is None:
            existing["retrieval_confidence"] = result.get("retrieval_confidence")
            existing["distance"] = result.get("distance")
    return kept


def chunk_provenance(chunk):
    metadata = chunk.get("metadata") or {}
    return {
        "doc_id": metadata.get("doc_id") or metadata.get("doc_name") or "",
        "page": metadata.get("page"),
        "printed_page": (metadata.get("printed_page") or "").strip(),
    }


def format_evidence(chunks):
    """The retrieved text, each passage headed with where it came from.

    Only the page and the document, never the chunk id. The model used to be given
    the id to cite with and wrote it into its prose - "the contract text in chunk
    3566 states" - which means nothing to the reviewer reading the spreadsheet.
    A quote is enough to find the passage again, so the id is not sent at all.
    """
    if not chunks:
        return "No contract text was retrieved for this requirement."

    blocks = []
    for chunk in chunks:
        where = chunk_provenance(chunk)

        header = [page_label(where["page"], where["printed_page"]) or "page not recorded"]
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
    blocks = SequenceMatcher(None, needle, haystack, autojunk=False).get_matching_blocks()
    matched = sum(block.size for block in blocks if block.size >= QUOTE_MATCH_MIN_RUN)
    return matched / len(needle) if needle else 0.0


def quote_supported(quote, chunk_text):
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


def quoted_chunk(deps, quote):
    """The retrieved chunk this quote was copied from, or None if it was not.

    The model is not given chunk ids, so the quote is what ties its evidence back
    to a passage. Finding it here doubles as the check that the wording is really
    in the contract rather than something the model composed.
    """
    for chunk in deps.chunks.values():
        if quote_supported(quote, chunk.get("text")):
            return chunk
    return None


def evidence_records(deps, cited):
    records = []
    for item in cited:
        quote = item.quote.strip()
        chunk = quoted_chunk(deps, quote)
        if chunk is None:
            records.append(EvidenceRecord(quote=quote, verified=False))
            continue
        records.append(EvidenceRecord(
            quote=quote,
            chunk_id=chunk.get("id"),
            retrieval_confidence=chunk.get("retrieval_confidence"),
            verified=True,
            **chunk_provenance(chunk),
        ))
    return records


def blend_confidence(llm_confidence, retrieval):
    if llm_confidence is None:
        return None
    if retrieval is None:
        return round(float(llm_confidence), 4)
    return round(math.sqrt(max(0.0, float(llm_confidence)) * max(0.0, float(retrieval))), 4)


async def search_contract(context: RunContext[ContractDeps], query: str) -> str:
    """Search the contract for passages relevant to a query.

    Args:
        query: Wording the contract itself would use - a defined term, a statutory
            cite, a section name, or the operative phrase you expect to find.

    Returns the matching passages, each headed with its page number and source
    document. Quote a passage verbatim to cite it.
    """
    deps = context.deps
    results = await retrieve(deps, query)

    log.debug(f"search_contract() call {deps.searches} for '{query[:120]}' returned {len(results)} chunks")
    if not results:
        return f"Nothing in the contract matched '{query}'. Try different wording or a broader phrase."

    record_chunks(deps, results)
    return format_evidence(results)


review_hooks = Hooks()


@review_hooks.on.before_tool_execute(tools=["search_contract"])
async def charge_search_budget(context: RunContext[ContractDeps], *, call: ToolCallPart,
                               tool_def: ToolDefinition,
                               args: ValidatedToolArgs) -> ValidatedToolArgs:
    deps = context.deps
    if deps.searches >= deps.max_searches:
        log.debug(f"charge_search_budget() refused search {deps.searches + 1}, "
                  f"the limit is {deps.max_searches}")
        raise ToolFailed(
            f"Search budget for this requirement is used up after {deps.max_searches} searches. "
            "Decide from the text you already have, and return UNCLEAR if it does not settle it.")

    deps.searches += 1
    return args


@review_hooks.on.tool_execute_error(tools=["search_contract"])
async def retry_failed_search(context: RunContext[ContractDeps], *, call: ToolCallPart,
                              tool_def: ToolDefinition, args: ValidatedToolArgs,
                              error: Exception):
    query = str(args.get("query", ""))[:120]
    Helper.print_exception("search_contract", error, f"Retrieval failed for query '{query}'.")
    raise ModelRetry("The search backend failed. Try once more with a shorter query.")


analyst_agent = Agent(
    bedrock_model(),
    output_type=RequirementAssessment,
    deps_type=ContractDeps,
    system_prompt=ANALYST_PROMPT,
    model_settings=REVIEW_MODEL_SETTINGS,
    tools=[search_contract],
    capabilities=[review_hooks, bedrock_hooks],
    retries=3,
    name="analyst",
)

question_agent = Agent(
    bedrock_model(),
    deps_type=ContractDeps,
    system_prompt=QUESTION_PROMPT,
    model_settings=REVIEW_MODEL_SETTINGS,
    tools=[search_contract],
    capabilities=[review_hooks, bedrock_hooks],
    retries=3,
    name="question",
)


@analyst_agent.output_validator
def check_citations(context: RunContext[ContractDeps], assessment: RequirementAssessment):
    # A quote that cannot be found in the retrieved text is not retried, it is
    # recorded unverified, so the reviewer sees the row was flagged rather than
    # losing the review to a failed retry.
    if assessment.status == "MET" and not assessment.evidence:
        raise ModelRetry("MET needs at least one quote from the retrieved text. Quote it or return UNCLEAR.")

    return assessment


async def review_requirement(requirement, deps=None, sheet="", item="", legal_cite="",
                             row=None, challenge=True, usage=None):
    review = RequirementReview(
        requirement=requirement, sheet=sheet, item=item, legal_cite=legal_cite,
        row=row, model=model_id(),
    )
    if not (requirement or "").strip():
        review.error = "Empty requirement text."
        return review

    deps = replace(deps or build_deps(), chunks={}, searches=0, query_cache={})
    run_usage = usage if usage is not None else RunUsage()

    try:
        seed = await retrieve(deps, requirement)
        record_chunks(deps, seed)
        review.chunks_retrieved = len(deps.chunks)

        assessment = (await analyst_agent.run(
            f"Requirement to review:\n{requirement}\n\n"
            f"Retrieved contract text:\n{format_evidence(seed)}\n\n"
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
            pool = format_evidence(list(deps.chunks.values()))
            unverified = [record.quote for record in review.evidence if not record.verified]

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

        log.info(f"review_requirement() {sheet} item {item} = {review.status}, "
                 f"llm confidence {review.llm_confidence}, retrieval confidence {review.retrieval_confidence}, "
                 f"quotes verified {review.quotes_verified}, chunks {review.chunks_retrieved}")
        return review

    except Exception as lclEx:
        Helper.print_exception("review_requirement", lclEx,
                               f"Review failed for requirement '{requirement[:120]}'.")
        review.status = "UNCLEAR"
        review.error = f"{type(lclEx).__name__}: {lclEx}"
        review.argument = review.argument or "The automated review did not complete for this requirement."
        return review


async def answer_question(query, deps=None, usage=None):
    deps = replace(deps or build_deps(), chunks={}, searches=0, query_cache={})
    result = await question_agent.run(query, deps=deps, usage=usage if usage is not None else RunUsage())
    return result.output
