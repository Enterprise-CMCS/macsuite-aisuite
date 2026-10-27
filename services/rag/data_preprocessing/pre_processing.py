from common.utils.helper import Helper
from common.utils.aws_files_access import object_keys
from data_preprocessing.bedrock import bedrock_BDA as bda
from data_preprocessing.bedrock.bda_results import OUTPUT_FOLDERS, per_doc_output_key, rag_output_key
from data_preprocessing.parsing import parsed_images
from data_preprocessing.parsing import parsed_text_data
from data_preprocessing.parsing import parsed_bda_table

SUPPORTED_EXTENSIONS = (".pdf", ".docx")


def process(source_key):

    ret_value = bda.invoke_bedrock_bda(source_key)
    if not ret_value:
        print(f"Value Returned by function invoke_bedrock_bda() is {ret_value}. Please check.")
        raise Exception(f"Value Returned by function invoke_bedrock_bda() is {ret_value}. Please check.")
    else:
        print(f"Value Returned by function invoke_bedrock_bda() is {ret_value}.")

    ret_value = parsed_text_data.invoke_parsed_text_data(source_key)
    if not ret_value:
        print(f"Value Returned by function invoke_parsed_text_data() is {ret_value}. Please check.")
        raise Exception(f"Value Returned by function invoke_parsed_text_data() is {ret_value}. Please check.")
    else:
        print(f"Value Returned by function invoke_parsed_text_data() is {ret_value}.")

    ret_value = parsed_bda_table.invoke_parsed_bda_data(source_key)
    if not ret_value:
        print(f"Value Returned by function invoke_parsed_bda_data() is {ret_value}. Please check.")
        raise Exception(f"Value Returned by function invoke_parsed_bda_data() is {ret_value}. Please check.")
    else:
        print(f"Value Returned by function invoke_parsed_bda_data() is {ret_value}.")

    ret_value = parsed_images.invoke_parsed_images_data(source_key)
    if not ret_value:
        print(f"Value Returned by function invoke_parsed_images_data() is {ret_value}. Please check.")
        raise Exception(f"Value Returned by function invoke_parsed_images_data() is {ret_value}. Please check.")
    else:
        print(f"Value Returned by function invoke_parsed_images_data() is {ret_value}.")

    return ret_value


def merge_outputs_to_rag_output(source_key):

    bucket = Helper.get_property("output_bucket")
    output_keys = [per_doc_output_key(source_key, kind) for kind in OUTPUT_FOLDERS]

    all_docs = []
    for output_key in output_keys:
        all_docs.extend(Helper.get_json_from_s3(bucket, output_key))
    documents = [doc for doc in all_docs if doc and doc.get("text")]

    rag_key = rag_output_key(source_key)
    Helper.create_file(bucket, rag_key, documents)
    print(f"Wrote {len(documents)} RAG document(s) for {source_key} to s3://{bucket}/{rag_key}")
    return rag_key


def handler(event, context=None):
    rag_outputs = []
    for key in object_keys(event):
        if not key.lower().endswith(SUPPORTED_EXTENSIONS):
            continue
        process(key)
        rag_outputs.append(merge_outputs_to_rag_output(key))

    if not rag_outputs:
        print(f"No PDF or DOCX objects in event. Skipping. Event={event}")
        return {"status": "skipped"}
    return {"status": "completed", "rag_output": rag_outputs}


# def main():
#     if len(sys.argv) < 2:
#         raise SystemExit("usage: python -m data_preprocessing.pre_processing <s3-key-in-input-bucket>")
#     process(sys.argv[1])
#     merge_outputs_to_rag_output(sys.argv[1])


# if __name__ == "__main__":
#     main()
