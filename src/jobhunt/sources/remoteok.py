"""RemoteOK job feed.

Endpoint: ``https://remoteok.com/api``

Like Himalayas, this is an aggregator rather than a company-scoped source: one
request returns the site's current firehose of the newest ~100 postings
across every category, with no pagination or server-side filtering, so it is
filtered locally by keyword before anything is stored. Every posting on the
site is remote by definition.

The endpoint 403s a generic ``User-Agent`` on some clients but has been
verified to work with this project's ``jobhunt/0.1`` UA (see ``base.py``).
The response's first element is a legal notice, not a job, and is skipped.
"""

from __future__ import annotations

import httpx

from ..models import Job, html_to_text
from .base import SourceResult

BASE = "https://remoteok.com/api"
SOURCE = "remoteok"
_MATCH_PREVIEW_LEN = 300


def _salary(item: dict) -> str:
    lo, hi = item.get("salary_min") or 0, item.get("salary_max") or 0
    if not lo and not hi:
        return ""
    if lo and hi:
        return f"USD {int(lo):,} - {int(hi):,}"
    return f"USD {int(lo or hi):,}"


def _matches(item: dict, preview: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    blob = " ".join(
        [
            item.get("position", "") or "",
            " ".join(item.get("tags") or []),
            preview,
        ]
    ).lower()
    return any(kw.lower() in blob for kw in keywords)


def _to_job(item: dict, text: str) -> Job:
    return Job(
        source=SOURCE,
        source_id=str(item.get("id", "")) or item.get("slug", ""),
        company=item.get("company", ""),
        title=item.get("position", ""),
        url=item.get("url", "") or item.get("apply_url", ""),
        location=item.get("location", "") or "Remote",
        remote=True,
        department=", ".join((item.get("tags") or [])[:2]),
        description=text,
        compensation=_salary(item),
        posted_at=item.get("date", ""),
    )


async def sync(client: httpx.AsyncClient, keywords: list[str]) -> SourceResult:
    """Fetch the current firehose, keeping only postings matching ``keywords``."""
    result = SourceResult(source=SOURCE)
    try:
        resp = await client.get(BASE)
        resp.raise_for_status()
        for item in resp.json():
            if "id" not in item:
                continue  # the leading legal-notice entry
            text = html_to_text(item.get("description"))
            if not _matches(item, text[:_MATCH_PREVIEW_LEN], keywords):
                continue
            result.jobs.append(_to_job(item, text))
        result.fetched.append("remoteok")
    except Exception as exc:  # noqa: BLE001
        result.note_error("remoteok", exc)
    return result
