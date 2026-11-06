"""The characterization of the requests, on a computer and on a server (P1-1 of
`notes/server-request-coverage-plan.md`): what each request of the table (ledger IDs I1-I5, H0-H14, W0-W3, C0, S1-S4,
mapped to the code by `notes/server-request-coverage-inventory.md`) does, first on 0.10, so that P1-2..P1-5 could show
that nothing changed but RC-REC-11's three leak fixes, now on 0.11, as Phase 2's baseline. It documents behaviour, and
never says that a request passed on as it came is safe.

A case drives the whole app as a client would, through raw ASGI (the raw target, every header occurrence in its order,
the body or the frames exactly as given): on a computer the app of a real runtime (``make_runtime``), and the bare app
with no mediator that ``latet serve --passthrough`` ran in 0.10 (0.11 removed the flag, and refuses every request
there); on a server, the server's app (its gate, then the space of an API key, PostgreSQL and Valkey). What leaves is seen by ``tests/request_recorder.py``. The observation of a case keeps names and categories
only, never a value: every synthetic original is ``<O:n>`` and every fake ``<F:n>`` (numbered as they first appear in
the case), a header or query value is the inbound occurrence it equals (``in3``) or a digest of what it became, a body
is its structure (names, kinds) and the digest of its canonical bytes, a log line its logger, level and template.

``tests/data/request-coverage/characterization-0.10.json.gz`` (the golden, ``GOLDEN``) holds, for the table and for the
generated cases of the four cells (computer and server, HTTP and WebSocket), the observation recorded on 0.10's pinned
head: Phase 1's evidence, never recorded again, what documents 0.10 (the leak rows' counts, P2-2's body oracle) reads
it, with the requests it was recorded for (``TABLE_0_10``). Phase 2 changed what a request does on purpose (every part
read or refused, model and session ids by their grammars, no gateway without a mediator), so the table's requests name
valid ids (``Inputs``) and ``tests/data/request-coverage/characterization-0.11.json.gz`` (the baseline, ``BASELINE``)
holds what 0.11 does, recorded on its head (``scripts/request_characterization.py record``): the two test modules
compare the table with it exactly (``baseline_judge``), the script's ``replay`` compares everything, so that a later
change that alters a request's outcome by accident is caught."""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import json
import logging
import random
import re
import threading
import types
import urllib.parse
import zlib
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
from starlette.requests import Request
from starlette.responses import StreamingResponse

from latet.bridge.api import Bridge
from latet.config import Config, Destination, IdentityConfig, LiveReview, LlmReview, Profile, RequestResource
from latet.engine.pipeline import Scope
from latet.gateway import forward
from latet.gateway.approvals import Decision
from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.mediate import Mediator
from latet.gateway.recorder import Recorder
from latet.gateway.routes import DEFAULT_UPSTREAMS, Upstream
from latet.logline import Paced
from latet.vision.policy import ImagePolicy

from .e2e.fake_upstream import FakeUpstream
from .request_recorder import RecordingClient, RecordingTransport, RecordingWSUpstream

GOLDEN = Path(__file__).parent / "data" / "request-coverage" / "characterization-0.10.json.gz"  # Phase 1's evidence
BASELINE = Path(__file__).parent / "data" / "request-coverage" / "characterization-0.11.json.gz"  # Phase 2's
OBSERVED_CALLS = Path(__file__).parent / "data" / "request-coverage" / "observed-calls.json"
SCHEMA = "latet-request-characterization/1"
# P1-4 sets it True with RC-REC-11's three fixes: the leak rows then hold their expected counts after Phase 1, not
# today's. No other row's expectation changes in Phase 1.
LEAK_FIXES_IN = True

# --- synthetic values -------------------------------------------------------------------------------------------------
# Never a real person's or secret: shapes the balanced profile masks (or, for the card number written as a JSON number,
# one it leaves in clear and the final check stops). A case's observation names each as <O:n>, n its place here.
EMAIL = "giulia.marchetti@gmail.com"
EMAIL_2 = "customer.number00042@gmail.com"
SECRET = "ghp_" + "Zq3vR8kL2mN5pT7wX9yB4cD6fH1jK0sA3eG5"
PHONE = "+39 347 555 0192"
IBAN = "IT60X0542811101000000123456"
CARD = "4111111111111111"
NAME = "Ornella Vanoni Bertolucci"  # read by the local reviewer of the table alone (no detector of the profile)
ORIGINALS = (EMAIL, EMAIL_2, SECRET, PHONE, IBAN, CARD, NAME)
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="  # one pixel

COMPUTER_HOST = "127.0.0.1:5283"
SERVER_URL = "https://latet.acme.lan"
SERVER_HOST = "latet.acme.lan"
CLIENT = ("127.0.0.1", 50123)
SERVER_CLIENT = ("10.0.3.9", 40000)
SEEDED_ROOT_KEY = bytes(range(32))  # the server cells' identities' root key (a test key)
_TURN_ENDS = ("response.completed", "response.done", "response.failed", "response.incomplete", '"ack"', '"error"')

# --- ASGI drivers ------------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class ClientView:
    """What the client got: an HTTP answer (status, headers, body), or a WebSocket's accept, denial, frames and close."""

    status: int | None = None
    headers: list[tuple[bytes, bytes]] = field(default_factory=list)
    body: bytes = b""
    accepted: bool = False
    frames: list[str | bytes] = field(default_factory=list)
    close: tuple[int, str] | None = None
    error: str | None = None  # the app raised (its type)


def _split(target: bytes) -> tuple[bytes, bytes, str]:
    path, _, query = target.partition(b"?")
    return path, query, urllib.parse.unquote(path.decode("latin-1"))


async def http_call(
    app: Any,
    method: str,
    target: bytes,
    headers: Sequence[tuple[bytes, bytes]],
    body: bytes,
    client: tuple[str, int],
    server: tuple[str, int],
    scheme: str,
    seconds: float = 60.0,
) -> ClientView:
    """One HTTP request through ``app`` as uvicorn hands it over (spec 2.3: the answer streamed while the client is
    there), the raw target and every header as given; the client stays until the answer ends."""
    raw_path, query, path = _split(target)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": scheme,
        "path": path,
        "raw_path": raw_path,
        "query_string": query,
        "root_path": "",
        "headers": list(headers),
        "client": client,
        "server": server,
        "state": {},
    }
    view = ClientView()
    done = asyncio.Event()
    asked = [False]

    async def receive() -> dict[str, Any]:
        if not asked[0]:
            asked[0] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            view.status = message["status"]
            view.headers = [(bytes(k), bytes(v)) for k, v in message.get("headers", [])]
        elif message["type"] == "http.response.body":
            view.body += message.get("body", b"")
            if not message.get("more_body"):
                done.set()

    try:
        await asyncio.wait_for(app(scope, receive, send), seconds)
    except Exception as exc:
        view.error = type(exc).__name__
    finally:
        done.set()
    return view


async def ws_call(
    app: Any,
    target: bytes,
    headers: Sequence[tuple[bytes, bytes]],
    frames: Sequence[str | bytes],
    client: tuple[str, int],
    server: tuple[str, int],
    scheme: str,
    seconds: float = 30.0,
    settle: Callable[[], Awaitable[None]] | None = None,
) -> ClientView:
    """One WebSocket through ``app``: its handshake, then each frame in turn, the next sent once the app has answered
    the one before (a turn's end, an ``ack`` the recording upstream sends, an error) or closed; then the client goes,
    once ``settle`` has returned (the relay done with what it answered), or at once without one. A client gone at once
    raced its last turn's record on 0.11 until F2 of the P2 baseline was fixed (the record was whole or an error, as the
    engine's thread and the close came); the table's rows wait (``run_case``), as the baseline was recorded so, and a
    client gone at once has a test of its own."""
    raw_path, query, path = _split(target)
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": scheme,
        "path": path,
        "raw_path": raw_path,
        "query_string": query,
        "root_path": "",
        "headers": list(headers),
        "client": client,
        "server": server,
        "subprotocols": [],
        "state": {},
        "extensions": {"websocket.http.response": {}},
    }
    view = ClientView()
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    changed = asyncio.Event()
    ends = [0]
    finished = asyncio.Event()

    async def receive() -> dict[str, Any]:
        return await inbox.get()

    async def send(message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "websocket.accept":
            view.accepted = True
        elif kind == "websocket.send":
            data = message.get("text")
            data = data if data is not None else message.get("bytes", b"")
            view.frames.append(data)
            if isinstance(data, bytes) or any(end in data for end in _TURN_ENDS):
                ends[0] += 1
        elif kind == "websocket.close":
            view.close = (message.get("code", 1000), message.get("reason") or "")
            finished.set()
        elif kind == "websocket.http.response.start":
            view.status = message["status"]
            view.headers = [(bytes(k), bytes(v)) for k, v in message.get("headers", [])]
        elif kind == "websocket.http.response.body":
            view.body += message.get("body", b"")
            if not message.get("more_body"):
                finished.set()
        changed.set()

    async def wait(check: Callable[[], bool], seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                while not check():
                    changed.clear()
                    await changed.wait()

    await inbox.put({"type": "websocket.connect"})
    task = asyncio.create_task(app(scope, receive, send))
    task.add_done_callback(lambda _: (finished.set(), changed.set()))
    await wait(lambda: view.accepted or finished.is_set(), seconds)
    for frame in frames:
        if finished.is_set() or not view.accepted:
            break
        before = ends[0]
        message = (
            {"type": "websocket.receive", "text": frame}
            if isinstance(frame, str)
            else {"type": "websocket.receive", "bytes": frame}
        )
        await inbox.put(message)
        await wait(lambda before=before: ends[0] > before or finished.is_set(), 10.0)
    if settle is not None and view.accepted and not finished.is_set():
        await settle()
    await inbox.put({"type": "websocket.disconnect", "code": 1000})
    try:
        await asyncio.wait_for(task, seconds)
    except Exception as exc:
        view.error = type(exc).__name__
    return view


# --- canonical, value-free observations -------------------------------------------------------------------------------


def digest(data: bytes | str) -> str:
    raw = data.encode("utf-8", "surrogatepass") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()[:16]


class Canon:
    """A case's originals and fakes as tokens: an original as ``<O:n>`` (its place in ``ORIGINALS``), a fake as
    ``<F:n>``, numbered as the fakes first appear in what is read through it, in the observation's fixed order."""

    def __init__(self, fakes: Sequence[str], secrets: Sequence[tuple[str, str]] = ()) -> None:
        named = [(o, f"<O:{i}>") for i, o in enumerate(ORIGINALS)] + [(s, t) for s, t in secrets if s]
        self.originals = sorted(named, key=lambda p: -len(p[0]))
        candidates = sorted({f for f in fakes if f and f not in ORIGINALS and len(f) >= 4}, key=lambda f: (-len(f), f))
        self.pattern = re.compile("|".join(map(re.escape, candidates))) if candidates else None
        self.fakes: dict[str, str] = {}

    def _token(self, match: re.Match[str]) -> str:
        return self.fakes.setdefault(match.group(0), f"<F:{len(self.fakes)}>")

    def text(self, value: str) -> str:
        if not value:
            return value
        if self.pattern is not None:  # left to right, the longest fake first where two start at one place
            value = self.pattern.sub(self._token, value)
        for original, token in self.originals:
            if original in value:
                value = value.replace(original, token)
            quoted = urllib.parse.quote(original, safe="")
            if quoted != original and quoted in value:
                value = value.replace(quoted, token)
        return value

    def data(self, value: bytes) -> bytes:
        return self.text(value.decode("latin-1")).encode("latin-1", "replace")

    def tokens(self, value: str) -> str:
        """The kinds of tokens a canonical text holds: ``O`` an original, ``F`` a fake."""
        return ("O" if "<O:" in value else "") + ("F" if "<F:" in value else "")


def skeleton(doc: Any, canon: Canon, depth: int = 0) -> Any:
    """A JSON document's structure: its names, and of each value its kind (a string's tokens too), lists cut at 12."""
    if depth > 12:
        return "…"
    if isinstance(doc, dict):
        return {canon.text(str(k)): skeleton(v, canon, depth + 1) for k, v in doc.items()}
    if isinstance(doc, list):
        out = [skeleton(v, canon, depth + 1) for v in doc[:12]]
        return out + ([f"+{len(doc) - 12}"] if len(doc) > 12 else [])
    if isinstance(doc, str):
        return "s" + canon.tokens(canon.text(doc))
    if isinstance(doc, bool):
        return "b"
    if isinstance(doc, int | float):
        return "n"
    return "z"


def body_view(content: bytes, canon: Canon, inbound: bytes | None = None) -> dict[str, Any]:
    canonical = canon.data(content)
    view: dict[str, Any] = {"len": len(canonical), "sha": digest(canonical)}
    if inbound is not None:
        view["as_sent"] = content == inbound
    try:
        doc = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        doc = None
        view["kind"] = "empty" if not content else "bytes"
    if doc is not None or content.strip() == b"null":
        view["json"] = skeleton(doc, canon)
    return view


def sent_body_view(sent: Any, canon: Canon, inbound: bytes) -> dict[str, Any]:
    """What a request carried upstream: a gzip-coded body by what it decodes to (its fakes then named as tokens, and
    no digest of zlib's output, which differs from one zlib to another), its coding said beside it; ``as_sent``
    compares the bytes as sent with the client's."""
    coding = next(
        (v.decode("latin-1").strip().lower() for n, v in sent.headers if n.lower() == b"content-encoding"), None
    )
    if coding in ("gzip", "x-gzip"):
        try:
            decoded = gzip.decompress(sent.content)
        except (OSError, EOFError, zlib.error):
            return body_view(sent.content, canon, inbound) | {"encoding": coding, "decodes": False}
        return body_view(decoded, canon) | {"as_sent": sent.content == inbound, "encoding": coding, "decodes": True}
    return body_view(sent.content, canon, inbound)


_RANDOM = frozenset({b"sec-websocket-key", b"sec-websocket-accept"})


def header_view(
    raw: Sequence[tuple[bytes, bytes]], inbound: Sequence[tuple[bytes, bytes]], canon: Canon, content: bytes | None
) -> list[list[str]]:
    """Each header occurrence in order: its name as sent, and its value's category: ``in<k>`` the k-th inbound
    occurrence's value under the same name (``in<k>~`` under another), ``len`` the content's length, else the digest
    of its canonical value (with its tokens)."""
    out = []
    for name, value in raw:
        lowered = name.lower()
        category = None
        for k, (iname, ivalue) in enumerate(inbound):
            if ivalue == value and iname.lower() == lowered:
                category = f"in{k}"
                break
        if category is None:
            for k, (_, ivalue) in enumerate(inbound):
                if ivalue == value and value:
                    category = f"in{k}~"
                    break
        if (
            category is None
            and lowered == b"content-length"
            and content is not None
            and value == str(len(content)).encode()
        ):
            category = "len"
        if category is None and lowered in _RANDOM:
            category = "random"  # a nonce the library draws for each connection
        if category is None and lowered == b"host" and value.startswith(b"127.0.0.1:"):
            category = "local"  # the recording WebSocket upstream's port, drawn for each run
        if category is None and (lowered == b"x-amz-date" or value.startswith(b"AWS4-HMAC-SHA256 ")):
            category = "sigv4"  # Latet's signature: its time and what it signs, of this run
        if category is None and lowered == b"allow":  # Starlette lists a route's methods from a set
            value = b", ".join(sorted(part.strip() for part in value.split(b",")))
        if category is None:
            text = canon.text(value.decode("latin-1"))
            category = "v" + canon.tokens(text) + ":" + digest(text)
        out.append([name.decode("latin-1"), category])
    return out


def target_view(raw: bytes, inbound_query: bytes, canon: Canon) -> dict[str, Any]:
    path, _, query = raw.partition(b"?")
    inbound = [part.partition(b"=") for part in inbound_query.split(b"&")] if inbound_query else []
    occurrences = []
    for part in query.split(b"&") if b"?" in raw else []:
        name, eq, value = part.partition(b"=")
        category = next(
            (f"in{k}" for k, (n, _, v) in enumerate(inbound) if n == name and v == value),
            "v" + canon.tokens(canon.text(value.decode("latin-1"))) + ":" + digest(canon.data(value)),
        )
        occurrences.append([canon.text(name.decode("latin-1")), ("=" if eq else "") + category])
    return {"path": canon.text(path.decode("latin-1")), "query": occurrences if b"?" in raw else None}


def words_of(body: bytes, canon: Canon) -> dict[str, Any]:
    """An answer's words: a Latet error's message as it says it (no value: its originals and fakes as tokens), any
    other answer by its structure and digest."""
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        doc = None
    if isinstance(doc, dict):
        error = doc.get("error")
        message = None
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
        elif isinstance(error, str):
            message = error
        if message is not None:
            return {"error": canon.text(message), "json": skeleton(doc, canon)}
    if body.startswith((b"event:", b"data:")):
        return sse_view(body, canon)
    return body_view(body, canon)


def sse_view(body: bytes, canon: Canon) -> dict[str, Any]:
    """A stream of server-sent events: each event's name (or its data's ``type``), and the digest of the canonical
    stream."""
    names = []
    for block in body.replace(b"\r\n", b"\n").split(b"\n\n"):
        event = data = None
        for line in block.split(b"\n"):
            if line.startswith(b"event:"):
                event = line[6:].strip().decode("latin-1")
            elif line.startswith(b"data:"):
                data = line[5:].strip()
        if event is None and data is not None:
            try:
                found = json.loads(data)
                event = found.get("type") if isinstance(found, dict) and isinstance(found.get("type"), str) else "data"
            except ValueError:
                event = "[DONE]" if data == b"[DONE]" else "data"
        if event is not None:
            names.append(event)
    canonical = canon.data(body)
    return {"sse": names, "len": len(canonical), "sha": digest(canonical)}


def frame_view(frame: str | bytes, canon: Canon) -> Any:
    if isinstance(frame, bytes):
        return {"bytes": len(frame), "sha": digest(canon.data(frame))}
    try:
        doc = json.loads(frame)
    except ValueError:
        return {"text": len(frame), "sha": digest(canon.text(frame))}
    if isinstance(doc, dict):
        kind = doc.get("type")
        error = doc.get("error") if isinstance(doc.get("error"), dict) else None
        if error is not None and isinstance(error.get("message"), str):
            return {"type": kind, "error": canon.text(error["message"])}
        return {
            "type": kind if isinstance(kind, str) else None,
            "json": skeleton(doc, canon),
            "sha": digest(canon.text(frame)),
        }
    return {"json": skeleton(doc, canon), "sha": digest(canon.text(frame))}


_SUMMARY_KEPT = (
    "agent",
    "upstream",
    "signed",
    "path",
    "protocol",
    "mode",
    "transport",
    "status",
    "masked",
    "new_masked",
    "by_type",
    "new_by_type",
    "by_class",
    "by_module",
    "restored",
    "flags",
    "block_reason",
    "verified",
)


def record_view(record: Any, canon: Canon) -> dict[str, Any]:
    """What the history was given of an exchange (``ExchangeRecord``): its summary's fields that do not change from a
    run to the next (no id, time, session key or duration), its blocks, leaks and headers by name and kind."""
    summary = record.summary
    view: dict[str, Any] = {}
    for key in _SUMMARY_KEPT:
        value = summary.get(key)
        view[key] = canon.text(value) if isinstance(value, str) else value
    view["model"] = None if summary.get("model") is None else "s"
    view["host"] = summary.get("host") is not None
    view["project"] = summary.get("project") is not None
    view["tokens"] = summary.get("tokens") is not None
    view["blocks"] = len(record.blocks)
    view["images"] = len(record.images)
    view["leaks"] = [[leak.get("type"), leak.get("kind"), leak.get("new")] for leak in record.leaks]
    view["request_headers"] = [name for name, _ in record.request_headers]
    view["response"] = sorted(record.response) if isinstance(record.response, dict) else None
    view["body_bytes"] = record.body_bytes
    return view


# --- what the engine, the mediator and the logs saw -------------------------------------------------------------------

_SOURCE = threading.local()


@contextlib.contextmanager
def _source(name: str) -> Iterator[None]:
    before = getattr(_SOURCE, "name", None)
    _SOURCE.name = name
    try:
        yield
    finally:
        _SOURCE.name = before


class EngineSeam:
    """The engine's ``hold``, ``renew``, ``reacquire`` and ``release`` counted where the gateway calls them; the calls
    of ``Engine.restores`` (``restores.<source>``); and each renew the vault itself is asked for (a vault that renews:
    ``StoredVault``), ``vault.renew.<source>``, by who asked: the renew timer (``Engine.renew``), or the restores of
    the response transforms, of ``Recorder._snapshot`` or of ``Bridge._rehydrate`` (re-review M2). A restores that
    renews no more is seen. Wraps one engine's methods and its vault's ``renew`` (instances' attributes), never a
    class."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.engine: Any = None
        self.vault: Any = None
        self._saved: list[str] = []

    def attach(self, engine: Any) -> None:
        if engine is self.engine:
            return
        self.detach()
        self.engine, self.vault = engine, engine.vault
        for name in ("hold", "reacquire", "release"):
            setattr(engine, name, self._counted(name, getattr(engine, name)))
            self._saved.append(name)
        renew, restores = engine.renew, engine.restores

        def counted_renew(session: Any) -> Any:
            self.counts["renew"] += 1
            with _source("timer"):
                return renew(session)

        def counted_restores(session: Any, json_mode: bool) -> Any:
            self.counts["restores." + (getattr(_SOURCE, "name", None) or "transform")] += 1
            return restores(session, json_mode)

        engine.renew, engine.restores = counted_renew, counted_restores
        self._saved += ["renew", "restores"]
        vault_renew = getattr(self.vault, "renew", None)
        if vault_renew is not None:

            def counted_vault_renew(*args: Any, **kwargs: Any) -> Any:
                self.counts["vault.renew." + (getattr(_SOURCE, "name", None) or "transform")] += 1
                return vault_renew(*args, **kwargs)

            self.vault.renew = counted_vault_renew

    def _counted(self, name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def counted(*args: Any, **kwargs: Any) -> Any:
            self.counts[name] += 1
            return original(*args, **kwargs)

        return counted

    def detach(self) -> None:
        if self.engine is not None:
            for name in self._saved:
                with contextlib.suppress(AttributeError):
                    delattr(self.engine, name)
            with contextlib.suppress(AttributeError):
                delattr(self.vault, "renew")
        self._saved.clear()
        self.engine = self.vault = None


class RenewClock:
    """``forward.renewing``'s sleeps under the test's hand (a controlled clock, re-review M2): its every
    ``RENEW_SECONDS`` waits for a ``tick``, which wakes each renewer once; the renewers ``handle`` starts are kept
    (``tasks``) to say how each ended. Installed as ``forward.asyncio``, the module's other uses of asyncio unchanged."""

    def __init__(self) -> None:
        self._real_sleep = asyncio.sleep
        self._waiting: list[asyncio.Future[None]] = []
        self.tasks: list[asyncio.Task[Any]] = []
        names = {name: getattr(asyncio, name) for name in dir(asyncio) if not name.startswith("__")}
        self.module = types.SimpleNamespace(**names)
        self.module.sleep = self.sleep
        self.module.create_task = self.create_task

    async def sleep(self, delay: float, result: Any = None) -> Any:
        if delay == forward.RENEW_SECONDS:
            future = asyncio.get_running_loop().create_future()
            self._waiting.append(future)
            await future
            return result
        return await self._real_sleep(delay, result)

    def create_task(self, coro: Any, **kwargs: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, **kwargs)
        if getattr(coro, "__name__", "") == "renewing":
            self.tasks.append(task)
        return task

    async def settle(self) -> None:
        for _ in range(8):
            await self._real_sleep(0)

    async def tick(self) -> None:
        waiting, self._waiting = self._waiting, []
        for future in waiting:
            if not future.done():
                future.set_result(None)
        await self.settle()

    def states(self) -> list[str]:
        return ["cancelled" if task.cancelled() else "ended" if task.done() else "running" for task in self.tasks]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.tasks.clear()
        self._waiting.clear()


class LogLines(logging.Handler):
    """Each line Latet logs at INFO or above: its logger, level, template (``record.msg``) and its arguments, kept in
    memory as texts until the observation names them (``log_args``): a number by its kind alone (a duration changes
    from a run to the next), a text with its originals and fakes as tokens."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[tuple[str, str, str, tuple[Any, ...]]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "latet" or record.name.startswith("latet."):
            args = record.args if isinstance(record.args, tuple) else ()
            kept = tuple(a if isinstance(a, int | float | str | type(None)) else type(a).__name__ for a in args)
            self.lines.append((record.name, record.levelname, str(record.msg), kept))


def log_args(args: Sequence[Any], canon: Canon) -> list[Any]:
    return ["n" if isinstance(a, int | float) and not isinstance(a, bool) else a if a is None else canon.text(str(a))
            for a in args]  # fmt: skip


@contextlib.contextmanager
def logged() -> Iterator[LogLines]:
    handler = LogLines()
    logger = logging.getLogger("latet")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


# --- the provider behind the recording transport ----------------------------------------------------------------------


class AnswerStream(httpx.AsyncByteStream):
    """An answer's body as the provider streams it, which knows whether the gateway closed it (``aclose``): an answer
    left open is RC-REC-11 (a)'s other half."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body

    async def aclose(self) -> None:
        self.closed = True


class Provider:
    """What the recording transport answers: ``FakeUpstream``'s app (unchanged), or a case's own answer (``override``:
    a redirect, a refusal, as status, headers and body); each answer's stream kept (``answers``), to say whether the
    gateway closed it."""

    def __init__(self) -> None:
        self.fake = FakeUpstream()
        self.inner = httpx.ASGITransport(app=self.fake.app)
        self.override: Callable[[httpx.Request], tuple[int, dict[str, str], bytes]] | None = None
        self.answers: list[AnswerStream] = []

    def _answer(self, status: int, headers: Any, body: bytes) -> httpx.Response:
        stream = AnswerStream(body)
        self.answers.append(stream)
        return httpx.Response(status, headers=headers, stream=stream)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.override is not None:
            return self._answer(*self.override(request))
        try:
            answer = await self.inner.handle_async_request(request)
        except Exception as exc:
            error = {"error": {"message": f"the fake provider could not read it ({type(exc).__name__})"}}
            return self._answer(400, {"content-type": "application/json"}, json.dumps(error).encode())
        body = await answer.aread()
        headers = [(k, v) for k, v in answer.headers.raw if k.lower() not in (b"content-length",)]
        return self._answer(answer.status_code, headers, body)


class Clients:
    """Every client the gateway makes (``GatewayConfig.client``, or ``make_client`` patched on a server), each over the
    one recording transport, so that a case counts what any of them built and sent."""

    def __init__(self, transport: RecordingTransport) -> None:
        self.transport = transport
        self.made: list[RecordingClient] = []

    def __call__(self, *_: Any, **__: Any) -> RecordingClient:
        client = RecordingClient(self.transport)
        self.made.append(client)
        return client

    def counts(self) -> tuple[int, int]:
        return sum(c.builds for c in self.made), sum(c.sends for c in self.made)


# --- cells ---------------------------------------------------------------------------------------------------------


@dataclass
class Cell:
    """A machine the cases run on: its app, how its addresses are written, and what each case reads of it."""

    machine: str  # "computer", "passthrough" (no mediator: 0.10's ``latet serve --passthrough``) or "server"
    app: Any
    prefix: str  # "" on a computer, "/u/<route key>" on a server
    host: str
    client: tuple[str, int]
    transport: RecordingTransport
    clients: Clients
    ws: RecordingWSUpstream
    provider: Provider
    clock: RenewClock
    seam: EngineSeam = field(default_factory=EngineSeam)
    bridge_header: tuple[bytes, bytes] | None = None
    runtime_of: Callable[[], Awaitable[Any]] | None = None  # the runtime whose mediator serves the cases
    other_app: Any = None  # the server's second instance
    other_runtime_of: Callable[[], Awaitable[Any]] | None = None  # the key's runtime there
    # Credentials of the run (the key's secret and route key, the bridge's token): named as tokens wherever they show.
    secrets: tuple[tuple[str, str], ...] = ()
    scheme: str = "http"
    tmp: Path | None = None

    async def runtime(self, other: bool = False) -> Any:
        """The runtime the cases are served by (``other``: on the server's second instance), its engine counted and
        its records kept from now on."""
        runtime_of = self.other_runtime_of if other and self.other_runtime_of is not None else self.runtime_of
        if runtime_of is None:
            return None
        runtime = await runtime_of()
        self.seam.attach(runtime.built.engine)
        recorder = runtime.gateway.recorder
        if recorder is not None and not getattr(recorder, "_rc_wrapped", False):
            _wrap_recorder(recorder)
        return runtime

    @property
    def server(self) -> tuple[str, int]:
        return ("latet.acme.lan", 443) if self.machine == "server" else ("127.0.0.1", 5283)


def _wrap_recorder(recorder: Recorder) -> None:
    """The records the history is given (``store.record``), kept on the recorder (``_rc_records``); its snapshots'
    restores counted as the recorder's (``EngineSeam``)."""
    store = recorder.store
    recorder._rc_records = []  # type: ignore[attr-defined]
    if store is not None:
        record = store.record

        def recorded(item: Any, *args: Any, **kwargs: Any) -> Any:
            recorder._rc_records.append(item)  # type: ignore[attr-defined]
            return record(item, *args, **kwargs)

        store.record = recorded  # type: ignore[method-assign]
    snapshot = recorder._snapshot

    def tagged(prepared: Any) -> Any:
        with _source("recorder"):
            return snapshot(prepared)

    recorder._snapshot = tagged  # type: ignore[method-assign]
    recorder._rc_wrapped = True  # type: ignore[attr-defined]


@contextlib.contextmanager
def patched(target: Any, name: str, value: Any) -> Iterator[None]:
    """``target.name`` set to ``value`` while it lasts (a module's, a class's or an instance's attribute)."""
    missing = object()
    before = target.__dict__.get(name, missing) if hasattr(target, "__dict__") else getattr(target, name, missing)
    setattr(target, name, value)
    try:
        yield
    finally:
        if before is missing:
            with contextlib.suppress(AttributeError):
                delattr(target, name)
        else:
            setattr(target, name, before)


def local_upstreams(ws_url: str) -> tuple[Upstream, ...]:
    """The default routes with ChatGPT's (Codex's WebSocket) at the recording WebSocket upstream: every other one keeps
    its provider's address, which the recording transport answers in its place (no request leaves this computer)."""
    return tuple(
        Upstream(u.name, u.prefix, ws_url, keep_prefix=u.keep_prefix, refuses_unread=u.refuses_unread)
        if u.name == "chatgpt"
        else u
        for u in DEFAULT_UPSTREAMS
    )


@contextlib.contextmanager
def common_patches(ws_url: str, clock: RenewClock) -> Iterator[None]:
    """What every cell runs under: the routes of ``local_upstreams``, the renewers' controlled clock, and the bridge's
    restores counted as its own."""
    from latet.gateway import app as gateway_app

    rehydrate = Bridge._rehydrate

    def tagged(self: Bridge, data: dict[str, Any]) -> Any:
        with _source("bridge"):
            return rehydrate(self, data)

    with (
        patched(gateway_app, "DEFAULT_UPSTREAMS", local_upstreams(ws_url)),
        patched(forward, "asyncio", clock.module),
        patched(Bridge, "_rehydrate", tagged),
    ):
        yield


def _parts(ws: RecordingWSUpstream) -> tuple[Provider, RecordingTransport, Clients, RenewClock]:
    provider = Provider()
    transport = RecordingTransport(provider)
    return provider, transport, Clients(transport), RenewClock()


# The owner-listed Vertex project of every cell (minors after 0.11, item 13): a computer's settings list it, a server's
# API key holds it among its own settings, as the administration's save keeps them; the read path's rows name it.
VERTEX_PROJECT = {
    "destination": "vertex",
    "url": "https://us-east5-aiplatform.googleapis.com",
    "role": "project",
    "value": "rc-project",
}


def computer_config(ws_url: str) -> Config:
    """A computer's settings for the cases: the balanced profile, no name model, no local identity (whose user and host
    names are the machine's), and a destination ``llm`` at the recording WebSocket upstream."""
    return Config(
        profile=Profile.BALANCED,
        ner="off",
        identity=IdentityConfig(enabled=False),
        upstreams=[Destination(name="llm", url=ws_url)],
        request_resources=[RequestResource.model_validate(VERTEX_PROJECT)],
    )


@contextlib.asynccontextmanager
async def computer_cell(tmp: Path, ws: RecordingWSUpstream) -> AsyncIterator[Cell]:
    """A computer: the app of a real runtime (its recorder, approvals, bridge and AWS store), its client recorded."""
    from latet.gateway.app import create_app
    from latet.runtime import make_runtime

    provider, transport, clients, clock = _parts(ws)
    with common_patches(ws.url, clock):
        runtime = make_runtime(computer_config(ws.url), data=tmp / "data", config_file=tmp / "config.yaml")
        try:
            runtime.gateway.client = clients
            app = create_app(runtime.gateway)

            async def runtime_of() -> Any:
                return runtime

            async with app.router.lifespan_context(app):
                token = (runtime.gateway.bridge_token or "").encode()
                cell = Cell(
                    "computer",
                    app,
                    "",
                    COMPUTER_HOST,
                    CLIENT,
                    transport,
                    clients,
                    ws,
                    provider,
                    clock,
                    bridge_header=(b"x-latet-token", token),
                    runtime_of=runtime_of,
                    secrets=((token.decode(), "<BRIDGE>"),),
                    tmp=tmp,
                )
                await cell.runtime()
                try:
                    yield cell
                finally:
                    await clock.stop()
                    cell.seam.detach()
        finally:
            runtime.close()


@contextlib.asynccontextmanager
async def passthrough_cell(ws: RecordingWSUpstream) -> AsyncIterator[Cell]:
    """The app with no mediator (a bare ``GatewayConfig``): what 0.10's ``latet serve --passthrough`` ran. 0.11 removed
    the flag (D5), and such an app refuses every request and WebSocket, request protection being unavailable (P2-4)."""
    from latet.gateway.app import GatewayConfig, create_app

    provider, transport, clients, clock = _parts(ws)
    with common_patches(ws.url, clock):
        app = create_app(GatewayConfig(client=clients))
        async with app.router.lifespan_context(app):
            try:
                yield Cell("passthrough", app, "", COMPUTER_HOST, CLIENT, transport, clients, ws, provider, clock)
            finally:
                await clock.stop()


_PACED_DUE = Paced.due
_FROZEN = threading.Lock()


def _frozen_due(self: Paced) -> int:
    """``Paced.due`` as it is, its clock stopped at 0 for this call alone: the instance keeps its own clock after it (a
    module's ``Paced`` outlives the cell)."""
    with _FROZEN:  # one swap at a time: two threads never keep each other's stopped clock
        clock = self._clock
        self._clock = lambda: 0.0
        try:
            return _PACED_DUE(self)
        finally:
            self._clock = clock


def server_config(ws_url: str) -> Config:
    """The shared settings of the server's cases: the balanced profile, no name model, and the destination ``llm`` at
    the recording WebSocket upstream (an identity's own user and host names are its computer's to send)."""
    return Config(profile=Profile.BALANCED, ner="off", upstreams=[Destination(name="llm", url=ws_url)])


@contextlib.asynccontextmanager
async def server_cell(servers: Sequence[Any], ws: RecordingWSUpstream) -> AsyncIterator[Cell]:
    """A server: the server's app (its gate, then the space of an API key, on PostgreSQL and Valkey), with the shared
    settings of ``server_config``; ``make_client`` patched so that each space's client is recorded. ``servers``: the
    instances of one server (``tests/server/conftest.py``'s ``instances``); the second serves the cases that say so."""
    from latet.gateway import app as gateway_app
    from latet.server import spaces
    from latet.server.app import create_server_app
    from latet.server.space_settings import PgSettings
    from latet.server.tokens import route_key

    from .server.support import listening

    provider, transport, clients, clock = _parts(ws)
    first = servers[0]
    with (
        common_patches(ws.url, clock),
        patched(gateway_app, "make_client", clients),
        # A seeded test vault: the key's fakes drawn alike in every run (a fake's length shapes what is sent and
        # restored), as a computer's are from the tests' fixed root key. Its vault starts empty: nothing sealed before.
        patched(spaces, "root_key", lambda *_, **__: SEEDED_ROOT_KEY),
        # The log's paced lines (a refusal said once a minute with how many since) on a clock that does not move: the
        # first of each kind said, the rest counted, whatever the run's speed; no summary written by a beat meanwhile.
        patched(Paced, "due", _frozen_due),
        patched(Paced, "flush", lambda self: 0),
    ):
        first.shared.set(server_config(ws.url), actor="test")
        made = getattr(first, "_rc_cells", 0) + 1
        first._rc_cells = made  # each cell an identity of its own: its vault and its conversations start empty
        identity, secret = first.identities.add_key(f"Request coverage {made}")
        own = PgSettings(first.db, first.shared, identity.id, SEEDED_ROOT_KEY, kind="key")
        listed = own.prepare(
            own.load().model_copy(update={"request_resources": [RequestResource.model_validate(VERTEX_PROJECT)]})
        )
        own.save(listed)
        own.save_reviewed(listed)  # the key's own settings, as the administration's save keeps them
        async with listening(*servers):

            async def runtime_of() -> Any:
                return await first.spaces.runtime(identity)

            async def other_runtime_of() -> Any:
                return await servers[-1].spaces.runtime(identity)

            apps = [create_server_app(server) for server in servers]
            cell = Cell(
                "server",
                apps[0],
                f"/u/{route_key(secret)}",
                SERVER_HOST,
                SERVER_CLIENT,
                transport,
                clients,
                ws,
                provider,
                clock,
                bridge_header=(b"x-latet-token", secret.encode()),
                runtime_of=runtime_of,
                other_app=apps[1] if len(apps) > 1 else None,
                other_runtime_of=other_runtime_of,
                secrets=((secret, "<KEY>"), (route_key(secret), "<ROUTE>")),
                scheme="https",
            )
            await cell.runtime()
            try:
                yield cell
            finally:
                await clock.stop()
                cell.seam.detach()


# --- cases ---------------------------------------------------------------------------------------------------------

Body = bytes | Callable[["Cell"], Awaitable[bytes]]


@dataclass(frozen=True)
class Case:
    """One request: where it goes (``target``, written after the cell's prefix), its headers in order, its body (or a
    function of the cell that makes it: a fake the vault drew), or its frames; ``setup``, what the case changes around
    it; ``leak``: one of RC-REC-11's rows, with its counts expected after Phase 1."""

    id: str
    ledger: tuple[str, ...]
    method: str = "POST"
    target: str = "/openai/v1/chat/completions"
    headers: tuple[tuple[str, str], ...] = ()
    body: Body = b""
    transport: str = "http"  # "http", "ws" or "bridge"
    frames: tuple[str | bytes, ...] = ()
    machines: tuple[str, ...] = ("computer", "server")
    setup: tuple[str, ...] = ()
    leak: str | None = None
    after_phase_1: dict[str, int | tuple[int, ...]] | None = (
        None  # the engine's counts once RC-REC-11 is fixed (leak rows only)
    )
    host: str | None = None  # a Host other than the cell's own
    raw_prefix: str | None = None  # on a server, an address other than the key's
    describe: str = ""


@dataclass(frozen=True)
class Inputs:
    """What the table's requests name only so that they are read at all, never what a row characterizes: the model ids
    (a body's, by its protocol; a cloud path's, by its provider), the agents' session ids and the tool calls' ids.
    0.10 read none of them, and its golden was recorded with placeholders (``INPUTS_0_10``); 0.11 takes each by its
    grammar (the plan's D4 notes, and their admission round of 2026-11-03: a model id by its provider's, a session id
    by its agent's, a tool call's id by its provider's, a call's output only after the call it answers) and refuses a
    placeholder before anything else of the request is exercised, so the P2 table names valid ones (``INPUTS``). The
    builders below write them."""

    openai: str  # a Chat Completions or Responses body's model
    codex: str  # a Codex WebSocket turn's model
    anthropic: str  # an Anthropic Messages body's model
    anthropic_other: str  # the second copy of h7-twice-verify-only's key
    bedrock: str  # a Bedrock path's model id
    bedrock_bad_region: str  # the model of i4-bedrock-bad-region's path
    vertex: str  # a Vertex path's Claude model
    gemini: str  # a Gemini path's model
    codex_session: str  # Codex's session-id header
    claude_session: str  # Claude Code's x-claude-code-session-id header
    call_id: str  # a Chat Completions tool call's id
    tool_use_id: str  # an Anthropic tool_use block's id
    ws_call_id: str  # the call w3-blocked's Codex turn answers
    ws_call_declared: bool  # whether that turn holds the function_call it answers

    def chat(self, text: str, **given: Any) -> bytes:
        return chat(text, model=self.openai, call_id=self.call_id, **given)

    def responses(self, text: str, **given: Any) -> bytes:
        return responses(text, model=self.openai, **given)

    def anthropic_body(self, text: str, **given: Any) -> bytes:
        return anthropic(text, model=self.anthropic, tool_use_id=self.tool_use_id, **given)

    def turn(self, text: str, **extra: Any) -> str:
        return turn(text, model=self.codex, **extra)

    def image_chat(self) -> bytes:
        return image_chat(self.openai)

    def ws_call(self, output: str) -> list[dict[str, Any]]:
        """A Codex turn's input answering a call with ``output``: the call itself first where it is declared."""
        answer = {"type": "function_call_output", "call_id": self.ws_call_id, "output": output}
        if not self.ws_call_declared:
            return [answer]
        call = {"type": "function_call", "call_id": self.ws_call_id, "name": "query", "arguments": "{}"}
        return [call, answer]

    def codex_ws(self) -> tuple[tuple[str, str], ...]:
        return (
            ("session-id", self.codex_session),
            ("originator", "codex_cli_rs"),
            ("authorization", "Bearer sk-rc-test"),
        )

    def claude_code(self) -> tuple[tuple[str, str], ...]:
        return (
            ("x-api-key", "sk-ant-rc-test"),
            ("anthropic-version", "2023-06-01"),
            ("content-type", "application/json"),
            ("user-agent", "claude-cli/2.1.312 (external, cli)"),
            ("x-claude-code-session-id", self.claude_session),
        )


# What 0.10's golden was recorded with (``tests/data/request-coverage/characterization-0.10.json.gz``, and P2-2's body
# oracle, whose inputs are that table's): kept so that what documents 0.10 still reads the requests it was recorded for.
INPUTS_0_10 = Inputs(
    openai="m",
    codex="m",
    anthropic="m",
    anthropic_other="n",
    bedrock="anthropic.claude-x",
    bedrock_bad_region="m",
    vertex="claude-x",
    gemini="gemini-2.5",
    codex_session="rc-ws-session",
    claude_session="rc-claude-session",
    call_id="c1",
    tool_use_id="t0",
    ws_call_id="c",
    ws_call_declared=False,
)
# The P2 table's: the forms the plan's sources name (model_ids.py: gpt-4o, gpt-5.2-codex, claude-sonnet-4-5, a dated
# Bedrock InvokeModel id, a Vertex snapshot, gemini-2.5-flash), UUIDs for the session ids, and tool call ids of OpenAI's
# and Anthropic's forms (body_coverage._ID), a Codex call's output after its call.
INPUTS = Inputs(
    openai="gpt-4o",
    codex="gpt-5.2-codex",
    anthropic="claude-sonnet-4-5",
    anthropic_other="claude-haiku-4-5",
    bedrock="anthropic.claude-sonnet-4-5-20250929-v1:0",
    bedrock_bad_region="anthropic.claude-sonnet-4-5-20250929-v1:0",
    vertex="claude-sonnet-4-5@20250929",
    gemini="gemini-2.5-flash",
    codex_session="019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a10",
    claude_session="3d7f2a91-5c4e-4b8d-a1f6-0e9c8b7a6d52",
    call_id="call_rc1",
    tool_use_id="toolu_rc1",
    ws_call_id="call_rc2",
    ws_call_declared=True,
)


def chat(
    text: str,
    *,
    model: str = INPUTS.openai,
    stream: bool = False,
    extra: dict[str, Any] | None = None,
    tool: str | None = None,
    call_id: str = INPUTS.call_id,
) -> bytes:
    messages: list[dict[str, Any]] = [{"role": "user", "content": text}]
    if tool is not None:
        call = {"id": call_id, "type": "function", "function": {"name": "query", "arguments": "{}"}}
        messages += [
            {"role": "assistant", "content": None, "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call_id, "content": tool},
        ]
    return json.dumps(
        {"model": model, "messages": messages, **({"stream": True} if stream else {}), **(extra or {})}
    ).encode()


def responses(text: str, *, model: str = INPUTS.openai, stream: bool = False) -> bytes:
    item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
    return json.dumps({"model": model, "input": [item], **({"stream": True} if stream else {})}).encode()


def anthropic(
    text: str,
    *,
    model: str = INPUTS.anthropic,
    stream: bool = False,
    tool_result: str | None = None,
    tool_use_id: str = INPUTS.tool_use_id,
) -> bytes:
    messages: list[dict[str, Any]] = [{"role": "user", "content": text}]
    if tool_result is not None:
        messages += [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": {"command": "ls"}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": tool_result}],
            },
        ]
    return json.dumps(
        {"model": model, "max_tokens": 10, "messages": messages, **({"stream": True} if stream else {})}
    ).encode()


def bedrock(text: str) -> bytes:
    return json.dumps(
        {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 10, "messages": [{"role": "user", "content": text}]}
    ).encode()


def vertex(text: str, *, stream: bool = False) -> bytes:
    """A Claude call over Vertex, as Claude Code makes it (``anthropic_version`` Vertex's)."""
    body: dict[str, Any] = {"anthropic_version": "vertex-2023-10-16", "max_tokens": 10}
    body |= {"messages": [{"role": "user", "content": text}]} | ({"stream": True} if stream else {})
    return json.dumps(body).encode()


def gemini(text: str) -> bytes:
    return json.dumps({"contents": [{"role": "user", "parts": [{"text": text}]}]}).encode()


def turn(text: str, *, model: str = INPUTS.codex, **extra: Any) -> str:
    item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
    return json.dumps({"type": "response.create", "model": model, "input": [item], **extra})


def image_chat(model: str = INPUTS.openai) -> bytes:
    return json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what does it say?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG}},
                    ],
                }
            ],
        }
    ).encode()


BEARER = (("authorization", "Bearer sk-rc-test"), ("content-type", "application/json"))
# A WebSocket row's one masked turn, once RC-REC-11 (c) is in.
WS_TURN_FIXED: dict[str, int | tuple[int, ...]] = {"hold": 1, "release": 1, "held": 0}
ANTHROPIC_KEY = (
    ("x-api-key", "sk-ant-rc-test"),
    ("anthropic-version", "2023-06-01"),
    ("content-type", "application/json"),
)
CODEX_WS = INPUTS.codex_ws()  # the generated WebSocket cases' headers where P1-0 observed none
CHAT = "/openai/v1/chat/completions"
CODEX = "/backend-api/codex/responses"


async def _fake_of(cell: Cell, original: str) -> str:
    runtime = await cell.runtime()
    entity = runtime.built.vault.by_original(original)
    return entity.surrogate if entity is not None else "no-fake-drawn"


def _rehydrate_body(original: str) -> Callable[[Cell], Awaitable[bytes]]:
    async def make(cell: Cell) -> bytes:
        fake = await _fake_of(cell, original)
        return json.dumps({"session": "rc-plugin", "items": [f"write to {fake}"], "model": True}).encode()

    return make


def _outbound_body(original: str) -> Callable[[Cell], Awaitable[bytes]]:
    async def make(cell: Cell) -> bytes:
        fake = await _fake_of(cell, original)
        return json.dumps({"session": "rc-plugin", "tool": "web_fetch", "values": [fake]}).encode()

    return make


def _gzip_body(raw: bytes) -> bytes:
    return gzip.compress(raw, mtime=0)


# The table: one row a path of the ledger (P1-0's inventories), on the machines where the path exists.
def _table(inputs: Inputs) -> tuple[Case, ...]:
    """The table, its requests naming ``inputs`` (``INPUTS``, or 0.10's ``INPUTS_0_10``)."""
    codex_ws, claude_code = inputs.codex_ws(), inputs.claude_code()
    return (
        # I1: the app's own paths and methods
        Case("i1-own-path", ("I1",), "GET", "/_latet/no-such", describe="under /_latet only Latet's own routes answer"),
        Case(
            "i1-method-unknown",
            ("I1",),
            "TRACE",
            CHAT,
            BEARER,
            inputs.chat("hello"),
            describe="a method outside _METHODS",
        ),
        Case(
            "i1-host-foreign",
            ("I1", "I2"),
            "POST",
            CHAT,
            BEARER,
            inputs.chat("hello"),
            host="evil.example",
            describe="a Host that is not the app's own",
        ),
        # I2: the server's gate
        Case(
            "i2-no-address",
            ("I2",),
            "POST",
            CHAT,
            BEARER,
            inputs.chat("hello"),
            machines=("server",),
            raw_prefix="/u/nokey",
        ),
        Case(
            "i2-unknown-key",
            ("I2",),
            "POST",
            CHAT,
            BEARER,
            inputs.chat("hello"),
            machines=("server",),
            raw_prefix="/u/" + "A" * 43,
            describe="an address no key has",
        ),
        Case(
            "i2-browser",
            ("I2",),
            "POST",
            CHAT,
            (*BEARER, ("origin", "https://evil.example")),
            inputs.chat("hello"),
            machines=("server",),
        ),
        Case("i2-bridge-not-a-call", ("I2", "C0"), "GET", "/_latet/dashboard", machines=("server",)),
        # I3: routes
        Case("i3-no-route", ("I3",), "POST", "/nowhere/v1/chat/completions", BEARER, inputs.chat("hello")),
        Case(
            "i3-escaped-path",
            ("I3", "I5"),
            "POST",
            "/openai/v1/chat/complet%69ons",
            BEARER,
            inputs.chat(f"write to {EMAIL}"),
            describe="the readable path decides the protocol; the raw path is forwarded",
        ),
        Case(
            "i3-query-kept",
            ("I3", "S1"),
            "POST",
            "/openai/v1/chat/completions?api-version=2024&&x=&api-version=2",
            BEARER,
            inputs.chat(f"write to {EMAIL}"),
            describe="escaped, blank and repeated query occurrences",
        ),
        Case(
            "i3-query-value",
            ("I3", "S1"),
            "POST",
            f"/openai/v1/chat/completions?note={urllib.parse.quote(EMAIL)}",
            BEARER,
            inputs.chat("hello"),
            describe="a value in the query: not read today (P2-1)",
        ),
        Case(
            "i3-destination", ("I3", "I4"), "POST", "/llm/v1/chat/completions", BEARER, inputs.chat(f"write to {EMAIL}")
        ),
        # I4: clouds
        Case(
            "i4-bedrock-invoke",
            ("I4", "H10"),
            "POST",
            f"/bedrock/us-east-1/model/{inputs.bedrock}/invoke",
            (("authorization", "Bearer bedrock-rc"), ("content-type", "application/json")),
            bedrock("hello there"),
        ),
        Case(
            "i4-bedrock-unread-call",
            ("I4",),
            "POST",
            f"/bedrock/us-east-1/model/{inputs.bedrock}/converse",
            (("content-type", "application/json"),),
            bedrock("hello"),
        ),
        Case(
            "i4-bedrock-bad-region",
            ("I4",),
            "POST",
            f"/bedrock/US_EAST/model/{inputs.bedrock_bad_region}/invoke",
            (("content-type", "application/json"),),
            bedrock("hello"),
        ),
        Case(
            "i4-bedrock-profiles",
            ("I4", "H1"),
            "GET",
            "/bedrock/us-east-1/inference-profiles?type=SYSTEM_DEFINED",
            (("authorization", "Bearer bedrock-rc"),),
        ),
        Case(
            "i4-bedrock-profile-unsigned",
            ("I4", "S2"),
            "POST",
            "/bedrock/us-east-1/profile/default/" + "k" * 22 + f"/model/{inputs.bedrock}/invoke",
            (("content-type", "application/json"),),
            bedrock("hello"),
            describe="a profile route whose key is not this Latet's",
        ),
        Case(
            "i4-vertex",
            ("I4", "H10"),
            "POST",
            f"/vertex/v1/projects/p/locations/us-east5/publishers/anthropic/models/{inputs.vertex}:rawPredict",
            (("authorization", "Bearer ya29.rc"), ("content-type", "application/json")),
            bedrock("hello vertex"),
        ),
        Case(
            "i4-vertex-unread",
            ("I4", "H2"),
            "POST",
            "/vertex/v1/projects/p/locations/us-east5/endpoints/e:predict",
            (("content-type", "application/json"),),
            b'{"instances": []}',
        ),
        # The Vertex read path (minors after 0.11, item 13): a project the cell lists (``VERTEX_PROJECT``), checked and
        # sent as it came.
        Case(
            "i4-vertex-read",
            ("I4", "H10"),
            "POST",
            f"/vertex/v1/projects/{VERTEX_PROJECT['value']}/locations/us-east5/publishers/anthropic/models/"
            f"{inputs.vertex}:rawPredict",
            (("authorization", "Bearer ya29.rc"), ("content-type", "application/json")),
            vertex("hello vertex"),
            describe="a Vertex call whose project the cell lists: checked, sent as it came",
        ),
        Case(
            "i4-vertex-stream",
            ("I4", "H10"),
            "POST",
            f"/vertex/v1/projects/{VERTEX_PROJECT['value']}/locations/us-east5/publishers/anthropic/models/"
            f"{inputs.vertex}:streamRawPredict",
            (("authorization", "Bearer ya29.rc"), ("content-type", "application/json")),
            vertex("hello vertex", stream=True),
            describe="the same, streamed",
        ),
        Case(
            "i4-gemini",
            ("I4", "H12"),
            "POST",
            f"/gemini/v1beta/models/{inputs.gemini}:generateContent?key=AIza-rc",
            (("content-type", "application/json"),),
            gemini(f"write to {EMAIL}"),
        ),
        # H0: no mediator (0.10's ``latet serve --passthrough``)
        Case(
            "h0-passthrough",
            ("H0", "S1"),
            "POST",
            CHAT,
            BEARER,
            inputs.chat(f"write to {EMAIL} key {SECRET}"),
            machines=("passthrough",),
        ),
        Case(
            "h0-passthrough-ws",
            ("H0", "W0", "S3", "S4"),
            "GET",
            CODEX,
            codex_ws,
            transport="ws",
            frames=(inputs.turn(f"write to {EMAIL}"), b"\x00\x01"),
            machines=("passthrough",),
        ),
        # H1-H12: preparation
        Case("h1-bodyless", ("H1",), "GET", "/openai/v1/models", (("authorization", "Bearer sk-rc-test"),)),
        Case(
            "h2-no-protocol", ("H2",), "POST", "/openai/v1/files", BEARER, json.dumps({"note": f"to {EMAIL}"}).encode()
        ),
        Case(
            "h2-gemini-unread",
            ("H2",),
            "POST",
            "/gemini/v1beta/files",
            (("content-type", "application/json"),),
            b'{"file": {}}',
            describe="a route that refuses unread bodies, on a computer too",
        ),
        Case("h3-undecodable", ("H3",), "POST", CHAT, (*BEARER, ("content-encoding", "gzip")), b"not gzip at all"),
        Case(
            "h3-unknown-encoding", ("H3",), "POST", CHAT, (*BEARER, ("content-encoding", "br2")), inputs.chat("hello")
        ),
        Case(
            "h3-gzip",
            ("H3", "H12"),
            "POST",
            CHAT,
            (*BEARER, ("content-encoding", "gzip")),
            _gzip_body(inputs.chat(f"write to {EMAIL}")),
            describe="a gzip body, masked and encoded again",
        ),
        Case(
            "h3-too-large",
            ("H3",),
            "POST",
            CHAT,
            (*BEARER, ("content-encoding", "gzip")),
            _gzip_body(inputs.chat("x" * 70_000)),
            setup=("body-limit",),
            describe="a few KiB that decode past max_request_mb (a server's bound, made 64 KiB here; a computer's is "
            "32 MiB since 0.11, far above this body)",
        ),
        Case(
            "h3-malformed-recognized",
            ("H3",),
            "POST",
            CHAT,
            BEARER,
            b'{"model": "' + inputs.openai.encode() + b'", "messages": [',
            describe="malformed JSON on a route Latet reads",
        ),
        Case(
            "h3-malformed-unknown",
            ("H2", "H3"),
            "POST",
            "/openai/v1/batches",
            BEARER,
            b'{"model": "' + inputs.openai.encode() + b'", "x": [',
            describe="malformed JSON on a route Latet does not read",
        ),
        Case(
            "h4-bedrock-not-object",
            ("H4",),
            "POST",
            f"/bedrock/us-east-1/model/{inputs.bedrock}/invoke",
            (("content-type", "application/json"),),
            b"[1, 2]",
        ),
        Case("h4-not-object", ("H4",), "POST", CHAT, BEARER, json.dumps([f"to {EMAIL}"]).encode()),
        Case("h4-empty-object", ("H4", "H12"), "POST", CHAT, BEARER, b"{}"),
        Case("h4-empty-body", ("H1",), "POST", CHAT, BEARER, b""),
        Case(
            "h5-bedrock-count-unread",
            ("H5",),
            "POST",
            f"/bedrock/us-east-1/model/{inputs.bedrock}/count-tokens",
            (("content-type", "application/json"),),
            b'{"input": {"converse": {}}}',
        ),
        Case(
            "h5-bedrock-no-messages",
            ("H5",),
            "POST",
            f"/bedrock/us-east-1/model/{inputs.bedrock}/invoke",
            (("content-type", "application/json"),),
            b'{"prompt": "hi"}',
        ),
        Case("h6-images-blocked", ("H6",), "POST", CHAT, BEARER, inputs.image_chat(), setup=("images-block",)),
        Case(
            "h7-twice-verify-only",
            ("H7",),
            "POST",
            "/anthropic/v1/messages",
            claude_code,
            f'{{"model": "{inputs.anthropic}", "max_tokens": 1, "messages": [{{"role": "user", "content": "hi"}}], '
            f'"model": "{inputs.anthropic_other}"}}'.encode(),
        ),
        Case(
            "h8-unreadable-fields",
            ("H8",),
            "POST",
            "/anthropic/v1/messages",
            ANTHROPIC_KEY,
            json.dumps(
                {
                    "model": inputs.anthropic,
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": [{"type": "mystery", "note": f"to {EMAIL}"}]}],
                }
            ).encode(),
            describe="a block of a kind Latet does not read: refused on a server, sent as it came on a computer",
        ),
        Case(
            "h8-schema-read",
            ("H8", "H12"),
            "POST",
            CHAT,
            BEARER,
            inputs.chat(
                "hello", extra={"response_format": {"type": "json_schema", "json_schema": {"schema": {"x": {"y": 1}}}}}
            ),
            describe="an answer's format with a schema: read on both machines",
        ),
        Case(
            "h9-image",
            ("H9",),
            "POST",
            CHAT,
            BEARER,
            inputs.image_chat(),
            describe="an image, no text recognition here",
        ),
        Case(
            "h9-image-claude-code",
            ("H9", "H10"),
            "POST",
            "/anthropic/v1/messages",
            claude_code,
            json.dumps(
                {
                    "model": inputs.anthropic,
                    "max_tokens": 1,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}
                            ],
                        }
                    ],
                }
            ).encode(),
        ),
        Case(
            "h10-verify-only-clean",
            ("H10",),
            "POST",
            "/anthropic/v1/messages",
            claude_code,
            inputs.anthropic_body("hello there"),
        ),
        Case(
            "h10-verify-only-blocked",
            ("H10",),
            "POST",
            "/anthropic/v1/messages",
            claude_code,
            inputs.anthropic_body(f"my key is {SECRET}"),
        ),
        Case(
            "h11-project-off", ("H11",), "POST", CHAT, BEARER, inputs.chat(f"write to {EMAIL}"), setup=("project-off",)
        ),
        Case(
            "h11-project-off-ws",
            ("H11", "W3"),
            "GET",
            CODEX,
            codex_ws,
            transport="ws",
            frames=(inputs.turn(f"write to {EMAIL}"),),
            setup=("project-off",),
        ),
        Case("h12-masked", ("H12", "I5", "S1"), "POST", CHAT, BEARER, inputs.chat(f"write to {EMAIL} or call {PHONE}")),
        Case("h12-masked-stream", ("H12", "S1"), "POST", CHAT, BEARER, inputs.chat(f"write to {EMAIL}", stream=True)),
        Case(
            "h12-anthropic",
            ("H12",),
            "POST",
            "/anthropic/v1/messages",
            ANTHROPIC_KEY,
            inputs.anthropic_body(f"mail {EMAIL_2}"),
        ),
        Case("h12-responses", ("H12",), "POST", "/openai/v1/responses", BEARER, inputs.responses(f"mail {EMAIL_2}")),
        Case(
            "h12-other-instance",
            ("H12", "I2"),
            "POST",
            CHAT,
            BEARER,
            inputs.chat(f"write to {EMAIL} or call {PHONE}"),
            setup=("other-instance",),
            machines=("server",),
            describe="the conversation's next request on the server's other instance",
        ),
        Case(
            "h12-duplicate-key",
            ("H12",),
            "POST",
            CHAT,
            BEARER,
            b'{"model": "' + inputs.openai.encode() + b'", "messages": [{"role": "user", "content": "hi"}], '
            b'"messages": [{"role": "user", "content": '
            b'"write to ' + EMAIL.encode() + b'"}]}',
            describe="a key given twice: read with its last copy",
        ),
        Case(
            "h12-mended",
            ("H12",),
            "POST",
            CHAT,
            BEARER,
            b'{"model": "' + inputs.openai.encode() + b'", "messages": [{"role": "user", "content": "half '
            b"\\ud83d write to " + EMAIL.encode() + b'"}]}',
            describe="half a character: read as U+FFFD, written anew",
        ),
        Case(
            "h12-unknown-field",
            ("H12",),
            "POST",
            CHAT,
            BEARER,
            inputs.chat("hello", extra={"metadata": {"note": EMAIL}}),
            describe="a field no adapter names, holding a value",
        ),
        Case(
            "h12-blocked-card",
            ("H12", "H13"),
            "POST",
            CHAT,
            BEARER,
            inputs.chat("list", tool='{"customer": "c-1", "card": ' + CARD + "}"),
            describe="a card number as a JSON number",
        ),
        Case(
            "h12-header-value",
            ("H12", "H14"),
            "POST",
            CHAT,
            (*BEARER, ("x-note", f"write to {EMAIL}")),
            inputs.chat("hello"),
            describe="a value in a header: not read today (P2-1)",
        ),
        Case(
            "h12-duplicate-headers",
            ("H14", "S1"),
            "POST",
            CHAT,
            (
                ("authorization", "Bearer sk-rc-test"),
                ("x-dup", "one"),
                ("content-type", "application/json"),
                ("X-Dup", "two"),
                ("x-latet-project", "/nowhere"),
                ("accept-encoding", "gzip"),
            ),
            inputs.chat(f"write to {EMAIL}"),
            describe="order, case, duplicates; x-latet-* dropped",
        ),
        # H13: reviews
        Case(
            "h13-local-reviewer",
            ("H13",),
            "POST",
            "/anthropic/v1/messages",
            ANTHROPIC_KEY,
            inputs.anthropic_body("summarize", tool_result=f"the patient is {NAME}"),
            setup=("reviewer",),
        ),
        Case(
            "h13-live-approved",
            ("H13",),
            "POST",
            CHAT,
            BEARER,
            inputs.chat(f"write to {EMAIL}"),
            setup=("live-approve",),
        ),
        Case("h13-live-refused", ("H13",), "POST", CHAT, BEARER, inputs.chat(f"pay to {IBAN}"), setup=("live-refuse",)),
        # H14 and the response path
        Case(
            "h14-upstream-error",
            ("H14", "S1"),
            "POST",
            CHAT,
            BEARER,
            inputs.chat(f"write to {EMAIL}"),
            setup=("upstream-500",),
        ),
        Case(
            "h14-redirect", ("H14", "S1"), "POST", CHAT, BEARER, inputs.chat(f"write to {EMAIL}"), setup=("redirect",)
        ),
        Case(
            "h14-unreachable",
            ("H14", "S1"),
            "POST",
            CHAT,
            BEARER,
            inputs.chat(f"write to {EMAIL}"),
            setup=("unreachable",),
        ),
        Case(
            "s2-expired-token",
            ("S2", "H14"),
            "POST",
            f"/bedrock/us-east-1/profile/default/{{profile_key}}/model/{inputs.bedrock}/invoke",
            (("content-type", "application/json"),),
            bedrock("hello"),
            setup=("aws-expired",),
            machines=("computer",),
            describe="AWS's ExpiredToken: credentials read again, one more build and send",
        ),
        # W0-W3: the WebSocket
        Case(
            "w0-codex",
            ("W0", "W3", "S3", "S4"),
            "GET",
            CODEX,
            codex_ws,
            transport="ws",
            frames=(inputs.turn(f"write to {EMAIL}"),),
            leak="c",
            after_phase_1=WS_TURN_FIXED,
            describe="a masked turn: its hold never released today (RC-REC-11 c, as every masked WebSocket turn)",
        ),
        Case(
            "w0-destination",
            ("W0", "S3"),
            "GET",
            "/llm/v1/responses?x=1&x=2",
            codex_ws,
            transport="ws",
            frames=(inputs.turn("hello"),),
            leak="c",
            after_phase_1=WS_TURN_FIXED,
        ),
        Case(
            "w0-no-route", ("W0", "I3"), "GET", "/nowhere/socket", codex_ws, transport="ws", frames=(inputs.turn("hi"),)
        ),
        Case(
            "w0-refused",
            ("W0", "S3"),
            "GET",
            CODEX,
            codex_ws,
            transport="ws",
            frames=(inputs.turn("hi"),),
            setup=("ws-refuse",),
        ),
        Case(
            "w0-redirect",
            ("W0", "S3"),
            "GET",
            CODEX,
            codex_ws,
            transport="ws",
            frames=(inputs.turn("hi"),),
            setup=("ws-redirect",),
        ),
        Case("w1-binary", ("W1", "S4"), "GET", CODEX, codex_ws, transport="ws", frames=(b"\x00binary",)),
        Case(
            "w2-contentless",
            ("W2", "S4"),
            "GET",
            CODEX,
            codex_ws,
            transport="ws",
            frames=('{"type": "response.cancel"}',),
        ),
        Case(
            "w3-unknown-type",
            ("W3", "S4"),
            "GET",
            CODEX,
            codex_ws,
            transport="ws",
            frames=(json.dumps({"type": "session.update", "note": EMAIL}),),
        ),
        Case(
            "w3-malformed", ("W3", "S4"), "GET", CODEX, codex_ws, transport="ws", frames=('{"type": "response.create',)
        ),
        Case("w3-not-object", ("W3", "S4"), "GET", CODEX, codex_ws, transport="ws", frames=(json.dumps([EMAIL]),)),
        Case(
            "w3-blocked",
            ("W3",),
            "GET",
            CODEX,
            codex_ws,
            transport="ws",
            frames=(
                json.dumps(
                    {
                        "type": "response.create",
                        "model": inputs.codex,
                        "input": inputs.ws_call('{"card": ' + CARD + "}"),
                    }
                ),
            ),
            leak="c",
            after_phase_1=WS_TURN_FIXED,
            describe="a turn masked, then stopped by the check: its hold never released today",
        ),
        Case(
            "w3-header-value",
            ("W0", "S3"),
            "GET",
            CODEX,
            (*codex_ws, ("x-note", f"to {EMAIL}")),
            transport="ws",
            frames=(inputs.turn("hello"),),
            leak="c",
            after_phase_1=WS_TURN_FIXED,
            describe="a value in a handshake header: not read today (P2-1); the turn's hold never released today",
        ),
        # C0: the plugin's bridge
        Case(
            "c0-mask",
            ("C0",),
            "POST",
            "/_latet/engine/v1/mask",
            (("content-type", "application/json"),),
            json.dumps({"session": "rc-plugin", "items": [{"text": f"write to {EMAIL}", "kind": "prompt"}]}).encode(),
            transport="bridge",
            machines=("computer", "server"),
        ),
        Case(
            "c0-rehydrate",
            ("C0",),
            "POST",
            "/_latet/engine/v1/rehydrate",
            (("content-type", "application/json"),),
            _rehydrate_body(EMAIL),
            transport="bridge",
        ),
        Case(
            "c0-verify-blocked",
            ("C0",),
            "POST",
            "/_latet/engine/v1/verify",
            (("content-type", "application/json"),),
            json.dumps({"session": "rc-plugin", "slots": [{"text": f"key {SECRET}"}]}).encode(),
            transport="bridge",
        ),
        Case(
            "c0-outbound",
            ("C0",),
            "POST",
            "/_latet/engine/v1/outbound",
            (("content-type", "application/json"),),
            _outbound_body(EMAIL),
            transport="bridge",
        ),
        Case(
            "c0-route-check",
            ("C0",),
            "POST",
            "/_latet/engine/v1/route-check",
            (("content-type", "application/json"),),
            json.dumps({"session": "rc-plugin", "env": {}, "sources": {}, "entrypoint": "cli"}).encode(),
            transport="bridge",
        ),
        Case(
            "c0-unauthorized",
            ("C0",),
            "POST",
            "/_latet/engine/v1/mask",
            (("content-type", "application/json"),),
            b'{"session": "x", "items": []}',
            transport="bridge",
            setup=("no-token",),
        ),
        # RC-REC-11's own rows (today's counts; the expected ones after Phase 1 beside them); the WebSocket rows above are (c) too
        Case(
            "rc11-a-unstarted-stream",
            ("H12", "Response"),
            "POST",
            CHAT,
            BEARER,
            inputs.chat(f"write to {EMAIL}", stream=True),
            setup=("unstarted",),
            leak="a",
            after_phase_1={"hold": 1, "release": 1, "renewer_running": 0, "held": 0, "unclosed": 0},
            describe="a restored stream whose relay never starts (its client gone before the answer's start): its hold "
            "renewed and never released, the provider's answer never closed",
        ),
        Case(
            "rc11-b-cancel-masking",
            ("H12",),
            "POST",
            CHAT,
            BEARER,
            inputs.chat(f"write to {EMAIL_2}"),
            setup=("cancel-masking",),
            leak="b",
            after_phase_1={"hold": 1, "release": 1, "held": 0},
            describe="cancelled while the executor masks: the hold the masking took has no owner",
        ),
        Case(
            "rc11-b-cancel-live",
            ("H13",),
            "POST",
            CHAT,
            BEARER,
            inputs.chat(f"call {PHONE}"),
            setup=("cancel-live",),
            leak="b",
            # One release (a lease that keeps the first handle until the executor ends: the reacquire finds it held and
            # hands it back) or two (handle's finally lets the first go, the lease the reacquired one): either design
            # releases each handle once, which the server's vault shows (held 0).
            after_phase_1={"hold": 1, "reacquire": 1, "release": (1, 2), "held": 0},
            describe="cancelled while the executor applies the Live Reviewer's decision: handle's finally released the "
            "first hold, the reacquire takes one no one releases",
        ),
        Case(
            "rc11-c-ws-turns",
            ("W3", "S4"),
            "GET",
            CODEX,
            codex_ws,
            transport="ws",
            frames=(inputs.turn(f"write to {EMAIL}"), inputs.turn(f"and {EMAIL_2}")),
            leak="c",
            after_phase_1={"hold": 2, "release": 2, "held": 0},
            describe="WebSocket turns: no release anywhere",
        ),
    )


TABLE = _table(INPUTS)
# Rows added after 0.11 was released (minors after 0.11, item 13: the Vertex read path), characterized by the 0.11
# baseline alone: 0.10's table never had them, so its golden, Phase 1's evidence, holds none.
ADDED_AFTER_0_11 = frozenset({"i4-vertex-read", "i4-vertex-stream"})
TABLE_0_10 = tuple(case for case in _table(INPUTS_0_10) if case.id not in ADDED_AFTER_0_11)
# RC-REC-11's rows are every row marked ``leak`` (a, b, and c: every table row with a masked WebSocket turn), and in
# the generated cells every WebSocket case (``phase_1_judge``): the only observations Phase 1 changes.
TABLE_IDS = tuple(case.id for case in TABLE)
assert len(set(TABLE_IDS)) == len(TABLE_IDS)


def table_for(machine: str) -> list[Case]:
    return [case for case in TABLE if machine in case.machines]


def phase_1_differences(case: Case, observation: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Where a leak row's counts differ from those it is expected to hold after Phase 1: ``{count: (now, expected)}``
    (an expected tuple: any of its values; ``held`` judged where the vault holds, on a server)."""
    counts = leak_counts(observation)
    found = {}
    for name, want in (case.after_phase_1 or {}).items():
        now = counts.get(name)
        if name == "held" and now is None:
            continue
        if (now not in want) if isinstance(want, tuple) else (now != want):
            found[name] = (now, want)
    return found


# What each leak's fix may change of an observation (P1-4); every other part of it stays as the golden has it.
PHASE_1_MAY_CHANGE: dict[str, tuple[tuple[str, ...], ...]] = {
    "a": (
        ("engine", "release"),
        ("engine", "renew"),
        ("engine", "vault.renew.timer"),
        ("engine", "renewers"),
        ("held",),
        ("answers",),
    ),
    "b": (("engine", "release"), ("held",)),
    "c": (("engine", "release"), ("held",)),
}


def _without(observation: dict[str, Any], paths: Sequence[tuple[str, ...]]) -> dict[str, Any]:
    out = json.loads(json.dumps(observation))
    for path in paths:
        found = out
        for key in path[:-1]:
            found = found.get(key, {}) if isinstance(found, dict) else {}
        if isinstance(found, dict):
            found.pop(path[-1], None)
    return out


def first_difference(a: Any, b: Any, path: str = "$") -> str | None:
    """Where two observations first differ (a JSON path and the two values, cut), or None."""
    if type(a) is not type(b):
        return f"{path}: {json.dumps(a)[:160]} != {json.dumps(b)[:160]}"
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{path}.{key}: on one side only"
            if (found := first_difference(a[key], b[key], f"{path}.{key}")) is not None:
                return found
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: {len(a)} items != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            if (found := first_difference(x, y, f"{path}[{i}]")) is not None:
                return found
        return None
    return None if a == b else f"{path}: {json.dumps(a)[:160]} != {json.dumps(b)[:160]}"


def phase_1_judge(
    observation: Any,
    golden: Any,
    *,
    case: Case | None = None,
    cell: str | None = None,
    fixes_in: bool | None = None,
) -> str | None:
    """Why ``observation`` is not what the golden allows, or None: the one rule of P1-1's comparisons (the test modules'
    and ``replay``'s). Before RC-REC-11's fixes (``fixes_in``, by default ``LEAK_FIXES_IN``) every observation equals
    the golden's. Once they are in: a leak row of the table (``case.leak``) equals it but for what its fix may change
    (``PHASE_1_MAY_CHANGE``) and holds its ``after_phase_1`` counts; a generated WebSocket case (``cell`` ``*-ws``)
    equals it but for its releases and holds, and releases each hold once (at least its holds, at most its holds and
    reacquires); every other observation stays exact."""
    fixed = LEAK_FIXES_IN if fixes_in is None else fixes_in
    leak = case.leak if case is not None else "c" if cell is not None and cell.endswith("-ws") else None
    if not fixed or leak is None or not isinstance(observation, dict) or not isinstance(golden, dict):
        return first_difference(observation, golden)
    paths = PHASE_1_MAY_CHANGE[leak]
    if (found := first_difference(_without(observation, paths), _without(golden, paths))) is not None:
        return found
    if case is not None:
        missed = phase_1_differences(case, observation)
        return f"after Phase 1: {missed}" if missed else None
    engine = observation.get("engine", {})
    hold, reacquire, release = (engine.get(k, 0) for k in ("hold", "reacquire", "release"))
    if not hold <= release <= hold + reacquire:
        return f"after Phase 1: {release} releases for {hold} holds and {reacquire} reacquires"
    return None


def baseline_judge(observation: Any, baseline: Any, **_: Any) -> str | None:
    """Why ``observation`` is not the baseline's, or None: Phase 2's rule, exact for every observation, a leak row's
    and a generated WebSocket case's included (the baseline was recorded with RC-REC-11's fixes in: no count may move)."""
    return first_difference(observation, baseline)


def judge_of(golden: dict[str, Any]) -> Callable[..., str | None]:
    """The rule a golden is compared by: the baseline's own (``rule``: ``exact``), else Phase 1's (0.10's golden)."""
    return baseline_judge if golden.get("rule") == "exact" else phase_1_judge


# --- running a case ------------------------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def setup_for(cell: Cell, case: Case, runtime: Any) -> AsyncIterator[dict[str, Any]]:
    """What ``case.setup`` changes around the case (each restored after it), and what the runner needs of it."""
    mediator: Mediator | None = runtime.mediator if runtime is not None else None
    found: dict[str, Any] = {}
    with contextlib.ExitStack() as stack:
        for name in case.setup:
            if name == "images-block" and mediator is not None:
                stack.enter_context(patched(mediator, "images", ImagePolicy("block", "setting")))
            elif name == "project-off" and mediator is not None:
                engine = mediator.engine
                off = Scope("rc-off", "rc-off", engine.masker, enabled=False)
                stack.enter_context(patched(engine, "scope", lambda session, off=off: off))
            elif name == "body-limit" and mediator is not None and cell.machine == "server":
                stack.enter_context(patched(mediator, "body_limit", 64 * 1024))
            elif name == "reviewer" and mediator is not None:
                stack.enter_context(_local_reviewer(mediator))
            elif name in ("live-approve", "live-refuse", "cancel-live") and mediator is not None:
                approved = name != "live-refuse"
                decision = Decision(approved, reason=None if approved else "you refused it")

                async def decided(batch: Any, gone: Any, held: Any = None, decision: Decision = decision) -> Decision:
                    return decision

                stack.enter_context(patched(mediator, "live_review", LiveReview(enabled=True)))
                stack.enter_context(patched(mediator, "await_decision", decided))
            elif name == "upstream-500":
                cell.provider.override = lambda request: (
                    500,
                    {"content-type": "application/json", "x-request-id": "rc"},
                    b'{"error": {"message": "overloaded"}}',
                )
            elif name == "redirect":
                cell.provider.override = lambda request: (302, {"location": "https://elsewhere.example/v1"}, b"")
            elif name == "unreachable":
                cell.transport.failure = httpx.ConnectError("no route to the provider")
            elif name == "ws-refuse":
                cell.ws.refuse = 403
            elif name == "ws-redirect":
                cell.ws.refuse, cell.ws.location = 302, "ws://elsewhere.example/socket"
            elif name == "aws-expired" and runtime is not None:
                found["profile_key"] = stack.enter_context(_aws_expired(runtime))
        try:
            yield found
        finally:
            cell.provider.override = None
            cell.transport.failure = None
            cell.ws.refuse, cell.ws.location = None, None


@contextlib.contextmanager
def _local_reviewer(mediator: Mediator) -> Iterator[None]:
    """The local reviewer on, against an answer of its own (no server: its endpoint is not a provider send): it finds
    ``NAME`` where it reads it."""
    from latet.engine.reviewer import ReviewClient, Reviewer

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(("/props", "/models")):
            return httpx.Response(404)
        text = json.loads(request.content)["messages"][1]["content"]
        findings = [{"text": NAME, "category": "person"}] if NAME in text else []
        chunk = {"choices": [{"delta": {"content": json.dumps({"findings": findings, "block": False})}}]}
        stream = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    config = LlmReview(enabled=True, model="rc-model")
    client = ReviewClient(config.url, config.model, None, transport=httpx.MockTransport(answer))
    with (
        patched(mediator, "llm_review", config),
        patched(mediator, "reviewer", Reviewer(config, b"r" * 32, client=client)),
    ):
        yield


@contextlib.contextmanager
def _aws_expired(runtime: Any) -> Iterator[str]:
    """The computer's AWS profile ``default`` whose first credentials Amazon finds stale (``FakeUpstream`` answers
    ExpiredToken to them), fresh once read again: the profile route's key."""
    from latet.gateway.aws import profile_tag
    from latet.gateway.sigv4 import AwsCredentials

    aws = runtime.gateway.aws
    reads = [0]

    async def profile(name: str) -> AwsCredentials:
        reads[0] += 1
        return AwsCredentials("AKIARCTEST", "rc-secret", "expired-token" if reads[0] == 1 else "fresh-token", None)

    with patched(aws, "profile", profile), patched(aws, "invalidate", lambda name: None):
        yield profile_tag(runtime.gateway.bridge_token or "", "default")


def _headers(cell: Cell, case: Case, host: str | None = None) -> list[tuple[bytes, bytes]]:
    named = [(b"host", (host or case.host or cell.host).encode())]
    named += [(k.encode("latin-1"), v.encode("latin-1")) for k, v in case.headers]
    if case.transport == "bridge" and "no-token" not in case.setup and cell.bridge_header is not None:
        named.append(cell.bridge_header)
    return named


def _relay_settled(cell: Cell, runtime: Any) -> Callable[[], Awaitable[None]] | None:
    """What a WebSocket client that stays until its last turn is recorded waits for: the relay's next steps on the loop,
    then the engine's thread (one worker: what the relay gave it, a turn's screening, done first), twice over. None
    with no mediator."""
    if runtime is None:
        return None
    executor = runtime.mediator.executor

    async def settle() -> None:
        for _ in range(2):
            await cell.clock.settle()
            await asyncio.get_running_loop().run_in_executor(executor, lambda: None)
        await cell.clock.settle()

    return settle


async def _drain(cell: Cell, runtime: Any) -> None:
    await cell.clock.settle()
    if runtime is not None:
        await asyncio.to_thread(lambda: None)
        recorder = runtime.gateway.recorder
        if recorder is not None and recorder.enabled:
            await asyncio.to_thread(recorder.drain, 10)
        executor = runtime.mediator.executor
        await asyncio.get_running_loop().run_in_executor(executor, lambda: None)  # what ``left`` queued, done


async def run_case(cell: Cell, case: Case) -> dict[str, Any]:
    """The case through the cell, and its observation (no value)."""
    other = "other-instance" in case.setup and cell.other_app is not None
    runtime = await cell.runtime(other) if cell.runtime_of is not None else None
    cell.transport.clear()
    cell.ws.clear()
    cell.provider.answers.clear()
    recorder = runtime.gateway.recorder if runtime is not None else None
    records: list[Any] = getattr(recorder, "_rc_records", []) if recorder is not None else []
    records.clear()
    held_before = _held(runtime)
    cell.seam.counts.clear()
    builds0, sends0 = cell.clients.counts()
    left = Counter()
    mediator = runtime.mediator if runtime is not None else None
    stack = contextlib.ExitStack()
    if mediator is not None:
        real_left = mediator.left

        def counted_left(prepared: Any) -> None:
            left["left"] += 1
            real_left(prepared)

        stack.enter_context(patched(mediator, "left", counted_left))
    view = ClientView()
    inbound_body = b""
    inbound_headers: list[tuple[bytes, bytes]] = []
    target = case.target
    with stack, logged() as logs:
        async with setup_for(cell, case, runtime) as found:
            if "{profile_key}" in target:
                target = target.replace("{profile_key}", found.get("profile_key", "k" * 22))
            body = case.body if isinstance(case.body, bytes) else await case.body(cell)
            inbound_body = body
            prefix = case.raw_prefix if case.raw_prefix is not None and cell.machine == "server" else cell.prefix
            raw_target = (prefix + target).encode("latin-1")
            inbound_headers = _headers(cell, case)
            app = cell.other_app if other else cell.app
            if case.leak in ("a", "b") and cell.machine != "passthrough":
                view = await _leak_row(cell, case, runtime, raw_target, inbound_headers, body)
            elif case.transport == "ws":
                view = await ws_call(
                    app,
                    raw_target,
                    inbound_headers,
                    case.frames,
                    cell.client,
                    cell.server,
                    "wss" if cell.scheme == "https" else "ws",
                    settle=None if "leaves-at-once" in case.setup else _relay_settled(cell, runtime),
                )
            else:
                view = await http_call(
                    app, case.method, raw_target, inbound_headers, body, cell.client, cell.server, cell.scheme
                )
            await _drain(cell, runtime)
            for _ in range(2):  # the renewers that still run renew once a tick
                await cell.clock.tick()
            states = cell.clock.states()
            await cell.clock.stop()
            await _drain(cell, runtime)
    builds, sends = cell.clients.counts()
    fakes = _fakes(runtime)
    canon = Canon(fakes, cell.secrets)
    inbound_query = raw_target.partition(b"?")[2]
    observation: dict[str, Any] = {"case": case.id, "machine": cell.machine}
    upstream = []
    for sent in cell.transport.sent:
        upstream.append(
            {
                "method": sent.method,
                "host": canon.text(sent.host if "127.0.0.1" not in sent.host else "<local>"),
                "target": target_view(sent.raw_path, inbound_query, canon),
                "headers": header_view(sent.headers, inbound_headers, canon, sent.content),
                "body": sent_body_view(sent, canon, inbound_body),
            }
        )
    observation["upstream"] = upstream
    observation["sockets"] = [
        {
            "target": target_view(
                shake.raw_path + (b"?" + shake.query_string if shake.query_string else b""), inbound_query, canon
            ),
            "headers": header_view(shake.headers, inbound_headers, canon, None),
            "accepted": shake.accepted,
            "frames": [frame_view(frame, canon) for frame in shake.frames],
            "as_sent": [_as_sent(frame, case.frames) for frame in shake.frames],
        }
        for shake in cell.ws.handshakes
    ]
    observation["counts"] = {
        "build": builds - builds0,
        "send": sends - sends0,
        "connect": len(cell.ws.handshakes),
        "frames_up": sum(len(s.frames) for s in cell.ws.handshakes),
    }
    client_view: dict[str, Any] = {"status": view.status, "error": view.error}
    if case.transport == "ws":
        client_view |= {
            "accepted": view.accepted,
            # A plain number: 0.11 closes with websockets' CloseCode, an int of another type than the baseline's JSON.
            "close": [int(view.close[0]), view.close[1]] if view.close else None,
            "frames": [frame_view(frame, canon) for frame in view.frames],
        }
        if view.status is not None:
            client_view["denial"] = words_of(view.body, canon)
    else:
        client_view["body"] = words_of(view.body, canon) if view.body else None
    upstream_answer = [(k, v) for k, v in view.headers]
    client_view["headers"] = header_view(upstream_answer, [], canon, view.body if case.transport != "ws" else None)
    observation["client"] = client_view
    engine: dict[str, Any] = dict(sorted(cell.seam.counts.items()))
    engine["renewers"] = states
    observation["engine"] = engine
    observation["left"] = left["left"]
    answers = cell.provider.answers
    observation["answers"] = {"made": len(answers), "closed": sum(stream.closed for stream in answers)}
    held_after = _held(runtime)
    # The conversations a holding vault holds more after the case than before it (None: a vault that holds nothing).
    observation["held"] = None if held_after is None or held_before is None else held_after - held_before
    observation["records"] = [record_view(r, canon) for r in records]
    observation["logs"] = [
        [name, level, canon.text(template), log_args(args, canon)] for name, level, template, args in logs.lines
    ]
    return observation


def _as_sent(frame: str | bytes, given: Sequence[str | bytes]) -> int:
    """Which of the client's frames this one is, as it was sent (its place), or -1: written anew."""
    return next((i for i, mine in enumerate(given) if mine == frame), -1)


def _fakes(runtime: Any) -> list[str]:
    if runtime is None:
        return []
    vault = runtime.built.vault
    found = []
    for original in ORIGINALS:
        entity = vault.by_original(original)
        if entity is not None:
            found.append(entity.surrogate)
    with contextlib.suppress(Exception):
        found += [e.surrogate for e in vault.entities()]
    return found


def _held(runtime: Any) -> int | None:
    """How many holds a vault that holds (``StoredVault``) counts now, every conversation's together; None for a vault
    that holds nothing (a computer's)."""
    if runtime is None:
        return None
    sessions = getattr(runtime.built.vault, "_sessions", None)
    return None if sessions is None else sum(getattr(held, "count", 1) for held in list(sessions.values()))


def _request(cell: Cell, raw_target: bytes, headers: list[tuple[bytes, bytes]], body: bytes) -> Request:
    raw_path, query, path = _split(raw_target)
    if cell.machine == "server":  # the space's app sees the address without the key, as the gate hands it on
        raw_path = raw_path[len(cell.prefix) :] or b"/"
        path = urllib.parse.unquote(raw_path.decode("latin-1"))
    sent = [False]

    async def receive() -> dict[str, Any]:
        if not sent[0]:
            sent[0] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "POST",
        "scheme": cell.scheme,
        "path": path,
        "raw_path": raw_path,
        "query_string": query,
        "headers": headers,
        "server": cell.server,
        "client": cell.client,
    }
    return Request(scope, receive)


async def _leak_row(
    cell: Cell, case: Case, runtime: Any, raw_target: bytes, headers: list[tuple[bytes, bytes]], body: bytes
) -> ClientView:
    """RC-REC-11's rows (a) and (b): ``Forwarder.handle`` over the cell's mediator, recorder and recorded client, as
    the app's forwarder is (``tests/server/test_conversation_identity.py`` does the same), so that the answer can be
    dropped unstarted, or the request cancelled while its executor step runs. Where the row's precondition does not
    hold (the request refused before any stream is answered, or before the step it is cancelled in is reached), what
    the client is answered is recorded, as for any other row (``_answered``)."""
    mediator = runtime.mediator
    gateway = runtime.gateway
    client = cell.clients()
    async with client:
        forwarder = Forwarder(gateway.routes, Capture(None), client, mediator, gateway.recorder, gateway.aws)
        request = _request(cell, raw_target, headers, body)
        view = ClientView()
        if "unstarted" in case.setup:
            # The answer handed to a client gone before it starts: its ASGI call fails at the response's start (the
            # server's send raises, as uvicorn's does once the connection is closed), so its body is never iterated.
            # Then dropped, as a finished request's response is (an unstarted relay's finally never runs).
            response = await forwarder.handle(request)
            if not isinstance(response, StreamingResponse):  # refused, or answered whole: no relay to leave unstarted
                return await _answered(response, request)
            view.status = response.status_code
            view.headers = list(response.raw_headers)

            async def gone() -> dict[str, Any]:
                return {"type": "http.disconnect"}

            async def closed(message: dict[str, Any]) -> None:
                raise OSError("the client is gone")

            scope = dict(request.scope, asgi={"version": "3.0", "spec_version": "2.4"})
            try:
                await response(scope, gone, cast(Any, closed))
            except Exception as exc:  # what the server makes of it: the client went away
                view.error = type(exc).__name__
            iterator = response.body_iterator
            if isinstance(iterator, AsyncGenerator):
                await iterator.aclose()
            return view
        gate = threading.Event()
        entered = threading.Event()
        if "cancel-masking" in case.setup:
            prepare = mediator.prepare

            def held_prepare(*args: Any, **kwargs: Any) -> Any:
                found = prepare(*args, **kwargs)  # masked: its conversation's hold taken
                entered.set()
                gate.wait(10)
                return found

            patch = patched(mediator, "prepare", held_prepare)
        else:  # cancel-live: the decision's application waits before its reacquire
            engine = mediator.engine
            reacquire = engine.reacquire

            def held_reacquire(*args: Any, **kwargs: Any) -> Any:
                entered.set()
                gate.wait(10)
                return reacquire(*args, **kwargs)

            patch = patched(engine, "reacquire", held_reacquire)
        with patch:
            task = asyncio.create_task(forwarder.handle(request))
            reached = asyncio.ensure_future(asyncio.to_thread(entered.wait, 10))
            await asyncio.wait({task, reached}, return_when=asyncio.FIRST_COMPLETED)
            if entered.is_set():  # the step is running: the request cancelled there
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                view.error = "CancelledError"
            else:  # answered before the step was reached (a refusal): nothing to cancel
                entered.set()
                view = await _answered(await task, request)
            gate.set()
            await reached
            await asyncio.get_running_loop().run_in_executor(mediator.executor, lambda: None)
        return view


async def _answered(response: Any, request: Request) -> ClientView:
    """What a client that stays is answered with ``response``: its start and its whole body, as for any other row."""
    view = ClientView()
    finished = asyncio.Event()

    async def receive() -> dict[str, Any]:
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            view.status = message["status"]
            view.headers = [(bytes(k), bytes(v)) for k, v in message.get("headers", [])]
        elif message["type"] == "http.response.body":
            view.body += message.get("body", b"")
            if not message.get("more_body"):
                finished.set()

    scope = dict(request.scope, asgi={"version": "3.0", "spec_version": "2.3"})
    try:
        await response(scope, receive, send)
    except Exception as exc:
        view.error = type(exc).__name__
    finally:
        finished.set()
    return view


def leak_counts(observation: dict[str, Any]) -> dict[str, int | None]:
    """The counts RC-REC-11's rows are judged by: holds, reacquires and releases at the engine seam, the renewers still
    running after the ticks, the holds a server's vault counts more after the row (None on a computer, whose vault
    holds nothing), and the provider's answers the gateway left open."""
    engine = observation["engine"]
    answers = observation.get("answers") or {"made": 0, "closed": 0}
    return {
        "hold": engine.get("hold", 0),
        "reacquire": engine.get("reacquire", 0),
        "release": engine.get("release", 0),
        "renewer_running": engine["renewers"].count("running"),
        "held": observation["held"],
        "unclosed": answers["made"] - answers["closed"],
    }


# --- generated cases -----------------------------------------------------------------------------------------------

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")  # gateway/app.py:_METHODS, checked by a test
_FILL = {
    "region": ("us-east-1", "eu-west-3", "us-east-1x", "US-EAST-1"),
    # A model id of each provider's grammar (0.10 drew placeholders, which 0.11 refuses before the rest is read):
    # Bedrock's, a cross-Region one with its colon escaped, Gemini's, a Vertex snapshot.
    "model": (
        "anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.anthropic.claude-haiku-4-5-20251001-v1%3A0",
        "gemini-2.5-flash",
        "claude-sonnet-4-5@20250929",
    ),
    "destination": ("llm",),
    "segment": ("v2", "files", "batches", "a%2Fb", "x"),
    "profile": ("default", "dev"),
    "profile_key": ("k" * 22,),
    "profile_id": ("us.anthropic.claude-sonnet-4-5-20250929-v1:0",),
    "session": ("s" * 22,),
    "launch_key": ("probe0",),
    "project": ("p-rc",),
    "location": ("us-east5", "global", "europe-west1", "moon-1"),
}
_EXTRA_TEMPLATES = (
    ("http", "GET", "/openai/v1/models", ()),
    ("http", "POST", "/openrouter/api/v1/chat/completions", ()),
    ("http", "POST", "/llm/v1/chat/completions", ()),
    ("http", "POST", "/backend-api/codex/responses", ()),
    ("http", "DELETE", "/openai/v1/responses/{segment}", ()),
)
_WS_TEMPLATES = ("/backend-api/codex/responses", "/llm/v1/responses", "/backend-api/codex/responses")
_QUERY_NAMES = ("beta", "alt", "key", "type", "api-version", "stream", "note")
_TEXTS = (
    "write to {EMAIL}",
    "call {PHONE} about the invoice",
    "my key is {SECRET}",
    "pay to {IBAN}",
    "hello there",
    "forward it to {EMAIL_2} and {EMAIL}",
    "nothing to see",
    "card {CARD} expires soon",
    "",
)
_VALUES = {"EMAIL": EMAIL, "EMAIL_2": EMAIL_2, "SECRET": SECRET, "PHONE": PHONE, "IBAN": IBAN, "CARD": CARD}
_HEADER_VALUES = {
    "authorization": "Bearer sk-rc-test",
    "x-api-key": "sk-ant-rc-test",
    "content-type": "application/json",
    "anthropic-version": "2023-06-01",
    "user-agent": "rc-agent/1.0",
    "accept": "application/json",
    "accept-encoding": "gzip",
    # The agents' own headers by the grammars 0.11 reads them with (request_profiles.py: the agents' headers are never
    # refused), as 0.10 read none: their session and thread ids UUIDs (n from 0 to 6), the enumerated values, Codex's
    # originator, a beta feature key and a window id.
    "session-id": "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a2{n}",
    "x-claude-code-session-id": "3d7f2a91-5c4e-4b8d-a1f6-0e9c8b7a6d6{n}",
    "thread-id": "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a3{n}",
    "x-client-request-id": "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a3{n}",
    "x-codex-window-id": "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a3{n}:1",
    "x-codex-beta-features": "unified_exec",
    "originator": "codex_cli_rs",
    "x-app": "cli",
    "x-initiator": "user",
    "anthropic-dangerous-direct-browser-access": "true",
    "sec-fetch-mode": "cors",
    "content-encoding": None,  # the body's coding is the generator's: gzip, with a gzip body
    "x-stainless-lang": "js",
    "host": None,
    "content-length": None,
    "connection": None,
}


def _observed() -> tuple[list[tuple[str, str, str, tuple[str, ...]]], dict[str, list[str]]]:
    """P1-0's observed inbound templates (method, raw path template, query names) and the header names seen on each."""
    doc = json.loads(OBSERVED_CALLS.read_text("utf-8"))
    templates: dict[tuple[str, str, str, tuple[str, ...]], None] = {}
    headers: dict[str, list[str]] = {}
    for call in doc["calls"]:
        if call["side"] != "inbound":
            continue
        key = (call["transport"], call["method"], call["path_template"], tuple(call["query_names"]))
        templates[key] = None
        names = headers.setdefault(call["transport"] + " " + call["path_template"], [])
        for name in call["header_names"]:
            if name not in names:
                names.append(name)
    return list(templates), headers


def _text(rng: random.Random) -> str:
    return rng.choice(_TEXTS).format(**_VALUES)


def _fill(template: str, rng: random.Random) -> str:
    out = template
    for name, choices in _FILL.items():
        out = out.replace("{" + name + "}", rng.choice(choices))
    return out


def _escape(path: str, rng: random.Random) -> str:
    """One letter of the path written as its percent-escape, now and then."""
    if rng.random() >= 0.15:
        return path
    letters = [i for i, c in enumerate(path) if c.isalpha()]
    if not letters:
        return path
    i = rng.choice(letters)
    return path[:i] + "%" + format(ord(path[i]), "02X") + path[i + 1 :]


def _query(names: Sequence[str], rng: random.Random) -> str:
    parts = [f"{name}={rng.choice(['true', 'sse', 'AIza-rc', 'SYSTEM_DEFINED', '1'])}" for name in names]
    for _ in range(rng.choice((0, 0, 0, 1, 2))):
        name = rng.choice(_QUERY_NAMES)
        value = rng.choice(["", "x", urllib.parse.quote(EMAIL), "a%20b", "1"])
        parts.append(name if rng.random() < 0.1 else f"{name}={value}")
    if parts and rng.random() < 0.1:
        parts.insert(rng.randrange(len(parts) + 1), "")  # a blank occurrence
    if parts and rng.random() < 0.1:
        parts.append(rng.choice(parts))  # repeated
    return "?" + "&".join(parts) if parts or rng.random() < 0.05 else ""


def _headers_for(names: Sequence[str], n: int, rng: random.Random) -> tuple[tuple[str, str], ...]:
    out = []
    for name in names:
        if name in ("host", "content-length", "connection") or name.startswith("x-latet"):
            continue
        if name.startswith(("x-fake-", "x-rc-")):  # what P1-0's own fakes and probe sent, never an agent
            continue
        value = _HEADER_VALUES.get(name, "rc-{n}")
        if value is None:
            continue
        out.append((name, value.format(n=n % 7)))
    if not out:
        out = [("authorization", "Bearer sk-rc-test"), ("content-type", "application/json")]
    if rng.random() < 0.1:
        name, value = rng.choice(out)
        out.insert(rng.randrange(len(out) + 1), (name.upper() if rng.random() < 0.5 else name, value + "-2"))
    if rng.random() < 0.08:
        out.append(("x-note", _text(rng)))
    if rng.random() < 0.04:
        out.append(("x-latet-project", "/nowhere"))
    return tuple(out)


def _body_for(path: str, rng: random.Random) -> tuple[bytes, list[str]]:
    """A body for ``path``'s family, mutated: (bytes, the mutations' names)."""
    text = _text(rng)
    stream = rng.random() < 0.3
    if "/v1/messages" in path or "rawPredict" in path:
        doc: Any = json.loads(anthropic(text, stream=stream and "rawPredict" not in path))
    elif "/invoke" in path or "count-tokens" in path:
        doc = json.loads(bedrock(text))
        if "count-tokens" in path:
            import base64

            doc = {"input": {"invokeModel": {"body": base64.b64encode(json.dumps(doc).encode()).decode()}}}
    elif "generateContent" in path or "countTokens" in path or "embedContent" in path:
        doc = json.loads(gemini(text))
    elif "responses" in path:
        doc = json.loads(responses(text, stream=stream))
    else:
        doc = json.loads(chat(text, stream=stream))
    mutations: list[str] = []
    roll = rng.random()
    if roll < 0.04:
        return b"", ["empty"]
    if roll < 0.07:
        return json.dumps(rng.choice([[text], text, 3, None])).encode(), ["not-object"]
    if roll < 0.10:
        raw = json.dumps(doc).encode()
        return raw[: max(1, len(raw) // 2)], ["malformed"]
    if isinstance(doc, dict) and rng.random() < 0.25:
        name = rng.choice(["metadata", "user", "store", "tools", "instructions", "service_tier", "x_rc"])
        doc[name] = rng.choice([{"note": _text(rng)}, _text(rng), 1, True, [_text(rng)]])
        mutations.append("added:" + name)
    if isinstance(doc, dict) and rng.random() < 0.1 and doc:
        name = rng.choice(sorted(doc))
        del doc[name]
        mutations.append("removed:" + name)
    raw = json.dumps(doc).encode()
    if rng.random() < 0.05 and raw.startswith(b"{"):
        # A key given twice: its first copy the body's own model, read by its grammar like the second.
        model = doc.get("model", "dup") if isinstance(doc, dict) else "dup"
        raw = b'{"model": ' + json.dumps(model).encode() + b", " + raw[1:]
        mutations.append("duplicate-key")
    if rng.random() < 0.03:
        raw = raw.replace(b"write to", b"write \\ud83d to", 1)
        mutations.append("mended")
    if rng.random() < 0.03:
        mutations.append("gzip")
        raw = _gzip_body(raw)
    return raw, mutations


def _frames_for(rng: random.Random) -> tuple[tuple[str | bytes, ...], list[str]]:
    frames: list[str | bytes] = []
    kinds: list[str] = []
    for _ in range(rng.choice((1, 1, 2, 3))):
        roll = rng.random()
        if roll < 0.6:
            frames.append(turn(_text(rng), **({"store": False} if rng.random() < 0.2 else {})))
            kinds.append("turn")
        elif roll < 0.7:
            frames.append('{"type": "response.cancel"}')
            kinds.append("contentless")
        elif roll < 0.78:
            frames.append(json.dumps({"type": "session.update", "note": _text(rng)}))
            kinds.append("unknown-type")
        elif roll < 0.85:
            frames.append(b"\x00rc" + bytes([rng.randrange(256)]))
            kinds.append("binary")
        elif roll < 0.92:
            frames.append(turn(_text(rng))[:20])
            kinds.append("malformed")
        else:
            frames.append(json.dumps([_text(rng)]))
            kinds.append("not-object")
    return tuple(frames), kinds


def generate(seed: int, count: int, transport: str) -> list[tuple[Case, dict[str, Any]]]:
    """``count`` cases for a cell's transport, drawn from the table's inputs and P1-0's observed templates by a bounded,
    seeded generator: methods from ``_METHODS``, raw targets from the templates with escaped, blank and repeated query
    occurrences, header lists from the observed names with synthetic values and duplicates, bodies of each family with
    fields added and removed (empty, not an object, malformed, a key twice, half a character, gzip), frames of each
    kind. Each with its descriptor: the template and the mutations' names, no value."""
    rng = random.Random(f"{seed}:{transport}")
    templates, header_names = _observed()
    http = [t for t in templates if t[0] == "http"] + list(_EXTRA_TEMPLATES)
    out: list[tuple[Case, dict[str, Any]]] = []
    for n in range(count):
        if transport == "ws":
            template = rng.choice(_WS_TEMPLATES)
            path = _escape(template, rng) + _query((), rng)
            names = header_names.get("ws /backend-api/codex/responses", [n for n, _ in CODEX_WS])
            headers = _headers_for(names, n, rng)
            frames, kinds = _frames_for(rng)
            case = Case(f"gen-ws-{n:05d}", ("generated",), "GET", path, headers, transport="ws", frames=frames)
            out.append((case, {"template": template, "frames": kinds}))
            continue
        _, method, template, query_names = rng.choice(http)
        mutations = []
        if rng.random() < 0.08:
            method = rng.choice(_METHODS)
            mutations.append("method")
        path = _escape(_fill(template, rng), rng) + _query(query_names, rng)
        names = header_names.get("http " + template, ["authorization", "content-type", "user-agent"])
        headers = _headers_for(names, n, rng)
        body, more = (b"", []) if method in ("GET", "HEAD", "OPTIONS") else _body_for(path, rng)
        if "gzip" in more:
            headers = (*headers, ("content-encoding", "gzip"))
        case = Case(f"gen-http-{n:05d}", ("generated",), method, path, headers, body)
        out.append((case, {"template": template, "method": method, "mutations": mutations + more}))
    return out


# --- the golden ----------------------------------------------------------------------------------------------------


def load_golden(path: Path = GOLDEN) -> dict[str, Any]:
    return json.loads(gzip.decompress(path.read_bytes()))


def load_baseline(path: Path = BASELINE) -> dict[str, Any]:
    """Phase 2's baseline: what 0.11 does, the table's tests and ``replay`` compare with (``baseline_judge``)."""
    return load_golden(path)


def write_golden(doc: dict[str, Any], path: Path = GOLDEN) -> str:
    """The golden written (gzip with no time in it, so the same content makes the same bytes); its SHA-256."""
    raw = json.dumps(doc, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    data = gzip.compress(raw, compresslevel=9, mtime=0)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def holds_no_value(doc: Any) -> list[str]:
    """Every place in ``doc`` where a synthetic original or a fake-shaped string of the cases sits (none, for a golden
    that keeps names and categories only)."""
    text = json.dumps(doc, ensure_ascii=False)
    forms = {
        original: (original, urllib.parse.quote(original, safe=""), json.dumps(original)[1:-1])
        for original in ORIGINALS
    }
    return [f"<O:{ORIGINALS.index(o)}>" for o, written in forms.items() if any(w in text for w in written)]


def expected(golden: dict[str, Any], machine: str, case: Case) -> dict[str, Any] | None:
    return golden.get("table", {}).get(machine, {}).get(case.id)


# --- observing whole cells (the script's record and replay) --------------------------------------------------------

CellFactory = Callable[[], contextlib.AbstractAsyncContextManager[Cell]]


async def observe_table(make_cell: CellFactory, machine: str) -> dict[str, Any]:
    """Every row of the table for ``machine``, in order, in one fresh cell: ``{row: observation}``."""
    async with make_cell() as cell:
        return {case.id: await run_case(cell, case) for case in table_for(machine)}


async def observe_generated(
    make_cell: CellFactory, transport: str, seed: int, count: int
) -> tuple[list[Any], set[str]]:
    """The ``count`` generated cases of ``transport``, in order, in one fresh cell: ``[[case, descriptor,
    observation]]``, and every fake the cell's vault drew (for the script's check that the golden holds none)."""
    out: list[Any] = []
    async with make_cell() as cell:
        for case, descriptor in generate(seed, count, transport):
            observation = await run_case(cell, case)
            # A vault's hold count is the table's leak rows' measure; across thousands of cases a hold left by an early
            # one lapses after HOLD_SECONDS at a moment the run's speed decides. The engine's counts stay exact.
            observation["held"] = None
            out.append([case.id, descriptor, observation])
        runtime = await cell.runtime() if cell.runtime_of is not None else None
        fakes = set(_fakes(runtime))
    return out, fakes


async def observe_machine(
    make_cell: CellFactory,
    machine: str,
    seed: int,
    count: int,
    out: Path,
    make_passthrough: CellFactory | None = None,
) -> None:
    """What ``scripts/request_characterization.py`` asks of one machine: its table (and the passthrough's), and its two
    cells of generated cases, written to ``out`` as JSON with the fakes the vaults drew (never committed)."""
    part: dict[str, Any] = {"table": {machine: await observe_table(make_cell, machine)}, "cells": {}}
    if make_passthrough is not None:
        part["table"]["passthrough"] = await observe_table(make_passthrough, "passthrough")
    fakes: set[str] = set()
    for transport in ("http", "ws"):
        if count <= 0:
            break
        cases, drawn = await observe_generated(make_cell, transport, seed, count)
        part["cells"][f"{machine}-{transport}"] = cases
        fakes |= drawn
    part["fakes"] = sorted(fakes)
    await asyncio.to_thread(out.write_bytes, json.dumps(part, sort_keys=True).encode())
