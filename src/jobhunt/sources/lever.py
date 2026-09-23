"""Lever public postings API.

Endpoint: ``https://api.lever.co/v0/postings/{slug}?mode=json``

Lever returns a flat list of postings with the description already split into
plain text (``descriptionPlain``) plus structured ``lists`` of requirements.
An unknown slug returns ``{"ok": false}`` rather than a 404, so that shape is
checked explicitly.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx

from ..models import Job, html_to_text, looks_remote
from .base import SourceResult

BASE = "https://api.lever.co/v0/postings/{slug}"
SOURCE = "lever"


def _iso_from_ms(value: object) -> str:
    """Lever timestamps are epoch milliseconds. Every other source stores ISO
    strings, and date filtering compares them as text, so convert here."""
    if not isinstance(value, (int, float)):
        return ""
    return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()


def _description(item: dict) -> str:
    """Rebuild the full posting text from Lever's split fields."""
    parts = [item.get("descriptionPlain") or html_to_text(item.get("description"))]
    for block in item.get("lists") or []:
        heading = block.get("text", "")
        body = html_to_text(block.get("content"))
        parts.append(f"{heading}\n{body}" if heading else body)
    parts.append(item.get("additionalPlain") or "")
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


async def fetch_board(client: httpx.AsyncClient, slug: str, display_name: str = "") -> list[Job]:
    resp = await client.get(BASE.format(slug=slug), params={"mode": "json"})
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, dict):
        # Lever signals an unknown board with a 200 and an error body.
        raise ValueError(payload.get("error", "unknown Lever board"))

    company = display_name or slug
    jobs: list[Job] = []
    for item in payload:
        categories = item.get("categories") or {}
        location = categories.get("location", "") or ""
        workplace = item.get("workplaceType", "") or ""
        title = item.get("text", "")
        description = _description(item)
        jobs.append(
            Job(
                source=SOURCE,
                source_id=str(item.get("id", "")),
                company=company,
                title=title,
                url=item.get("hostedUrl", "") or item.get("applyUrl", ""),
                location=location,
                # An explicit non-remote workplaceType (e.g. "hybrid") is
                # authoritative and shouldn't be overridden by a location/title
                # guess. Only fall back to the heuristic when Lever left it blank.
                remote=(
                    workplace.lower() == "remote"
                    if workplace
                    else looks_remote(location, title, description)
                ),
                department=categories.get("team", "") or categories.get("department", "") or "",
                description=description,
                posted_at=_iso_from_ms(item.get("createdAt")),
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
