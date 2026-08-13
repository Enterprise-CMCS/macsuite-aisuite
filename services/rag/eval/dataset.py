"""Deterministic JSONL helpers for evaluation datasets."""

import hashlib
import json
from pathlib import Path
from typing import Iterable


def requirement_id(requirement: str) -> str:
    """Build the stable, privacy-preserving join key for requirement text."""
    normalized = " ".join(requirement.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def read_jsonl(path: str | Path) -> list[dict]:
    """Read non-empty JSONL records in file order."""
    with Path(path).open("r", encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def write_jsonl(path: str | Path, records: Iterable[dict]) -> None:
    """Write compact UTF-8 JSONL with a newline after every record."""
    with Path(path).open("w", encoding="utf-8", newline="\n") as output:
        for record in records:
            output.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
