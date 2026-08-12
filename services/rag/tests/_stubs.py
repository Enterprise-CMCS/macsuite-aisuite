"""Offline dependency stubs for RAG unit tests."""

import os
import sys
from unittest.mock import MagicMock


def install_offline_stubs():
    """Install import-safe dependency stubs and local database settings."""
    for name in (
        "boto3",
        "botocore",
        "botocore.exceptions",
        "dotenv",
        "asyncpg",
        "pgvector",
        "pgvector.asyncpg",
        "structlog",
        "numpy",
    ):
        sys.modules.setdefault(name, MagicMock(name=name))

    boto3 = sys.modules["boto3"]
    boto3.Session.return_value.client.return_value = MagicMock(
        name="offline_aws_client"
    )

    dotenv = sys.modules["dotenv"]
    dotenv.find_dotenv.return_value = ""
    dotenv.load_dotenv.return_value = False

    for name, value in {
        "db_host": "localhost",
        "db_name": "test_database",
        "db_user": "test_user",
        "db_password": "test_password",
        "db_port": "5432",
        "AWS_REGION": "us-east-1",
    }.items():
        os.environ.setdefault(name, value)
