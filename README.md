# jobhuntMCP

[![tests](https://github.com/jeremylach2/jobhuntMCP/actions/workflows/tests.yml/badge.svg)](https://github.com/jeremylach2/jobhuntMCP/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An MCP server (and CLI) that turns the job search into something you can drive
from a conversation: it pulls postings from whatever company ATS boards you
watch, narrows thousands of them to a readable shortlist, and tracks what you
thought of each one and where the application stands.

```
> sync the boards and show me what's new in AI infrastructure

  Synced 91 boards, 412 new postings.
  Screened 8,930 unscored, 214 plausible. Top candidates:

   79  [a3f9c21d] Northwind Labs - Software Engineer, Inference Platform (NYC) [remote]
   74  [7b2e0a44] Clearwater Systems - Backend Engineer, Core ($180K – $240K)
   71  [c0d81e93] Fathom Compute - Software Engineer, Model Performance [remote]
```

(Companies above are fictional placeholders. Your own target list starts from a
handful of examples in `profile/targets.example.yaml` and grows as you add boards.)

## Why it is built this way

The interesting constraint is that **relevance is not a string-matching
problem**. Whether a posting is worth your time depends on what the team
actually does, how the requirements line up against your background, and what
the role is really asking for underneath the title. None of that survives
being reduced to a keyword score.

So the server does not try. It splits the work:

| Layer | Does | Lives in |
|---|---|---|
| Ingestion | Fetch and normalize postings from seven sources | `sources/` |
| Prefilter | Cheap keyword triage: cut ~9,000 postings to ~200 plausible ones | `scoring.py` |
| Judgment | Read the shortlist and decide what actually fits | the model |
| Memory | Persist assessments and pipeline state across sessions | `db.py` |

`scoring.py` is explicitly a *prefilter*, not an evaluator. It only answers "is
this plausibly the right family of job", so that a model is not asked to read
eight thousand postings to find the forty that matter. The real judgment is
written back through `record_fit` and persists, so the second session knows
what the first one concluded.

Tool outputs are bounded for the same reason. `search_jobs` returns one line
per posting. Only `get_job` returns a full description. Browsing a hundred
results costs a fraction of the context that reading one posting does.

## Sources

All seven are public, unauthenticated endpoints that employers publish for
distribution. No scraping, no browser automation, no credentials.

| Source | Endpoint | Scope |
|---|---|---|
| **Greenhouse** | `boards-api.greenhouse.io` | target company list |
| **Ashby** | `api.ashbyhq.com/posting-api` | target company list, includes salary bands |
| **Lever** | `api.lever.co/v0/postings` | target company list |
| **SmartRecruiters** | `api.smartrecruiters.com/v1/companies` | target company list; descriptions fetched when a posting is first read |
| **Himalayas** | `himalayas.app/jobs/api` | whole remote market, keyword-filtered |
| **Hacker News** | `hn.algolia.com` | monthly "Who is hiring?" thread |
| **RemoteOK** | `remoteok.com/api` | whole remote market, keyword-filtered |

LinkedIn and Indeed are deliberately absent: both prohibit automated access in
their terms, and both actively block it. Workday is absent too: it has no
documented public feed. Anything from those goes in by hand.

Boards are fetched serially with a one-second delay. A personal tool has no
reason to hammer a free public endpoint.

## Setup

The easiest and quickest way to get setup is running the `setup` skill (see
`.claude/skills/setup/`).
Run it from
a Claude Code session in this repo (`/setup` or "set up jobhunt") and it will
install dependencies, interview you for a resume, help you verify and add
target companies, and register the server. To do it by hand instead:

```bash
uv venv
uv pip install -e .

# Your resume, as markdown. Gitignored, so it never enters the repo.
cp profile/resume.example.md profile/resume.md
$EDITOR profile/resume.md

# Your target companies, keywords, and preferences. Also gitignored.
cp profile/targets.example.yaml profile/targets.yaml
$EDITOR profile/targets.yaml
```

No API keys or accounts are needed: every source is a public, unauthenticated
endpoint. Everything the tool knows about you lives in `profile/`.

Register the MCP server with Claude Code:

```bash
claude mcp add jobhunt -- /absolute/path/to/.venv/Scripts/python.exe -m jobhunt.server
```

Restart Claude Code after registering (or after any change to `server.py`)
so it picks up the server. Then, in a session:

```
> sync the boards, then shortlist what's worth reading
> read the Northwind Labs one and tell me honestly whether it's a stretch
> score it and mark me as applied
> what's in my pipeline that's gone quiet for two weeks?
```

## CLI

The same data without a model in the loop, useful for cron:

```bash
jobhunt sync                              # pull all ATS boards
jobhunt sync --sources himalayas,hn,remoteok  # add the aggregators
jobhunt search "agent" --remote --limit 20
jobhunt search --new 1                    # first seen in the last day
jobhunt shortlist --posted 14             # skip roles open for months
jobhunt shortlist --limit 30              # keyword-ranked triage
jobhunt find "Northwind Labs"             # which ATS board, verified live
jobhunt show a3f9c21d
jobhunt status a3f9c21d applied --notes "referred by X"
jobhunt pipeline
jobhunt stats
```

## MCP tools

| Tool | Purpose |
|---|---|
| `sync_boards` | Fetch the latest postings into local storage |
| `search_jobs` | Browse stored postings as one-line summaries, filterable to what's new (`new_within_days`) or freshly posted (`posted_within_days`) |
| `get_job` | Read one posting in full |
| `shortlist_for_review` | Rank unscored postings by keyword relevance for triage |
| `get_profile` | Return your resume, criteria, and preferences, as context for judging fit |
| `get_company_notes` / `add_company_notes` | Read/save cached research on a company (WLB, culture, stability) |
| `record_fit` | Persist an assessment (score, verdict, reasons, concerns) |
| `set_status` | Move a posting along the application pipeline |
| `add_note` | Append a timestamped note to a posting's history |
| `add_manual_posting` | Enter a posting by hand (for sources that can't be fetched) |
| `list_applications` | Show the pipeline |
| `find_company_board` | Find and verify a company's ATS board from its name or any link to it |
| `list_targets` / `add_target` | Manage the watched company list (`add_target` checks the board exists first) |
| `stats` | Summarize storage and pipeline state |

## Configuration

`profile/targets.yaml` holds the watched boards, the keywords used to filter
the aggregator sources, and a `preferences` block (locations, remote, salary
floor, company stage) that `get_profile` surfaces to the model as judgment
context. It starts as a copy of `profile/targets.example.yaml` (see Setup
above) and is gitignored from there. Edit it by hand, the same way you'd edit
keywords. Verify each slug against the live endpoint before adding it:
`jobhunt find "Company"` guesses and probes slugs for you, or pass
`--url` with any link to one of its postings. The slug is the path segment
in the company's board URL:

```
job-boards.greenhouse.io/SLUG   -> greenhouse
jobs.ashbyhq.com/SLUG           -> ashby
jobs.lever.co/SLUG              -> lever
jobs.smartrecruiters.com/SLUG   -> smartrecruiters
```

If a company changes ATS, its board starts failing rather than silently going
quiet. `sync` reports it under `errors`.

Paths are overridable: `JOBHUNT_HOME`, `JOBHUNT_DB`, `JOBHUNT_RESUME`,
`JOBHUNT_TARGETS`.

There's no free API for company-review data (WLB, culture, stability), so
that's not fetched automatically, and never on-demand for every posting,
either. It's a cache you (or the model, when asked) fill in by hand:
`get_job` shows whatever is already cached for a posting's company, and
`add_company_notes` saves new findings so later postings from the same
company reuse them instead of re-researching.

## Scope

Search, triage, and tracking. It does not submit applications: ATS forms vary
too much for that to be reliable, and an agent should not be sending things
under your name that you have not read. You apply. It remembers.

## Tests

```bash
uv pip install -e ".[dev]"
pytest
ruff check .
mypy src/jobhunt
```

Network-dependent tests are marked `live` and skipped by default:
`pytest -m live` exercises the real endpoints. CI runs the offline suite plus
lint and type checks on every push.
