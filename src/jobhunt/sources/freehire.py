"""Freehire job feed, for part-time and contract work.

Endpoint: ``https://freehire.me/api/v1/agent/jobs/search`` (API reference:
https://freehire.me/docs/api). Unlike the other aggregators this one is
authenticated: the key comes from ``FREEHIRE_API_KEY`` (see ``config.load`` for
``.env`` handling) and is sent as a Bearer token.

Freehire filters server-side, so instead of pulling a firehose and matching
locally this asks only for remote postings of the short-engagement types
(``contract``, ``part_time``) and runs one full-text query per AI-flavoured
term. Results are deduplicated by ``public_slug`` across queries. The
employment type is stored in ``Job.department`` so ``search_jobs`` can match it.

The ``/agent`` variant returns full descriptions inline, so no follow-up fetch
is needed.
"""

from __future__ import annotations

import asyncio
import html
import os

import httpx

from ..models import Job, html_to_text
from .base import SourceResult

BASE = "https://freehire.me/api/v1/agent/jobs/search"
SOURCE = "freehire"
API_KEY_ENV = "FREEHIRE_API_KEY"

EMPLOYMENT_TYPES = ("contract", "part_time")
DEFAULT_QUERIES = ("AI", "LLM", "agent", "MCP")
# Geography is an eligibility gate, not a fit judgment: "remote" on this feed
# often means remote within Brazil or the EU. "global" is worldwide-open roles.
DEFAULT_REGIONS = ("north_america", "global")
PAGE_SIZE = 100  # API maximum
MAX_PAGES = 3  # per (query, employment type); keeps one sync bounded


def _compensation(enrichment: dict) -> str:
    lo = enrichment.get("salary_min") or 0
    hi = enrichment.get("salary_max") or 0
    if not lo and not hi:
        return ""
    cur = enrichment.get("salary_currency") or ""
    period = enrichment.get("salary_period") or ""
    amount = f"{int(lo):,} - {int(hi):,}" if lo and hi else f"{int(lo or hi):,}"
    return " ".join(p for p in (cur, amount, f"/ {period}" if period else "") if p)


def _clean(value: str | None) -> str:
    """Decode entities in short text fields (the feed double-escapes, e.g. ``&amp;``)."""
    return html.unescape(value or "").strip()


def _to_job(item: dict) -> Job:
    enrichment = item.get("enrichment") or {}
    return Job(
        source=SOURCE,
        source_id=item["public_slug"],
        company=_clean(item.get("company")),
        title=_clean(item.get("title")),
        url=item.get("url", ""),
        location=_clean(item.get("location")) or "Remote",
        remote=True,  # every request filters work_mode=remote
        department=enrichment.get("employment_type", ""),
        description=html_to_text(item.get("description")),
        compensation=_compensation(enrichment),
        posted_at=item.get("posted_at") or item.get("created_at") or "",
    )


async def sync(
    client: httpx.AsyncClient,
    queries: tuple[str, ...] | list[str] | None = None,
    delay: float = 1.0,
    regions: tuple[str, ...] | list[str] | None = None,
) -> SourceResult:
    """Fetch remote contract/part-time postings matching each query, in ``regions``."""
    result = SourceResult(source=SOURCE)
    key = os.environ.get(API_KEY_ENV)
    if not key:
        result.errors[SOURCE] = f"{API_KEY_ENV} not set"
        return result

    headers = {"Authorization": f"Bearer {key}"}
    seen: set[str] = set()
    try:
        for employment_type in EMPLOYMENT_TYPES:
            for query in queries or DEFAULT_QUERIES:
                for page in range(MAX_PAGES):
                    resp = await client.get(
                        BASE,
                        headers=headers,
                        params={
                            "q": query,
                            "employment_type": employment_type,
                            "work_mode": "remote",
                            "regions": ",".join(regions or DEFAULT_REGIONS),
                            "sort": "posted_at",
                            "description_format": "text",
                            "limit": PAGE_SIZE,
                            "offset": page * PAGE_SIZE,
                        },
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    items = body.get("data") or []
                    for item in items:
                        slug = item.get("public_slug")
                        if slug and slug not in seen and not item.get("closed_at"):
                            seen.add(slug)
                            result.jobs.append(_to_job(item))
                    total = (body.get("meta") or {}).get("total", 0)
                    await asyncio.sleep(delay)
                    if (page + 1) * PAGE_SIZE >= total:
                        break
        result.fetched.append(SOURCE)
    except Exception as exc:  # noqa: BLE001
        result.note_error(SOURCE, exc)
    return result
