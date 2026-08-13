"""Requirement verdict parsing and asynchronous grading helpers."""

import asyncio
import json
import os
import re
import time
from typing import Any

from common.utils import contract_config, settings
from common.utils.contract_registry import resolve_contract
from common.utils.logger import get_logger
from search.database_searching import agents
from search.database_searching.agents import search_agent
from search.database_searching.deps import build_chat_deps
from search.requirements.verdict_store import record_verdict


logger = get_logger(__name__)

DEFAULT_CONCURRENCY = 4
CONCURRENCY_ENV_VAR = "REQUIREMENTS_CONCURRENCY"
VERDICT_SOURCE = "requirements_batch"
EMBED_MODEL_ENV_VAR = "BEDROCK_EMBED_MODEL_ID"
MODEL_SETTINGS = {"temperature": 0.0, "top_p": 1.0, "retries": 5}

_VERDICT_KEYS = (
    "Requirement",
    "Recommendation",
    "Response",
    "Source",
    "Page",
)


def format_source_with_page(source: Any, page: Any) -> str:
    """Append a page to a source when a meaningful page is present."""
    source_text = "N/A" if source is None else str(source)
    if page in (None, "", "N/A"):
        return source_text
    return f"{source_text}: {page}"


def _unclear_verdict(text: str) -> dict:
    return {
        "Requirement": "",
        "Recommendation": "UNCLEAR",
        "Response": text[:500],
        "Source": "N/A",
        "Page": "",
    }


def _parse_agent_verdict(text) -> tuple[dict, bool, str]:
    """Return (normalized verdict, JSON parsed_ok, original raw text)."""
    try:
        response_text = text if isinstance(text, str) else str(text)
    except Exception:
        return _unclear_verdict(""), False, ""

    try:
        cleaned_text = re.sub(
            r"<thinking>.*?</thinking>", "", response_text, flags=re.DOTALL
        ).strip()
        json_match = re.search(
            r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", cleaned_text, re.DOTALL
        )
        if not json_match:
            return _unclear_verdict(response_text), False, response_text

        try:
            result = json.loads(json_match.group())
        except json.JSONDecodeError:
            return _unclear_verdict(response_text), False, response_text

        recommendation = result.get(
            "Recommendation", result.get("Verdict", "UNCLEAR")
        )
        response = result.get(
            "Response", result.get("Reasoning", cleaned_text[:500])
        )
        if not isinstance(response, str):
            response = str(response) if response else "No response provided"
        source = result.get("Source", "N/A")
        page = result.get("Page", "")
        if page is None:
            page = ""
        elif not isinstance(page, (str, int)):
            page = str(page)
        requirement = result.get("Requirement", "")

        return (
            {
                "Requirement": requirement if isinstance(requirement, str) else str(requirement or ""),
                "Recommendation": (
                    recommendation.upper() if recommendation else "UNCLEAR"
                ),
                "Response": response if response else "No response provided",
                "Source": format_source_with_page(source, page),
                "Page": page,
            },
            True,
            response_text,
        )
    except Exception:
        return _unclear_verdict(response_text), False, response_text


def parse_agent_verdict(text) -> dict:
    """Extract a normalized verdict from an agent response without raising."""
    parsed, _parsed_ok, _raw_output = _parse_agent_verdict(text)
    return parsed


def _result_text(result) -> str:
    if hasattr(result, "output"):
        return result.output
    return str(result)


def _error_verdict(requirement: str, error: Exception) -> dict:
    error_text = str(error) or error.__class__.__name__
    return {
        "Requirement": requirement,
        "Recommendation": "ERROR",
        "Response": f"Error processing requirement: {error_text}",
        "Source": "N/A",
        "Page": "",
        "success": False,
        "error": error_text,
    }


def _embed_model_id() -> str:
    return (
        os.environ.get(EMBED_MODEL_ENV_VAR)
        or getattr(settings, "MODEL_ID", None)
        or "unknown"
    )


async def _persist_grade(
    text,
    result: dict,
    deps,
    started: float,
    *,
    parsed_ok: bool,
    raw_output: str | None,
) -> None:
    """Record a graded verdict without ever affecting the caller's payload."""
    try:
        latency_ms = int((time.perf_counter() - started) * 1000)
        chunks = list(getattr(deps, "last_retrieval", []) or [])
        contract = resolve_contract(
            contract_config.load_config(contract_config.resolve_config_path()),
            None,
        )
        embeddings_table = (
            getattr(getattr(deps, "search_engine", None), "table_name", None)
            or contract.embeddings_table_name
        )
        await record_verdict(
            source=VERDICT_SOURCE,
            client=None,
            requirement_text=text,
            verdict=result.get("Recommendation", "ERROR"),
            response_text=result.get("Response"),
            source_text=result.get("Source"),
            page_text=result.get("Page"),
            raw_output=None if parsed_ok else raw_output,
            parsed_ok=parsed_ok,
            model_id=agents.model_id,
            embed_model_id=_embed_model_id(),
            prompt_version=agents.PROMPT_VERSION,
            prompt_sha256=agents.PROMPT_SHA256,
            contract_id=contract.contract_id,
            embeddings_table=embeddings_table,
            chunks=chunks,
            model_settings=dict(MODEL_SETTINGS),
            retrieval={"final_count": len(chunks)},
            latency_ms=latency_ms,
        )
    except Exception as error:
        logger.warning("verdict_record_skipped", error=str(error))


async def grade_requirement(text, deps, retry_unclear=True) -> dict:
    """Grade one requirement, retrying one unclear response when requested."""
    started = time.perf_counter()
    parsed_ok = False
    raw_output = None
    try:
        agent_response = await search_agent.run(user_prompt=text, deps=deps)
        raw_output = _result_text(agent_response)
        parsed, parsed_ok, raw_output = _parse_agent_verdict(raw_output)

        if parsed["Recommendation"] == "UNCLEAR" and retry_unclear:
            retry_prompt = (
                "Based on the available documentation, please determine if the following "
                "requirement is MET or NOT MET. If there is insufficient information, "
                "explain what specific information is missing.\n\n"
                f"Requirement: {text}\n\n"
                "Please provide a clear MET or NOT MET determination with specific evidence, "
                "or explain exactly what information is needed."
            )
            retry_response = await search_agent.run(
                user_prompt=retry_prompt, deps=deps
            )
            raw_output = _result_text(retry_response)
            parsed, parsed_ok, raw_output = _parse_agent_verdict(raw_output)

        if not parsed["Requirement"]:
            parsed["Requirement"] = text
        result = {**parsed, "success": True, "error": None}
    except Exception as error:
        result = _error_verdict(text, error)
        parsed_ok = False
        raw_output = result.get("Response")

    await _persist_grade(
        text,
        result,
        deps,
        started,
        parsed_ok=parsed_ok,
        raw_output=raw_output,
    )
    return result


def _configured_concurrency(concurrency) -> int:
    if concurrency is not None:
        value = concurrency
    else:
        try:
            value = int(os.environ.get(CONCURRENCY_ENV_VAR, DEFAULT_CONCURRENCY))
        except (TypeError, ValueError):
            value = DEFAULT_CONCURRENCY

    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_CONCURRENCY


async def grade_requirements(
    items, retry_unclear=True, concurrency=None
) -> list:
    """Grade requirements concurrently while preserving request order."""
    semaphore = asyncio.Semaphore(_configured_concurrency(concurrency))

    async def grade_item(index: int, item: dict) -> dict:
        item_id = item.get("id", index)
        async with semaphore:
            requirement = item.get("text", "")
            try:
                deps = build_chat_deps()
                result = await grade_requirement(
                    item["text"], deps, retry_unclear=retry_unclear
                )
            except Exception as error:
                result = _error_verdict(requirement, error)

        return {"id": item_id, **result}

    tasks = [
        asyncio.create_task(grade_item(index, item))
        for index, item in enumerate(items)
    ]
    return await asyncio.gather(*tasks)
