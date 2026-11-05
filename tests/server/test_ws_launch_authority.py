"""Four protected WS receipt cases; actual PG/Valkey, no provider listener."""

from __future__ import annotations

import os

import pytest

from latet.config import Config, Profile, ProjectEntry
from latet.engine.pipeline import RequestScope, request_scope
from latet.gateway.launch_authority import REQUEST_LAUNCH, RequestLaunch
from latet.projects import Projects
from latet.server.launch_binding import Binding
from latet.server.launch_requests import ServerLaunchRequests
from latet.server.plugin_bindings import PluginBindings
from tests.unit.test_response_sink_failures import _sinks
from tests.unit.ws_launch_authority_support import _CASES, exercise

from .test_launch_authority_core import _ID, _ISSUER, _KEY, _sent
from .test_launch_authority_core import (
    launch_bootstrap_db as launch_bootstrap_db,
)
from .test_launch_authority_core import (
    owned_launch_namespaces as owned_launch_namespaces,
)
from .test_launch_authority_core import (
    protected_core as protected_core,
)

pytestmark = pytest.mark.server


@pytest.mark.parametrize("case", _CASES)
async def test_protected_ws_launch_authority(protected_core, tmp_path, monkeypatch, case):
    db, _bus, authority, codec, _master = protected_core
    directory = tmp_path / "ws-project"
    directory.mkdir()
    directory = directory.resolve()
    with _sinks(tmp_path) as (mediator, _recorder, _store):
        scopes = Projects(
            Config(
                ner="off",
                projects=[ProjectEntry(path=str(directory), profile=Profile.SECRETS, enabled=case != "missing-off")],
            ),
            mediator.engine.vault,
            files=False,
        )
        mediator.engine.scopes = scopes
        project = scopes.resolve(str(directory))
        assert project.scope is not None
        session = mediator.engine.session("session-id:5f4a3e00-0000-4000-8000-00000000f4a3")
        mediator.engine.attach(session, str(directory))
        previous = session.project_id, session.cwd
        binding = Binding(str(directory), project.root, project.id, "client", "windows" if os.name == "nt" else "posix")
        locator = authority.locator(_KEY)
        _sent(authority, authority.reserve_open(_ISSUER, locator, _ID, binding, codec.verifier(_KEY), codec))
        opened = _sent(authority, authority.reserve_hold(_ISSUER, _ID, 0, codec))
        assert opened.receipt is not None
        address = opened.receipt.address
        adapter = ServerLaunchRequests(authority, codec, PluginBindings(db, _ISSUER, b"r" * 32))
        presented = adapter.derive(_KEY) if case != "missing-off" else None

        async def end():
            return await mediator.run(authority.terminal, address.issuer, address.locator, address.generation)

        def app_factory(relay):
            async def app(scope, receive, send):
                # The real protected adapter/receipts are supplied at the Space ingress seam.
                # Gate authentication and raw-header stripping belong to the HTTP integration witnesses.
                scope[REQUEST_LAUNCH] = RequestLaunch(adapter, presented)
                token = request_scope.set(RequestScope())
                try:
                    await relay(scope, receive, send)
                finally:
                    scope.pop(REQUEST_LAUNCH, None)
                    request_scope.reset(token)

            return app

        await exercise(case, mediator, monkeypatch, app_factory, end, project.scope)
        assert (session.project_id, session.cwd) == previous
