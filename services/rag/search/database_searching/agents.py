import json
import re
 
from dataclasses import dataclass, field, replace
from typing import Dict, Optional

from pydantic_ai import Agent, ModelRetry, RunContext, ToolDefinition, ToolFailed
from pydantic_ai.capabilities import ValidatedToolArgs
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import ThinkingPart, ToolCallPart
from pydantic_ai.usage import RunUsage, UsageLimits
 
from common.utils.helper import Helper
from common.utils.logger import log
from search.database_searching.model_provider import (bedrock_hooks,bedrock_model,model_id,)
from search.database_searching.review_models import (EvidenceRecord,RequirementReview,page_number,)
from search.database_searching.search import SearchEngine
from difflib import SequenceMatcher
 


# ==================================CONFIGURATION============================
 
MAX_SEARCHES = 3
HIGH_CONFIDENCE_THRESHOLD = 0.70
MEDIUM_CONFIDENCE_THRESHOLD = 0.40

# Shared regulatory reference (42 CFR Part 438 + CMS State Guide).
# Used only as interpretive context for Stage 2, never as contract evidence.
MAX_REFERENCE_CHUNKS = 5
MAX_REFERENCE_CHARS = 6000

# Stage 2 decision confidence (HIGH/MEDIUM/LOW) sets the band shown in Status.
# Numeric retrieval confidence is passed to Stage 2 as an input and kept as a
# MET guardrail (>= 0.40), but is never shown in Status itself.
DECISION_CONFIDENCE_LABELS = {"HIGH", "MEDIUM", "LOW"}



# =====================STAGE 1 PROMPT: EVIDENCE ANALYSIS ONLY===================
 
EVIDENCE_SYSTEM_PROMPT = """
You are an expert contract evidence analyst.
 
Your task is to analyze a contractual requirement using ONLY retrieved
contract text and identify the evidence needed to evaluate the requirement.
 
You are STAGE 1 of a two-stage review process.
 
IMPORTANT:
You MUST NOT decide whether the requirement is MET, NOT MET, UNCLEAR,
or UNSURE.
 
A separate decision agent will make the final recommendation.
 
Your responsibilities are:

1. Analyze the requirement carefully.
2. Review the retrieved contract passages.
3. Search for additional contract text when needed.
4. Identify the strongest evidence relevant to the requirement.
5. Explain what the retrieved evidence establishes.
6. Identify any material information that remains missing.
7. Return exact contract quotations and their actual chunk IDs.
 
EVIDENCE RULES:
 
- Use ONLY retrieved contract text.
- Do not use outside knowledge.
- Do not make assumptions.
- Every retrieved passage is identified by [chunk N].
- Every evidence item must reference an actual retrieved chunk ID.
- Never invent a chunk ID.
- Quotes must be copied EXACTLY from the retrieved contract text.
- Never paraphrase evidence quotes.
- Never truncate quotes with ellipses (...).
- Return every passage materially needed for evaluating the requirement.
- Do not use headers or footers as evidence unless they contain the
  contractual provision being evaluated.
- Do not generate document names or page numbers.
- Python derives document and page information from chunk metadata.
- If the retrieved evidence is incomplete, clearly explain what material
  information is missing.
 
 
REASONING RULES:

The Reasoning field should explain what the evidence establishes.
The reasoning should:
 
- Be concise and focused.
- Preferably be 2-4 sentences.
- Explain the substantive meaning of the retrieved provisions.
- Identify relevant thresholds, obligations, conditions, exceptions, time periods, or other material facts.
- Explain material gaps when the evidence is incomplete.
- Do not state MET, or UNSURE.
- Do not include document names.
- Do not include page numbers.
- Do not include chunk IDs.
- Do not repeat verbatim evidence quotes in the Reasoning field.
 
 
MISSING INFORMATION:

If the evidence does not fully address the requirement, identify exactly
what is missing.
 
Examples:
- required threshold not established
- required time period not stated
- responsible party not identified
- obligation is incomplete
- exception is not addressed
- retrieved text discusses the subject but does not establish the
  required condition
 
If nothing material is missing, say nothing seems missing.
 
Return EXACTLY one JSON object:
 
{
    "Reasoning": "<concise evidence analysis>",
    "Evidence": [
        {
            "chunk_id": 1234,
            "quote": "<exact verbatim contract wording>"
        }
    ],
    "MissingInformation": "<material missing information or empty string>"
}
 
Do not return Recommendation.
Do not return Status.
Do not return MET.
Do not return UNSURE.
Do not return Source or Page fields.
"""
# ========================STAGE 2 PROMPT:FINAL DECISION ONLY======================================

DECISION_SYSTEM_PROMPT = """
You are the final contract requirement decision agent.
 
You are STAGE 2 of a two-stage contract review process.
 
Stage 1 has already:
 
- searched the contract,
- analyzed the retrieved evidence,
- identified relevant contract passages,
- provided exact quotations.
 
Python has independently:
 
- verified the quotations against the retrieved chunks,
- removed unsupported quotations,
- calculated retrieval confidence from verified cited evidence,
- mapped evidence to document and page information.
 
You MUST make the final recommendation using ONLY the information provided
to you.

You do NOT have access to contract search.

Do NOT invent new evidence.
Do NOT use outside knowledge beyond the RegulatoryReference provided.
Do NOT assume facts that are not established by the evidence.

==================================================
REGULATORY REFERENCE
==================================================
RegulatoryReference contains excerpts from 42 CFR Part 438 and the CMS
"State Guide to CMS Criteria for Medicaid Managed Care Contract Review and
Approval". ItemId and LegalCite identify the review item and the rule it
derives from.

Use RegulatoryReference ONLY to interpret the requirement:

- Determine the substantive obligation the review item is testing.
- Where the review item wording differs from the regulation text, the
  regulation governs. Do not mark UNSURE solely because the contract follows
  the regulation rather than the review item wording.
- Apply any State Guide tip that applies to this item number.
- Contract language that is substantively equivalent to the regulation
  satisfies it. Verbatim wording of the review item is NOT required.
- An explicit contract obligation to comply with the specific cited CFR
  section supports MET when nothing in the contract contradicts it.
- If the contract omits an element the regulation requires (for example a
  protected category, threshold or time period), return UNSURE and name the
  missing element.
- If the requirement appears not to apply (for example, a triggering
  condition such as the rural exception is not present, or the State rather
  than the plan performs the function), return UNSURE and begin Response
  with "Possibly N/A because ...".
- Regulatory text is NEVER evidence that the contract meets the requirement.
  MET still requires verified contract evidence.
- If RegulatoryReference is empty, decide from the requirement and the
  verified evidence alone.
- Only suggest asking the State about something that would change the
  decision.

APPLICABILITY CONDITIONS ARE NOT CONTRACT ELEMENTS:
- Phrases that describe WHEN a requirement applies -- for example "in
  states that limit disenrollment", "if specified by the federal authority",
  "if used in the payment arrangement", "for enrollees who qualify under the
  rural exception" -- describe the state's program, not language the
  contract must repeat. Do NOT return UNSURE only because the contract does
  not restate such a condition.
- If the contract contains the substantive obligation, treat the condition
  as satisfied or not applicable unless the evidence contradicts it.
- If the triggering condition clearly does not exist in this contract (for
  example the contract offers a choice of plans, so the rural exception does
  not apply, or the State rather than the plan performs the function),
  return UNSURE with HIGH confidence and begin Response with
  "Possibly N/A because ...".

WEIGH STAGE 1 GAPS, DO NOT COPY THEM:
- MissingInformation lists what Stage 1 could not find. For each gap,
  decide whether it is MATERIAL under the regulation. Ignore gaps that are
  applicability conditions, definitions of terms, or wording differences
  where the verified evidence already establishes the same obligation.
- Return UNSURE only for a material gap, and name it.

CHECK LISTS ELEMENT BY ELEMENT:
- When the regulation lists required items (protected categories, required
  handbook contents, required timeframes), compare each item against the
  verified contract text. Never state that the contract includes an item
  unless that word or an equivalent appears in VerifiedEvidence. For
  example, "gender" or "sexual orientation" in the contract is not the same
  as "sex" in the regulation; a missing item is a material gap.

==================================================
FINAL RECOMMENDATIONS
==================================================
 
You may return ONLY:
MET
or
UNSURE
 
MET:
Return MET when the verified evidence clearly establishes the substantive
requirement. 

Exact wording is not required.

Equivalent contractual language is acceptable when it clearly establishes
the same obligation, threshold, condition, responsibility, time period,
or substantive requirement.

Multiple verified passages may be considered together.

A MEDIUM retrieval-confidence score may still support MET when the verified
evidence clearly and sufficiently establishes the requirement.
 
UNSURE:

Return UNSURE when the provided evidence does not establish the complete
material requirement.
 
Examples include:
 
- evidence is missing,
- evidence is incomplete,
- evidence is ambiguous,
- evidence conflicts,
- a required threshold is missing,
- a required time period is missing,
- a required obligation is not established,
- a material condition is not addressed,
- Stage 1 identifies unresolved material information.
 
If a material part of the requirement cannot be established from the
verified evidence, return UNSURE.
 
==================================================
RETRIEVAL CONFIDENCE
==================================================
Retrieval confidence measures the strength/relevance of retrieved evidence.
It does NOT determine the recommendation by itself.
 
HIGH retrieval confidence does NOT automatically mean MET.
 
For example, highly relevant evidence may clearly discuss the requirement
but still fail to establish a required threshold. In that case, return
UNSURE.
 
MEDIUM retrieval confidence may support MET when the verified evidence
clearly establishes the complete requirement.
 
LOW RETRIEVAL CONFIDENCE RULE:
If retrieval confidence is below 0.40, or retrieval confidence is missing,
the final recommendation MUST be UNSURE.
MET is permitted ONLY when:
- retrieval confidence is at least 0.40, AND
- the verified evidence substantively establishes the complete requirement.
Therefore:
HIGH retrieval confidence (>= 0.70):
- MET if evidence establishes the requirement.
- UNSURE if evidence does not establish the requirement.
MEDIUM retrieval confidence (>= 0.40 and < 0.70):
- MET if evidence establishes the requirement.
- UNSURE if evidence does not establish the requirement.
LOW retrieval confidence (< 0.40 or missing):
- ALWAYS UNSURE.

Retrieval confidence is NOT shown to reviewers. Your DecisionConfidence is,
and it should take retrieval confidence into account (see below).
 
Your primary task is therefore:
 
1. Determine whether the verified evidence establishes the requirement.
2. Use retrieval confidence as supporting information.
3. Return MET only when the substantive requirement is established.
4. Otherwise return UNSURE.

==================================================
DECISION CONFIDENCE
==================================================
Rate how confident you are in YOUR recommendation. This is what reviewers
see in the Status column (for example "MET with Medium Confidence").

Set it by weighing these inputs together:
1. Stage 1 analysis: Stage1Reasoning (what the Evidence Analyst found the
   contract establishes), VerifiedEvidence (the Python-verified contract
   passages) and MissingInformation (what Stage 1 could not find).
2. Your regulatory judgment: apply RegulatoryReference (42 CFR Part 438 and
   the CMS State Guide) to decide how completely that verified evidence
   satisfies the requirement as the regulation defines it. This is the
   primary factor. You may disagree with Stage 1's reasoning when the
   regulation reads differently, but only on the basis of the verified
   evidence.
3. RetrievalConfidence / RetrievalConfidenceBand: how strongly the cited
   contract passages matched the requirement (a supporting factor).

How retrieval should affect your confidence:
- HIGH retrieval with complete, direct language supports HIGH.
- MEDIUM retrieval: be cautious about HIGH; prefer MEDIUM unless the
  language is direct and complete.
- LOW retrieval (< 0.40) means the evidence matched weakly: the
  recommendation must be UNSURE, and your confidence in it should reflect
  that the contract may contain text that was not retrieved.
- Retrieval never overrides regulatory judgment: a strong match that
  omits an element the regulation requires is still UNSURE.

Allowed combinations:

MET with HIGH:
Direct contract language clearly establishes every material element of the
requirement as interpreted under the regulation.

MET with MEDIUM:
Every material element is established, but only by reading equivalent,
indirect or cross-referenced contract language (for example an explicit
obligation to comply with the cited CFR section) as satisfying it.

MET with LOW is NOT allowed. If you would only be LOW confident in MET,
return UNSURE with LOW instead.

UNSURE with HIGH:
The evidence clearly does not establish a material element (it is missing,
contradicted, or narrower than the regulation), or the requirement clearly
does not apply ("Possibly N/A").

UNSURE with MEDIUM:
The requirement is partially addressed; a specific element remains
unestablished or needs confirmation from the State.

UNSURE with LOW:
A close call that could reasonably be MET; the evidence is ambiguous,
indirect or conflicting, or applicability is uncertain.

==================================================
RESPONSE
==================================================
Return a short final explanation (2-4 sentences) of why the evidence
supports the decision. When the regulation or a State Guide tip shaped the
decision, name it (for example "42 CFR 438.56(c)(2)(iii)").

Do not:
- invent evidence,
- cite new contract language,
- add new quotations,
- generate page numbers,
- generate document names,
- modify the retrieval confidence.


Return EXACTLY one JSON object:
{
    "Recommendation": "MET | UNSURE",
    "DecisionConfidence": "HIGH | MEDIUM | LOW",
    "Response": "<concise explanation of the final decision>"
}
"""
 

# ===============================DEPENDENCIES========================================
 
@dataclass
class ContractDeps:
    search_engine: SearchEngine = field(default_factory=SearchEngine)
    # Shared regulatory knowledge base (42 CFR Part 438 + CMS State Guide).
    # Kept separate from search_engine so regulatory text never becomes
    # contract evidence and chunk IDs from the two tables never collide.
    reference_engine: Optional[SearchEngine] = None
    chunks: Dict[int, dict] = field(default_factory=dict)
    max_searches: int = MAX_SEARCHES
    searches: int = 0


def build_deps(table_name=None):
    try:
        common_table = Helper.get_common_embeddings_table_name()
    except Exception as lclEx:
        log.warning(f"build_deps() could not resolve the shared regulatory table: {lclEx}")
        common_table = None

    if not common_table:
        log.info("build_deps() no shared regulatory table configured; Stage 2 runs without RegulatoryReference.")

    return ContractDeps(
        search_engine=SearchEngine(table_name=table_name),
        reference_engine=SearchEngine(table_name=common_table) if common_table else None,)


# ==================================== RETRIEVAL=========================================
async def retrieve(deps, query):
    return await deps.search_engine.semantic_search(query)


async def retrieve_reference(deps, requirement, item="", legal_cite=""):
    """
    Look up regulatory context for one requirement in the shared table.

    hybrid_search is used so the lexical half can match item IDs
    (e.g. I.B.2.01) and cites (e.g. 438.56(c)(2)(iii)). Results are NOT
    recorded in deps.chunks: they are interpretive context for Stage 2 only.
    """
    if deps.reference_engine is None:
        return []

    query = " ".join(part for part in (item, legal_cite, requirement) if part)
    try:
        results = await deps.reference_engine.hybrid_search(query, limit=MAX_REFERENCE_CHUNKS)
    except Exception as lclEx:
        Helper.print_exception("retrieve_reference", lclEx, f"Regulatory lookup failed for item '{item}'.")
        log.warning(f"retrieve_reference() lookup failed for item '{item}': {lclEx}")
        return []

    references, used = [], 0
    for result in results or []:
        text = (result.get("text") or "").strip()
        if not text or used + len(text) > MAX_REFERENCE_CHARS:
            continue
        metadata = get_metadata(result)
        references.append({
            "source": metadata.get("doc_id", ""),
            "page": page_number(metadata.get("page"), metadata.get("printed_page", "")),
            "text": text,})
        used += len(text)

    log.debug(f"retrieve_reference() item '{item}' returned {len(references)} reference passage(s)")
    return references

def record_chunks(deps, results):
    kept = []
    for result in results:
        print('result_keys:', result.keys())
        print('result:', result)
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
 
# CHUNK / PROVENANCE 
# =============================================================================
def get_metadata(chunk):
    metadata = chunk.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            return {}
    return metadata if isinstance(metadata, dict) else {}


def chunk_provenance(chunk):
    metadata = get_metadata(chunk)
    return {
        "doc_id": metadata.get("doc_id"),
        "page": metadata.get("page"),
        "printed_page": (metadata.get("printed_page") or "").strip(),}
 
def format_retrieved_chunks(chunks):
    blocks = []
    for chunk in chunks:
        text = chunk.get("text")
        chunk_id = chunk.get("id")
        metadata = get_metadata(chunk)
        if not text or not text.strip():
            raise ValueError(
                f"Retrieved chunk (id={chunk_id}) has no text")

        doc_id = metadata.get("doc_id", "")
        page = page_number(metadata.get("page"),metadata.get("printed_page", ""),)
 
        blocks.append(
            f"[chunk {chunk_id}]\n"
            f"Document: {doc_id}\n"
            f"Page: {page}\n"
            f"Text:\n{text.strip()}")
    return "\n\n".join(blocks)
 
# JSON / TEXT HELPERS
# =============================================================================

def extract_json_object(output):
    """Extract and validate one JSON object from model output."""
    if isinstance(output, dict): 
        return output
    
    text = str(output).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("No JSON object found in model output")
    
    # strict=False: verbatim contract quotes often contain raw newlines/tabs,
    # which strict JSON rejects ("Invalid control character").
    parsed = json.loads(text[start:end + 1], strict=False)
    if isinstance(parsed, str):  # JSON-encoded string wrapping another object
        parsed = json.loads(parsed, strict=False)
    if not isinstance(parsed, dict):
        raise TypeError(f"Expected JSON object/dict, got {type(parsed).__name__}")
    return parsed
 
 
RESULT_TAG = re.compile(r"\[result\s+\d+\]\s*",re.IGNORECASE,)

def strip_result_tags(text):
    return RESULT_TAG.sub("", text or "")

def normalize_text(text):
    return re.sub(r"\s+"," ",text or "",).strip()

def quote_matches(quote, text, threshold=0.95):
    normalized_quote = normalize_text(quote)
    normalized_text = normalize_text(text)
    if not normalized_quote or not normalized_text:
        return False
    if normalized_quote in normalized_text:
        return True
    matcher = SequenceMatcher(None, normalized_quote, normalized_text)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    coverage = matched / len(normalized_quote)
    return coverage >= threshold

# CONFIDENCE
# =============================================================================
def confidence_band(score):
    """Convert numeric retrieval confidence into an Excel-friendly band."""
    if score is None:
        return "Low Confidence"
    if score >= HIGH_CONFIDENCE_THRESHOLD:
        return "High Confidence"
    if score >= MEDIUM_CONFIDENCE_THRESHOLD:
        return "Medium Confidence"
    return "Low Confidence"
 
 
def confidence_label(score):
    """Backward-compatible display helper."""
    return confidence_band(score)
 
 
def calculate_retrieval_confidence(evidence):
    """
    Calculate final retrieval confidence ONLY from evidence that:
 
    1. Stage 1 explicitly cited.
    2. Python independently verified.
    3. Has a retrieval-confidence score.

    """
    scores = [record.retrieval_confidence
        for record in evidence
        if (record.verified and record.retrieval_confidence is not None)]
    return max(scores) if scores else None


def recommendation_display(review):
    """
    Excel-friendly AI Recommendation value.
    Examples:
        MET — High Confidence
        MET — Medium Confidence
        UNSURE — Low Confidence
    """
    status = (review.status or "UNSURE").strip().upper()
    if status not in {"MET", "UNSURE"}:
        status = "UNSURE"
    # Band shown to reviewers = the Decision Analyst's confidence.
    band = (review.decision_confidence or "Low").title()
    if status == "MET" and band not in ("High", "Medium"):
        status = "UNSURE"
    return f"{status} — {band} Confidence"
 
# EVIDENCE DISPLAY
# =============================================================================
def format_stage1_reasoning(review):
    """
    Creates the content intended for the Excel AI Reasoning column.
    Output:
        <Stage-1 reasoning>
        Cited contract text:
        document, page X
        "exact quote"
    """
 
    lines = []
    reasoning = (review.argument or "").strip()

    if reasoning:
        lines.append(reasoning)

    verified = [record for record in review.evidence if record.verified]
    if verified:
        lines += ["","Cited contract text:","",]
 
        for record in verified:
            page = page_number(record.page, record.printed_page,)
            lines.append(f"{record.doc_id}, Page {page}")
            lines.append(f'"{record.quote}"')
            lines.append("")
    else:
        lines += ["", "Cited contract text:", "", "No verified cited contract text was found.",]
    return "\n".join(lines).rstrip()
 
# OPTIONAL REVIEW DISPLAY
# =============================================================================
def format_review_display(review, deps):
    lines = [
        f"AI Recommendation: {recommendation_display(review)}",
        "",
        "AI Reasoning:",
        format_stage1_reasoning(review),]
    if review.decision_response:
        lines += ["", "Decision rationale:", review.decision_response]
    return "\n".join(lines).rstrip()

# SEARCH TOOL
# =============================================================================
async def search_contract(context: RunContext[ContractDeps],query: str,) -> str:
    deps = context.deps
    if deps.searches >= deps.max_searches:
        return ("Search limit reached. Use the evidence already retrieved and complete the analysis.")

    deps.searches += 1
    results = await retrieve(deps,query)
    log.debug(
        f"search_contract() call {deps.searches}/{deps.max_searches} "
        f"for '{query[:120]}' returned "
        f"{len(results)} chunks")
 
    if not results:
        return (f"Nothing in the contract matched '{query}'. "
                "Try different wording or a broader phrase.")
    record_chunks(deps,results)
    return format_retrieved_chunks(results)
 
# TOOL HOOKS
# =============================================================================
review_hooks = Hooks()
@review_hooks.on.before_tool_execute(tools=["search_contract"])
async def charge_search_budget(context: RunContext[ContractDeps],*,call: ToolCallPart,
    tool_def: ToolDefinition,args: ValidatedToolArgs,) -> ValidatedToolArgs:
    deps = context.deps
    if deps.searches >= deps.max_searches:
        log.debug(f"charge_search_budget() refused search {deps.searches + 1}, the limit is {deps.max_searches}")
 
        raise ToolFailed(f"Search budget for this requirement is used up after {deps.max_searches} searches. Decide from the text "
            "you already have, and return UNSURE if it does not settle it.")
    deps.searches += 1
    return args
 
 
@review_hooks.on.tool_execute_error(tools=["search_contract"])
async def retry_failed_search(context: RunContext[ContractDeps],*,call: ToolCallPart,
    tool_def: ToolDefinition,args: ValidatedToolArgs,error: Exception,):
    query = str(args.get("query", ""))[:120]
    Helper.print_exception("search_contract",error,f"Retrieval failed for query '{query}'.",)
    raise ModelRetry("The search backend failed. Try once more with a shorter query.")
 
# STAGE 1 AGENT
# =============================================================================
evidence_agent = Agent(bedrock_model(),deps_type=ContractDeps,system_prompt=EVIDENCE_SYSTEM_PROMPT,
    model_settings={"temperature": 0.0,"top_p": 1.0,},
    tools=[search_contract], capabilities=[review_hooks, bedrock_hooks,],
    retries=3,
    name="evidence_agent",)
 
# STAGE 2 AGENT
# =============================================================================
# Extended thinking (Nova 2 "reasoningConfig") for the Decision Analyst only.
# Effort: "low" | "medium" | "high". Reasoning tokens count toward max_tokens.
# If Bedrock rejects the request (ValidationException), remove temperature,
# top_p and max_tokens below and try again.
DECISION_REASONING_EFFORT = "medium"

decision_agent = Agent(bedrock_model(), system_prompt=DECISION_SYSTEM_PROMPT,
    model_settings={
        "temperature": 0.0,
        "top_p": 1.0,
        "max_tokens": 8000,
        "bedrock_additional_model_requests_fields": {
            "reasoningConfig": {"type": "enabled", "maxReasoningEffort": DECISION_REASONING_EFFORT},
        },
    },
    capabilities=[bedrock_hooks,],
    retries=3,
    name="decision_agent",)
 
# EVIDENCE VERIFICATION
# =============================================================================
def build_verified_evidence(evidence_items,deps,):
    """
    Convert Stage-1 evidence into EvidenceRecord objects and independently
    verify each quote against the retrieved chunk.
    """
    records = []
    if not isinstance(evidence_items, list):
        return records

    for evidence_item in evidence_items:
        if not isinstance(evidence_item,dict,):
            continue
        try:
            chunk_id = int(evidence_item.get("chunk_id"))
        except (TypeError, ValueError):
            continue

        quote = (evidence_item.get("quote") or "").strip()
        chunk = deps.chunks.get(chunk_id)
 
        # Ignore invented IDs or empty quotations.
        if not chunk or not quote:
            continue
        text = chunk.get("text") or ""
        metadata = get_metadata(chunk)
        verified = quote_matches(quote, text)
 
        records.append(EvidenceRecord(quote=quote, chunk_text=text, chunk_id=chunk_id, doc_id=metadata.get("doc_id","",),
                page=metadata.get("page"),
                printed_page=metadata.get("printed_page","",),
                retrieval_confidence=chunk.get("retrieval_confidence"),
                verified=verified, ))
    return records

# STAGE 2 INPUT
# =============================================================================
def build_decision_input(requirement, reasoning, missing_information, evidence, retrieval_confidence,
                         item="", legal_cite="", reference=None,):

    verified_evidence = []
    for record in evidence:
        if not record.verified:
            continue
        page = page_number(record.page, record.printed_page,)
        verified_evidence.append(
            {
                "contract_text": record.chunk_text,
                "document": record.doc_id,
                "page": page,
                "retrieval_confidence": record.retrieval_confidence,
            })
 
    return {
        "ItemId": item,
        "LegalCite": legal_cite,
        "Requirement": requirement,
        "RegulatoryReference": reference or [],
        "Stage1Reasoning": reasoning,
        "VerifiedEvidence": verified_evidence,
        "MissingInformation": missing_information,
        "RetrievalConfidence":retrieval_confidence,
        "RetrievalConfidenceBand":confidence_band(retrieval_confidence),}
 
# MAIN TWO-STAGE REQUIREMENT REVIEW
# =============================================================================
async def review_requirement(
    requirement,
    deps=None,
    sheet="",
    item="",
    legal_cite="",
    row=None,
    usage=None,
):
 
    review = RequirementReview(
        requirement=requirement,
        sheet=sheet,
        item=item,
        legal_cite=legal_cite,
        row=row,
        model=model_id(),
    )
 
    if not (requirement or "").strip():
        review.error = "Empty requirement text."
        return review
 
    deps = replace(deps or build_deps(), chunks={}, searches=0,)
    run_usage = usage if usage is not None else RunUsage()
 
    try:
        # Initial retrieval
        seed = await retrieve(deps, requirement)
        record_chunks(deps, seed)
        review.chunks_retrieved = len(deps.chunks)

        # Stage 1: Evidence Analysis
        stage1_result = await evidence_agent.run(f"Requirement to verify:\n{requirement}\n\n"
            "Retrieved contract context(each passage is identified by a persistent [chunk N] ID):\n"
            f"{format_retrieved_chunks(seed)}\n\n"
            "Analyze the requirement using only the retrieved contract context. "
            "If additional evidence is needed, use search_contract. "
            "Return exactly one JSON object containing Reasoning, Evidence, and MissingInformation.",
            deps=deps,
            usage=run_usage,
            usage_limits=UsageLimits(request_limit=6 + deps.max_searches),)
        review.chunks_retrieved = len(deps.chunks)

        try:
            stage1 = extract_json_object(stage1_result.output)
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            log.warning(f"Stage 1 JSON parse failed, retrying once: {exc}")
        
            retry_result = await evidence_agent.run(
                (
                    f"Requirement to verify:\n{requirement}\n\n"
                    "Retrieved contract context:\n"
                    f"{format_retrieved_chunks(list(deps.chunks.values()))}\n\n"
                    "Your previous response could not be parsed as JSON. "
                    "Analyze the requirement and return EXACTLY one valid JSON object:\n"
                    '{"Reasoning":"...",'
                    '"Evidence":[{"chunk_id":123,"quote":"..."}],'
                    '"MissingInformation":"..."}\n'
                    "No markdown fences or text outside the JSON."
                ),
                deps=deps,
                usage=run_usage,
                usage_limits=UsageLimits(request_limit=5),
            )
        
            stage1 = extract_json_object(retry_result.output)

        if not isinstance(stage1, dict):
            raise TypeError(f"Stage 1 expected dict, got {type(stage1).__name__}")
 
        # Stage 1 Reasoning
        stage1_reasoning = strip_result_tags(stage1.get("Reasoning", "",)).strip()
        missing_information = (stage1.get("MissingInformation","",)or "").strip()
 
        # This becomes the basis of the Excel AI Reasoning column.
        review.argument = stage1_reasoning
        review.missing_information = missing_information

        # Python Evidence Verification
        review.evidence = build_verified_evidence(stage1.get("Evidence",[],),deps,)
        review.quotes_verified = (bool(review.evidence) and all(record.verified for record in review.evidence))
 
        # Python Retrieval Confidence
        review.retrieval_confidence = (calculate_retrieval_confidence(review.evidence))
        review.combined_confidence = (review.retrieval_confidence)
 
        # Only verified cited evidence determines where-found/source information.
        review.sources = review.where_found()
 
        # Regulatory reference (42 CFR Part 438 + CMS State Guide).
        # Interpretive context only; never contract evidence.
        reference = await retrieve_reference(deps, requirement, item=item, legal_cite=legal_cite,)

        # Stage 2 input
        decision_input = build_decision_input(
            requirement=requirement,
            reasoning=stage1_reasoning,
            missing_information=missing_information,
            evidence=review.evidence,
            retrieval_confidence=review.retrieval_confidence,
            item=item,
            legal_cite=legal_cite,
            reference=reference,)
 
        # Stage 2: final MET/UNSURE decision
        stage2_result = await decision_agent.run(
            ("Make the final contract review decision using the following validated Stage-1 analysis.\n\n"
            f"{json.dumps(decision_input, indent=2)}"), usage=run_usage, usage_limits=UsageLimits(request_limit=5),)
 
        # Confirm extended thinking actually ran (look for this line in the log).
        thought = any(isinstance(part, ThinkingPart)
                      for message in stage2_result.all_messages()
                      for part in getattr(message, "parts", []))
        log.info(f"review_requirement() {sheet} item {item}: Stage 2 extended thinking used = {thought}")

        # Parse Stage 2
        try:
            stage2 = extract_json_object(stage2_result.output)
            if not isinstance(stage2, dict):
                raise TypeError(f"Stage 2 expected dict, got {type(stage2).__name__}")
            
        except (json.JSONDecodeError,ValueError,TypeError,) as parse_err:
            log.warning(f"review_requirement() " f"{sheet} item {item}: "
                "Stage 2 output was not valid JSON: " f"{parse_err}")
            review.status = "UNSURE"
            review.decision_confidence = "Low"
            review.error = (f"Stage 2 JSON error: {parse_err}")
            return review

        # Final Recommendation
        status = str(stage2.get("Recommendation", "UNSURE")).strip().upper()
        if status not in {"MET", "UNSURE"}:
            status = "UNSURE"

        score = review.retrieval_confidence
        stage2_response = strip_result_tags(stage2.get("Response","",)).strip()

        # Decision confidence: the Decision Analyst's own label. This, not
        # retrieval confidence, sets the High/Medium/Low shown in Status.
        decision_label = str(stage2.get("DecisionConfidence", "")).strip().upper()
        if decision_label not in DECISION_CONFIDENCE_LABELS:
            decision_label = "LOW"

        # MET is only allowed with HIGH or MEDIUM decision confidence.
        if status == "MET" and decision_label == "LOW":
            log.info("Stage 2 returned MET with LOW decision confidence; changing to UNSURE with LOW.")
            status = "UNSURE"

        # Hard guardrail: MET requires at least Medium retrieval confidence.
        # Retrieval confidence is used ONLY here, never shown in Status.
        if status == "MET" and (score is None or score < MEDIUM_CONFIDENCE_THRESHOLD):
            log.info(f"Stage 2 returned MET but retrieval confidence was {score}; overriding final status to UNSURE with LOW.")
            status = "UNSURE"
            decision_label = "LOW"
            stage2_response = ("Changed from MET to UNSURE because the supporting contract evidence "
                               "was too weakly matched to confirm it. " + stage2_response).strip()

        review.status = status
        review.decision_response = stage2_response
        review.decision_confidence = decision_label.title()

        # Logging
        log.info(
            f"review_requirement() "
            f"{sheet} item {item} "
            f"= {review.status}, "
            f"retrieval confidence "
            f"{review.retrieval_confidence}, "
            f"decision confidence "
            f"{decision_label}, "
            f"regulatory passages "
            f"{len(reference)}, "
            f"retrieval band "
            f"{confidence_band(review.retrieval_confidence)}, "
            f"chunks "
            f"{review.chunks_retrieved}"
        )
 
        log.info("Stage 2 decision response: "f"{stage2_response}")
        log.info("review_requirement() display:\n" + format_review_display(review, deps,))
 
        return review
 
    except Exception as lclEx:
        Helper.print_exception("review_requirement", lclEx,
            f"Review failed for requirement {requirement[:120]}.",)
        review.status = "UNSURE"
        review.decision_confidence = "Low"
        review.error = (f"{type(lclEx).__name__}: {lclEx}")
        review.argument = (review.argument or "The automated review did not complete for this requirement.")
        print(f"\nERROR: {review.error}")
        print(f"Requirement: {requirement[:200]}\n")
 
        return review
 
 
# =============================================================================
# FORMATTED QUESTION
# =============================================================================
 
async def answer_question_formatted(
    query,
    deps=None,
    usage=None,
):
    """
    Use the same two-stage review pipeline for a formatted question.
    """
 
    review = await review_requirement(
        requirement=query,
        deps=deps,
        usage=usage,
    )
 
    return format_review_display(
        review,
        deps,
    )
