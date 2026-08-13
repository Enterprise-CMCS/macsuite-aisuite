"""Score an evaluation prediction file without network or runtime dependencies."""

import argparse
import json
import sys
from pathlib import Path

from eval.dataset import read_jsonl
from eval.metrics import score


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--json-out", required=True, type=Path)
    parser.add_argument("--md-out", required=True, type=Path)
    parser.add_argument("--fail-under-agreement", type=float)
    return parser


def _consistent_metadata(predictions: list[dict], key: str):
    if not predictions or any(key not in prediction for prediction in predictions):
        return "unknown"
    values = {prediction[key] for prediction in predictions}
    return values.pop() if len(values) == 1 else "unknown"


def _format_rate(value) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def _markdown(metrics: dict, predictions: list[dict]) -> str:
    retry_unclear = _consistent_metadata(predictions, "retry_unclear")
    model_id = _consistent_metadata(predictions, "model_id")
    interval = metrics["agreement_ci95"]
    interval_text = (
        "n/a"
        if interval is None
        else f"{interval[0]:.2%}–{interval[1]:.2%}"
    )
    return "\n".join(
        [
            "# Evaluation report",
            "",
            f"- Agreement rate: {_format_rate(metrics['agreement_rate'])} "
            f"(n = {metrics['n_scored']}, 95% CI {interval_text})",
            "- UNCLEAR on human-decided: "
            f"{_format_rate(metrics['unclear_on_decided_rate'])}",
            f"- Decided agreement rate: "
            f"{_format_rate(metrics['decided_agreement_rate'])}",
            f"- False MET rate: {_format_rate(metrics['false_met_rate'])}",
            f"- Error rate: {_format_rate(metrics['error_rate'])}",
            f"- Coverage: {_format_rate(metrics['coverage'])}",
            f"- retry_unclear: {retry_unclear}",
            f"- model_id: {model_id}",
            "",
        ]
    )


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    ground_truth = read_jsonl(args.ground_truth)
    predictions = read_jsonl(args.predictions)
    metrics = score(ground_truth, predictions)

    args.json_out.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.md_out.write_text(_markdown(metrics, predictions), encoding="utf-8")

    agreement_rate = metrics["agreement_rate"]
    if (
        args.fail_under_agreement is not None
        and agreement_rate is not None
        and agreement_rate < args.fail_under_agreement
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
