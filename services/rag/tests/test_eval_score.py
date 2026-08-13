"""Tests for the offline evaluation scoring CLI."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

FIXTURES = RAG_ROOT / "tests" / "fixtures" / "eval"


class EvalScoreCliTests(unittest.TestCase):
    def test_score_imports_with_aws_environment_unset(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}
        result = subprocess.run(
            [sys.executable, "-c", "import eval.score"],
            cwd=RAG_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_writes_json_and_markdown_without_requirement_text(self):
        from eval import score as score_cli

        with tempfile.TemporaryDirectory() as directory:
            json_out = Path(directory) / "report.json"
            md_out = Path(directory) / "report.md"
            result = score_cli.main(
                [
                    "--ground-truth",
                    str(FIXTURES / "ground_truth.jsonl"),
                    "--predictions",
                    str(FIXTURES / "predictions.jsonl"),
                    "--json-out",
                    str(json_out),
                    "--md-out",
                    str(md_out),
                ]
            )

            self.assertEqual(result, 0)
            metrics = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(metrics["n_scored"], 9)
            markdown = md_out.read_text(encoding="utf-8")
            self.assertIn("Agreement rate", markdown)
            self.assertIn("UNCLEAR on human-decided", markdown)
            self.assertIn("n = ", markdown)
            self.assertIn("retry_unclear", markdown)
            self.assertIn("us.amazon.nova-pro-v1:0", markdown)

            requirement_texts = [
                json.loads(line)["requirement"]
                for line in (FIXTURES / "ground_truth.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            for requirement in requirement_texts:
                self.assertNotIn(requirement, markdown)
                self.assertNotIn(requirement, json_out.read_text(encoding="utf-8"))

    def test_fail_under_agreement_returns_one_and_high_agreement_returns_zero(self):
        from eval import score as score_cli
        from eval.dataset import write_jsonl

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            low_result = score_cli.main(
                [
                    "--ground-truth",
                    str(FIXTURES / "ground_truth.jsonl"),
                    "--predictions",
                    str(FIXTURES / "predictions.jsonl"),
                    "--json-out",
                    str(directory / "low.json"),
                    "--md-out",
                    str(directory / "low.md"),
                    "--fail-under-agreement",
                    "0.9",
                ]
            )

            high_gt = directory / "high-gt.jsonl"
            high_predictions = directory / "high-predictions.jsonl"
            write_jsonl(
                high_gt,
                [
                    {
                        "requirement_id": "0123456789abcdef",
                        "requirement": "Lorem requirement.",
                        "human_label": "MET",
                        "source_row": 11,
                    }
                ],
            )
            write_jsonl(
                high_predictions,
                [{"requirement_id": "0123456789abcdef", "tool_label": "MET"}],
            )
            high_result = score_cli.main(
                [
                    "--ground-truth",
                    str(high_gt),
                    "--predictions",
                    str(high_predictions),
                    "--json-out",
                    str(directory / "high.json"),
                    "--md-out",
                    str(directory / "high.md"),
                    "--fail-under-agreement",
                    "0.9",
                ]
            )

            self.assertEqual(low_result, 1)
            self.assertEqual(high_result, 0)

    def test_markdown_uses_unknown_for_missing_run_metadata(self):
        from eval import score as score_cli
        from eval.dataset import write_jsonl

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            predictions = directory / "predictions.jsonl"
            write_jsonl(
                predictions,
                [{"requirement_id": "166f4ef17e08b4c5", "tool_label": "MET"}],
            )
            markdown = directory / "report.md"

            result = score_cli.main(
                [
                    "--ground-truth",
                    str(FIXTURES / "ground_truth.jsonl"),
                    "--predictions",
                    str(predictions),
                    "--json-out",
                    str(directory / "report.json"),
                    "--md-out",
                    str(markdown),
                ]
            )

            self.assertEqual(result, 0)
            self.assertIn("unknown", markdown.read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    unittest.main()
