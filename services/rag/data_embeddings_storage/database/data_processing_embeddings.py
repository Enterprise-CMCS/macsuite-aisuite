import json
import numpy as np
from data_embeddings_storage.database.embeddings_client import BedrockEmbeddingClient
from data_embeddings_storage.database.connection import initialization_db, get_connection, release_connection
from data_embeddings_storage.database.embeddings_schema import qualified_embeddings_table
from common.utils.helper import Helper


def _non_empty_text(item):
    if not isinstance(item, dict):
        return ""
    text = item.get("text")
    return text.strip() if isinstance(text, str) else ""


def _flatten_embedding(embedding):
    if isinstance(embedding, np.ndarray):
        embedding_flat = embedding.flatten()
    else:
        embedding_flat = np.array(embedding, dtype=np.float32).flatten()
    if not embedding_flat.size:
        raise ValueError("Empty embedding received")
    return embedding_flat


class EmbeddingProcessor:

    def __init__(self, max_concurrent_requests=10, table_name=None):
        self.embedding_client = BedrockEmbeddingClient()
        self.max_concurrent_requests = max_concurrent_requests
        self.table_name = table_name or Helper.get_embeddings_table_name()

    async def generate_embeddings(self, data, batch_size=20, fail_fast=False):
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")

        records = []
        status = {"total": len(data), "generated": 0, "failed": 0, "errors": []}

        for i in range(0, len(data), batch_size):
            batch = data[i:i + batch_size]
            batch_num = i // batch_size + 1
            print(f"\nProcessing batch {batch_num}: {len(batch)} items")

            filtered_batch = [item for item in batch if _non_empty_text(item)]
            skipped = len(batch) - len(filtered_batch)
            if skipped:
                status["failed"] += skipped
                status["errors"].append(
                    f"Batch {batch_num}: {skipped} item(s) have empty text"
                )
                if fail_fast:
                    break

            if not filtered_batch:
                continue

            texts = [_non_empty_text(item) for item in filtered_batch]
            print(f"Generating embeddings for {len(filtered_batch)} items...")
            try:
                embeddings = await self.embedding_client.batch_get_embeddings(
                    texts,
                    max_concurrent_requests=self.max_concurrent_requests,
                )
            except Exception as e:
                error_msg = f"Batch {batch_num} failed with error: {e}"
                print(error_msg)
                status["failed"] += len(filtered_batch)
                status["errors"].append(error_msg)
                if fail_fast:
                    break
                continue

            if len(embeddings) != len(filtered_batch):
                status["failed"] += len(filtered_batch)
                status["errors"].append(
                    f"Batch {batch_num}: expected {len(filtered_batch)} embedding(s), got {len(embeddings)}"
                )
                if fail_fast:
                    break
                continue

            for item, embedding in zip(filtered_batch, embeddings):
                if isinstance(embedding, Exception):
                    status["failed"] += 1
                    status["errors"].append(f"Embedding error: {str(embedding)}")
                    continue

                try:
                    embedding_flat = _flatten_embedding(embedding)
                except Exception as e:
                    status["failed"] += 1
                    status["errors"].append(f"Embedding error: {str(e)}")
                    continue

                text = item.get("text", "")
                metadata_json = json.dumps(item.get("metadata") or {})
                records.append((text, metadata_json, embedding_flat))
                status["generated"] += 1

            if fail_fast and status["failed"]:
                break

        return records, status

    async def insert_embeddings(self, db_connection, records, batch_size=None):
        """Insert prepared (text, metadata_json, embedding) records on the caller's connection."""
        if not records:
            return 0
        if batch_size is not None and batch_size <= 0:
            raise ValueError("batch_size must be positive")

        table = qualified_embeddings_table(self.table_name)
        sql = f"""
            INSERT INTO {table} (text, metadata, embedding)
            VALUES ($1, $2, $3)
        """
        if batch_size is None:
            await db_connection.executemany(sql, records)
        else:
            for i in range(0, len(records), batch_size):
                await db_connection.executemany(sql, records[i:i + batch_size])
        return len(records)

    async def process_data(self, data, batch_size=20, db_connection=None):
        records, gen_status = await self.generate_embeddings(data, batch_size=batch_size)
        status = {
            "total": gen_status["total"],
            "postgres_processed": 0,
            "failed": gen_status["failed"],
            "errors": list(gen_status["errors"]),
        }

        if records:
            owns_connection = db_connection is None
            if owns_connection:
                await initialization_db()
                db_connection = await get_connection()

            try:
                print("Storing embeddings in the postgres database...")
                async with db_connection.transaction():
                    status["postgres_processed"] = await self.insert_embeddings(
                        db_connection,
                        records,
                        batch_size=batch_size,
                    )
            except Exception as e:
                error_msg = f"DB insert failed with error: {e}"
                print(error_msg)
                status["failed"] += len(records)
                status["errors"].append(error_msg)
            finally:
                if owns_connection:
                    await release_connection(db_connection)

        print(f"\n{'='*20} Processing info {'='*20}")
        print("Processing complete:")
        print(f"Total items: {status['total']}")
        print(f"Successfully processed: {status['postgres_processed']}")
        print(f"Failed items: {status['failed']}")
        print(f"{'='*50}\n")

        return status

    async def store_embeddings(self, pool, batch, embeddings):
        batch_status = {"inserted": 0, "ids": [], "errors": [], "failed": 0}
        table = qualified_embeddings_table(self.table_name)

        async with pool.transaction():
            for item, embedding in zip(batch, embeddings):
                if isinstance(embedding, Exception):
                    batch_status["failed"] += 1
                    batch_status["errors"].append(f"Embedding error: {str(embedding)}")
                    continue

                text = item.get("text", "")
                metadata_json = json.dumps(item.get("metadata") or {})

                try:
                    embedding_flat = _flatten_embedding(embedding)
                except Exception as e:
                    batch_status["failed"] += 1
                    batch_status["errors"].append(str(e))
                    continue

                doc_id = await pool.fetchval(f"""
                    INSERT INTO {table} (text, metadata, embedding)
                    VALUES ($1, $2, $3)
                    RETURNING id
                """, text, metadata_json, embedding_flat)

                batch_status["inserted"] += 1
                batch_status["ids"].append((doc_id, text))
        return batch_status

                    
