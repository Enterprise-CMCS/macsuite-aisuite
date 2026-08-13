"""Regression tests for chunking synthetic contract language."""

import sys
import unittest
from pathlib import Path

_RAG_ROOT = Path(__file__).resolve().parents[1]
if str(_RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(_RAG_ROOT))

from data_embeddings_storage.database.recursive_text_splitter import (  # noqa: E402
    RecursiveCharacterTextSplitter,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "synthetic_contract.txt"
_GOLDEN_PREFIXES = [
    "SYNTHETIC SERVICES AGREEMENT\n\n1. Purpose",
    "ny, benefit program, or contract. Every ",
    "es not transfer operational responsibili",
    "ty minutes after a confirmed pickup time",
    "2.2 A defined term applies in both the s",
    "y invalid request, but its rejection not",
    " recorded separately from a provider-cau",
    "rriculum and a summary showing completio",
    " Records must remain searchable by reque",
    "w-up questions within fifteen Business D",
    "cy shall minimize operational disruption",
    "eiving party can document was lawfully p",
    "k is complete. A notice is not an admiss",
    "ction of a material Subcontractor, but i",
    " thirty days after receipt. The parties ",
    "nal invoice has already issued, in which",
    "7.1 Meridian shall maintain continuity p",
    "7.2 Neither party is responsible for a d",
    "nt qualifying event is not an excused de",
    "Agency may require a shorter cure period",
    "o disclose proprietary tools, license th",
    "9.1 Notices under this Agreement must be",
]


def _shared_edge_length(previous: str, current: str) -> int:
    """Return the longest suffix of previous that prefixes current."""
    for length in range(min(len(previous), len(current)), 0, -1):
        if previous[-length:] == current[:length]:
            return length
    return 0


class RecursiveTextSplitterFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = _FIXTURE_PATH.read_text(encoding="utf-8")
        cls.chunks = RecursiveCharacterTextSplitter().split_text(cls.fixture)

    def test_at_least_ninety_percent_of_chunks_end_at_a_text_boundary(self):
        boundary_aligned = [
            chunk.strip().endswith((".", ";", ":")) or chunk.endswith("\n")
            for chunk in self.chunks
        ]

        self.assertGreaterEqual(
            sum(boundary_aligned) / len(boundary_aligned),
            0.9,
        )

    def test_mean_consecutive_overlap_is_at_least_one_hundred_characters(self):
        overlap_lengths = [
            _shared_edge_length(previous, current)
            for previous, current in zip(self.chunks, self.chunks[1:])
        ]

        self.assertTrue(overlap_lengths)
        self.assertGreaterEqual(
            sum(overlap_lengths) / len(overlap_lengths),
            100,
        )

    def test_locked_defaults_match_golden_chunk_count_and_prefixes(self):
        self.assertEqual(len(self.chunks), 22)
        self.assertEqual(
            [chunk[:40] for chunk in self.chunks],
            _GOLDEN_PREFIXES,
        )


if __name__ == "__main__":
    unittest.main()
