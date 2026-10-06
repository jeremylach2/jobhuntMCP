"""Hacker News "Ask HN: Who is hiring?" threads, via the Algolia search API.

Endpoints:
  - ``https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring``
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
# Newest first. Plain /search ranks by relevance and can return a years-old thread.
SEARCH_BY_DATE = "https://hn.algolia.com/api/v1/search_by_date"
ITEM = "https://hn.algolia.com/api/v1/items/{id}"
SOURCE = "hn"

_SEPARATORS = re.compile(r"\s*[|–—\-•]\s*")


async def latest_thread_id(client: httpx.AsyncClient) -> tuple[str, str]:
    """Return (story_id, title) for the most recent Who-is-hiring thread."""
    resp = await client.get(
        SEARCH_BY_DATE,
        params={
            "tags": "story,author_whoishiring",
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


def _to_job(comment_id: str, author: str, text: str, created_at: str) -> Job:
    company, role, location = _parse_header(text)
    return Job(
        source=SOURCE,
        source_id=comment_id,
        company=company or f"(HN {author})",
        title=role or "See posting",
        url=f"https://news.ycombinator.com/item?id={comment_id}",
        location=location,
        remote=looks_remote(text[:400]),
        department="",
        description=text,
        posted_at=created_at,
    )


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
            result.jobs.append(
                _to_job(str(child.get("id", "")), child["author"], text,
                        child.get("created_at", ""))
            )
        result.fetched.append(title)
    except Exception as exc:  # noqa: BLE001
        result.note_error("hn", exc)
    return result


async def search(
    client: httpx.AsyncClient, query: str, max_results: int = 60
) -> SourceResult:
    """Full-text search the current month's thread for ``query``, server-side.

    Covers every top-level posting in the thread, not just the first ``limit``
    that ``sync`` walks. Replies to postings match too, so they're dropped by
    checking the comment's parent is the story itself.
    """
    result = SourceResult(source=SOURCE)
    try:
        story_id, title = await latest_thread_id(client)
        resp = await client.get(
            SEARCH,
            params={"tags": f"comment,story_{story_id}", "query": query, "hitsPerPage": 500},
        )
        resp.raise_for_status()
        for hit in resp.json().get("hits", []):
            if str(hit.get("parent_id")) != story_id or not hit.get("comment_text"):
                continue
            text = html_to_text(hit["comment_text"])
            result.jobs.append(
                _to_job(str(hit.get("objectID", "")), hit.get("author") or "?", text,
                        hit.get("created_at", ""))
            )
            if len(result.jobs) >= max_results:
                break
        result.fetched.append(title)
    except Exception as exc:  # noqa: BLE001
        result.note_error("hn", exc)
    return result
