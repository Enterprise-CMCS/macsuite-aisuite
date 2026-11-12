import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import lru_cache
from typing import Annotated

import structlog
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import APIRouter, FastAPI, Path, Request, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from common.utils.manifest import (
    REQUEST_KEY_PATTERN,
    SUPPORTED_DOCUMENT_EXTENSIONS,
    Manifest,
    ManifestAccepted,
    ManifestError,
    ManifestStatus,
    ReviewResults,
    manifest_object_key,
    results_object_key,
    results_path,
)
from common.utils.helper import Helper
from common.utils.settings import aws_client
from data_preprocessing.bedrock.bda_results import rag_output_key

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/manifest", tags=["Manifest"])

_HEAD_OBJECT_WORKERS = 16
_MAX_REPORTED_ERRORS = 50

_RUN_STATUS = {"RUNNING": "PROCESSING", "PENDING_REDRIVE": "PROCESSING", "SUCCEEDED": "COMPLETED"}
_ERROR_RESPONSES = {
    code: {"model": ManifestError}
    for code in (
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_502_BAD_GATEWAY,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    )
}
RequestKey = Annotated[str, Path(pattern=REQUEST_KEY_PATTERN)]


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

    # a.pdf and a.docx in one folder pre-process to the same RAG_Output file.
    by_output = {}
    for document in manifest.documents:
        by_output.setdefault(rag_output_key(document.s3.path), []).append(document.documentId)
    errors.extend(
        f"Documents {', '.join(ids)} share a folder and file name, so their pre-processing output would collide."
        for ids in by_output.values()
        if len(ids) > 1
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


def _accepted(manifest, execution_arn, message):
    return ManifestAccepted(
        requestKey=manifest.requestKey,
        executionArn=execution_arn,
        documentCount=len(manifest.documents),
        requirementCount=len(manifest.legalRequirements),
        message=message,
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

    errors = _document_errors(manifest)
    if errors:
        logger.warning("manifest_rejected", request_key=request_key, errors=errors)
        raise ManifestApiError(status.HTTP_400_BAD_REQUEST, request_key, "FAILED_VALIDATION", errors)

    execution = _describe_execution(request_key)
    if execution is not None:
        if _run_status(execution) == "PROCESSING":
            return _accepted(manifest, execution["executionArn"], "Manifest is already processing.")
        # Step Functions keeps closed execution names for 90 days, so a key cannot be rerun.
        raise ManifestApiError(
            status.HTTP_409_CONFLICT,
            request_key,
            _run_status(execution),
            [f"requestKey '{request_key}' was already processed ({execution['status']}); resubmit with a new requestKey."],
        )

    with _upstream(request_key, "store the manifest"):
        _client("s3").put_object(
            Bucket=_run_bucket(),
            Key=manifest_object_key(request_key),
            Body=manifest.model_dump_json(),
            ContentType="application/json",
        )

    sfn = _client("stepfunctions")
    with _upstream(request_key, "start the workflow"):
        try:
            response = sfn.start_execution(
                stateMachineArn=_state_machine_arn(request_key),
                name=request_key,
                input=json.dumps(_workflow_input(manifest)),
            )
        except sfn.exceptions.ExecutionAlreadyExists:
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
        resultsPath=results_path(request_key) if run_status == "COMPLETED" else None,
        error=execution.get("error"),
        cause=execution.get("cause"),
    )


@router.get("/{request_key}/results", response_model=ReviewResults, responses=_ERROR_RESPONSES)
def get_manifest_results(request_key: RequestKey):
    s3 = _client("s3")
    with _upstream(request_key, "read the results"):
        try:
            body = s3.get_object(Bucket=_run_bucket(), Key=results_object_key(request_key))["Body"]
        except s3.exceptions.NoSuchKey:
            body = None
    if body is not None:
        return StreamingResponse(body.iter_chunks(), media_type="application/json")

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
    app.include_router(router)
    app.add_exception_handler(ManifestApiError, _manifest_api_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
