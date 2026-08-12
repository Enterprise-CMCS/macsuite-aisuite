"""Red-phase tests for the request-scoped agent endpoint surface."""

import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from _stubs import install_offline_stubs
except ModuleNotFoundError:
    from tests._stubs import install_offline_stubs


install_offline_stubs()


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


pydantic_ai = types.ModuleType("pydantic_ai")
pydantic_ai.Agent = _FakeAgent
pydantic_ai.RunContext = _FakeRunContext
pydantic_ai_models = types.ModuleType("pydantic_ai.models")
pydantic_ai_bedrock = types.ModuleType("pydantic_ai.models.bedrock")
pydantic_ai_bedrock.BedrockConverseModel = MagicMock
sys.modules.setdefault("pydantic_ai", pydantic_ai)
sys.modules.setdefault("pydantic_ai.models", pydantic_ai_models)
sys.modules.setdefault("pydantic_ai.models.bedrock", pydantic_ai_bedrock)

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

try:
    from fastapi.testclient import TestClient
except (ImportError, ModuleNotFoundError):
    from starlette.testclient import TestClient

import search.database_searching.agents as agents_mod  # noqa: E402
from search.routes.endpoint import app  # noqa: E402


class AgentEndpointContractTests(unittest.TestCase):
    def setUp(self):
        self.agent_run = AsyncMock(
            return_value=SimpleNamespace(output="stub-answer")
        )
        self.run_patch = patch.object(
            agents_mod.search_agent,
            "run",
            self.agent_run,
            create=True,
        )
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)
        self.client = TestClient(app)

    def test_contracts_lists_only_public_contract_metadata(self):
        response = self.client.get("/contracts")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"contracts"})
        contracts = payload["contracts"]
        self.assertEqual(len(contracts), 5)
        for contract in contracts:
            self.assertEqual(set(contract), {"contract_id", "is_default"})
        self.assertEqual(
            [item["contract_id"] for item in contracts if item["is_default"]],
            ["tn_6756"],
        )
        for private_value in ("embeddings", "state_of_", "aisuite-dev-"):
            self.assertNotIn(private_value, response.text)

    def test_get_unknown_contract_returns_400_and_names_valid_id(self):
        response = self.client.get(
            "/agent",
            params={"query": "x", "contract_id": "zzz"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("tn_6756", response.text)

    def test_post_unknown_contract_returns_400(self):
        response = self.client.post(
            "/agent",
            json={"query": "x", "contract_id": "zzz"},
        )

        self.assertEqual(response.status_code, 400)

    def test_injection_shaped_contract_is_rejected_before_agent_run(self):
        response = self.client.get(
            "/agent",
            params={"query": "x", "contract_id": "tn_6756;drop"},
        )

        self.assertEqual(response.status_code, 400)
        self.agent_run.assert_not_awaited()

    def test_get_explicit_contract_uses_scoped_table_and_echoes_id(self):
        response = self.client.get(
            "/agent",
            params={"query": "hello", "contract_id": "wa_6369"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["contract_id"], "wa_6369")
        self.agent_run.assert_awaited_once()
        deps = self.agent_run.await_args.kwargs["deps"]
        self.assertEqual(
            deps.search_engine.table_name,
            "embeddings_wa_6369_ifc",
        )

    def test_get_omitted_contract_echoes_default_id(self):
        response = self.client.get("/agent", params={"query": "hello"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["contract_id"], "tn_6756")

    def test_agent_request_log_includes_resolved_contract_id(self):
        with patch("search.routes.endpoint.logger.info") as log_info:
            omitted = self.client.get("/agent", params={"query": "hello"})
            explicit = self.client.get(
                "/agent",
                params={"query": "hello", "contract_id": "wa_6369"},
            )

        self.assertEqual(omitted.status_code, 200)
        self.assertEqual(explicit.status_code, 200)
        request_events = [
            call
            for call in log_info.call_args_list
            if call.args and call.args[0] == "agent_request"
        ]
        self.assertGreaterEqual(len(request_events), 2)
        self.assertEqual(request_events[0].kwargs["contract_id"], "tn_6756")
        self.assertEqual(request_events[1].kwargs["contract_id"], "wa_6369")

    def test_post_omitted_contract_echoes_default_id(self):
        response = self.client.post("/agent", json={"query": "hello"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["contract_id"], "tn_6756")


if __name__ == "__main__":
    unittest.main()
