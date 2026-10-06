"""Orchestrates fetching every configured source into the local store."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .config import Config
from .db import Store
from .scoring import variant_key
from .sources import ATS, freehire, himalayas, hn, remoteok
from .sources.base import SourceResult, make_client

# Aggregator postings unseen for this long are retired. Generous on purpose:
# RemoteOK's window is only its newest ~100 postings, and the HN thread is
# replaced monthly, so a posting drops out of the feed long before it closes.
FEED_RETIRE_DAYS = 30


@dataclass
class SyncReport:
    new: int = 0
    seen: int = 0
    retired: int = 0
    per_source: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "new_postings": self.new,
            "postings_seen": self.seen,
            "retired": self.retired,
            "per_source": self.per_source,
            "errors": self.errors,
        }


async def run_sync(
    cfg: Config,
    store: Store,
    sources: list[str] | None = None,
    delay: float = 1.0,
) -> SyncReport:
    """Fetch the requested sources and write them to the store.

    Defaults to the ATS boards only. The aggregators are opt-in because they
    return the whole market rather than a curated company list, which is useful
    for breadth but noisy as a default.
    """
    sources = sources or list(ATS)
    report = SyncReport()

    async with make_client() as client:
        for name in sources:
            if name in ATS:
                boards = cfg.boards(name)
                if not boards:
                    continue
                result = await ATS[name].sync(client, boards, delay=delay)
            elif name == "himalayas":
                result = await himalayas.sync(client, cfg.keywords, delay=delay)
            elif name == "hn":
                result = await hn.sync(client, cfg.keywords)
            elif name == "remoteok":
                result = await remoteok.sync(client, cfg.keywords)
            elif name == "freehire":
                opts = cfg.targets.get("freehire", {}) or {}
                result = await freehire.sync(
                    client, opts.get("queries"), delay=delay, regions=opts.get("regions")
                )
            else:
                report.errors[name] = "unknown source"
                continue

            new, seen = store.upsert_jobs(result.jobs)

            retired = 0
            if name in ATS and result.fetched:
                # Only retire boards that were actually reached this run.
                retired = store.deactivate_missing(
                    name, result.fetched, {j.id for j in result.jobs}
                )
            elif result.fetched:
                # Feeds are a rolling window, so absence from one fetch means
                # nothing. Retire only what hasn't shown up in a while.
                cutoff = datetime.now(UTC) - timedelta(days=FEED_RETIRE_DAYS)
                retired = store.retire_unseen(name, cutoff.isoformat())

            report.new += new
            report.seen += seen
            report.retired += retired
            report.per_source[name] = {
                "new": new,
                "seen": seen,
                "retired": retired,
                "boards_ok": len(result.fetched),
                "boards_failed": len(result.errors),
            }
            for board, err in result.errors.items():
                report.errors[f"{name}:{board}"] = err

    return report


# Sources that can answer an arbitrary query across the wider market, rather
# than only the watched companies or the profile keywords.
MARKET_SOURCES = ("himalayas", "hn", "remoteok")


@dataclass
class MarketResult:
    job_ids: list[str] = field(default_factory=list)  # in source order, deduped
    new: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


async def run_market_search(
    store: Store,
    query: str,
    sources: list[str] | None = None,
    max_per_source: int = 60,
    delay: float = 1.0,
    countries: list[str] | None = None,
) -> MarketResult:
    """Search the aggregators live for ``query`` and store what comes back.

    Results are upserted so get_job / record_fit / set_status work on them
    like any synced posting. Nothing is retired: a search hit list says
    nothing about which postings closed.

    ``countries`` limits himalayas to postings hirable from any of them (an
    eligibility gate, like freehire's regions, not a fit judgment). hn and
    remoteok only have free-text locations, so they're never filtered by it:
    a guess there would silently drop real matches.
    """
    out = MarketResult()
    seen_ids: set[str] = set()
    async with make_client() as client:
        for name in sources or MARKET_SOURCES:
            if name == "himalayas":
                result = await _himalayas_by_country(
                    client, query, countries or [], max_per_source, delay
                )
            elif name == "hn":
                result = await hn.search(client, query, max_per_source)
            elif name == "remoteok":
                # No server-side search, but the whole feed is one request.
                result = await remoteok.sync(client, [query])
                del result.jobs[max_per_source:]
            else:
                out.errors[name] = f"unknown source, expected one of {', '.join(MARKET_SOURCES)}"
                continue

            new, _ = store.upsert_jobs(result.jobs)
            out.new += new
            out.per_source[name] = len(result.jobs)
            for job in result.jobs:
                if job.id not in seen_ids:
                    seen_ids.add(job.id)
                    out.job_ids.append(job.id)
            for board, err in result.errors.items():
                out.errors[board] = err
    return out


async def _himalayas_by_country(
    client: httpx.AsyncClient,
    query: str,
    countries: list[str],
    max_per_country: int,
    delay: float,
) -> SourceResult:
    """One himalayas search per country (the API takes one), merged by id."""
    if not countries:
        return await himalayas.search(client, query, max_per_country, delay=delay)
    merged = SourceResult(source="himalayas")
    seen: set[str] = set()
    for i, country in enumerate(countries):
        if i:
            await asyncio.sleep(delay)
        part = await himalayas.search(client, query, max_per_country, delay=delay, country=country)
        for job in part.jobs:  # a posting open to several countries comes back for each
            if job.id not in seen:
                seen.add(job.id)
                merged.jobs.append(job)
        merged.fetched += part.fetched
        merged.errors.update(part.errors)
    return merged


def market_rows(store: Store, job_ids: list[str]) -> list[tuple[Any, int]]:
    """Rows for ``job_ids`` in that order, one per role, with its variant count.

    Aggregators list one posting per country, so a single role can fill
    several lines. Rows sharing (company, title) collapse into the first one
    seen, paired with how many more were folded into it. One query, no
    descriptions: the caller only renders one-line summaries.
    """
    rows = store.search(ids=job_ids, limit=0, description_limit=0)
    by_id = {row["id"]: row for row in rows}
    kept: dict[tuple[str, str], list[Any]] = {}
    for job_id in job_ids:
        row = by_id.get(job_id)
        if row is None:
            continue
        key = variant_key(row)
        if key in kept:
            kept[key][1] += 1
        else:
            kept[key] = [row, 0]
    return [(row, extra) for row, extra in kept.values()]
