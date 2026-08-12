"""Red-phase tests for request-scoped chat dependencies."""

import importlib
import inspect
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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


class ChatDepsFactoryTests(unittest.TestCase):
    def setUp(self):
        self._restore_env_after_test("EMBEDDINGS_TABLE_NAME")
        os.environ.pop("EMBEDDINGS_TABLE_NAME", None)

    def _restore_env_after_test(self, name):
        original = os.environ.get(name)

        def restore():
            if original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original

        self.addCleanup(restore)

    def _deps_module(self):
        module = importlib.import_module("search.database_searching.deps")
        cache = getattr(module, "_ENGINE_CACHE", None)
        if isinstance(cache, dict):
            cache.clear()
        return module

    def test_explicit_contract_resolves_its_embeddings_table(self):
        deps_module = self._deps_module()

        deps = deps_module.build_chat_deps("wa_6369")

        self.assertEqual(deps.search_engine.table_name, "embeddings_wa_6369_ifc")

    def test_omitted_contract_uses_active_ini_table(self):
        deps_module = self._deps_module()

        deps = deps_module.build_chat_deps(None)

        self.assertEqual(
            deps.search_engine.table_name,
            "embeddings_tn_6756_tenncare",
        )

    def test_explicit_contract_beats_environment_override(self):
        os.environ["EMBEDDINGS_TABLE_NAME"] = "embeddings_override"
        deps_module = self._deps_module()

        explicit = deps_module.build_chat_deps("me_0002")
        default = deps_module.build_chat_deps(None)

        self.assertEqual(
            explicit.search_engine.table_name,
            "embeddings_me_0002_nemt",
        )
        self.assertEqual(default.search_engine.table_name, "embeddings_override")

    def test_search_engine_cache_is_keyed_by_contract_table(self):
        deps_module = self._deps_module()

        first = deps_module.build_chat_deps("tn_6756")
        second = deps_module.build_chat_deps("tn_6756")
        other = deps_module.build_chat_deps("wa_6369")

        self.assertIs(first.search_engine, second.search_engine)
        self.assertIsNot(first.search_engine, other.search_engine)

    def test_each_call_returns_fresh_chat_deps_with_cached_engine(self):
        deps_module = self._deps_module()

        first = deps_module.build_chat_deps("tn_6756")
        second = deps_module.build_chat_deps("tn_6756")

        self.assertIsNot(first, second)
        self.assertIs(first.search_engine, second.search_engine)

    def test_sequential_contracts_remain_isolated_in_one_process(self):
        deps_module = self._deps_module()

        wa_deps = deps_module.build_chat_deps("wa_6472")
        me_deps = deps_module.build_chat_deps("me_0002")

        self.assertEqual(
            wa_deps.search_engine.table_name,
            "embeddings_wa_6472_ahimc",
        )
        self.assertEqual(
            me_deps.search_engine.table_name,
            "embeddings_me_0002_nemt",
        )
        self.assertNotEqual(
            wa_deps.search_engine.table_name,
            me_deps.search_engine.table_name,
        )


class SearchResultsSignatureTests(unittest.TestCase):
    def test_get_search_results_accepts_contract_or_table_scope(self):
        results_module = importlib.import_module(
            "search.database_searching.results"
        )

        parameters = inspect.signature(
            results_module.get_search_results
        ).parameters

        self.assertTrue(
            {"contract_id", "table_name"} & parameters.keys(),
            "get_search_results must accept contract_id or table_name",
        )


if __name__ == "__main__":
    unittest.main()
