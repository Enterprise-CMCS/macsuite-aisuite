from typing import List, Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["MET", "NOT MET", "UNCLEAR"]


def page_label(page):
    return f"page {page + 1}" if page is not None else ""


def simplify_doc_id(doc_id):
    doc_id = (doc_id or "").lower()
    for extension in (".pdf", ".docx", ".doc"):
        if doc_id.endswith(extension):
            doc_id = doc_id[: -len(extension)]
            break
    return "".join(character for character in doc_id if character.isalnum())


class CitedEvidence(BaseModel):
    chunk_id: int = Field(description="The id shown in the [chunk <id>] header of the search results")
    quote: str = Field(min_length=1, description="Verbatim wording copied from that chunk, no paraphrasing")


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
    misread_evidence: List[int] = Field(
        default_factory=list,
        description="Chunk ids whose quotes were stretched beyond what they actually say",
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

    def where_found(self):
        documents = {simplify_doc_id(record.doc_id) for record in self.evidence if record.doc_id}
        seen = []
        for record in self.evidence:
            pointer = page_label(record.page)
            if len(documents) > 1 and record.doc_id:
                pointer = f"{pointer} - {record.doc_id}" if pointer else record.doc_id
            pointer = pointer or record.doc_id
            if pointer and pointer not in seen:
                seen.append(pointer)
        return " | ".join(seen)

    def page_numbers(self):
        pages = {record.page + 1 for record in self.evidence if record.page is not None}
        return ", ".join(str(page) for page in sorted(pages))
