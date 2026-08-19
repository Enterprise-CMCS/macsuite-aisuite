from common.utils.contract_config import validate_embeddings_table_name
from data_embeddings_storage.database.embeddings_schema import (
    APP_OWNER_ROLE,
    EMBEDDING_DIMENSION,
    create_embeddings_table_sql,
    embeddings_index_statements,
    expected_index_names,
    validate_app_schema,
)

INSUFFICIENT_PRIVILEGE = "42501"


async def ensure_table(connection, table_name):
    await connection.execute(
        create_embeddings_table_sql(table_name, EMBEDDING_DIMENSION),
    )


async def ensure_owner(connection, table_name, owner_role=APP_OWNER_ROLE):
    statement = await connection.fetchval(
        "SELECT format('ALTER TABLE %I.%I OWNER TO %I', $1::text, $2::text, $3::text)",
        validate_app_schema(),
        validate_embeddings_table_name(table_name),
        owner_role,
    )
    try:
        await connection.execute(statement)
    except Exception as exc:
        if getattr(exc, "sqlstate", "") != INSUFFICIENT_PRIVILEGE:
            raise
        return False
    return True


async def ensure_indexes(connection, table_name):
    for statement in embeddings_index_statements(table_name):
        try:
            await connection.execute(statement)
        except Exception as exc:
            if getattr(exc, "sqlstate", "") != INSUFFICIENT_PRIVILEGE:
                raise
            return False
    return True


async def missing_indexes(connection, table_name):
    rows = await connection.fetch(
        "SELECT indexname FROM pg_indexes WHERE schemaname = $1 AND tablename = $2",
        validate_app_schema(),
        validate_embeddings_table_name(table_name),
    )
    present = {row["indexname"] for row in rows}
    return [name for name in expected_index_names(table_name) if name not in present]
