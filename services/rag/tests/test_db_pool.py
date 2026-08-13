import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from _stubs import install_offline_stubs
except ModuleNotFoundError:
    from tests._stubs import install_offline_stubs


install_offline_stubs()

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from data_embeddings_storage.database import connection  # noqa: E402

FALLBACK_POOL_MIN = 1
FALLBACK_POOL_MAX = 10
POOL_HEADROOM = 2
# Keep aligned with search.requirements.verdicts.DEFAULT_CONCURRENCY (must stay 4).
DEFAULT_CONCURRENCY = 4


class DbPoolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._previous_pool = connection.postgres_pool
        connection.postgres_pool = None

    def tearDown(self):
        connection.postgres_pool = self._previous_pool

    def test_fallback_max_covers_hybrid_concurrency_plus_headroom(self):
        verdicts_src = (
            RAG_ROOT / "search" / "requirements" / "verdicts.py"
        ).read_text(encoding="utf-8")
        self.assertIn(f"DEFAULT_CONCURRENCY = {DEFAULT_CONCURRENCY}", verdicts_src)
        self.assertGreaterEqual(
            FALLBACK_POOL_MAX,
            2 * DEFAULT_CONCURRENCY + POOL_HEADROOM,
        )

    async def test_initialization_db_passes_settings_pool_sizes(self):
        dummy_pool = MagicMock(name="dummy_pool")
        create_pool = AsyncMock(return_value=dummy_pool)

        with (
            patch.object(connection, "DB_POOL_MIN", 2),
            patch.object(connection, "DB_POOL_MAX", 10),
            patch.object(connection.asyncpg, "create_pool", create_pool),
        ):
            pool = await connection.initialization_db()

        self.assertIs(pool, dummy_pool)
        create_pool.assert_awaited_once()
        kwargs = create_pool.await_args.kwargs
        self.assertEqual(kwargs["min_size"], 2)
        self.assertEqual(kwargs["max_size"], 10)

    async def test_initialization_db_falls_back_when_settings_unset(self):
        dummy_pool = MagicMock(name="dummy_pool")
        create_pool = AsyncMock(return_value=dummy_pool)

        with (
            patch.object(connection, "DB_POOL_MIN", None),
            patch.object(connection, "DB_POOL_MAX", None),
            patch.object(connection.asyncpg, "create_pool", create_pool),
        ):
            await connection.initialization_db()

        kwargs = create_pool.await_args.kwargs
        self.assertEqual(kwargs["min_size"], FALLBACK_POOL_MIN)
        self.assertEqual(kwargs["max_size"], FALLBACK_POOL_MAX)


if __name__ == "__main__":
    unittest.main()
