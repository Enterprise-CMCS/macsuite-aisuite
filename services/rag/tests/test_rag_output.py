"""Unit tests for the S3-triggered pre-processing -> RAG_Output -> embeddings flow (no AWS or Postgres)."""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote_plus

RAG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if RAG_ROOT not in sys.path:
    sys.path.insert(0, RAG_ROOT)

# These modules call AWS or Postgres at import time. patch.dict removes the stubs again after import.
_IMPORT_STUBS = {
    name: MagicMock()
    for name in (
        "asyncpg",
        "structlog",
        "data_embeddings_storage.database.data_processing_embeddings",
        "data_embeddings_storage.database.table_setup",
        "data_preprocessing.bedrock.bedrock_BDA",
        "data_preprocessing.parsing.parsed_bda_table",
        "data_preprocessing.parsing.parsed_images",
        "data_preprocessing.parsing.parsed_text_data",
    )
}
with patch.dict(sys.modules, _IMPORT_STUBS):
    from common.utils.aws_files_access import object_keys
    from data_embeddings_storage import rag_process
    from data_preprocessing import pre_processing
    from data_preprocessing.bedrock.bda_results import rag_output_key


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


class ObjectKeysTests(unittest.TestCase):
    def test_decodes_s3_notification_keys(self):
        event = _s3_event("folder one/My File(1).pdf")
        event["Records"].append({"eventSource": "aws:sqs"})

        self.assertEqual(object_keys(event), ["folder one/My File(1).pdf"])

    def test_event_without_records(self):
        self.assertEqual(object_keys({}), [])


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
        self.assertEqual(documents[0]["metadata"], {"element_type": "TEXT", "page": 1})


class PreProcessingHandlerTests(unittest.TestCase):
    def test_processes_each_pdf_or_docx_key(self):
        with patch.object(pre_processing, "process") as process, \
                patch.object(pre_processing, "merge_outputs_to_rag_output", side_effect=rag_output_key):
            result = pre_processing.handler(_s3_event("NE/a b.pdf", "NE/c.DOCX", "NE/notes.txt"))

        self.assertEqual([call.args[0] for call in process.call_args_list], ["NE/a b.pdf", "NE/c.DOCX"])
        self.assertEqual(
            result,
            {"status": "completed", "rag_output": ["RAG_Output/NE/a_b.json", "RAG_Output/NE/c.json"]},
        )

    def test_skips_events_without_documents(self):
        with patch.object(pre_processing, "process") as process:
            result = pre_processing.handler(_s3_event("NE/notes.txt"))

        self.assertEqual(result, {"status": "skipped"})
        process.assert_not_called()


class RagProcessHandlerTests(unittest.TestCase):
    def test_accepts_only_rag_output_json(self):
        self.assertTrue(rag_process.is_rag_output_key("RAG_Output/NE/a.json"))
        self.assertFalse(rag_process.is_rag_output_key("NE_bdaoutput/BDATextOutput/a-preprocessed-text.json"))
        self.assertFalse(rag_process.is_rag_output_key("RAG_Output/NE/a.txt"))

    def test_embeddings_table_per_source_file(self):
        self.assertEqual(rag_process.embeddings_table_for_key("RAG_Output/NE/My_File-v2.json"), "emb_my_file_v2")

        long_name = rag_process.embeddings_table_for_key("RAG_Output/" + "a" * 80 + ".json")
        self.assertEqual(len(long_name), rag_process.MAX_TABLE_NAME_LEN)
        self.assertTrue(long_name.startswith("emb_a"))

    def test_handler_embeds_each_rag_output_file(self):
        with patch.object(rag_process, "invoke_rag_process", new=AsyncMock(return_value={"failed": 0})) as invoke:
            result = rag_process.handler(_s3_event("RAG_Output/NE/a.json", "NE_bdaoutput/x.json"))

        invoke.assert_awaited_once_with("RAG_Output/NE/a.json")
        self.assertEqual(result, {"status": "completed", "results": {"RAG_Output/NE/a.json": {"failed": 0}}})

    def test_handler_skips_other_keys(self):
        with patch.object(rag_process, "invoke_rag_process", new=AsyncMock()) as invoke:
            result = rag_process.handler(_s3_event("NE_bdaoutput/x.json"))

        self.assertEqual(result, {"status": "skipped"})
        invoke.assert_not_called()


class InvokeRagProcessTests(unittest.IsolatedAsyncioTestCase):
    async def _invoke(self, status):
        processor = MagicMock()
        processor.process_data = AsyncMock(return_value=status)
        documents = [{"text": "chunk", "metadata": {}}, {"text": ""}, "junk"]
        with patch.object(rag_process.Helper, "get_property", return_value="post-bucket"), \
                patch.object(rag_process.Helper, "get_json_from_s3", return_value=documents) as get_json, \
                patch.object(rag_process, "create_embedding_table", new=AsyncMock()) as create_table, \
                patch.object(rag_process, "EmbeddingProcessor", return_value=processor) as processor_cls:
            try:
                return await rag_process.invoke_rag_process("RAG_Output/NE/a.json")
            finally:
                get_json.assert_called_once_with("post-bucket", "RAG_Output/NE/a.json")
                create_table.assert_awaited_once_with("emb_a")
                processor_cls.assert_called_once_with(table_name="emb_a")
                processor.process_data.assert_awaited_once_with([{"text": "chunk", "metadata": {}}], batch_size=30)

    async def test_embeds_combined_documents_into_per_file_table(self):
        status = {"total": 1, "postgres_processed": 1, "failed": 0, "errors": []}

        self.assertEqual(await self._invoke(status), status)

    async def test_raises_when_any_embedding_fails(self):
        with self.assertRaises(RuntimeError):
            await self._invoke({"total": 1, "postgres_processed": 0, "failed": 1, "errors": ["boom"]})


if __name__ == "__main__":
    unittest.main()
