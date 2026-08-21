"""Query-time lookup of the table-of-contents index built during pre-processing.

Chunk metadata only carries doc_id and a zero-based page, which is not something
a contract reviewer can act on. This turns that pair back into the citation they
expect - "V.R.11 Contingency Plan, printed page 169" - by locating the innermost
contents entry whose page range covers the chunk.
"""

from common.utils.helper import Helper
from common.utils.logger import log

_cached_index = None


class TableOfContents:
    """The contents index for every document under the active contract."""

    def __init__(self, documents=None):
        self.documents = {}
        for document in documents or []:
            doc_id = document.get("doc_id")
            if not doc_id:
                continue
            entries = sorted(
                (entry for entry in document.get("entries", []) if entry.get("start_page") is not None),
                key=lambda entry: (entry["start_page"], entry.get("level", 1)),
            )
            self.documents[doc_id] = {
                "entries": entries,
                "page_labels": document.get("page_labels") or {},
                "page_count": document.get("page_count"),
            }

    @property
    def available(self):
        return bool(self.documents)

    def _document_for(self, doc_id):
        if not doc_id:
            return None
        document = self.documents.get(doc_id)
        if document:
            return document
        # Chunk metadata and the contents index both derive doc_id from the S3
        # key, but a re-upload under a slightly different name should still land.
        wanted = simplify_doc_id(doc_id)
        for candidate, document in self.documents.items():
            if simplify_doc_id(candidate) == wanted:
                return document
        return None

    def printed_page(self, doc_id, page):
        document = self._document_for(doc_id)
        if not document or page is None:
            return None
        return document["page_labels"].get(str(page))

    def resolve(self, doc_id, page):
        """The innermost contents entry covering this page, or None."""
        document = self._document_for(doc_id)
        if not document or page is None:
            return None

        covering = [entry for entry in document["entries"]
                    if entry["start_page"] <= page <= entry.get("end_page", entry["start_page"])]
        if not covering:
            return None

        # Several entries can share a page. The one that starts latest and sits
        # deepest is the section the text on that page actually belongs to.
        innermost = max(covering, key=lambda entry: (entry["start_page"], entry.get("level", 1)))
        return {
            "path": innermost.get("path") or "",
            "title": innermost.get("title") or "",
            "breadcrumb": innermost.get("breadcrumb") or "",
            "level": innermost.get("level"),
            "section_start_page": innermost.get("start_page"),
            "section_end_page": innermost.get("end_page"),
            "printed_section_start": innermost.get("printed_start_page"),
            "printed_page": document["page_labels"].get(str(page)),
        }

    def citation(self, doc_id, page):
        """One line a reviewer can follow back into the PDF."""
        entry = self.resolve(doc_id, page)
        printed = self.printed_page(doc_id, page)
        location = printed or (f"page index {page}" if page is not None else "page unknown")
        if not entry:
            return f"{doc_id} ({location})"

        heading = " ".join(part for part in (entry["path"], entry["title"]) if part).strip()
        return f"{heading} - {doc_id} ({location})"


def load_table_of_contents(refresh=False):
    """Read the contents index from S3 once per process and hold onto it.

    A full workbook run asks for this hundreds of times, and it is a single small
    object that only changes when pre-processing runs again.
    """
    global _cached_index
    if _cached_index is not None and not refresh:
        return _cached_index

    try:
        bucket = Helper.get_property("output_bucket")
        folder = Helper.get_property("BDAToCOutputFolder")
        filename = Helper.get_property("BDAToCOutputFilename")
        key = f"{folder.rstrip('/')}/{filename}"

        documents = Helper.get_json_from_s3(bucket, key)
        _cached_index = TableOfContents(documents)
        log.info(f"load_table_of_contents() Loaded contents index for {len(_cached_index.documents)} "
                 f"document(s) from s3://{bucket}/{key}")
    except Exception as lclEx:
        # Provenance is an enrichment. Losing it must not stop a review, it just
        # means the output cites doc_id and page instead of a section.
        Helper.print_exception("load_table_of_contents", lclEx,
                               "Could not load the table-of-contents index; citations fall back to doc/page. "
                               "Run data_preprocessing.pre_processing to build it.")
        _cached_index = TableOfContents([])

    return _cached_index


def simplify_doc_id(doc_id):
    doc_id = (doc_id or "").lower()
    # Table chunks keep the ".pdf" that text chunks drop, so the extension has to
    # come off or every table in the contract loses its section citation.
    for extension in (".pdf", ".docx", ".doc"):
        if doc_id.endswith(extension):
            doc_id = doc_id[: -len(extension)]
            break
    return "".join(character for character in doc_id if character.isalnum())
