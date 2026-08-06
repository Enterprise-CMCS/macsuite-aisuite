import asyncio

from common.utils.helper import Helper
from common.utils.logger import log
from data_embeddings_storage.database.connection import (
    close_db,
    get_connection,
    initialization_db,
    release_connection,
)
from data_embeddings_storage.database.table_objects import (
    ensure_indexes,
    ensure_owner,
    ensure_table,
    missing_indexes,
)


async def create_embedding_table(table_name=None):
    resolved_table = table_name or Helper.get_embeddings_table_name()

    await initialization_db()
    connect = await get_connection()

    try:
        await ensure_table(connect, resolved_table)
        await ensure_owner(connect, resolved_table)
        await ensure_indexes(connect, resolved_table)

        missing = await missing_indexes(connect, resolved_table)
        if missing:
            log.warning(
                "embeddings_indexes_missing",
                table=resolved_table,
                missing=missing,
                remediation=(
                    "run bootstrap or scripts/sql/init-aisuite-schema.sql as master"
                ),
            )
        else:
            print(f"Embeddings table '{resolved_table}' ready.")
    finally:
        await release_connection(connect)
        await close_db()


if __name__ == "__main__":
    asyncio.run(create_embedding_table())
