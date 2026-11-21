"""A new floor of the company's (Latet Enterprise) reaches every space of an instance through its spaces, which watch
the server's edition once: each space applies it in its own worker, never in the thread that heard it, one being built
before anyone has it; a space that cannot take it is closed, and none is built or served while the floor cannot be
said (the gate and the administration answer it in fixed words)."""

import asyncio
import threading

import pytest
from starlette.testclient import TestClient

from latet.config import TermConfig
from latet.floor import FLOOR_UNAVAILABLE, FloorUnavailable
from latet.server import app as app_module
from latet.server import spaces as spaces_module
from latet.server.app import create_server_app
from latet.server.tokens import route_key

from ..floors import CompanyFloor, FloorEdition, TermsFloor
from .support import URL, close_test_server, listening, open_test_server, until

pytestmark = pytest.mark.server

FALCON = TermConfig(value="Northwind Traders", type="CLIENT")
ADMIN = "/_latet/server/v1/admin"


class Unreadable(FloorEdition):
    """An edition whose floor cannot be said once ``broken`` (a server's policy this instance cannot read)."""

    broken = False

    def floor(self, identity):
        if self.broken:
            raise FloorUnavailable("this instance cannot read the server's policy")
        return self.locked


@pytest.fixture
def floored(server):
    """The instance with an edition whose floor the test sets (none at first). Its spaces were made to watch the
    server's own edition: the test tells them as that edition would (``Spaces.floor_changed``)."""
    server.edition.close()
    edition = server.edition = Unreadable()
    return server, edition


def _locked(runtime):
    return bool(runtime.in_use().locked_terms)


async def test_the_spaces_watch_the_servers_edition_and_no_space_watches_it_itself(schema, prefix, monkeypatch):
    edition = FloorEdition()
    monkeypatch.setattr(app_module, "server_edition", lambda server, **options: edition)
    server = open_test_server(schema, prefix)
    try:
        assert edition.watchers == [server.spaces.floor_changed]
        emily, _ = server.identities.add_person("Emily Carter")
        async with listening(server):
            runtime = await server.spaces.runtime(emily)
            assert edition.watchers == [server.spaces.floor_changed] and runtime.edition is edition
    finally:
        close_test_server(server)


async def test_a_new_floor_reaches_every_space_in_its_own_worker(floored):
    server, edition = floored
    emily, _ = server.identities.add_person("Emily Carter")
    billing, _ = server.identities.add_key("Billing CI")
    applied_in: list[str] = []

    class Watched(TermsFloor):
        def raise_to(self, config):
            applied_in.append(threading.current_thread().name)
            return super().raise_to(config)

    async with listening(server):
        first, second = await server.spaces.runtime(emily), await server.spaces.runtime(billing)
        assert not _locked(first) and not _locked(second)
        edition.locked = Watched(FALCON)
        heard = threading.Thread(target=server.spaces.floor_changed, name="latet-ee-listener")  # as the listener hears
        heard.start()
        heard.join()
        await until(lambda: _locked(first) and _locked(second))
        assert applied_in and all(name.startswith("latet-space") for name in applied_in)
        assert "Northwind" not in first.built.masker.mask("ask Northwind Traders").text
        assert await server.spaces.runtime(emily) is first  # raised in place, not built again


async def test_a_space_that_cannot_take_a_new_floor_is_closed_and_none_is_built_below_it(floored):
    server, edition = floored
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        await server.spaces.runtime(emily)
        edition.broken = True
        server.spaces.floor_changed()
        await until(lambda: server.spaces.count() == 0)
        with pytest.raises(FloorUnavailable):
            await server.spaces.runtime(emily)
        edition.broken = False
        assert await server.spaces.runtime(emily) is not None  # once it can be said, built on it


async def test_a_floor_heard_while_a_space_is_built_is_applied_before_anyone_has_it(floored, monkeypatch):
    """M4's server half: the floor changes after the runtime compared it, before the space is handed out."""
    server, edition = floored
    emily, _ = server.identities.add_person("Emily Carter")
    made = spaces_module.make_runtime

    def built_then_heard(*args, **kwargs):
        runtime = made(*args, **kwargs)
        edition.locked = TermsFloor(FALCON)  # a policy heard in the instant after the build read the floor
        server.spaces.floor_changed()
        return runtime

    monkeypatch.setattr(spaces_module, "make_runtime", built_then_heard)
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        assert _locked(runtime)


async def test_a_floor_that_cannot_be_said_once_a_space_is_built_refuses_it_before_anyone_has_it(floored, monkeypatch):
    server, edition = floored
    emily, _ = server.identities.add_person("Emily Carter")
    made = spaces_module.make_runtime

    def built_then_unreadable(*args, **kwargs):
        runtime = made(*args, **kwargs)
        edition.broken = True  # a policy this instance cannot read, heard in the instant after the build
        server.spaces.floor_changed()
        return runtime

    monkeypatch.setattr(spaces_module, "make_runtime", built_then_unreadable)
    async with listening(server):
        with pytest.raises(FloorUnavailable):  # said as such: the gate answers it in its fixed words
            await server.spaces.runtime(emily)
        assert server.spaces.count() == 0


async def test_a_floor_heard_while_one_waits_to_be_applied_is_applied_once(floored, monkeypatch):
    """One ``_FLOOR`` queued at a time: one heard while the first runs is queued once, and its mark stays while
    another change runs before it, so a floor heard then queues no second."""
    server, edition = floored
    emily, _ = server.identities.add_person("Emily Carter")
    applied: list[int] = []
    running, go = threading.Event(), threading.Event()
    reapplying, reapply_go = threading.Event(), threading.Event()

    class Counted(TermsFloor):
        def raise_to(self, config):
            applied.append(1)
            if len(applied) == 1:
                running.set()
                assert go.wait(5)
            return super().raise_to(config)

    def held_reapply(space):
        reapplying.set()
        assert reapply_go.wait(5)

    monkeypatch.setattr(spaces_module, "_reapply", held_reapply)
    async with listening(server):
        await server.spaces.runtime(emily)
        space = server.spaces._spaces[emily.id]
        edition.locked = Counted(FALCON)
        server.spaces.floor_changed()
        await until(running.is_set)  # the first application runs in the space's worker
        server.spaces._enqueue(space, "reapply", {})  # another change, queued behind it
        server.spaces.floor_changed()  # heard while the first runs: queued once, after the other change
        await asyncio.sleep(0.05)
        go.set()
        await until(reapplying.is_set)  # the other change runs; the floor heard meanwhile waits behind it
        server.spaces.floor_changed()  # one waits already: none more
        await asyncio.sleep(0.05)
        reapply_go.set()
        await until(lambda: space.queue.empty() and not space.pending)
        await asyncio.sleep(0.05)
        assert len(applied) == 2


async def test_while_the_floor_cannot_be_said_a_space_is_refused_before_anything_is_read_for_it(floored, monkeypatch):
    server, edition = floored
    emily, _ = server.identities.add_person("Emily Carter")
    read: list[str] = []
    counts = spaces_module._read_counts
    monkeypatch.setattr(spaces_module, "_read_counts", lambda *args: read.append("counts") or counts(*args))
    edition.broken = True
    async with listening(server):
        with pytest.raises(FloorUnavailable):
            await server.spaces.runtime(emily)
        assert read == []  # no count, no shared settings, no root key, no backends: it fails fast


def test_the_gate_answers_a_floor_that_cannot_be_said_in_fixed_words(floored):
    server, edition = floored
    _, secret = server.identities.add_key("Billing CI")
    edition.broken = True
    with TestClient(create_server_app(server), base_url=URL) as client:
        answer = client.get(f"/u/{route_key(secret)}/_latet/engine/v1/ping", headers={"x-latet-token": secret})
    assert answer.status_code == 503 and answer.json() == {"error": f"latet: {FLOOR_UNAVAILABLE}"}


def test_the_administration_answers_a_floor_that_cannot_be_said_in_fixed_words(floored):
    server, edition = floored
    admin = {"x-latet-admin": server.admin.issue("first administrator")}
    with TestClient(create_server_app(server), base_url=URL) as client:
        key = client.post(f"{ADMIN}/identities", json={"kind": "key", "name": "Billing CI"}, headers=admin).json()
        path = f"{ADMIN}/identities/{key['identity']['id']}/settings"
        edition.broken = True
        built = client.get(path, headers=admin)  # its space is built: the build asks the floor
        assert built.status_code == 503 and built.json()["error"] == FLOOR_UNAVAILABLE
        edition.broken = False
        read = client.get(path, headers=admin).json()
        runtime = server.spaces._spaces[key["identity"]["id"]].runtime
        before = runtime.config
        edition.broken = True  # the key's space is open: its change asks the floor
        changed = client.put(path, json={"etag": read["etag"], "profile": "max"}, headers=admin)
        assert changed.status_code == 503 and changed.json()["error"] == FLOOR_UNAVAILABLE
        assert runtime.config is before  # nothing applied


class Escaping(CompanyFloor):
    """The company's floor, whose reasons end with what a terminal would act on (a reason can name a project's folder,
    which can hold anything)."""

    def below(self, config):
        return [reason + " " + chr(27) + "]0;owned" + chr(7) for reason in super().below(config)]


def test_an_api_keys_settings_below_the_floor_are_refused_and_the_command_exits_2(server, monkeypatch, tmp_path):
    """F5: the administration sets an API key's settings below the company's floor: 409 with the reasons, nothing
    applied, the call recorded with them; `latet server config --key … set` prints the server's words escaped and exits
    2."""
    from typer.testing import CliRunner

    from latet.cli import server as server_cli
    from latet.cli.agent import MARKERS
    from latet.server.client import _refusal

    server.edition.close()
    server.edition = FloorEdition(Escaping())
    admin = {"x-latet-admin": server.admin.issue("first administrator")}
    with TestClient(create_server_app(server), base_url=URL) as client:
        key = client.post(f"{ADMIN}/identities", json={"kind": "key", "name": "Billing CI"}, headers=admin).json()
        key_id = key["identity"]["id"]
        path = f"{ADMIN}/identities/{key_id}/settings"
        assert client.put(path, json={"profile": "max"}, headers=admin).status_code == 200
        runtime = server.spaces._spaces[key_id].runtime
        before = runtime.config
        refused = client.put(path, json={"profile": "balanced"}, headers=admin)
        assert refused.status_code == 409, refused.text
        [reason] = refused.json()["floor"]
        assert reason.startswith("profile: from max to balanced")
        assert refused.json()["error"].startswith("Set by your company, not changed: profile: from max to balanced")
        assert runtime.config is before  # nothing applied, nothing waits
        assert not list(runtime.mediator.approvals.changes)
        [entry] = [e for e in server.audit.query(action="settings.change", limit=20) if e.details["status"] == 409]
        assert entry.details["floor"] == [reason]

        def call(method, where, body=None, **given):
            answer = client.request(method, f"{ADMIN}/{where}", json=body, params=given.get("query"), headers=admin)
            if answer.status_code >= 400:
                raise _refusal(answer)
            return answer.json()

        monkeypatch.setattr(server_cli, "admin_call", call)
        monkeypatch.setenv("LATET_SERVER_URL", URL)
        monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", admin["x-latet-admin"])
        monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config" / "config.yaml"))
        for name in (
            "LATET_CONNECTION",
            "LATET_SERVER_FINGERPRINT",
            "AGENT",
            *(n for m in MARKERS.values() for n in m),
        ):
            monkeypatch.delenv(name, raising=False)
        import latet.cli.admin  # noqa: F401 - registers the commands
        from latet.cli.main import app

        done = CliRunner().invoke(app, ["server", "config", "--key", key_id, "set", "profile", "balanced"])
    assert done.exit_code == 2, done.output
    assert "Set by your company, not changed: profile: from max to balanced" in done.output
    assert chr(27) not in done.output and "owned" in done.output  # the server's words, escaped
    assert runtime.config is before
