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

# Bedrock reports a model that failed on its own terms as a 424 ModelErrorException.
# botocore does not retry it, reasonably - a 4xx is normally the caller's fault -
# but this one is not ours. Nova Pro answers a tool-use turn with a malformed block
# often enough to matter: three requirements out of 667 died on "Model produced
# invalid sequence as part of ToolUse" in a full CRT run, and each one costs a
# reviewer a row they have to do by hand.
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


# Every agent in the review registers this. The failure is the model's, not the
# tool's, so it belongs here next to the model rather than with the retrieval policy
# in agents.py - and putting it here is what lets the challenger and the adjudicator
# share it without importing from the module that imports them.
bedrock_hooks = Hooks()


@bedrock_hooks.on.model_request
async def retry_model_error(context: RunContext, *, request_context: ModelRequestContext, handler):
    """Ask again when the model fails on its own terms, rather than losing the row.

    Only a ModelErrorException is retried. A 4xx that really is ours - a malformed
    request, a model the account cannot invoke, an expired token - would fail the
    same way three times over, and the run is better off seeing it immediately.

    The retry is the same request again. There is nothing to correct: the request
    was fine and the model's answer to it was not.
    """
    for attempt in range(1, MODEL_ATTEMPTS + 1):
        try:
            return await handler(request_context)
        except ModelHTTPError as lclEx:
            if lclEx.status_code != MODEL_ERROR_STATUS or attempt == MODEL_ATTEMPTS:
                raise
            log.warning(f"retry_model_error() {lclEx.model_name} failed the request on attempt "
                        f"{attempt} of {MODEL_ATTEMPTS}: {lclEx}. Asking again.")
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
