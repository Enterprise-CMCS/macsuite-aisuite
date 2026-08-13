"""Tests for extracting synthetic CRT workbooks into ground-truth JSONL."""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

# Earlier-loaded tests may have replaced numpy; pandas requires the real package.
_numpy = sys.modules.get("numpy")
if isinstance(_numpy, MagicMock):
    del sys.modules["numpy"]

try:
    import openpyxl  # noqa: F401
    import pandas as pd

    HAS_WORKBOOK_DEPS = True
except (ImportError, ModuleNotFoundError):
    pd = None
    HAS_WORKBOOK_DEPS = False

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))


@unittest.skipUnless(
    HAS_WORKBOOK_DEPS,
    "pandas and openpyxl are required for evaluation extractor tests",
)
class EvalExtractCrtTests(unittest.TestCase):
    def _write_workbook(self, path, rows, header_row=10):
        pd.DataFrame(rows).to_excel(path, index=False, startrow=header_row)

    def test_extracts_nonempty_requirements_without_rag_response(self):
        from eval import extract_crt
        from eval.dataset import read_jsonl, requirement_id

        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "review.xlsx"
            output = Path(directory) / "ground-truth.jsonl"
            self._write_workbook(
                workbook,
                {
                    "Requirement": [
                        "Lorem requirement alpha.",
                        "Lorem requirement bravo.",
                        "",
                    ],
                    "Recommendation": ["MET", "Not Met", "UNCLEAR"],
                    "RAG Response": ["private alpha", "private bravo", "private empty"],
                    "Source": ["Lorem: 1", "Lorem: 2", "Lorem: 3"],
                },
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = extract_crt.main(
                    ["--input", str(workbook), "--output", str(output)]
                )

            self.assertEqual(result, 0)
            records = read_jsonl(output)
            self.assertEqual(len(records), 2)
            self.assertEqual(
                records[0],
                {
                    "requirement_id": requirement_id("Lorem requirement alpha."),
                    "requirement": "Lorem requirement alpha.",
                    "human_label": "MET",
                    "source_row": 11,
                },
            )
            self.assertEqual(records[1]["human_label"], "NOT_MET")
            self.assertEqual(records[1]["source_row"], 12)
            self.assertNotIn("RAG Response", output.read_text(encoding="utf-8"))
            self.assertNotIn("private alpha", output.read_text(encoding="utf-8"))

            summary = stdout.getvalue().lower()
            self.assertIn("total rows", summary)
            self.assertIn("3", summary)
            self.assertIn("rows with human verdict", summary)
            self.assertIn("distinct raw verdicts", summary)
            self.assertIn("not met", summary)

    def test_existing_output_without_force_exits_two_and_is_unchanged(self):
        from eval import extract_crt

        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "review.xlsx"
            output = Path(directory) / "ground-truth.jsonl"
            self._write_workbook(
                workbook,
                {
                    "Requirement": ["Lorem requirement."],
                    "Recommendation": ["MET"],
                    "RAG Response": [""],
                    "Source": [""],
                },
            )
            output.write_bytes(b"sentinel\n")

            result = extract_crt.main(
                ["--input", str(workbook), "--output", str(output)]
            )

            self.assertEqual(result, 2)
            self.assertEqual(output.read_bytes(), b"sentinel\n")

    def test_unknown_verdict_lists_value_and_writes_no_output(self):
        from eval import extract_crt

        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "review.xlsx"
            output = Path(directory) / "ground-truth.jsonl"
            self._write_workbook(
                workbook,
                {
                    "Requirement": ["Lorem requirement."],
                    "Recommendation": ["Deficient"],
                    "RAG Response": [""],
                    "Source": [""],
                },
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = extract_crt.main(
                    ["--input", str(workbook), "--output", str(output)]
                )

            self.assertNotEqual(result, 0)
            self.assertIn("Deficient", stdout.getvalue())
            self.assertFalse(output.exists())

    def test_header_row_index_is_patchable(self):
        from eval import extract_crt
        from eval.dataset import read_jsonl

        with tempfile.TemporaryDirectory() as directory:
            workbook = Path(directory) / "review.xlsx"
            output = Path(directory) / "ground-truth.jsonl"
            self._write_workbook(
                workbook,
                {
                    "Requirement": ["Lorem requirement."],
                    "Recommendation": ["UNCLEAR"],
                    "RAG Response": [""],
                    "Source": [""],
                },
                header_row=0,
            )

            with patch.object(extract_crt, "HEADER_ROW_INDEX", 0):
                result = extract_crt.main(
                    ["--input", str(workbook), "--output", str(output)]
                )

            self.assertEqual(result, 0)
            self.assertEqual(len(read_jsonl(output)), 1)


if __name__ == "__main__":
    unittest.main()
