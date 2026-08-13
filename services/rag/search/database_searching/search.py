import asyncio

import numpy as np
from data_embeddings_storage.database.connection import get_connection, release_connection
from search.database_searching.aws_embedding_client import BedrockEmbeddingClient
from search.database_searching.fusion import reciprocal_rank_fusion
from search.database_searching.reranker import CohereReranker
from common.utils.helper import Helper
from common.utils.logger import get_logger

logger = get_logger(__name__)

CANDIDATE_LIMIT = 32


class SearchEngine:
    def __init__(self, table_name=None):
        self.bedrock_client = BedrockEmbeddingClient()
        self.reranker = CohereReranker()
        resolved_table_name = table_name or Helper.get_embeddings_table_name()
        self.table_name = Helper.validate_embeddings_table_name(resolved_table_name)

    async def fulltext_search(self, query: str, limit: int = 30):
        connection = await get_connection()
        try:
            results = await connection.fetch(f"""
                SELECT
                    id,
                    text,
                    metadata,
                    ts_rank(search_tsv, plainto_tsquery('english', $1)) AS rank
                FROM {self.table_name}
                WHERE search_tsv @@ plainto_tsquery('english', $1)
                ORDER BY rank DESC
                LIMIT $2
            """, query, limit)

            return [dict(row) for row in results] if results else []
        finally:
            await release_connection(connection)

    async def hybrid_search(
        self,
        normalized_query: str,
        expanded_query: str,
        *,
        limit: int = CANDIDATE_LIMIT,
        top_k: int = 8,
    ):
        fulltext_hits, vector_hits = await asyncio.gather(
            self.fulltext_search(normalized_query, limit=limit),
            self.semantic_search(expanded_query, limit=limit),
        )

        fulltext_tagged = [
            {**hit, "retrieval_leg": "fulltext", "distance": None}
            for hit in fulltext_hits
        ]
        vector_tagged = [
            {**hit, "retrieval_leg": "vector"} for hit in vector_hits
        ]

        fused = reciprocal_rank_fusion(fulltext_tagged, vector_tagged)
        for fusion_rank, document in enumerate(fused, start=1):
            document["fusion_rank"] = fusion_rank

        candidates = fused[:CANDIDATE_LIMIT]

        try:
            return await self.reranker.rerank_results(
                query=normalized_query,
                documents=candidates,
                top_k=top_k,
            )
        except Exception as error:
            logger.error(
                "hybrid_search_rerank_failed",
                table_name=self.table_name,
                error=str(error),
            )
            return candidates[:top_k]

    async def semantic_search(self, query_text, limit=100):
        query_embedding = await self.bedrock_client.get_embedding(query_text)

        if isinstance(query_embedding, list):
            embedding_flattened = np.array(query_embedding).flatten()
        else:
            embedding_float = query_embedding.get('float', [])
            embedding_flattened = np.array(embedding_float).flatten()

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
