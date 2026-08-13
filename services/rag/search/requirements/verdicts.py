"""Requirement verdict parsing and asynchronous grading helpers."""

import asyncio
import json
import os
import re
from typing import Any

from search.database_searching.agents import search_agent
from search.database_searching.deps import build_chat_deps


DEFAULT_CONCURRENCY = 4
CONCURRENCY_ENV_VAR = "REQUIREMENTS_CONCURRENCY"

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


def parse_agent_verdict(text) -> dict:
    """Extract a normalized verdict from an agent response without raising."""
    try:
        response_text = text if isinstance(text, str) else str(text)
        cleaned_text = re.sub(
            r"<thinking>.*?</thinking>", "", response_text, flags=re.DOTALL
        ).strip()
        json_match = re.search(
            r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", cleaned_text, re.DOTALL
        )
        if not json_match:
            return _unclear_verdict(response_text)

        try:
            result = json.loads(json_match.group())
        except json.JSONDecodeError:
            return _unclear_verdict(response_text)

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

        return {
            "Requirement": requirement if isinstance(requirement, str) else str(requirement or ""),
            "Recommendation": (
                recommendation.upper() if recommendation else "UNCLEAR"
            ),
            "Response": response if response else "No response provided",
            "Source": format_source_with_page(source, page),
            "Page": page,
        }
    except Exception:
        try:
            response_text = text if isinstance(text, str) else str(text)
        except Exception:
            response_text = ""
        return _unclear_verdict(response_text)


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


async def grade_requirement(text, deps, retry_unclear=True) -> dict:
    """Grade one requirement, retrying one unclear response when requested."""
    try:
        agent_response = await search_agent.run(user_prompt=text, deps=deps)
        parsed = parse_agent_verdict(_result_text(agent_response))

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
            parsed = parse_agent_verdict(_result_text(retry_response))

        if not parsed["Requirement"]:
            parsed["Requirement"] = text
        return {**parsed, "success": True, "error": None}
    except Exception as error:
        return _error_verdict(text, error)


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
