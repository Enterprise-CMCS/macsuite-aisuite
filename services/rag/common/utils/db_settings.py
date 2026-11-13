import json

from botocore.exceptions import ClientError

from common.utils.helper import Helper
from common.utils.logger import log
from common.utils.settings import aws_client


def get_secret():
    secret_name = Helper.get_property("vector-db-admin-secret", default=None)
    if not secret_name:
        raise Exception(
            "get_secret() Database secret is not configured. Set DB_SECRET_ARN, "
            "or 'vector-db-admin-secret' in aws.properties.ini.")
    log.debug(f"get_secret() secret_name= {secret_name}")

    secrets_client = aws_client(Helper.get_property("aws_client_secretsmanager", default="secretsmanager"))
    try:
        response = secrets_client.get_secret_value(SecretId=secret_name)
    except ClientError as lclEx:
        Helper.print_exception("db_settings.get_secret() Exception Occurred while retrieving secret:", lclEx, f"Error in getting secret for {secret_name}")
        raise

    secret_dict = json.loads(response["SecretString"])
    Helper.print_jason_obj("secret_dict", secret_dict, "password")
    return secret_dict


DB_POOL_MIN = int(Helper.get_property("db_pool_min"))
DB_POOL_MAX = int(Helper.get_property("db_pool_max"))

_db_credentials = get_secret()
DB_HOST = _db_credentials.get("host")
DB_PORT = int(_db_credentials.get("port"))
DB_NAME = _db_credentials.get("dbname")
DB_USER = _db_credentials.get("username")
DB_PASSWORD = _db_credentials.get("password")
log.debug(f"db_settings DB_HOST={DB_HOST} DB_PORT={DB_PORT} DB_NAME={DB_NAME} DB_USER={DB_USER}")
