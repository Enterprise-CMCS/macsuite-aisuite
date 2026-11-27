from concurrent.futures import ThreadPoolExecutor
import json
from urllib.parse import unquote_plus

from botocore.exceptions import ClientError

_MISSING_S3_OBJECT_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


def object_keys(event):
    if not isinstance(event, dict):
        return []

    keys = []
    for record in event.get("Records", []):
        if "s3" in record:
            keys.append(unquote_plus(record["s3"]["object"]["key"]))

    for doc in event.get("documents", []):
        if isinstance(doc, dict) and isinstance(doc.get("s3"), dict):
            path = doc["s3"].get("path") or doc["s3"].get("key")
            if path:
                keys.append(path)

    if isinstance(event.get("s3"), dict):
        path = event["s3"].get("path") or event["s3"].get("key")
        if path:
            keys.append(path)

    for rag_key in event.get("rag_output", []):
        if isinstance(rag_key, str) and rag_key:
            keys.append(rag_key)

    if isinstance(event.get("key"), str) and event["key"]:
        keys.append(event["key"])

    return keys


def list_s3_keys(s3, bucket, prefix="", suffix=""):
    suffix = suffix.lower()
    paginator = s3.get_paginator("list_objects_v2")
    return [
        obj["Key"]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for obj in page.get("Contents", ())
        if not suffix or obj["Key"].lower().endswith(suffix)
    ]


def read_s3_bytes(s3, bucket, key):
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def read_s3_many(s3, bucket, keys, max_workers=16):
    if not keys:
        return []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(lambda key: read_s3_bytes(s3, bucket, key), keys))


def stream_s3_object(s3, bucket, key):
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"]
    except s3.exceptions.NoSuchKey:
        return None


def write_s3_json(s3, bucket, key, body, *, if_none_match=False):
    extra = {"IfNoneMatch": "*"} if if_none_match else {}
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json", **extra)


def s3_object_version(response):
    if not isinstance(response, dict):
        raise ValueError("S3 response metadata is required to determine object version.")
    version_id = response.get("VersionId")
    if isinstance(version_id, str) and version_id and version_id != "null":
        return f"version:{version_id}"
    etag = response.get("ETag")
    last_modified = response.get("LastModified")
    if isinstance(etag, str) and etag:
        if last_modified is not None:
            ts = last_modified.isoformat() if hasattr(last_modified, "isoformat") else str(last_modified)
            return f"etag:{etag}:{ts}"
        return f"etag:{etag}"
    raise ValueError(f"S3 response is missing VersionId and ETag: {sorted(response)}")


def read_s3_json_with_version(s3, bucket, key):
    response = s3.get_object(Bucket=bucket, Key=key)
    data = json.loads(response["Body"].read().decode("utf-8"))
    return data, s3_object_version(response)


def head_s3_object_version(s3, bucket, key):
    candidate = getattr(getattr(s3, "exceptions", None), "NoSuchKey", None)
    extra_missing = (
        (candidate,)
        if isinstance(candidate, type) and issubclass(candidate, BaseException)
        else ()
    )
    try:
        response = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in _MISSING_S3_OBJECT_CODES:
            return None
        raise
    except extra_missing:
        return None
    return s3_object_version(response)