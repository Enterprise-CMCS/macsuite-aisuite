"""Tests for the CRT workbook requirements API client."""

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Earlier-loaded tests stub numpy via install_offline_stubs(); pandas needs the real package.
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

try:
    from _stubs import install_offline_stubs
except ModuleNotFoundError:
    from tests._stubs import install_offline_stubs


install_offline_stubs()

if pd is None:
    sys.modules.setdefault("pandas", MagicMock(name="pandas"))

try:
    import httpx  # noqa: F401
except (ImportError, ModuleNotFoundError):
    httpx = types.ModuleType("httpx")

    class _HTTPError(Exception):
        pass

    httpx.HTTPError = _HTTPError
    httpx.ConnectError = _HTTPError
    httpx.AsyncClient = MagicMock
    sys.modules["httpx"] = httpx

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from search.excel_process import process_excel_with_rag as excel_client  # noqa: E402
from search.excel_process.crt_layout import (  # noqa: E402
    RAG_RESPONSE_COL,
    RECOMMENDATION_COL,
    REQUIREMENT_COL,
    SOURCE_COL,
)


def _api_result(row_id, recommendation, response, source):
    return {
        "id": str(row_id),
        "success": recommendation != "ERROR",
        "error": None,
        "Requirement": f"Requirement {row_id}",
        "Recommendation": recommendation,
        "Response": response,
        "Source": source,
        "Page": "",
    }


class ExcelClientArgumentTests(unittest.TestCase):
    def test_parse_args_exposes_client_flags(self):
        args = excel_client.parse_args(
            [
                "--input",
                "input-workbook",
                "--output",
                "output-workbook",
                "--api-url",
                "http://api.example",
                "--max-rows",
                "12",
                "--batch-size",
                "3",
            ]
        )

        self.assertEqual(args.input, Path("input-workbook"))
        self.assertEqual(args.output, Path("output-workbook"))
        self.assertEqual(args.api_url, "http://api.example")
        self.assertEqual(args.max_rows, 12)
        self.assertEqual(args.batch_size, 3)


@unittest.skipUnless(
    HAS_WORKBOOK_DEPS,
    "pandas and openpyxl are required for workbook client tests",
)
class ExcelClientBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunks_rows_and_maps_shuffled_results_by_id(self):
        processor = excel_client.ExcelRAGProcessor(
            "input-workbook",
            api_url="http://api.example/",
            batch_size=2,
        )
        processor.df = pd.DataFrame(
            {REQUIREMENT_COL: ["First", "Second", "Third"]},
            index=[12, 15, 19],
        )
        for column in [RECOMMENDATION_COL, RAG_RESPONSE_COL, SOURCE_COL]:
            processor.df[column] = pd.NA

        first_response = MagicMock()
        first_response.raise_for_status.return_value = None
        first_response.json.return_value = {
            "results": [
                _api_result(15, "NOT MET", "second response", "B: 2"),
                _api_result(12, "MET", "first response", "A: 1"),
            ]
        }
        second_response = MagicMock()
        second_response.raise_for_status.return_value = None
        second_response.json.return_value = {
            "results": [
                _api_result(19, "UNCLEAR", "third response", "C: 3"),
            ]
        }

        http_client = MagicMock()
        http_client.post = AsyncMock(
            side_effect=[first_response, second_response]
        )
        http_client.aclose = AsyncMock()

        with patch.object(
            excel_client.httpx,
            "AsyncClient",
            return_value=http_client,
        ):
            await processor.process_all()

        self.assertEqual(http_client.post.await_count, 2)
        first_call = http_client.post.await_args_list[0]
        second_call = http_client.post.await_args_list[1]
        self.assertEqual(first_call.args[0], "http://api.example/requirements")
        self.assertEqual(
            first_call.kwargs["json"],
            {
                "requirements": [
                    {"id": "12", "text": "First"},
                    {"id": "15", "text": "Second"},
                ],
                "retry_unclear": True,
            },
        )
        self.assertEqual(
            second_call.kwargs["json"]["requirements"],
            [{"id": "19", "text": "Third"}],
        )
        self.assertEqual(processor.df.at[12, RECOMMENDATION_COL], "MET")
        self.assertEqual(
            processor.df.at[12, RAG_RESPONSE_COL],
            "first response",
        )
        self.assertEqual(processor.df.at[12, SOURCE_COL], "A: 1")
        self.assertEqual(processor.df.at[15, RECOMMENDATION_COL], "NOT MET")
        self.assertEqual(processor.df.at[15, RAG_RESPONSE_COL], "second response")
        self.assertEqual(processor.df.at[15, SOURCE_COL], "B: 2")
        http_client.aclose.assert_awaited_once()

    async def test_http_error_marks_each_batch_row_error(self):
        processor = excel_client.ExcelRAGProcessor(
            "input-workbook",
            batch_size=25,
        )
        processor.df = pd.DataFrame(
            {REQUIREMENT_COL: ["First", "Second"]},
            index=[4, 9],
        )
        for column in [RECOMMENDATION_COL, RAG_RESPONSE_COL, SOURCE_COL]:
            processor.df[column] = pd.NA

        http_client = MagicMock()
        http_client.post = AsyncMock(
            side_effect=excel_client.httpx.ConnectError("offline")
        )
        http_client.aclose = AsyncMock()

        await processor.process_all(client=http_client)

        self.assertEqual(
            processor.df[RECOMMENDATION_COL].tolist(),
            ["ERROR", "ERROR"],
        )
        self.assertEqual(processor.df[SOURCE_COL].tolist(), ["N/A", "N/A"])
        self.assertEqual(processor.error_count, 2)


class ExcelClientSourceTests(unittest.TestCase):
    def test_client_has_no_embedded_agent_parser(self):
        source = Path(excel_client.__file__).read_text(encoding="utf-8")

        self.assertNotIn("search" + "_agent", source)
        self.assertNotIn("_parse" + "_agent_response", source)


if __name__ == "__main__":
    unittest.main()
