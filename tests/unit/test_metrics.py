"""A Latet server's metrics: the text format Prometheus reads, the counts latet.observe brings from the engine, the
recorder and the Live Reviewer's waits, the gauges read from the server (what they read of PostgreSQL and Valkey read in
the metrics' own threads, never waited for by a scrape), labels kept to the sets the registry knows, and /_latet/metrics
on a socket of its own, behind its token, which names no value, route key, token or someone's own type."""

import asyncio
import base64
import io
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from latet import observe
from latet.engine.pipeline import Engine
from latet.engine.types import MaskedText, Policy, Replacement, SlotKind
from latet.gateway.approvals import Approvals, Batch
from latet.gateway.recorder import Outcome, Recorder
from latet.server import live, logs, machines
from latet.server.live import CHAIN_FRESH, ChainVerdicts
from latet.server.machines import Limits, miss_key
from latet.server.master import MasterKey
from latet.server.metrics import CODES, KINDS, Metrics, code_label, metric_type
from latet.server.operations import METRICS, Operations, request_kind
from latet.server.settings import ServerSettings
from latet.state.local import LocalApprovals

URL = "https://latet.acme.lan"
PROMETHEUS = "http://10.0.5.10:7283"  # Prometheus asks an instance's metrics socket by its address
ROUTE = "r" * 26
TOKEN = "metrics-" + "x" * 32
GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
LINE = re.compile(
    r"^(# (HELP|TYPE) latet_[a-z0-9_]+ .+"
    r'|latet_[a-z0-9_]+(\{[a-z_]+="[^"\n]*"(,[a-z_]+="[^"\n]*")*\})? (\+Inf|-?[0-9.]+(e[-+]?[0-9]+)?))$'
)


@pytest.mark.parametrize(
    "etype", ["LATET_PERSONAL_TOKEN", "LATET_API_KEY", "LATET_ADMIN_TOKEN", "LATET_COMPUTER_KEY", "LATET_ROUTE_KEY"]
)
def test_latet_secret_types_are_fixed_metric_labels(etype):
    assert metric_type(etype, "SECRET") == etype


class Clock:
    """The metrics' clock, which a test moves."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def server(tmp_path, clock):
    token = tmp_path / "metrics.token"
    token.write_text(TOKEN + "\n")
    found = SimpleNamespace(
        settings=SimpleNamespace(url=URL, metrics_token_file=token, metrics_port=7283, vault_limit=200_000),
        master=MasterKey([secrets.token_bytes(32)]),
        spaces=SimpleNamespace(count=lambda: 3),
        identities=SimpleNamespace(enabled_count=lambda: 7),
        db=SimpleNamespace(ping=lambda: True),
        bus=SimpleNamespace(ping=lambda: True, rejected=2),
        chain=SimpleNamespace(read=lambda: "ok"),  # the beat's verdict, sealed: the chain whole
        limits=Limits(),
        metrics=Metrics(clock=clock),
        edition=SimpleNamespace(metrics=lambda metrics: None),  # E5's Metrics.watch asks the edition for its own
    )
    yield found
    found.metrics.close()


def _inner(seen=None, status=200):
    """The server's app, as a stand-in: each scope it was handed, answered ``status`` (one a call, from an iterator)."""

    async def app(scope, receive, send):
        if seen is not None:
            seen.append(scope)
        code = status if isinstance(status, int) else next(status)
        await send({"type": "http.response.start", "status": code, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app.state = SimpleNamespace()
    return app


def _untouchable(asked):
    """A stand-in that fails whatever is asked of it, and says what was."""

    class Untouchable:
        def __getattr__(self, name):
            asked.append(name)
            raise AssertionError(f"{name} was asked")

    return Untouchable()


def _scrape(client, token=TOKEN, **headers):
    return client.get(METRICS, headers={"authorization": f"Bearer {token}", **headers})


def _settled(client, clock, *wanted, token=TOKEN):
    """A scrape once what it reads of the stores is in: the first scrape starts those reads, and none waits for them;
    each store's read may come in at another scrape, so a test waits for every line it reads. Each of ``wanted`` is a
    whole line: a gauge's help can quote a line (``latet_audit_chain_known 0``) before its value is in."""
    for _ in range(300):
        answer = _scrape(client, token)
        if all(line in answer.text.splitlines() for line in wanted):
            return answer.text
        clock.advance(1.5)  # past the second in which a text is served again
        time.sleep(0.01)
    raise AssertionError(f"{wanted} never came")


def _until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("not within the time")
        time.sleep(0.01)


# --- the format, the labels, the counts --------------------------------------------------------------------------------


def test_every_line_is_prometheus_text_and_a_histogram_is_cumulative():
    metrics = Metrics()
    metrics.request_seconds.observe(0.2, "agent")
    metrics.request_seconds.observe(700.0, "agent")
    metrics.masked([("GITHUB_PAT", "SECRET")])
    text = metrics.render()
    metrics.close()
    assert all(LINE.match(line) for line in text.splitlines()), text
    assert 'latet_http_request_duration_seconds_bucket{kind="agent",le="0.25"} 1' in text
    assert 'latet_http_request_duration_seconds_bucket{kind="agent",le="+Inf"} 2' in text
    assert 'latet_http_request_duration_seconds_count{kind="agent"} 2' in text
    assert 'latet_masking_values_total{type="GITHUB_PAT"} 1' in text and text.endswith("\n")


@pytest.mark.parametrize(
    ("etype", "eclass", "label"),
    [
        ("GITHUB_PAT", "SECRET", "GITHUB_PAT"),
        ("EMAIL", "PII", "EMAIL"),
        ("CLIENT", "CUSTOM", "CUSTOM"),  # a term's type
        ("EMAIL", "CUSTOM", "CUSTOM"),  # a term typed like one of Latet's own: still someone's words
        ("Emily's clients", "SECRET", "CUSTOM"),  # someone's own words
        ("PATIENT_ID", "STRUCT", "CUSTOM"),  # a field rule's own type
        ("PERSON", "LLM", "PERSON"),  # the local reviewer's
    ],
)
def test_a_values_type_is_named_only_when_it_is_latets_own(etype, eclass, label):
    assert metric_type(etype, eclass) == label


@pytest.mark.parametrize(
    ("status", "websocket", "label"),
    [
        (200, False, "200"),
        (529, False, "529"),  # Anthropic's overloaded
        (299, False, "2xx"),  # a destination's own status: by its class
        (451, False, "4xx"),
        (799, False, "other"),
        (99, False, "other"),
        (None, False, "0"),  # ended unanswered
        (None, True, "ws"),
        (101, True, "101"),
    ],
)
def test_a_status_is_counted_as_itself_when_the_set_names_it_else_by_its_class(status, websocket, label):
    assert code_label(status, websocket=websocket) == label and label in CODES | {"other"}


def test_a_destinations_own_status_is_counted_by_its_class_never_as_it_came(server):
    statuses = iter([299, 799, 451, 529])  # an identity's own destination chooses its statuses
    client = TestClient(Operations(_inner(status=statuses), server), base_url=URL)
    for _ in range(4):
        client.get(f"/u/{ROUTE}/anthropic/v1/messages")
    requests = server.metrics.requests
    assert [requests.value("agent", code) for code in ("2xx", "other", "4xx", "529")] == [1, 1, 1, 1]
    text = server.metrics.render()
    assert 'code="299"' not in text and 'code="799"' not in text and 'code="451"' not in text


def test_every_family_the_code_names_to_valkeys_unreadable_values_is_in_the_labels_set():
    sources = [live.__file__, live.__file__.replace("live.py", "queue.py")]
    named = set()
    for source in sources:
        with open(source, encoding="utf-8") as file:
            text = file.read()
        named |= set(re.findall(r'\.met\("([a-z-]+)"\)', text))
        named |= set(re.findall(r'_opened\([^()]*, "([a-z-]+)"\)', text))
        named |= set(re.findall(r'_counts\([^()]*, "([a-z-]+)"\)', text))
    assert named and named <= live.UNREADABLE_FAMILIES, named - live.UNREADABLE_FAMILIES


def test_the_engine_reports_the_values_of_a_text_it_masks_for_the_first_time(vault, masker):
    metrics = Metrics()
    unlisten = observe.listen(metrics)
    try:
        engine = Engine(vault, masker)
        session = engine.session("s")
        engine.mask_text(f"deploy with {GH_PAT} for Acme Corp", SlotKind.USER_TEXT, session)
        engine.mask_text(f"deploy with {GH_PAT} for Acme Corp", SlotKind.USER_TEXT, session)  # sent again: cached
    finally:
        unlisten()
    assert metrics.masked_values.value("GITHUB_PAT") == 1 and metrics.masked_values.value("CUSTOM") == 1
    assert GH_PAT not in metrics.render() and "Acme" not in metrics.render()
    metrics.close()


def test_the_recorder_reports_each_exchange_and_each_stop_whatever_the_history():
    metrics = Metrics()
    unlisten = observe.listen(metrics)
    try:
        recorder = Recorder(None, history="off")
        stopped = SimpleNamespace(block_reason="sensitive values would leave in clear")
        recorder.finish(
            Outcome({"user-agent": "claude-cli/2.1.300"}, "anthropic", "/v1/messages", "http", 0.0, stopped)
        )
        recorder.finish(Outcome({"originator": "codex_cli_rs"}, "openai", "/v1/responses", "ws", 0.0, None))
        # The plugin's own check is no exchange with a provider, whatever it stopped.
        check = Outcome({"user-agent": "claude-cli/plugin"}, "anthropic", "/plugin/final-check", "plugin", 0.0, stopped)
        recorder.finish(check)
    finally:
        unlisten()
    assert metrics.exchanges.value("claude-code") == 1 and metrics.stops.value("claude-code") == 1
    assert metrics.exchanges.value("codex") == 1 and metrics.stops.value("codex") == 0
    metrics.close()


async def test_a_request_or_a_plugins_call_waiting_for_a_decision_is_counted_while_it_waits():
    metrics = Metrics()
    unlisten = observe.listen(metrics)
    approvals = Approvals()
    held = await approvals.open(Batch("cc:s", None, "claude-code", []))
    called = await approvals.open(Batch("cc:t", None, "claude-code", [], polled=True))
    try:
        waiting = asyncio.ensure_future(approvals.wait(held, 0.2))  # a request, held through the gateway
        deciding = asyncio.ensure_future(approvals.decided(called))  # a plugin's call, whose answer the plugin polls
        await asyncio.sleep(0.05)
        during = metrics._waiting
        await waiting
        approvals.abandon(called)
        await deciding
    finally:
        approvals.abandon(held)
        unlisten()
        metrics.close()
    assert during == 2 and metrics._waiting == 0


async def test_a_wait_its_backend_fails_to_end_is_no_longer_counted():
    class Failing(LocalApprovals):  # a server's queue, when Valkey's connection drops at the end of a wait
        def polled(self, batch, delta):
            if delta < 0:
                raise ConnectionError("Valkey is away")
            super().polled(batch, delta)

    metrics = Metrics()
    unlisten = observe.listen(metrics)
    approvals = Approvals(Failing())
    batch = await approvals.open(Batch("cc:s", None, "claude-code", []))
    try:
        for _ in range(3):
            with pytest.raises(ConnectionError):
                await approvals.wait(batch, 0.01)
        text = metrics.render()
    finally:
        approvals.abandon(batch)
        unlisten()
        metrics.close()
    assert metrics._waiting == 0 and "latet_approvals_waiting 0" in text  # nothing waits: none is counted


def test_a_report_that_cannot_be_made_breaks_nothing_and_counts_nothing(vault, masker, monkeypatch):
    metrics = Metrics()
    unlisten = observe.listen(metrics)
    odd = Replacement(0, 4, "abcd", "wxyz", "EMAIL", "PII", "rule", Policy.MASK, None)  # a class that is a text
    monkeypatch.setattr(masker, "mask", lambda text, *args, **kwargs: MaskedText("wxyz", [odd]))
    try:
        Recorder(None, history="off").finish(Outcome(None, "anthropic", "/v1/messages", "http", 0.0))  # no headers
        engine = Engine(vault, masker)
        found = engine.mask_text("abcd", SlotKind.USER_TEXT, engine.session("s"))
    finally:
        unlisten()
    assert found.text == "wxyz" and found.replacements == [odd]  # as with no one listening
    assert metrics.exchanges.value("other") == 0 and metrics.masked_values.value("EMAIL") == 0
    metrics.close()


def test_a_listener_that_fails_breaks_nothing():
    class Failing:
        def exchange(self, agent, stopped):
            raise RuntimeError("a listener that fails")

    failing, metrics = Failing(), Metrics()
    unlisten, also = observe.listen(failing), observe.listen(metrics)
    try:
        Recorder(None, history="off").finish(Outcome({"user-agent": "claude-cli/2.1.300"}, "anthropic", "/", "http", 0))
    finally:
        unlisten()
        also()
    assert metrics.exchanges.value("claude-code") == 1  # the other listener heard it all the same
    metrics.close()


def test_a_label_value_outside_its_set_is_counted_as_other():
    metrics = Metrics()
    exported = metrics.counter(
        "latet_reports_exported_total", "Reports exported, by format.", ("format",), allowed={"format": {"csv"}}
    )
    exported.inc("csv")
    exported.inc("Emily's report.docx")  # a caller's mistake: never a label as it came
    text = metrics.render()
    metrics.close()
    assert exported.value("csv") == 1 and exported.value("other") == 1
    assert 'latet_reports_exported_total{format="other"} 1' in text and "Emily" not in text


@pytest.mark.parametrize(
    ("labels", "allowed", "help_text"),
    [
        (("format",), {}, "A label without a set."),
        (("le",), {"le": {"1"}}, "A histogram's own label."),
        (("__name__",), {"__name__": {"x"}}, "Prometheus's own label."),
        (("Format",), {"Format": {"x"}}, "Not a label's name."),
        (("format", "format"), {"format": {"x"}}, "A label twice."),
        (("format",), {"format": {str(n) for n in range(513)}}, "More values than a label takes."),
        (("a", "b"), {"a": {str(n) for n in range(100)}, "b": {str(n) for n in range(100)}}, "Too many series."),
        ((), {}, "Two\nlines."),
        ((), {}, "A back" + chr(0x5C) + "slash."),
        ((), {}, ""),
    ],
)
def test_a_metric_whose_labels_or_help_the_registry_cannot_keep_to_is_refused(labels, allowed, help_text):
    metrics = Metrics()
    try:
        with pytest.raises(ValueError, match="latet_test_refused"):
            metrics.counter("latet_test_refused_total", help_text or "", labels, allowed=allowed)
        with pytest.raises(ValueError, match="latet_test_refused"):
            metrics.gauge("latet_test_refused", help_text or "", lambda: 1.0, labels, allowed=allowed)
    finally:
        metrics.close()


# --- /_latet/metrics: its socket, its token -----------------------------------------------------------------------------


def test_the_metrics_answer_their_token_alone_on_their_own_socket_whatever_the_host(server, clock):
    app = Operations(_inner(), server)
    prometheus, instance = TestClient(app, base_url=PROMETHEUS), TestClient(app, base_url=URL)
    refused = _scrape(prometheus, "wrong-token")
    assert refused.status_code == 401 and refused.headers["www-authenticate"] == "Bearer"
    assert refused.json() == {"error": "unauthorized"}  # nothing of the settings
    instance.get(f"/u/{ROUTE}/anthropic/v1/messages")
    text = _settled(
        prometheus,
        clock,
        "latet_identities_enabled 7",
        "latet_spaces_open 3",
        "latet_database_up 1",
        "latet_valkey_up 1",
        "latet_bus_rejected 2",
        "latet_audit_chain_ok 1",
        "latet_audit_chain_known 1",
    )
    answer = _scrape(prometheus)
    assert answer.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    for one in (refused, answer):
        assert one.headers["cache-control"] == "no-store" and one.headers["x-content-type-options"] == "nosniff"
    assert "latet_spaces_open 3" in text and "latet_build_info{version=" in text
    assert "latet_database_up 1" in text and "latet_valkey_up 1" in text
    assert "latet_bus_rejected 2" in text and "latet_audit_chain_ok 1" in text and "latet_audit_chain_known 1" in text
    assert (
        "latet_vault_near_limit" not in text
    )  # the stand-in has no vault table: that gauge is left out, the rest shown
    assert 'latet_http_requests_total{kind="agent",code="200"} 1' in text
    assert 'latet_http_requests_total{kind="metrics",code="401"} 1' in text
    assert ROUTE not in text and "wrong-token" not in text and TOKEN not in text
    assert all(LINE.match(line) for line in text.splitlines()), text


def test_a_request_is_answered_whatever_its_count_does(server, monkeypatch):
    def failing(*values, amount=1.0):
        raise RuntimeError("a registry that fails")

    monkeypatch.setattr(server.metrics.requests, "inc", failing)
    answer = TestClient(Operations(_inner(), server), base_url=URL).get(f"/u/{ROUTE}/anthropic/v1/messages")
    assert (answer.status_code, answer.text) == (200, "ok")  # a metric never fails the request it counts


def test_the_instances_own_socket_never_answers_the_metrics(server, clock):
    seen = []
    app = Operations(_inner(seen), server)
    answer = _scrape(TestClient(app, base_url=URL))
    assert answer.text == "ok" and [scope["path"] for scope in seen] == [METRICS]  # the server's app's, as any path
    answer = _scrape(TestClient(app, base_url="http://10.0.5.10:5283"))
    assert answer.text == "ok" and len(seen) == 2  # by the instance's address: its gate's to refuse
    # Counted as any other path there, never as a scrape: the metrics socket counts its own.
    text = _settled(TestClient(app, base_url=PROMETHEUS), clock, "latet_identities_enabled 7")
    assert 'latet_http_requests_total{kind="other",code="200"} 2' in text


def test_the_socket_that_took_a_request_decides_never_its_host_or_a_forwarded_header(server):
    seen = []
    app = Operations(_inner(seen), server)
    spoofed = {
        "authorization": f"Bearer {TOKEN}",
        "host": "10.0.5.10:7283",
        "x-forwarded-port": "7283",
        "x-forwarded-host": "10.0.5.10:7283",
        "forwarded": "host=10.0.5.10:7283;proto=http",
    }
    answer = TestClient(app, base_url=URL).get(METRICS, headers=spoofed)  # taken by the instance's own socket
    assert answer.text == "ok" and len(seen) == 1  # the server's app's, as any path: nothing rendered
    named = TestClient(app, base_url=PROMETHEUS).get(
        METRICS, headers={"authorization": f"Bearer {TOKEN}", "host": "latet.acme.lan"}
    )  # taken by the metrics socket, the server's own name in its Host
    assert named.status_code == 200 and "latet_build_info" in named.text and len(seen) == 1


def test_on_the_metrics_socket_nothing_else_is_answered_and_nothing_is_handed_on(server):
    asked = []
    client = TestClient(Operations(_untouchable(asked), server), base_url=PROMETHEUS)
    mine = {"authorization": f"Bearer {TOKEN}"}
    for path in ("/_latet/health", f"/u/{ROUTE}/anthropic/v1/messages", "/_latet/metrics/", "/"):
        answer = client.get(path, headers=mine)
        assert (answer.status_code, answer.json()) == (404, {"error": "there is no such route"}), path
        assert answer.headers["cache-control"] == "no-store" and answer.headers["x-content-type-options"] == "nosniff"
    assert client.post(METRICS, headers=mine, content=b"{}").status_code == 404
    assert client.head(METRICS, headers=mine).status_code == 404
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"ws://10.0.5.10:7283{METRICS}") as socket:
        socket.receive_text()  # closed before it is accepted (the client names the socket: its base URL is http's)
    assert asked == []  # the server's app was never called


def test_metrics_are_off_without_a_token_file(server):
    server.settings.metrics_token_file = None
    seen = []
    answer = _scrape(TestClient(Operations(_inner(seen), server), base_url=PROMETHEUS))
    assert answer.text == "ok" and len(seen) == 1  # no metrics socket: whatever comes is the server's app's
    assert "metrics_token_file" not in answer.text and "LATET_SERVER_METRICS" not in answer.text


def test_a_scrape_a_browser_makes_is_refused_before_its_token_is_read(server):
    client = TestClient(Operations(_inner(), server), base_url=PROMETHEUS, client=("198.51.100.7", 40000))
    for headers in ({"origin": "https://evil.example"}, {"sec-fetch-site": "same-origin"}):
        answer = _scrape(client, **headers)
        assert (answer.status_code, answer.json()) == (403, {"error": "browser requests are not accepted here"})
    for _ in range(70):  # a page's request spends nothing of its address's refused tokens
        _scrape(client, "wrong", origin="https://evil.example")
    assert _scrape(client).status_code == 200


def test_sixty_refused_tokens_a_minute_and_an_address_waits_without_valkey_being_asked(server, clock):
    asked = []
    server.bus = _untouchable(asked)
    app = Operations(_inner(), server)
    stranger = TestClient(app, base_url=PROMETHEUS, client=("203.0.113.7", 40000))
    for _ in range(60):
        assert _scrape(stranger, "wrong-token").status_code == 401
    refused = _scrape(stranger, "wrong-token")
    assert (refused.status_code, refused.headers["retry-after"]) == (429, "60")
    assert _scrape(stranger).status_code == 429  # its right token too, until the window ends: not compared
    assert asked == []  # no refused scrape asked anything of Valkey
    prometheus = TestClient(app, base_url=PROMETHEUS, client=("10.0.5.20", 40000))
    assert _scrape(prometheus).status_code == 200  # another address's right token is answered
    clock.advance(60)
    assert _scrape(stranger).status_code == 200
    server.limits.past(miss_key("198.51.100.9"), 60)  # past the shared budget, as this instance remembers it
    shared = TestClient(app, base_url=PROMETHEUS, client=("198.51.100.9", 40000))
    assert _scrape(shared).status_code == 429


def test_misses_spread_over_one_48_count_as_one_address(server):
    app = Operations(_inner(), server)
    for n in range(60):  # 60 of the 65,536 /64 of one /48, a refused token each
        client = TestClient(app, base_url=PROMETHEUS, client=(f"2001:db8:1:{n:x}::7", 40000))
        assert _scrape(client, "wrong-token").status_code == 401
    another = TestClient(app, base_url=PROMETHEUS, client=("2001:db8:1:ffff::7", 40000))
    refused = _scrape(another)
    assert (refused.status_code, refused.headers["retry-after"]) == (429, "60")  # its /48 waits, its right token too
    elsewhere = TestClient(app, base_url=PROMETHEUS, client=("2001:db8:2::7", 40000))
    assert _scrape(elsewhere).status_code == 200  # another /48


def test_the_token_is_bearer_in_any_case_and_one_authorization_header_alone(server):
    app = Operations(_inner(), server)
    client = TestClient(app, base_url=PROMETHEUS)
    assert client.get(METRICS, headers={"authorization": f"bearer {TOKEN}"}).status_code == 200
    assert client.get(METRICS, headers={"authorization": f"BEARER {TOKEN}"}).status_code == 200
    two = [("authorization", f"Bearer {TOKEN}"), ("authorization", "Bearer something-else")]
    assert client.get(METRICS, headers=two).status_code == 401  # two headers name none
    for written in (f"Bearer  {TOKEN}", f"Basic {TOKEN}", TOKEN, f"Bearer {TOKEN}x", "Bearer " + TOKEN[:-1]):
        assert client.get(METRICS, headers={"authorization": written}).status_code == 401, written
    assert client.get(METRICS).status_code == 401
    shown = repr(vars(app)) + repr(app) + repr(vars(server.metrics)) + repr(server.metrics) + repr(server.settings)
    assert TOKEN not in shown  # the token is kept nowhere: a keyed hash under a key of the process's


def test_a_refused_scrape_is_said_once_a_minute_and_never_with_its_header(server, caplog):
    client = TestClient(Operations(_inner(), server), base_url=PROMETHEUS)
    with caplog.at_level(logging.INFO):
        for _ in range(5):
            _scrape(client, "a-pasted-admin-token")
    said = [record for record in caplog.records if record.name.startswith("latet")]
    assert len(said) == 1 and "refused" in said[0].getMessage()
    assert "a-pasted-admin-token" not in caplog.text and TOKEN not in caplog.text


@pytest.mark.parametrize(
    "written",
    [
        "x" * 31,  # too short to be a secret
        "a metrics token with spaces in it, long enough",
        "metrics-" + "\N{LATIN SMALL LETTER E WITH ACUTE}" * 32,  # not ASCII: a scrape could never send it as written
        "latet_adm_" + "a" * 43,  # an administration token: a scrape over http would carry it in clear
        "x" * 40 + "latet_pat_" + "b" * 43,
        "latet_key_" + "c" * 43,
        "metrics-" + "y" * 32 + "\nmetrics-" + "z" * 32,  # two lines
        "metrics-" + "y" * 32 + "\x07",
        "x" * 1025,
        "",
        "   \n",
    ],
)
def test_a_metrics_token_that_is_not_one_stops_the_start_without_quoting_it(server, written):
    server.settings.metrics_token_file.write_text(written, "utf-8")
    with pytest.raises(ValueError, match="metrics_token_file") as raised:
        Operations(_inner(), server)
    words = str(raised.value)
    assert (not written.strip() or written.strip()[:20] not in words) and raised.value.__context__ is None


def test_a_metrics_token_that_is_another_secret_stops_the_start(server, tmp_path):
    key = b"\xfb\xff" * 16  # its base64 holds + and /, which the URL's alphabet writes - and _
    server.master = MasterKey([secrets.token_bytes(32), key])
    written = base64.b64encode(key).decode()
    server.settings.metrics_token_file.write_text(written + "\n", "utf-8")  # a version of the master key
    with pytest.raises(ValueError, match="master key") as raised:
        Operations(_inner(), server)
    assert written not in str(raised.value)
    unpadded = tmp_path / "unpadded.token"
    unpadded.write_text(base64.urlsafe_b64encode(key).decode().rstrip("="), "utf-8")
    server.settings.metrics_token_file = unpadded
    with pytest.raises(ValueError, match="master key"):
        Operations(_inner(), server)
    server.settings.master_key_file = tmp_path / "master.key"
    server.settings.master_key_file.write_text("metrics-" + "w" * 40, "utf-8")
    server.settings.metrics_token_file = server.settings.master_key_file
    with pytest.raises(ValueError, match="master_key_file"):  # the very file another secret setting names
        Operations(_inner(), server)


@pytest.mark.parametrize(
    ("setting", "address"),
    [
        ("database_url", "postgresql://latet:{}@db.acme.lan:5432/latet?sslmode=verify-full&sslrootcert=system"),
        ("database_url", "postgresql://latet@db.acme.lan:5432/latet?password={}"),  # libpq reads it there too
        ("valkey_url", "rediss://:{}@cache.acme.lan:6379/0"),
        ("valkey_url", "rediss://cache.acme.lan:6379/0?password={}"),  # and redis-py
    ],
)
def test_a_metrics_token_that_is_a_stores_password_stops_the_start(server, setting, address):
    password = "copied-" + "7f3a" * 10 + "/+"  # one secret made once and copied into two files
    setattr(server.settings, setting, address.format(quote(password, safe="")))
    server.settings.metrics_token_file.write_text(password + "\n", "utf-8")
    with pytest.raises(ValueError, match=setting) as raised:
        Operations(_inner(), server)
    assert "7f3a" not in str(raised.value) and raised.value.__context__ is None


def test_a_metrics_token_that_is_the_reviewers_key_stops_the_start(server):
    server.reviewer_key = "reviewer-" + "k" * 40  # Task 21's: the shared reviewer's key, read at the start
    server.settings.metrics_token_file.write_text(server.reviewer_key, "utf-8")
    with pytest.raises(ValueError, match="reviewer_key_file") as raised:
        Operations(_inner(), server)
    assert "kkkk" not in str(raised.value)
    server.reviewer_key = None  # a server with no key for its reviewer: nothing to compare
    server.settings.metrics_token_file.write_text("reviewer-" + "k" * 40, "utf-8")
    Operations(_inner(), server)


def test_a_metrics_token_file_that_cannot_be_read_stops_the_start(server, tmp_path):
    server.settings.metrics_token_file = tmp_path / "absent.token"
    with pytest.raises(ValueError, match="metrics_token_file names a file that cannot be read"):
        Operations(_inner(), server)
    large = tmp_path / "large.token"
    large.write_bytes(b"m" * 5000)
    server.settings.metrics_token_file = large
    with pytest.raises(ValueError, match="more than 4096 bytes"):  # read no further than that
        Operations(_inner(), server)
    binary = tmp_path / "binary.token"
    binary.write_bytes(bytes(range(0xC0, 0xFF)))
    server.settings.metrics_token_file = binary
    with pytest.raises(ValueError, match="metrics_token_file") as raised:
        Operations(_inner(), server)
    assert raised.value.__context__ is None


def test_a_token_file_that_is_no_regular_file_stops_the_start_unread(server, tmp_path):
    """A device or a pipe named there (a file that never ends, a FIFO nobody writes): refused before it is read, as
    every other secret's file is, never waited on (the instance's start hung before any socket was bound)."""
    if os.name == "nt":
        special = Path("NUL")
    else:
        special = tmp_path / "metrics.fifo"
        os.mkfifo(special)
    server.settings.metrics_token_file = special
    with pytest.raises(ValueError, match="metrics_token_file names a file that cannot be read: it is not a regular"):
        Operations(_inner(), server)


def test_the_metrics_have_a_port_of_their_own():
    found = ServerSettings(url=URL, database_url="postgresql://127.0.0.1/latet", valkey_url="redis://127.0.0.1/0")
    assert (found.metrics_host, found.metrics_port) == ("127.0.0.1", 7283)
    with pytest.raises(ValidationError, match="metrics_port"):
        ServerSettings(
            url=URL, database_url="postgresql://127.0.0.1/latet", valkey_url="redis://127.0.0.1/0", port=7300,
            metrics_port=7300,
        )  # fmt: skip
    for wrong in (0, 65536):
        with pytest.raises(ValidationError):
            ServerSettings(
                url=URL, database_url="postgresql://127.0.0.1/latet", valkey_url="redis://127.0.0.1/0",
                metrics_port=wrong,
            )  # fmt: skip


# --- what a scrape reads, and what it waits for -------------------------------------------------------------------------


def test_a_later_part_adds_its_metrics_to_the_servers_and_they_are_served(server, clock):
    exported = server.metrics.counter(
        "latet_reports_exported_total", "Reports exported, by format.", ("format",), allowed={"format": {"csv", "pdf"}}
    )
    exported.inc("csv")
    exported.inc("csv", amount=2)
    server.metrics.gauge(
        "latet_siem_queued", "Events waiting for a SIEM output.", lambda: {("splunk",): 12}, ("output",),
        allowed={"output": {"splunk"}},
    )  # fmt: skip
    server.metrics.gauge("latet_reports_kept", "Reports kept, read from PostgreSQL.", lambda: 4, every=60.0)
    with pytest.raises(ValueError, match="latet_<area>_<what>"):
        server.metrics.counter("latet_reports_exported_total", "Twice.")
    with pytest.raises(ValueError, match="latet_<area>_<what>"):
        server.metrics.gauge("reports", "Not Latet's name.", lambda: 1.0)
    text = _settled(TestClient(Operations(_inner(), server), base_url=PROMETHEUS), clock, "latet_reports_kept 4")
    assert 'latet_reports_exported_total{format="csv"} 3' in text and 'latet_siem_queued{output="splunk"} 12' in text
    assert all(LINE.match(line) for line in text.splitlines()), text


def test_two_apps_of_one_server_serve_its_one_registry(server):
    first, second = Operations(_inner(), server), Operations(_inner(), server)
    assert first.metrics is second.metrics is server.metrics
    assert server.metrics.render().count("# TYPE latet_spaces_open gauge") == 1


def test_a_metric_that_fails_is_left_out_alone(server, caplog):
    metrics = server.metrics
    one = {"kind": {"a"}}
    metrics.gauge("latet_test_text", "A reading that is a text.", lambda: "n/a")
    metrics.gauge("latet_test_pairs", "Two values for one label.", lambda: {("a", "b"): 1.0}, ("kind",), allowed=one)
    metrics.gauge("latet_test_none", "None for a value.", lambda: {("a",): None}, ("kind",), allowed=one)
    metrics.gauge("latet_test_raises", "A reading that raises.", lambda: 1 / 0)
    metrics.gauge("latet_test_bool", "A reading that is a truth.", lambda: True)
    metrics.gauge("latet_test_nan", "A reading that is no number.", lambda: float("nan"))
    with caplog.at_level(logging.WARNING):
        answer = _scrape(TestClient(Operations(_inner(), server), base_url=PROMETHEUS))
    assert answer.status_code == 200 and "latet_spaces_open 3" in answer.text and "latet_test_" not in answer.text
    said = caplog.text
    assert "latet_test_raises" in said and "ZeroDivisionError" in said and "latet_test_text" in said


def test_a_store_that_does_not_answer_holds_no_scrape_and_is_asked_once(server, clock):
    stuck, pings, counts = threading.Event(), [], []
    server.db = SimpleNamespace(ping=lambda: pings.append(1) or stuck.wait(5))
    server.identities = SimpleNamespace(enabled_count=lambda: counts.append(1) or stuck.wait(5))
    server.bus = SimpleNamespace(ping=lambda: False, rejected=0)  # answered: the connection is lost
    metrics = Metrics(clock=clock, probe_timeout=0.05)
    metrics.watch(server)
    try:
        started = time.monotonic()
        first = metrics.render()
        took = time.monotonic() - started
        for _ in range(7):
            clock.advance(31)  # past the gauges' interval each time
            started = time.monotonic()
            last = metrics.render()
            assert time.monotonic() - started < 0.2  # a probe still out is not waited for again
    finally:
        stuck.set()
        time.sleep(0.1)
        metrics.close()
    assert took < 0.05 + 0.2 and "latet_database_up 0" in first and "latet_valkey_up 0" in first
    assert "latet_identities_enabled" not in first  # never read: left out
    assert len(pings) == 1 and len(counts) == 1  # one ping out the whole time, nothing queued behind it
    seconds = float(re.search(r"^latet_database_seconds (\S+)$", last, re.M)[1])
    assert "latet_database_up 0" in last and seconds >= 7 * 31  # the time since that ping began


def test_a_store_read_that_fails_is_asked_again_an_interval_later_never_at_each_scrape(server, clock, monkeypatch):
    calls = []

    def failing(db, limit):
        calls.append(1)
        raise RuntimeError("PostgreSQL did not answer")

    monkeypatch.setattr("latet.server.vault.vault_usage", failing)
    metrics = server.metrics
    metrics.watch(server)
    for _ in range(10):  # ten renders within 300 s
        metrics.render()
        _until(lambda: calls)
        clock.advance(25)
    assert len(calls) == 1
    clock.advance(60)
    metrics.render()
    _until(lambda: len(calls) == 2)


async def test_fifty_scrapes_at_once_make_one_render(server):
    app = Operations(_inner(), server)
    renders, render = [], server.metrics.render

    def counted():
        renders.append(1)
        time.sleep(0.1)
        return render()

    server.metrics.render = counted
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=PROMETHEUS) as client:
        mine = {"authorization": f"Bearer {TOKEN}"}
        answers = await asyncio.gather(*(client.get(METRICS, headers=mine) for _ in range(50)))
        again = await client.get(METRICS, headers=mine)  # in the same second: the same text
    assert [answer.status_code for answer in answers] == [200] * 50 and len(renders) == 1
    assert len({answer.text for answer in [*answers, again]}) == 1


def test_a_chain_read_that_hangs_changes_neither_a_renders_time_nor_the_chains_gauges_until_their_time(server, clock):
    stuck, answers = threading.Event(), iter(["ok"])
    server.chain = SimpleNamespace(read=lambda: next(answers, None) or stuck.wait(30))
    metrics = server.metrics
    metrics.watch(server)
    try:
        text = _settled(TestClient(Operations(_inner(), server), base_url=PROMETHEUS), clock, "latet_audit_chain_ok 1")
        read_at = clock.now  # its reading came by now
        clock.advance(31)  # its interval: the read is asked again, and hangs
        started = time.monotonic()
        text = metrics.render()
        assert time.monotonic() - started < 0.5
        assert "latet_audit_chain_ok 1" in text and "latet_audit_chain_known 1" in text
        clock.now = read_at + 170  # its reading is still served for three minutes
        text = metrics.render()
        assert "latet_audit_chain_ok 1" in text and "latet_audit_chain_known 1" in text
        clock.now = read_at + 185  # and no longer
        text = metrics.render()
        assert "latet_audit_chain_ok" not in text and "latet_audit_chain_known 0" in text.splitlines()
    finally:
        stuck.set()


def _served_until(server, clock, wanted, *, until, longer):
    """``wanted`` once its store answered, still served ``until`` seconds after while every read since fails, and no
    longer ``longer`` seconds after."""
    client = TestClient(Operations(_inner(), server), base_url=PROMETHEUS)
    _settled(client, clock, wanted)
    read_at = clock.now
    while clock.now < read_at + until:  # a scrape every 30 seconds, each read failing
        clock.now = min(clock.now + 30, read_at + until)
        assert wanted in server.metrics.render(), clock.now - read_at
    clock.now = read_at + longer
    return server.metrics.render()


def test_a_store_reading_is_served_for_three_minutes_while_its_store_does_not_answer(server, clock):
    answers = iter([7])

    def count():
        if (found := next(answers, None)) is None:
            raise ConnectionError("PostgreSQL is away")
        return found

    server.identities = SimpleNamespace(enabled_count=count)
    text = _served_until(server, clock, "latet_identities_enabled 7", until=170, longer=185)
    assert "latet_identities_enabled" not in text and "latet_database_up 1" in text


def test_the_vaults_near_their_limit_are_served_for_ten_minutes(server, clock, monkeypatch):
    answers = iter([[("u_emilycarte", 190_000)]])

    def usage(db, limit):
        if (found := next(answers, None)) is None:
            raise ConnectionError("PostgreSQL is away")
        return found

    monkeypatch.setattr("latet.server.vault.vault_usage", usage)
    text = _served_until(server, clock, "latet_vault_near_limit 1", until=590, longer=605)
    assert "latet_vault_near_limit" not in text


def test_a_later_parts_store_reading_is_served_for_three_minutes_or_twice_its_interval(server, clock):
    for name, every, until, longer in (("latet_test_often", 10.0, 170, 185), ("latet_test_seldom", 120.0, 230, 245)):
        answers = iter([4])

        def kept(answers=answers):
            if (found := next(answers, None)) is None:
                raise ConnectionError("PostgreSQL is away")
            return found

        server.metrics.gauge(name, "Kept, read from PostgreSQL.", kept, every=every)
        text = _served_until(server, clock, f"{name} 4", until=until, longer=longer)
        assert f"{name} 4" not in text


def test_the_chains_state_is_always_said_and_a_verdict_past_its_freshness_is_left_out(server, clock):
    wall = Clock()
    kept: dict[str, bytes] = {}
    bus = SimpleNamespace(
        key=lambda name: f"latet:{name}",
        client=SimpleNamespace(get=kept.get, set=lambda key, value, ex: kept.__setitem__(key, value)),
    )  # Valkey, as a dict
    server.chain = ChainVerdicts(bus, server.master, clock=wall)
    server.chain.keep(SimpleNamespace(ok=False, broken_at=12))  # the beat found the chain broken
    client = TestClient(Operations(_inner(), server), base_url=PROMETHEUS)
    text = _settled(client, clock, "latet_audit_chain_ok 0")
    assert "latet_audit_chain_known 1" in text
    wall.advance(CHAIN_FRESH + 1)  # no check since: the hourly check stopped
    clock.advance(61)
    text = _settled(client, clock, "latet_audit_chain_known 0")
    assert "latet_audit_chain_ok" not in text  # left out, and said by its state: no alert clears by itself


def test_a_chain_that_cannot_be_read_is_not_known(server, clock):
    def unreadable():
        raise ConnectionError("Valkey is away")

    server.chain = SimpleNamespace(read=unreadable)
    text = _settled(TestClient(Operations(_inner(), server), base_url=PROMETHEUS), clock, "latet_audit_chain_known 0")
    assert "latet_audit_chain_ok" not in text


def test_a_chain_refresh_between_its_gauges_never_mixes_verdicts(server, clock, monkeypatch):
    wall = Clock()
    kept: dict[str, bytes] = {}
    bus = SimpleNamespace(
        key=lambda name: f"latet:{name}",
        client=SimpleNamespace(get=kept.get, set=lambda key, value, ex: kept.__setitem__(key, value)),
    )
    server.chain = ChainVerdicts(bus, server.master, clock=wall)
    server.chain.keep(SimpleNamespace(ok=False, broken_at=12))
    metrics = server.metrics
    metrics.watch(server)
    metrics.render()  # start the real asynchronous reading
    reading = next(item for item in metrics._readings if item.name == "latet_audit_chain")
    assert reading._out is not None
    reading._out.result(timeout=5)
    initial = metrics.render()
    assert "latet_audit_chain_ok 0" in initial and "latet_audit_chain_known 1" in initial

    started, release = threading.Event(), threading.Event()
    answers, first_values = [], []
    actual_read = server.chain.read

    def refreshed():
        started.set()
        assert release.wait(5)
        answer = actual_read()  # the actual sealed verdict, now past its wall-clock freshness
        answers.append(answer)
        return answer

    monkeypatch.setattr(server.chain, "read", refreshed)
    gauge = next(item for item in metrics._all if item.name == "latet_audit_chain_ok")
    first_lines = gauge.lines

    def finish_between_gauges(*args, **kwargs):
        lines = first_lines(*args, **kwargs)
        value = next((float(line.split()[-1]) for line in lines if line.startswith("latet_audit_chain_ok ")), None)
        first_values.append(value)
        assert started.wait(5)
        release.set()
        assert reading._out is not None
        reading._out.result(timeout=5)  # published before the next gauge is read; no sleep or polling
        return lines

    monkeypatch.setattr(gauge, "lines", finish_between_gauges)
    wall.advance(CHAIN_FRESH + 1)
    clock.advance(31)
    try:
        text = metrics.render()
    finally:
        release.set()
        if reading._out is not None:
            reading._out.result(timeout=5)
        monkeypatch.setattr(gauge, "lines", first_lines)
    assert first_values == [0.0] and answers == ["unknown"]
    observed = tuple(
        next((line.split()[-1] for line in text.splitlines() if line.startswith(name + " ")), None)
        for name in ("latet_audit_chain_ok", "latet_audit_chain_known")
    )
    assert observed in (("0", "1"), (None, "0")), observed
    settled = metrics.render()
    assert "latet_audit_chain_ok" not in settled and "latet_audit_chain_known 0" in settled


def test_valkeys_unreadable_values_are_answered_while_another_thread_meets_them(server, monkeypatch):
    unreadable = live._Unreadable()
    monkeypatch.setattr(live, "UNREADABLE", unreadable)
    families = [*sorted(live.UNREADABLE_FAMILIES), "a family the code never names"]
    stop = threading.Event()

    def meet():
        count = 0
        while not stop.is_set():
            unreadable.met(families[count % len(families)])
            count += 1

    thread = threading.Thread(target=meet)
    metrics = server.metrics
    metrics.watch(server)
    thread.start()
    try:
        texts = [metrics.render() for _ in range(30)]
    finally:
        stop.set()
        thread.join()
    assert all("latet_valkey_unreadable{family=" in text for text in texts[1:])
    last = metrics.render()
    assert 'latet_valkey_unreadable{family="other"}' in last and "never names" not in last
    assert "a family the code never names" not in unreadable.snapshot()  # counted as other where it is met, too


def test_the_credentials_waiting_are_counted(server, monkeypatch):
    monkeypatch.setattr(machines._WAITING, "count", 37)
    metrics = server.metrics
    metrics.watch(server)
    assert "latet_credentials_waiting 37" in metrics.render()


def test_what_the_log_drops_is_counted_while_its_output_is_stalled(server, monkeypatch):
    monkeypatch.setattr(logs, "_DROPPED", logs._Dropped())
    released = threading.Event()

    class Stalled(io.StringIO):
        def write(self, text):
            released.wait(10)
            return super().write(text)

    writer = logs._Writer(Stalled())
    metrics = server.metrics
    metrics.watch(server)
    try:
        writer._put("the line being written\n")
        _until(lambda: writer._writing)
        for _ in range(logs.QUEUE_LINES + 3):
            writer._put("a line\n")
        text = metrics.render()
    finally:
        released.set()
        writer.close()
    assert "latet_log_dropped_total 3" in text and "# TYPE latet_log_dropped_total counter" in text


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        (f"/u/{ROUTE}/anthropic/v1/messages", "agent"),
        (f"/u/{ROUTE}/_latet/engine/v1/mask", "plugin"),
        (f"/u/{ROUTE}/_latet/admin/destinations", "computer"),
        ("/_latet/server/v1/whoami", "computer"),
        ("/_latet/server/v1/admin/status", "admin"),
        ("/_latet/server/v1/admin", "admin"),
        ("/_latet/api/v1/me", "dashboard"),
        ("/_latet/ui/", "dashboard"),
        ("/_latet/ready", "health"),
        ("/_latet/health", "health"),
        ("/_latet/metrics", "other"),  # on the instance's own port: the metrics socket counts its scrapes itself
        ("/favicon.ico", "other"),
        ("/_latet/uix", "other"),
    ],
)
def test_a_request_is_counted_by_its_kind_never_its_path(path, kind):
    assert request_kind(path) == kind and kind in KINDS
