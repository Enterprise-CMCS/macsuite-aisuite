import asyncpg
from common.utils.helper import Helper
from common.utils.settings import DB_HOST, DB_NAME, DB_USER, DB_PASSWORD, DB_PORT

async def check_embeddings_table(table_name=None):
    resolved_table = table_name or Helper.get_embeddings_table_name()
    conn = await asyncpg.connect(
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        host=DB_HOST,
        port=DB_PORT
    )

    try:
        table_exists = await conn.fetchval(f"""
            SELECT COUNT(*) FROM {resolved_table};
        """)
        print(f"Total Embeddings stored in {resolved_table}: {table_exists}")

        rows = await conn.fetch(f"""
            SELECT id, text, metadata FROM {resolved_table} ORDER BY id ASC LIMIT 3;
        """)

        print("\nSample embeddings:")
        for row in rows:
            print(f" - ID: {row['id']}, Text: {row['text'][:50]}..., Metadata: {row['metadata']}")

    finally:
        await conn.close()
