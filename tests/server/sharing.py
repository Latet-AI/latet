"""An edition that puts in force the rules for everyone its shared settings store, which no edition of Latet does: the
Community Edition shares none, and the Enterprise Edition's rules for everyone are its policy and its synced terms,
which reach the spaces otherwise (``Edition.shared_rules`` answers none in both). The tests of what a rule for everyone
does to the spaces laid over it (approvals, compositions, an API key's layering) run under it, so that the machinery
the shared settings keep for rules stays tested. Never part of Latet: a test's double."""

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
        server.edition.close()  # the server's own, whose listener would outlive the test
        server.edition = SharingEdition()
        server.shared.rules = server.edition.shared_rules
        server.shared.forget()
