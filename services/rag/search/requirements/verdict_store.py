"""Best-effort persistence of requirement verdicts and their supporting chunks."""

import hashlib
import json
import os
import uuid
from typing import Any

from common.utils.logger import get_logger
from data_embeddings_storage.database.connection import (
    get_connection,
    release_connection,
)
from data_embeddings_storage.database.verdict_schema import (
    VERDICT_CHUNKS_TABLE,
    VERDICTS_TABLE,
)

logger = get_logger(__name__)

PERSISTENCE_ENV_VAR = "VERDICT_PERSISTENCE_ENABLED"
CHUNK_TEXT_ENV_VAR = "VERDICT_STORE_CHUNK_TEXT"
ENABLED_VALUE = "true"

_VERDICT_COLUMNS = (
    "request_id",
    "source",
    "client",
    "contract_id",
    "embeddings_table",
    "requirement_text",
    "requirement_sha256",
    "verdict",
    "response_text",
    "source_text",
    "page_text",
    "raw_output",
    "parsed_ok",
    "model_id",
    "embed_model_id",
    "prompt_version",
    "prompt_sha256",
    "model_settings",
    "retrieval",
    "latency_ms",
)

_CHUNK_COLUMNS = (
    "verdict_id",
    "rank",
    "embeddings_table",
    "embedding_row_id",
    "doc_name",
    "page",
    "distance",
    "relevance_score",
    "retrieval_leg",
    "fusion_rank",
    "rerank_score",
    "chunk_sha256",
    "chunk_text",
)


def _insert_sql(table: str, columns: tuple[str, ...], returning: str = "") -> str:
    placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
    statement = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    )
    return f"{statement} {returning}".strip()


_VERDICT_INSERT_SQL = _insert_sql(VERDICTS_TABLE, _VERDICT_COLUMNS, "RETURNING id")
_CHUNK_INSERT_SQL = _insert_sql(VERDICT_CHUNKS_TABLE, _CHUNK_COLUMNS)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_value(value: dict | None) -> str | None:
    return None if value is None else json.dumps(value)


def _metadata(chunk: dict) -> dict:
    metadata = chunk.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _embedding_row_id(chunk: dict) -> int | None:
    value = chunk.get("id")
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _page(metadata: dict) -> str | None:
    page = metadata.get("page")
    return None if page is None else str(page)


def _chunk_text(text: str) -> str | None:
    if os.environ.get(CHUNK_TEXT_ENV_VAR) == ENABLED_VALUE:
        return text
    return None


def _chunk_values(
    verdict_id: Any, rank: int, embeddings_table: str, chunk: dict
) -> tuple:
    metadata = _metadata(chunk)
    text = chunk.get("text") or ""
    return (
        verdict_id,
        rank,
        embeddings_table,
        _embedding_row_id(chunk),
        metadata.get("doc_name"),
        _page(metadata),
        chunk.get("distance"),
        chunk.get("_relevance_score"),
        chunk.get("retrieval_leg"),
        chunk.get("fusion_rank"),
        chunk.get("rerank_score"),
        _sha256(text),
        _chunk_text(text),
    )


async def record_verdict(
    *,
    source: str,
    requirement_text: str,
    verdict: str,
    response_text: str | None = None,
    source_text: str | None = None,
    page_text: str | None = None,
    raw_output: str | None = None,
    parsed_ok: bool,
    model_id: str,
    embed_model_id: str,
    prompt_version: str,
    prompt_sha256: str,
    contract_id: str,
    embeddings_table: str,
    chunks: list[dict],
    client: str | None = None,
    request_id: str | None = None,
    model_settings: dict | None = None,
    retrieval: dict | None = None,
    latency_ms: int | None = None,
) -> None:
    """Persist one verdict and its chunks, never raising to the caller."""
    if os.environ.get(PERSISTENCE_ENV_VAR) != ENABLED_VALUE:
        return None

    connection = None
    try:
        connection = await get_connection()
        try:
            async with connection.transaction():
                verdict_id = await connection.fetchval(
                    _VERDICT_INSERT_SQL,
                    request_id or str(uuid.uuid4()),
                    source,
                    client,
                    contract_id,
                    embeddings_table,
                    requirement_text,
                    _sha256(requirement_text),
                    verdict,
                    response_text,
                    source_text,
                    page_text,
                    raw_output,
                    parsed_ok,
                    model_id,
                    embed_model_id,
                    prompt_version,
                    prompt_sha256,
                    _json_value(model_settings),
                    _json_value(retrieval),
                    latency_ms,
                )
                for rank, chunk in enumerate(chunks or [], start=1):
                    await connection.execute(
                        _CHUNK_INSERT_SQL,
                        *_chunk_values(
                            verdict_id, rank, embeddings_table, chunk
                        ),
                    )
        finally:
            await release_connection(connection)
    except Exception as error:
        logger.error(
            "verdict_persistence_failed",
            error=str(error),
            contract_id=contract_id,
            exc_info=True,
        )
    return None
