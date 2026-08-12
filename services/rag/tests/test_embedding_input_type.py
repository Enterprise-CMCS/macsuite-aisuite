"""Request-shape tests for search and ingest embedding clients."""

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from _stubs import install_offline_stubs


install_offline_stubs()

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from data_embeddings_storage.database.embeddings_client import (  # noqa: E402
    BedrockEmbeddingClient as IngestEmbeddingClient,
)
from search.database_searching.aws_embedding_client import (  # noqa: E402
    BedrockEmbeddingClient as SearchEmbeddingClient,
)


class EmbeddingInputTypeTests(unittest.TestCase):
    @staticmethod
    def _request_body(client_class):
        client = client_class()
        client.client = MagicMock()
        client.client.invoke_model.return_value = {
            "body": io.BytesIO(b'{"embeddings": [[0.1, 0.2]]}')
        }

        client.invoke_model_sync("hello")

        return json.loads(client.client.invoke_model.call_args.kwargs["body"])

    def test_search_embedding_uses_search_query_input_type(self):
        request_body = self._request_body(SearchEmbeddingClient)

        self.assertEqual(request_body["input_type"], "search_query")

    def test_ingest_embedding_uses_search_document_input_type(self):
        request_body = self._request_body(IngestEmbeddingClient)

        self.assertEqual(request_body["input_type"], "search_document")


if __name__ == "__main__":
    unittest.main()
