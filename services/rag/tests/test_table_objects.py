"""Unit tests for shared table DDL helpers (no live Postgres)."""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.modules.setdefault("asyncpg", MagicMock())
sys.modules.setdefault("structlog", MagicMock())

RAG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if RAG_ROOT not in sys.path:
    sys.path.insert(0, RAG_ROOT)

from data_embeddings_storage.database.embeddings_schema import (  # noqa: E402
    expected_index_names,
)
from data_embeddings_storage.database.table_objects import (  # noqa: E402
    ensure_indexes,
    ensure_owner,
    missing_indexes,
)

class _SqlStateError(Exception):
    def __init__(self, sqlstate: str):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class TableObjectsTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_indexes_returns_false_on_42501(self):
        connection = AsyncMock()
        connection.execute = AsyncMock(side_effect=_SqlStateError("42501"))

        ok = await ensure_indexes(connection, "embeddings_tn_6756_tenncare")

        self.assertFalse(ok)
        connection.execute.assert_awaited()

    async def test_ensure_indexes_reraises_other_sqlstate(self):
        connection = AsyncMock()
        connection.execute = AsyncMock(side_effect=_SqlStateError("42P01"))

        with self.assertRaises(_SqlStateError) as ctx:
            await ensure_indexes(connection, "embeddings_tn_6756_tenncare")
        self.assertEqual(ctx.exception.sqlstate, "42P01")

    async def test_ensure_owner_returns_false_on_42501(self):
        connection = AsyncMock()
        connection.fetchval = AsyncMock(return_value="ALTER TABLE ...")
        connection.execute = AsyncMock(side_effect=_SqlStateError("42501"))

        ok = await ensure_owner(connection, "embeddings_tn_6756_tenncare")

        self.assertFalse(ok)

    async def test_missing_indexes_reports_absent_names(self):
        table = "embeddings_tn_6756_tenncare"
        expected = list(expected_index_names(table))
        connection = AsyncMock()
        connection.fetch = AsyncMock(
            return_value=[
                {"indexname": expected[0]},
                {"indexname": expected[1]},
            ]
        )

        missing = await missing_indexes(connection, table)

        self.assertEqual(missing, expected[2:])

    async def test_missing_indexes_empty_when_all_present(self):
        table = "embeddings_tn_6756_tenncare"
        expected = list(expected_index_names(table))
        connection = AsyncMock()
        connection.fetch = AsyncMock(
            return_value=[{"indexname": name} for name in expected]
        )

        missing = await missing_indexes(connection, table)

        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
