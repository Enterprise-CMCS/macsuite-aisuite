import json
import re
from typing import NamedTuple

from data_embeddings_storage.database.embeddings_schema import (
    APP_SCHEMA,
    qualified_embeddings_table,
)
from data_embeddings_storage.database.table_objects import ensure_owner

REGISTRY_TABLE = "embedding_versions"
QUALIFIED_REGISTRY_TABLE = f"{APP_SCHEMA}.{REGISTRY_TABLE}"

BUILDING = "BUILDING"
READY = "READY"
FAILED = "FAILED"


SOURCE_METADATA_KEY = "rag_output_key"

_VERSIONED_TABLE_RE = re.compile(r"^embeddings_[a-z]{2}_v[1-9][0-9]*$")
_REGISTRY_LOCK = "aisuite-embedding-version-registry"
_STATE_LOCK_PREFIX = "aisuite-embedding-version:"

_CREATE_REGISTRY_SQL = f"""
    CREATE TABLE IF NOT EXISTS {QUALIFIED_REGISTRY_TABLE} (
        state        TEXT        NOT NULL,
        version      INTEGER     NOT NULL,
        table_name   TEXT        NOT NULL UNIQUE,
        request_key  TEXT        NOT NULL UNIQUE,
        status       TEXT        NOT NULL DEFAULT '{BUILDING}'
                     CHECK (status IN ('{BUILDING}', '{READY}', '{FAILED}')),
        row_count    INTEGER,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ,
        PRIMARY KEY (state, version)
    )
"""


class EmbeddingVersion(NamedTuple):
    state: str
    version: int
    table_name: str

    def as_payload(self):
        return {"state": self.state, "version": self.version, "tableName": self.table_name}


def normalize_state(state):
    code = (state or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise ValueError(f"Invalid state '{state}': expected a two-letter code.")
    return code


def versioned_table_name(state, version):
    return f"embeddings_{normalize_state(state).lower()}_v{int(version)}"


def validate_versioned_table_name(table_name):
    """Guard workflow input: only tables this module allocates may be written to."""
    if not isinstance(table_name, str) or not _VERSIONED_TABLE_RE.fullmatch(table_name):
        raise ValueError(f"Invalid versioned embeddings table '{table_name}'.")
    return table_name


async def _table_exists(connection, qualified_name):
    return await connection.fetchval("SELECT to_regclass($1) IS NOT NULL", qualified_name)


async def ensure_registry(connection):
    if await _table_exists(connection, QUALIFIED_REGISTRY_TABLE):
        return
    async with connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", _REGISTRY_LOCK)
        await connection.execute(_CREATE_REGISTRY_SQL)
    # Outside the transaction: a refused ALTER OWNER would otherwise abort it.
    await ensure_owner(connection, REGISTRY_TABLE)


async def allocate_version(connection, state, request_key):
    """Reserve the next version for the state, or return the one this request already holds."""
    state = normalize_state(state)
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))", f"{_STATE_LOCK_PREFIX}{state}"
        )
        existing = await connection.fetchrow(
            f"SELECT state, version, table_name FROM {QUALIFIED_REGISTRY_TABLE} WHERE request_key = $1",
            request_key,
        )
        if existing:
            return EmbeddingVersion(existing["state"], existing["version"], existing["table_name"])

        version = await connection.fetchval(
            f"SELECT COALESCE(MAX(version), 0) + 1 FROM {QUALIFIED_REGISTRY_TABLE} WHERE state = $1",
            state,
        )
        # Never adopt a table the registry does not know about (e.g. created by hand).
        while await _table_exists(connection, qualified_embeddings_table(versioned_table_name(state, version))):
            version += 1

        allocated = EmbeddingVersion(state, version, versioned_table_name(state, version))
        await connection.execute(
            f"INSERT INTO {QUALIFIED_REGISTRY_TABLE} (state, version, table_name, request_key) "
            "VALUES ($1, $2, $3, $4)",
            allocated.state,
            allocated.version,
            allocated.table_name,
            request_key,
        )
    return allocated


async def delete_source_rows(connection, table_name, rag_output_key):
    """Remove rows previously embedded from one RAG_Output file (uses the metadata GIN index)."""
    await connection.execute(
        f"DELETE FROM {qualified_embeddings_table(table_name)} WHERE metadata @> $1::jsonb",
        json.dumps({SOURCE_METADATA_KEY: rag_output_key}),
    )


async def mark_ready(connection, request_key, table_name):
    """Publish the version once every document is embedded; an empty table is a failed run."""
    row_count = await connection.fetchval(
        f"SELECT count(*) FROM {qualified_embeddings_table(table_name)}"
    )
    if not row_count:
        raise RuntimeError(f"Embeddings table {table_name} is empty; no document produced searchable text.")

    result = await connection.execute(
        f"UPDATE {QUALIFIED_REGISTRY_TABLE} SET status = $1, row_count = $2, completed_at = now() "
        "WHERE request_key = $3 AND table_name = $4",
        READY,
        row_count,
        request_key,
        table_name,
    )
    if result.endswith(" 0"):
        raise LookupError(f"No embedding version {table_name} is registered for request {request_key}.")
    return row_count


async def mark_failed(connection, request_key):
    """Flag an unfinished version as failed; returns False when nothing was building."""
    result = await connection.execute(
        f"UPDATE {QUALIFIED_REGISTRY_TABLE} SET status = $1, completed_at = now() "
        "WHERE request_key = $2 AND status = $3",
        FAILED,
        request_key,
        BUILDING,
    )
    return not result.endswith(" 0")
