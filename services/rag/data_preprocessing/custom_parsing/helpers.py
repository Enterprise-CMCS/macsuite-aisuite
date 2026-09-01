import re
 
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional 
import pdfplumber
 
 
"**Detect section/subsection/appendix headings**"

# Ex. SHOULD match: 1.1 Basis of Contract Authority, 1.21 Information Technology, 1.21.12 System Security
# Ex. SHOULD NOT match: 63 O.S. §§ 1-133, 42 C.F.R. § 438.242, 30 Business Days
SECTION_RE = re.compile(r"^(1(?:\.\d+)+)\s+(.+?)\s*$")

# Additional heading style such as: Section 1: Introduction
REPORT_SECTION_RE = re.compile(r"^Section\s+(\d+)\s*:\s*(.+?)\s*$", re.I)
HEADER_FOOTER_MIN_REPEAT = 3

# remove table of contents, list of appendix
TOC_LINE_RE = re.compile(r"\.{5,}\s*\d+\s*$")
APPENDIX_LIST_RE = re.compile(r"^appendix\s+[A-Za-z0-9]+[:\s].*\.{3,}\s*\d+\s*$",re.I,)
  
# Detect possible Appendix headings.
# Ex.Appendix 1C: "Quality Performance Withhold Program of this Contract...are not automatically treated as Appendix headings.
APPENDIX_HEADER_RE = re.compile(r"^Appendix\s+([A-Za-z0-9]+)\s*[:.]\s*(.+?)\s*$",re.I,)

 
PRINTED_PAGE_PATTERNS = [
    r"\bPage\s+(\d+)\b",
    r"\bPage\s+(\d+)\s+of\s+\d+\b",
    r"^\s*(\d+)\s*$",
    r"^\s*-\s*(\d+)\s*-\s*$",
    r"\b([A-Z]-\d+)\b",
]

def extract_printed_page(page) -> Optional[str]:
    x0, top, x1, bottom = page.bbox
    height = bottom - top
 
    footer = page.crop((x0, top + height * 0.88, x1, bottom)).extract_text() or ""
    header = page.crop((x0, top, x1, top + height * 0.12)).extract_text() or ""
 
    for text in [footer, header]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in lines:
            for pattern in PRINTED_PAGE_PATTERNS:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    return match.group(1)
    return None

"**Text cleaning **"
# normalizing text by removing extra spaces, tabs, and newlines
def clean_text(text: str) -> str:
    if text is None:
        return ""
    text = str(text)
    text = text.replace("\u00a0"," ",)
    text = re.sub(r"[ \t]+"," ",text,)
    text = re.sub(r"\n{3,}","\n\n",text,)
    return text.strip()

"**Page Number/Footer/TOC helpers **"
# Check if a line is a page number (1 to 4 digits).
def is_page_number(line: str,) -> bool:
    return bool(re.fullmatch(r"\d{1,4}",line.strip(),))

# Detect common page number format such as 2 | page, 2 | p a g e, page 2, P a g e 2 etc.
def is_page_footer(line: str) -> bool:
    line = clean_text(line)
 
    patterns = [
        r"^\d{1,4}\s*\|\s*p\s*a\s*g\s*e\s*$",
        r"^p\s*a\s*g\s*e\s*\d{1,4}\s*$",
        r"^\d{1,4}\s*[-–]\s*p\s*a\s*g\s*e\s*$",
    ]
 
    return any(re.fullmatch(pattern, line, re.I) for pattern in patterns)
  
# normalize line, check line if toc, match with toc regex, if neither is met, return false
def is_toc_line(line: str,) -> bool:
    line = clean_text(line).lower()
    if line == "table of contents":
        return True
    
    # Ex. 1.1 Basis of Contract Authority ............ 5
    if TOC_LINE_RE.search(line):
        return True
    return False
 
# check if line in appendix list
def is_appendix_list_line(line: str,) -> bool: 
    return bool(APPENDIX_LIST_RE.match(clean_text(line)))

"**Skip whole pages **"
# skip the page with table of contents, list of appendix, acronyms, abbreviations.
def should_skip_page(lines: List[str],) -> bool:
    """
    Skip front-matter pages such as:
    - Table of Contents
    - List of Acronyms / Abbreviations
    """
 
    if not lines:
        return False
 
    first_lines = " ".join(lines[:15]).lower()
 
    if "table of contents" in first_lines:
        return True
 
    if ("list of acronyms and abbreviations"in first_lines):
        return True
 
    if "list of acronyms" in first_lines:
        return True
 
    if ("acronym / abbreviation" in first_lines):
        return True
 
    if ("acronym" in first_lines and "definition" in first_lines):
        return True
 
    toc_count = sum(1 for line in lines if is_toc_line(line))
 
    appendix_count = sum(1 for line in lines if is_appendix_list_line(line))
 
    if toc_count >= 3:
        return True
 
    if appendix_count >= 2:
        return True
    
    return False

"**Table bboxs extraction to remove smaller tables from larger one **"
def bbox_area(bbox) -> float:
    x0, top, x1, bottom = bbox
    return max(0, x1 - x0) * max(0, bottom - top)
 
def bbox_inside(inner_bbox, outer_bbox, tolerance: float = 5) -> bool:
    """
    Return True when inner_bbox is inside outer_bbox.
    """
    ix0, itop, ix1, ibottom = inner_bbox
    ox0, otop, ox1, obottom = outer_bbox
 
    return (ix0 >= ox0 - tolerance and itop >= otop - tolerance 
            and ix1 <= ox1 + tolerance and ibottom <= obottom + tolerance)
 
def remove_nested_tables(found_tables):
    """
    pdfplumber may detect a large table and smaller tables inside that table.
    Keep the largest outer table and remove tables completely contained inside it.
    """
    if not found_tables:
        return []
    
    sorted_tables = sorted(found_tables,key=lambda table: bbox_area(table.bbox), reverse=True,)
    kept = []
 
    for table in sorted_tables:
        is_nested = False
        for existing in kept:
            if bbox_inside(table.bbox, existing.bbox):
                is_nested = True
                break
        if not is_nested:
            kept.append(table)

    # sort the remaining tables
    kept.sort(key=lambda table: table.bbox[1])
 
    return kept

"**Table Markdown**"
# Convert a table to Markdown format.
def table_to_markdown(table: List[List[Optional[str]]],) -> str:
 
    # check edge case if table is empty
    if not table:
        return ""
    
    rows = []
    # Iterates through every row and cell in the input table
    for row in table:
        rows.append([clean_text(cell or "") for cell in row])
 
    # defining header and body of the table
    header = rows[0]
    body = rows[1:]
 
    # validate if header is empty
    if not any(header):
        return ""
 
    # count the maximum number of columns in the table to ensure consistent formatting
    col_count = max(len(row) for row in rows)
    header = (header + [""] * col_count)[:col_count]
 
    # holder for the markdown representation of the table
    markdown = []
 
    markdown.append("| " + " | ".join(header) + " |")
    markdown.append("| " + " | ".join(["---"] * col_count) + " |")
 
    for row in body:
        row = (row + [""] * col_count)[:col_count]
        if any(row):
            markdown.append("| " + " | ".join(row) + " |")

    return "\n".join(markdown)
 
# remove pipe characters inside a table cell, otherwise will be considered as column
def escape_markdown_cell(text: str,) -> str: 
    text = clean_text(text)
    return text.replace("|", r"\|",)
  
# Improved Markdown conversion for tables.
# cleans every cell, removes "|" inside cell text, empty rows, normalizes row-length, 
def table_to_markdown_safe(table: List[List[Optional[str]]],) -> str:
 
    if not table:
        return ""
 
    cleaned_rows = []
    for row in table:
        cleaned_row = []
        for cell in row:
            cleaned_row.append(escape_markdown_cell(cell or ""))
        cleaned_rows.append(cleaned_row)

    if not cleaned_rows:
        return ""
 
    col_count = max(len(row) for row in cleaned_rows)
 
    normalized_rows = []
    for row in cleaned_rows:
        normalized_rows.append((row + [""] * col_count)[:col_count])
 
    header = normalized_rows[0]
    if not any(header):
        return ""
    
    markdown = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * col_count) + " |",]
 
    for row in normalized_rows[1:]:
        if not any(row):
            continue
        markdown.append("| " + " | ".join(row) + " |")
 
    return "\n".join(markdown)
 
"**Header/footer helpers**"
# collect repeated lines coming from top/bottom margins.
def collect_repeated_lines(pdf_path: Path,) -> set:
    repeated = Counter()
    with pdfplumber.open(pdf_path) as pdf:
 
        for page in pdf.pages:
            height = page.height
            words = (page.extract_words() or [])
            margin_lines = []
            for word in words:
 
                top = float(word["top"])
                bottom = float(word["bottom"])

                # Only look near top or bottom of page
                if (top < 80 or bottom > height - 80):
 
                    margin_lines.append(word["text"])

            line_text = clean_text(" ".join(margin_lines))

            if line_text:
                repeated[line_text] += 1
 
    return {text for text, count in repeated.items() if count >= HEADER_FOOTER_MIN_REPEAT}
 
"**Remove Table words from Normal Text**"
# Determine whether the center of a PDF word falls inside a detected table bounding box. 
# BBox is used internally only for removing table text from normal text extraction.
def word_inside_bbox(word: Dict[str, Any], bbox,) -> bool:
 
    x_center = (float(word["x0"]) + float(word["x1"])) / 2
    y_center = (float(word["top"]) + float(word["bottom"])) / 2
 
    x0, top, x1, bottom = bbox
 
    return (x0 <= x_center <= x1 and top <= y_center <= bottom)
  
# Remove all words that belong to detected table. 
# It prevents table values such as 1.23.1.15 - Enrollee Services from being sent to find_heading().
def remove_table_words(words: List[Dict[str, Any]],table_bboxes: List[Any],) -> List[Dict[str, Any]]: 
    clean_words = [] 
    for word in words: 
        inside_table = any(word_inside_bbox(word,bbox,) for bbox in table_bboxes) 
        if inside_table:
            continue 
        clean_words.append(word)
    return clean_words

"**Word to Lines**"
# Convert extracted PDF words into visual text lines.
# Words with approximately the same vertical position are treated as belonging to the same line. 
# Words within each line are ordered from left to right using x0.
def words_to_lines(words: List[Dict[str, Any]], y_tolerance: float = 3) -> List[str]:
    if not words:
        return []
 
    # Sort words from top to bottom, then left to right based on coordinates
    sorted_words = sorted(words, key=lambda word: (float(word["top"]), float(word["x0"])))
 
    lines = []
    current_words = []
    current_top = None
 
    for word in sorted_words:
        word_top = float(word["top"])
 
        # Start the first line.
        if current_top is None:
            current_top = word_top
            current_words.append(word)
            continue
 
        # Word is approximately on the same visual line.
        if abs(word_top - current_top) <= y_tolerance:
            current_words.append(word)
            continue
 
        # New line detected. Save the previous line.
        current_words.sort(key=lambda item: float(item["x0"]))
        line_text = clean_text(" ".join(item["text"] for item in current_words))
 
        if line_text:
            lines.append(line_text)
 
        # Start the next line.
        current_words = [word]
        current_top = word_top
 
    # Save the final line.
    if current_words:
        current_words.sort(key=lambda item: float(item["x0"]))
        line_text = clean_text(" ".join(item["text"] for item in current_words))
 
        if line_text:
            lines.append(line_text)
 
    return lines

"**Heading detection**"
# Check if a line begins with a legal/statutory citation.
# Ex: 63 O.S. §§ 1-133, 42 C.F.R. § 438.242, 56 O.S. § 4002 etc. These are normal content and must not become sections.
def is_legal_citation_line(line: str) -> bool:
    line = clean_text(line)
    patterns = [
        r"^\d+\s+O\.S\.",
        r"^\d+\s+C\.F\.R\.",
        r"^\d+\s+U\.S\.C\.",
        r"^\d+\s+OAC\b",
        r"^\d+\s+CFR\b",
        r"^\d+\s+USC\b",
    ]
 
    return any(re.match(pattern, line, re.I) for pattern in patterns)
 
# find heading in the text and return its number and name,
def find_heading(line: str) -> Optional[Dict[str, str]]:
    line = clean_text(line)
    # Legal citations should always remain normal text.
    if is_legal_citation_line(line):
        return None

    # Additional report-style section heading.
    report_match = REPORT_SECTION_RE.match(line)
    if report_match:
        return {
            "number": report_match.group(1),
            "name": clean_text(report_match.group(2)),
            "heading_type": "report-style",
        }

    match = SECTION_RE.match(line)
    if not match:
        return None
 
    number = match.group(1)
    name = clean_text(match.group(2))
    # Avoid treating table-of-contents dotted lines
    # as content headings.
    if "." * 5 in name:
        return None
    # Very long "heading names" are usually sentences,
    # not actual headings.
    if len(name) > 150:
        return None
    return {
        "number": number,
        "name": name,
    }
 
# Determine whether a line that starts with "Appendix" is only a reference to an appendix inside normal section/subsection.
# Ex. Appendix 1C: "Quality Performance Withhold Program" of this Contract and are reference only.
def is_appendix_reference(line: str) -> bool:
    line_lower = clean_text(line).lower()
    reference_phrases = ["of this contract","as described in","as outlined in","in accordance with","pursuant to","refer to appendix","described in appendix",]
    return any(phrase in line_lower for phrase in reference_phrases)
 
# Detect actual Appendix headings.
# Ex. Appendix 1F: List of Deliverables to OHCA
def find_appendix_heading(line: str) -> Optional[Dict[str, str]]:
    line = clean_text(line)
    if is_appendix_reference(line):
        return None
    match = APPENDIX_HEADER_RE.match(line)
    if not match:
        return None
    appendix = match.group(1)
    title = clean_text(match.group(2))
 
    # Actual Appendix titles should be reasonably short. Long sentence-like values are likely references.
    # longer appendix title also be noticed in parity report type document
    if len(title) > 200:
        return None
 
    # A period inside a long sentence often indicates this is prose rather than an Appendix heading.
    if title.endswith(".") and len(title.split()) > 10:
        return None
 
    return {
        "appendix": appendix,
        "title": title,
    }
 
# checks if the text is simple heading or normal body text
def is_simple_heading(line: str) -> bool:
    line = clean_text(line)
    if not line:
        return False
    if len(line) > 120:
        return False
    if line.endswith("."):
        return False
    if line.isupper() and len(line.split()) >= 3:
        return True
    return False
 
# Check whether normal paragraph text ends a sentence.
def ends_sentence(line: str) -> bool:
    return line.strip().endswith((".", "?", "!"))
 
"**page element helpers**"
# Create an intermediate TABLE element.
# bbox is kept here only because the extraction process needs it to distinguish table text from normal text.
def make_table_element(page_no: int, table_index: int, markdown: str, bbox) -> Dict[str, Any]:
 
    return {
        "element_type": "TABLE",
        "page": page_no,
        "table_index": table_index,
        "markdown": markdown,
        "bbox": bbox,
    }
 
# Create an intermediate TEXT element.
def make_text_element(page_no: int, lines: List[str]) -> Dict[str, Any]:
 
    return {
        "element_type": "TEXT",
        "page": page_no,
        "text": "\n".join(lines),
    }
 
"**Final JSON record helpers**"
# creating a single json for unspecified texts/paragraphs
def make_flat_text_record(doc_id: str, text: str, page_no: int, printed_page: Optional[str] = None, appendix: Optional[str] = None, appendix_title: Optional[str] = None) -> Dict[str, Any]:
 
    metadata = {
        "doc_id": doc_id,
        "page": page_no,
        "printed_page" : printed_page,
        "element_type": "TEXT",
    }
    if appendix:
        metadata["appendix"] = appendix
    if appendix_title:
        metadata["appendix_title"] = appendix_title
    return {
        "doc_id": doc_id,
        "text": text,
        "metadata": metadata,
    }

def make_section_record(doc_id: str, section_no: str, name: str, page_no: int, printed_page: Optional[str]= None, appendix: Optional[str] = None, appendix_title: Optional[str] = None) -> Dict[str, Any]:
 
    metadata = {
        "doc_id": doc_id,
        "page": page_no,
        "printed_page": printed_page,
        "element_type": "TEXT",
        "section": section_no,
        "section_name": name,
    }

    if appendix:
        metadata["appendix"] = appendix
    if appendix_title:
        metadata["appendix_title"] = appendix_title
    return {
        "doc_id": doc_id,
        "Section": section_no,
        "Name": name,
        "Text": "",
        "Subsections": [],
        "metadata": metadata,
    }
 

def make_subsection_record(doc_id: str, subsection_no: str, name: str, page_no: int, printed_page: Optional[str]= None, section_no: Optional[str] = None, section_name: Optional[str] = None, appendix: Optional[str] = None, appendix_title: Optional[str] = None) -> Dict[str, Any]:
    metadata = {
        "doc_id": doc_id,
        "page": page_no,
        "printed_page":printed_page,
        "element_type": "TEXT",
        "section": section_no,
        "section_name": section_name,
        "subsection": subsection_no,
        "subsection_name":name,
    }
    if appendix:
        metadata["appendix"] = appendix
    if appendix_title:
        metadata["appendix_title"] = appendix_title
    return {
        "Subsection": subsection_no,
        "Name": name,
        "Text": "",
        "metadata": metadata,
    }

# Create the final JSON record for a detected table. The table content is stored as Markdown in "text".
def make_table_record(doc_id: str, markdown: str, page_no: int,  table_index: int, printed_page: Optional[str] = None, appendix: Optional[str] = None, appendix_title: Optional[str] = None) -> Dict[str, Any]:
    metadata = {
        "doc_id": doc_id,
        "page": page_no,
        "printed_page": printed_page,
        "element_type": "TABLE",
        "table_index": table_index,
    }
    if appendix:
        metadata["appendix"] = appendix
    if appendix_title:
        metadata["appendix_title"] = appendix_title
    return {
        "doc_id": doc_id,
        "text": markdown,
        "metadata": metadata,
    }
 
# Flush flats text to the output. Flat text is used for normal content that does not belong to a recognized Section or Subsection.
def flush_flat_buffer(output: List[Dict[str, Any]], buffer: List[str], doc_id: str, page_no: int, printed_page: Optional[str] = None, is_heading: bool = False, appendix: Optional[str] = None, appendix_title: Optional[str] = None) -> None:
    if not buffer:
        return
    text = clean_text(" ".join(buffer))
    if not text:
        buffer.clear()
        return
    if is_heading:
        prefix = "#" if text.isupper() else "##"
        text = f"{prefix} {text}"
    output.append(
        make_flat_text_record(
            doc_id=doc_id,
            text=text,
            page_no=page_no,
            printed_page=printed_page,
            appendix=appendix,
            appendix_title=appendix_title,
        )
    )
    buffer.clear()
