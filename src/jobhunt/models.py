"""Core data types shared by sources, storage, and the MCP server."""

from __future__ import annotations

import hashlib
import html as htmllib
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v\xa0]+")
_BLANKS_RE = re.compile(r"\n{3,}")


def html_to_text(html: str | None) -> str:
    """Flatten an ATS job description (HTML) into readable plain text.

    ATS descriptions are small, well-formed fragments, so a regex pass beats
    pulling in a parser dependency. Block tags become newlines so that bullet
    lists survive as separate lines instead of running together.

    Entities are decoded only after tags are stripped, so a literal "&lt;"
    in the text can't turn into a tag and get stripped. That means an
    *escaped* fragment (Greenhouse's ``content``) must be unescaped by its
    adapter first, or its tags come through as text.
    """
    if not html:
        return ""
    text = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", html)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = _TAG_RE.sub("", text)
    text = htmllib.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS_RE.sub("\n\n", text).strip()


@dataclass(slots=True)
class Job:
    """A single normalized job posting.

    `id` is derived from source + source_id so that re-syncing the same posting
    updates the existing row rather than creating a duplicate.
    """

    source: str
    source_id: str
    company: str
    title: str
    url: str
    location: str = ""
    remote: bool = False
    department: str = ""
    description: str = ""
    compensation: str = ""
    posted_at: str = ""
    first_seen: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def id(self) -> str:
        digest = hashlib.sha256(f"{self.source}:{self.source_id}".encode()).hexdigest()
        return digest[:16]

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["id"] = self.id
        row["remote"] = int(self.remote)
        return row

    def summary(self) -> str:
        """One-line form used in list output, where full descriptions are noise."""
        where = self.location or ("Remote" if self.remote else "")
        return f"[{self.id}] {self.company} - {self.title}" + (f" ({where})" if where else "")


# Matched against short location/title strings only. "distributed" and
# "virtual" used to be here too, which made any description mentioning
# "distributed systems" read as remote (about half of all remote-tagged
# Greenhouse postings, measured 2026-09-22).
REMOTE_HINTS = ("remote", "anywhere", "work from home", "wfh")

# What counts as a remote signal inside a description, where a bare "remote"
# is too noisy ("remote execution", "remote-first, but not remote-only").
_REMOTE_PROSE_RE = re.compile(
    r"fully remote|100% remote|remote[- ]first|remote[- ]friendly|#li-remote"
    r"|open to remote|remote (?:position|role)|work from anywhere"
    r"|(?:work|based|held|located)\s+remotely|remotely\s+(?:in|from|within)\b",
    re.I,
)

_DAYS = r"\b(?:\d(?:-\d)?\+?|one|two|three|four|five)\s*days?\s*(?:a|per|/)\s*week\b"
_OFFICE = r"\b(?:in\s+(?:the\s+)?office|onsite|on-site|in-office|in-person|in\s+person)\b"

# Catches in-office cadence stated in prose ("4 days a week in the office",
# "in-person attendance expected at least three days per week") even when the
# words "hybrid"/"onsite" don't otherwise appear near "remote". Seen in
# practice on postings tagged remote by their location field alone, with the
# actual requirement buried in the description body a few paragraphs down.
# "in-person" only counts next to a day cadence: alone it's usually an
# onboarding trip or offsite at a remote-first company.
_ONSITE_CADENCE_RE = re.compile(
    rf"{_DAYS}[^.\n]{{0,60}}{_OFFICE}|{_OFFICE}[^.\n]{{0,60}}{_DAYS}", re.I
)


def has_onsite_requirement(*fields: str | None) -> bool:
    """True if any field states hybrid/onsite work, including an in-office cadence.

    Split out from `looks_remote` so a source adapter can run it against the
    full description too, not just location/title, since that's often where
    the actual day-count requirement lives.
    """
    blob = " ".join(f for f in fields if f).lower()
    if any(x in blob for x in ("hybrid", "on-site", "onsite", "in-office")):
        return True
    return bool(_ONSITE_CADENCE_RE.search(blob))


def looks_remote(*fields: str | None, description: str | None = None) -> bool:
    """Heuristic remote detection.

    ATS boards have no standard remote flag, so this reads the short fields
    (location, title) for any remote hint, and the ``description`` only for
    explicit remote phrasing. `hybrid`, `on-site`, and an in-office cadence
    anywhere are treated as not-remote even when the word `remote` also
    appears, since those postings require relocation.
    """
    blob = " ".join(f for f in fields if f).lower()
    if has_onsite_requirement(blob, description):
        return False
    if any(hint in blob for hint in REMOTE_HINTS):
        return True
    return bool(description and _REMOTE_PROSE_RE.search(description))
