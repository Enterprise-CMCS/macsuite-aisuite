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


SKIP_SUBTYPES = {"PAGE_NUMBER", "FOOTER", "HEADER"}

MIN_CONTENT_CHARS = 3


def element_page(element):

    page_indices = element.get("page_indices") or []
    if page_indices and page_indices[0] is not None:
        return page_indices[0]
    locations = element.get("locations") or []
    if locations:
        return locations[0].get("page_index")
    return None


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
            "element_type": "TEXT",
            "subtype": "summary+descriptionofdocument",
        }
    }
    
    final_docs = [summary_doc]

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
