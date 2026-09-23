"""Job board adapters. Each exposes an async ``sync`` returning a SourceResult.

``ATS`` is the registry of company-scoped boards: each module there also
exposes ``fetch_board(client, slug, display_name)``, which the sync loop and
board discovery both call. Adding an ATS means adding its module here.
"""

from types import ModuleType

from . import ashby, greenhouse, lever, smartrecruiters

ATS: dict[str, ModuleType] = {
    "greenhouse": greenhouse,
    "ashby": ashby,
    "lever": lever,
    "smartrecruiters": smartrecruiters,
}
