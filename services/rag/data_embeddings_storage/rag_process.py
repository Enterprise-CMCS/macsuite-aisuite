import asyncio
import hashlib
import re

from common.utils.helper import Helper
from common.utils.logger import log
from common.utils.aws_files_access import object_keys
from data_preprocessing.bedrock.bda_results import RAG_OUTPUT_PREFIX
from data_embeddings_storage.database import embedding_versions
from data_embeddings_storage.database.connection import close_db, pooled_connection
from data_embeddings_storage.database.data_processing_embeddings import EmbeddingProcessor
from data_embeddings_storage.database.table_setup import create_embedding_table

MAX_TABLE_NAME_LEN = 41
EMBED_BATCH_SIZE = 30


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


def embeddings_table_for_key(key):
    if is_common_rag_output_key(key):
        common_table = Helper.get_common_embeddings_table_name()
        if common_table:
            return common_table

    base = key.rsplit("/", 1)[-1].removesuffix(".json")
    slug = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_") or "doc"
    name = f"emb_{slug}"
    if len(name) > MAX_TABLE_NAME_LEN:
        digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
        name = f"{name[: MAX_TABLE_NAME_LEN - 9].rstrip('_')}_{digest}"
    return name


def _load_rag_documents(key):
    bucket = Helper.get_property("output_bucket")
    log.info(f"Loading s3://{bucket}/{key}")
    documents = [
        doc for doc in Helper.get_json_from_s3(bucket, key)
        if isinstance(doc, dict) and doc.get("text")
    ]
    log.info(f"{len(documents)} document(s) to embed from {key}")
    return documents


async def _store_embeddings(documents, table_name, key):
    status = await EmbeddingProcessor(table_name=table_name).process_data(documents, batch_size=EMBED_BATCH_SIZE)
    if status["failed"]:
        raise RuntimeError(f"Embedding failed for {status['failed']} document(s) of {key}: {status['errors'][:5]}")
    return status


async def invoke_rag_process(key):
    documents = _load_rag_documents(key)
    is_common = is_common_rag_output_key(key)
    table_name = embeddings_table_for_key(key)

    if is_common:
        documents = [
            {**doc, "metadata": {**(doc.get("metadata") or {}), embedding_versions.SOURCE_METADATA_KEY: key}}
            for doc in documents
        ]
        log.info(f"invoke_rag_process() Using common embeddings table {table_name} for {key}")
        await create_embedding_table(table_name)
        try:
            async with pooled_connection() as connection:
                await embedding_versions.delete_source_rows(connection, table_name, key)
        finally:
            await close_db()
        if not documents:
            return {"total": 0, "postgres_processed": 0, "failed": 0, "errors": []}
        return await _store_embeddings(documents, table_name, key)

    if not documents:
        return {"total": 0, "postgres_processed": 0, "failed": 0, "errors": []}

    log.info(f"invoke_rag_process() Using embeddings table {table_name}")
    await create_embedding_table(table_name)
    return await _store_embeddings(documents, table_name, key)


def _required(event, field):
    value = event.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Embeddings action '{event.get('action')}' requires '{field}'.")
    return value


async def _allocate(event):
    request_key = _required(event, "requestKey")
    async with pooled_connection() as connection:
        await embedding_versions.ensure_registry(connection)
        version = await embedding_versions.allocate_version(connection, _required(event, "state"), request_key)
    await create_embedding_table(version.table_name)
    log.info(f"Allocated {version.table_name} (version {version.version}) for request {request_key}")
    return version.as_payload()


async def _embed(event):
    key = _required(event, "key")
    table_name = embedding_versions.validate_versioned_table_name(event.get("tableName"))
    if not is_rag_output_key(key):
        raise ValueError(f"'{key}' is not a {RAG_OUTPUT_PREFIX} JSON object.")

    documents = [
        {**doc, "metadata": {**(doc.get("metadata") or {}), embedding_versions.SOURCE_METADATA_KEY: key}}
        for doc in _load_rag_documents(key)
    ]
    async with pooled_connection() as connection:
        await embedding_versions.delete_source_rows(connection, table_name, key)
    if not documents:
        return {"key": key, "tableName": table_name, "embedded": 0}

    status = await _store_embeddings(documents, table_name, key)
    return {"key": key, "tableName": table_name, "embedded": status["postgres_processed"]}


async def _finalize(event):
    request_key = _required(event, "requestKey")
    table_name = embedding_versions.validate_versioned_table_name(event.get("tableName"))
    async with pooled_connection() as connection:
        # Adds dropped_at to a registry created before retention existed.
        await embedding_versions.ensure_registry(connection)
        row_count = await embedding_versions.mark_ready(connection, request_key, table_name)
    log.info(f"Embeddings table {table_name} ready with {row_count} row(s) for request {request_key}")
    return {"tableName": table_name, "rowCount": row_count}


async def _fail(event):
    request_key = _required(event, "requestKey")
    async with pooled_connection() as connection:
        # The run may have failed before the registry was ever created.
        await embedding_versions.ensure_registry(connection)
        marked = await embedding_versions.mark_failed(connection, request_key)
    return {"requestKey": request_key, "markedFailed": marked}


async def _expire(event):
    # Invoked monthly by EventBridge, which always sends retentionDays
    # (EMBEDDINGS_RETENTION_DAYS in embeddings-lambda-construct.ts).
    retention_days = event.get("retentionDays")
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 1:
        raise ValueError(f"retentionDays must be a whole number of days >= 1, got {retention_days!r}.")
    async with pooled_connection() as connection:
        await embedding_versions.ensure_registry(connection)
        result = await embedding_versions.expire_versions(connection, retention_days)
    log.info(f"Dropped {len(result.dropped)} expired embeddings table(s): {result.dropped}")
    return {"retentionDays": retention_days, "dropped": result.dropped, "notOwner": result.not_owner}


ACTIONS = {"allocate": _allocate, "embed": _embed, "finalize": _finalize, "fail": _fail, "expire": _expire}


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

