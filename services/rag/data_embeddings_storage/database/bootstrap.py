import asyncio
import os
import sys

import asyncpg

from common.utils.helper import Helper
from data_embeddings_storage.database.embeddings_schema import (
    EMBEDDING_DIMENSION,
    create_embeddings_table_sql,
    embeddings_index_statements,
)


APP_ROLE = "aisuite_app"


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def embeddings_tables_to_bootstrap() -> list[str]:
    return Helper.list_embeddings_table_names()


async def ensure_embeddings_table(connection, table_name: str) -> None:
    await connection.execute(
        create_embeddings_table_sql(table_name, EMBEDDING_DIMENSION),
    )
    for statement in embeddings_index_statements(table_name):
        await connection.execute(statement)


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

            for table_name in embeddings_tables_to_bootstrap():
                await ensure_embeddings_table(connection, table_name)

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
