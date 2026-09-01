import sys
from pathlib import Path
import os
import asyncio
import json

sys.path.insert(0, str(Path(__file__).parent.parent))

from data_embeddings_storage.database.recursive_text_splitter import RecursiveCharacterTextSplitter
from common.utils.helper import Helper
from common.utils.settings import aws_client
from common.utils.aws_files_access import AwsFilesAccess
from common.utils.logger import log
from data_embeddings_storage.database.creating_main import process_pg_vector_db


log.info(f"***************** File process_rag.py **************************{ __file__}")

def flatten_json(data):
    # log.info ("************************ Function flatten_json Start *********************************")
    flat = []
    if isinstance(data, dict):
        flat.append(data)
    elif isinstance(data, list):
        for item in data:
            flat.extend(flatten_json(item))
    return flat


async def invoke_rag_process():
    log.info(f"***************** invoke_rag_process Starts **************************")
    try:       
        s3_client = aws_client("s3")
        bedrock_runtime = aws_client("bedrock-runtime")
        output_bucket = Helper.get_property("output_bucket")
        output_excel_json_folder_pre_process = Helper.get_property("OutPutExcelJsonFolderPreProcess")
        bda_text_output_folder = Helper.get_property("BDATextOutputFolder")
        bda_table_output_folder = Helper.get_property("BDATableOutputFolder")
        bda_image_output_folder = Helper.get_property("BDAImageOutputFolder")
        str_temp_folder = Helper.get_property("TempFolder")

        if not bda_text_output_folder.endswith('/'):
            bda_text_output_folder += '/'
        if not bda_table_output_folder.endswith('/'):
            bda_table_output_folder += '/'
        if not bda_image_output_folder.endswith('/'):
            bda_image_output_folder += '/'
        if not str_temp_folder.endswith('/'):
            str_temp_folder += '/'

        if not output_excel_json_folder_pre_process.endswith('/'):
            output_excel_json_folder_pre_process += '/'


        str_bda_text_json_filename = Helper.get_property("BDATextOutputFilename")
        str_bda_images_json_filename = Helper.get_property("BDAImageOutputFilename")
        str_bda_tables_json_filename = Helper.get_property("BDATableOutputFilename")

        log.info(f"bedrock_runtime={bedrock_runtime}")
        log.info(f"bda_text_output_folder={bda_text_output_folder}")
        log.info(f"bda_table_output_folder={bda_table_output_folder}")
        log.info(f"bda_image_output_folder={bda_image_output_folder}")

        log.info(f"str_bda_text_json_filename={str_bda_text_json_filename}")
        log.info(f"str_bda_images_json_filename={str_bda_images_json_filename}")
        log.info(f"str_bda_tables_json_filename={str_bda_tables_json_filename}")
        log.info(f"output_excel_json_folder_pre_process={output_excel_json_folder_pre_process}")


        #get BDA Text Data
        log.info(f"Opening Output BDA Texts Json File to access the BDA pre-processed data={ str_bda_text_json_filename}")
        str_bda_text_json_filename = os.path.join(bda_text_output_folder, str_bda_text_json_filename)
        texts_data = Helper.get_json_from_s3(output_bucket, str_bda_text_json_filename)

        # get BDA Images Data
        log.info(f"Opening Output BDA Images Json File to access the BDA pre-processed data={str_bda_images_json_filename}")
        str_bda_images_json_filename = os.path.join(bda_image_output_folder, str_bda_images_json_filename)
        images_data = Helper.get_json_from_s3(output_bucket, str_bda_images_json_filename)

        # get Table Images Data
        log.info(f"Opening Output BDA Table Json File to access the BDA pre-processed data={str_bda_tables_json_filename}")
        str_bda_tables_json_filename = os.path.join(bda_table_output_folder, str_bda_tables_json_filename)
        table_data = Helper.get_json_from_s3(output_bucket, str_bda_tables_json_filename)

        log.info(f"Length of the text_data {len(texts_data)}")
        log.info(f"Length of the image_data {len(images_data)}")
        log.info(f"Length of the table_data {len(table_data)}")

        # This line to access the pre-process the Excel files
        json_files_table = []
        json_files = []
        aws_files_access = AwsFilesAccess(s3_client)

        log.info(f"Processing files from Output files location = { output_excel_json_folder_pre_process}")
        json_files = aws_files_access.aws_file_with_extension(output_bucket, output_excel_json_folder_pre_process, ".json")
        for filename in json_files:
            if filename.endswith(".json"):
                log.info(f"Processing file Excel Json Files {filename} from bucket {output_bucket}")
                data = Helper.get_json_from_s3(output_bucket, filename )
                #with open(file_path, "r", encoding="utf-8") as f:
                #    data = json.load(f)
                if data:
                    #SSSS
                    #log.info(f"data={data}")
                    flat_data = flatten_json(data)
                    #log.info(f"flat_data={flat_data}")
                    valid_items = [item for item in flat_data if isinstance(item, dict) and item.get("text")]
                    json_files_table.extend(valid_items)
                log.info(f"Processed: {filename}")

        # Write Data to temp file
        log.info(f"Total valid JSON entries: {len(json_files_table)}")


        # Combine all JSON data (texts, images, table, and excel files)
        all_json_data = texts_data + images_data + table_data + json_files_table
        log.info(f"Total number of documents before splitting: {len(all_json_data)}")

        # Initialize text splitter
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1024,
            chunk_overlap=30,
            length_function=len,
            is_separator_regex=False,
        )

        # Split documents and maintain JSON format
        log.info("Split documents and maintain JSON format")

        split_docs = []
        for doc in all_json_data:
            metadata = doc.get('metadata', {})
            element_type = metadata.get('element_type')
            if element_type == "TEXT":
                text_content = doc.get('text', "")
                # Split the text content into chunks
                text_chunks = text_splitter.split_text(text_content)

                # Create a new JSON entry for each chunk
                for idx, chunk in enumerate(text_chunks):
                    split_entry = {
                        "text": chunk,
                        "metadata": {
                            **metadata,
                        }
                    }
                    split_docs.append(split_entry)
            else:
                split_docs.append(doc)

        log.info(f"Total number of chunks after splitting: split_docs = {len(split_docs)}")

        # Save split documents to JSON file
        log.info("Save split documents to JSON file")

        str_rag_split_out_put_file = Helper.get_property("RagSplitOutPutFile")
        rag_split_out_put_bucket = Helper.get_property("RagSplitOutPutBucket")
        rag_split_out_put_folder = Helper.get_property("RagSplitOutPutFolder")

        if not rag_split_out_put_folder.endswith('/'):
            rag_split_out_put_folder += '/'
        str_rag_split_out_put_file = os.path.join(rag_split_out_put_folder, str_rag_split_out_put_file)

        log.info(f"invoke_rag_process() Split OutPut File name str_rag_split_out_put_file={ str_rag_split_out_put_file}")

        s3_client.put_object(Bucket=rag_split_out_put_bucket, Key=str_rag_split_out_put_file, Body=json.dumps(split_docs, indent=4), ContentType='application/json; charset=utf-8')


        log.info(f"Split documents saved to: {str_rag_split_out_put_file}")
        """ Moving this the following code to generating embeddings"""
        # Decide to perform either pgvector or ChromaDB
        chroma_db_or_pg_vector = Helper.get_property("PGVector")
        log.info(f"Flag PGVector = {chroma_db_or_pg_vector}")
        bootlretval = False
        
        if chroma_db_or_pg_vector == "CHROMADB":
            # SSSS
            #bootlretval = process_chroma_db(bedrock_runtime, split_docs)
            log.info(f" After calling ProcessChromaDB. Returning bootlretval={bootlretval}")
        else:
            # SSSS
            bootlretval = True
            process_pg_vector_db_status = await process_pg_vector_db()
            log.info(
                f'After calling ProcessPGVectorDB. Returning ProcessPGVectorDB_Status \nTotal = {process_pg_vector_db_status["total"]} \nPostgres Processed = {process_pg_vector_db_status["postgres_processed"]} \nFailed={process_pg_vector_db_status["failed"]}, \nError: {process_pg_vector_db_status["errors"]}')
        log.info(f"***************** invoke_rag_process() Ends **************************")
        return bootlretval

    except Exception as lclEx:
        Helper.print_exception("invoke_rag_process()", lclEx, "invoke_rag_process() ")
        raise lclEx

"""IF you want to run this file locally or independent, Uncomment the below code and run."""

def main():
    print("******************************* Starting main from rag_process file")
    asyncio.run(invoke_rag_process())
    print("******************************* End main from rag_process file")


if __name__ == "__main__":
    main()
