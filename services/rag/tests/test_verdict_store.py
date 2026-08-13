"""Red-phase tests for best-effort verdict persistence."""

# Expected production API:
# async def record_verdict(
#     *, source: str, requirement_text: str, verdict: str,
#     response_text: str | None = None, source_text: str | None = None,
#     page_text: str | None = None, raw_output: str | None = None,
#     parsed_ok: bool, model_id: str, embed_model_id: str,
#     prompt_version: str, prompt_sha256: str, contract_id: str,
#     embeddings_table: str, chunks: list[dict], client: str | None = None,
#     request_id: str | None = None, model_settings: dict | None = None,
#     retrieval: dict | None = None, latency_ms: int | None = None,
# ) -> None

import hashlib
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from _stubs import install_offline_stubs


install_offline_stubs()

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

import search.requirements.verdict_store as verdict_store  # noqa: E402
from search.requirements.verdict_store import record_verdict  # noqa: E402


def _connection(verdict_id=41):
    connection = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=transaction)
    transaction.__aexit__ = AsyncMock(return_value=False)
    connection.transaction = MagicMock(return_value=transaction)
    connection.fetchval = AsyncMock(return_value=verdict_id)
    connection.execute = AsyncMock()
    return connection, transaction


def _kwargs(chunks=None):
    return {
        "source": "requirements_batch",
        "requirement_text": "The contractor shall respond within one day.",
        "verdict": "MET",
        "response_text": "The response-time requirement is explicit.",
        "source_text": "contract.pdf",
        "page_text": "12",
        "raw_output": None,
        "parsed_ok": True,
        "model_id": "test-foundation-model",
        "embed_model_id": "test-embedding-model",
        "prompt_version": "hybrid-search-v1",
        "prompt_sha256": "prompt-sha-256-value",
        "contract_id": "tn_6756",
        "embeddings_table": "embeddings_tn_6756_tenncare",
        "chunks": chunks
        if chunks is not None
        else [
            {
                "text": "First supporting contract excerpt.",
                "metadata": {"doc_name": "contract.pdf", "page": 12},
                "id": 101,
                "distance": 0.12,
                "_relevance_score": 0.91,
                "retrieval_leg": "vector",
                "fusion_rank": 1,
                "rerank_score": 0.98,
            },
            {
                "text": "Second supporting contract excerpt.",
                "metadata": {"doc_name": "schedule.pdf", "page": "A-4"},
                "id": 202,
                "distance": None,
                "_relevance_score": 0.82,
                "retrieval_leg": "fulltext",
                "fusion_rank": 2,
                "rerank_score": 0.88,
            },
        ],
        "client": "test-client",
        "request_id": "2b45fd5a-3c4f-4df8-8e26-67f5e6ad91f4",
        "model_settings": {"temperature": 0},
        "retrieval": {"final_count": 2},
        "latency_ms": 123,
    }


def _inserted_value(call, column):
    sql = call.args[0]
    match = re.search(
        r"insert\s+into\s+\S+\s*\((?P<columns>.*?)\)\s*values\s*\(",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"Could not parse INSERT columns from SQL: {sql}")
    columns = [item.strip().strip('"') for item in match.group("columns").split(",")]
    return call.args[columns.index(column) + 1]


class VerdictStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_flag_returns_without_opening_connection(self):
        for flag_value in (None, "false", "TRUE", "1"):
            with self.subTest(flag_value=flag_value):
                environment = {}
                if flag_value is not None:
                    environment["VERDICT_PERSISTENCE_ENABLED"] = flag_value
                get_connection = AsyncMock()
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(
                        verdict_store,
                        "get_connection",
                        get_connection,
                    ),
                ):
                    result = await record_verdict(**_kwargs())

                self.assertIsNone(result)
                get_connection.assert_not_awaited()

    async def test_inserts_verdict_and_each_chunk_in_one_transaction(self):
        connection, transaction = _connection()
        get_connection = AsyncMock(return_value=connection)
        release_connection = AsyncMock()
        payload = _kwargs()

        with (
            patch.dict(
                os.environ,
                {"VERDICT_PERSISTENCE_ENABLED": "true"},
                clear=True,
            ),
            patch.object(verdict_store, "get_connection", get_connection),
            patch.object(
                verdict_store,
                "release_connection",
                release_connection,
            ),
        ):
            result = await record_verdict(**payload)

        self.assertIsNone(result)
        get_connection.assert_awaited_once_with()
        release_connection.assert_awaited_once_with(connection)
        connection.transaction.assert_called_once_with()
        transaction.__aenter__.assert_awaited_once_with()
        transaction.__aexit__.assert_awaited_once()
        connection.fetchval.assert_awaited_once()
        self.assertEqual(connection.execute.await_count, len(payload["chunks"]))
        self.assertIn("insert into", connection.fetchval.await_args.args[0].lower())
        self.assertIn("verdicts", connection.fetchval.await_args.args[0].lower())
        for call in connection.execute.await_args_list:
            self.assertIn("insert into", call.args[0].lower())
            self.assertIn("verdict_chunks", call.args[0].lower())
            self.assertEqual(_inserted_value(call, "verdict_id"), 41)

    async def test_all_inserted_values_use_positional_parameters(self):
        connection, _ = _connection()
        payload = _kwargs(chunks=_kwargs()["chunks"][:1])

        with (
            patch.dict(
                os.environ,
                {"VERDICT_PERSISTENCE_ENABLED": "true"},
                clear=True,
            ),
            patch.object(
                verdict_store,
                "get_connection",
                AsyncMock(return_value=connection),
            ),
            patch.object(
                verdict_store,
                "release_connection",
                AsyncMock(),
            ),
        ):
            await record_verdict(**payload)

        calls = [connection.fetchval.await_args, *connection.execute.await_args_list]
        for call in calls:
            sql, *parameters = call.args
            placeholders = [int(number) for number in re.findall(r"\$(\d+)", sql)]
            self.assertTrue(placeholders, sql)
            self.assertEqual(
                sorted(set(placeholders)),
                list(range(1, len(parameters) + 1)),
            )
            self.assertEqual(max(placeholders), len(parameters))
            for value in parameters:
                if isinstance(value, str) and len(value) >= 8:
                    self.assertNotIn(value, sql)

    async def test_chunk_text_is_hashed_but_not_stored_by_default(self):
        connection, _ = _connection()
        chunk = _kwargs()["chunks"][0]

        with (
            patch.dict(
                os.environ,
                {"VERDICT_PERSISTENCE_ENABLED": "true"},
                clear=True,
            ),
            patch.object(
                verdict_store,
                "get_connection",
                AsyncMock(return_value=connection),
            ),
            patch.object(
                verdict_store,
                "release_connection",
                AsyncMock(),
            ),
        ):
            await record_verdict(**_kwargs(chunks=[chunk]))

        chunk_insert = connection.execute.await_args
        self.assertEqual(
            _inserted_value(chunk_insert, "chunk_sha256"),
            hashlib.sha256(chunk["text"].encode()).hexdigest(),
        )
        self.assertIsNone(_inserted_value(chunk_insert, "chunk_text"))

    async def test_write_failure_is_logged_swallowed_and_releases_connection(self):
        connection, _ = _connection()
        connection.execute.side_effect = RuntimeError("database unavailable")
        release_connection = AsyncMock()

        with (
            patch.dict(
                os.environ,
                {"VERDICT_PERSISTENCE_ENABLED": "true"},
                clear=True,
            ),
            patch.object(
                verdict_store,
                "get_connection",
                AsyncMock(return_value=connection),
            ),
            patch.object(
                verdict_store,
                "release_connection",
                release_connection,
            ),
            patch.object(verdict_store, "logger") as logger,
        ):
            result = await record_verdict(**_kwargs())

        self.assertIsNone(result)
        logger.error.assert_called()
        release_connection.assert_awaited_once_with(connection)


if __name__ == "__main__":
    unittest.main()
