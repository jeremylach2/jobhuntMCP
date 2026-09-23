"""The jobhunt MCP server.

Design note, since this is the part worth explaining:

The tools here deliberately do not evaluate anything. They fetch, filter,
store, and retrieve. The model connected to this server does the judging.
That split matters. A tool that tried to decide "is this a good job for me"
in Python would be a pile of brittle keyword rules, and it would be wrong in
exactly the cases that matter. Instead `shortlist_for_review` uses cheap string
matching to cut tens of thousands of postings down to a readable handful, the
model reads those and forms an opinion, and `record_fit` writes that opinion
back so it persists across sessions.

Tool outputs are bounded for the same reason: `search_jobs` returns one-line
summaries and only `get_job` returns a full description, so browsing a hundred
results costs a fraction of the context that reading one posting does.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from . import config
from .db import STATUSES, Store
from .models import Job
from .scoring import triage
from .sources import ATS, discover, smartrecruiters
from .sources.base import make_client
from .sync import run_sync

mcp = MCPServer(
    "jobhunt",
    instructions=__doc__,
    version="0.1.0",
)

_cfg = config.load()
_store: Store | None = None


def store() -> Store:
    """Open the SQLite store lazily, so importing the module stays cheap."""
    global _store
    if _store is None:
        _store = Store(_cfg.db_path)
    return _store


# Shared by every read-only tool: they neither mutate local state nor reach
# the network, so a client can call them freely without confirmation.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# Synced sources store ISO timestamps; manual postings may hold free text.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _fmt_job_line(row: Any) -> str:
    bits = [f"[{row['id']}] {row['company']} - {row['title']}"]
    where = row["location"] or ("Remote" if row["remote"] else "")
    if where:
        bits.append(f"({where})")
    if row["remote"]:
        bits.append("[remote]")
    if row["compensation"]:
        bits.append(f"[{row['compensation']}]")
    if _ISO_DATE.match(row["posted_at"] or ""):
        bits.append(f"[posted {row['posted_at'][:10]}]")
    if row["score"] is not None:
        bits.append(f"[fit {row['score']}]")
    if row["status"]:
        bits.append(f"[{row['status']}]")
    return " ".join(bits)


def _days_ago(days: int) -> str:
    """ISO cutoff for an "in the last N days" param. 0 or less means no filter."""
    if days <= 0:
        return ""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _fmt_preferences(prefs: dict[str, Any]) -> str:
    """Render `Config.preferences` for `get_profile`, dropping unset fields."""
    lines = [f"{key}: {value}" for key, value in prefs.items() if value not in ("", [], None)]
    return "\n".join(lines) if lines else "(none set, edit profile/targets.yaml to add some)"


def _resolve_remote_only(remote_only: str) -> bool:
    """Resolve the "auto"/"true"/"false" tri-state against the stored profile.

    Plain `bool` params default to False, which is easy for a caller to leave
    untouched even when profile/targets.yaml says remote is required -- the
    list then quietly includes onsite roles. "auto" (the default) closes that
    gap by reading the preference itself instead of relying on the caller to
    remember it.
    """
    if remote_only == "true":
        return True
    if remote_only == "false":
        return False
    return str(_cfg.preferences.get("remote", "")).strip().lower() == "required"


# --------------------------------------------------------------------- syncing


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def sync_boards(sources: str = "greenhouse,ashby,lever,smartrecruiters") -> str:
    """Fetch the latest postings from the configured job boards into local storage.

    Run this first in a session, or whenever results look stale. Takes roughly a
    second per company board, so a full sync of ~90 boards takes a couple of
    minutes.

    Args:
        sources: Comma-separated. ATS boards scoped to the target company list:
            greenhouse, ashby, lever, smartrecruiters. Keyword-scoped
            aggregators covering the wider market: himalayas (remote roles),
            hn (Who-is-hiring thread), remoteok (remote roles).
    """
    wanted = [s.strip() for s in sources.split(",") if s.strip()]
    report = await run_sync(_cfg, store(), wanted)
    out = json.dumps(report.as_dict(), indent=2)
    if report.new:
        out += "\n\nTo list the new postings: search_jobs(new_within_days=1)."
    return out


# -------------------------------------------------------------------- browsing


@mcp.tool(annotations=READ_ONLY)
def search_jobs(
    query: str = "",
    company: str = "",
    source: str = "",
    remote_only: str = "auto",
    min_fit: int = -1,
    unscored_only: bool = False,
    exclude_applied: bool = False,
    new_within_days: int = 0,
    posted_within_days: int = 0,
    limit: int = 40,
) -> str:
    """Search stored postings. Returns one line per job, not full descriptions.

    Use this to browse and narrow down. Use get_job to actually read one.
    For "what's new since the last sync", pass new_within_days=1 (or however
    many days since the user last synced).

    Args:
        query: Free text matched against title, description, and department.
        company: Filter to one company (substring match).
        source: One of greenhouse, ashby, lever, smartrecruiters, himalayas, hn,
            remoteok, manual.
        remote_only: "auto" (default) restricts to remote postings when
            profile/targets.yaml's preferences.remote is "required", otherwise
            includes everything. Pass "true"/"false" to override.
        min_fit: Only postings you already scored at or above this (0-100).
        unscored_only: Only postings with no recorded fit assessment yet.
        exclude_applied: Hide anything already applied to or further along.
        new_within_days: Only postings this tool first saw in the last N days
            (i.e. new to you). 0 (default) means no filter.
        posted_within_days: Only postings the employer posted in the last N
            days (i.e. fresh on the market). 0 (default) means no filter.
        limit: Max results (default 40).
    """
    rows = store().search(
        query=query,
        company=company,
        source=source,
        remote_only=_resolve_remote_only(remote_only),
        min_score=min_fit if min_fit >= 0 else None,
        unscored_only=unscored_only,
        exclude_applied=exclude_applied,
        new_since=_days_ago(new_within_days),
        posted_since=_days_ago(posted_within_days),
        limit=limit,
        # The one-line summary never reads the description. Skip pulling it
        # out of SQLite so browsing a wide result set stays cheap.
        description_limit=0,
    )
    if not rows:
        return "No matching postings. Try a broader query, or run sync_boards first."
    lines = [_fmt_job_line(r) for r in rows]
    return f"{len(rows)} posting(s):\n" + "\n".join(lines)


async def _description(row: Any) -> str:
    """The stored description, fetching and caching it first for list-only sources.

    SmartRecruiters' list feed has no description text (see that adapter), so
    it's fetched the first time someone actually reads the posting. A failed
    fetch returns a note rather than raising: the metadata is still useful.
    """
    if row["description"] or row["source"] != "smartrecruiters":
        return row["description"] or ""
    try:
        async with make_client() as client:
            text = await smartrecruiters.fetch_description(client, row["source_id"])
    except Exception as exc:  # noqa: BLE001
        return f"(couldn't fetch the description: {type(exc).__name__}. Open the url instead.)"
    if text:
        store().set_description(row["id"], text)
    return text


# Reads local state, but can reach the network once per posting to fill in a
# description a list-only source didn't include.
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def get_job(job_id: str, full_description: bool = True) -> str:
    """Read one posting in full, including its description and any saved assessment.

    Args:
        job_id: The bracketed id from search results.
        full_description: Set False for metadata only, to save context.
    """
    row = store().get_job(job_id)
    if row is None:
        return f"No posting with id {job_id!r}."

    out = [
        f"{row['company']} - {row['title']}",
        f"id:          {row['id']}",
        f"url:         {row['url']}",
        f"location:    {row['location']}{' (remote)' if row['remote'] else ''}",
        f"department:  {row['department']}",
        f"source:      {row['source']}",
        f"posted:      {row['posted_at']}",
        f"first seen:  {row['first_seen']}",
        f"active:      {'yes' if row['active'] else 'no (posting disappeared from the board)'}",
    ]
    if row["compensation"]:
        out.append(f"comp:        {row['compensation']}")
    if row["score"] is not None:
        out.append(f"\nrecorded fit: {row['score']}/100 - {row['verdict']}")
        for label, key in (("reasons", "reasons"), ("concerns", "concerns")):
            try:
                items = json.loads(row[key] or "[]")
            except json.JSONDecodeError:
                items = []
            for item in items:
                out.append(f"  {label[:-1]}: {item}")
    if row["status"]:
        out.append(f"application status: {row['status']}")
        if row["notes"]:
            out.append(f"notes: {row['notes']}")

    events = store().events(job_id)
    if events:
        out.append("\nhistory:")
        out += [f"  {e['at'][:10]} {e['kind']}: {e['detail']}" for e in events]

    # Surfaced only if already cached, never triggers research here. Doing
    # that on every get_job would mean a web search per posting. Company
    # research is on-demand, via get_company_notes / add_company_notes,
    # not automatic.
    company_notes = store().get_company_notes(row["company"])
    if company_notes:
        out.append(f"\n--- company notes (as of {company_notes['updated_at'][:10]}) ---")
        out.append(company_notes["notes"])

    description = await _description(row) if full_description else ""
    if description:
        out.append("\n--- description ---")
        out.append(description[:12000])

    return "\n".join(out)


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def add_manual_posting(
    company: str,
    title: str,
    url: str,
    location: str = "",
    remote: bool = False,
    description: str = "",
    compensation: str = "",
    department: str = "",
    posted_at: str = "",
) -> str:
    """Add a posting from outside the synced boards (a Workday link, a company's
    own careers page, anything not on a synced board).

    Before adding one by hand, try find_company_board: if the company has a
    board that can be synced, add_target it instead and later postings arrive
    on their own.

    Fetch and read the posting yourself first (e.g. with a web-fetch tool), then
    pass what you found here. This does not fetch the URL itself. Once added, the
    posting behaves like any synced one: get_job, record_fit, set_status, and
    add_note all work on it, and it shows up in search_jobs with source "manual".

    Re-adding the same url updates that same posting rather than creating a
    duplicate, so re-running this after the listing changes is safe.

    Args:
        company: Company name.
        title: Job title.
        url: The posting's URL. Identifies the posting -- re-adding the same
            url updates it in place.
        location: Free-text location, e.g. "United States, Remote".
        remote: Whether the posting is remote.
        description: The job description text, as much as you extracted.
        compensation: Salary/comp range if listed, as free text.
        department: Team or department, if stated.
        posted_at: Posting date if stated, as free text.
    """
    if not company.strip() or not title.strip() or not url.strip():
        return "company, title, and url are required."
    job = Job(
        source="manual",
        source_id=url.strip(),
        company=company.strip(),
        title=title.strip(),
        url=url.strip(),
        location=location,
        remote=remote,
        department=department,
        description=description,
        compensation=compensation,
        posted_at=posted_at,
    )
    store().upsert_jobs([job])
    return (
        f"Added [{job.id}] {job.company} - {job.title}. "
        "Read it back with get_job, then score with record_fit."
    )


@mcp.tool(annotations=READ_ONLY)
def shortlist_for_review(
    limit: int = 25,
    remote_only: str = "auto",
    source: str = "",
    posted_within_days: int = 0,
) -> str:
    """Pick the unscored postings most worth reading, so you can assess them.

    This is the intended entry point for triage. It ranks every unscored posting
    with a cheap keyword heuristic, which only judges whether a role is in the
    right family, never whether it is actually a good fit, and returns the top
    slice for you to read properly with get_job and then score with record_fit.

    Args:
        limit: How many candidates to return (default 25).
        remote_only: "auto" (default) restricts to remote postings when
            profile/targets.yaml's preferences.remote is "required", otherwise
            includes everything. Pass "true"/"false" to override.
        source: Restrict to one source.
        posted_within_days: Only postings the employer posted in the last N
            days, to skip roles that have sat open for months. 0 (default)
            means no filter.
    """
    rows = store().search(
        unscored_only=True,
        remote_only=_resolve_remote_only(remote_only),
        source=source,
        posted_since=_days_ago(posted_within_days),
        limit=0,
        # relevance() only ever looks at the first 4000 chars. Fetching more
        # than that for the whole unscored corpus is pure waste.
        description_limit=4000,
    )
    if not rows:
        return "Nothing unscored. Run sync_boards, or widen the filters."

    ranked = triage(rows, _cfg.keywords)
    if not ranked:
        return f"Screened {len(rows)} unscored postings, none looked relevant."

    lines = [
        f"Screened {len(rows)} unscored postings, {len(ranked)} plausible. "
        f"Top {min(limit, len(ranked))} to review. Each 'relevance=' number "
        "is a keyword prefilter score, not a fit judgment -- read every "
        "posting with get_job and score it yourself with record_fit:",
    ]
    for rel, row in ranked[:limit]:
        line = f"  relevance={rel.score:<3d} {_fmt_job_line(row)}"
        if rel.flags:
            line += f"  !{', '.join(rel.flags)}"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
def get_profile() -> str:
    """Return the resume and search criteria, as context for judging fit.

    Call this before scoring postings so assessments reflect actual background
    rather than a guess at it.
    """
    resume = _cfg.resume_text()
    if not resume:
        return (
            f"No resume found at {_cfg.resume_path}.\n"
            "Create it (markdown) so fit assessments have something to work from."
        )
    return (
        f"--- resume ---\n{resume}\n\n"
        f"--- target keywords ---\n{', '.join(_cfg.keywords)}\n\n"
        f"--- de-prioritize ---\n{', '.join(_cfg.exclude_keywords)}\n\n"
        f"--- preferences ---\n{_fmt_preferences(_cfg.preferences)}"
    )


# ---------------------------------------------------------- company research


@mcp.tool(annotations=READ_ONLY)
def get_company_notes(company: str) -> str:
    """Return cached research on a company (WLB, culture, funding, layoffs, ...).

    This server has no API for review-site data. There isn't a free,
    unauthenticated one, and scraping Glassdoor/Comparably is against their
    terms the same way LinkedIn/Indeed are excluded elsewhere in this tool.
    Instead, use your own web search to research the company, then save what
    you find with add_company_notes so future postings from it reuse it
    instead of re-researching every time.
    """
    row = store().get_company_notes(company)
    if row is None:
        return (
            f"No cached notes on {company!r}. Research it (recent reviews, "
            "layoffs, funding, WLB reputation) and save findings with "
            "add_company_notes."
        )
    return f"{company} (as of {row['updated_at'][:10]}):\n{row['notes']}"


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def add_company_notes(company: str, notes: str) -> str:
    """Save (or replace) research findings about a company.

    This replaces any existing notes for the company rather than appending.
    It's meant to hold the current state of what's known, not a log. Include
    a source and rough date in `notes` if you have one, so staleness is
    judgeable later.

    Args:
        company: Company name, matched case-insensitively against postings.
        notes: What you found (WLB/culture signal, funding, layoffs, growth).
    """
    store().set_company_notes(company, notes)
    return f"Saved notes for {company}."


# ------------------------------------------------------------------- recording


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def record_fit(
    job_id: str,
    score: int,
    verdict: str = "",
    reasons: str = "",
    concerns: str = "",
) -> str:
    """Save your assessment of a posting so it persists across sessions.

    Score after reading the posting with get_job and the background from
    get_profile, not from the title alone.

    Args:
        job_id: The posting's id.
        score: 0-100. Roughly: 80+ apply now, 60-79 worth a look,
            below 40 not a fit.
        verdict: One-line summary of the call.
        reasons: Semicolon-separated points in favor.
        concerns: Semicolon-separated reservations or gaps.
    """
    if not 0 <= score <= 100:
        return "score must be between 0 and 100."
    if not store().exists(job_id):
        return f"No posting with id {job_id!r}."
    split = lambda s: [p.strip() for p in s.split(";") if p.strip()]  # noqa: E731
    store().set_fit(job_id, score, verdict, split(reasons), split(concerns))
    return f"Recorded fit {score}/100 for {job_id}."


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    )
)
def set_status(job_id: str, status: str, notes: str = "") -> str:
    """Move a posting along the application pipeline and log the change.

    Args:
        job_id: The posting's id.
        status: One of interested, applied, screen, interview, onsite, offer,
            rejected, withdrawn, ghosted.
        notes: Optional context (recruiter name, next step, take-home details).
    """
    if not store().exists(job_id):
        return f"No posting with id {job_id!r}."
    try:
        store().set_status(job_id, status, notes)
    except ValueError as exc:
        return str(exc)
    return f"{job_id} -> {status}."


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    )
)
def add_note(job_id: str, note: str) -> str:
    """Append a timestamped note to a posting's history.

    Args:
        job_id: The posting's id.
        note: What happened (interview feedback, a contact, a follow-up date).
    """
    if not store().exists(job_id):
        return f"No posting with id {job_id!r}."
    store().add_note(job_id, note)
    return f"Note added to {job_id}."


@mcp.tool(annotations=READ_ONLY)
def list_applications(status: str = "") -> str:
    """Show the application pipeline, optionally filtered to one status.

    Args:
        status: One of interested, applied, screen, interview, onsite, offer,
            rejected, withdrawn, ghosted. Omit for everything.
    """
    if status and status not in STATUSES:
        return f"unknown status {status!r}, expected one of {', '.join(STATUSES)}"
    rows = store().pipeline(status)
    if not rows:
        return "Nothing tracked yet. Use set_status to start tracking a posting."
    lines = []
    for row in rows:
        line = f"[{row['id']}] {row['status']:<11} {row['company']} - {row['title']}"
        if row["applied_at"]:
            line += f"  (applied {row['applied_at'][:10]})"
        if row["notes"]:
            line += f"\n      note: {row['notes']}"
        lines.append(line)
    return f"{len(rows)} tracked:\n" + "\n".join(lines)


# --------------------------------------------------------------------- targets


def _watched(source: str, slug: str) -> str:
    """Display name if (source, slug) is already on the target list, else ""."""
    return _cfg.boards(source).get(slug, "")


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def find_company_board(company: str = "", url: str = "") -> str:
    """Find which ATS board (greenhouse, ashby, lever, smartrecruiters) a company uses.

    Use this before add_target, and whenever a posting came from somewhere
    that can't be synced (builtin.com, a careers page): if the company has a
    board here, adding it means future postings sync automatically instead of
    being entered by hand.

    Pass a url if you have one. A link to the posting on the ATS itself
    (job-boards.greenhouse.io/..., jobs.ashbyhq.com/..., jobs.lever.co/...,
    jobs.smartrecruiters.com/...) is exact. Otherwise it guesses slugs from
    the company name, which can hit a different company with the same name:
    check the sample titles look right before calling add_target.

    Args:
        company: Company name, e.g. "Monte Carlo".
        url: Optional. Any link that may point at the company's ATS board.
    """
    if not company.strip() and not url.strip():
        return "Pass a company name, a url, or both."
    async with make_client() as client:
        result = await discover.find_boards(client, company, url)
    if not result.tried:
        return "That url doesn't link to a supported ATS. Pass the company name too."

    lines = []
    for m in result.matches:
        line = f"  {m.source}:{m.slug} - {m.job_count} open posting(s)"
        if m.sample_titles:
            line += ", e.g. " + "; ".join(m.sample_titles)
        if watched := _watched(m.source, m.slug):
            line += f"  (already watched as {watched!r})"
        lines.append(line)
    if lines:
        head = f"Found {len(lines)} board(s) for {company or url!r}:"
        tail = (
            "If the sample titles fit this company and it isn't already watched, "
            "call add_target(source, slug, display_name)."
        )
        return "\n".join([head, *lines, tail])

    out = [f"No board found. Tried: {', '.join(result.tried)}."]
    if result.errors:
        errs = ", ".join(f"{k} ({v})" for k, v in result.errors.items())
        out.append(f"Could not check: {errs}. Retry later before concluding anything.")
    out.append(
        "If you can open the company's careers page, look for a link to "
        "job-boards.greenhouse.io, jobs.ashbyhq.com, jobs.lever.co, or "
        "jobs.smartrecruiters.com and pass "
        "it as url. If it uses another ATS (e.g. Workday), it can't be synced: "
        "add its postings with add_manual_posting instead."
    )
    return "\n".join(out)


@mcp.tool(annotations=READ_ONLY)
def list_targets() -> str:
    """List the company boards currently being watched, grouped by source."""
    out = []
    for source in ATS:
        boards = _cfg.boards(source)
        if boards:
            names = ", ".join(sorted(boards.values()))
            out.append(f"{source} ({len(boards)}): {names}")
    return "\n\n".join(out) or "No target companies configured."


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    )
)
async def add_target(source: str, slug: str, display_name: str = "") -> str:
    """Add a company board to the watch list, after checking it exists.

    Use find_company_board first if you don't already know the exact source
    and slug. The slug is the path segment from the company's board URL, for
    example jobs.ashbyhq.com/acme -> slug "acme" on source "ashby".

    Args:
        source: greenhouse, ashby, lever, or smartrecruiters.
        slug: The company's slug on that ATS.
        display_name: Human-readable name, e.g. "Monte Carlo". Defaults to the slug.
    """
    if source not in ATS:
        return f"source must be one of: {', '.join(ATS)}"
    slug = slug.strip()
    if watched := _watched(source, slug):
        return f"{source}:{slug} is already watched as {watched!r}."
    async with make_client() as client:
        try:
            match = await discover.probe(client, source, slug)
        except Exception as exc:  # noqa: BLE001
            return f"Couldn't reach {source} to verify {slug!r} ({type(exc).__name__}). Not added."
    if match is None:
        return (
            f"No {source} board with slug {slug!r}. Not added. "
            "Use find_company_board to look up the right one."
        )
    _cfg.targets["companies"].setdefault(source, {})[slug] = display_name or slug
    _cfg.save_targets()
    return (
        f"Added {display_name or slug} ({source}:{slug}, {match.job_count} open posting(s)). "
        "Run sync_boards to pull them in."
    )


@mcp.tool(annotations=READ_ONLY)
def stats() -> str:
    """Summarize what is in local storage and where applications stand."""
    return json.dumps(store().stats(), indent=2)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
