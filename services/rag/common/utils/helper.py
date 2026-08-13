import json
import os

from common.utils.logger import log
from common.utils import contract_config as contract_cfg

_RAISE_IF_MISSING = object()


class Helper:

    ENV_PROPERTY_OVERRIDES = {
        "aws_region": ("AWS_REGION", "AWS_DEFAULT_REGION"),
        "input_bucket_name": ("DOCUMENTS_BUCKET",),
        "output_bucket": ("POST_PROCESSING_BUCKET",),
        "RagSplitOutPutBucket": ("POST_PROCESSING_BUCKET",),
        "temp_bucket_name": ("PIPELINE_TEMP_BUCKET",),
        "vector-db-admin-secret": ("DB_SECRET_ARN", "DB_SECRET_NAME"),
        "foundation_llm_model_id": ("BEDROCK_MODEL_ID",),
        "model_id_bedrock_profile_embed": ("BEDROCK_EMBED_MODEL_ID",),
        "model_id_bedrock_embeddings": ("BEDROCK_EMBED_MODEL_ID",),
        "embeddings_table_name": ("EMBEDDINGS_TABLE_NAME",),
        "chunk_size": ("CHUNK_SIZE",),
        "chunk_overlap": ("CHUNK_OVERLAP",),
    }

    @staticmethod
    def get_env_property(str_property_name):
        for env_name in Helper.ENV_PROPERTY_OVERRIDES.get(str_property_name, ()):
            env_value = os.getenv(env_name)
            if env_value:
                return env_value.strip()
        return None

    @staticmethod
    def resolve_config_path(config_file='aws.properties.ini'):
        return contract_cfg.resolve_config_path(
            config_file,
            utils_dir=os.path.dirname(os.path.abspath(__file__)),
        )

    @staticmethod
    def load_config(config_file='aws.properties.ini'):
        config_path = Helper.resolve_config_path(config_file)
        try:
            return contract_cfg.load_config(config_path)
        except FileNotFoundError:
            log.error(f"load_config() Configuration file '{config_path}' not found.")
            raise

    @staticmethod
    def contract_sections(config):
        return contract_cfg.contract_sections(config)

    @staticmethod
    def resolve_active_contract_section(config):
        return contract_cfg.resolve_active_contract_section(config)

    @staticmethod
    def list_embeddings_table_names(config_file='aws.properties.ini'):
        config = Helper.load_config(config_file)
        return contract_cfg.list_embeddings_table_names_from_config(config)

    @staticmethod
    def list_contracts(config_file='aws.properties.ini'):
        from common.utils import contract_registry as contract_reg

        config = Helper.load_config(config_file)
        return contract_reg.list_contracts(config)

    @staticmethod
    def resolve_contract(contract_id, config_file='aws.properties.ini'):
        from common.utils import contract_registry as contract_reg

        config = Helper.load_config(config_file)
        return contract_reg.resolve_contract(config, contract_id)

    @staticmethod
    def validate_embeddings_table_name(table_name):
        return contract_cfg.validate_embeddings_table_name(table_name)

    @staticmethod
    def get_embeddings_table_name(config_file='aws.properties.ini', default="embeddings"):
        name = Helper.get_property(
            "embeddings_table_name",
            config_file=config_file,
            default=default,
        )
        return Helper.validate_embeddings_table_name(name.strip())

    @staticmethod
    def get_property(str_property_name, config_file='aws.properties.ini', default=_RAISE_IF_MISSING):
        env_value = Helper.get_env_property(str_property_name)
        if env_value:
            log.debug(f"get_property() Property name = {str_property_name} resolved from environment.")
            return env_value

        try:
            config = Helper.load_config(config_file)
            return contract_cfg.get_config_property(
                config,
                str_property_name,
                default=default if default is not _RAISE_IF_MISSING else None,
                raise_if_missing=default is _RAISE_IF_MISSING,
            )
        except KeyError as lclEx:
            if default is not _RAISE_IF_MISSING:
                log.debug(f"get_property() Property '{str_property_name}' is absent from the property file, using the supplied default.")
                return default
            Helper.print_exception("get_property", lclEx, f" Exception occurred while getting property '{str_property_name}'. Please check if this property is present in property file.")
            raise lclEx
        except Exception as lclEx:
            Helper.print_exception("get_property", lclEx, f" Exception occurred while getting property '{str_property_name}'.")
            raise lclEx

    @staticmethod
    def get_positive_int_property(str_property_name, config_file='aws.properties.ini'):
        try:
            value = Helper.get_property(str_property_name, config_file=config_file)
        except KeyError as error:
            raise ValueError(
                f"Property '{str_property_name}' is required and must be a positive integer."
            ) from error

        try:
            parsed_value = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Property '{str_property_name}' must be a positive integer; got {value!r}."
            ) from error

        if parsed_value <= 0:
            raise ValueError(
                f"Property '{str_property_name}' must be greater than zero; got {value!r}."
            )
        return parsed_value

    @staticmethod
    def get_config_aws_properties(config_file='aws.properties.ini'):
        ai_prop_file = os.getenv("AIPropFile")
        if ai_prop_file is not None and ai_prop_file != "":
            config_file = ai_prop_file.strip()
            log.debug(f"{config_file}get_property() Property file variable AIPropFile from environment is either None or an empty string.")
        else:
            log.debug(
                f"getConfigAWSProperties() Property file variable AIPropFile from environment is either None or an empty string.")

        try:
            return Helper.load_config(config_file)
        except FileNotFoundError:
            print(f"getConfigAWSProperties() Error: Configuration file '{config_file}' not found.")
            return None

    @staticmethod
    def create_file(bucket_name, str_file_name, file_data, str_folder=None):
        from common.utils.settings import aws_client
        str_file_name=str(str_file_name)
        s3_client = aws_client('s3')
        if str_folder is None:
            str_key= str_file_name
        else:
            if not str_folder.endswith('/'):
                str_folder += '/'
            str_key = str_folder + str_file_name
        log.debug(f"createFile() = bucketName={bucket_name}, Folder name ={str_folder} strKey = {str_key} ")
        s3_client.put_object(Bucket=bucket_name, Key=str_key, Body=json.dumps(file_data, indent=2))
        log.info(f"File '{str_file_name}' successfully created in bucket '{bucket_name}'.")

    @staticmethod
    def print_exception(called_from, lcl_exception, extra_msg="NONE"):
        log.debug("********************************* print_exception start *************************************")
        log.debug(f"Called from {called_from} \nExtra Message={extra_msg}")
        log.debug(f"Type of Exception type(ex)= {type(lcl_exception)}")
        log.debug(f"Argument of exception= {lcl_exception.args}")
        log.debug(f"Exception = {lcl_exception}")
        for val in lcl_exception.args:
            log.debug(f"\n Arguments= {val}")

        log.debug("\n********************************* print_exception End *************************************")

    @staticmethod
    def clean_filename(name):
        log.debug(f"***************** Parsed_excel_table.clean_filename starts  **********with passed name={name}")
        return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)

    @staticmethod
    def summary_file(str_file_name, msg):
        from common.utils.settings import aws_client
        str_summary_folder = Helper.get_property("SummaryLogFolder")
        summary_file_bucket = Helper.get_property("temp_bucket_name")
        if not str_summary_folder.endswith('/'):
            str_summary_folder += '/'

        if str_file_name == "TEXT":
            str_summary_file = os.path.join(str_summary_folder, "Text_SummaryFile.log")
        elif str_file_name == "TABLE":
            str_summary_file = os.path.join(str_summary_folder, "Table_SummaryFile.log")
        elif str_file_name == "IMAGE":
            str_summary_file = os.path.join(str_summary_folder, "Image_SummaryFile.log")
        elif str_file_name == "EXCELTABLE":
            str_summary_file = os.path.join(str_summary_folder, "ExcelTable_SummaryFile.log")
        elif str_file_name == "BEDROCK":
            str_summary_file = os.path.join(str_summary_folder, "Bedrock_SummaryFile.log")
        else:
            str_summary_file = os.path.join(str_summary_folder, "Unknown_SummaryFile.log")
        s3_client = aws_client('s3')

        try:
            response = s3_client.get_object(Bucket=summary_file_bucket, Key=str_summary_file)
            existing_content = response['Body'].read().decode('utf-8')
            updated_content = existing_content + msg
            s3_client.put_object(Bucket=summary_file_bucket, Key=str_summary_file, Body=updated_content.encode('utf-8'))
            log.debug(f"Successfully appended data to {str_summary_file} in {summary_file_bucket}.")
        except s3_client.exceptions.NoSuchKey:
            s3_client.put_object(Bucket=summary_file_bucket, Key=str_summary_file, Body=msg.encode('utf-8'))
            log.debug(f"Created new file {str_summary_file} in {summary_file_bucket} with the new data.")

        except Exception as lclEx:
            Helper.print_exception("summary_file", lclEx, " Exception occurred while writing to summary files.")
            raise lclEx

    @staticmethod
    def delete_bucket_file_recursively(s3_resource, bucket_name, p_prefix=""):
        buckets3_resource = s3_resource.Bucket(bucket_name)
        if p_prefix == "":
            # Delete all objects (including versions if versioning is enabled)
            buckets3_resource.objects.delete()
            buckets3_resource.object_versions.delete()
            # Now delete the bucket itself
            #bucket.delete()
            log.info(f"All contents from Bucket '{bucket_name}' have been deleted.")
        else:
            buckets3_resource.objects.filter(Prefix=p_prefix).delete()
            log.info(f"All contents from Bucket '{bucket_name}' and folder {p_prefix} have been deleted.")

    @staticmethod
    def check_if_s3_folder_exists(s3_client, bucket_name: str, folder_path: str):
        if not folder_path.endswith('/'):
            folder_path += '/'
        try:
            response = s3_client.list_objects_v2(
                Bucket=bucket_name,
                Prefix=folder_path,
                MaxKeys=1  # We only need to check for the existence of one object
            )

            # If 'Contents' is present in the response, it means at least one object
            # exists under that prefix, indicating the "folder" exists.
            return 'Contents' in response
        except Exception as lclEx:
            log.error(f"CheckIfS3FolderExists() An error occurred: {lclEx}")
            Helper.print_exception("CheckIfS3FolderExists", lclEx,
                                 f" Exception occurred while Check If S3 Folder Exists bucket - {bucket_name} for folder_path {folder_path}.")
            return False

    @staticmethod
    def create_s3_folder_if_not_exists(s3_client, bucket_name, folder_path):
        if not folder_path.endswith('/'):
            folder_path += '/'
        try:
            response = s3_client.list_objects_v2(
                Bucket=bucket_name,
                Prefix=folder_path,
                MaxKeys=1  # We only need to check for the existence of one object
            )

            # If 'Contents' is present in the response, it means at least one object
            # exists under that prefix, indicating the "folder" exists.
            if 'Contents' in response:
                log.info(f"The folder exists '{folder_path}' exists in bucket '{bucket_name}'.")
            else:
                log.debug(f"The folder does not '{folder_path}' does not exist in bucket '{bucket_name}'.")
                s3_client.put_object(Bucket=bucket_name, Key=folder_path, Body='')
                log.info(f"Folder '{folder_path}' created in bucket '{bucket_name}'.")
        except Exception as e:
            log.info(f"The folder '{folder_path}' does not exist in bucket '{bucket_name}'.")
            s3_client.put_object(Bucket=bucket_name, Key=folder_path, Body='')
            log.info(f"Folder '{folder_path}' created in bucket '{bucket_name}'.")

    @staticmethod
    def get_json_from_s3(bucket_name, file_key):
        from common.utils.settings import aws_client
        log.debug(f"getJsonFromS3() Gettting data for file bucket name{bucket_name} and file {file_key}")
        s3_client = aws_client('s3')
        try:
            response = s3_client.get_object(Bucket=bucket_name, Key=file_key)
            file_content = response['Body'].read().decode('utf-8')
            #log.info(file_content)
            json_data = json.loads(file_content)
            return json_data
        except Exception as lclEx:
            Helper.print_exception("getJsonFromS3()", lclEx, f" Exception occurred while reading data form S3 bucket{bucket_name} for file {file_key}.")
            raise lclEx

    @staticmethod
    def get_account_id(sts_client):
        #sts_client = aws_client("sts")
        if not sts_client:
            raise Exception(
                f"Exception getAccountID() AWS Client STS. Variable sts_client = {sts_client}, is not set properly. STS client is not available")
        else:
            log.debug(f"AWS Client STS. Variable sts_client = {sts_client}, is set properly.")

        account_id = sts_client.get_caller_identity()['Account']
        if account_id is None:
            raise Exception(f"Account ID is Null. Variable account_id={account_id} is not set properly. ")
        else:
            log.debug(f"Variable account_id={account_id}")
            return account_id

    @staticmethod
    def get_current_region(p_aws_session):
        #session = aws_session()
        #current_region = Helper.getCurrentRegion(session)

        if not p_aws_session:
            raise Exception(
                f"AWS Session. Variable awsSession = {p_aws_session}, is not set properly. AWS Session is not available")

        current_region = p_aws_session.region_name
        if current_region is None:
            raise Exception(f"Current region is Null. Variable current_region={current_region} is not set properly. ")
        else:
            log.debug(f"Variable current_region={current_region}")
            return current_region

    def print_jason_obj(json_obj, json_obj_value, dont_print="Pr1nt"):
        log.info(f"printJasonObj() Value of {json_obj}, Will not print value for {dont_print}")
        if json_obj_value is not None:
            if isinstance(json_obj_value, dict):
                log.info(f"Passed variable jsonObjValue is of type DICT")
                for key, value in json_obj_value.items():
                    if key not in dont_print:
                        log.info(f"Key: {key}, Value: {value}")
            elif isinstance(json_obj_value, str):
                log.info(f"Passed variable jsonObjValue is of type STR")
                log.info(f"{json_obj_value}")
            #elif isinstance(jsonObjValue, NoneType):
            #    log.debug(f"Passed variable jsonObjValue is of type None")
            #    log.debug(f"Value of {jsonObj}, Please check the value for variable {jsonObj}")

            else:
                log.warning(f"Warning: Expected a dictionary, str but got type: {type(json_obj_value)} for variable {json_obj}. This Error will be ignored.")
                #raise Exception (f"printJasonObj() Warning: Expected a dictionary, str but got type: {type(jsonObjValue)}")
        else:
            log.warning(f"Value of {json_obj}, Please check the value for variable {json_obj}. It is set to None.")
