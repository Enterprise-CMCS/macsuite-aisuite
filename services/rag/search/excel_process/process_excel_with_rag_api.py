#!/usr/bin/env python3
"""Same CRT workbook driver as process_excel_with_rag.py, but sourced from the
deployed /agent API instead of a local Bedrock/RDS connection.

On this branch /agent (answer_question_formatted() in agents.py) runs the same
single-pass contract_agent that review_requirement() uses locally, and returns
its verdict as formatted text - "AI recommended status: Met/Not Met/Unclear",
then "AI response: ...". parse_agent_review() below pulls the status back out
of that text so Status gets written for real, not left defaulted. What /agent
does NOT expose as separate fields: retrieval/combined confidence scores,
individual evidence quotes, or a verified/unverified flag - those stay blank
here and only the RAG Analysis "AI Response" column carries that detail as
prose. Needs network reach to the ALB (VPN or in-VPC) but no AWS credentials
on the machine running it.

--contract picks which embeddings table /agent searches, e.g.
`--contract embeddings_ne_1_1`, so reviewing a different submission's
knowledge base is a flag, not an aws.properties.ini edit plus a redeploy.
Omit it to use whatever contract the API has active by default.

--env picks a known environment's ALB. RAG_API_BASE_URL (env var) or --base-url
(flag) override it, e.g. for a local uvicorn run - --base-url wins over
RAG_API_BASE_URL, which wins over --env.
"""

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from common.utils.logger import log
from search.database_searching.review_models import RequirementReview
from search.excel_process.process_excel_with_rag import (
    DEFAULT_CONCURRENCY,
    append_sidecar,
    default_folder,
    find_workbooks,
    load_sidecar,
    output_path,
    report_unreviewed,
    saveable,
    sidecar_path,
    summarise,
    CRTWorkbook,
)

DESCRIPTION = "Answer CRT requirements in output_excel/ via the deployed /agent API, not local AWS credentials."

ALB_URLS = {
    # These are the ALB URLs for the various environments. They are used to construct the base URL for the API calls.
    "dev": "http://127.0.0.1:8001/",
    #"dev": "https://alb.dev.macsuite.aisuite.internal.cms.gov/",
    "qa": "https://alb.qa.macsuite.aisuite.internal.cms.gov/",
    "uat": "https://alb.uat.macsuite.aisuite.internal.cms.gov/",
    "prod": "https://alb.prod.macsuite.aisuite.internal.cms.gov/",
}

# Edit these two to run without passing --env/--contract each time. Both are
# still overridable per-invocation: --env/--contract win over these, and
# --base-url wins over ALB_URLS entirely.
DEFAULT_ENV = "dev"
DEFAULT_CONTRACT = None  # e.g. "embeddings_fl" - None uses the API's active contract

DEFAULT_TIMEOUT = 120.0

NO_STATUS_NOTE = (
    "Could not find an \"AI recommended status:\" line in /agent's response, so Status "
    "here defaults to Unsure. Read the AI response below and set Status yourself."
)

# answer_question_formatted() on the server builds its response as:
#   AI recommended status: Met|Not Met|Unclear
#
#   AI response: <argument>
#
#   Page number: ...
#   ...
# but falls back to raw, unparsed model output (which can still carry a
# <thinking> block) when its own JSON parse fails server-side.
STATUS_LINE = re.compile(r"^AI recommended status:\s*(.+?)\s*$", re.IGNORECASE)
AI_RESPONSE_PREFIX = re.compile(r"^AI response:\s*", re.IGNORECASE)
STATUS_MAP = {"met": "MET", "not met": "NOT MET", "unclear": "UNCLEAR"}

THINKING_BLOCK = re.compile(r"<thinking>.*?</thinking>", re.IGNORECASE | re.DOTALL)
STRAY_THINKING_TAG = re.compile(r"</?thinking>", re.IGNORECASE)


def strip_thinking(text):
    text = THINKING_BLOCK.sub("", text or "")
    text = STRAY_THINKING_TAG.sub("", text)
    return text.strip()


def parse_agent_review(text):
    """(status, argument) parsed out of /agent's formatted response, or
    (None, text) if it doesn't match the expected shape - e.g. the server's
    own JSON parse failed and it returned raw model output instead."""
    first_line, _, rest = (text or "").partition("\n")
    match = STATUS_LINE.match(first_line.strip())
    if not match:
        return None, text.strip()

    status = STATUS_MAP.get(match.group(1).strip().rstrip(".").lower())
    if status is None:
        return None, text.strip()

    body = AI_RESPONSE_PREFIX.sub("", rest.lstrip("\n"), count=1)
    return status, body.strip()


def base_url(args):
    if args.base_url:
        return args.base_url
    env_override = os.environ.get("RAG_API_BASE_URL")
    if env_override:
        return env_override
    return ALB_URLS[args.env]


async def review_via_api(client, entry, contract=None):
    """POST one requirement to /agent, falling back to an error-marked review
    on any request failure so a single bad row cannot stop the run."""
    review = RequirementReview(
        requirement=entry["requirement"], sheet=entry["sheet"], item=entry["item"],
        legal_cite=entry["legal_cite"], row=entry["row"],
    )
    payload = {"query": entry["requirement"]}
    if contract:
        payload["contract"] = contract
    try:
        response = await client.post("/agent", json=payload)
        response.raise_for_status()
        data = response.json()
    except Exception as lclEx:
        log.warning(f"review_via_api() {entry['sheet']} row {entry['row']} failed: {lclEx}")
        review.error = f"{type(lclEx).__name__}: {lclEx}"
        return review

    if not data.get("success", True):
        review.argument = strip_thinking(data.get("response", ""))
        review.error = review.argument
        return review

    status, argument = parse_agent_review(strip_thinking(data.get("response", "")))
    review.argument = argument
    if status:
        review.status = status
    else:
        review.error = NO_STATUS_NOTE
    return review


async def review_all(client, todo, sidecar, concurrency, contract=None):
    gate = asyncio.Semaphore(concurrency)
    counter = {"finished": 0}

    async def run_one(entry):
        async with gate:
            review = await review_via_api(client, entry, contract=contract)
            append_sidecar(sidecar, review)
            counter["finished"] += 1
            print(f"  [{counter['finished']}/{len(todo)}] {entry['sheet']} {entry['item']} -> {review.status}")
            return review

    return await asyncio.gather(*(run_one(entry) for entry in todo))


async def review_workbook(client, workbook_path, sheets=None, limit=None, concurrency=DEFAULT_CONCURRENCY,
                          fresh=False, skip_answered=False, render_only=False, contract=None):
    workbook = CRTWorkbook(workbook_path)
    pending = workbook.requirements(sheet_names=sheets, skip_answered=skip_answered)
    if limit:
        pending = pending[:limit]

    sidecar = sidecar_path(workbook_path)
    if fresh and sidecar.exists():
        sidecar.unlink()
    done = load_sidecar(sidecar)

    todo = [row for row in pending if (row["sheet"], row["row"]) not in done]
    print(f"{workbook_path.name}: {len(pending)} requirement(s), {len(done)} already reviewed, "
          f"{len(todo)} to go")

    if render_only:
        if todo:
            report_unreviewed(todo)
    else:
        for review in await review_all(client, todo, sidecar, concurrency, contract=contract):
            done[(review.sheet, review.row)] = review

    reviews = [done[(row["sheet"], row["row"])] for row in pending
               if (row["sheet"], row["row"]) in done]
    for review in reviews:
        workbook.write_review(review)
    workbook.write_analysis(reviews)

    saved = workbook.save(output_path(workbook_path))
    summarise(workbook_path.name, reviews)
    return saved, reviews


async def resolve_contract_label(client, contract):
    """What to print before burning API calls across every row: the pinned
    --contract, or the server's current default from GET /contracts, so a run
    against the wrong table fails fast and visibly instead of silently."""
    if contract:
        return contract
    try:
        response = await client.get("/contracts")
        response.raise_for_status()
        return f"{response.json().get('default', 'unknown')} (server default - pass --contract to pin it)"
    except Exception as lclEx:
        return f"unknown - could not reach GET /contracts: {lclEx}"


async def run(args):
    folder = Path(args.folder) if args.folder else default_folder()
    if not folder.is_dir():
        print(f"Folder not found: {folder}")
        return 1

    workbooks = find_workbooks(folder)
    if not workbooks:
        print(f"No .xlsm or .xlsx workbooks in {folder}")
        return 1

    locked = [output_path(path) for path in workbooks if not saveable(output_path(path))]
    if locked:
        print("Close these in Excel first, otherwise the run cannot save its findings:")
        for path in locked:
            print(f"  {path}")
        return 1

    url = base_url(args)
    verb = "Rendering" if args.render_only else "Answering"
    print(f"{verb} {len(workbooks)} workbook(s) in {folder}"
          + ("" if args.render_only else f" via {url}") + "\n")

    async with httpx.AsyncClient(base_url=url, timeout=args.timeout) as client:
        if not args.render_only:
            try:
                health = await client.get("/health")
                health.raise_for_status()
            except Exception as lclEx:
                print(f"Could not reach {url}: {lclEx}")
                print("Check VPN/network path to the ALB - no AWS credentials are needed, just connectivity.")
                return 1

            contract_label = await resolve_contract_label(client, args.contract)
            print(f"Contract: {contract_label}\n")

        for workbook_path in workbooks:
            saved, _ = await review_workbook(
                client,
                workbook_path,
                sheets=args.sheet or None,
                limit=args.limit,
                concurrency=args.concurrency,
                fresh=args.fresh,
                skip_answered=args.skip_answered,
                render_only=args.render_only,
                contract=args.contract,
            )
            print(f"  written to {saved}\n")

    return 0


def main():
    sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--env", choices=sorted(ALB_URLS), default=DEFAULT_ENV,
                        help=f"Which environment's ALB to call (default {DEFAULT_ENV}, "
                             "set DEFAULT_ENV in this file to change it). Overridden by "
                             "RAG_API_BASE_URL if set, or by --base-url.")
    parser.add_argument("--base-url",
                        help="Override the ALB URL, e.g. for a local uvicorn run. Takes "
                             "precedence over RAG_API_BASE_URL and --env.")
    parser.add_argument("--contract", default=DEFAULT_CONTRACT,
                        help="Embeddings table to search, e.g. embeddings_ne_1_1. "
                             f"Default: {DEFAULT_CONTRACT!r} (set DEFAULT_CONTRACT in this "
                             "file to change it; None uses the API's active contract).")
    parser.add_argument("--folder", help="Folder holding the workbooks (default: output_excel/)")
    parser.add_argument("--sheet", action="append",
                        help="Only this sheet, repeatable. Default is all requirement sheets.")
    parser.add_argument("--limit", type=int, help="Stop after this many requirements, for a smoke test")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Requirements in flight at once (default {DEFAULT_CONCURRENCY})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"Per-request timeout in seconds (default {DEFAULT_TIMEOUT})")
    parser.add_argument("--skip-answered", action="store_true",
                        help="Leave rows that already have a Status alone")
    parser.add_argument("--fresh", action="store_true", help="Discard the sidecar and answer everything again")
    parser.add_argument("--render-only", action="store_true",
                        help="Rebuild the workbook from the sidecar without querying anything")
    args = parser.parse_args()

    if args.render_only and args.fresh:
        parser.error("--fresh discards the sidecar that --render-only renders from. Pick one.")

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
