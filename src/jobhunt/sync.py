"""Orchestrates fetching every configured source into the local store."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .db import Store
from .sources import ashby, greenhouse, himalayas, hn, lever, remoteok
from .sources.base import make_client


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
    sources = sources or ["greenhouse", "ashby", "lever"]
    report = SyncReport()

    async with make_client() as client:
        for name in sources:
            if name in ("greenhouse", "ashby", "lever"):
                boards = cfg.boards(name)
                if not boards:
                    continue
                module = {"greenhouse": greenhouse, "ashby": ashby, "lever": lever}[name]
                result = await module.sync(client, boards, delay=delay)
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
            if name in ("greenhouse", "ashby", "lever") and result.fetched:
                # Only retire boards that were actually reached this run.
                retired = store.deactivate_missing(
                    name, result.fetched, {j.id for j in result.jobs}
                )

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
