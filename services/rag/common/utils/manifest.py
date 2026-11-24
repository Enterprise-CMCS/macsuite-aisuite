import hashlib
import re
from collections import Counter
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from common.utils.contract_config import validate_embeddings_table_name
from common.utils.helper import Helper

SUPPORTED_DOCUMENT_EXTENSIONS = (".pdf", ".docx")

MANIFEST_PREFIX = "manifests/"
RESULTS_PREFIX = "manifest-results/"


REQUEST_KEY_PATTERN = r"^[A-Za-z0-9_-]{1,80}$"
# Keeps Postgres index names (e.g. idx_<table>_search_text_trgm, +21 chars) within the 63-byte limit.
MAX_TABLE_NAME_LEN = 41

MAX_DOCUMENTS = 500

Recommendation = Literal["MET", "UNCLEAR"]
Confidence = Literal["HIGH", "MEDIUM", "LOW"]


def _prefix(property_name: str, default: str) -> str:
    try:
        raw = Helper.get_property(property_name, default=default)
    except (KeyError, TypeError):
        raw = default
    cleaned = (raw if isinstance(raw, str) else default).strip().strip("/")
    return f"{cleaned}/" if cleaned else default


def manifest_object_key(request_key: str) -> str:
    return f"{_prefix('manifest_prefix', MANIFEST_PREFIX)}{request_key}/manifest.json"


def results_object_key(request_key: str) -> str:
    return f"{_prefix('results_prefix', RESULTS_PREFIX)}{request_key}/results.json"


def review_item_prefix(request_key: str) -> str:
    return f"{_prefix('results_prefix', RESULTS_PREFIX)}{request_key}/items/"


def manifest_embeddings_table_name(request_key: str, state: str) -> str:
    """Build a Postgres-safe embeddings table name from requestKey and state."""
    if not isinstance(request_key, str) or not re.fullmatch(REQUEST_KEY_PATTERN, request_key):
        raise ValueError(f"Invalid requestKey '{request_key}'.")
    code = (state or "").strip().lower()
    if len(code) != 2 or not code.isalpha():
        raise ValueError(f"Invalid state '{state}': expected a two-letter code.")

    slug = re.sub(r"[^a-z0-9]+", "_", request_key.lower()).strip("_") or "req"
    name = f"embeddings_{slug}_{code}"
    if len(name) > MAX_TABLE_NAME_LEN:
        digest = hashlib.sha256(f"{request_key}:{code}".encode("utf-8")).hexdigest()[:8]
        suffix = f"_{digest}_{code}"
        max_slug = MAX_TABLE_NAME_LEN - len("embeddings_") - len(suffix)
        short_slug = slug[:max_slug].rstrip("_")
        name = f"embeddings_{short_slug}{suffix}" if short_slug else f"embeddings_{digest}_{code}"
    return validate_embeddings_table_name(name)


class LegalRequirement(BaseModel):
    id: str = Field(min_length=1, description="Requirement record identifier")
    itemNo: str = Field(min_length=1, description="Requirement item number")
    legalCite: str = Field(min_length=1, description="Federal legal citation")
    requirement: str = Field(min_length=1, description="Requirement text to review")


class DocumentLocation(BaseModel):
    bucket: str = Field(min_length=3, description="S3 bucket name")
    path: str = Field(min_length=1, description="S3 object key")


class ManifestDocument(BaseModel):
    documentId: str = Field(min_length=1)
    s3: DocumentLocation


class Manifest(BaseModel):
    """Inbound manifest.json sent by MuleSoft after it uploads the documents."""

    requestKey: str = Field(pattern=REQUEST_KEY_PATTERN)
    reviewType: str = Field(min_length=1)
    state: str = Field(pattern=r"^[A-Za-z]{2}$", description="Two-letter US state code")
    legalRequirements: List[LegalRequirement] = Field(min_length=1)
    documents: List[ManifestDocument] = Field(min_length=1, max_length=MAX_DOCUMENTS)

    @field_validator("state")
    @classmethod
    def _upper_state(cls, state):
        return state.upper()

    @model_validator(mode="after")
    def _ids_are_unique(self):
        for label, ids in (
            ("legalRequirements[].id", [item.id for item in self.legalRequirements]),
            ("documents[].documentId", [doc.documentId for doc in self.documents]),
            ("documents[].s3.path", [doc.s3.path for doc in self.documents]),
        ):
            duplicates = sorted(value for value, count in Counter(ids).items() if count > 1)
            if duplicates:
                raise ValueError(f"{label} must be unique; duplicated: {', '.join(duplicates)}")
        return self


class RequirementResult(LegalRequirement):
    siRecommendation: Recommendation
    confidence: Confidence
    foundIn: str
    citedText: str
    rationale: str
    reasoning: str
    error: Optional[str] = None


class ReviewResults(BaseModel):
    requestKey: str
    reviewType: str
    state: str
    legalRequirements: List[RequirementResult]


class ManifestAccepted(BaseModel):
    requestKey: str
    status: Literal["PROCESSING"] = "PROCESSING"
    executionArn: Optional[str] = None
    documentCount: int
    requirementCount: int
    message: str


class ManifestStatus(BaseModel):
    requestKey: str
    status: Literal["PROCESSING", "COMPLETED", "FAILED"]
    startedAt: Optional[str] = None
    stoppedAt: Optional[str] = None
    resultsPath: Optional[str] = None
    error: Optional[str] = None
    cause: Optional[str] = None


class ManifestError(BaseModel):

    requestKey: Optional[str] = None
    status: str
    errors: List[str]
