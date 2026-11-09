"""A server reaches a destination's name at any of the addresses its lookup checked (``egress.pin``): in the order the
system gave them, the next one only when the connection to the one before failed, before any byte of the request left;
never an address the lookup did not check, never another once a connection was made, a bounded number of them in a
bounded time. A name with a forbidden address among them is refused whole, as before."""

import asyncio
import contextlib
import datetime
import ipaddress
import socket
import ssl
import threading
import types

import httpcore
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from latet.gateway import request_transport, ws
from latet.gateway.forward import client_options, make_client
from latet.server import egress
from latet.server.egress import EgressRefused

from .conftest import refused_on_v6

# Addresses on the internet. (The documentation ranges are refused: a network may use them inside.)
PUBLIC, PUBLIC_V6, OTHER = "1.2.3.4", "2a00::1", "5.6.7.8"
OK = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
BODY = b'{"model":"gpt-4.1-mini","messages":[{"role":"user","content":"hello"}]}'


def _resolving(*addresses):
    """A resolver that answers these addresses for any name."""
    return lambda host, port: [ipaddress.ip_address(a) for a in addresses]


class _Server:
    """A server on 127.0.0.1 that reads each request whole (its head, and the body its Content-Length says), keeps it,
    and answers ``answer``, each connection in a thread of its own; with ``answer`` None, it closes the connection once
    it has read the request, answering nothing. ``garbage``: it answers a connection's first bytes with something that
    is no TLS, and closes it. ``tls``: it speaks TLS with that context."""

    def __init__(self, answer: bytes | None = OK, *, garbage: bool = False, tls: ssl.SSLContext | None = None) -> None:
        self.answer, self.garbage, self.tls = answer, garbage, tls
        self.requests: list[bytes] = []
        self.connections = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._kept, args=(conn,), daemon=True).start()

    def _kept(self, conn: socket.socket) -> None:
        with contextlib.suppress(OSError):
            if self.tls is not None:
                conn = self.tls.wrap_socket(conn, server_side=True)
            with conn:
                self._answer(conn)

    def _answer(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        if self.garbage:
            conn.recv(65536)
            conn.sendall(b"this is no TLS\r\n\r\n")
            return
        data = b""
        while True:
            while b"\r\n\r\n" not in data:
                if not (chunk := conn.recv(65536)):
                    return
                data += chunk
            head, _, data = data.partition(b"\r\n\r\n")
            length = next(
                (
                    int(line.split(b":", 1)[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                0,
            )
            while len(data) < length:
                if not (chunk := conn.recv(65536)):
                    return
                data += chunk
            self.requests.append(head + b"\r\n\r\n" + data[:length])
            data = data[length:]
            if self.answer is None:
                return
            conn.sendall(self.answer)

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def connects(monkeypatch):
    """Every TCP connection a client of Latet's asks httpcore's backend for, as ``(host, seconds)``, made as asked."""
    asked: list[tuple[str, float | None]] = []
    connect = httpcore.AnyIOBackend.connect_tcp

    async def recorded(self, host, port, timeout=None, local_address=None, socket_options=None):  # noqa: ASYNC109 - httpcore's interface
        asked.append((host, timeout))
        return await connect(self, host, port, timeout, local_address, socket_options)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", recorded)
    return asked


@pytest.fixture
def lookups(monkeypatch):
    """Every name the system is asked to resolve (an address written as a number is none)."""
    asked: list[str] = []
    resolve = socket.getaddrinfo

    def recorded(host, *args, **kwargs):
        text = host.decode() if isinstance(host, bytes) else str(host)
        try:
            ipaddress.ip_address(text)
        except ValueError:
            asked.append(text)
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", recorded)
    return asked


def _request(client, url, addresses, *, host="gw.acme.com", extensions=None, **options):
    """A POST of ``BODY`` to ``url`` that carries ``addresses`` as egress's checked ones (none: no such list)."""
    carried = dict(extensions or {})
    if addresses is not None:
        carried[egress.ADDRESSES] = addresses
    headers = {"host": host, "content-type": "application/json"}
    return client.build_request("POST", url, headers=headers, content=BODY, extensions=carried, **options)


# --- what egress hands the connection ----------------------------------------------------------------------------------


async def test_a_name_is_pinned_with_every_address_checked_in_the_order_the_system_gave_them(monkeypatch):
    """The URL carries the first address, as before, and the extensions every address the same lookup checked, in the
    order the system gave them (Windows gives ``::1`` before ``127.0.0.1`` for ``localhost``)."""
    monkeypatch.setattr(egress, "resolve", _resolving(PUBLIC_V6, PUBLIC, OTHER))
    url, host, extensions = await egress.pin("https://gw.acme.com:8443/v1")
    assert (url, host) == (f"https://[{PUBLIC_V6}]:8443/v1", "gw.acme.com:8443")
    assert extensions == {"sni_hostname": "gw.acme.com", egress.ADDRESSES: (PUBLIC_V6, PUBLIC, OTHER)}
    assert (await egress.pin(f"https://{PUBLIC}/v1"))[2][egress.ADDRESSES] == (PUBLIC,)  # written as a number: itself


async def test_a_forbidden_address_refuses_the_name_wherever_it_comes_and_a_long_answer_is_tried_in_part(monkeypatch):
    """A name may answer dozens of addresses: any one of them that leads inside refuses the whole name, though it come
    after every address a connection would try; one that leads nowhere inside is tried at its first
    ``ADDRESSES_TRIED``."""
    many = [f"1.2.3.{n}" for n in range(1, 31)]
    assert 2 <= egress.ADDRESSES_TRIED < len(many)
    monkeypatch.setattr(egress, "resolve", _resolving(*many, "10.0.3.9"))
    with pytest.raises(EgressRefused, match="own network"):
        await egress.pin("https://gw.acme.com/v1")
    monkeypatch.setattr(egress, "resolve", _resolving(*many))
    assert (await egress.pin("https://gw.acme.com/v1"))[2][egress.ADDRESSES] == tuple(many[: egress.ADDRESSES_TRIED])


async def test_while_no_nat64_prefix_is_known_no_address_a_nat64_could_translate_is_among_those_tried(monkeypatch):
    """Until the network says its NAT64 prefixes, an IPv6 address of the global space could carry one of the server's
    network: none is among the addresses tried, wherever it came in the answer."""
    monkeypatch.setattr(egress, "_NAT64", egress._Nat64())

    def resolver(host, port):
        if host == "ipv4only.arpa":
            return []  # the network's name server does not answer
        return [ipaddress.ip_address(a) for a in (PUBLIC_V6, PUBLIC, "2a00::2", OTHER)]

    monkeypatch.setattr(egress, "resolve", resolver)
    url, _, extensions = await egress.pin("https://both.acme.com/v1")
    assert url == f"https://{PUBLIC}/v1" and extensions[egress.ADDRESSES] == (PUBLIC, OTHER)


def test_each_address_but_the_last_is_given_half_the_time_left_and_at_least_its_least(monkeypatch):
    """L3: each address but the last is given half of the time left, and at least ``ATTEMPT_SECONDS`` (5 s, or all that
    is left when less is); the last all of what is left. A name with one address keeps the whole connect timeout, as
    before, and the first address, the one the system prefers, is given the most. One refused at once leaves its time
    to the next; once the time is spent none is tried but the first, which always is. ``ADDRESSES_TRIED`` at most."""
    now = [100.0]
    monkeypatch.setattr(egress, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    addresses = [f"1.2.3.{n}" for n in range(1, 31)]
    count = egress.ADDRESSES_TRIED
    assert egress.ATTEMPT_SECONDS == 5.0

    def walked(found, seconds, took):
        """``tries`` of ``found`` in ``seconds``, each address taking ``took`` of the time it was given."""
        given = []
        for address, share in egress.tries(found, seconds):
            given.append((address, round(share, 6)))
            now[0] += took(share)
        return given

    refused = walked(addresses, 20.0, lambda share: 0.0)  # each refused at once
    assert refused == [*((a, 10.0) for a in addresses[: count - 1]), (addresses[count - 1], 20.0)]
    silent = walked(addresses, 20.0, lambda share: share)  # each waited out what it was given
    assert silent == [(addresses[0], 10.0), (addresses[1], 5.0), (addresses[2], 5.0)]
    assert walked(addresses[:2], 20.0, lambda share: share) == [(addresses[0], 10.0), (addresses[1], 10.0)]
    assert walked(addresses[:1], 20.0, lambda share: share) == [(addresses[0], 20.0)]  # one address: the whole time
    assert walked(addresses, 6.0, lambda share: share) == [
        (addresses[0], 5.0),
        (addresses[1], 1.0),
    ]  # its least, or less
    assert walked(addresses, 20.0, lambda share: 25.0) == [(addresses[0], 10.0)]  # the first took longer than the whole
    assert list(egress.tries(addresses[:1], 0.0)) == [(addresses[0], 0.0)]


# --- how the server's client for an identity's own destinations connects -----------------------------------------------


async def test_a_request_reaches_the_next_address_checked_when_connecting_to_the_first_is_refused(connects, lookups):
    """Windows resolves ``localhost`` to ``::1`` first, and a provider in Docker listens on 127.0.0.1 alone (Keycloak,
    OpenLDAP): the request goes to 127.0.0.1, the next address its name's lookup checked, once the connection to ``::1``
    was refused, and is sent once. The connection kept is the one to the address it was made to, and the next request
    is sent on it first (L2): the address that refused costs its time once, not on every request. No name is looked up
    again."""
    server, held = refused_on_v6(_Server)
    try:
        async with make_client(trust_env=False) as client:
            for _ in range(2):
                url = f"http://[::1]:{server.port}/v1/chat/completions"
                answer = await client.send(_request(client, url, ("::1", "127.0.0.1"), host=f"localhost:{server.port}"))
                assert answer.status_code == 200 and await answer.aread() == b"{}"
    finally:
        server.close()
        if held is not None:
            held.close()
    assert [host for host, _ in connects] == ["::1", "127.0.0.1"]  # the second request: on the kept connection
    assert server.connections == 1 and len(server.requests) == 2 and all(r.endswith(BODY) for r in server.requests)
    assert all(f"\r\nhost: localhost:{server.port}\r\n".encode() in r.lower() for r in server.requests)
    assert lookups == []


async def test_nothing_is_connected_to_but_the_addresses_the_lookup_checked(connects, lookups):
    """The addresses tried are the request's own, as its name's lookup checked them, the first of them its URL's host:
    a list another address heads is not followed, nor one with a name in it, and the request then goes to its URL's
    host alone, as does one that carries no list or a single address. Nothing is looked up again, whatever its Host
    header names."""
    server, held = refused_on_v6(_Server)
    url = f"http://[::1]:{server.port}/v1/chat/completions"
    try:
        async with make_client(trust_env=False) as client:
            for addresses in (("127.0.0.1", "::1"), ("::1", "localhost"), ("::1",), None):
                with pytest.raises(httpx.ConnectError):
                    await client.send(_request(client, url, addresses, host=f"localhost:{server.port}"))
    finally:
        server.close()
        if held is not None:
            held.close()
    assert [host for host, _ in connects] == ["::1"] * 4 and server.connections == 0 and lookups == []


async def test_no_other_address_is_tried_once_the_request_left_and_its_body_is_sent_once(connects):
    """A connection made and the request sent on it, no answer comes: the request is not sent again, at the next
    address or anywhere, and its body left once."""
    server = _Server(answer=None)
    try:
        async with make_client(trust_env=False) as client:
            url = f"http://127.0.0.1:{server.port}/v1/chat/completions"
            with pytest.raises(httpx.RemoteProtocolError):
                await client.send(_request(client, url, ("127.0.0.1", "::1")))
    finally:
        server.close()
    assert [host for host, _ in connects] == ["127.0.0.1"]
    assert server.connections == 1 and len(server.requests) == 1 and server.requests[0].endswith(BODY)


async def test_no_other_address_is_tried_when_tls_fails_on_the_connection_made(connects):
    """The TCP connection made, its TLS handshake failed (an answer that is no TLS, as a certificate that does not hold
    would): that is no connection refused, and no other address is tried."""
    server = _Server(garbage=True)
    try:
        async with make_client(trust_env=False) as client:
            url = f"https://127.0.0.1:{server.port}/v1/chat/completions"
            request = _request(client, url, ("127.0.0.1", "::1"), extensions={"sni_hostname": "gw.acme.com"})
            with pytest.raises(httpx.ConnectError):
                await client.send(request)
    finally:
        server.close()
    assert [host for host, _ in connects] == ["127.0.0.1"] and server.connections == 1


async def test_the_addresses_tried_are_bounded(monkeypatch):
    """A name may answer dozens of addresses: a request tries the first ``ADDRESSES_TRIED`` of them, no more, and is
    refused with the last failure."""
    asked = []

    async def refused(self, host, port, timeout=None, local_address=None, socket_options=None):  # noqa: ASYNC109 - httpcore's interface
        asked.append(host)
        raise httpcore.ConnectError("refused")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", refused)
    addresses = tuple(f"2a00::{n:x}" for n in range(1, 31))
    async with make_client(trust_env=False) as client:
        request = _request(
            client, "https://[2a00::1]/v1/chat/completions", addresses, extensions={"sni_hostname": "gw"}
        )
        with pytest.raises(httpx.ConnectError, match="refused"):
            await client.send(request)
    assert asked == list(addresses[: egress.ADDRESSES_TRIED])


class _Recorded(httpcore.AsyncMockStream):
    """A connection that answers 200 at once, keeping the time its TLS handshake was given."""

    def __init__(self, handshakes: list[float | None]) -> None:
        super().__init__([b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"])
        self.handshakes = handshakes

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):  # noqa: ASYNC109 - httpcore's interface
        self.handshakes.append(timeout)
        return self


async def test_each_tcp_connection_takes_its_share_of_the_connect_time_and_tls_keeps_it_whole(monkeypatch):
    """The connect timeout is shared by the TCP connections tried, each refused at once: every address but the last is
    given half of what is left (``tries``), the last all of it; the TLS handshake on the connection made is given the
    client's connect timeout, as before."""
    asked, handshakes = [], []
    count = egress.ADDRESSES_TRIED
    addresses = tuple(f"2a00::{n:x}" for n in range(1, count + 1))

    async def connect(self, host, port, timeout=None, local_address=None, socket_options=None):  # noqa: ASYNC109 - httpcore's interface
        asked.append((host, timeout))
        if host != addresses[-1]:
            raise httpcore.ConnectError("refused")
        return _Recorded(handshakes)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    async with make_client(trust_env=False) as client:
        request = _request(
            client, "https://[2a00::1]/v1/chat/completions", addresses, extensions={"sni_hostname": "gw.acme.com"}
        )
        answer = await client.send(request)
        assert answer.status_code == 200
        await answer.aclose()
    connect_timeout = client.timeout.connect
    assert connect_timeout is not None and [host for host, _ in asked] == list(addresses)
    shares = [timeout for _, timeout in asked]
    expected = [connect_timeout / 2] * (count - 1) + [connect_timeout]
    assert all(s is not None and abs(s - e) < 0.05 for s, e in zip(shares, expected, strict=True))
    assert handshakes == [connect_timeout]


@pytest.mark.timing
async def test_the_addresses_that_do_not_answer_take_the_connect_timeout_together(monkeypatch):
    """None of the addresses answers within its share: the request is refused when the connect timeout is spent, the
    addresses tried in their order, none past it (the least share made small, for a test of a second)."""
    asked = []
    monkeypatch.setattr(egress, "ATTEMPT_SECONDS", 0.1)

    async def silent(self, host, port, timeout=None, local_address=None, socket_options=None):  # noqa: ASYNC109 - httpcore's interface
        asked.append(host)
        await asyncio.sleep(timeout)
        raise httpcore.ConnectTimeout("timed out")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", silent)
    addresses = tuple(f"2a00::{n:x}" for n in range(1, 31))
    async with make_client(trust_env=False) as client:
        request = _request(client, "https://[2a00::1]/v1", addresses, timeout=httpx.Timeout(10.0, connect=1.0))
        started = asyncio.get_running_loop().time()
        with pytest.raises(httpx.ConnectTimeout):
            await client.send(request)
        took = asyncio.get_running_loop().time() - started
    assert 0.9 <= took < 2.5 and 3 <= len(asked) <= egress.ADDRESSES_TRIED and asked == list(addresses[: len(asked)])


async def test_a_request_never_takes_a_connection_kept_at_an_address_outside_its_own_list(monkeypatch):
    """L6: a connection kept at 127.0.0.1 serves only a request whose own list holds 127.0.0.1. One whose list is
    ``::1`` alone, or ``::1`` and an address on the internet, or that carries none, is refused without touching it; one
    whose list holds 127.0.0.1 is sent on it at once, nothing connected first (L2)."""
    asked: list[str] = []
    connect = httpcore.AnyIOBackend.connect_tcp

    async def loopback(self, host, port, timeout=None, local_address=None, socket_options=None):  # noqa: ASYNC109 - httpcore's interface
        asked.append(host)
        if host != "127.0.0.1":  # refused at once: ::1, and the internet no test reaches
            raise httpcore.ConnectError("refused")
        return await connect(self, host, port, timeout, local_address, socket_options)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", loopback)
    server = _Server()
    url = f"http://[::1]:{server.port}/v1/chat/completions"
    try:
        async with make_client(trust_env=False) as client:
            kept = await client.send(_request(client, url, ("::1", "127.0.0.1")))
            assert kept.status_code == 200 and await kept.aread() == b"{}" and asked == ["::1", "127.0.0.1"]
            asked.clear()
            for addresses in (("::1",), None, ("::1", "2a00::5")):
                with pytest.raises(httpx.ConnectError):
                    await client.send(_request(client, url, addresses))
            assert asked == ["::1", "::1", "::1", "2a00::5"] and server.connections == 1 and len(server.requests) == 1
            asked.clear()
            again = await client.send(_request(client, url, ("::1", "127.0.0.1")))
            assert again.status_code == 200 and await again.aread() == b"{}"
    finally:
        server.close()
    assert asked == [] and server.connections == 1 and len(server.requests) == 2


def _certificate(name: str, folder) -> str:
    """A self-signed certificate for ``name`` alone, with its key, in ``folder`` (``cert.pem``, ``key.pem``): its PEM."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    (folder / "cert.pem").write_bytes(pem)
    unlocked = serialization.NoEncryption()
    (folder / "key.pem").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, unlocked)
    )
    return pem.decode("ascii")


async def test_a_connection_kept_for_one_tls_name_never_serves_another_at_the_same_address(tmp_path):
    """L1: the provider's certificate holds for a.example alone. A connection kept for a.example at 127.0.0.1 never
    serves a request for b.example there, whether b's list holds that address alone or comes to it after ``::1``
    refused: each makes a connection of its own, whose TLS refuses b.example, and nothing of b's request is sent."""
    pem = _certificate("a.example", tmp_path)
    served = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    served.load_cert_chain(tmp_path / "cert.pem", tmp_path / "key.pem")
    server, held = refused_on_v6(lambda: _Server(tls=served))
    port = server.port
    trusted = ssl.create_default_context(cadata=pem)
    reaching = request_transport.reaching(trusted)
    try:
        async with httpx.AsyncClient(transport=reaching, **client_options(trust_env=False)) as client:
            a = _request(client, f"https://127.0.0.1:{port}/v1", ("127.0.0.1",), host="a.example")
            a.extensions["sni_hostname"] = "a.example"
            answered = await client.send(a)
            assert answered.status_code == 200 and await answered.aread() == b"{}"
            for url, addresses in (
                (f"https://127.0.0.1:{port}/v1", ("127.0.0.1",)),
                (f"https://[::1]:{port}/v1", ("::1", "127.0.0.1")),
            ):
                b = _request(client, url, addresses, host="b.example")
                b.extensions["sni_hostname"] = "b.example"
                with pytest.raises(httpx.ConnectError):
                    await client.send(b)
    finally:
        server.close()
        if held is not None:
            held.close()
    assert server.connections == 3 and len(server.requests) == 1 and b"\r\nhost: a.example\r\n" in server.requests[0]


async def test_an_identity_holds_no_more_connections_than_its_one_pool_did_whatever_its_tls_names(monkeypatch):
    """One identity's client holds 100 connections at most, the limit its one pool had before connections were kept
    for their TLS name, however many names it sends to: 150 requests to 50 names, each holding its connection while
    its answer is open, have 100 connections at once, and the other 50 wait for one, as on that one pool, and are
    answered once the first answers close."""
    open_now = most = 0

    class _Counted(httpcore.AsyncMockStream):
        """A connection answering 200, counted while it is open."""

        def __init__(self) -> None:
            super().__init__([OK])
            self.counted = True

        async def aclose(self) -> None:
            nonlocal open_now
            if self.counted:
                self.counted, open_now = False, open_now - 1
            await super().aclose()

    async def counted(self, host, port, timeout=None, local_address=None, socket_options=None):  # noqa: ASYNC109 - httpcore's interface
        nonlocal open_now, most
        open_now += 1
        most = max(most, open_now)
        return _Counted()

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", counted)
    async with make_client(trust_env=False) as client:
        requests = []
        for n in range(150):
            name = f"gw{n % 50}.acme.com"
            request = _request(client, "https://[2a00::1]/v1", ("2a00::1", "2a00::2"), host=name)
            request.extensions["sni_hostname"] = name
            requests.append(request)
        sending = [asyncio.create_task(client.send(request, stream=True)) for request in requests]
        await asyncio.wait(sending, timeout=1.0)  # time for every request to get a connection, were it to
        first = [task for task in sending if task.done()]
        assert most == 100 and len(first) == 100  # the other 50 wait
        for task in first:
            await task.result().aclose()
        answers = await asyncio.wait_for(asyncio.gather(*sending), 10)
        assert [answer.status_code for answer in answers] == [200] * 150
        for answer in answers:
            await answer.aclose()
    assert most == 100 and request_transport.LIMITS.max_connections == 100


# --- a WebSocket ---------------------------------------------------------------------------------------------------------


async def test_a_websocket_is_connected_at_the_addresses_checked_for_its_name():
    """The relay hands the WebSocket adapter the addresses the lookup checked, in their order, and the port: the URL's
    host alone when no list of them comes with it."""

    async def pin(url):
        assert url == "https://gw.acme.com:8443/v1/responses"
        extensions = {"sni_hostname": "gw.acme.com", egress.ADDRESSES: ("::1", "127.0.0.1")}
        return "https://[::1]:8443/v1/responses", "gw.acme.com:8443", extensions

    async def single(url):
        return "https://127.0.0.1/v1/responses", "gw.acme.com", {"sni_hostname": "gw.acme.com"}

    assert await ws._pinned(pin, "wss://gw.acme.com:8443/v1/responses") == (("::1", "127.0.0.1"), 8443)
    assert await ws._pinned(single, "wss://gw.acme.com/v1/responses") == (("127.0.0.1",), 443)
