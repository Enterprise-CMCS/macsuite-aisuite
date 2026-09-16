import os
import sys
import json
# import datetime
import tempfile
from pathlib import Path
import pdfplumber
import boto3
from io import BytesIO

root_dir = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root_dir))

from common.utils.helper import Helper
from common.utils.settings import aws_client, aws_session
from data_preprocessing.parsing.parsed_text_data import parsed_text_info
from data_preprocessing.parsing.parsed_bda_table import parse_table_elements_simple
import data_preprocessing.bedrock.bedrock_BDA as bda_mod

from parser import parse_pdf_to_records, parse_docx_to_records

s3 = boto3.client("s3")

#helper functions
def text_record(text, metadata):
    text = (text or "").strip()
    if not text:
        return None
    return {"text": text, "metadata": metadata}
 
def normalize_records(records):
    text_records = []
    table_records = []
 
    for record in records:
        metadata = record.get("metadata", {})
        element_type = metadata.get("element_type")
 
        if element_type == "TABLE":
            if record.get("text"):
                table_records.append(record)
            continue
 
        if element_type == "TEXT":
            if record.get("text"):
                text_records.append(record)
            continue
 
        section_no = record.get("Section") or record.get("section")
        section_name = record.get("Name") or record.get("name")
        section_text = record.get("Text") or record.get("text")
 
        if section_no or section_name or section_text:
            combined_text = f"Section {section_no or ''}: {section_name or ''}\n{section_text or ''}".strip()
            item = text_record(
                combined_text,
                {
                    **metadata,
                    "doc_id": metadata.get("doc_id") or record.get("doc_id"),
                    "page": metadata.get("page") or record.get("Page") or record.get("page"),
                    "element_type": "TEXT",
                    "subtype": "section",
                    "section": section_no,
                    "section_name": section_name,
                },
            )
            if item:
                text_records.append(item)
 
        for subsection in record.get("Subsections", []) or []:
            subsection_no = subsection.get("Subsection") or subsection.get("subsection")
            subsection_name = subsection.get("Name") or subsection.get("name")
            subsection_text = subsection.get("Text") or subsection.get("text")
 
            combined_text = f"Section {section_no or ''} Subsection {subsection_no or ''}: {subsection_name or ''}\n{subsection_text or ''}".strip()
            item = text_record(
                combined_text,
                {
                    **metadata,
                    "doc_id": metadata.get("doc_id") or record.get("doc_id"),
                    "page": subsection.get("Page") or subsection.get("page") or metadata.get("page"),
                    "element_type": "TEXT",
                    "subtype": "subsection",
                    "section": section_no,
                    "section_name": section_name,
                    "subsection": subsection_no,
                    "subsection_name": subsection_name,
                },
            )
            if item:
                text_records.append(item)
    return text_records, table_records

def is_pdf_text_extractable(file_bytes: bytes, min_words: int = 30) -> bool:
    try:
        with pdfplumber.open(BytesIO(file_bytes)) as pdf:
            total = len(pdf.pages)
            sample_indexes = sorted(set([0, 1, 2, total // 2, max(total - 2, 0), total - 1]))
            word_count = sum(len(pdf.pages[i].extract_words() or []) for i in sample_indexes if 0 <= i < total)
            return word_count >= min_words
    except Exception:
        return False

def _bda_output_prefix_for_key(output_prefix, input_key):
    file_name = os.path.basename(input_key)
    file_stem = os.path.splitext(file_name)[0].replace(" ", "_")
    return f"{output_prefix.rstrip('/')}/{file_stem}"
 
def _fetch_bda_result_jsons_for_keys(s3_client, output_bucket, output_prefix, input_keys):
    results = []
    paginator = s3_client.get_paginator("list_objects_v2")
 
    for input_key in input_keys:
        bda_doc_prefix = _bda_output_prefix_for_key(output_prefix, input_key)
 
        for page in paginator.paginate(Bucket=output_bucket, Prefix=bda_doc_prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
 
                if not key.endswith("result.json"):
                    continue
 
                response = s3_client.get_object(Bucket=output_bucket, Key=key)
                results.append(json.load(response["Body"]))
 
    return results

def run_bda_ocr_and_normalize(input_bucket, input_keys, output_bucket):
    input_keys = [key for key in input_keys if key.lower().endswith(".pdf")]

    if not input_keys:
        return [], []
 
    bda_mod.bda_client = aws_client("bedrock-data-automation")
    bda_mod.bda_runtime_client = aws_client("bedrock-data-automation-runtime")
    bda_mod.s3AwsClient = aws_client("s3")
    bda_mod.session = aws_session()
    bda_mod.current_region = bda_mod.session.region_name
    bda_mod.sts_client = aws_client("sts")
    bda_mod.account_id = bda_mod.sts_client.get_caller_identity()["Account"]
 
    project_name = Helper.get_property("projectname")
    project_description = Helper.get_property("projectdescription")
    project_stage = Helper.get_property("ProjectStage")
    output_prefix = Helper.get_property("output_prefix")
    data_automation_v = Helper.get_property("DataAutomationV")
 
    project_arn = bda_mod.create_bda_project(project_name, project_description, project_stage)
 
    da_profile_arn = (
        f"arn:aws:bedrock:{bda_mod.current_region}:"
        f"{bda_mod.account_id}:data-automation-profile/{data_automation_v}"
    )
 
    bda_mod.bda_invoke(
        project_arn=project_arn,
        input_bucket=input_bucket,
        p_files=input_keys,
        output_bucket=output_bucket,
        out_prefix=output_prefix,
        da_profile_arn=da_profile_arn,
        project_stage=project_stage,
    )
 
    raw_bda_results = _fetch_bda_result_jsons_for_keys(
        s3_client=bda_mod.s3AwsClient,
        output_bucket=output_bucket,
        output_prefix=output_prefix,
        input_keys=input_keys,
    )
 
    text_records = []
    table_records = []
 
    for raw_result in raw_bda_results:
        text_records.extend(parsed_text_info(raw_result))
        table_records.extend(parse_table_elements_simple(raw_result))
 
    print(
        f"BDA OCR normalized {len(input_keys)} scanned PDF(s): "
        f"text={len(text_records)}, tables={len(table_records)}"
    )
 
    return text_records, table_records

# custom pdf parsing function
def run_custom_pdf_parser(input_bucket: str,input_keys: list[str], output_bucket: str, text_output_key: str,table_output_key: str,ocr_mode: str = "AUTO",) -> None:
    text_records = []
    table_records = []
    scanned_keys = []

    for input_key in input_keys:

        response = s3.get_object(Bucket=input_bucket, Key=input_key)
        file_bytes = response["Body"].read()
        doc_id = Path(input_key).stem

        suffix = Path(input_key).suffix.lower()
        if suffix not in (".pdf", ".docx"):
            continue

        if suffix == ".docx":
            print(f"Using custom parser for DOCX: {input_key}")
            records = parse_docx_to_records(BytesIO(file_bytes), doc_id)
            doc_text_records, doc_table_records = normalize_records(records)
            text_records.extend(doc_text_records)
            table_records.extend(doc_table_records)
            continue

        if suffix == ".pdf":
            use_custom_parser = True

            if ocr_mode.upper() == "BDA":
                use_custom_parser = False
            elif ocr_mode.upper() == "AUTO":
                use_custom_parser = is_pdf_text_extractable(file_bytes)

            if use_custom_parser:
                print(f"Using custom parser for digital PDF: {input_key}")
                records = parse_pdf_to_records(file_bytes, doc_id)
                doc_text_records, doc_table_records = normalize_records(records)
                text_records.extend(doc_text_records)
                table_records.extend(doc_table_records)
            else:
                print(f"Using BDA/Textract OCR fallback for scanned PDF: {input_key}")
                scanned_keys.append(input_key)

    if scanned_keys:
        bda_text_records, bda_table_records = run_bda_ocr_and_normalize(input_bucket=input_bucket, input_keys=scanned_keys,output_bucket=output_bucket,)
        text_records.extend(bda_text_records)
        table_records.extend(bda_table_records)
 
    s3.put_object(Bucket=output_bucket,Key=text_output_key, Body=json.dumps(text_records, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",)
    
    s3.put_object(Bucket=output_bucket,Key=table_output_key,Body=json.dumps(table_records, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",)


