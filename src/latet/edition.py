"""Latet's edition. This first version has the Community Edition alone, for a server's ``Server.edition``: E1 writes
the module whole (each hook, the Enterprise Edition and how it is loaded), with the same names.

``shared_rules`` is the hook of the rules a server's administrators set for everyone: the Enterprise Edition gives the
ones in force; the Community Edition gives none (people share rules by hand there: a rules file, a project's
latet.yaml), and the shared settings every identity is laid over hold only what a server needs to run
(``server.shared.in_force``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from latet.config import Config


class Edition(Protocol):
    name: Literal["community", "enterprise"]

    def shared_rules(self, stored: Config) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


class Community:
    """The Community Edition: every hook does nothing."""

    name: Literal["community", "enterprise"] = "community"

    def shared_rules(self, stored: Config) -> Mapping[str, Any]:
        """The rules for everyone in force, read from the shared settings as stored: none."""
        return {}

    def close(self) -> None:
        return None
