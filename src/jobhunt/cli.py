"""Command-line interface.

Covers the parts of the workflow that make sense without a model in the loop:
syncing boards, grepping stored postings, and moving applications along. The
judgment-heavy parts (reading a posting and deciding whether it fits) live in
the MCP server, because that is where a model is available to do them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import config
from .db import STATUSES, Store
from .scoring import triage
from .sync import run_sync


def _line(row) -> str:
    bits = [f"[{row['id']}] {row['company']} - {row['title']}"]
    if row["location"]:
        bits.append(f"({row['location']})")
    if row["compensation"]:
        bits.append(f"[{row['compensation']}]")
    if row["score"] is not None:
        bits.append(f"[fit {row['score']}]")
    if row["status"]:
        bits.append(f"[{row['status']}]")
    return " ".join(bits)


def cmd_sync(args, cfg, store) -> int:
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    print(f"Syncing {', '.join(sources)} ... (about 1s per board)", file=sys.stderr)
    report = asyncio.run(run_sync(cfg, store, sources, delay=args.delay))
    print(json.dumps(report.as_dict(), indent=2))
    return 0


def cmd_search(args, cfg, store) -> int:
    rows = store.search(
        query=args.query,
        company=args.company,
        source=args.source,
        remote_only=args.remote,
        min_score=args.min_fit if args.min_fit >= 0 else None,
        unscored_only=args.unscored,
        exclude_applied=args.exclude_applied,
        limit=args.limit,
        description_limit=0,  # _line() never reads it
    )
    if not rows:
        print("No matching postings.")
        return 1
    for row in rows:
        print(_line(row))
    print(f"\n{len(rows)} posting(s)", file=sys.stderr)
    return 0


def cmd_show(args, cfg, store) -> int:
    row = store.get_job(args.job_id)
    if row is None:
        print(f"No posting with id {args.job_id!r}", file=sys.stderr)
        return 1
    print(f"{row['company']} - {row['title']}")
    print(f"url:      {row['url']}")
    print(f"location: {row['location']}{' (remote)' if row['remote'] else ''}")
    if row["compensation"]:
        print(f"comp:     {row['compensation']}")
    print(f"source:   {row['source']}   posted: {row['posted_at']}")
    if row["score"] is not None:
        print(f"fit:      {row['score']}/100 - {row['verdict']}")
    if row["status"]:
        print(f"status:   {row['status']}")
    for event in store.events(args.job_id):
        print(f"  {event['at'][:10]} {event['kind']}: {event['detail']}")
    if not args.brief:
        print("\n--- description ---")
        print(row["description"])
    return 0


def cmd_shortlist(args, cfg, store) -> int:
    rows = store.search(
        unscored_only=True, remote_only=args.remote, limit=0, description_limit=4000
    )
    ranked = triage(rows, cfg.keywords)
    if not ranked:
        print("Nothing relevant among unscored postings.")
        return 1
    print(f"Screened {len(rows)} unscored, {len(ranked)} plausible:", file=sys.stderr)
    for rel, row in ranked[: args.limit]:
        print(f"{rel.score:3d}  {_line(row)}")
    return 0


def cmd_status(args, cfg, store) -> int:
    if not store.exists(args.job_id):
        print(f"No posting with id {args.job_id!r}", file=sys.stderr)
        return 1
    try:
        store.set_status(args.job_id, args.status, args.notes)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"{args.job_id} -> {args.status}")
    return 0


def cmd_pipeline(args, cfg, store) -> int:
    rows = store.pipeline(args.status)
    if not rows:
        print("Nothing tracked yet.")
        return 1
    for row in rows:
        line = f"[{row['id']}] {row['status']:<11} {row['company']} - {row['title']}"
        if row["applied_at"]:
            line += f"  (applied {row['applied_at'][:10]})"
        print(line)
    return 0


def cmd_stats(args, cfg, store) -> int:
    print(json.dumps(store.stats(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobhunt",
        description="Find, triage, and track engineering jobs from public ATS boards.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="fetch the latest postings from job boards")
    p.add_argument("--sources", default="greenhouse,ashby,lever",
                   help="comma-separated: greenhouse, ashby, lever, himalayas, hn, remoteok")
    p.add_argument("--delay", type=float, default=1.0,
                   help="seconds between board fetches (default 1.0)")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("search", help="search stored postings")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--company", default="")
    p.add_argument("--source", default="")
    p.add_argument("--remote", action="store_true")
    p.add_argument("--min-fit", type=int, default=-1, dest="min_fit")
    p.add_argument("--unscored", action="store_true")
    p.add_argument("--exclude-applied", action="store_true", dest="exclude_applied")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", help="print one posting in full")
    p.add_argument("job_id")
    p.add_argument("--brief", action="store_true", help="omit the description")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("shortlist", help="rank unscored postings by keyword relevance")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--remote", action="store_true")
    p.set_defaults(func=cmd_shortlist)

    p = sub.add_parser("status", help="set an application status")
    p.add_argument("job_id")
    p.add_argument("status", choices=STATUSES)
    p.add_argument("--notes", default="")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("pipeline", help="show tracked applications")
    p.add_argument("--status", default="", choices=("", *STATUSES))
    p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("stats", help="summarize local storage")
    p.set_defaults(func=cmd_stats)

    return parser


def main() -> int:
    # Salary bands and locations carry en-dashes and other non-ASCII. The
    # default Windows console codepage mangles them into replacement chars.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args()
    cfg = config.load()
    with Store(cfg.db_path) as store:
        return args.func(args, cfg, store)


if __name__ == "__main__":
    raise SystemExit(main())
