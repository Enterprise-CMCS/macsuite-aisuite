import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_RAG_ROOT = Path(__file__).resolve().parents[1]
if str(_RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(_RAG_ROOT))

from search.routes import security  # noqa: E402


try:
    import fastapi  # noqa: F401

    _FASTAPI_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    _FASTAPI_AVAILABLE = False

_APP_AVAILABLE = False
_APP_SKIP_REASON = "FastAPI is not installed"

class _FakeAgent:
    def __init__(self, *args, **kwargs):
        self.tools = {}

    def tool(self, fn=None, **kwargs):
        def register(func):
            self.tools[func.__name__] = func
            return func

        return register if fn is None else register(fn)


class _FakeRunContext:
    def __class_getitem__(cls, item):
        return cls


if _FASTAPI_AVAILABLE:
    try:
        try:
            from _stubs import install_offline_stubs
        except ModuleNotFoundError:
            from tests._stubs import install_offline_stubs

        install_offline_stubs()

        pydantic_ai = types.ModuleType("pydantic_ai")
        pydantic_ai.Agent = _FakeAgent
        pydantic_ai.RunContext = _FakeRunContext
        pydantic_ai_models = types.ModuleType("pydantic_ai.models")
        pydantic_ai_bedrock = types.ModuleType("pydantic_ai.models.bedrock")
        pydantic_ai_bedrock.BedrockConverseModel = MagicMock
        sys.modules.setdefault("pydantic_ai", pydantic_ai)
        sys.modules.setdefault("pydantic_ai.models", pydantic_ai_models)
        sys.modules.setdefault("pydantic_ai.models.bedrock", pydantic_ai_bedrock)

        from fastapi.testclient import TestClient
        from search.routes.endpoint import app

        _APP_AVAILABLE = True
        _APP_SKIP_REASON = ""
    except Exception as exc:
        _APP_SKIP_REASON = f"endpoint dependencies are unavailable: {exc}"


class SecurityHelperTests(unittest.TestCase):
    def setUp(self):
        security._resolved_api_key = None

    def tearDown(self):
        security._resolved_api_key = None

    def test_parse_allowed_origins_handles_empty_values(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertEqual(security.parse_allowed_origins(value), [])

    def test_parse_allowed_origins_trims_comma_separated_values(self):
        self.assertEqual(
            security.parse_allowed_origins(
                " https://one.example ,https://two.example/ ,  "
            ),
            ["https://one.example", "https://two.example/"],
        )

    def test_parse_allowed_origins_never_returns_wildcard(self):
        origins = security.parse_allowed_origins(
            "*, https://one.example, *"
        )
        self.assertEqual(origins, ["https://one.example"])
        self.assertNotIn("*", origins)

    def test_only_health_path_is_exempt(self):
        self.assertTrue(security.is_exempt_path("/health"))
        for path in ("/", "/agent", "/query", "/docs", "/redoc", "/openapi.json"):
            with self.subTest(path=path):
                self.assertFalse(security.is_exempt_path(path))

    def test_keys_match_accepts_only_the_correct_nonempty_key(self):
        self.assertTrue(security.keys_match("correct", "correct"))
        for provided in ("wrong", "", None):
            with self.subTest(provided=provided):
                self.assertFalse(security.keys_match(provided, "correct"))

    def test_resolve_api_key_requires_secret_arn(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(security.ApiKeyUnresolvedError):
                security.resolve_api_key()

    def test_check_request_allows_health_without_key(self):
        self.assertEqual(security.check_request("/health", None), (None, ""))

    def test_check_request_rejects_missing_key_on_protected_path(self):
        status_code, _ = security.check_request("/agent", None)
        self.assertEqual(status_code, 401)

    def test_check_request_rejects_wrong_key(self):
        with patch.object(security, "resolve_api_key", return_value="correct"):
            status_code, _ = security.check_request("/agent", "wrong")
        self.assertEqual(status_code, 401)

    def test_check_request_accepts_correct_key(self):
        with patch.object(security, "resolve_api_key", return_value="correct"):
            self.assertEqual(
                security.check_request("/agent", "correct"),
                (None, ""),
            )

    def test_check_request_returns_503_when_secret_is_unresolved(self):
        with patch.object(
            security,
            "resolve_api_key",
            side_effect=security.ApiKeyUnresolvedError,
        ):
            status_code, _ = security.check_request("/agent", "provided")
        self.assertEqual(status_code, 503)

    def test_build_cors_kwargs_disables_credentials_without_origins(self):
        with patch.dict(os.environ, {"API_ALLOWED_ORIGINS": ""}):
            kwargs = security.build_cors_kwargs()
        self.assertEqual(kwargs["allow_origins"], [])
        self.assertFalse(kwargs["allow_credentials"])

    def test_build_cors_kwargs_enables_credentials_for_explicit_origins(self):
        with patch.dict(
            os.environ,
            {"API_ALLOWED_ORIGINS": "*, https://app.example"},
        ):
            kwargs = security.build_cors_kwargs()
        self.assertEqual(kwargs["allow_origins"], ["https://app.example"])
        self.assertTrue(kwargs["allow_credentials"])
        self.assertNotIn("*", kwargs["allow_origins"])


@unittest.skipUnless(
    _FASTAPI_AVAILABLE and _APP_AVAILABLE,
    _APP_SKIP_REASON,
)
class ApiSecurityIntegrationTests(unittest.TestCase):
    def setUp(self):
        security._resolved_api_key = None
        self.client = TestClient(app)

    def tearDown(self):
        security._resolved_api_key = None

    def _request(self, method, path, body=None, headers=None):
        kwargs = {}
        if body is not None:
            kwargs["json"] = body
        if headers is not None:
            kwargs["headers"] = headers
        return getattr(self.client, method)(path, **kwargs)

    def test_health_is_public(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)

    def test_protected_routes_reject_missing_key(self):
        requests = (
            ("get", "/", None),
            ("get", "/agent", None),
            ("post", "/agent", {}),
            ("get", "/query", None),
            ("post", "/query", {}),
            ("get", "/docs", None),
            ("get", "/openapi.json", None),
        )
        for method, path, body in requests:
            with self.subTest(method=method, path=path):
                response = self._request(method, path, body)
                self.assertEqual(response.status_code, 401)

    def test_agent_rejects_incorrect_key_for_get_and_post(self):
        with patch.object(security, "resolve_api_key", return_value="correct"):
            for method, body in (("get", None), ("post", {})):
                with self.subTest(method=method):
                    response = self._request(
                        method,
                        "/agent",
                        body,
                        {"x-api-key": "wrong"},
                    )
                    self.assertEqual(response.status_code, 401)

    def test_agent_accepts_correct_key_for_get_and_post(self):
        with patch.object(security, "resolve_api_key", return_value="correct"):
            for method, body in (("get", None), ("post", {})):
                with self.subTest(method=method):
                    response = self._request(
                        method,
                        "/agent",
                        body,
                        {"x-api-key": "correct"},
                    )
                    self.assertNotIn(response.status_code, (401, 403))

    def test_unresolved_secret_returns_503_for_protected_routes_only(self):
        with patch.object(
            security,
            "resolve_api_key",
            side_effect=security.ApiKeyUnresolvedError,
        ):
            for method, path, body in (
                ("get", "/", None),
                ("get", "/agent", None),
                ("post", "/agent", {}),
                ("get", "/query", None),
                ("post", "/query", {}),
                ("get", "/docs", None),
                ("get", "/openapi.json", None),
            ):
                with self.subTest(method=method, path=path):
                    response = self._request(
                        method,
                        path,
                        body,
                        {"x-api-key": "provided"},
                    )
                    self.assertEqual(response.status_code, 503)
                    self.assertNotEqual(response.status_code, 200)

            self.assertEqual(self.client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
