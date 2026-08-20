import argparse
import json
from pathlib import Path
from typing import Any, Dict, List
 
import pdfplumber
 
from helpers import (clean_text, collect_repeated_lines, ends_sentence, find_appendix_heading, find_heading,
    flush_flat_buffer, is_appendix_list_line, is_page_number, is_simple_heading, is_toc_line, make_section_record, make_subsection_record, make_table_element, make_table_record,
    make_text_element, remove_table_words, should_skip_page, table_to_markdown_safe, words_to_lines,remove_nested_tables, is_page_footer)
 
"*****PDF page extraction*****"
# Extract text and tables from each page of the PDF, while filtering repeated lines and page numbers.
# Tables are detected BEFORE normal text is parsed.
# Words inside table regions are removed from the normal 
# text so values inside tables cannot accidentally become Sections/Subsections.

def extract_pdf_pages(pdf_path: Path) -> List[Dict[str, Any]]:
    repeated_lines = collect_repeated_lines(pdf_path)
    pages = []
 
    with pdfplumber.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            width = page.width
            height = page.height
 
            # Crop 8% to 93 % from top-bottom for common header/footer 
            # and keep main body of text
            cropped = page.crop((0, height * 0.08, width, height * 0.93))

            raw_words = cropped.extract_words() or []
            raw_lines = words_to_lines(raw_words)
            raw_lines = [clean_text(line) for line in raw_lines if clean_text(line)]
 
            if should_skip_page(raw_lines):
                continue

            # 1. Detect tables before normal text
            found_tables = cropped.find_tables() or []
            found_tables = remove_nested_tables(found_tables)
 
            table_bboxes = [table.bbox for table in found_tables]
            table_elements = []
 
            for table_index, table_object in enumerate(found_tables, start=1):
                extracted_table = table_object.extract()
                table_md = table_to_markdown_safe(extracted_table)
                if not table_md:
                    continue
                table_elements.append(make_table_element(page_no=page_index, table_index=table_index, markdown=table_md,bbox=table_object.bbox,))
                # print(table_elements)

            # 2. Extract page words.
            words = cropped.extract_words() or []
 
            # Remove words belonging to tables. 
            # prevents "1.23.1.15 - Enrollee Services" inside tables from becoming fake subsections.
            words = remove_table_words(words, table_bboxes)
 
            # Reconstruct normal text after table words are removed.
            extracted_lines = words_to_lines(words)
            lines = []
 
            for line in extracted_lines:
                line = clean_text(line)
 
                if not line:
                    continue
                if line in repeated_lines:
                    continue
                if is_page_number(line):
                    continue
                if is_page_footer(line):
                    continue
                if is_toc_line(line):
                    continue
                if is_appendix_list_line(line):
                    continue
 
                lines.append(line)
 
            # Skip pages mostly containing TOC, acronym lists, or Appendix lists.
            if should_skip_page(lines):
                continue
 
            # 3. Build only TEXT and TABLE elements.
            elements = []
 
            if lines:
                elements.append(make_text_element(page_no=page_index, lines=lines))
 
            elements.extend(table_elements)
            pages.append({"page": page_index, "elements": elements})
 
    return pages
 
"*****Build final JSON structure from page-level TEXT and TABLE elements*****"
# it preserves:
# 1. Valid contract headings create Sections/Subsections.
# 2. Normal paragraphs are assigned to current Section/Subsection.
# 3. Unspecified text outside any known Section/Subsection becomes flat TEXT.
# 4. TABLE elements are stored independently in Markdown.
# 5. TABLE contents are never sent to find_heading().

def parse_pdf_to_records(pdf_path: Path) -> List[Dict[str, Any]]:
    pages = extract_pdf_pages(pdf_path)
    doc_id = pdf_path.stem
    return build_section_json(pages, doc_id)

def build_section_json(pages: List[Dict[str, Any]], doc_id: str) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    # Ex. sections_by_number["1.21"] -> Information Technology
    sections_by_number: Dict[str, Dict[str, Any]] = {}
 
    flat_buffer = []
    flat_buffer_page = None
    flat_buffer_is_heading = False
    current_section = None
    current_subsection = None
 
    # Current Appendix context: remain None unless an actual Appendix heading is detected
    current_appendix = None
    current_appendix_title = None
 
    # Creates a Section once and returns the existing Section on later references
    def ensure_section(section_no: str, name: str, page_no: int) -> Dict[str, Any]:
        if section_no not in sections_by_number:
            section_record = make_section_record(
                doc_id=doc_id,
                section_no=section_no,
                name=name,
                page_no=page_no,
                appendix=current_appendix,
                appendix_title=current_appendix_title,
            )
            sections_by_number[section_no] = section_record
            output.append(section_record)
 
        return sections_by_number[section_no]
 
    # Process all PDF pages
    for page in pages:
        page_no = page["page"] #page number
 
        # Process TEXT and TABLE independently.
        for element in page.get("elements", []):
            element_type = element.get("element_type")

            # TABLE
            if element_type == "TABLE":
                # Flush pending flat text before storing table.
                if flat_buffer:
                    flush_flat_buffer(output=output,buffer=flat_buffer,doc_id=doc_id, page_no=flat_buffer_page or page_no,
                        is_heading=flat_buffer_is_heading,appendix=current_appendix,appendix_title=current_appendix_title,)
                    flat_buffer_page = None
                    flat_buffer_is_heading = False
 
                table_record = make_table_record(doc_id=doc_id,markdown=element.get("markdown", ""), page_no=page_no, 
                                                 table_index=element.get("table_index", 1),appendix=current_appendix, appendix_title=current_appendix_title,)
                output.append(table_record)
                continue
 
            # TEXT
            if element_type != "TEXT":
                continue
 
            lines = element.get("text", "").splitlines()
 
            for line in lines:
                line = clean_text(line)
 
                if not line:
                    continue
 
                # Detect actual Appendix heading.
                appendix_heading = find_appendix_heading(line)
 
                if appendix_heading:
 
                    if flat_buffer:
                        flush_flat_buffer(output=output,buffer=flat_buffer,doc_id=doc_id,page_no=flat_buffer_page or page_no,
                            is_heading=flat_buffer_is_heading,appendix=current_appendix,appendix_title=current_appendix_title,)

                    flat_buffer_page = None
                    flat_buffer_is_heading = False
                    current_appendix = appendix_heading["appendix"]
                    current_appendix_title = appendix_heading["title"]

                    # Appendix starts a new structural context.
                    current_section = None
                    current_subsection = None

                    output.append({
                        "doc_id": doc_id,
                        "text": f"Appendix {current_appendix}: {current_appendix_title}",
                        "metadata": {
                            "doc_id": doc_id,
                            "page": page_no,
                            "element_type": "TEXT",
                            "appendix": current_appendix,
                            "appendix_title": current_appendix_title,
                        },
                    })
                    continue
 
                # Detect regular contract heading.
                heading = find_heading(line)
 
                if heading:
                    if flat_buffer:
                        flush_flat_buffer(output=output,buffer=flat_buffer, doc_id=doc_id, page_no=flat_buffer_page or page_no,
                                         is_heading=flat_buffer_is_heading, appendix=current_appendix, appendix_title=current_appendix_title,)
                        flat_buffer_page = None
                        flat_buffer_is_heading = False
 
                    number = heading["number"]
                    name = heading["name"]

                    # Report-style headings such as "Section 1: Introduction"
                    if heading.get("heading_type") == "report-style":
                        current_section = ensure_section(
                            section_no=number,
                            name=name,
                            page_no=page_no,
                        )
                        current_subsection = None
                        continue

                    parts = number.split(".")
 
                    # Ex. parent section.
                    # 1.21       -> Section = 1.21
                    # 1.21.12    -> Section = 1.21
                    #               Subsection = 1.21.12

                    if len(parts) >= 2:
                        section_no = ".".join(parts[:2])
                    else:
                        section_no = number
 
                    # Section
                    if len(parts) == 2:
                        current_section = ensure_section(
                            section_no=number,
                            name=name,
                            page_no=page_no,
                        )
                        current_subsection = None
                        continue
 
                    # Subsection
                    current_section = ensure_section(
                        section_no=section_no,
 
                        # If parent Section was not previously use its number as temporary name.
                        name=section_no,
                        page_no=page_no,
                    )
 
                    current_subsection = make_subsection_record(
                        doc_id=doc_id,
                        subsection_no=number,
                        name=name,
                        page_no=page_no,
                        appendix=current_appendix,
                        appendix_title=current_appendix_title,
                    )
 
                    current_section["Subsections"].append(current_subsection)
                    continue
 
                # Normal paragraph / text
                if current_subsection is not None:
                    current_subsection["Text"] += ("\n" if current_subsection["Text"] else "") + line
 
                elif current_section is not None:
                    current_section["Text"] += ("\n" if current_section["Text"] else "") + line
 
                else:
                    # Text not associated with any recognized Section/Subsection is kept as flat TEXT.
                    if flat_buffer_page is None:
                        flat_buffer_page = page_no
                    if not flat_buffer:
                        flat_buffer_is_heading = is_simple_heading(line)
                    flat_buffer.append(line)
 
                    # Simple heading buffer
                    if flat_buffer_is_heading:
 
                        # Handles titles split across lines such as:
                        # STATE OF OKLAHOMA CONTRACT WITH
                        # OKLAHOMA COMPLETE HEALTH,
                        # INC.
                        if ends_sentence(line) or not line.endswith(","):
                            flush_flat_buffer(output=output, buffer=flat_buffer, doc_id=doc_id, page_no=flat_buffer_page,
                                is_heading=True,appendix=current_appendix,appendix_title=current_appendix_title,)
                            flat_buffer_page = None
                            flat_buffer_is_heading = False
 
                    # Normal paragraph buffer
                    else:
                        # Keep paragraph lines together until a sentence ends.
                        if ends_sentence(line):
                            flush_flat_buffer(output=output,buffer=flat_buffer,doc_id=doc_id,page_no=flat_buffer_page,
                                is_heading=False,appendix=current_appendix,appendix_title=current_appendix_title,)
                            flat_buffer_page = None
                            flat_buffer_is_heading = False

    # Flush text remaining at the end of the PDF
    if flat_buffer:
        flush_flat_buffer(output=output,buffer=flat_buffer,doc_id=doc_id,page_no=flat_buffer_page or 1,
            is_heading=flat_buffer_is_heading,appendix=current_appendix,appendix_title=current_appendix_title,)
 
    return output

"*****Parsing Function*****"

def parse_pdf_to_records(pdf_path: Path) -> List[Dict[str, Any]]:
    pages = extract_pdf_pages(pdf_path)
    doc_id = pdf_path.stem
    return build_section_json(pages, doc_id)

def parse_pdf_to_json(pdf_path: Path, output_json: Path) -> None:
    parsed = parse_pdf_to_records(pdf_path)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    with output_json.open("w", encoding="utf-8") as f:
        json.dump(parsed, f, indent=2, ensure_ascii=False)

    print(f"Saved parsed JSON to: {output_json}")
 
def main():
    parser = argparse.ArgumentParser(
        description="Parse PDF to JSON with structured text, sections, subsections and Markdown tables representation."
    )
    parser.add_argument("pdf_path", help="Path to PDF file")
    parser.add_argument("output_json", help="Path to output JSON file")
 
    args = parser.parse_args()
    input_file = Path(args.pdf_path)
    output_json = Path(args.output_json)
 
    if input_file.suffix.lower() != ".pdf":
        raise ValueError("This version supports PDF only. Convert DOC/DOCX to PDF first.")
 
    parse_pdf_to_json(input_file, output_json)
 
if __name__ == "__main__":
    main()
