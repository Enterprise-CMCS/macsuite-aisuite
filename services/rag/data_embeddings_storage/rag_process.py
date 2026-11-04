import asyncio
import hashlib
import re
from common.utils.helper import Helper
from common.utils.logger import log
from common.utils.aws_files_access import object_keys
from data_preprocessing.bedrock.bda_results import RAG_OUTPUT_PREFIX
from data_embeddings_storage.database.data_processing_embeddings import EmbeddingProcessor
from data_embeddings_storage.database.table_setup import create_embedding_table


 

def embeddings_table_for_key(key):
    base = key.rsplit("/", 1)[-1].removesuffix(".json")
    slug = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_") or "doc"
    name = f"emb_{slug}"
    if len(name) > 41:
        digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
        name = f"{name[: 41 - 9].rstrip('_')}_{digest}"
    return name


def is_rag_output_key(key):
    return key.startswith(RAG_OUTPUT_PREFIX) and key.endswith(".json")


async def invoke_rag_process(key):

    bucket = Helper.get_property("output_bucket")

    log.info(f"invoke_rag_process() Loading s3://{bucket}/{key}")
    documents = [
        doc for doc in Helper.get_json_from_s3(bucket, key)
        if isinstance(doc, dict) and doc.get("text")
    ]
    log.info(f"invoke_rag_process() {len(documents)} document(s) to embed from {key}")
    if not documents:
        return {"total": 0, "postgres_processed": 0, "failed": 0, "errors": []}

    table_name = embeddings_table_for_key(key)
    log.info(f"invoke_rag_process() Using embeddings table {table_name}")
    await create_embedding_table(table_name)
    status = await EmbeddingProcessor(table_name=table_name).process_data(documents, batch_size=30)
    if status["failed"]:
        raise RuntimeError(f"Embedding failed for {status['failed']} document(s) of {key}: {status['errors'][:5]}")
    return status


<<<<<<< HEAD
def handler(event, context=None):
    
    keys = [key for key in object_keys(event) if is_rag_output_key(key)]
    if not keys:
        log.info(f"No RAG_Output JSON objects in event. Skipping. Event={event}")
        return {"status": "skipped"}
    return {"status": "completed", "results": {key: asyncio.run(invoke_rag_process(key)) for key in keys}}


# def main():
#     if len(sys.argv) < 2:
#         raise SystemExit("usage: python -m data_embeddings_storage.rag_process RAG_Output/<folders>/<file name>.json")
#     print(asyncio.run(invoke_rag_process(sys.argv[1])))


# if __name__ == "__main__":
#     main()
=======
        #get BDA Text Data
        log.info(f"Opening Output BDA Texts Json File to access the BDA pre-processed data={ str_bda_text_json_filename}")
        str_bda_text_json_filename = f"{bda_text_output_folder}{str_bda_text_json_filename}"
        texts_data = Helper.get_json_from_s3(output_bucket, str_bda_text_json_filename)

        #with open(str_bda_text_json_file_name, "r", encoding='utf-8') as f:
        #    texts_data = json.load(f)

        # str_bda_images_json_file_name = "output-BDA-texts.json"

        # get BDA Images Data
        log.info(f"Opening Output BDA Images Json File to access the BDA pre-processed data={str_bda_images_json_filename}")
        str_bda_images_json_filename = f"{bda_image_output_folder}{str_bda_images_json_filename}"
        images_data = Helper.get_json_from_s3(output_bucket, str_bda_images_json_filename)
        #with open(str_bda_images_json_file_name, "r", encoding='utf-8') as f:
        #    images_data = json.load(f)


        # get Table Images Data
        log.info(f"Opening Output BDA Table Json File to access the BDA pre-processed data={str_bda_tables_json_filename}")
        str_bda_tables_json_filename = f"{bda_table_output_folder}{str_bda_tables_json_filename}"
        table_data = Helper.get_json_from_s3(output_bucket, str_bda_tables_json_filename)
        #with open(str_bda_images_json_file_name, "r", encoding='utf-8') as f:
        #    images_data = json.load(f)

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

        '''
        for filename in os.listdir(strOutPutExcelJsonFolderPreProcess):
            if filename.endswith(".json"):
                file_path = os.path.join(strOutPutExcelJsonFolderPreProcess, filename)
                log.info("Processing file Excel Json Files " + file_path)
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data:
                        flat_data = flatten_json(data)
                        valid_items = [item for item in flat_data if isinstance(item, dict) and item.get("text")]
                        json_files_table.extend(valid_items)
                log.info(f"Processed: {filename}")
        
        log.info(f"Total valid JSON entries: {len(json_files_table)}")
        
        # Combine all JSON data (texts, images, and excel files)
        all_json_data = texts_data + images_data + json_files_table
        log.info(f"Total number of documents before splitting: {len(all_json_data)}")
        '''

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

        # strRagSplitOutPutFile = "split_final_document_data.json"
        # strRagSplitOutPutFile= os.getenv("RagSplitOutPutFile")
        str_rag_split_out_put_file = Helper.get_property("RagSplitOutPutFile")
        rag_split_out_put_bucket = Helper.get_property("RagSplitOutPutBucket")
        rag_split_out_put_folder = Helper.get_property("RagSplitOutPutFolder")

        if not rag_split_out_put_folder.endswith('/'):
            rag_split_out_put_folder += '/'
        str_rag_split_out_put_file = f"{rag_split_out_put_folder}{str_rag_split_out_put_file}"

        log.info(f"invoke_rag_process() Split OutPut File name str_rag_split_out_put_file={ str_rag_split_out_put_file}")

        s3_client.put_object(Bucket=rag_split_out_put_bucket, Key=str_rag_split_out_put_file, Body=json.dumps(split_docs, indent=4), ContentType='application/json; charset=utf-8')

        #with open(strRagSplitOutPutFile, "w", encoding="utf-8") as f:
        #    json.dump(split_docs, f, ensure_ascii=False, indent=4)

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
>>>>>>> feature/regulatory-reference-decision-agent
