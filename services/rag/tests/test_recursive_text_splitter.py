"""Behavioral tests for recursive contract-text chunking."""

import sys
import unittest
from pathlib import Path

_RAG_ROOT = Path(__file__).resolve().parents[1]
if str(_RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(_RAG_ROOT))

from data_embeddings_storage.database.recursive_text_splitter import (  # noqa: E402
    RecursiveCharacterTextSplitter,
)


def _shared_edge_length(previous: str, current: str) -> int:
    """Return the longest suffix of previous that prefixes current."""
    for length in range(min(len(previous), len(current)), 0, -1):
        if previous[-length:] == current[:length]:
            return length
    return 0


def _reconstruct(chunks: list[str]) -> str:
    """Join chunks after removing their real suffix/prefix overlap."""
    if not chunks:
        return ""

    reconstructed = chunks[0]
    previous = chunks[0]
    for current in chunks[1:]:
        overlap_length = _shared_edge_length(previous, current)
        reconstructed += current[overlap_length:]
        previous = current
    return reconstructed


class RecursiveCharacterTextSplitterTests(unittest.TestCase):
    def test_empty_and_whitespace_only_input_return_no_chunks(self):
        splitter = RecursiveCharacterTextSplitter(chunk_size=20, chunk_overlap=5)

        self.assertEqual(
            (splitter.split_text(""), splitter.split_text(" \n\t  ")),
            ([], []),
        )

    def test_short_text_returns_one_unchanged_chunk(self):
        splitter = RecursiveCharacterTextSplitter(chunk_size=20, chunk_overlap=5)
        text = "short text"

        self.assertEqual(splitter.split_text(text), [text])

    def test_paragraph_boundary_is_preferred_without_splitting_words(self):
        splitter = RecursiveCharacterTextSplitter(chunk_size=15, chunk_overlap=0)
        text = "Alpha bravo\n\nCharlie delta"

        chunks = splitter.split_text(text)
        boundary_aligned = any(
            left.endswith("\n\n") or right.startswith("\n\n")
            for left, right in zip(chunks, chunks[1:])
        )
        words_are_intact = all(
            any(word in chunk for chunk in chunks)
            for word in ("Alpha", "bravo", "Charlie", "delta")
        )

        self.assertEqual((len(chunks), boundary_aligned, words_are_intact), (2, True, True))

    def test_no_chunk_exceeds_chunk_size(self):
        splitter = RecursiveCharacterTextSplitter(chunk_size=17, chunk_overlap=4)
        text = "Alpha beta gamma. Delta epsilon zeta. Eta theta iota."

        self.assertTrue(
            all(len(chunk) <= splitter.chunk_size for chunk in splitter.split_text(text))
        )

    def test_consecutive_chunks_share_real_configured_overlap(self):
        splitter = RecursiveCharacterTextSplitter(chunk_size=20, chunk_overlap=5)
        text = "alpha beta gamma delta epsilon zeta eta theta"

        chunks = splitter.split_text(text)
        overlap_lengths = [
            _shared_edge_length(previous, current)
            for previous, current in zip(chunks, chunks[1:])
        ]

        self.assertTrue(
            overlap_lengths
            and all(
                length >= min(splitter.chunk_overlap, len(previous))
                for length, previous in zip(overlap_lengths, chunks)
            )
        )

    def test_removing_actual_overlaps_reconstructs_input(self):
        splitter = RecursiveCharacterTextSplitter(chunk_size=20, chunk_overlap=5)
        text = "Alpha beta.\n\nGamma delta; epsilon zeta eta theta."

        chunks = splitter.split_text(text)

        self.assertEqual(_reconstruct(chunks), text)

    def test_identical_calls_are_byte_identical(self):
        splitter = RecursiveCharacterTextSplitter(chunk_size=20, chunk_overlap=5)
        text = "Alpha beta. Gamma delta.\n\nEpsilon zeta eta."

        self.assertEqual(splitter.split_text(text), splitter.split_text(text))

    def test_overlap_equal_to_or_larger_than_chunk_size_is_rejected(self):
        def construction_error(overlap: int):
            try:
                RecursiveCharacterTextSplitter(chunk_size=20, chunk_overlap=overlap)
            except Exception as error:  # noqa: BLE001 - the type is the assertion
                return type(error)
            return None

        self.assertEqual(
            (construction_error(20), construction_error(21)),
            (ValueError, ValueError),
        )

    def test_oversized_unsplittable_run_is_hard_split_at_chunk_size(self):
        splitter = RecursiveCharacterTextSplitter(chunk_size=20, chunk_overlap=0)
        text = "x" * 45

        self.assertEqual(splitter.split_text(text), ["x" * 20, "x" * 20, "x" * 5])

    def test_paragraph_separator_precedes_sentence_separator_in_cascade(self):
        splitter = RecursiveCharacterTextSplitter(chunk_size=24, chunk_overlap=0)
        first_paragraph = "Alpha one. Beta two."
        text = f"{first_paragraph}\n\nGamma three. Delta four."

        chunks = splitter.split_text(text)
        paragraph_boundary_aligned = any(
            left.endswith("\n\n") or right.startswith("\n\n")
            for left, right in zip(chunks, chunks[1:])
        )

        self.assertEqual(
            (
                paragraph_boundary_aligned,
                any(first_paragraph in chunk for chunk in chunks),
            ),
            (True, True),
        )

    def test_constructor_defaults_expose_contract_separator_profile(self):
        splitter = RecursiveCharacterTextSplitter()

        self.assertEqual(
            (
                splitter.chunk_size,
                splitter.chunk_overlap,
                getattr(splitter, "separators", None),
            ),
            (
                1024,
                150,
                ["\n\n", "\n", ". ", "; ", " ", ""],
            ),
        )


if __name__ == "__main__":
    unittest.main()
