"""Shared HTTP plumbing and the source registry.

Every adapter is an async callable that takes an ``httpx.AsyncClient`` plus the
identifiers it needs, and returns normalized :class:`~jobhunt.models.Job`
objects. Adapters never raise on a single bad board: a company whose slug is
wrong should not abort a sync across forty others, so per-company failures are
collected and reported instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

USER_AGENT = "jobhunt/0.1 (personal job search tool; +https://github.com/)"

# ATS endpoints are generous but not free. One second between company fetches
# keeps a full sync polite without making it slow.
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass
class SourceResult:
    """Outcome of syncing one source, including which boards failed and why."""

    source: str
    jobs: list = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    fetched: list[str] = field(default_factory=list)

    def note_error(self, company: str, exc: Exception) -> None:
        if isinstance(exc, httpx.HTTPStatusError):
            self.errors[company] = f"HTTP {exc.response.status_code}"
        elif isinstance(exc, httpx.TimeoutException):
            self.errors[company] = "timeout"
        else:
            self.errors[company] = f"{type(exc).__name__}: {exc}"


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )
