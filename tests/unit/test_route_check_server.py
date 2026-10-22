"""Task 6's server route contract through the existing authenticated bridge consumer.

Prepared without execution. The scope is test-owned gate output, never request authority.
No future helper or computer-EOP interface is assumed here.
"""

import pytest
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig
from latet.gateway.app import OnServer, create_app

CHECK = "/_latet/engine/v1/route-check"
ORIGIN = "https://latet.acme.lan"
SPACE = ORIGIN + "/u/" + "a" * 26
OTHER_SPACE = ORIGIN + "/u/" + "b" * 26
IDENTITY = "u_emilycarte"
LOCAL = "http://127.0.0.1:5283"


def facts(bridge=SPACE, provider=None, **extra):
    return {
        "session": "task6-route",
        "bridge": bridge,
        "env": {"ANTHROPIC_BASE_URL": provider or f"{bridge}/anthropic"},
        "sources": {},
        "entrypoint": "cli",
        "surface": "terminal",
        **extra,
    }


@pytest.fixture
def server_bridge(tmp_path, request):
    from latet.runtime import make_runtime

    rt = make_runtime(
        Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "data", config_file=tmp_path / "c.yaml"
    )
    origin = getattr(request, "param", ORIGIN)
    rt.gateway.on_server = OnServer(origin, IDENTITY)
    app = create_app(rt.gateway)
    gate = {"latet.identity": IDENTITY, "latet.space": origin + "/u/" + "a" * 26}

    async def scoped(scope, receive, send):
        await app({**scope, **gate}, receive, send)

    try:
        with TestClient(scoped, base_url=origin) as client:
            yield client, gate
    finally:
        rt.close()


@pytest.mark.parametrize("bridge", [SPACE, SPACE + "/", ORIGIN + ":443/u/" + "a" * 26])
def test_server_bridge_accepts_only_its_canonical_space(server_bridge, bridge):
    client, _ = server_bridge
    response = client.post(CHECK, json=facts(bridge, f"{SPACE}/anthropic"))
    assert response.status_code == 200
    assert response.json() == {"ok": True, "reason": None, "toast": None}


@pytest.mark.parametrize("server_bridge", ["https://[::1]", "https://[::1]:8443"], indirect=True)
def test_server_bridge_accepts_a_valid_ipv6_authority(server_bridge):
    client, gate = server_bridge
    response = client.post(CHECK, json=facts(gate["latet.space"]))
    assert response.status_code == 200 and response.json()["ok"] is True


@pytest.mark.parametrize(
    "bridge",
    [
        OTHER_SPACE,
        SPACE + "//",
        SPACE + "/anthropic",
        SPACE + "?TOKEN_QUERY_SENTINEL",
        SPACE + "#TOKEN_FRAGMENT_SENTINEL",
        SPACE.replace("https:", "http:"),
        SPACE.replace("latet.acme.lan", "name:PASSWORD_SENTINEL@latet.acme.lan"),
        SPACE.replace("latet.acme.lan", "latet.acme.lan:99999"),
        SPACE.replace("latet.acme.lan", "latet.acme.lan:bad"),
        SPACE.replace("/u/", "/u/%61"),
        SPACE.replace("/u/", "/u/./"),
        SPACE.replace("latet.acme.lan", "latet.acme.lan\\ATTACK_SENTINEL"),
        SPACE.replace("latet.acme.lan", "[::1"),
        SPACE + "\n",
    ],
)
def test_server_bridge_rejects_noncanonical_or_different_space(server_bridge, bridge):
    client, _ = server_bridge
    response = client.post(CHECK, json=facts(bridge, f"{SPACE}/anthropic"))
    assert response.status_code == 200
    verdict = response.json()
    assert verdict["ok"] is False and verdict["toast"] is None
    for secret in (
        "TOKEN_QUERY_SENTINEL",
        "TOKEN_FRAGMENT_SENTINEL",
        "PASSWORD_SENTINEL",
        "ATTACK_SENTINEL",
        "a" * 26,
        "b" * 26,
    ):
        assert secret not in response.text


@pytest.mark.parametrize(
    "space", [None, 1, {}, "", LOCAL, OTHER_SPACE + "//", "https://foreign.acme.lan/u/" + "a" * 26]
)
def test_server_scope_failure_never_selects_local_semantics(server_bridge, space):
    client, gate = server_bridge
    if space is None:
        gate.pop("latet.space")
    else:
        gate["latet.space"] = space
    response = client.post(CHECK, json=facts(LOCAL, f"{LOCAL}/anthropic"))
    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_client_space_fields_cannot_repair_missing_server_scope(server_bridge):
    client, gate = server_bridge
    gate.pop("latet.space")
    body = facts(LOCAL, f"{LOCAL}/anthropic", space=SPACE, **{"latet.space": SPACE})
    response = client.post(CHECK + "?space=" + SPACE, json=body, headers={"latet.space": SPACE})
    assert response.status_code == 200 and response.json()["ok"] is False


@pytest.mark.parametrize(("entrypoint", "surface"), [("cli", "terminal"), ("cli", "desktop"), ("claude-desktop", None)])
@pytest.mark.parametrize("provider", ["https://api.anthropic.com", "http://127.0.0.1:4000", OTHER_SPACE + "/anthropic"])
def test_server_route_failure_is_never_desktop_advice(server_bridge, entrypoint, surface, provider):
    client, _ = server_bridge
    response = client.post(CHECK, json=facts(provider=provider, entrypoint=entrypoint, surface=surface))
    assert response.status_code == 200
    verdict = response.json()
    assert verdict["ok"] is False and verdict["toast"] is None


@pytest.mark.parametrize("source", ["project", "local"])
def test_server_route_cannot_be_selected_by_a_repository(server_bridge, source):
    client, _ = server_bridge
    response = client.post(CHECK, json=facts(sources={"LATET_BRIDGE_URL": source}))
    assert response.status_code == 200 and response.json()["ok"] is False


def test_each_request_uses_its_own_gate_space(server_bridge):
    client, gate = server_bridge
    assert client.post(CHECK, json=facts()).json()["ok"] is True
    gate["latet.space"] = OTHER_SPACE
    assert client.post(CHECK, json=facts()).json()["ok"] is False
    assert client.post(CHECK, json=facts(OTHER_SPACE)).json()["ok"] is True
    gate["latet.space"] = SPACE
    assert client.post(CHECK, json=facts()).json()["ok"] is True


def test_wrong_scope_identity_cannot_use_server_bridge(server_bridge):
    client, gate = server_bridge
    gate["latet.identity"] = "u_danielokaf"
    assert client.post(CHECK, json=facts()).status_code == 401


@pytest.mark.parametrize("header", [{"origin": "https://page.acme.lan"}, {"sec-fetch-site": "cross-site"}])
def test_server_bridge_keeps_browser_rejection(server_bridge, header):
    client, _ = server_bridge
    assert client.post(CHECK, json=facts(), headers=header).status_code == 401


def test_server_route_bounds_space_urls_before_character_scan_and_parser(monkeypatch):
    """Direct public consumer: HTTP JSON does not preserve this instrumented str subclass."""
    from latet.integrations import routes

    work = []
    real_parts = routes._parts

    class TrackedURL(str):
        def __iter__(self):
            work.append("scan")
            if len(self) > 2048:
                raise AssertionError("oversized space URL reached the character scan")
            return super().__iter__()

    def parsed(value):
        if isinstance(value, TrackedURL):
            work.append("parse")
        return real_parts(value)

    monkeypatch.setattr(routes, "_parts", parsed)
    assert routes.check_claude_route(facts(), SPACE, server_origin=ORIGIN).ok is True
    observed = []
    for field in ("bridge", "space"):
        for size in (2048, 2049):
            work.clear()
            candidate = TrackedURL(SPACE + "a" * (size - len(SPACE)))
            given = facts(provider=f"{SPACE}/anthropic")
            space = SPACE
            if field == "bridge":
                given["bridge"] = candidate
            else:
                space = candidate
            verdict = routes.check_claude_route(given, space, server_origin=ORIGIN)
            assert verdict.ok is False and verdict.toast is None
            observed.append((field, size, list(work)))

    # Exactly the ceiling is still inspected (then rejected by the existing route-key limit).
    # Above it, neither the Python character loop nor the forwarded real parser may run.
    assert observed == [
        ("bridge", 2048, ["scan", "parse"]),
        ("bridge", 2049, []),
        ("space", 2048, ["scan", "parse"]),
        ("space", 2049, []),
    ]


def test_local_route_check_keeps_desktop_and_proxy_behavior():
    from latet.integrations.routes import check_claude_route

    proxy = check_claude_route(facts(LOCAL, "http://127.0.0.1:4000"))
    assert proxy.ok and proxy.toast
    desktop = check_claude_route(facts(LOCAL, "https://api.anthropic.com", surface="desktop"))
    assert desktop.ok and desktop.toast
