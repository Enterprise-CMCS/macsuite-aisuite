"""Unit tests for database bootstrap helpers (no live Postgres)."""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.modules.setdefault("asyncpg", MagicMock())
sys.modules.setdefault("structlog", MagicMock())

RAG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if RAG_ROOT not in sys.path:
    sys.path.insert(0, RAG_ROOT)

from data_embeddings_storage.database.bootstrap import (  # noqa: E402
    APP_ROLE,
    APP_ROTATION_ROLES,
    grant_app_role_privileges,
    migrate_public_embeddings_tables,
)
from data_embeddings_storage.database.embeddings_schema import (  # noqa: E402
    APP_SCHEMA,
    SEARCH_PATH_SQL,
    qualified_embeddings_table,
)


class BootstrapHelpersTests(unittest.IsolatedAsyncioTestCase):
    def test_rotation_roles_include_primary_and_clone(self):
        self.assertEqual(APP_ROTATION_ROLES, (APP_ROLE, "aisuite_app_clone"))

    def test_qualified_table_and_search_path(self):
        self.assertEqual(
            qualified_embeddings_table("embeddings_tn_6756_tenncare"),
            "aisuite_schema.embeddings_tn_6756_tenncare",
        )
        self.assertEqual(SEARCH_PATH_SQL, "SET search_path TO aisuite_schema, public")

    async def test_migrate_public_embeddings_moves_each_table(self):
        connection = AsyncMock()
        connection.fetch = AsyncMock(
            return_value=[
                {"tablename": "embeddings_me_0002_nemt"},
                {"tablename": "embeddings_tn_6756_tenncare"},
            ]
        )
        connection.fetchval = AsyncMock(return_value="ALTER TABLE ...")
        connection.execute = AsyncMock()

        await migrate_public_embeddings_tables(connection, APP_SCHEMA)

        self.assertEqual(connection.execute.await_count, 2)

    async def test_grant_app_role_privileges_executes_grants(self):
        connection = AsyncMock()
        connection.fetchval = AsyncMock(return_value="SQL")
        connection.execute = AsyncMock()

        await grant_app_role_privileges(
            connection,
            "vectordb",
            APP_ROLE,
            APP_SCHEMA,
        )

        self.assertEqual(connection.execute.await_count, 9)


if __name__ == "__main__":
    unittest.main()
