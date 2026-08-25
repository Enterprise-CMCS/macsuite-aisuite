import sys
import json
from pathlib import Path


src_path = Path(__file__).resolve().parent.parent.parent  # Adjust the number based on your project structure
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

import logging
import numpy as np
from data_embeddings_storage.database.connection import get_connection, release_connection
from search.database_searching.aws_embedding_client import BedrockEmbeddingClient
from search.database_searching.reranker import CohereReranker
from common.utils.helper import Helper
from common.utils import logger
from common.utils.save_logger import save_retrieval_log


LOG_FILE = Path(r"C:\Moktari\code\rag_results\state_of_oklahoma\semantic\log\semantic_results.jsonl")

logger = logging.getLogger(__name__)

TABLE_NAME = Helper.get_property("embeddings_table_name")


class SearchEngine:
    def __init__(self, table_name=None):
        self.bedrock_client = BedrockEmbeddingClient()
        self.reranker = CohereReranker()
        self.table_name = table_name or Helper.get_embeddings_table_name()

    async def fulltext_search(self, query: str, limit: int = 30):
        connection = await get_connection()
        try:
            results = await connection.fetch(f"""
                SELECT
                    id,
                    text,
                    ts_rank(search_tsv, plainto_tsquery('english', $1)) AS rank
                FROM {self.table_name}
                WHERE search_tsv @@ plainto_tsquery('english', $1)
                ORDER BY rank DESC
                LIMIT $2
            """, query, limit)

            return [dict(row) for row in results] if results else []
        finally:
            await release_connection(connection)

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
                    embedding <=> $1::vector AS distance,
                    1-(embedding <=> $1::vector) AS similarity
                FROM {self.table_name}
                ORDER BY embedding <=> $1::vector
                LIMIT $2
            """, embedding_flattened, limit)

            # return [dict(row) for row in results] if results else []
        finally:
            await release_connection(connection)

        results = [dict(row) for row in results] if results else []
 
        logger.info( "[SEMANTIC] Query=%s | Results=%s", query_text,
            [
                {
                    "id": r["id"],
                    "similarity": round(float(r["similarity"]), 4),
                    "doc": (json.loads(r["metadata"]) or {}).get("doc_id"),
                    "page": (json.loads(r["metadata"]) or {}).get("page"),
                }
                for r in results[:10]
            ],
        )

        save_retrieval_log(stage="semantic", query=query_text, results=results, log_file=LOG_FILE, max_results=3,)

        return results
