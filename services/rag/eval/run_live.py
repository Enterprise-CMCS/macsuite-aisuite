"""Run an explicitly enabled evaluation against the requirements HTTP API."""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

try:
    import httpx
except ImportError:
    httpx = SimpleNamespace(Client=None, post=None)

from eval.dataset import read_jsonl, write_jsonl
from eval.labels import normalize_label


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--api-url",
        default=os.environ.get("REQUIREMENTS_API_URL", "http://127.0.0.1:8001"),
    )
    parser.add_argument("--model-id", default="unknown")
    parser.add_argument("--prompt-version", default="unknown")
    parser.add_argument("--contract-id", default="unknown")
    parser.add_argument("--run-id", default="unknown")
    parser.add_argument(
        "--retry-unclear",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if os.environ.get("AISUITE_EVAL_LIVE") != "1":
        print("Set AISUITE_EVAL_LIVE=1 to enable live evaluation.")
        return 2
    if httpx.Client is None:
        print("httpx is required for live evaluation.")
        return 2

    ground_truth = read_jsonl(args.ground_truth)
    payload = {
        "requirements": [
            {
                "id": row["requirement_id"],
                "text": row["requirement"],
            }
            for row in ground_truth
        ],
        "retry_unclear": args.retry_unclear,
    }
    api_key = os.environ.get("AISUITE_EVAL_API_KEY")
    client_options = {"timeout": 120.0}
    if api_key:
        client_options["headers"] = {"x-api-key": api_key}

    with httpx.Client(**client_options) as client:
        response = client.post(
            f"{args.api_url.rstrip('/')}/requirements",
            json=payload,
        )
        response.raise_for_status()
        results = response.json()["results"]

    timestamp = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    predictions = [
        {
            "requirement_id": result["id"],
            "tool_label": normalize_label(result.get("Recommendation")),
            "model_id": args.model_id,
            "retry_unclear": args.retry_unclear,
            "prompt_version": args.prompt_version,
            "contract_id": args.contract_id,
            "run_id": args.run_id,
            "timestamp": timestamp,
        }
        for result in results
    ]
    write_jsonl(args.output, predictions)
    return 0


if __name__ == "__main__":
    sys.exit(main())
