import asyncio
import os
import sys

import asyncpg

from common.utils.helper import Helper
from data_embeddings_storage.database.embeddings_schema import (
    APP_OWNER_ROLE,
    APP_SCHEMA,
    validate_app_schema,
)
from data_embeddings_storage.database.table_objects import (
    ensure_indexes,
    ensure_table,
)


APP_ROLE = "aisuite_app"
APP_ROTATION_ROLES = (APP_ROLE, "aisuite_app_clone")


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def embeddings_tables_to_bootstrap() -> list[str]:
    return Helper.list_embeddings_table_names()


async def ensure_embeddings_table(connection, table_name: str) -> None:
    await ensure_table(connection, table_name)
    await ensure_indexes(connection, table_name)


async def role_exists(connection, role_name: str) -> bool:
    return bool(
        await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = $1)",
            role_name,
        )
    )


async def ensure_app_role(connection, app_password: str) -> None:
    exists = await role_exists(connection, APP_ROLE)
    role_action = "ALTER" if exists else "CREATE"
    role_statement = await connection.fetchval(
        f"SELECT format("
        f"'{role_action} ROLE %I WITH LOGIN PASSWORD %L', "
        "$1::text, $2::text"
        ")",
        APP_ROLE,
        app_password,
    )
    await connection.execute(role_statement)


async def ensure_app_owner_role(connection) -> None:
    if not await role_exists(connection, APP_OWNER_ROLE):
        await _execute_format(
            connection,
            "SELECT format('CREATE ROLE %I NOLOGIN', $1::text)",
            APP_OWNER_ROLE,
        )

    # RDS master is not superuser; membership is required to reassign ownership.
    await _execute_format(
        connection,
        "SELECT format('GRANT %I TO %I', $1::text, current_user::text)",
        APP_OWNER_ROLE,
    )

    for role_name in APP_ROTATION_ROLES:
        if not await role_exists(connection, role_name):
            continue
        await _execute_format(
            connection,
            "SELECT format('GRANT %I TO %I', $1::text, $2::text)",
            APP_OWNER_ROLE,
            role_name,
        )
        await _execute_format(
            connection,
            "SELECT format('ALTER ROLE %I INHERIT', $1::text)",
            role_name,
        )


async def reassign_schema_objects(connection, schema: str) -> None:
    await _execute_format(
        connection,
        "SELECT format('ALTER SCHEMA %I OWNER TO %I', $1::text, $2::text)",
        schema,
        APP_OWNER_ROLE,
    )

    tables = await connection.fetch(
        """
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = $1
        ORDER BY tablename
        """,
        schema,
    )
    for row in tables:
        await _execute_format(
            connection,
            "SELECT format("
            "'ALTER TABLE %I.%I OWNER TO %I', $1::text, $2::text, $3::text)",
            schema,
            row["tablename"],
            APP_OWNER_ROLE,
        )

    sequences = await connection.fetch(
        """
        SELECT c.relname AS seqname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = $1 AND c.relkind = 'S'
        ORDER BY c.relname
        """,
        schema,
    )
    for row in sequences:
        await _execute_format(
            connection,
            "SELECT format("
            "'ALTER SEQUENCE %I.%I OWNER TO %I', $1::text, $2::text, $3::text)",
            schema,
            row["seqname"],
            APP_OWNER_ROLE,
        )


async def _execute_format(connection, format_expr: str, *args) -> None:
    statement = await connection.fetchval(format_expr, *args)
    await connection.execute(statement)


async def grant_app_role_privileges(
    connection,
    database: str,
    role_name: str,
    schema: str,
) -> None:
    await _execute_format(
        connection,
        "SELECT format('GRANT CONNECT ON DATABASE %I TO %I', $1::text, $2::text)",
        database,
        role_name,
    )
    await _execute_format(
        connection,
        "SELECT format('GRANT USAGE ON SCHEMA public TO %I', $1::text)",
        role_name,
    )
    await _execute_format(
        connection,
        "SELECT format("
        "'GRANT USAGE, CREATE ON SCHEMA %I TO %I', $1::text, $2::text)",
        schema,
        role_name,
    )
    await _execute_format(
        connection,
        "SELECT format("
        "'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
        "IN SCHEMA %I TO %I', $1::text, $2::text)",
        schema,
        role_name,
    )
    await _execute_format(
        connection,
        "SELECT format("
        "'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', "
        "$1::text, $2::text)",
        schema,
        role_name,
    )
    await _execute_format(
        connection,
        "SELECT format("
        "'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA %I TO %I', "
        "$1::text, $2::text)",
        schema,
        role_name,
    )
    await _execute_format(
        connection,
        "SELECT format("
        "'ALTER DEFAULT PRIVILEGES IN SCHEMA %I "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', "
        "$1::text, $2::text)",
        schema,
        role_name,
    )
    await _execute_format(
        connection,
        "SELECT format("
        "'ALTER DEFAULT PRIVILEGES IN SCHEMA %I "
        "GRANT USAGE, SELECT ON SEQUENCES TO %I', "
        "$1::text, $2::text)",
        schema,
        role_name,
    )
    await _execute_format(
        connection,
        "SELECT format("
        "'ALTER ROLE %I SET search_path TO %I, public', "
        "$1::text, $2::text)",
        role_name,
        schema,
    )


async def migrate_public_embeddings_tables(connection, schema: str) -> None:
    rows = await connection.fetch(
        """
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename LIKE 'embeddings%'
        ORDER BY tablename
        """,
    )
    for row in rows:
        await _execute_format(
            connection,
            "SELECT format("
            "'ALTER TABLE public.%I SET SCHEMA %I', $1::text, $2::text)",
            row["tablename"],
            schema,
        )


async def bootstrap_database() -> None:
    database = required_environment("PGDATABASE")
    app_password = required_environment("APP_PASSWORD")
    schema = validate_app_schema(APP_SCHEMA)
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

            await ensure_app_role(connection, app_password)
            await ensure_app_owner_role(connection)
            await _execute_format(
                connection,
                "SELECT format('CREATE SCHEMA IF NOT EXISTS %I', $1::text)",
                schema,
            )
            await migrate_public_embeddings_tables(connection, schema)

            for table_name in embeddings_tables_to_bootstrap():
                await ensure_embeddings_table(connection, table_name)

            await reassign_schema_objects(connection, schema)

            for role_name in APP_ROTATION_ROLES:
                if role_name != APP_ROLE and not await role_exists(
                    connection,
                    role_name,
                ):
                    continue
                await grant_app_role_privileges(
                    connection,
                    database,
                    role_name,
                    schema,
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
