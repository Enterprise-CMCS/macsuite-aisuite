import json

import numpy as np
from data_embeddings_storage.database.connection import get_connection, release_connection
from search.database_searching.aws_embedding_client import BedrockEmbeddingClient
from search.database_searching.reranker import CohereReranker
from common.utils.helper import Helper
from common.utils.logger import log

RRF_K = 60


MIN_EF_SEARCH = 64


def _parse_metadata(metadata):
    """asyncpg hands JSONB back as text unless a codec is registered."""
    if isinstance(metadata, str):
        try:
            return json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            return {}
    return metadata if isinstance(metadata, dict) else {}


class SearchEngine:
    def __init__(self, table_name=None):
        self.bedrock_client = BedrockEmbeddingClient()
        self.reranker = CohereReranker()
        self.table_name = table_name or Helper.get_embeddings_table_name()

    async def embed_query(self, query_text):
        embedding = await self.bedrock_client.get_embedding(query_text, input_type="search_query")

        if isinstance(embedding, list):
            return np.array(embedding).flatten()
        return np.array(embedding.get('float', [])).flatten()

    async def fulltext_search(self, query: str, limit: int = 30):
        connection = await get_connection()
        try:
            results = await connection.fetch(f"""
                SELECT
                    id,
                    text,
                    metadata,
                    ts_rank_cd(search_tsv, query, 32) AS rank
                FROM {self.table_name},
                     to_tsquery('english',NULLIF(replace(plainto_tsquery('english', $1)::text,' & ',' | '),'')) AS query
                WHERE search_tsv @@ query
                ORDER BY rank DESC
                LIMIT $2
            """, query, limit)

            return [dict(row) for row in results] if results else []
        finally:
            await release_connection(connection)

    async def semantic_search(self, query_text, limit=100):
        embedding_flattened = await self.embed_query(query_text)

        connection = await get_connection()
        try:
            results = await connection.fetch(f"""
                SELECT
                    id,
                    text,
                    metadata,
                    embedding <=> $1::vector AS distance
                FROM {self.table_name}
                ORDER BY embedding <=> $1::vector
                LIMIT $2
            """, embedding_flattened, limit)

            return [dict(row) for row in results] if results else []
        finally:
            await release_connection(connection)

    async def hybrid_search(self, query_text, limit=12, dense_limit=60, lexical_limit=60):
        embedding_flattened = await self.embed_query(query_text)
        ef_search = max(MIN_EF_SEARCH, dense_limit)

        connection = await get_connection()
        try:
            async with connection.transaction():
                await connection.execute(f"SET LOCAL hnsw.ef_search = {int(ef_search)}")

                results = await connection.fetch(f"""
                    WITH dense AS (
                        SELECT
                            id,
                            text,
                            metadata,
                            embedding <=> $1::vector AS distance,
                            ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector) AS position
                        FROM {self.table_name}
                        ORDER BY embedding <=> $1::vector
                        LIMIT $3
                    ),
                    lexical AS (
                        SELECT
                            id,
                            text,
                            metadata,
                            -- Scored here as well so a chunk the vector arm never
                            -- shortlisted still carries a cosine distance. Otherwise
                            -- its retrieval confidence lands in the workbook blank.
                            embedding <=> $1::vector AS distance,
                            ts_rank_cd(search_tsv, query, 32) AS lexical_rank,
                            ROW_NUMBER() OVER (ORDER BY ts_rank_cd(search_tsv, query, 32) DESC) AS position
                        FROM {self.table_name},
                             to_tsquery('english',NULLIF(replace(plainto_tsquery('english', $2)::text,' & ',' | '),'')) AS query
                        WHERE search_tsv @@ query
                        ORDER BY lexical_rank DESC
                        LIMIT $4
                    )
                    SELECT
                        COALESCE(d.id, l.id) AS id,
                        COALESCE(d.text, l.text) AS text,
                        COALESCE(d.metadata, l.metadata) AS metadata,
                        COALESCE(d.distance, l.distance) AS distance,
                        CASE WHEN COALESCE(d.distance, l.distance) IS NOT NULL
                             THEN ROUND(GREATEST(0, LEAST(1, 1 - COALESCE(d.distance, l.distance)))::numeric, 4)::float8
                        END AS retrieval_confidence,
                        l.lexical_rank,
                        d.position AS dense_position,
                        l.position AS lexical_position,
                        (COALESCE(1.0 / ($5 + d.position), 0)
                            + COALESCE(1.0 / ($5 + l.position), 0))::float8 AS fused_score
                    FROM dense d
                    FULL OUTER JOIN lexical l ON d.id = l.id
                    ORDER BY fused_score DESC
                    LIMIT $6
                """, embedding_flattened, query_text, dense_limit, lexical_limit, RRF_K, limit)

            return [dict(row, metadata=_parse_metadata(row["metadata"])) for row in results] if results else []
        finally:
            await release_connection(connection)

    async def reranked_search(self, query_text, limit=8, candidate_limit=40):
        candidates = await self.hybrid_search(query_text, limit=candidate_limit)
        if not candidates:
            return []
        try:
            reranked = await self.reranker.rerank_results(query_text, candidates, top_k=limit)
        except Exception as lclEx:
            Helper.print_exception("SearchEngine.reranked_search", lclEx,
                                   "Cohere rerank failed, falling back to the fused hybrid order.")
            return candidates[:limit]

        log.debug(f"reranked_search() reranked {len(candidates)} candidates down to {len(reranked)}")
        return reranked
