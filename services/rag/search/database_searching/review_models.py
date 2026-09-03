import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["MET", "NOT MET", "UNCLEAR"]

# "in chunk 3566", "from chunks 3733 and 3740" - how the model cites the search
# results while it works. The reviewer reading the workbook never sees the chunks,
# so the ids are swapped for the pages they came from. The preposition is matched
# too, because "in chunk 3566" has to become "on page 85" rather than "in page 85".
CHUNK_MENTION = re.compile(
    r"\b(?:(in|within|at|from|of)\s+)?chunks?\s+#?(\d+(?:\s*(?:,|and|&)\s*#?\d+)*)",
    re.IGNORECASE)
CHUNK_ID = re.compile(r"\d+")

# "the retrieved chunks", with no id to name a page with. A passage is what the
# reviewer would call it, and it reads the same in every sentence the word appears in.
BARE_CHUNK = re.compile(r"\bchunks?\b", re.IGNORECASE)


def page_number(page, printed_page=""):
    """The number printed on the page, falling back to its position in the file."""
    printed_page = (printed_page or "").strip()
    if printed_page.lower().startswith("page"):
        printed_page = printed_page[4:].strip()

    if printed_page:
        return printed_page
    return str(page + 1) if page is not None else ""


def page_label(page, printed_page=""):
    number = page_number(page, printed_page)
    return f"page {number}" if number else ""


def simplify_doc_id(doc_id):
    doc_id = (doc_id or "").lower()
    for extension in (".pdf", ".docx", ".doc"):
        if doc_id.endswith(extension):
            doc_id = doc_id[: -len(extension)]
            break
    return "".join(character for character in doc_id if character.isalnum())


class CitedEvidence(BaseModel):
    quote: str = Field(
        min_length=1,
        description="Verbatim wording copied from the retrieved contract text, no paraphrasing",
    )


class RequirementAssessment(BaseModel):
    status: Status
    argument: str = Field(description="Why the contract text supports this status, referring to the quotes")
    evidence: List[CitedEvidence] = Field(default_factory=list)
    missing_information: str = Field(
        default="",
        description="For NOT MET or UNCLEAR, the specific language or provision that would settle it",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="How likely this status is correct, 0.0 to 1.0")


class Challenge(BaseModel):
    """The opposing case, argued by a separate agent that has not committed to a view."""

    counter_status: Status = Field(description="The status the evidence supports once the assessment is challenged")
    counter_argument: str = Field(description="The strongest case against the assessment's status")
    misread_evidence: List[str] = Field(
        default_factory=list,
        description="The exact words the reviewer stretched beyond what they actually say",
    )
    evidence_gaps: List[str] = Field(
        default_factory=list,
        description="What the assessment treated as settled but the retrieved text never states",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="How strong the opposing case is, 0.0 to 1.0")


class Adjudication(BaseModel):
    """Final call, made after seeing both the assessment and the challenge."""

    status: Status
    argument: str = Field(description="Why this status is correct, addressing the challenge head on")
    counter_argument: str = Field(description="The strongest remaining argument against this status")
    follow_up: str = Field(default="", description="What the reviewer should ask the state for, if anything")
    confidence: float = Field(ge=0.0, le=1.0, description="Calibrated likelihood this status is correct")


class EvidenceRecord(BaseModel):
    quote: str
    chunk_id: Optional[int] = None
    doc_id: str = ""
    page: Optional[int] = None
    # Optional because reviews recorded before this field settled on "" wrote it as
    # null, and a sidecar line that will not load is a review quietly done twice.
    printed_page: Optional[str] = ""
    retrieval_confidence: Optional[float] = None
    verified: bool = False


class RequirementReview(BaseModel):
    requirement: str
    sheet: str = ""
    item: str = ""
    legal_cite: str = ""
    row: Optional[int] = None

    status: Status = "UNCLEAR"
    argument: str = ""
    counter_argument: str = ""
    follow_up: str = ""
    missing_information: str = ""

    llm_confidence: Optional[float] = None
    retrieval_confidence: Optional[float] = None
    combined_confidence: Optional[float] = None
    quotes_verified: bool = False

    evidence: List[EvidenceRecord] = Field(default_factory=list)
    sources: str = ""
    chunks_retrieved: int = 0
    model: str = ""
    error: str = ""

    def in_reviewer_terms(self, text):
        """The model's prose with its chunk ids swapped for the pages they came from."""
        pages = {record.chunk_id: page_number(record.page, record.printed_page)
                 for record in self.evidence if record.chunk_id is not None}

        def swap(match):
            preposition = (match.group(1) or "").lower()
            found = (pages.get(int(chunk_id)) for chunk_id in CHUNK_ID.findall(match.group(2)))
            numbers = [number for number in dict.fromkeys(found) if number]

            if not numbers:
                # Nothing was quoted from these chunks, so there is no page to name.
                where = "the contract"
            else:
                listed = (" and ".join(numbers) if len(numbers) < 3
                          else ", ".join(numbers[:-1]) + " and " + numbers[-1])
                where = f"page{'s' if len(numbers) > 1 else ''} {listed}"
                if preposition in ("in", "within", "at"):
                    preposition = "on"

            said = f"{preposition} {where}".strip()
            # The model writes mid-sentence and at the start of one.
            return said[0].upper() + said[1:] if match.group(0)[0].isupper() else said

        cleaned = CHUNK_MENTION.sub(swap, text or "")
        return BARE_CHUNK.sub(
            lambda match: "passages" if match.group(0).lower().endswith("s") else "passage",
            cleaned)

    def where_found(self):
        documents = {simplify_doc_id(record.doc_id) for record in self.evidence if record.doc_id}
        seen = []
        for record in self.evidence:
            pointer = page_label(record.page, record.printed_page)
            if len(documents) > 1 and record.doc_id:
                pointer = f"{pointer} - {record.doc_id}" if pointer else record.doc_id
            pointer = pointer or record.doc_id
            if pointer and pointer not in seen:
                seen.append(pointer)
        return " | ".join(seen)

    def page_numbers(self):
        # Ordered by position in the file, because roman-numeral front matter and
        # the body's own numbering do not sort together as text.
        ordered = {}
        for record in self.evidence:
            number = page_number(record.page, record.printed_page)
            if number:
                ordered.setdefault(number, record.page if record.page is not None else 0)
        return ", ".join(sorted(ordered, key=lambda number: ordered[number]))
