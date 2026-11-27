import json
import os
import re
from botocore.exceptions import ClientError

from common.utils.helper import Helper
from common.utils.logger import log


def _split_s3_key(key):
    if "/" not in key:
        return None, key
    return key.rsplit("/", 1)


def doc_name_for_key(key):
    _, basename = _split_s3_key(key)
    return os.path.splitext(basename)[0].replace(" ", "_")


OUTPUT_FOLDERS = {"text": "BDATextOutput", "table": "BDATableOutput", "image": "BDAImageOutput"}
RUN_ARTIFACTS_PREFIX = "runs/"
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
MAX_S3_KEY_BYTES = 1024
# Reserve headroom under doc_output_prefix for BDA's nested result/asset suffixes.
BDA_INTERNAL_KEY_HEADROOM_BYTES = 160


def _clean_namespace(namespace):
    if namespace is None or namespace == "":
        return None
    if not isinstance(namespace, str) or not _NAMESPACE_RE.fullmatch(namespace):
        raise ValueError(f"Invalid artifact namespace '{namespace}'.")
    return namespace


def output_root(key, namespace=None):
    ns = _clean_namespace(namespace)
    dir_part, _ = _split_s3_key(key)
    if dir_part is None:
        root = "_bdaoutput/"
    else:
        parts = dir_part.split("/")
        if parts[0]:
            root = "/".join([f"{parts[0]}_bdaoutput", *parts[1:]]) + "/"
        else:
            root = "/" + "/".join(["_bdaoutput", *parts[1:]]) + "/"
    return f"{RUN_ARTIFACTS_PREFIX}{ns}/{root}" if ns else root


def bda_output_prefix(key, namespace=None):
    return f"{output_root(key, namespace=namespace)}bdaoutput/"


def doc_output_prefix(key, namespace=None):
    return f"{bda_output_prefix(key, namespace=namespace)}{doc_name_for_key(key)}/"


def per_doc_output_key(key, kind, namespace=None):
    return f"{output_root(key, namespace=namespace)}{OUTPUT_FOLDERS[kind]}/{doc_name_for_key(key)}-preprocessed-{kind}.json"


RAG_OUTPUT_PREFIX = "RAG_Output/"


def rag_output_key(key, namespace=None):
    """state_of_TN/Contract/a.pdf -> RAG_Output/state_of_TN/Contract/a.json ; a.pdf -> RAG_Output/a.json"""
    ns = _clean_namespace(namespace)
    dir_part, _ = _split_s3_key(key)
    ns_prefix = f"{RUN_ARTIFACTS_PREFIX}{ns}/" if ns else ""
    folder_prefix = f"{dir_part}/" if dir_part is not None else ""
    return f"{RAG_OUTPUT_PREFIX}{ns_prefix}{folder_prefix}{doc_name_for_key(key)}.json"


def derived_artifact_key_errors(source_key, namespace=None):
    """Return error messages if derived S3 artifact keys could exceed S3's 1,024-byte UTF-8 limit."""
    errors = []
    max_bda_prefix = MAX_S3_KEY_BYTES - BDA_INTERNAL_KEY_HEADROOM_BYTES
    bda_prefix = doc_output_prefix(source_key, namespace=namespace)
    bda_prefix_bytes = len(bda_prefix.encode("utf-8"))
    if bda_prefix_bytes > max_bda_prefix:
        errors.append(
            f"derived BDA output prefix is {bda_prefix_bytes} UTF-8 bytes "
            f"(max {max_bda_prefix} to stay within S3's {MAX_S3_KEY_BYTES}-byte key limit)"
        )

    candidates = [
        ("RAG_Output key", rag_output_key(source_key, namespace=namespace)),
        *((f"{kind} output key", per_doc_output_key(source_key, kind, namespace=namespace)) for kind in OUTPUT_FOLDERS),
    ]
    for label, artifact_key in candidates:
        size = len(artifact_key.encode("utf-8"))
        if size > MAX_S3_KEY_BYTES:
            errors.append(f"derived {label} is {size} UTF-8 bytes (max {MAX_S3_KEY_BYTES})")
    return errors

class BDAResults:
    def __init__(self):

        pass


    def fetch_parsed_bda_results(self, bucket, output_prefix,awsPClientS3, calledFrom=""):
        log.info(f"*********Bda_Parsed_file_Access.fetch_parsed_bda_results start ******************** called with bucket={bucket}, output_prefix={output_prefix}")
        parsed_data = []
        paginator = awsPClientS3.get_paginator("list_objects_v2")
        log.debug(f"fetch_parsed_bda_results() paginator={paginator}")


        count = 0
        for page in paginator.paginate(Bucket=bucket, Prefix=output_prefix):
            log.debug(" Outer for loop")
            for obj in page.get("Contents", []):
                count= count + 1
                #print(" Inner for loop")
                key = obj.get("Key")
                #log.debug(f" Inner for loop Key ={key}")
                if not key:
                    continue
                if key.endswith("result.json"):
                    try:
                        log.debug(f"Looking for KEY which ends with result.json. Inner for loop Key ={key}")
                        resp = awsPClientS3.get_object(Bucket=bucket, Key=key)
                        data = json.load(resp["Body"])
                        parsed_data.append(data)
                    except ClientError as exClientError:
                        log.error("Error", error=f"Warning: Failed to download {key}: {exClientError}")
                        raise exClientError
                    except json.JSONDecodeError as exJSONDecodeError:
                        log.error("Error", error=f"Warning: invalid JSON in {key}: {exJSONDecodeError}")
                        raise exJSONDecodeError
                    except Exception as lclEx:
                        log.error("Error", error=f"Warning: unexpected error for {key}: {lclEx}")
                        raise lclEx
        log.info(f"fetch_parsed_bda_results() Returning parsed data. Length of parsed_data={len(parsed_data)}")
        #log.info(parsed_data[0])
        return parsed_data
