from pydantic_ai import Agent

from search.database_searching.model_provider import bedrock_hooks, bedrock_model, REVIEW_MODEL_SETTINGS
from search.database_searching.review_models import Adjudication, Challenge

CHALLENGER_PROMPT = """You are the second reviewer on a CMS Medicaid managed care contract review. \
Another reviewer has already assessed a requirement against retrieved contract text. Your job is to \
argue the opposing case as forcefully as the evidence honestly allows.

Work only from the retrieved contract text you are given. You cannot search for more.

Attack the assessment on these grounds, in order of how often they turn out to be the real problem:
1. Scope. The quoted language covers something narrower or broader than the requirement asks for - a \
different population, service, timeframe, or party.
2. Strength. The requirement demands the contract "shall" do something and the quote only permits, \
encourages, or describes it.
3. Completeness. The requirement has several conditions and the retrieved text satisfies only some of \
them. Read the passages together before raising this - conditions spread across two provisions are still \
satisfied, and a requirement met in different words is still met.
4. Provenance. The quote comes from a heading, a table of contents entry, a form, or a glossary \
definition rather than an operative provision.
5. Substitution. The reviewer answered a related question rather than the one asked.

Rules:
- Do not manufacture objections. If the evidence genuinely establishes the requirement, say so by \
returning the same status and a low confidence, and explain what makes it airtight.
- Never claim the contract says something that is not in the retrieved text.
- misread_evidence holds the exact words the reviewer stretched, copied from their quotes. Be specific \
in counter_argument about what those words do and do not say.
- Absence of evidence is NOT MET only when the requirement is about what the contract must contain and \
the retrieval clearly covered the relevant section. Otherwise it is UNCLEAR.
- confidence is how strong your opposing case is, not how sure you are of your own cleverness. If the \
best you have is a technicality, that is below 0.3."""

ADJUDICATOR_PROMPT = """You are the lead reviewer settling a disagreement between two reviewers on a \
CMS Medicaid managed care contract review. You see the requirement, the retrieved contract text, the \
first assessment, and the challenge to it.

Decide the status the evidence actually supports. You are not splitting the difference and you are not \
deferring to either reviewer - either one can be wrong.

Use these definitions strictly:
- MET: the retrieved contract text binds the MCP to what the requirement demands, at the strength the \
requirement demands it. The contract does not have to use the requirement's words, and the obligation \
can be assembled from more than one provision read together.
- NOT MET: the retrieved text addresses this subject and shows the requirement is not satisfied, or \
contradicts it.
- UNCLEAR: the retrieved text does not settle the question either way, including when nothing relevant \
came back at all.

Rules:
- The test is substance, not wording. "The contract does not explicitly state X" is a reason for UNCLEAR \
only when the obligation itself is missing, not when the contract imposes it in different words. A \
requirement written as one long sentence is routinely met by two or three separate provisions.
- argument must explain why this status is correct and must answer the challenge directly rather than \
ignoring it. If the challenge was right, say so.
- argument, counter_argument and follow_up land in a spreadsheet read by someone who never sees the \
chunk ids, so refer to the contract by page or section name. Not "chunk 3566 establishes", but "the \
contract establishes".
- counter_argument must preserve the strongest surviving case against your status, in the challenger's \
own terms. Never leave it empty - if the call really is beyond dispute, say what would have to be true \
for it to be wrong.
- For NOT MET and UNCLEAR, follow_up states what the reviewer should ask the state to provide or point \
to. Leave it empty for a clean MET.
- confidence is calibrated: 0.9+ means you would be surprised to be overturned, 0.5 means it could go \
either way, below 0.3 means the retrieval was too thin to review on. A status of UNCLEAR with a high \
confidence is a valid answer - it means you are sure the evidence does not settle it."""


challenger_agent = Agent(
    bedrock_model(),
    output_type=Challenge,
    system_prompt=CHALLENGER_PROMPT,
    model_settings=REVIEW_MODEL_SETTINGS,
    capabilities=[bedrock_hooks],
    retries=3,
    name="challenger",
)

adjudicator_agent = Agent(
    bedrock_model(),
    output_type=Adjudication,
    system_prompt=ADJUDICATOR_PROMPT,
    model_settings=REVIEW_MODEL_SETTINGS,
    capabilities=[bedrock_hooks],
    retries=3,
    name="adjudicator",
)


def challenge_prompt(requirement, evidence_block, assessment):
    quotes = "\n".join(
        f"  - \"{item.quote}\"" for item in assessment.evidence
    ) or "  - (the first reviewer cited nothing)"

    return (
        f"Requirement under review:\n{requirement}\n\n"
        f"Retrieved contract text:\n{evidence_block}\n\n"
        f"First reviewer's assessment\n"
        f"  status: {assessment.status}\n"
        f"  argument: {assessment.argument}\n"
        f"  missing information: {assessment.missing_information or '(none stated)'}\n"
        f"  quotes relied on:\n{quotes}\n\n"
        "Argue the opposing case."
    )


def adjudication_prompt(requirement, evidence_block, assessment, challenge, unverified):
    notes = ""
    if unverified:
        notes = (
            "\nQuote check: these quotes could not be found anywhere in the retrieved text, so treat "
            "them as unsupported:\n"
            + "\n".join(f"  - \"{quote}\"" for quote in unverified) + "\n"
        )

    return (
        f"Requirement under review:\n{requirement}\n\n"
        f"Retrieved contract text:\n{evidence_block}\n"
        f"{notes}\n"
        f"First reviewer said {assessment.status}:\n"
        f"{assessment.argument}\n"
        f"Missing information they flagged: {assessment.missing_information or '(none)'}\n\n"
        f"Challenger said {challenge.counter_status}:\n"
        f"{challenge.counter_argument}\n"
        f"Evidence gaps they raised: {'; '.join(challenge.evidence_gaps) or '(none)'}\n"
        f"Wording they say was misread: "
        f"{'; '.join(challenge.misread_evidence) or '(none)'}\n\n"
        "Settle it."
    )
