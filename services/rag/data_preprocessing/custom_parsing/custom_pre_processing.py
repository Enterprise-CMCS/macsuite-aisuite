import sys
from pathlib import Path
from custom_pdf_parser import run_custom_pdf_parser
from common.utils.helper import Helper
import boto3

root_dir = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root_dir))

INPUT_BUCKET = Helper.get_property('input_bucket_name')
OUTPUT_BUCKET = Helper.get_property('output_bucket')

INPUT_PREFIX = Helper.get_property('input_prefix')

PARSED_TEXT_OUTPUT_KEY = f"{Helper.get_property('BDATextOutputFolder')}{Helper.get_property('BDATextOutputFilename')}"
PARSED_TABLE_OUTPUT_KEY = f"{Helper.get_property('BDATableOutputFolder')}{Helper.get_property('BDATableOutputFilename')}"


def list_doc_keys(bucket, prefix):
    s3 = boto3.client("s3")
    keys = []

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith((".pdf", ".docx")):
                keys.append(key)
    return keys

"***custom data preprocessing***"
def process():

    input_document_keys = list_doc_keys(INPUT_BUCKET, INPUT_PREFIX)
    print(input_document_keys)
    if not input_document_keys:
        raise ValueError(f"No PDF files found in s3://{INPUT_BUCKET}/{INPUT_PREFIX}")
 
    print(f"Found {len(input_document_keys)} PDF files to process")
    run_custom_pdf_parser(input_bucket=INPUT_BUCKET,input_keys=input_document_keys,output_bucket=OUTPUT_BUCKET,
                        text_output_key=PARSED_TEXT_OUTPUT_KEY,table_output_key=PARSED_TABLE_OUTPUT_KEY,)
      
def main():
    print("Starting custom PDF preprocessing...")
    process()
    print("Custom PDF preprocessing completed.")
 
if __name__ == "__main__":
    main()
