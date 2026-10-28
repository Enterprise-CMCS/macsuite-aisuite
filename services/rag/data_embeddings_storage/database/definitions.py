"""
Find the contract's glossary / definitions section in the BDA output and turn it
into one chunk per defined term, tagged in metadata:

    metadata["section_type"] = "definition"
    metadata["term"]         = "Grievance"

Contracts without such a section are returned unchanged.
Call once in rag_process.py, after the data is combined and before splitting.
"""
import re

# "|----|------|" separator line under a markdown table header
_SEPARATOR = re.compile(r"^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*(:?-{3,}:?)?\s*\|?\s*$")


def _squeeze_row(line):
    """One markdown table row with BDA's padding spaces removed (None for separator rows)."""
    if _SEPARATOR.match(line):
        return None
    cells = [re.sub(r"\s+", " ", cell).strip() for cell in line.strip().strip("|").split("|")]
    return "| " + " | ".join(cells) + " |"

# Section headings states use for their definitions.
DEFINITIONS_HEADING = re.compile(
    r"^(?:[A-Z0-9.]+\s+)?(GLOSSARY(?:\s+OF\s+TERMS)?|DEFINITIONS|TERMS\s+AND\s+DEFINITIONS|DEFINED\s+TERMS)$",
    re.I)

# "**Grievance:** A written ..." or "Grievance: A written ..." at the start of a line.
TERM_LINE = re.compile(r"^\*{0,2}(?P<term>[A-Z][^:*\n]{0,90}?)\s*:\s*\*{0,2}(?:\s+|$)")

# Words that end a sentence lead-in rather than name a term ("... identified through:").
_LEAD_IN_WORDS = {"can", "be", "is", "are", "will", "may", "must", "shall", "through",
                  "include", "includes", "including", "following"}

HEADING_SUBTYPES = ("title", "section_header")

# Fewer terms than this and the section is left untouched (it is probably not a
# "Term: definition" glossary, or the heading matched something else).
MIN_DEFINITIONS = 3

def _plain(text):
    return re.sub(r"[#*_]", "", text or "").strip()

def _split_prefix(text):
    """BDA puts the section heading on the first line of each element. Return (prefix, body)."""
    first, newline, rest = (text or "").partition("\n")
    return (first, rest) if newline and rest.strip() else ("", text or "")

def _term_of(body):
    first_line = body.strip().split("\n", 1)[0]
    match = TERM_LINE.match(first_line)
    if not match:
        return None
    term = match.group("term").strip()
    words = term.split()
    if not (0 < len(words) <= 10) or term.endswith("."):
        return None
    rest_of_line = first_line[match.end():].strip()
    if not rest_of_line and any(word.lower() in _LEAD_IN_WORDS for word in words):
        return None                                   # "... can be identified through:" + list
    return term

def _is_definitions_heading(doc):
    meta = doc.get("metadata", {})
    return (meta.get("element_type") == "TEXT"
            and meta.get("subtype") in HEADING_SUBTYPES
            and bool(DEFINITIONS_HEADING.match(_plain(doc.get("text")))))

def _ends_section(doc, start_subtype):
    """A new heading at the same or a higher level that is not itself a definition."""
    meta = doc.get("metadata", {})
    if meta.get("element_type") != "TEXT":
        return False
    subtype = meta.get("subtype")
    levels = ("title",) if start_subtype == "title" else HEADING_SUBTYPES
    return subtype in levels and _term_of(_plain(doc.get("text"))) is None


def _table_rows(text):
    """Table cells -> plain text lines; drops margin comments ('Commented [RK1]: ...')."""
    lines = []
    for raw in text.splitlines():
        row = _squeeze_row(raw) if raw.strip() else None
        if not row:
            continue
        cells = [cell.strip() for cell in row.strip("| ").split(" | ")]
        cells = [cell for cell in cells if cell and not cell.startswith("Commented")]
        if cells:
            lines.append(" ".join(cells))
    return lines


def tag_definitions(docs):
    """Return docs with the definitions section rebuilt as one tagged chunk per term."""
    start = next((i for i, doc in enumerate(docs) if _is_definitions_heading(doc)), None)
    # no glossary: nothing changes
    if start is None:
        return docs                                            

    heading = _plain(docs[start]["text"])
    start_subtype = docs[start]["metadata"].get("subtype")
    end = next((i for i in range(start + 1, len(docs)) if _ends_section(docs[i], start_subtype)), len(docs))

    definitions = []                                           # one dict per term
    leading = []                                               # section text before the first term
    def add_text(text, meta):
        text = text.strip()
        if not text:
            return
        term = _term_of(text)
        if term and not (definitions and definitions[-1]["term"] == term):
            definitions.append({"term": term, "lines": [text], "meta": meta})
        elif not definitions:                                  # text before the first term: keep it
            leading.append({"text": text, "metadata": meta})
        elif definitions:                                      # continuation (list, next page)
            current = definitions[-1]["lines"]
            seen = {_plain(line) for block in current for line in block.split("\n")}
            new_lines = [line for line in text.split("\n") if _plain(line) not in seen]
            if new_lines:                                      # BDA often repeats the lead-in line
                current.append("\n".join(new_lines))

    # BDA lists tables after all text, so selecttables on the section's pages separately.
    section = docs[start + 1:end]
    pages = [doc.get("metadata", {}).get("page") for doc in section]
    pages = [page for page in pages if isinstance(page, int)]
    first_page, last_page = (min(pages), max(pages)) if pages else (None, None)
    tables = [i for i, doc in enumerate(docs)
              if not (start < i < end)
              and doc.get("metadata", {}).get("element_type") == "TABLE"
              and isinstance(doc.get("metadata", {}).get("page"), int)
              and first_page is not None and first_page <= doc["metadata"]["page"] <= last_page]
    section = sorted(section + [docs[i] for i in tables],
                     key=lambda doc: doc.get("metadata", {}).get("page") or 0)   # stable: keeps text order

    for doc in section:
        meta = doc.get("metadata", {})
        if meta.get("element_type") == "TABLE":
            for line in _table_rows(doc.get("text", "")):     # e.g. a glossary page BDA read as a table
                add_text(line, meta)
        elif meta.get("element_type") == "TEXT":
            _, body = _split_prefix(doc.get("text", ""))
            add_text(body, meta)

    # a heading was found but the entries are not in "Term: definition" form
    # Keep the section exactly as it was.
    if len(definitions) < MIN_DEFINITIONS:
        return docs

    tagged = []
    for definition in definitions:
        body = "\n".join(dict.fromkeys(definition["lines"]))  # drop exact repeats BDA sometimes makes
        tagged.append({
            "text": f"{heading}\n{body}",
            "metadata": {**definition["meta"], "element_type": "TEXT", "subtype": "definition",
                         "section_type": "definition", "term": definition["term"].replace("*", "").strip()},
        })

    moved = set(tables)
    after = [doc for i, doc in enumerate(docs) if i >= end and i not in moved]
    return docs[:start + 1] + leading + tagged + after