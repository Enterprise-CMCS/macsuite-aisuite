from data_embeddings_storage.database.embeddings_schema import (
    APP_SCHEMA,
    validate_app_schema,
)


def _qualified_table(table_name: str) -> str:
    schema = validate_app_schema()
    return f"{schema}.{table_name}"


VERDICTS_TABLE = _qualified_table("verdicts")
VERDICT_CHUNKS_TABLE = _qualified_table("verdict_chunks")


def create_verdicts_table_sql():
    verdicts_table = _qualified_table("verdicts")
    return f"""
                CREATE TABLE IF NOT EXISTS {verdicts_table} (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    request_id UUID NOT NULL,
                    source TEXT NOT NULL,
                    client TEXT,
                    contract_id TEXT NOT NULL,
                    embeddings_table TEXT NOT NULL,
                    requirement_text TEXT NOT NULL,
                    requirement_sha256 TEXT NOT NULL,
                    verdict TEXT NOT NULL CHECK (
                        verdict IN ('MET', 'NOT MET', 'UNCLEAR', 'ERROR')
                    ),
                    response_text TEXT,
                    source_text TEXT,
                    page_text TEXT,
                    raw_output TEXT,
                    parsed_ok BOOLEAN NOT NULL,
                    model_id TEXT NOT NULL,
                    embed_model_id TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    prompt_sha256 TEXT NOT NULL,
                    model_settings JSONB,
                    retrieval JSONB,
                    latency_ms INTEGER,
                    schema_version SMALLINT NOT NULL DEFAULT 1
                )
                """


def create_verdict_chunks_table_sql():
    verdict_chunks_table = _qualified_table("verdict_chunks")
    verdicts_table = _qualified_table("verdicts")
    return f"""
                CREATE TABLE IF NOT EXISTS {verdict_chunks_table} (
                    id BIGSERIAL PRIMARY KEY,
                    verdict_id BIGINT NOT NULL REFERENCES {verdicts_table}(id)
                        ON DELETE CASCADE,
                    rank SMALLINT NOT NULL,
                    embeddings_table TEXT NOT NULL,
                    embedding_row_id INTEGER,
                    doc_name TEXT,
                    page TEXT,
                    distance DOUBLE PRECISION,
                    relevance_score DOUBLE PRECISION,
                    retrieval_leg TEXT,
                    fusion_rank SMALLINT,
                    rerank_score DOUBLE PRECISION,
                    chunk_sha256 TEXT NOT NULL,
                    chunk_text TEXT,
                    UNIQUE (verdict_id, rank)
                )
                """


def _index_specs():
    verdicts_table = _qualified_table("verdicts")
    verdict_chunks_table = _qualified_table("verdict_chunks")
    return (
        ("idx_verdicts_created_at", f"ON {verdicts_table} (created_at)"),
        (
            "idx_verdicts_requirement_sha256",
            f"ON {verdicts_table} (requirement_sha256)",
        ),
        (
            "idx_verdicts_contract_id_created_at",
            f"ON {verdicts_table} (contract_id, created_at)",
        ),
        (
            "idx_verdict_chunks_verdict_id",
            f"ON {verdict_chunks_table} (verdict_id)",
        ),
    )


def expected_verdict_index_names():
    return tuple(name for name, _ in _index_specs())


def verdict_index_statements():
    return [
        f"""
                CREATE INDEX IF NOT EXISTS {name}
                {body}
                """
        for name, body in _index_specs()
    ]
