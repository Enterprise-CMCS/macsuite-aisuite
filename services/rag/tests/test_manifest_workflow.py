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
    from search.database_searching import agents as review_agents
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
        self.connection = MagicMock()
        self.connection.execute = AsyncMock()

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
        events = []

        async def record_execute(sql, *args):
            if "pg_advisory_lock" in sql:
                events.append(("lock", args[0]))
            elif "pg_advisory_unlock" in sql:
                events.append(("unlock", args[0]))

        async def record_delete(conn, tbl, key):
            events.append(("delete", tbl, key))

        async def record_store(docs, tbl, key):
            events.append(("store", tbl, key))
            return {"postgres_processed": len(docs)}

        self.connection.execute.side_effect = record_execute
        with patch.object(rag_process, "_load_rag_documents", return_value=documents), \
                patch.object(rag_process, "delete_source_rows", side_effect=record_delete) as delete_rows, \
                patch.object(rag_process, "_store_embeddings", side_effect=record_store) as store:
            result = rag_process.handler({"action": "embed", "key": RAG_KEY, "tableName": table})

        self.assertEqual(result, {"key": RAG_KEY, "tableName": table, "embedded": 2})
        delete_rows.assert_called_once_with(self.connection, table, RAG_KEY)
        stored, table_name, _ = store.call_args.args
        self.assertEqual(table_name, table)
        self.assertEqual(
            [doc["metadata"] for doc in stored],
            [{"page": 1, "rag_output_key": RAG_KEY}, {"rag_output_key": RAG_KEY}],
        )
        lock_id = f"{table}:{RAG_KEY}"
        self.assertEqual(
            events,
            [
                ("lock", lock_id),
                ("delete", table, RAG_KEY),
                ("store", table, RAG_KEY),
                ("unlock", lock_id),
            ],
        )

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

        self.assertEqual(found_in([evidence(87)], self.labels), "Page 88")
        self.assertEqual(found_in([evidence(page) for page in (44, 40, 41, 42, 43)], self.labels), "Pages 41-45")
        self.assertEqual(
            found_in([evidence(2), evidence(6, doc_id="supporting-2.docx")], self.labels),
            "DOC-001: Page 3; DOC-003: Page 7",
        )
        self.assertEqual(found_in([], self.labels), "Not found")

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
                    evidence(8, doc_id="addendum", source_key="DOC-003/c/addendum.pdf"),
                ],
                labels,
            ),
            "DOC-001: Page 3; DOC-003: Page 9",
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
            ("MET", "HIGH", "Page 41", "maintain a network"),
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
        self.assertEqual(sorted(store.saved_items), [1, 2])
        self.assertIs(store.saved_results, results)
        self.assertEqual((results.requestKey, results.state), (REQUEST_KEY, "MD"))

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

    def test_results_conflict_until_written_then_stream_from_s3(self):
        self._execution("RUNNING")
        self.s3.get_object.side_effect = self.s3.exceptions.NoSuchKey()

        pending = self.client.get(f"/manifest/{REQUEST_KEY}/results")

        self.assertEqual(pending.status_code, 409)
        self.assertEqual(pending.json()["status"], "PROCESSING")

        results = {"requestKey": REQUEST_KEY, "reviewType": "STREAMLINED", "state": "MD", "legalRequirements": []}
        body = MagicMock()
        body.iter_chunks.return_value = iter([json.dumps(results).encode()])
        self.s3.get_object.side_effect = None
        self.s3.get_object.return_value = {"Body": body}

        ready = self.client.get(f"/manifest/{REQUEST_KEY}/results")

        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json(), results)
        self.assertEqual(
            self.s3.get_object.call_args.kwargs,
            {"Bucket": TEMP_BUCKET, "Key": f"manifest-results/{REQUEST_KEY}/results.json"},
        )


class AgentEndpointErrorTests(unittest.TestCase):
    def setUp(self):
        endpoint._deps_by_table.clear()
        endpoint._known_table_names = {"embeddings_tn_6756"}
        self.client = TestClient(endpoint.app)

    def tearDown(self):
        endpoint._deps_by_table.clear()
        endpoint._known_table_names = None

    def test_agent_endpoint_marks_review_error_unsuccessful(self):
        failed_review = RequirementReview(
            requirement="Must cover NEMT",
            status="UNSURE",
            argument="Automated review did not complete cleanly; manual review is required.",
            error="ClientError: ThrottlingException",
        )
        with (
            patch.object(endpoint, "build_deps", return_value=object()),
            patch.object(endpoint, "answer_question_formatted", wraps=review_agents.answer_question_formatted),
            patch.object(review_agents, "review_requirement", AsyncMock(return_value=failed_review)),
        ):
            res = self.client.post(
                "/agent",
                json={"query": "Must cover NEMT", "contract": "embeddings_tn_6756"},
            )

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["contract"], "embeddings_tn_6756")
        self.assertIn("Error: ClientError: ThrottlingException", body["response"])

    def test_agent_endpoint_marks_clean_review_successful(self):
        ok_review = RequirementReview(
            requirement="Must cover NEMT",
            status="MET",
            decision_confidence="HIGH",
            argument="Contract covers NEMT in Section 4.",
            decision_response="Verified evidence establishes NEMT coverage.",
            error="",
        )
        with (
            patch.object(endpoint, "build_deps", return_value=object()),
            patch.object(endpoint, "answer_question_formatted", wraps=review_agents.answer_question_formatted),
            patch.object(review_agents, "review_requirement", AsyncMock(return_value=ok_review)),
        ):
            res = self.client.post(
                "/agent",
                json={"query": "Must cover NEMT", "contract": "embeddings_tn_6756"},
            )

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["success"])
        self.assertIn("AI Recommendation: MET", body["response"])
        self.assertIn("Contract covers NEMT in Section 4.", body["response"])
        self.assertNotIn("Error:", body["response"])


if __name__ == "__main__":
    unittest.main()
