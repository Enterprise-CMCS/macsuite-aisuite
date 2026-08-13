import hashlib
import json
import sys
import types
import unittest
from pathlib import Path
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
    """Import-safe stand-in for the generic RunContext annotation."""

    def __class_getitem__(cls, item):
        return cls


pydantic_ai = types.ModuleType("pydantic_ai")
pydantic_ai.Agent = _FakeAgent
pydantic_ai.RunContext = _FakeRunContext
pydantic_ai_models = types.ModuleType("pydantic_ai.models")
pydantic_ai_bedrock = types.ModuleType("pydantic_ai.models.bedrock")
pydantic_ai_bedrock.BedrockConverseModel = MagicMock
sys.modules["pydantic_ai"] = pydantic_ai
sys.modules["pydantic_ai.models"] = pydantic_ai_models
sys.modules["pydantic_ai.models.bedrock"] = pydantic_ai_bedrock

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

import search.database_searching.agents as agents_mod  # noqa: E402

# Let tests that patch SearchEngine's imported database functions load it afresh.
sys.modules.pop("search.database_searching.search", None)


class _Ctx:
    def __init__(self, deps):
        self.deps = deps


class AgentHybridToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.deps = agents_mod.ChatDeps(acronyms={})
        self.context = _Ctx(self.deps)

    def _hybrid_tool(self):
        self.assertIn(
            "hybrid_search",
            agents_mod.search_agent.tools,
            "hybrid_search must be registered on search_agent",
        )
        return agents_mod.search_agent.tools["hybrid_search"]

    def _semantic_tool(self):
        self.assertIn("semantic_search", agents_mod.search_agent.tools)
        return agents_mod.search_agent.tools["semantic_search"]

    def test_hybrid_search_is_registered(self):
        self.assertTrue(
            "hybrid_search" in agents_mod.search_agent.tools
            or hasattr(agents_mod, "hybrid_search"),
            "hybrid_search must be registered on search_agent",
        )

    def test_semantic_search_remains_registered(self):
        self.assertIn("semantic_search", agents_mod.search_agent.tools)

    async def test_hybrid_and_semantic_search_use_distinct_cache_namespaces(self):
        hybrid_search = self._hybrid_tool()
        self.deps.search_engine.semantic_search = AsyncMock(return_value=[])
        self.deps.search_engine.hybrid_search = AsyncMock(return_value=[])

        with patch.object(
            agents_mod,
            "generate_cache_key",
            wraps=agents_mod.generate_cache_key,
        ) as generate_cache_key:
            await agents_mod.search_agent.tools["semantic_search"](
                self.context, "termination clause"
            )
            await hybrid_search(self.context, "termination clause")

        search_types = [
            call.args[1] if len(call.args) > 1 else call.kwargs["search_type"]
            for call in generate_cache_key.call_args_list
        ]
        self.assertGreaterEqual(len(search_types), 2)
        self.assertNotEqual(search_types[0], search_types[1])

    async def test_hybrid_search_deduplicates_without_query_reranking(self):
        hybrid_search = self._hybrid_tool()
        self.deps.search_engine.hybrid_search = AsyncMock(
            return_value=[
                {
                    "id": "hit-1",
                    "text": "Termination text",
                    "metadata": {},
                    "distance": 0.2,
                    "retrieval_leg": "vector",
                    "fusion_rank": 1,
                }
            ]
        )

        with patch.object(
            agents_mod,
            "deduplicate_results",
            wraps=agents_mod.deduplicate_results,
        ) as deduplicate_results:
            await hybrid_search(self.context, "termination clause")

        deduplicate_results.assert_called_once()
        args, kwargs = deduplicate_results.call_args
        dedupe_query = kwargs.get("query", args[1] if len(args) > 1 else None)
        self.assertEqual(dedupe_query, "")

    async def test_hybrid_search_returns_cleaned_provenance_fields(self):
        hybrid_search = self._hybrid_tool()
        self.deps.search_engine.hybrid_search = AsyncMock(
            return_value=[
                {
                    "id": "hit-1",
                    "text": "Termination text",
                    "metadata": json.dumps({"doc_name": "contract.pdf", "page": 7}),
                    "distance": None,
                    "retrieval_leg": "fulltext",
                    "fusion_rank": 1,
                    "rerank_score": 0.97,
                }
            ]
        )

        results = await hybrid_search(self.context, "termination clause")

        self.assertLessEqual(len(results), 8)
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertTrue(
            {
                "text",
                "metadata",
                "id",
                "distance",
                "retrieval_leg",
                "fusion_rank",
                "rerank_score",
            }.issubset(result)
        )
        self.assertEqual(
            result["metadata"], {"doc_name": "contract.pdf", "page": 7}
        )
        self.assertEqual(result["rerank_score"], 0.97)
        self.assertEqual(result["display"], "[result 1]")
        self.assertEqual(self.deps.last_retrieval, results)

    async def test_semantic_provenance_is_ordered_capped_and_keeps_fields(self):
        semantic_search = self._semantic_tool()
        self.deps.search_engine.semantic_search = AsyncMock(
            return_value=[
                {
                    "id": f"hit-{index}",
                    "text": f"Contract text {index}",
                    "metadata": json.dumps({"page": index}),
                    "distance": index / 10,
                }
                for index in range(10)
            ]
        )

        results = await semantic_search(self.context, "unmatched")

        self.assertEqual([result["id"] for result in results], [
            f"hit-{index}" for index in range(agents_mod.FINAL_RESULTS)
        ])
        self.assertEqual(self.deps.last_retrieval, results)
        self.assertEqual(len(self.deps.last_retrieval), agents_mod.FINAL_RESULTS)
        for index, result in enumerate(results):
            self.assertTrue(
                {
                    "text",
                    "metadata",
                    "id",
                    "distance",
                    "_relevance_score",
                }.issubset(result)
            )
            self.assertEqual(result["metadata"], {"page": index})
            self.assertEqual(result["distance"], index / 10)
            self.assertEqual(result["display"], f"[result {index + 1}]")

    async def test_second_semantic_search_replaces_provenance(self):
        semantic_search = self._semantic_tool()
        self.deps.search_engine.semantic_search = AsyncMock(
            side_effect=[
                [
                    {
                        "id": "first",
                        "text": "First result",
                        "metadata": {},
                        "distance": 0.1,
                    }
                ],
                [
                    {
                        "id": "second",
                        "text": "Second result",
                        "metadata": {},
                        "distance": 0.2,
                    }
                ],
            ]
        )

        await semantic_search(self.context, "first query")
        second = await semantic_search(self.context, "second query")

        self.assertEqual([result["id"] for result in second], ["second"])
        self.assertEqual(self.deps.last_retrieval, second)

    async def test_second_hybrid_search_replaces_provenance(self):
        hybrid_search = self._hybrid_tool()
        self.deps.search_engine.hybrid_search = AsyncMock(
            side_effect=[
                [
                    {
                        "id": "first",
                        "text": "First result",
                        "metadata": {},
                        "distance": None,
                        "retrieval_leg": "fulltext",
                        "fusion_rank": 1,
                    }
                ],
                [
                    {
                        "id": "second",
                        "text": "Second result",
                        "metadata": {},
                        "distance": 0.2,
                        "retrieval_leg": "vector",
                        "fusion_rank": 1,
                    }
                ],
            ]
        )

        await hybrid_search(self.context, "first query")
        second = await hybrid_search(self.context, "second query")

        self.assertEqual([result["id"] for result in second], ["second"])
        self.assertEqual(self.deps.last_retrieval, second)

    async def test_cache_hits_restore_provenance_for_both_tools(self):
        for tool_name in ("semantic_search", "hybrid_search"):
            with self.subTest(tool=tool_name):
                deps = agents_mod.ChatDeps(acronyms={})
                context = _Ctx(deps)
                tool = agents_mod.search_agent.tools[tool_name]
                raw_result = {
                    "id": tool_name,
                    "text": "Cached result",
                    "metadata": {},
                    "distance": 0.1,
                }
                if tool_name == "hybrid_search":
                    raw_result.update(
                        retrieval_leg="vector",
                        fusion_rank=1,
                    )
                search_mock = AsyncMock(return_value=[raw_result])
                setattr(deps.search_engine, tool_name, search_mock)

                expected = await tool(context, "same query")
                deps.last_retrieval = [{"id": "stale"}]
                actual = await tool(context, "same query")

                self.assertEqual(actual, expected)
                self.assertEqual(deps.last_retrieval, expected)
                search_mock.assert_awaited_once()

    async def test_empty_and_exception_paths_clear_provenance(self):
        for tool_name in ("semantic_search", "hybrid_search"):
            with self.subTest(tool=tool_name):
                deps = agents_mod.ChatDeps(acronyms={})
                context = _Ctx(deps)
                tool = agents_mod.search_agent.tools[tool_name]
                search_mock = AsyncMock(return_value=[])
                setattr(deps.search_engine, tool_name, search_mock)
                deps.last_retrieval = [{"id": "stale"}]

                self.assertEqual(await tool(context, "empty query"), [])
                self.assertEqual(deps.last_retrieval, [])

                search_mock.side_effect = RuntimeError("search unavailable")
                deps.last_retrieval = [{"id": "stale"}]
                self.assertEqual(await tool(context, "error query"), [])
                self.assertEqual(deps.last_retrieval, [])

    def test_prompt_version_and_sha256_match_system_prompt(self):
        self.assertTrue(
            hasattr(agents_mod, "PROMPT_VERSION"),
            "PROMPT_VERSION must be defined",
        )
        self.assertIsInstance(agents_mod.PROMPT_VERSION, str)
        self.assertTrue(agents_mod.PROMPT_VERSION)
        self.assertEqual(
            agents_mod.PROMPT_SHA256,
            hashlib.sha256(agents_mod.BASE_SYSTEM_PROMPT.encode()).hexdigest(),
        )

    def test_analyze_requirement_with_rag_is_removed(self):
        self.assertFalse(
            hasattr(agents_mod, "analyze_requirement_with_rag"),
            "analyze_requirement_with_rag must be removed",
        )


if __name__ == "__main__":
    unittest.main()
