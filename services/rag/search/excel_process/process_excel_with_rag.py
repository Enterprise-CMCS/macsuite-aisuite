#!/usr/bin/env python3
"""Run job: review every requirement in the workbooks dropped into output_excel/.

Point it at a folder, it finds the Contract Review Tool workbooks in there, sends
each requirement through the review agents, and writes a reviewed copy alongside
the original. The uploaded file is never modified.

    python search/excel_process/process_excel_with_rag.py
    python search/excel_process/process_excel_with_rag.py --sheet "E. Providers & Network"
    python search/excel_process/process_excel_with_rag.py --limit 20 --concurrency 2

A full CRT is 667 requirements and each one costs three model calls, so progress
is appended to a .reviewed.jsonl sidecar as it goes. Re-running picks up where the
last run stopped; --fresh throws the sidecar away and starts over.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from common.utils.logger import log
from data_embeddings_storage.database.connection import close_db
from search.database_searching.agents import build_deps, review_requirement
from search.database_searching.review_models import RequirementReview
from search.excel_process.crt_workbook import CRTWorkbook

WORKBOOK_SUFFIXES = (".xlsm", ".xlsx")

# Bedrock throttles per account, and the connection pool tops out at five, so a
# handful of requirements in flight is the sweet spot. Raise it if you have the
# on-demand quota for it.
DEFAULT_CONCURRENCY = 4


def default_folder():
    # services/rag/search/excel_process/this_file -> repository root
    return Path(__file__).resolve().parents[4] / "output_excel"


def find_workbooks(folder):
    return sorted(path for path in folder.glob("*")
                  if path.suffix.lower() in WORKBOOK_SUFFIXES
                  and not path.name.startswith("~$")
                  and ".reviewed" not in path.name)


def saveable(path):
    """Whether the run will be able to write `path` when it finishes.

    Worth knowing before the model calls rather than after. Excel keeps an
    exclusive lock on a workbook it has open, and a full CRT is the better part of
    an hour of Bedrock time before anything reaches the save.
    """
    if not path.exists():
        return True
    try:
        with path.open("r+b"):
            return True
    except OSError:
        return False


def sidecar_path(workbook_path):
    return workbook_path.with_suffix(workbook_path.suffix + ".reviewed.jsonl")


def output_path(workbook_path):
    return workbook_path.with_name(f"{workbook_path.stem}.reviewed{workbook_path.suffix}")


def load_sidecar(path):
    """Reviews from an earlier run, keyed by sheet and row."""
    done = {}
    if not path.exists():
        return done

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                review = RequirementReview.model_validate_json(line)
            except Exception as lclEx:
                # A run killed mid-write leaves a partial last line. Skip it.
                log.debug(f"load_sidecar() Ignoring an unreadable sidecar line: {lclEx}")
                continue
            done[(review.sheet, review.row)] = review

    log.info(f"load_sidecar() Reusing {len(done)} requirement(s) already reviewed in {path.name}")
    return done


def append_sidecar(path, review):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(review.model_dump_json() + "\n")


async def review_workbook(workbook_path, sheets=None, limit=None, concurrency=DEFAULT_CONCURRENCY,
                          challenge=True, fresh=False, skip_answered=False, rerank=False):
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

    deps = build_deps(rerank=rerank)
    gate = asyncio.Semaphore(concurrency)
    counter = {"finished": 0}

    async def run_one(entry):
        async with gate:
            review = await review_requirement(
                entry["requirement"],
                deps=deps,
                sheet=entry["sheet"],
                item=entry["item"],
                legal_cite=entry["legal_cite"],
                row=entry["row"],
                challenge=challenge,
            )
            append_sidecar(sidecar, review)
            counter["finished"] += 1
            print(f"  [{counter['finished']}/{len(todo)}] {entry['sheet']} {entry['item']} "
                  f"-> {review.status}"
                  + (f" (confidence {review.combined_confidence:.2f})"
                     if review.combined_confidence is not None else ""))
            return review

    for review in await asyncio.gather(*(run_one(entry) for entry in todo)):
        done[(review.sheet, review.row)] = review

    reviews = [done[(row["sheet"], row["row"])] for row in pending
               if (row["sheet"], row["row"]) in done]
    for review in reviews:
        workbook.write_review(review)
    workbook.write_analysis(reviews)

    saved = workbook.save(output_path(workbook_path))
    summarise(workbook_path.name, reviews)
    return saved, reviews


def summarise(name, reviews):
    counts = {}
    for review in reviews:
        counts[review.status] = counts.get(review.status, 0) + 1
    errors = sum(1 for review in reviews if review.error)
    unverified = sum(1 for review in reviews if review.evidence and not review.quotes_verified)

    print(f"\n{name}: {len(reviews)} reviewed at {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    for status in ("MET", "NOT MET", "UNCLEAR"):
        if counts.get(status):
            print(f"  {status:<8} {counts[status]}")
    if unverified:
        print(f"  {unverified} row(s) cite a quote that could not be matched back to the retrieved text")
    if errors:
        print(f"  {errors} row(s) failed and are marked in the General Comments column")


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

    print(f"Reviewing {len(workbooks)} workbook(s) in {folder}\n")
    try:
        for workbook_path in workbooks:
            saved, _ = await review_workbook(
                workbook_path,
                sheets=args.sheet or None,
                limit=args.limit,
                concurrency=args.concurrency,
                challenge=not args.no_challenge,
                fresh=args.fresh,
                skip_answered=args.skip_answered,
                rerank=args.rerank,
            )
            print(f"  written to {saved}\n")
    finally:
        await close_db()

    return 0


def main():
    # CRT item ids carry the odd en dash ("I.E.1.07-08"). A console that cannot
    # encode one makes print() raise, which would take a two-hour run down with it,
    # so the progress lines degrade instead.
    sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--folder", help="Folder holding the workbooks (default: output_excel/)")
    parser.add_argument("--sheet", action="append",
                        help="Only this sheet, repeatable. Default is all requirement sheets.")
    parser.add_argument("--limit", type=int, help="Stop after this many requirements, for a smoke test")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Requirements in flight at once (default {DEFAULT_CONCURRENCY})")
    parser.add_argument("--no-challenge", action="store_true",
                        help="Skip the challenger and adjudicator, one pass only")
    parser.add_argument("--skip-answered", action="store_true",
                        help="Leave rows that already have a Status alone")
    parser.add_argument("--fresh", action="store_true", help="Discard the sidecar and review everything again")
    parser.add_argument("--rerank", action="store_true",
                        help="Add the Cohere rerank pass over the hybrid shortlist (off by default)")
    args = parser.parse_args()

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
