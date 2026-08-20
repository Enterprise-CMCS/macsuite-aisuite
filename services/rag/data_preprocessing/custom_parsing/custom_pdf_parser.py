import json
import tempfile
from pathlib import Path
 
import boto3
 
from parser import parse_pdf_to_records
 
 
s3 = boto3.client("s3")
 
 
def run_custom_pdf_parser(input_bucket: str,input_keys: list[str],output_bucket: str,text_output_key: str,table_output_key: str,) -> None:

    text_records = []
    table_records = []
 
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir = Path(temp_dir)
        print(temp_dir)
        for input_key in input_keys:
            if not input_key.lower().endswith(".pdf"):
                continue 
            local_pdf = temp_dir / Path(input_key).name
            s3.download_file(input_bucket, input_key, str(local_pdf))
 
            records = parse_pdf_to_records(local_pdf)
 
            for record in records:
                element_type = record.get("metadata", {}).get("element_type")
 
                if element_type == "TABLE":
                    table_records.append(record)
                elif element_type == "TEXT":
                    text_records.append(record)
 
    s3.put_object(
        Bucket=output_bucket,
        Key=text_output_key,
        Body=json.dumps(text_records, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    s3.put_object(
        Bucket=output_bucket,
        Key=table_output_key,
        Body=json.dumps(table_records, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )