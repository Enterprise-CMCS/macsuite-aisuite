import sys, os, asyncio, json
from pathlib import Path
 
sys.path.insert(0, str(Path(__file__).parent.parent))
 
from data_embeddings_storage.database.recursive_text_splitter import RecursiveCharacterTextSplitter
from common.utils.helper import Helper
from common.utils.settings import aws_client
from common.utils.aws_files_access import AwsFilesAccess
from common.utils.logger import log
from data_embeddings_storage.database.creating_main import process_pg_vector_db
 
 
log.info(f"***************** File process_rag.py **************************{__file__}")
 
def check_s3_folder(folder):
    if not folder.endswith("/"):
        folder += "/"
    return folder
 
def flatten_json(data):
    flat = []
    if isinstance(data, dict):
        flat.append(data)
    elif isinstance(data, list):
        for item in data:
            flat.extend(flatten_json(item))
    return flat
 
def get_json_list_from_s3(bucket, key):
    data = Helper.get_json_from_s3(bucket, key)
    if data is None: 
        return []
    if isinstance(data, list):
        return data
    return [data]

def split_text_documents(all_json_data):

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1024, 
        chunk_overlap=30, 
        length_function=len, 
        is_separator_regex=False
    )
 
    split_docs = []
    for doc in all_json_data:
        if not isinstance(doc, dict) or not doc.get("text"): 
            continue
 
        metadata = doc.get("metadata", {})
        if metadata.get("element_type") != "TEXT":
            split_docs.append(doc)
            continue
 
        for idx, chunk in enumerate(text_splitter.split_text(doc.get("text", ""))):
            split_docs.append({"text": chunk, "metadata": {**metadata, "chunk_index": idx}})
 
    return split_docs
 
 
async def invoke_rag_process():
    log.info("***************** invoke_rag_process Starts **************************")
 
    try:
        s3_client = aws_client("s3")
        output_bucket = Helper.get_property("output_bucket")
 
        text_folder = check_s3_folder(Helper.get_property("CustomTextOutputFolder", default=Helper.get_property("BDATextOutputFolder")))
        table_folder = check_s3_folder(Helper.get_property("CustomTableOutputFolder", default=Helper.get_property("BDATableOutputFolder")))
 
        text_file = Helper.get_property("CustomTextOutputFilename", default="custom-parsed-texts.json")
        table_file = Helper.get_property("CustomTableOutputFilename", default="custom-parsed-tables.json")
 
        text_key, table_key = f"{text_folder}{text_file}", f"{table_folder}{table_file}"
 
        log.info(f"Loading custom parsed text data from s3://{output_bucket}/{text_key}")
        texts_data = get_json_list_from_s3(output_bucket, text_key)
 
        log.info(f"Loading custom parsed table data from s3://{output_bucket}/{table_key}")
        table_data = get_json_list_from_s3(output_bucket, table_key)
 
        all_json_data = texts_data + table_data
 
        log.info(f"Length of custom text data: {len(texts_data)}")
        log.info(f"Length of custom table data: {len(table_data)}")
        log.info(f"Total number of documents before splitting: {len(all_json_data)}")
 
        split_docs = split_text_documents(all_json_data)
        log.info(f"Total number of chunks after splitting: {len(split_docs)}")
 
        rag_bucket = Helper.get_property("RagSplitOutPutBucket")
        rag_folder = check_s3_folder(Helper.get_property("RagSplitOutPutFolder"))
        rag_key = f'{rag_folder}{Helper.get_property("RagSplitOutPutFile")}'
 
        log.info(f"Saving split RAG output to s3://{rag_bucket}/{rag_key}")
        s3_client.put_object(
            Bucket=rag_bucket,
            Key=rag_key,
            Body=json.dumps(split_docs, indent=4),
            ContentType="application/json; charset=utf-8",
        )
 
        if Helper.get_property("PGVector") == "CHROMADB":
            log.info("CHROMADB path is currently disabled.")
            return False
 
        status = await process_pg_vector_db()
        log.info(
            f'After ProcessPGVectorDB: Total={status["total"]}, '
            f'Postgres Processed={status["postgres_processed"]}, '
            f'Failed={status["failed"]}, Errors={status["errors"]}'
        )
 
        log.info("***************** invoke_rag_process() Ends **************************")
        return True
 
    except Exception as exc:
        Helper.print_exception("invoke_rag_process()", exc, "invoke_rag_process() ")
        raise
def main():
    print("******************************* Starting main from rag_process file")
    asyncio.run(invoke_rag_process())
    print("******************************* End main from rag_process file")
if __name__ == "__main__":
    main()
