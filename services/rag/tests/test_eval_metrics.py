"""Tests for offline evaluation metrics."""

import sys
import unittest
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

FIXTURES = RAG_ROOT / "tests" / "fixtures" / "eval"
EXPECTED_KEYS = {
    "n_ground_truth",
    "n_scored",
    "n_decided",
    "n_unmatched_predictions",
    "coverage",
    "agreement_rate",
    "agreement_ci95",
    "decided_agreement_rate",
    "unclear_on_decided_rate",
    "false_met_rate",
    "error_rate",
    "confusion",
}


class EvalMetricsTests(unittest.TestCase):
    def _fixture_score(self):
        from eval.dataset import read_jsonl
        from eval.metrics import score

        return score(
            read_jsonl(FIXTURES / "ground_truth.jsonl"),
            read_jsonl(FIXTURES / "predictions.jsonl"),
        )

    def test_fixture_pair_has_exact_metric_contract_and_values(self):
        from eval.metrics import wilson_interval

        result = self._fixture_score()

        self.assertEqual(set(result), EXPECTED_KEYS)
        self.assertEqual(result["n_ground_truth"], 10)
        self.assertEqual(result["n_scored"], 9)
        self.assertEqual(result["n_decided"], 7)
        self.assertEqual(result["n_unmatched_predictions"], 1)
        self.assertEqual(result["coverage"], 0.9)
        self.assertEqual(result["agreement_rate"], 4 / 9)
        self.assertEqual(result["agreement_ci95"], wilson_interval(4, 9))
        self.assertEqual(result["decided_agreement_rate"], 3 / 7)
        self.assertEqual(result["unclear_on_decided_rate"], 2 / 7)
        self.assertEqual(result["false_met_rate"], 1 / 3)
        self.assertEqual(result["error_rate"], 1 / 9)

    def test_confusion_matrix_totals_n_scored(self):
        result = self._fixture_score()
        total = sum(
            count
            for predicted_counts in result["confusion"].values()
            for count in predicted_counts.values()
        )
        self.assertEqual(total, 9)

    def test_zero_denominator_metrics_are_none(self):
        from eval.metrics import score

        result = score(
            [
                {
                    "requirement_id": "0123456789abcdef",
                    "requirement": "Lorem requirement.",
                    "human_label": "UNCLEAR",
                    "source_row": 11,
                }
            ],
            [{"requirement_id": "0123456789abcdef", "tool_label": "UNCLEAR"}],
        )

        self.assertEqual(result["n_decided"], 0)
        self.assertIsNone(result["decided_agreement_rate"])
        self.assertIsNone(result["unclear_on_decided_rate"])
        self.assertIsNone(result["false_met_rate"])

    def test_unmatched_and_missing_predictions_do_not_inflate_scored(self):
        result = self._fixture_score()

        self.assertEqual(result["n_unmatched_predictions"], 1)
        self.assertEqual(result["n_scored"], 9)
        self.assertEqual(result["coverage"], 0.9)

    def test_wilson_interval_matches_reference(self):
        from eval.metrics import wilson_interval

        low, high = wilson_interval(8, 10, z=1.959964)
        self.assertEqual((round(low, 4), round(high, 4)), (0.4902, 0.9433))


if __name__ == "__main__":
    unittest.main()
