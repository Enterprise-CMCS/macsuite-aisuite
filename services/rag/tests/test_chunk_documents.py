"""Tests for document splitting and chunk provenance metadata."""

import sys
import unittest
from pathlib import Path

_RAG_ROOT = Path(__file__).resolve().parents[1]
if str(_RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(_RAG_ROOT))

from data_embeddings_storage.database.chunk_documents import (  # noqa: E402
    CHUNKING_VERSION,
    split_documents,
)
from data_embeddings_storage.database.recursive_text_splitter import (  # noqa: E402
    RecursiveCharacterTextSplitter,
)


class ChunkDocumentsTests(unittest.TestCase):
    def test_text_chunks_receive_per_document_provenance(self):
        table_doc = {
            "text": "Header | Value",
            "metadata": {"element_type": "TABLE", "source": "table-1"},
        }
        documents = [
            {
                "text": "Alpha beta gamma delta epsilon zeta.",
                "metadata": {
                    "element_type": "TEXT",
                    "source": "contract.pdf",
                    "page": 7,
                },
            },
            table_doc,
        ]
        splitter = RecursiveCharacterTextSplitter(chunk_size=15, chunk_overlap=0)

        results = split_documents(documents, splitter)
        text_chunks = [
            result
            for result in results
            if result.get("metadata", {}).get("element_type") == "TEXT"
        ]

        self.assertGreater(len(text_chunks), 1)
        self.assertEqual(
            [chunk["metadata"]["chunk_index"] for chunk in text_chunks],
            list(range(len(text_chunks))),
        )
        for chunk in text_chunks:
            metadata = chunk["metadata"]
            self.assertEqual(metadata["chunk_count"], len(text_chunks))
            self.assertEqual(metadata["chunking_version"], CHUNKING_VERSION)
            self.assertTrue(metadata["chunking_version"])
            self.assertEqual(metadata["source"], "contract.pdf")
            self.assertEqual(metadata["page"], 7)

        self.assertIs(results[-1], table_doc)
        for key in ("chunk_index", "chunk_count", "chunking_version"):
            self.assertNotIn(key, results[-1]["metadata"])

    def test_empty_text_document_emits_no_chunk(self):
        splitter = RecursiveCharacterTextSplitter(chunk_size=15, chunk_overlap=0)
        table_doc = {"text": "table", "metadata": {"element_type": "TABLE"}}

        results = split_documents(
            [
                {"text": " \n\t", "metadata": {"element_type": "TEXT"}},
                table_doc,
            ],
            splitter,
        )

        self.assertEqual(results, [table_doc])


if __name__ == "__main__":
    unittest.main()
