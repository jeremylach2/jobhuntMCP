"""SmartRecruiters public Posting API.

Endpoints:
  - ``https://api.smartrecruiters.com/v1/companies/{company}/postings`` (list)
  - ``https://api.smartrecruiters.com/v1/companies/{company}/postings/{id}`` (one)

The slug is the company identifier from its board URL, e.g.
``jobs.smartrecruiters.com/ServiceNow`` -> ``ServiceNow`` (case-insensitive).

Unlike the other ATS feeds, the list endpoint carries no description: the text
is only on the per-posting endpoint. Fetching it during sync would mean one
request per posting, i.e. ten-plus minutes for a single large employer at this
project's one-second spacing. So sync stores postings without a description,
and ``fetch_description`` fills it in the first time one is actually read
(see ``get_job`` in server.py). The triage prefilter works from title and
department in the meantime, which is where most of its weight is anyway.

An unknown company is not a 404 here: it returns 200 and an empty list, the
same as a real company with nothing open. Both are reported as an error,
since neither gives sync anything to do.
"""

from __future__ import annotations

import asyncio

import httpx

from ..models import Job, html_to_text
from .base import SourceResult

BASE = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
SOURCE = "smartrecruiters"
PAGE_SIZE = 100  # the API's maximum
MAX_PAGES = 50  # 5,000 postings; a board bigger than that is not a target list entry

_SECTIONS = ("jobDescription", "qualifications", "additionalInformation", "companyDescription")


def _location(loc: dict) -> str:
    where = loc.get("fullLocation") or ", ".join(
        p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p
    )
    if loc.get("remote"):
        where = f"{where} (remote)" if where else "Remote"
    elif loc.get("hybrid"):
        where = f"{where} (hybrid)"
    return where


def _to_job(item: dict, slug: str, company: str) -> Job:
    loc = item.get("location") or {}
    posting_id = str(item.get("id", ""))
    return Job(
        source=SOURCE,
        # Carries the company identifier so the description can be fetched
        # later from the job row alone.
        source_id=f"{slug}/{posting_id}",
        company=company,
        title=(item.get("name") or "").strip(),
        url=f"https://jobs.smartrecruiters.com/{slug}/{posting_id}",
        location=_location(loc),
        remote=bool(loc.get("remote")) and not loc.get("hybrid"),
        department=(item.get("department") or {}).get("label", "")
        or (item.get("function") or {}).get("label", ""),
        posted_at=item.get("releasedDate", ""),
    )


async def fetch_board(
    client: httpx.AsyncClient, slug: str, display_name: str = "", delay: float = 1.0
) -> list[Job]:
    company = display_name or slug
    jobs: list[Job] = []
    for page in range(MAX_PAGES):
        resp = await client.get(
            BASE.format(slug=slug), params={"limit": PAGE_SIZE, "offset": page * PAGE_SIZE}
        )
        resp.raise_for_status()
        payload = resp.json()
        batch = payload.get("content") or []
        if page == 0 and not batch:
            raise ValueError("no postings (unknown company, or nothing open)")
        jobs.extend(_to_job(item, slug, company) for item in batch)
        if len(jobs) >= payload.get("totalFound", 0) or len(batch) < PAGE_SIZE:
            break
        await asyncio.sleep(delay)
    return jobs


async def probe_board(client: httpx.AsyncClient, slug: str) -> tuple[int, list[str]]:
    """(total postings, first few titles) from one request, for board discovery.

    `fetch_board` would page through the whole board, which for a large
    employer is dozens of requests just to answer "does this exist".
    """
    resp = await client.get(BASE.format(slug=slug), params={"limit": 3})
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("content"):
        raise ValueError("no postings (unknown company, or nothing open)")
    titles = [(item.get("name") or "").strip() for item in payload["content"]]
    return int(payload.get("totalFound", len(titles))), titles


async def fetch_description(client: httpx.AsyncClient, source_id: str) -> str:
    """Full posting text for a stored SmartRecruiters ``source_id``."""
    slug, _, posting_id = source_id.partition("/")
    resp = await client.get(f"{BASE.format(slug=slug)}/{posting_id}")
    resp.raise_for_status()
    sections = ((resp.json().get("jobAd") or {}).get("sections")) or {}
    parts = []
    for key in _SECTIONS:
        section = sections.get(key) or {}
        body = html_to_text(section.get("text"))
        if body:
            title = section.get("title", "")
            parts.append(f"{title}\n{body}" if title else body)
    return "\n\n".join(parts)


async def sync(
    client: httpx.AsyncClient, boards: dict[str, str], delay: float = 1.0
) -> SourceResult:
    result = SourceResult(source=SOURCE)
    for slug, display in boards.items():
        try:
            result.jobs.extend(await fetch_board(client, slug, display, delay=delay))
            result.fetched.append(display or slug)
        except Exception as exc:  # noqa: BLE001
            result.note_error(display or slug, exc)
        await asyncio.sleep(delay)
    return result
