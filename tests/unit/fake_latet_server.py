"""A Latet server as a computer meets it, for the computer's own tests: the machines' calls and one space's routes,
over HTTPS on 127.0.0.1 with a self-signed certificate. No PostgreSQL or Valkey: its answers are the bodies E0c reads
from E0b, its refusals E0b's (tests/server/test_e0b_contract.py checks the real ones): a token it does not take 401
``{"error": "unauthorized"}``, a web page's request (an ``Origin``, a ``sec-fetch-site``) 403, a sign-in link or an
identity without the token's computer key 403 ``computer_key_required`` (``computer_key_missing`` for a token it holds
no key of), a limit 429 with ``Retry-After``, a busy server 503 with ``Retry-After: 1``, ``cache-control: no-store`` on
the machines' calls."""

from __future__ import annotations

import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from latet.server.certificate import make_certificate
from latet.server.machines import COMPUTER_KEY_WORDS, KEYLESS_WORDS
from latet.server.tokens import route_key

API = "/_latet/server/v1"
EMILY = {"id": "u_emilycarte", "name": "Emily Carter", "kind": "person"}
REFUSED = {"error": "unauthorized"}  # E0b's words for a token or an administration token it does not take
PAGE = {"error": "browser requests are not accepted here"}
BUSY = {"error": "the server cannot answer now: try again in a moment"}
LIMITED = {"error": "too many credentials refused from this address: wait a minute"}
ADMIN_TOKEN = "latet_adm_" + "r" * 43  # the one administration token it takes
_NO_STORE = {"cache-control": "no-store", "x-content-type-options": "nosniff"}
KEY_REQUIRED = {"error": COMPUTER_KEY_WORDS, "reason": "computer_key_required"}  # E0c's words (R16)
KEYLESS = {"error": KEYLESS_WORDS, "reason": "computer_key_missing"}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _from_a_page(request: Request) -> bool:
    return "origin" in request.headers or "sec-fetch-site" in request.headers


class FakeLatetServer:
    """``url``, and ``fingerprint`` and ``pem`` of its certificate (made for it, or ``certificate``: the files of
    another, served in its place); ``code``, the one-time code that works (once); ``tokens``, the secrets it takes (a
    computer's token, or an API key a test puts there), and ``keys``, the computer key of each token it issued (a test
    may put one there); what computers sent it, in ``identities``, ``labels`` (each enrolment's), ``asked`` (each
    machine's call that reached it: (method, path below /_latet/server/v1)), ``calls`` ((method, path below the space))
    and ``keyed`` (each call of either kind that carried a computer key: (method, path), whatever its key). What a test makes it answer: ``busy`` (that many machines' calls answered 503
    ``Retry-After: 1``, as E0b answers while its credentials' threads are busy), ``limited`` (429 ``Retry-After: 60``,
    as past a limit), ``answers`` (a machine's call, by its path below /_latet/server/v1, answered as given; a list
    of answers is given in turn, once each, and the call answered as it is after them) and ``space_answers`` (a call
    to a space, by its path below the space, answered as given once the gate let it through)."""

    def __init__(self, folder: Path, certificate: tuple[Path, Path] | None = None) -> None:
        self.port = free_port()
        self.url = f"https://127.0.0.1:{self.port}"
        if certificate is None:
            self.crt, self.key, self.fingerprint = make_certificate(self.url, folder)
        else:
            from latet.engine.reviewer.certificate import fingerprint_of

            self.crt, self.key = certificate
            self.fingerprint = fingerprint_of(self.crt.read_text("utf-8"))
        self.pem = self.crt.read_text("utf-8")
        self.code: str | None = "code-" + secrets.token_urlsafe(12)
        self.tokens: set[str] = set()
        self.keys: dict[str, str] = {}
        self.keyed: list[tuple[str, str]] = []
        self.identities: list[dict[str, Any]] = []
        self.labels: list[Any] = []
        self.asked: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []
        self.busy = 0
        self.limited = False
        self.answers: dict[str, Response | list[Response]] = {}
        self.space_answers: dict[str, Response] = {}
        machine = self._machine
        self.app = Starlette(
            routes=[
                Route(f"{API}/enrol", machine(self.enrol), methods=["POST"]),
                Route(f"{API}/login-code", machine(self.login_code), methods=["POST"]),
                Route(f"{API}/whoami", machine(self.whoami), methods=["GET"]),
                Route(f"{API}/identity", machine(self.identity), methods=["PUT"]),
                Route(f"{API}/token", machine(self.drop_token), methods=["DELETE"]),
                Route(f"{API}/moved", machine(self.moved), methods=["GET"]),
                Route(f"{API}/admin/status", machine(self.admin_status), methods=["GET"]),
                Route("/u/{route}/{rest:path}", self.space, methods=["GET", "POST", "DELETE"]),
            ]
        )
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def _machine(self, handler):
        """A machine's call as E0b answers it: a web page's refused first, then what the test asked for, then the call;
        ``no-store`` on every answer."""

        async def endpoint(request: Request) -> Response:
            self.asked.append((request.method, request.url.path.removeprefix(f"{API}/")))
            if "x-latet-computer-key" in request.headers:
                self.keyed.append((request.method, request.url.path.removeprefix(f"{API}/")))
            if _from_a_page(request):
                answer: Response = JSONResponse(PAGE, status_code=403)
            elif (given := self._given(request.url.path.removeprefix(f"{API}/"))) is not None:
                answer = given
            elif self.busy > 0:
                self.busy -= 1
                answer = JSONResponse(BUSY, status_code=503, headers={"retry-after": "1"})
            elif self.limited:
                answer = JSONResponse(LIMITED, status_code=429, headers={"retry-after": "60"})
            else:
                answer = await handler(request)
            answer.headers.update(_NO_STORE)
            return answer

        return endpoint

    def _given(self, path: str) -> Response | None:
        """The answer a test gave for ``path``: the same each time, or the next of a list (None once it is spent)."""
        given = self.answers.get(path)
        if isinstance(given, list):
            return given.pop(0) if given else None
        return given

    def _known(self, request: Request) -> str | None:
        given = request.headers.get("x-latet-token", "")
        return given if given in self.tokens else None

    def _keyless(self, request: Request, token: str) -> Response | None:
        """E0c's refusal of a call that asks for the token's computer key (R16), or None when it carries it."""
        if token not in self.keys:
            return JSONResponse(KEYLESS, status_code=403)
        if request.headers.get("x-latet-computer-key") != self.keys[token]:
            return JSONResponse(KEY_REQUIRED, status_code=403)
        return None

    def _routes(self, secret: str) -> dict[str, str]:
        """The addresses E0b's server gives with a secret (its `routes_of`)."""
        base = f"{self.url}/u/{route_key(secret)}"
        return {"base": base, "anthropic": f"{base}/anthropic", "openai": f"{base}/openai/v1"}

    async def _body(self, request: Request) -> dict[str, Any]:
        return await request.json() if await request.body() else {}

    async def enrol(self, request: Request) -> Response:
        given = await self._body(request)
        self.labels.append(given.get("label"))
        if self.code is None or given.get("code") != self.code:
            said = "this code is unknown, used or late: ask your administrator for a new one"
            return JSONResponse({"error": said}, status_code=401)
        self.code = None
        token = "latet_pat_" + secrets.token_urlsafe(32)
        self.tokens.add(token)
        self.keys[token] = key = "latet_ck_" + secrets.token_urlsafe(32)
        answer = {"token": token, "computer_key": key, "identity": EMILY, "url": self.url}
        return JSONResponse({**answer, **self._routes(token)})

    async def login_code(self, request: Request) -> Response:
        if (token := self._known(request)) is None:
            return JSONResponse(REFUSED, status_code=401)
        if (refused := self._keyless(request, token)) is not None:
            return refused
        code = secrets.token_urlsafe(24)  # E0b's sign-in codes
        return JSONResponse({"code": code, "url": f"{self.url}/_latet/ui/login?code={code}"})

    async def whoami(self, request: Request) -> Response:
        token = self._known(request)
        if token is None:
            return JSONResponse(REFUSED, status_code=401)
        return JSONResponse({**EMILY, "url": self.url, "version": "test", **self._routes(token)})

    async def identity(self, request: Request) -> Response:
        if (token := self._known(request)) is None:
            return JSONResponse(REFUSED, status_code=401)
        if (refused := self._keyless(request, token)) is not None:
            return refused
        self.identities.append(await self._body(request))
        return JSONResponse({"ok": True})

    async def drop_token(self, request: Request) -> Response:
        token = self._known(request)
        if token is None:
            return JSONResponse(REFUSED, status_code=401)
        self.tokens.discard(token)
        self.keys.pop(token, None)
        return JSONResponse({"ok": True})

    async def moved(self, request: Request) -> Response:
        """A redirect to another of its calls, which a computer never follows."""
        return RedirectResponse(f"{API}/whoami", status_code=307)

    async def admin_status(self, request: Request) -> Response:
        if request.headers.get("x-latet-admin") != ADMIN_TOKEN:
            return JSONResponse(REFUSED, status_code=401)
        return JSONResponse({"version": "test", "url": self.url, "edition": []})

    async def space(self, request: Request) -> Response:
        """An identity's address, as E0b's gate answers it: a web page's request refused, an address it does not know
        refused, its bridge's calls with the identity's own token alone."""
        route, rest = request.path_params["route"], request.path_params["rest"]
        if "x-latet-computer-key" in request.headers:
            self.keyed.append((request.method, rest))
        if _from_a_page(request):
            return JSONResponse({"error": "browser requests are not accepted on an identity's address"}, 403)
        if route not in {route_key(token) for token in self.tokens}:
            return JSONResponse({"error": "there is no such address"}, status_code=403)
        token = self._known(request)
        if rest.startswith("_latet/") and (token is None or route_key(token) != route):
            return JSONResponse(REFUSED, status_code=401)
        self.calls.append((request.method, rest))
        if (given := self.space_answers.get(rest)) is not None:
            return given
        body = await self._body(request)
        if rest == "_latet/admin/destinations":
            return JSONResponse({"names": ["llm-acme-com" for _ in body.get("destinations", [])]})
        if rest == "_latet/admin/user-name":
            return JSONResponse({"surrogate": "usr_4f2a" if body.get("name") else None})
        if rest == "_latet/engine/v1/ping":
            return JSONResponse({"ok": True, "version": "test"})
        return JSONResponse({"ok": True})

    def start(self) -> FakeLatetServer:
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning",
            ssl_certfile=str(self.crt), ssl_keyfile=str(self.key),
        )  # fmt: skip
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        if self._server is not None and self._thread is not None:
            self._server.should_exit = True
            self._thread.join(timeout=5)
            if self._thread.is_alive():  # a connection that will not close (a TLS handshake refused midway, under load)
                self._server.force_exit = True
                self._thread.join(timeout=10)
