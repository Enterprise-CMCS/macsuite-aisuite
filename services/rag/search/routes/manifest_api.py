import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import lru_cache
from typing import Annotated, Optional

import structlog
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import APIRouter, FastAPI, Path, Query, Request, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from common.utils.aws_files_access import stream_s3_object, write_s3_json
from common.utils.manifest import (
    REQUEST_KEY_PATTERN,
    SUPPORTED_DOCUMENT_EXTENSIONS,
    Manifest,
    ManifestAccepted,
    ManifestError,
    ManifestStatus,
    ReviewResults,
    manifest_embeddings_table_name,
    manifest_object_key,
    results_object_key,
)
from common.utils.helper import Helper
from common.utils.settings import aws_client
from data_preprocessing.bedrock.bda_results import (
    derived_artifact_key_errors,
    doc_output_prefix,
    rag_output_key,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/manifest", tags=["Manifest"])

_HEAD_OBJECT_WORKERS = 16
_MAX_REPORTED_ERRORS = 50
_MAX_EMBEDDED_COUNT_PLACEHOLDER = 999_999_999
DEFAULT_MAX_MANIFEST_BODY_BYTES = 5 * 1024 * 1024
MAX_WORKFLOW_INPUT_BYTES = 256 * 1024
# Leave headroom below API Gateway's 10 MiB buffered REST response ceiling.
MAX_RESULTS_RESPONSE_BYTES = 9 * 1024 * 1024

_RUN_STATUS = {"RUNNING": "PROCESSING", "PENDING_REDRIVE": "PROCESSING", "SUCCEEDED": "COMPLETED"}
_TERMINAL_DEFAULT_ERROR = {"TIMED_OUT": "States.Timeout", "ABORTED": "ExecutionAborted"}
_ERROR_RESPONSES = {
    code: {"model": ManifestError}
    for code in (
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_413_CONTENT_TOO_LARGE,
        status.HTTP_502_BAD_GATEWAY,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    )
}
RequestKey = Annotated[str, Path(pattern=REQUEST_KEY_PATTERN)]


def _max_manifest_body_bytes() -> int:
    raw = os.environ.get("MAX_MANIFEST_BODY_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_MANIFEST_BODY_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_MANIFEST_BODY_BYTES
    return value if value > 0 else DEFAULT_MAX_MANIFEST_BODY_BYTES


def _payload_too_large_response(limit: int) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        content=ManifestError(
            requestKey=None,
            status="PAYLOAD_TOO_LARGE",
            errors=[f"Manifest request body exceeds the {limit} byte limit."],
        ).model_dump(),
    )


class _ManifestBodyLimitMiddleware:
    """Enforce an explicit payload ceiling on POST /manifest.

    WAF exempts POST /v1/manifest from SizeRestrictions_BODY so large manifests
    are not blocked at 8 KiB; this middleware enforces the application limit
    before Pydantic parses the request body.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        if scope.get("path", "").rstrip("/") != router.prefix:
            await self.app(scope, receive, send)
            return

        limit = _max_manifest_body_bytes()
        content_length = None
        for name, value in scope.get("headers", ()):
            if name.lower() == b"content-length":
                try:
                    content_length = int(value.decode("ascii").strip())
                except (UnicodeDecodeError, ValueError):
                    content_length = None
                break
        if content_length is not None and content_length > limit:
            response = _payload_too_large_response(limit)
            await response(scope, receive, send)
            return

        chunks = []
        received = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                continue
            chunk = message.get("body", b"")
            if chunk:
                received += len(chunk)
                if received > limit:
                    response = _payload_too_large_response(limit)
                    await response(scope, receive, send)
                    return
                chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        body_sent = False

        async def replay_receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


class ManifestApiError(Exception):
    def __init__(self, status_code, request_key, status_label, errors):
        super().__init__("; ".join(errors))
        self.status_code = status_code
        self.body = ManifestError(requestKey=request_key, status=status_label, errors=list(errors))


@lru_cache(maxsize=None)
def _client(service):
    return aws_client(service, config=Config(max_pool_connections=_HEAD_OBJECT_WORKERS * 2))


@contextmanager
def _upstream(request_key, action):
    try:
        yield
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error("manifest_upstream_error", request_key=request_key, action=action, error=str(exc))
        raise ManifestApiError(
            status.HTTP_502_BAD_GATEWAY, request_key, "UPSTREAM_ERROR", [f"Could not {action} ({code})."]
        ) from exc


def _run_bucket():
    return Helper.get_property("temp_bucket_name")


def _state_machine_arn(request_key):
    arn = os.environ.get("INGEST_STATE_MACHINE_ARN", "").strip()
    if not arn:
        raise ManifestApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            request_key,
            "NOT_CONFIGURED",
            ["INGEST_STATE_MACHINE_ARN is not configured."],
        )
    return arn


def _execution_arn(request_key):

    return f"{_state_machine_arn(request_key).replace(':stateMachine:', ':execution:', 1)}:{request_key}"


def _describe_execution(request_key):
    sfn = _client("stepfunctions")
    with _upstream(request_key, "read the run status"):
        try:
            return sfn.describe_execution(executionArn=_execution_arn(request_key))
        except sfn.exceptions.ExecutionDoesNotExist:
            return None


def _run_status(execution):
    return _RUN_STATUS.get(execution["status"], "FAILED")


def _document_errors(manifest):
    documents_bucket = Helper.get_property("input_bucket_name")
    s3 = _client("s3")

    def check(document):
        location = f"Document '{document.documentId}' (s3://{document.s3.bucket}/{document.s3.path})"
        if not document.s3.path.lower().endswith(SUPPORTED_DOCUMENT_EXTENSIONS):
            return f"{location} must be one of: {', '.join(SUPPORTED_DOCUMENT_EXTENSIONS)}."
        if document.s3.bucket != documents_bucket:
            return f"{location} must be uploaded to bucket '{documents_bucket}'."
        try:
            s3.head_object(Bucket=document.s3.bucket, Key=document.s3.path)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            return f"{location} was not found or is not readable ({code})."
        return None

    with ThreadPoolExecutor(max_workers=_HEAD_OBJECT_WORKERS) as pool:
        errors = [error for error in pool.map(check, manifest.documents) if error]

    # a.pdf and a.docx in one folder pre-process to the same RAG_Output / BDA output prefix.
    reported_collisions = set()
    for key_fn in (rag_output_key, doc_output_prefix):
        by_output = {}
        for document in manifest.documents:
            by_output.setdefault(
                key_fn(document.s3.path, namespace=manifest.requestKey), []
            ).append(document.documentId)
        for ids in by_output.values():
            if len(ids) > 1:
                pair = tuple(ids)
                if pair not in reported_collisions:
                    reported_collisions.add(pair)
                    errors.append(
                        f"Documents {', '.join(ids)} share a folder and file name, so their pre-processing output would collide."
                    )
    return errors


def _workflow_input(manifest):
    # The requirements stay in S3: thousands of them would exceed the 256 KiB state payload limit.
    return {
        "requestKey": manifest.requestKey,
        "reviewType": manifest.reviewType,
        "state": manifest.state,
        "requirementCount": len(manifest.legalRequirements),
        "documents": [document.model_dump() for document in manifest.documents],
    }


def _serialized_workflow_input(manifest):
    return json.dumps(_workflow_input(manifest))


def _workflow_limit_errors(manifest, workflow_input_json):
    errors = []
    request_key = manifest.requestKey

    for document in manifest.documents:
        key_errors = derived_artifact_key_errors(document.s3.path, namespace=request_key)
        if key_errors:
            location = f"Document '{document.documentId}' (s3://{document.s3.bucket}/{document.s3.path})"
            errors.append(f"{location}: {'; '.join(key_errors)}.")

    workflow_input_bytes = len(workflow_input_json.encode("utf-8"))
    if workflow_input_bytes > MAX_WORKFLOW_INPUT_BYTES:
        errors.append(
            f"Serialized workflow input is {workflow_input_bytes} bytes (UTF-8), "
            f"exceeding the {MAX_WORKFLOW_INPUT_BYTES} byte Step Functions limit; "
            "reduce the number of documents or shorten their S3 paths."
        )

    rag_keys = [rag_output_key(document.s3.path, namespace=request_key) for document in manifest.documents]
    preprocess_result = [{"status": "completed", "rag_output": [key]} for key in rag_keys]
    preprocess_bytes = len(json.dumps(preprocess_result, separators=(",", ":")).encode("utf-8"))
    if preprocess_bytes > MAX_WORKFLOW_INPUT_BYTES:
        errors.append(
            f"Projected PreProcessDocuments Map result is {preprocess_bytes} bytes (UTF-8), "
            f"exceeding the {MAX_WORKFLOW_INPUT_BYTES} byte Step Functions limit; "
            "reduce the number of documents or shorten their S3 paths."
        )

    table_name = manifest_embeddings_table_name(request_key, manifest.state)
    embed_result = [
        {"key": key, "tableName": table_name, "embedded": _MAX_EMBEDDED_COUNT_PLACEHOLDER}
        for key in rag_keys
    ]
    embed_bytes = len(json.dumps(embed_result, separators=(",", ":")).encode("utf-8"))
    if embed_bytes > MAX_WORKFLOW_INPUT_BYTES:
        errors.append(
            f"Projected EmbedDocuments Map result is {embed_bytes} bytes (UTF-8), "
            f"exceeding the {MAX_WORKFLOW_INPUT_BYTES} byte Step Functions limit; "
            "reduce the number of documents or shorten their S3 paths."
        )

    return errors


def _accepted(manifest, execution_arn, message):
    return ManifestAccepted(
        requestKey=manifest.requestKey,
        executionArn=execution_arn,
        documentCount=len(manifest.documents),
        requirementCount=len(manifest.legalRequirements),
        message=message,
    )


def _already_processed_error(request_key, execution):
    return ManifestApiError(
        status.HTTP_409_CONFLICT,
        request_key,
        _run_status(execution),
        [f"requestKey '{request_key}' was already processed ({execution['status']}); resubmit with a new requestKey."],
    )


def _reserve_manifest(manifest):
    request_key = manifest.requestKey
    s3 = _client("s3")
    bucket = _run_bucket()
    key = manifest_object_key(request_key)

    try:
        write_s3_json(s3, bucket, key, manifest.model_dump_json(), if_none_match=True)
        return
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        if code not in ("PreconditionFailed", "ConditionalRequestConflict"):
            logger.error("manifest_upstream_error", request_key=request_key, action="store the manifest", error=str(exc))
            raise ManifestApiError(
                status.HTTP_502_BAD_GATEWAY, request_key, "UPSTREAM_ERROR", [f"Could not store the manifest ({code})."]
            ) from exc

    with _upstream(request_key, "read the existing manifest"):
        existing = stream_s3_object(s3, bucket, key)
        raw = _read_s3_body(existing) if existing is not None else None

    if raw is None:
        raise ManifestApiError(
            status.HTTP_409_CONFLICT,
            request_key,
            "CONFLICT",
            [f"requestKey '{request_key}' is currently being reserved; retry the request."],
        )

    try:
        same_manifest = Manifest.model_validate_json(raw) == manifest
    except ValueError:
        same_manifest = False

    if not same_manifest:
        raise ManifestApiError(
            status.HTTP_409_CONFLICT,
            request_key,
            "CONFLICT",
            [f"requestKey '{request_key}' is already reserved with different manifest content."],
        )


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=ManifestAccepted, responses=_ERROR_RESPONSES)
def submit_manifest(manifest: Manifest):
    """Validate MuleSoft's manifest and start pre-processing, embeddings and requirement review."""
    request_key = manifest.requestKey
    logger.info(
        "manifest_received",
        request_key=request_key,
        state=manifest.state,
        review_type=manifest.reviewType,
        document_count=len(manifest.documents),
        requirement_count=len(manifest.legalRequirements),
    )

    state_machine_arn = _state_machine_arn(request_key)

    workflow_input_json = _serialized_workflow_input(manifest)
    limit_errors = _workflow_limit_errors(manifest, workflow_input_json)
    if limit_errors:
        logger.warning("manifest_rejected", request_key=request_key, errors=limit_errors)
        raise ManifestApiError(status.HTTP_400_BAD_REQUEST, request_key, "FAILED_VALIDATION", limit_errors)

    errors = _document_errors(manifest)
    if errors:
        logger.warning("manifest_rejected", request_key=request_key, errors=errors)
        raise ManifestApiError(status.HTTP_400_BAD_REQUEST, request_key, "FAILED_VALIDATION", errors)

    execution = _describe_execution(request_key)
    if execution is not None and _run_status(execution) != "PROCESSING":
        # Step Functions keeps closed execution names for 90 days, so a key cannot be rerun.
        raise _already_processed_error(request_key, execution)

    _reserve_manifest(manifest)

    if execution is not None:
        return _accepted(manifest, execution["executionArn"], "Manifest is already processing.")

    sfn = _client("stepfunctions")
    with _upstream(request_key, "start the workflow"):
        try:
            response = sfn.start_execution(
                stateMachineArn=state_machine_arn,
                name=request_key,
                input=workflow_input_json,
            )
        except sfn.exceptions.ExecutionAlreadyExists:
            existing = _describe_execution(request_key)
            if existing is not None and _run_status(existing) != "PROCESSING":
                raise _already_processed_error(request_key, existing)
            return _accepted(manifest, _execution_arn(request_key), "Manifest is already processing.")

    logger.info("manifest_accepted", request_key=request_key, execution_arn=response["executionArn"])
    return _accepted(manifest, response["executionArn"], "Manifest validated; processing started.")


@router.get("/{request_key}", response_model=ManifestStatus, responses=_ERROR_RESPONSES)
def get_manifest_status(request_key: RequestKey):
    execution = _describe_execution(request_key)
    if execution is None:
        raise ManifestApiError(
            status.HTTP_404_NOT_FOUND, request_key, "NOT_FOUND", [f"No run exists for requestKey '{request_key}'."]
        )

    run_status = _run_status(execution)
    started, stopped = execution.get("startDate"), execution.get("stopDate")
    return ManifestStatus(
        requestKey=request_key,
        status=run_status,
        startedAt=started.isoformat() if started else None,
        stoppedAt=stopped.isoformat() if stopped else None,
        resultsPath=f"/manifest/{request_key}/results" if run_status == "COMPLETED" else None,
        error=execution.get("error") or _TERMINAL_DEFAULT_ERROR.get(execution.get("status")),
        cause=execution.get("cause"),
    )


def _read_s3_body(body) -> bytes:
    try:
        return body.read()
    finally:
        body.close()


def _paginate_results(
    request_key: str,
    raw: bytes,
    offset: Optional[int],
    limit: Optional[int],
) -> Response:
    if offset is None and limit is None:
        if len(raw) > MAX_RESULTS_RESPONSE_BYTES:
            raise ManifestApiError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                request_key,
                "PAYLOAD_TOO_LARGE",
                [
                    f"Results payload is {len(raw)} bytes, exceeding the {MAX_RESULTS_RESPONSE_BYTES} byte "
                    "response limit; paginate with 'offset' and 'limit' query parameters."
                ],
            )
        return Response(content=raw, media_type="application/json")

    payload = json.loads(raw)
    items = payload.get("legalRequirements", [])
    start = offset or 0
    end = len(items) if limit is None else min(start + limit, len(items))
    page_items = items[start:end]

    def serialize(subset):
        return json.dumps({**payload, "legalRequirements": subset}).encode("utf-8")

    page_bytes = serialize(page_items)
    if len(page_bytes) > MAX_RESULTS_RESPONSE_BYTES:
        if len(page_items) <= 1:
            raise ManifestApiError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                request_key,
                "PAYLOAD_TOO_LARGE",
                [f"Results page at offset {start} exceeds the {MAX_RESULTS_RESPONSE_BYTES} byte response limit."],
            )
        lo, hi = 1, len(page_items) - 1
        best_count = 0
        best_bytes = b""
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = serialize(page_items[:mid])
            if len(candidate) <= MAX_RESULTS_RESPONSE_BYTES:
                best_count = mid
                best_bytes = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        if best_count == 0:
            raise ManifestApiError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                request_key,
                "PAYLOAD_TOO_LARGE",
                [f"Results page at offset {start} exceeds the {MAX_RESULTS_RESPONSE_BYTES} byte response limit."],
            )
        page_items = page_items[:best_count]
        page_bytes = best_bytes

    headers = {
        "X-Total-Count": str(len(items)),
        "X-Offset": str(start),
        "X-Limit": str(len(page_items)),
    }
    next_offset = start + len(page_items)
    if next_offset < len(items):
        headers["X-Next-Offset"] = str(next_offset)
    return Response(content=page_bytes, media_type="application/json", headers=headers)


@router.get("/{request_key}/results", response_model=ReviewResults, responses=_ERROR_RESPONSES)
def get_manifest_results(
    request_key: RequestKey,
    offset: Annotated[Optional[int], Query(ge=0)] = None,
    limit: Annotated[Optional[int], Query(ge=1)] = None,
):
    with _upstream(request_key, "read the results"):
        body = stream_s3_object(_client("s3"), _run_bucket(), results_object_key(request_key))
        raw = _read_s3_body(body) if body is not None else None
    if raw is not None:
        return _paginate_results(request_key, raw, offset, limit)

    execution = _describe_execution(request_key)
    if execution is not None and _run_status(execution) == "PROCESSING":
        raise ManifestApiError(
            status.HTTP_409_CONFLICT,
            request_key,
            "PROCESSING",
            ["Results are not ready yet; fetch them after the SQS completion notification."],
        )
    raise ManifestApiError(
        status.HTTP_404_NOT_FOUND,
        request_key,
        _run_status(execution) if execution is not None else "NOT_FOUND",
        [f"No results exist for requestKey '{request_key}'."],
    )


def _validation_message(error):
    path = ""
    for part in error.get("loc", ())[1:]: 
        path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else str(part))
    return f"{path}: {error['msg']}" if path else error["msg"]


async def _manifest_api_error(request: Request, exc: ManifestApiError):
    return JSONResponse(status_code=exc.status_code, content=exc.body.model_dump())


async def _validation_error(request: Request, exc: RequestValidationError):
    if not request.url.path.startswith(router.prefix):
        return await request_validation_exception_handler(request, exc)

    messages = [_validation_message(error) for error in exc.errors()]
    if len(messages) > _MAX_REPORTED_ERRORS:
        hidden = len(messages) - _MAX_REPORTED_ERRORS
        messages = [*messages[:_MAX_REPORTED_ERRORS], f"... and {hidden} more error(s)."]
    body = exc.body if isinstance(exc.body, dict) else {}
    request_key = body.get("requestKey") or request.path_params.get("request_key")
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=ManifestError(
            requestKey=request_key if isinstance(request_key, str) else None,
            status="FAILED_VALIDATION",
            errors=messages,
        ).model_dump(),
    )


def install(app: FastAPI):
    app.add_middleware(_ManifestBodyLimitMiddleware)
    app.include_router(router)
    app.add_exception_handler(ManifestApiError, _manifest_api_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
