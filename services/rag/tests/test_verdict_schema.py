import inspect
import os
import re
import sys
import unittest

RAG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if RAG_ROOT not in sys.path:
    sys.path.insert(0, RAG_ROOT)

from data_embeddings_storage.database.embeddings_schema import APP_SCHEMA  # noqa: E402
from data_embeddings_storage.database import verdict_schema  # noqa: E402
from data_embeddings_storage.database.verdict_schema import (  # noqa: E402
    VERDICT_CHUNKS_TABLE,
    VERDICTS_TABLE,
    create_verdict_chunks_table_sql,
    create_verdicts_table_sql,
    expected_verdict_index_names,
    verdict_index_statements,
)


def _compact(sql):
    return " ".join(sql.lower().split())


class VerdictSchemaTests(unittest.TestCase):
    def test_table_names_are_qualified_with_shared_app_schema(self):
        self.assertEqual(VERDICTS_TABLE, f"{APP_SCHEMA}.verdicts")
        self.assertEqual(
            VERDICT_CHUNKS_TABLE,
            f"{APP_SCHEMA}.verdict_chunks",
        )

        source = inspect.getsource(verdict_schema)
        self.assertIn("APP_SCHEMA", source)
        self.assertNotIn('"aisuite_schema"', source)
        self.assertNotIn("'aisuite_schema'", source)

    def test_verdicts_create_statement_has_locked_columns(self):
        sql = _compact(create_verdicts_table_sql())

        self.assertIn(
            f"create table if not exists {APP_SCHEMA}.verdicts",
            sql,
        )
        expected_fragments = (
            "id bigserial primary key",
            "created_at timestamptz not null default now()",
            "request_id uuid not null",
            "source text not null",
            "client text",
            "contract_id text not null",
            "embeddings_table text not null",
            "requirement_text text not null",
            "requirement_sha256 text not null",
            "verdict text not null",
            "response_text text",
            "source_text text",
            "page_text text",
            "raw_output text",
            "parsed_ok boolean not null",
            "model_id text not null",
            "embed_model_id text not null",
            "prompt_version text not null",
            "prompt_sha256 text not null",
            "model_settings jsonb",
            "retrieval jsonb",
            "latency_ms integer",
            "schema_version smallint not null default 1",
        )
        for fragment in expected_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, sql)

        self.assertRegex(
            sql,
            r"check\s*\(\s*verdict\s+in\s*"
            r"\(\s*'met'\s*,\s*'not met'\s*,\s*'unclear'\s*,\s*'error'\s*\)"
            r"\s*\)",
        )
        source_column = re.search(
            r"\bsource\s+text\s+not\s+null(?P<tail>.*?)(?:,\s*client\b)",
            sql,
        )
        self.assertIsNotNone(source_column)
        self.assertNotIn("check", source_column.group("tail"))
        self.assertNotRegex(sql, r"\bclient\s+text\s+not\s+null\b")
        for nullable_column, data_type in (
            ("response_text", "text"),
            ("source_text", "text"),
            ("page_text", "text"),
            ("raw_output", "text"),
            ("model_settings", "jsonb"),
            ("retrieval", "jsonb"),
            ("latency_ms", "integer"),
        ):
            self.assertNotRegex(
                sql,
                rf"\b{nullable_column}\s+{data_type}\s+not\s+null\b",
            )

    def test_verdict_chunks_create_statement_has_locked_columns(self):
        sql = _compact(create_verdict_chunks_table_sql())

        self.assertIn(
            f"create table if not exists {APP_SCHEMA}.verdict_chunks",
            sql,
        )
        expected_fragments = (
            "id bigserial primary key",
            "verdict_id bigint not null",
            "rank smallint not null",
            "embeddings_table text not null",
            "embedding_row_id integer",
            "doc_name text",
            "page text",
            "distance double precision",
            "relevance_score double precision",
            "retrieval_leg text",
            "fusion_rank smallint",
            "rerank_score double precision",
            "chunk_sha256 text not null",
            "chunk_text text",
            "unique (verdict_id, rank)",
        )
        for fragment in expected_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, sql)

        self.assertRegex(
            sql,
            rf"verdict_id\s+bigint\s+not\s+null\s+references\s+"
            rf"{re.escape(APP_SCHEMA)}\.verdicts\s*\(\s*id\s*\)\s+"
            r"on\s+delete\s+cascade",
        )
        for nullable_column, data_type in (
            ("embedding_row_id", "integer"),
            ("doc_name", "text"),
            ("page", "text"),
            ("distance", "double precision"),
            ("relevance_score", "double precision"),
            ("retrieval_leg", "text"),
            ("fusion_rank", "smallint"),
            ("rerank_score", "double precision"),
            ("chunk_text", "text"),
        ):
            self.assertNotRegex(
                sql,
                rf"\b{nullable_column}\s+{data_type}\s+not\s+null\b",
            )

    def test_expected_indexes_cover_locked_columns(self):
        expected_names = (
            "idx_verdicts_created_at",
            "idx_verdicts_requirement_sha256",
            "idx_verdicts_contract_id_created_at",
            "idx_verdict_chunks_verdict_id",
        )
        self.assertEqual(expected_verdict_index_names(), expected_names)

        statements = verdict_index_statements()
        self.assertEqual(len(statements), 4)
        compact = [_compact(statement) for statement in statements]
        for name, table, columns in (
            (expected_names[0], VERDICTS_TABLE, "created_at"),
            (expected_names[1], VERDICTS_TABLE, "requirement_sha256"),
            (expected_names[2], VERDICTS_TABLE, "contract_id, created_at"),
            (expected_names[3], VERDICT_CHUNKS_TABLE, "verdict_id"),
        ):
            with self.subTest(index=name):
                self.assertTrue(
                    any(
                        f"create index if not exists {name}" in statement
                        and f"on {table.lower()} ({columns})" in statement
                        for statement in compact
                    )
                )


if __name__ == "__main__":
    unittest.main()
