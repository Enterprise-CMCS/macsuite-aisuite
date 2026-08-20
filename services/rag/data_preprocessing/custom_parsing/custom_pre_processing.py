import sys
from pathlib import Path
from custom_pdf_parser import run_custom_pdf_parser

INPUT_BUCKET = 'aisuite-dev-contract-rag'
OUTPUT_BUCKET = 'aisuite-dev-contract-rag-post-processing'
 
INPUT_DOCUMENT_KEYS = ["state_of_OK/MCCRS-OK-5691/OK Complete Health Executed SoonerSelect Children Specialty Contract-_5691_1.pdf",
                       "state_of_OK/MCCRS-OK-5691/240329_SoonerSelect MHPAEA Report-_5691_1.pdf"]
 
PARSED_TEXT_OUTPUT_KEY = "state_of_OK_bdaoutput/MCCRS-OK-5691/BDATextOutput/parsed_text.json"
PARSED_TABLE_OUTPUT_KEY = "state_of_OK_bdaoutput/MCCRS-OK-5691/BDATableOutput/parsed_table.json"

"***custom data preprocessing***"

def process():
    run_custom_pdf_parser(input_bucket=INPUT_BUCKET,input_keys=INPUT_DOCUMENT_KEYS,output_bucket=OUTPUT_BUCKET,
                          text_output_key=PARSED_TEXT_OUTPUT_KEY,table_output_key=PARSED_TABLE_OUTPUT_KEY,)
  
def main():
    print("Starting custom PDF preprocessing...")
    process()
    print("Custom PDF preprocessing completed.")
 
 
if __name__ == "__main__":
    main()
