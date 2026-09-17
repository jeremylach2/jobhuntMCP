"""Ashby public posting API.

Endpoint: ``https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true``

This is Ashby's documented public feed and the richest of the three ATS
sources: it returns plain-text descriptions, an explicit ``isRemote`` flag, and
posted salary bands where the employer publishes them. Many AI-infrastructure
startups run their boards on Ashby, which makes it the highest-yield source for
a platform/agent-infrastructure search.
"""

from __future__ import annotations

import asyncio

import httpx

from ..models import Job, html_to_text
from .base import SourceResult

BASE = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
SOURCE = "ashby"


def _compensation(item: dict) -> str:
    comp = item.get("compensation") or {}
    return (
        comp.get("compensationTierSummary")
        or comp.get("scrapeableCompensationSalarySummary")
        or ""
    )


def _location(item: dict) -> str:
    """Primary location, with remote alternates appended when they exist."""
    primary = item.get("location") or ""
    extras = [
        sec.get("location", "")
        for sec in (item.get("secondaryLocations") or [])
        if sec.get("location")
    ]
    if extras:
        return f"{primary} (+{len(extras)} more: {', '.join(extras[:3])})"
    return primary


async def fetch_board(client: httpx.AsyncClient, slug: str, display_name: str = "") -> list[Job]:
    resp = await client.get(
        BASE.format(slug=slug), params={"includeCompensation": "true"}
    )
    resp.raise_for_status()
    payload = resp.json()
    company = display_name or slug
    jobs: list[Job] = []
    for item in payload.get("jobs", []):
        if not item.get("isListed", True):
            continue
        description = item.get("descriptionPlain") or html_to_text(
            item.get("descriptionHtml")
        )
        # Ashby sets isRemote on any posting with a remote option, including
        # hybrid roles. workplaceType is the stricter signal.
        workplace = (item.get("workplaceType") or "").lower()
        jobs.append(
            Job(
                source=SOURCE,
                source_id=str(item.get("id", "")),
                company=company,
                title=(item.get("title") or "").strip(),
                url=item.get("jobUrl", "") or item.get("applyUrl", ""),
                location=_location(item),
                remote=workplace == "remote" or (
                    bool(item.get("isRemote")) and workplace != "onsite"
                ),
                department=item.get("team") or item.get("department") or "",
                description=description,
                compensation=_compensation(item),
                posted_at=item.get("publishedAt", ""),
            )
        )
    return jobs


async def sync(
    client: httpx.AsyncClient, boards: dict[str, str], delay: float = 1.0
) -> SourceResult:
    result = SourceResult(source=SOURCE)
    for slug, display in boards.items():
        try:
            result.jobs.extend(await fetch_board(client, slug, display))
            result.fetched.append(display or slug)
        except Exception as exc:  # noqa: BLE001
            result.note_error(display or slug, exc)
        await asyncio.sleep(delay)
    return result
