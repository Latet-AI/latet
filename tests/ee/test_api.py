"""The Enterprise routes of the dashboard's API, a server's (a computer has none: tests/ee/test_computer.py): behind the
dashboard's sign-in and its checks, the license shown to everyone on the server and changed by its administrators
only; and the gates a route that needs a feature goes through until a license switches it on."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig
from latet.ee.api import license_routes, licensed_route, managing_route, serving_route
from latet.ee.license import Licensing
from latet.gateway.app import create_app
from latet.runtime import make_runtime

from .editions import memory_edition
from .licenses import TRIAL, Clock, MemoryLicenses, at
from .test_license import _licensing

API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}
LASTING = {"expires": "2099-12-31"}


@pytest.fixture
def server_dashboard(tmp_path, monkeypatch, licensed):
    """A real dashboard (``create_app``: its sign-in, its origin and Fetch-Metadata checks, its catch-all) mounting a
    server's Enterprise Edition, its license in a store in memory (no PostgreSQL), signed in as a person signs in; the
    client and the store."""
    edition, store, _ = memory_edition(monkeypatch, tmp_path)
    store.write(licensed.make(**LASTING))
    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=edition)
    try:
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            code = client.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token})
            assert client.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False).status_code == 303
            yield client, store
    finally:
        rt.close()
        edition.close()


@pytest.mark.parametrize(
    ("method", "route"),
    [("GET", "license"), ("HEAD", "license"), ("PUT", "license"), ("DELETE", "license"), ("GET", "policy"),
     ("HEAD", "policy")],
)  # fmt: skip
def test_the_enterprise_routes_are_behind_the_dashboards_sign_in_and_its_checks(server_dashboard, method, route):
    client, store = server_dashboard
    url = f"{API}/ee/{route}"
    reads = method in ("GET", "HEAD")
    signed_in = client.request(method, url, headers=UI)
    if reads:
        assert signed_in.status_code == 200, signed_in.text
    else:  # no route changes a license from a person's dashboard: the catch-all's 404, behind the sign-in too
        assert signed_in.status_code == 404 and signed_in.json() == {"error": "not found"}
    # Each refusal is the sign-in's own (its words: a body answered to HEAD is not sent), never a route's.
    cross_site = client.request(method, url, headers={**UI, "sec-fetch-site": "cross-site"})
    assert cross_site.status_code == 403 and (method == "HEAD" or cross_site.json() == {"error": "cross-site request"})
    foreign = client.request(method, url, headers={**UI, "origin": "https://example.com"})
    assert foreign.status_code == 403 and (method == "HEAD" or foreign.json() == {"error": "origin not allowed"})
    client.cookies.clear()  # no session
    for headers in (UI, {}):
        unsigned = client.request(method, url, headers=headers)
        assert unsigned.status_code == 401 and (method == "HEAD" or unsigned.json() == {"error": "unauthorized"})
    assert store.version == 1 and store.data is not None  # the license as it was installed


def test_the_dashboard_serves_the_license_to_read_only(tmp_path, monkeypatch, licensed):
    edition, store, _ = memory_edition(monkeypatch, tmp_path)
    store.write(licensed.make(expires="2099-12-31"))
    guarded = []
    dashboard = SimpleNamespace(guarded=lambda h: guarded.append(h) or h, rt=SimpleNamespace(identity=None),
                                _in_pool=asyncio.to_thread)  # fmt: skip
    [route] = license_routes(edition, dashboard)
    assert isinstance(route, Route)
    assert route.methods == {"GET", "HEAD"} and len(guarded) == 1  # behind the dashboard's sign-in
    with TestClient(Starlette(routes=[route])) as client:
        assert client.get(f"{API}/ee/license").json()["license"]["licensee"] == "Acme Corp"
        for method in ("PUT", "DELETE"):
            assert client.request(method, f"{API}/ee/license", json={"license": "{}"}).status_code == 405
    assert store.read() is not None and store.version == 1  # nothing changed


def _server_page(tmp_path, monkeypatch, clock, ran=None):
    """The License routes of a server's edition, as a person's dashboard there mounts them: 120 users counted, its
    license in a store in memory (tests/ee/test_server.py has a server's own). ``ran``: what the dashboard's threads
    (``Dashboard._in_pool``) were given."""
    edition, store, _ = memory_edition(monkeypatch, tmp_path, clock=clock, count_users=lambda: 120)

    async def in_pool(work, *args, **kwargs):
        if ran is not None:
            ran.append(work)
        return await asyncio.to_thread(work, *args, **kwargs)

    dashboard = SimpleNamespace(guarded=lambda handler: handler, rt=SimpleNamespace(identity=None), _in_pool=in_pool)
    return TestClient(Starlette(routes=license_routes(edition, dashboard))), edition, store


def test_on_a_server_a_person_sees_the_license_without_its_headcount_and_changes_nothing(
    tmp_path, monkeypatch, licensed
):
    ran = []
    page, _edition, store = _server_page(tmp_path, monkeypatch, time.time, ran)
    store.write(licensed.make(users=100, **LASTING))
    with page as client:
        shown = client.get(f"{API}/ee/license").json()
        assert len(ran) == 1  # one job in the dashboard's bounded threads, never the default executor
        assert shown["license"]["users"] == 100 and shown["source"] == "server" and "changes" not in shown
        # How many people and API keys the server has, and since when more than bought (which the day of the pause
        # would give back), are its administrators' (E3): the notice stays.
        assert shown["users_in_use"] is None and shown["over_since"] is None and shown["pauses_on"] is None
        assert shown["state"] == "grace" and "users-over" in shown["notices"]
        assert client.put(f"{API}/ee/license", json={"license": "{}"}, headers=UI).status_code == 405
        assert client.delete(f"{API}/ee/license", headers=UI).status_code == 405
    assert store.version == 1 and store.data is not None  # the license as it was installed


def test_on_a_server_a_person_sees_the_day_the_licenses_end_pauses_what_produces(tmp_path, monkeypatch, licensed):
    page, _, store = _server_page(tmp_path, monkeypatch, lambda: at("2027-11-05"))
    store.write(licensed.make(users=100))  # its last day 2027-10-31
    with page as client:
        shown = client.get(f"{API}/ee/license").json()
        assert (shown["state"], shown["pauses_on"], shown["users_in_use"], shown["over_since"]) == (
            "grace", "2027-12-01", None, None
        )  # fmt: skip


def test_a_route_that_needs_a_feature_answers_403_until_a_license_switches_it_on(licensed):
    licensing = _licensing(MemoryLicenses(), licensed)

    async def report(request):
        return JSONResponse({"report": True})

    app = Starlette(routes=[Route("/report", licensed_route(licensing, "reports", report, run=asyncio.to_thread))])
    with TestClient(app) as client:
        refused = client.get("/report")
        assert refused.status_code == 403 and refused.json()["feature"] == "reports"
        assert "needs a Latet Enterprise license" in refused.json()["error"]
        licensing.install(licensed.make(features=["locked-rules"], **LASTING))
        assert client.get("/report").status_code == 403
        licensing.install(licensed.make(features=["reports"], **LASTING))
        assert client.get("/report").json() == {"report": True}


def test_a_licensed_route_reads_and_never_changes_in_any_standing(licensed):
    """L-A: ``licensed_route`` is for reading what protects, which answers whatever the standing (a trial that ended,
    a pause, a file that cannot be used): any other method is refused, the handler never called; a change goes
    through ``managing_route``."""
    clock = Clock(at("2026-11-01"))
    store = MemoryLicenses()
    licensing = _licensing(store, licensed, clock)
    seen = []

    async def floor(request):
        seen.append(request.method)
        return JSONResponse({"level": 3})

    route = licensed_route(licensing, "locked-rules", floor, run=asyncio.to_thread)
    app = Starlette(routes=[Route("/floor", route, methods=["GET", "HEAD", "PUT", "POST", "DELETE"])])
    with TestClient(app) as client:
        licensing.install(licensed.make(**TRIAL))
        for at_moment in (at("2026-11-01"), at("2026-12-03")):  # valid, then a trial that ended
            clock.now = at_moment
            assert client.get("/floor").json() == {"level": 3} and client.head("/floor").status_code == 200
            for method in ("PUT", "POST", "DELETE"):
                refused = client.request(method, "/floor")
                assert refused.status_code == 405 and refused.headers["allow"] == "GET, HEAD"
                assert "only reads" in refused.json()["error"]
        store.data = b"not a license"  # licensed stays on (M1): still read-only
        assert client.get("/floor").json() == {"level": 3} and client.put("/floor").status_code == 405
    assert seen == ["GET", "HEAD", "GET", "HEAD", "GET"]


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_a_body_holding_what_json_has_no_number_for_is_refused(constant):
    """M4: Python's json reads NaN and the infinities, which JSON has not: a body holding one is refused (400), as a
    body that is not JSON, never carried on to a store that cannot keep it."""
    from latet.ee.admin import _section_of
    from latet.ee.api import license_of

    refused = license_of(f'{{"license": "a license", "x": {constant}}}'.encode())
    assert not isinstance(refused, str) and refused.status_code == 400
    assert _section_of(f'{{"level": {constant}}}'.encode()) is None
    assert _section_of(f'{{"note": [{constant}]}}'.encode()) is None
    assert license_of(b'{"license": "a license", "x": 1.5}') == "a license"  # a number JSON has is read
    assert _section_of(b'{"level": 1.5}') == {"level": 1.5}


@pytest.mark.parametrize("number", ["1e999", "-1e999", "1.5e400"])
def test_a_number_too_large_for_a_float_is_refused_not_read_as_an_infinity(number):
    """Round 2, #4: JSON's 1e999 is valid JSON, which Python reads as an infinity: refused as one (400)."""
    from latet.ee.admin import _section_of
    from latet.ee.api import json_body, license_of

    with pytest.raises(ValueError, match="too large for a float"):
        json_body(f'{{"n": {number}}}'.encode())
    refused = license_of(f'{{"license": "a license", "n": {number}}}'.encode())
    assert not isinstance(refused, str) and refused.status_code == 400
    assert _section_of(f'{{"level": {number}}}'.encode()) is None
    assert json_body(b'{"n": 1e300, "m": -0.5}') == {"n": 1e300, "m": -0.5}


def test_the_gates_read_the_license_in_the_threads_they_are_given(licensed):
    licensing = _licensing(MemoryLicenses(), licensed)
    licensing.install(licensed.make(**LASTING))
    ran = []

    async def in_pool(work, *args, **kwargs):  # a dashboard's bounded threads, or the administration's
        ran.append(work.__name__)
        return await asyncio.to_thread(work, *args, **kwargs)

    async def answer(request):
        return JSONResponse({"done": True})

    routes = [
        Route("/report", licensed_route(licensing, "reports", answer, run=in_pool)),
        Route("/policy", managing_route(licensing, "locked-rules", answer, run=in_pool), methods=["PUT"]),
        Route("/decide", serving_route(licensing, "approvals", answer, run=in_pool), methods=["POST"]),
    ]
    with TestClient(Starlette(routes=routes)) as client:
        for answered in (client.get("/report"), client.put("/policy"), client.post("/decide")):
            assert answered.json() == {"done": True}
    assert ran == ["require", "require", "change_refused", "require_serving"]


@pytest.mark.parametrize("gate", [licensed_route, managing_route, serving_route])
def test_a_gate_is_never_made_without_the_threads_it_reads_in(gate):
    """No default: a later part's route on a server never falls back to the default executor without saying so."""

    async def answer(request):
        return JSONResponse({})

    with pytest.raises(TypeError, match="run"):
        gate(Licensing(MemoryLicenses()), "reports", answer)


def test_a_route_that_decides_says_the_trial_ended_and_decides_nothing(licensed):
    clock = Clock(at("2026-11-01"))
    licensing = _licensing(MemoryLicenses(), licensed, clock)
    done = []

    async def decide(request):
        done.append(True)
        return JSONResponse({"decided": True})

    app = Starlette(
        routes=[
            Route("/decide", serving_route(licensing, "approvals", decide, run=asyncio.to_thread), methods=["POST"])
        ]
    )
    with TestClient(app) as client:
        assert client.post("/decide").json()["feature"] == "approvals"  # no license: 403, as licensed_route
        licensing.install(licensed.make(**TRIAL))
        assert client.post("/decide").json() == {"decided": True}
        clock.now = at("2026-12-03")
        refused = client.post("/decide")
        assert refused.status_code == 403 and refused.json()["code"] == "trial-ended"
        assert "trial ended on 2026-12-02" in refused.json()["error"] and done == [True]


def test_a_route_that_manages_settings_still_reads_after_a_trial_ended_and_changes_nothing(licensed):
    clock = Clock(at("2026-11-01"))
    licensing = _licensing(MemoryLicenses(), licensed, clock)
    seen = []

    async def policy(request):
        seen.append(request.method)
        return JSONResponse({"level": 3})

    route = managing_route(licensing, "locked-rules", policy, run=asyncio.to_thread)
    app = Starlette(routes=[Route("/policy", route, methods=["GET", "PUT"])])
    with TestClient(app) as client:
        assert client.get("/policy").json()["feature"] == "locked-rules"  # no license: 403, as licensed_route
        licensing.install(licensed.make(**TRIAL))
        assert client.get("/policy").json() == {"level": 3} and client.put("/policy").json() == {"level": 3}
        clock.now = at("2026-12-03")
        assert client.get("/policy").json() == {"level": 3}  # read-only: what was set still answers
        refused = client.put("/policy")
        assert refused.status_code == 403 and refused.json()["code"] == "trial-ended"
        assert "trial ended on 2026-12-02" in refused.json()["error"] and seen == ["GET", "PUT", "GET"]


def test_a_route_that_manages_settings_reads_and_changes_nothing_while_the_license_cannot_manage(licensed):
    clock = Clock(at("2026-11-01"))
    store = MemoryLicenses()
    licensing = _licensing(store, licensed, clock)
    seen = []

    async def policy(request):
        seen.append(request.method)
        return JSONResponse({"level": 3})

    route = managing_route(licensing, "locked-rules", policy, run=asyncio.to_thread)
    app = Starlette(routes=[Route("/policy", route, methods=["GET", "PUT", "DELETE"])])
    with TestClient(app) as client:
        licensing.install(licensed.make(expires="2026-12-02"))  # a paid license
        clock.now = at("2027-01-10")  # 39 days past its last day: paused
        assert client.get("/policy").json() == {"level": 3}  # what was set can be read
        refused = client.put("/policy")
        assert refused.status_code == 403 and refused.json()["code"] == "not-manageable", refused.text
        assert refused.json()["feature"] == "locked-rules" and "paused" in refused.json()["error"]
        assert "can be read, not changed" in refused.json()["error"]
        store.data = b"not a license"  # what protects keeps the features its installer verified
        assert client.get("/policy").json() == {"level": 3}
        refused = client.delete("/policy")
        assert refused.status_code == 403 and refused.json()["code"] == "not-manageable"
        assert "cannot be used" in refused.json()["error"] and "can be read, not changed" in refused.json()["error"]
        assert seen == ["GET", "GET"]


def test_a_license_change_that_stops_something_says_each_thing_it_stops():
    """E6's R29: the dashboard's 409 names what the change stops, the policy, the synced terms or both, and asks for
    the same confirmation, `"policy_stops": true`, which covers each."""
    from latet.ee.api import BOTH_STOP, STOPS, TERMS_STOP, stops_said

    assert stops_said({"policy": "stops"}) == STOPS and "synced terms" not in STOPS
    assert stops_said({"terms": "stop"}) == TERMS_STOP and "server's policy" not in TERMS_STOP
    assert stops_said({"policy": "stops", "terms": "stop"}) == BOTH_STOP
    assert all('"policy_stops": true' in words for words in (STOPS, TERMS_STOP, BOTH_STOP))


def test_a_license_change_that_stops_central_approvals_says_so_beside_what_else_stops():
    """The final security review's L1: central approvals stopping is named in the 409, alone or with the policy and the
    synced terms, each with what the license left does not list, and the same confirmation covers it."""
    from latet.ee.api import stops_said

    again = 'send it again with "policy_stops": true to make it'
    assert stops_said({"approvals": "stop"}) == (
        f"not changed: central approvals stop with this change (the license left lists no approvals): {again}"
    )
    assert stops_said({"policy": "stops", "approvals": "stop"}) == (
        "not changed: the server's policy stops being in force and central approvals stop with this change (the "
        f"license left lists neither locked rules nor approvals): {again}"
    )
    assert stops_said({"policy": "stops", "terms": "stop", "approvals": "stop"}) == (
        "not changed: the server's policy stops being in force, the synced terms stop being masked and central "
        "approvals stop with this change (the license left lists none of locked rules, term sync and approvals): "
        f"{again}"
    )
