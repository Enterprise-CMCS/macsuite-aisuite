import os
import uuid
import re
import json 
import boto3

from common.utils.settings import aws_client
from common.utils.helper import Helper
from common.utils.logger import log
from data_preprocessing.bedrock.bda_results import BDAResults
from data_preprocessing.parsing.parsed_toc import contents_pages

def _parse_markdown_table(md):

    rows = []
    if not md:
        return rows

    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            continue

        if re.fullmatch(r"\|?\s*-{1,}\s*(\|\s*-{1,}\s*)+\|?\s*", line):
            continue
        if "|" not in line:
            continue

        cells = [c.strip() for c in re.split(r"\|", line)]

        if cells: 

            if line.startswith("|"):
                cells = cells[1:] if cells[0] == "" else cells
        
            if line.endswith("|"):
                cells = cells[:-1] if cells[-1] == "" else cells

        if any(cell != "" for cell in cells):
            rows.append(cells)
            
    return rows


def _is_num(x: str) -> bool:
    x = x.strip().replace(",", "")
    return bool(re.fullmatch(r"-?\d+(\.\d+)?%", x) or re.fullmatch(r"-?\d+(\.\d+)?", x))


def _infer_headers(rows, provided):

    if provided:
        return provided, 0
    if not rows:
        return [], 0
    first = rows[0]
    nonnum = sum(not _is_num(c) for c in first)

    if nonnum >= max(1, len(first) // 2):
        return first, 1
    return [f"Column_{i+1}" for i in range(len(first))], 0


def _has_row_headers(data):

    if not data or not data[0]:
        return False
    first_col = [r[0] for r in data if r]
    texty = sum((not _is_num(v)) and v != "" for v in first_col)
    return texty >= max(2, len(first_col) // 3)




def parse_table_elements_simple(source_data):

    results = []
    source_key = source_data.get("metadata", {}).get("s3_key")
    doc_name = source_key.rsplit("/", 1)[-1].rsplit(".", 1)[0] if source_key else "unknown"
    log.info(f"parse_table_elements_simple() source_key={source_key}")
    toc_pages = contents_pages(source_data)
    skipped_contents = 0

    for elements in source_data.get("elements"):
        element_type = elements.get("type")
        if element_type != "TABLE":
            continue
        title = elements.get("title") or ""
        text    = (elements.get("representation") or {}).get("markdown","")
        table_id= elements.get("id") or str(uuid.uuid4())
        page_index = elements.get("locations", [{}])[0].get("page_index")
        csv_uri = elements.get("csv_s3_uri", None)
        if page_index in toc_pages:
            skipped_contents += 1
            continue
        log.debug(f"parse_table_elements_simple() element_type={element_type}, title={title}, table_id={table_id} text={text}")
        each_table_rows = _parse_markdown_table(text)
        
        if not each_table_rows:
            continue

        hdrs, start_idx = _infer_headers(each_table_rows, elements.get("headers") or [])
        data_rows = each_table_rows[start_idx:]

        if data_rows and len(hdrs) != len(data_rows[0]):
            maxlen = max(len(r) for r in data_rows)
            if len(hdrs) < maxlen:
                hdrs += [f"Column_{i+1}" for i in range(len(hdrs), maxlen)]
            data_rows = [(r + [""] * (len(hdrs) - len(r)))[:len(hdrs)] for r in data_rows]

        results.append({
            "doc_id": f"{doc_name}::{table_id}",
            "text": text.strip(),
            "metadata": {
                "doc_id": doc_name,
                "element_type": "TABLE",
                "page": page_index,
            }
        })

    log.info(f"parse_table_elements_simple() Kept {len(results)} table(s), skipped "
             f"{skipped_contents} on the printed-contents pages {sorted(toc_pages)}")
    return results


def invoke_parsed_bda_data():
    try:
        log.info(f"***************** invoke_parsed_bda_data Starts. __name__={__name__}")
        awsClientS3 = aws_client("s3")

        output_bucket = Helper.get_property("output_bucket")
        output_prefix = Helper.get_property("output_prefix")

        log.info(f"output_bucket={output_bucket}, output_prefix={output_prefix}")
        log.debug(f"Calling fetch_parsed_bda_results")

        bdaResults = BDAResults()
        parsed_data = bdaResults.fetch_parsed_bda_results(output_bucket, output_prefix, awsClientS3, "BDATable")
        log.info(f"Loaded BDA Table {len(parsed_data)} document")
        test_table = []
        log.debug(f"Parsing loaded document")
        for dataObj in parsed_data:
            table_data = parse_table_elements_simple(dataObj)
            test_table.extend(table_data)

        bda_table_output = Helper.get_property("BDATableOutputFolder")

        bda_table_output_filename = os.path.join(
            bda_table_output,
            Helper.get_property("BDATableOutputFilename"))
        log.debug(f"Output Table File Name={bda_table_output_filename}")

        s3_client = boto3.client('s3')
        s3_client.put_object(Bucket=output_bucket, Key=bda_table_output_filename, Body=json.dumps(test_table,indent=2), ContentType='application/json; charset=utf-8')

        log.info(f"***************** invoke_parsed_bda_data End. __name__={__name__}")
        return True

    except Exception as lclEx:
        Helper.print_exception("invoke_parsed_bda_data", lclEx,"Error occurred in function invoke_parsed_bda_data.")
        # Re-raise the same exception
        log.info(f"***************** invoke_parsed_bda_data End. __name__={__name__}")
        return False
