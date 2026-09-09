import os
import json
import itertools
import re
import boto3
import datetime

from data_preprocessing.bedrock.bda_results import BDAResults
from common.utils.settings import aws_client,aws_session
from common.utils.helper import Helper
from common.utils.logger import log


# Page furniture carries no requirement, so it is not retrievable text. The page
# numbers are read out of these elements first, in printed_page_map().
SKIP_SUBTYPES = {"PAGE_NUMBER", "FOOTER", "HEADER"}

MIN_CONTENT_CHARS = 3

# BDA usually files the printed number as its own PAGE_NUMBER element. On the odd
# page it lands in the footer or header instead, so those are read as well - but
# only after every PAGE_NUMBER has had its say.
PAGE_LABEL_SUBTYPES = ("PAGE_NUMBER", "FOOTER", "HEADER")

MARKDOWN_NOISE = re.compile(r"[*_#`]+")

# "Page 12", "Page 12 of 183". A page number written in prose is not accepted, so
# a footer that happens to mention a page cannot be mistaken for one.
LABELLED_PAGE = re.compile(r"^page\s+([0-9ivxlcdm]{1,9})(?:\s+of\s+[0-9ivxlcdm]{1,9})?$", re.IGNORECASE)

ARABIC_PAGE = re.compile(r"^\d{1,4}$")

# Ordered high to low so the same table reads and writes roman numerals.
ROMAN_NUMERALS = (("m", 1000), ("cm", 900), ("d", 500), ("cd", 400), ("c", 100), ("xc", 90),
                  ("l", 50), ("xl", 40), ("x", 10), ("ix", 9), ("v", 5), ("iv", 4), ("i", 1))


def element_page(element):

    page_indices = element.get("page_indices") or []
    if page_indices and page_indices[0] is not None:
        return page_indices[0]
    locations = element.get("locations") or []
    if locations:
        return locations[0].get("page_index")
    return None


def roman_to_int(token):
    """0 if the token is not a roman numeral, so it doubles as the check."""
    token = token.lower()
    total = 0
    read = 0
    for numeral, value in ROMAN_NUMERALS:
        while token.startswith(numeral, read):
            total += value
            read += len(numeral)
    return total if read == len(token) else 0


def int_to_roman(number):
    numerals = []
    for numeral, value in ROMAN_NUMERALS:
        while number >= value:
            numerals.append(numeral)
            number -= value
    return "".join(numerals)


def page_token(text, bare_ok):
    """The page number printed on the page, or "" if this text is not one."""
    text = MARKDOWN_NOISE.sub("", text or "").strip().strip("[](){}|.,:;-–— ")
    if not text:
        return ""

    labelled = LABELLED_PAGE.match(text)
    if labelled:
        text = labelled.group(1)
    elif not bare_ok:
        # BDA breaks a footer like "RFP Boilerplate I 07012019" into pieces, and a
        # stray "I" is not page one. Away from a PAGE_NUMBER element a number has
        # to say that is what it is.
        return ""

    if ARABIC_PAGE.match(text):
        return text
    # Lower-cased because BDA reads the odd numeral as capitals, and a run that
    # goes xviii, xix, XX reads like a bug rather than a page number.
    if text.isalpha() and roman_to_int(text):
        return text.lower()
    return ""


def page_counting(token, page):
    """How this page's printed number relates to its position in the file."""
    if token.isdigit():
        return int(token) - page, False
    return roman_to_int(token) - page, True


def settle_page_numbers(labels):
    """Fill in the pages BDA read no number on, and correct the ones it misread.

    Printed numbers run one to a page, so the step between a page's position in
    the file and the number printed on it holds steady for as long as the
    numbering does. Where the numbered pages either side of a page agree on that
    step, the page between them has to follow it - which fills the cover and any
    other page BDA left bare, and overrules the odd misreading, such as the "I" of
    a footer picked up as page one. Where the neighbours disagree the page is left
    exactly as BDA read it, so numbering that restarts part way through a document
    survives: front matter ending at xxi followed by a body restarting at 1 is a
    disagreement, not an error.
    """
    counting = {page: page_counting(token, page) for page, token in labels.items()}
    counting = {page: how for page, how in counting.items() if how[0] + page > 0}
    if not counting:
        return labels

    settled = dict(labels)
    for page in range(0, max(counting) + 1):
        before = [known for known in counting if known < page]
        after = [known for known in counting if known > page]
        neighbours = {counting[max(before)] if before else None,
                      counting[min(after)] if after else None}
        neighbours.discard(None)
        if len(neighbours) != 1:
            continue

        step, roman = neighbours.pop()
        if page + step > 0:
            settled[page] = int_to_roman(page + step) if roman else str(page + step)

    return settled


def printed_page_map(elements):
    """Map each BDA page index to the page number printed on that page."""
    labels = {}
    for sub_type in PAGE_LABEL_SUBTYPES:
        for element in elements or []:
            if element.get("type") != "TEXT" or element.get("sub_type") != sub_type:
                continue

            page = element_page(element)
            if page is None or page in labels:
                continue

            token = page_token((element.get("representation") or {}).get("markdown"),
                               bare_ok=sub_type == "PAGE_NUMBER")
            if token:
                labels[page] = token

    if not labels:
        log.info("printed_page_map() No printed page numbers found, citations will use file position")
        return {}

    settled = settle_page_numbers(labels)
    changed = sum(1 for page, token in settled.items() if labels.get(page) != token)
    log.info(f"printed_page_map() Read {len(labels)} printed page number(s), worked out {changed} more "
             f"from the pages around them, first={settled.get(min(settled))} "
             f"last={settled.get(max(settled))}")
    return settled


def parsed_text_info(source_data):
    log.info("***************** Parsed_text_data.parsed_text_info start *****************************")
    doc_data = source_data.get('document', {})

    source_key = source_data.get("metadata", {}).get("s3_key")
    doc_id = source_key.rsplit("/", 1)[-1].rsplit(".", 1)[0]

    summary_text = f"# Title\n{doc_data.get('description', '')}\n\n## Summary\n{doc_data.get('summary', '')}"
    log.debug(f"source_key={source_key}")
    log.debug(f"doc_id={doc_id}")
    log.debug(f"summary_text={summary_text}")
    summary_doc = {
        "text": summary_text,
        "doc_id": doc_id,
        "metadata": {
            "doc_id": doc_id,
            "page": None,
            "printed_page": "",
            "element_type": "TEXT",
            "subtype": "summary+descriptionofdocument",
        }
    }

    final_docs = [summary_doc]

    printed_pages = printed_page_map(source_data.get("elements", []))

    texts = []
    skipped_furniture = 0
    skipped_empty = 0
    for element in source_data.get("elements", []):
        if element.get("type") == "TEXT":
            sub_type = element.get("sub_type") or "PARAGRAPH"
            if sub_type in SKIP_SUBTYPES:
                skipped_furniture += 1
                continue

            page = element_page(element)

            text_content = element.get("representation", {}).get("markdown", "").strip()

            if not text_content or text_content == '[ ]':
                continue
            if len(re.sub(r"[^0-9A-Za-z]+", "", text_content)) < MIN_CONTENT_CHARS:
                skipped_empty += 1
                continue
            texts.append({
                "text": text_content,
                "page": page,
                "printed_page": printed_pages.get(page, ""),
                "element_type": "TEXT",
                "subtype": sub_type.lower(),
            })

    log.info(f"parsed_text_info() Kept {len(texts)} text element(s), skipped {skipped_furniture} "
             f"page-furniture and {skipped_empty} with no retrievable content")

    for idx, elem in enumerate(texts):
        paragraph_id = f"{doc_id}::p{elem['page'] or 0}::e{idx}"
        final_docs.append({
            "doc_id": paragraph_id,
            "text": elem['text'],
            "metadata": {
                "doc_id": doc_id,
                "page": elem['page'],
                "printed_page": elem['printed_page'],
                "element_type": "TEXT",
                "subtype": elem['subtype'],
            }
        })

    return final_docs


def invoke_parsed_text_data():
    log.info(f"***************** invoke_parsed_text_data Starts. __name__={__name__}")
    try:

        output_bucket = Helper.get_property("output_bucket")
        output_prefix = Helper.get_property("output_prefix")
        bda_text_output_folder = Helper.get_property("BDATextOutputFolder")
        bda_text_output_filename = Helper.get_property("BDATextOutputFilename")

        aws_client_s3 = aws_client("s3")

        log.info(f"invoke_parsed_text_data() output_bucket={output_bucket} output_prefix={output_prefix}")
        log.debug(f"invoke_parsed_text_data() Calling fetch_parsed_bda_results")

        bda_results = BDAResults()

        parsed_data = bda_results.fetch_parsed_bda_results(output_bucket, output_prefix, aws_client_s3, "Text")
        log.debug(f"invoke_parsed_text_data() After calling  fetch_parsed_bda_results")
        log.info(f"invoke_parsed_text_data() Loaded Text {len(parsed_data)} document(s)")
        text_data = []

        log.debug(f"invoke_parsed_text_data() Parsing loaded document")
        for data in parsed_data:
            docs = parsed_text_info(data)
            text_data.extend(docs)

        # BDATextOutputFilename = "Output-BDA-texts.json"
        bda_text_output_filename = os.path.join(bda_text_output_folder, bda_text_output_filename)

        log.debug(
            f"invoke_parsed_text_data() Creating One file for {len(text_data)} Parsed document. Name of Output Text File Name={bda_text_output_filename}")
        s3_client = boto3.client('s3')
        s3_client.put_object(Bucket=output_bucket, Key=bda_text_output_filename, Body=json.dumps(text_data,indent=2))
        ####
        #with open(BDATextOutputFilename, "w", encoding='utf-8') as f:
        #    log.info(f"Dumping data to file {BDATextOutputFilename}")
        #    json.dump(Text_data, f, indent=2)

        ####
        log.info(f"*****************invoke_parsed_text_data() Ends .__name__={__name__}. Returning True.")
        return True

    except Exception as lclAllEx:
        Helper.print_exception("invoke_parsed_text_data", lclAllEx,"Error occurred in function invokeParsedTextData.")
        # Re-raise the same exception
        log.error(f"***************** invoke_parsed_text_data End. __name__={__name__}. . Returning False.")
        return False
