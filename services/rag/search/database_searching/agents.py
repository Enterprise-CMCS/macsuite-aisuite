import json
import re
from dataclasses import dataclass, field, replace
from typing import Dict

from pydantic_ai import Agent, ModelRetry, RunContext, ToolDefinition, ToolFailed
from pydantic_ai.capabilities import ValidatedToolArgs
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.usage import RunUsage, UsageLimits

from common.utils.helper import Helper
from common.utils.logger import log
from search.database_searching.model_provider import (bedrock_hooks,bedrock_model,model_id,)
from search.database_searching.review_models import (EvidenceRecord,RequirementReview,page_number,)
from search.database_searching.search import SearchEngine


MAX_SEARCHES = 3

AGENT_SYSTEM_PROMPT = """You are an expert contract analysis assistant. Your task is to determine whether a specific contractual requirement is supported by the retrieved contract text.
Base every conclusion ONLY on the retrieved contract context. Do not use outside knowledge, assumptions, or information that is not explicitly supported by the retrieved text.
 
RECOMMENDATION DEFINITIONS:

MET:
The retrieved contract text explicitly provides sufficient evidence that the requirement is satisfied.
NOT MET:
The retrieved contract text explicitly provides sufficient evidence that the requirement is not satisfied.
UNCLEAR:
The retrieved contract text does not provide enough explicit evidence to determine whether the requirement is satisfied.
 
EVIDENCE RULES:
- Use only retrieved contract text.
- Every retrieved passage is identified by [chunk N].
- Every supporting citation must reference an actual retrieved chunk ID.
- Never invent a chunk ID.
- Return every passage materially needed to support the recommendation in Evidence.
- For each evidence item, return the chunk_id and an exact verbatim quote from that chunk.
- Quotes must be copied exactly from the retrieved contract text.
- Never paraphrase evidence quotes.
- Never truncate a quote with an ellipsis (...).
- Do not use headers or footers as evidence unless they themselves contain the contractual provision being evaluated.
- If the retrieved text does not contain sufficient supporting evidence, return an empty Evidence list when appropriate.
- Do not generate document names or page numbers. Python derives document and page information from the cited chunk metadata.
 
RESPONSE RULES:
- Response contains reasoning only.
- Explain why the retrieved evidence supports MET, NOT MET, or UNCLEAR.
- Keep the explanation concise and focused, preferably 2-4 sentences.
- Do not repeat the entire requirement.
- Do not include document names in Response.
- Do not include page numbers in Response.
- Do not include chunk IDs in Response.
- Do not include citation references in Response.
- Do not include verbatim contract quotes in Response.
- For MET, explain what contractual provision or obligation satisfies the requirement.
- For NOT MET, explain what material requirement the contract explicitly fails to satisfy.
- For UNCLEAR, explain specifically what required fact, threshold, obligation, or provision cannot be established from the retrieved text.
 
Return exactly one JSON object in this format:
{
    "Recommendation": "MET | NOT MET | UNCLEAR",
    "Response": "<concise reasoning only>",
    "Evidence": [
        {
            "chunk_id": 1234,
            "quote": "<exact verbatim contract wording from chunk 1234>"
        }
    ]
}
Do not return Source or Page fields. Python derives document names and page numbers from the Evidence chunk metadata.
"""

@dataclass
class ContractDeps:
    search_engine: SearchEngine = field(default_factory=SearchEngine)
    chunks: Dict[int, dict] = field(default_factory=dict)
    max_searches: int = MAX_SEARCHES
    searches: int = 0

def build_deps(table_name=None):
    return ContractDeps(search_engine=SearchEngine(table_name=table_name))

async def retrieve(deps, query):
    return await deps.search_engine.hybrid_search(query)

# CHECK CHUNKS
def record_chunks(deps, results):
    kept = []
    for result in results:
        chunk_id = result.get("id")
        if chunk_id is None:
            continue
        try:
            chunk_id = int(chunk_id)
        except (TypeError, ValueError):
            continue
        result["id"] = chunk_id
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
        "doc_id": metadata.get("doc_id"),
        "page": metadata.get("page"),
        "printed_page": (metadata.get("printed_page") or "").strip()
    }

def format_retrieved_chunks(chunks):
    blocks = []
    for chunk in chunks:
        text = chunk.get("text")
        chunk_id = chunk.get("id")
        metadata = chunk.get("metadata") or {}
        if not text or not text.strip():
            raise ValueError(f"Retrieved chunk (id={chunk_id}) has no text")
        doc_id = metadata.get("doc_id", "")
        page = page_number(metadata.get("page"),metadata.get("printed_page", ""),)
        blocks.append(
            f"[chunk {chunk_id}]\n"
            f"Document: {doc_id}\n"
            f"Page: {page}\n"
            f"Text:\n{text.strip()}")

    return "\n\n".join(blocks)

# MODEL OUTPUT HELPERS 
def extract_json_object(text):
    """The first {...} JSON object in the text, tolerating a <thinking>...</thinking>
    preamble or other stray text the model sometimes emits before the JSON.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in model output")
    return json.loads(text[start:end + 1])

def chunks_sources(chunks):
    pages_by_doc = {}
    for chunk in chunks:
        where = chunk_provenance(chunk)
        doc_id = where["doc_id"]
        if not doc_id:
            raise ValueError(f"Retrieved chunk (id={chunk.get('id')}) has no doc_id in its metadata")
        number = page_number(where["page"], where["printed_page"])
        if not number:
            continue
        pages = pages_by_doc.setdefault(doc_id, [])
        if number not in pages:
            pages.append(number)

    return " | ".join(f"{doc}: {', '.join(pages)}" for doc, pages in pages_by_doc.items())


def confidence_label(score):
    """A qualitative read of retrieval_confidence, since a bare 0-1 float means
    little to a reviewer skimming a spreadsheet."""
    if score is None:
        return "No evidence"
    if score >= 0.7:
        return "Strong evidence"
    if score >= 0.4:
        return "Moderate evidence"
    return "Weak evidence"


FIRST_QUOTE = re.compile(r'"([^"\n]{15,400})"')
FIRST_SINGLE_QUOTE = re.compile(r"'([^'\n]{15,400})'")
LEADING_RESULT_TAG = re.compile(r"^\[result\s+\d+\]\s*")
RESULT_TAG = re.compile(r"\[result\s+\d+\]\s*", re.IGNORECASE)


def strip_result_tags(text):
    """Safety net for the model echoing our internal '[result N]' context labels
    back into its Response text despite being told not to."""
    return RESULT_TAG.sub("", text or "")

def first_quote(text):
    text = text or ""
    match = FIRST_QUOTE.search(text) or FIRST_SINGLE_QUOTE.search(text)
    if not match:
        return ""
    return LEADING_RESULT_TAG.sub("", match.group(1).strip())

def normalize_text(text):
    return re.sub(r"\s+", " ", text or "").strip()

def top_citation(deps):
    """The single highest-confidence retrieved chunk, for the one-line citation display."""
    scored = [chunk for chunk in deps.chunks.values() if chunk.get("retrieval_confidence") is not None]
    return max(scored, key=lambda chunk: chunk["retrieval_confidence"]) if scored else None

#DISPLAY
def format_review_display(review, deps):
    lines = [
        f"AI recommended status: {review.status.title()}",
        "",
        f"AI response: {review.in_reviewer_terms(review.argument)}",
        "",
        f"Page number: {review.page_numbers()}",
        "",
        f"Confidence: {confidence_label(review.retrieval_confidence)}",
    ]
    verified = [record for record in review.evidence if record.verified]
    if verified:
        lines += ["", "Cited contract text:"]
 
        for record in verified:
            page = page_number(record.page, record.printed_page,)
            lines += [f"{record.doc_id}, page {page}", f'"{record.quote}"', "",]
    else:
        lines += ["","Note: No verified cited contract text was found for this status.",]

    return "\n".join(lines).rstrip()

#SEARCH TOOL
async def search_contract(context: RunContext[ContractDeps], query: str) -> str:

    deps = context.deps
    results = await retrieve(deps, query)

    log.debug(f"search_contract() call {deps.searches} for '{query[:120]}' returned {len(results)} chunks")
    if not results:
        return f"Nothing in the contract matched '{query}'. Try different wording or a broader phrase."

    record_chunks(deps, results)
    return format_retrieved_chunks(results)

#TOOL HOOKS
review_hooks = Hooks()
@review_hooks.on.before_tool_execute(tools=["search_contract"])
async def charge_search_budget(context: RunContext[ContractDeps], *, call: ToolCallPart,
        tool_def: ToolDefinition,args: ValidatedToolArgs) -> ValidatedToolArgs:
    deps = context.deps
    if deps.searches >= deps.max_searches:
        log.debug(f"charge_search_budget() refused search {deps.searches + 1}, the limit is {deps.max_searches}")
        raise ToolFailed(f"Search budget for this requirement is used up after {deps.max_searches} searches. "
            "Decide from the text you already have, and return UNCLEAR if it does not settle it.")
    deps.searches += 1
    return args


@review_hooks.on.tool_execute_error(tools=["search_contract"])
async def retry_failed_search(context: RunContext[ContractDeps], *, call: ToolCallPart,
    tool_def: ToolDefinition, args: ValidatedToolArgs,error: Exception):
    query = str(args.get("query", ""))[:120]
    Helper.print_exception("search_contract", error, f"Retrieval failed for query '{query}'.")
    raise ModelRetry("The search backend failed. Try once more with a shorter query.")

#AGENT
contract_agent = Agent(
    bedrock_model(),
    deps_type=ContractDeps,
    system_prompt=AGENT_SYSTEM_PROMPT,
    model_settings={"temperature": 0.0, "top_p": 1.0},
    tools=[search_contract],
    capabilities=[review_hooks, bedrock_hooks],
    retries=3,
    name="contract_agent",
)

# REQUIREMENT REVIEW
async def review_requirement(requirement, deps=None, sheet="", item="", legal_cite="",
                             row=None, usage=None):

    review = RequirementReview(requirement=requirement, sheet=sheet, item=item, legal_cite=legal_cite,
        row=row, model=model_id(),
    )
    if not (requirement or "").strip():
        review.error = "Empty requirement text."
        return review

    deps = replace(deps or build_deps(), chunks={}, searches=0)
    run_usage = usage if usage is not None else RunUsage()

    try:
        #Initial retrieval
        seed = await retrieve(deps, requirement)
        record_chunks(deps, seed)
        review.chunks_retrieved = len(deps.chunks)

        #Agent analysis
        result = await contract_agent.run(f"Requirement to verify:\n{requirement}\n\n"
            "Retrieved contract context(each passage is identified by a persistent [chunk N] ID):\n"
            f"{format_retrieved_chunks(seed)}\n\n"
            "Analyze the requirement using only the retrieved contract context. "
            "If additional evidence is needed, use search_contract. "
            "Return exactly one JSON object containing Recommendation, Response, and Evidence.",
            deps=deps,
            usage=run_usage,
            usage_limits=UsageLimits(request_limit=4 + deps.max_searches),
        )
        review.chunks_retrieved = len(deps.chunks)

        #Parse Model Json
        try:
            parsed = extract_json_object(result.output)
        except (json.JSONDecodeError, ValueError, TypeError) as parse_err:
            log.warning(f"review_requirement() {sheet} item {item}: model output was not valid JSON: {parse_err}")
            review.status = "UNCLEAR"
            review.argument = f"The model output could not be parsed as JSON. Raw output was:\n{result.output}"
            return review

        # Recommendation
        status = str(parsed.get("Recommendation", "UNCLEAR")).strip().upper()
        review.status = (status if status in {"MET", "NOT MET", "UNCLEAR"} else "UNCLEAR")
        review.argument = strip_result_tags(parsed.get("Response", ""))
 
        # Evidence
        review.evidence = []
        evidence_items = parsed.get("Evidence", [])
 
        if not isinstance(evidence_items, list):
            evidence_items = []
 
        for evidence_item in evidence_items:
            if not isinstance(evidence_item, dict):
                continue
            try:
                chunk_id = int(evidence_item.get("chunk_id"))
            except (TypeError, ValueError):
                continue
 
            quote = (evidence_item.get("quote") or "").strip()
            chunk = deps.chunks.get(chunk_id)
 
            # Ignore invented/missing chunk IDs.
            if not chunk or not quote:
                continue
            text = chunk.get("text") or ""
            metadata = chunk.get("metadata") or {}
 
            # Normalize whitespace before quote verification.
            normalized_quote = normalize_text(quote)
            normalized_text = normalize_text(text)
 
            verified = (normalized_quote in normalized_text)
 
            review.evidence.append(EvidenceRecord(
                    quote=quote,
                    chunk_id=chunk_id,
                    doc_id=metadata.get("doc_id","",),
                    page=metadata.get("page"),
                    printed_page=metadata.get("printed_page","",),
                    retrieval_confidence=chunk.get("retrieval_confidence"),
                    verified=verified,))
 
        # Quote verification
        review.quotes_verified = (bool(review.evidence) and all(record.verified for record in review.evidence))
 
        # Confidence
        # Only VERIFIED CITED evidence contributes to the final retrieval
        # confidence. A high-scoring retrieved chunk that was not actually
        # used as evidence does not determine the final confidence.

        verified_cited = [record.retrieval_confidence for record in review.evidence
            if (record.verified and record.retrieval_confidence is not None)]
 
        review.retrieval_confidence = (max(verified_cited) if verified_cited else None)

        # is combined_confidence and retrieval confidence same?
        review.combined_confidence = (review.retrieval_confidence)
 
        # Sources now represent evidence actually cited, rather than every retrieved chunk.
        review.sources = review.where_found()
 
        # LOGGING
        log.info(
            f"review_requirement() {sheet} item {item} "
            f"= {review.status}, "
            f"retrieval confidence {review.retrieval_confidence}, "
            f"chunks {review.chunks_retrieved}")
 
        log.info("review_requirement() display:\n" + format_review_display(review, deps))
        return review

    except Exception as lclEx:
        Helper.print_exception("review_requirement", lclEx, f"Review failed for requirement '{requirement[:120]}'.")
        review.status = "UNCLEAR"
        review.error = f"{type(lclEx).__name__}: {lclEx}"
        review.argument = review.argument or "The automated review did not complete for this requirement."
        return review


# FORMATTED QUESTION
async def answer_question_formatted(query,deps=None,usage=None,):
 
    deps = replace(deps or build_deps(), chunks={}, searches=0,)
    run_usage = (usage if usage is not None else RunUsage())
    seed = await retrieve(deps, query)
    record_chunks(deps, seed)
 
    result = await contract_agent.run(f"Question:\n{query}\n\n"
        "Retrieved contract context (each passage is identified by a persistent [chunk N] ID):\n"
        f"{format_retrieved_chunks(seed)}\n\n"
        "Analyze the question using only the retrieved contract context. If additional evidence is needed, use search_contract. "
        "Return exactly one JSON object containing Recommendation, Response, and Evidence.",
        deps=deps, usage=run_usage,
        usage_limits=UsageLimits(request_limit=4 + deps.max_searches),)
 
    try:
        parsed = extract_json_object(result.output)
    except (json.JSONDecodeError, ValueError,TypeError,):
        log.warning("answer_question_formatted() model output was not valid JSON, returning it as-is")
 
        return result.output
 
    review = RequirementReview(
        requirement=query, status=(str(parsed.get("Recommendation","UNCLEAR",)).strip().upper()),
        argument=strip_result_tags(parsed.get("Response","",)),
        model=model_id(),)
 
    evidence_items = parsed.get("Evidence", [],)
 
    if not isinstance(evidence_items, list,):
        evidence_items = []

    for evidence_item in evidence_items:
        if not isinstance(evidence_item,dict,):
            continue
        try:
            chunk_id = int(evidence_item.get("chunk_id"))
        except (TypeError,ValueError,):
            continue
 
        quote = (evidence_item.get("quote") or "").strip()
        chunk = deps.chunks.get(chunk_id)

        if not chunk or not quote:
            continue
 
        text = chunk.get("text") or ""
        metadata = (chunk.get("metadata") or {})
 
        verified = (normalize_text(quote) in normalize_text(text))
 
        review.evidence.append(EvidenceRecord(
                quote=quote,chunk_id=chunk_id,
                doc_id=metadata.get("doc_id","",),
                page=metadata.get("page"),
                printed_page=metadata.get("printed_page","",),
                retrieval_confidence=chunk.get("retrieval_confidence"),
                verified=verified,))
 
    review.quotes_verified = (bool(review.evidence) and all(record.verified for record in review.evidence))
    verified_cited = [record.retrieval_confidence for record in review.evidence
        if (record.verified and record.retrieval_confidence is not None)]
    review.retrieval_confidence = (max(verified_cited) if verified_cited else None)
    review.combined_confidence = (review.retrieval_confidence)
    review.sources = (review.where_found())
 
    return format_review_display(review, deps,)
