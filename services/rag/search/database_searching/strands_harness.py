"""The Strands harness: runs every model loop in the requirement review.

Strands runs in this process, so search_contract is ordinary Python on the service's asyncpg pool.
Always go through invoke_async: agent(...) moves to another thread and event loop, and the pool
only works on the loop that created it.

Kept free of common.utils.settings so it can be unit tested without AWS: models and the search
function are passed in.
"""
from dataclasses import dataclass

from botocore.exceptions import ClientError
from pydantic import BaseModel, Field
from strands import ModelRetryStrategy, tool
from strands.vended_plugins.goal import GoalLoop
from strands_harness import create_harness

from common.utils.logger import log

MODEL_ERROR_CODE = "ModelErrorException"
MODEL_ATTEMPTS = 3
# Strands' own defaults. Throttling clears with time, so it gets more attempts than a bad response.
THROTTLE_ATTEMPTS = 6
RETRY_DELAY_SECONDS = 4

SEARCH_FAILED_MESSAGE = "The search backend failed. Try once more with a shorter query."
LOOKUP_FAILED_MESSAGE = "The definition lookup failed. Use search_contract to find the definition instead."

MAX_SCHEMA_ATTEMPTS = 2
MAX_EVIDENCE_ATTEMPTS = 2
LIMIT_SCHEMA_ATTEMPTS = "limit_structured_output_attempts"
SCHEMA_PROMPT = ("Return your analysis as structured output now. Copy every quote exactly, "
    "character for character, from the retrieved contract text.")


class EvidenceItem(BaseModel):
    chunk_id: int = Field(description="The retrieved chunk ID the quote comes from.")
    quote: str = Field(description="Text copied exactly from that chunk.")


class EvidenceAnalysis(BaseModel):
    """The Stage 1 evidence analysis of one requirement."""

    Reasoning: str = Field(description="What the evidence establishes.")
    Evidence: list[EvidenceItem] = Field(default_factory=list)
    MissingInformation: str = Field(default="", description="Material information that is still missing.")


@dataclass
class TokenUsage:
    """Tokens across every model loop in one review."""

    input_tokens: int = 0
    output_tokens: int = 0


class HarnessLimitError(RuntimeError):
    """The model loop stopped on a limit, so it has no answer to parse."""


class SearchRefused(Exception):
    """The search did not run. The message goes to the model as a failed tool call."""


class ModelErrorRetry(ModelRetryStrategy):
    """Also re-sends Bedrock's 424, which Nova returns for a malformed tool-use block.

    Strands only retries throttling. A 424 has its own count: the base counter also counts throttles,
    so sharing it would let earlier throttles use up the 424 attempts. max_attempts still caps the total.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._model_errors = 0

    def _reset_retry_state(self):
        super()._reset_retry_state()
        self._model_errors = 0

    def is_retryable(self, exception):
        # Strands calls this once per failed model call, before it counts the attempt.
        if isinstance(exception, ClientError) and exception.response.get("Error", {}).get("Code") == MODEL_ERROR_CODE:
            self._model_errors += 1
            return self._model_errors < MODEL_ATTEMPTS
        return super().is_retryable(exception)


def build_agent(model, system_prompt, name, *, tools=None, output_model=None, output_prompt=None, plugins=None):
    """A new Agent for one run. An Agent keeps conversation history and refuses concurrent calls, and
    requirements are reviewed concurrently, so agents are never shared. Models hold no run state and are.

    Every create_harness default is off. A review may only use the tools passed here, and each
    requirement stands alone, so nothing can reach the shell, the web, or another row's memory.
    """
    return create_harness(
        model=model,
        tools=tools or [],
        plugins=plugins or [],
        builtin_tools=[],
        builtin_plugins=[],
        memory=False,
        skills=False,
        session=False,
        context_manager=False,
        caching=False,
        background_tasks=False,
        system_prompt=system_prompt,
        structured_output_model=output_model,
        structured_output_prompt=output_prompt,
        callback_handler=None,
        retry_strategy=ModelErrorRetry(max_attempts=THROTTLE_ATTEMPTS, initial_delay=RETRY_DELAY_SECONDS),
        name=name,
    )


async def run_agent(agent, prompt, limits, usage):
    """Runs one model loop and returns the AgentResult."""
    result = await agent.invoke_async(prompt, limits=limits)

    tokens = result.metrics.accumulated_usage
    usage.input_tokens += tokens.get("inputTokens", 0)
    usage.output_tokens += tokens.get("outputTokens", 0)

    # A schema failure returns the text, which the caller parses and retries like any bad output.
    if str(result.stop_reason).startswith("limit_") and result.stop_reason != LIMIT_SCHEMA_ATTEMPTS:
        raise HarnessLimitError(f"{agent.name} stopped on {result.stop_reason}.")
    if not str(result).strip():
        log.warning(f"{agent.name} returned no text, stop reason {result.stop_reason}.")
    return result


async def call_backend(tool_name, backend, argument, failed_message):
    """Runs a tool's backend. A refusal or failure goes to the model as a failed tool call."""
    try:
        return await backend(argument)
    except SearchRefused as lclEx:
        log.debug(f"{tool_name} refused: {lclEx}")
        return {"status": "error", "content": [{"text": str(lclEx)}]}
    except Exception as lclEx:
        # Logged here so the raw exception never reaches the model, which can do nothing with it.
        log.warning(f"{tool_name} failed for '{argument[:120]}': {type(lclEx).__name__}: {lclEx}")
        return {"status": "error", "content": [{"text": failed_message}]}


def search_tool(search):
    """The search_contract tool for one run. search: async function(query) -> str."""

    @tool(name="search_contract")
    async def search_contract(query: str):
        """Search the contract for passages about a query. Returns passages with their [chunk N] IDs.

        Args:
            query: What to look for in the contract.
        """
        return await call_backend("search_contract", search, query, SEARCH_FAILED_MESSAGE)

    return search_contract


def definition_tool(lookup):
    """The lookup_definition tool for one run. lookup: async function(term) -> str."""

    @tool(name="lookup_definition")
    async def lookup_definition(term: str):
        """Find where the contract defines a term, usually in its definitions section. Matches the
        exact wording, so it finds short glossary entries that search_contract can miss. Returns
        passages with their [chunk N] IDs.

        Args:
            term: The defined term only, for example Prior Authorization.
        """
        return await call_backend("lookup_definition", lookup, term, LOOKUP_FAILED_MESSAGE)

    return lookup_definition


def analysis_in(message):
    """The EvidenceAnalysis the model submitted in this message, or None."""
    for block in message.get("content", []):
        tool_use = block.get("toolUse")
        if tool_use and tool_use.get("name") == EvidenceAnalysis.__name__:
            try:
                return EvidenceAnalysis.model_validate(tool_use["input"])
            except ValueError:
                return None
    return None


def last_analysis(messages):
    """The latest EvidenceAnalysis the model submitted in this conversation, or None."""
    for message in reversed(messages):
        if message.get("role") == "assistant":
            analysis = analysis_in(message)
            if analysis is not None:
                return analysis
    return None


def evidence_goal(find_problems):
    """Sends the analysis back once with what is wrong. find_problems(analysis) -> list of str.

    Output that is not an analysis passes here, because the parse-and-retry path handles it. The
    second answer stands either way, and Python still marks any quote it cannot verify.
    """

    def validate(response, agent, **kwargs):
        analysis = analysis_in(response)
        problems = find_problems(analysis) if analysis else []
        if not problems:
            return True
        log.info(f"evidence_goal() sent the analysis back: {problems}")
        return {"passed": False, "feedback": "\n".join(problems)}

    return GoalLoop(validate, max_attempts=MAX_EVIDENCE_ATTEMPTS)


async def run_evidence(prompt, *, model, system_prompt, search, max_turns, usage, find_problems=None, lookup=None):
    """One Stage 1 model loop. Returns the raw model output for extract_json_object.

    lookup: async function(term) -> str, offered to the model as lookup_definition when given.
    """
    tools = [search_tool(search)] + ([definition_tool(lookup)] if lookup else [])
    agent = build_agent(model, system_prompt, "evidence_agent", tools=tools,
                        output_model=EvidenceAnalysis, output_prompt=SCHEMA_PROMPT,
                        plugins=[evidence_goal(find_problems)] if find_problems else None)
    limits = {"turns": max_turns, "structured_output_attempts": MAX_SCHEMA_ATTEMPTS}
    try:
        return str(await run_agent(agent, prompt, limits, usage))
    except HarnessLimitError:
        # The limit can land on the evidence_goal pass, after a complete analysis. That analysis
        # is still the best answer, and Python verifies its quotes either way.
        analysis = last_analysis(agent.messages)
        if analysis is None:
            raise
        log.warning("evidence_agent hit its limit after an analysis; using the latest one.")
        return analysis.model_dump_json()


async def run_decision(prompt, *, model, system_prompt, max_turns, usage):
    """One Stage 2 model loop. Returns the AgentResult, so the caller can see the reasoning blocks."""
    agent = build_agent(model, system_prompt, "decision_agent")
    return await run_agent(agent, prompt, {"turns": max_turns}, usage)
