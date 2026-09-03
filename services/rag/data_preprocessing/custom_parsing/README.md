## Custom PDF Parser Logic
This PR adds a custom PDF parsing flow to replace the previous Bedrock Data Automation preprocessing path for PDF text and table extraction.
The new parser processes PDF documents directly using pdfplumber. It extracts only text and tables. Image parsing is not included in this version.
### Files Added
- custom_pre_processing.py
- custom_pdf_parser.py
- parser.py
- helpers.py

Step-by-Step Flow
1. Start preprocessing
The preprocessing starts from `custom_pre_processing.py`.
This file defines:
- input S3 bucket
- output S3 bucket
- PDF files to process
- S3 output path for parsed text JSON
- S3 output path for parsed table JSON
Then it calls run_custom_pdf_parser() from `custom_pdf_parser.py`.

2. Download PDFs from S3
`custom_pdf_parser.py` loops through the each PDF files.
- It checks that the file ends with .pdf.
- It downloads the PDF from S3 into a temporary local folder.
- It sends the local PDF file to the parser.
The downloaded files are temporary and are removed automatically after processing.

3. Parse each PDF
The parser runs through parse_pdf_to_records()vfrom `parser.py`.This function:
- opens the PDF
- extracts page-level text and tables
- builds the final JSON records
- returns the parsed records as Python dictionaries
