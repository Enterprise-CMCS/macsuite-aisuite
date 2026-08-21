"""Structured outputs exchanged between the contract-review agents.

Every agent returns one of these instead of prose we have to parse afterwards, so
a malformed answer fails inside pydantic-ai (which retries) rather than silently
landing in the workbook as an "UNCLEAR" row.

Note that the agents cite chunk ids, never document names or page numbers. The
retrieval layer already knows which document and page each chunk came from, so
resolving provenance ourselves removes the most common way these reviews go
wrong: a plausible-looking citation the model invented.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from search.database_searching.toc_index import simplify_doc_id

Status = Literal["MET", "NOT MET", "UNCLEAR"]


class CitedEvidence(BaseModel):
    """A verbatim quote tied back to the chunk it was retrieved from."""

    chunk_id: int = Field(description="The id shown in the [chunk <id>] header of the search results")
    quote: str = Field(min_length=1, description="Verbatim wording copied from that chunk, no paraphrasing")


class RequirementAssessment(BaseModel):
    """First-pass reading of one requirement against the retrieved contract text."""

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
    """One quote with the provenance we resolved for it ourselves."""

    quote: str
    chunk_id: Optional[int] = None
    doc_id: str = ""
    page: Optional[int] = None
    printed_page: Optional[str] = None
    toc_path: str = ""
    toc_title: str = ""
    citation: str = ""
    retrieval_confidence: Optional[float] = None
    verified: bool = False


class RequirementReview(BaseModel):
    """Everything the workbook needs for a single requirement row."""

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
    toc_sections: str = ""
    chunks_retrieved: int = 0
    model: str = ""
    error: str = ""

    def where_found(self):
        """Column F of the review tool: the shortest useful pointer into the document.

        The document name is left off when every quote came from the same file, which
        is the normal case for a single contract. Repeating it on all three citations
        pushed the section and page - the parts a reviewer actually navigates by -
        off the visible width of the column.
        """
        # Normalised, because a table chunk keeps the ".pdf" that a text chunk drops.
        # Comparing the raw strings makes one document look like two and puts the
        # filename back on every line.
        documents = {simplify_doc_id(record.doc_id) for record in self.evidence if record.doc_id}
        seen = []
        for record in self.evidence:
            where = " ".join(part for part in (record.toc_path, record.toc_title) if part).strip()
            page = record.printed_page or (
                f"page index {record.page}" if record.page is not None else "")
            pointer = ", ".join(part for part in (where, page) if part)
            if len(documents) > 1 and record.doc_id:
                pointer = f"{pointer} - {record.doc_id}" if pointer else record.doc_id
            pointer = pointer or record.citation
            if pointer and pointer not in seen:
                seen.append(pointer)
        return " | ".join(seen)
