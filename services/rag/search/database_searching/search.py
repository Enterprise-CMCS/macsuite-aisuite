import numpy as np
from data_embeddings_storage.database.connection import get_connection, release_connection
from search.database_searching.aws_embedding_client import BedrockEmbeddingClient
from search.database_searching.reranker import CohereReranker
from typing import List, Dict, Any


class SearchEngine:
    def __init__(self):
        self.bedrock_client = BedrockEmbeddingClient()
        self.reranker = CohereReranker()
        self.table_name = "embeddings"

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


    async def semantic_search(self, query_text, limit = 100):
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

    # async def hybrid_search(self, query, top_k=10,bm25_weight=0.5,fulltext_weight=0.1,semantic_weight=0.3,rrf_k=60):


    #     print("[Hybrid Search] Starting...")


    #     # Full-text
    #     fulltext_results = await self.fulltext_search(query, limit=top_k*2)
    #     fulltext_map = {r['id']: {'rank': i+1, 'text': r['text']} for i, r in enumerate(fulltext_results)}
    #     print(f"[Fulltext] Found {len(fulltext_results)} results, IDs: {list(fulltext_map.keys())[:5]}")


    #     # Semantic
    #     semantic_results = await self.semantic_search(query, limit=top_k*2)
    #     semantic_map = {r['id']: {'rank': i+1, 'text': r['text'], 'metadata': r['metadata']} for i, r in enumerate(semantic_results)}
    #     print(f"[Semantic] Found {len(semantic_results)} results, IDs: {list(semantic_map.keys())[:5]}")


    #     # Merge all doc_ids
    #     all_doc_ids = set(list(fulltext_map.keys()) + list(semantic_map.keys()))

    #     rrf_scores = {}
    #     for doc_id in all_doc_ids:
    #         fulltext_rank = fulltext_map.get(doc_id, {}).get('rank', float('inf'))
    #         semantic_rank = semantic_map.get(doc_id, {}).get('rank', float('inf'))


    #         score = (
    #             (1.0 / (rrf_k + fulltext_rank) if fulltext_rank != float('inf') else 0.0) * fulltext_weight +
    #             (1.0 / (rrf_k + semantic_rank) if semantic_rank != float('inf') else 0.0) * semantic_weight
    #         )


    #         rrf_scores[doc_id] = {
    #             'score': score,
    #             'fulltext_rank': fulltext_rank if fulltext_rank != float('inf') else None,
    #             'semantic_rank': semantic_rank if semantic_rank != float('inf') else None
    #         }


    #     sorted_results = sorted(rrf_scores.items(), key=lambda x: x[1]['score'], reverse=True)[:top_k]



    #     if not sorted_results:
    #         return []


    #     doc_ids = [int(doc_id) for doc_id, _ in sorted_results]
    #     postgres_pool = await get_connection()

    #     try:
    #         placeholders = ','.join(f'${i+1}::INTEGER' for i in range(len(doc_ids)))
    #         full_docs = await postgres_pool.fetch(f"""
    #             SELECT id, text, metadata, embedding
    #             FROM embeddings
    #             WHERE id = ANY(ARRAY[{placeholders}])
    #         """, *doc_ids)

    #         doc_map = {doc['id']: doc for doc in full_docs}
            
    #     finally:
    #         await postgres_pool.close()

    #     final_results = []
    #     for doc_id, rrf_data in sorted_results:
    #         if doc_id in doc_map:
    #             doc = doc_map[doc_id]
    #             final_results.append({
    #                 'id': doc['id'],
    #                 'text': doc['text'],
    #                 'metadata': doc['metadata'],
    #                 'embedding': doc['embedding'],
    #                 'rrf_score': rrf_data['score'],
    #                 'debug': {
    #                     'fulltext_rank': rrf_data['fulltext_rank'],
    #                     'semantic_rank': rrf_data['semantic_rank'],
    #                 }
    #             })


    #     print(f"Hybrid search complete: {len(final_results)} results")



    #     return final_results
    

    # async def hybrid_cohere_search(self,query,hybridresult,top_k=5,bm25_weight=0.5,semantic_weight=0.4,include_debug=False):

    #     print("Starting hybrid + reranking...")

    #     print("Reranking with Cohere...")
    #     reranked = await self.reranker.rerank_results(
    #         query,
    #         hybridresult,
    #         top_k=top_k
    #     )
        
    #     if include_debug:
    #         for result in reranked:
    #             if 'debug' in result:
    #                 result['rerank_debug'] = {
    #                     'original_rrf_score': result.get('rrf_score'),
    #                     'cohere_score': result.get('rerank_score'),
    #                 }
        
    #     return reranked



