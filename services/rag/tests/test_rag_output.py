"""Unit tests for the pre-processing -> RAG_Output -> embeddings handlers (no AWS or Postgres)."""

import asyncio
from datetime import datetime, timezone
import io
import json
import os
import sys
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote_plus

from botocore.exceptions import ClientError
import numpy as np

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
        "data_embeddings_storage.database.embeddings_client",
        "data_embeddings_storage.database.table_setup",
        "data_preprocessing.parsing.parsed_bda_table",
        "data_preprocessing.parsing.parsed_images",
        "data_preprocessing.parsing.parsed_text_data",
    )
}
with patch.dict(sys.modules, _IMPORT_STUBS):
    from common.utils.aws_files_access import (
        head_s3_object_version,
        object_keys,
        read_s3_json_with_version,
        s3_object_version,
    )
    from data_embeddings_storage import rag_process
    from data_embeddings_storage.database import data_processing_embeddings
    from data_preprocessing import pre_processing
    from data_preprocessing.bedrock import bedrock_BDA
    from data_preprocessing.bedrock.bda_results import (
        BDA_INTERNAL_KEY_HEADROOM_BYTES,
        MAX_S3_KEY_BYTES,
        bda_output_prefix,
        derived_artifact_key_errors,
        doc_output_prefix,
        output_root,
        per_doc_output_key,
        rag_output_key,
    )


def _s3_event(*keys):
    return {
        "Records": [
            {"eventSource": "aws:s3", "s3": {"object": {"key": quote_plus(key, safe="/")}}}
            for key in keys
        ]
    }


class RagOutputKeyTests(unittest.TestCase):
    def test_keeps_source_folders_under_rag_output(self):
        self.assertEqual(
            rag_output_key("state_of_TN/Contract/a.pdf"),
            "RAG_Output/state_of_TN/Contract/a.json",
        )

    def test_root_level_file(self):
        self.assertEqual(rag_output_key("My File.docx"), "RAG_Output/My_File.json")

    def test_root_level_bda_outputs_do_not_collide_with_root_folder(self):
        self.assertEqual(output_root("a.pdf"), "_bdaoutput/")
        self.assertEqual(output_root("root/a.pdf"), "root_bdaoutput/")
        self.assertNotEqual(bda_output_prefix("a.pdf"), bda_output_prefix("root/a.pdf"))
        self.assertNotEqual(doc_output_prefix("a.pdf"), doc_output_prefix("root/a.pdf"))
        for kind in ("text", "table", "image"):
            self.assertNotEqual(per_doc_output_key("a.pdf", kind), per_doc_output_key("root/a.pdf", kind))

    def test_namespaces_intermediate_and_rag_outputs_per_request_key(self):
        key = "NE/sub/a.pdf"
        self.assertEqual(output_root(key, namespace="req-1"), "runs/req-1/NE_bdaoutput/sub/")
        self.assertEqual(bda_output_prefix(key, namespace="req-1"), "runs/req-1/NE_bdaoutput/sub/bdaoutput/")
        self.assertEqual(doc_output_prefix(key, namespace="req-1"), "runs/req-1/NE_bdaoutput/sub/bdaoutput/a/")
        self.assertEqual(
            per_doc_output_key(key, "text", namespace="req-1"),
            "runs/req-1/NE_bdaoutput/sub/BDATextOutput/a-preprocessed-text.json",
        )
        self.assertEqual(rag_output_key(key, namespace="req-1"), "RAG_Output/runs/req-1/NE/sub/a.json")

        self.assertNotEqual(doc_output_prefix(key, namespace="req-1"), doc_output_prefix(key, namespace="req-2"))
        self.assertNotEqual(rag_output_key(key, namespace="req-1"), rag_output_key(key, namespace="req-2"))

        for bad_namespace in ("../escape", "req/1", "a" * 81):
            with self.subTest(bad_namespace=bad_namespace), self.assertRaises(ValueError):
                rag_output_key(key, namespace=bad_namespace)

    def test_preserves_empty_directory_components_in_intermediate_and_rag_paths(self):
        variants = [
            "a.pdf",
            "/a.pdf",
            "//a.pdf",
            "folder/sub/a.pdf",
            "folder//sub/a.pdf",
            "folder/sub//a.pdf",
            "/folder/sub/a.pdf",
        ]
        for namespace in (None, "req-1"):
            with self.subTest(namespace=namespace):
                self.assertEqual(len({output_root(k, namespace=namespace) for k in variants}), len(variants))
                self.assertEqual(len({bda_output_prefix(k, namespace=namespace) for k in variants}), len(variants))
                self.assertEqual(len({doc_output_prefix(k, namespace=namespace) for k in variants}), len(variants))
                self.assertEqual(len({rag_output_key(k, namespace=namespace) for k in variants}), len(variants))
                for kind in ("text", "table", "image"):
                    self.assertEqual(
                        len({per_doc_output_key(k, kind, namespace=namespace) for k in variants}),
                        len(variants),
                    )
        self.assertEqual(output_root("folder//sub/a.pdf"), "folder_bdaoutput//sub/")
        self.assertEqual(doc_output_prefix("folder//sub/a.pdf"), "folder_bdaoutput//sub/bdaoutput/a/")
        self.assertEqual(rag_output_key("folder//sub/a.pdf"), "RAG_Output/folder//sub/a.json")

    def test_derived_artifact_key_errors_enforces_s3_utf8_key_limit(self):
        self.assertEqual(derived_artifact_key_errors("NE/sub/a.pdf", namespace="req-1"), [])

        valid_1024_key = ("a/" * 509) + "ab.pdf"
        self.assertEqual(len(valid_1024_key.encode("utf-8")), MAX_S3_KEY_BYTES)
        errors = derived_artifact_key_errors(valid_1024_key, namespace="req-1")
        self.assertTrue(errors)
        self.assertTrue(any("1024" in err for err in errors))

        # A key shorter than 1,024 UTF-8 bytes whose derived BDA prefix still exceeds 1,024 - headroom.
        utf8_key = ("é/" * 260) + "doc.pdf"
        self.assertLess(len(utf8_key.encode("utf-8")), MAX_S3_KEY_BYTES)
        self.assertGreater(
            len(doc_output_prefix(utf8_key, namespace="r" * 80).encode("utf-8")),
            MAX_S3_KEY_BYTES - BDA_INTERNAL_KEY_HEADROOM_BYTES,
        )
        self.assertTrue(derived_artifact_key_errors(utf8_key, namespace="r" * 80))


class BdaInvokePrefixTests(unittest.TestCase):
    def test_bda_invoke_delimits_document_prefix_with_trailing_slash(self):
        existing_keys = [
            "NE_bdaoutput/bdaoutput/ab/job-1/job_metadata.json",
            "NE_bdaoutput/bdaoutput/ab/job-1/result.json",
        ]
        listed_prefixes = []
        deleted_prefixes = []

        class _FakePaginator:
            def paginate(self, Bucket, Prefix):
                listed_prefixes.append(Prefix)
                matching = [{"Key": k} for k in existing_keys if k.startswith(Prefix)]
                return [{"Contents": matching}] if matching else [{}]

        fake_s3_client = MagicMock()
        fake_s3_client.get_paginator.return_value = _FakePaginator()

        fake_bda_runtime = MagicMock()
        fake_bda_runtime.invoke_data_automation_async.return_value = {"invocationArn": "arn:bda:inv-1"}
        fake_bda_runtime.get_data_automation_status.return_value = {"status": "Success"}

        def fake_delete(_s3_resource, _bucket, prefix):
            deleted_prefixes.append(prefix)

        with patch.object(bedrock_BDA, "s3AwsClient", fake_s3_client), \
                patch.object(bedrock_BDA, "bda_runtime_client", fake_bda_runtime), \
                patch.object(bedrock_BDA.boto3, "resource", return_value=MagicMock()), \
                patch.object(bedrock_BDA.Helper, "delete_bucket_file_recursively", side_effect=fake_delete), \
                patch.object(bedrock_BDA.time, "sleep"):
            summary = bedrock_BDA.bda_invoke(
                "arn:bda:project",
                "in-bucket",
                ["NE/a.pdf"],
                "out-bucket",
                "NE_bdaoutput/bdaoutput/",
                "arn:bda:profile",
                "LIVE",
            )

        self.assertEqual(summary, {"success_count": 1, "failed_count": 0, "failed_files": []})
        self.assertEqual(listed_prefixes, ["NE_bdaoutput/bdaoutput/a/"])
        self.assertEqual(deleted_prefixes, [])
        submitted = fake_bda_runtime.invoke_data_automation_async.call_args.kwargs
        self.assertEqual(
            submitted["outputConfiguration"]["s3Uri"],
            "s3://out-bucket/NE_bdaoutput/bdaoutput/a/",
        )


class ObjectKeysTests(unittest.TestCase):
    def test_decodes_s3_notification_keys(self):
        event = _s3_event("folder one/My File(1).pdf")
        event["Records"].append({"eventSource": "aws:sqs"})

        self.assertEqual(object_keys(event), ["folder one/My File(1).pdf"])

    def test_event_without_records(self):
        self.assertEqual(object_keys({}), [])

    def test_extracts_manifest_document_item_and_rag_output_keys(self):
        single_doc = {
            "documentId": "DOC-001",
            "s3": {
                "bucket": "aisuite-dev-contract-rag",
                "path": "SUB-123/ATT-001/DOC-001/amendment.pdf",
            },
        }
        self.assertEqual(object_keys(single_doc), ["SUB-123/ATT-001/DOC-001/amendment.pdf"])

        manifest_event = {
            "requestKey": "3f6c1e2a-8b4d-4c1e-9a77-2d5b0f1c9e41",
            "documents": [
                single_doc,
                {
                    "documentId": "DOC-003",
                    "s3": {
                        "bucket": "aisuite-dev-contract-rag",
                        "path": "SUB-123/ATT-001/DOC-003/supporting-2.docx",
                    },
                },
            ],
        }
        self.assertEqual(
            object_keys(manifest_event),
            [
                "SUB-123/ATT-001/DOC-001/amendment.pdf",
                "SUB-123/ATT-001/DOC-003/supporting-2.docx",
            ],
        )

        step_output = {
            "status": "completed",
            "rag_output": ["RAG_Output/SUB-123/ATT-001/DOC-001/amendment.json"],
        }
        self.assertEqual(
            object_keys(step_output),
            ["RAG_Output/SUB-123/ATT-001/DOC-001/amendment.json"],
        )


class MergeOutputsToRagOutputTests(unittest.TestCase):
    def test_merges_text_table_and_image_outputs_into_one_rag_file(self):
        outputs = {
            "text": [
                {"text": "chunk one", "metadata": {"element_type": "TEXT", "page": 1}},
                {"text": "", "metadata": {"element_type": "TEXT"}},
            ],
            "table": [{"text": "| a | b |", "metadata": {"element_type": "TABLE"}}],
            "image": [{"text": "chart summary", "metadata": {"element_type": "IMAGE"}}],
        }
        requested_keys = []

        def fake_get_json(bucket, key):
            self.assertEqual(bucket, "post-bucket")
            requested_keys.append(key)
            return outputs[key.rsplit("-preprocessed-", 1)[1].removesuffix(".json")]

        with patch.object(pre_processing.Helper, "get_property", return_value="post-bucket"), \
                patch.object(pre_processing.Helper, "get_json_from_s3", side_effect=fake_get_json), \
                patch.object(pre_processing.Helper, "create_file") as create_file:
            key = pre_processing.merge_outputs_to_rag_output("NE/sub/a.pdf")

        self.assertEqual(key, "RAG_Output/NE/sub/a.json")
        self.assertEqual(
            requested_keys,
            [
                "NE_bdaoutput/sub/BDATextOutput/a-preprocessed-text.json",
                "NE_bdaoutput/sub/BDATableOutput/a-preprocessed-table.json",
                "NE_bdaoutput/sub/BDAImageOutput/a-preprocessed-image.json",
            ],
        )
        create_file.assert_called_once()
        bucket, rag_key, documents = create_file.call_args.args
        self.assertEqual((bucket, rag_key), ("post-bucket", "RAG_Output/NE/sub/a.json"))
        self.assertEqual([doc["text"] for doc in documents], ["chunk one", "| a | b |", "chart summary"])
        self.assertEqual(
            documents[0]["metadata"],
            {"element_type": "TEXT", "page": 1, "source_key": "NE/sub/a.pdf"},
        )

    def test_merges_namespaced_outputs_and_preserves_source_key(self):
        requested_keys = []

        def fake_get_json(_bucket, key):
            requested_keys.append(key)
            return [{"text": f"from {key}", "metadata": {"page": 1}}]

        with patch.object(pre_processing.Helper, "get_property", return_value="post-bucket"), \
                patch.object(pre_processing.Helper, "get_json_from_s3", side_effect=fake_get_json), \
                patch.object(pre_processing.Helper, "create_file") as create_file:
            key = pre_processing.merge_outputs_to_rag_output("NE/sub/a.pdf", namespace="req-42")

        self.assertEqual(key, "RAG_Output/runs/req-42/NE/sub/a.json")
        self.assertEqual(
            requested_keys,
            [
                "runs/req-42/NE_bdaoutput/sub/BDATextOutput/a-preprocessed-text.json",
                "runs/req-42/NE_bdaoutput/sub/BDATableOutput/a-preprocessed-table.json",
                "runs/req-42/NE_bdaoutput/sub/BDAImageOutput/a-preprocessed-image.json",
            ],
        )
        _, rag_key, documents = create_file.call_args.args
        self.assertEqual(rag_key, "RAG_Output/runs/req-42/NE/sub/a.json")
        self.assertEqual(documents[0]["metadata"]["source_key"], "NE/sub/a.pdf")


class PreProcessingHandlerTests(unittest.TestCase):
    def test_processes_each_pdf_or_docx_key(self):
        attempts = iter(["attempt-1", "attempt-2"])
        cleaned = []
        with patch.object(pre_processing.Helper, "get_property", return_value="in-bucket"), \
                patch.object(pre_processing.Helper, "get_s3_object_version", side_effect=["version:v1", "version:v2"]), \
                patch.object(pre_processing, "_new_attempt_namespace", side_effect=attempts), \
                patch.object(pre_processing, "_cleanup_attempt_artifacts", side_effect=cleaned.append), \
                patch.object(pre_processing, "process") as process, \
                patch.object(
                    pre_processing,
                    "merge_outputs_to_rag_output",
                    side_effect=lambda key, namespace=None, **_kw: rag_output_key(key, namespace=namespace),
                ) as merge:
            result = pre_processing.handler(_s3_event("NE/a b.pdf", "NE/c.DOCX", "NE/notes.txt"))

        self.assertEqual([call.args[0] for call in process.call_args_list], ["NE/a b.pdf", "NE/c.DOCX"])
        self.assertEqual(
            [call.kwargs for call in process.call_args_list],
            [{"namespace": "attempt-1"}, {"namespace": "attempt-2"}],
        )
        self.assertEqual(
            merge.call_args_list[0].kwargs,
            {
                "namespace": None,
                "artifact_namespace": "attempt-1",
                "expected_source_version": "version:v1",
                "input_bucket": "in-bucket",
            },
        )
        self.assertEqual(cleaned, ["attempt-1", "attempt-2"])
        self.assertEqual(
            result,
            {"status": "completed", "rag_output": ["RAG_Output/NE/a_b.json", "RAG_Output/NE/c.json"]},
        )

    def test_isolates_concurrent_common_notifications_and_skips_stale_publish(self):
        fake_s3 = MagicMock()
        fake_s3.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
        cleaned = []

        def fake_get_property(name, **_kw):
            return {"input_bucket_name": "in-bucket", "output_bucket": "post-bucket"}[name]

        # Start sees version:v1, but before publishing the source object has moved to version:v2.
        with patch.object(pre_processing.Helper, "get_property", side_effect=fake_get_property), \
                patch.object(
                    pre_processing.Helper,
                    "get_s3_object_version",
                    side_effect=["version:v1", "version:v2"],
                ), \
                patch.object(pre_processing, "_new_attempt_namespace", return_value="attempt-stale"), \
                patch.object(pre_processing, "_cleanup_attempt_artifacts", side_effect=cleaned.append), \
                patch.object(pre_processing, "process") as process, \
                patch.object(
                    pre_processing.Helper,
                    "get_json_from_s3",
                    return_value=[{"text": "old text", "metadata": {"page": 1}}],
                ) as get_json, \
                patch.object(pre_processing, "aws_client", return_value=fake_s3):
            result = pre_processing.handler(_s3_event("common/shared.pdf"))

        process.assert_called_once_with("common/shared.pdf", namespace="attempt-stale")
        self.assertTrue(
            all(call.args[1].startswith("runs/attempt-stale/") for call in get_json.call_args_list)
        )
        fake_s3.put_object.assert_not_called()
        self.assertEqual(cleaned, ["attempt-stale"])
        self.assertEqual(result, {"status": "skipped", "stale": ["common/shared.pdf"]})

    def test_conditional_publish_aborts_when_conflict_reveals_newer_source_version(self):
        fake_s3 = MagicMock()
        fake_s3.head_object.return_value = {"ETag": '"etag-old"'}
        fake_s3.put_object.side_effect = ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")

        def fake_get_property(name, **_kw):
            return {"input_bucket_name": "in-bucket", "output_bucket": "post-bucket"}[name]

        # Pre-write check sees v1, concurrent v2 updates RAG_Output causing 412, post-conflict check sees v2.
        with patch.object(pre_processing.Helper, "get_property", side_effect=fake_get_property), \
                patch.object(
                    pre_processing.Helper,
                    "get_s3_object_version",
                    side_effect=["version:v1", "version:v2"],
                ), \
                patch.object(
                    pre_processing.Helper,
                    "get_json_from_s3",
                    return_value=[{"text": "v1 chunk", "metadata": {"page": 1}}],
                ), \
                patch.object(pre_processing, "aws_client", return_value=fake_s3):
            published = pre_processing.merge_outputs_to_rag_output(
                "common/shared.pdf",
                namespace=None,
                artifact_namespace="attempt-v1",
                expected_source_version="version:v1",
                input_bucket="in-bucket",
            )

        self.assertIsNone(published)
        fake_s3.put_object.assert_called_once()
        self.assertEqual(fake_s3.put_object.call_args.kwargs["IfMatch"], '"etag-old"')

    def test_skips_missing_common_source_before_bda(self):
        with patch.object(pre_processing.Helper, "get_property", return_value="in-bucket"), \
                patch.object(pre_processing.Helper, "get_s3_object_version", return_value=None), \
                patch.object(pre_processing, "process") as process:
            result = pre_processing.handler(_s3_event("common/deleted.pdf"))

        process.assert_not_called()
        self.assertEqual(result, {"status": "skipped", "stale": ["common/deleted.pdf"]})

    def test_cleanup_deletes_object_versions_and_markers_under_attempt_prefix(self):
        fake_bucket = MagicMock()
        fake_filtered = fake_bucket.object_versions.filter.return_value
        fake_filtered.delete.return_value = [{"Deleted": [{"Key": "runs/attempt-1/a", "VersionId": "v1"}]}]
        fake_resource = MagicMock()
        fake_resource.Bucket.return_value = fake_bucket
        fake_session = MagicMock()
        fake_session.resource.return_value = fake_resource

        with patch.object(pre_processing.Helper, "get_property", return_value="post-bucket"), \
                patch.object(pre_processing, "aws_session", return_value=fake_session):
            pre_processing._cleanup_attempt_artifacts("attempt-1")

        fake_resource.Bucket.assert_called_once_with("post-bucket")
        fake_bucket.object_versions.filter.assert_called_once_with(Prefix="runs/attempt-1/")
        fake_filtered.delete.assert_called_once_with()
        fake_bucket.objects.filter.assert_not_called()

    def test_delete_bucket_file_recursively_raises_on_partial_version_delete_errors(self):
        fake_bucket = MagicMock()
        fake_bucket.object_versions.filter.return_value.delete.return_value = [
            {"Errors": [{"Key": "runs/attempt-1/a", "VersionId": "v1", "Code": "AccessDenied"}]}
        ]
        fake_resource = MagicMock()
        fake_resource.Bucket.return_value = fake_bucket

        with self.assertRaises(RuntimeError):
            pre_processing.Helper.delete_bucket_file_recursively(
                fake_resource, "post-bucket", "runs/attempt-1/"
            )

    def test_namespaces_manifest_document_items_by_request_key(self):
        event = {
            "documentId": "DOC-001",
            "requestKey": "req-overlap-1",
            "s3": {"bucket": "in-bucket", "path": "NE/shared.pdf"},
        }
        with patch.object(pre_processing, "process") as process, \
                patch.object(pre_processing, "merge_outputs_to_rag_output", side_effect=rag_output_key) as merge:
            result = pre_processing.handler(event)

        process.assert_called_once_with("NE/shared.pdf", namespace="req-overlap-1")
        merge.assert_called_once_with("NE/shared.pdf", namespace="req-overlap-1")
        self.assertEqual(
            result,
            {"status": "completed", "rag_output": ["RAG_Output/runs/req-overlap-1/NE/shared.json"]},
        )

    def test_skips_events_without_documents(self):
        with patch.object(pre_processing, "process") as process:
            result = pre_processing.handler(_s3_event("NE/notes.txt"))

        self.assertEqual(result, {"status": "skipped"})
        process.assert_not_called()


class RagProcessHandlerTests(unittest.TestCase):
    def test_accepts_only_rag_output_json(self):
        self.assertTrue(rag_process.is_rag_output_key("RAG_Output/NE/a.json"))
        self.assertTrue(rag_process.is_rag_output_key("RAG_Output/runs/req-1/NE/a.json"))
        self.assertFalse(rag_process.is_rag_output_key("NE_bdaoutput/BDATextOutput/a-preprocessed-text.json"))
        self.assertFalse(rag_process.is_rag_output_key("RAG_Output/NE/a.txt"))

    def test_identifies_common_rag_output_keys(self):
        with patch.object(rag_process.Helper, "get_common_input_prefix", return_value="common/"):
            self.assertTrue(rag_process.is_common_rag_output_key("RAG_Output/common/42_cfr_438.json"))
            self.assertFalse(rag_process.is_common_rag_output_key("RAG_Output/runs/req-1/common/42_cfr_438.json"))
            self.assertFalse(rag_process.is_common_rag_output_key("RAG_Output/NE/My_File-v2.json"))

    def test_handler_embeds_only_common_rag_output_files(self):
        with patch.object(rag_process.Helper, "get_common_input_prefix", return_value="common/"), \
                patch.object(rag_process, "invoke_rag_process", new=AsyncMock(return_value={"failed": 0})) as invoke:
            result = rag_process.handler(
                _s3_event("RAG_Output/common/cfr_438.json", "RAG_Output/NE/a.json", "NE_bdaoutput/x.json")
            )

        invoke.assert_awaited_once_with("RAG_Output/common/cfr_438.json")
        self.assertEqual(
            result,
            {"status": "completed", "results": {"RAG_Output/common/cfr_438.json": {"failed": 0}}},
        )

    def test_handler_skips_non_common_keys(self):
        with patch.object(rag_process.Helper, "get_common_input_prefix", return_value="common/"), \
                patch.object(rag_process, "invoke_rag_process", new=AsyncMock()) as invoke:
            result = rag_process.handler(_s3_event("RAG_Output/NE/a.json", "NE_bdaoutput/x.json"))

        self.assertEqual(result, {"status": "skipped"})
        invoke.assert_not_called()


class S3ObjectVersionTests(unittest.TestCase):
    def test_prefers_s3_version_id_and_falls_back_to_etag_with_timestamp(self):
        ts = datetime(2026, 11, 27, 16, 19, 10, tzinfo=timezone.utc)
        self.assertEqual(
            s3_object_version({"VersionId": "v-123", "ETag": '"abc"', "LastModified": ts}),
            "version:v-123",
        )
        self.assertEqual(
            s3_object_version({"VersionId": "null", "ETag": '"abc"', "LastModified": ts}),
            'etag:"abc":2026-11-27T16:19:10+00:00',
        )
        with self.assertRaises(ValueError):
            s3_object_version({})

    def test_reads_json_with_version_and_handles_missing_head_object(self):
        s3 = MagicMock()
        s3.get_object.return_value = {
            "Body": io.BytesIO(b'[{"text": "hello"}]'),
            "VersionId": "ver-7",
        }
        self.assertEqual(
            read_s3_json_with_version(s3, "post-bucket", "RAG_Output/common/a.json"),
            ([{"text": "hello"}], "version:ver-7"),
        )

        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}},
            "HeadObject",
        )
        self.assertIsNone(head_s3_object_version(s3, "post-bucket", "RAG_Output/common/a.json"))

        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Denied"}},
            "HeadObject",
        )
        with self.assertRaises(ClientError):
            head_s3_object_version(s3, "post-bucket", "RAG_Output/common/a.json")


class _SharedAdvisoryStore:
    def __init__(self, initial_rows=None):
        self.committed_rows = list(initial_rows or [])
        self.events = []
        self._locks = {}

    def lock_for(self, key):
        return self._locks.setdefault(key, asyncio.Lock())


class _FakeTransactionalConnection:
    """In-memory connection that commits or rolls back DELETE + INSERT batches atomically."""

    def __init__(self, initial_rows=None, fail_on_executemany_call=None, shared_store=None):
        self._store = shared_store or _SharedAdvisoryStore(initial_rows)
        self._working_rows = None
        self._in_tx = False
        self._held_locks = []
        self.fail_on_executemany_call = fail_on_executemany_call
        self.executemany_calls = 0

    @property
    def committed_rows(self):
        return self._store.committed_rows

    @property
    def events(self):
        return self._store.events

    @asynccontextmanager
    async def transaction(self):
        self.events.append(("tx_begin",))
        self._in_tx = True
        self._working_rows = None
        try:
            yield self
        except BaseException:
            self._working_rows = None
            self._in_tx = False
            while self._held_locks:
                self._held_locks.pop().release()
            self.events.append(("tx_rollback",))
            raise
        if self._working_rows is not None:
            self._store.committed_rows = self._working_rows
        self._working_rows = None
        self._in_tx = False
        while self._held_locks:
            self._held_locks.pop().release()
        self.events.append(("tx_commit",))

    def _rows_for_write(self):
        if not self._in_tx:
            return self._store.committed_rows
        if self._working_rows is None:
            self._working_rows = list(self._store.committed_rows)
        return self._working_rows

    async def execute(self, sql, *args):
        if "pg_advisory_xact_lock" in sql:
            lock = self._store.lock_for(args[0])
            await lock.acquire()
            self._held_locks.append(lock)
            self.events.append(("xact_lock", self._in_tx, args[0]))
            return
        if sql.strip().upper().startswith("DELETE FROM"):
            target = json.loads(args[0])
            self.events.append(("delete", self._in_tx, target))
            rows = self._rows_for_write()
            rows[:] = [
                row for row in rows
                if not all(row["metadata"].get(k) == v for k, v in target.items())
            ]
            return
        raise AssertionError(f"Unexpected SQL: {sql}")

    async def executemany(self, sql, records):
        self.executemany_calls += 1
        self.events.append(("insert_batch", self._in_tx, len(records)))
        if self.fail_on_executemany_call == self.executemany_calls:
            raise RuntimeError(f"insert batch {self.executemany_calls} failed")
        rows = self._rows_for_write()
        for text, metadata_json, embedding in records:
            rows.append({"text": text, "metadata": json.loads(metadata_json), "embedding": embedding})


class InvokeRagProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_non_common_keys(self):
        with patch.object(rag_process.Helper, "get_common_input_prefix", return_value="common/"):
            with self.assertRaises(ValueError):
                await rag_process.invoke_rag_process("RAG_Output/NE/a.json")

    async def test_embeds_common_prefix_into_fixed_table(self):
        key = "RAG_Output/common/cfr_438.json"
        other_key = "RAG_Output/common/other.json"
        connection = _FakeTransactionalConnection(
            initial_rows=[
                {"text": "old reg chunk", "metadata": {"rag_output_key": key}, "embedding": np.array([0.0], dtype=np.float32)},
                {"text": "keep me", "metadata": {"rag_output_key": other_key}, "embedding": np.array([9.0], dtype=np.float32)},
            ]
        )
        bedrock_client = MagicMock()

        async def fake_batch_get_embeddings(texts, max_concurrent_requests=10):
            connection.events.append(("generate", list(texts)))
            return [np.array([1.0, 2.0], dtype=np.float32) for _ in texts]

        bedrock_client.batch_get_embeddings = AsyncMock(side_effect=fake_batch_get_embeddings)

        @asynccontextmanager
        async def fake_pooled_connection():
            yield connection

        def fake_head_version(bucket, checked_key):
            connection.events.append(("check_version", connection._in_tx, bucket, checked_key))
            return "version:v1"

        documents = [{"text": "reg chunk", "metadata": {"page": 2}}]

        with patch.object(rag_process.Helper, "get_property", return_value="post-bucket"), \
                patch.object(rag_process.Helper, "get_common_input_prefix", return_value="common/"), \
                patch.object(rag_process.Helper, "get_common_embeddings_table_name", return_value="embeddings_cms_regs"), \
                patch.object(rag_process.Helper, "get_json_with_version_from_s3", return_value=(documents, "version:v1")), \
                patch.object(rag_process.Helper, "get_s3_object_version", side_effect=fake_head_version), \
                patch.object(rag_process, "create_embedding_table", new=AsyncMock()) as create_table, \
                patch.object(rag_process, "pooled_connection", fake_pooled_connection), \
                patch.object(rag_process, "close_db", new=AsyncMock()) as close_db, \
                patch.object(rag_process, "EmbeddingProcessor", data_processing_embeddings.EmbeddingProcessor), \
                patch.object(data_processing_embeddings, "BedrockEmbeddingClient", return_value=bedrock_client):
            result = await rag_process.invoke_rag_process(key)

        self.assertEqual(result, {"total": 1, "postgres_processed": 1, "failed": 0, "errors": []})
        create_table.assert_awaited_once_with("embeddings_cms_regs")
        close_db.assert_awaited_once()
        self.assertEqual(
            [(r["text"], r["metadata"]) for r in connection.committed_rows],
            [
                ("keep me", {"rag_output_key": other_key}),
                ("reg chunk", {"page": 2, "rag_output_key": key}),
            ],
        )
        self.assertEqual(
            connection.events,
            [
                ("generate", ["reg chunk"]),
                ("tx_begin",),
                ("xact_lock", True, f"embeddings_cms_regs:{key}"),
                ("check_version", True, "post-bucket", key),
                ("delete", True, {"rag_output_key": key}),
                ("insert_batch", True, 1),
                ("tx_commit",),
            ],
        )

    async def test_stale_invocation_does_not_overwrite_newer_committed_rows(self):
        key = "RAG_Output/common/cfr_438.json"
        other_key = "RAG_Output/common/other.json"
        store = _SharedAdvisoryStore(
            initial_rows=[
                {"text": "v0 chunk", "metadata": {"rag_output_key": key}, "embedding": np.array([0.0], dtype=np.float32)},
                {"text": "untouched", "metadata": {"rag_output_key": other_key}, "embedding": np.array([9.0], dtype=np.float32)},
            ]
        )
        s3_state = {
            "documents": [{"text": "stale v1 chunk", "metadata": {"page": 1}}],
            "version": "version:v1",
        }
        v1_loaded = asyncio.Event()
        v2_committed = asyncio.Event()

        def fake_load_json_with_version(_bucket, _key):
            return list(s3_state["documents"]), s3_state["version"]

        def fake_head_version(_bucket, _key):
            return s3_state["version"]

        bedrock_client = MagicMock()

        async def fake_batch_get_embeddings(texts, max_concurrent_requests=10):
            if texts == ["stale v1 chunk"]:
                v1_loaded.set()
                await v2_committed.wait()
                return [np.array([1.0], dtype=np.float32)]
            return [np.array([2.0], dtype=np.float32) for _ in texts]

        bedrock_client.batch_get_embeddings = AsyncMock(side_effect=fake_batch_get_embeddings)

        @asynccontextmanager
        async def fake_pooled_connection():
            yield _FakeTransactionalConnection(shared_store=store)

        async def run_newer_after_v1_loads():
            await v1_loaded.wait()
            s3_state["documents"] = [{"text": "fresh v2 chunk", "metadata": {"page": 5}}]
            s3_state["version"] = "version:v2"
            try:
                return await rag_process.invoke_rag_process(key)
            finally:
                v2_committed.set()

        with patch.object(rag_process.Helper, "get_property", return_value="post-bucket"), \
                patch.object(rag_process.Helper, "get_common_input_prefix", return_value="common/"), \
                patch.object(rag_process.Helper, "get_common_embeddings_table_name", return_value="embeddings_cms_regs"), \
                patch.object(rag_process.Helper, "get_json_with_version_from_s3", side_effect=fake_load_json_with_version), \
                patch.object(rag_process.Helper, "get_s3_object_version", side_effect=fake_head_version), \
                patch.object(rag_process, "create_embedding_table", new=AsyncMock()), \
                patch.object(rag_process, "pooled_connection", fake_pooled_connection), \
                patch.object(rag_process, "close_db", new=AsyncMock()), \
                patch.object(rag_process, "EmbeddingProcessor", data_processing_embeddings.EmbeddingProcessor), \
                patch.object(data_processing_embeddings, "BedrockEmbeddingClient", return_value=bedrock_client):
            stale_result, fresh_result = await asyncio.gather(
                rag_process.invoke_rag_process(key),
                run_newer_after_v1_loads(),
            )

        self.assertEqual(fresh_result, {"total": 1, "postgres_processed": 1, "failed": 0, "errors": []})
        self.assertEqual(
            stale_result,
            {"total": 1, "postgres_processed": 0, "stale": True, "failed": 0, "errors": []},
        )
        self.assertEqual(
            [(r["text"], r["metadata"]) for r in store.committed_rows],
            [
                ("untouched", {"rag_output_key": other_key}),
                ("fresh v2 chunk", {"page": 5, "rag_output_key": key}),
            ],
        )

    async def test_deleted_source_under_lock_skips_replacement(self):
        key = "RAG_Output/common/cfr_438.json"
        existing_rows = [
            {"text": "existing reg chunk", "metadata": {"rag_output_key": key}, "embedding": np.array([1.0], dtype=np.float32)}
        ]
        connection = _FakeTransactionalConnection(initial_rows=existing_rows)
        bedrock_client = MagicMock()
        bedrock_client.batch_get_embeddings = AsyncMock(
            return_value=[np.array([2.0], dtype=np.float32)]
        )

        @asynccontextmanager
        async def fake_pooled_connection():
            yield connection

        with patch.object(rag_process.Helper, "get_property", return_value="post-bucket"), \
                patch.object(rag_process.Helper, "get_common_input_prefix", return_value="common/"), \
                patch.object(rag_process.Helper, "get_common_embeddings_table_name", return_value="embeddings_cms_regs"), \
                patch.object(rag_process.Helper, "get_json_with_version_from_s3", return_value=([{"text": "stale"}], "version:v1")), \
                patch.object(rag_process.Helper, "get_s3_object_version", return_value=None), \
                patch.object(rag_process, "create_embedding_table", new=AsyncMock()), \
                patch.object(rag_process, "pooled_connection", fake_pooled_connection), \
                patch.object(rag_process, "close_db", new=AsyncMock()), \
                patch.object(rag_process, "EmbeddingProcessor", data_processing_embeddings.EmbeddingProcessor), \
                patch.object(data_processing_embeddings, "BedrockEmbeddingClient", return_value=bedrock_client):
            result = await rag_process.invoke_rag_process(key)

        self.assertEqual(result, {"total": 1, "postgres_processed": 0, "stale": True, "failed": 0, "errors": []})
        self.assertEqual(connection.committed_rows, existing_rows)

    async def test_bedrock_failure_preserves_previous_rows_without_deleting(self):
        key = "RAG_Output/common/cfr_438.json"
        previous_rows = [
            {"text": "existing reg chunk", "metadata": {"rag_output_key": key}, "embedding": np.array([1.0], dtype=np.float32)}
        ]
        connection = _FakeTransactionalConnection(initial_rows=previous_rows)
        bedrock_client = MagicMock()
        bedrock_client.batch_get_embeddings = AsyncMock(
            side_effect=[
                [np.array([1.0], dtype=np.float32)],
                [RuntimeError("Bedrock throttled")],
            ]
        )

        @asynccontextmanager
        async def fake_pooled_connection():
            yield connection

        documents = [{"text": "chunk 1", "metadata": {}}, {"text": "chunk 2", "metadata": {}}]

        with patch.object(rag_process, "EMBED_BATCH_SIZE", 1), \
                patch.object(rag_process.Helper, "get_property", return_value="post-bucket"), \
                patch.object(rag_process.Helper, "get_common_input_prefix", return_value="common/"), \
                patch.object(rag_process.Helper, "get_common_embeddings_table_name", return_value="embeddings_cms_regs"), \
                patch.object(rag_process.Helper, "get_json_with_version_from_s3", return_value=(documents, "version:v1")), \
                patch.object(rag_process.Helper, "get_s3_object_version", return_value="version:v1"), \
                patch.object(rag_process, "create_embedding_table", new=AsyncMock()), \
                patch.object(rag_process, "pooled_connection", fake_pooled_connection), \
                patch.object(rag_process, "close_db", new=AsyncMock()) as close_db, \
                patch.object(rag_process, "EmbeddingProcessor", data_processing_embeddings.EmbeddingProcessor), \
                patch.object(data_processing_embeddings, "BedrockEmbeddingClient", return_value=bedrock_client):
            with self.assertRaises(RuntimeError):
                await rag_process.invoke_rag_process(key)

        close_db.assert_awaited_once()
        self.assertEqual(connection.events, [])
        self.assertEqual(connection.committed_rows, previous_rows)

    async def test_insert_failure_rolls_back_delete_and_earlier_batches(self):
        key = "RAG_Output/common/cfr_438.json"
        previous_rows = [
            {"text": "existing reg chunk", "metadata": {"rag_output_key": key}, "embedding": np.array([1.0], dtype=np.float32)}
        ]
        connection = _FakeTransactionalConnection(initial_rows=previous_rows, fail_on_executemany_call=2)
        bedrock_client = MagicMock()
        bedrock_client.batch_get_embeddings = AsyncMock(
            side_effect=lambda texts, max_concurrent_requests=10: [
                np.array([2.0], dtype=np.float32) for _ in texts
            ]
        )

        @asynccontextmanager
        async def fake_pooled_connection():
            yield connection

        documents = [{"text": "new chunk 1", "metadata": {}}, {"text": "new chunk 2", "metadata": {}}]

        with patch.object(rag_process, "EMBED_BATCH_SIZE", 1), \
                patch.object(rag_process.Helper, "get_property", return_value="post-bucket"), \
                patch.object(rag_process.Helper, "get_common_input_prefix", return_value="common/"), \
                patch.object(rag_process.Helper, "get_common_embeddings_table_name", return_value="embeddings_cms_regs"), \
                patch.object(rag_process.Helper, "get_json_with_version_from_s3", return_value=(documents, "version:v1")), \
                patch.object(rag_process.Helper, "get_s3_object_version", return_value="version:v1"), \
                patch.object(rag_process, "create_embedding_table", new=AsyncMock()), \
                patch.object(rag_process, "pooled_connection", fake_pooled_connection), \
                patch.object(rag_process, "close_db", new=AsyncMock()) as close_db, \
                patch.object(rag_process, "EmbeddingProcessor", data_processing_embeddings.EmbeddingProcessor), \
                patch.object(data_processing_embeddings, "BedrockEmbeddingClient", return_value=bedrock_client):
            with self.assertRaisesRegex(RuntimeError, "insert batch 2 failed"):
                await rag_process.invoke_rag_process(key)

        close_db.assert_awaited_once()
        self.assertEqual(connection.committed_rows, previous_rows)
        self.assertEqual(
            connection.events,
            [
                ("tx_begin",),
                ("xact_lock", True, f"embeddings_cms_regs:{key}"),
                ("delete", True, {"rag_output_key": key}),
                ("insert_batch", True, 1),
                ("insert_batch", True, 1),
                ("tx_rollback",),
            ],
        )


class EmbeddingProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_embeddings_fail_fast_stops_after_first_failed_batch(self):
        bedrock_client = MagicMock()
        bedrock_client.batch_get_embeddings = AsyncMock(
            return_value=[RuntimeError("throttled")]
        )
        with patch.object(data_processing_embeddings, "BedrockEmbeddingClient", return_value=bedrock_client):
            processor = data_processing_embeddings.EmbeddingProcessor(table_name="embeddings_cms_regs")
            records, status = await processor.generate_embeddings(
                [{"text": "first"}, {"text": "second"}],
                batch_size=1,
                fail_fast=True,
            )

        self.assertEqual(records, [])
        self.assertEqual(status["failed"], 1)
        bedrock_client.batch_get_embeddings.assert_awaited_once_with(["first"], max_concurrent_requests=10)

    async def test_insert_embeddings_uses_qualified_table_and_caller_connection(self):
        connection = MagicMock()
        connection.executemany = AsyncMock()
        with patch.object(data_processing_embeddings, "BedrockEmbeddingClient"):
            processor = data_processing_embeddings.EmbeddingProcessor(table_name="embeddings_cms_regs")
            records = [("hello", "{}", np.array([1.0, 2.0], dtype=np.float32))]
            inserted = await processor.insert_embeddings(connection, records, batch_size=30)

        self.assertEqual(inserted, 1)
        connection.executemany.assert_awaited_once()
        sql, batch = connection.executemany.call_args.args
        self.assertIn("INSERT INTO aisuite_schema.embeddings_cms_regs", sql)
        self.assertEqual(batch, records)


if __name__ == "__main__":
    unittest.main()
