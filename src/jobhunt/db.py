"""SQLite storage for postings, fit assessments, and application tracking.

One file, no migrations framework: the schema is created on demand with
``CREATE TABLE IF NOT EXISTS``, so an existing database keeps working after an
upgrade that only adds tables or indexes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    location    TEXT DEFAULT '',
    remote      INTEGER DEFAULT 0,
    department  TEXT DEFAULT '',
    description TEXT DEFAULT '',
    compensation TEXT DEFAULT '',
    posted_at   TEXT DEFAULT '',
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    active      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS fit (
    job_id     TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    score      INTEGER NOT NULL,
    verdict    TEXT DEFAULT '',
    reasons    TEXT DEFAULT '',
    concerns   TEXT DEFAULT '',
    scored_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    job_id      TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,
    applied_at  TEXT DEFAULT '',
    updated_at  TEXT NOT NULL,
    notes       TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL,
    at         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS company_notes (
    company     TEXT PRIMARY KEY COLLATE NOCASE,
    notes       TEXT DEFAULT '',
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_active  ON jobs(active);
CREATE INDEX IF NOT EXISTS idx_events_job   ON events(job_id);
"""

# jobs columns other than `description`, which `search()` projects separately
# so callers that only need metadata (a one-line summary, a triage prefix) are
# not handed the full posting text just to discard it.
_JOB_METADATA_COLUMNS = (
    "id", "source", "source_id", "company", "title", "url", "location",
    "remote", "department", "compensation", "posted_at", "first_seen",
    "last_seen", "active",
)

# Valid application states, in rough pipeline order.
STATUSES = (
    "interested",
    "applied",
    "screen",
    "interview",
    "onsite",
    "offer",
    "rejected",
    "withdrawn",
    "ghosted",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    """SQLite-backed storage, safe to share across threads.

    The MCP server runs sync tool functions on a worker thread pool, so a
    single `Store` gets called from whichever thread the framework picks.
    A plain `sqlite3.Connection` refuses that: it is bound to the thread that
    created it and raises ProgrammingError everywhere else. Rather than share
    one connection with `check_same_thread=False` and hand-serialize every
    call site, each thread gets its own connection, created on first use.
    WAL mode lets those connections read concurrently with a writer.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Tracked only so close() can shut down connections this thread never
        # opened. The list is touched rarely, so one lock around it is enough.
        self._connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        # Create the schema once, up front: running the DDL on every new
        # connection would have each thread take the write lock just to
        # re-assert tables that already exist.
        self._connect(create_schema=True)

    def _connect(self, create_schema: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # Concurrent readers alongside a writer, instead of a locked database.
        conn.execute("PRAGMA journal_mode = WAL")
        # Wait out a competing writer (a long sync_boards) rather than failing.
        conn.execute("PRAGMA busy_timeout = 10000")
        if create_schema:
            conn.executescript(SCHEMA)
            conn.commit()
        self._local.conn = conn
        with self._lock:
            self._connections.append(conn)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
        return conn

    def close(self) -> None:
        with self._lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                # A connection owned by another thread can refuse to close
                # here. Nothing left to do about it at shutdown.
                pass
        self._local = threading.local()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- jobs

    def upsert_jobs(self, jobs: Iterable[Job]) -> tuple[int, int]:
        """Insert or refresh postings. Returns (new_count, seen_count).

        ``first_seen`` is preserved on conflict so that "what is new since
        yesterday" stays answerable across repeated syncs.
        """
        new = seen = 0
        now = _now()
        for job in jobs:
            seen += 1
            row = job.to_row()
            exists = self.conn.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            if exists is None:
                new += 1
            self.conn.execute(
                """
                INSERT INTO jobs (id, source, source_id, company, title, url, location,
                                  remote, department, description, compensation, posted_at,
                                  first_seen, last_seen, active)
                VALUES (:id, :source, :source_id, :company, :title, :url, :location,
                        :remote, :department, :description, :compensation, :posted_at, :first_seen,
                        :last_seen, 1)
                ON CONFLICT(id) DO UPDATE SET
                    title       = excluded.title,
                    url         = excluded.url,
                    location    = excluded.location,
                    remote      = excluded.remote,
                    department  = excluded.department,
                    description = excluded.description,
                    compensation = excluded.compensation,
                    last_seen   = excluded.last_seen,
                    active      = 1
                """,
                {**row, "last_seen": now},
            )
        self.conn.commit()
        return new, seen

    def deactivate_missing(self, source: str, companies: list[str], keep_ids: set[str]) -> int:
        """Retire postings that vanished from a freshly synced board.

        Scoped to the companies actually fetched, so a partial or failed sync
        never retires jobs from a board that was not reached.
        """
        if not companies:
            return 0
        placeholders = ",".join("?" * len(companies))
        rows = self.conn.execute(
            f"SELECT id FROM jobs WHERE source = ? AND active = 1 "
            f"AND company IN ({placeholders})",
            (source, *companies),
        ).fetchall()
        stale = [r["id"] for r in rows if r["id"] not in keep_ids]
        for job_id in stale:
            self.conn.execute("UPDATE jobs SET active = 0 WHERE id = ?", (job_id,))
        self.conn.commit()
        return len(stale)

    def search(
        self,
        query: str = "",
        company: str = "",
        source: str = "",
        remote_only: bool = False,
        min_score: int | None = None,
        unscored_only: bool = False,
        new_since: str = "",
        include_inactive: bool = False,
        exclude_applied: bool = False,
        limit: int = 50,
        description_limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """Search stored postings.

        ``description_limit`` caps how much of each posting's description is
        pulled out of SQLite: None returns it in full, 0 omits it, and a
        positive number truncates it. Filtering (``query``) still matches
        against the full column regardless. Only the *projected* text is
        capped. A bulk caller that only needs metadata (a one-line summary,
        a triage prefix) should pass this rather than fetching every
        posting's full text just to discard it.
        """
        desc_col = "j.description"
        args: list[Any] = []
        if description_limit is not None:
            desc_col = "substr(j.description, 1, ?) AS description"
            args.append(description_limit)
        cols = ", ".join(f"j.{c}" for c in _JOB_METADATA_COLUMNS)
        sql = [
            f"SELECT {cols}, {desc_col}, f.score, f.verdict, f.reasons, f.concerns, a.status",
            "FROM jobs j",
            "LEFT JOIN fit f ON f.job_id = j.id",
            "LEFT JOIN applications a ON a.job_id = j.id",
            "WHERE 1=1",
        ]
        if not include_inactive:
            sql.append("AND j.active = 1")
        if query:
            sql.append("AND (j.title LIKE ? OR j.description LIKE ? OR j.department LIKE ?)")
            args += [f"%{query}%"] * 3
        if company:
            sql.append("AND j.company LIKE ?")
            args.append(f"%{company}%")
        if source:
            sql.append("AND j.source = ?")
            args.append(source)
        if remote_only:
            sql.append("AND j.remote = 1")
        if min_score is not None:
            sql.append("AND f.score >= ?")
            args.append(min_score)
        if unscored_only:
            sql.append("AND f.score IS NULL")
        if new_since:
            sql.append("AND j.first_seen >= ?")
            args.append(new_since)
        if exclude_applied:
            sql.append("AND (a.status IS NULL OR a.status = 'interested')")
        sql.append("ORDER BY f.score IS NULL, f.score DESC, j.first_seen DESC")
        if limit > 0:
            # limit <= 0 means "everything", which the triage pass needs: a
            # silent cap there would hide most of the corpus from screening.
            sql.append("LIMIT ?")
            args.append(limit)
        return self.conn.execute("\n".join(sql), args).fetchall()

    def exists(self, job_id: str) -> bool:
        """Cheap existence check, for callers that don't need the full row.

        Plain lookup against `jobs` only. Unlike `get_job`, it doesn't pay
        for the `fit`/`applications` joins just to answer yes/no.
        """
        return (
            self.conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
            is not None
        )

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT j.*, f.score, f.verdict, f.reasons, f.concerns,
                      a.status, a.applied_at, a.notes
               FROM jobs j
               LEFT JOIN fit f ON f.job_id = j.id
               LEFT JOIN applications a ON a.job_id = j.id
               WHERE j.id = ?""",
            (job_id,),
        ).fetchone()

    # ----------------------------------------------------------------- fit

    def set_fit(
        self,
        job_id: str,
        score: int,
        verdict: str = "",
        reasons: list[str] | None = None,
        concerns: list[str] | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO fit (job_id, score, verdict, reasons, concerns, scored_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET
                   score = excluded.score, verdict = excluded.verdict,
                   reasons = excluded.reasons, concerns = excluded.concerns,
                   scored_at = excluded.scored_at""",
            (
                job_id,
                score,
                verdict,
                json.dumps(reasons or []),
                json.dumps(concerns or []),
                _now(),
            ),
        )
        self.conn.commit()

    # -------------------------------------------------------- applications

    def set_status(self, job_id: str, status: str, notes: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(
                f"unknown status {status!r}, expected one of {', '.join(STATUSES)}"
            )
        applied_at = _now() if status == "applied" else ""
        self.conn.execute(
            """INSERT INTO applications (job_id, status, applied_at, updated_at, notes)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET
                   status = excluded.status,
                   updated_at = excluded.updated_at,
                   notes = CASE WHEN excluded.notes != '' THEN excluded.notes
                                ELSE applications.notes END,
                   applied_at = CASE WHEN applications.applied_at != ''
                                     THEN applications.applied_at
                                     ELSE excluded.applied_at END""",
            (job_id, status, applied_at, _now(), notes),
        )
        detail = status + (f": {notes}" if notes else "")
        self.conn.execute(
            "INSERT INTO events (job_id, at, kind, detail) VALUES (?, ?, ?, ?)",
            (job_id, _now(), "status", detail),
        )
        self.conn.commit()

    def add_note(self, job_id: str, note: str) -> None:
        self.conn.execute(
            "INSERT INTO events (job_id, at, kind, detail) VALUES (?, ?, ?, ?)",
            (job_id, _now(), "note", note),
        )
        self.conn.commit()

    def events(self, job_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE job_id = ? ORDER BY at", (job_id,)
        ).fetchall()

    def pipeline(self, status: str = "") -> list[sqlite3.Row]:
        sql = """SELECT j.company, j.title, j.url, j.id, a.status, a.applied_at,
                        a.updated_at, a.notes, f.score
                 FROM applications a
                 JOIN jobs j ON j.id = a.job_id
                 LEFT JOIN fit f ON f.job_id = j.id"""
        args: list[Any] = []
        if status:
            sql += " WHERE a.status = ?"
            args.append(status)
        sql += " ORDER BY a.updated_at DESC"
        return self.conn.execute(sql, args).fetchall()

    # --------------------------------------------------------- company notes

    def get_company_notes(self, company: str) -> sqlite3.Row | None:
        """Cached research for one company (WLB, culture, funding, layoffs, ...).

        Lookup is case-insensitive (the column is COLLATE NOCASE) since the
        same company name can arrive with different casing from `targets.yaml`
        display names versus how a model happens to type it.
        """
        return self.conn.execute(
            "SELECT * FROM company_notes WHERE company = ?", (company,)
        ).fetchone()

    def set_company_notes(self, company: str, notes: str) -> None:
        """Replace the cached notes for a company.

        A full overwrite, not an append. This holds the current state of
        what's known about the company, not a log of research sessions. The
        caller re-researches and calls this again when `updated_at` looks stale.
        """
        self.conn.execute(
            """INSERT INTO company_notes (company, notes, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(company) DO UPDATE SET
                   notes = excluded.notes, updated_at = excluded.updated_at""",
            (company, notes, _now()),
        )
        self.conn.commit()

    def stats(self) -> dict[str, Any]:
        def one(q: str) -> int:
            return self.conn.execute(q).fetchone()[0]

        by_status = {
            r["status"]: r["n"]
            for r in self.conn.execute(
                "SELECT status, COUNT(*) n FROM applications GROUP BY status"
            )
        }
        by_source = {
            r["source"]: r["n"]
            for r in self.conn.execute(
                "SELECT source, COUNT(*) n FROM jobs WHERE active = 1 GROUP BY source"
            )
        }
        return {
            "active_jobs": one("SELECT COUNT(*) FROM jobs WHERE active = 1"),
            "total_jobs": one("SELECT COUNT(*) FROM jobs"),
            "scored": one("SELECT COUNT(*) FROM fit"),
            "by_status": by_status,
            "by_source": by_source,
        }
