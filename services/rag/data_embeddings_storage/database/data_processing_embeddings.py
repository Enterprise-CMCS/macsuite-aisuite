import asyncio
import json
import numpy as np
from data_embeddings_storage.database import connection
from data_embeddings_storage.database.embeddings_client import BedrockEmbeddingClient
from data_embeddings_storage.database.connection import initialization_db, close_db, get_connection, release_connection


class EmbeddingProcessor:

    def __init__(self, max_concurrent_requests=1):
        self.embedding_client = BedrockEmbeddingClient()
        self.max_concurrent_requests = max_concurrent_requests
    
    async def process_data(self, data, batch_size=1):

        status = {"total": len(data), "postgres_processed": 0, "failed": 0, "errors": []}

        await initialization_db()
        db_connection = await get_connection()

        try:
            for i in range(0, len(data), batch_size):
                batch = data[i:i + batch_size]
                print(f"\nProcessing batch {i // batch_size + 1}: {len(batch)} items")
                try:
                    filtered_batch = [
                        item for item in batch 
                        if item.get('text', '').strip()
                    ]
                    
                    if not filtered_batch:
                        status["failed"] += len(batch)
                        status["errors"].append(f"Batch {i // batch_size + 1}: All items have empty text")
                        continue
                    
                    texts = [
                        item.get('text', '').strip() 
                        for item in filtered_batch
                    ]

                    print(f"Generating embeddings for {len(filtered_batch)} items...")
                    embeddings = await self.embedding_client.batch_get_embeddings(
                        texts,
                        max_concurrent_requests=self.max_concurrent_requests
                    )

                    print("Storing embeddings in the postgres database...")
                    batch_status = await self.store_embeddings(db_connection, filtered_batch, embeddings)

                    status["postgres_processed"] += batch_status["inserted"]

                    if batch_status["errors"]:
                        status["errors"].extend(batch_status["errors"])
                        status["failed"] += batch_status["failed"]
                    
                except Exception as e:
                    error_msg = f"Batch {i // batch_size + 1} failed with error: {e}"
                    print(error_msg)
                    status["failed"] += len(batch)
                    status["errors"].append(error_msg)

        finally:
            await release_connection(db_connection)
            await close_db()

            
        print(f"\n{'='*20} Processing info {'='*20}")
        print(f"Processing complete:")
        print(f"Total items: {status['total']}")
        print(f"Successfully processed: {status['postgres_processed']}")
        print(f"Failed items: {status['failed']}")
        print(f"{'='*50}\n")

        return status
    
    async def store_embeddings(self, pool, batch, embeddings):

        batch_status = {"inserted": 0, "ids": [], "errors": [], "failed": 0}

        async with pool.transaction():
            for item, embedding in zip(batch, embeddings):
                try:
                    if isinstance(embedding, Exception):
                        batch_status["failed"] += 1
                        batch_status["errors"].append(f"Embedding error: {str(embedding)}")
                        continue

                    text = item.get('text', '')
                    metadata_dic = item.get('metadata', {})
                    metadata_json = json.dumps(metadata_dic)

                    if isinstance(embedding, np.ndarray):
                        embedding_flat = embedding.flatten()
                    else:
                        # Fallback for unexpected types
                        embedding_flat = np.array(embedding, dtype=np.float32).flatten()

                    if not embedding_flat.size:
                        batch_status["failed"] += 1
                        batch_status["errors"].append("Empty embedding received")
                        continue

                    doc_id = await pool.fetchval("""
                        INSERT INTO embeddings (text, metadata, embedding)
                        VALUES ($1, $2, $3)
                        RETURNING id
                    """, text, metadata_json, embedding_flat)

                    batch_status["inserted"] += 1
                    batch_status["ids"].append((doc_id, text))
                except Exception as e:
                    batch_status["failed"] += 1
                    batch_status["errors"].append(f"DB insert error: {str(e)}")
        return batch_status

                    
