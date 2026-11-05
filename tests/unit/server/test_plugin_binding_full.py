"""A launched Claude Code session whose identity's plugin-binding quota is full (root's ruling I-3 on P2-6a's review):
the plugin's calls go on, the session decided as claimed (it only tightens), never answered 503."""

from __future__ import annotations

from pathlib import Path

from latet.bridge.api import Bridge
from latet.config import Config, ProjectEntry
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.app import OnServer
from latet.gateway.mediate import Mediator
from latet.projects import Projects

from ...conftest import ROOT_KEY

_URL = "https://latet.acme.lan"
_LAUNCH = "0123456789abcdef0123456789abcdef"


class _Full:
    """A server's plugin bindings whose identity has filled its quota: every new binding is refused."""

    def __init__(self) -> None:
        self.asked: list[bool] = []

    def session_digest(self, session: str) -> bytes:
        return b"d" * 32

    def refresh(self, session: str, project: str, root: str, *, required: bool) -> bool:
        self.asked.append(required)
        return False


def test_a_launched_session_of_a_full_quota_starts_claimed_and_is_never_refused(tmp_path: Path):
    folder = tmp_path / "repo"
    folder.mkdir()
    built = build(Config(ner="off"), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    try:
        mediator.engine.scopes = Projects(
            Config(ner="off", projects=[ProjectEntry(path=str(folder))]), mediator.engine.vault, files=False
        )
        full = _Full()
        bridge = Bridge(mediator, "token", on_server=OnServer(_URL, "u_emilyaaaaa"), plugin_bindings=full)  # type: ignore[arg-type]
        session = bridge._session_of({"session": "s-1", "cwd": str(folder), "launch_id": _LAUNCH})
        assert full.asked == [True], "the mark was asked to be written, as a launched session's"
        assert session.key == "cc:s-1" and session.project_id is None, "not bound: claimed, its loosenings never apply"
    finally:
        mediator.close()
        mediator.executor.shutdown(wait=True)
        mediator.bridge_executor.shutdown(wait=True)
