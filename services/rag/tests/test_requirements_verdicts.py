"""Red-phase tests for shared requirement verdict parsing and grading."""

import asyncio
import json
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

import search.database_searching.agents as agents_mod  # noqa: E402
from search.requirements.verdicts import (  # noqa: E402
    format_source_with_page,
    grade_requirement,
    grade_requirements,
    parse_agent_verdict,
)


def _verdict(
    requirement="The contractor shall respond within one day.",
    recommendation="MET",
    response="The contract supplies supporting evidence.",
    source="Service requirements",
    page="12",
):
    return json.dumps(
        {
            "Requirement": requirement,
            "Recommendation": recommendation,
            "Response": response,
            "Source": source,
            "Page": page,
        }
    )


class _StringOnlyResult:
    def __init__(self, text):
        self.text = text

    def __str__(self):
        return self.text


class RequirementVerdictParsingTests(unittest.TestCase):
    def test_parse_well_formed_json_preserves_all_pascal_case_fields(self):
        parsed = parse_agent_verdict(_verdict())

        self.assertEqual(
            set(parsed),
            {"Requirement", "Recommendation", "Response", "Source", "Page"},
        )
        self.assertEqual(parsed["Recommendation"], "MET")
        self.assertEqual(parsed["Source"], "Service requirements: 12")
        self.assertEqual(parsed["Page"], "12")

    def test_parse_extracts_json_embedded_in_surrounding_prose(self):
        parsed = parse_agent_verdict(
            f"Here is the requested verdict:\n{_verdict(page='4')}\nThank you."
        )

        self.assertEqual(parsed["Recommendation"], "MET")
        self.assertEqual(parsed["Source"], "Service requirements: 4")

    def test_parse_strips_thinking_block_before_extracting_json(self):
        parsed = parse_agent_verdict(
            "<thinking>I should not expose this analysis.</thinking>\n"
            + _verdict(recommendation="NOT MET")
        )

        self.assertEqual(parsed["Recommendation"], "NOT MET")
        self.assertNotIn("thinking", parsed["Response"])

    def test_parse_malformed_or_absent_json_degrades_to_unclear(self):
        for text in ('{"Recommendation": "MET"', "No JSON was returned"):
            with self.subTest(text=text):
                parsed = parse_agent_verdict(text)
                self.assertEqual(parsed["Recommendation"], "UNCLEAR")
                self.assertEqual(
                    set(parsed),
                    {
                        "Requirement",
                        "Recommendation",
                        "Response",
                        "Source",
                        "Page",
                    },
                )

    def test_missing_page_keeps_source_without_suffix(self):
        payload = json.loads(_verdict())
        payload.pop("Page")

        parsed = parse_agent_verdict(json.dumps(payload))

        self.assertEqual(parsed["Source"], "Service requirements")
        self.assertEqual(format_source_with_page("Service requirements", ""), "Service requirements")
        self.assertEqual(
            format_source_with_page("Service requirements", "N/A"),
            "Service requirements",
        )

    def test_present_page_formats_source_and_page(self):
        self.assertEqual(
            format_source_with_page("Service requirements", "12"),
            "Service requirements: 12",
        )
        self.assertEqual(
            parse_agent_verdict(_verdict(page="12"))["Source"],
            "Service requirements: 12",
        )

    def test_null_source_and_page_coerce_to_strings(self):
        parsed = parse_agent_verdict(
            json.dumps(
                {
                    "Requirement": "A requirement",
                    "Recommendation": "MET",
                    "Response": "Evidence",
                    "Source": None,
                    "Page": None,
                }
            )
        )

        self.assertEqual(parsed["Source"], "N/A")
        self.assertEqual(parsed["Page"], "")
        self.assertIsInstance(parsed["Source"], str)
        self.assertIsInstance(parsed["Page"], str)


class RequirementVerdictGradingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.deps = object()

    def _patch_agent_run(self, *responses, side_effect=None):
        agent_run = AsyncMock(side_effect=side_effect)
        if responses:
            agent_run.side_effect = [SimpleNamespace(output=text) for text in responses]
        return patch.object(
            agents_mod.search_agent,
            "run",
            agent_run,
            create=True,
        ), agent_run

    async def test_first_pass_unclear_retries_exactly_once(self):
        run_patch, agent_run = self._patch_agent_run(
            _verdict(recommendation="UNCLEAR"),
            _verdict(recommendation="MET"),
        )

        with run_patch:
            result = await grade_requirement("A requirement", self.deps)

        self.assertEqual(result["Recommendation"], "MET")
        self.assertEqual(agent_run.await_count, 2)
        self.assertIn("Requirement: A requirement", agent_run.await_args_list[1].kwargs["user_prompt"])

    async def test_retry_unclear_false_does_not_retry(self):
        run_patch, agent_run = self._patch_agent_run(
            _verdict(recommendation="UNCLEAR")
        )

        with run_patch:
            result = await grade_requirement(
                "A requirement", self.deps, retry_unclear=False
            )

        self.assertEqual(result["Recommendation"], "UNCLEAR")
        agent_run.assert_awaited_once()

    async def test_non_unclear_first_pass_never_retries(self):
        run_patch, agent_run = self._patch_agent_run(_verdict(recommendation="MET"))

        with run_patch:
            result = await grade_requirement("A requirement", self.deps)

        self.assertEqual(result["Recommendation"], "MET")
        agent_run.assert_awaited_once()

    async def test_output_attribute_and_string_fallback_parse_identically(self):
        expected = _verdict(recommendation="NOT MET", page="8")
        agent_run = AsyncMock(
            side_effect=[
                SimpleNamespace(output=expected),
                _StringOnlyResult(expected),
            ]
        )

        with patch.object(
            agents_mod.search_agent, "run", agent_run, create=True
        ):
            with_output = await grade_requirement(
                "A requirement", self.deps, retry_unclear=False
            )
            with_string_fallback = await grade_requirement(
                "A requirement", self.deps, retry_unclear=False
            )

        verdict_keys = {
            "Requirement",
            "Recommendation",
            "Response",
            "Source",
            "Page",
        }
        self.assertEqual(
            {key: with_output[key] for key in verdict_keys},
            {key: with_string_fallback[key] for key in verdict_keys},
        )

    async def test_agent_exception_becomes_error_and_does_not_abort_batch(self):
        async def run(user_prompt, deps):
            if user_prompt == "broken":
                raise RuntimeError("model unavailable")
            return SimpleNamespace(output=_verdict(requirement=user_prompt))

        items = [{"id": "bad", "text": "broken"}, {"id": "good", "text": "works"}]
        with (
            patch.object(
                agents_mod.search_agent,
                "run",
                AsyncMock(side_effect=run),
                create=True,
            ),
            patch(
                "search.requirements.verdicts.build_chat_deps",
                side_effect=[object(), object()],
            ),
        ):
            results = await grade_requirements(items, retry_unclear=False)

        self.assertEqual([result["id"] for result in results], ["bad", "good"])
        self.assertEqual(results[0]["Recommendation"], "ERROR")
        self.assertFalse(results[0]["success"])
        self.assertTrue(results[0]["error"])
        self.assertTrue(results[1]["success"])

    async def test_batch_builds_fresh_deps_once_per_item(self):
        items = [
            {"id": "a", "text": "first"},
            {"id": "b", "text": "second"},
        ]
        build_deps = MagicMock(side_effect=[object(), object()])
        agent_run = AsyncMock(
            side_effect=[
                SimpleNamespace(output=_verdict(requirement="first")),
                SimpleNamespace(output=_verdict(requirement="second")),
            ]
        )

        with (
            patch(
                "search.requirements.verdicts.build_chat_deps",
                build_deps,
            ),
            patch.object(
                agents_mod.search_agent, "run", agent_run, create=True
            ),
        ):
            await grade_requirements(items, retry_unclear=False)

        self.assertEqual(build_deps.call_count, len(items))

    async def test_batch_preserves_request_order_with_concurrency(self):
        async def run(user_prompt, deps):
            delay = {"first": 0.03, "second": 0.01, "third": 0}[user_prompt]
            await asyncio.sleep(delay)
            return SimpleNamespace(output=_verdict(requirement=user_prompt))

        items = [
            {"id": "a", "text": "first"},
            {"id": "b", "text": "second"},
            {"id": "c", "text": "third"},
        ]
        with (
            patch.object(
                agents_mod.search_agent,
                "run",
                AsyncMock(side_effect=run),
                create=True,
            ),
            patch(
                "search.requirements.verdicts.build_chat_deps",
                side_effect=[object(), object(), object()],
            ),
        ):
            results = await grade_requirements(
                items, retry_unclear=False, concurrency=3
            )

        self.assertEqual([result["id"] for result in results], ["a", "b", "c"])
        self.assertEqual(
            [result["Requirement"] for result in results],
            ["first", "second", "third"],
        )

    async def test_batch_assigns_zero_based_ids_when_omitted(self):
        items = [{"text": "first"}, {"text": "second"}]
        agent_run = AsyncMock(
            side_effect=[
                SimpleNamespace(output=_verdict(requirement="first")),
                SimpleNamespace(output=_verdict(requirement="second")),
            ]
        )

        with (
            patch.object(
                agents_mod.search_agent, "run", agent_run, create=True
            ),
            patch(
                "search.requirements.verdicts.build_chat_deps",
                side_effect=[object(), object()],
            ),
        ):
            results = await grade_requirements(items, retry_unclear=False)

        self.assertEqual([result["id"] for result in results], [0, 1])


if __name__ == "__main__":
    unittest.main()
