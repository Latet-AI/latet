"""Destinations end to end: routed from the start, changed from the dashboard at once and from the file
once you approve, and added by the agents' wrappers: a change waiting for you is then asked again, with
them in it. What the dashboard saves while a wrapper adds one, or while a reload applies the file, is
kept with it; the dashboards hear of each change on the event loop. Removing one in the dashboard takes
away that one alone, and not while an agent still uses it: its settings point at it, or a wrapper
registered it."""

import asyncio
import base64
import json
import threading

import pytest
from starlette.testclient import TestClient

from latet import destinations
from latet.config import Config, Destination, IdentityConfig, TermConfig, load_config, save_config
from latet.destinations import name_for
from latet.doctor import checks
from latet.gateway.app import create_app
from latet.integrations import agents, routes
from latet.runtime import make_runtime
from latet.service import autostart

from ..conftest import ROOT_KEY
from .fake_upstream import FakeUpstream

pytestmark = pytest.mark.e2e

API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}
KEY = {"authorization": "Bearer sk-test"}
GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
EMAIL = "mario.rossi@acme.it"


@pytest.fixture(scope="module")
def old():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture(scope="module")
def new():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture(scope="module")
def third():
    fake = FakeUpstream().start()
    yield fake
    fake.stop()


@pytest.fixture
def daemon(tmp_path, monkeypatch, old):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    config = Config(identity=IdentityConfig(enabled=False), upstreams=[Destination(name="acme", url=old.url)])
    save_config(config, tmp_path / "config.yaml")
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        code = client.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token})
        client.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False)
        yield rt, client
    rt.close()


@pytest.fixture
def agent_files(tmp_path, monkeypatch):
    """The agents' settings in folders of the test: Claude Code's, Codex's and your organization's managed
    settings. None of yours is read, no agent is asked for its version, no port is probed. Gives a writer for
    each, which points that agent at an address."""
    claude, codex, managed = tmp_path / "claude", tmp_path / "codex", tmp_path / "managed-settings.json"
    claude.mkdir()
    codex.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setattr(routes, "MANAGED_SETTINGS", (managed,))
    monkeypatch.setattr(agents, "ALL", {})
    monkeypatch.setattr(checks, "check_port", lambda: [])
    monkeypatch.setattr(checks, "check_engine", lambda: [])
    monkeypatch.setattr(autostart, "installed", lambda: False)

    def env(path):
        return lambda url: path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": url}}), "utf-8")

    def provider(url):
        (codex / "config.toml").write_text(f'[model_providers.acme]\nbase_url = "{url}"\n', "utf-8")

    return {"claude": env(claude / "settings.json"), "managed": env(managed), "codex": provider}


def _send(client, text, route="acme"):
    body = {"model": "m", "stream": True, "messages": [{"role": "user", "content": text}]}
    return client.post(f"/{route}/v1/chat/completions", json=body, headers=KEY)


def _token(rt):
    return {"x-latet-token": rt.gateway.bridge_token}


def _in_use(client):
    """The destinations the Integrations page lists, and whether an agent uses each."""
    return {d["name"]: d["in_use"] for d in client.get(f"{API}/integrations").json()["destinations"]}


def _meanwhile(monkeypatch, owner, name, call):
    """The first time ``owner.name`` runs, ``call`` (a request) starts in another thread and has half a
    second: done by then, unless what runs holds the settings, which makes it wait for its turn. Returns the
    thread and its answers."""
    real, answers = getattr(owner, name), []
    thread = threading.Thread(target=lambda: answers.append(call()), daemon=True)

    def hooked(*args, **kwargs):
        if thread.ident is None:  # the first time only
            thread.start()
            thread.join(timeout=0.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, hooked)
    return thread, answers


def _held(config):
    """What a save and an add each bring in: the values the dashboard gave (terms, or never to mask), and
    the names of the destinations."""
    return [*(t.value for t in config.terms), *config.allow_values], [d.name for d in config.upstreams]


def _told(monkeypatch, broadcaster):
    """What ``broadcaster`` tells the dashboards: each event, its state (a change settled), and whether it was
    published on the event loop, the only place their live feeds work."""
    told, publish = [], broadcaster.publish

    def recording(summary, event="exchange"):
        try:
            on_loop = asyncio.get_running_loop() is not None
        except RuntimeError:  # a worker thread
            on_loop = False
        told.append((event, summary.get("state"), on_loop))
        publish(summary, event)

    monkeypatch.setattr(broadcaster, "publish", recording)
    return told


def test_a_destination_is_routed_from_the_start(daemon, old):
    _, client = daemon
    assert _send(client, "to the old one").status_code == 200
    assert "to the old one" in old.all_received()


def test_an_openai_compatible_provider_by_name(daemon, new):
    """DeepSeek, Mistral or a model server of yours, added by name: an agent whose base URL is
    http://127.0.0.1:5283/deepseek/v1 has its requests masked and its answers restored, as with OpenAI."""
    rt, client = daemon
    config = load_config(rt.config_file)
    added = config.model_copy(update={"upstreams": [*config.upstreams, Destination(name="deepseek", url=new.url)]})
    save_config(added, rt.config_file)  # what `latet config add upstreams` does, then it reloads the daemon
    assert client.post("/_latet/admin/reload", headers=_token(rt)).json()["applied"] is True
    text = f"deploy with {GH_PAT} and mail {EMAIL}"
    r = _send(client, text, route="deepseek")
    assert r.status_code == 200
    assert new.requests[-1]["path"] == "/v1/chat/completions"
    sent = new.received[-1].decode()
    assert GH_PAT not in sent and EMAIL not in sent
    chunks = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: {")]
    deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
    said = "".join(delta.get("content") or "" for delta in deltas)
    arguments = "".join(call["function"]["arguments"] for delta in deltas for call in delta.get("tool_calls") or [])
    assert said == f"You said: {text}"
    assert json.loads(arguments) == {"content": text}


def test_an_address_changed_in_the_file_waits_for_you(daemon, old, new):
    rt, client = daemon
    moved = load_config(rt.config_file).model_copy(update={"upstreams": [Destination(name="acme", url=new.url)]})
    save_config(moved, rt.config_file)
    assert client.post("/_latet/admin/reload", headers=_token(rt)).json()["applied"] is False
    assert _send(client, "still the old one").status_code == 200
    assert "still the old one" in old.all_received() and "still the old one" not in new.all_received()
    [change] = client.get(f"{API}/changes").json()["changes"]
    assert change["loosened"] == ["destination acme: its address changed"]
    assert change["destinations"] == [{"name": "acme", "before": old.url, "after": new.url}]
    client.post(f"{API}/changes/{change['id']}", json={"action": "approve"}, headers=UI)
    assert _send(client, "now the new one").status_code == 200
    assert "now the new one" in new.all_received()


def test_the_dashboard_changes_an_address_at_once(daemon, new):
    _, client = daemon
    r = client.put(f"{API}/config", json={"upstreams": [{"name": "acme", "url": new.url}]}, headers=UI)
    assert r.status_code == 200
    assert _send(client, "straight to the new one").status_code == 200
    assert "straight to the new one" in new.all_received()


def test_the_wrappers_add_a_destination_once(daemon, new):
    rt, client = daemon
    body = {"destinations": [{"url": new.url, "agent": "codex"}]}
    assert client.post("/_latet/admin/destinations", json=body).status_code == 401
    first = client.post("/_latet/admin/destinations", json=body, headers=_token(rt)).json()
    second = client.post("/_latet/admin/destinations", json=body, headers=_token(rt)).json()
    assert first == second == {"names": [name_for(new.url)]}
    assert [d.name for d in load_config(rt.config_file).upstreams] == ["acme", name_for(new.url)]
    assert _send(client, "through the added one", route=name_for(new.url)).status_code == 200
    assert "through the added one" in new.all_received()


def test_removing_a_destination_takes_away_that_one_alone(daemon, agent_files, old, new, third):
    """The page was loaded with a and b; a wrapper adds c; a is removed. Only a goes: b and c stay in the settings
    in use, in config.yaml and routed (the page used to send the list as it had loaded it, and c was dropped)."""
    rt, client = daemon
    page = [{"name": "a", "url": old.url}, {"name": "b", "url": new.url}]
    assert client.put(f"{API}/config", json={"upstreams": page}, headers=UI).status_code == 200
    body = {"destinations": [{"url": third.url, "agent": "codex"}]}
    [c] = client.post("/_latet/admin/destinations", json=body, headers=_token(rt)).json()["names"]
    r = client.delete(f"{API}/destinations/a", headers=UI)
    assert r.status_code == 200 and r.json()["ok"] is True
    for config in (rt.config, load_config(rt.config_file)):
        assert [d.name for d in config.upstreams] == ["b", c]
    assert _send(client, "to b", route="b").status_code == 200 and "to b" in new.all_received()
    assert _send(client, "to c", route=c).status_code == 200 and "to c" in third.all_received()
    assert _send(client, "to a", route="a").status_code == 404  # a is gone


def test_a_destination_that_is_not_there_is_not_found_and_only_the_page_may_remove_one(daemon):
    rt, client = daemon
    r = client.delete(f"{API}/destinations/nowhere", headers=UI)
    assert r.status_code == 404 and "nowhere" in r.json()["error"]
    assert client.delete(f"{API}/destinations/acme").status_code == 403  # refused without the page's header
    assert [d.name for d in rt.config.upstreams] == ["acme"]


def test_a_destination_a_wrapper_registered_is_in_use_and_stays(daemon, agent_files, new):
    """`latet claude` and `latet codex` give the agent a private settings file (deleted when it ends) or `-c`
    arguments: no file of the agent names the destination, but the session uses it."""
    rt, client = daemon
    name = name_for(new.url)
    assert _in_use(client) == {"acme": False}
    body = {"destinations": [{"url": new.url, "agent": "claude"}]}
    assert client.post("/_latet/admin/destinations", json=body, headers=_token(rt)).json() == {"names": [name]}
    assert _in_use(client) == {"acme": False, name: True}
    r = client.delete(f"{API}/destinations/{name}", headers=UI)
    assert r.status_code == 409 and name in r.json()["error"] and "in use" in r.json()["error"]
    assert [d.name for d in rt.config.upstreams] == ["acme", name]
    assert _send(client, "still routed", route=name).status_code == 200
    assert client.delete(f"{API}/destinations/acme", headers=UI).status_code == 200  # the one no one uses goes


def test_a_wrapper_that_finds_a_destination_already_there_uses_it_too(daemon, agent_files, old):
    rt, client = daemon
    body = {"destinations": [{"url": old.url, "agent": "codex"}]}
    assert client.post("/_latet/admin/destinations", json=body, headers=_token(rt)).json() == {"names": ["acme"]}
    assert _in_use(client) == {"acme": True}
    assert client.delete(f"{API}/destinations/acme", headers=UI).status_code == 409
    assert [d.name for d in rt.config.upstreams] == ["acme"]


def test_a_wrapper_registering_during_a_removal_keeps_a_destination_to_use(daemon, agent_files, old, monkeypatch):
    """The removal has looked and found acme unused when a wrapper registers its address. The removal holds the
    settings until it is done, so the wrapper comes after and adds the address again: the wrapper is never left
    with a name that was just removed (it would get 404s)."""
    rt, client = daemon
    dashboard, answers = rt.gateway.dashboard, []
    body = {"destinations": [{"url": old.url, "agent": "claude"}]}
    wrapper = threading.Thread(
        target=lambda: answers.append(client.post("/_latet/admin/destinations", json=body, headers=_token(rt))),
        daemon=True,
    )
    looked = dashboard._in_use

    def looking():
        found = looked()  # the removal holds the settings from here to its end
        wrapper.start()
        wrapper.join(timeout=0.5)  # done by then, unless the removal holds the settings
        return found

    monkeypatch.setattr(dashboard, "_in_use", looking)
    assert client.delete(f"{API}/destinations/acme", headers=UI).status_code == 200
    wrapper.join(timeout=30)
    assert not wrapper.is_alive() and answers[0].status_code == 200
    [name] = answers[0].json()["names"]
    assert [d.name for d in rt.config.upstreams] == [name]  # the address is there again, under the name it was told
    assert _send(client, "after the removal", route=name).status_code == 200
    assert "after the removal" in old.all_received()


@pytest.mark.parametrize("where", ["claude", "managed", "codex"])
def test_a_file_pointing_at_latet_by_a_name_puts_that_destination_in_use(daemon, agent_files, where):
    """Claude Code's settings, your organization's managed settings or Codex's configuration, with Latet's
    address as localhost: acme is in use, and acme-2 (which the address does not name) is not."""
    rt, client = daemon
    two = [{"name": "acme", "url": "https://acme.example.com"}, {"name": "acme-2", "url": "https://acme2.example.com"}]
    assert client.put(f"{API}/config", json={"upstreams": two}, headers=UI).status_code == 200
    assert _in_use(client) == {"acme": False, "acme-2": False}
    agent_files[where](f"http://localhost:{rt.gateway.port}/acme/v1")
    assert _in_use(client) == {"acme": True, "acme-2": False}
    assert client.delete(f"{API}/destinations/acme", headers=UI).status_code == 409
    assert client.delete(f"{API}/destinations/acme-2", headers=UI).status_code == 200
    assert [d.name for d in rt.config.upstreams] == ["acme"]


def test_a_longer_name_is_not_taken_for_a_shorter_one(daemon, agent_files):
    """A file pointing at /acme-2 does not use acme: the name is read whole, not as the start of a longer one."""
    rt, client = daemon
    two = [{"name": "acme", "url": "https://acme.example.com"}, {"name": "acme-2", "url": "https://acme2.example.com"}]
    assert client.put(f"{API}/config", json={"upstreams": two}, headers=UI).status_code == 200
    agent_files["claude"](f"http://127.0.0.1:{rt.gateway.port}/acme-2")
    assert _in_use(client) == {"acme": False, "acme-2": True}
    assert client.delete(f"{API}/destinations/acme", headers=UI).status_code == 200
    assert [d.name for d in rt.config.upstreams] == ["acme-2"]


def test_a_waiting_change_is_asked_again_with_an_added_destination(daemon, new):
    """A change waits; a wrapper adds a destination, in use at once. The change is asked again with it in it
    (a new one, the first superseded), so approving it neither drops it nor counts as stale."""
    rt, client = daemon
    save_config(load_config(rt.config_file).model_copy(update={"upstreams": []}), rt.config_file)
    assert client.post("/_latet/admin/reload", headers=_token(rt)).json()["applied"] is False
    [change] = client.get(f"{API}/changes").json()["changes"]
    assert change["loosened"] == ["destination acme: removed"]
    body = {"destinations": [{"url": new.url, "agent": "claude"}]}
    client.post("/_latet/admin/destinations", json=body, headers=_token(rt))
    assert _send(client, "added while waiting", route=name_for(new.url)).status_code == 200
    [again] = client.get(f"{API}/changes").json()["changes"]
    assert again["id"] != change["id"] and again["loosened"] == ["destination acme: removed"]
    assert [d.name for d in load_config(rt.config_file).upstreams] == [name_for(new.url)]  # what waits holds it
    first = client.get(f"/_latet/admin/changes/{change['id']}", headers=_token(rt)).json()
    assert first == {"waiting": False, "state": "superseded"}  # what `--wait` tells the agent that asked
    r = client.post(f"{API}/changes/{again['id']}", json={"action": "approve"}, headers=UI)
    assert r.json()["state"] == "approved"
    assert [d.name for d in rt.config.upstreams] == [name_for(new.url)]
    assert [d.name for d in load_config(rt.config_file).upstreams] == [name_for(new.url)]
    assert _send(client, "routed once approved", route=name_for(new.url)).status_code == 200
    assert "routed once approved" in new.all_received()


def test_a_waiting_change_giving_an_added_destination_s_name_another_address_says_so(daemon, new):
    """config.yaml gives a name still free another address, and lowers something else, so it waits. A wrapper
    then adds a destination of that name for its own address. The change asked again is weighed against the
    settings now in use: approving it would move that destination, and it says so, with both addresses."""
    rt, client = daemon
    name, elsewhere = name_for(new.url), "https://elsewhere.example"
    config = load_config(rt.config_file)
    upstreams = [*config.upstreams, Destination(name=name, url=elsewhere)]
    save_config(config.model_copy(update={"ner": "off", "upstreams": upstreams}), rt.config_file)
    assert client.post("/_latet/admin/reload", headers=_token(rt)).json()["applied"] is False
    [change] = client.get(f"{API}/changes").json()["changes"]
    assert change["loosened"] == ["local name detection: turned off"]  # adding a destination lowers nothing
    body = {"destinations": [{"url": new.url, "agent": "claude"}]}
    assert client.post("/_latet/admin/destinations", json=body, headers=_token(rt)).json() == {"names": [name]}
    assert _send(client, "its own address meanwhile", route=name).status_code == 200
    assert "its own address meanwhile" in new.all_received()
    [again] = client.get(f"{API}/changes").json()["changes"]
    assert again["loosened"] == ["local name detection: turned off", f"destination {name}: its address changed"]
    assert again["destinations"] == [{"name": name, "before": new.url, "after": elsewhere}]
    assert again["id"] != change["id"]


def test_a_waiting_change_that_now_lowers_nothing_applies_when_a_destination_is_added(daemon, new):
    """What a change waits to lower, you lowered meanwhile in the dashboard. Asked again when a wrapper adds a
    destination, it lowers nothing against the settings in use: it applies, with the destination."""
    rt, client = daemon
    save_config(load_config(rt.config_file).model_copy(update={"ner": "off"}), rt.config_file)
    assert client.post("/_latet/admin/reload", headers=_token(rt)).json()["applied"] is False
    [change] = client.get(f"{API}/changes").json()["changes"]
    assert client.put(f"{API}/config", json={"ner": "off"}, headers=UI).status_code == 200
    body = {"destinations": [{"url": new.url, "agent": "codex"}]}
    assert client.post("/_latet/admin/destinations", json=body, headers=_token(rt)).status_code == 200
    assert client.get(f"{API}/changes").json()["changes"] == []
    first = client.get(f"/_latet/admin/changes/{change['id']}", headers=_token(rt)).json()
    assert first == {"waiting": False, "state": "superseded"}
    for config in (rt.config, load_config(rt.config_file)):
        assert config.ner == "off" and [d.name for d in config.upstreams] == ["acme", name_for(new.url)]


TERM = {"value": "Acme Corp", "type": "COMPANY"}
SAVES = {  # what the dashboard sends: a term, or a value never to mask
    "config": lambda c: c.put(f"{API}/config", json={"terms": [TERM]}, headers=UI),
    "terms": lambda c: c.post(f"{API}/terms", json=TERM, headers=UI),
    "allow": lambda c: c.post(f"{API}/allow", json={"value": TERM["value"]}, headers=UI),
    "rules": lambda c: c.post(
        f"{API}/rules/import", json={"file": "terms: [{value: Acme Corp, type: COMPANY}]", "apply": True}, headers=UI
    ),
}


@pytest.mark.parametrize("save", list(SAVES.values()), ids=list(SAVES))
def test_a_dashboard_save_while_a_destination_is_added_is_kept_too(daemon, new, monkeypatch, save):
    """The wrapper's add reads the settings, the dashboard saves meanwhile, the add lands: neither undoes the
    other (the save waits for the add, then lands on top)."""
    rt, client = daemon
    saver, answers = _meanwhile(monkeypatch, destinations, "add_destination", lambda: save(client))
    body = {"destinations": [{"url": new.url, "agent": "claude"}]}
    assert client.post("/_latet/admin/destinations", json=body, headers=_token(rt)).status_code == 200
    saver.join(timeout=30)
    assert not saver.is_alive() and answers[0].status_code == 200
    kept = (["Acme Corp"], ["acme", name_for(new.url)])
    assert _held(rt.config) == kept
    assert _held(load_config(rt.config_file)) == kept


def test_a_dashboard_save_while_you_approve_a_change_is_kept_too(daemon, monkeypatch):
    """The same for your answer to a waiting change: it applies, and the save lands on top of it."""
    rt, client = daemon
    save_config(load_config(rt.config_file).model_copy(update={"upstreams": []}), rt.config_file)
    assert client.post("/_latet/admin/reload", headers=_token(rt)).json()["applied"] is False
    [change] = client.get(f"{API}/changes").json()["changes"]
    saver, answers = _meanwhile(monkeypatch, rt.mediator.approvals, "settle_change", lambda: SAVES["config"](client))
    r = client.post(f"{API}/changes/{change['id']}", json={"action": "approve"}, headers=UI)
    assert r.json()["state"] == "approved"
    saver.join(timeout=30)
    assert not saver.is_alive() and answers[0].status_code == 200
    kept = (["Acme Corp"], [])  # the approved change took the destination away
    assert _held(rt.config) == kept
    assert _held(load_config(rt.config_file)) == kept


def test_a_dashboard_save_while_a_reload_applies_the_file_is_kept_too(daemon, monkeypatch):
    """The same for a reload: it reads config.yaml, weighs it and applies it in one step. A term saved in the
    dashboard while it applies the file (which lowers nothing) waits for it, then lands on top."""
    rt, client = daemon
    edited = load_config(rt.config_file).model_copy(update={"terms": [TermConfig(value="Project Falcon")]})
    save_config(edited, rt.config_file)
    saver, answers = _meanwhile(monkeypatch, rt, "apply_config", lambda: SAVES["terms"](client))
    assert client.post("/_latet/admin/reload", headers=_token(rt)).json()["applied"] is True
    saver.join(timeout=30)
    assert not saver.is_alive() and answers[0].status_code == 200
    kept = (["Project Falcon", "Acme Corp"], ["acme"])
    assert _held(rt.config) == kept
    assert _held(load_config(rt.config_file)) == kept


def test_the_dashboards_hear_of_each_change_on_the_event_loop(daemon, new, monkeypatch):
    """A change asked for, asked again when a destination is added (the first superseded), then approved: each
    is decided in a worker thread, and each reaches the dashboards' live feeds on the event loop."""
    rt, client = daemon
    told = _told(monkeypatch, rt.mediator.approvals.broadcaster)
    save_config(load_config(rt.config_file).model_copy(update={"upstreams": []}), rt.config_file)
    assert client.post("/_latet/admin/reload", headers=_token(rt)).json()["applied"] is False
    body = {"destinations": [{"url": new.url, "agent": "claude"}]}
    assert client.post("/_latet/admin/destinations", json=body, headers=_token(rt)).status_code == 200
    [change] = client.get(f"{API}/changes").json()["changes"]
    assert client.post(f"{API}/changes/{change['id']}", json={"action": "approve"}, headers=UI).status_code == 200
    assert told == [
        ("change", None, True),
        ("change_settled", "superseded", True),
        ("change", None, True),
        ("change_settled", "approved", True),
    ]
