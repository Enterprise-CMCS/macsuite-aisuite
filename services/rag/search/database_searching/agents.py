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
from search.database_searching.search import FederatedSearchEngine, SearchEngine

TOP_K = 8
CANDIDATE_LIMIT = 40

# Reference-material passages (42 CFR Part 438 / CMS State Guide) retrieved per
# search on top of the contract's own TOP_K, never instead of them.
REFERENCE_TOP_K = 4

MAX_SEARCHES = 3

# How many times check_citations will bounce a REFERENCE MATERIAL citation back
# to the model before resolving it programmatically. Uncapped, a regulation-heavy
# requirement (one whose wording tracks a specific CFR cite closely) can spend the
# whole analyst run re-quoting the regulation instead of the contract, burning
# through the shared request budget until the run aborts with UsageLimitExceeded
# and the review is lost entirely.
MAX_CITATION_RETRIES = 2

MAX_CHUNK_CHARS = 1800

QUOTE_MATCH_RATIO = 0.85

QUOTE_MATCH_MIN_RUN = 8

ANALYST_PROMPT = """You are a CMS reviewer checking whether a state's Medicaid managed care contract \
satisfies one specific requirement from the Contract Review Tool.

You work only from retrieved contract text. You have no knowledge of this contract beyond what the \
search results contain, and federal regulations you happen to know do not tell you what this contract \
says.

Retrieved text comes in two sections. REFERENCE MATERIAL (42 CFR Part 438, CMS's State Guide) comes \
first - read it to understand what the requirement means and what CMS expects. THE STATE'S CONTRACT \
comes second - that section is the only thing a status can rest on. Never quote the reference section as \
evidence the requirement is met - if the contract section has nothing on point, that is NOT MET or \
Unsure regardless of how clearly the regulation states the rule.

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
- Unsure: the retrieved text does not settle it either way. This includes retrieval coming back with \
nothing relevant. Unsure is the honest answer far more often than reviewers like, and it is much less \
damaging than a wrong MET.

Rules:
- Every quote must be copied verbatim from the retrieved text. Do not stitch wording from two passages \
into one quote. The quote is how the passage is identified, so copy it exactly.
- Cite the operative provision, not a heading, table of contents line, glossary definition, or form.
- argument explains how the quoted wording satisfies or fails the requirement, in the reviewer's terms.
- A reviewer reads argument, missing_information and follow_up in a spreadsheet, so refer to the \
contract by the page or section name shown above each passage.
- For NOT MET and Unsure, missing_information names the specific provision or language that would \
settle the question.
- confidence is calibrated: 0.9+ means the quoted text is unambiguous, around 0.5 means it is arguable, \
below 0.3 means you are reviewing on evidence too thin to rely on."""

QUESTION_PROMPT = """You answer questions about a state's Medicaid managed care contract from retrieved \
contract text only.

Always call search_contract before answering. Ground every claim in the retrieved text and quote the \
contract wording that supports it. Cite the document and page number shown above each passage so the \
reader can find it in the document.

Retrieved text comes in two sections. REFERENCE MATERIAL (42 CFR Part 438, CMS's State Guide) comes \
first and explains what a requirement means or what CMS expects. THE STATE'S CONTRACT comes second. Say \
plainly when you are drawing on the reference section rather than the contract, and never present \
reference material as the contract's own wording.

If the retrieved text does not answer the question, say what is missing instead of filling the gap. Do \
not describe your searching - answer the question."""


@dataclass
class ContractDeps:
    search_engine: SearchEngine = field(default_factory=lambda: build_search_engine())
    chunks: Dict[int, dict] = field(default_factory=dict)
    top_k: int = TOP_K
    candidate_limit: int = CANDIDATE_LIMIT
    max_searches: int = MAX_SEARCHES
    searches: int = 0
    citation_retries: int = 0


def build_search_engine(table_name=None):
    """The active contract's table, federated with the common table when one is
    configured and isn't already the table being asked for (e.g. while the common
    section itself is the active contract, during its own ingest).

    The common table is labeled "common" end to end (see chunk_provenance,
    format_evidence, check_citations) so retrieved federal regulation and CMS
    guidance never gets treated as if it were the state's own contract text."""
    primary = table_name or Helper.get_embeddings_table_name()
    common = Helper.get_common_embeddings_table_name()
    if common and common != primary:
        return FederatedSearchEngine([(primary, "contract"), (common, "common")],
                                     reference_limit=REFERENCE_TOP_K)
    return SearchEngine(table_name=primary)


def build_deps(table_name=None):
    return ContractDeps(search_engine=build_search_engine(table_name=table_name))


async def retrieve(deps, query):
    return await deps.search_engine.hybrid_search(
        query, limit=deps.top_k,
        dense_limit=deps.candidate_limit, lexical_limit=deps.candidate_limit)


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
        # "contract" unless the chunk came off the federated common-knowledge-base
        # table (see search.FederatedSearchEngine) - untagged results are always
        # the state's own contract, since only the common table gets labeled.
        "source": chunk.get("source", "contract"),
    }


def _chunk_block(chunk):
    where = chunk_provenance(chunk)

    header = [page_label(where["page"], where["printed_page"]) or "page not recorded"]
    if where["doc_id"]:
        header.append(f"document: {where['doc_id']}")

    text = (chunk.get("text") or "").strip()
    if len(text) > MAX_CHUNK_CHARS:
        text = text[:MAX_CHUNK_CHARS].rsplit(" ", 1)[0] + " ..."
    return f"{' | '.join(header)}\n{text}"


def format_evidence(chunks):
    """The retrieved text, grouped into two sections rather than interleaved by
    rank: regulatory reference material first, the contract itself second - so
    the model reads what a requirement means before it reads what the contract
    says, the same order a human reviewer would work in.

    Only the page and the document head each passage, never the chunk id. The
    model used to be given the id to cite with and wrote it into its prose -
    "the contract text in chunk 3566 states" - which means nothing to the
    reviewer reading the spreadsheet. A quote is enough to find the passage
    again, so the id is not sent at all.
    """
    if not chunks:
        return "No contract text was retrieved for this requirement."

    reference = [chunk for chunk in chunks if chunk_provenance(chunk)["source"] == "common"]
    contract = [chunk for chunk in chunks if chunk_provenance(chunk)["source"] != "common"]

    sections = []
    if reference:
        sections.append(
            "=== REFERENCE MATERIAL (42 CFR Part 438 / CMS State Guide) - read this first for what the "
            "requirement means and what CMS expects. This is never the contract and can never by itself "
            "support a status. ===\n\n" + "\n\n".join(_chunk_block(chunk) for chunk in reference)
        )
    if contract:
        sections.append(
            "=== THE STATE'S CONTRACT - check whether this satisfies the requirement. Only text in this "
            "section can support a status. ===\n\n" + "\n\n".join(_chunk_block(chunk) for chunk in contract)
        )
    return "\n\n".join(sections)


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

    Contract chunks are searched first. State contracts often copy 42 CFR 438
    word for word, and a quote the contract itself carries must be credited to
    the contract, not to whichever copy happened to be retrieved first.
    """
    chunks = sorted(deps.chunks.values(), key=lambda chunk: chunk_provenance(chunk)["source"] == "common")
    for chunk in chunks:
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
            "Decide from the text you already have, and return Unsure if it does not settle it.")

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
    # check_citations' reference-material retries draw on the same output budget
    # as schema-validation retries, so they get their own headroom on top of it -
    # otherwise two citation retries leave one for everything else and a schema
    # slip after them loses the review to UnexpectedModelBehavior.
    retries={"tools": 3, "output": 3 + MAX_CITATION_RETRIES},
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


def _evidence_source(deps, item):
    chunk = quoted_chunk(deps, item.quote.strip())
    return chunk_provenance(chunk)["source"] if chunk is not None else None


@analyst_agent.output_validator
def check_citations(context: RunContext[ContractDeps], assessment: RequirementAssessment):
    deps = context.deps

    # A quote that cannot be found in the retrieved text is not retried, it is
    # recorded unverified, so the reviewer sees the row was flagged rather than
    # losing the review to a failed retry.
    if assessment.status == "MET" and not assessment.evidence:
        raise ModelRetry("MET needs at least one quote from the retrieved text. Quote it or return Unsure.")

    # A quote that only matches a chunk from the REFERENCE MATERIAL section is
    # regulation or CMS guidance, not the contract.
    reference_only = [item for item in assessment.evidence if _evidence_source(deps, item) == "common"]
    if not reference_only:
        return assessment

    if deps.citation_retries < MAX_CITATION_RETRIES:
        deps.citation_retries += 1
        raise ModelRetry(
            f"The quote \"{reference_only[0].quote[:120]}\" is from the REFERENCE MATERIAL section (42 "
            "CFR Part 438 or the CMS State Guide), not THE STATE'S CONTRACT section. Only the contract's "
            "wording can support a status - quote the matching contract provision instead, or return "
            "NOT MET / Unsure if the contract has none."
        )

    # Retried MAX_CITATION_RETRIES times and it still reaches for the regulation
    # instead of the contract - resolved here rather than raised again, since
    # another retry risks spending the rest of the shared request budget and
    # losing the review to UsageLimitExceeded instead of landing on an answer.
    kept = [item for item in assessment.evidence if item not in reference_only]
    return assessment.model_copy(update={
        "evidence": kept,
        "status": assessment.status if kept else "Unsure",
        "missing_information": assessment.missing_information or (
            "Retrieval surfaced only the federal regulation / CMS guidance on this point, not "
            "corresponding language in the contract itself."
        ),
    })


async def review_requirement(requirement, deps=None, sheet="", item="", legal_cite="",
                             row=None, challenge=True, usage=None):
    review = RequirementReview(
        requirement=requirement, sheet=sheet, item=item, legal_cite=legal_cite,
        row=row, model=model_id(),
    )
    if not (requirement or "").strip():
        review.error = "Empty requirement text."
        return review

    deps = replace(deps or build_deps(), chunks={}, searches=0, citation_retries=0)
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
            usage_limits=UsageLimits(request_limit=4 + deps.max_searches + MAX_CITATION_RETRIES),
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

            supported = any(record.verified and record.source != "common" for record in review.evidence)
            if review.status == "MET" and not supported:
                # review.evidence is the analyst's quotes, which may be unverified
                # (check_citations does not reject a quote it cannot find) - and
                # the adjudicator's argument is free text, nothing in it checked
                # against the retrieved chunks. A MET with no quote found in the
                # contract's own text would otherwise land in the workbook with
                # nothing a reviewer could check it against.
                log.info(f"review_requirement() {sheet} item {item}: challenge called MET with no "
                         "verified evidence behind it - downgraded to Unsure")
                review.status = "Unsure"
                review.follow_up = review.follow_up or (
                    "The reviewers called this MET, but not from a quote verified against the retrieved "
                    "contract text - confirm this directly against the contract."
                )

        cited = [record.retrieval_confidence for record in review.evidence
                 if record.retrieval_confidence is not None]
        if not cited:
            # Contract chunks only - reference material never supports a status,
            # so it must not lend the row its confidence either.
            cited = [chunk.get("retrieval_confidence") for chunk in deps.chunks.values()
                     if chunk.get("retrieval_confidence") is not None
                     and chunk_provenance(chunk)["source"] != "common"]
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
        review.status = "Unsure"
        review.error = f"{type(lclEx).__name__}: {lclEx}"
        review.argument = review.argument or "The automated review did not complete for this requirement."
        return review


async def answer_question(query, deps=None, usage=None):
    deps = replace(deps or build_deps(), chunks={}, searches=0)
    result = await question_agent.run(query, deps=deps, usage=usage if usage is not None else RunUsage())
    return result.output
