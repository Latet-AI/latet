"""RC-REC-8's send paths (P1-3 of ``notes/server-request-coverage-plan.md``): no request reaches a provider but through
``gateway/request_transport.py``, so that no other send can be added unseen.

An AST walk of ``src/latet/`` finds every place that could send over the network: a ``build_request(``, a ``_upstream(``,
a websockets ``connect(`` (or a class made from it), httpx's own ``httpx.get(`` and its kin, and the ``.send(``,
``.request(``, ``.stream(``, ``.get(``, ``.post(``, ``.put(``, ``.patch(``, ``.delete(``, ``.head(`` or ``.options(``
of a client, each receiver resolved by its binding: a name or an attribute assigned from ``httpx.AsyncClient``,
``httpx.Client``, ``make_client``, a websockets ``connect`` or a function that returns one of them, or a parameter
annotated with one, so that a ``dict.get(`` is never one. Outside ``request_transport.py``, each place found must be in
the reviewed allowlist below, named one by one with why it is no provider send; and each entry of the allowlist must
still be found, so that none outlives its place. ``tests/request_send_guard.py`` backs it while the e2e and agent suites
run (R-87)."""

from __future__ import annotations

import ast
import asyncio
import re
import sys
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from latet.gateway import outcome, request_policy, request_transport

from ..request_send_guard import SendGuard, UnguardedSend, installed

SRC = Path(__file__).resolve().parents[2] / "src" / "latet"
TRANSPORT = "gateway/request_transport.py"  # the one module that sends to a provider over HTTP (S1, S2)

SENDS = frozenset({"send", "request", "stream", "get", "post", "put", "patch", "delete", "head", "options"})
# Flagged whatever their receiver: what sends a message, builds a request, or hands one to httpx's transport.
ALWAYS = frozenset({"send", "build_request", "handle_async_request", "handle_request"})
_HTTPX_CLIENTS = ("AsyncClient", "Client")
_FACTORIES = frozenset({"make_client"})  # latet.gateway.forward's shared client


@dataclass(frozen=True, slots=True, order=True)
class Site:
    """One place that could send: its module (under ``src/latet``), the function it is in, and what it calls."""

    path: str
    where: str
    call: str


# The reviewed allowlist (R-77, P1-0's inventory §5): each place outside request_transport.py, and why it sends nothing
# to a provider. Named one by one: a new call, in these modules too, is a new entry to review.
ALLOWED: dict[Site, str] = {
    # The local reviewer (engine/reviewer/client.py): original texts to the owner's own model, by the owner's
    # configuration; outside Latet's enforcement (Global constraints), no provider send.
    Site("engine/reviewer/client.py", "ReviewClient.ask", "self._http.stream"): "the local reviewer",
    Site("engine/reviewer/client.py", "ReviewClient.models", "self._http.get"): "the local reviewer",
    Site("engine/reviewer/client.py", "ReviewClient.props", "self._http.get"): "the local reviewer",
    # The tutorial's in-process clients: an ASGI transport to the tutorial's own app, never the network.
    Site("tutorial/agent.py", "send", "client.post"): "the tutorial's in-process client",
    Site("tutorial/session.py", "TutorialSession._reload", "client.post"): "the tutorial's in-process client",
    # Model downloads, release checks, a computer's client of its Latet server: Latet's own services, no request of an
    # agent's.
    Site("models.py", "install", "http.stream"): "a model's download",
    Site("releases.py", "_latest", "http.get"): "a release check",
    Site("releases.py", "_tagged", "http.get"): "a release check",
    Site("releases.py", "_from_page", "http.get"): "a release check",
    Site("server/client.py", "ServerClient._exchange", "self._http.stream"): "a computer's calls to its Latet server",
}


def _annotated_client(annotation: ast.expr | None, httpx_names: set[str], clients: set[str]) -> bool:
    if annotation is None:
        return False
    text = ast.unparse(annotation)
    for module in httpx_names:
        if re.search(rf"\b{re.escape(module)}\.({'|'.join(_HTTPX_CLIENTS)})\b", text):
            return True
    return any(re.search(rf"\b{re.escape(name)}\b", text) for name in clients)


class _Module:
    """What one module binds: httpx under which names, the client classes and websockets ``connect`` it imports, the
    functions and classes that make a client."""

    def __init__(self, tree: ast.Module) -> None:
        self.httpx: set[str] = set()
        self.clients: set[str] = {"ClientConnection"}  # httpx's classes imported by name, websockets' connection
        self.connects: set[str] = set()  # websockets' connect, and classes made from it
        self.factories: set[str] = set(_FACTORIES)
        self.chained: set[str] = set()  # the attributes any class of the module binds to a client (``_attributes``)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "httpx":
                        self.httpx.add(alias.asname or "httpx")
                    elif alias.name.startswith("websockets"):
                        self.connects.add(f"{alias.asname or alias.name}.connect")
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    name = alias.asname or alias.name
                    if node.module == "httpx" and alias.name in _HTTPX_CLIENTS:
                        self.clients.add(name)
                    elif node.module.startswith("websockets") and alias.name == "connect":
                        self.connects.add(name)
        for _ in range(2):  # a class made from a class made from connect
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and any(ast.unparse(b) in self.connects for b in node.bases):
                    self.connects.add(node.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and self.annotated(node.returns):
                self.factories.add(node.name)

    def annotated(self, annotation: ast.expr | None) -> bool:
        return _annotated_client(annotation, self.httpx, self.clients)

    def makes(self, call: ast.Call) -> str | None:
        """What a call makes a client with, when it does: its callee's text."""
        callee = ast.unparse(call.func)
        if callee in self.connects or callee in self.factories:
            return callee
        if any(callee == f"{module}.{cls}" for module in self.httpx for cls in _HTTPX_CLIENTS):
            return callee
        if callee in self.clients - {"ClientConnection"}:
            return callee
        if isinstance(call.func, ast.Attribute) and call.func.attr in self.factories:
            return callee
        return None


_Scope = tuple[str, ast.AST, str | None, ast.AST | None]


def _scopes(tree: ast.Module) -> Iterator[_Scope]:
    """Each function, with its qualified name, its class (for ``self.`` attributes) and the function it is nested in
    (whose names it reads as its own: a closure), nested ones included."""

    def visit(node: ast.AST, prefix: str, owner: str | None, outer: ast.AST | None) -> Iterator[_Scope]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                yield from visit(child, f"{prefix}{child.name}.", child.name, None)
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{prefix}{child.name}"
                yield name, child, owner, outer
                yield from visit(child, f"{name}.", owner, child)
            else:
                yield from visit(child, prefix, owner, outer)

    yield "<module>", tree, None, None
    yield from visit(tree, "", None, None)


def _own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """The nodes of a function (or the module) outside the functions and classes it defines."""
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        yield child
        yield from _own_nodes(child)


def _attributes(tree: ast.Module, module: _Module) -> dict[str, set[str]]:
    """For each class, the ``self.<name>`` attributes it binds to a client (from a client, a parameter annotated as one,
    or a name bound to one), anywhere in its methods."""
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        attrs: set[str] = set()
        for _ in range(2):
            for method in node.body:
                if not isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                names = _bound(method, module, attrs)
                for sub in ast.walk(method):
                    targets: list[ast.expr] = []
                    value: ast.expr | None = None
                    if isinstance(sub, ast.Assign):
                        targets, value = list(sub.targets), sub.value
                    elif isinstance(sub, ast.AnnAssign):
                        targets, value = [sub.target], sub.value
                        if isinstance(sub.target, ast.Attribute) and module.annotated(sub.annotation):
                            attrs.add(sub.target.attr)
                    for target in _targets(targets):
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                            and value is not None
                            and _client(value, module, names, attrs)
                        ):
                            attrs.add(target.attr)
        found[node.name] = attrs
    return found


def _targets(targets: list[ast.expr]) -> Iterator[ast.expr]:
    """Each target of an assignment, those a tuple unpacks to included (``self._http, owned = _http(...)``)."""
    for target in targets:
        if isinstance(target, ast.Tuple | ast.List):
            yield from _targets(list(target.elts))
        else:
            yield target


def _client(expr: ast.expr, module: _Module, names: set[str], attrs: set[str]) -> bool:
    if isinstance(expr, ast.Await):
        return _client(expr.value, module, names, attrs)
    if isinstance(expr, ast.Call):
        return module.makes(expr) is not None
    if isinstance(expr, ast.Name):
        return expr.id in names
    if isinstance(expr, ast.Attribute):
        if isinstance(expr.value, ast.Name) and expr.value.id == "self":
            return expr.attr in attrs
        return (
            expr.attr in module.chained
        )  # ``state["forwarder"].client``: a name a class of the module binds a client to
    if isinstance(expr, ast.BoolOp):
        return any(_client(v, module, names, attrs) for v in expr.values)
    if isinstance(expr, ast.IfExp):
        return _client(expr.body, module, names, attrs) or _client(expr.orelse, module, names, attrs)
    return False


def _bound(scope: ast.AST, module: _Module, attrs: set[str]) -> set[str]:
    """The names a function binds to a client: its parameters annotated as one, and what it assigns from one (``=``,
    ``with ... as``)."""
    names: set[str] = set()
    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        args = scope.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if arg is not None and module.annotated(arg.annotation):
                names.add(arg.arg)
    for _ in range(2):
        for node in _own_nodes(scope):
            if isinstance(node, ast.Assign) and _client(node.value, module, names, attrs):
                names.update(t.id for t in _targets(node.targets) if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if module.annotated(node.annotation) or (
                    node.value is not None and _client(node.value, module, names, attrs)
                ):
                    names.add(node.target.id)
            elif isinstance(node, ast.With | ast.AsyncWith):
                for item in node.items:
                    if isinstance(item.optional_vars, ast.Name) and _client(item.context_expr, module, names, attrs):
                        names.add(item.optional_vars.id)
    return names


def _flagged(func: ast.expr, module: _Module, names: set[str], attrs: set[str], *, taken: bool = False) -> bool:
    """Whether calling ``func``, an attribute, could send: one of ``ALWAYS``, whatever its receiver (but the transport
    itself), or one of ``SENDS`` on a client or on httpx. ``taken``: the method taken to be called later, where a
    ``.send`` whose receiver is not known to be a client is a value's own (an image policy's ``send``, seven times in
    ``src/latet``) and is left to the runtime guard."""
    if not isinstance(func, ast.Attribute):
        return False
    receiver = func.value
    if isinstance(receiver, ast.Name) and receiver.id == "request_transport":
        return False  # the transport's own steps: the way to send
    if func.attr in ALWAYS and not (taken and func.attr == "send"):
        return True
    return func.attr in SENDS and (
        _client(receiver, module, names, attrs) or (isinstance(receiver, ast.Name) and receiver.id in module.httpx)
    )


def sends_in(source: str, path: str) -> list[Site]:
    """Every place in ``source`` (the module ``path``) that could send over the network: a call, a method taken to be
    called later, or one reached by ``getattr``."""
    tree = ast.parse(source)
    module = _Module(tree)
    attributes = _attributes(tree, module)
    module.chained = set().union(*attributes.values())
    found: set[Site] = set()
    bound: dict[ast.AST, set[str]] = {}
    for where, scope, owner, outer in _scopes(tree):
        attrs = attributes.get(owner or "", set())
        names = bound[scope] = _bound(scope, module, attrs) | (bound.get(outer, set()) if outer is not None else set())
        nodes = list(_own_nodes(scope))
        called = {id(node.func) for node in nodes if isinstance(node, ast.Call)}
        for node in nodes:
            if isinstance(node, ast.Attribute) and id(node) not in called and isinstance(node.ctx, ast.Load):
                if _flagged(node, module, names, attrs, taken=True):  # ``sending = client.send``
                    found.add(Site(path, where, f"{ast.unparse(node)} (reference)"))
                continue
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            made = module.makes(node)
            if made is not None and made in module.connects:
                found.add(Site(path, where, f"{made}(connect)" if made != "connect" else "connect"))
            elif isinstance(func, ast.Name) and func.id in ("build_request", "_upstream"):
                found.add(Site(path, where, func.id))
            elif isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
                name = node.args[1]
                if isinstance(name, ast.Constant) and isinstance(name.value, str):
                    taken = ast.Attribute(value=node.args[0], attr=name.value, ctx=ast.Load())
                    if _flagged(taken, module, names, attrs):
                        found.add(Site(path, where, ast.unparse(node)))
            elif _flagged(func, module, names, attrs):
                found.add(Site(path, where, ast.unparse(func)))
    return sorted(found)


def _found() -> list[Site]:
    found: list[Site] = []
    for file in sorted(SRC.rglob("*.py")):
        path = file.relative_to(SRC).as_posix()
        if path != TRANSPORT:
            found += sends_in(file.read_text("utf-8"), path)
    return found


def test_no_provider_send_outside_request_transport():
    found = _found()
    unreviewed = [site for site in found if site not in ALLOWED]
    assert unreviewed == [], "a send outside gateway/request_transport.py: guard it, or review it into ALLOWED"
    outlived = [site for site in ALLOWED if site not in found]
    assert outlived == [], "an allowlist entry whose place is gone: remove it"
    # The transport itself sends: the walk sees it there (what it would flag anywhere else).
    assert sends_in((SRC / TRANSPORT).read_text("utf-8"), TRANSPORT), "request_transport.py builds and sends"


def test_the_walk_resolves_each_receiver_by_its_binding():
    """The walk's own check, on code written for it: what sends is found, a ``dict.get(`` never."""
    source = textwrap.dedent(
        """
        import httpx
        from websockets.asyncio.client import ClientConnection, connect
        from latet.gateway.forward import make_client

        class Again(connect):
            pass

        async def opened(url: str) -> ClientConnection:
            return await Again(url)

        class Sender:
            def __init__(self, client: httpx.AsyncClient | None = None) -> None:
                self.client = client or make_client()
                self.table = {}

            async def go(self, request):
                self.table.get("x")
                await self.client.send(request)

        async def free(url, settings: dict):
            settings.get("a")
            async with httpx.AsyncClient() as client:
                await client.post(url)
            shared = make_client()
            built = shared.build_request("POST", url)
            await shared.send(built)
            up = await opened(url)
            await up.send("frame")
            await connect(url)
            httpx.get(url)
            plain = {}
            plain.get("b")

        async def ordinary(client, request, state, url, policy):
            await client.send(request)  # an unannotated parameter
            await state["forwarder"].client.send(request)  # a chained attribute
            await state["forwarder"].client.post(url)  # its last name a client attribute of the module's
            sending = state["forwarder"].client.send  # a method's reference, its receiver a client
            await sending(request)
            allowed = policy.send  # a value's own send, its receiver not known: the runtime guard's
            await getattr(client, "send")(request)
            await httpx.AsyncHTTPTransport().handle_async_request(request)  # httpx's transport, called directly
            await request_transport.send(client, request)  # the transport itself: the way to send
            state.get("c")
        """
    )
    assert [(s.where, s.call) for s in sends_in(source, "x.py")] == [
        ("Sender.go", "self.client.send"),
        ("free", "client.post"),
        ("free", "connect"),
        ("free", "httpx.get"),
        ("free", "shared.build_request"),
        ("free", "shared.send"),
        ("free", "up.send"),
        ("opened", "Again(connect)"),
        ("ordinary", "client.send"),
        ("ordinary", "getattr(client, 'send')"),
        ("ordinary", "httpx.AsyncHTTPTransport().handle_async_request"),
        ("ordinary", "state['forwarder'].client.post"),
        ("ordinary", "state['forwarder'].client.send"),
        ("ordinary", "state['forwarder'].client.send (reference)"),
    ]


def _latet_code(module: str, source: str) -> dict[str, Any]:
    """``source`` run as if it were Latet's module ``module`` (its frames name it): what the runtime guard judges by."""
    scope: dict[str, Any] = {"__name__": module, "sys": sys}
    exec(compile(textwrap.dedent(source), f"<{module}>", "exec"), scope)
    return scope


async def test_the_runtime_guard_refuses_a_send_of_latets_without_the_transports_token(monkeypatch):
    """``tests/request_send_guard.py``'s own check (its RED, a deliberate unguarded send): a request Latet's code
    sends without the token is refused before the transport writes it; the transport's own send, authorized, passes, and
    so do a test's own request and the local reviewer's. A WebSocket handshake of Latet's passes only as the
    transport's adapter opens it, with the token of that very connection (S3, P1-4): the relay's old name is no
    allowance any more."""
    reached: list[bytes] = []

    async def answered(self: Any, request: httpx.Request) -> httpx.Response:  # httpx's transport, with no network
        reached.append(await request.aread())
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", answered)
    unguarded = _latet_code(
        "latet.gateway.scratch_send",
        """
        async def unguarded(client, url):
            return await client.post(url, content=b'{"unguarded": 1}')

        def opened(guard):
            guard.check("ws", None, sys._getframe())
        """,
    )
    reviewer = _latet_code(
        "latet.engine.reviewer.client", "async def ask(client, url):\n    return await client.post(url)"
    )
    relay = _latet_code("latet.gateway.ws", "def _upstream(guard):\n    guard.check('ws', None, sys._getframe())")
    url = "http://127.0.0.1:9/v1/chat/completions"
    guard = SendGuard(checking=True)
    with installed(guard):
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(UnguardedSend):
                await unguarded["unguarded"](client, url)
            with pytest.raises(UnguardedSend):
                unguarded["opened"](guard)
            assert reached == []  # refused before the transport wrote anything
            await client.post(url, content=b"{}")  # the test's own request: not Latet's
            await reviewer["ask"](client, url)  # the local reviewer: no provider send
            with pytest.raises(UnguardedSend):
                relay["_upstream"](guard)  # no handshake is allowed by name (P1-4)
            with _plain_exchange() as (arena, envelope, decision):
                authorized = request_transport.validate_send(decision, arena, envelope, request_policy.current(), 1)
                assert isinstance(authorized, request_transport.AuthorizedRequest)
                built = request_transport.build(authorized, client, "POST", url, [("content-type", "application/json")])
                assert isinstance(built, request_transport.BuiltRequest)
                sent = await request_transport.send(client, built)
                assert isinstance(sent, httpx.Response) and sent.status_code == 200
    assert reached == [b"{}", b"", envelope.body]
    assert [line.split(" without")[0] for line in guard.refused] == [
        "http connection from latet.gateway.scratch_send.unguarded",
        "ws connection from latet.gateway.scratch_send.opened",
        "ws connection from latet.gateway.ws._upstream",
    ]
    assert httpx.AsyncHTTPTransport.handle_async_request is answered  # put back as it was


async def test_the_runtime_guard_follows_latets_client_into_tasks_and_a_token_passes_its_own_request_alone(monkeypatch):
    """The review's M-4 and M-6: a client Latet's code made needs the transport's token whatever runs its request (a
    task of its own, ``gather``: no frame of Latet's on their stack); and the token passes the very request the
    transport sends, never another made meanwhile (as a redirect would be)."""
    reached: list[str] = []
    inner: list[httpx.AsyncClient] = []

    async def answered(self: Any, request: httpx.Request) -> httpx.Response:  # httpx's transport, with no network
        reached.append(request.url.path)
        if request.url.path == "/v1/chat/completions" and inner:  # another request while the first's token is active
            await inner[0].send(inner[0].build_request("POST", "http://127.0.0.1:9/second"))
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", answered)
    made = _latet_code("latet.gateway.scratch_client", "import httpx\ndef client():\n    return httpx.AsyncClient()")
    guard = SendGuard(checking=True)
    with installed(guard):
        client = made["client"]()  # a client of Latet's
        try:
            with pytest.raises(UnguardedSend):
                await asyncio.create_task(client.post("http://127.0.0.1:9/task"))
            [gathered] = await asyncio.gather(client.post("http://127.0.0.1:9/gathered"), return_exceptions=True)
            assert isinstance(gathered, UnguardedSend)
            inner.append(client)
            with _plain_exchange() as (arena, envelope, decision):
                authorized = request_transport.validate_send(decision, arena, envelope, request_policy.current(), 1)
                assert isinstance(authorized, request_transport.AuthorizedRequest)
                headers = [("content-type", "application/json")]
                built = request_transport.build(
                    authorized, client, "POST", "http://127.0.0.1:9/v1/chat/completions", headers
                )
                assert isinstance(built, request_transport.BuiltRequest)
                with pytest.raises(UnguardedSend):
                    await request_transport.send(client, built)
        finally:
            await client.aclose()
    assert reached == ["/v1/chat/completions"]  # first alone; task, gathered and nested second did not
    assert [line.split(" without")[0] for line in guard.refused] == [
        "http request of a client of Latet's",
        "http request of a client of Latet's",
        "http request of a client of Latet's",
    ]


@contextmanager
def _plain_exchange() -> Any:
    from latet.gateway.coverage import envelope_of
    from latet.gateway.routes import Upstream

    from .test_request_outcomes import _mediator, _strict

    path = "/v1/chat/completions"
    headers = {"content-type": "application/json"}
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }
    body = b'{"model": "m", "messages": []}'
    envelope = envelope_of(scope, "http", "openai", body)
    upstream = Upstream("openai", "", "http://127.0.0.1:9", envelope_expected=frozenset({("model", "m")}))
    mediator = _mediator()
    arena = mediator.prepare_http(upstream, "POST", path, headers, body, ())
    try:
        decision = _strict(arena, envelope, upstream)
        assert isinstance(decision, outcome.Forward)
        yield arena, envelope, decision
    finally:
        arena.close_receipt()
        if arena.lease is not None:
            arena.lease.release()
        mediator.close()
