"""Unit tests for the MuleSoft manifest workflow (no AWS, Bedrock or Postgres).

Covers the manifest contract, per-request embeddings tables, the
embeddings Lambda actions, the requirement review runner and the manifest API.
"""

import json
import os
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Third-party packages are imported before the stubs so patch.dict keeps them in sys.modules.
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

RAG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if RAG_ROOT not in sys.path:
    sys.path.insert(0, RAG_ROOT)

# These modules call AWS or Postgres at import time. patch.dict removes the stubs again after import.
_IMPORT_STUBS = {
    name: MagicMock()
    for name in (
        "asyncpg",
        "structlog",
        "pydantic_ai",
        "pydantic_ai.capabilities",
        "pydantic_ai.capabilities.hooks",
        "pydantic_ai.messages",
        "pydantic_ai.usage",
        "common.utils.settings",
        "data_embeddings_storage.database.connection",
        "data_embeddings_storage.database.data_processing_embeddings",
        "data_embeddings_storage.database.table_setup",
        "search.database_searching.model_provider",
        "search.database_searching.search",
    )
}
_IMPORT_STUBS["pydantic_ai"].Agent.return_value.tool.side_effect = lambda fn: fn
with patch.dict(sys.modules, _IMPORT_STUBS):
    from common.utils.manifest import (
        MAX_TABLE_NAME_LEN,
        Manifest,
        RequirementResult,
        manifest_embeddings_table_name,
    )
    from data_embeddings_storage import rag_process
    from search import manifest_review
    from search.database_searching.review_models import EvidenceRecord, RequirementReview, simplify_doc_id
    from search.routes import endpoint, manifest_api

REQUEST_KEY = "3f6c1e2a-8b4d-4c1e-9a77-2d5b0f1c9e41"
DOCUMENTS_BUCKET = "aisuite-dev-contract-rag"
TEMP_BUCKET = "aisuite-dev-llm-pipeline-temp"
STATE_MACHINE_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:aisuite-dev-manifest-ingest"
EXECUTION_ARN = f"arn:aws:states:us-east-1:123456789012:execution:aisuite-dev-manifest-ingest:{REQUEST_KEY}"
RAG_KEY = "RAG_Output/SUB-123/ATT-001/DOC-001/amendment.json"


def requirement(index):
    return {
        "id": f"a0X8z00000{index:04d}EAK",
        "itemNo": f"{index}.1",
        "legalCite": "42 CFR 438.206",
        "requirement": f"Requirement {index} the contract must satisfy.",
    }


def manifest_payload(**overrides):
    payload = {
        "requestKey": REQUEST_KEY,
        "reviewType": "STREAMLINED",
        "state": "md",
        "legalRequirements": [requirement(1), requirement(2)],
        "documents": [
            {"documentId": "DOC-001", "s3": {"bucket": DOCUMENTS_BUCKET, "path": "SUB-123/ATT-001/DOC-001/amendment.pdf"}},
            {"documentId": "DOC-003", "s3": {"bucket": DOCUMENTS_BUCKET, "path": "SUB-123/ATT-001/DOC-003/supporting-2.docx"}},
        ],
    }
    payload.update(overrides)
    return payload


def evidence(page, doc_id="amendment.pdf", **fields):
    return EvidenceRecord(quote=fields.pop("quote", "quoted text"), doc_id=doc_id, page=page, verified=True, **fields)


class ManifestContractTests(unittest.TestCase):
    def test_accepts_any_number_of_requirements_and_normalizes_state(self):
        manifest = Manifest.model_validate(
            manifest_payload(legalRequirements=[requirement(index) for index in range(4000)])
        )

        self.assertEqual(manifest.state, "MD")
        self.assertEqual(len(manifest.legalRequirements), 4000)

    def test_rejects_duplicate_requirement_ids_and_document_paths(self):
        duplicate_ids = manifest_payload(legalRequirements=[requirement(1), requirement(1)])
        duplicate_paths = manifest_payload()
        duplicate_paths["documents"][1]["s3"]["path"] = duplicate_paths["documents"][0]["s3"]["path"]

        for payload, message in (
            (duplicate_ids, "legalRequirements[].id must be unique"),
            (duplicate_paths, "documents[].s3.path must be unique"),
        ):
            with self.subTest(message=message):
                with self.assertRaises(ValidationError) as raised:
                    Manifest.model_validate(payload)
                self.assertIn(message, str(raised.exception))


class ManifestTableNameTests(unittest.TestCase):
    def test_short_request_key_includes_digest_and_disambiguates_case_and_separators(self):
        self.assertEqual(
            manifest_embeddings_table_name("REQ-123", "MD"),
            "embeddings_req_123_5fde4fd5_md",
        )
        self.assertEqual(
            manifest_embeddings_table_name("12345", "tn"),
            "embeddings_12345_60a59929_tn",
        )
        distinct_keys = ("REQ-123", "req-123", "req_123", "REQ_123", "---", "___")
        tables = [manifest_embeddings_table_name(key, "MD") for key in distinct_keys]
        self.assertEqual(len(set(tables)), len(distinct_keys))
        for table in tables:
            self.assertLessEqual(len(table), MAX_TABLE_NAME_LEN)

    def test_long_request_key_is_truncated_with_deterministic_hash(self):
        table_uuid = manifest_embeddings_table_name(REQUEST_KEY, "MD")
        self.assertTrue(table_uuid.startswith("embeddings_3f6c1e2a_8b4d_4c1e_"))
        self.assertTrue(table_uuid.endswith("_md"))
        self.assertLessEqual(len(table_uuid), MAX_TABLE_NAME_LEN)
        self.assertEqual(table_uuid, manifest_embeddings_table_name(REQUEST_KEY, "md"))

        # Two long keys sharing a 79-char prefix still get distinct table names.
        first = manifest_embeddings_table_name("a" * 79 + "1", "MD")
        second = manifest_embeddings_table_name("a" * 79 + "2", "MD")
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(first), MAX_TABLE_NAME_LEN)
        self.assertLessEqual(len(second), MAX_TABLE_NAME_LEN)

    def test_rejects_invalid_request_key_or_state(self):
        for bad_key in ("", "bad;drop", "a" * 81, None):
            with self.subTest(bad_key=bad_key), self.assertRaises(ValueError):
                manifest_embeddings_table_name(bad_key, "MD")
        for bad_state in ("", "Maryland", "12", None):
            with self.subTest(bad_state=bad_state), self.assertRaises(ValueError):
                manifest_embeddings_table_name("REQ-1", bad_state)


class EmbeddingsActionTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.connection = MagicMock()
        self.connection.execute = AsyncMock()

        @asynccontextmanager
        async def transaction():
            self.events.append(("tx_begin",))
            try:
                yield
            except BaseException:
                self.events.append(("tx_rollback",))
                raise
            self.events.append(("tx_commit",))

        self.connection.transaction = transaction

        @asynccontextmanager
        async def pooled_connection():
            yield self.connection

        self.close_db = AsyncMock()
        for patcher in (
            patch.object(rag_process, "pooled_connection", pooled_connection),
            patch.object(rag_process, "close_db", self.close_db),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_create_table_builds_request_key_state_table(self):
        expected_table = manifest_embeddings_table_name(REQUEST_KEY, "MD")
        with patch.object(rag_process, "create_embedding_table", new=AsyncMock()) as create_table:
            result = rag_process.handler({"action": "create_table", "state": "md", "requestKey": REQUEST_KEY})

        self.assertEqual(result, {"state": "MD", "tableName": expected_table})
        create_table.assert_awaited_once_with(expected_table)
        self.close_db.assert_awaited_once()

    def test_embed_replaces_earlier_rows_of_the_same_file(self):
        table = manifest_embeddings_table_name(REQUEST_KEY, "MD")
        documents = [{"text": "chunk one", "metadata": {"page": 1}}, {"text": "chunk two"}]
        source = rag_process.LoadedRagSource(
            bucket="post-bucket",
            key=RAG_KEY,
            documents=documents,
            version="version:v1",
        )
        processor = MagicMock()
        records = [("chunk one", "{}", [0.1]), ("chunk two", "{}", [0.2])]

        async def record_execute(sql, *args):
            if "pg_advisory_xact_lock" in sql:
                self.events.append(("lock", args[0]))

        def record_version_check(bucket, key):
            self.events.append(("check_version", bucket, key))
            return "version:v1"

        async def record_generate(docs, tbl, key):
            self.events.append(("generate", tbl, key))
            return processor, records

        async def record_delete(conn, tbl, key):
            self.events.append(("delete", conn is self.connection, tbl, key))

        async def record_store(conn, proc, recs):
            self.events.append(("store", conn is self.connection, proc is processor, len(recs)))
            return len(recs)

        self.connection.execute.side_effect = record_execute
        with patch.object(rag_process, "_load_rag_documents", return_value=source), \
                patch.object(rag_process, "_current_source_version", side_effect=record_version_check), \
                patch.object(rag_process, "_generate_embeddings", side_effect=record_generate) as generate, \
                patch.object(rag_process, "delete_source_rows", side_effect=record_delete) as delete_rows, \
                patch.object(rag_process, "_store_embeddings", side_effect=record_store) as store:
            result = rag_process.handler({"action": "embed", "key": RAG_KEY, "tableName": table})

        self.assertEqual(result, {"key": RAG_KEY, "tableName": table, "embedded": 2})
        delete_rows.assert_called_once_with(self.connection, table, RAG_KEY)
        store.assert_called_once_with(self.connection, processor, records)
        stamped, table_name, _ = generate.call_args.args
        self.assertEqual(table_name, table)
        self.assertEqual(
            [doc["metadata"] for doc in stamped],
            [{"page": 1, "rag_output_key": RAG_KEY}, {"rag_output_key": RAG_KEY}],
        )
        lock_id = f"{table}:{RAG_KEY}"
        self.assertEqual(
            self.events,
            [
                ("generate", table, RAG_KEY),
                ("tx_begin",),
                ("lock", lock_id),
                ("check_version", "post-bucket", RAG_KEY),
                ("delete", True, table, RAG_KEY),
                ("store", True, True, 2),
                ("tx_commit",),
            ],
        )

    def test_embed_empty_documents_deletes_rows_atomically_without_generating(self):
        table = manifest_embeddings_table_name(REQUEST_KEY, "MD")
        source = rag_process.LoadedRagSource(
            bucket="post-bucket",
            key=RAG_KEY,
            documents=[],
            version="version:v1",
        )

        async def record_delete(conn, tbl, key):
            self.events.append(("delete", conn is self.connection, tbl, key))

        with patch.object(rag_process, "_load_rag_documents", return_value=source), \
                patch.object(rag_process, "_current_source_version", return_value="version:v1"), \
                patch.object(rag_process, "_generate_embeddings", new=AsyncMock()) as generate, \
                patch.object(rag_process, "delete_source_rows", side_effect=record_delete), \
                patch.object(rag_process, "_store_embeddings", new=AsyncMock()) as store:
            result = rag_process.handler({"action": "embed", "key": RAG_KEY, "tableName": table})

        self.assertEqual(result, {"key": RAG_KEY, "tableName": table, "embedded": 0})
        generate.assert_not_awaited()
        store.assert_not_awaited()
        self.assertEqual(
            self.events,
            [
                ("tx_begin",),
                ("delete", True, table, RAG_KEY),
                ("tx_commit",),
            ],
        )

    def test_embed_raises_when_source_version_changes_under_lock(self):
        table = manifest_embeddings_table_name(REQUEST_KEY, "MD")
        source = rag_process.LoadedRagSource(
            bucket="post-bucket",
            key=RAG_KEY,
            documents=[{"text": "chunk one"}],
            version="version:v1",
        )
        with patch.object(rag_process, "_load_rag_documents", return_value=source), \
                patch.object(rag_process, "_current_source_version", return_value="version:v2"), \
                patch.object(rag_process, "_generate_embeddings", new=AsyncMock(return_value=(MagicMock(), [("chunk one", "{}", [0.1])]))), \
                patch.object(rag_process, "delete_source_rows", new=AsyncMock()) as delete_rows, \
                patch.object(rag_process, "_store_embeddings", new=AsyncMock()) as store:
            with self.assertRaisesRegex(RuntimeError, "changed while embedding"):
                rag_process.handler({"action": "embed", "key": RAG_KEY, "tableName": table})

        delete_rows.assert_not_awaited()
        store.assert_not_awaited()

    def test_rejects_unknown_actions_and_invalid_table_names(self):
        with self.assertRaises(ValueError):
            rag_process.handler({"action": "drop"})
        with self.assertRaises(ValueError):
            rag_process.handler({"action": "embed", "key": RAG_KEY, "tableName": "other_table"})
        with self.assertRaises(ValueError):
            rag_process.handler({"action": "embed", "key": RAG_KEY, "tableName": "embeddings_" + "a" * 40})
        self.assertEqual(self.close_db.await_count, 2)


class ResultMappingTests(unittest.TestCase):
    def setUp(self):
        self.manifest = Manifest.model_validate(manifest_payload())
        self.labels = manifest_review.document_labels(self.manifest)

    def test_found_in_reports_page_ranges_per_document(self):
        found_in = manifest_review.found_in

        self.assertEqual(found_in([evidence(87)], self.labels), "DOC-001: Page 88")
        self.assertEqual(
            found_in([evidence(page) for page in (44, 40, 41, 42, 43)], self.labels),
            "DOC-001: Pages 41-45",
        )
        self.assertEqual(
            found_in([evidence(2), evidence(6, doc_id="supporting-2.docx")], self.labels),
            "DOC-001: Page 3; DOC-003: Page 7",
        )
        self.assertEqual(found_in([], self.labels), "Not found")

    def test_found_in_omits_document_label_only_for_single_document_manifest(self):
        single_manifest = Manifest.model_validate(
            manifest_payload(documents=[manifest_payload()["documents"][0]])
        )
        single_labels = manifest_review.document_labels(single_manifest)

        self.assertEqual(manifest_review.found_in([evidence(87)], single_labels), "Page 88")
        self.assertEqual(
            manifest_review.found_in([evidence(page) for page in (44, 40, 41, 42, 43)], single_labels),
            "Pages 41-45",
        )
        self.assertEqual(
            manifest_review.found_in([evidence(2)], self.labels),
            "DOC-001: Page 3",
        )
        self.assertEqual(
            manifest_review.found_in([evidence(2)], single_labels, single_document=False),
            "DOC-001: Page 3",
        )

    def test_shared_basenames_keep_distinct_document_ids_and_pages(self):
        manifest = Manifest.model_validate(
            manifest_payload(
                documents=[
                    {"documentId": "DOC-001", "s3": {"bucket": DOCUMENTS_BUCKET, "path": "DOC-001/a/contract.pdf"}},
                    {"documentId": "DOC-002", "s3": {"bucket": DOCUMENTS_BUCKET, "path": "DOC-002/b/contract.pdf"}},
                    {"documentId": "DOC-003", "s3": {"bucket": DOCUMENTS_BUCKET, "path": "DOC-003/c/addendum.pdf"}},
                ]
            )
        )
        labels = manifest_review.document_labels(manifest)

        self.assertNotIn("contract", labels)
        self.assertEqual(
            manifest_review.found_in(
                [
                    evidence(2, doc_id="contract", source_key="DOC-001/a/contract.pdf"),
                    evidence(6, doc_id="contract", source_key="DOC-002/b/contract.pdf"),
                ],
                labels,
            ),
            "DOC-001: Page 3; DOC-002: Page 7",
        )
        self.assertEqual(
            manifest_review.found_in(
                [
                    evidence(2, doc_id="contract", source_key="RAG_Output/DOC-001/a/contract.json"),
                    evidence(
                        4,
                        doc_id="contract",
                        source_key=f"RAG_Output/runs/{REQUEST_KEY}/DOC-002/b/contract.json",
                    ),
                    evidence(8, doc_id="addendum", source_key="DOC-003/c/addendum.pdf"),
                ],
                labels,
            ),
            "DOC-001: Page 3; DOC-002: Page 5; DOC-003: Page 9",
        )
        review = RequirementReview(
            requirement="Shared basename check",
            evidence=[
                evidence(2, doc_id="contract", source_key="DOC-001/a/contract.pdf"),
                evidence(6, doc_id="contract", source_key="DOC-002/b/contract.pdf"),
            ],
        )
        self.assertEqual(
            review.where_found(),
            "page 3 - DOC-001/a/contract.pdf | page 7 - DOC-002/b/contract.pdf",
        )

    def test_met_review_cites_only_verified_evidence(self):
        item = self.manifest.legalRequirements[0]
        review = RequirementReview(
            requirement=item.requirement,
            status="MET",
            decision_confidence="High",
            argument="Section 7.2 sets time and distance standards in chunk 12.",
            decision_response="The network adequacy requirement is met.",
            evidence=[
                evidence(40, quote="maintain a network", chunk_id=12),
                EvidenceRecord(quote="paraphrased", chunk_id=13, doc_id="amendment.pdf", page=90, verified=False),
            ],
        )

        result = manifest_review.to_result(item, review, self.labels)

        self.assertEqual(
            (result.id, result.itemNo, result.legalCite, result.requirement),
            (item.id, item.itemNo, item.legalCite, item.requirement),
        )
        self.assertEqual(
            (result.siRecommendation, result.confidence, result.foundIn, result.citedText),
            ("MET", "HIGH", "DOC-001: Page 41", "maintain a network"),
        )
        self.assertEqual(result.rationale, "The network adequacy requirement is met.")
        self.assertEqual(result.reasoning, "Section 7.2 sets time and distance standards on page 41.")
        self.assertIsNone(result.error)

    def test_unsure_and_failed_reviews_are_unclear(self):
        item = self.manifest.legalRequirements[1]
        unsure = RequirementReview(
            requirement=item.requirement,
            status="UNSURE",
            decision_confidence="Medium",
            missing_information="The notice timeframe is not stated.",
        )
        failed = RequirementReview(
            requirement=item.requirement, status="MET", decision_confidence="High", error="ThrottlingException"
        )

        unclear = manifest_review.to_result(item, unsure, self.labels)
        errored = manifest_review.to_result(item, failed, self.labels)

        self.assertEqual((unclear.siRecommendation, unclear.confidence, unclear.foundIn), ("UNCLEAR", "MEDIUM", "Not found"))
        self.assertEqual(unclear.reasoning, "Missing information: The notice timeframe is not stated.")
        self.assertEqual((errored.siRecommendation, errored.confidence, errored.error), ("UNCLEAR", "LOW", "ThrottlingException"))
        self.assertIn("manual review", errored.rationale)


class FakeStore:
    def __init__(self, completed=None):
        self.completed = completed or {}
        self.saved_items = {}
        self.saved_results = None

    def completed_items(self):
        return dict(self.completed)

    def save_item(self, index, result):
        self.saved_items[index] = result

    def save_results(self, results):
        self.saved_results = results


class RunReviewTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _manifest(count):
        return Manifest.model_validate(manifest_payload(legalRequirements=[requirement(i) for i in range(count)]))

    @staticmethod
    def _checkpoint(item, **fields):
        return RequirementResult(
            **{**item.model_dump(), **fields},
            siRecommendation="MET",
            confidence="HIGH",
            foundIn="Page 1",
            citedText="quoted text",
            rationale="checkpointed",
            reasoning="checkpointed",
        )

    @staticmethod
    def _reviewer(failing=()):
        async def review(item):
            return RequirementReview(
                requirement=item.requirement,
                status="MET",
                decision_confidence="High",
                evidence=[evidence(0)],
                error="ThrottlingException" if item.id in failing else "",
            )

        return AsyncMock(side_effect=review)

    async def test_resumes_from_checkpoints_and_keeps_manifest_order(self):
        manifest = self._manifest(3)
        first, second, third = manifest.legalRequirements
        # Index 1 holds a checkpoint from a different manifest, so it is reviewed again.
        store = FakeStore({0: self._checkpoint(first), 1: self._checkpoint(second, id="stale")})
        review = self._reviewer()

        results = await manifest_review.run_review(manifest, store, review, concurrency=2)

        self.assertCountEqual([call.args[0].id for call in review.await_args_list], [second.id, third.id])
        self.assertEqual([item.id for item in results.legalRequirements], [first.id, second.id, third.id])
        self.assertEqual(results.legalRequirements[0].rationale, "checkpointed")
        self.assertEqual(results.legalRequirements[1].foundIn, "DOC-001: Page 1")
        self.assertEqual(sorted(store.saved_items), [1, 2])
        self.assertIs(store.saved_results, results)
        self.assertEqual((results.requestKey, results.state), (REQUEST_KEY, "MD"))

    async def test_single_document_manifest_omits_document_label_in_results(self):
        manifest = Manifest.model_validate(
            manifest_payload(
                documents=[manifest_payload()["documents"][0]],
                legalRequirements=[requirement(0)],
            )
        )
        store = FakeStore()

        results = await manifest_review.run_review(manifest, store, self._reviewer())

        self.assertEqual(results.legalRequirements[0].foundIn, "Page 1")

    async def test_failed_reviews_are_reported_but_not_checkpointed(self):
        manifest = self._manifest(5)
        store = FakeStore()

        results = await manifest_review.run_review(
            manifest, store, self._reviewer(failing={manifest.legalRequirements[2].id})
        )

        self.assertEqual(results.legalRequirements[2].siRecommendation, "UNCLEAR")
        self.assertEqual(sorted(store.saved_items), [0, 1, 3, 4])

    async def test_too_many_failed_reviews_abort_without_results(self):
        manifest = self._manifest(2)
        store = FakeStore()

        with self.assertRaises(manifest_review.ReviewIncompleteError):
            await manifest_review.run_review(
                manifest, store, self._reviewer(failing={item.id for item in manifest.legalRequirements})
            )
        self.assertIsNone(store.saved_results)


class ManifestApiTests(unittest.TestCase):
    def setUp(self):
        self.s3 = MagicMock()
        self.s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        self.sfn = MagicMock()
        self.sfn.exceptions.ExecutionDoesNotExist = type("ExecutionDoesNotExist", (Exception,), {})
        self.sfn.exceptions.ExecutionAlreadyExists = type("ExecutionAlreadyExists", (Exception,), {})
        self.sfn.describe_execution.side_effect = self.sfn.exceptions.ExecutionDoesNotExist()
        self.sfn.start_execution.return_value = {"executionArn": EXECUTION_ARN}

        properties = {"input_bucket_name": DOCUMENTS_BUCKET, "temp_bucket_name": TEMP_BUCKET}
        for patcher in (
            patch.object(manifest_api, "_client", side_effect={"s3": self.s3, "stepfunctions": self.sfn}.get),
            patch.object(manifest_api.Helper, "get_property", side_effect=properties.__getitem__),
            patch.dict(os.environ, {"INGEST_STATE_MACHINE_ARN": STATE_MACHINE_ARN}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        app = FastAPI()
        manifest_api.install(app)
        self.client = TestClient(app)

    def _execution(self, status, **fields):
        self.sfn.describe_execution.side_effect = None
        self.sfn.describe_execution.return_value = {"executionArn": EXECUTION_ARN, "status": status, **fields}

    def test_accepts_a_valid_manifest_and_starts_the_workflow(self):
        response = self.client.post("/manifest", json=manifest_payload())

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json(),
            {
                "requestKey": REQUEST_KEY,
                "status": "PROCESSING",
                "executionArn": EXECUTION_ARN,
                "documentCount": 2,
                "requirementCount": 2,
                "message": "Manifest validated; processing started.",
            },
        )
        stored = self.s3.put_object.call_args.kwargs
        self.assertEqual((stored["Bucket"], stored["Key"]), (TEMP_BUCKET, f"manifests/{REQUEST_KEY}/manifest.json"))
        self.assertEqual(stored["IfNoneMatch"], "*")
        started = self.sfn.start_execution.call_args.kwargs
        self.assertEqual((started["stateMachineArn"], started["name"]), (STATE_MACHINE_ARN, REQUEST_KEY))
        workflow_input = json.loads(started["input"])
        self.assertEqual((workflow_input["state"], workflow_input["requirementCount"]), ("MD", 2))
        self.assertNotIn("legalRequirements", workflow_input)

    def test_accepts_manifest_larger_than_8kib_within_app_limit(self):
        large_payload = manifest_payload(legalRequirements=[requirement(i) for i in range(120)])
        raw = json.dumps(large_payload).encode("utf-8")
        self.assertGreater(len(raw), 8192)

        response = self.client.post("/manifest", content=raw, headers={"Content-Type": "application/json"})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["requirementCount"], 120)

    def test_rejects_manifest_body_exceeding_app_limit_via_content_length_and_stream(self):
        raw = json.dumps(manifest_payload(legalRequirements=[requirement(i) for i in range(25)])).encode("utf-8")
        limit = len(raw) - 1

        with patch.dict(os.environ, {"MAX_MANIFEST_BODY_BYTES": str(limit)}):
            by_header = self.client.post(
                "/manifest",
                content=raw,
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(by_header.status_code, 413)
            self.assertEqual(by_header.json()["status"], "PAYLOAD_TOO_LARGE")

            def chunked_body():
                midpoint = len(raw) // 2
                yield raw[:midpoint]
                yield raw[midpoint:]

            by_stream = self.client.post(
                "/manifest",
                content=chunked_body(),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(by_stream.status_code, 413)
            self.assertEqual(by_stream.json()["status"], "PAYLOAD_TOO_LARGE")

        self.s3.put_object.assert_not_called()
        self.sfn.start_execution.assert_not_called()

    def test_rejects_workflow_input_exceeding_step_functions_limit_before_reserving_key(self):
        long_prefix = "segment/" * 65
        oversized = manifest_payload(
            documents=[
                {
                    "documentId": f"DOC-{index:03d}",
                    "s3": {"bucket": DOCUMENTS_BUCKET, "path": f"{long_prefix}doc-{index:03d}.pdf"},
                }
                for index in range(500)
            ]
        )
        raw = json.dumps(oversized).encode("utf-8")
        self.assertLess(len(raw), manifest_api.DEFAULT_MAX_MANIFEST_BODY_BYTES)
        self.assertGreater(len(raw), manifest_api.MAX_WORKFLOW_INPUT_BYTES)

        rejected = self.client.post("/manifest", json=oversized)

        self.assertEqual(rejected.status_code, 400)
        body = rejected.json()
        self.assertEqual((body["requestKey"], body["status"]), (REQUEST_KEY, "FAILED_VALIDATION"))
        self.assertIn(str(manifest_api.MAX_WORKFLOW_INPUT_BYTES), body["errors"][0])
        self.s3.head_object.assert_not_called()
        self.s3.put_object.assert_not_called()
        self.sfn.start_execution.assert_not_called()

        # Because the oversized manifest was never reserved, the same requestKey can be reused.
        retry = self.client.post("/manifest", json=manifest_payload())
        self.assertEqual(retry.status_code, 202)
        self.sfn.start_execution.assert_called_once()

    def test_workflow_input_size_limit_counts_utf8_bytes_at_boundary(self):
        serialized = manifest_api._serialized_workflow_input(Manifest.model_validate(manifest_payload()))
        exact_bytes = len(serialized.encode("utf-8"))

        with patch.object(manifest_api, "MAX_WORKFLOW_INPUT_BYTES", exact_bytes):
            accepted = self.client.post("/manifest", json=manifest_payload())
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(self.sfn.start_execution.call_args.kwargs["input"], serialized)

        multibyte = json.dumps({"text": "é" * 20}, ensure_ascii=False)
        self.assertLess(len(multibyte), len(multibyte.encode("utf-8")))
        self.s3.put_object.reset_mock()
        self.sfn.start_execution.reset_mock()
        with patch.object(manifest_api, "_serialized_workflow_input", return_value=multibyte), \
                patch.object(manifest_api, "MAX_WORKFLOW_INPUT_BYTES", len(multibyte)):
            rejected = self.client.post("/manifest", json=manifest_payload())
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()["status"], "FAILED_VALIDATION")
        self.s3.put_object.assert_not_called()
        self.sfn.start_execution.assert_not_called()

    def test_rejects_derived_s3_artifact_keys_exceeding_1024_utf8_bytes_before_reserving_key(self):
        # Valid 1,024-byte S3 source key whose derived run/BDA/RAG artifact keys exceed S3's 1,024-byte limit.
        long_source_key = ("a/" * 509) + "ab.pdf"
        self.assertEqual(len(long_source_key.encode("utf-8")), 1024)
        oversized_key_payload = manifest_payload(
            documents=[{"documentId": "DOC-LONG", "s3": {"bucket": DOCUMENTS_BUCKET, "path": long_source_key}}]
        )

        rejected = self.client.post("/manifest", json=oversized_key_payload)

        self.assertEqual(rejected.status_code, 400)
        body = rejected.json()
        self.assertEqual((body["requestKey"], body["status"]), (REQUEST_KEY, "FAILED_VALIDATION"))
        self.assertIn("1024", body["errors"][0])
        self.s3.head_object.assert_not_called()
        self.s3.put_object.assert_not_called()
        self.sfn.start_execution.assert_not_called()

        retry = self.client.post("/manifest", json=manifest_payload())
        self.assertEqual(retry.status_code, 202)
        self.sfn.start_execution.assert_called_once()

    def test_rejects_downstream_step_functions_map_result_exceeding_256kib_before_reserving_key(self):
        long_request_key = "r" * 80
        # 500 docs with 395-byte S3 keys fit in StartExecution input (< 256 KiB)
        # but exceed 262,144 bytes in the PreProcessDocuments and EmbedDocuments Map results.
        prefix = "p" * (395 - len("/doc-000.pdf"))
        payload = manifest_payload(
            requestKey=long_request_key,
            documents=[
                {
                    "documentId": f"DOC-{index:03d}",
                    "s3": {"bucket": DOCUMENTS_BUCKET, "path": f"{prefix}/doc-{index:03d}.pdf"},
                }
                for index in range(500)
            ],
        )
        manifest_obj = Manifest.model_validate(payload)
        self.assertEqual(len(manifest_obj.documents[0].s3.path.encode("utf-8")), 395)
        self.assertLess(
            len(manifest_api._serialized_workflow_input(manifest_obj).encode("utf-8")),
            manifest_api.MAX_WORKFLOW_INPUT_BYTES,
        )

        rejected = self.client.post("/manifest", json=payload)

        self.assertEqual(rejected.status_code, 400)
        body = rejected.json()
        self.assertEqual((body["requestKey"], body["status"]), (long_request_key, "FAILED_VALIDATION"))
        self.assertTrue(any("PreProcessDocuments" in err for err in body["errors"]))
        self.assertTrue(any("EmbedDocuments" in err for err in body["errors"]))
        self.s3.head_object.assert_not_called()
        self.s3.put_object.assert_not_called()
        self.sfn.start_execution.assert_not_called()

        # Same requestKey remains available after validation failure.
        retry = self.client.post("/manifest", json=manifest_payload(requestKey=long_request_key))
        self.assertEqual(retry.status_code, 202)
        self.sfn.start_execution.assert_called_once()

    def test_accepts_distinct_s3_keys_differing_only_by_empty_directory_components(self):
        payload = manifest_payload(
            documents=[
                {"documentId": "DOC-001", "s3": {"bucket": DOCUMENTS_BUCKET, "path": "folder/sub/a.pdf"}},
                {"documentId": "DOC-002", "s3": {"bucket": DOCUMENTS_BUCKET, "path": "folder//sub/a.pdf"}},
            ]
        )

        response = self.client.post("/manifest", json=payload)

        self.assertEqual(response.status_code, 202)
        self.sfn.start_execution.assert_called_once()

    def test_rejects_documents_that_are_missing_or_in_another_bucket(self):
        self.s3.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
        payload = manifest_payload()
        payload["documents"][1]["s3"]["bucket"] = "another-bucket"

        response = self.client.post("/manifest", json=payload)

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual((body["requestKey"], body["status"]), (REQUEST_KEY, "FAILED_VALIDATION"))
        self.assertEqual(len(body["errors"]), 2)
        self.assertIn("was not found or is not readable (404)", body["errors"][0])
        self.assertIn(f"must be uploaded to bucket '{DOCUMENTS_BUCKET}'", body["errors"][1])
        self.s3.put_object.assert_not_called()
        self.sfn.start_execution.assert_not_called()

    def test_reports_schema_errors_in_the_manifest_error_shape(self):
        response = self.client.post("/manifest", json=manifest_payload(state="Maryland", legalRequirements=[]))

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual((body["requestKey"], body["status"]), (REQUEST_KEY, "FAILED_VALIDATION"))
        self.assertTrue(any(error.startswith("state: ") for error in body["errors"]))
        self.assertTrue(any(error.startswith("legalRequirements: ") for error in body["errors"]))

    def test_a_finished_request_key_cannot_be_resubmitted(self):
        self._execution("SUCCEEDED")

        response = self.client.post("/manifest", json=manifest_payload())

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "COMPLETED")
        self.sfn.start_execution.assert_not_called()

    def test_idempotent_retry_with_same_manifest_returns_202_without_overwriting(self):
        original = Manifest.model_validate(manifest_payload())
        self.s3.put_object.side_effect = ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        body = MagicMock()
        body.read.return_value = original.model_dump_json().encode()
        self.s3.get_object.return_value = {"Body": body}
        self.sfn.start_execution.side_effect = self.sfn.exceptions.ExecutionAlreadyExists()

        response = self.client.post("/manifest", json=manifest_payload())

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["message"], "Manifest is already processing.")
        self.assertEqual(self.s3.put_object.call_count, 1)
        self.assertEqual(self.s3.put_object.call_args.kwargs["IfNoneMatch"], "*")

    def test_rejects_reusing_request_key_with_different_manifest_content(self):
        original = Manifest.model_validate(manifest_payload())
        self.s3.put_object.side_effect = ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        body = MagicMock()
        body.read.return_value = original.model_dump_json().encode()
        self.s3.get_object.return_value = {"Body": body}

        different = manifest_payload(legalRequirements=[requirement(99)])
        response = self.client.post("/manifest", json=different)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "CONFLICT")
        self.sfn.start_execution.assert_not_called()

    def test_status_points_to_results_once_completed(self):
        self._execution(
            "SUCCEEDED",
            startDate=datetime(2025, 1, 1, tzinfo=timezone.utc),
            stopDate=datetime(2025, 1, 1, 2, tzinfo=timezone.utc),
        )

        response = self.client.get(f"/manifest/{REQUEST_KEY}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "COMPLETED")
        self.assertEqual(response.json()["resultsPath"], f"/manifest/{REQUEST_KEY}/results")

    def test_status_reports_default_error_for_timed_out_and_aborted_executions(self):
        for sfn_status, expected_error in (
            ("TIMED_OUT", "States.Timeout"),
            ("ABORTED", "ExecutionAborted"),
        ):
            self._execution(
                sfn_status,
                startDate=datetime(2025, 1, 1, tzinfo=timezone.utc),
                stopDate=datetime(2025, 1, 2, tzinfo=timezone.utc),
            )
            response = self.client.get(f"/manifest/{REQUEST_KEY}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "FAILED")
            self.assertEqual(response.json()["error"], expected_error)

    def test_results_conflict_until_written_then_return_buffered_json(self):
        self._execution("RUNNING")
        self.s3.get_object.side_effect = self.s3.exceptions.NoSuchKey()

        pending = self.client.get(f"/manifest/{REQUEST_KEY}/results")

        self.assertEqual(pending.status_code, 409)
        self.assertEqual(pending.json()["status"], "PROCESSING")

        results = {"requestKey": REQUEST_KEY, "reviewType": "STREAMLINED", "state": "MD", "legalRequirements": []}
        raw = json.dumps(results).encode()
        body = MagicMock()
        body.read.return_value = raw
        self.s3.get_object.side_effect = None
        self.s3.get_object.return_value = {"Body": body}

        ready = self.client.get(f"/manifest/{REQUEST_KEY}/results")

        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json(), results)
        self.assertEqual(ready.headers.get("content-length"), str(len(raw)))
        body.close.assert_called_once()
        self.assertEqual(
            self.s3.get_object.call_args.kwargs,
            {"Bucket": TEMP_BUCKET, "Key": f"manifest-results/{REQUEST_KEY}/results.json"},
        )

    def test_results_pagination_enforces_buffered_size_limit(self):
        items = [
            {
                **requirement(index),
                "siRecommendation": "MET",
                "confidence": "HIGH",
                "foundIn": f"DOC-001: Page {index + 1}",
                "citedText": f"Quoted text {index} " + ("x" * 120),
                "rationale": "Met.",
                "reasoning": "Verified.",
            }
            for index in range(5)
        ]
        results = {
            "requestKey": REQUEST_KEY,
            "reviewType": "STREAMLINED",
            "state": "MD",
            "legalRequirements": items,
        }
        raw = json.dumps(results).encode("utf-8")
        two_item_size = len(json.dumps({**results, "legalRequirements": items[:2]}).encode("utf-8"))
        self.assertLess(two_item_size, len(raw))

        def make_body(*_args, **_kwargs):
            body = MagicMock()
            body.read.return_value = raw
            return {"Body": body}

        self.s3.get_object.side_effect = make_body

        with patch.object(manifest_api, "MAX_RESULTS_RESPONSE_BYTES", two_item_size):
            too_large = self.client.get(f"/manifest/{REQUEST_KEY}/results")
            self.assertEqual(too_large.status_code, 413)
            self.assertEqual(too_large.json()["status"], "PAYLOAD_TOO_LARGE")
            self.assertIn("offset", too_large.json()["errors"][0])

            # Requesting more items than fit shrinks the page to stay within MAX_RESULTS_RESPONSE_BYTES.
            collected = []
            next_offset = 0
            while next_offset is not None:
                page = self.client.get(f"/manifest/{REQUEST_KEY}/results", params={"offset": next_offset, "limit": 5})
                self.assertEqual(page.status_code, 200)
                self.assertLessEqual(len(page.content), two_item_size)
                self.assertEqual(page.headers["x-total-count"], "5")
                self.assertEqual(page.headers["x-offset"], str(next_offset))
                page_items = page.json()["legalRequirements"]
                self.assertEqual(page.headers["x-limit"], str(len(page_items)))
                collected.extend(page_items)
                raw_next = page.headers.get("x-next-offset")
                next_offset = int(raw_next) if raw_next is not None else None

            self.assertEqual([item["id"] for item in collected], [item["id"] for item in items])

        # If a single item exceeds the buffered ceiling, return 413 rather than an oversized response.
        with patch.object(manifest_api, "MAX_RESULTS_RESPONSE_BYTES", 50):
            single_too_large = self.client.get(f"/manifest/{REQUEST_KEY}/results", params={"offset": 0, "limit": 1})
            self.assertEqual(single_too_large.status_code, 413)
            self.assertEqual(single_too_large.json()["status"], "PAYLOAD_TOO_LARGE")


class RemovedEndpointTests(unittest.TestCase):
    def test_removed_routes_are_unavailable_and_not_advertised(self):
        client = TestClient(endpoint.app)
        for method, path in (("GET", "/agent"), ("POST", "/agent"), ("GET", "/contracts")):
            with self.subTest(method=method, path=path):
                self.assertEqual(client.request(method, path).status_code, 404)

        paths = client.get("/openapi.json").json()["paths"]
        advertised = [item["path"] for item in client.get("/").json()["endpoints"]]
        for path in ("/agent", "/contracts"):
            self.assertNotIn(path, paths)
            self.assertNotIn(path, advertised)
        self.assertIn("/manifest", paths)
        self.assertEqual(client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
