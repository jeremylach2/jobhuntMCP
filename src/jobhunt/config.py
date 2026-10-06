"""Configuration: where state lives, and which boards to watch.

The target list is plain YAML so it can be edited by hand or by the model
through the ``add_target`` MCP tool. Paths are overridable by environment
variable so the MCP server can be pointed at a different profile without
editing code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .sources import ATS

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent

# Sources that are scoped to an explicit company list, vs. aggregators that are
# scoped by keyword instead.
ATS_SOURCES = tuple(ATS)
FEED_SOURCES = ("himalayas", "hn", "remoteok", "freehire")


def _path_from_env(var: str, default: Path) -> Path:
    raw = os.environ.get(var)
    return Path(raw).expanduser() if raw else default


def load_dotenv(path: Path) -> None:
    """Load ``KEY=value`` lines from ``path`` into the environment.

    Existing environment variables win, so a real export or the MCP server's
    registered env overrides the file. Deliberately minimal (no quoting rules
    beyond stripping matching quotes, no interpolation) to avoid a dependency
    for one secret.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


@dataclass
class Config:
    root: Path
    db_path: Path
    resume_path: Path
    targets_path: Path
    targets: dict[str, Any] = field(default_factory=dict)

    @property
    def keywords(self) -> list[str]:
        return list(self.targets.get("keywords", []))

    @property
    def exclude_keywords(self) -> list[str]:
        return list(self.targets.get("exclude_keywords", []))

    @property
    def preferences(self) -> dict[str, Any]:
        """Personal constraints (location, remote, salary floor, ...).

        These are judgment inputs for the model via `get_profile`, not a
        filter applied in code. See the no-fit-logic-in-Python rule in
        AGENTS.md. Blank/empty values mean "no preference" and are dropped by
        callers that render this for display.
        """
        return dict(self.targets.get("preferences", {}) or {})

    @property
    def market_countries(self) -> list[str]:
        """Countries `search_market` restricts himalayas results to, from
        ``market.countries`` in targets.yaml. Empty means no restriction.

        Unlike `preferences`, this *is* applied as a filter: whether a posting
        will hire from your country is eligibility, not fit.
        """
        market = self.targets.get("market", {}) or {}
        return [str(c).strip() for c in market.get("countries", []) or [] if str(c).strip()]

    def boards(self, source: str) -> dict[str, str]:
        """Return {slug: display_name} for one ATS source."""
        raw = (self.targets.get("companies", {}) or {}).get(source, {}) or {}
        if isinstance(raw, list):
            # Allow a bare list of slugs as shorthand for {slug: slug}.
            return {slug: slug for slug in raw}
        return {slug: (name or slug) for slug, name in raw.items()}

    def resume_text(self) -> str:
        if self.resume_path.exists():
            return self.resume_path.read_text(encoding="utf-8")
        return ""

    def save_targets(self) -> None:
        self.targets_path.write_text(
            yaml.safe_dump(self.targets, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )


def load(root: Path | None = None) -> Config:
    root = root or _path_from_env("JOBHUNT_HOME", PROJECT_ROOT)
    load_dotenv(root / ".env")
    targets_path = _path_from_env("JOBHUNT_TARGETS", root / "profile" / "targets.yaml")
    cfg = Config(
        root=root,
        db_path=_path_from_env("JOBHUNT_DB", root / "jobhunt.db"),
        resume_path=_path_from_env("JOBHUNT_RESUME", root / "profile" / "resume.md"),
        targets_path=targets_path,
        targets={},
    )
    if targets_path.exists():
        cfg.targets = yaml.safe_load(targets_path.read_text(encoding="utf-8")) or {}
    cfg.targets.setdefault("companies", {})
    for source in ATS_SOURCES:
        cfg.targets["companies"].setdefault(source, {})
    cfg.targets.setdefault("keywords", [])
    cfg.targets.setdefault("exclude_keywords", [])
    cfg.targets.setdefault("preferences", {})
    return cfg
