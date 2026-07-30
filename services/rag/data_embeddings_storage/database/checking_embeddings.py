import asyncpg
import asyncio
import os
from common.utils.settings import DB_HOST, DB_NAME, DB_USER, DB_PASSWORD, DB_PORT

async def check_embeddings_table():
    conn = await asyncpg.connect(
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        host=DB_HOST,
        port=DB_PORT
    )

    try:
        table_exists = await conn.fetchval("""
            SELECT COUNT(*) FROM embeddings;
        """)
        print(f"Total Embeddings stored: {table_exists}")

        rows = await conn.fetch("""
            SELECT id, text, metadata FROM embeddings ORDER BY id ASC LIMIT 3;
        """)

        print("\nSample embeddings:")
        for row in rows:
            print(f" - ID: {row['id']}, Text: {row['text'][:50]}..., Metadata: {row['metadata']}")

    finally:
        await conn.close()

