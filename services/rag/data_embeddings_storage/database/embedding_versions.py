import json
import re
from datetime import timedelta
from typing import NamedTuple

from common.utils.logger import log
from data_embeddings_storage.database.embeddings_schema import (
    APP_SCHEMA,
    qualified_embeddings_table,
)
from data_embeddings_storage.database.table_objects import INSUFFICIENT_PRIVILEGE, ensure_owner

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
        dropped_at   TIMESTAMPTZ,
        PRIMARY KEY (state, version)
    )
"""

# Registries created before retention existed get the column from ensure_registry.
# pg_attribute, unlike information_schema.columns, is not filtered by the login's privileges.
_REGISTRY_STATE_SQL = """
    SELECT to_regclass($1) IS NOT NULL AS present,
           EXISTS (SELECT 1 FROM pg_attribute
                   WHERE attrelid = to_regclass($1) AND attname = 'dropped_at' AND NOT attisdropped)
               AS has_dropped_at
"""
_ADD_DROPPED_AT_SQL = f"ALTER TABLE {QUALIFIED_REGISTRY_TABLE} ADD COLUMN IF NOT EXISTS dropped_at TIMESTAMPTZ"

# DROP TABLE needs ACCESS EXCLUSIVE; never queue live searches behind it for long.
DROP_LOCK_TIMEOUT = "5s"
LOCK_NOT_AVAILABLE = "55P03"

_REGISTRY_ROWS_SQL = (
    "SELECT state, version, table_name, status, created_at, completed_at, dropped_at "
    f"FROM {QUALIFIED_REGISTRY_TABLE}"
)
# Re-read under the state lock: the version may have been published, failed or dropped
# since it was selected. can_drop is NULL when the table no longer exists.
_LOCK_VERSION_SQL = f"""
    SELECT r.status, r.dropped_at, pg_has_role(c.relowner, 'MEMBER') AS can_drop
    FROM {QUALIFIED_REGISTRY_TABLE} r
    LEFT JOIN pg_class c ON c.oid = to_regclass($2)
    WHERE r.table_name = $1
    FOR UPDATE OF r
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


async def _add_dropped_at(connection):
    try:
        await connection.execute(_ADD_DROPPED_AT_SQL)
    except Exception as exc:
        if getattr(exc, "sqlstate", "") != INSUFFICIENT_PRIVILEGE:
            raise
        raise RuntimeError(
            f"{QUALIFIED_REGISTRY_TABLE} is missing dropped_at and this login does not own it; "
            "run the database bootstrap task to reassign ownership to the shared owner role."
        ) from exc


async def ensure_registry(connection):
    registry = await connection.fetchrow(_REGISTRY_STATE_SQL, QUALIFIED_REGISTRY_TABLE)
    if registry["present"]:
        if not registry["has_dropped_at"]:
            await _add_dropped_at(connection)
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
            f"SELECT state, version, table_name, dropped_at FROM {QUALIFIED_REGISTRY_TABLE} "
            "WHERE request_key = $1",
            request_key,
        )
        if existing:
            if existing["dropped_at"] is not None:
                # Never recreate an expired table under a registry row that says it is gone.
                raise RuntimeError(
                    f"Embedding version {existing['table_name']} for request {request_key} expired and "
                    "was dropped; submit the manifest again under a new requestKey."
                )
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
        "WHERE request_key = $3 AND table_name = $4 AND dropped_at IS NULL",
        READY,
        row_count,
        request_key,
        table_name,
    )
    if result.endswith(" 0"):
        raise LookupError(f"No live embedding version {table_name} is registered for request {request_key}.")
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


def select_expired(rows, now, retention_days):
    """Registry rows whose table can be dropped, ordered by state and version.

    The newest live READY version of a state is never selected: a READY version expires
    only once a newer, live READY version of the same state has been published for the
    retention period. FAILED versions expire from when they failed, and a version still
    BUILDING after the retention period is abandoned.
    """
    cutoff = now - timedelta(days=retention_days)
    published = {}
    for row in rows:
        if row["status"] == READY and row["dropped_at"] is None and row["completed_at"] is not None:
            published.setdefault(row["state"], []).append(row)

    def expired(row):
        if row["dropped_at"] is not None:
            return False
        if row["status"] == READY:
            return any(
                newer["version"] > row["version"] and newer["completed_at"] < cutoff
                for newer in published.get(row["state"], ())
            )
        if row["status"] == FAILED:
            return row["completed_at"] is not None and row["completed_at"] < cutoff
        if row["status"] == BUILDING:
            return row["created_at"] < cutoff
        return False

    return sorted((row for row in rows if expired(row)), key=lambda row: (row["state"], row["version"]))


class ExpiryResult(NamedTuple):
    dropped: list
    not_owner: list


async def _drop_version(connection, row):
    """Drop one version under its state lock; returns 'dropped', 'not_owner' or 'changed'."""
    table_name = validate_versioned_table_name(row["table_name"])
    qualified = qualified_embeddings_table(table_name)
    async with connection.transaction():
        # Same lock allocate_version takes, so a retried allocation cannot interleave.
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))", f"{_STATE_LOCK_PREFIX}{row['state']}"
        )
        await connection.execute(f"SET LOCAL lock_timeout = '{DROP_LOCK_TIMEOUT}'")
        current = await connection.fetchrow(_LOCK_VERSION_SQL, table_name, qualified)
        if current is None or current["dropped_at"] is not None or current["status"] != row["status"]:
            return "changed"
        if current["can_drop"] is False:
            return "not_owner"
        await connection.execute(f"DROP TABLE IF EXISTS {qualified}")
        await connection.execute(
            f"UPDATE {QUALIFIED_REGISTRY_TABLE} SET dropped_at = now() WHERE table_name = $1",
            table_name,
        )
    return "dropped"


async def expire_versions(connection, retention_days):
    """Drop version tables past the retention period.

    Every expired table is attempted. Tables this login cannot drop because another
    rotation login owns them are logged and reported but not retried: only the bootstrap
    task can reassign them. Any other failure (e.g. lock_timeout behind a live query)
    raises at the end so the invocation is retried.
    """
    now = await connection.fetchval("SELECT now()")
    rows = select_expired(await connection.fetch(_REGISTRY_ROWS_SQL), now, retention_days)
    dropped, not_owner, failed = [], [], []
    for row in rows:
        table_name = row["table_name"]
        try:
            outcome = await _drop_version(connection, row)
        except Exception as exc:
            timed_out = getattr(exc, "sqlstate", "") == LOCK_NOT_AVAILABLE
            log.error(f"expire_versions() Could not drop {table_name}: {'lock timeout' if timed_out else exc}")
            failed.append(table_name)
            continue
        if outcome == "dropped":
            dropped.append(table_name)
        elif outcome == "not_owner":
            log.error(
                f"expire_versions() Cannot drop {table_name}: owned by a role this login is not a "
                "member of. Run the database bootstrap task to reassign it to the shared owner role."
            )
            not_owner.append(table_name)
        else:
            log.info(f"expire_versions() Skipping {table_name}: its registry row changed since selection")
    if failed:
        raise RuntimeError(f"Could not drop expired embeddings table(s): {', '.join(failed)}")
    return ExpiryResult(dropped, not_owner)
