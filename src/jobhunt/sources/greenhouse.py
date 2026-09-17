"""Greenhouse public job board API.

Endpoint: ``https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true``

``content=true`` returns the full HTML description in the same call, which
avoids a second request per posting. The slug is the path segment on a
company's board URL, e.g. ``job-boards.greenhouse.io/anthropic`` -> ``anthropic``.
"""

from __future__ import annotations

import asyncio

import httpx

from ..models import Job, html_to_text, looks_remote
from .base import SourceResult

BASE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
SOURCE = "greenhouse"


async def fetch_board(client: httpx.AsyncClient, slug: str, display_name: str = "") -> list[Job]:
    resp = await client.get(BASE.format(slug=slug), params={"content": "true"})
    resp.raise_for_status()
    payload = resp.json()
    company = display_name or slug
    jobs: list[Job] = []
    for item in payload.get("jobs", []):
        location = (item.get("location") or {}).get("name", "")
        department = ""
        departments = item.get("departments") or []
        if departments:
            department = departments[0].get("name", "")
        title = item.get("title", "")
        jobs.append(
            Job(
                source=SOURCE,
                source_id=str(item.get("id", "")),
                company=company,
                title=title,
                url=item.get("absolute_url", ""),
                location=location,
                remote=looks_remote(location, title),
                department=department,
                description=html_to_text(item.get("content")),
                posted_at=item.get("updated_at", "") or item.get("first_published", ""),
            )
        )
    return jobs


async def sync(
    client: httpx.AsyncClient, boards: dict[str, str], delay: float = 1.0
) -> SourceResult:
    """Fetch every configured Greenhouse board.

    ``boards`` maps slug -> display name. Boards are fetched serially with a
    small delay rather than concurrently. A personal tool has no reason to
    hammer a free public endpoint.
    """
    result = SourceResult(source=SOURCE)
    for slug, display in boards.items():
        try:
            result.jobs.extend(await fetch_board(client, slug, display))
            result.fetched.append(display or slug)
        except Exception as exc:  # noqa: BLE001 - one bad slug must not abort the sync
            result.note_error(display or slug, exc)
        await asyncio.sleep(delay)
    return result
