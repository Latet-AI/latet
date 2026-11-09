"""Launch-credential safe-sink contracts against existing consumers; no launch registry yet."""

import json
import logging

import pytest

from latet.gateway.capture import Capture
from latet.logline import redacted
from latet.server.logs import JsonFormatter, TextFormatter

from ..thread_pools import frame_locals

# Synthetic canonical base64url encodings of 32 bytes, never issued credentials.
KEY_BODY = "Ab3_" * 10 + "XYw"
LAUNCH_KEY = "latet_lk_" + KEY_BODY
BRIDGE_KEY = "latet_brg_" + "Bc4-" * 10 + "XYw"
QUERY_KEY = "p26a-synthetic-query-credential"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (f"launch diagnostic {LAUNCH_KEY} refused", "launch diagnostic latet_lk_*** refused"),
        (f"x-latet-launch-key: {LAUNCH_KEY}", "x-latet-launch-key: ***"),
        (
            f"POST /_k/{LAUNCH_KEY}/openai/v1/responses?limit=1 400",
            "POST /_k/latet_lk_***/openai/v1/responses?limit=1 400",
        ),
    ],
    ids=("diagnostic", "header", "path"),
)
def test_a_launch_key_never_appears_in_a_redacted_line(line, expected):
    assert len(KEY_BODY) == 43
    assert redacted(line) == expected


@pytest.mark.parametrize("format_name", ["text", "json"])
def test_a_launch_key_never_appears_in_an_ordinary_server_warning(format_name):
    record = logging.LogRecord(
        "latet.gateway.launch",
        logging.WARNING,
        __file__,
        1,
        "launch diagnostic refused: %s; bridge: %s",
        (LAUNCH_KEY, BRIDGE_KEY),
        None,
    )
    assert LAUNCH_KEY in record.getMessage() and BRIDGE_KEY in record.getMessage()
    formatter = TextFormatter() if format_name == "text" else JsonFormatter()
    rendered = formatter.format(record)
    assert "WARNING" in rendered and "launch diagnostic refused" in rendered
    assert "latet.gateway.launch" in rendered and "latet_brg_***" in rendered
    assert BRIDGE_KEY not in rendered, "the existing neighboring-secret redactor must run"
    if format_name == "json":
        line = json.loads(rendered)
        assert (line["level"], line["logger"]) == ("WARNING", "latet.gateway.launch")
        assert line["message"].startswith("launch diagnostic refused: ")
    assert KEY_BODY not in rendered and LAUNCH_KEY not in rendered


def test_a_launch_key_never_appears_in_an_enabled_capture_url(tmp_path):
    directory = tmp_path / "authorized-capture"
    capture = Capture(directory)
    assert capture.enabled
    exchange = capture.exchange(
        "http",
        "openai",
        "POST",
        f"https://upstream.invalid/_k/{LAUNCH_KEY}/openai/v1/responses?key={QUERY_KEY}&limit=1",
    )
    assert exchange is not None
    exchange.response_end()
    [path] = list(directory.glob("*.jsonl"))
    persisted = path.read_text("utf-8")
    opened, ended = [json.loads(line) for line in persisted.splitlines()]
    assert (opened["t"], opened["kind"], opened["upstream"], opened["method"]) == (
        "open",
        "http",
        "openai",
        "POST",
    )
    assert opened["url"].startswith("https://upstream.invalid/_k/")
    assert opened["url"].endswith("/openai/v1/responses?key=***&limit=1")
    assert ended["t"] == "end" and ended["error"] is None
    assert QUERY_KEY not in persisted, "the enabled URL consumer must reach its existing credential redactor"
    assert KEY_BODY not in persisted and LAUNCH_KEY not in persisted


def test_an_enabled_capture_redacts_the_launch_header_and_keeps_authorized_content(tmp_path):
    """Existing green control: the header marker already covers launch-key; ordinary content remains."""
    directory = tmp_path / "authorized-capture"
    exchange = Capture(directory).exchange("ws", "openai", "GET", "wss://upstream.invalid/v1/responses")
    assert exchange is not None
    exchange.request(
        [("x-latet-launch-key", LAUNCH_KEY), ("x-latet-token", BRIDGE_KEY), ("accept", "application/json")],
        b"authorized ordinary conversation",
        None,
    )
    exchange.ws("up", "authorized ordinary frame")
    exchange.response_end()
    [path] = list(directory.glob("*.jsonl"))
    persisted = path.read_text("utf-8")
    opened, requested, frame, ended = [json.loads(line) for line in persisted.splitlines()]
    assert (opened["t"], opened["kind"], opened["method"]) == ("open", "ws", "GET")
    assert requested["headers"] == [
        ["x-latet-launch-key", f"<redacted len={len(LAUNCH_KEY)}>"],
        ["x-latet-token", f"<redacted len={len(BRIDGE_KEY)}>"],
        ["accept", "application/json"],
    ]
    assert requested["body"] == "authorized ordinary conversation"
    assert requested["wire_bytes"] == len(b"authorized ordinary conversation")
    assert (frame["t"], frame["dir"], frame["data"]) == ("ws", "up", "authorized ordinary frame")
    assert ended["t"] == "end" and ended["error"] is None
    assert LAUNCH_KEY not in persisted and KEY_BODY not in persisted and BRIDGE_KEY not in persisted


def test_launch_key_redaction_preserves_neighboring_secrets_and_noncredentials():
    """Existing green controls plus ordinary words must not become credentials merely by their prefix."""
    line = f"bridge {BRIDGE_KEY}; /openai/v1/responses?key={QUERY_KEY}&limit=1"
    assert redacted(line) == "bridge latet_brg_***; /openai/v1/responses?key=***&limit=1"
    for ordinary in ("latet_lk_notes", "latet_code_review", "latet_brg_notes", "POST /openai/v1/responses 200"):
        assert redacted(ordinary) == ordinary


# R124's next source-only batch uses only the existing authenticated Bridge and Projects owners.
@pytest.fixture
async def launch_bridge(tmp_path):
    import httpx
    from starlette.applications import Starlette

    from latet.bridge.api import Bridge
    from latet.config import Config, IdentityConfig, ProjectEntry
    from latet.engine.masker import Masker
    from latet.engine.pipeline import Engine
    from latet.engine.vault import Vault
    from latet.engine.verifier import Verifier
    from latet.gateway.launch_keys import LocalLaunchKeys
    from latet.gateway.mediate import Mediator
    from latet.projects import Projects

    folder = tmp_path / "registered-launch"
    (folder / ".git").mkdir(parents=True)
    (folder / "latet.yaml").write_text("{}\n", encoding="utf-8")
    config = Config(
        ner="off",
        identity=IdentityConfig(enabled=False),
        projects=[ProjectEntry(path=str(folder))],
    )
    vault = Vault(b"\x13" * 32)
    masker = Masker(vault, [])
    engine = Engine(vault, masker)
    engine.scopes = Projects(config, vault)
    mediator = Mediator(engine, Verifier(masker))
    launches = LocalLaunchKeys(b"\x13" * 32)
    bridge = Bridge(mediator, BRIDGE_KEY, launch_keys=launches)
    transport = httpx.ASGITransport(app=Starlette(routes=bridge.routes()))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5283", trust_env=False) as client:
            yield client, mediator, folder
    finally:
        launches.close()
        mediator.close()
        mediator.executor.shutdown(wait=True, cancel_futures=True)
        mediator.bridge_executor.shutdown(wait=True, cancel_futures=True)


async def _registered_launch_folder_is_reachable(client, mediator, folder):
    from latet import __version__
    from latet.projects import project_id

    headers = {"x-latet-token": BRIDGE_KEY}
    ping = await client.get("/_latet/engine/v1/ping", headers=headers)
    assert ping.status_code == 200 and ping.json() == {"ok": True, "version": __version__}
    scoped = await client.post(
        "/_latet/engine/v1/scope",
        headers=headers,
        json={"session": "p26a-registered-project", "cwd": str(folder), "items": []},
    )
    assert scoped.status_code == 200 and scoped.json() == {"added": 0}
    session = mediator.engine.session("cc:p26a-registered-project")
    assert (session.project_id, session.cwd) == (project_id(str(folder)), str(folder))
    project = mediator.engine.scopes.get(session.project_id)
    assert project is not None and project.root == str(folder)
    assert project.personal is not None and project.personal.path == str(folder)
    assert project.repo_file == folder / "latet.yaml" and project.notices == []
    assert project.scope is None, "empty registered project settings must not construct a detector/model scope"


def _opened_launch(response):
    import base64
    import re

    assert response.status_code == 200, "the authenticated launch-open capability must exist"
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert isinstance(payload, dict) and set(payload) == {"key", "launch_id"}
    key, launch_id = payload["key"], payload["launch_id"]
    assert isinstance(key, str) and re.fullmatch(r"latet_lk_[A-Za-z0-9_-]{43}", key)
    raw = base64.urlsafe_b64decode(key.removeprefix("latet_lk_") + "=")
    assert len(raw) == 32
    assert base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") == key.removeprefix("latet_lk_")
    assert isinstance(launch_id, str) and re.fullmatch(r"[0-9a-f]{32}", launch_id)
    return key, launch_id


async def test_a_registered_launch_folder_reaches_the_existing_authenticated_bridge(launch_bridge):
    """Green control: real ping, JSON guard and project-file/owner-registration resolution work."""
    client, mediator, folder = launch_bridge
    await _registered_launch_folder_is_reachable(client, mediator, folder)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-latet-token": "p26a-wrong-synthetic-token"},
        {"x-latet-token": BRIDGE_KEY, "origin": "https://browser.invalid"},
        {"x-latet-token": BRIDGE_KEY, "sec-fetch-site": "cross-site"},
    ],
    ids=("missing-token", "wrong-token", "browser-origin", "cross-site"),
)
async def test_existing_bridge_authentication_rejects_untrusted_launch_setup(launch_bridge, headers):
    """Green controls use the existing guarded route, without pretending a launch registry exists."""
    client, mediator, folder = launch_bridge
    ping = await client.get("/_latet/engine/v1/ping", headers={"x-latet-token": BRIDGE_KEY})
    assert ping.status_code == 200 and ping.json()["ok"] is True
    denied = await client.post(
        "/_latet/engine/v1/scope",
        headers=headers,
        json={"session": "p26a-denied-project", "cwd": str(folder), "items": []},
    )
    assert denied.status_code == 401 and denied.json() == {"error": "unauthorized"}
    assert mediator.engine.sessions() == [], "authorization must reject before a session/project is attached"


async def test_launch_open_returns_a_256_bit_key_for_a_registered_folder(launch_bridge):
    client, mediator, folder = launch_bridge
    await _registered_launch_folder_is_reachable(client, mediator, folder)
    response = await client.post(
        "/_latet/engine/v1/launches", headers={"x-latet-token": BRIDGE_KEY}, json={"folder": str(folder)}
    )
    _opened_launch(response)


async def test_two_launch_opens_for_one_folder_have_distinct_keys_and_ids(launch_bridge):
    client, mediator, folder = launch_bridge
    await _registered_launch_folder_is_reachable(client, mediator, folder)
    responses = [
        await client.post(
            "/_latet/engine/v1/launches", headers={"x-latet-token": BRIDGE_KEY}, json={"folder": str(folder)}
        )
        for _ in range(2)
    ]
    assert [response.status_code for response in responses] == [200, 200], "both launch opens must succeed"
    first, second = [_opened_launch(response) for response in responses]
    assert first[0] != second[0], "a second launch must never reuse the first launch key"
    assert first[1] != second[1], "a second launch has its own non-secret identifier"


@pytest.fixture
def local_launches():
    from latet.gateway.launch_keys import LocalLaunchKeys

    owner = LocalLaunchKeys(b"\x17" * 32)
    try:
        yield owner
    finally:
        owner.close()


def _launch_binding(folder=b"/registered", project="a" * 12):
    from latet.gateway.launch_keys import Binding

    return Binding(folder, b"/registered", project)


def _local_open(owner, identity="computer", binding=None):
    ticket = owner.begin_open(identity)
    try:
        return owner.commit_open(ticket, binding or _launch_binding())
    finally:
        owner.abort(ticket)


def _launch_owned_weight(owner):
    import sys

    from latet.gateway import launch_keys

    seen = set()

    def weight(value):
        if id(value) in seen:
            return 0
        seen.add(id(value))
        size = sys.getsizeof(value)
        if isinstance(value, dict):
            return size + sum(weight(k) + weight(v) for k, v in value.items())
        if isinstance(value, (set, tuple, list)):
            return size + sum(weight(v) for v in value)
        if type(value).__module__ == launch_keys.__name__:
            return size + sum(weight(getattr(value, slot)) for slot in value.__slots__ if hasattr(value, slot))
        return size  # shared callback/classes/loop internals are not traversed as registry-owned objects

    return weight(owner)


def test_local_launch_keys_draw_256_bits_once_and_retain_only_private_hashes(local_launches, monkeypatch):
    from latet.gateway import launch_keys

    calls = []
    original = launch_keys.secrets.token_bytes

    def drawn(size):
        calls.append(size)
        return original(size)

    monkeypatch.setattr(launch_keys.secrets, "token_bytes", drawn)
    opened = _local_open(local_launches)
    assert calls.count(32) == 1
    raw_key = opened["key"].encode()
    entry = next(iter(local_launches._entries.values()))
    assert raw_key not in (entry.locator, entry.keyed, entry.identity)
    assert all(len(value) == 32 for value in (entry.locator, entry.keyed, entry.identity))
    assert "latet_lk_" not in repr(local_launches) + repr(entry)
    compared = []
    compare = launch_keys.hmac.compare_digest

    def constant(left, right):
        compared.append((left, right))
        return compare(left, right)

    monkeypatch.setattr(launch_keys.hmac, "compare_digest", constant)
    assert local_launches.verify(opened["key"], "computer").code == "pending"
    assert any(left == entry.keyed and right == entry.keyed for left, right in compared)


def test_local_launch_pending_active_end_and_final_generation_are_distinct(local_launches):
    opened = _local_open(local_launches)
    assert local_launches.verify(LAUNCH_KEY, "computer").code == "unknown"
    assert local_launches.verify(opened["key"], "computer").code == "pending"
    hold = local_launches.reserve_hold(opened["launch_id"], "computer", 0)
    try:
        assert local_launches.verify(opened["key"], "computer").code == "pending"
        assert hold.acknowledge() == 1
        checked = local_launches.verify(opened["key"], "computer")
        assert checked.code == "active" and checked.authority is not None
        assert local_launches.validate(checked.authority)
        assert local_launches.verify(opened["key"], "foreign").code == "mismatch"
        # A claimed project is compared by the launch authority a request asks (``launch_authority.key_project``),
        # never by the registry, which only the gateway's ingress reads.
        assert local_launches.end(opened["launch_id"], "computer")
        assert not local_launches.validate(checked.authority)
        assert local_launches.verify(opened["key"], "computer").code == "ended"
    finally:
        hold.close()
        hold.close()
    assert local_launches.counts == {"entries": 1, "bindings": 0, "holds": 0}


def test_local_launch_replacement_hold_cleanup_cannot_end_new_owner(local_launches):
    opened = _local_open(local_launches)
    first = local_launches.reserve_hold(opened["launch_id"], "computer", 0)
    second = None
    try:
        first.acknowledge()
        second = local_launches.reserve_hold(opened["launch_id"], "computer", 1)
        assert local_launches.counts["holds"] == 2
        assert second.acknowledge() == 2
        first.close()
        assert local_launches.verify(opened["key"], "computer").code == "active"
    finally:
        first.close()
        if second is not None:
            second.close()
    assert local_launches.verify(opened["key"], "computer").code == "ended"


def test_pending_grace_expires_but_a_held_local_launch_has_no_offline_timeout():
    from latet.gateway.launch_keys import LocalLaunchKeys

    now = [0.0]
    owner = LocalLaunchKeys(b"\x19" * 32, clock=lambda: now[0])
    try:
        pending, active = _local_open(owner), _local_open(owner)
        hold = owner.reserve_hold(active["launch_id"], "computer", 0)
        try:
            hold.acknowledge()
            now[0] = 59.999
            assert owner.verify(pending["key"], "computer").code == "pending"
            now[0] = 60.0
            assert owner.verify(pending["key"], "computer").code == "ended"
            now[0] = 1_000_000.0
            assert owner.verify(active["key"], "computer").code == "active"
        finally:
            hold.close()
    finally:
        owner.close()
    assert owner.retained_bytes == 0


def test_local_recovery_fences_entry_and_publication_and_allows_a_true_new_boot(local_launches):
    from latet.gateway.launch_keys import LaunchRefused, LocalLaunchKeys

    snapshot = local_launches.prepare()
    ticket = local_launches.begin_recovery(LAUNCH_KEY, "1" * 32, "computer", **snapshot)
    assert not local_launches.confirmed_exit("1" * 32, "computer", snapshot["epoch"])
    with pytest.raises(LaunchRefused, match="launch-stale"):
        local_launches.commit_recovery(ticket, _launch_binding())
    local_launches.abort(ticket)
    assert local_launches.verify(LAUNCH_KEY, "computer").code == "unknown"
    with pytest.raises(LaunchRefused, match="launch-stale"):
        local_launches.begin_recovery(LAUNCH_KEY, "1" * 32, "computer", **snapshot)
    newer = LocalLaunchKeys(b"\x17" * 32)
    try:
        assert newer.prepare()["epoch"] != snapshot["epoch"]
        fresh = newer.begin_recovery(LAUNCH_KEY, "1" * 32, "computer", **newer.prepare())
        newer.commit_recovery(fresh, _launch_binding())
        newer.abort(fresh)
        hold = newer.reserve_hold("1" * 32, "computer", 0)
        try:
            hold.acknowledge()
            assert newer.verify(LAUNCH_KEY, "computer").code == "active"
        finally:
            hold.close()
        with pytest.raises(LaunchRefused, match="launch-ended"):
            newer.begin_recovery(LAUNCH_KEY, "1" * 32, "computer", **newer.prepare())
    finally:
        newer.close()


def test_confirmed_absent_exit_advances_the_global_fence_and_abort_releases_reservations(local_launches):
    snapshot = local_launches.prepare()
    ticket = local_launches.begin_open("computer")
    assert local_launches.counts == {"entries": 1, "bindings": 1, "holds": 0}
    local_launches.abort(ticket)
    assert local_launches.counts == {"entries": 0, "bindings": 0, "holds": 0}
    assert local_launches.prepare()["retirement"] == snapshot["retirement"]
    assert local_launches.confirmed_exit("f" * 32, "computer", snapshot["epoch"])
    assert local_launches.prepare()["retirement"] == snapshot["retirement"] + 1


@pytest.mark.parametrize("bound", ["identity64", "bindings1024", "entries4096", "holds256"])
def test_real_local_launch_admission_boundaries_do_not_evict_or_leak(local_launches, bound):
    from latet.gateway.launch_keys import LaunchRefused

    holds = []
    if bound == "identity64":
        for _ in range(64):
            _local_open(local_launches)
        before = local_launches.counts
        with pytest.raises(LaunchRefused, match="launch-capacity"):
            _local_open(local_launches)
    elif bound == "bindings1024":
        for index in range(1024):
            _local_open(local_launches, f"identity-{index // 64}")
        before = local_launches.counts
        with pytest.raises(LaunchRefused, match="launch-capacity"):
            _local_open(local_launches, "extra")
    elif bound == "entries4096":
        # A registry full of tombstones forgets its oldest for a new launch (the review's Minor 1): never
        # launch-capacity until a restart; the forgotten key is then claimed. Live launches still fill it.
        keys = []
        for _ in range(4096):
            opened = _local_open(local_launches)
            local_launches.end(opened["launch_id"], "computer")
            keys.append(opened["key"])
        newest = _local_open(local_launches)
        codes = [local_launches.verify(key, "computer").code for key in keys]
        assert codes.count("unknown") == 1 and codes.count("ended") == 4095
        assert local_launches.verify(newest["key"], "computer").code == "pending"
        assert local_launches.counts == {"entries": 4096, "bindings": 1, "holds": 0}
        assert _launch_owned_weight(local_launches) <= local_launches.retained_bytes
        return
    else:
        opened = _local_open(local_launches)
        try:
            for generation in range(256):
                hold = local_launches.reserve_hold(opened["launch_id"], "computer", generation)
                holds.append(hold)
                hold.acknowledge()
            before = local_launches.counts
            with pytest.raises(LaunchRefused, match="launch-capacity"):
                local_launches.reserve_hold(opened["launch_id"], "computer", 256)
            assert local_launches.counts == before
            assert _launch_owned_weight(local_launches) <= local_launches.retained_bytes
        finally:
            for hold in holds:
                hold.close()
        return
    assert local_launches.counts == before
    assert _launch_owned_weight(local_launches) <= local_launches.retained_bytes


def test_local_owner_charges_owned_shapes_and_releases_only_after_detach(local_launches):
    opened = _local_open(local_launches, binding=_launch_binding(b"/registered/" + b"a" * 4084))
    hold = local_launches.reserve_hold(opened["launch_id"], "computer", 0)
    hold.acknowledge()
    assert _launch_owned_weight(local_launches) <= local_launches.retained_bytes
    before = local_launches.retained_bytes
    local_launches.close()
    assert 0 < local_launches.retained_bytes <= before
    hold.close()
    assert local_launches.retained_bytes == 0


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/launches", "POST"),
        ("/launches/prepare", "GET"),
        ("/launches/recover", "POST"),
        ("/launches/" + "0" * 32 + "/hold?generation=0", "GET"),
        ("/launches/" + "0" * 32 + "/end", "POST"),
        ("/launches/" + "0" * 32 + "/exit", "POST"),
    ],
)
async def test_new_launch_controls_keep_existing_bridge_authentication(launch_bridge, path, method):
    client, _, _ = launch_bridge
    response = await client.request(method, "/_latet/engine/v1" + path, headers={"x-latet-token": "wrong"})
    assert response.status_code == 401 and response.json() == {"error": "unauthorized"}


async def test_a_launchs_hold_is_its_own_process_and_no_others(launch_bridge):
    """A hold through the bridge's route: the launched process's, with its own authority (the bridge token and its
    launch's id, at its generation), held (``test_actual_held_launch_connection_acknowledges_and_cleanup_releases``);
    another launch's id refused, and the same launch's without the authority."""
    client, mediator, folder = launch_bridge
    await _registered_launch_folder_is_reachable(client, mediator, folder)
    headers = {"x-latet-token": BRIDGE_KEY}
    opened = (await client.post("/_latet/engine/v1/launches", headers=headers, json={"folder": str(folder)})).json()
    other = "1" * 32 if opened["launch_id"] != "1" * 32 else "2" * 32
    another = await client.get(f"/_latet/engine/v1/launches/{other}/hold?generation=0", headers=headers)
    assert another.status_code == 409 and another.json() == {"error": "launch-mismatch"}
    own = f"/_latet/engine/v1/launches/{opened['launch_id']}/hold?generation=0"
    for anonymous in ({}, {"x-latet-token": "wrong"}):
        refused = await client.get(own, headers=anonymous)
        assert refused.status_code == 401 and refused.json() == {"error": "unauthorized"}
    application = client._transport.app
    bridge = next(route.endpoint.__self__ for route in application.routes if route.path.endswith("/launches/prepare"))
    assert bridge.launch_keys.verify(opened["key"], "computer").code == "pending"  # neither refusal took a hold


async def test_launch_open_refuses_an_arbitrary_resolved_folder_and_keeps_prepare_compatible(launch_bridge, tmp_path):
    client, _, _ = launch_bridge
    headers = {"x-latet-token": BRIDGE_KEY}
    bare = tmp_path / "unregistered"
    bare.mkdir()
    denied = await client.post("/_latet/engine/v1/launches", headers=headers, json={"folder": str(bare)})
    assert denied.status_code == 400 and denied.json() == {"error": "launch-unregistered"}
    prepared = await client.get("/_latet/engine/v1/launches/prepare", headers=headers)
    assert prepared.status_code == 200 and set(prepared.json()) == {"epoch", "retirement"}


@pytest.mark.parametrize("ending", ["cancel", "disconnect", "explicit-end"])
async def test_actual_held_launch_connection_acknowledges_and_cleanup_releases(launch_bridge, ending):
    import asyncio

    client, mediator, folder = launch_bridge
    await _registered_launch_folder_is_reachable(client, mediator, folder)
    opened = (
        await client.post(
            "/_latet/engine/v1/launches", headers={"x-latet-token": BRIDGE_KEY}, json={"folder": str(folder)}
        )
    ).json()
    application = client._transport.app
    bridge = next(route.endpoint.__self__ for route in application.routes if route.path.endswith("/launches/prepare"))
    local_launches = bridge.launch_keys
    acknowledged = asyncio.Event()
    disconnect = asyncio.Event()
    messages = []

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and message.get("more_body"):
            assert local_launches.verify(opened["key"], "computer").code == "active"
            acknowledged.set()

    path = f"/_latet/engine/v1/launches/{opened['launch_id']}/hold"
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "http_version": "1.1",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"generation=0",
        "headers": [(b"x-latet-token", BRIDGE_KEY.encode())],
        "server": ("127.0.0.1", 5283),
        "client": ("127.0.0.1", 123),
        "root_path": "",
        "asgi": {"version": "3.0"},
    }
    task = asyncio.create_task(application(scope, receive, send))
    try:
        await asyncio.wait_for(acknowledged.wait(), 3)
        assert local_launches.verify(opened["key"], "computer").code == "active"
        assert json.loads(messages[1]["body"]) == {"held": True, "generation": 1}
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            if ending == "disconnect":
                disconnect.set()
            else:
                response = await client.post(
                    f"/_latet/engine/v1/launches/{opened['launch_id']}/end",
                    headers={"x-latet-token": BRIDGE_KEY},
                    json={},
                )
                assert response.status_code == 200 and response.json() == {"ended": True}
            await asyncio.wait_for(task, 3)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert local_launches.verify(opened["key"], "computer").code == "ended"
    assert local_launches.counts["holds"] == 0


async def test_real_registered_subfolder_keeps_the_launch_project(launch_bridge, local_launches):
    from latet.bridge.api import Bridge

    client, mediator, folder = launch_bridge
    await _registered_launch_folder_is_reachable(client, mediator, folder)
    child = folder / "subfolder"
    child.mkdir()
    bridge = Bridge(mediator, BRIDGE_KEY, launch_keys=local_launches)
    controls = bridge._launch_controls  # the computer's launch controls, as the bridge was built with them
    root_binding = await mediator.run_bridge(controls._launch_binding, str(folder))
    child_binding = await mediator.run_bridge(controls._launch_binding, str(child))
    assert root_binding.project == child_binding.project
    opened = _local_open(local_launches, binding=root_binding)
    hold = local_launches.reserve_hold(opened["launch_id"], "computer", 0)
    try:
        hold.acknowledge()
        # The product's path (the review's Minor 5): the launch authority a request asks, with the subfolder claimed.
        from latet.engine.pipeline import RequestScope, request_scope
        from latet.gateway.launch_authority import RequestLaunch
        from latet.gateway.local_launch import LOCAL_AUTHORITY, take_local_launch

        scope = {
            "type": "http",
            "path": "/v1/x",
            "raw_path": b"/v1/x",
            "headers": [(b"x-latet-launch-key", opened["key"].encode())],
        }
        launch = RequestLaunch(LOCAL_AUTHORITY, take_local_launch(scope, local_launches, computer=True))
        token = request_scope.set(RequestScope())
        try:
            chosen = launch.decide(None, str(child), legacy_eligible=False, projects=mediator.engine.scopes)
        finally:
            request_scope.reset(token)
        assert chosen.status == "verified" and chosen.project.id == root_binding.project
    finally:
        hold.close()


def test_retirement_exhaustion_keeps_negative_entries_and_stops_recovery(local_launches):
    from latet.gateway.launch_keys import MAX_GENERATION, LaunchRefused

    opened = _local_open(local_launches)
    local_launches._retirement = MAX_GENERATION  # finite scalar edge; count-cap tests above use the real bounds
    assert not local_launches.confirmed_exit(opened["launch_id"], "computer", local_launches.prepare()["epoch"])
    assert local_launches.verify(opened["key"], "computer").code == "ended"
    with pytest.raises(LaunchRefused, match="launch-stale"):
        local_launches.begin_recovery(LAUNCH_KEY, "e" * 32, "computer", **local_launches.prepare())


def test_byte_ceiling_failure_does_not_publish_and_authority_receipts_cannot_be_forged(local_launches):
    from dataclasses import replace

    from latet.gateway.launch_keys import LaunchRefused

    local_launches.ceiling = lambda added: "full" if added else None
    with pytest.raises(LaunchRefused, match="launch-capacity"):
        _local_open(local_launches)
    assert local_launches.counts == {"entries": 0, "bindings": 0, "holds": 0}
    local_launches.ceiling = None
    opened = _local_open(local_launches)
    hold = local_launches.reserve_hold(opened["launch_id"], "computer", 0)
    try:
        hold.acknowledge()
        authority = local_launches.verify(opened["key"], "computer").authority
        assert local_launches.validate(authority)
        assert not local_launches.validate(replace(authority, project="b" * 12))
    finally:
        hold.close()


async def test_a_server_bridge_never_uses_a_supplied_volatile_local_owner(launch_bridge, local_launches):
    import httpx
    from starlette.applications import Starlette

    from latet.bridge.api import Bridge
    from latet.gateway.app import OnServer

    _, mediator, folder = launch_bridge
    bridge = Bridge(
        mediator, BRIDGE_KEY, on_server=OnServer("https://server.invalid", "identity"), launch_keys=local_launches
    )
    application = Starlette(routes=bridge.routes())

    async def authenticated(scope, receive, send):
        await application({**scope, "latet.identity": "identity"}, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=authenticated), base_url="https://server.invalid", trust_env=False
    ) as client:
        response = await client.post("/_latet/engine/v1/launches", json={"folder": str(folder)})
        prepared = await client.get("/_latet/engine/v1/launches/prepare")
    assert response.status_code == 503 and response.json() == {"error": "launch-unavailable"}
    assert prepared.status_code == 503 and prepared.json() == {"error": "launch-unavailable"}
    assert prepared.headers["cache-control"] == "no-store"
    assert local_launches.counts == {"entries": 0, "bindings": 0, "holds": 0}


async def test_authenticated_recovery_is_short_and_keeps_clear_keys_out_of_worker_arguments(launch_bridge, monkeypatch):
    import httpx
    from starlette.applications import Starlette

    from latet.bridge.api import Bridge
    from latet.gateway.launch_keys import LocalLaunchKeys

    client, mediator, folder = launch_bridge
    headers = {"x-latet-token": BRIDGE_KEY}
    opened = (await client.post("/_latet/engine/v1/launches", headers=headers, json={"folder": str(folder)})).json()
    replacement = LocalLaunchKeys(b"\x13" * 32)
    original = mediator.submit_launch
    calls = []

    def metadata_only(fn, *args, **kwargs):
        assert opened["key"] not in repr(args) + repr(kwargs)
        calls.append(fn.__name__)
        return original(fn, *args, **kwargs)

    monkeypatch.setattr(mediator, "submit_launch", metadata_only)
    bridge = Bridge(mediator, BRIDGE_KEY, launch_keys=replacement)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=Starlette(routes=bridge.routes())),
            base_url="http://127.0.0.1:5283",
            trust_env=False,
        ) as recovering:
            prepared = (await recovering.get("/_latet/engine/v1/launches/prepare", headers=headers)).json()
            response = await recovering.post(
                "/_latet/engine/v1/launches/recover",
                headers=headers,
                json={"key": opened["key"], "launch_id": opened["launch_id"], "folder": str(folder), **prepared},
            )
            assert response.status_code == 200 and response.json() == {
                "launch_id": opened["launch_id"],
                "generation": 0,
            }
            assert opened["key"] not in response.text and calls == ["_launch_stage"]
        hold = replacement.reserve_hold(opened["launch_id"], "computer", 0)
        try:
            hold.acknowledge()
            assert replacement.verify(opened["key"], "computer").code == "active"
        finally:
            hold.close()
    finally:
        replacement.close()


async def test_failed_launch_recovery_releases_request_and_actual_future(launch_bridge, monkeypatch):
    import asyncio
    import gc
    from weakref import ref

    import httpx
    from starlette.applications import Starlette

    from latet.bridge.api import Bridge
    from latet.gateway.launch_keys import LocalLaunchKeys

    client, mediator, folder = launch_bridge
    headers = {"x-latet-token": BRIDGE_KEY}
    launch = await client.post("/_latet/engine/v1/launches", headers=headers, json={"folder": str(folder)})
    key, launch_id = _opened_launch(launch)
    del launch
    replacement = LocalLaunchKeys(b"\x13" * 32)
    bridge = Bridge(mediator, BRIDGE_KEY, launch_keys=replacement)
    requests = []
    submitted = []
    original_json, original_work = bridge._launch_json, bridge._launch_controls._launch_work

    async def observed_json(request, fields):
        requests.append(ref(request))
        return await original_json(request, fields)

    def observed_work(*args):
        future = original_work(*args)
        submitted.append(ref(future))
        return future

    async def recover(recovering, prepared):
        response = await recovering.post(
            "/_latet/engine/v1/launches/recover",
            headers=headers,
            json={
                "key": key,
                "launch_id": launch_id,
                "folder": str(folder / "absent-child"),
                **prepared,
            },
        )
        return response.status_code, response.json().get("error")

    monkeypatch.setattr(bridge, "_launch_json", observed_json)
    monkeypatch.setattr(bridge._launch_controls, "_launch_work", observed_work)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=Starlette(routes=bridge.routes())),
            base_url="http://127.0.0.1:5283",
            trust_env=False,
        ) as recovering:
            prepared = (await recovering.get("/_latet/engine/v1/launches/prepare", headers=headers)).json()
            collecting = gc.isenabled()
            gc.disable()
            try:
                status, error = await asyncio.wait_for(recover(recovering, prepared), 3)
                assert (status, error) == (400, "launch-unregistered")
                assert replacement.counts == {"entries": 0, "bindings": 0, "holds": 0}
                barrier = mediator.bridge_executor.submit(lambda: None)
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(barrier)), 3)
                for _ in range(8):
                    await asyncio.sleep(0)
                (request_ref,) = requests
                (future_ref,) = submitted
                request_released = request_ref() is None
                future_released = future_ref() is None
                assert request_released and future_released
            finally:
                if collecting:
                    gc.enable()
    finally:
        replacement.close()


def test_unrelated_retirement_rejects_delayed_recovery_without_ending_the_current_hold(local_launches):
    from latet.gateway.launch_keys import LaunchRefused

    opened = _local_open(local_launches)
    hold = local_launches.reserve_hold(opened["launch_id"], "computer", 0)
    try:
        hold.acknowledge()
        ticket = local_launches.begin_recovery(
            opened["key"], opened["launch_id"], "computer", **local_launches.prepare()
        )
        local_launches.confirmed_exit("d" * 32, "computer", local_launches.prepare()["epoch"])
        with pytest.raises(LaunchRefused, match="launch-stale"):
            local_launches.commit_recovery(ticket, _launch_binding())
        local_launches.abort(ticket)
        assert local_launches.verify(opened["key"], "computer").code == "active"
        fresh = local_launches.begin_recovery(
            opened["key"], opened["launch_id"], "computer", **local_launches.prepare()
        )
        local_launches.commit_recovery(fresh, _launch_binding())
        local_launches.abort(fresh)
    finally:
        hold.close()


async def test_runtime_owns_and_counts_the_local_registry_exactly_once(launch_bridge, local_launches, tmp_path):
    from types import SimpleNamespace
    from typing import cast

    from latet.config import Config, IdentityConfig
    from latet.engine.build import Built
    from latet.gateway.app import GatewayConfig
    from latet.runtime import Runtime
    from latet.state.protocols import VaultStore

    _, mediator, _ = launch_bridge
    engine = mediator.engine
    gateway = GatewayConfig(mediator=mediator, bridge_token=BRIDGE_KEY)
    runtime = Runtime(
        Config(ner="off", identity=IdentityConfig(enabled=False)),
        Built(engine.vault, engine.masker, engine, mediator.verifier),
        cast(VaultStore, SimpleNamespace(close=lambda: None)),  # this witness uses only Runtime.close's store boundary
        gateway,
        tmp_path,
        launch_keys=local_launches,
    )
    try:
        assert gateway.launch_keys is local_launches
        before = runtime.request_retained_bytes
        _local_open(local_launches)
        assert runtime.request_retained_bytes - before == 1024 + 16384
    finally:
        runtime.close()
    assert runtime.request_retained_bytes == 0


def _observe_local_abort(owner, monkeypatch):
    import asyncio

    loop = asyncio.get_running_loop()
    aborted = asyncio.Event()
    original = type(owner).abort

    def abort(instance, ticket):
        result = original(instance, ticket)
        if instance is owner:
            loop.call_soon_threadsafe(aborted.set)
        return result

    monkeypatch.setattr(type(owner), "abort", abort)
    return aborted


async def test_cancelled_launch_open_keeps_worker_reservation_until_actual_completion(launch_bridge, monkeypatch):
    import asyncio
    import threading

    client, _, folder = launch_bridge
    application = client._transport.app
    bridge = next(route.endpoint.__self__ for route in application.routes if route.path.endswith("/launches/prepare"))
    owner = bridge.launch_keys
    aborted = _observe_local_abort(owner, monkeypatch)
    controls = bridge._launch_controls  # the computer's launch controls, as the bridge was built with them
    original_binding, original_view = controls._launch_binding, controls._launch_view
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    workers = []

    def blocked(value):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(3), "the test must release its one bounded worker barrier"
        return original_binding(value)

    def tracked(work):
        work = original_view(work)
        workers.append(work)
        return work

    monkeypatch.setattr(controls, "_launch_binding", blocked)
    monkeypatch.setattr(controls, "_launch_view", tracked)
    request = asyncio.create_task(
        client.post("/_latet/engine/v1/launches", headers={"x-latet-token": BRIDGE_KEY}, json={"folder": str(folder)})
    )
    try:
        await asyncio.wait_for(started.wait(), 3)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert owner.counts == {"entries": 1, "bindings": 1, "holds": 0}
        release.set()
        await asyncio.wait_for(asyncio.gather(*workers), 3)
        await asyncio.wait_for(aborted.wait(), 3)
        assert owner.counts == {"entries": 0, "bindings": 0, "holds": 0}
        assert all(work.result() is None for work in workers)
    finally:
        release.set()
        request.cancel()
        await asyncio.gather(request, *workers, return_exceptions=True)


@pytest.mark.parametrize("mode", ["private-cancel", "success", "queued-cancel"])
async def test_launch_reservation_tracks_the_actual_executor_future(launch_bridge, monkeypatch, mode):
    import asyncio
    import threading
    from concurrent.futures import Future

    client, mediator, folder = launch_bridge
    application = client._transport.app
    bridge = next(route.endpoint.__self__ for route in application.routes if route.path.endswith("/launches/prepare"))
    owner = bridge.launch_keys
    aborted = _observe_local_abort(owner, monkeypatch) if mode != "success" else None
    controls = bridge._launch_controls  # the computer's launch controls, as the bridge was built with them
    original_binding, original_view = controls._launch_binding, controls._launch_view
    original_submit = mediator.bridge_executor.submit
    started, release = asyncio.Event(), threading.Event()
    view_ready = asyncio.Event()
    loop = asyncio.get_running_loop()
    submitted: list[Future[None]] = []
    wrappers: list[asyncio.Future[None]] = []
    queued = mode == "queued-cancel"

    def barrier():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(3), "the test must release its one bounded worker barrier"

    def blocked(value):
        if not queued:
            barrier()
        return original_binding(value)

    def submit(*args, **kwargs):
        future = original_submit(*args, **kwargs)
        submitted.append(future)
        return future

    def tracked(work):
        view = original_view(work)
        wrappers.append(view)
        view_ready.set()
        return view

    monkeypatch.setattr(controls, "_launch_binding", blocked)
    monkeypatch.setattr(controls, "_launch_view", tracked)
    monkeypatch.setattr(mediator.bridge_executor, "submit", submit)
    blocker = original_submit(barrier) if queued else None
    request = asyncio.create_task(
        client.post("/_latet/engine/v1/launches", headers={"x-latet-token": BRIDGE_KEY}, json={"folder": str(folder)})
    )
    try:
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.wait_for(view_ready.wait(), 3)
        (actual,) = submitted
        assert not actual.done() and actual.running() is not queued
        if mode != "success":
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request, 3)
            (wrapper,) = wrappers
            wrapper.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(wrapper, 3)
            await asyncio.sleep(0)
            assert not actual.done() and not actual.cancelled() and not release.is_set()
            assert actual.running() is not queued
            assert owner.counts == {"entries": 1, "bindings": 1, "holds": 0}
        release.set()
        assert await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(actual)), 3) is None
        if aborted is not None:
            await asyncio.wait_for(aborted.wait(), 3)
            assert owner.counts == {"entries": 0, "bindings": 0, "holds": 0}
        else:
            _opened_launch(await asyncio.wait_for(request, 3))
            assert owner.counts == {"entries": 1, "bindings": 1, "holds": 0}
    finally:
        release.set()
        request.cancel()
        for future in submitted:
            view = asyncio.shield(asyncio.wrap_future(future))
            await asyncio.wait_for(asyncio.gather(view, return_exceptions=True), 3)
        if blocker is not None:
            await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(blocker)), 3)
        await asyncio.wait_for(asyncio.gather(request, *wrappers, return_exceptions=True), 3)


def test_final_retirement_fences_a_prepare_observed_before_delayed_owners_detach(local_launches):
    from latet.gateway.launch_keys import LaunchRefused

    opened = _local_open(local_launches)
    hold = local_launches.reserve_hold(opened["launch_id"], "computer", 0)
    hold.acknowledge()
    before = local_launches.prepare()
    mutation = local_launches.begin_recovery(opened["key"], opened["launch_id"], "computer", **before)
    try:
        assert not local_launches.confirmed_exit(opened["launch_id"], "computer", before["epoch"])
        fresh_before_deletion = local_launches.prepare()
        assert fresh_before_deletion == before
        with pytest.raises(LaunchRefused, match="launch-stale"):
            local_launches.commit_recovery(mutation, _launch_binding())
        hold.close()
        assert local_launches.prepare()["retirement"] == before["retirement"]
        local_launches.abort(mutation)
        assert local_launches.prepare()["retirement"] == before["retirement"] + 1
        assert local_launches.verify(opened["key"], "computer").code == "unknown"
        with pytest.raises(LaunchRefused, match="launch-stale"):
            local_launches.begin_recovery(opened["key"], opened["launch_id"], "computer", **fresh_before_deletion)
    finally:
        hold.close()
        local_launches.abort(mutation)


async def test_a_blocked_hold_drain_stays_charged_across_parent_cancellation(local_launches):
    import asyncio

    from latet.bridge.api import _LaunchHold

    opened = _local_open(local_launches)
    response = _LaunchHold(local_launches.reserve_hold(opened["launch_id"], "computer", 0))
    acknowledged, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def receive():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            draining.set()
            await release.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("more_body"):
            assert local_launches.verify(opened["key"], "computer").code == "active"
            acknowledged.set()

    request = asyncio.create_task(response({"type": "http"}, receive, send))
    try:
        await asyncio.wait_for(acknowledged.wait(), 3)
        authority = local_launches.verify(opened["key"], "computer").authority
        request.cancel()
        await asyncio.wait_for(draining.wait(), 3)
        before = local_launches.retained_bytes
        assert local_launches.counts["holds"] == 1
        assert local_launches.verify(opened["key"], "computer").code == "ended"
        assert not local_launches.validate(authority)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert local_launches.retained_bytes == before and local_launches.counts["holds"] == 1
        assert not local_launches.validate(authority)
        release.set()
        assert response.drain_task is not None
        await asyncio.wait_for(response.drain_task, 3)
        assert local_launches.counts == {"entries": 1, "bindings": 0, "holds": 0}
    finally:
        release.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        if response.drain_task is not None:
            await asyncio.wait_for(response.drain_task, 3)


async def test_a_failed_positive_hold_send_ends_and_detaches_once(local_launches):
    from latet.bridge.api import _LaunchHold

    opened = _local_open(local_launches)
    lease = local_launches.reserve_hold(opened["launch_id"], "computer", 0)

    async def receive():
        raise AssertionError("failed ACK send must create no receive wait")

    async def send(message):
        if message["type"] == "http.response.body":
            assert local_launches.verify(opened["key"], "computer").code == "active"
            raise OSError("test-owned send failed")

    with pytest.raises(OSError, match="test-owned send failed"):
        await _LaunchHold(lease)({"type": "http"}, receive, send)
    lease.close()
    assert local_launches.verify(opened["key"], "computer").code == "ended"
    assert local_launches.counts == {"entries": 1, "bindings": 0, "holds": 0}


def _await_chain_function(coroutine, function):
    """The label of the function a coroutine names as its own definition, when that function is program code.

    The await-chain rule (root's ruling, checkpoint 13): a coroutine on a walked task's own await chain refers to the
    function it was made from (its frame's function object). That function is program code, not retained data, only
    when it is a module-level function of a ``latet.*`` module: its code is the coroutine's (identity), it has no
    closure, and its module and qualname resolve to this very object in the module's globals. Anything else is None:
    bound methods and library functions are never covered (asyncio's ``Timeout.__aexit__`` keeps its own exact boundary
    in the HOLD witness), nor is the same function reached any other way.
    """
    import sys
    import types

    if type(coroutine) is not types.CoroutineType or type(function) is not types.FunctionType:
        return None
    if function.__code__ is not coroutine.cr_code or function.__closure__ is not None:
        return None
    module_name, qualname = function.__module__, function.__qualname__
    if not isinstance(module_name, str) or not module_name.startswith("latet.") or not isinstance(qualname, str):
        return None
    module = sys.modules.get(module_name)
    if "." in qualname or module is None or function.__globals__ is not vars(module):
        return None
    if vars(module).get(qualname) is not function:
        return None
    return f"{module_name}.{qualname}"


def _constant(value, depth=0):
    """A value a definition may carry as a module constant: an immutable atom, or a tuple or frozenset of them."""
    if type(value) in (str, bytes, int, float, complex, bool, type(None)) or value is Ellipsis:
        return True
    return type(value) in (tuple, frozenset) and depth < 8 and all(_constant(part, depth + 1) for part in value)


def _shared_definition(function):
    """Whether ``function`` is a definition made once with its module: its qualname, with no ``<locals>``, resolves
    through the module's globals and the classes' own dicts to this very object."""
    import sys

    qualname, module_name = function.__qualname__, function.__module__
    if not isinstance(qualname, str) or not isinstance(module_name, str) or "<locals>" in qualname:
        return False
    module = sys.modules.get(module_name)
    if module is None or function.__globals__ is not vars(module):
        return False
    first, *rest = qualname.split(".")
    found = vars(module).get(first)
    for part in rest:
        if not isinstance(found, type):
            return False
        found = vars(found).get(part)
    if isinstance(found, (staticmethod, classmethod)):
        found = found.__func__
    return found is function


def _function_dict(function):
    """A function's own ``__dict__`` when it already exists, read through its slot (root's ruling, checkpoint 16):
    ``function.__dict__`` would create an empty one, so the walker would count what it made."""
    import ctypes
    import types

    assert type(function) is types.FunctionType and types.FunctionType.__dictoffset__ > 0
    address = ctypes.c_void_p.from_address(id(function) + types.FunctionType.__dictoffset__).value
    if not address:
        return None
    found = ctypes.cast(address, ctypes.py_object).value
    assert type(found) is dict
    return found


def _program_code(function, referents):
    """What ``function`` reaches that is shared program code, recorded and never counted (root's ruling, checkpoint
    15): its docstring, ``__name__``, ``__qualname__`` and ``__module__``, its annotations (found as the one referent
    left, never read through ``__annotations__``), and a shared definition's defaults when they are module constants.
    Its code object is checked by the walker and never counted either. Its ``__dict__``, its closure and the defaults
    of a per-request function stay counted, and the same objects reached from retained data are counted. On Python
    3.14 the annotations are a function of their own until read (``__annotate__``, PEP 649), whose closure holds the
    namespace of the class it was defined in: that function is the annotations, recorded and not followed."""
    annotate = getattr(function, "__annotate__", None)  # never called: calling it would make the annotations
    known = (
        function.__code__,
        function.__globals__,
        function.__builtins__,
        _function_dict(function),
        function.__closure__,
        function.__defaults__,
        function.__kwdefaults__,
        function.__type_params__,
    )
    names = (function.__doc__, function.__name__, function.__qualname__, function.__module__)
    found = [part for part in referents if part is not None and any(part is name for name in names)]
    if annotate is not None and any(part is annotate for part in referents):
        found.append(annotate)
    left = [part for part in referents if not any(part is other for other in (*known, *names, annotate))]
    if len(left) == 1 and type(left[0]) in (dict, tuple):
        found.append(left[0])
    if _shared_definition(function):
        defaults, keywords = function.__defaults__, function.__kwdefaults__
        if defaults is not None and _constant(defaults):
            found.append(defaults)
        if keywords is not None and all(_constant(value) for value in keywords.values()):
            found.append(keywords)
    return found


class _LaunchGraph:
    """R133 finite application graph; unknown private types/frames fail closed.

    Boundaries are object identities, never arbitrary modules with matching names.
    The two injected I/O callables, their continuations and event waiters stand in
    for transport. They are inventoried separately, not a Uvicorn measurement.
    """

    def __init__(
        self,
        shared,
        driver_codes=(),
        private_frame_codes=(),
        *,
        observer_codes=None,
        private_types=(),
        shared_modules=(),
        await_chain_functions=False,
    ):
        self.shared = dict(shared)
        self.driver_codes = frozenset(driver_codes)
        self.private_frame_codes = frozenset(private_frame_codes)
        self.observer_codes = None if observer_codes is None else frozenset(observer_codes)
        self.private_types = tuple(private_types)
        self.shared_modules = frozenset(shared_modules)
        # Opt-in await-chain rule (_await_chain_function): only the defining function a coroutine on a walked task's
        # own await chain names is recorded and not descended into; the coroutine, its frame and the task stay counted.
        self.await_chain_functions = bool(await_chain_functions)

    def measure(self, roots, *, census=None):
        import asyncio
        import sys
        from types import FrameType

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            assert all(root is not asyncio.current_task() for root in roots)
        scheduler_codes = {
            function.__code__
            for name in ("_run_once", "run_forever", "run_until_complete")
            if (function := getattr(getattr(loop, name, None), "__func__", None)) is not None
        }
        live = {}
        frames = []
        frame: FrameType | None = sys._getframe()
        try:
            while frame is not None:
                assert len(frames) < 64, "observer stack exceeds the pinned profile"
                frames.append(frame)
                if frame.f_code in scheduler_codes:
                    assert frame_locals(frame).get("self") is loop
                    live[id(frame)] = "active scheduler frame"
                elif frame.f_code is asyncio.Handle._run.__code__:
                    assert getattr(frame_locals(frame).get("self"), "_loop", None) is loop
                    live[id(frame)] = "active scheduler frame"
                elif self.observer_codes is None or frame.f_code in self.observer_codes:
                    live[id(frame)] = "active observer frame"
                if (
                    self.observer_codes is not None
                    and loop is not None
                    and live.get(id(frame)) == "active scheduler frame"
                ):
                    break
                frame = frame.f_back
            if loop is not None:
                if self.observer_codes is None:
                    assert "active scheduler frame" in live.values()
                for name in ("_ready", "_scheduled"):
                    collection = getattr(loop, name, None)
                    assert collection is not None
                    live[id(collection)] = f"current loop {name}"
            return self._measure(roots, live, census)
        finally:
            frames.clear()
            frame = None

    def _measure(self, roots, live, census):
        import asyncio
        import contextvars
        import gc
        import inspect
        import sys
        import types
        import weakref
        from collections import deque
        from functools import partial
        from typing import Any, cast

        from starlette.datastructures import Headers, QueryParams, State
        from starlette.requests import Request

        from latet.bridge.api import _LaunchHold
        from latet.gateway.launch_keys import Hold

        atoms = (str, bytes, bytearray, int, float, bool, type(None), object)
        containers = (dict, list, tuple, set, frozenset, deque)
        python_objects = (Request, Headers, QueryParams, State, _LaunchHold, Hold, asyncio.Event, *self.private_types)
        async_types = (asyncio.Task, asyncio.Future, asyncio.Handle, asyncio.TimerHandle)
        allowed_globals = {
            "builtins",
            "asyncio.base_events",
            "asyncio.tasks",
            "asyncio.locks",
            "asyncio.events",
            "asyncio.futures",
            "starlette.applications",
            "starlette.routing",
            "starlette._exception_handler",
            "starlette.middleware.errors",
            "starlette.middleware.exceptions",
            "latet.bridge.api",
            "latet.gateway.launch_keys",
            "latet.gateway.security",
        }
        allowed_globals.update(self.shared_modules)
        boundaries = dict(self.shared)
        # functools.partial among them: Python 3.14's partial object names its own class among its referents.
        for definition in (*atoms, *containers, *python_objects, *async_types, partial):
            boundaries[id(definition)] = f"shared class definition {definition.__module__}.{definition.__name__}"
        for name in allowed_globals:
            module = sys.modules.get(name)
            if module is not None:
                boundaries[id(module)] = f"shared module {name}"
                boundaries[id(module.__dict__)] = f"shared globals {name}"
        pending: list[tuple[str, Any, tuple[str, ...]]] = [("root", value, ()) for value in roots]
        seen: dict[int, object] = {}  # keep temporary proxy identities live during this synchronous walk
        totals: dict[str, int] = {}
        counts: dict[str, int] = {}
        cuts: dict[str, int] = {}
        frame_counts: dict[str, int] = {}
        await_chain: set[int] = set()  # coroutines on a walked task's own await chain (await_chain_functions only)
        chain_functions: dict[str, int] = {}
        recorded: dict[int, object] = {}  # program code seen through functions: recorded, never counted
        program_code: dict[str, int] = {}
        observation_bytes = 0
        observed = []  # weak references only; no graph snapshot becomes a new owner
        while pending:
            edge, value, trail = pending.pop()
            if id(value) in live:
                reason = live[id(value)]
                cuts[reason] = cuts.get(reason, 0) + 1
                continue
            if id(value) in boundaries:
                if self.observer_codes is not None:
                    reason = boundaries[id(value)]
                    cuts[reason] = cuts.get(reason, 0) + 1
                continue
            if id(value) in seen:
                continue
            seen[id(value)] = value
            assert len(seen) <= 4096, "application graph object bound exceeded"
            kind = type(value)
            category = f"{kind.__module__}.{kind.__name__}"
            children = []
            item: Any = value
            if kind in atoms:
                pass
            elif kind in containers:
                children = [*item.keys(), *item.values()] if kind is dict else list(item)
            elif kind is types.CoroutineType:
                if item.cr_code in self.driver_codes:
                    continue
                children = [item.cr_frame, item.cr_await, *gc.get_referents(item)]
                if id(item) in await_chain:
                    defining = [child for child in children if _await_chain_function(item, child) is not None]
                    if defining:
                        label = _await_chain_function(item, defining[0])
                        assert label is not None
                        chain_functions[label] = chain_functions.get(label, 0) + 1
                        children = [child for child in children if child is not defining[0]]
                    del defining
            elif kind is types.FrameType:
                if item.f_code in self.driver_codes:
                    continue
                assert item.f_globals.get("__name__") in allowed_globals or item.f_code in self.private_frame_codes, (
                    f"unknown frame at {edge}"
                )
                assert item.f_trace is None, f"unexpected traced frame at {edge}"
                name = item.f_code.co_qualname
                frame_counts[name] = frame_counts.get(name, 0) + 1
                if sys.version_info >= (3, 13):
                    children = [item.f_back, item.f_locals, *gc.get_referents(item)]
                else:
                    # CPython 3.12: f_locals is a dict this very read makes (and leaves on the frame), the walker's own
                    # artifact as 3.13's proxy is: its keys that are the code's own names are program code, recorded
                    # and not counted; any other key, and every value, is followed and counted. Then it is emptied:
                    # left on a frame that outlives the walk (a pool worker's _worker, which ends with its thread), it
                    # would keep what the frame has deleted since, a finished work item and the call it held among them.
                    # On 3.12 that dict is also the frame's own locals(), so the clear would take names from a frame that
                    # keeps it or execs into it; the frames this walk reads do neither.
                    snapshot = item.f_locals
                    code_names = {
                        id(local): local
                        for local in (*item.f_code.co_varnames, *item.f_code.co_cellvars, *item.f_code.co_freevars)
                    }
                    for key in snapshot:
                        if code_names.get(id(key)) is key and id(key) not in recorded:
                            recorded[id(key)] = key
                            program_code["builtins.str"] = program_code.get("builtins.str", 0) + sys.getsizeof(key)
                    observation_bytes += sys.getsizeof(snapshot)
                    children = [
                        item.f_back,
                        *(key for key in snapshot if code_names.get(id(key)) is not key),
                        *snapshot.values(),
                        *(part for part in gc.get_referents(item) if part is not snapshot),
                    ]
                    if item.f_code.co_flags & inspect.CO_OPTIMIZED:  # a module's or class body's is its namespace
                        snapshot.clear()
                    del snapshot, code_names
            elif (kind.__module__, kind.__name__) == ("builtins", "FrameLocalsProxy"):
                # CPython 3.13 exposes fast locals through a proxy with an owning frame reference. The keys that are
                # the owning code's own names are program code (root's ruling, checkpoint 16): recorded, not counted;
                # any other key, and every value, is followed and counted.
                owners = gc.get_referents(item)
                names = {
                    id(name): name
                    for owner in owners
                    if type(owner) is types.FrameType
                    for name in (*owner.f_code.co_varnames, *owner.f_code.co_cellvars, *owner.f_code.co_freevars)
                }
                keys = list(item.keys())
                for key in keys:
                    if names.get(id(key)) is key and id(key) not in recorded:
                        recorded[id(key)] = key
                        program_code["builtins.str"] = program_code.get("builtins.str", 0) + sys.getsizeof(key)
                children = [*owners, *(key for key in keys if names.get(id(key)) is not key), *item.values()]
                del owners, names, keys
            elif kind is types.FunctionType:
                if item.__globals__.get(item.__name__) is item:
                    assert item.__module__ in allowed_globals, f"unknown shared function at {edge}"
                    continue
                referents = gc.get_referents(item)  # its __dict__ only if it exists: never item.__dict__
                code = _program_code(item, referents)
                parts = list(code)
                while parts:  # recorded, never counted nor followed from here; counted if data reaches it
                    part = parts.pop()
                    if id(part) in recorded:
                        continue
                    recorded[id(part)] = part
                    name = f"{type(part).__module__}.{type(part).__name__}"
                    program_code[name] = program_code.get(name, 0) + sys.getsizeof(part)
                    if type(part) in (tuple, frozenset):
                        parts.extend(value for value in part if _constant(value))
                    elif type(part) is dict:
                        parts.extend(value for pair in part.items() for value in pair if _constant(value))
                del parts
                children = [child for child in referents if not any(child is part for part in code)]
                del referents, code
            elif kind in (types.CellType, types.MethodType, types.BuiltinMethodType, partial, types.TracebackType):
                children = gc.get_referents(item)
            elif kind is types.CodeType:
                assert item in self.private_frame_codes or item.co_filename in {
                    sys.modules[name].__file__
                    for name in allowed_globals
                    if getattr(sys.modules.get(name), "__file__", None)
                }, f"unknown code definition at {edge}"
                continue
            elif isinstance(item, type):
                module = sys.modules.get(item.__module__)
                assert item.__module__ in allowed_globals and module is not None, f"unknown class definition at {edge}"
                assert vars(module).get(item.__name__) is item, f"private class definition at {edge}"
                continue
            elif kind in async_types or (kind.__module__, kind.__name__) == ("asyncio.tasks", "_GatheringFuture"):
                children = gc.get_referents(item)
                if isinstance(item, asyncio.Task):
                    waiter = getattr(item, "_fut_waiter", ...)
                    assert waiter is None or isinstance(waiter, asyncio.Future)
                    children.extend((item.get_coro(), item.get_context(), waiter))
                    if self.await_chain_functions:
                        step: Any = item.get_coro()
                        depth = 0
                        while type(step) is types.CoroutineType:
                            depth += 1
                            assert depth <= 64, "await chain exceeds the pinned profile"
                            await_chain.add(id(step))
                            step = step.cr_await
                        del step
                if hasattr(item, "__dict__"):
                    children.append(vars(item))
            elif kind in (contextvars.Context, contextvars.ContextVar):
                children = gc.get_referents(item)
                if kind is contextvars.Context:
                    children.extend(part for pair in item.items() for part in pair)
            elif (kind.__module__, kind.__name__) in {
                ("_asyncio", "FutureIter"),
                ("builtins", "coroutine_wrapper"),
                ("builtins", "hamt"),
                ("builtins", "hamt_bitmap_node"),
                ("builtins", "hamt_array_node"),
                ("builtins", "hamt_collision_node"),
                ("builtins", "list_iterator"),
                ("builtins", "tuple_iterator"),
                ("builtins", "set_iterator"),
                ("builtins", "dict_keyiterator"),
                ("builtins", "dict_valueiterator"),
                ("builtins", "dict_itemiterator"),
            }:
                boundaries[id(kind)] = f"shared class definition {category}"
                children = gc.get_referents(item)  # C storage is followed even without __dict__
            elif kind in python_objects:
                children = gc.get_referents(item)
                if hasattr(item, "__dict__"):
                    children.append(vars(item))
                if kind in (Request, _LaunchHold):
                    observed.append(weakref.ref(item))
            elif isinstance(item, BaseException) and kind in (asyncio.CancelledError,):
                children = [item.args, item.__traceback__, item.__context__, item.__cause__, vars(item)]
            else:
                lineage = f"; path {' > '.join(trail)}" if trail else ""
                raise AssertionError(f"unknown private edge {edge}: {category}{lineage}")
            if (kind.__module__, kind.__name__) == ("builtins", "FrameLocalsProxy"):
                observation_bytes += sys.getsizeof(item)  # the walker's own artifact: recorded, not counted
            else:
                totals[category] = totals.get(category, 0) + sys.getsizeof(item)
                counts[category] = counts.get(category, 0) + 1
                if census is not None:
                    # Only the paired fixture keeps this identity live during its synchronous sample.
                    census[id(item)] = item
            assert sum(totals.values()) <= 262144, "application graph byte inspection bound exceeded"
            if census is not None and children:
                owner = category
                if kind is types.FunctionType:
                    owner = f"{item.__module__}.{cast(types.FunctionType, item).__qualname__}"
                elif kind is types.FrameType:
                    owner = cast(types.FrameType, item).f_code.co_qualname
                trail = (*trail[-11:], f"{edge} -> {owner[:160]}")
            pending.extend((f"{category}[{index}]", child, trail) for index, child in enumerate(children))
        result: Any = {
            "bytes": sum(totals.values()),
            "components": totals,
            "objects": counts,
            "live_boundary_edges": cuts,
            "private_frames": frame_counts,
            "observation_proxy_bytes": observation_bytes,
            # Recorded, not counted (root's ruling, checkpoint 15): shared program code reached through functions and
            # the walker's own FrameLocalsProxy objects. They never enter "bytes" nor the census.
            "uncounted": {
                "program_code": sum(program_code.values()),
                "program_code_components": program_code,
                "observation_proxies": observation_bytes,
            },
        }
        recorded.clear()
        if self.await_chain_functions:
            result["await_chain_functions"] = chain_functions
        return result, observed


def test_application_graph_counts_private_context_and_rejects_unknown_edges():
    import contextvars
    import sys
    from types import GeneratorType

    variable = contextvars.ContextVar("p26a-private-graph-control")
    context = contextvars.Context()
    context.run(variable.set, bytearray(4096))
    measured, _ = _LaunchGraph({id(variable): "test-driver variable definition"}).measure([context])
    assert measured["components"]["builtins.bytearray"] >= 4096

    def private_frame():
        payload = bytearray(4096)
        yield payload

    suspended = private_frame()
    try:
        assert isinstance(suspended, GeneratorType)
        next(suspended)
        frame = suspended.gi_frame
        assert frame is not None
        measured, _ = _LaunchGraph({id(frame): "test-driver suspended frame"}).measure([frame.f_locals])
        assert measured["components"]["builtins.bytearray"] >= 4096
        if sys.version_info >= (3, 13):  # the walker's own proxy: recorded, not counted (checkpoint 15)
            assert measured["uncounted"]["observation_proxies"] > 0
            assert "builtins.FrameLocalsProxy" not in measured["components"]
    finally:
        suspended.close()

    def dead_dispatch(payload):
        return sys._getframe()

    dead = dead_dispatch(bytearray(4096))
    measured, _ = _LaunchGraph(
        {id(sys): "shared sys module", id(dead_dispatch.__globals__): "shared driver globals"},
        private_frame_codes=(dead_dispatch.__code__,),
    ).measure([dead])
    assert measured["components"]["builtins.bytearray"] >= 4096
    assert measured["private_frames"][dead_dispatch.__qualname__] == 1
    assert measured["live_boundary_edges"]["active observer frame"] >= 1
    del dead

    class UnknownPrivate:
        __slots__ = ("payload",)

        def __init__(self):
            self.payload = bytearray(4096)

    with pytest.raises(AssertionError, match="unknown private edge"):
        _LaunchGraph({}).measure([UnknownPrivate()])


def test_the_graph_walk_leaves_nothing_on_a_frame_it_reads():
    """Python 3.12's ``f_locals`` of a function's frame is a dict that the read makes and keeps on the frame, each value
    in it, until the frame ends or is read again (3.13's is a view, PEP 667). Read by the walk on a frame that outlives
    it, a pool worker's ``_worker`` (it ends with its thread), it kept the work item the worker had since deleted, and
    the STORE call's argument with it: the launch tail never settled, and a HOLD drain never ended."""
    import weakref

    class Payload:
        __slots__ = ("__weakref__",)

    def holder(payload):
        yield
        del payload
        yield

    payload = Payload()
    gone = weakref.ref(payload)
    suspended = holder(payload)
    del payload
    try:
        next(suspended)
        frame = suspended.gi_frame
        assert frame is not None
        _LaunchGraph({}, private_frame_codes=(holder.__code__,), private_types=(Payload,)).measure([frame])
        del frame
        next(suspended)  # the frame deletes its argument and lives on
        assert gone() is None, "the walk kept what the frame it read has deleted since"
    finally:
        suspended.close()


def test_await_chain_rule_names_only_a_latet_coroutines_own_module_function(monkeypatch):
    """The HOLD witness's await-chain rule accepts a latet.* coroutine's own module-level function, nothing else."""
    import asyncio
    import types
    from typing import Any

    from latet.bridge import api as bridge_api
    from latet.server import spaces as server_spaces

    held: Any = server_spaces._held  # called with inert arguments: each coroutine is closed unstarted
    space_globals: Any = vars(server_spaces)
    coroutine = held(None, None, None, None)
    copy = types.FunctionType(held.__code__, space_globals, held.__name__)
    copied = copy(None, None, None, None)

    def outer():
        value = 1

        async def probe():
            return value

        return probe

    template = outer()
    name = "_p26a_closure_probe"
    closure = types.FunctionType(template.__code__, space_globals, name, None, template.__closure__)
    closure.__qualname__ = name
    monkeypatch.setitem(vars(server_spaces), name, closure)  # only the closure differs from an accepted function
    enclosed = closure()
    method: Any = bridge_api._LaunchHold.__call__
    bound = method(None, None, None, None)
    library = asyncio.sleep(0)
    try:
        assert _await_chain_function(coroutine, held) == "latet.server.spaces._held"
        assert _await_chain_function(copied, copy) is None  # its qualname resolves to another object
        assert _await_chain_function(coroutine, copy) is None
        assert _await_chain_function(enclosed, closure) is None  # a closure
        assert _await_chain_function(bound, method) is None  # a method, never a module-level function
        assert _await_chain_function(library, asyncio.sleep) is None  # a library function
        assert _await_chain_function(library, held) is None  # another coroutine's code
    finally:
        for made in (coroutine, copied, enclosed, bound, library):
            made.close()


async def test_await_chain_rule_applies_only_on_a_walked_tasks_own_await_chain():
    """The function is recorded, never descended into, only as named by a coroutine on a walked task's own chain."""
    import asyncio
    import contextvars
    from typing import Any

    from latet.server import spaces as server_spaces

    held: Any = server_spaces._held  # inert arguments: the task is cancelled before its first step
    loop = asyncio.get_running_loop()
    task = loop.create_task(held(None, None, None, None), context=contextvars.Context())
    shared = {id(loop): "exact running loop"}
    codes = (held.__code__,)
    loose = held(None, None, None, None)
    try:
        measured, _ = _LaunchGraph(shared, private_frame_codes=codes, await_chain_functions=True).measure([task])
        assert measured["await_chain_functions"] == {"latet.server.spaces._held": 1}
        assert measured["objects"]["builtins.coroutine"] == 1 and measured["private_frames"]["_held"] == 1
        with pytest.raises(AssertionError, match="unknown shared function"):  # the rule is the witness's choice
            _LaunchGraph(shared, private_frame_codes=codes).measure([task])
        with pytest.raises(AssertionError, match="unknown shared function"):  # the same function in retained data
            _LaunchGraph(shared, private_frame_codes=codes, await_chain_functions=True).measure([task, [held]])
        with pytest.raises(AssertionError, match="unknown shared function"):  # a coroutine on no task's chain
            _LaunchGraph(shared, private_frame_codes=codes, await_chain_functions=True).measure([[loose]])
    finally:
        loose.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


_SHARED_DEFAULT = "d" * 65536  # a module constant, the default of a shared definition below
_SHARED_RETAINED = [bytes(65536)]  # a module-level mutable default: what it holds may grow, never a constant


class _SharedDefinition:
    def run(self, value=_SHARED_DEFAULT, *, flag: bool = True) -> str:
        """A shared method definition, made once with its module."""
        return value if flag else ""


class _SharedMutableDefault:
    def run(self, retained=_SHARED_RETAINED) -> int:
        return len(retained)


def _fails_the_hold_bound(measured):
    from latet.gateway.launch_keys import HOLD_BYTES

    return measured["bytes"] + 8192 > HOLD_BYTES


def test_what_a_hold_retains_of_its_own_is_counted_and_fails_the_bound():
    """Root's ruling (checkpoint 15): a large value a hold keeps through a frame local, a closure cell, a default of a
    per-request function or its scope is counted, whatever the walker leaves uncounted."""
    module = {id(globals()): "this test module's globals"}

    def held(payload):
        yield len(payload)

    from types import GeneratorType

    suspended = held(bytes(65536))
    next(suspended)
    try:
        assert isinstance(suspended, GeneratorType)
        frame = suspended.gi_frame
        assert frame is not None
        measured, _ = _LaunchGraph(module, private_frame_codes=(held.__code__,)).measure([frame])
        assert _fails_the_hold_bound(measured) and measured["components"]["builtins.bytes"] >= 65536
        assert "builtins.FrameLocalsProxy" not in measured["components"]  # the walker's own proxy: recorded only
        assert measured["uncounted"]["observation_proxies"] > 0
    finally:
        suspended.close()

    def with_cell(payload):
        def inner():
            return payload

        return inner

    def with_closure_default(payload, marker):
        def inner(data=payload):
            return marker, data

        return inner

    def with_nested_default(payload):
        def inner(data=payload):
            return data

        return inner

    for function in (
        with_cell(bytes(65536)),
        with_closure_default(bytes(65536), object()),
        with_nested_default(bytes(65536)),
    ):
        measured, _ = _LaunchGraph(module, private_frame_codes=(function.__code__,)).measure([function])
        assert _fails_the_hold_bound(measured), function.__qualname__
    scope = {"type": "http", "path": "/hold", "headers": [(b"x-hold-pad", bytes(65536))]}
    measured, _ = _LaunchGraph({}).measure([scope])
    assert _fails_the_hold_bound(measured)
    measured, _ = _LaunchGraph(module, private_frame_codes=(_SharedMutableDefault.run.__code__,)).measure(
        [_SharedMutableDefault.run]
    )
    assert _fails_the_hold_bound(measured)  # a mutable default is retained data, not a module constant


def test_program_code_is_recorded_not_counted_only_as_reached_through_its_function():
    """A function's docstring, names, module, annotations and a shared definition's constant defaults are program
    code: recorded, not counted, when reached through the function, and counted when retained data reaches them."""
    module = {id(globals()): "this test module's globals"}

    def per_request(marker):
        def inner():
            return marker

        return inner

    function = per_request(object())
    function.__doc__ = "w" * 65536
    codes = (function.__code__,)
    measured, _ = _LaunchGraph(module, private_frame_codes=codes).measure([function])
    assert not _fails_the_hold_bound(measured)
    assert measured["uncounted"]["program_code"] >= 65536
    doc = function.__doc__
    for roots in ([function, [doc]], [[doc], function]):  # the same string reached from retained data, either order
        measured, _ = _LaunchGraph(module, private_frame_codes=codes).measure(roots)
        assert _fails_the_hold_bound(measured) and measured["components"]["builtins.str"] >= 65536
    shared = _SharedDefinition.run
    measured, _ = _LaunchGraph(module, private_frame_codes=(shared.__code__,)).measure([shared])
    assert not _fails_the_hold_bound(measured)
    assert measured["uncounted"]["program_code"] >= 65536  # its constant default, names, module and annotations
    measured, _ = _LaunchGraph(module, private_frame_codes=(shared.__code__,)).measure([shared, (_SHARED_DEFAULT,)])
    assert _fails_the_hold_bound(measured)  # the same constant reached from retained data is counted


def test_the_walker_creates_no_function_dict_and_counts_an_existing_one():
    """Root's ruling (checkpoint 16): the walker never materialises a function's ``__dict__`` it measures; a
    ``__dict__`` that exists, holding a large value, is counted."""
    import gc

    module = {id(globals()): "this test module's globals"}

    def per_request(marker):
        def inner():
            return marker

        return inner

    def own_dicts(function):  # never through function.__dict__, which would create it
        shared = (function.__globals__, function.__builtins__)
        found = gc.get_referents(function)
        return [part for part in found if type(part) is dict and not any(part is other for other in shared)]

    fresh = per_request(object())
    assert own_dicts(fresh) == []
    measured, _ = _LaunchGraph(module, private_frame_codes=(fresh.__code__,)).measure([fresh])
    assert own_dicts(fresh) == [] and "builtins.dict" not in measured["components"]
    keeping = per_request(object())
    vars(keeping)["payload"] = bytes(65536)
    measured, _ = _LaunchGraph(module, private_frame_codes=(keeping.__code__,)).measure([keeping])
    assert _fails_the_hold_bound(measured) and measured["components"]["builtins.dict"] > 0


def test_a_frames_local_names_are_program_code_and_their_values_are_counted():
    """Root's ruling (checkpoint 16): the names a FrameLocalsProxy shows as keys are the code object's own names,
    recorded and not counted; a local's value is counted, and so is the same name string reached from retained data."""
    from typing import Any

    name = "retained_name_" + "n" * 65536  # a local's name as long as a hold's whole bound
    namespace: dict[str, Any] = {"__name__": "tests.unit.test_launch_keys.local_names_probe"}
    exec(compile(f"def held(payload):\n    {name} = payload\n    yield len({name})\n", "<probe>", "exec"), namespace)
    held: Any = namespace["held"]
    codes = (held.__code__,)
    from types import GeneratorType

    small = held(b"v")
    large = held(bytes(65536))
    try:
        for suspended in (small, large):
            next(suspended)
            assert isinstance(suspended, GeneratorType) and suspended.gi_frame is not None
        measured, _ = _LaunchGraph({}, private_frame_codes=codes).measure([small.gi_frame])
        assert not _fails_the_hold_bound(measured)
        assert measured["uncounted"]["program_code"] >= 65536  # the local's name, a code name
        measured, _ = _LaunchGraph({}, private_frame_codes=codes).measure([large.gi_frame])
        assert _fails_the_hold_bound(measured)  # the local's value is counted
        retained = next(local for local in held.__code__.co_varnames if local.startswith("retained_name_"))
        measured, _ = _LaunchGraph({}, private_frame_codes=codes).measure([small.gi_frame, [retained]])
        assert _fails_the_hold_bound(measured)  # the same name reached from retained data is counted
    finally:
        small.close()
        large.close()


async def test_full_local_asgi_hold_graph_stays_bounded_through_replacement_and_drain(launch_bridge, capsys):
    import asyncio
    import contextvars
    import json
    from types import CoroutineType

    from starlette.middleware.errors import ServerErrorMiddleware
    from starlette.middleware.exceptions import ExceptionMiddleware
    from starlette.routing import Match, Route, WebSocketRoute

    from latet.gateway.app import GatewayConfig, create_app
    from latet.gateway.launch_keys import HOLD_BYTES
    from latet.gateway.local_launch import LocalLaunchDispatch, LocalLaunchIngress, LocalRequestLaunch
    from latet.gateway.security import LocalOnlyMiddleware

    client, mediator, folder = launch_bridge
    await _registered_launch_folder_is_reachable(client, mediator, folder)
    bridge = next(
        route.endpoint.__self__ for route in client._transport.app.routes if route.path.endswith("/launches/prepare")
    )
    owner = bridge.launch_keys
    assert owner is not None
    opened = (
        await client.post(
            "/_latet/engine/v1/launches", headers={"x-latet-token": BRIDGE_KEY}, json={"folder": str(folder)}
        )
    ).json()
    application = create_app(
        GatewayConfig(mediator=mediator, bridge_token=BRIDGE_KEY, launch_keys=owner, fixed_routes=[])
    )
    application.middleware_stack = application.build_middleware_stack()
    loop = asyncio.get_running_loop()
    assert not loop.get_debug(), "R133 pins the normal non-debug local profile"
    shared = {
        id(application): "shared Starlette app",
        id(application.router): "shared router",
        id(loop): "shared loop",
    }
    shared[id(owner)] = "separately charged registry"
    shared[id(application.routes)] = "shared route list"
    shared[id(application.router.middleware_stack)] = "shared router callable"
    for match in Match:
        shared[id(match)] = "shared routing enum"
    layer: object = application.middleware_stack
    layers = []
    while layer is not application.router:
        assert isinstance(
            layer,
            (ServerErrorMiddleware, LocalLaunchIngress, LocalOnlyMiddleware, LocalLaunchDispatch, ExceptionMiddleware),
        )
        layers.append(type(layer))
        if isinstance(layer, LocalLaunchIngress):
            assert layer.owner is owner  # the separately charged registry
        shared[id(layer)] = f"shared middleware {type(layer).__name__}"
        for name in ("_exception_handlers", "_status_handlers"):
            if hasattr(layer, name):
                shared[id(getattr(layer, name))] = f"shared middleware {name}"
        layer = layer.app
    # A computer's gateway, exactly as gateway/app.py builds it (dc4bd676): Starlette's error layer outermost, the
    # launch carrier sanitized before LocalOnlyMiddleware, denied carriers refused after it, Starlette's exceptions.
    assert application.max_body_size is None
    assert layers == [
        ServerErrorMiddleware,
        LocalLaunchIngress,
        LocalOnlyMiddleware,
        LocalLaunchDispatch,
        ExceptionMiddleware,
    ]
    for route in application.routes:
        assert isinstance(route, (Route, WebSocketRoute))
        shared[id(route)] = "shared route"
        shared[id(route.app)] = "shared route callable"
        endpoint = route.endpoint
        shared[id(endpoint)] = "shared route endpoint"
        endpoint_owner = getattr(endpoint, "__self__", None)
        if endpoint_owner is not None:
            shared[id(endpoint_owner)] = "shared bridge"
    entry = owner._entries[opened["launch_id"]]
    shared[id(entry)] = "separately charged entry"
    assert entry.binding is not None
    shared[id(entry.binding)] = "separately charged binding"
    for variable, value in contextvars.copy_context().items():
        shared[id(variable)] = f"inherited driver context variable {variable.name}"
        shared[id(value)] = f"inherited driver context value {variable.name}"
    snapshots = []  # numeric totals and weak references, never owned-object snapshots
    observers = []
    requests = []
    drivers = []
    driver_codes = set()
    path = f"/_latet/engine/v1/launches/{opened['launch_id']}/hold"

    def make_driver(generation):
        started, body_blocked, body_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        acknowledged, receiving, draining, release = (asyncio.Event() for _ in range(4))
        events = (started, body_blocked, body_release, acknowledged, receiving, draining, release)
        headers = [(b"host", b"127.0.0.1:5283"), (b"x-latet-token", BRIDGE_KEY.encode())]
        headers.extend((f"x-pad-{index:02}".encode(), b"p") for index in range(29))
        remaining = 4096 - sum(len(name) + len(value) for name, value in headers) - len(b"x-forwarded-for")
        forwarded = b"f" * remaining
        headers.append((b"x-forwarded-for", forwarded))
        assert len(headers) == 32 and sum(len(k) + len(v) for k, v in headers) == 4096
        query = b"generation=" + str(generation).zfill(20).encode() + b"&" * 33
        assert len(query) == 64
        scope = {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "http_version": "1.1",
            "path": path,
            "raw_path": "".join(f"%{ord(character):02x}" for character in path).encode(),
            "query_string": query,
            "headers": headers,
            "server": ("127.0.0.1", 65535),
            "client": (forwarded.decode("latin1"), 0),
            "root_path": "",
            "state": {},
            "asgi": {"version": "3.0", "spec_version": "2.3"},
        }
        assert len(scope["raw_path"]) <= 256

        async def receive():
            receiving.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                draining.set()
                await release.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                assert message["status"] == 200
                started.set()
            elif message.get("more_body"):
                assert json.loads(message["body"]) == {"held": True, "generation": generation + 1}
                assert owner.verify(opened["key"], "computer").code == "active"
                body_blocked.set()
                await body_release.wait()
                acknowledged.set()

        for function in (send, receive):
            shared[id(function)] = "injected transport callable"
            driver_codes.add(function.__code__)
        drivers.append(events)
        task = asyncio.create_task(application(scope, receive, send), name=f"p26a-asgi-{generation}")
        requests.append(task)
        return events

    def owned_drains():
        found = []
        for task in asyncio.all_tasks():
            if task.get_name() == "latet-launch-drain":
                coroutine = task.get_coro()
                assert isinstance(coroutine, CoroutineType)
                frame = coroutine.cr_frame
                if frame is not None and frame_locals(frame).get("lease") in owner._holds:
                    found.append(task)
        assert len(found) <= 2
        return found

    def checkpoint(label, physical):
        boundary = dict(shared)
        for events in drivers:
            for event in events:
                boundary[id(event)] = "injected driver event"
                for waiter in event._waiters:
                    boundary[id(waiter)] = "injected transport waiter"
        roots = [task for task in requests if not task.done()]
        roots.extend(owned_drains())
        # The two launch layers' module is shared program code, as LocalOnlyMiddleware's is; their frames are still
        # walked, and the carrier result LocalLaunchIngress leaves in each request's scope is that request's own state:
        # counted.
        measured, weak_objects = _LaunchGraph(
            boundary,
            driver_codes,
            private_types=(LocalRequestLaunch,),
            shared_modules=("latet.gateway.local_launch",),
        ).measure(roots)
        observers.extend(weak_objects)
        snapshots.append((label, physical, measured))
        with capsys.disabled():
            print(
                "P26A_APPLICATION_GRAPH",
                json.dumps({"checkpoint": label, "physical": physical, **measured}, sort_keys=True),
            )
        assert owner.counts["holds"] == physical
        assert measured["bytes"] + 8192 * physical <= HOLD_BYTES * physical, "candidate lacks the explicit 8KiB reserve"

    try:
        first = make_driver(0)
        await asyncio.wait_for(first[1].wait(), 3)
        checkpoint("blocked-positive-send", 1)
        first[2].set()
        await asyncio.wait_for(first[4].wait(), 3)
        checkpoint("acknowledged-wait", 1)
        second = make_driver(1)
        await asyncio.wait_for(second[1].wait(), 3)
        await asyncio.wait_for(first[5].wait(), 3)
        checkpoint("replacement-overlap-and-old-drain", 2)
        second[2].set()
        await asyncio.wait_for(second[4].wait(), 3)
        authority = owner.verify(opened["key"], "computer").authority
        assert authority is not None
        requests[1].cancel()
        await asyncio.wait_for(second[5].wait(), 3)
        assert owner.verify(opened["key"], "computer").code == "ended" and not owner.validate(authority)
        checkpoint("logical-stop-with-two-physical-drains", 2)
        requests[1].cancel()
        await asyncio.gather(requests[1], return_exceptions=True)
        assert owner.verify(opened["key"], "computer").code == "ended" and not owner.validate(authority)
        checkpoint("repeated-parent-cancel", 2)
    finally:
        for events in drivers:
            events[2].set()
            events[6].set()
        await asyncio.wait_for(asyncio.gather(*requests, *owned_drains(), return_exceptions=True), 3)
        assert owner.counts["holds"] == 0
    for task in requests:
        coroutine = task.get_coro()
        assert isinstance(coroutine, CoroutineType)
        assert task.done() and coroutine.cr_frame is None
    requests.clear()
    task = coroutine = None
    await asyncio.sleep(0)  # the observer's gather wakeup callback still owns its completed children
    assert all(reference() is None for reference in observers), (
        "request/response survives actual teardown without forced GC"
    )
    measured, _ = _LaunchGraph(shared, driver_codes).measure([])
    assert measured["bytes"] == 0
    with capsys.disabled():
        print("P26A_APPLICATION_GRAPH", json.dumps({"checkpoint": "teardown", **measured}, sort_keys=True))
    assert len(snapshots) == 5


def _h11_fixture(application, monkeypatch):
    import asyncio
    import logging

    from uvicorn.protocols.http.h11_impl import H11Protocol

    from latet.cli.main import uvicorn_server

    loop = asyncio.get_running_loop()
    access = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access, "handlers", list(access.handlers))
    monkeypatch.setattr(access, "filters", list(access.filters))
    monkeypatch.setattr(access, "propagate", access.propagate)
    for name in ("_nameToLevel", "_levelToName"):
        monkeypatch.setattr(logging, name, dict(getattr(logging, name)))
    server = uvicorn_server(application, "127.0.0.1", 5283, log_config=None, log_level=None, access_log=False)
    if server.config.http == "auto":
        server.config.http = "h11"  # exercise the pinned h11 profile before explicit production selection
    server.config.load()
    protocol_class = server.config.http_protocol_class
    assert issubclass(protocol_class, H11Protocol)
    state = server.server_state

    class Wire(asyncio.Transport):
        def __init__(self, protocol, port):
            self.protocol = protocol
            self.port = port
            self.reading = True
            self.closed = False
            self.output = bytearray()

        def get_extra_info(self, name, default=None):
            if name == "peername":
                return ("127.0.0.1", self.port)
            if name == "sockname":
                return ("127.0.0.1", 5283)
            return default

        def pause_reading(self):
            self.reading = False

        def resume_reading(self):
            self.reading = True

        def is_closing(self):
            return self.closed

        def write(self, data):
            assert len(self.output) + len(data) <= 16384
            self.output.extend(data)

        def close(self):
            if not self.closed:
                self.closed = True
                loop.call_soon(self.protocol.connection_lost, None)

        def feed(self, data):
            assert self.reading and not self.closed
            assert 0 < len(data) <= 65536
            self.protocol.data_received(data)

    def connect(port):
        wire = Wire(protocol_class(server.config, state, {}), port)
        wire.protocol.connection_made(wire)
        return wire

    return state, connect


async def _h11_until(condition):
    import asyncio

    for _ in range(64):
        if condition():
            return
        await asyncio.sleep(0)
    assert condition(), "h11 hold transition did not settle"


async def test_reused_h11_hold_refuses_unread_pipeline_over_512_kib(launch_bridge, monkeypatch, capsys):
    import asyncio

    from latet.gateway.app import GatewayConfig, create_app

    client, mediator, folder = launch_bridge
    await _registered_launch_folder_is_reachable(client, mediator, folder)
    bridge = next(
        route.endpoint.__self__ for route in client._transport.app.routes if route.path.endswith("/launches/prepare")
    )
    owner = bridge.launch_keys
    assert owner is not None
    response = await client.post(
        "/_latet/engine/v1/launches", headers={"x-latet-token": BRIDGE_KEY}, json={"folder": str(folder)}
    )
    assert response.status_code == 200
    opened = response.json()
    application = create_app(
        GatewayConfig(mediator=mediator, bridge_token=BRIDGE_KEY, launch_keys=owner, fixed_routes=[])
    )
    state, connect = _h11_fixture(application, monkeypatch)
    a, b = connect(10001), connect(10002)

    def request(generation):
        path = f"/_latet/engine/v1/launches/{opened['launch_id']}/hold?generation={generation}"
        return f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:5283\r\nx-latet-token: {BRIDGE_KEY}\r\n\r\n".encode()

    async def acknowledged(wire, generation):
        body = f'{{"held":true,"generation":{generation}}}\n'.encode()
        await _h11_until(lambda: body in wire.output and wire.reading and owner.counts["holds"] == 1)
        check = owner.verify(opened["key"], "computer")
        assert check.code == "active" and check.authority is not None
        assert check.authority.generation == generation

    def unread():
        return len(a.protocol.conn._receive_buffer)

    batch_bytes = 65536
    ceiling = 512 * 1024
    queued = b"".join(request(generation) for generation in range(2, 20, 2))
    assert len(queued) < batch_bytes
    try:
        a.feed(request(0))
        await acknowledged(a, 1)
        for batch in range(10):
            if batch:
                b.feed(request(2 * batch - 1))
                # A's next buffered request can supersede B before B has detached.
                await _h11_until(lambda batch=batch: f'{{"held":true,"generation":{2 * batch}}}\n'.encode() in b.output)
                await acknowledged(a, 2 * batch + 1)
                assert b.protocol.cycle.response_complete
            before = unread()
            if batch == 0:
                data = queued + b"x" * (batch_bytes - len(queued))
            elif batch == 8:
                data = b"x" * (ceiling - before)
            else:
                data = b"x" * batch_bytes
            a.feed(data)
            if batch >= 7:
                with capsys.disabled():
                    print(f"P26A_H11_BATCH batch={batch + 1} before={before} incoming={len(data)} after={unread()}")
            if batch < 9:
                assert unread() == before + len(data)
                assert not a.reading and not a.closed
                assert unread() == ceiling if batch == 8 else unread() < ceiling
            else:
                assert before <= ceiling < before + len(data)
                assert a.closed and unread() <= before, "h11 retained the crossing batch on a reused held connection"
                await _h11_until(lambda: owner.counts["holds"] == 0)
                assert owner.verify(opened["key"], "computer").code == "ended"
    finally:
        a.close()
        b.close()
        await asyncio.wait_for(asyncio.gather(*tuple(state.tasks)), 3)
        await _h11_until(lambda: not state.tasks and not state.connections)
        assert owner.counts["holds"] == 0


async def test_bounded_h11_consumes_chunked_body_larger_than_unread_ceiling(monkeypatch):
    import asyncio

    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    consumed = 0

    async def consume(request):
        nonlocal consumed
        async for chunk in request.stream():
            consumed += len(chunk)
        return PlainTextResponse(str(consumed))

    state, connect = _h11_fixture(Starlette(routes=[Route("/body", consume, methods=["POST"])]), monkeypatch)
    wire = connect(10003)
    payload = b"x" * (60 * 1024)
    batch = f"{len(payload):x}\r\n".encode() + payload + b"\r\n"
    try:
        wire.feed(b"POST /body HTTP/1.1\r\nHost: 127.0.0.1:5283\r\nTransfer-Encoding: chunked\r\n\r\n")
        for number in range(1, 10):
            wire.feed(batch)
            expected = number * len(payload)
            await _h11_until(lambda expected=expected: consumed == expected and wire.reading)
            assert len(wire.protocol.conn._receive_buffer) == 0
        wire.feed(b"0\r\n\r\n")
        await _h11_until(lambda: not state.tasks)
        assert consumed == 552960 > 512 * 1024
        assert b"HTTP/1.1 200 OK" in wire.output and wire.output.endswith(b"552960")
    finally:
        wire.close()
        await asyncio.wait_for(asyncio.gather(*tuple(state.tasks)), 3)
        await _h11_until(lambda: not state.tasks and not state.connections)


async def test_bounded_h11_closes_on_incompatible_unread_storage(monkeypatch):
    from starlette.applications import Starlette
    from uvicorn.protocols.http.h11_impl import H11Protocol

    def must_not_parse(*args):
        raise AssertionError("incompatible buffer reached h11")

    state, connect = _h11_fixture(Starlette(), monkeypatch)
    wire = connect(10004)
    try:
        buffer = wire.protocol.conn._receive_buffer
        with monkeypatch.context() as incompatible:
            incompatible.setattr(buffer, "_data", b"")
            incompatible.setattr(H11Protocol, "data_received", must_not_parse)
            wire.feed(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:5283\r\n\r\n")
            assert wire.closed and buffer._data == b""
        assert not state.tasks and not wire.output
    finally:
        wire.close()
        await _h11_until(lambda: not state.connections)


async def test_held_drain_does_not_retain_fired_timer_from_old_heap(local_launches, capsys):
    import asyncio
    import contextvars
    import gc
    import weakref
    from contextlib import suppress
    from types import CoroutineType, FrameType

    from latet.bridge.api import _LaunchHold
    from latet.gateway.launch_keys import HOLD_BYTES

    loop = asyncio.get_running_loop()
    assert not loop.get_debug()
    run_once = getattr(loop, "_run_once", None)
    assert run_once is not None
    run_once_code = run_once.__func__.__code__

    def heap():
        scheduled = getattr(loop, "_scheduled", None)
        assert type(scheduled) is list
        return scheduled

    assert len(heap()) <= 32
    original_heap = id(heap())
    opened = _local_open(local_launches)
    lease = local_launches.reserve_hold(opened["launch_id"], "computer", 0)
    response = _LaunchHold(lease)
    acknowledged, receiving, draining, release, fired = (asyncio.Event() for _ in range(5))
    sentinels = []

    async def receive():
        receiving.set()
        with suppress(asyncio.CancelledError):
            await asyncio.Event().wait()
        draining.set()
        await release.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("more_body"):
            acknowledged.set()

    def ignore_timer():
        pass

    def fire(payload):
        assert len(payload) == 65536
        fired.set()

    def install_first_batch():
        sentinels.append(weakref.ref(loop.call_at(loop.time() + 1, fire, bytearray(65536))))
        for _ in range(255):
            loop.call_later(60, ignore_timer).cancel()

    def helper_task(drain):
        coroutine = drain.get_coro()
        assert isinstance(coroutine, CoroutineType)
        frame = coroutine.cr_frame
        assert frame is not None
        held = frame_locals(frame)
        assert held["lease"] is lease
        children = held["tasks"]
        del held
        assert len(children) == 2
        (helper,) = (
            task for task in children if not getattr(task.get_coro(), "__qualname__", "").endswith(".disconnected")
        )
        assert helper.done()
        return helper

    def retained_sentinel_bytes(drain, heap_id, sentinel):
        for error in gc.get_referents(helper_task(drain)):
            if type(error) is not asyncio.CancelledError or error.__traceback__ is None:
                continue
            frame: FrameType | None = error.__traceback__.tb_frame
            for _ in range(16):
                if frame is None:
                    break
                if frame.f_code is run_once_code:
                    old = frame.f_locals.get("new_scheduled")
                    if type(old) is list and id(old) == heap_id:
                        assert old is not heap()
                        for timer in old:
                            if timer is sentinel():
                                args = getattr(timer, "_args", None)
                                assert type(args) is tuple and len(args) == 1 and type(args[0]) is bytearray
                                return len(args[0])
                frame = frame.f_back
        return 0

    def cancel_pending_sentinel():
        if sentinels and (timer := sentinels[0]()) is not None and getattr(timer, "_scheduled", False):
            timer.cancel()

    request = asyncio.create_task(response({"type": "http"}, receive, send))
    async with asyncio.timeout(3) as deadline:
        try:
            await acknowledged.wait()
            await receiving.wait()
            authority = local_launches.verify(opened["key"], "computer").authority
            assert authority is not None and local_launches.validate(authority)
            request.cancel()
            loop.call_soon(install_first_batch)
            await draining.wait()
            drain = response.drain_task
            assert drain is not None
            helper_task(drain)
            assert local_launches.verify(opened["key"], "computer").code == "ended"
            assert not local_launches.validate(authority) and local_launches.counts["holds"] == 1
            first_heap = id(heap())
            assert first_heap != original_heap and len(sentinels) == 1
            sentinel = sentinels[0]
            assert sentinel() is not None and any(timer is sentinel() for timer in heap()) and not fired.is_set()
            for _ in range(256):
                loop.call_later(60, ignore_timer).cancel()
            await asyncio.sleep(0)
            assert id(heap()) != first_heap and not fired.is_set()
            await fired.wait()
            await asyncio.sleep(0)
            assert all(timer is not sentinel() for timer in heap())
            retained = retained_sentinel_bytes(drain, first_heap, sentinel)
            if sentinel() is not None:
                assert retained == 65536, "live sentinel lacks the owned helper/old heap route"
            shared = {
                id(loop): "shared loop",
                id(local_launches): "separately charged registry",
                id(lease._entry): "separately charged entry",
                id(fire): "driver sentinel callback",
                id(fired): "driver sentinel completion event",
                id(receive): "injected transport callable",
                id(send): "injected transport callable",
            }
            driver_timer = getattr(deadline, "_timeout_handler", None)
            assert driver_timer is not None
            shared[id(driver_timer)] = "active driver deadline"
            for waiter in release._waiters:
                shared[id(waiter)] = "injected transport waiter"
            for variable, value in contextvars.copy_context().items():
                shared[id(variable)] = "inherited driver context variable"
                shared[id(value)] = "inherited driver context value"
            measured, _ = _LaunchGraph(shared, (receive.__code__, send.__code__)).measure([request, drain])
            with capsys.disabled():
                print(
                    "P26A_TIMER_TAIL",
                    json.dumps({"retained_sentinel_bytes": retained, "physical": 1, **measured}, sort_keys=True),
                )
            assert measured["bytes"] + 8192 <= HOLD_BYTES, "old timer heap exceeds the charged physical hold envelope"
            assert retained == 0 and sentinel() is None
        finally:
            cancel_pending_sentinel()
            release.set()
            await asyncio.gather(request, return_exceptions=True)
            if response.drain_task is not None:
                await asyncio.gather(response.drain_task, return_exceptions=True)
            assert local_launches.counts["holds"] == 0
            request = response = drain = None
            await asyncio.sleep(0)
            assert not sentinels or sentinels[0]() is None
