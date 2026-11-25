import asyncio
import json

from common.utils.aws_files_access import object_keys
from common.utils.contract_config import validate_embeddings_table_name
from common.utils.helper import Helper
from common.utils.logger import log
from common.utils.manifest import MAX_TABLE_NAME_LEN, manifest_embeddings_table_name
from data_preprocessing.bedrock.bda_results import RAG_OUTPUT_PREFIX
from data_embeddings_storage.database.connection import close_db, pooled_connection
from data_embeddings_storage.database.data_processing_embeddings import EmbeddingProcessor
from data_embeddings_storage.database.embeddings_schema import qualified_embeddings_table
from data_embeddings_storage.database.table_setup import create_embedding_table

EMBED_BATCH_SIZE = 30
SOURCE_METADATA_KEY = "rag_output_key"


def is_rag_output_key(key):
    return key.startswith(RAG_OUTPUT_PREFIX) and key.endswith(".json")


def is_common_rag_output_key(key, common_prefix=None):
    if not is_rag_output_key(key):
        return False
    if common_prefix is None:
        common_prefix = Helper.get_common_input_prefix()
    if not common_prefix:
        return False
    normalized = common_prefix.strip("/")
    if not normalized:
        return False
    return key.startswith(f"{RAG_OUTPUT_PREFIX}{normalized}/")


def _load_rag_documents(key):
    bucket = Helper.get_property("output_bucket")
    log.info(f"Loading s3://{bucket}/{key}")
    documents = [
        doc for doc in Helper.get_json_from_s3(bucket, key)
        if isinstance(doc, dict) and doc.get("text")
    ]
    log.info(f"{len(documents)} document(s) to embed from {key}")
    return documents


async def _generate_embeddings(documents, table_name, key):
    processor = EmbeddingProcessor(table_name=table_name)
    records, status = await processor.generate_embeddings(
        documents,
        batch_size=EMBED_BATCH_SIZE,
        fail_fast=True,
    )
    if status["failed"]:
        raise RuntimeError(f"Embedding failed for {status['failed']} document(s) of {key}: {status['errors'][:5]}")
    return processor, records


async def _store_embeddings(connection, processor, records):
    return await processor.insert_embeddings(connection, records, batch_size=EMBED_BATCH_SIZE)


async def delete_source_rows(connection, table_name, rag_output_key):
    """Remove rows previously embedded from one RAG_Output file (uses the metadata GIN index)."""
    await connection.execute(
        f"DELETE FROM {qualified_embeddings_table(table_name)} WHERE metadata @> $1::jsonb",
        json.dumps({SOURCE_METADATA_KEY: rag_output_key}),
    )


async def _replace_source_embeddings(documents, table_name, key):
    stamped = [
        {**doc, "metadata": {**(doc.get("metadata") or {}), SOURCE_METADATA_KEY: key}}
        for doc in documents
    ]
    processor = None
    records = []
    if stamped:
        processor, records = await _generate_embeddings(stamped, table_name, key)

    lock_id = f"{table_name}:{key}"
    async with pooled_connection() as connection:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", lock_id)
            await delete_source_rows(connection, table_name, key)
            inserted = await _store_embeddings(connection, processor, records) if records else 0

    return {"total": len(stamped), "postgres_processed": inserted, "failed": 0, "errors": []}


def _validate_manifest_table(table_name):
    if not isinstance(table_name, str) or not table_name.startswith("embeddings_"):
        raise ValueError(f"Invalid manifest embeddings table '{table_name}'.")
    if len(table_name) > MAX_TABLE_NAME_LEN:
        raise ValueError(f"Embeddings table '{table_name}' exceeds {MAX_TABLE_NAME_LEN} characters.")
    return validate_embeddings_table_name(table_name)


async def invoke_rag_process(key):
    if not is_common_rag_output_key(key):
        raise ValueError(f"S3 notification embedding is only supported for common reference keys, got '{key}'.")
    table_name = Helper.get_common_embeddings_table_name()
    if not table_name:
        raise ValueError("common_embeddings_table_name is not configured.")

    documents = _load_rag_documents(key)
    log.info(f"invoke_rag_process() Using common embeddings table {table_name} for {key}")
    await create_embedding_table(table_name)
    try:
        return await _replace_source_embeddings(documents, table_name, key)
    finally:
        await close_db()


def _required(event, field):
    value = event.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Embeddings action '{event.get('action')}' requires '{field}'.")
    return value


async def _create_table(event):
    request_key = _required(event, "requestKey")
    state = _required(event, "state")
    table_name = manifest_embeddings_table_name(request_key, state)
    await create_embedding_table(table_name)
    log.info(f"Created embeddings table {table_name} for request {request_key}")
    return {"state": state.strip().upper(), "tableName": table_name}


async def _embed(event):
    key = _required(event, "key")
    table_name = _validate_manifest_table(event.get("tableName"))
    if not is_rag_output_key(key):
        raise ValueError(f"'{key}' is not a {RAG_OUTPUT_PREFIX} JSON object.")

    status = await _replace_source_embeddings(_load_rag_documents(key), table_name, key)
    return {"key": key, "tableName": table_name, "embedded": status["postgres_processed"]}


ACTIONS = {"create_table": _create_table, "embed": _embed}


async def _run_action(event):
    action = ACTIONS.get(event["action"])
    if action is None:
        raise ValueError(f"Unsupported embeddings action '{event['action']}'.")
    try:
        return await action(event)
    finally:
        await close_db()


def handler(event, context=None):
    if isinstance(event, dict) and event.get("action"):
        return asyncio.run(_run_action(event))

    keys = [key for key in object_keys(event) if is_common_rag_output_key(key)]
    if not keys:
        log.info(f"No common RAG_Output JSON objects in event. Skipping. Event={event}")
        return {"status": "skipped"}
    return {"status": "completed", "results": {key: asyncio.run(invoke_rag_process(key)) for key in keys}}
