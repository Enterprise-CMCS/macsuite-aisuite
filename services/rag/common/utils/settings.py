import os
import boto3
import json
from pathlib import Path

from dotenv import load_dotenv, find_dotenv
from botocore.exceptions import ClientError
from common.utils.helper import Helper
from common.utils.logger import log

log.debug(f"***************** File *****************************{__file__}")


AWS_ACCESS_KEY_ID = ""      # os.getenv("aws_access_key_id")
AWS_SECRET_ACCESS_KEY = ""  # os.getenv("aws_secret_access_key")
AWS_SESSION_TOKEN = ""      # os.getenv("aws_session_token")
AWS_REGION = ""             # os.getenv("aws_region")

MODEL_ID = None             # os.getenv("model_id")
EMBEDDING_DIMENSION = None  # int(os.getenv("embedding_dimension", 1536))
EMBEDDING_BATCH_SIZE = None # os.getenv("embedding_batch_size")
DB_POOL_MIN = None          # int(os.getenv("db_pool_min", 1))
DB_POOL_MAX = None          # int(os.getenv("db_pool_max", 5))

DB_HOST = None
DB_NAME = None
DB_USER = None
DB_PASSWORD = None
DB_PORT = None


def load_env():
    log.debug("************************* LoadENV Start *******************************************")
    log.debug("Loading Environment variables")

    env_path = find_dotenv()
    if not env_path:
        batch_dir = Path(__file__).parent.parent.parent.parent
        env_path = batch_dir / ".env"
        if not env_path.exists():
            log.warning(f"Could not find .env file at {env_path}")
    load_dotenv(dotenv_path=env_path)
    log.debug(f"Loading env from: {env_path}")

    global AWS_ACCESS_KEY_ID
    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("aws_access_key_id")
    global AWS_SECRET_ACCESS_KEY
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY") or os.getenv("aws_secret_access_key")
    global AWS_SESSION_TOKEN
    AWS_SESSION_TOKEN = os.getenv("AWS_SESSION_TOKEN") or os.getenv("aws_session_token")
    global AWS_REGION
    AWS_REGION = Helper.get_property("aws_region", default=None)

    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        log.debug("load_env() Static AWS credentials are set in the environment and will be used.")
    else:
        log.debug("load_env() No static AWS credentials set; boto3 default credential chain will be used.")

    if AWS_REGION == "" or AWS_REGION is None:
        raise Exception(
            "LoadENV() AWS region is not set. Set AWS_REGION or AWS_DEFAULT_REGION, "
            "or 'aws_region' in aws.properties.ini.")
    else:
        log.debug(f"AWS_REGION - {AWS_REGION}, is set properly.")


def aws_session():
    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        return boto3.Session(
            aws_access_key_id = AWS_ACCESS_KEY_ID,
            aws_secret_access_key = AWS_SECRET_ACCESS_KEY,
            aws_session_token = AWS_SESSION_TOKEN or None,
            region_name = AWS_REGION
        )

    return boto3.Session(region_name = AWS_REGION)


def aws_client(aws_service_name, config=None):
    #log.info(f"settings.aws_client(): Method entered: aws_service_name= {aws_service_name}")
    session = aws_session()
    client = session.client(aws_service_name, config=config)
    log.debug(f"settings.aws_client(): Method exiting: Setting session.client({ aws_service_name}), Return Client=  { client}")
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

    EMBEDDING_DIMENSION = int(os.getenv("embedding_dimension", Helper.get_property("embedding_dimension")))

    log.debug(f"init_env() EMBEDDING_DIMENSION={EMBEDDING_DIMENSION}")

    global EMBEDDING_BATCH_SIZE
    EMBEDDING_BATCH_SIZE = os.getenv("embedding_batch_size", Helper.get_property("embedding_batch_size"))
    log.debug(f"init_env() EMBEDDING_BATCH_SIZE={EMBEDDING_BATCH_SIZE}")

    global DB_POOL_MIN
    DB_POOL_MIN = int(os.getenv("db_pool_min", Helper.get_property("db_pool_min")))
    log.debug(f"init_env() DB_POOL_MIN={DB_POOL_MIN}")

    global DB_POOL_MAX
    DB_POOL_MAX = int(os.getenv("db_pool_max", Helper.get_property("db_pool_max")))
    log.debug(f"init_env() DB_POOL_MAX={DB_POOL_MAX}")

    global DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

    db_host_env = os.getenv("db_host")
    db_name_env = os.getenv("db_name")
    db_user_env = os.getenv("db_user")
    db_password_env = os.getenv("db_password")
    db_port_env = os.getenv("db_port")

    if db_host_env and db_name_env and db_user_env and db_password_env:
        log.debug(f"init_env() Using database credentials from .env file (local development)")
        DB_HOST = db_host_env
        DB_PORT = int(db_port_env) if db_port_env else 5432
        DB_NAME = db_name_env
        DB_USER = db_user_env
        DB_PASSWORD = db_password_env
        log.debug(f"init_env() DB_HOST={DB_HOST}, DB_PORT={DB_PORT}, DB_NAME={DB_NAME}, DB_USER={DB_USER}")
    else:
        log.debug(f"init_env() Database credentials not found in .env, fetching from AWS Secrets Manager")
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

