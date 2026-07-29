import asyncio
import os
import sys

import asyncpg


APP_ROLE = "aisuite_app"
EMBEDDING_DIMENSION = 1536


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


async def bootstrap_database() -> None:
    database = required_environment("PGDATABASE")
    app_password = required_environment("APP_PASSWORD")
    connection = await asyncpg.connect(
        database=database,
        host=required_environment("PGHOST"),
        password=required_environment("PGPASSWORD"),
        port=int(required_environment("PGPORT")),
        ssl="require",
        user=required_environment("PGUSER"),
    )

    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtext('aisuite-database-bootstrap'))",
            )
            await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await connection.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            await connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS embeddings (
                    id SERIAL PRIMARY KEY,
                    text TEXT NOT NULL,
                    metadata JSONB DEFAULT NULL,
                    embedding VECTOR({EMBEDDING_DIMENSION}) NOT NULL,
                    search_tsv tsvector GENERATED ALWAYS AS (
                        setweight(
                            to_tsvector('english', coalesce(text, '')),
                            'A'
                        ) ||
                        setweight(
                            to_tsvector(
                                'english',
                                coalesce(metadata::text, '')
                            ),
                            'B'
                        )
                    ) STORED,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """,
            )
            await connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw
                ON embeddings
                USING hnsw (embedding vector_cosine_ops)
                WITH (m=16, ef_construction=128)
                """,
            )
            await connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_embeddings_metadata
                ON embeddings USING GIN (metadata)
                """,
            )
            await connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_search_text_trgm
                ON embeddings USING GIN (text gin_trgm_ops)
                """,
            )
            await connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_search_tsv
                ON embeddings USING GIN (search_tsv)
                """,
            )

            role_exists = await connection.fetchval(
                "SELECT EXISTS("
                "SELECT 1 FROM pg_roles WHERE rolname = $1"
                ")",
                APP_ROLE,
            )
            role_action = "ALTER" if role_exists else "CREATE"
            role_statement = await connection.fetchval(
                f"SELECT format("
                f"'{role_action} ROLE %I WITH LOGIN PASSWORD %L', "
                "$1::text, $2::text"
                ")",
                APP_ROLE,
                app_password,
            )
            await connection.execute(role_statement)

            connect_grant = await connection.fetchval(
                "SELECT format("
                "'GRANT CONNECT ON DATABASE %I TO %I', "
                "$1::text, $2::text"
                ")",
                database,
                APP_ROLE,
            )
            await connection.execute(connect_grant)
            await connection.execute(
                """
                GRANT USAGE ON SCHEMA public TO aisuite_app;
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON ALL TABLES IN SCHEMA public TO aisuite_app;
                GRANT USAGE, SELECT
                    ON ALL SEQUENCES IN SCHEMA public TO aisuite_app;
                GRANT EXECUTE
                    ON ALL FUNCTIONS IN SCHEMA public TO aisuite_app;
                ALTER DEFAULT PRIVILEGES IN SCHEMA public
                    GRANT SELECT, INSERT, UPDATE, DELETE
                    ON TABLES TO aisuite_app;
                ALTER DEFAULT PRIVILEGES IN SCHEMA public
                    GRANT USAGE, SELECT
                    ON SEQUENCES TO aisuite_app;
                """,
            )
    finally:
        await connection.close()


def main() -> int:
    try:
        asyncio.run(bootstrap_database())
    except Exception:
        print("Database bootstrap failed.", file=sys.stderr)
        return 1

    print("Database bootstrap completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
