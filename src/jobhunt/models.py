"""Core data types shared by sources, storage, and the MCP server."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKS_RE = re.compile(r"\n{3,}")

_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&apos;": "'", "&nbsp;": " ", "&mdash;": "-", "&ndash;": "-",
}


def html_to_text(html: str | None) -> str:
    """Flatten an ATS job description (HTML) into readable plain text.

    ATS descriptions are small, well-formed fragments, so a regex pass beats
    pulling in a parser dependency. Block tags become newlines so that bullet
    lists survive as separate lines instead of running together.
    """
    if not html:
        return ""
    text = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", html)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = _TAG_RE.sub("", text)
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
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


REMOTE_HINTS = (
    "remote", "anywhere", "distributed", "work from home", "wfh", "virtual",
)

# Catches in-office cadence stated in prose ("4 days a week in the office",
# "onsite 3 days/week") even when the words "hybrid"/"onsite" don't otherwise
# appear near "remote". Seen in practice on postings tagged remote by their
# location field alone, with the actual requirement buried in the description
# body a few paragraphs down.
_ONSITE_CADENCE_RE = re.compile(
    r"\b\d(?:-\d)?\+?\s*days?\s*(?:a|per)\s*week\b[^.\n]{0,60}"
    r"\b(?:in\s+(?:the\s+)?office|onsite|on-site|in-office)\b"
    r"|\b(?:in\s+(?:the\s+)?office|onsite|on-site|in-office)\b[^.\n]{0,60}"
    r"\b\d(?:-\d)?\+?\s*days?\s*(?:a|per)\s*week\b",
    re.I,
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


def looks_remote(*fields: str | None) -> bool:
    """Heuristic remote detection.

    ATS boards have no standard remote flag, so the location and title strings
    are all we have. `hybrid` and `on-site` are treated as not-remote even when
    the word `remote` also appears, since those postings require relocation.
    """
    blob = " ".join(f for f in fields if f).lower()
    if has_onsite_requirement(blob):
        return False
    return any(hint in blob for hint in REMOTE_HINTS)
