"""A space on a Latet server, as E0c completes it: /me names the server and whose space it is, /config names what is
set for everyone, the user name's fake is the computer's, and a destination only the server would reach is refused.
A computer's dashboard names no server."""

import asyncio
import hmac
import ipaddress
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from latet.api import rest
from latet.api.rest import USER_NAME_BYTES
from latet.config import Config, IdentityConfig, ProjectEntry, TermConfig
from latet.engine.rehydrate import restore_text
from latet.engine.types import SlotKind
from latet.gateway import forward, ws
from latet.gateway.app import OnServer, create_app
from latet.runtime import Runtime, digest, make_runtime
from latet.server import egress
from latet.server import shared as shared_module
from latet.server.machines import in_work, merged_identity
from latet.server.shared import SettingsStale, _laid_back, carried
from latet.server.spaces import _reapply
from latet.state import IdentityRef, local_backends

from ...conftest import ROOT_KEY
from .conftest import EMILY, SHARED, URL, MemorySettings, as_identity

API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}
BILLING = IdentityRef("u_billingci2", "Billing CI", "key")
HOST = {"host": "latet.acme.lan"}  # a WebSocket's handshake through the test client names the server's host so
W = "billing@acme.com"  # a value the administrator lets through for everyone: it waits for each person
INSIDE = ipaddress.ip_address("10.0.3.9")
OUTSIDE = ipaddress.ip_address("1.2.3.4")


def _client(rt, identity_id=EMILY.id):
    return TestClient(as_identity(create_app(rt.gateway), identity_id), base_url=URL)


def _put(c, path, body):
    """A save of the dashboard's: the etag of the settings as the page read them (``GET /config``) sent with ``body``,
    as the dashboard sends it (on a server a save without one is refused, 409 ``settings_stale``)."""
    etag = c.get(f"{API}/config").json()["etag"]
    return c.put(f"{API}{path}", json={**body, "etag": etag}, headers=UI)


@pytest.fixture(autouse=True)
def _names(monkeypatch):
    """No test resolves a name with the system's resolver (pytest-socket does not stop it): a name resolves to an
    address on the internet, but where a test says otherwise."""
    monkeypatch.setattr(egress, "resolve", lambda host, port: [OUTSIDE])


def test_me_names_the_server_and_whose_space_it_is(space):
    rt, _ = space
    with _client(rt) as c:
        me = c.get(f"{API}/me").json()
    assert me["server"] == {"url": URL, "identity": {"id": EMILY.id, "name": "Emily Carter", "kind": "person"}}


def test_a_server_space_masks_and_restores_latet_credentials_and_route_keys(space):
    rt, _ = space
    tail = "Q7wZ2k4NBX5pL3mD_r8S-v6Hj9Ta0Bc1Eo2Fi3Gu4J5"
    tokens = [f"latet_{prefix}_" + tail for prefix in ("pat", "key", "adm", "ck", "scim", "brg")]
    tokens.append("latet_code_" + tail[:32])
    route = "Em1ly_Cart3r-Rout3K3y_7q9"
    text = " ".join([*tokens, URL + "/u/" + route + "/anthropic"])
    session = rt.built.engine.session("latet-secrets-on-server")
    masked = rt.built.engine.mask_text(text, SlotKind.TOOL_RESULT, session, tool="Read")
    assert all(value not in masked.text for value in [*tokens, route])
    assert {r.etype for r in masked.replacements} == {
        "LATET_PERSONAL_TOKEN",
        "LATET_API_KEY",
        "LATET_ADMIN_TOKEN",
        "LATET_COMPUTER_KEY",
        "LATET_SCIM_TOKEN",
        "LATET_BRIDGE_TOKEN",
        "LATET_ONE_TIME_CODE",
        "LATET_ROUTE_KEY",
    }
    assert URL + "/u/" in masked.text and masked.text.endswith("/anthropic")
    assert restore_text(masked.text, rt.built.engine.restores(session, False))[0] == text


def test_an_api_keys_space_says_it_is_one(tmp_path):
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=MemorySettings(SHARED))
    rt = make_runtime(None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=BILLING)
    rt.gateway.on_server = OnServer(URL, BILLING.id)
    try:
        with _client(rt, BILLING.id) as c:
            who = c.get(f"{API}/me").json()["server"]["identity"]
        assert who == {"id": BILLING.id, "name": "Billing CI", "kind": "key"}
    finally:
        rt.close()


def test_a_computers_dashboard_names_no_server(tmp_path):
    rt = make_runtime(
        Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "d", config_file=tmp_path / "c.yaml"
    )
    token = {"x-latet-token": rt.gateway.bridge_token}
    try:
        with TestClient(create_app(rt.gateway), base_url=f"http://127.0.0.1:{rt.gateway.port}") as c:
            code = c.post("/_latet/admin/login-code", headers=token).json()["code"]
            c.get(f"/_latet/ui/login?code={code}", follow_redirects=False)
            assert c.get(f"{API}/me").json()["server"] is None
            assert "shared" not in c.get(f"{API}/config").json()
    finally:
        rt.close()


def test_config_keeps_her_lists_whole_and_names_no_list_set_for_everyone(space):
    rt, _ = space
    with _client(rt) as c:
        terms = c.get(f"{API}/config").json()["terms"]
        mine = {"terms": [*terms, {"value": "Mercury Labs", "type": "CLIENT"}]}
        assert _put(c, "/config", mine).status_code == 200
        view = c.get(f"{API}/config").json()
    assert [t["value"] for t in view["terms"]] == ["Project Falcon", "Mercury Labs"]
    assert "shared" not in view  # rules for everyone are an edition's; destinations are marked on /integrations


def test_the_user_names_fake_is_the_computers_and_only_when_the_space_wants_one(space):
    rt, _ = space
    with _client(rt) as c:
        assert c.post("/_latet/admin/user-name", json={"name": "emily"}).json() == {"surrogate": None}

        def surrogate(config):
            review = config.auto_mode_review.model_copy(update={"user_name": "surrogate"})
            return config.model_copy(update={"auto_mode_review": review})

        rt.update_config(surrogate)
        fake = c.post("/_latet/admin/user-name", json={"name": "emily"}).json()["surrogate"]
        assert fake and fake != "emily"
        assert c.post("/_latet/admin/user-name", json={}).status_code == 400


def test_a_destination_only_the_server_would_reach_is_refused_and_a_known_one_is_named(space):
    rt, _ = space
    with _client(rt) as c:
        mine = c.post("/_latet/admin/destinations", json={"destinations": [{"url": "https://127.0.0.1:8443"}]})
        assert mine.status_code == 400 and "server itself" in mine.json()["error"]
        known = c.post("/_latet/admin/destinations", json={"destinations": [{"url": "https://llm.acme.com"}]})
    assert known.json()["names"] == ["llm"]


# --- helpers ----------------------------------------------------------------------------------------------------------


def _surrogate(rt):
    """The space's settings want the user name's fake (``auto_mode_review.user_name: surrogate``)."""
    review = rt.config.auto_mode_review.model_copy(update={"user_name": "surrogate"})
    rt.update_config(lambda config: config.model_copy(update={"auto_mode_review": review}))


def _reapplied(rt):
    """The space's settings read again and applied, as a server's space applies a change it hears."""
    _reapply(SimpleNamespace(runtime=rt, identity=SimpleNamespace(kind=rt.identity.kind)))


def _shared_now(rt, settings, shared):
    """The administrator's change of the shared settings to ``shared``, applied by her space: what lowers protection
    against what she approved waits for her."""
    settings._shared = lambda: shared
    _reapplied(rt)


def _masked(rt, text):
    """``text`` as a tool's output in one of her conversations leaves: masked as her settings in use say."""
    return rt.built.engine.mask_text(text, SlotKind.TOOL_RESULT, rt.built.engine.session("cc:s1")).text


def _upstream(rt):
    """The providers the space reaches, a stand-in that records every request it gets: never the network."""
    seen: list[httpx.Request] = []

    def model(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": []})

    rt.gateway.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(model))
    return seen


def _key_space(tmp_path, settings):
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    rt = make_runtime(None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=BILLING)
    rt.gateway.on_server = OnServer(URL, BILLING.id)
    return rt


# --- the user name a computer sends (T9.3, T9.17) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "emily" + chr(0xD800),  # a lone surrogate: JSON's escape of it parses to a text no UTF-8 holds
        "emily" + chr(0x202E),  # a bidirectional override
        "emi" + chr(0x200B) + "ly",  # a zero-width space
        "emily" + chr(0x2028),  # a line separator
        "emily" + chr(0x85),  # a C1 control
        "emily" + chr(0x0A),
        "x" * 257,
        "   ",
        "",
        5,
        None,
    ],
    ids=[
        "surrogate",
        "override",
        "zero-width",
        "separator",
        "c1",
        "newline",
        "257",
        "spaces",
        "empty",
        "number",
        "null",
    ],
)
def test_a_user_name_that_is_not_one_is_refused_and_nothing_is_interned(space, name):
    rt, _ = space
    _surrogate(rt)
    before = len(rt.built.vault.entities())
    body = json.dumps({"name": name}).encode()  # ensure_ascii: a lone surrogate goes as JSON's escape
    with _client(rt) as c:
        refused = c.post("/_latet/admin/user-name", content=body, headers={"content-type": "application/json"})
    assert refused.status_code == 400 and refused.json()["error"].startswith("name: ")
    assert len(rt.built.vault.entities()) == before


@pytest.mark.parametrize(
    "body",
    [b'{"name": "emily", "x": 1}', b'["emily"]', b'"emily"', b"emily", b'{"name": "emily"', b"{}", b""],
    ids=["another-key", "a-list", "a-text", "not-json", "cut", "empty-object", "nothing"],
)
def test_a_user_names_body_of_any_other_shape_is_refused_whatever_the_settings(space, body):
    rt, _ = space
    with _client(rt) as c:
        kept = c.post("/_latet/admin/user-name", content=body, headers={"content-type": "application/json"})
        _surrogate(rt)
        wanted = c.post("/_latet/admin/user-name", content=body, headers={"content-type": "application/json"})
    for refused in (kept, wanted):
        assert refused.status_code == 400 and "emily" not in refused.text
    assert rt.built.vault.by_original("emily") is None


def test_a_user_name_is_interned_as_it_is_sent_and_a_body_past_its_bound_is_not_read(space):
    rt, _ = space
    _surrogate(rt)
    name = " Emily " + chr(0xE9) + "x" * 200  # spaces, case and a letter of another script, as sent
    with _client(rt) as c:
        fake = c.post("/_latet/admin/user-name", json={"name": name}).json()["surrogate"]
        past = c.post("/_latet/admin/user-name", json={"name": "emily", "pad": "x" * USER_NAME_BYTES})
    assert rt.built.vault.by_original(name).surrogate == fake
    assert past.status_code == 413 and rt.built.vault.by_original("emily") is None


def test_a_vault_that_takes_no_new_value_answers_in_its_own_words_never_500(space):
    rt, _ = space
    _surrogate(rt)
    assert rt.new_values is not None
    rt.new_values.ceiling = lambda entity: "this space holds its share of the server's memory: it learns no new value"
    with _client(rt) as c:
        full = c.post("/_latet/admin/user-name", json={"name": "emily"})
    assert full.status_code == 409 and "its share" in full.json()["error"] and "emily" not in full.text


# --- an API key has no computer (T9.12) -------------------------------------------------------------------------------


def test_an_api_keys_space_makes_neither_of_the_computers_calls_and_nothing_of_them_is_read(tmp_path):
    settings = MemorySettings(SHARED)
    rt = _key_space(tmp_path, settings)
    try:
        _surrogate(rt)  # its administrator's settings want a fake: a name sent would be interned without the check
        before = rt.config
        calls = {
            "/_latet/admin/user-name": {"name": "billing-ci"},
            "/_latet/admin/destinations": {"destinations": [{"url": "https://collector.example.net"}]},
        }
        with _client(rt, BILLING.id) as c:
            for path, body in calls.items():
                refused = c.post(path, json=body)
                assert (refused.status_code, refused.json()) == (403, {"error": "an API key has no computer"}), path
                # Refused before its body is read: one past every bound is refused the same, not 413.
                big = c.post(path, content=b"x" * (rest.DESTINATIONS_BYTES + 1))
                assert big.status_code == 403, path
        assert rt.config == before and rt.built.vault.by_original("billing-ci") is None
        assert [d.name for d in rt.config.upstreams] == ["llm"]
    finally:
        rt.close()


# --- the work threads and what the work raises (T9.13) ----------------------------------------------------------------


def test_a_computers_calls_do_their_work_in_the_servers_work_threads(space, monkeypatch):
    rt, _ = space
    rt.gateway.work = in_work  # as the server hands it to each space it builds
    _surrogate(rt)
    threads: list[str] = []
    add, intern = rt.add_destinations, rt.built.vault.intern

    def adding(*args, **kwargs):
        threads.append(threading.current_thread().name)
        return add(*args, **kwargs)

    def interning(*args, **kwargs):
        threads.append(threading.current_thread().name)
        return intern(*args, **kwargs)

    monkeypatch.setattr(rt, "add_destinations", adding)
    monkeypatch.setattr(rt.built.vault, "intern", interning)
    with _client(rt) as c:
        named = c.post("/_latet/admin/destinations", json={"destinations": [{"url": "https://gw.acme.com"}]})
        faked = c.post("/_latet/admin/user-name", json={"name": "emily"})
    assert named.json() == {"names": ["gw-acme-com"]} and faked.json()["surrogate"]
    assert len(threads) == 2 and all(name.startswith("latet-work") for name in threads)


def test_what_a_computers_call_raises_in_the_work_threads_is_answered_never_500(space, monkeypatch):
    rt, settings = space
    rt.gateway.work = in_work
    _surrogate(rt)

    def unanswered(*args, **kwargs):
        raise ConnectionError("the vault's store did not answer")

    monkeypatch.setattr(rt.built.vault, "intern", unanswered)

    def stale(own):
        raise SettingsStale("the settings of u_emilycarte changed on another instance since they were read")

    monkeypatch.setattr(settings, "_keep_own", stale)
    with _client(rt) as c:
        down = c.post("/_latet/admin/user-name", json={"name": "emily"})
        moved = c.post("/_latet/admin/destinations", json={"destinations": [{"url": "https://llm.acme.com/v2"}]})
    assert down.status_code == 503 and down.headers["retry-after"] == "1" and "emily" not in down.text
    assert moved.status_code == 409 and moved.json()["reason"] == "settings_stale"
    assert "u_emilycarte" not in moved.text and [d.name for d in rt.config.upstreams] == ["llm"]


# --- the destinations a computer names (T9.4, T9.16) ------------------------------------------------------------------


def test_a_shared_destination_written_with_a_slash_is_named_with_nothing_resolved(space, monkeypatch):
    rt, _ = space
    resolved: list[str] = []

    def never(host, port):  # a known address is never judged, so never resolved (a failure here would be swallowed)
        resolved.append(host)
        raise AssertionError("a known address is never resolved")

    monkeypatch.setattr(egress, "resolve", never)
    with _client(rt) as c:
        named = c.post("/_latet/admin/destinations", json={"destinations": [{"url": "https://llm.acme.com/"}]})
    assert named.json() == {"names": ["llm"]} and [d.name for d in rt.config.upstreams] == ["llm"]
    assert resolved == []


def test_an_address_whose_name_leads_inside_the_servers_network_is_refused_whatever_its_case(space, monkeypatch):
    rt, _ = space
    monkeypatch.setattr(egress, "resolve", lambda host, port: [INSIDE])
    with _client(rt) as c:
        upper = c.post("/_latet/admin/destinations", json={"destinations": [{"url": "https://LLM.acme.com"}]})
        server = c.post("/_latet/admin/destinations", json={"destinations": [{"url": f"{URL}/v1"}]})
    assert upper.status_code == 400 and "own network" in upper.json()["error"]
    assert server.status_code == 400 and "server itself" in server.json()["error"]
    assert [d.name for d in rt.config.upstreams] == ["llm"] and "10.0.3.9" not in upper.text


@pytest.mark.parametrize(
    "url", ["https://gw.acme.com/" + "x" * 2030, "https://gw.acme.com/" + chr(0x202E) + "v1", "https://gw acme.com"]
)
def test_an_address_past_its_bound_or_holding_a_character_outside_printable_ascii_is_refused(space, url):
    rt, _ = space  # its name would be judged good (``_names``): only the bound refuses it
    with _client(rt) as c:
        refused = c.post("/_latet/admin/destinations", json={"destinations": [{"url": url}]})
    assert refused.status_code == 400 and refused.json()["error"].startswith("destinations: ")
    assert len(url) > 2048 or chr(0x202E) in url or " " in url
    assert [d.name for d in rt.config.upstreams] == ["llm"]


@pytest.mark.parametrize(
    "body",
    [
        {"destinations": [{"url": "https://gw.acme.com"}], "x": 1},
        {"destinations": [{"url": "https://gw.acme.com", "name": "gw"}]},
        {"destinations": [{"url": "https://gw.acme.com", "agent": "opencode"}]},
        {"destinations": [{"url": "https://gw.acme.com"}] * 21},
        {"destinations": []},
        {"destinations": "https://gw.acme.com"},
    ],
    ids=["another-key", "another-field", "another-agent", "21", "none", "a-text"],
)
def test_destinations_of_any_other_shape_are_refused(space, body):
    rt, _ = space
    with _client(rt) as c:
        refused = c.post("/_latet/admin/destinations", json=body)
    assert refused.status_code == 400 and [d.name for d in rt.config.upstreams] == ["llm"]


def test_a_shared_destination_removed_between_the_judgment_and_the_add_refuses_the_whole_call(space, monkeypatch):
    rt, _ = space
    add = rt.add_destinations

    def meanwhile(wanted, *, judged=None):
        # A re-application took the shared destination away between the call's judgment and its change.
        rt.apply_config(rt.config.model_copy(update={"upstreams": []}), save=False, as_is=True)
        return add(wanted, judged=judged)

    monkeypatch.setattr(rt, "add_destinations", meanwhile)
    with _client(rt) as c:
        refused = c.post("/_latet/admin/destinations", json={"destinations": [{"url": "https://llm.acme.com"}]})
    assert (refused.status_code, refused.json()) == (409, {"error": "your settings changed meanwhile: try again"})
    assert rt.config.upstreams == [] and "llm" not in rt.wrapped


# --- a save never approves what waits (T9.14) -------------------------------------------------------------------------


def _approved(settings):
    return settings.approved


@pytest.mark.parametrize(
    ("key", "loosening"),
    [("allow_values", W), ("disabled_secret_rules", "generic-api-key")],
    ids=["a-value-let-through", "a-secret-rule-off"],
)
def test_what_an_administrator_loosens_waits_whatever_she_saves_meanwhile_and_applies_when_she_approves(
    space, key, loosening
):
    rt, settings = space
    with _client(rt) as c:
        terms = c.get(f"{API}/config").json()["terms"]
        mine = [*terms, {"value": "Mercury Labs", "type": "CLIENT"}]
        assert _put(c, "/config", {"terms": mine}).status_code == 200
    _shared_now(rt, settings, SHARED.model_copy(update={key: [*getattr(SHARED, key), loosening]}))

    def waits() -> None:
        assert loosening not in getattr(rt.config, key) and loosening not in getattr(_approved(settings), key)
        [change] = rt.mediator.approvals.waiting_changes()
        assert loosening in getattr(change.config, key)

    waits()
    with _client(rt) as c:
        view = c.get(f"{API}/config").json()
        assert "shared" not in view and loosening not in view.get(key, [])  # (7) never named
        # (1) One more term of hers: in use at once, what waits still waits.
        venus = {"value": "Project Venus", "type": "PROJECT"}
        assert _put(c, "/config", {"terms": [*view["terms"], venus]}).status_code == 200
        assert "Project Venus" in [t.value for t in rt.config.terms]
        assert "Project Venus" in [t.value for t in _approved(settings).terms]
        waits()
        # (2) A save that names what waits too: her term applies, what waits does not.
        rt.update_config(
            lambda config: config.model_copy(
                update={key: [*getattr(config, key), loosening], "terms": [*config.terms, TermConfig(value="Orion")]}
            )
        )
        assert "Orion" in [t.value for t in rt.config.terms]
        waits()
        # (3) Her own term removed: gone at once, what waits still out.
        kept = [t for t in c.get(f"{API}/config").json()["terms"] if t["value"] != "Mercury Labs"]
        assert _put(c, "/config", {"terms": kept}).status_code == 200
        assert "Mercury Labs" not in [t.value for t in rt.config.terms]
        waits()
        # (4) A computer's destination: in use, named, and the change asked again with it.
        named = c.post("/_latet/admin/destinations", json={"destinations": [{"url": "https://gw.acme.com"}]})
        assert named.json() == {"names": ["gw-acme-com"]} and "gw-acme-com" in [d.name for d in rt.config.upstreams]
        waits()
        [change] = rt.mediator.approvals.waiting_changes()
        assert "gw-acme-com" in [d.name for d in change.config.upstreams]
        assert "Project Venus" in [t.value for t in change.config.terms]
        assert "Mercury Labs" not in [t.value for t in change.config.terms]
        # (5) A computer's identity, as the server's machines' call applies it.
        sent = IdentityConfig(usernames=["emily"], hostname="emily-laptop")
        rt.update_config(lambda config: config.model_copy(update={"identity": merged_identity(config.identity, sent)}))
        assert rt.config.identity.hostname == "emily-laptop"
        waits()
        if key == "allow_values":
            assert W not in _masked(rt, f"write to {W}")
        # (6) Her approval applies what waited, with everything she did meanwhile.
        [change] = rt.mediator.approvals.waiting_changes()
        assert c.post(f"{API}/changes/{change.id}", json={"action": "approve"}, headers=UI).json()["ok"] is True
    assert loosening in getattr(rt.config, key) and loosening in getattr(_approved(settings), key)
    assert {"Project Venus", "Orion"} <= {t.value for t in rt.config.terms}
    assert "gw-acme-com" in [d.name for d in rt.config.upstreams] and rt.config.identity.hostname == "emily-laptop"
    assert rt.mediator.approvals.waiting_changes() == []


def test_a_shared_term_removed_stays_masked_through_her_saves_until_she_approves(space):
    rt, settings = space
    _shared_now(rt, settings, SHARED.model_copy(update={"terms": []}))  # Project Falcon removed for everyone
    with _client(rt) as c:
        view = c.get(f"{API}/config").json()
        assert "Project Falcon" in [t["value"] for t in view["terms"]]
        mine = [*view["terms"], {"value": "Mercury Labs", "type": "CLIENT"}]  # the page keeps the term it shows
        assert _put(c, "/config", {"terms": mine}).status_code == 200
        assert "Project Falcon" not in _masked(rt, "the Project Falcon launch")
        assert "Project Falcon" in [t.value for t in _approved(settings).terms]
        [change] = rt.mediator.approvals.waiting_changes()
        assert "Project Falcon" not in [t.value for t in change.config.terms]
        assert "Mercury Labs" in [t.value for t in change.config.terms]
        assert c.post(f"{API}/changes/{change.id}", json={"action": "approve"}, headers=UI).status_code == 200
    assert [t.value for t in rt.config.terms] == ["Mercury Labs"]


def test_her_removal_of_a_term_the_shared_settings_dropped_is_hers_and_ends_what_waited(space):
    rt, settings = space
    _shared_now(rt, settings, SHARED.model_copy(update={"terms": []}))
    with _client(rt) as c:
        assert _put(c, "/config", {"terms": []}).status_code == 200
    assert rt.config.terms == [] and _approved(settings).terms == []
    assert rt.mediator.approvals.waiting_changes() == []  # nothing lowers protection against what she approved now


def test_a_save_over_a_composition_without_the_settings_she_approved_is_refused_saying_a_change_waits(space):
    rt, settings = space
    _shared_now(rt, settings, SHARED.model_copy(update={"allow_values": [*SHARED.allow_values, W]}))
    before = rt.config
    with _client(rt) as c:
        terms = c.get(f"{API}/config").json()["terms"]
        settings.approved = None  # what she approved last can no longer be read
        mine = {"terms": [*terms, {"value": "Orion", "type": "PROJECT"}]}
        refused = _put(c, "/config", mine)
        named = c.post("/_latet/admin/destinations", json={"destinations": [{"url": "https://gw.acme.com"}]})
    for answer in (refused, named):
        assert answer.status_code == 409 and answer.json()["reason"] == "settings_change_waits"
    assert rt.config == before and W not in rt.config.allow_values


def test_a_save_of_the_very_settings_in_use_over_a_composition_changes_nothing(space):
    rt, settings = space
    _shared_now(rt, settings, SHARED.model_copy(update={"allow_values": [*SHARED.allow_values, W]}))
    [waiting] = rt.mediator.approvals.waiting_changes()
    before, own = rt.config, dict(settings.own)
    assert rt.update_config(lambda config: config.model_copy(deep=True)) is None  # kept as it is: no rejection
    assert rt.config is before and settings.own == own
    assert [c.id for c in rt.mediator.approvals.waiting_changes()] == [waiting.id]


@contextmanager
def _space_over(tmp_path, shared):
    """A space as the ``space`` fixture makes it, over these shared settings; and the store of its settings."""
    settings = MemorySettings(shared)
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    rt = make_runtime(None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=EMILY)
    rt.gateway.on_server = OnServer(URL, EMILY.id)
    try:
        yield rt, settings
    finally:
        rt.close()


ATLAS = TermConfig(value="Atlas", type="PROJECT")


def test_her_removal_of_one_of_two_terms_the_shared_settings_dropped_leaves_the_other_hers_to_approve(tmp_path):
    """The administrator removes two shared terms; she removes one of them herself, which applies at once: the change
    waiting is asked again over the settings in use now (its base), and her approval applies it, never stale."""
    shared = SHARED.model_copy(update={"terms": [*SHARED.terms, ATLAS]})
    with _space_over(tmp_path, shared) as (rt, settings):
        _shared_now(rt, settings, shared.model_copy(update={"terms": []}))  # both removed for everyone
        with _client(rt) as c:
            kept = [t for t in c.get(f"{API}/config").json()["terms"] if t["value"] != "Atlas"]
            assert _put(c, "/config", {"terms": kept}).status_code == 200
            assert [t.value for t in rt.config.terms] == ["Project Falcon"]  # what is left of the removal waits
            [change] = rt.mediator.approvals.waiting_changes()
            decided = c.post(f"{API}/changes/{change.id}", json={"action": "approve"}, headers=UI)
        assert decided.json() == {"ok": True, "state": "approved"}
        assert rt.config.terms == [] and rt.mediator.approvals.waiting_changes() == []


def test_a_reapplication_asks_a_change_again_when_the_settings_in_use_moved_under_it(tmp_path):
    """The settings in use changed under a change waiting with the very settings found (her approval of a part of it
    on another instance): the re-application asks it again over them, so that her approval here is never stale."""
    shared = SHARED.model_copy(update={"terms": [*SHARED.terms, ATLAS]})
    with _space_over(tmp_path, shared) as (rt, settings):
        _shared_now(rt, settings, shared.model_copy(update={"terms": []}))
        assert settings.approved is not None
        approved = settings.approved.model_copy(update={"terms": [t for t in settings.approved.terms if t != ATLAS]})
        settings.approved = approved  # as another instance kept it
        _reapplied(rt)
        assert [t.value for t in rt.config.terms] == ["Project Falcon"]
        [change] = rt.mediator.approvals.waiting_changes()
        assert change.base == digest(rt.config)
        assert rt.decide_change(change.id, True) == "approved" and rt.config.terms == []


def test_her_edits_of_items_of_her_own_apply_while_a_change_waits(space):
    """A field rule, a term and a destination of her own, changed under the same name while an administrator's
    loosening waits: her changes apply at once and are approved; what waits still waits, and a shared item keeps the
    shared version."""
    rt, settings = space
    with _client(rt) as c:
        view = c.get(f"{API}/config").json()
        mine = {
            "terms": [*view["terms"], {"value": "Mercury Labs", "type": "CLIENT"}],
            "fields": [{"name": "notes", "type": "NOTE", "mask": False}],
            "upstreams": [*view["upstreams"], {"name": "gw", "url": "https://gw.acme.com"}],
        }
        assert _put(c, "/config", mine).status_code == 200
    _shared_now(rt, settings, SHARED.model_copy(update={"allow_values": [*SHARED.allow_values, W]}))
    retyped = {"Mercury Labs": "PROJECT", "Project Falcon": "CLIENT"}  # hers, and one set for everyone
    with _client(rt) as c:
        view = c.get(f"{API}/config").json()
        changed = {
            "terms": [{**t, "type": retyped.get(t["value"], t["type"])} for t in view["terms"]],
            "fields": [{**f, "mask": True} for f in view["fields"]],
            "upstreams": [{**d, "url": "https://gw2.acme.com"} if d["name"] == "gw" else d for d in view["upstreams"]],
        }
        assert _put(c, "/config", changed).status_code == 200
    assert settings.approved is not None
    for config in (rt.config, settings.approved):  # in use, and approved
        assert {t.value: t.type for t in config.terms} == {"Project Falcon": "PROJECT", "Mercury Labs": "PROJECT"}
        assert [(f.name, f.mask) for f in config.fields] == [("notes", True)]
        assert {d.name: d.url for d in config.upstreams} == {
            "llm": "https://llm.acme.com",
            "gw": "https://gw2.acme.com",
        }
        assert W not in config.allow_values
    [change] = rt.mediator.approvals.waiting_changes()
    assert W in change.config.allow_values and [(f.name, f.mask) for f in change.config.fields] == [("notes", True)]


def test_a_save_naming_the_version_of_her_own_item_that_waits_approves_nothing_of_it(space):
    """Her own field rule, its values let through on another instance (a loosening of hers, kept, not approved): here
    the version she approved applies and the other waits. A save that names the version waiting applies none of it,
    and approves none of it: that is what waits for her approval."""
    rt, settings = space
    with _client(rt) as c:
        assert _put(c, "/config", {"fields": [{"name": "notes", "type": "NOTE"}]}).status_code == 200
    settings.own = {**settings.own, "fields": [{"name": "notes", "type": "NOTE", "mask": False}]}
    _reapplied(rt)
    assert [(f.name, f.mask) for f in rt.config.fields] == [("notes", True)] and rt.mediator.approvals.waiting_changes()
    with _client(rt) as c:
        view = c.get(f"{API}/config").json()
        named = {
            "fields": [{"name": "notes", "type": "NOTE", "mask": False}],
            "terms": [*view["terms"], {"value": "Orion"}],
        }
        assert _put(c, "/config", named).status_code == 200
    assert settings.approved is not None and "Orion" in [t.value for t in rt.config.terms]
    for config in (rt.config, settings.approved):  # in use, and approved: the version she approved
        assert [(f.name, f.mask) for f in config.fields] == [("notes", True)]
    [change] = rt.mediator.approvals.waiting_changes()
    assert [(f.name, f.mask) for f in change.config.fields] == [("notes", False)]


def test_a_save_over_a_composition_takes_her_version_of_her_own_items_and_never_of_a_shared_one():
    """As a save is read over a composition (``_laid_back`` for what is kept, ``carried`` for what she approves): her
    version of an item of her own she changed under its mark, the shared version of a shared item whatever the save
    holds of it."""
    falcon, mercury = (
        TermConfig(value="Project Falcon", type="PROJECT"),
        TermConfig(value="Mercury Labs", type="CLIENT"),
    )
    hers = mercury.model_copy(update={"type": "PROJECT"})
    mine = [falcon.model_copy(update={"case_sensitive": True}), hers]  # a shared term made to match fewer forms
    assert _laid_back("terms", mine, [falcon, mercury], [falcon, mercury], [falcon]) == [falcon, hers]
    saved, in_force = Config(terms=mine), Config(terms=[falcon, mercury])
    assert carried(saved, in_force, in_force, in_force, Config(terms=[falcon])).terms == [falcon, hers]


def test_a_save_over_a_composition_whose_approved_settings_do_not_open_is_refused_saying_a_change_waits(
    space, monkeypatch
):
    rt, settings = space
    _shared_now(rt, settings, SHARED.model_copy(update={"allow_values": [*SHARED.allow_values, W]}))
    before = rt.config
    with _client(rt) as c:
        terms = c.get(f"{API}/config").json()["terms"]

        def unopened():
            raise ValueError("the approved settings of u_emilycarte kept in the database do not open with its root key")

        monkeypatch.setattr(settings, "reviewed", unopened)
        refused = _put(c, "/config", {"terms": [*terms, {"value": "Orion"}]})
    assert refused.status_code == 409 and refused.json()["reason"] == "settings_change_waits"
    assert "u_emilycarte" not in refused.text and rt.config == before


# --- the dashboard's saves on a server (the fix round's Minors 1 and 7) -----------------------------------------------


def test_a_dashboard_save_on_a_server_is_made_in_the_servers_work_threads(space, monkeypatch):
    """No store call of a save (what she approved, read again over a composition) waits in the default executor, which
    the spaces' builds hold: a save, a value let through and an import are made in the server's work threads."""
    rt, _ = space
    rt.gateway.work = in_work  # as the server hands it to each space it builds
    threads: list[str] = []
    update = rt.update_config

    def spied(*args, **kwargs):
        threads.append(threading.current_thread().name)
        return update(*args, **kwargs)

    monkeypatch.setattr(rt, "update_config", spied)
    with _client(rt) as c:
        imported = c.post(
            f"{API}/rules/import", json={"file": "terms:\n  - {value: Orion}\n", "apply": True}, headers=UI
        )
        view = c.get(f"{API}/config").json()  # read in a worker thread, before the default executor fails
        taken: list[object] = []

        async def failing(work, *args, **kwargs):
            taken.append(work)
            raise AssertionError("a save took a thread of the default executor")

        with monkeypatch.context() as patched:  # the space's own closing drains its recorder in one, as before
            patched.setattr(asyncio, "to_thread", failing)
            mine = {"terms": [*view["terms"], {"value": "Mercury Labs"}], "etag": view["etag"]}
            saved = c.put(f"{API}/config", json=mine, headers=UI)
            allowed = c.post(f"{API}/allow", json={"value": W}, headers=UI)
    assert imported.json()["applied"] is True and saved.status_code == 200 and allowed.status_code == 200
    assert len(threads) == 3 and all(name.startswith("latet-work") for name in threads) and taken == []
    assert {"Orion", "Mercury Labs"} <= {t.value for t in rt.config.terms} and W in rt.config.allow_values


def test_a_dashboards_saves_take_one_work_thread_at_a_time(space, monkeypatch):
    """A flood of one person's saves holds one of the server's work threads, the others waiting their turn on the
    event loop: the threads stay for everyone else's work."""
    rt, _ = space
    rt.gateway.work = in_work
    entered, held, lock = threading.Event(), threading.Event(), threading.Lock()
    inside, most = [0], [0]
    update = rt.update_config

    def holding(*args, **kwargs):
        with lock:
            inside[0] += 1
            most[0] = max(most[0], inside[0])
        entered.set()
        held.wait(5)  # the first save holds its thread a moment
        try:
            return update(*args, **kwargs)
        finally:
            with lock:
                inside[0] -= 1

    monkeypatch.setattr(rt, "update_config", holding)
    answers: list[httpx.Response] = []
    with _client(rt) as c:
        etag = c.get(f"{API}/config").json()["etag"]  # two saves of one page, as two tabs read alike

        def save(profile: str) -> None:
            answers.append(c.put(f"{API}/config", json={"profile": profile, "etag": etag}, headers=UI))

        saves = [threading.Thread(target=save, args=(profile,)) for profile in ("max", "secrets")]
        for one in saves:
            one.start()
        assert entered.wait(5)
        time.sleep(0.5)  # the other save came meanwhile: it waits for its turn
        held.set()
        for one in saves:
            one.join(10)
    # One thread at a time; the save that comes second, made from the settings the first changed, is refused as stale.
    assert sorted(a.status_code for a in answers) == [200, 409] and most[0] == 1
    assert [a.json().get("reason") for a in answers if a.status_code == 409] == ["settings_stale"]


def test_a_save_its_store_does_not_answer_says_when_to_try_again(space, monkeypatch):
    rt, settings = space
    rt.gateway.work = in_work

    def down(own):
        raise OSError("the settings of u_emilycarte could not be read or kept in the database (PoolTimeout)")

    monkeypatch.setattr(settings, "_keep_own", down)
    with _client(rt) as c:
        saved = _put(c, "/config", {"profile": "max"})
        named = c.post("/_latet/admin/destinations", json={"destinations": [{"url": "https://gw.acme.com"}]})
    for answer in (saved, named):
        assert (answer.status_code, answer.json()["reason"]) == (503, "settings_unavailable"), answer.text
        assert answer.headers["retry-after"] == "1" and "PoolTimeout" not in answer.text
    assert rt.config.profile.value != "max"


# --- the etag (T9.7, T9.18) -------------------------------------------------------------------------------------------


def test_a_second_save_from_one_reading_is_refused_and_nothing_of_it_applies(space):
    rt, _ = space
    with _client(rt) as c:
        read = c.get(f"{API}/config").json()
        first = c.put(f"{API}/config", json={"profile": "max", "etag": read["etag"]}, headers=UI)
        second = c.put(f"{API}/config", json={"profile": "secrets", "etag": read["etag"]}, headers=UI)
        fresh = c.get(f"{API}/config").json()
    assert first.status_code == 200 and fresh["etag"] != read["etag"] and fresh["profile"] == "max"
    assert (second.status_code, second.json()["reason"]) == (409, "settings_stale") and rt.config.profile.value == "max"


def test_a_page_read_before_a_reapplication_is_stale_for_the_settings_and_for_a_projects_own(space):
    rt, settings = space
    with _client(rt) as c:
        project = c.post(f"{API}/projects", json={"path": "/home/emily/billing-api"}, headers=UI).json()
        etag = c.get(f"{API}/config").json()["etag"]
        _shared_now(rt, settings, SHARED.model_copy(update={"terms": [*SHARED.terms, TermConfig(value="Atlas")]}))
        saved = c.put(f"{API}/config", json={"profile": "max", "etag": etag}, headers=UI)
        own = c.put(f"{API}/projects/{project['id']}/personal", json={"profile": "max", "etag": etag}, headers=UI)
    for refused in (saved, own):
        assert refused.status_code == 409 and refused.json()["reason"] == "settings_stale", refused.text
    assert rt.config.profile.value != "max" and rt.config.projects == []


def test_an_etag_that_is_no_text_is_refused_and_any_text_is_a_mismatch_never_an_error(space):
    rt, _ = space
    with _client(rt) as c:
        project = c.post(f"{API}/projects", json={"path": "/home/emily/billing-api"}, headers=UI).json()
        for path in ("/config", f"/projects/{project['id']}/personal"):
            accent = c.put(f"{API}{path}", json={"profile": "max", "etag": chr(0xE9)}, headers=UI)
            surrogate = json.dumps({"profile": "max", "etag": chr(0xD800)}).encode()  # JSON's escape of it
            lone = c.put(f"{API}{path}", content=surrogate, headers={**UI, "content-type": "application/json"})
            number = c.put(f"{API}{path}", json={"profile": "max", "etag": 5}, headers=UI)
            assert (accent.status_code, lone.status_code, number.status_code) == (409, 409, 400), path
    assert rt.config.profile.value != "max"


def test_the_etag_is_compared_in_constant_time_over_bytes(space, monkeypatch):
    rt, _ = space
    compared: list[tuple[type, type]] = []
    real = hmac.compare_digest

    def spied(a, b):
        compared.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(rest.hmac, "compare_digest", spied)
    with _client(rt) as c:
        etag = c.get(f"{API}/config").json()["etag"]
        assert c.put(f"{API}/config", json={"profile": "max", "etag": etag}, headers=UI).status_code == 200
    assert compared == [(bytes, bytes)]


def test_the_etag_is_the_digest_of_the_settings_the_view_shows(space, monkeypatch):
    """The view, its etag and the shared items come from one reading of the settings in use: a change applied between
    two readings would give an etag the page's save could not be judged by."""
    rt, _ = space
    before, after = rt.config, rt.config.model_copy(update={"profile": "max"})
    reads: list[bool] = []

    def config(self):
        reads.append(True)
        return before if len(reads) == 1 else after  # applied right after the first reading

    monkeypatch.setattr(Runtime, "config", property(config, lambda self, value: None), raising=False)
    with _client(rt) as c:
        view = c.get(f"{API}/config").json()
    assert view["profile"] == before.profile.value and view["etag"] == digest(before)


def test_a_computers_dashboard_answers_its_etag_and_refuses_a_stale_save(tmp_path):
    rt = make_runtime(
        Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "d", config_file=tmp_path / "c.yaml"
    )
    token = {"x-latet-token": rt.gateway.bridge_token}
    try:
        with TestClient(create_app(rt.gateway), base_url=f"http://127.0.0.1:{rt.gateway.port}") as c:
            code = c.post("/_latet/admin/login-code", headers=token).json()["code"]
            c.get(f"/_latet/ui/login?code={code}", follow_redirects=False)
            etag = c.get(f"{API}/config").json()["etag"]
            assert etag == digest(rt.config)
            assert c.put(f"{API}/config", json={"history": "metadata", "etag": etag}, headers=UI).status_code == 200
            stale = c.put(f"{API}/config", json={"history": "off", "etag": etag}, headers=UI)
            assert c.put(f"{API}/config", json={"retention_days": 7}, headers=UI).status_code == 200  # none: as ever
    finally:
        rt.close()
    assert stale.status_code == 409 and stale.json()["reason"] == "settings_stale" and rt.config.history == "metadata"


@pytest.mark.parametrize("body", [{"profile": "max"}, {"profile": "max", "etag": None}], ids=["none", "null"])
def test_a_computers_own_settings_for_a_project_are_saved_without_an_etag_as_ever(tmp_path, body):
    """On a computer, where only a server refuses a save that names no etag (T9.18's recommended form, Task 11): your
    own settings for a project saved without one are kept, as before; one from an older read is still refused."""
    rt = make_runtime(
        Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "d", config_file=tmp_path / "c.yaml"
    )
    token = {"x-latet-token": rt.gateway.bridge_token}
    try:
        with TestClient(create_app(rt.gateway), base_url=f"http://127.0.0.1:{rt.gateway.port}") as c:
            code = c.post("/_latet/admin/login-code", headers=token).json()["code"]
            c.get(f"/_latet/ui/login?code={code}", follow_redirects=False)
            project = c.post(f"{API}/projects", json={"path": str(tmp_path / "billing-api")}, headers=UI).json()
            read = c.get(f"{API}/config").json()["etag"]
            saved = c.put(f"{API}/projects/{project['id']}/personal", json=body, headers=UI)
            stale = c.put(
                f"{API}/projects/{project['id']}/personal", json={"profile": "secrets", "etag": read}, headers=UI
            )
    finally:
        rt.close()
    assert saved.status_code == 200, saved.text
    assert [entry.profile for entry in rt.config.projects] == ["max"]  # kept, and the older read refused
    assert (stale.status_code, stale.json()["reason"]) == (409, "settings_stale")


@pytest.mark.parametrize("body", [{"profile": "max"}, {"profile": "max", "etag": None}], ids=["none", "null"])
def test_a_save_on_a_server_that_names_no_etag_is_refused_as_one_from_an_older_page(space, body):
    """T9.18's recommended form, made with Task 11 once the dashboard sends the etag: on a server, where the dashboard
    and the server ship together, a save that names no etag is refused as stale (409, ``settings_stale``), for the
    settings and for a project's own, nothing of it applied: a page opened before an upgrade reads the settings again
    rather than saving what it read over them. Its words name the field, for whoever wrote the call."""
    rt, settings = space
    with _client(rt) as c:
        project = c.post(f"{API}/projects", json={"path": "/home/emily/billing-api"}, headers=UI).json()
        before, own = rt.config, dict(settings.own)
        for path in ("/config", f"/projects/{project['id']}/personal"):
            refused = c.put(f"{API}{path}", json=body, headers=UI)
            assert (refused.status_code, refused.json()["reason"]) == (409, "settings_stale"), path
            assert "etag" in refused.json()["error"], path
        assert rt.config == before and settings.own == own
        assert _put(c, "/config", {"profile": "max"}).status_code == 200  # with the etag of a page read now
    assert rt.config.profile.value == "max"


def test_more_projects_of_her_own_than_a_server_keeps_are_refused_with_their_reason(space, monkeypatch):
    """Her settings for one project more than a server keeps for one identity: refused (409) with a reason the dashboard
    words in her language (T11.5), as the other refusals of what settings hold are; nothing of them applied."""
    rt, _ = space
    monkeypatch.setattr(shared_module, "PROJECTS_LIMIT", 1)
    with _client(rt) as c:
        first, second = (
            c.post(f"{API}/projects", json={"path": f"/home/emily/{name}"}, headers=UI).json()["id"]
            for name in ("billing-api", "web-shop")
        )
        assert _put(c, f"/projects/{first}/personal", {"profile": "max"}).status_code == 200
        refused = _put(c, f"/projects/{second}/personal", {"profile": "max"})
    assert (refused.status_code, refused.json()["reason"]) == (409, "settings_too_many_projects")
    assert "web-shop" not in refused.text and [p.path for p in rt.config.projects] == ["/home/emily/billing-api"]


def test_the_view_names_the_host_name_of_each_of_her_computers(space):
    """T11.4: on a server the view gives the identity's other host names beside its host name (each of her computers
    sends its own), which her dashboard shows."""
    rt, _ = space
    for sent in (IdentityConfig(hostname="emily-laptop"), IdentityConfig(hostname="emily-desktop")):
        rt.update_config(
            lambda config, sent=sent: config.model_copy(update={"identity": merged_identity(config.identity, sent)})
        )
    with _client(rt) as c:
        identity = c.get(f"{API}/config").json()["identity"]
    assert (identity["hostname"], identity["hostnames"]) == ("emily-laptop", ["emily-desktop"])


# --- what is set for everyone, off the event loop (T9.6) --------------------------------------------------------------


def test_the_clis_other_calls_refuse_by_themselves_on_a_server(space, monkeypatch):
    rt, _ = space
    issued, decided, read = [], [], []
    change = rt.propose(rt.config.model_copy(update={"allow_values": [*rt.config.allow_values, W]}), "file")
    assert change is not None
    monkeypatch.setattr(rt.gateway.dashboard, "issue_code", lambda: issued.append(True) or "x")
    monkeypatch.setattr(rt, "decide_change", lambda *args: decided.append(args) or "approved")
    monkeypatch.setattr(rt.built.vault, "by_original", lambda value: read.append(value))
    calls = [
        ("POST", "/_latet/admin/login-code", None),
        ("POST", "/_latet/admin/reload", None),
        ("POST", f"/_latet/admin/changes/{change.id}", {"action": "approve"}),
        ("GET", f"/_latet/admin/changes/{change.id}", None),
        ("POST", "/_latet/admin/known", {"values": ["emily"]}),
    ]
    with _client(rt) as c:
        for method, path, body in calls:
            refused = c.request(method, path, json=body)
            assert refused.status_code == 409 and "Latet server" in refused.json()["error"], path
    assert issued == decided == read == [] and [c.id for c in rt.mediator.approvals.waiting_changes()] == [change.id]


# --- nothing under /_latet reaches a space's catch-all routes (T9.15) -------------------------------------------------


def test_nothing_under_latet_reaches_the_catch_all_routes_of_a_space_or_of_a_computer(space, tmp_path, monkeypatch):
    rt, _ = space
    handled, relayed = [], []
    monkeypatch.setattr(forward.Forwarder, "handle", lambda self, request: handled.append(request.url.path))
    monkeypatch.setattr(ws, "relay", lambda websocket, *args, **kwargs: relayed.append(websocket.url.path))
    seen = _upstream(rt)
    computer = make_runtime(
        Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "d", config_file=tmp_path / "c.yaml"
    )
    here = f"127.0.0.1:{computer.gateway.port}"
    try:
        for app, base, host in ((as_identity(create_app(rt.gateway), EMILY.id), URL, HOST),
                                (create_app(computer.gateway), f"http://{here}", {"host": here})):  # fmt: skip
            with TestClient(app, base_url=base) as c:
                for method, path in (("GET", "/_latet/engine/v1/mask"), ("PUT", "/_latet/admin/destinations"),
                                     ("PATCH", "/_latet/api/v1/config"), ("GET", "/_latet")):  # fmt: skip
                    assert c.request(method, path).status_code == 404, (method, path)
                with (
                    pytest.raises(WebSocketDisconnect) as closed,
                    c.websocket_connect("/_latet/engine/v1/ping", headers=host),
                ):
                    pass
                assert closed.value.code == 4404
    finally:
        computer.close()
    assert handled == relayed == seen == []


# --- the stats' bound (T9.8) ------------------------------------------------------------------------------------------


def _summaries(n):
    return [{"ts": 1_790_000_000 + i, "agent": "codex", "flags": [], "new_masked": 1} for i in range(n)]


@pytest.mark.parametrize(("most", "read", "partial"), [(3, 3, True), (3, 2, False)])
def test_the_stats_say_when_they_count_the_latest_exchanges_of_the_period_alone(
    space, monkeypatch, most, read, partial
):
    rt, _ = space
    monkeypatch.setattr(rest, "STATS_MOST", most)
    monkeypatch.setattr(rt.exchanges, "summaries_parts", lambda since, asked: iter([_summaries(read)]))
    with _client(rt) as c:
        stats = c.get(f"{API}/stats?days=90").json()
    assert sum(day["exchanges"] for day in stats["by_day"]) == read
    assert (stats.get("partial"), stats.get("latest")) == ((True, most) if partial else (None, None))


def test_a_projects_own_settings_carry_the_etag_beside_them(space):
    rt, _ = space
    with _client(rt) as c:
        project = c.post(f"{API}/projects", json={"path": "/home/emily/billing-api"}, headers=UI).json()
        etag = c.get(f"{API}/config").json()["etag"]
        saved = c.put(f"{API}/projects/{project['id']}/personal", json={"profile": "max", "etag": etag}, headers=UI)
    assert saved.status_code == 200 and rt.config.projects == [ProjectEntry(path=project["root"], profile="max")]
