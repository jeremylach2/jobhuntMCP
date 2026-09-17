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
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from . import config
from .db import STATUSES, Store
from .models import Job
from .scoring import triage
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


def _fmt_job_line(row: Any) -> str:
    bits = [f"[{row['id']}] {row['company']} - {row['title']}"]
    where = row["location"] or ("Remote" if row["remote"] else "")
    if where:
        bits.append(f"({where})")
    if row["remote"]:
        bits.append("[remote]")
    if row["compensation"]:
        bits.append(f"[{row['compensation']}]")
    if row["score"] is not None:
        bits.append(f"[fit {row['score']}]")
    if row["status"]:
        bits.append(f"[{row['status']}]")
    return " ".join(bits)


def _fmt_preferences(prefs: dict[str, Any]) -> str:
    """Render `Config.preferences` for `get_profile`, dropping unset fields."""
    lines = [f"{key}: {value}" for key, value in prefs.items() if value not in ("", [], None)]
    return "\n".join(lines) if lines else "(none set, edit profile/targets.yaml to add some)"


# --------------------------------------------------------------------- syncing


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
)
async def sync_boards(sources: str = "greenhouse,ashby,lever") -> str:
    """Fetch the latest postings from the configured job boards into local storage.

    Run this first in a session, or whenever results look stale. Takes roughly a
    second per company board, so a full sync of ~90 boards takes a couple of
    minutes.

    Args:
        sources: Comma-separated. ATS boards scoped to the target company list:
            greenhouse, ashby, lever. Keyword-scoped aggregators covering the
            wider market: himalayas (remote roles), hn (Who-is-hiring thread),
            remoteok (remote roles).
    """
    wanted = [s.strip() for s in sources.split(",") if s.strip()]
    report = await run_sync(_cfg, store(), wanted)
    return json.dumps(report.as_dict(), indent=2)


# -------------------------------------------------------------------- browsing


@mcp.tool(annotations=READ_ONLY)
def search_jobs(
    query: str = "",
    company: str = "",
    source: str = "",
    remote_only: bool = False,
    min_fit: int = -1,
    unscored_only: bool = False,
    exclude_applied: bool = False,
    limit: int = 40,
) -> str:
    """Search stored postings. Returns one line per job, not full descriptions.

    Use this to browse and narrow down. Use get_job to actually read one.

    Args:
        query: Free text matched against title, description, and department.
        company: Filter to one company (substring match).
        source: One of greenhouse, ashby, lever, himalayas, hn, remoteok, manual.
        remote_only: Only postings flagged remote.
        min_fit: Only postings you already scored at or above this (0-100).
        unscored_only: Only postings with no recorded fit assessment yet.
        exclude_applied: Hide anything already applied to or further along.
        limit: Max results (default 40).
    """
    rows = store().search(
        query=query,
        company=company,
        source=source,
        remote_only=remote_only,
        min_score=min_fit if min_fit >= 0 else None,
        unscored_only=unscored_only,
        exclude_applied=exclude_applied,
        limit=limit,
        # The one-line summary never reads the description. Skip pulling it
        # out of SQLite so browsing a wide result set stays cheap.
        description_limit=0,
    )
    if not rows:
        return "No matching postings. Try a broader query, or run sync_boards first."
    lines = [_fmt_job_line(r) for r in rows]
    return f"{len(rows)} posting(s):\n" + "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
def get_job(job_id: str, full_description: bool = True) -> str:
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

    if full_description and row["description"]:
        out.append("\n--- description ---")
        out.append(row["description"][:12000])

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
    own careers page, anything not on greenhouse/ashby/lever/himalayas/hn/remoteok).

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
    limit: int = 25, remote_only: bool = False, source: str = ""
) -> str:
    """Pick the unscored postings most worth reading, so you can assess them.

    This is the intended entry point for triage. It ranks every unscored posting
    with a cheap keyword heuristic, which only judges whether a role is in the
    right family, never whether it is actually a good fit, and returns the top
    slice for you to read properly with get_job and then score with record_fit.

    Args:
        limit: How many candidates to return (default 25).
        remote_only: Restrict to remote postings.
        source: Restrict to one source.
    """
    rows = store().search(
        unscored_only=True,
        remote_only=remote_only,
        source=source,
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
        f"Top {min(limit, len(ranked))} to review "
        f"(relevance is a keyword prefilter, not a fit judgment, "
        f"read them with get_job before scoring):",
    ]
    for rel, row in ranked[:limit]:
        line = f"  {rel.score:3d}  {_fmt_job_line(row)}"
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


@mcp.tool(annotations=READ_ONLY)
def list_targets() -> str:
    """List the company boards currently being watched, grouped by source."""
    out = []
    for source in ("greenhouse", "ashby", "lever"):
        boards = _cfg.boards(source)
        if boards:
            names = ", ".join(sorted(boards.values()))
            out.append(f"{source} ({len(boards)}): {names}")
    return "\n\n".join(out) or "No target companies configured."


@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
)
def add_target(source: str, slug: str, display_name: str = "") -> str:
    """Add a company board to the watch list. The slug is not checked here.

    The slug is the path segment from the company's careers URL, for example
    jobs.ashbyhq.com/acme -> slug "acme" on source "ashby". Run sync_boards
    afterward to actually verify it. A wrong slug shows up in that report's
    errors rather than failing this call.

    Args:
        source: greenhouse, ashby, or lever.
        slug: The company's slug on that ATS.
        display_name: Human-readable name. Defaults to the slug.
    """
    if source not in ("greenhouse", "ashby", "lever"):
        return "source must be one of: greenhouse, ashby, lever"
    _cfg.targets["companies"].setdefault(source, {})[slug] = display_name or slug
    _cfg.save_targets()
    return (
        f"Added {display_name or slug} ({source}:{slug}). "
        f"Run sync_boards to pull it in. If the slug is wrong, "
        f"the sync report will list it under errors."
    )


@mcp.tool(annotations=READ_ONLY)
def stats() -> str:
    """Summarize what is in local storage and where applications stand."""
    return json.dumps(store().stats(), indent=2)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
