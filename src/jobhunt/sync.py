"""Orchestrates fetching every configured source into the local store."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Config
from .db import Store
from .sources import ATS, himalayas, hn, remoteok
from .sources.base import make_client

# Aggregator postings unseen for this long are retired. Generous on purpose:
# RemoteOK's window is only its newest ~100 postings, and the HN thread is
# replaced monthly, so a posting drops out of the feed long before it closes.
FEED_RETIRE_DAYS = 30


@dataclass
class SyncReport:
    new: int = 0
    seen: int = 0
    retired: int = 0
    per_source: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "new_postings": self.new,
            "postings_seen": self.seen,
            "retired": self.retired,
            "per_source": self.per_source,
            "errors": self.errors,
        }


async def run_sync(
    cfg: Config,
    store: Store,
    sources: list[str] | None = None,
    delay: float = 1.0,
) -> SyncReport:
    """Fetch the requested sources and write them to the store.

    Defaults to the ATS boards only. The aggregators are opt-in because they
    return the whole market rather than a curated company list, which is useful
    for breadth but noisy as a default.
    """
    sources = sources or list(ATS)
    report = SyncReport()

    async with make_client() as client:
        for name in sources:
            if name in ATS:
                boards = cfg.boards(name)
                if not boards:
                    continue
                result = await ATS[name].sync(client, boards, delay=delay)
            elif name == "himalayas":
                result = await himalayas.sync(client, cfg.keywords, delay=delay)
            elif name == "hn":
                result = await hn.sync(client, cfg.keywords)
            elif name == "remoteok":
                result = await remoteok.sync(client, cfg.keywords)
            else:
                report.errors[name] = "unknown source"
                continue

            new, seen = store.upsert_jobs(result.jobs)

            retired = 0
            if name in ATS and result.fetched:
                # Only retire boards that were actually reached this run.
                retired = store.deactivate_missing(
                    name, result.fetched, {j.id for j in result.jobs}
                )
            elif result.fetched:
                # Feeds are a rolling window, so absence from one fetch means
                # nothing. Retire only what hasn't shown up in a while.
                cutoff = datetime.now(UTC) - timedelta(days=FEED_RETIRE_DAYS)
                retired = store.retire_unseen(name, cutoff.isoformat())

            report.new += new
            report.seen += seen
            report.retired += retired
            report.per_source[name] = {
                "new": new,
                "seen": seen,
                "retired": retired,
                "boards_ok": len(result.fetched),
                "boards_failed": len(result.errors),
            }
            for board, err in result.errors.items():
                report.errors[f"{name}:{board}"] = err

    return report
