from common.utils.contract_config import validate_embeddings_table_name

EMBEDDING_DIMENSION = 1536
APP_SCHEMA = "aisuite_schema"
APP_OWNER_ROLE = "aisuite_app_owner"
SEARCH_PATH_SQL = f"SET search_path TO {APP_SCHEMA}, public"


def validate_app_schema(schema_name: str = APP_SCHEMA) -> str:
    return validate_embeddings_table_name(schema_name)


def qualified_embeddings_table(table_name: str) -> str:
    table = validate_embeddings_table_name(table_name)
    schema = validate_app_schema()
    return f"{schema}.{table}"


def create_embeddings_table_sql(table_name, embedding_dimension=EMBEDDING_DIMENSION):
    qualified = qualified_embeddings_table(table_name)
    return f"""
                CREATE TABLE IF NOT EXISTS {qualified} (
                    id SERIAL PRIMARY KEY,
                    text TEXT NOT NULL,
                    metadata JSONB DEFAULT NULL,
                    embedding VECTOR({embedding_dimension}) NOT NULL,
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
                """


def _index_specs(table_name):
    table = validate_embeddings_table_name(table_name)
    qualified = qualified_embeddings_table(table_name)
    return (
        (
            f"idx_{table}_hnsw",
            f"ON {qualified} USING hnsw (embedding vector_cosine_ops) "
            "WITH (m=16, ef_construction=128)",
        ),
        (f"idx_{table}_metadata", f"ON {qualified} USING GIN (metadata)"),
        (
            f"idx_{table}_search_text_trgm",
            f"ON {qualified} USING GIN (text gin_trgm_ops)",
        ),
        (f"idx_{table}_search_tsv", f"ON {qualified} USING GIN (search_tsv)"),
    )


def expected_index_names(table_name):
    return tuple(name for name, _ in _index_specs(table_name))


def embeddings_index_statements(table_name):
    return [
        f"""
                CREATE INDEX IF NOT EXISTS {name}
                {body}
                """
        for name, body in _index_specs(table_name)
    ]
