import re

from data_embeddings_storage.database.recursive_text_splitter import RecursiveCharacterTextSplitter

# "|----|------|" separator line under a markdown table header
_SEPARATOR = re.compile(r"^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*(:?-{3,}:?)?\s*\|?\s*$")


def _squeeze_row(line):
    """Remove the padding spaces BDA puts in every markdown table cell."""
    if _SEPARATOR.match(line):
        return None
    cells = [re.sub(r"\s+", " ", cell).strip() for cell in line.strip().strip("|").split("|")]
    return "| " + " | ".join(cells) + " |"

def _is_empty_row(row):
    return not row.replace("|", "").strip()

def split_table(text, chunk_size=2000, chunk_overlap=200): #the parameters are kept for future adjustments
    """Split one BDA markdown table into chunks of whole rows.
    Padding spaces inside cells are removed.
    Header row is repeated at the top of every chunk
    An empty header row is dropped.
    If a single row is longer than chunk_size; that
    row is split at sentence/word boundaries.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    rows = [row for row in (_squeeze_row(line) for line in lines) if row is not None]
    has_separator = any(_SEPARATOR.match(line) for line in lines[:3])
    header = rows[0] if has_separator and rows else None
    body = rows[1:] if header else rows
    if header and _is_empty_row(header):
        header = None
    body = [row for row in body if not _is_empty_row(row)]

    # header only
    if not body:                               
        return [header] if header else []
    prefix = f"{header}\n" if header else ""
    room = max(chunk_size - len(prefix), chunk_size // 2)
    row_splitter = RecursiveCharacterTextSplitter(chunk_size=room, chunk_overlap=min(chunk_overlap, room // 4))

    chunks, current, size = [], [], 0
    for row in body:
        pieces = [row] if len(row) <= room else row_splitter.split_text(row)
        for piece in pieces:
            if current and size + len(piece) + 1 > room:
                chunks.append(prefix + "\n".join(current))
                current, size = [], 0
            current.append(piece)
            size += len(piece) + 1
    if current:
        chunks.append(prefix + "\n".join(current))
    return chunks
