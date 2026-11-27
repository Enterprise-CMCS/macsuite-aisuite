import json
import uuid

from botocore.exceptions import ClientError

from common.utils.aws_files_access import head_s3_object_etag, object_keys, write_s3_json
from common.utils.helper import Helper
from common.utils.logger import log
from common.utils.manifest import SUPPORTED_DOCUMENT_EXTENSIONS
from common.utils.settings import aws_client, aws_session
from data_preprocessing.bedrock import bedrock_BDA as bda
from data_preprocessing.bedrock.bda_results import (
    OUTPUT_FOLDERS,
    RUN_ARTIFACTS_PREFIX,
    per_doc_output_key,
    rag_output_key,
)
from data_preprocessing.parsing import parsed_images
from data_preprocessing.parsing import parsed_text_data
from data_preprocessing.parsing import parsed_bda_table

_MAX_PUBLISH_ATTEMPTS = 3
_CONFLICT_CODES = frozenset({"PreconditionFailed", "ConditionalRequestConflict"})


def _new_attempt_namespace():
    return f"attempt-{uuid.uuid4().hex}"


def _cleanup_attempt_artifacts(attempt_namespace):
    output_bucket = Helper.get_property("output_bucket")
    prefix = f"{RUN_ARTIFACTS_PREFIX}{attempt_namespace}/"
    try:
        Helper.delete_bucket_file_recursively(aws_session().resource("s3"), output_bucket, prefix)
    except Exception as exc:
        log.warning(f"Could not clean up attempt artifacts at s3://{output_bucket}/{prefix}: {exc}")


def _publish_rag_output_if_current(
    output_bucket,
    rag_key,
    documents,
    source_bucket,
    source_key,
    expected_source_version,
):
    s3 = aws_client("s3")
    body = json.dumps(documents, indent=2)
    for attempt in range(1, _MAX_PUBLISH_ATTEMPTS + 1):
        existing_etag = head_s3_object_etag(s3, output_bucket, rag_key)
        current_version = Helper.get_s3_object_version(source_bucket, source_key)
        if current_version != expected_source_version:
            log.warning(
                f"Skipping stale RAG_Output publish for s3://{source_bucket}/{source_key}: "
                f"started with {expected_source_version}, current is {current_version}"
            )
            return False
        try:
            write_s3_json(
                s3,
                output_bucket,
                rag_key,
                body,
                if_none_match=existing_etag is None,
                if_match=existing_etag,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code not in _CONFLICT_CODES:
                raise
            current_version = Helper.get_s3_object_version(source_bucket, source_key)
            if current_version != expected_source_version:
                log.warning(
                    f"Skipping stale RAG_Output publish after conflict for s3://{source_bucket}/{source_key}: "
                    f"started with {expected_source_version}, current is {current_version}"
                )
                return False
            if attempt == _MAX_PUBLISH_ATTEMPTS:
                raise
    return False


def process(source_key, namespace=None):

    ret_value = bda.invoke_bedrock_bda(source_key, namespace=namespace)
    if not ret_value:
        print(f"Value Returned by function invoke_bedrock_bda() is {ret_value}. Please check.")
        raise Exception(f"Value Returned by function invoke_bedrock_bda() is {ret_value}. Please check.")
    else:
        print(f"Value Returned by function invoke_bedrock_bda() is {ret_value}.")

    ret_value = parsed_text_data.invoke_parsed_text_data(source_key, namespace=namespace)
    if not ret_value:
        print(f"Value Returned by function invoke_parsed_text_data() is {ret_value}. Please check.")
        raise Exception(f"Value Returned by function invoke_parsed_text_data() is {ret_value}. Please check.")
    else:
        print(f"Value Returned by function invoke_parsed_text_data() is {ret_value}.")

    ret_value = parsed_bda_table.invoke_parsed_bda_data(source_key, namespace=namespace)
    if not ret_value:
        print(f"Value Returned by function invoke_parsed_bda_data() is {ret_value}. Please check.")
        raise Exception(f"Value Returned by function invoke_parsed_bda_data() is {ret_value}. Please check.")
    else:
        print(f"Value Returned by function invoke_parsed_bda_data() is {ret_value}.")

    ret_value = parsed_images.invoke_parsed_images_data(source_key, namespace=namespace)
    if not ret_value:
        print(f"Value Returned by function invoke_parsed_images_data() is {ret_value}. Please check.")
        raise Exception(f"Value Returned by function invoke_parsed_images_data() is {ret_value}. Please check.")
    else:
        print(f"Value Returned by function invoke_parsed_images_data() is {ret_value}.")

    return ret_value


def merge_outputs_to_rag_output(
    source_key,
    namespace=None,
    *,
    artifact_namespace=None,
    expected_source_version=None,
    input_bucket=None,
):

    bucket = Helper.get_property("output_bucket")
    read_namespace = namespace if artifact_namespace is None else artifact_namespace
    output_keys = [per_doc_output_key(source_key, kind, namespace=read_namespace) for kind in OUTPUT_FOLDERS]

    all_docs = []
    for output_key in output_keys:
        all_docs.extend(Helper.get_json_from_s3(bucket, output_key))
    documents = [
        {**doc, "metadata": {**(doc.get("metadata") or {}), "source_key": source_key}}
        for doc in all_docs
        if doc and doc.get("text")
    ]

    rag_key = rag_output_key(source_key, namespace=namespace)
    if expected_source_version is not None:
        source_bucket = input_bucket or Helper.get_property("input_bucket_name")
        published = _publish_rag_output_if_current(
            bucket,
            rag_key,
            documents,
            source_bucket,
            source_key,
            expected_source_version,
        )
        if not published:
            return None
    else:
        Helper.create_file(bucket, rag_key, documents)
    print(f"Wrote {len(documents)} RAG document(s) for {source_key} to s3://{bucket}/{rag_key}")
    return rag_key


def handler(event, context=None):
    namespace = event.get("requestKey") if isinstance(event, dict) else None
    rag_outputs = []
    stale_keys = []
    keys = list(dict.fromkeys(object_keys(event)))
    for key in keys:
        if not key.lower().endswith(SUPPORTED_DOCUMENT_EXTENSIONS):
            continue
        if namespace is None:
            input_bucket = Helper.get_property("input_bucket_name")
            source_version = Helper.get_s3_object_version(input_bucket, key)
            if source_version is None:
                log.warning(f"Skipping pre-processing for missing source s3://{input_bucket}/{key}")
                stale_keys.append(key)
                continue
            attempt_namespace = _new_attempt_namespace()
            try:
                process(key, namespace=attempt_namespace)
                rag_key = merge_outputs_to_rag_output(
                    key,
                    namespace=None,
                    artifact_namespace=attempt_namespace,
                    expected_source_version=source_version,
                    input_bucket=input_bucket,
                )
            finally:
                _cleanup_attempt_artifacts(attempt_namespace)
            if rag_key is None:
                stale_keys.append(key)
            else:
                rag_outputs.append(rag_key)
        else:
            process(key, namespace=namespace)
            rag_outputs.append(merge_outputs_to_rag_output(key, namespace=namespace))

    if not rag_outputs:
        if stale_keys:
            return {"status": "skipped", "stale": stale_keys}
        print(f"No PDF or DOCX objects in event. Skipping. Event={event}")
        return {"status": "skipped"}
    return {"status": "completed", "rag_output": rag_outputs}