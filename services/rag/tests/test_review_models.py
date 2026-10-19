import sys
import unittest
from pathlib import Path

_RAG_ROOT = Path(__file__).resolve().parents[1]
if str(_RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(_RAG_ROOT))

from search.database_searching.review_models import (  # noqa: E402
    EvidenceRecord,
    RequirementReview,
    adjoins,
    best_quoted,
    merge_evidence,
    neighbour_ids,
    page_label,
    page_reference,
    quote_score,
    quote_supported,
    with_neighbours,
)

QUOTE = ("The Contractor must notify the enrollee in writing of the disenrollment decision "
         "within five business days")

EXACT = ("Section 4.2 Disenrollment. The Contractor must notify the enrollee in writing of the "
         "disenrollment decision within five business days of receiving the request.")

# Same provision restated elsewhere with a different deadline - close enough to
# clear the threshold, which is how a quote used to land on the wrong page.
NEAR = ("Section 2.1 Overview. The Contractor must notify the enrollee in writing of the "
        "disenrollment decision within ten business days of receiving the request.")

UNRELATED = "Capitation payments are made monthly in arrears."


class PageReferenceTests(unittest.TestCase):
    def test_printed_number_differs_from_pdf_position(self):
        self.assertEqual(page_reference(13, "xiv"), "xiv (PDF p. 14)")
        self.assertEqual(page_label(51, "31"), "page 31 (PDF p. 52)")

    def test_printed_number_matches_pdf_position(self):
        self.assertEqual(page_label(8, "9"), "page 9")

    def test_no_printed_number_falls_back_to_pdf_position(self):
        self.assertEqual(page_label(51, ""), "page 52")
        self.assertEqual(page_label(51, None), "page 52")

    def test_printed_number_without_position(self):
        self.assertEqual(page_label(None, "Page 7"), "page 7")

    def test_nothing_recorded(self):
        self.assertEqual(page_label(None, ""), "")


class ReviewerTermsTests(unittest.TestCase):
    def _review(self):
        return RequirementReview(requirement="r", evidence=[
            EvidenceRecord(quote="a", chunk_id=3566, page=51, printed_page="31"),
            EvidenceRecord(quote="b", chunk_id=3733, page=53, printed_page="33"),
        ])

    def test_chunk_ids_become_both_page_numbers(self):
        self.assertEqual(
            self._review().in_reviewer_terms("The text in chunk 3566 states"),
            "The text on page 31 (PDF p. 52) states",
        )

    def test_where_found_and_page_numbers_carry_both(self):
        review = self._review()
        self.assertEqual(review.where_found(), "page 31 (PDF p. 52) | page 33 (PDF p. 54)")
        self.assertEqual(review.page_numbers(), "31 (PDF p. 52), 33 (PDF p. 54)")


class StatusTests(unittest.TestCase):
    def test_na_is_a_status(self):
        self.assertEqual(RequirementReview(requirement="r", status="N/A").status, "N/A")

    def test_legacy_unclear_still_loads(self):
        self.assertEqual(RequirementReview(requirement="r", status="UNCLEAR").status, "Unsure")


class QuoteScoreTests(unittest.TestCase):
    def test_verbatim_scores_one(self):
        self.assertEqual(quote_score(QUOTE, EXACT), 1.0)

    def test_near_duplicate_clears_threshold_but_scores_lower(self):
        self.assertTrue(quote_supported(QUOTE, NEAR))
        self.assertLess(quote_score(QUOTE, NEAR), 1.0)

    def test_unrelated_text_is_not_supported(self):
        self.assertFalse(quote_supported(QUOTE, UNRELATED))

    def test_empty_inputs(self):
        self.assertEqual(quote_score(QUOTE, ""), 0.0)
        self.assertEqual(quote_score("", EXACT), 0.0)

    def test_elided_quote_takes_its_weakest_fragment(self):
        quote = ("The Contractor must notify the enrollee in writing ... "
                 "capitation payments are withheld for twelve months")
        self.assertFalse(quote_supported(quote, EXACT))


class BestQuotedTests(unittest.TestCase):
    def test_exact_match_beats_earlier_near_duplicate(self):
        near = {"id": 1, "text": NEAR}
        exact = {"id": 2, "text": EXACT}
        self.assertIs(best_quoted(QUOTE, [near, exact]), exact)

    def test_ties_go_to_the_earlier_chunk(self):
        first = {"id": 1, "text": EXACT}
        second = {"id": 2, "text": EXACT}
        self.assertIs(best_quoted(QUOTE, [first, second]), first)

    def test_near_duplicate_used_when_nothing_closer(self):
        near = {"id": 1, "text": NEAR}
        self.assertIs(best_quoted(QUOTE, [{"id": 0, "text": UNRELATED}, near]), near)

    def test_nothing_carries_the_quote(self):
        self.assertIsNone(best_quoted(QUOTE, [{"id": 0, "text": UNRELATED}]))
        self.assertIsNone(best_quoted(QUOTE, []))


# The lead-in ends one chunk and its first list item starts the next - the
# shape of the NE "for cause, at any time" quote that failed verification.
LEAD_IN = "c. A member may initiate a request for disenrollment for cause, at any time, under any one of the following circumstances:"
LIST_ITEM = "i. The MCO does not, because of moral or religious objections, cover the service the member seeks."
SPANNING = ("A member may initiate a request for disenrollment for cause, at any time, under any one of "
            "the following circumstances: The MCO does not, because of moral or religious objections, "
            "cover the service the member seeks")


def _text_chunk(chunk_id, text, page=53, doc="ne.pdf", element_type="TEXT"):
    return {"id": chunk_id, "text": text,
            "metadata": {"doc_id": doc, "page": page, "element_type": element_type}}


class SpanningQuoteTests(unittest.TestCase):
    def test_quote_across_adjoining_chunks_goes_to_the_first(self):
        lead, item = _text_chunk(40, LEAD_IN), _text_chunk(41, LIST_ITEM)
        self.assertIs(best_quoted(SPANNING, [item, lead]), lead)

    def test_single_chunk_match_is_preferred(self):
        lead, item = _text_chunk(40, LEAD_IN), _text_chunk(41, LIST_ITEM)
        self.assertIs(best_quoted(LIST_ITEM, [lead, item]), item)

    def test_chunks_that_do_not_run_on_are_not_joined(self):
        lead = _text_chunk(40, LEAD_IN)
        self.assertIsNone(best_quoted(SPANNING, [lead, _text_chunk(42, LIST_ITEM)]))
        self.assertIsNone(best_quoted(SPANNING, [lead, _text_chunk(41, LIST_ITEM, doc="other.pdf")]))
        self.assertIsNone(best_quoted(SPANNING, [lead, _text_chunk(41, LIST_ITEM, page=90)]))


class MergeEvidenceTests(unittest.TestCase):
    def test_order_kept_and_repeats_dropped(self):
        first = [EvidenceRecord(quote="The MCO must accept members.", verified=True)]
        second = [EvidenceRecord(quote="the MCO must  accept members", verified=True),
                  EvidenceRecord(quote="Another provision.", verified=True)]
        merged = merge_evidence(first, second)
        self.assertEqual([record.quote for record in merged],
                         ["The MCO must accept members.", "Another provision."])

    def test_verified_copy_replaces_unverified_in_place(self):
        unverified = EvidenceRecord(quote="Each year is a performance year.", verified=False)
        other = EvidenceRecord(quote="Other text.", verified=True)
        verified = EvidenceRecord(quote="Each year is a performance year", verified=True, chunk_id=7)
        merged = merge_evidence([unverified, other], [verified])
        self.assertEqual([record.chunk_id for record in merged], [7, None])
        self.assertTrue(all(record.verified for record in merged))

    def test_nothing_to_merge(self):
        self.assertEqual(merge_evidence([], []), [])


def _chunk(chunk_id, page, doc="ne.pdf", element_type="TEXT"):
    return {"id": chunk_id, "text": f"chunk {chunk_id}",
            "metadata": {"doc_id": doc, "page": page, "element_type": element_type}}


class NeighbourTests(unittest.TestCase):
    def test_ids_either_side_without_the_hits(self):
        self.assertEqual(neighbour_ids([10, 11, 20]), [9, 12, 19, 21])
        self.assertEqual(neighbour_ids([10], span=2), [8, 9, 11, 12])
        self.assertEqual(neighbour_ids([]), [])

    def test_adjoins_same_document_type_and_nearby_page(self):
        self.assertTrue(adjoins(_chunk(10, 88), _chunk(11, 89)))
        self.assertFalse(adjoins(_chunk(10, 88), _chunk(11, 91)))
        self.assertFalse(adjoins(_chunk(10, 88), _chunk(11, 88, doc="other.pdf")))
        self.assertFalse(adjoins(_chunk(10, 88), _chunk(11, 88, element_type="TABLE")))
        self.assertFalse(adjoins(_chunk(10, None), _chunk(11, None)))

    def test_neighbours_sit_either_side_of_their_hit(self):
        hit, other = _chunk(10, 88), _chunk(50, 120)
        rows = [_chunk(9, 88), _chunk(11, 89)]
        expanded = with_neighbours([hit, other], rows, expand=[10])
        self.assertEqual([chunk["id"] for chunk in expanded], [9, 10, 11, 50])

    def test_only_expanded_hits_get_neighbours(self):
        expanded = with_neighbours([_chunk(10, 88)], [_chunk(9, 88)], expand=[])
        self.assertEqual([chunk["id"] for chunk in expanded], [10])

    def test_non_adjoining_rows_are_left_out(self):
        rows = [_chunk(9, 40), _chunk(11, 88, element_type="TABLE")]
        expanded = with_neighbours([_chunk(10, 88)], rows, expand=[10])
        self.assertEqual([chunk["id"] for chunk in expanded], [10])

    def test_a_gap_is_not_bridged(self):
        rows = [_chunk(9, 10), _chunk(8, 88)]
        expanded = with_neighbours([_chunk(10, 88)], rows, expand=[10], span=2)
        self.assertEqual([chunk["id"] for chunk in expanded], [10])

    def test_shared_neighbour_and_neighbouring_hits_are_not_repeated(self):
        hits = [_chunk(10, 88), _chunk(12, 88), _chunk(11, 88)]
        rows = [_chunk(9, 88), _chunk(11, 88), _chunk(13, 88)]
        expanded = with_neighbours(hits, rows, expand=[10, 12])
        self.assertEqual([chunk["id"] for chunk in expanded], [9, 10, 12, 13, 11])


if __name__ == "__main__":
    unittest.main()
