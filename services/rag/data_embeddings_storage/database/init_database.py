from common.utils.settings import DB_HOST, DB_NAME, DB_USER, DB_PASSWORD, DB_PORT
import asyncio
import asyncpg

async def setup_pgvector_extension():
    """Setup pgvector extension without using connection pool"""
    conn = await asyncpg.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )
    
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
        print("pgvector extension enabled.")
        print("pg_trgm extension enabled.")
    except asyncpg.PostgresError as e:
        print(f"Error enabling pgvector extension: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(setup_pgvector_extension())