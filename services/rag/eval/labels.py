"""Canonical labels shared by evaluation data and reports."""

MET = "MET"
NOT_MET = "NOT_MET"
UNCLEAR = "UNCLEAR"
ERROR = "ERROR"

HUMAN_LABELS = (MET, NOT_MET, UNCLEAR)
TOOL_LABELS = (MET, NOT_MET, UNCLEAR, ERROR)

# Raw values are normalized to uppercase with collapsed whitespace before lookup.
RAW_TO_CANONICAL = {
    "MET": MET,
    "NOT MET": NOT_MET,
    "NOT_MET": NOT_MET,
    "UNCLEAR": UNCLEAR,
    "ERROR": ERROR,
}


class UnknownLabelError(ValueError):
    """Raised when a non-empty label is outside the accepted vocabulary."""


def normalize_label(raw_label: str | None) -> str | None:
    """Return the canonical form of a raw verdict."""
    if raw_label is None:
        return None

    normalized = " ".join(raw_label.strip().split()).upper()
    if not normalized:
        return None

    try:
        return RAW_TO_CANONICAL[normalized]
    except KeyError as error:
        raise UnknownLabelError(f"Unknown label: {raw_label}") from error
