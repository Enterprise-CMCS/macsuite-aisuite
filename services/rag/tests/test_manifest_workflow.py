"""Unit tests for the MuleSoft manifest workflow (no AWS, Bedrock or Postgres).

Covers the manifest contract, versioned per-state embeddings tables, the
embeddings Lambda actions, the requirement review runner and the manifest API.
"""

import json
import os
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
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
        "common.utils.settings",
        "data_embeddings_storage.database.connection",
        "data_embeddings_storage.database.data_processing_embeddings",
        "data_embeddings_storage.database.table_setup",
    )
}
with patch.dict(sys.modules, _IMPORT_STUBS):
    from common.utils.manifest import Manifest, RequirementResult
    from data_embeddings_storage import rag_process
    from data_embeddings_storage.database import embedding_versions
    from search import manifest_review
    from search.database_searching.review_models import EvidenceRecord, RequirementReview
    from search.routes import manifest_api

REQUEST_KEY = "3f6c1e2a-8b4d-4c1e-9a77-2d5b0f1c9e41"
DOCUMENTS_BUCKET = "aisuite-dev-contract-rag"
TEMP_BUCKET = "aisuite-dev-llm-pipeline-temp"
STATE_MACHINE_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:aisuite-dev-manifest-ingest"
EXECUTION_ARN = f"arn:aws:states:us-east-1:123456789012:execution:aisuite-dev-manifest-ingest:{REQUEST_KEY}"
RAG_KEY = "RAG_Output/SUB-123/ATT-001/DOC-001/amendment.json"
NOW = datetime(2026, 12, 1, 6, 0, tzinfo=timezone.utc)


class PgError(Exception):
    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def version_row(version, status, state="MD", created=None, completed=None, dropped=None):
    created = created or NOW - timedelta(days=60)
    if completed is None and status != "BUILDING":
        completed = created
    return {
        "state": state,
        "version": version,
        "table_name": f"embeddings_{state.lower()}_v{version}",
        "status": status,
        "created_at": created,
        "completed_at": completed,
        "dropped_at": dropped,
    }


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


class EmbeddingVersionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _connection(existing=None, values=()):
        connection = MagicMock()
        connection.execute = AsyncMock(return_value="UPDATE 1")
        connection.fetchrow = AsyncMock(return_value=existing)
        connection.fetchval = AsyncMock(side_effect=list(values))
        return connection

    @staticmethod
    def _expiry_connection(rows, current=None):
        connection = EmbeddingVersionTests._connection(values=[NOW])
        connection.fetch = AsyncMock(return_value=rows)
        if current is None:
            by_name = {row["table_name"]: row for row in rows}

            async def fetchrow(sql, table_name, qualified):
                row = by_name[table_name]
                return {"status": row["status"], "dropped_at": None, "can_drop": True}

            connection.fetchrow = AsyncMock(side_effect=fetchrow)
        else:
            connection.fetchrow = AsyncMock(return_value=current)
        return connection

    def test_only_allocated_table_names_are_accepted(self):
        self.assertEqual(embedding_versions.versioned_table_name("md", 3), "embeddings_md_v3")
        self.assertEqual(embedding_versions.validate_versioned_table_name("embeddings_md_v3"), "embeddings_md_v3")
        for invalid in ("embeddings_md", "embeddings_md_v0", "embeddings_tn_6756_tenncare", "embeddings_md_v1; --", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                embedding_versions.validate_versioned_table_name(invalid)
        with self.assertRaises(ValueError):
            embedding_versions.versioned_table_name("Maryland", 1)

    async def test_retried_allocation_returns_the_version_the_request_holds(self):
        connection = self._connection(
            existing={"state": "MD", "version": 2, "table_name": "embeddings_md_v2", "dropped_at": None}
        )

        version = await embedding_versions.allocate_version(connection, "md", REQUEST_KEY)

        self.assertEqual(version, embedding_versions.EmbeddingVersion("MD", 2, "embeddings_md_v2"))
        connection.fetchval.assert_not_awaited()
        self.assertEqual(connection.execute.await_count, 1)  # only the advisory lock

    async def test_allocation_takes_the_next_version_and_skips_unregistered_tables(self):
        # Next registry version is 3, but embeddings_md_v3 already exists outside the registry.
        connection = self._connection(values=[3, True, False])

        version = await embedding_versions.allocate_version(connection, "MD", REQUEST_KEY)

        self.assertEqual(version, embedding_versions.EmbeddingVersion("MD", 4, "embeddings_md_v4"))
        lock, insert = connection.execute.await_args_list
        self.assertEqual(lock.args[1], "aisuite-embedding-version:MD")
        self.assertIn("INSERT INTO aisuite_schema.embedding_versions", insert.args[0])
        self.assertEqual(insert.args[1:], ("MD", 4, "embeddings_md_v4", REQUEST_KEY))

    async def test_an_empty_table_is_never_published(self):
        connection = self._connection(values=[0])

        with self.assertRaises(RuntimeError):
            await embedding_versions.mark_ready(connection, REQUEST_KEY, "embeddings_md_v4")
        connection.execute.assert_not_awaited()

    def test_expiry_keeps_the_latest_ready_and_drops_only_versions_past_retention(self):
        old, recent = NOW - timedelta(days=45), NOW - timedelta(days=10)
        rows = [
            # MD: v1 superseded by v2 45 days ago, v2 superseded by v3 only 10 days ago.
            version_row(1, "READY", created=old, completed=old - timedelta(days=5)),
            version_row(2, "READY", created=old, completed=old),
            version_row(3, "READY", created=recent, completed=recent),
            version_row(4, "FAILED", created=old, completed=old),
            version_row(5, "FAILED", created=recent, completed=recent),
            version_row(6, "BUILDING", created=old),
            version_row(7, "BUILDING", created=recent),
            version_row(8, "READY", created=old, completed=old, dropped=recent),
            # TN: a single READY version, however old, is the latest and is kept.
            version_row(1, "READY", state="TN", created=old, completed=old - timedelta(days=300)),
            # NE: v2 is the newest READY but already dropped, so it supersedes nothing.
            version_row(1, "READY", state="NE", created=old, completed=old - timedelta(days=5)),
            version_row(2, "READY", state="NE", created=old, completed=old, dropped=recent),
        ]

        expired = embedding_versions.select_expired(rows, NOW, 30)

        self.assertEqual(
            [row["table_name"] for row in expired],
            ["embeddings_md_v1", "embeddings_md_v4", "embeddings_md_v6"],
        )

    def test_expiry_retention_boundary_is_exclusive(self):
        rows = [
            version_row(1, "FAILED", completed=NOW - timedelta(days=30)),
            version_row(2, "FAILED", completed=NOW - timedelta(days=30, seconds=1)),
        ]

        expired = embedding_versions.select_expired(rows, NOW, 30)

        self.assertEqual([row["table_name"] for row in expired], ["embeddings_md_v2"])

    async def test_expiry_drops_each_table_under_the_state_lock_with_a_lock_timeout(self):
        connection = self._expiry_connection([version_row(4, "FAILED"), version_row(6, "BUILDING")])

        result = await embedding_versions.expire_versions(connection, 30)

        self.assertEqual(result, embedding_versions.ExpiryResult(["embeddings_md_v4", "embeddings_md_v6"], []))
        statements = [call.args for call in connection.execute.await_args_list]
        self.assertEqual(statements[0], ("SELECT pg_advisory_xact_lock(hashtext($1))", "aisuite-embedding-version:MD"))
        self.assertEqual(statements[1], ("SET LOCAL lock_timeout = '5s'",))
        self.assertEqual(statements[2], ("DROP TABLE IF EXISTS aisuite_schema.embeddings_md_v4",))
        self.assertEqual(statements[3][1:], ("embeddings_md_v4",))
        self.assertEqual(statements[6], ("DROP TABLE IF EXISTS aisuite_schema.embeddings_md_v6",))
        self.assertEqual(connection.transaction.call_count, 2)

    async def test_expiry_continues_past_a_lock_timeout_then_raises(self):
        connection = self._expiry_connection([version_row(4, "FAILED"), version_row(6, "BUILDING")])

        async def execute(statement, *args):
            if statement == "DROP TABLE IF EXISTS aisuite_schema.embeddings_md_v4":
                raise PgError(embedding_versions.LOCK_NOT_AVAILABLE)
            return "OK"

        connection.execute = AsyncMock(side_effect=execute)

        with self.assertRaises(RuntimeError) as raised:
            await embedding_versions.expire_versions(connection, 30)

        self.assertIn("embeddings_md_v4", str(raised.exception))
        self.assertNotIn("embeddings_md_v6", str(raised.exception))
        self.assertIn(
            ("DROP TABLE IF EXISTS aisuite_schema.embeddings_md_v6",),
            [call.args for call in connection.execute.await_args_list],
        )

    async def test_expiry_reports_tables_owned_by_the_other_login_without_failing(self):
        connection = self._expiry_connection(
            [version_row(4, "FAILED")], current={"status": "FAILED", "dropped_at": None, "can_drop": False}
        )

        result = await embedding_versions.expire_versions(connection, 30)

        self.assertEqual(result, embedding_versions.ExpiryResult([], ["embeddings_md_v4"]))
        self.assertFalse(any("DROP TABLE" in call.args[0] for call in connection.execute.await_args_list))

    async def test_expiry_skips_a_version_that_changed_since_selection(self):
        # e.g. an abandoned BUILDING run was finalized while expiry was running.
        connection = self._expiry_connection(
            [version_row(6, "BUILDING")], current={"status": "READY", "dropped_at": None, "can_drop": True}
        )

        result = await embedding_versions.expire_versions(connection, 30)

        self.assertEqual(result, embedding_versions.ExpiryResult([], []))
        self.assertFalse(any("DROP TABLE" in call.args[0] for call in connection.execute.await_args_list))

    async def test_expiry_records_a_version_whose_table_is_already_gone(self):
        connection = self._expiry_connection(
            [version_row(4, "FAILED")], current={"status": "FAILED", "dropped_at": None, "can_drop": None}
        )

        result = await embedding_versions.expire_versions(connection, 30)

        self.assertEqual(result.dropped, ["embeddings_md_v4"])

    async def test_a_dropped_version_is_never_reused_or_published(self):
        connection = self._connection(
            existing={"state": "MD", "version": 6, "table_name": "embeddings_md_v6", "dropped_at": NOW}
        )
        with self.assertRaises(RuntimeError):
            await embedding_versions.allocate_version(connection, "md", REQUEST_KEY)

        connection = self._connection(values=[12])
        connection.execute = AsyncMock(return_value="UPDATE 0")
        with self.assertRaises(LookupError):
            await embedding_versions.mark_ready(connection, REQUEST_KEY, "embeddings_md_v6")
        self.assertIn("dropped_at IS NULL", connection.execute.await_args.args[0])

    async def test_registry_gains_dropped_at_only_when_missing(self):
        connection = self._connection()
        connection.fetchrow = AsyncMock(return_value={"present": True, "has_dropped_at": True})
        await embedding_versions.ensure_registry(connection)
        connection.execute.assert_not_awaited()

        connection.fetchrow = AsyncMock(return_value={"present": True, "has_dropped_at": False})
        await embedding_versions.ensure_registry(connection)
        self.assertEqual(connection.execute.await_args.args, (embedding_versions._ADD_DROPPED_AT_SQL,))

    async def test_registry_migration_refused_for_lack_of_ownership_says_how_to_fix_it(self):
        connection = self._connection()
        connection.fetchrow = AsyncMock(return_value={"present": True, "has_dropped_at": False})
        connection.execute = AsyncMock(side_effect=PgError("42501"))

        with self.assertRaises(RuntimeError) as raised:
            await embedding_versions.ensure_registry(connection)

        self.assertIn("bootstrap", str(raised.exception))

    def test_new_registries_have_a_dropped_at_column(self):
        self.assertIn("dropped_at   TIMESTAMPTZ", embedding_versions._CREATE_REGISTRY_SQL)


class EmbeddingsActionTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()

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

    def test_allocate_creates_the_versioned_table(self):
        version = embedding_versions.EmbeddingVersion("MD", 4, "embeddings_md_v4")
        with patch.object(embedding_versions, "ensure_registry", new=AsyncMock()), \
                patch.object(embedding_versions, "allocate_version", new=AsyncMock(return_value=version)) as allocate, \
                patch.object(rag_process, "create_embedding_table", new=AsyncMock()) as create_table:
            result = rag_process.handler({"action": "allocate", "state": "MD", "requestKey": REQUEST_KEY})

        self.assertEqual(result, {"state": "MD", "version": 4, "tableName": "embeddings_md_v4"})
        allocate.assert_awaited_once_with(self.connection, "MD", REQUEST_KEY)
        create_table.assert_awaited_once_with("embeddings_md_v4")
        self.close_db.assert_awaited_once()

    def test_embed_replaces_earlier_rows_of_the_same_file(self):
        documents = [{"text": "chunk one", "metadata": {"page": 1}}, {"text": "chunk two"}]
        with patch.object(rag_process, "_load_rag_documents", return_value=documents), \
                patch.object(embedding_versions, "delete_source_rows", new=AsyncMock()) as delete_rows, \
                patch.object(rag_process, "_store_embeddings",
                             new=AsyncMock(return_value={"postgres_processed": 2})) as store:
            result = rag_process.handler({"action": "embed", "key": RAG_KEY, "tableName": "embeddings_md_v4"})

        self.assertEqual(result, {"key": RAG_KEY, "tableName": "embeddings_md_v4", "embedded": 2})
        delete_rows.assert_awaited_once_with(self.connection, "embeddings_md_v4", RAG_KEY)
        stored, table_name, _ = store.await_args.args
        self.assertEqual(table_name, "embeddings_md_v4")
        self.assertEqual(
            [doc["metadata"] for doc in stored],
            [{"page": 1, "rag_output_key": RAG_KEY}, {"rag_output_key": RAG_KEY}],
        )

    def test_expire_drops_old_versions_with_the_requested_retention(self):
        outcome = embedding_versions.ExpiryResult(["embeddings_md_v1"], ["embeddings_md_v2"])
        with patch.object(embedding_versions, "ensure_registry", new=AsyncMock()),                 patch.object(embedding_versions, "expire_versions", new=AsyncMock(return_value=outcome)) as expire:
            result = rag_process.handler({"action": "expire", "retentionDays": 30})

        self.assertEqual(
            result, {"retentionDays": 30, "dropped": ["embeddings_md_v1"], "notOwner": ["embeddings_md_v2"]}
        )
        expire.assert_awaited_once_with(self.connection, 30)
        self.close_db.assert_awaited_once()

    def test_expire_requires_a_whole_number_of_days(self):
        with patch.object(embedding_versions, "expire_versions", new=AsyncMock()) as expire:
            for invalid in (0, -1, True, 1.9, "30", None):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    rag_process.handler({"action": "expire", "retentionDays": invalid})
            with self.assertRaises(ValueError):
                rag_process.handler({"action": "expire"})
        expire.assert_not_awaited()

    def test_rejects_unknown_actions_and_tables_it_did_not_allocate(self):
        with self.assertRaises(ValueError):
            rag_process.handler({"action": "drop"})
        with self.assertRaises(ValueError):
            rag_process.handler({"action": "embed", "key": RAG_KEY, "tableName": "embeddings_tn_6756_tenncare"})
        self.close_db.assert_awaited_once()


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


if __name__ == "__main__":
    unittest.main()
