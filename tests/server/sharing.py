"""An edition that shares its rules, as the Enterprise Edition will (E1, E6): the tests of what a rule for everyone does
to the spaces laid over it (approvals, compositions, an API key's layering) run under it, so that the machinery the
Community Edition keeps for its shared settings stays tested with rules too. Never part of Latet: a test's double."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from latet.config import Config
from latet.edition import Community
from latet.server.shared import RULE_KEYS


class SharingEdition(Community):
    """Every rule for everyone the shared settings store is in force."""

    def shared_rules(self, stored: Config) -> Mapping[str, Any]:
        return {key: getattr(stored, key) for key in RULE_KEYS}


def share_rules(*servers: Any) -> None:
    """Have ``servers`` (each a ``Server``) share the rules their shared settings store, from their next read on."""
    for server in servers:
        server.edition = SharingEdition()
        server.shared.rules = server.edition.shared_rules
        server.shared.forget()
