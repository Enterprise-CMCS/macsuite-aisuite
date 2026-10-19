import sys
import unittest
from pathlib import Path

_RAG_ROOT = Path(__file__).resolve().parents[1]
if str(_RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(_RAG_ROOT))

from data_embeddings_storage.database.recursive_text_splitter import (  # noqa: E402
    RecursiveCharacterTextSplitter,
    inline_lead_in,
    is_list_item,
    opening_lead_in,
    split_with_context,
)


def _text(text, subtype="paragraph", page=88, doc="ne"):
    return {"text": text, "metadata": {"doc_id": doc, "page": page, "element_type": "TEXT",
                                       "subtype": subtype}}


class SplitterTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(RecursiveCharacterTextSplitter(chunk_size=100).split_text("  One line.  "),
                         ["One line."])

    def test_empty_text(self):
        splitter = RecursiveCharacterTextSplitter()
        self.assertEqual(splitter.split_text(""), [])
        self.assertEqual(splitter.split_text("   "), [])

    def test_breaks_at_sentences_not_mid_word(self):
        text = "The Contractor shall notify the enrollee. " * 10
        chunks = RecursiveCharacterTextSplitter(chunk_size=100, chunk_overlap=0).split_text(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 100)
            self.assertTrue(chunk.endswith("enrollee."), chunk)

    def test_prefers_paragraph_breaks(self):
        first, second = "A" * 60 + " ends.", "B" * 60 + " ends."
        chunks = RecursiveCharacterTextSplitter(chunk_size=100, chunk_overlap=0).split_text(
            f"{first}\n\n{second}")
        self.assertEqual(chunks, [first, second])

    def test_overlap_carries_trailing_pieces(self):
        text = "Alpha one. Bravo two. Charlie three. Delta four."
        chunks = RecursiveCharacterTextSplitter(chunk_size=30, chunk_overlap=12).split_text(text)
        # "Bravo two. " (11) fits the 12-character overlap; "Charlie three. " (15) does not.
        self.assertEqual(chunks, ["Alpha one. Bravo two.", "Bravo two. Charlie three.", "Delta four."])

    def test_overlap_dropped_when_it_would_not_fit(self):
        text = "Alpha one. Bravo two. Charlie three. Delta four."
        chunks = RecursiveCharacterTextSplitter(chunk_size=25, chunk_overlap=12).split_text(text)
        self.assertEqual(chunks, ["Alpha one. Bravo two.", "Charlie three.", "Delta four."])

    def test_unbroken_run_is_sliced(self):
        chunks = RecursiveCharacterTextSplitter(chunk_size=10, chunk_overlap=0).split_text("x" * 25)
        self.assertEqual([len(chunk) for chunk in chunks], [10, 10, 5])

    def test_nothing_is_lost(self):
        text = "Section 4. The Contractor must; comply, with all. " * 40
        chunks = RecursiveCharacterTextSplitter(chunk_size=120, chunk_overlap=0).split_text(text)
        self.assertEqual("".join(chunks).replace(" ", ""), text.replace(" ", ""))


class LeadInTests(unittest.TestCase):
    def test_list_items(self):
        for text in ("xxii. Information about EPSDT", "(a) Notice", "- item", "3. Item", "• item"):
            self.assertTrue(is_list_item(text, "paragraph"), text)
        self.assertTrue(is_list_item("Anything", "list"))
        self.assertFalse(is_list_item("4.2.1 The Contractor shall", "paragraph"))
        self.assertFalse(is_list_item("The Contractor shall", "paragraph"))

    def test_opening_lead_in_is_the_last_sentence(self):
        text = "This section covers handbooks. The member handbook must include the following:"
        self.assertEqual(opening_lead_in(text), "The member handbook must include the following:")
        self.assertEqual(opening_lead_in("No list follows."), "")

    def test_cfr_cite_is_not_a_sentence_break(self):
        text = "Per 42 C.F.R. § 438.10(g), the handbook must include:"
        self.assertEqual(opening_lead_in(text), text)

    def test_inline_lead_in(self):
        self.assertEqual(inline_lead_in("The notice must state: (a) the reason (b) the date"),
                         "The notice must state:")
        self.assertEqual(inline_lead_in("Note: this is prose."), "")


class SplitWithContextTests(unittest.TestCase):
    def setUp(self):
        self.splitter = RecursiveCharacterTextSplitter(chunk_size=200, chunk_overlap=0)

    def test_list_items_carry_heading_and_lead_in(self):
        docs = [
            _text("## 4.7 Member Handbook", subtype="section_title"),
            _text("The member handbook must include the following:"),
            _text("xxi. Information about grievances"),
            _text("xxii. Information about the EPSDT program"),
        ]
        chunks = [doc["text"] for doc in split_with_context(docs, self.splitter)]
        self.assertEqual(chunks[0], "## 4.7 Member Handbook")
        self.assertEqual(chunks[1], "4.7 Member Handbook\nThe member handbook must include the following:")
        self.assertEqual(chunks[3], "4.7 Member Handbook\nThe member handbook must include the following:\n"
                                    "xxii. Information about the EPSDT program")

    def test_a_paragraph_closes_the_list(self):
        docs = [
            _text("The handbook must include:"),
            _text("i. Item one"),
            _text("The Contractor shall also maintain a website."),
            _text("ii. Unrelated item"),
        ]
        chunks = [doc["text"] for doc in split_with_context(docs, self.splitter)]
        self.assertEqual(chunks[1], "The handbook must include:\ni. Item one")
        self.assertEqual(chunks[3], "ii. Unrelated item")

    def test_later_slices_of_a_long_list_keep_its_lead_in(self):
        items = "\n".join(f"({letter}) the enrollee's right number {letter} under the contract"
                          for letter in "abcdefgh")
        docs = [_text(f"The notice must state:\n{items}")]
        chunks = [doc["text"] for doc in split_with_context(docs, self.splitter)]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(chunks[0].startswith("The notice must state:\n(a)"))
        for chunk in chunks[1:]:
            self.assertTrue(chunk.startswith("The notice must state:\n("), chunk)

    def test_context_resets_at_a_new_document(self):
        docs = [
            _text("Section One", subtype="title", doc="first"),
            _text("The handbook must include:", doc="first"),
            _text("i. Item", doc="second"),
        ]
        self.assertEqual(split_with_context(docs, self.splitter)[-1]["text"], "i. Item")

    def test_other_elements_pass_through_and_metadata_is_kept(self):
        table = {"text": "| a | b |", "metadata": {"element_type": "TABLE"}}
        docs = [_text("Heading", subtype="title"), table, _text("Body text.", page=5)]
        split = split_with_context(docs, self.splitter)
        self.assertIs(split[1], table)
        self.assertEqual(split[2]["text"], "Heading\nBody text.")
        self.assertEqual(split[2]["metadata"]["page"], 5)

    def test_summary_neither_gets_nor_sets_context(self):
        docs = [
            _text("Heading", subtype="title"),
            _text("# Title\nA contract", subtype="summary+descriptionofdocument", page=None),
            _text("Body text."),
        ]
        chunks = [doc["text"] for doc in split_with_context(docs, self.splitter)]
        self.assertEqual(chunks[1], "# Title\nA contract")
        self.assertEqual(chunks[2], "Heading\nBody text.")


if __name__ == "__main__":
    unittest.main()
