from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote_plus


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
               




