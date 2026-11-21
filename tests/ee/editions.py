"""The Enterprise Edition in the tests, without PostgreSQL: a server's edition (a context with a server) whose license
is kept by E1's ``MemoryLicenses``, its settings and secrets in memory, and its policy a ``Source`` of the test's own.
What needs PostgreSQL (the administration API, a change in the administration log) is in the ``server`` tests."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any, Literal

from latet.edition import EditionContext
from latet.ee import edition as edition_module
from latet.ee.license import Licensing
from latet.ee.settings import EnterpriseSettings, with_section

from .licenses import MemoryLicenses


class Source:
    """A source of the floor of the test's own: what it holds and what was asked of it."""

    def __init__(self, floor=None, summary=None):
        self.held, self.said = floor, summary
        self.calls: list[str] = []
        self.server: object = None  # the server and the functions an edition made it with (``memory_edition``)
        self.made: dict[str, Any] = {}

    def floor(self):
        self.calls.append("floor")
        return self.held

    def refresh(self):
        self.calls.append("refresh")
        return False

    def summary(self):
        return self.said

    def start(self):
        self.calls.append("start")

    def close(self):
        self.calls.append("close")


class MemorySettings:
    """The Enterprise settings kept in memory, as a server's database keeps them."""

    def __init__(self) -> None:
        self.kept = EnterpriseSettings()

    def load(self) -> EnterpriseSettings:
        return self.kept

    def where(self) -> str | None:
        return "server"

    def set_section(
        self,
        name: str,
        value: Mapping[str, Any] | None,
        *,
        actor: str,
        address: str | None = None,
        action: str | None = None,
    ) -> EnterpriseSettings:
        self.kept = with_section(self.kept, name, value)
        return self.kept

    def drop(self) -> None:
        return None


class MemorySecrets:
    """The Enterprise settings' secrets kept in memory, as a server's database keeps them."""

    def __init__(self) -> None:
        self.kept: dict[str, str] = {}
        self.bound: dict[str, str | None] = {}

    def save(self, name: str, value: str, *, bound: str | None = None, conn: Any = None) -> Literal["server"]:
        self.kept[name], self.bound[name] = value, bound
        return "server"

    def load(self, name: str, *, bound: str | None = None) -> str | None:
        if name in self.kept and self.bound.get(name) != bound:
            raise ValueError("sealed for something else")  # as the server's master key would not open it
        return self.kept.get(name)

    def remove(self, name: str, *, conn: Any = None) -> bool:
        self.bound.pop(name, None)
        return self.kept.pop(name, None) is not None


def memory_edition(
    monkeypatch,
    tmp_path,
    *,
    clock: Callable[[], float] = time.time,
    count_users: Callable[[], int | None] = lambda: None,
    source: Source | None = None,
) -> tuple[edition_module.EnterpriseEdition, MemoryLicenses, Source]:
    """A server's Enterprise Edition over a license store in memory (no listener, no copies to drop), its policy
    ``source`` (a new ``Source`` when None); the edition, its store and its source. The test closes the edition."""
    store = MemoryLicenses()
    source = Source() if source is None else source

    def on_server(self, server, keys, listen, ticker):
        self.licensing = Licensing(store, clock=self.context.clock, count_users=count_users, keys=keys)
        self.settings_store = MemorySettings()
        self.secrets = MemorySecrets()

    def make_policy(server, **given):  # as latet.ee.policy_sources.make_policy is called: the server, then by name
        source.server, source.made = server, given
        return source

    monkeypatch.setattr(edition_module.EnterpriseEdition, "_on_server", on_server)
    monkeypatch.setattr(edition_module, "make_policy", make_policy)
    context = EditionContext(data=tmp_path / "data", config_dir=tmp_path, server=SimpleNamespace(), clock=clock)
    return edition_module.EnterpriseEdition(context), store, source
