"""Himalayas remote-job feed.

Endpoint: ``https://himalayas.app/jobs/api``

Unlike the ATS sources, this is an aggregator: it is not scoped to a company
list, so it provides breadth for roles at companies not yet on the target list.
Everything here is remote by definition. The feed is cursor-paginated and
returns the whole firehose, so it is filtered locally by keyword before
anything is stored. Otherwise a sync would import thousands of irrelevant
non-engineering postings.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx

from ..models import Job, html_to_text
from .base import SourceResult

BASE = "https://himalayas.app/jobs/api"
SOURCE = "himalayas"
PAGE_SIZE = 100


def _salary(item: dict) -> str:
    lo, hi = item.get("minSalary") or 0, item.get("maxSalary") or 0
    if not lo and not hi:
        return ""
    currency = item.get("currency") or "USD"
    period = item.get("salaryPeriod") or "annual"
    if lo and hi:
        return f"{currency} {int(lo):,} - {int(hi):,} ({period})"
    return f"{currency} {int(lo or hi):,} ({period})"


def _matches(item: dict, keywords: list[str]) -> bool:
    if not keywords:
        return True
    blob = " ".join(
        [
            item.get("title", "") or "",
            " ".join(item.get("categories") or []),
            item.get("excerpt", "") or "",
        ]
    ).lower()
    return any(kw.lower() in blob for kw in keywords)


def _to_job(item: dict) -> Job:
    pub = item.get("pubDate")
    posted = (
        datetime.fromtimestamp(pub, tz=UTC).isoformat()
        if isinstance(pub, (int, float))
        else ""
    )
    return Job(
        source=SOURCE,
        source_id=item.get("guid", "") or item.get("applicationLink", ""),
        company=item.get("companyName", ""),
        title=item.get("title", ""),
        url=item.get("applicationLink", "") or item.get("guid", ""),
        location=", ".join(item.get("locationRestrictions") or []) or "Remote",
        remote=True,
        department=", ".join((item.get("parentCategories") or [])[:2]),
        description=html_to_text(item.get("description")) or item.get("excerpt", ""),
        compensation=_salary(item),
        posted_at=posted,
    )


async def sync(
    client: httpx.AsyncClient,
    keywords: list[str],
    max_pages: int = 5,
    delay: float = 1.0,
) -> SourceResult:
    """Page through the feed, keeping only postings matching ``keywords``."""
    result = SourceResult(source=SOURCE)
    cursor: str | None = None
    try:
        for _ in range(max_pages):
            params: dict[str, str | int] = {"limit": PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            resp = await client.get(BASE, params=params)
            resp.raise_for_status()
            payload = resp.json()
            batch = payload.get("jobs", [])
            if not batch:
                break
            result.jobs.extend(_to_job(i) for i in batch if _matches(i, keywords))
            cursor = payload.get("nextCursor")
            if not cursor:
                break
            await asyncio.sleep(delay)
        result.fetched.append("himalayas")
    except Exception as exc:  # noqa: BLE001
        result.note_error("himalayas", exc)
    return result
