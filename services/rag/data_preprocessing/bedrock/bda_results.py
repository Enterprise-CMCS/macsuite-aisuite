import json
from botocore.exceptions import ClientError

from common.utils.helper import Helper
from common.utils.logger import log


class BDAResults:
    def __init__(self):

        pass

    """Look for object in list_objects_v2
        Get paginator for given bucket and Prefix
        Gets Body of files ending with result.json and creates Array of it and return to Callin function"""

    def fetch_parsed_bda_results(self, bucket, output_prefix,awsPClientS3, calledFrom=""):
        log.info(f"*********Bda_Parsed_file_Access.fetch_parsed_bda_results start ******************** called with bucket={bucket}, output_prefix={output_prefix}")
        parsed_data = []
        paginator = awsPClientS3.get_paginator("list_objects_v2")
        log.debug(f"fetch_parsed_bda_results() paginator={paginator}")


        count = 0
        for page in paginator.paginate(Bucket=bucket, Prefix=output_prefix):
            log.debug(" Outer for loop")
            for obj in page.get("Contents", []):
                count= count + 1
                #print(" Inner for loop")
                key = obj.get("Key")
                #log.debug(f" Inner for loop Key ={key}")
                if not key:
                    continue
                if key.endswith("result.json"):
                    try:
                        log.debug(f"Looking for KEY which ends with result.json. Inner for loop Key ={key}")
                        resp = awsPClientS3.get_object(Bucket=bucket, Key=key)
                        data = json.load(resp["Body"])
                        parsed_data.append(data)
                    except ClientError as exClientError:
                        log.error("Error", error=f"Warning: Failed to download {key}: {exClientError}")
                        raise exClientError
                    except json.JSONDecodeError as exJSONDecodeError:
                        log.error("Error", error=f"Warning: invalid JSON in {key}: {exJSONDecodeError}")
                        raise exJSONDecodeError
                    except Exception as lclEx:
                        log.error("Error", error=f"Warning: unexpected error for {key}: {lclEx}")
                        raise lclEx
        log.info(f"fetch_parsed_bda_results() Returning parsed data. Length of parsed_data={len(parsed_data)}")
        #log.info(parsed_data[0])
        return parsed_data
