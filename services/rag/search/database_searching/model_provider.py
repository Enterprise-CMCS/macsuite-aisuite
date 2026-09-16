import asyncio

from pydantic_ai import ModelHTTPError, RunContext
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.models import ModelRequestContext
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


MODEL_ERROR_STATUS = 424

MODEL_ATTEMPTS = 3

RETRY_BACKOFF_SECONDS = 1.0

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


bedrock_hooks = Hooks()


@bedrock_hooks.on.model_request
async def retry_model_error(context: RunContext, *, request_context: ModelRequestContext, handler):

    for attempt in range(1, MODEL_ATTEMPTS + 1):
        try:
            return await handler(request_context)
        except ModelHTTPError as lclEx:
            if lclEx.status_code != MODEL_ERROR_STATUS or attempt == MODEL_ATTEMPTS:
                raise
            log.warning(f"retry_model_error() {lclEx.model_name} failed the request on attempt "
                        f"{attempt} of {MODEL_ATTEMPTS}: {lclEx}. Asking again.")
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
