"""A Latet server's logs: one line per record, as text or JSON, on the standard output; never a route key, a one-time
code, a token, a key, a password or what a library quotes, wherever a line could hold one; no line that anyone who
sends a request can write; one line per request and per WebSocket, uvicorn's own off; Latet's level opens Latet's lines
alone."""

import asyncio
import contextlib
import errno
import gc
import io
import json
import logging
import os
import socket
import ssl
import sys
import threading
import time
import warnings
from datetime import datetime, timedelta

import httpx
import psycopg
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.client import connect

from latet.logline import described, redacted
from latet.projects import project_id
from latet.server import logs as logs_module
from latet.server.egress import EgressRefused
from latet.server.logs import (
    LINE_LIMIT,
    PATH_LIMIT,
    REFUSED,
    JsonFormatter,
    RequestLog,
    TextFormatter,
    configure_logging,
    flush,
    saved,
)
from latet.server.tokens import ADMIN_PREFIX, COMPUTER_PREFIX, KEY_PREFIX, TOKEN_PREFIX, new_secret, route_key

TOKEN, KEY, ADMIN = new_secret(TOKEN_PREFIX), new_secret(KEY_PREFIX), new_secret(ADMIN_PREFIX)
COMPUTER = new_secret(COMPUTER_PREFIX)  # a computer's key (R16)
ROUTE = route_key(TOKEN)
SECRETS = (ROUTE, "Xy7-code_42", TOKEN, KEY, ADMIN, COMPUTER, "s3cret-pw", "v4lkey-pw")
EMAIL = "emily.carter@acme.com"
BACKSLASH = chr(0x5C)


class _Written(io.StringIO):
    """An output configure_logging writes to: read, it holds every line logged before (its writer's thread writes
    them, and is waited for)."""

    def getvalue(self):
        for handler in logging.getLogger().handlers:
            handler.flush()
        return super().getvalue()


@pytest.fixture
def logs():
    """What configure_logging writes, read back; the logging as it was, after the test."""
    with saved():
        yield _Written()


def _lines(stream):
    """The JSON lines written, each read back."""
    return [json.loads(line) for line in stream.getvalue().splitlines()]


async def _ok(request):
    return PlainTextResponse("ok")


async def _fails(request):
    raise ValueError(f"could not read the address {EMAIL}")


async def _echo(websocket):
    await websocket.accept()
    await websocket.send_text(await websocket.receive_text())
    await websocket.close()


async def _refuse(websocket):
    await websocket.close(code=4400)


def _app():
    """A server's app in small: RequestLog in front of a few routes."""
    routes = [
        Route("/fails", _fails),
        WebSocketRoute("/u/{route}/refused", _refuse),
        WebSocketRoute("/{path:path}", _echo),
        Route("/{path:path}", _ok, methods=["GET", "POST", "PUT"]),
    ]
    return RequestLog(Starlette(routes=routes))


@contextlib.contextmanager
def _uvicorn(app, **config):
    """A real uvicorn on 127.0.0.1, as `latet server run` starts it (its logging left to configure_logging): its
    port."""
    settings = {"host": "127.0.0.1", "port": 0, "log_config": None, "ws": "websockets-sansio", **config}
    server = uvicorn.Server(uvicorn.Config(app, **settings))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.01)
    try:
        yield server.servers[0].sockets[0].getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(10)


def _scope(path, method="GET"):
    """An HTTP request's scope as a server gives it, the path as it was sent."""
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
    }


async def _call(app, scope):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    await app(scope, receive, send)


def _answering(status):
    """An app that answers every request with ``status``."""

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return app


# --- the redaction -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        f"POST /u/{ROUTE}/anthropic/v1/messages 200",
        "GET /_latet/ui/login?code=Xy7-code_42 303",
        f"enrolled with {TOKEN}",
        f"x-latet-token: {KEY}",
        f'{{"x-latet-admin": "{ADMIN}"}}',
        f"found {COMPUTER} in a file",
        "postgresql://latet:s3cret-pw@db.acme.lan:5432/latet",
        "redis://:v4lkey-pw@cache.acme.lan:6379/0",
    ],
    ids=[
        "route",
        "login-code",
        "personal-token",
        "api-key",
        "admin-token",
        "computer-key",
        "postgres-password",
        "valkey-password",
    ],
)
def test_no_secret_survives_the_redaction(line):
    out = redacted(line)
    assert out != line and not any(secret in out for secret in SECRETS)


@pytest.mark.parametrize(
    ("line", "secrets"),
    [
        ("redis://cache.acme.lan:6379/0?password=v4lkey-pw", ["v4lkey-pw"]),
        ("postgresql://db.acme.lan/latet?user=latet&password=s3cret-pw", ["s3cret-pw"]),
        (f"GET /U/{ROUTE}/anthropic/v1/messages", [ROUTE]),
        (f"GET /_latet/ui/login?next=%2Fu%2F{ROUTE}%2Fanthropic", [ROUTE]),
        (f'{{"path": "{BACKSLASH}/u{BACKSLASH}/{ROUTE}{BACKSLASH}/v1/messages"}}', [ROUTE]),
        (f"Bearer_{TOKEN}", [TOKEN]),
        (f"[('x-latet-token', '{TOKEN}')]", [TOKEN]),
        ("[(b'x-latet-admin', b'hunter2-in-the-wrong-field')]", ["hunter2-in-the-wrong-field"]),
        ("[(b'x-latet-computer-key', b'hunter2-in-the-wrong-field')]", ["hunter2-in-the-wrong-field"]),
        ("x-latet-computer-key: bOld-key", ["bOld-key", "Old-key"]),
        ("x-latet-token: bOld-password", ["bOld-password", "Old-password"]),
        ("authorization: Bearer sk-ant-api03-PROVIDERKEY", ["Bearer sk-ant", "PROVIDERKEY"]),
        ("proxy-authorization: Basic dXNlcjpwYXNz", ["dXNlcjpwYXNz"]),
        ("x-api-key: sk-ant-api03-PROVIDERKEY", ["PROVIDERKEY"]),
        ("api-key: azure-key-123", ["azure-key-123"]),
        ("cookie: latet_session=abcdef", ["abcdef"]),
        ("set-cookie: __cf_bm=PROVIDERCOOKIE; path=/", ["PROVIDERCOOKIE"]),
        ("[(b'set-cookie', b'__cf_bm=PROVIDERCOOKIE')]", ["PROVIDERCOOKIE"]),
        ("https://gw.acme.lan/v1beta/models/gemini-pro:generateContent?key=AIzaSyFAKEKEY", ["AIzaSyFAKEKEY"]),
        ("/openai/v1/realtime?model=gpt-realtime&api-key=azure-key-in-the-query", ["azure-key-in-the-query"]),
        ("/x?api_key=k-one&token=t-two&access_token=a-three&sig=s-four", ["k-one", "t-two", "a-three", "s-four"]),
        ("/o?X-Amz-Signature=sig1&X-Amz-Credential=AKIAFAKE&X-Amz-Security-Token=tok1", ["sig1", "AKIAFAKE", "tok1"]),
        ("{'query_string': b'code=Xy7-code_42'}", ["Xy7-code_42"]),
    ],
    ids=[
        "valkey-query-password",
        "postgres-query-password",
        "uppercase-route",
        "escaped-route-query",
        "escaped-route-json",
        "bearer-token",
        "token-header-pairs",
        "admin-bytes-header",
        "computer-key-bytes-header",
        "computer-key-header-prefix",
        "token-bytes-prefix",
        "provider-bearer",
        "proxy-basic",
        "provider-api-key",
        "azure-api-key",
        "cookie",
        "set-cookie",
        "set-cookie-bytes",
        "gemini-query-key",
        "realtime-query-key",
        "multiple-query-secrets",
        "aws-query-secrets",
        "login-query-bytes",
    ],
)
def test_the_other_forms_of_a_secret_are_taken_out_too(line, secrets):
    out = redacted(line)
    assert not any(secret in out for secret in secrets)


@pytest.mark.parametrize(
    ("line", "secrets"),
    [
        ("/oauth/token?grant_type=x&client_secret=s3cr3t-of-acme", ["s3cr3t-of-acme"]),
        ("/oauth/token?refresh_token=rt-of-acme&id_token=it-of-acme", ["rt-of-acme", "it-of-acme"]),
        ("/b/o?X-Goog-Signature=goog-sig-123&X-Goog-Credential=gc-of-acme", ["goog-sig-123", "gc-of-acme"]),
        ("host=db.acme.lan user=latet password=s3cret-pw dbname=latet", ["s3cret-pw"]),
        ("host=db.acme.lan password = 's3cret pw, with spaces' dbname=latet", ["s3cret", "with spaces"]),
        ("x-amz-security-token: FQoGZXIvYXdzEFAKETOKEN", ["FQoGZXIvYXdzEFAKETOKEN"]),
        ('{"api_key": "sk-proj-FAKEKEY", "x": 1}', ["sk-proj-FAKEKEY"]),
        (f'{{"password": "s3cret {BACKSLASH}"quoted{BACKSLASH}" pw"}}', ["s3cret", "quoted"]),
        ('{"token": "tok-of-acme", "client_secret": "cs-of-acme"}', ["tok-of-acme", "cs-of-acme"]),
        ("/x?code%3DXy7-code_42", ["Xy7-code_42"]),
        ("GET /_latet/ui/login?next=%2F_latet%2Fui%3Fcode%3DXy7-code_42%26x%3D1", ["Xy7-code_42"]),
        (
            f'{{{BACKSLASH}"authorization{BACKSLASH}": {BACKSLASH}"Bearer sk-ant-PROVIDERKEY{BACKSLASH}"}}',
            ["PROVIDERKEY"],
        ),
        (f'{{{BACKSLASH}"x-latet-admin{BACKSLASH}": {BACKSLASH}"hunter2-in-the-wrong-field{BACKSLASH}"}}', ["hunter2"]),
        (f'{{{BACKSLASH}"api_key{BACKSLASH}": {BACKSLASH}"sk-proj-FAKEKEY{BACKSLASH}"}}', ["sk-proj-FAKEKEY"]),
        ("authorization: Bearer\r\n sk-ant-PROVIDERKEY\r\nx-other: 1", ["PROVIDERKEY"]),
        ("cookie: a=1;\r\n\tsession=PROVIDERCOOKIE", ["PROVIDERCOOKIE"]),
        ("authorization Bearer sk-ant-PROVIDERKEY", ["PROVIDERKEY"]),
        ("proxy-authorization  Basic dXNlcjpwYXNz", ["dXNlcjpwYXNz"]),
        ("redis://:p4ss@word@cache.acme.lan:6379/0", ["p4ss", "word"]),
        ("postgresql://latet:a@b@c@db.acme.lan/latet", ["a@b@c", "b@c"]),
    ],
)
def test_the_forms_a_line_could_hold_one_day_are_taken_out_as_well(line, secrets):
    """What no line of Latet's carries today (it logs no header, body or address, and a library's arguments by their
    type), taken out all the same, as a net: OAuth's and Google's query credentials, libpq's ``password=`` in key=value
    form, AWS's session token as a header, a JSON field that names a credential, a query whose ``=`` is escaped, JSON
    whose quotes are escaped, a header folded on the next line or with no separator, a password holding an ``@``."""
    out = redacted(line)
    assert not any(secret in out for secret in secrets), out


def test_what_only_looks_like_those_forms_is_left_as_it_is():
    for line in (
        "the upstream answered status code=500",
        '{"input_tokens": 12, "token": 5, "tokens": "many"}',
        "authorization failed: no token was given",
        "https://latet.acme.lan:8443/@scope/package",
        "a password was refused (by its place: password 2)",
    ):
        assert redacted(line) == line


@pytest.mark.parametrize(
    ("line", "out"),
    [
        ("x-latet-token: bOld-password", "x-latet-token: ***"),  # not one letter of it, though it starts like bytes
        ("authorization: Bearer sk-ant-api03-PROVIDERKEY", "authorization: ***"),
        ("[(b'x-latet-admin', b'hunter2')]", "[(b'x-latet-admin', b'***')]"),
    ],
)
def test_a_header_value_goes_whole(line, out):
    assert redacted(line) == out


def test_the_redaction_leaves_the_rest_as_it_is():
    for line in (
        "POST /openai/v1/chat/completions 200",
        "GET /_latet/api/v1/exchanges?limit=50",
        "https://latet.acme.lan:8443/x",
    ):
        assert redacted(line) == line
    assert redacted("/bedrock/us-east-1/session/" + "a" * 32 + "/model/m/invoke").count("***") == 1


N = 64_000
HOSTILE = {  # 64,000 characters each, of what makes a pattern start again and again
    "dotted words": "a." * (N // 2),  # 2.4 seconds with the plan's pattern for an address's password
    "addresses with no end": "://a:" * (N // 5),
    "an address with no @": "://a:" + "a" * N,
    "routes": "/u/" * (N // 3),
    "routes run together": "/u" * (N // 2),
    "escaped routes": "%2Fu%2F" * (N // 7),
    "codes": "?code=" * (N // 6),
    "quotes": "'" * N,
    "the start of a secret": "latet_pat_" * (N // 10),
    "token headers": "x-latet-token " * (N // 14),
    "a token header and blanks": "x-latet-token:" + " " * N,
    "credential headers": "cookie " * (N // 7),
    "a credential header and blanks": "authorization" + " " * N,
    "escaped credential names": "%3Fcode%3D" * (N // 10),
    "key=value passwords": "password=" * (N // 9),
    "a quoted key=value password with no end": "password='" + "a" * N,
    "JSON credential fields": '"token": "' * (N // 10),
    "escaped JSON credential fields": f'{BACKSLASH}"token{BACKSLASH}": {BACKSLASH}"' * (N // 14),
    "a header folded again and again": "authorization: x" + "\r\n " * (N // 3),
    "a header and its schemes": "authorization bearer " * (N // 21),
    "a password of @": "://a:" + "@" * N,
    "addresses with an @": "://a:b@" * (N // 7),
}


@pytest.mark.parametrize("text", HOSTILE.values(), ids=HOSTILE.keys())
def test_the_redaction_takes_time_in_proportion_to_the_line(text):
    started = time.perf_counter()
    redacted(text)
    assert time.perf_counter() - started < 0.5  # a few milliseconds; a pattern that starts again at each character: s


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (OSError(errno.ENOSPC, f"no room for {EMAIL}", f"/home/{EMAIL}/x"), f"OSError ({os.strerror(errno.ENOSPC)})"),
        (OSError(errno.EACCES, EMAIL, "/home/emily/.config/latet"), f"PermissionError ({os.strerror(errno.EACCES)})"),
        (FileNotFoundError(errno.ENOENT, EMAIL), f"FileNotFoundError ({os.strerror(errno.ENOENT)})"),
        (OSError(99_999, EMAIL), "OSError"),  # a number the C library has no text for
        (OSError(EMAIL), "OSError"),  # no number at all
        (TimeoutError(EMAIL), "TimeoutError"),
        (ssl.SSLError(1, EMAIL), "SSLError"),  # OpenSSL's code: 1 would read "Operation not permitted"
        (socket.gaierror(-2, EMAIL), "gaierror"),  # the resolver's codes
        (socket.herror(1, EMAIL), "herror"),
        (ValueError(EMAIL), "ValueError"),
        (KeyError(EMAIL), "KeyError"),
    ],
)
def test_an_error_is_described_by_its_type_and_the_c_librarys_text_for_its_number(error, said):
    assert described(error) == said and EMAIL not in described(error) and "/home" not in described(error)


# --- the lines -----------------------------------------------------------------------------------------------------------


def test_text_lines_are_redacted_whole(logs):
    configure_logging("text", "info", stream=logs)
    logging.getLogger("latet.server").info("a computer enrolled with %s at /u/%s/", TOKEN, ROUTE)
    line = logs.getvalue()
    assert "a computer enrolled" in line and "INFO" in line and TOKEN not in line and ROUTE not in line


def test_json_lines_are_one_object_each_and_redacted(logs):
    configure_logging("json", "info", stream=logs)
    log = logging.getLogger("latet.server")
    log.warning("token %s", TOKEN)
    log.debug("not shown at info")
    try:
        raise ValueError(f"bad key {KEY} for {EMAIL}")
    except ValueError:
        log.exception("failed")
    first, second = _lines(logs)
    assert (first["level"], first["logger"]) == ("WARNING", "latet.server") and TOKEN not in first["message"]
    assert "ValueError" in second["exception"] and KEY not in second["exception"] and EMAIL not in second["exception"]


def test_uvicorns_lines_go_through_it_and_its_access_lines_are_off(logs):
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)  # as an earlier start of uvicorn may leave it
    configure_logging("text", "info", stream=logs)
    logging.getLogger("uvicorn.error").info("Uvicorn running on https://0.0.0.0:5283")
    logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/1.1" %d', "10.0.0.5", "GET", f"/u/{ROUTE}/x", 200)
    out = logs.getvalue()
    assert "Uvicorn running" in out and ROUTE not in out and "10.0.0.5" not in out


def test_each_request_is_one_line_with_no_secret(logs):
    configure_logging("json", "info", stream=logs)

    async def ok(_):
        return PlainTextResponse("ok")

    app = RequestLog(Starlette(routes=[Route("/{path:path}", ok, methods=["GET", "POST"])]))
    with TestClient(app) as client:
        client.post(f"/u/{ROUTE}/anthropic/v1/messages?code=Xy7-code_42", headers={"x-latet-token": TOKEN})
    [line] = [json.loads(x) for x in logs.getvalue().splitlines() if '"method"' in x]
    assert (line["method"], line["path"], line["status"]) == ("POST", "/u/***/anthropic/v1/messages", 200)
    assert isinstance(line["ms"], int) and not any(secret in logs.getvalue() for secret in SECRETS)


@pytest.mark.parametrize(
    ("before", "written"),
    [("/x/", "/x/latet_pat_***/anthropic"), ("/x/abcdefghijklmnopqrstuvwxyz", "/x/***_pat_***/anthropic")],
)
async def test_no_part_of_a_latet_secret_is_left_in_a_paths_line(logs, before, written):
    """S8 (area 3, M1): a secret of Latet's in a path (a base URL pasted wrong, say) goes whole, though a run of 26
    letters of a route key's alphabet sits in its body (the run was taken out first, and the rest of it written), or
    right before it."""
    configure_logging("json", "info", stream=logs)
    secret = "latet_pat_Q0-abcdefghijklmnopqrstuvwxyzABC9_Zq0x-Ty"
    await _call(RequestLog(_answering(200)), _scope(f"{before}{secret}/anthropic", "POST"))
    [line] = _lines(logs)
    assert line["path"] == written and "Zq0x" not in logs.getvalue() and "Q0-" not in logs.getvalue()


async def test_no_part_of_a_computer_key_is_left_in_a_paths_line(logs):
    configure_logging("json", "info", stream=logs)
    secret = "latet_ck_Q0-abcdefghijklmnopqrstuvwxyzABC9_Zq0x-Ty1"
    await _call(RequestLog(_answering(200)), _scope(f"/x/{secret}/anthropic", "POST"))
    [line] = _lines(logs)
    assert line["path"] == "/x/latet_ck_***/anthropic" and "Zq0x" not in logs.getvalue()


def test_a_dashboard_projects_id_is_not_written(logs):
    configure_logging("json", "info", stream=logs)
    project = project_id("/home/emily/src/falcon-holdings")  # a hash of the folder, which a list of folders gives back
    with TestClient(_app()) as client:
        client.put(f"/_latet/api/v1/projects/{project}/personal")
    [line] = _lines(logs)
    assert line["path"] == "/_latet/api/v1/projects/***/personal" and project not in logs.getvalue()


SESSION = "ab" * 16  # a Bedrock session's id, which a signing route carries


def _written_otherwise():
    """A route key, and a session's id, as a client or a proxy may write them: what the router reads is the same."""
    first, middle = ROUTE[0], ROUTE[5]
    return [
        f"/%75/{ROUTE}/anthropic/v1/messages",
        f"/%55/{ROUTE}/anthropic/v1/messages",
        f"/u/%{ord(first):02x}{ROUTE[1:]}/anthropic/v1/messages",
        f"/u/{ROUTE[:5]}%{ord(middle):02X}{ROUTE[6:]}/anthropic/v1/messages",
        f"/u%2F{ROUTE}/anthropic/v1/messages",
        f"//u//{ROUTE}/anthropic/v1/messages",
        f"/u;x/{ROUTE}/anthropic/v1/messages",
        f"/bedrock/us-east-1/sessi%6Fn/{SESSION}/model/m/invoke",
    ]


def _none_of_it(out):
    """Whether ``out`` holds no part of the route key or of the session's id that is long enough to mean anything."""
    parts = [ROUTE[i : i + 6] for i in range(0, len(ROUTE) - 5)] + [SESSION[:8], SESSION[-8:]]
    return not any(part in out for part in parts)


async def test_a_route_key_written_otherwise_is_taken_out(logs):
    configure_logging("json", "info", stream=logs)
    for path in _written_otherwise():  # as the gate answers an address written otherwise: each the first of its kind
        await _call(RequestLog(_answering(403)), _scope(path, "POST"))
    assert len(_lines(logs)) == len(_written_otherwise()) and _none_of_it(logs.getvalue())


def _beside_another_sign():
    """A route key right after what is not a sign a path keeps (an escape's last hex digit, a byte that is no text) or
    against a letter or a digit: what a line's rule for a key standing alone did not take out (the key's holder alone
    writes them so; the gate refuses each)."""
    key = ROUTE.encode()
    return [
        b"/u/%20" + key + b"/anthropic/v1/messages",
        b"/u/%25" + key + b"/anthropic/v1/messages",
        b"/x/%C3%A9" + key + b"/anthropic/v1/messages",
        b"/x/\xe9" + key + b"/anthropic/v1/messages",  # a raw byte that is no ASCII
        b"/x/8" + key + b"/anthropic/v1/messages",
        b"/x/2" + key + b"7/anthropic/v1/messages",  # signs of its own alphabet on both sides
        b"/x/a" + key + b"/anthropic/v1/messages",
        b"/U/%20" + ROUTE.upper().encode() + b"/anthropic/v1/messages",
    ]


@pytest.mark.parametrize("raw", _beside_another_sign(), ids=range(len(_beside_another_sign())))
async def test_a_route_key_beside_another_sign_is_taken_out(logs, raw):
    configure_logging("json", "info", stream=logs)
    scope = {**_scope("/x", "POST"), "raw_path": raw, "path": raw.decode("latin-1")}
    await _call(RequestLog(_answering(403)), scope)
    [line] = _lines(logs)
    assert line["status"] == 403 and _none_of_it(logs.getvalue().lower())  # in either case


def test_through_uvicorn_a_route_key_written_otherwise_is_taken_out(logs):
    configure_logging("json", "info", stream=logs)
    with _uvicorn(_app()) as port:
        for target in (f"/%75/{ROUTE}/anthropic/v1/messages", f"//u//{ROUTE}/anthropic/v1/messages"):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:  # the request line byte for byte
                sock.sendall(
                    f"POST {target} HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
                )
                while sock.recv(4096):
                    pass
    assert len([line for line in _lines(logs) if line.get("method") == "POST"]) == 2 and _none_of_it(logs.getvalue())


class _Null(io.TextIOBase):
    """An output that takes every line and keeps none: what the cost of writing a line is measured on."""

    def write(self, text):
        return len(text)


async def _requests(app, path, count):
    """How long ``count`` requests to ``path`` take through ``app``: the best of three runs."""
    times = []
    for _ in range(3):
        started = time.perf_counter()
        for _ in range(count):
            await _call(app, _scope(path))
        times.append(time.perf_counter() - started)
    return min(times)


@pytest.mark.parametrize("format", ["text", "json"])
@pytest.mark.parametrize(
    "piece",
    [
        "/a.b",  # a shape the redaction tries again and again
        "/a%2Eb",  # an escape in each segment, which reading the path as the router does decodes one by one
    ],
)
async def test_past_a_kilobyte_a_paths_length_costs_its_line_nothing(format, piece, monkeypatch):
    """What a request's line reads of its path is bounded whatever the path's length: the bytes handed to the path's
    decoding and to its escaping again, each counted (PATH_LIMIT at most a request), and the time as a coarse bound."""
    handed: list[int] = []
    for name in ("unquote_to_bytes", "quote"):
        real = getattr(logs_module, name)

        def counted(text, *args, _real=real, **kwargs):
            handed.append(len(text))
            return _real(text, *args, **kwargs)

        monkeypatch.setattr(logs_module, name, counted)
    with saved():
        configure_logging(format, "info", stream=_Null())
        app = RequestLog(_answering(400))  # a status that is never paced: a line each
        kilobyte = await _requests(app, piece * (PATH_LIMIT // len(piece)), 200)
        handed.clear()
        sixty = await _requests(app, piece * (60 * PATH_LIMIT // len(piece)), 200)
    assert len(handed) == 2 * 3 * 200 and max(handed) <= PATH_LIMIT  # never the 60 KiB sent
    assert sixty < 3 * kilobyte


async def test_no_text_handed_to_the_redaction_is_over_its_bound(monkeypatch):
    sizes = []
    real = logs_module.redacted

    def counted(text):
        sizes.append(len(text))
        return real(text)

    monkeypatch.setattr(logs_module, "redacted", counted)
    for format in ("text", "json"):
        sizes.clear()
        with saved():
            configure_logging(format, "info", stream=io.StringIO())
            await _call(RequestLog(_answering(404)), _scope("/a" * (30 * PATH_LIMIT), "POST"))  # 60 KiB of path
            assert len(sizes) == 3  # its method, its path and its line, each once
            assert max(sizes) <= PATH_LIMIT + 128  # the path, once cut, and the line made of it
            sizes.clear()
            refused = RequestLog(_answering(429))
            await _call(refused, _scope("/a" * (30 * PATH_LIMIT), "POST"))  # the first: its line
            sizes.clear()
            for _ in range(100):  # the others, counted: their path is not even read
                await _call(refused, _scope("/a" * (30 * PATH_LIMIT), "POST"))
            assert sizes == []
            logging.getLogger("latet.server").info("%s", "b " * (512 * 1024))  # a record of 1 MiB
            try:
                raise ValueError("c " * (512 * 1024))
            except ValueError:
                logging.getLogger("latet.server").exception("d " * (512 * 1024))
            chained = None
            for _ in range(300):  # a chain of causes: a traceback of tens of thousands of characters
                try:
                    raise ValueError("e") from chained
                except ValueError as error:
                    chained = error
            try:
                raise ValueError("f") from chained
            except ValueError:
                logging.getLogger("latet.server").exception("a chain")
            assert max(sizes) <= 2 * LINE_LIMIT


async def test_a_long_method_is_cut_and_redacted_too(logs):
    configure_logging("json", "info", stream=logs)
    await _call(RequestLog(_answering(405)), _scope("/x", "G" * 60_000))
    await _call(RequestLog(_answering(405)), _scope("/x", TOKEN))  # a method is any token: one of Latet's, say
    first, second = _lines(logs)
    assert first["method"] == "G" * 32 and first["message"] == "G" * 32 + " /x 405 0 ms"
    assert TOKEN[10:20] not in logs.getvalue() and second["message"] == "latet_pat_*** /x 405 0 ms"


@pytest.mark.parametrize("form", ["/u/{}/anthropic/v1/messages", "//u//{}/x", "/u;x/{}/x", "/u%2F{}/x"])
async def test_no_part_of_a_route_key_is_left_across_the_cut(logs, form):
    configure_logging("json", "info", stream=logs)
    app = RequestLog(_answering(400))  # a status that is never paced: a line each
    for before in range(PATH_LIMIT - 40, PATH_LIMIT + 8):  # wherever the cut falls in the key, or before it, or after
        await _call(app, _scope("/" + "-" * before + form.format(ROUTE)))
    lines = _lines(logs)
    assert len(lines) == 48 and all(len(line["path"]) <= PATH_LIMIT + 8 for line in lines)
    assert ROUTE[:4] not in logs.getvalue() and ROUTE[-4:] not in logs.getvalue()


# --- WebSockets ----------------------------------------------------------------------------------------------------------


def test_a_websocket_is_one_line_of_its_own(logs):
    configure_logging("json", "info", stream=logs)
    with TestClient(_app()) as client:
        with client.websocket_connect(f"/u/{ROUTE}/openai/v1/realtime?api-key=azure-key-in-the-query") as websocket:
            websocket.send_text("hello")
            assert websocket.receive_text() == "hello"
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"/u/{ROUTE}/refused?api-key=k"):
            pass
    said = [(line["method"], line["path"], line["status"]) for line in _lines(logs)]
    assert said == [("WS", "/u/***/openai/v1/realtime", 101), ("WS", "/u/***/refused", 403)]
    assert "azure-key-in-the-query" not in logs.getvalue() and ROUTE not in logs.getvalue()


async def test_a_websocket_that_ends_before_it_is_answered_says_how_it_ended(logs):
    configure_logging("json", "info", stream=logs)

    async def gone(scope, receive, send):
        await receive()  # websocket.connect
        await receive()  # the client went away before an answer

    messages = iter([{"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1001}])

    async def receive():
        return next(messages)

    async def send(message):
        pass

    await RequestLog(gone)({"type": "websocket", "path": "/x", "raw_path": b"/x", "headers": []}, receive, send)
    [line] = _lines(logs)
    assert (line["method"], line["status"]) == ("WS", 1001)


def test_through_uvicorn_a_websocket_leaves_neither_the_address_nor_the_query(logs):
    configure_logging("json", "info", stream=logs)
    with _uvicorn(_app()) as port:
        query = "?model=gpt-realtime&api-key=azure-key-in-the-query"
        with connect(f"ws://127.0.0.1:{port}/u/{ROUTE}/openai/v1/realtime{query}", proxy=None) as websocket:
            websocket.send("hello")
            assert websocket.recv() == "hello"
    out = logs.getvalue()
    [line] = [line for line in _lines(logs) if line.get("method") == "WS"]
    assert (line["path"], line["status"]) == ("/u/***/openai/v1/realtime", 101)
    assert "127.0.0.1" not in out and "azure-key-in-the-query" not in out and ROUTE not in out
    assert "WebSocket" not in out  # uvicorn's own line for it is dropped


def test_through_uvicorn_an_application_error_is_its_types_and_frames(logs):
    configure_logging("json", "info", stream=logs)
    with _uvicorn(_app()) as port, httpx.Client(trust_env=False) as client:
        assert client.get(f"http://127.0.0.1:{port}/fails").status_code == 500
    [error] = [line for line in _lines(logs) if line["level"] == "ERROR"]
    assert (error["logger"], error["message"]) == ("uvicorn.error", "Exception in ASGI application")
    assert error["exception"].endswith("ValueError") and "_fails" in error["exception"]
    assert EMAIL not in logs.getvalue() and "127.0.0.1" not in logs.getvalue()


# --- the levels ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("uvicorn_level", [None, "debug"])
def test_debug_opens_latets_lines_and_no_librarys(logs, uvicorn_level):
    configure_logging("json", "debug", stream=logs)
    provider = "sk-ant-api03-PROVIDERKEYOFEMILY"
    headers = {"authorization": f"Bearer {provider}", "cookie": "theme=dark-and-secret"}
    with (
        _uvicorn(_app(), log_level=uvicorn_level) as port,  # "debug": uvicorn sets its own logger to DEBUG
        connect(f"ws://127.0.0.1:{port}/u/{ROUTE}/x", additional_headers=headers, proxy=None) as websocket,
    ):
        websocket.send(f"Please write to {EMAIL} about Project Falcon")
        websocket.recv()
    logging.getLogger("latet.server").debug("a line of Latet's own at debug")
    out = logs.getvalue()
    assert "a line of Latet's own at debug" in out
    for held in (provider, "dark-and-secret", EMAIL, "Project Falcon", "127.0.0.1"):
        assert held not in out


def test_the_libraries_stay_at_warning_whatever_is_asked(logs):
    configure_logging("json", "debug", stream=logs)
    for name in ("psycopg", "psycopg.pool", "psycopg_pool.sched", "redis", "httpx", "httpcore", "websockets",
                 "websockets.client", "asyncio", "uvicorn.asgi", "PIL"):  # fmt: skip
        assert not logging.getLogger(name).isEnabledFor(logging.INFO), name
    assert logging.getLogger("latet.server.anything").isEnabledFor(logging.DEBUG)
    assert logging.getLogger("uvicorn.error").isEnabledFor(logging.INFO)
    assert not logging.getLogger("uvicorn.error").isEnabledFor(logging.DEBUG)
    logging.getLogger("psycopg").setLevel(logging.DEBUG)  # a library's logger opened anyway: its lines stay out
    logging.getLogger("psycopg").debug("connection attempt to host=%s user=%s", "db.acme.lan", "latet")
    assert logs.getvalue() == ""


@pytest.mark.parametrize("level", ["trace", "TRACE", "notset", "10", "verbose", ""])
def test_only_four_levels_are_taken(logs, level):
    with pytest.raises(ValueError, match="debug, info, warning or error"):
        configure_logging("json", level, stream=logs)


@pytest.mark.parametrize("format", ["yaml", "JSON", "", "text "])
def test_only_two_formats_are_taken(logs, format):
    with pytest.raises(ValueError, match="text or json"):
        configure_logging(format, "info", stream=logs)


def test_a_json_line_keeps_a_stack_asked_for_by_its_frames(logs):
    configure_logging("json", "info", stream=logs)
    logging.getLogger("latet.server").info("where it was asked", stack_info=True)
    [line] = _lines(logs)
    assert line["stack"].startswith("Stack (most recent call last):")
    assert "in test_a_json_line_keeps_a_stack_asked_for_by_its_frames" in line["stack"]


def test_a_text_line_says_its_time_in_utc_with_its_zone_each_field_where_it_was(logs):
    """The date and the time stay two fields, the level the third and the logger the fourth, as a text line had them
    before it said its zone: what reads the lines by their spaces reads them as it did."""
    configure_logging("text", "info", stream=logs)
    logging.getLogger("latet.server").info("when")
    date, time, level, name, message = logs.getvalue().rstrip("\n").split(" ", 4)
    assert (level, name, message) == ("INFO", "latet.server:", "when")
    assert time.endswith("+00:00") and datetime.fromisoformat(f"{date} {time}").utcoffset() == timedelta(0)


def test_a_level_in_capitals_is_taken(logs):
    configure_logging("json", "WARNING", stream=logs)
    logging.getLogger("latet.server").info("not shown")
    logging.getLogger("latet.server").warning("shown")
    assert [line["message"] for line in _lines(logs)] == ["shown"]


async def test_an_upstream_request_writes_nothing(logs):
    configure_logging("json", "info", stream=logs)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post("https://gw.acme.lan/v1beta/models/gemini-pro:generateContent?key=AIzaSyFAKEKEYOFACME")
    assert logs.getvalue() == ""


# --- what a library's record shows ----------------------------------------------------------------------------------------


class _Connection:
    """What psycopg's repr of a connection names."""

    def __repr__(self):
        return "<psycopg.Connection [IDLE] (host=db.acme.lan user=latet database=latet) at 0x7f>"


@pytest.mark.parametrize("name", ["psycopg", "psycopg.pool", "psycopg.transaction", "psycopg_pool.sched"])
def test_psycopgs_records_keep_their_fixed_text_and_lose_what_they_quote(logs, name):
    configure_logging("json", "info", stream=logs)
    violation = psycopg.errors.UniqueViolation(f"Key (external_id)=({EMAIL}) already exists.")
    logging.getLogger(name).warning("error connecting in %r: %s", "latet-latet", violation)
    try:
        raise violation
    except psycopg.Error:
        logging.getLogger(name).exception("error ignored in rollback of %s: %s", _Connection(), violation)
    first, second = _lines(logs)
    assert first["message"] == "error connecting in <str>: <UniqueViolation>"
    assert second["message"] == "error ignored in rollback of <_Connection>: <UniqueViolation>"
    assert second["exception"].endswith("psycopg.errors.UniqueViolation")
    for held in ("db.acme.lan", "user=latet", EMAIL, "already exists"):
        assert held not in logs.getvalue()


def test_a_librarys_record_keeps_numbers_and_shows_the_rest_by_its_type(logs):
    configure_logging("text", "info", stream=logs)
    error = logging.getLogger("uvicorn.error")
    error.info("Uvicorn running on %s://%s:%d (Press CTRL+C to quit)", "http", "10.0.0.5", 5283)
    error.error(OSError(f"[Errno 98] could not bind for {EMAIL}"))  # a message that is the error itself
    # A start that failed, as Starlette hands it to uvicorn: its first line alone.
    error.error(f"Traceback (most recent call last):\n  File 'app.py', line 1, in start\nValueError: {EMAIL}")
    logging.getLogger("redis.connection").warning("%(what)s to %(host)s", {"what": "lost", "host": "cache.acme.lan"})
    logging.getLogger("websockets.client").warning("%d frames, %s", "not a number", EMAIL)  # a format that does not fit
    out = logs.getvalue()
    assert "Uvicorn running on <str>://<str>:5283 (Press CTRL+C to quit)" in out and "<OSError>" in out
    assert "<str> to <str>" in out and "%d frames, %s (<str>, <str>)" in out
    for held in ("10.0.0.5", EMAIL, "cache.acme.lan", "lost"):
        assert held not in out


@pytest.mark.parametrize("name", ["redis.connection", "uvicorn.error"])
def test_a_librarys_single_mapping_is_one_argument_shown_by_its_type(logs, name):
    configure_logging("json", "info", stream=logs)
    library = logging.getLogger(name)
    library.warning("headers: %s", {EMAIL: "x", "authorization": "Bearer sk-ant-PROVIDERKEY"})  # logging makes it args
    library.warning("%s", {f"/u/{ROUTE}": 1})
    library.warning("lost %(host)s", {"host": "cache.acme.lan"})  # named fields: each by its type
    assert [line["message"] for line in _lines(logs)] == ["headers: <dict>", "<dict>", "lost <str>"]
    assert EMAIL not in logs.getvalue() and ROUTE not in logs.getvalue() and "PROVIDERKEY" not in logs.getvalue()


async def test_asyncio_in_debug_mode_names_a_callback_without_its_arguments(logs):
    configure_logging("json", "info", stream=logs)

    def fails(address):
        raise KeyError("x")

    loop = asyncio.get_running_loop()
    loop.set_debug(True)  # what an operator turns on to find what holds the loop: a callback's repr with its arguments
    try:
        loop.call_soon(fails, EMAIL)
        await asyncio.sleep(0.05)
    finally:
        loop.set_debug(False)
    [line] = [line for line in _lines(logs) if line["logger"] == "asyncio"]
    assert line["message"].startswith("Exception in callback ") and line["message"].endswith(".fails")
    assert EMAIL not in logs.getvalue()


def test_an_exception_is_shown_by_its_types_and_frames_never_its_words(logs):
    # Each error's words are made when it is raised, as a driver's or a library's are: a traceback's frames show the
    # code of each line, never what it held.
    words = {name: f"{name} {EMAIL}" for name in ("context", "added", "member", "plain", "note", "refused")}
    configure_logging("json", "info", stream=logs)
    log = logging.getLogger("latet.server.admin")
    try:
        try:
            raise KeyError(words["context"])
        except KeyError:
            raise RuntimeError(words["added"])  # noqa: B904 - what was being handled is shown, by its type
    except RuntimeError:
        log.exception("the context")
    try:
        raise ExceptionGroup("two failed", [ValueError(words["member"]), EgressRefused("gw.acme.lan" + " is inside")])
    except ExceptionGroup:
        log.exception("a group")
    noted = ValueError(words["plain"])
    noted.add_note(words["note"])
    try:
        raise noted
    except ValueError:
        log.exception("a note")
    try:
        raise EgressRefused(words["refused"])  # its words name the destination's host
    except EgressRefused:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application\n")
    context, group, note, egress = _lines(logs)
    assert "KeyError" in context["exception"] and context["exception"].endswith("RuntimeError")
    assert "During handling of the above exception" in context["exception"]
    assert "ValueError" in group["exception"] and "latet.server.egress.EgressRefused" in group["exception"]
    assert note["exception"].endswith("ValueError") and egress["message"] == "Exception in ASGI application"
    assert egress["exception"].endswith("latet.server.egress.EgressRefused")
    for held in (EMAIL, "gw.acme.lan is inside"):
        assert held not in logs.getvalue()


def test_a_traceback_another_formatter_left_in_the_record_is_not_reused():
    try:
        raise ValueError(f"bad address {EMAIL}")
    except ValueError:
        record = logging.LogRecord("latet.server", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    logging.Formatter().format(record)  # another handler's formatter, first: it keeps the whole traceback in the record
    assert EMAIL in record.exc_text
    for formatter in (TextFormatter(), JsonFormatter()):
        out = formatter.format(record)
        assert "ValueError" in out and EMAIL not in out


def test_a_few_of_latets_own_errors_keep_their_fixed_words(logs):
    from latet.engine.vault import MaskingError
    from latet.server.db import DatabaseError

    configure_logging("json", "info", stream=logs)
    for error in (DatabaseError("the connection to PostgreSQL failed (OperationalError)"), MaskingError("no fake")):
        try:
            raise error
        except RuntimeError:
            logging.getLogger("latet.server").exception("failed")
    first, second = _lines(logs)
    assert first["exception"].endswith("DatabaseError: the connection to PostgreSQL failed (OperationalError)")
    assert second["exception"].endswith("latet.engine.vault.MaskingError: no fake")


def test_a_full_vaults_and_a_full_spaces_words_are_kept(logs):
    """F-S4: both stop a request that brings a new value, with words that are fixed and name no value (a count and a
    setting's name; ``spaces.PAST_SHARE``): the line says why, as a traceback's last line."""
    from latet.runtime import SpaceFull
    from latet.server.spaces import PAST_SHARE
    from latet.server.vault import VaultFull

    configure_logging("json", "info", stream=logs)
    full = "this identity's vault holds 3 values, and this server allows 3 at most (vault_limit)"
    for error in (VaultFull(full), SpaceFull(PAST_SHARE)):
        try:
            raise error
        except RuntimeError:
            logging.getLogger("latet.server").exception("stopped")
    vault, space = _lines(logs)
    assert vault["exception"].endswith(f"latet.server.vault.VaultFull: {full}")
    assert space["exception"].endswith(f"latet.runtime.SpaceFull: {PAST_SHARE}")


def test_an_error_given_to_a_line_of_latets_is_written_by_its_type(logs):
    from latet.engine.reviewer.client import ReviewError
    from latet.server.db import DatabaseError

    configure_logging("json", "info", stream=logs)
    log = logging.getLogger("latet.server")
    log.error("failed: %s", ValueError(EMAIL))  # what no reading of the code saw coming: the net
    log.error(KeyError(EMAIL))  # the message itself
    log.error("failed: %(why)s", {"why": OSError(EMAIL)})
    log.error("failed: %s", DatabaseError("the connection to PostgreSQL failed (OperationalError)"))  # fixed words
    log.info("the local reviewer did not answer: %s", ReviewError("the server answered HTTP 500"))  # as it logs it
    log.error("failed: %s (%d times)", "a store", 3)  # anything else as it is
    assert [line["message"] for line in _lines(logs)] == [
        "failed: ValueError",
        "KeyError",
        "failed: OSError",
        "failed: latet.server.db.DatabaseError: the connection to PostgreSQL failed (OperationalError)",
        "the local reviewer did not answer: latet.engine.reviewer.client.ReviewError: the server answered HTTP 500",
        "failed: a store (3 times)",
    ]
    assert EMAIL not in logs.getvalue()


async def test_a_task_nobody_awaited_names_its_error_and_not_what_it_held(logs):
    configure_logging("json", "info", stream=logs)

    async def fails():
        raise KeyError(EMAIL)

    task = asyncio.ensure_future(fails())
    await asyncio.wait([task])  # ended, and what it raised never asked for
    del task
    gc.collect()  # asyncio says, as it lets the task go, that nobody asked what it raised
    [line] = [line for line in _lines(logs) if line["logger"] == "asyncio"]
    assert line["message"] == "Task exception was never retrieved" and line["exception"].endswith("KeyError")
    assert EMAIL not in logs.getvalue()


# --- nobody writes a line of his own ---------------------------------------------------------------------------------------


FORGED = "2026-10-14 10:00:00,000 INFO latet.server.requests: POST /_latet/server/v1/admin/identities 201 4 ms"
# What ends a line for one reader or another (Python's splitlines among them), or acts on a terminal, or reorders what
# follows it, or cannot be written in UTF-8: a line feed, a carriage return, the other separators, an escape, a bell,
# DEL, the 8-bit controls (NEL, CSI), the line and paragraph separators, the bidirectional controls (a right-to-left
# override, an isolate, a mark, the Arabic letter mark), a lone surrogate.
UNSAFE = [
    chr(code)
    for code in (
        *(0x0A, 0x0D, 0x0B, 0x0C, 0x1C, 0x1B, 0x07, 0x7F, 0x85, 0x9B, 0x2028, 0x2029),
        *(0x202E, 0x2066, 0x200F, 0x061C, 0xD800),
    )
]


def _one_line(stream):
    """Whether ``stream`` holds one line and its end, nothing of UNSAFE in it."""
    out = stream.getvalue()
    return len(out.splitlines()) == 1 and out.endswith("\n") and not any(char in out[:-1] for char in UNSAFE)


@pytest.mark.parametrize("format", ["text", "json"])
@pytest.mark.parametrize("sent", ["%0A", "%0D", "%1B", "%C2%85", "%C2%9B", "%E2%80%A8", "%E2%80%AE"])
def test_no_path_writes_a_line_of_its_own(logs, format, sent):
    configure_logging(format, "info", stream=logs)
    with TestClient(_app()) as client:
        client.get(f"/x{sent}{FORGED}")
    assert _one_line(logs)


@pytest.mark.parametrize("format", ["text", "json"])
async def test_a_path_with_no_raw_form_is_written_escaped(logs, format):
    configure_logging(format, "info", stream=logs)
    path = "/x" + "".join(UNSAFE[:-1]) + FORGED  # as uvicorn decodes it (a lone surrogate it never makes)
    await _call(RequestLog(_answering(200)), {"type": "http", "method": "GET", "path": path, "headers": []})
    assert _one_line(logs)


@pytest.mark.parametrize("format", ["text", "json"])
def test_a_message_holds_no_line_break_and_no_terminal_sequence(logs, format):
    configure_logging(format, "info", stream=logs)
    logging.getLogger("latet.server").info("a label: %s", "x".join(UNSAFE) + FORGED)
    assert _one_line(logs)


def test_a_right_to_left_override_is_written_by_its_code(logs):
    configure_logging("text", "info", stream=logs)
    path = "/v1/chat/" + chr(0x202E) + "txt.exe"  # a blocked request's decoded path, as the forwarder logs it
    logging.getLogger("latet.gateway.forward").warning("blocked %s %s: %s", "POST", path, "masking failed")
    assert _one_line(logs) and "/v1/chat/" + BACKSLASH + "u202etxt.exe" in logs.getvalue()


def test_a_traceback_in_text_is_marked_line_by_line(logs):
    configure_logging("text", "info", stream=logs)
    try:
        raise ValueError(f"bad address{chr(10)}{FORGED} {EMAIL}")
    except ValueError:
        logging.getLogger("latet.server").exception("failed")
    first, *rest = logs.getvalue().splitlines()
    assert first.endswith(" ERROR latet.server: failed") and len(rest) > 2
    assert all(line.startswith("  | ") for line in rest) and rest[-1] == "  | ValueError"
    assert EMAIL not in logs.getvalue()


@pytest.mark.parametrize("format", ["text", "json"])
def test_a_record_that_cannot_be_written_leaves_one_fixed_line(format, capsys):
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    with saved():
        configure_logging(format, "info", stream=stream)
        log = logging.getLogger("latet.server")
        log.info("enrolled %s at /u/%s/ (label %s)", TOKEN, ROUTE, "laptop" + chr(0xD800))
        log.info("%d computers for %s", TOKEN)  # a format that does not fit what it is given
    stream.flush()
    written, err = stream.buffer.getvalue().decode(), capsys.readouterr().err
    assert len(written.splitlines()) == 2 and "a log record could not be written: TypeError" in written
    assert "enrolled" in written and TOKEN not in written and ROUTE not in written
    assert err == ""


def test_what_json_cannot_hold_is_written_by_its_type(logs):
    configure_logging("json", "info", stream=logs)

    class Answer:
        def __repr__(self):
            return f"Answer(to={EMAIL})"

    logging.getLogger("latet.server.requests").info("odd", extra={"status": Answer(), "ms": 3})
    [line] = _lines(logs)
    assert (line["status"], line["ms"]) == ("<Answer>", 3) and EMAIL not in logs.getvalue()


# --- what an outsider makes at will ----------------------------------------------------------------------------------------


async def test_requests_answered_429_are_said_once_a_minute_with_how_many(logs):
    configure_logging("json", "info", stream=logs)
    now = [1000.0]
    app = RequestLog(_answering(429), clock=lambda: now[0])
    for _ in range(999):
        await _call(app, _scope(f"/u/{ROUTE}/anthropic/v1/messages", "POST"))
    now[0] += 61
    await _call(app, _scope(f"/u/{ROUTE}/anthropic/v1/messages", "POST"))
    await _call(RequestLog(_answering(200)), _scope("/_latet/health"))  # every other answer has its line
    first, last, other = _lines(logs)
    assert first["message"] == "POST /u/***/anthropic/v1/messages 429 0 ms" and first["status"] == 429
    assert last["message"] == "POST /u/***/anthropic/v1/messages 429 0 ms (999 answered 429 since the line before)"
    assert other["status"] == 200


def _refused_by_the_gate(path):
    """A request Task 14's gate answers itself (its limit by address), as it marks it in the scope."""
    return {**_scope(path, "POST"), REFUSED: "limit"}


async def test_a_flood_of_one_kind_of_429_leaves_the_first_line_of_another(logs):
    configure_logging("json", "info", stream=logs)
    now = [1000.0]
    app = RequestLog(_answering(429), clock=lambda: now[0])
    for _ in range(500):  # a stranger's addresses, which the gate refuses past its limit by address
        await _call(app, _refused_by_the_gate("/u/aaaaaaaaaaaaaaaaaaaaaaaaaa/anthropic/v1/messages"))
    now[0] += 30
    await _call(app, _scope(f"/u/{ROUTE}/anthropic/v1/messages", "POST"))  # a provider's 429, passed on to its agent
    await _call(app, _scope("/_latet/server/v1/admin/identities", "POST"))  # an administrator's
    await _call(app, _scope("/_latet/api/v1/config", "PUT"))  # the dashboard's
    await _call(app, _refused_by_the_gate("/_latet/server/v1/machines/enrol"))  # the server's own, by the gate
    assert [line["message"] for line in _lines(logs)] == [
        "POST /u/***/anthropic/v1/messages 429 0 ms",
        "POST /u/***/anthropic/v1/messages 429 0 ms",
        "POST /_latet/server/v1/admin/identities 429 0 ms",
        "PUT /_latet/api/v1/config 429 0 ms",
        "POST /_latet/server/v1/machines/enrol 429 0 ms",
    ]


async def test_what_was_counted_and_not_said_is_said_by_the_flush(logs):
    flush()  # what other tests' RequestLogs counted, if any is still alive, said before this test's lines
    configure_logging("json", "info", stream=logs)
    app = RequestLog(_answering(429), clock=lambda: 1000.0)
    for _ in range(500):
        await _call(app, _refused_by_the_gate("/u/aaaaaaaaaaaaaaaaaaaaaaaaaa/anthropic/v1/messages"))
    await _call(app, _scope(f"/u/{ROUTE}/anthropic/v1/messages", "POST"))
    await _call(app, _scope(f"/u/{ROUTE}/anthropic/v1/messages", "POST"))
    error = logging.getLogger("uvicorn.error")
    for _ in range(10):
        error.warning("Invalid HTTP request received.")
    flush()  # what Task 14's beat does once a minute: no further request needed
    flush()  # and nothing is said twice
    said = [line["message"] for line in _lines(logs)]
    assert said[:3] == [
        "POST /u/***/anthropic/v1/messages 429 0 ms",
        "POST /u/***/anthropic/v1/messages 429 0 ms",
        "Invalid HTTP request received.",
    ]
    assert sorted(said[3:]) == [
        "1 more request to an identity's address answered 429 since the line before",
        "499 more requests to an identity's address refused 429 by the gate since the line before",
        "Invalid HTTP request received. (9 since the line before)",
    ]


def _status_in_path():
    """An app that answers each request with the status its path starts with (``/404/...``)."""

    async def app(scope, receive, send):
        status = int(scope["path"].split("/")[1])
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return app


async def test_an_outsiders_403s_and_404s_are_said_once_a_minute_each_status_and_kind_on_its_own(logs):
    """A 404 for an unknown route or a 403 for a foreign Host, one a request that anyone sends at will: said as the 429s
    are, so that a flood of one never hides the first line of another, nor of what came from behind the gate."""
    flush()  # what other tests' RequestLogs counted, if any is still alive, said before this test's lines
    configure_logging("json", "info", stream=logs)
    now = [1000.0]
    app = RequestLog(_status_in_path(), clock=lambda: now[0])
    for _ in range(300):
        await _call(app, {**_scope("/404/unknown"), REFUSED: True})  # the gate's own 404
    await _call(app, {**_scope("/403/host"), REFUSED: True})  # its 403: the first line of its kind
    await _call(app, _scope("/404/models/x"))  # a 404 that came from behind the gate: a kind of its own
    for _ in range(5):
        await _call(app, {**_scope("/403/host"), REFUSED: True})
    now[0] += 61
    await _call(app, {**_scope("/404/unknown"), REFUSED: True})
    flush()  # the beat: the 403s counted and not said
    assert [line["message"] for line in _lines(logs)] == [
        "GET /404/unknown 404 0 ms",
        "GET /403/host 403 0 ms",
        "GET /404/models/x 404 0 ms",
        "GET /404/unknown 404 0 ms (300 refused 404 by the gate since the line before)",
        "5 more requests to the server's own refused 403 by the gate since the line before",
    ]


async def test_requests_answered_401_405_and_413_are_said_once_a_minute_too(logs):
    """S8 (area 3, M2): a request with no credential (401), a method its route does not take (405) or a length past the
    bound (413), which anyone sends at will, is paced as a 403, a 404 or a 429 is: a burst of them no longer fills the
    output's queue with a line each."""
    flush()  # what other tests' RequestLogs counted, if any is still alive, said before this test's lines
    configure_logging("json", "info", stream=logs)
    app = RequestLog(_status_in_path(), clock=lambda: 1000.0)
    for status in (401, 405, 413):
        for _ in range(50):
            await _call(app, {**_scope(f"/{status}/x"), REFUSED: True})
    assert [line["message"] for line in _lines(logs)] == [f"GET /{s}/x {s} 0 ms" for s in (401, 405, 413)]
    flush()
    assert [line["message"] for line in _lines(logs)][3:] == [
        f"49 more requests to the server's own refused {s} by the gate since the line before" for s in (401, 405, 413)
    ]


async def test_a_refusal_of_the_servers_own_is_said_once_a_minute_whatever_its_status(logs):
    """R1: what the server marks as its own refusal (a sign-in link that opens nothing is a 303, a store that did not
    answer a 503) is paced as a 403 or a 429 is, whatever its status; the same status from behind the gate is said each
    time."""
    flush()  # what other tests' RequestLogs counted, if any is still alive, said before this test's lines
    configure_logging("json", "info", stream=logs)
    app = RequestLog(_status_in_path(), clock=lambda: 1000.0)
    for _ in range(50):
        await _call(app, {**_scope("/303/x"), REFUSED: True})
    for _ in range(2):
        await _call(app, _scope("/303/y"))  # a redirect of what is behind the gate: said each time
    assert [line["message"] for line in _lines(logs)] == ["GET /303/x 303 0 ms", *["GET /303/y 303 0 ms"] * 2]
    flush()
    assert [line["message"] for line in _lines(logs)][3:] == [
        "49 more requests to the server's own refused 303 by the gate since the line before"
    ]


def test_uvicorns_warnings_that_a_stranger_makes_are_said_once_a_minute(logs, monkeypatch):
    configure_logging("json", "info", stream=logs)
    [gate] = logging.getLogger().handlers[0].filters
    now = [1000.0]
    for paced in gate._repeated.values():
        monkeypatch.setattr(paced, "_clock", lambda: now[0])
    error = logging.getLogger("uvicorn.error")
    for _ in range(999):
        error.warning("Invalid HTTP request received.")
        error.warning("Unsupported upgrade request.")
    now[0] += 61
    error.warning("Invalid HTTP request received.")
    said = [line["message"] for line in _lines(logs)]
    assert said == [
        "Invalid HTTP request received.",
        "Unsupported upgrade request.",
        "Invalid HTTP request received. (999 since the line before)",
    ]


def test_malformed_requests_to_uvicorn_make_one_warning(logs):
    configure_logging("json", "info", stream=logs)
    with _uvicorn(_app()) as port:
        for _ in range(1000):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(b"NOT HTTP AT ALL\r\n\r\n")
                sock.recv(4096)
    warned = [line["message"] for line in _lines(logs) if line["level"] == "WARNING"]
    assert warned == ["Invalid HTTP request received."]


@pytest.mark.parametrize("format", ["text", "json"])
async def test_a_long_line_is_cut_after_the_redaction(logs, format):
    configure_logging(format, "info", stream=logs)
    await _call(RequestLog(_answering(404)), _scope("/" + "a" * 8150 + f"/u/{ROUTE}/" + "b" * N))
    password = "Pq7Zx9Wv3Ty5"
    for before in range(7900, 8200, 3):  # wherever the cut falls, the address is taken out first
        logging.getLogger("latet.server").info("%s", "c" * before + f" postgresql://latet:{password}@db.acme.lan/latet")
    lines = logs.getvalue().splitlines()
    assert len(lines) == 101 and all(len(line.encode()) <= LINE_LIMIT for line in lines)
    assert "[cut]" in lines[0] and ROUTE not in logs.getvalue() and password[:4] not in logs.getvalue()


@pytest.mark.parametrize("format", ["text", "json"])
@pytest.mark.parametrize("piece", ["Pq7Zx9Wv3Ty5", "Pq7Z@x9Wv3Ty5"], ids=["plain", "holding-an-at"])
def test_an_address_cut_before_its_end_leaves_no_part_of_its_password(logs, format, piece):
    configure_logging(format, "info", stream=logs)
    password = piece * 2000  # past where a long text is cut before it is redacted, its last @ with it
    for before in (0, 100, 8000):
        logging.getLogger("latet.server").info("%s", "c" * before + f" postgresql://latet:{password}@db.acme.lan/x")
    assert len(logs.getvalue().splitlines()) == 3 and "Pq7Z" not in logs.getvalue() and "Wv3T" not in logs.getvalue()


@pytest.mark.parametrize("format", ["text", "json"])
@pytest.mark.parametrize("text", HOSTILE.values(), ids=HOSTILE.keys())
def test_a_line_of_any_shape_and_length_is_written_in_time(logs, format, text):
    configure_logging(format, "info", stream=logs)
    started = time.perf_counter()
    logging.getLogger("latet.server").info("%s", text * 16)  # a megabyte of it
    try:
        raise ValueError("x")
    except ValueError:
        logging.getLogger("latet.server").exception("%s", text * 16)
    assert time.perf_counter() - started < 0.5


# --- a slow or stalled output ------------------------------------------------------------------------------------------


class _Stalled(io.TextIOBase):
    """An output that takes nothing until it is let go: a log driver that blocks while its endpoint is away, a pipe
    nobody reads."""

    def __init__(self, stall: float = 10.0) -> None:
        self.entered, self.go, self.stall = threading.Event(), threading.Event(), stall
        self.written: list[str] = []

    def write(self, text):
        self.entered.set()
        self.go.wait(self.stall)
        self.written.append(text)
        return len(text)


async def test_a_stalled_output_never_holds_the_loop_and_what_it_cannot_take_is_counted(monkeypatch):
    """Each line is written by a thread of its own: a request's line costs the event loop no wait on the output. What
    waits is bounded: past it a line is dropped and counted, never waited for, and the beat's flush says how many."""
    monkeypatch.setattr(logs_module, "QUEUE_LINES", 20)
    output = _Stalled()
    with saved():
        configure_logging("json", "info", stream=output)
        app = RequestLog(_answering(200))
        await _call(app, _scope("/first"))
        assert output.entered.wait(5)  # the output took that one, and stalls
        slowest = 0.0
        for n in range(199):  # one line each, on the event loop
            began = time.perf_counter()
            await _call(app, _scope(f"/x/{n}"))
            slowest = max(slowest, time.perf_counter() - began)
        flush()  # the beat, once a minute: no room yet, so the count waits
        output.go.set()
        logging.getLogger().handlers[0].flush()
        flush()  # the beat again: said now
        logging.getLogger().handlers[0].flush()
    assert slowest < 0.25  # never the output's 10 s
    said = [json.loads(line) for line in "".join(output.written).splitlines()]
    assert [line["path"] for line in said[:21]] == ["/first", *(f"/x/{n}" for n in range(20))]
    assert (
        said[21]["message"]
        == "179 log records were dropped since the line before: the output did not take them in time"
    )
    assert len(said) == 22 and said[21]["level"] == "WARNING"


def test_a_record_waiting_for_the_output_is_written_when_the_logging_is_set_again():
    """What waits is written before the writer is replaced (as at the process's end): nothing logged is lost."""
    output = _Stalled(stall=0.2)
    with saved():
        configure_logging("json", "info", stream=output)
        for n in range(5):
            logging.getLogger("latet.server").info("line %d", n)
        configure_logging("json", "info", stream=io.StringIO())
    assert [json.loads(line)["message"] for line in "".join(output.written).splitlines()] == [
        f"line {n}" for n in range(5)
    ]


# --- warnings and exceptions that nothing catches -------------------------------------------------------------------------


def test_a_warning_and_what_ends_a_thread_go_to_the_lines(logs, capsys):
    configure_logging("json", "info", stream=logs)
    with warnings.catch_warnings():  # shown whatever filters the environment sets (it keeps how one is shown)
        warnings.simplefilter("always")
        warnings.warn(f"could not read the address {EMAIL}", UserWarning, stacklevel=1)

    def fails():
        raise KeyError(EMAIL)

    thread = threading.Thread(target=fails)
    thread.start()
    thread.join()

    class Leaky:
        def __del__(self):
            raise ValueError(f"could not close the file of {EMAIL}")

    leaky = Leaky()
    del leaky
    gc.collect()
    sys.excepthook(KeyError, KeyError(EMAIL), None)
    assert capsys.readouterr().err == ""
    warned, ended, unraisable, uncaught = _lines(logs)
    assert warned["logger"] == "py.warnings" and warned["message"].startswith("UserWarning at ")
    assert warned["message"].endswith(f"{__file__}, line {_line_of('warnings.warn(f')}")
    assert ended["message"] == "a thread ended on an uncaught KeyError" and ended["exception"].endswith("KeyError")
    assert unraisable["message"] == "an exception could not be raised: ValueError"
    assert uncaught["message"] == "the process ends on an uncaught KeyError" and uncaught["level"] == "CRITICAL"
    assert EMAIL not in logs.getvalue()


def _line_of(text):
    """The line of this file that ``text`` starts (after its indentation)."""
    with open(__file__, encoding="utf-8") as source:
        return next(number for number, line in enumerate(source, 1) if line.lstrip().startswith(text))


def test_after_saved_the_logging_is_as_it_was():
    root = logging.getLogger()
    before = (list(root.handlers), root.level, logging.getLogger("httpx").level, logging.getLogger("latet").level,
              warnings.showwarning, sys.excepthook, threading.excepthook, sys.unraisablehook)  # fmt: skip
    with saved():
        configure_logging("json", "debug", stream=io.StringIO())
        assert logging.getLogger("latet").level == logging.DEBUG and sys.excepthook is not before[5]
    after = (list(root.handlers), root.level, logging.getLogger("httpx").level, logging.getLogger("latet").level,
             warnings.showwarning, sys.excepthook, threading.excepthook, sys.unraisablehook)  # fmt: skip
    assert after == before
