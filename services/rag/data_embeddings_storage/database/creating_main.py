import asyncio
import os
import json
import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).parent.parent.parent))

from database.init_database import setup_pgvector_extension
from database.table_setup import create_embedding_table
from database.data_processing_embeddings import EmbeddingProcessor
from common.utils.helper import Helper
from common.utils.logger import log
# from chucking.chunking_data import recursive_chucking

async def process_pg_vector_db():

    print("\n"+ "=" *25)
    print("Embeddings Pipline: From Raw Data -> aws Bedrock -> Postgres + HNSW")
    print("=" *25 + "\n")

    print("Step 1: Setting up pgvector extension...")
    try:
        await setup_pgvector_extension()
    except Exception as e:
        print(f"Failed to setup pgvector extension: {e}")

    print("\nStep 2: Creating embeddings table and indexes...")
    await create_embedding_table()
    
    print("\nStep 3: Loading data from S3...")
    
    # Load the split document data from S3
    rag_split_out_put_bucket = Helper.get_property("RagSplitOutPutBucket")
    rag_split_out_put_folder = Helper.get_property("RagSplitOutPutFolder")
    rag_split_out_put_file = Helper.get_property("RagSplitOutPutFile")
    
    if not rag_split_out_put_folder.endswith('/'):
        rag_split_out_put_folder += '/'
    
    s3_file_key = os.path.join(rag_split_out_put_folder, rag_split_out_put_file)
    
    log.info(f"Loading data from S3: Bucket={rag_split_out_put_bucket}, Key={s3_file_key}")
    print(f"Loading from S3: s3://{rag_split_out_put_bucket}/{s3_file_key}")
    
    all_chunks = Helper.get_json_from_s3(rag_split_out_put_bucket, s3_file_key)
    log.info(f"Loaded {len(all_chunks)} chunks from S3")
    print(f"Successfully loaded {len(all_chunks)} chunks from S3")

    print("\nStep 4: Processing embeddings...")
    processor = EmbeddingProcessor()
    status = await processor.process_data(all_chunks, batch_size=30)

    print(f"\nProcessing Summary:")
    print(f"Total: {status['total']}")
    print(f"Postgres Processed: {status['postgres_processed']}")
    print(f"Failed: {status['failed']}")
    if status['errors']:
        print(f" - Errors: {len(status['errors'])} ")

    print("\nVerifying database content...")
    
    return status
  