import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from data_preprocessing.bedrock import bedrock_BDA as bda
from data_preprocessing.parsing import parsed_images
from data_preprocessing.parsing import parsed_text_data
from data_preprocessing.parsing import parsed_bda_table
from data_preprocessing.parsing import parsed_toc

def process():

    ret_value = bda.invoke_bedrock_bda()
    if not ret_value:
        print(f"Value Returned by function invoke_bedrock_bda() is {ret_value}. Please check.")
        raise Exception(f"Value Returned by function invoke_bedrock_bda() is {ret_value}. Please check.")
    else:
        print(f"Value Returned by function invoke_bedrock_bda() is {ret_value}.")

    ret_value = parsed_text_data.invoke_parsed_text_data()
    if not ret_value:
        print(f"Value Returned by function invoke_parsed_text_data() is {ret_value}. Please check.")
        raise Exception(f"Value Returned by function invoke_parsed_text_data() is {ret_value}. Please check.")
    else:
        print(f"Value Returned by function invoke_parsed_text_data() is {ret_value}.")

    ret_value = parsed_bda_table.invoke_parsed_bda_data()
    if not ret_value:
        print(f"Value Returned by function invoke_parsed_bda_data() is {ret_value}. Please check.")
        raise Exception(f"Value Returned by function invoke_parsed_bda_data() is {ret_value}. Please check.")
    else:
        print(f"Value Returned by function invoke_parsed_bda_data() is {ret_value}.")

    ret_value = parsed_images.invoke_parsed_images_data()
    if not ret_value:
        print(f"Value Returned by function invoke_parsed_images_data() is {ret_value}. Please check.")
        raise Exception(f"Value Returned by function invoke_parsed_images_data() is {ret_value}. Please check.")
    else:
        print(f"Value Returned by function invoke_parsed_images_data() is {ret_value}.")

    ret_value = parsed_toc.invoke_parsed_toc()
    if not ret_value:
        print(f"Value Returned by function invoke_parsed_toc() is {ret_value}. Please check.")
        raise Exception(f"Value Returned by function invoke_parsed_toc() is {ret_value}. Please check.")
    else:
        print(f"Value Returned by function invoke_parsed_toc() is {ret_value}.")

    return ret_value


def main():
    print("Starting Glue job...")
    process()
    print("Glue job completed.")


if __name__ == "__main__":
    main()
