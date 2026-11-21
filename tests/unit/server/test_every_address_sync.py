"""In a worker thread, a server's own fetch reaches a name at any of the addresses its lookup checked
(``request_transport.reaching_sync``, an HTTP client's pool, and ``connected``, a socket's: Latet Enterprise's term sync
and syslog), as a destination of an identity's own is (test_every_address.py): in their order, the next one only when
the TCP connection to the one before failed, before any byte left; never an address the lookup did not check, never a
name looked up, never another once a connection was made, a bounded number of them in a bounded time; a connection
kept for its address and TLS name alone."""

import socket
import ssl

import httpcore
import httpx
import pytest

from latet.gateway import request_transport
from latet.server import egress

from .conftest import refused_on_v6
from .test_every_address import BODY, _certificate, _Server

ANSWERED = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"


def _client(context: ssl.SSLContext | None = None, backend: httpcore.NetworkBackend | None = None) -> httpx.Client:
    """An httpx client whose pool is ``reaching_sync``'s, each phase within 30 seconds, as Latet Enterprise's term sync
    makes one."""
    transport = httpx.HTTPTransport(retries=0)
    transport._pool = request_transport.reaching_sync(context or ssl.create_default_context(), backend)
    return httpx.Client(transport=transport, timeout=30.0, trust_env=False, follow_redirects=False)


def _request(client, url, addresses, *, host="intranet.acme.com", name=None, **options):
    """A POST of ``BODY`` to ``url`` that carries ``addresses`` as egress's checked ones (none: no such list), and TLS's
    name ``name``."""
    extensions = {} if addresses is None else {egress.ADDRESSES: addresses}
    if name is not None:
        extensions["sni_hostname"] = name
    headers = {"host": host, "content-type": "application/json"}
    return client.build_request("POST", url, headers=headers, content=BODY, extensions=extensions, **options)


@pytest.fixture
def connects(monkeypatch):
    """Every TCP connection a synchronous client asks httpcore's backend for, by its address: one to ``::1`` refused at
    once when ``refusing`` holds it (``connects.refusing``), the rest made as asked."""
    asked: list[str] = []
    refusing: set[str] = set()
    connect = httpcore.SyncBackend.connect_tcp

    def recorded(self, host, port, timeout=None, local_address=None, socket_options=None):
        asked.append(host)
        if host in refusing:
            raise httpcore.ConnectError("refused")
        return connect(self, host, port, timeout, local_address, socket_options)

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", recorded)
    return asked, refusing


@pytest.fixture
def lookups(monkeypatch):
    """Every name the system is asked to resolve (an address written as a number is none)."""
    asked: list[str] = []
    resolve = socket.getaddrinfo

    def recorded(host, *args, **kwargs):
        text = host.decode() if isinstance(host, bytes) else str(host)
        if egress.literal(text) is None:
            asked.append(text)
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", recorded)
    return asked


# --- an HTTP client's pool ------------------------------------------------------------------------------------------


def test_a_request_reaches_the_next_address_checked_when_connecting_to_the_first_is_refused(connects, lookups):
    """A source in Docker listens on 127.0.0.1 alone, and Windows's ``localhost`` is ``::1`` first: the request goes to
    127.0.0.1 once the connection to ``::1`` was refused, and is sent once; the next request is sent on the connection
    kept there, nothing connected first. No name is looked up."""
    asked, _ = connects
    server, held = refused_on_v6(_Server)
    try:
        with _client() as client:
            for _ in range(2):
                url = f"http://[::1]:{server.port}/api/projects"
                answer = client.send(_request(client, url, ("::1", "127.0.0.1"), host=f"localhost:{server.port}"))
                assert answer.status_code == 200 and answer.read() == b"{}"
    finally:
        server.close()
        if held is not None:
            held.close()
    assert asked == ["::1", "127.0.0.1"] and lookups == []
    assert server.connections == 1 and len(server.requests) == 2 and all(r.endswith(BODY) for r in server.requests)
    assert all(f"\r\nhost: localhost:{server.port}\r\n".encode() in r.lower() for r in server.requests)


def test_nothing_is_connected_to_but_the_addresses_the_lookup_checked(connects, lookups):
    """A list another address heads is not followed, nor one with a name in it: the request goes to its URL's host
    alone then, as one that carries no list or a single address does. Nothing is looked up, whatever its Host names."""
    asked, refusing = connects
    refusing.add("::1")
    server = _Server()
    url = f"http://[::1]:{server.port}/api/projects"
    try:
        with _client() as client:
            for addresses in (("127.0.0.1", "::1"), ("::1", "localhost"), ("::1",), None):
                with pytest.raises(httpx.ConnectError):
                    client.send(_request(client, url, addresses, host=f"localhost:{server.port}"))
    finally:
        server.close()
    assert asked == ["::1"] * 4 and server.connections == 0 and lookups == []


def test_no_other_address_is_tried_once_the_request_left_nor_when_tls_fails_on_the_connection_made(connects):
    """A connection made and the request sent on it, no answer comes: it is not sent again, its body left once. A TCP
    connection made whose TLS handshake fails tries no other address either."""
    asked, _ = connects
    silent = _Server(answer=None)
    try:
        with _client() as client:
            url = f"http://127.0.0.1:{silent.port}/api/projects"
            with pytest.raises(httpx.RemoteProtocolError):
                client.send(_request(client, url, ("127.0.0.1", "::1")))
    finally:
        silent.close()
    assert asked == ["127.0.0.1"] and silent.connections == 1 and len(silent.requests) == 1
    asked.clear()
    garbled = _Server(garbage=True)
    try:
        with _client() as client:
            url = f"https://127.0.0.1:{garbled.port}/api/projects"
            with pytest.raises(httpx.ConnectError):
                client.send(_request(client, url, ("127.0.0.1", "::1"), name="intranet.acme.com"))
    finally:
        garbled.close()
    assert asked == ["127.0.0.1"] and garbled.connections == 1


def test_a_request_never_takes_a_connection_kept_at_an_address_outside_its_own_list(monkeypatch):
    """test_every_address.py's test of the same name, for the synchronous pool: a connection kept at 127.0.0.1 serves
    only a request whose own list holds 127.0.0.1. One whose list is ``::1`` alone, or ``::1`` and 127.0.0.2, or that
    carries none, is refused without touching it; one whose list holds 127.0.0.1 is sent on it at once, nothing
    connected first. The rule held before this test was written (each attempt is kept under its own address, ``_at``,
    and ``_kept_first`` looks at the request's own list alone): it was added for the security review of the merge of
    0.12.1 (L2), and it fails when every attempt is kept under its URL's host instead."""
    asked: list[str] = []
    connect = httpcore.SyncBackend.connect_tcp

    def loopback(self, host, port, timeout=None, local_address=None, socket_options=None):
        asked.append(host)
        if host != "127.0.0.1":  # refused at once: ::1, and 127.0.0.2, which no test reaches
            raise httpcore.ConnectError("refused")
        return connect(self, host, port, timeout, local_address, socket_options)

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", loopback)
    server = _Server()
    url = f"http://[::1]:{server.port}/api/projects"
    try:
        with _client() as client:
            kept = client.send(_request(client, url, ("::1", "127.0.0.1")))
            assert kept.status_code == 200 and kept.read() == b"{}" and asked == ["::1", "127.0.0.1"]
            asked.clear()
            for addresses in (("::1",), None, ("::1", "127.0.0.2")):
                with pytest.raises(httpx.ConnectError):
                    client.send(_request(client, url, addresses))
            assert asked == ["::1", "::1", "::1", "127.0.0.2"] and server.connections == 1 and len(server.requests) == 1
            asked.clear()
            again = client.send(_request(client, url, ("::1", "127.0.0.1")))
            assert again.status_code == 200 and again.read() == b"{}"
    finally:
        server.close()
    assert asked == [] and server.connections == 1 and len(server.requests) == 2


def test_a_connection_kept_for_one_tls_name_never_serves_another_at_the_same_address(tmp_path):
    """The source's certificate holds for a.example alone: a connection kept for a.example at 127.0.0.1 never serves a
    request for b.example there, whether b's list holds that address alone or comes to it after ``::1`` refused; each
    makes a connection of its own, whose TLS refuses b.example, and nothing of b's request is sent."""
    pem = _certificate("a.example", tmp_path)
    served = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    served.load_cert_chain(tmp_path / "cert.pem", tmp_path / "key.pem")
    server, held = refused_on_v6(lambda: _Server(tls=served))
    port = server.port
    try:
        with _client(ssl.create_default_context(cadata=pem)) as client:
            a = _request(client, f"https://127.0.0.1:{port}/v1", ("127.0.0.1",), host="a.example", name="a.example")
            answered = client.send(a)
            assert answered.status_code == 200 and answered.read() == b"{}"
            for url, addresses in ((f"https://127.0.0.1:{port}/v1", ("127.0.0.1",)),
                                   (f"https://[::1]:{port}/v1", ("::1", "127.0.0.1"))):  # fmt: skip
                with pytest.raises(httpx.ConnectError):
                    client.send(_request(client, url, addresses, host="b.example", name="b.example"))
    finally:
        server.close()
        if held is not None:
            held.close()
    assert server.connections == 3 and len(server.requests) == 1 and b"\r\nhost: a.example\r\n" in server.requests[0]


class _Recorded(httpcore.MockStream):
    """A connection that answers 200 at once, keeping the time its TLS handshake was given."""

    def __init__(self, handshakes: list[float | None]) -> None:
        super().__init__([ANSWERED])
        self.handshakes = handshakes

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.handshakes.append(timeout)
        return self


def test_the_addresses_tried_are_bounded_and_each_takes_its_share_of_the_connect_time(monkeypatch):
    """A name may answer dozens of addresses: the first ``ADDRESSES_TRIED`` are tried, no more. Each refused at once,
    every address but the last is given half of the connect time left (``egress.tries``), the last all of it; the TLS
    handshake on the connection made keeps the client's connect timeout."""
    asked: list[tuple[str, float | None]] = []
    handshakes: list[float | None] = []
    count = egress.ADDRESSES_TRIED
    addresses = tuple(f"2a00::{n:x}" for n in range(1, 31))
    answering = [addresses[count - 1]]

    def connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        asked.append((host, timeout))
        if host not in answering:
            raise httpcore.ConnectError("refused")
        return _Recorded(handshakes)

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    with _client() as client:
        answer = client.send(_request(client, "https://[2a00::1]/v1", addresses, name="intranet.acme.com"))
        assert answer.status_code == 200
        answer.close()
        connect_timeout = client.timeout.connect
        assert connect_timeout is not None
        assert [host for host, _ in asked] == list(addresses[:count])
        expected = [connect_timeout / 2] * (count - 1) + [connect_timeout]
        assert all(s is not None and abs(s - e) < 0.05 for (_, s), e in zip(asked, expected, strict=True))
        assert handshakes == [connect_timeout]
    asked.clear()
    answering.clear()
    with _client() as client, pytest.raises(httpx.ConnectError, match="refused"):  # none answers: the last refusal
        client.send(_request(client, "https://[2a00::1]/v1", addresses, name="intranet.acme.com"))
    assert [host for host, _ in asked] == list(addresses[:count])


# --- a socket's connection -------------------------------------------------------------------------------------------


def test_a_socket_reaches_the_next_address_checked_and_never_a_name(lookups):
    """``connected`` makes its connection to 127.0.0.1 once ``::1`` refused it, and never connects to a name among the
    addresses, nor looks it up; with none that answers, the last failure is raised."""
    server, held = refused_on_v6(_Server)
    try:
        with request_transport.connected(("::1", "localhost", "127.0.0.1"), server.port, 10.0) as made:
            assert made.getpeername()[:2] == ("127.0.0.1", server.port)
    finally:
        server.close()
    try:
        with pytest.raises(OSError, match="no address written as a number"):
            request_transport.connected(("localhost",), server.port, 1.0)
        with pytest.raises(OSError) as refused:  # noqa: PT011 - refused, or not made in time: in the system's words
            request_transport.connected(("::1",), server.port, 1.0)
    finally:
        if held is not None:
            held.close()
    assert "no address" not in str(refused.value) and lookups == [] and server.connections == 1


def test_a_socket_tries_a_bounded_number_of_addresses_each_with_its_share_of_the_time(monkeypatch):
    """The first ``ADDRESSES_TRIED`` addresses are tried, each refused at once given half of the time left, the last
    all of it; once one takes the connection, no other is tried."""
    asked: list[tuple[str, float]] = []
    count = egress.ADDRESSES_TRIED
    addresses = tuple(f"2a00::{n:x}" for n in range(1, 31))
    answering: list[str] = []
    made = object()

    def connect(address, timeout=None, *args, **kwargs):
        asked.append((address[0], timeout))
        if address[0] not in answering:
            raise ConnectionRefusedError("refused")
        return made

    monkeypatch.setattr(socket, "create_connection", connect)
    with pytest.raises(ConnectionRefusedError):
        request_transport.connected(addresses, 6514, 20.0)
    assert [address for address, _ in asked] == list(addresses[:count])
    expected = [10.0] * (count - 1) + [20.0]
    assert all(abs(s - e) < 0.05 for (_, s), e in zip(asked, expected, strict=True))
    asked.clear()
    answering.append(addresses[1])
    assert request_transport.connected(addresses, 6514, 20.0) is made
    assert [address for address, _ in asked] == list(addresses[:2])
