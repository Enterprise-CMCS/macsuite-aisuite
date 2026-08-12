"""Unit tests for deterministic reciprocal rank fusion."""

import inspect
import sys
import unittest
from pathlib import Path


RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from search.database_searching.fusion import reciprocal_rank_fusion  # noqa: E402


class ReciprocalRankFusionTests(unittest.TestCase):
    def test_default_rank_constant_is_sixty(self):
        signature = inspect.signature(reciprocal_rank_fusion)

        self.assertEqual(signature.parameters["k"].default, 60)

    def test_document_ranked_first_in_both_lists_outranks_single_list_leader(self):
        lexical = [
            {"id": "shared", "text": "shared lexical"},
            {"id": "lexical-only", "text": "lexical"},
        ]
        semantic = [
            {"id": "shared", "text": "shared semantic"},
            {"id": "semantic-only", "text": "semantic"},
        ]

        fused = reciprocal_rank_fusion(lexical, semantic)
        ids = [document["id"] for document in fused]

        self.assertLess(ids.index("shared"), ids.index("lexical-only"))

    def test_document_present_in_only_one_list_remains_in_output(self):
        fused = reciprocal_rank_fusion(
            [{"id": "shared"}, {"id": "lexical-only"}],
            [{"id": "shared"}, {"id": "semantic-only"}],
        )

        self.assertEqual(
            set(document["id"] for document in fused),
            {"shared", "lexical-only", "semantic-only"},
        )

    def test_repeated_fusion_is_deterministic_and_ties_keep_first_seen_order(self):
        first = [{"id": "first-seen"}, {"id": "second-seen"}]
        second = [{"id": "second-seen"}, {"id": "first-seen"}]

        first_run = reciprocal_rank_fusion(first, second)
        second_run = reciprocal_rank_fusion(first, second)
        first_ids = [document["id"] for document in first_run]
        second_ids = [document["id"] for document in second_run]

        self.assertEqual(first_ids, second_ids)
        self.assertEqual(first_ids, ["first-seen", "second-seen"])

    def test_empty_list_preserves_non_empty_list_order(self):
        ranked = [{"id": "one"}, {"id": "two"}, {"id": "three"}]

        fused = reciprocal_rank_fusion(ranked, [])

        self.assertEqual([document["id"] for document in fused], ["one", "two", "three"])

    def test_accepts_more_than_two_ranked_lists(self):
        fused = reciprocal_rank_fusion(
            [{"id": "one"}],
            [{"id": "two"}],
            [{"id": "three"}],
        )

        self.assertEqual(
            [document["id"] for document in fused],
            ["one", "two", "three"],
        )


if __name__ == "__main__":
    unittest.main()
