import json
from dataclasses import dataclass, field
import os
import re
from typing import List, Dict, Any, Optional
import hashlib

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.bedrock import BedrockConverseModel

from search.database_searching.search import SearchEngine
from common.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------- Constants ----------------

MAX_CACHE_SIZE = 1000
MAX_TEXT_LENGTH = 2000          
# SEARCH_LIMIT = 32               
# FINAL_RESULTS = 8 

SEARCH_LIMIT = 30  
FINAL_RESULTS = 10


CLAUSE_HINTS = {
    "termination": [
        "termination",
        "term and termination",
        "termination for convenience",
        "termination for cause",
    ],
    "indemnity": ["indemnity", "indemnification"],
    "liability": ["limitation of liability", "liability cap"],
    "confidentiality": ["confidentiality", "non-disclosure", "NDA"],
    "governing law": ["governing law", "jurisdiction"],
}  


# ---------------- Dependencies ----------------


@dataclass
class ChatDeps:
    """Dependencies for chat agent with optimized caching."""
    acronyms: dict
    timing: dict = field(default_factory=dict)
    search_engine: SearchEngine = field(default_factory=SearchEngine)
    _result_cache: dict = field(default_factory=dict)  # Cache for search results
    _cache_hits: int = 0
    _cache_misses: int = 0


# BASE_SYSTEM_PROMPT = """
# You are an expert RAG assistant specialized in providing accurate, evidence-based answers. Your responses must always be grounded in retrieved information from the knowledge base.
#
# CORE PRINCIPLES:
# - Always ground answers in retrieved information. Never speculate or make up information.
# - Choose the right search strategy based on query complexity and importance.
# - Synthesize information from multiple results when available.
# - Be transparent about limitations and confidence levels.
#
# SEARCH STRATEGY SELECTION:
#
# Use semantic_search for:
# - Conceptual or theoretical questions
# - Exploratory queries
# - When speed is prioritized
# - Simple, straightforward questions
#
# Use hybrid_search for:
# - Specific factual queries requiring exact terms
# - Queries mixing concepts and specific terminology
# - General purpose searches (default choice)
# - Balanced speed and accuracy needs
#
# Use reranked_search for:
# - Critical business decisions
# - Complex multi-faceted questions
# - When highest accuracy is essential
# - Queries requiring deep context understanding
#
# RESPONSE GUIDELINES:
# 1. Always search first - Never answer without retrieving information
# 2. Cite evidence - Reference specific results when making claims
# 3. Acknowledge gaps - If information is incomplete or missing, state it clearly
# 4. Synthesize clearly - Combine multiple sources into coherent answers
# 5. Be concise - Provide direct answers without unnecessary elaboration
# 6. Handle errors gracefully - If search fails, explain and suggest alternatives
#
# QUALITY STANDARDS:
# - Prioritize accuracy over speed
# - Use complete sentences and proper formatting
# - Avoid hedging language when evidence is clear
# - Be specific with facts, numbers, and details from retrieved results
# """

#ask for output in a list or JSON
BASE_SYSTEM_PROMPT = """You are an expert contract analysis assistant. Your task is to verify whether specific contractual requirements are supported by the provided retrieved text.

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
- Recommendation must be either MET or NOT MET.
- Never return UNCLEAR or any other recommendation.

ANALYSIS TASK:
For each requirement provided by the user, analyze whether the contract text explicitly supports the requirement and provide a recommendation.

RECOMMENDATION DEFINITIONS:
MET:
The retrieved text explicitly states the requirement is met.
NOT MET:
The retrieved text does not provide enough evidence to determine whether the requirement is met.

Return output in the following JSON format exactly:

{
    "Requirement": "<repeat the requirement text>",
    "Recommendation": "MET | NOT MET",
    "Reasoning": "<detailed explanation with evidence and reasoning. Include quotes if helpful>",
    "Source": "<Document name, Page 12 | Another document, Pages 15, 18>",
    "Page": "<Page number(s) only, for example: 12, 15, 18>"
}

ADDITIONAL GUIDELINES:
- Prefer direct quotes from the contract when possible in your Response.
- Keep evidence excerpts focused and precise.
- If multiple relevant excerpts exist, include them in your Response.
- If no sufficient evidence exists, state that clearly in the Response and return NOT MET as the Recommendation.
- Provide detailed reasoning in the Response field, explaining why you made this Recommendation.

CITATION FORMAT:
- In the Source field, always include document name and page number together.
- Use this exact format for one page:
  "Document Name, Page 12"
- Use this exact format for multiple pages:
  "Document Name, Pages 12, 15, 18"
- For multiple documents, separate each document with a pipe:
  "Document A, Pages 12, 15 | Document B, Page 8"
- Do not use colon format like "Document Name: 12, 15".
- Do not put page numbers without the document name.
- Do not repeat the same page numbers twice.
- The Page field should contain only the page number list, without document names.
"""


model_id = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")
model = BedrockConverseModel(model_id)

search_agent = Agent(
    model,
    deps_type=ChatDeps,
    system_prompt=BASE_SYSTEM_PROMPT,
    retries=5,
    model_settings={"temperature": 0.0, "top_p": 1.0},
)


def _normalize_query(query: str) -> str:
    query = query.lower().strip()
    query = re.sub(r"\s+", " ", query)
    query = re.sub(r"[^\w\s.,?!-]", "", query)
    return query


def _expand_query(query: str) -> str:

    q = query.lower()
    expansions = []
    for key, hints in CLAUSE_HINTS.items():
        if key in q:
            expansions.extend(hints)
    if not expansions:
        return query
    return query + " " + " ".join(sorted(set(expansions)))


def generate_cache_key(query: str, search_type: str) -> str:
    normalized = _normalize_query(query)
    return hashlib.md5(f"{search_type}:{normalized}".encode()).hexdigest()


def _manage_cache_size(cache: dict, max_size: int = MAX_CACHE_SIZE) -> None:
    if len(cache) > max_size:
        remove_count = max_size // 5
        for _ in range(remove_count):
            cache.popitem()
        logger.info(f"Cache pruned: removed {remove_count} old entries")


def _calculate_relevance_score(result: Dict[str, Any], query: str) -> float:

    text = (result.get("text") or "").lower()
    query_lower = query.lower()

    # Base similarity from pgvector distance
    distance = float(result.get("distance", 1.0))
    # Clamp to [0, 2] then map to [0,1]; if your setup guarantees [0,1], this still works.
    distance = max(0.0, min(distance, 2.0))
    similarity = 1.0 - (distance / 2.0)
    score = similarity

    query_terms = [t for t in query_lower.split() if len(t) > 2]
    term_matches = sum(1 for term in query_terms if term in text)
    if query_terms:
        score += (term_matches / len(query_terms)) * 0.2

    if query_lower in text:
        score += 0.1

    return max(0.0, min(score, 1.0))  


def deduplicate_results(
    results: List[Dict[str, Any]], query: str = ""
) -> List[Dict[str, Any]]:
    if not results:
        return results

    seen = set()
    deduplicated = []

    for result in results:
        text = (result.get("text") or "").strip()
        metadata = result.get("metadata", {})

        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}

        doc_id = metadata.get("doc_id") or metadata.get("doc_name") or ""
        page = metadata.get("page", "none")

        unique_key = hashlib.md5(f"{doc_id}:{page}:{text[:200]}".encode()).hexdigest()

        if unique_key not in seen:
            seen.add(unique_key)
            if query:
                result["_relevance_score"] = _calculate_relevance_score(result, query)
            deduplicated.append(result)

    if query and deduplicated:
        deduplicated.sort(key=lambda x: x.get("_relevance_score", 0.0), reverse=True)

    removed = len(results) - len(deduplicated)
    if removed > 0:
        logger.info(f"Removed {removed} duplicates, ranked by relevance")

    return deduplicated


def _parse_metadata(metadata: Any) -> Dict[str, Any]:
    if isinstance(metadata, str):
        try:
            return json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            return {}
    return metadata if isinstance(metadata, dict) else {}


def _truncate_text(text: str, max_length: int = MAX_TEXT_LENGTH) -> str:
    if len(text) <= max_length:
        return text

    cutoff = max_length
    candidates = []
    for sep in ["\n\n", "\n", ". "]:
        idx = text.rfind(sep, 0, cutoff)
        if idx != -1 and idx > max_length * 0.6:
            candidates.append(idx + len(sep))

    if candidates:
        return text[: max(candidates)].strip() + "..."

    return text[:max_length].rsplit(" ", 1)[0] + "..."


@search_agent.tool
async def semantic_search(context: RunContext[ChatDeps], query: str) -> List[Dict[str, Any]]:

    try:
        normalized_query = _normalize_query(query)
        expanded_query = _expand_query(normalized_query)
        cache_key = generate_cache_key(expanded_query, "semantic_raw")

        if cache_key in context.deps._result_cache:
            context.deps._cache_hits += 1
            hits = context.deps._cache_hits
            misses = context.deps._cache_misses
            cache_ratio = hits / (hits + misses)
            logger.info(f"[SEMANTIC] Cache hit (ratio: {cache_ratio:.2%})")
            return context.deps._result_cache[cache_key]

        context.deps._cache_misses += 1
        logger.info(f"[SEMANTIC] Query: {query}")
        logger.debug(f"[SEMANTIC] Expanded query: {expanded_query}")


        results = await context.deps.search_engine.semantic_search(query_text=expanded_query,limit=SEARCH_LIMIT,)
         # Do not return an empty list to the Bedrock tool call.
        if not results:
            logger.warning("[SEMANTIC] No results found for query: %s", expanded_query)
 
            no_results = [
                {
                    "id": None,
                    "text": ("No relevant supporting evidence was found using semantic search."),
                    "metadata": {},
                    "retrieval_method": "semantic",
                    "semantic_score": 0.0,
                    "semantic_distance": 1.0,
                }
            ]
 
            context.deps._result_cache[cache_key] = no_results
            return no_results
 
        # Remove duplicate content .
        results = deduplicate_results(results)
        results = results[:FINAL_RESULTS]
 
        final_results = []
 
        for result in results:
            final_results.append(
                {
                    "id": result.get("id"),
                    "text": _truncate_text((result.get("text") or "").strip()),
                    "metadata": _parse_metadata(result.get("metadata", {})),
                    "retrieval_method": "semantic",
                    "semantic_score": float(result.get("similarity") or 0.0),
                    "semantic_distance": float(result.get("distance") or 0.0),
                }
            )
 
        context.deps._result_cache[cache_key] = final_results
        _manage_cache_size(context.deps._result_cache)
 
        logger.info("[SEMANTIC] Returning %d results.",len(final_results),)
 
        return final_results
 
    except Exception as exc:
        logger.error("[SEMANTIC] Search failed: %s",exc,exc_info=True,)
 
        return [
            {
                "id": None,
                "text": ("The semantic search could not be completed because of a retrieval error."),
                "metadata": {},
                "retrieval_method": "semantic",
                "semantic_score": 0.0,
                "semantic_distance": 1.0,
            }
        ]


async def analyze_requirement_with_rag(
    requirement: str,
    deps: Optional[ChatDeps] = None,
) -> Dict[str, Any]:

    deps = deps or ChatDeps(acronyms={})

    # 1. Retrieve context
    context_results = await semantic_search(
        RunContext(deps=deps, state={}, tools=search_agent.tools),
        query=requirement,
    )

    # made changes to the output format to include the document name and page number in the response, 
    # and to handle cases where no relevant evidence is found
    if not context_results: 
        return {
            "Requirement": requirement, 
            "Recommendation": "NOT MET", 
            "Response": ( "No relevant supporting evidence was found " " in the retrieved documents." ),
            "Source": "", "Page": "", 
            }

    formatted_results = []
    for i, r in enumerate(context_results, 1):
        md = r.get("metadata", {})
        doc_name = md.get("doc_name") or md.get("doc_id") or "Unknown document"
        page = md.get("page", "unknown")
        clause_label = md.get("clause_label") or md.get("section_title") or ""
        header = f"[result {i}] Document: {doc_name} | Page: {page}"
        if clause_label:
            header += f" | Clause: {clause_label}"
        chunk_text = r.get("text", "")
        formatted_results.append(f"{header}\n{chunk_text}")

    retrieval_block = (
        "\n\n".join(formatted_results) if formatted_results else "No relevant text found."
    )

    user_message = (
        "Requirement to verify:\n"
        f"{requirement}\n\n"
        "Retrieved contract context (each [result i] includes text and metadata):\n"
        f"{retrieval_block}\n\n"
        "Now perform the analysis as specified in the system prompt and return a single JSON object."
    )

    result = await search_agent.run(user_message, deps=deps)

    try:
        parsed = json.loads(result.output_text)
        return parsed
    except Exception:
    
        return {
            "Requirement": requirement,
            "Recommendation": "NOT MET",
            "Response": (
                "The model output could not be parsed as JSON. "
                "Raw output was:\n" + result.output_text
            ),
            "Source": "",
            "Page": "",
        }


