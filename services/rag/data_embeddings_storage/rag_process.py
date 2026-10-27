import asyncio
import hashlib
import re
from common.utils.helper import Helper
from common.utils.logger import log
from common.utils.aws_files_access import object_keys
from data_preprocessing.bedrock.bda_results import RAG_OUTPUT_PREFIX
from data_embeddings_storage.database.data_processing_embeddings import EmbeddingProcessor
from data_embeddings_storage.database.table_setup import create_embedding_table


 

def embeddings_table_for_key(key):
    base = key.rsplit("/", 1)[-1].removesuffix(".json")
    slug = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_") or "doc"
    name = f"emb_{slug}"
    if len(name) > 41:
        digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
        name = f"{name[: 41 - 9].rstrip('_')}_{digest}"
    return name


def is_rag_output_key(key):
    return key.startswith(RAG_OUTPUT_PREFIX) and key.endswith(".json")


async def invoke_rag_process(key):

    bucket = Helper.get_property("output_bucket")

    log.info(f"invoke_rag_process() Loading s3://{bucket}/{key}")
    documents = [
        doc for doc in Helper.get_json_from_s3(bucket, key)
        if isinstance(doc, dict) and doc.get("text")
    ]
    log.info(f"invoke_rag_process() {len(documents)} document(s) to embed from {key}")
    if not documents:
        return {"total": 0, "postgres_processed": 0, "failed": 0, "errors": []}

    table_name = embeddings_table_for_key(key)
    log.info(f"invoke_rag_process() Using embeddings table {table_name}")
    await create_embedding_table(table_name)
    status = await EmbeddingProcessor(table_name=table_name).process_data(documents, batch_size=30)
    if status["failed"]:
        raise RuntimeError(f"Embedding failed for {status['failed']} document(s) of {key}: {status['errors'][:5]}")
    return status


def handler(event, context=None):
    
    keys = [key for key in object_keys(event) if is_rag_output_key(key)]
    if not keys:
        log.info(f"No RAG_Output JSON objects in event. Skipping. Event={event}")
        return {"status": "skipped"}
    return {"status": "completed", "results": {key: asyncio.run(invoke_rag_process(key)) for key in keys}}


# def main():
#     if len(sys.argv) < 2:
#         raise SystemExit("usage: python -m data_embeddings_storage.rag_process RAG_Output/<folders>/<file name>.json")
#     print(asyncio.run(invoke_rag_process(sys.argv[1])))


# if __name__ == "__main__":
#     main()
