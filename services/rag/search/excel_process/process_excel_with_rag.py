#!/usr/bin/env python3

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

import httpx
import pandas as pd

src_path = Path(__file__).parent.parent.parent
sys.path.insert(0, str(src_path))

from common.utils.logger import get_logger
from search.excel_process.crt_layout import (
    DATA_START_INDEX,
    HEADER_ROW_INDEX,
    RAG_RESPONSE_COL,
    RECOMMENDATION_COL,
    REQUIREMENT_COL,
    SOURCE_COL,
)

logger = get_logger(__name__)

if DATA_START_INDEX != HEADER_ROW_INDEX + 1:
    raise RuntimeError("CRT data rows must start immediately after the header row")

DEFAULT_API_URL = "http://127.0.0.1:8001"
DEFAULT_BATCH_SIZE = 25


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description="Grade requirements from a CRT workbook through the requirements API."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--api-url",
        default=os.environ.get("REQUIREMENTS_API_URL", DEFAULT_API_URL),
    )
    parser.add_argument("--max-rows", type=positive_int)
    parser.add_argument("--batch-size", type=positive_int, default=DEFAULT_BATCH_SIZE)
    return parser.parse_args(args)


class ExcelRAGProcessor:
    def __init__(
        self,
        excel_file_path: str,
        api_url: str = DEFAULT_API_URL,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ):
        self.excel_file_path = Path(excel_file_path)
        self.api_url = api_url.rstrip("/")
        self.batch_size = batch_size
        self.api_key = os.environ.get("AISUITE_EVAL_API_KEY") or os.environ.get("API_KEY")
        if not self.api_key:
            raise ValueError(
                "AISUITE_EVAL_API_KEY or API_KEY must be set to call POST /requirements"
            )
        self.df: Optional[pd.DataFrame] = None
        self.processed_count = 0
        self.error_count = 0
        logger.info(f"Initialized ExcelRAGProcessor for: {self.excel_file_path}")

    def load_excel(self):
        if not self.excel_file_path.exists():
            raise FileNotFoundError(f"Excel file not found: {self.excel_file_path}")

        logger.info(f"Loading Excel file: {self.excel_file_path}")
        self.df = pd.read_excel(
            self.excel_file_path,
            header=HEADER_ROW_INDEX,
            engine="openpyxl",
        )
        logger.info(
            f"Loaded dataframe with {len(self.df)} data rows, "
            f"columns: {list(self.df.columns)}"
        )

        for col in [RECOMMENDATION_COL, RAG_RESPONSE_COL, SOURCE_COL]:
            self.df[col] = pd.Series(
                [pd.NA] * len(self.df),
                dtype="object",
                index=self.df.index,
            )

        return self.df

    def _set_error(self, idx, message: str):
        self.df.at[idx, RECOMMENDATION_COL] = "ERROR"
        self.df.at[idx, RAG_RESPONSE_COL] = message
        self.df.at[idx, SOURCE_COL] = "N/A"
        self.error_count += 1

    async def _process_batch(self, client: httpx.AsyncClient, rows):
        payload = {
            "requirements": [
                {"id": str(idx), "text": requirement}
                for idx, requirement in rows
            ],
            "retry_unclear": True,
        }

        try:
            response = await client.post(
                f"{self.api_url}/requirements",
                json=payload,
                headers={"x-api-key": self.api_key},
            )
            response.raise_for_status()
            response_body = response.json()
            results_by_id = {
                str(result["id"]): result
                for result in response_body["results"]
            }
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            logger.error(f"Requirements batch failed: {exc}")
            for idx, _ in rows:
                self._set_error(idx, f"Requirements API batch failed: {exc}")
            self.processed_count += len(rows)
            return

        for idx, _ in rows:
            result = results_by_id.get(str(idx))
            if result is None:
                self._set_error(
                    idx,
                    f"Requirements API returned no result for row {idx}",
                )
            else:
                self.df.at[idx, RECOMMENDATION_COL] = result["Recommendation"]
                self.df.at[idx, RAG_RESPONSE_COL] = result["Response"]
                self.df.at[idx, SOURCE_COL] = result["Source"]
                if result["Recommendation"] == "ERROR":
                    self.error_count += 1
            self.processed_count += 1

    async def process_all(
        self,
        max_rows: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None,
    ):
        if self.df is None:
            raise RuntimeError("Call load_excel() first")

        requirement_rows = []
        for idx in self.df[self.df[REQUIREMENT_COL].notna()].index:
            requirement = str(self.df.at[idx, REQUIREMENT_COL]).strip()
            if requirement:
                requirement_rows.append((idx, requirement))

        if max_rows is not None:
            requirement_rows = requirement_rows[:max_rows]

        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))

        try:
            for start in range(0, len(requirement_rows), self.batch_size):
                await self._process_batch(
                    client,
                    requirement_rows[start : start + self.batch_size],
                )
        finally:
            if owns_client:
                await client.aclose()

    def _default_output_path(self) -> Path:
        return self.excel_file_path.with_name(
            f"{self.excel_file_path.stem}_rag_results"
            f"{self.excel_file_path.suffix}"
        )

    def save_progress(self, output_path: Optional[Path] = None):
        output_path = output_path or self._default_output_path()
        try:
            self._save_to_excel(output_path)
            logger.info(f"[SAVED] Progress saved to: {output_path}")
        except Exception as exc:
            logger.error(f"Error saving progress: {exc}")

    def save_final(self, output_path: Optional[Path] = None):
        output_path = output_path or self._default_output_path()
        self._save_to_excel(output_path)
        logger.info(f"[SUCCESS] FINAL EXCEL SAVED: {output_path}")
        logger.info(f"Total requirements processed: {self.processed_count}")
        logger.info(f"Errors encountered: {self.error_count}")

    def _save_to_excel(self, output_path: Path):
        output_path = Path(output_path)
        if output_path.resolve() == self.excel_file_path.resolve():
            raise ValueError("Output path must be different from the input workbook")

        header_df = pd.read_excel(
            self.excel_file_path,
            header=None,
            nrows=HEADER_ROW_INDEX,
            engine="openpyxl",
        )

        with pd.ExcelWriter(str(output_path), engine="openpyxl") as writer:
            header_df.to_excel(writer, index=False, header=False, startrow=0)
            self.df.to_excel(writer, index=False, startrow=HEADER_ROW_INDEX)


async def main(argv=None):
    args = parse_args(argv)
    processor = ExcelRAGProcessor(
        str(args.input),
        api_url=args.api_url,
        batch_size=args.batch_size,
    )
    processor.load_excel()
    await processor.process_all(max_rows=args.max_rows)
    processor.save_final(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
