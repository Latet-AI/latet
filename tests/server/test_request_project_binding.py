"""A request's project on a server: trusted only through a binding Latet made, claimed otherwise (RC-REC-7, D7, R-73).

The same rules as on a computer (``tests/unit/test_request_project_binding.py``), through a server's own authorities:
a project's request exception applies to a request whose project Latet trusts (the plugin's binding the bridge wrote for
the identity, or a verified launch key), never to one that names a project in a header or in its text; a claimed
project's rules add protection and never lower it; and a binding the bridge wrote for one identity is never reached by
another identity's request. Projects are compared, not folders: a session or a launch in a subfolder of a project of
the settings is that project's (R-105)."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from latet.config import Config, ProjectEntry, RequestException, TermConfig
from latet.gateway.request_profiles import REVISION
from latet.server.app import create_server_app, listen, open_server
from latet.server.db import Database, migrate
from latet.server.master import MasterKey, root_key
from latet.server.space_settings import PgSettings
from latet.server.tokens import route_key

from .support import DATABASE, URL
from .test_launch_server_adapter import _held_control, _wait_tasks

pytestmark = pytest.mark.server

_ROOT = "/client/acme"
_SESSION = "5c2d7e10-3b4a-4f6e-8d9c-1a2b3c4d5e6f"  # Claude Code session ids are UUIDs
GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
_TEAM = {
    "destination": "anthropic",
    "profile": "anthropic.messages.1",
    "reason": "custom-header",
    "components": [{"kind": "header", "name": "x-team"}],
}


def _approved(**fields) -> RequestException:
    """An exception as the administration's save approves it for an API key: stamped by Latet, by PostgreSQL's day."""
    # A minute back: PostgreSQL's clock, which decides, may run a little behind this machine's.
    stamps = {"id": "x" + secrets.token_hex(5), "granted": datetime.now(UTC) - timedelta(minutes=1)}
    return RequestException.model_validate({**_TEAM, **stamps, "revision": REVISION, **fields})


@dataclass
class _Served:
    server: Any
    app: Any
    client: httpx.AsyncClient
    keys: list[tuple[Any, str]]
    sent: list[httpx.Request] = field(default_factory=list)

    def route(self, key: int = 0) -> str:
        return f"/u/{route_key(self.keys[key][1])}"

    def control(self, key: int = 0) -> dict[str, str]:
        return {"x-latet-token": self.keys[key][1]}

    async def ask(
        self, key: int = 0, *, text: str = "an ordinary question", team: bool = True, **headers: str
    ) -> httpx.Response:
        body = {"model": "claude-sonnet-4-5", "max_tokens": 8, "messages": [{"role": "user", "content": text}]}
        system = headers.pop("system", None)
        if system is not None:
            body["system"] = system
        sent = {"x-api-key": "sk-fixture", **({"x-team": "data-science"} if team else {}), **headers}
        return await self.client.post(self.route(key) + "/anthropic/v1/messages", headers=sent, json=body)

    async def bind(self, cwd: str, key: int = 0) -> None:
        """The plugin's bridge registers its session's folder, as its hook does at the session's start."""
        bridge = URL + self.route(key)
        facts = {
            "bridge": bridge,
            "session": _SESSION,
            "cwd": cwd,
            "env": {"ANTHROPIC_BASE_URL": bridge + "/anthropic"},
            "sources": {},
            "entrypoint": "cli",
            "surface": "terminal",
        }
        checked = await self.client.post(
            self.route(key) + "/_latet/engine/v1/route-check", headers=self.control(key), json=facts
        )
        assert checked.status_code == 200 and checked.json()["ok"] is True

    @contextlib.asynccontextmanager
    async def launched(self, folder: str) -> AsyncIterator[dict[str, str]]:
        """A launch the server opened for ``folder`` (its client's root ``_ROOT``), held as a launcher holds it: the
        header carrying its key."""
        base = self.route() + "/_latet/engine/v1"
        prepared = await self.client.get(base + "/launches/prepare", headers=self.control())
        assert prepared.status_code == 200
        opened = await self.client.post(
            base + "/launches", headers=self.control(), json={"folder": folder, "root": _ROOT, "flavor": "posix"}
        )
        assert opened.status_code == 200
        launch = opened.json()
        item = base + "/launches/" + launch["launch_id"]
        async with _held_control(self.app, item + "/hold", self.keys[0][1], 0) as (held, _disconnect):
            yield {"x-latet-launch-key": launch["key"]}
            ended = await self.client.post(item + "/end", headers=self.control(), json={})
            assert ended.status_code == 200
            await _wait_tasks({held})
        exited = await self.client.post(
            item + "/exit", headers=self.control(), json={"epoch": prepared.json()["epoch"]}
        )
        assert exited.status_code == 200


@contextlib.asynccontextmanager
async def _served(settings, schema, prefix, tmp_path, monkeypatch, *, projects, exceptions, keys=1):
    """A server whose API keys each have ``projects`` and ``exceptions`` (approved), its app and a client of it."""
    settings = settings.model_copy(update={"database_pool": 4, "valkey_connections": 4})
    database = Database(DATABASE, schema=schema, min_size=0, max_size=1)
    try:
        migrate(database, master=MasterKey.load(settings))
    finally:
        database.close()
    server = open_server(settings, schema=schema, prefix=prefix, data=tmp_path / "models")
    sent: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        body = b'{"id":"msg_test","type":"message","role":"assistant","model":"claude","content":[]}'
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(body))

    monkeypatch.setattr(
        "latet.gateway.app.make_client",
        lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False),
    )
    listener = None
    try:
        server.shared.set(Config(ocr="off", history="off"), actor="server")
        made = []
        for n in range(keys):
            identity, token = server.identities.add_key(f"P2-6 project binding {n}")
            storage = PgSettings(
                server.db, server.shared, identity.id, root_key(server.db, server.master, identity.id, clock=server.clock),
                kind="key",
            )  # fmt: skip
            config = storage.prepare(
                storage.load().model_copy(update={"ner": "off", "projects": projects, "request_exceptions": exceptions})
            )
            storage.save(config)
            storage.save_reviewed(config)
            made.append((identity, token))
        app = create_server_app(server)
        listener = asyncio.create_task(listen(server))
        async with asyncio.timeout(30):
            await server.listening.wait()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, trust_env=False) as client:
            yield _Served(server, app, client, made, sent)
    finally:
        if listener is not None:
            listener.cancel()
            await _wait_tasks({listener}, allow_cancelled=True)
        await _wait_tasks(set(server.spaces._building.values()))
        await _wait_tasks(set(server.spaces._index_tasks))
        await server.spaces.close(wait=False)
        await _wait_tasks(set(server.spaces._closing))
        await server.launch_mutations.drain()
        server.close()


@pytest.fixture
def serve(settings, schema, prefix, tmp_path, monkeypatch):
    def made(**kwargs):
        return _served(settings, schema, prefix, tmp_path, monkeypatch, **kwargs)

    return made


# --- RC-REC-7: a claim confers no project exception; a binding Latet made does --------------------------------------


@pytest.mark.parametrize("how", ["header", "text"])
async def test_a_claim_confers_no_project_exception_on_a_server(serve, how):
    async with serve(projects=[ProjectEntry(path=_ROOT)], exceptions=[_approved(project=_ROOT)]) as served:
        claim = {"x-latet-project": _ROOT} if how == "header" else {"system": f"Working directory: {_ROOT}"}
        answer = await served.ask(**claim)
    assert answer.status_code == 400 and "x-team" in answer.text and served.sent == []


@pytest.mark.parametrize("cwd", [_ROOT, _ROOT + "/src"])
async def test_a_plugin_bound_session_gets_its_projects_exception_on_a_server(serve, cwd):
    async with serve(projects=[ProjectEntry(path=_ROOT)], exceptions=[_approved(project=_ROOT)]) as served:
        session = {"x-claude-code-session-id": _SESSION}
        assert (await served.ask(**session)).status_code == 400  # not bound yet: the identity's exceptions alone
        await served.bind(cwd)
        answer = await served.ask(**session)
    assert answer.status_code == 200 and served.sent[-1].headers["x-team"] == "data-science"


@pytest.mark.parametrize("folder", [_ROOT, _ROOT + "/src"])
async def test_a_launch_key_gets_its_projects_exception_on_a_server(serve, folder):
    async with serve(projects=[ProjectEntry(path=_ROOT)], exceptions=[_approved(project=_ROOT)]) as served:
        async with served.launched(folder) as key:
            answer = await served.ask(**key)
        other = await served.ask()  # no key, no binding: the identity's exceptions alone
    assert answer.status_code == 200 and served.sent[-1].headers["x-team"] == "data-science"
    assert other.status_code == 400 and len(served.sent) == 1


@pytest.mark.parametrize(
    ("folder", "named"),
    [(_ROOT + "/src/deep", _ROOT), (_ROOT, _ROOT + "/src")],
    ids=["launched-in-a-subfolder", "exception-on-a-subfolder"],
)
async def test_a_trusted_launch_in_a_projects_subfolder_gets_its_exceptions_on_a_server(
    serve, monkeypatch, folder, named
):
    """I-4: a server reads no file, so a folder is compared with a project by the nearest folder of the settings that
    holds it. A launch whose working folder is a subfolder of a project of the settings is that project's, and gets
    the exceptions named on its root; one named on a subfolder of it applies to a launch at its root. The folders
    compared are recorded on the request as it is prepared (``Prepared.excepted``), in its worker."""
    from latet.gateway import request_transport

    compared: list[frozenset[str]] = []
    original = request_transport.send

    async def watched(http, built):
        compared.append(built.authorized.arena.excepted)
        return await original(http, built)

    monkeypatch.setattr(request_transport, "send", watched)
    exceptions = [_approved(project=named)]
    async with (
        serve(projects=[ProjectEntry(path=_ROOT)], exceptions=exceptions) as served,
        served.launched(folder) as key,
    ):
        answer = await served.ask(**key)
    assert answer.status_code == 200 and served.sent[-1].headers["x-team"] == "data-science"
    assert compared == [frozenset({named})]


async def test_another_project_inside_gets_none_of_the_outer_projects_exceptions_on_a_server(serve):
    """Projects are compared, not folders (R-105): a project of the settings inside another is its own, and a launch
    there gets none of the outer project's exceptions."""
    inner = _ROOT + "/sub"
    async with (
        serve(
            projects=[ProjectEntry(path=_ROOT), ProjectEntry(path=inner)], exceptions=[_approved(project=_ROOT)]
        ) as served,
        served.launched(inner) as key,
    ):
        answer = await served.ask(**key)
    assert answer.status_code == 400 and served.sent == []


# --- R-73: a binding the bridge wrote for one identity is never another identity's ---------------------------------


async def test_a_binding_of_one_identity_is_never_reached_by_another_identitys_request(serve):
    async with serve(projects=[ProjectEntry(path=_ROOT)], exceptions=[_approved(project=_ROOT)], keys=2) as served:
        await served.bind(_ROOT, key=0)
        session = {"x-claude-code-session-id": _SESSION}
        theirs = await served.ask(1, **session)  # the other identity, with the bound session's id
        mine = await served.ask(0, **session)
    assert theirs.status_code == 400 and mine.status_code == 200 and len(served.sent) == 1


# --- D7: a claimed project's rules tighten and never loosen -------------------------------------------------------


async def test_a_claimed_folder_tightens_but_never_loosens_on_a_server(serve):
    """Your settings for the project add a term and let a token through: under a claim the term applies and the
    allowance does not (the token is masked as anywhere else); under the plugin's trusted binding both apply."""
    entry = ProjectEntry(path=_ROOT, allow_values=[GH_PAT], terms=[TermConfig(value="Project Heron", type="PROJECT")])
    async with serve(projects=[entry], exceptions=[]) as served:
        claimed = await served.ask(text=f"Project Heron ships with {GH_PAT}", team=False, **{"x-latet-project": _ROOT})
        assert claimed.status_code == 200
        out = served.sent[-1].content
        assert b"Project Heron" not in out and GH_PAT.encode() not in out
        await served.bind(_ROOT)
        trusted = await served.ask(text=f"deploy with {GH_PAT}", team=False, **{"x-claude-code-session-id": _SESSION})
    assert trusted.status_code == 200 and GH_PAT.encode() in served.sent[-1].content
