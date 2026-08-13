"""Red-phase tests for hybrid retrieval and asynchronous reranking."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from _stubs import install_offline_stubs


install_offline_stubs()

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from search.database_searching.reranker import CohereReranker  # noqa: E402
from search.database_searching.search import SearchEngine  # noqa: E402


def _fulltext_hit(document_id, rank=0.8):
    return {
        "id": document_id,
        "text": f"fulltext {document_id}",
        "metadata": {"source": "fulltext"},
        "rank": rank,
    }


def _vector_hit(document_id, distance=0.2):
    return {
        "id": document_id,
        "text": f"vector {document_id}",
        "metadata": {"source": "vector"},
        "distance": distance,
    }


class HybridSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = SearchEngine(table_name="embeddings_tn_6756_tenncare")
        self.engine.fulltext_search = AsyncMock(return_value=[_fulltext_hit("lexical")])
        self.engine.semantic_search = AsyncMock(return_value=[_vector_hit("semantic")])
        self.engine.reranker.rerank_results = AsyncMock(
            side_effect=self._rerank_in_received_order
        )

    async def _rerank_in_received_order(self, *, query, documents, top_k):
        return [
            {**document, "rerank_score": 1.0 - (index / 100)}
            for index, document in enumerate(documents[:top_k])
        ]

    def _assert_hybrid_search_exists(self):
        self.assertTrue(
            hasattr(self.engine, "hybrid_search"),
            "SearchEngine.hybrid_search is not implemented",
        )

    def _assert_search_call(self, search_mock, expected_query, expected_limit):
        search_mock.assert_awaited_once()
        args, kwargs = search_mock.await_args
        query = args[0] if args else kwargs["query"]
        limit = kwargs.get("limit", args[1] if len(args) > 1 else None)
        self.assertEqual(query, expected_query)
        self.assertEqual(limit, expected_limit)

    async def test_awaits_each_retrieval_leg_exactly_once(self):
        self._assert_hybrid_search_exists()

        await self.engine.hybrid_search("termination clause", "expanded query")

        self.engine.fulltext_search.assert_awaited_once()
        self.engine.semantic_search.assert_awaited_once()

    async def test_routes_normalized_and_expanded_queries_to_the_correct_legs(self):
        self._assert_hybrid_search_exists()
        normalized = "termination clause"
        expanded = "termination clause indemnity extra"

        await self.engine.hybrid_search(normalized, expanded)

        self._assert_search_call(self.engine.fulltext_search, normalized, 32)
        self._assert_search_call(self.engine.semantic_search, expanded, 32)

    async def test_uses_default_limit_of_thirty_two_for_both_legs(self):
        self._assert_hybrid_search_exists()

        await self.engine.hybrid_search("normalized", "expanded")

        self._assert_search_call(self.engine.fulltext_search, "normalized", 32)
        self._assert_search_call(self.engine.semantic_search, "expanded", 32)

    async def test_caps_candidates_and_preserves_reranker_order(self):
        self._assert_hybrid_search_exists()
        self.engine.fulltext_search.return_value = [
            _fulltext_hit(f"fulltext-{index}") for index in range(20)
        ]
        self.engine.semantic_search.return_value = [
            _vector_hit(f"vector-{index}") for index in range(20)
        ]

        async def reverse_rerank(*, query, documents, top_k):
            return [
                {**document, "rerank_score": 0.9 - (index / 100)}
                for index, document in enumerate(reversed(documents[:top_k]))
            ]

        self.engine.reranker.rerank_results.side_effect = reverse_rerank

        results = await self.engine.hybrid_search("normalized", "expanded")

        self.engine.reranker.rerank_results.assert_awaited_once()
        rerank_kwargs = self.engine.reranker.rerank_results.await_args.kwargs
        self.assertEqual(rerank_kwargs["query"], "normalized")
        self.assertLessEqual(len(rerank_kwargs["documents"]), 32)
        self.assertEqual(rerank_kwargs["top_k"], 8)
        expected_ids = [
            document["id"] for document in reversed(rerank_kwargs["documents"][:8])
        ]
        self.assertEqual([document["id"] for document in results], expected_ids)
        self.assertTrue(all("rerank_score" in document for document in results))

    async def test_tags_legs_and_retains_required_fields(self):
        self._assert_hybrid_search_exists()

        results = await self.engine.hybrid_search("normalized", "expanded")

        by_id = {document["id"]: document for document in results}
        for document in results:
            self.assertTrue(
                {
                    "id",
                    "text",
                    "metadata",
                    "distance",
                    "retrieval_leg",
                    "fusion_rank",
                }.issubset(document)
            )
        self.assertIsNone(by_id["lexical"]["distance"])
        self.assertEqual(by_id["lexical"]["retrieval_leg"], "fulltext")
        self.assertEqual(by_id["semantic"]["distance"], 0.2)
        self.assertEqual(by_id["semantic"]["retrieval_leg"], "vector")
        self.assertTrue(all(document["fusion_rank"] >= 1 for document in results))

    async def test_reranker_failure_returns_fused_results_without_scores(self):
        self._assert_hybrid_search_exists()
        self.engine.reranker.rerank_results.side_effect = RuntimeError(
            "reranker unavailable"
        )

        results = await self.engine.hybrid_search("normalized", "expanded")

        self.assertTrue(results)
        self.assertEqual(
            [document["id"] for document in results],
            ["lexical", "semantic"],
        )
        self.assertTrue(
            all(document.get("rerank_score") is None for document in results)
        )

    async def test_reranker_failure_returns_candidate_window_not_full_fused_list(self):
        self._assert_hybrid_search_exists()
        self.engine.fulltext_search.return_value = [
            _fulltext_hit(f"fulltext-{index}") for index in range(40)
        ]
        self.engine.semantic_search.return_value = [
            _vector_hit(f"vector-{index}") for index in range(40)
        ]
        self.engine.reranker.rerank_results.side_effect = RuntimeError(
            "reranker unavailable"
        )

        results = await self.engine.hybrid_search(
            "normalized", "expanded", top_k=40
        )

        self.assertEqual(len(results), 32)
        self.assertTrue(
            all(document.get("rerank_score") is None for document in results)
        )

    async def test_empty_fulltext_leg_still_returns_semantic_results(self):
        self._assert_hybrid_search_exists()
        self.engine.fulltext_search.return_value = []

        results = await self.engine.hybrid_search("normalized", "expanded")

        self.assertEqual([document["id"] for document in results], ["semantic"])
        self.assertEqual(results[0]["retrieval_leg"], "vector")


class CohereRerankerAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_blocking_rerank_call_to_executor(self):
        reranker = CohereReranker()
        reranker.client.rerank = MagicMock(
            return_value={
                "results": [{"index": 0, "relevanceScore": 0.9}],
            }
        )
        loop = MagicMock()

        async def invoke_in_executor(executor, callable_, *args):
            return callable_(*args)

        loop.run_in_executor = AsyncMock(side_effect=invoke_in_executor)

        with (
            patch.object(asyncio, "get_event_loop", return_value=loop),
            patch.object(asyncio, "get_running_loop", return_value=loop),
        ):
            results = await reranker.rerank_results(
                query="hello",
                documents=[{"id": 1, "text": "hello"}],
            )

        loop.run_in_executor.assert_awaited_once()
        dispatched_callable = loop.run_in_executor.await_args.args[1]
        self.assertIsNot(
            dispatched_callable,
            reranker.rerank_results,
            "the blocking client call itself must be handed to the executor",
        )
        reranker.client.rerank.assert_called_once()
        self.assertEqual(results[0]["rerank_score"], 0.9)


if __name__ == "__main__":
    unittest.main()
