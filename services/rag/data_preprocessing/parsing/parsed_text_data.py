import os
import json 
import itertools
import boto3
import datetime

from data_preprocessing.bedrock.bda_results import BDAResults
from common.utils.settings import aws_client,aws_session
from common.utils.helper import Helper
from common.utils.logger import log


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
    for element in source_data.get("elements", []):
        if element.get("type") == "TEXT":
            text_content = element.get("representation", {}).get("markdown", "").strip()
            
            if not text_content or text_content == '[ ]':
                continue
            texts.append({
                "text": text_content,
                "page": element.get("page_indices", [None])[0],
                "element_type": "TEXT", 
                "subtype": "paragraph",
            })

    for idx, elem in enumerate(texts):
        paragraph_id = f"{doc_id}::p{elem['page'] or 0}"
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
