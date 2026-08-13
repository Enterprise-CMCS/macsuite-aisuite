import asyncpg
from pgvector.asyncpg import register_vector
from common.utils.settings import (
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_POOL_MAX,
    DB_POOL_MIN,
    DB_PORT,
    DB_USER,
)
from data_embeddings_storage.database.embeddings_schema import SEARCH_PATH_SQL

postgres_pool = None


async def register_vector_init(conn):
    try:
        await register_vector(conn)
    except Exception:
        pass
    await conn.execute(SEARCH_PATH_SQL)


async def initialization_db():
    global postgres_pool
    if postgres_pool is None:
        postgres_pool = await asyncpg.create_pool(
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            host=DB_HOST,
            port=DB_PORT,
            min_size=DB_POOL_MIN if DB_POOL_MIN is not None else 1,
            max_size=DB_POOL_MAX if DB_POOL_MAX is not None else 10,
            init=register_vector_init,
            ssl="require",
        )
    print("Database connection pool initialized.")

    return postgres_pool


async def register_vector_types(conn):
    await register_vector(conn)
    await conn.execute(SEARCH_PATH_SQL)


async def get_connection():
    global postgres_pool
    if postgres_pool is None:
        await initialization_db()
    if postgres_pool is None:
        raise RuntimeError("Failed to initialize database pool")
    return await postgres_pool.acquire()


async def release_connection(connection):
    global postgres_pool
    if postgres_pool is None:
        raise RuntimeError("Database pool is not initialized")
    await postgres_pool.release(connection)


async def close_db():
    global postgres_pool
    if postgres_pool is not None:
        await postgres_pool.close()
        postgres_pool = None
        print("Database connection pool closed.")
