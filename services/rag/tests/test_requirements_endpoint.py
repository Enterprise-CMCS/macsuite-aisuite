"""Red-phase tests for the batch requirements endpoint."""

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


def _success_verdict(item_id, requirement, recommendation="MET"):
    return {
        "id": item_id,
        "success": True,
        "error": None,
        "Requirement": requirement,
        "Recommendation": recommendation,
        "Response": f"Evidence for {requirement}",
        "Source": "Core: 10",
        "Page": "10",
    }


class RequirementsEndpointContractTests(unittest.TestCase):
    def setUp(self):
        self.grade_requirements = AsyncMock()
        self.grader_patch = patch(
            "search.requirements.verdicts.grade_requirements",
            new=self.grade_requirements,
        )
        self.grader_patch.start()
        self.addCleanup(self.grader_patch.stop)
        self.client = TestClient(app)

    def test_post_requirements_returns_ordered_verdicts(self):
        items = [
            {"id": "row-12", "text": "Provide statewide service."},
            {"id": "row-13", "text": "Maintain an incident log."},
        ]
        self.grade_requirements.return_value = [
            _success_verdict(item["id"], item["text"]) for item in items
        ]

        response = self.client.post(
            "/requirements",
            json={"requirements": items, "retry_unclear": True},
        )

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(len(results), 2)
        self.assertEqual([result["id"] for result in results], ["row-12", "row-13"])
        required_keys = {
            "id",
            "success",
            "Requirement",
            "Recommendation",
            "Response",
            "Source",
            "Page",
        }
        for result in results:
            self.assertTrue(required_keys.issubset(result))
        self.grade_requirements.assert_awaited_once()

    def test_empty_requirements_returns_4xx_without_grading(self):
        response = self.client.post("/requirements", json={"requirements": []})

        self.assertLess(response.status_code, 500)
        self.assertGreaterEqual(response.status_code, 400)
        self.grade_requirements.assert_not_awaited()

    def test_batch_over_default_limit_returns_4xx_without_grading(self):
        items = [
            {"id": f"row-{index}", "text": f"Requirement {index}"}
            for index in range(26)
        ]

        response = self.client.post("/requirements", json={"requirements": items})

        self.assertLess(response.status_code, 500)
        self.assertGreaterEqual(response.status_code, 400)
        self.grade_requirements.assert_not_awaited()

    def test_requirement_text_over_limit_returns_4xx_without_grading(self):
        response = self.client.post(
            "/requirements",
            json={"requirements": [{"id": "row-12", "text": "x" * 2001}]},
        )

        self.assertLess(response.status_code, 500)
        self.assertGreaterEqual(response.status_code, 400)
        self.grade_requirements.assert_not_awaited()

    def test_item_failure_is_isolated_and_mixed_results_pass_through(self):
        mixed_results = [
            {
                "id": "row-12",
                "success": False,
                "error": "Bedrock timeout",
                "Requirement": "Provide statewide service.",
                "Recommendation": "ERROR",
                "Response": "Error processing requirement: Bedrock timeout",
                "Source": "N/A",
                "Page": "",
            },
            _success_verdict("row-13", "Maintain an incident log."),
        ]
        self.grade_requirements.return_value = mixed_results

        response = self.client.post(
            "/requirements",
            json={
                "requirements": [
                    {"id": "row-12", "text": "Provide statewide service."},
                    {"id": "row-13", "text": "Maintain an incident log."},
                ]
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], mixed_results)

    def test_summary_is_computed_from_results(self):
        self.grade_requirements.return_value = [
            _success_verdict("row-12", "First requirement.", "MET"),
            _success_verdict("row-13", "Second requirement.", "NOT MET"),
            {
                "id": "row-14",
                "success": False,
                "error": "Agent unavailable",
                "Requirement": "Third requirement.",
                "Recommendation": "ERROR",
                "Response": "Error processing requirement: Agent unavailable",
                "Source": "N/A",
                "Page": "",
            },
        ]

        response = self.client.post(
            "/requirements",
            json={
                "requirements": [
                    {"id": "row-12", "text": "First requirement."},
                    {"id": "row-13", "text": "Second requirement."},
                    {"id": "row-14", "text": "Third requirement."},
                ]
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["summary"],
            {
                "total": 3,
                "succeeded": 2,
                "failed": 1,
                "by_recommendation": {"MET": 1, "NOT MET": 1, "ERROR": 1},
            },
        )


class AgentEndpointRegressionTests(unittest.TestCase):
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

    def test_get_agent_keeps_agent_response_shape(self):
        response = self.client.get("/agent", params={"query": "hello"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.json()),
            {"query", "response", "success", "contract_id"},
        )

    def test_post_agent_keeps_agent_response_shape(self):
        response = self.client.post("/agent", json={"query": "hello"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.json()),
            {"query", "response", "success", "contract_id"},
        )


if __name__ == "__main__":
    unittest.main()
