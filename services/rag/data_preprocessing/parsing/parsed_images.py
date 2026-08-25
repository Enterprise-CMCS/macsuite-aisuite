import os
import base64
import json
import boto3
import datetime
from urllib.parse import urlparse

from data_preprocessing.bedrock.bda_results import BDAResults
from common.utils.helper import Helper
from common.utils.logger import log
from common.utils.settings import aws_client, aws_session


aws_client_s3 = None
bedrock_runtime = None
modelId_bedrock_profile_arn = None


image_prompt = """Your are an AI assistance I need you analysis the DIAGRAM/IMAGE/CHART give detail information what is happening in Markdown version, If there is any
                          complex flow chart understand and give node,edges, any thing that is in the present in the image and their relationship in markdown format."""


def down_image_bytes(s3_uri):
    log.info("***************** Parsed_Images.down_image_bytes start *****************************")
    try:
        prased = urlparse(s3_uri[0])
        bucket = prased.netloc
        key = prased.path.lstrip("/")
        log.info(f"down_image_bytes() Getting data and reading bucket={bucket}, Body of key = {key}")
        response = aws_client_s3.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
    except Exception as e:
        log.warning(f"down_image_bytes() Failed to download image from {s3_uri[0]}: {str(e)}")
        return None


def foundational_llm_model_image_analysis(image_string):
    log.debug("***************** Parsed_Images.foundational_llm_model_image_analysis start *****************************")
    
    body = json.dumps({
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "text": image_prompt
                    },
                    {
                        "image": {
                            "format": "png",
                            "source": {
                                "bytes": image_string
                            }
                        }
                    }
                ]
            }
        ],
        "inferenceConfig": {
            "maxTokens": 1024,
            "temperature": 0.0,
            "topP": 1.0
        }
    })

    response = bedrock_runtime.invoke_model(
        body=body,
        modelId=modelId_bedrock_profile_arn,
        accept='application/json', 
        contentType='application/json')

    response_body = json.loads(response.get('body').read())
    
    response_text = response_body["output"]["message"]["content"][0]["text"].strip()
    
    return response_text


def image_text_organization(data, elements, image_analysis_text, source_key,  doc_id):
    log.debug("***************** Parsed_Images.image_text_organization start *****************************")
    log.debug(f"Passed (Decoded)image_analysis_text =  {image_analysis_text}".encode('cp1252', errors='ignore').decode('cp1252'))
    log.debug(f"Passed (Decoded) source_key= {source_key}.encode('cp1252', errors='ignore').decode('cp1252')")
    log.debug(f"Passed (Decoded) doc_id={doc_id}.encode('cp1252', errors='ignore').decode('cp1252')")
    summary = elements.get("summary","")
    content_markdown = elements.get("representation",{}).get("markdown",None)
    if image_analysis_text:
        content = f"## Summary\n{summary}\n## Content\n\n{content_markdown}\n\n{image_analysis_text}"
    else:
        content = f"## Summary\n{summary}\n## Content\n\n{content_markdown}"

    page_number = elements.get("locations",[])[0].get("page_index")
    element_id=elements.get("id")
    image_location = elements.get("crop_images",[None])[0]
    log.debug(f"image_location={image_location}")

    meta_doc = data.get("metadata",{}).get("s3_key")
    image_type = elements.get('sub_type')
    reading_order = elements.get("reading_order",float("inf"))
    images = {
        "text" : content,
        "doc_id": f"{doc_id}::Order{reading_order}",
        "metadata":{
            "doc_name":doc_id,
            "page":page_number,
            "element_type": "IMAGE",
            "sub_type": image_type,
        }
    }

    return images


def parsed_image_info(source_data):
    log.info("***************** Parsed_Images.parsed_image_info start *****************************")
    total_images_analysed=0
    image_data = []
    source_key = source_data.get("metadata", {}).get("s3_key")
    log.debug(f"s3_akey from data. source_key={source_key}")
    doc_id = source_key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    log.debug(f"Getting doc_id={doc_id}")

    #test = source_data.get("elements").get("type")["FIGURE"]
    #ll = len(f"Test = {test}")
    #log.debug(f"s3_akey from data. source_key {ll}")
    #r=1/0
    for elements in source_data.get("elements"):
        element_type = elements.get("type")
        if element_type == "FIGURE":
            sub_type = elements.get("sub_type")
            if sub_type in ["DIAGRAM","IMAGE","CHART"]:
                #log.debug(f"Element type = {element_type} Sub element type ={sub_type}")
                s3_keys = elements.get("crop_images")
                #log.debug(f"Getting data for file with s3_keys={s3_keys}")
                image_s3_key = down_image_bytes(s3_keys)

                # Skip this image if download failed (returns None)
                if image_s3_key is None:
                    log.warning(f"Skipping image analysis for missing file: {s3_keys}")
                    continue

                # image = Image.open(io.BytesIO(image_s3_key))
                # image.show()

                str_encoded = base64.b64encode(image_s3_key).decode("utf-8")
                # image_analysis_text = foundational_llm_model_image_analysis(str_encoded)
                try:
                    image_analysis_text = foundational_llm_model_image_analysis(str_encoded)
                except Exception as e:
                    error_text = str(e)
            
                    if "aspect ratio" in error_text.lower():
                        log.warning(
                            f"Skipping image because Bedrock rejected aspect ratio. "
                            f"source_key={source_key}, image={s3_keys}, error={error_text}"
                        )
                        image_analysis_text = ""
                    else:
                        raise

                image_data_temp = image_text_organization(source_data, elements, image_analysis_text, source_key,doc_id)
                image_data.extend([image_data_temp])
                total_images_analysed = total_images_analysed + 1
                continue

            image_analysis_text = ''
            other_subtype_document = image_text_organization(source_data,elements,image_analysis_text,source_key,doc_id)
            total_images_analysed = total_images_analysed + 1
            image_data += [other_subtype_document]
    log.info(f"parsed_image_info() Total Images for {source_key} document. totalImagesAnalysed={total_images_analysed}")
    return image_data

def invoke_parsed_images_data():
    try:
        log.info(f"invoke_parsed_images_data() Starts Parsed_Images File __main__={__name__}")
        output_bucket = Helper.get_property("output_bucket")
        output_prefix = Helper.get_property("output_prefix")
        foundation_llm_model_id = Helper.get_property("foundation_llm_model_id")

        global bedrock_runtime
        bedrock_runtime = aws_client('bedrock-runtime')
        if not bedrock_runtime:
            log.info(f"invoke_parsed_images_data() Failed to access Bedrock-runtime.")
            raise Exception(f"invoke_parsed_images_data() Failed to access Bedrock-runtime while calling aws_client(bedrock-runtime)")
        else:
            log.debug("Success getting while bedrock runtime.")

        if foundation_llm_model_id == "" or foundation_llm_model_id is None:
            raise Exception(
                f"foundation_llm_model_id - {foundation_llm_model_id}, is not set properly from environment.")
        else:
            log.debug(f"foundation_llm_model_id = {foundation_llm_model_id}, is set properly from environment.")


        global aws_client_s3
        aws_client_s3 = aws_client("s3")
        if not aws_client_s3:
            raise Exception(
                f"S3 Client. Variable s3AwsClient = {aws_client_s3}, is not set properly. S3 client is not available")

        session = aws_session()
        if not session:
            raise Exception(
                f"AWS Session. Variable awsSession = {session}, is not set properly. AWS Session is not available")

        current_region = session.region_name
        if current_region is None:
            raise Exception(f"Current region is Null. Variable current_region={current_region} is not set properly. ")
        else:
            log.debug(f"Variable current_region={current_region}")

        sts_client = aws_client("sts")
        if not sts_client:
            raise Exception(
                f"Exception invoke_parsed_images_data() AWS Client STS. Variable sts_client = {sts_client}, is not set properly. STS client is not available")
        else:
            log.debug(f"AWS Client STS. Variable sts_client = {sts_client}, is set properly.")
        account_id = sts_client.get_caller_identity()['Account']
        if account_id is None:
            raise Exception(f"Account ID is Null. Variable account_id={account_id} is not set properly. ")
        else:
            log.debug(f"Variable account_id={account_id}")

        global modelId_bedrock_profile_arn

        modelId_bedrock_profile_arn = f'arn:aws:bedrock:{current_region}:{account_id}:inference-profile/{foundation_llm_model_id}'

        log.info(f"invoke_parsed_images_data() modelId_bedrock_profile_arn={modelId_bedrock_profile_arn}")
        log.info(f"invoke_parsed_images_data() output_bucket={output_bucket}, output_prefix={output_prefix}")
        log.debug(f"invoke_parsed_images_data() Calling fetch_parsed_bda_results")
        #r=1/0
        bda_results = BDAResults()
        parsed_data = bda_results.fetch_parsed_bda_results(output_bucket, output_prefix, aws_client_s3, "Images")
        log.info(f"invoke_parsed_images_data() Loaded Images {len(parsed_data)} document")
        now = datetime.datetime.now()
        Helper.summary_file("IMAGE", f"\n{now}invoke_parsed_images_data() Loaded Images {len(parsed_data)} document")


        all_image = []
        log.debug(f"Length of parsed data={len(parsed_data)}")
        i_count = 0
        for data in parsed_data:
            log.debug(f"Parsing data at counter = {i_count}")
            image_data_lcl = parsed_image_info(data)
            source_key = data.get("metadata", {}).get("s3_key")
            Helper.summary_file("IMAGE", f"\n{now}Processing file {source_key}")
            all_image.extend(image_data_lcl)
            i_count = i_count + 1


        #BDAImageOutputFolder = os.getenv("BDAImageOutputFolder")
        bda_image_output_folder = Helper.get_property("BDAImageOutputFolder")
        #BDAImageOutputFilename = os.getenv("BDAImageOutputFilename")

        bda_image_output_filename = os.path.join(bda_image_output_folder, Helper.get_property("BDAImageOutputFilename"))
        log.debug(f"Output Table File Name={bda_image_output_filename}")

        # BDAImagesOutputFilename = "Output-BDA-images.json"

        log.debug(f"Output Image File Name={bda_image_output_filename}")

        s3_client = boto3.client('s3')
        s3_client.put_object(Bucket=output_bucket, Key=bda_image_output_filename, Body=json.dumps(all_image,indent=2),
                             ContentType='application/json; charset=utf-8')

        ####
        #with open(BDAImageOutputFilename, "w", encoding='utf-8') as f:
        #    json.dump(all_image, f, indent=2)

        log.debug(f"***************** invoke_parsed_images_data End. __name__={__name__}. Returning True.")
        return True

    except Exception as lclAllEx:
        Helper.print_exception("invoke_parsed_images_data", lclAllEx, "Error occurred in function invoke_parsed_images_data.")
        # Re-raise the same exception
        log.info(f"***************** invoke_parsed_images_data End. __name__={__name__}. Returning False.")
        return False
