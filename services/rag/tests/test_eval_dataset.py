"""Tests for deterministic JSONL evaluation datasets."""

import tempfile
import unittest
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = RAG_ROOT / "tests" / "fixtures" / "eval"

import sys

if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))


class EvalDatasetTests(unittest.TestCase):
    def test_ground_truth_fixture_round_trip_is_byte_identical(self):
        from eval.dataset import read_jsonl, write_jsonl

        source = FIXTURES / "ground_truth.jsonl"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "ground_truth.jsonl"
            write_jsonl(output, read_jsonl(source))
            self.assertEqual(output.read_bytes(), source.read_bytes())

    def test_write_jsonl_is_compact_utf8_with_trailing_newline(self):
        from eval.dataset import write_jsonl

        records = [
            {
                "requirement_id": "0123456789abcdef",
                "requirement": "Lorem café.",
                "human_label": "MET",
                "source_row": 11,
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "records.jsonl"
            write_jsonl(output, records)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                '{"requirement_id":"0123456789abcdef","requirement":"Lorem café.",'
                '"human_label":"MET","source_row":11}\n',
            )


if __name__ == "__main__":
    unittest.main()
