import json
import os
import re

from data_preprocessing.bedrock.bda_results import BDAResults
from common.utils.settings import aws_client
from common.utils.helper import Helper
from common.utils.logger import log

# Bounding boxes are page-relative fractions. Lines on the same printed row sit
# within a few ten-thousandths of each other; the next row is ~0.011 further down.
ROW_TOLERANCE = 0.004

# Anything right of this column on a contents page is the page label, not title text.
PAGE_LABEL_LEFT = 0.60

CONTENTS_HEADING = re.compile(r"^table\s+of\s+contents$", re.IGNORECASE)
PAGE_LABEL = re.compile(r"^(?:page\s+)?([ivxlcdm]+|\d+)$", re.IGNORECASE)
MARKER_TOKEN = r"[IVXLCDM]{1,4}|[A-Z]{1,3}|\d{1,2}"
MARKER_ONLY = re.compile(rf"^({MARKER_TOKEN})[.)]$")
LEADING_MARKER = re.compile(rf"^({MARKER_TOKEN})([.)])\s+\S")
TRAILING_MARKER = re.compile(rf"\s({MARKER_TOKEN})([.)]?)$")

# An unnumbered heading set in capitals, which is how this corpus writes the top
# level of a section - "PROVIDER NETWORK REQUIREMENTS". Long enough to rule out an
# acronym on its own line.
SECTION_TITLE = re.compile(r"^[A-Z][A-Z0-9 ,&/'()\-]{8,}$")

# A numbered heading BDA handed back as a list item. The bold run is what tells it
# apart from ordinary numbered prose: a heading emboldens the number and the title
# together - "- **9. Core Benefits and Services**" - while a list item closes the
# bold straight after the marker - "- **1.** Offering or giving a bribe...".
LIST_HEADING = re.compile(r"^\s*[-*]?\s*\*\*(\d{1,2})\.\s+([^*]+?)\*\*\s*$")

ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def _page_index(item):
    locations = item.get("locations") or []
    if locations:
        page = locations[0].get("page_index")
        if page is not None:
            return page
    if item.get("page_index") is not None:
        return item.get("page_index")
    return (item.get("page_indices") or [None])[0]


def _bounding_box(item):
    locations = item.get("locations") or []
    return (locations[0].get("bounding_box") or {}) if locations else {}


def _element_text(item):
    text = (item.get("representation") or {}).get("text") or item.get("text") or ""
    return text.strip()


def _line_text(line):
    # Dotted leaders survive OCR often enough to be worth stripping here.
    return re.sub(r"[.\s]+$", "", _element_text(line))


def _normalize(title):
    return re.sub(r"[^A-Z0-9]+", " ", (title or "").upper()).strip()


def _roman_to_int(token):
    total = 0
    highest = 0
    for char in reversed(token.upper()):
        value = ROMAN_VALUES.get(char)
        if value is None:
            return None
        total = total + value if value >= highest else total - value
        highest = max(highest, value)
    return total or None


def _letter_ordinal(token):
    """A -> 1 ... Z -> 26, then the doubled AA -> 27, BB -> 28 this corpus uses."""
    token = token.upper()
    if not token.isalpha():
        return None
    if len(token) == 1:
        return ord(token) - 64
    if len(set(token)) == 1:
        return 26 * (len(token) - 1) + ord(token[0]) - 64
    return None


def _next_letter(ordinal):
    """The inverse of _letter_ordinal, for a section letter BDA did not give us."""
    repeat = (ordinal - 1) // 26 + 1
    return chr((ordinal - 1) % 26 + 65) * repeat


# ----------------------------------------------------------------------------
# Printed page labels
# ----------------------------------------------------------------------------

def page_labels(source_data):
    """page_index -> printed label, e.g. {1: 'ii', 21: 'Page 1'}."""
    labels = {}
    for element in source_data.get("elements", []):
        if element.get("sub_type") != "PAGE_NUMBER":
            continue
        page = _page_index(element)
        text = _element_text(element)
        if page is None or not text or page in labels:
            continue
        labels[page] = text
    return _repair_labels(labels)


def _printed_value(label):
    match = PAGE_LABEL.match((label or "").strip())
    return match.group(1).lower() if match else None


def arabic_page_offset(labels):
    """How far printed page 1 sits into the PDF, e.g. 20 pages of front matter.

    Taken as the most common page_index - printed_number difference so one OCR
    misread of a page number cannot shift the whole document.
    """
    offsets = {}
    for page, label in labels.items():
        value = _printed_value(label)
        if value and value.isdigit():
            offset = page - int(value)
            offsets[offset] = offsets.get(offset, 0) + 1
    if not offsets:
        return None
    return max(offsets.items(), key=lambda item: item[1])[0]


def _repair_labels(labels):
    """Replace a label that cannot be a page number with one read off the arabic run.

    BDA now and then takes a section marker sitting low on the page for the page
    number - one page of this contract comes back labelled "I." - and that label is
    what a reviewer gets told to turn to. The rest of the run is regular enough to
    say what the page must have been. Roman front matter is left alone; it is a real
    page label, just not an arabic one.
    """
    offset = arabic_page_offset(labels)
    if offset is None:
        return labels

    repaired = dict(labels)
    for page, label in labels.items():
        if _printed_value(label):
            continue
        printed = page - offset
        if printed < 1:
            continue  # front matter, where the arabic run says nothing useful
        repaired[page] = f"Page {printed}"
        log.info(f"_repair_labels() Page index {page} came back labelled {label!r}, which is not a "
                 f"page number. Using 'Page {printed}' from the arabic run instead.")
    return repaired


# ----------------------------------------------------------------------------
# Printed contents page
# ----------------------------------------------------------------------------

def _rows(lines):
    """Group loose text lines back into printed rows by vertical position."""
    positioned = [(line, _bounding_box(line)) for line in lines]
    positioned = [pair for pair in positioned if pair[1].get("top") is not None]
    positioned.sort(key=lambda pair: (pair[1]["top"], pair[1].get("left", 0.0)))

    grouped = []
    for line, box in positioned:
        if grouped and abs(box["top"] - grouped[-1][0][1]["top"]) <= ROW_TOLERANCE:
            grouped[-1].append((line, box))
        else:
            grouped.append([(line, box)])

    for row in grouped:
        row.sort(key=lambda pair: pair[1].get("left", 0.0))
    return grouped


def _parse_row(row):
    """Turn one reconstructed contents row into {marker, title, printed_page}."""
    marker = None
    printed_page = None
    title_parts = []

    for line, box in row:
        text = _line_text(line)
        if not text:
            continue
        if box.get("left", 0.0) >= PAGE_LABEL_LEFT and PAGE_LABEL.match(text):
            printed_page = text
            continue
        if marker is None and not title_parts and len(text) <= 5 and MARKER_ONLY.match(f"{text}."):
            marker = text.rstrip(".)")
            continue
        title_parts.append(text)

    title = " ".join(title_parts).strip()
    if not title and not printed_page:
        return None
    return {"marker": marker, "title": title, "printed_page": printed_page}


def contents_page_indices(source_data, lines_by_page):
    """The contents pages: the heading page plus any spillover pages after it."""
    heading_page = None
    for page in sorted(lines_by_page):
        if any(CONTENTS_HEADING.match(_line_text(line)) for line in lines_by_page[page]):
            heading_page = page
            break
    if heading_page is None:
        return []

    pages = [heading_page]
    page = heading_page + 1
    while page in lines_by_page:
        rows = [_parse_row(row) for row in _rows(lines_by_page[page])]
        # A continuation page still looks like a contents page: rows of title plus
        # page label. Once that stops being true we have walked off the end.
        if len([row for row in rows if row and row["printed_page"] and row["title"]]) < 3:
            break
        pages.append(page)
        page += 1
    return pages


def _lines_by_page(source_data):
    lines_by_page = {}
    for line in source_data.get("text_lines") or []:
        page = _page_index(line)
        if page is not None:
            lines_by_page.setdefault(page, []).append(line)
    return lines_by_page


def contents_pages(source_data):
    """The printed contents pages, for the parsers that need to leave them out.

    Their content is a list of section titles against page labels, which is answer-
    shaped enough to out-rank the provision a requirement is really asking about.
    This index is what those pages are for, so nothing is lost by keeping them out
    of the embeddings as well.
    """
    return set(contents_page_indices(source_data, _lines_by_page(source_data)))


def parse_printed_contents(source_data):
    lines_by_page = _lines_by_page(source_data)
    pages = contents_page_indices(source_data, lines_by_page)
    if not pages:
        log.info("parse_printed_contents() No printed table of contents in this document.")
        return []

    entries = []
    for page in pages:
        for row in _rows(lines_by_page[page]):
            entry = _parse_row(row)
            if not entry or not entry["title"]:
                continue
            if CONTENTS_HEADING.match(entry["title"]) and not entry["printed_page"]:
                continue  # the heading of the contents page itself
            if not entry["printed_page"]:
                if entries and not entry["marker"]:
                    # A title too long for the column wraps onto the next row.
                    entries[-1]["title"] = f"{entries[-1]['title']} {entry['title']}".strip()
                continue
            entries.append(entry)

    log.info(f"parse_printed_contents() Recovered {len(entries)} contents rows from pages {pages}.")
    return entries


# ----------------------------------------------------------------------------
# Outline from the section headings
# ----------------------------------------------------------------------------

def _split_marker(text):
    """Pull the section marker off a heading, whichever end BDA left it on.

    BDA flattens the number column into the heading text, sometimes in front
    ("V. AWARD") and sometimes behind ("RESIDENT BIDDER Y."). A multi-character
    marker has to keep its punctuation to count, otherwise a heading that simply
    ends in an acronym would look numbered.
    """
    match = MARKER_ONLY.match(text)
    if match:
        return match.group(1), ""

    match = LEADING_MARKER.match(text)
    if match:
        return match.group(1), text[match.end(2):].strip()

    match = TRAILING_MARKER.search(text)
    if match:
        marker, punctuation = match.group(1), match.group(2)
        if len(marker) == 1 or punctuation:
            return marker, text[:match.start()].strip()

    return None, text


def _element_pages(element):
    """Every page the element touches, in order."""
    pages = [location.get("page_index") for location in element.get("locations") or []]
    pages.extend(element.get("page_indices") or [])
    return sorted({page for page in pages if page is not None})


def _list_headings(element):
    """Numbered headings buried in a LIST block, as (page, line_position, text).

    BDA tags most of this contract's subsection headings LIST rather than
    SECTION_HEADER, and usually merges them into the same element as the body text
    underneath. Skipping them leaves the outline jumping from V.E.8 to V.E.30, and
    everything in between - emergency services, family planning, transportation -
    gets cited against whichever section happened to be parsed last.
    """
    markdown = (element.get("representation") or {}).get("markdown") or ""
    lines = markdown.splitlines()
    pages = _element_pages(element)
    for position, line in enumerate(lines):
        match = LIST_HEADING.match(line)
        if not match:
            continue
        # A long block runs over a page break, so the heading is placed on the page
        # its share of the way through rather than on the element's first page.
        page = pages[position * len(pages) // len(lines)] if pages else None
        yield page, position / (len(lines) + 1), f"{match.group(1)}. {match.group(2).strip()}"


def section_headings(source_data):
    headings = []
    for element in source_data.get("elements", []):
        sub_type = element.get("sub_type")
        reading_order = element.get("reading_order", 0)

        if sub_type in ("SECTION_HEADER", "TITLE"):
            page = _page_index(element)
            text = _element_text(element)
            if page is None or not text:
                continue
            headings.append({
                "page": page,
                "reading_order": float(reading_order),
                "text": text,
                "is_title": sub_type == "TITLE",
            })
        elif sub_type == "LIST":
            for page, line_offset, text in _list_headings(element):
                if page is None:
                    continue
                headings.append({
                    "page": page,
                    # Offset by where the line sits in the block, so a heading half
                    # way down one element still sorts after the element before it
                    # and before the next.
                    "reading_order": reading_order + line_offset,
                    "text": text,
                    "is_title": False,
                })

    headings.sort(key=lambda heading: (heading["page"], heading["reading_order"]))
    return headings


def _classify(marker, counters):
    """Decide which outline depth a marker belongs to and advance the counters.

    Roman numerals and subsection letters overlap (I, V, X, L, C, D, M), so the
    running sequence breaks the tie: a marker that is exactly the next expected
    section number is a section, otherwise a single letter that carries on the
    subsection run is a subsection. Skipping ahead is allowed because BDA does
    miss the odd heading, but going backwards is not - that is a false positive.
    """
    roman = _roman_to_int(marker) if marker.isalpha() else None
    letter = _letter_ordinal(marker)

    if roman is not None and roman == counters[0] + 1:
        counters[0], counters[1], counters[2] = roman, 0, 0
        return 1
    if letter is not None and letter > counters[1]:
        counters[1], counters[2] = letter, 0
        return 2
    if marker.isdigit() and int(marker) > counters[2]:
        counters[2] = int(marker)
        return 3
    if roman is not None and len(marker) > 1 and roman > counters[0]:
        counters[0], counters[1], counters[2] = roman, 0, 0
        return 1
    return None


def build_outline(source_data):
    """Numbered outline entries in document order, each with its breadcrumb."""
    counters = [0, 0, 0]
    open_stack = []
    entries = []

    for heading in section_headings(source_data):
        marker, title = _split_marker(heading["text"])
        level = _classify(marker, counters) if marker else None

        if level is None and marker and not title:
            # A bare "1." that does not carry on any run is a numbering artefact,
            # not a heading. Keeping it would put a title-less step in the trail.
            continue

        if level is None:
            # Front-matter titles anchor the top level. Anything else is an
            # unnumbered heading, and it belongs one step under the deepest
            # numbered section still open - not under the unnumbered heading
            # before it, or a run of them nests into a chain seven deep.
            numbered = [open_entry["level"] for open_entry in open_stack if open_entry["number"]]
            if heading["is_title"]:
                level, marker = 1, None
            elif counters[0] and SECTION_TITLE.match(heading["text"]):
                # BDA drops the odd section letter - "PROVIDER NETWORK
                # REQUIREMENTS" comes back with no "I." on it. Left unnumbered the
                # section's own 1., 2., 3. run reads as going backwards inside the
                # section before it and every subsection under it loses its number,
                # so the letter is taken to be the next one in the run.
                counters[1] += 1
                marker = _next_letter(counters[1])
                level = 2
            else:
                level, marker = max(numbered, default=0) + 1, None

        while open_stack and open_stack[-1]["level"] >= level:
            closed = open_stack.pop()
            closed["end_page"] = max(closed["start_page"], heading["page"] - 1)

        if marker and level == 1:
            counters[1] = counters[2] = 0
        elif marker and level == 2:
            counters[2] = 0

        parents = list(open_stack)
        numbers = [parent["number"] for parent in parents if parent["number"]]
        if marker:
            numbers.append(marker)

        entry = {
            "number": marker,
            "path": ".".join(numbers),
            # A numbered heading whose text BDA dropped is left blank here and
            # picked up later from the printed contents if it can be found.
            "title": title if (title or marker) else heading["text"],
            "level": level,
            "start_page": heading["page"],
            "end_page": None,
            "parents": parents,
        }
        entries.append(entry)
        open_stack.append(entry)

    return entries


def _heading_label(entry):
    return f"{entry['path']} {entry['title']}".strip()


def _apply_breadcrumbs(entries):
    """Build the trails last, so a title recovered from the printed contents also
    shows up in the trail of every heading sitting underneath it."""
    for entry in entries:
        trail = [_heading_label(parent) for parent in entry.pop("parents")]
        trail.append(_heading_label(entry))
        entry["breadcrumb"] = " > ".join(part for part in trail if part)


def _fill_missing_titles(entries, printed, labels):
    """Borrow wording from the printed contents where BDA dropped a heading's text.

    Matching on the marker alone is not enough - every section has an "A." and a
    "J." - so the printed row also has to point at roughly the page where the
    heading actually is.
    """
    offset = arabic_page_offset(labels)
    printed_pages = {}
    for page, label in labels.items():
        value = _printed_value(label)
        if value and value not in printed_pages:
            printed_pages[value] = page

    candidates = []
    for row in printed:
        if not row["marker"] or not row["title"]:
            continue
        value = _printed_value(row["printed_page"])
        if not value:
            continue
        page = int(value) + offset if value.isdigit() and offset is not None else printed_pages.get(value)
        if page is not None:
            candidates.append((row["marker"].upper(), row["title"], page))

    for entry in entries:
        if not entry["number"] or entry["title"]:
            continue
        nearby = [(abs(page - entry["start_page"]), title)
                  for marker, title, page in candidates
                  if marker == entry["number"].upper() and abs(page - entry["start_page"]) <= 2]
        if nearby:
            entry["title"] = min(nearby)[1]


def build_toc_index(source_data):
    source_key = source_data.get("metadata", {}).get("s3_key", "")
    doc_id = source_key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    page_count = len(source_data.get("pages") or [])

    labels = page_labels(source_data)
    entries = build_outline(source_data)
    _fill_missing_titles(entries, parse_printed_contents(source_data), labels)
    _apply_breadcrumbs(entries)

    last_page = page_count - 1 if page_count else 0
    for entry in entries:
        if entry["end_page"] is None:
            entry["end_page"] = max(entry["start_page"], last_page)
        entry["printed_start_page"] = labels.get(entry["start_page"])
        entry["printed_end_page"] = labels.get(entry["end_page"])

    index = {
        "doc_id": doc_id,
        "source_key": source_key,
        "page_count": page_count,
        "arabic_page_offset": arabic_page_offset(labels),
        "page_labels": {str(page): label for page, label in sorted(labels.items())},
        "entries": entries,
    }
    log.info(f"build_toc_index() doc_id={doc_id} entries={len(entries)} pages={page_count} "
             f"page_offset={index['arabic_page_offset']}")
    return index


def invoke_parsed_toc():
    log.info(f"***************** invoke_parsed_toc Starts. __name__={__name__}")
    try:
        output_bucket = Helper.get_property("output_bucket")
        output_prefix = Helper.get_property("output_prefix")
        toc_output_folder = Helper.get_property("BDAToCOutputFolder")
        toc_output_filename = Helper.get_property("BDAToCOutputFilename")

        aws_client_s3 = aws_client("s3")

        log.info(f"invoke_parsed_toc() output_bucket={output_bucket} output_prefix={output_prefix}")
        bda_results = BDAResults()
        parsed_data = bda_results.fetch_parsed_bda_results(output_bucket, output_prefix, aws_client_s3, "ToC")
        log.info(f"invoke_parsed_toc() Loaded {len(parsed_data)} document(s)")

        toc_data = [build_toc_index(data) for data in parsed_data]

        toc_output_key = os.path.join(toc_output_folder, toc_output_filename)
        log.debug(f"invoke_parsed_toc() Writing table-of-contents index to {toc_output_key}")
        aws_client_s3.put_object(Bucket=output_bucket, Key=toc_output_key,
                                 Body=json.dumps(toc_data, indent=2))

        log.info(f"***************** invoke_parsed_toc() Ends. __name__={__name__}. Returning True.")
        return True

    except Exception as lclAllEx:
        Helper.print_exception("invoke_parsed_toc", lclAllEx, "Error occurred in function invoke_parsed_toc.")
        log.error(f"***************** invoke_parsed_toc End. __name__={__name__}. Returning False.")
        return False
