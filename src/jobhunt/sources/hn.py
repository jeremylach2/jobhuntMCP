"""Hacker News "Ask HN: Who is hiring?" threads, via the Algolia search API.

Endpoints:
  - ``https://hn.algolia.com/api/v1/search?tags=story,author_whoishiring``
  - ``https://hn.algolia.com/api/v1/items/{id}`` for the comment tree

Each top-level comment in the monthly thread is one job posting written in free
form, so there is nothing to parse reliably into structured fields. This
adapter deliberately stops at light extraction: company name from the
conventional ``Company | Role | Location`` first line, and stores the raw
comment as the description. Judging what a posting actually is, is the job of
the model reading it through the MCP server, not of a regex here.
"""

from __future__ import annotations

import re

import httpx

from ..models import Job, html_to_text, looks_remote
from .base import SourceResult

SEARCH = "https://hn.algolia.com/api/v1/search"
ITEM = "https://hn.algolia.com/api/v1/items/{id}"
SOURCE = "hn"

_SEPARATORS = re.compile(r"\s*[|–—\-•]\s*")


async def latest_thread_id(client: httpx.AsyncClient) -> tuple[str, str]:
    """Return (story_id, title) for the most recent Who-is-hiring thread."""
    resp = await client.get(
        SEARCH,
        params={
            "tags": "story,author_whoishiring",
            "query": "Ask HN: Who is hiring?",
            "hitsPerPage": 5,
        },
    )
    resp.raise_for_status()
    for hit in resp.json().get("hits", []):
        title = hit.get("title", "") or ""
        if "who is hiring" in title.lower():
            return str(hit.get("objectID", "")), title
    raise ValueError("no Who-is-hiring thread found")


def _parse_header(text: str) -> tuple[str, str, str]:
    """Pull (company, role, location) from the conventional first line.

    Returns empty strings for anything the posting did not follow convention
    on. The full text is preserved regardless, so nothing is lost.
    """
    first = next((ln.strip() for ln in text.split("\n") if ln.strip()), "")
    parts = [p.strip() for p in _SEPARATORS.split(first) if p.strip()]
    company = parts[0][:80] if parts else ""
    role = parts[1][:120] if len(parts) > 1 else ""
    location = parts[2][:80] if len(parts) > 2 else ""
    return company, role, location


async def sync(
    client: httpx.AsyncClient, keywords: list[str], limit: int = 400
) -> SourceResult:
    """Fetch the current month's thread and keep comments matching ``keywords``."""
    result = SourceResult(source=SOURCE)
    try:
        story_id, title = await latest_thread_id(client)
        resp = await client.get(ITEM.format(id=story_id))
        resp.raise_for_status()
        children = resp.json().get("children", [])[:limit]
        for child in children:
            raw = child.get("text")
            if not raw or child.get("author") is None:
                continue  # deleted or flagged comment
            text = html_to_text(raw)
            if keywords and not any(kw.lower() in text.lower() for kw in keywords):
                continue
            company, role, location = _parse_header(text)
            result.jobs.append(
                Job(
                    source=SOURCE,
                    source_id=str(child.get("id", "")),
                    company=company or f"(HN {child.get('author')})",
                    title=role or "See posting",
                    url=f"https://news.ycombinator.com/item?id={child.get('id')}",
                    location=location,
                    remote=looks_remote(text[:400]),
                    department="",
                    description=text,
                    posted_at=child.get("created_at", ""),
                )
            )
        result.fetched.append(title)
    except Exception as exc:  # noqa: BLE001
        result.note_error("hn", exc)
    return result
