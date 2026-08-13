"""Extract human CRT verdicts into deterministic ground-truth JSONL."""

import argparse
import sys
from pathlib import Path

import pandas as pd

from eval.dataset import requirement_id, write_jsonl
from eval.labels import UnknownLabelError, normalize_label
from search.excel_process.crt_layout import HEADER_ROW_INDEX


def _text(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.output.exists() and not args.force:
        print(f"Refusing to overwrite existing output: {args.output}")
        return 2

    dataframe = pd.read_excel(
        args.input, header=HEADER_ROW_INDEX, engine="openpyxl"
    )
    required_columns = {"Requirement", "Recommendation"}
    missing_columns = required_columns.difference(dataframe.columns)
    if missing_columns:
        print(f"Missing required columns: {', '.join(sorted(missing_columns))}")
        return 1

    raw_verdicts = [
        _text(value)
        for value in dataframe["Recommendation"]
        if _text(value)
    ]
    distinct_raw_verdicts = list(dict.fromkeys(raw_verdicts))
    unknown_verdicts = []
    for raw_verdict in distinct_raw_verdicts:
        try:
            normalize_label(raw_verdict)
        except UnknownLabelError:
            unknown_verdicts.append(raw_verdict)

    print(f"Total rows: {len(dataframe)}")
    print(f"Rows with human verdict: {len(raw_verdicts)}")
    print(
        "Distinct raw verdicts: "
        + (", ".join(distinct_raw_verdicts) if distinct_raw_verdicts else "(none)")
    )

    if unknown_verdicts:
        print("Unknown verdicts: " + ", ".join(unknown_verdicts))
        return 1

    records = []
    for index, row in dataframe.iterrows():
        requirement = _text(row["Requirement"])
        if not requirement:
            continue
        records.append(
            {
                "requirement_id": requirement_id(requirement),
                "requirement": requirement,
                "human_label": normalize_label(_text(row["Recommendation"])),
                "source_row": HEADER_ROW_INDEX + 1 + int(index),
            }
        )

    write_jsonl(args.output, records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
