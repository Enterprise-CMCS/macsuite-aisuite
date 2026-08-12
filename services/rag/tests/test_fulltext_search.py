"""Unit tests for full-text search SQL and table-name safety."""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from _stubs import install_offline_stubs


install_offline_stubs()

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

_GET_CONNECTION = AsyncMock()
_RELEASE_CONNECTION = AsyncMock()
with (
    patch(
        "data_embeddings_storage.database.connection.get_connection",
        new=_GET_CONNECTION,
    ),
    patch(
        "data_embeddings_storage.database.connection.release_connection",
        new=_RELEASE_CONNECTION,
    ),
):
    from search.database_searching.search import SearchEngine  # noqa: E402


class FulltextSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _GET_CONNECTION.reset_mock()
        _RELEASE_CONNECTION.reset_mock()

    async def test_fulltext_search_selects_and_returns_metadata(self):
        connection = AsyncMock()
        connection.fetch = AsyncMock(
            return_value=[
                {
                    "id": 7,
                    "text": "matching text",
                    "metadata": {"source": "contract.pdf"},
                    "rank": 0.75,
                }
            ]
        )
        _GET_CONNECTION.return_value = connection
        engine = SearchEngine(table_name="embeddings_tn_6756_tenncare")

        results = await engine.fulltext_search("matching", limit=5)

        sql, query, limit = connection.fetch.await_args.args
        self.assertIn("metadata", sql.lower())
        self.assertEqual((query, limit), ("matching", 5))
        self.assertEqual(
            set(results[0]),
            {"id", "text", "metadata", "rank"},
        )
        _RELEASE_CONNECTION.assert_awaited_once_with(connection)

    async def test_rejects_invalid_table_name_before_query(self):
        with self.assertRaises(ValueError):
            SearchEngine(table_name="embeddings;drop table")

        _GET_CONNECTION.assert_not_awaited()
        _RELEASE_CONNECTION.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
