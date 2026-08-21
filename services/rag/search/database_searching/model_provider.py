from pydantic_ai.models.bedrock import BedrockConverseModel, BedrockModelSettings
from pydantic_ai.providers.bedrock import BedrockProvider

from common.utils.helper import Helper
from common.utils.settings import aws_client
from common.utils.logger import log

REVIEW_MODEL_SETTINGS = BedrockModelSettings(
    temperature=0.0,
    top_p=1.0,
    max_tokens=4096,
)

_provider = None


def model_id():
    return Helper.get_property("foundation_llm_model_id", default="us.amazon.nova-pro-v1:0")


def bedrock_model(model_name=None):
    """A BedrockConverseModel on the service's own boto3 client."""
    global _provider
    if _provider is None:

        _provider = BedrockProvider(bedrock_client=aws_client("bedrock-runtime"))
        log.debug("bedrock_model() Created the shared Bedrock provider for the agents.")

    return BedrockConverseModel(model_name or model_id(), provider=_provider)
