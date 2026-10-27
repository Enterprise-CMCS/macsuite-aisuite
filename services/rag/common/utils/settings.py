import boto3

from common.utils.helper import Helper
from common.utils.logger import log

log.debug(f"***************** File *****************************{__file__}")



MODEL_ID = None
EMBEDDING_DIMENSION = None
EMBEDDING_BATCH_SIZE = None


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
    log.debug(f"settings.aws_client(): Method exiting: Setting session.client({ aws_service_name}), Return Client=  { client}")
    return client


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


init_env()

