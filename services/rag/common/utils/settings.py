import boto3
import json

from botocore.exceptions import ClientError
from common.utils.helper import Helper
from common.utils.logger import log

log.debug(f"***************** File *****************************{__file__}")



MODEL_ID = None
EMBEDDING_DIMENSION = None
EMBEDDING_BATCH_SIZE = None
DB_POOL_MIN = None
DB_POOL_MAX = None

DB_HOST = None
DB_NAME = None
DB_USER = None
DB_PASSWORD = None
DB_PORT = None


def load_env():
    log.debug("************************* LoadENV Start *******************************************")
    log.debug("Resolving AWS region; credentials come from the boto3 default chain (aws configure / IAM role).")

    global AWS_REGION
    AWS_REGION = Helper.get_property("aws_region", default=None) or boto3.Session().region_name

    if AWS_REGION == "" or AWS_REGION is None:
        raise Exception(
            "LoadENV() AWS region is not set. Set 'aws_region' in aws.properties.ini "
            "or a default region via 'aws configure'.")
    else:
        log.debug(f"AWS_REGION - {AWS_REGION}, is set properly.")


def aws_session():
    return boto3.Session(region_name = AWS_REGION)


def aws_client(aws_service_name, config=None):
    #log.info(f"settings.aws_client(): Method entered: aws_service_name= {aws_service_name}")
    session = aws_session()
    client = session.client(aws_service_name, config=config)
    #log.debug(f"settings.aws_client(): Method exiting: Setting session.client({ aws_service_name}), Return Client=  { client}")
    return client


def get_secret():
    secret_name = Helper.get_property("vector-db-admin-secret", default=None)
    if not secret_name:
        raise Exception(
            "get_secret() Database secret is not configured. Set DB_SECRET_ARN, "
            "or 'vector-db-admin-secret' in aws.properties.ini.")
    log.debug(f"get_secret() secret_name= {secret_name}")

    the_aws_client = aws_client(Helper.get_property("aws_client_secretsmanager", default="secretsmanager"))
    log.debug(f"get_secret() AWS Client secret Manager AWS_client {the_aws_client}")

    try:
        log.debug(f"get_secret() Retrieving secret from AWS for secret name {secret_name}")
        get_secret_value_response = the_aws_client.get_secret_value(
            SecretId=secret_name
        )
    except ClientError as lclEx:
        log.error(f"get_secret()Error retrieving secret: {lclEx}")
        Helper.print_exception("Setting.get_secret() Exception Occurred while retrieving secret:",lclEx, f"Error in getting secret for {secret_name}")
        raise lclEx

    secret = get_secret_value_response['SecretString']
    secret_dict = json.loads(secret)
    Helper.print_jason_obj("secret_dict", secret_dict, "password")
    return secret_dict


def create_s3_folders():
    aws_s3client = aws_client("s3")

    input_bucket_name = Helper.get_property("input_bucket_name")
    output_bucket = Helper.get_property("output_bucket")
    code_bucket_name = Helper.get_property("code_bucket_name")

    str_bda_text_output_folder = Helper.get_property("BDATextOutputFolder")
    Helper.create_s3_folder_if_not_exists(aws_s3client, output_bucket, str_bda_text_output_folder)

    str_bda_table_output_folder = Helper.get_property("BDATableOutputFolder")
    Helper.create_s3_folder_if_not_exists(aws_s3client, output_bucket, str_bda_table_output_folder)

    str_bda_image_output_folder = Helper.get_property("BDAImageOutputFolder")
    Helper.create_s3_folder_if_not_exists(aws_s3client, output_bucket, str_bda_image_output_folder)

    str_out_put_excel_json_folder_pre_process = Helper.get_property("OutPutExcelJsonFolderPreProcess")
    Helper.create_s3_folder_if_not_exists(aws_s3client, output_bucket, str_out_put_excel_json_folder_pre_process)

    str_temp_folder = Helper.get_property("TempFolder")
    Helper.create_s3_folder_if_not_exists(aws_s3client, code_bucket_name, str_temp_folder)

    str_summary_file = Helper.get_property("SummaryLogFolder")
    Helper.create_s3_folder_if_not_exists(aws_s3client, code_bucket_name, str_summary_file)


def init_env():
    load_env()

    global MODEL_ID
    MODEL_ID = Helper.get_property("model_id_bedrock_profile_embed")

    log.debug(f"init_env() MODEL_ID={MODEL_ID}")
    global EMBEDDING_DIMENSION
    EMBEDDING_DIMENSION = int(Helper.get_property("embedding_dimension"))
    log.debug(f"init_env() EMBEDDING_DIMENSION={EMBEDDING_DIMENSION}")

    global EMBEDDING_BATCH_SIZE
    EMBEDDING_BATCH_SIZE = Helper.get_property("embedding_batch_size")
    log.debug(f"init_env() EMBEDDING_BATCH_SIZE={EMBEDDING_BATCH_SIZE}")

    global DB_POOL_MIN
    DB_POOL_MIN = int(Helper.get_property("db_pool_min"))
    log.debug(f"init_env() DB_POOL_MIN={DB_POOL_MIN}")

    global DB_POOL_MAX
    DB_POOL_MAX = int(Helper.get_property("db_pool_max"))
    log.debug(f"init_env() DB_POOL_MAX={DB_POOL_MAX}")

    global DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

    log.debug(f"init_env() Fetching database credentials from AWS Secrets Manager")
    db_credentials = get_secret()
    log.debug(f"init_env()  after calling get_secret(). Setting database parameters got from secret string")
    DB_HOST = db_credentials.get("host")
    log.debug(f"init_env() DB_HOST={DB_HOST}")
    DB_PORT = int(db_credentials.get("port"))
    log.debug(f"init_env() DB_PORT={DB_PORT}")
    DB_NAME = db_credentials.get("dbname")
    log.debug(f"init_env() DB_NAME={DB_NAME}")
    DB_USER = db_credentials.get("username")
    log.debug(f"init_env() DB_USER={DB_USER}")
    DB_PASSWORD = db_credentials.get("password")


init_env()

