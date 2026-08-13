"""Regression guard for the persisted prompt identity."""

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
sys.modules["pydantic_ai"] = pydantic_ai
sys.modules["pydantic_ai.models"] = pydantic_ai_models
sys.modules["pydantic_ai.models.bedrock"] = pydantic_ai_bedrock

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

import search.database_searching.agents as agents_mod  # noqa: E402


class PromptVersionGuardTests(unittest.TestCase):
    def test_persisted_prompt_identity_is_explicitly_versioned(self):
        self.assertEqual(agents_mod.PROMPT_VERSION, "hybrid-search-v2")
        self.assertEqual(
            agents_mod.PROMPT_SHA256,
            "823de49710e58f6306034874135bd6998df4d91c02f480b8f24835f7e04f1aa5",
        )


if __name__ == "__main__":
    unittest.main()
