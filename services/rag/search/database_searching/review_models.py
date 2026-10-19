import re
from difflib import SequenceMatcher
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Status = Literal["MET", "NOT MET", "Unsure", "N/A"]

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

QUOTE_MATCH_RATIO = 0.85

QUOTE_MATCH_MIN_RUN = 8

MIN_FRAGMENT_CHARS = 20


def _normalize(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())).strip()


def _match_ratio(needle, haystack):
    blocks = SequenceMatcher(None, needle, haystack, autojunk=False).get_matching_blocks()
    matched = sum(block.size for block in blocks if block.size >= QUOTE_MATCH_MIN_RUN)
    return matched / len(needle) if needle else 0.0


def quote_score(quote, chunk_text):
    """How closely chunk_text carries the quote, from 0.0 to 1.0.

    A quote elided with "..." is scored fragment by fragment and takes its
    weakest fragment, so one fragment copied exactly cannot carry another that
    was not copied at all. A fragment found verbatim scores 1.0."""
    haystack = _normalize(chunk_text)
    if not haystack:
        return 0.0

    fragments = [part for part in re.split(r"\.{3}|…", quote or "")
                 if len(_normalize(part)) >= MIN_FRAGMENT_CHARS]
    score = 1.0
    for fragment in fragments or [quote]:
        needle = _normalize(fragment)
        if not needle:
            return 0.0
        if needle not in haystack:
            score = min(score, _match_ratio(needle, haystack))
    return score


def quote_supported(quote, chunk_text):
    return quote_score(quote, chunk_text) >= QUOTE_MATCH_RATIO


def best_quoted(quote, chunks):
    """The chunk that carries the quote most closely, or None if none carries it.

    Taking the first chunk over the threshold credited a quote to whichever
    near-duplicate was retrieved first - contracts repeat their own wording
    across sections, so a fuzzy match on page 55 won over the exact text on
    page 86 and Where Found sent the reviewer to the wrong page. Ties go to the
    earlier chunk, so pass them in order of preference.

    Lives here rather than in agents.py because that module needs AWS access to
    import, and this is unit tested.

    A quote no single chunk carries is tried against each chunk joined to the
    one that follows it in the document, and credited to the first of the two.
    The splitter cuts mid-list, so a list's lead-in and its first item - "for
    cause, at any time, under any one of the following circumstances: The MCO
    does not..." - routinely sit either side of a boundary. Rejecting that
    quote as unfound turned real contract text into a NOT MET."""
    chunks = list(chunks)
    best, best_score = None, QUOTE_MATCH_RATIO
    for chunk in chunks:
        score = quote_score(quote, chunk.get("text"))
        if score > best_score or (best is None and score >= best_score):
            best, best_score = chunk, score
            if score == 1.0:
                break
    if best is not None:
        return best

    for first, second in adjoining_pairs(chunks):
        joined = f"{first.get('text') or ''} {second.get('text') or ''}"
        score = quote_score(quote, joined)
        if score > best_score or (best is None and score >= best_score):
            best, best_score = first, score
            if score == 1.0:
                break
    return best


def adjoining_pairs(chunks):
    """Each chunk with the one inserted straight after it, where the two run on.

    Ids are SERIAL in document order, so id + 1 in the same document, element
    type and page run is the text that continues it (see adjoins)."""
    by_id = {chunk.get("id"): chunk for chunk in chunks if chunk.get("id") is not None}
    for chunk_id, chunk in sorted(by_id.items()):
        following = by_id.get(chunk_id + 1)
        if following is not None and adjoins(chunk, following):
            yield chunk, following


def merge_evidence(*groups):
    """The records of each group in order, a quote already seen dropped.

    Quotes are compared normalized, so the adjudicator re-quoting the analyst's
    wording with different spacing or punctuation is not listed twice. A
    verified copy replaces an unverified one of the same quote, keeping its
    place, so a quote the analyst garbled and the adjudicator copied cleanly
    counts as verified."""
    merged, at = [], {}
    for group in groups:
        for record in group:
            key = _normalize(record.quote)
            if key not in at:
                at[key] = len(merged)
                merged.append(record)
            elif record.verified and not merged[at[key]].verified:
                merged[at[key]] = record
    return merged


def neighbour_ids(hit_ids, span=1):
    """The row ids either side of each hit, not counting the hits themselves.

    Chunks are inserted in document order, so a SERIAL id one either side of a
    hit is usually the passage just before or after it - the list lead-in ("the
    member handbook must include") that the item on its own lost, or the rest of
    a sentence the splitter cut."""
    hits = set(hit_ids)
    return sorted({hit + step for hit in hits for step in range(-span, span + 1) if step} - hits)


def _position(chunk):
    metadata = chunk.get("metadata") or {}
    return (metadata.get("doc_id") or metadata.get("doc_name") or "",
            metadata.get("element_type"), metadata.get("page"))


def adjoins(hit, row, max_page_gap=1):
    """Whether row continues the same run of text as hit.

    Ids only follow document order within one document and one element type -
    tables and images are inserted after all of the text - so a neighbouring id
    from another document, another element type or a distant page is not the
    passage beside the hit and is left out."""
    hit_doc, hit_type, hit_page = _position(hit)
    row_doc, row_type, row_page = _position(row)
    if hit_doc != row_doc or hit_type != row_type:
        return False
    if hit_page is None or row_page is None:
        return False
    return abs(hit_page - row_page) <= max_page_gap


def with_neighbours(hits, rows, expand, span=1):
    """hits with the adjoining rows placed either side of the hits in expand.

    Each neighbour sits next to its hit so the model reads the passage in
    order, and the run stops at the first id that does not adjoin, so a gap is
    never bridged. A row already among the hits, or already placed beside an
    earlier hit, is not repeated."""
    by_id = {row.get("id"): row for row in rows}
    placed = {hit.get("id") for hit in hits}
    expand = set(expand)

    def run(hit, steps):
        found = []
        for step in steps:
            row = by_id.get(hit["id"] + step)
            if row is None or row.get("id") in placed or not adjoins(hit, row):
                break
            found.append(row)
        placed.update(row.get("id") for row in found)
        return found

    expanded = []
    for hit in hits:
        if hit.get("id") not in expand:
            expanded.append(hit)
            continue
        before = run(hit, range(-1, -span - 1, -1))
        after = run(hit, range(1, span + 1))
        expanded.extend(reversed(before))
        expanded.append(hit)
        expanded.extend(after)
    return expanded


def page_number(page, printed_page=""):
    """The number printed on the page, falling back to its position in the file."""
    printed_page = (printed_page or "").strip()
    if printed_page.lower().startswith("page"):
        printed_page = printed_page[4:].strip()

    if printed_page:
        return printed_page
    return str(page + 1) if page is not None else ""


def page_reference(page, printed_page=""):
    """The printed number, with the PDF position beside it when the two differ.

    Reviewers work both ways - some by the number printed on the page, some by
    the PDF viewer's page counter - and on a contract with roman-numeral front
    matter the two drift apart, so either one alone sends some reviewers to the
    wrong page."""
    number = page_number(page, printed_page)
    position = str(page + 1) if page is not None else ""
    if number and position and number != position:
        return f"{number} (PDF p. {position})"
    return number


def page_label(page, printed_page=""):
    reference = page_reference(page, printed_page)
    return f"page {reference}" if reference else ""


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
        description="For NOT MET or Unsure, the specific language or provision that would settle it",
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
    # The adjudicator can land on a status neither reviewer quoted for. Without
    # quotes of its own, a MET it reached from the contract text could not be
    # checked, and was recorded as Unsure beside an argument saying "explicitly".
    evidence: List[CitedEvidence] = Field(
        default_factory=list,
        description="For MET and N/A, the contract wording that decides it, copied verbatim",
    )
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
    # "contract" or "common" (42 CFR Part 438 / CMS State Guide). check_citations
    # already refuses a "common" quote as MET evidence, so this is audit-trail
    # visibility for the sidecar rather than something downstream branches on.
    source: str = "contract"


class RequirementReview(BaseModel):
    requirement: str
    sheet: str = ""
    item: str = ""
    legal_cite: str = ""
    row: Optional[int] = None

    status: Status = "Unsure"
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

    @field_validator("status", mode="before")
    @classmethod
    def _legacy_status(cls, value):
        # Sidecars written before the rename say "UNCLEAR". A line that will not
        # load is a review quietly done twice, or dropped under --render-only.
        return "Unsure" if value == "UNCLEAR" else value

    def in_reviewer_terms(self, text):
        """The model's prose with its chunk ids swapped for the pages they came from."""
        pages = {record.chunk_id: page_reference(record.page, record.printed_page)
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
            number = page_reference(record.page, record.printed_page)
            if number:
                ordered.setdefault(number, record.page if record.page is not None else 0)
        return ", ".join(sorted(ordered, key=lambda number: ordered[number]))
