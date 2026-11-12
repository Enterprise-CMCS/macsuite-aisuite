import json

from strands.models import BedrockModel

from common.utils.helper import Helper
from common.utils.settings import aws_session

EVIDENCE_MODEL_SETTINGS = {"temperature": 0.0, "top_p": 1.0}

_models = {}


def model_id():
    return Helper.get_property("foundation_llm_model_id", default="us.amazon.nova-2-lite-v1:0")


def strands_model(settings=EVIDENCE_MODEL_SETTINGS):
    """The Strands model for these settings. Built once: each one creates its own boto3 client."""
    key = json.dumps(settings, sort_keys=True)
    if key not in _models:
        _models[key] = BedrockModel(boto_session=aws_session(), model_id=model_id(), streaming=False, **settings)
    return _models[key]
