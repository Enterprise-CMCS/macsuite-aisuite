"""Unit tests for database bootstrap helpers (no live Postgres)."""

import inspect
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
    bootstrap_database,
    ensure_app_owner_role,
    ensure_verdict_tables,
    grant_app_role_privileges,
    migrate_public_embeddings_tables,
    reassign_schema_objects,
)
from data_embeddings_storage.database.embeddings_schema import (  # noqa: E402
    APP_OWNER_ROLE,
    APP_SCHEMA,
    SEARCH_PATH_SQL,
    qualified_embeddings_table,
)
from data_embeddings_storage.database.verdict_schema import (  # noqa: E402
    expected_verdict_index_names,
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

    async def test_ensure_app_owner_role_grants_current_user_and_existing_roles(self):
        connection = AsyncMock()
        connection.fetchval = AsyncMock(
            side_effect=[
                False,  # owner does not exist -> CREATE
                "CREATE ROLE aisuite_app_owner NOLOGIN",
                "GRANT aisuite_app_owner TO aisuite_admin",
                True,  # aisuite_app exists
                "GRANT aisuite_app_owner TO aisuite_app",
                "ALTER ROLE aisuite_app INHERIT",
                False,  # aisuite_app_clone missing
            ]
        )
        connection.execute = AsyncMock()

        await ensure_app_owner_role(connection)

        executed = [c.args[0] for c in connection.execute.await_args_list]
        self.assertIn("CREATE ROLE aisuite_app_owner NOLOGIN", executed)
        self.assertIn("GRANT aisuite_app_owner TO aisuite_admin", executed)
        self.assertIn("GRANT aisuite_app_owner TO aisuite_app", executed)
        self.assertIn("ALTER ROLE aisuite_app INHERIT", executed)
        self.assertFalse(
            any("aisuite_app_clone" in str(stmt) for stmt in executed),
        )

    async def test_reassign_schema_objects_alters_tables_and_sequences(self):
        connection = AsyncMock()
        connection.fetch = AsyncMock(
            side_effect=[
                [
                    {"tablename": "embeddings"},
                    {"tablename": "embeddings_tn_6756_tenncare"},
                ],
                [
                    {"seqname": "embeddings_id_seq"},
                    {"seqname": "embeddings_tn_6756_tenncare_id_seq"},
                ],
            ]
        )
        connection.fetchval = AsyncMock(
            side_effect=[
                "ALTER SCHEMA aisuite_schema OWNER TO aisuite_app_owner",
                "ALTER TABLE aisuite_schema.embeddings OWNER TO aisuite_app_owner",
                (
                    "ALTER TABLE aisuite_schema.embeddings_tn_6756_tenncare "
                    "OWNER TO aisuite_app_owner"
                ),
                (
                    "ALTER SEQUENCE aisuite_schema.embeddings_id_seq "
                    "OWNER TO aisuite_app_owner"
                ),
                (
                    "ALTER SEQUENCE aisuite_schema.embeddings_tn_6756_tenncare_id_seq "
                    "OWNER TO aisuite_app_owner"
                ),
            ]
        )
        connection.execute = AsyncMock()

        await reassign_schema_objects(connection, APP_SCHEMA)

        self.assertEqual(connection.execute.await_count, 5)
        self.assertEqual(APP_OWNER_ROLE, "aisuite_app_owner")

    async def test_ensure_verdict_tables_creates_tables_then_indexes(self):
        connection = AsyncMock()
        connection.execute = AsyncMock()

        await ensure_verdict_tables(connection)

        executed = [c.args[0] for c in connection.execute.await_args_list]
        index_names = expected_verdict_index_names()
        self.assertEqual(len(executed), 2 + len(index_names))
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS aisuite_schema.verdicts",
            executed[0],
        )
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS aisuite_schema.verdict_chunks",
            executed[1],
        )
        for statement, name in zip(executed[2:], index_names):
            self.assertIn(f"CREATE INDEX IF NOT EXISTS {name}", statement)

    def test_bootstrap_creates_verdict_tables_before_reassign(self):
        source = inspect.getsource(bootstrap_database)
        verdict_call = source.index("ensure_verdict_tables")
        reassign_call = source.index("reassign_schema_objects")
        self.assertLess(verdict_call, reassign_call)
        self.assertEqual(source.count("pg_advisory_xact_lock"), 1)

    def test_ownership_and_grant_helpers_stay_table_agnostic(self):
        for function in (reassign_schema_objects, grant_app_role_privileges):
            self.assertNotIn("verdict", inspect.getsource(function))


if __name__ == "__main__":
    unittest.main()
