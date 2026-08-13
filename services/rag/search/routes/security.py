"""Authentication and CORS helpers for the search API."""

import hmac
import json
import os
import threading


class ApiKeyUnresolvedError(RuntimeError):
    """Raised when the configured API key cannot be resolved."""


_resolved_api_key: str | None = None
_resolution_lock = threading.Lock()


def parse_allowed_origins(value: str | None) -> list[str]:
    """Parse configured CORS origins, excluding empty and wildcard entries."""
    if not value:
        return []
    return [
        origin
        for entry in value.split(",")
        if (origin := entry.strip()) and origin != "*"
    ]


def is_exempt_path(path: str) -> bool:
    """Return whether a request path is exempt from API-key authentication."""
    return path.rstrip("/") == "/health"


def keys_match(provided_key: str | None, expected_key: str | None) -> bool:
    """Compare non-empty API keys without leaking comparison timing."""
    if not provided_key or not expected_key:
        return False
    try:
        return hmac.compare_digest(provided_key, expected_key)
    except (TypeError, ValueError):
        return False


def resolve_api_key() -> str:
    """Resolve and cache the API key stored in AWS Secrets Manager."""
    global _resolved_api_key

    if _resolved_api_key is not None:
        return _resolved_api_key

    with _resolution_lock:
        if _resolved_api_key is not None:
            return _resolved_api_key

        secret_arn = os.environ.get("API_KEY_SECRET_ARN")
        if not secret_arn:
            raise ApiKeyUnresolvedError("API_KEY_SECRET_ARN is not configured")

        try:
            import boto3

            response = boto3.client("secretsmanager").get_secret_value(
                SecretId=secret_arn
            )
            secret = json.loads(response["SecretString"])
            api_key = secret["apiKey"]
        except Exception as exc:
            raise ApiKeyUnresolvedError("Unable to resolve the API key") from exc

        if not isinstance(api_key, str) or not api_key.strip():
            raise ApiKeyUnresolvedError(
                "Resolved secret does not contain a non-empty apiKey"
            )

        _resolved_api_key = api_key
        return api_key


def build_cors_kwargs() -> dict[str, object]:
    """Build keyword arguments suitable for FastAPI's CORSMiddleware."""
    origins = parse_allowed_origins(os.environ.get("API_ALLOWED_ORIGINS"))
    return {
        "allow_origins": origins,
        "allow_credentials": bool(origins),
        "allow_methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "x-api-key"],
    }


def check_request(path: str, header_value: str | None) -> tuple[int | None, str]:
    """Check whether a request path and API-key value should be accepted."""
    if is_exempt_path(path):
        return None, ""
    if not header_value:
        return 401, "Missing API key"

    try:
        expected_key = resolve_api_key()
    except ApiKeyUnresolvedError:
        return 503, "API key is unavailable"

    if not keys_match(header_value, expected_key):
        return 401, "Invalid API key"
    return None, ""
