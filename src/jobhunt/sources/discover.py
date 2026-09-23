"""Find which ATS board a company posts on, and verify it against the live API.

Two ways in, both ending in the same live probe:

- A URL. Careers pages and job-board aggregators (builtin.com, YC's job
  board, ...) usually link through to the underlying ATS posting, and the
  board slug can be read straight out of that link.
- A company name. Slugs are usually the name lowercased with spaces removed
  or hyphenated, so a handful of variants are tried against each ATS.

Guessing can hit a *different* company that happens to share the slug
("arena" is not a unique name), so a match is returned with its job count and
sample titles as evidence, and the caller decides whether it's the right one.
Deciding that from name similarity in Python would be exactly the brittle
string logic AGENTS.md keeps out of this codebase.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field

import httpx

from . import ATS

# One pattern per ATS, matched anywhere in a URL or pasted page text.
URL_PATTERNS: dict[str, re.Pattern[str]] = {
    "greenhouse": re.compile(
        r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)"
        r"|boards-api\.greenhouse\.io/v1/boards/([\w-]+)",
        re.I,
    ),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)", re.I),
    "lever": re.compile(r"jobs\.lever\.co/([\w.-]+)", re.I),
    "smartrecruiters": re.compile(
        r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)"
        r"|api\.smartrecruiters\.com/v1/companies/([\w-]+)",
        re.I,
    ),
}

# Path segments the patterns above can capture that are never board slugs.
_NOT_SLUGS = {"embed", "api", "v1", "jobs", "job_board"}

# Suffixes dropped when guessing slugs. "ai" stays in the joined variant too,
# since boards like "togetherai" keep it.
_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company",
             "technologies", "technology", "labs", "hq", "group", "ai"}

MAX_GUESSES = 4
SAMPLE_TITLES = 3


@dataclass
class BoardMatch:
    source: str
    slug: str
    job_count: int
    sample_titles: list[str] = field(default_factory=list)


@dataclass
class DiscoveryResult:
    matches: list[BoardMatch] = field(default_factory=list)
    tried: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def slugs_from_text(text: str) -> list[tuple[str, str]]:
    """Every (source, slug) an ATS link in ``text`` points at, in order, deduped."""
    found: list[tuple[str, str]] = []
    for source, pattern in URL_PATTERNS.items():
        for match in pattern.finditer(text):
            slug = next((g for g in match.groups() if g), "")
            if slug and slug.lower() not in _NOT_SLUGS and (source, slug) not in found:
                found.append((source, slug))
    return found


def guess_slugs(company: str) -> list[str]:
    """Likely slugs for a company name, most likely first."""
    words = re.findall(r"[a-z0-9]+", company.lower())
    core = [w for w in words if w not in _SUFFIXES] or words
    guesses = [
        "".join(words),
        "-".join(words),
        "".join(core),
        "-".join(core),
        core[0] if core else "",
    ]
    seen: list[str] = []
    for guess in guesses:
        if guess and guess not in seen:
            seen.append(guess)
    return seen[:MAX_GUESSES]


async def probe(client: httpx.AsyncClient, source: str, slug: str) -> BoardMatch | None:
    """The board at (source, slug) if it exists, None if the ATS says it doesn't.

    Anything other than a clean "no such board" (a timeout, a 5xx) is raised,
    so a flaky endpoint isn't mistaken for a wrong slug.
    """
    module = ATS[source]
    try:
        # An adapter whose full fetch is paginated offers a one-request probe.
        if hasattr(module, "probe_board"):
            count, titles = await module.probe_board(client, slug)
        else:
            jobs = await module.fetch_board(client, slug)
            count, titles = len(jobs), [j.title for j in jobs]
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    except json.JSONDecodeError:
        raise  # a garbled response says nothing about whether the board exists
    except ValueError:
        return None  # an adapter's "no such board" signal (Lever, SmartRecruiters)
    return BoardMatch(
        source=source, slug=slug, job_count=count, sample_titles=titles[:SAMPLE_TITLES]
    )


async def find_boards(
    client: httpx.AsyncClient, company: str = "", url: str = "", delay: float = 0.5
) -> DiscoveryResult:
    """Probe the slugs a URL points at, or else guesses from the company name.

    Serial with a short delay, like every other fetch here. A name search is
    at most MAX_GUESSES x len(ATS) requests.
    """
    result = DiscoveryResult()
    candidates = slugs_from_text(url) if url else []
    if not candidates:
        candidates = [(source, slug) for slug in guess_slugs(company) for source in ATS]

    for source, slug in candidates:
        key = f"{source}:{slug}"
        result.tried.append(key)
        try:
            match = await probe(client, source, slug)
        except Exception as exc:  # noqa: BLE001 - report it, keep probing the rest
            result.errors[key] = (
                f"HTTP {exc.response.status_code}"
                if isinstance(exc, httpx.HTTPStatusError)
                else type(exc).__name__
            )
            match = None
        if match:
            result.matches.append(match)
        await asyncio.sleep(delay)
    return result
