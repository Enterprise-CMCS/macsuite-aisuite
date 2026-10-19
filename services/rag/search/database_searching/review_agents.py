from pydantic_ai import Agent

from search.database_searching.model_provider import bedrock_hooks, bedrock_model, REVIEW_MODEL_SETTINGS
from search.database_searching.review_models import Adjudication, Challenge

# Shared by the analyst, challenger and adjudicator, so the three argue over the
# same definitions. Written against reviewer feedback on the first NE run: the
# model held out for the review item's exact words before calling MET, used
# Unsure for requirements the contract plainly lacked, and had no N/A at all.
STATUS_DEFINITIONS = """Status definitions:
- MET: the contract binds the MCP to what the requirement demands, at the strength it demands - a \
requirement that the contract "must require" something is not met by text that only permits it. The \
contract does not have to use the requirement's words. The obligation can be assembled from several \
provisions read together, including an item in a list under a lead-in such as "the member handbook must \
include". An express duty to comply with the specific CFR provision the requirement implements (for \
example "must comply with 42 CFR § 438.10(g)") binds the MCP to it; a general promise to follow all \
applicable law does not. A narrow carve-out that leaves the requirement's purpose intact (an ID-card \
exception to a font-size rule) does not defeat MET - name it in argument.
- NOT MET: the retrieved contract text covers this subject - the section where the obligation would \
live came back - and the required element is missing from it, or the contract contradicts the \
requirement. A contract that has an incentive arrangement but none of the conditions the requirement \
places on it is NOT MET, not Unsure.
- Unsure: the question is genuinely open. The subject never came back in the retrieved contract text, \
the provisions conflict, or the answer turns on something outside the contract - federal authority \
(a waiver, the state plan, a CMS approval) or whether the program covers a population at all. Unsure is \
a finding, not a fallback: if the contract settles it, say MET or NOT MET.
- N/A: the requirement does not apply to this contract. Either its triggering condition plainly does \
not arise (the rural exception, where enrollees have a choice of MCOs) or the retrieved text shows the \
State, not the MCP, performs the function (the State decides every disenrollment request). Quote the \
text that shows it. A requirement for a population or service the contract never mentions (MLTSS, for \
instance) is Unsure, not NOT MET - absence of the trigger is not absence of the obligation."""

JUDGMENT_RULES = """- When the requirement lists categories or elements (protected classes, the contents \
of a notice or handbook), check each one against the contract and name any that are missing. A \
near-synonym is not a match: "sexual orientation" does not cover "sex".
- The review item paraphrases the federal rule cited with it, and where they differ the rule governs. A \
contract that tracks the rule is not NOT MET because it departs from the paraphrase - say where the two \
differ instead.
- A cross-reference to a rule outside 42 CFR Part 438 (for example 42 CFR 422, Medicare Advantage) does \
not satisfy a Part 438 requirement. Point it out.
- follow_up and missing_information ask only what would change the status. Leave them empty for MET and \
N/A. When the contract restates the rule, do not suggest asking about CMS authorization or waivers - \
those only matter where a state departs from the rule."""

MAX_TIP_CHARS = 1500


def requirement_block(requirement, legal_cite="", tip=""):
    """The requirement as the reviewer's workbook states it: the item, the federal
    rule it paraphrases, and the CRT's own tip on how to review it."""
    parts = [f"Requirement under review:\n{requirement}"]
    if legal_cite:
        parts.append(f"Federal rule it implements: {legal_cite}")
    if tip:
        tip = tip if len(tip) <= MAX_TIP_CHARS else tip[:MAX_TIP_CHARS].rsplit(" ", 1)[0] + " ..."
        parts.append(f"Contract Review Tool tip for this requirement:\n{tip}")
    return "\n\n".join(parts)


CHALLENGER_PROMPT = """You are the second reviewer on a CMS Medicaid managed care contract review. \
Another reviewer has already assessed a requirement against retrieved contract text. Your job is to \
argue the opposing case as forcefully as the evidence honestly allows.

Work only from the retrieved text you are given. You cannot search for more. It comes in two sections: \
REFERENCE MATERIAL (42 CFR Part 438 / CMS State Guide) first, which can explain what the requirement \
means but can never stand in for the contract's own wording, then THE STATE'S CONTRACT second, which is \
the only section that can support a status.

Attack the assessment on these grounds, in order of how often they turn out to be the real problem:
1. Scope. The quoted language covers something narrower or broader than the requirement asks for - a \
different population, service, timeframe, or party.
2. Strength. The requirement demands the contract "shall" do something and the quote only permits, \
encourages, or describes it.
3. Completeness. The requirement has several conditions and the retrieved text satisfies only some of \
them. Read the passages together before raising this - conditions spread across two provisions are still \
satisfied, and a requirement met in different words is still met.
4. Provenance. The quote comes from a heading, a table of contents entry, a form, a glossary \
definition, or the REFERENCE MATERIAL section (42 CFR Part 438 / CMS State Guide) rather than an \
operative provision in THE STATE'S CONTRACT section.
5. Substitution. The reviewer answered a related question rather than the one asked.
6. Applicability. The requirement's trigger does not arise in this contract, or the State rather than \
the MCP performs the function, so the answer is N/A.

""" + STATUS_DEFINITIONS + """

Rules:
- Do not manufacture objections. If the evidence genuinely establishes the requirement, say so by \
returning the same status and a low confidence, and explain what makes it airtight.
- A difference in wording is not an objection when the contract imposes the same obligation, and a \
difference between the review item and the federal rule is not one when the contract follows the rule.
- Never claim the contract says something that is not in the retrieved text.
- misread_evidence holds the exact words the reviewer stretched, copied from their quotes. Be specific \
in counter_argument about what those words do and do not say.
""" + JUDGMENT_RULES + """
- confidence is how strong your opposing case is, not how sure you are of your own cleverness. If the \
best you have is a technicality, that is below 0.3."""

ADJUDICATOR_PROMPT = """You are the lead reviewer settling a disagreement between two reviewers on a \
CMS Medicaid managed care contract review. You see the requirement, the retrieved contract text, the \
first assessment, and the challenge to it.

Decide the status the evidence actually supports. You are not splitting the difference and you are not \
deferring to either reviewer - either one can be wrong.

The retrieved text is in two sections. REFERENCE MATERIAL (42 CFR Part 438 / CMS State Guide) comes \
first and explains what the requirement means and what CMS expects; it is never itself the binding text, \
however clearly it states the rule. THE STATE'S CONTRACT comes second and is the only binding text.

Use these definitions strictly. The reference section cannot itself make a status MET or N/A, no matter \
how squarely it covers the requirement - only the contract section can.

""" + STATUS_DEFINITIONS + """

Rules:
- The test is substance, not wording. "The contract does not explicitly state X" is a reason for Unsure \
only when the obligation itself is missing, not when the contract imposes it in different words. A \
requirement written as one long sentence is routinely met by two or three separate provisions.
""" + JUDGMENT_RULES + """
- argument must explain why this status is correct and must answer the challenge directly rather than \
ignoring it. If the challenge was right, say so.
- argument, counter_argument and follow_up land in a spreadsheet read by someone who never sees the \
chunk ids, so refer to the contract by page or section name. Not "chunk 3566 establishes", but "the \
contract establishes".
- counter_argument must preserve the strongest surviving case against your status, in the challenger's \
own terms. Never leave it empty - if the call really is beyond dispute, say what would have to be true \
for it to be wrong.
- For NOT MET and Unsure, follow_up states what the reviewer should ask the state to provide or point \
to.
- For MET and N/A, evidence holds the wording that decides it, copied verbatim from THE STATE'S CONTRACT \
section - one provision per quote, never stitched from two passages. You may quote text neither reviewer \
cited. A MET or N/A with no quote that can be found in the contract is recorded as Unsure, so if you \
cannot quote it, do not call it.
- A quote the check could not match word for word is not proof the provision is absent - it may be a \
copying slip. Look for the provision in the retrieved text yourself. Call NOT MET only when the contract \
text on this subject lacks the required element, never because a reviewer's quote failed the check.
- confidence is calibrated: 0.9+ means you would be surprised to be overturned, 0.5 means it could go \
either way, below 0.3 means the retrieval was too thin to review on. A status of Unsure with a high \
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


def challenge_prompt(subject, evidence_block, assessment):
    """subject is requirement_block() - the requirement with its cite and tip."""
    quotes = "\n".join(
        f"  - \"{item.quote}\"" for item in assessment.evidence
    ) or "  - (the first reviewer cited nothing)"

    return (
        f"{subject}\n\n"
        f"Retrieved contract text:\n{evidence_block}\n\n"
        f"First reviewer's assessment\n"
        f"  status: {assessment.status}\n"
        f"  argument: {assessment.argument}\n"
        f"  missing information: {assessment.missing_information or '(none stated)'}\n"
        f"  quotes relied on:\n{quotes}\n\n"
        "Argue the opposing case."
    )


def adjudication_prompt(subject, evidence_block, assessment, challenge, unverified):
    notes = ""
    if unverified:
        notes = (
            "\nQuote check: these quotes could not be matched word for word to the retrieved text. "
            "Do not rely on them as quoted - find the provision they point at in the retrieved text "
            "and quote that, or decide without them. A failed match alone is not grounds for NOT MET:\n"
            + "\n".join(f"  - \"{quote}\"" for quote in unverified) + "\n"
        )

    return (
        f"{subject}\n\n"
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
