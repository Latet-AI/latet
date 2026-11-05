"""Four local WS authority cases through actual ingress, relay and protocol."""

from __future__ import annotations

import pytest

from latet.config import Config, Profile, ProjectEntry
from latet.gateway.launch_keys import Binding, LocalLaunchKeys
from latet.gateway.local_launch import LocalLaunchDispatch, LocalLaunchIngress
from latet.projects import Projects

from .test_response_sink_failures import _sinks
from .ws_launch_authority_support import _CASES, exercise


@pytest.mark.parametrize("case", _CASES)
async def test_local_ws_launch_authority(tmp_path, monkeypatch, case):
    directory = tmp_path / "ws-project"
    directory.mkdir()
    directory = directory.resolve()
    owner = LocalLaunchKeys(b"r" * 32)
    hold = None
    try:
        with _sinks(tmp_path) as (mediator, _recorder, _store):
            scopes = Projects(
                Config(
                    ner="off",
                    projects=[
                        ProjectEntry(path=str(directory), profile=Profile.SECRETS, enabled=case != "missing-off")
                    ],
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
            opened = owner.commit_open(
                owner.begin_open("computer"),
                Binding(str(directory).encode(), project.root.encode(), project.id),
            )
            hold = owner.reserve_hold(opened["launch_id"], "computer", 0)
            assert hold.acknowledge() == 1

            async def end():
                return owner.end(opened["launch_id"], "computer")

            def app_factory(relay):
                ingress = LocalLaunchIngress(LocalLaunchDispatch(relay), owner)

                async def app(scope, receive, send):
                    if case != "missing-off":
                        scope["headers"] = [*scope["headers"], (b"x-latet-launch-key", opened["key"].encode())]
                    await ingress(scope, receive, send)

                return app

            await exercise(case, mediator, monkeypatch, app_factory, end, project.scope)
            assert (session.project_id, session.cwd) == previous
    finally:
        if hold is not None:
            hold.stop()
            await hold.drain()
            hold.close()
        owner.close()
