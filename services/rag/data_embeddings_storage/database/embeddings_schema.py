from common.utils.contract_config import validate_embeddings_table_name

EMBEDDING_DIMENSION = 1536


def create_embeddings_table_sql(table_name, embedding_dimension=EMBEDDING_DIMENSION):
    table = validate_embeddings_table_name(table_name)
    return f"""
                CREATE TABLE IF NOT EXISTS {table} (
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


def embeddings_index_statements(table_name):
    table = validate_embeddings_table_name(table_name)
    return [
        f"""
                CREATE INDEX IF NOT EXISTS idx_{table}_hnsw
                ON {table}
                USING hnsw (embedding vector_cosine_ops)
                WITH (m=16, ef_construction=128)
                """,
        f"""
                CREATE INDEX IF NOT EXISTS idx_{table}_metadata
                ON {table} USING GIN (metadata)
                """,
        f"""
                CREATE INDEX IF NOT EXISTS idx_{table}_search_text_trgm
                ON {table} USING GIN (text gin_trgm_ops)
                """,
        f"""
                CREATE INDEX IF NOT EXISTS idx_{table}_search_tsv
                ON {table} USING GIN (search_tsv)
                """,
    ]
