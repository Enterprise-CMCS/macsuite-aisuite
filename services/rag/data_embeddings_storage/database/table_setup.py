from data_embeddings_storage.database.connection import initialization_db, close_db, get_connection, release_connection
import asyncio
from common.utils.helper import Helper
from common.utils.settings import EMBEDDING_DIMENSION
from data_embeddings_storage.database.embeddings_schema import (
    create_embeddings_table_sql,
    embeddings_index_statements,
)


async def create_embedding_table(table_name=None):
    resolved_table = table_name or Helper.get_embeddings_table_name()

    await initialization_db()
    connect = await get_connection()

    try:
        await connect.execute(
            create_embeddings_table_sql(resolved_table, EMBEDDING_DIMENSION),
        )
        print(f"Embeddings table '{resolved_table}' created successfully.")

        for statement in embeddings_index_statements(resolved_table):
            await connect.execute(statement)
        print(f"Indexes created successfully for '{resolved_table}'.")

    finally:
        await release_connection(connect)
        await close_db()


if __name__ == "__main__":
    asyncio.run(create_embedding_table())
