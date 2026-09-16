import json
import re
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Dict

from pydantic_ai import Agent, ModelRetry, RunContext, ToolDefinition, ToolFailed
from pydantic_ai.capabilities import ValidatedToolArgs
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.usage import RunUsage, UsageLimits

from common.utils.helper import Helper
from common.utils.logger import log
from search.database_searching.model_provider import (
    bedrock_hooks,
    bedrock_model,
    model_id,
)
from search.database_searching.review_models import RequirementReview, page_number
from search.database_searching.search import SearchEngine


MAX_SEARCHES = 3

AGENT_SYSTEM_PROMPT = """You are an expert contract analysis assistant. Your task is to verify whether specific contractual requirements are supported by the provided retrieved text.

You must base all conclusions ONLY on the retrieved context. Do not use outside knowledge or assumptions.

IMPORTANT RULES:
- Never describe the search process or retrieval steps.
- Do not explain how the information was found.
- Only present the conclusion and supporting evidence.
- Return all relevant pages
- Everytime explict evidence in the form of a quote is returned, always have page number with it
- Always include specific sources and page. Never return "Hybrid Search Results" for source.
- Do NOT return a header or footer as source, always refer to the citation or metadata
- Always return the document name as source
- Never include internal labels like "[result 1]" or "[result 2]" in your Response or in any quote. Quote only the actual contract wording.
- Never truncate a quote with an ellipsis (...). Every quote must be a complete sentence or clause as it appears in the source text.

ANALYSIS TASK:
For each requirement provided by the user, analyze whether the contract text explicitly supports the requirement and provide a recommendation.

RECOMMENDATION DEFINITIONS:
MET:
The retrieved text explicitly states the requirement is met.
NOT MET:
The retrieved text shows that the requirement is not met.
UNCLEAR:
The retrieved text does not provide enough explicit evidence to determine whether the requirement is met.

Return output in the following JSON format exactly:

{
    "Recommendation": "MET | NOT MET | UNCLEAR",
    "Response": "<detailed explanation with evidence and reasoning. Include quotes if helpful>"
}


ADDITIONAL GUIDELINES:
- Prefer direct quotes from the contract when possible in your Response.
- Keep evidence excerpts focused and precise.
- If multiple relevant excerpts exist, include them in your Response.
- If no evidence exists, state that clearly in your Response and return UNCLEAR as Recommendation.
- Provide detailed reasoning in the Response field, explaining why you made this Recommendation."""


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
    metadata = chunk.get("metadata")
    return {
        "doc_id": metadata.get("doc_id"),
        "page": metadata.get("page"),
        "printed_page": (metadata.get("printed_page") or "").strip()
    }


def format_retrieved_chunks(chunks):
    blocks = []
    for i, chunk in enumerate(chunks, 1):
        text = chunk.get("text")
        if not text or not text.strip():
            raise ValueError(f"Retrieved chunk {i} (id={chunk.get('id')}) has no text")
        blocks.append(f"[result {i}]\n{text.strip()}")

    return "\n\n".join(blocks)


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


def top_citation(deps):
    """The single highest-confidence retrieved chunk, for the one-line citation display."""
    scored = [chunk for chunk in deps.chunks.values() if chunk.get("retrieval_confidence") is not None]
    return max(scored, key=lambda chunk: chunk["retrieval_confidence"]) if scored else None


def format_review_display(review, deps):
    chunk = top_citation(deps)
    where = chunk_provenance(chunk) if chunk else {"doc_id": "", "page": None, "printed_page": ""}
    page = page_number(where["page"], where["printed_page"]) if chunk else ""
    quote = first_quote(review.argument)

    lines = [
        f"AI recommended status: {review.status.title()}",
        "",
        f"AI response: {review.argument}",
        "",
        f"Page number: {page}",
        "",
        f"Confidence: {confidence_label(review.retrieval_confidence)}",
    ]
    if where["doc_id"] or quote:
        lines += ["", "Cited contract text:"]
        if where["doc_id"]:
            lines.append(f"{where['doc_id']}, page {page}")
        if quote:
            lines.append(f'"{quote}"')

    return "\n".join(lines)


async def search_contract(context: RunContext[ContractDeps], query: str) -> str:

    deps = context.deps
    results = await retrieve(deps, query)

    log.debug(f"search_contract() call {deps.searches} for '{query[:120]}' returned {len(results)} chunks")
    if not results:
        return f"Nothing in the contract matched '{query}'. Try different wording or a broader phrase."

    record_chunks(deps, results)
    return format_retrieved_chunks(results)


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


async def review_requirement(requirement, deps=None, sheet="", item="", legal_cite="",
                             row=None, challenge=True, usage=None):

    review = RequirementReview(
        requirement=requirement, sheet=sheet, item=item, legal_cite=legal_cite,
        row=row, model=model_id(),
    )
    if not (requirement or "").strip():
        review.error = "Empty requirement text."
        return review

    deps = replace(deps or build_deps(), chunks={}, searches=0)
    run_usage = usage if usage is not None else RunUsage()

    try:
        seed = await retrieve(deps, requirement)
        record_chunks(deps, seed)
        review.chunks_retrieved = len(deps.chunks)

        result = await contract_agent.run(
            f"Requirement to verify:\n{requirement}\n\n"
            "Retrieved contract context (each [result i] includes text and metadata):\n"
            f"{format_retrieved_chunks(seed)}\n\n"
            "Now perform the analysis as specified in the system prompt and return a single JSON object.",
            deps=deps,
            usage=run_usage,
            usage_limits=UsageLimits(request_limit=4 + deps.max_searches),
        )
        review.chunks_retrieved = len(deps.chunks)

        try:
            parsed = extract_json_object(result.output)
        except (json.JSONDecodeError, ValueError, TypeError) as parse_err:
            log.warning(f"review_requirement() {sheet} item {item}: model output was not valid JSON: {parse_err}")
            review.status = "UNCLEAR"
            review.argument = f"The model output could not be parsed as JSON. Raw output was:\n{result.output}"
            return review

        review.status = parsed.get("Recommendation", "UNCLEAR")
        review.argument = strip_result_tags(parsed.get("Response", ""))

        cited = [chunk.get("retrieval_confidence") for chunk in deps.chunks.values()
                 if chunk.get("retrieval_confidence") is not None]
        review.retrieval_confidence = max(cited) if cited else None
        review.combined_confidence = review.retrieval_confidence
        review.sources = chunks_sources(deps.chunks.values())

        log.info(f"review_requirement() {sheet} item {item} = {review.status}, "
                 f"retrieval confidence {review.retrieval_confidence}, "
                 f"chunks {review.chunks_retrieved}")
        log.info("review_requirement() display:\n" + format_review_display(review, deps))
        return review

    except Exception as lclEx:
        Helper.print_exception("review_requirement", lclEx,
                               f"Review failed for requirement '{requirement[:120]}'.")
        review.status = "UNCLEAR"
        review.error = f"{type(lclEx).__name__}: {lclEx}"
        review.argument = review.argument or "The automated review did not complete for this requirement."
        return review


async def answer_question_formatted(query, deps=None, usage=None):

    deps = replace(deps or build_deps(), chunks={}, searches=0)
    run_usage = usage if usage is not None else RunUsage()

    seed = await retrieve(deps, query)
    record_chunks(deps, seed)

    result = await contract_agent.run(
        f"Question:\n{query}\n\n"
        "Retrieved contract context (each [result i] includes text and metadata):\n"
        f"{format_retrieved_chunks(seed)}\n\n"
        "Now perform the analysis as specified in the system prompt and return a single JSON object.",
        deps=deps,
        usage=run_usage,
        usage_limits=UsageLimits(request_limit=4 + deps.max_searches),
    )

    try:
        parsed = extract_json_object(result.output)
    except (json.JSONDecodeError, ValueError, TypeError):
        log.warning("answer_question_formatted() model output was not valid JSON, returning it as-is")
        return result.output

    cited = [chunk.get("retrieval_confidence") for chunk in deps.chunks.values()
             if chunk.get("retrieval_confidence") is not None]

    review = SimpleNamespace(
        status=parsed.get("Recommendation", "UNCLEAR"),
        argument=strip_result_tags(parsed.get("Response", "")),
        retrieval_confidence=max(cited) if cited else None,
    )
    return format_review_display(review, deps)
