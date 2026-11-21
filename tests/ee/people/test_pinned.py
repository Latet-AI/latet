"""The identity provider is reached only at the address checked at each connection (ee/oidc.py: PinnedTransport, over
latet.server.egress), this computer only at an issuer's own origin when the issuer is on it, and each request within a
total time."""

import asyncio
import ipaddress

import httpx
import pytest

from latet.ee import oidc
from latet.ee.oidc import PinnedTransport, SignInError
from latet.server import egress

from ..addresses import (
    LOCALHOST,
    METADATA,
    PAST,
    Server,
    answering,
    async_connects,
    certificate,
    refused_on_v6,
    resolving,
    serving,
    trusting,
)

ISSUER = "https://sso.acme.lan/realms/acme"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"


@pytest.fixture
def sent(monkeypatch):
    """What the transport underneath was handed: nothing leaves the process."""
    seen = []

    async def handle(transport, request):
        seen.append((str(request.url), request.headers["host"], request.extensions.get("sni_hostname")))
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    return seen


def _resolving(monkeypatch, *addresses):
    asked = []

    def resolve(host, port):
        asked.append(host)
        return [ipaddress.ip_address(address) for address in addresses]

    monkeypatch.setattr(egress, "resolve", resolve)
    return asked


async def test_each_request_goes_to_the_address_checked_with_the_providers_host_and_tls_name(monkeypatch, sent):
    _resolving(monkeypatch, "10.0.3.9")
    async with httpx.AsyncClient(transport=PinnedTransport(ISSUER)) as http:
        await http.get(DISCOVERY)
    assert sent == [("https://10.0.3.9/realms/acme/.well-known/openid-configuration", "sso.acme.lan", "sso.acme.lan")]


@pytest.mark.parametrize("address", ["169.254.169.254", "127.0.0.1", "fe80::1", "::ffff:10.0.3.9", "0.0.0.0"])
async def test_a_provider_pointed_at_the_metadata_address_or_this_computer_is_never_reached(monkeypatch, sent, address):
    _resolving(monkeypatch, address)
    async with httpx.AsyncClient(transport=PinnedTransport("https://sso2.acme.lan/realms/acme")) as http:
        with pytest.raises(httpx.ConnectError, match="refused"):
            await http.get("https://sso2.acme.lan/realms/acme/.well-known/openid-configuration")
    assert sent == []


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6379/certs",  # Valkey, say
        "https://localhost/certs",
        "https://127.1/certs",  # numbers the resolver reads as this computer
        "https://2130706433/certs",
        "https://[::1]/certs",
    ],
)
async def test_this_computer_is_never_reached_for_an_issuer_elsewhere(monkeypatch, sent, url):
    asked = _resolving(monkeypatch, "127.0.0.1")
    async with httpx.AsyncClient(transport=PinnedTransport(ISSUER)) as http:
        with pytest.raises(httpx.ConnectError, match="refused"):
            await http.get(url)
    assert sent == [] and asked == []  # refused before any lookup


async def test_an_issuer_on_this_computer_is_reached_at_its_own_origin_alone(monkeypatch, sent):
    _resolving(monkeypatch, "127.0.0.1")
    keycloak = "http://localhost:8080/realms/acme"
    async with httpx.AsyncClient(transport=PinnedTransport(keycloak)) as http:
        await http.get(keycloak + "/.well-known/openid-configuration")
        for elsewhere in ("http://localhost:6379/certs", "http://127.0.0.1:8080/certs", "https://localhost:8080/c"):
            with pytest.raises(httpx.ConnectError, match="refused"):
                await http.get(elsewhere)
    assert sent == [("http://127.0.0.1:8080/realms/acme/.well-known/openid-configuration", "localhost:8080", None)]


async def test_a_provider_that_takes_too_long_is_refused_whatever_it_sends(monkeypatch):
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(oidc, "REQUEST_SECONDS", 0.2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as http:
        with pytest.raises(SignInError) as caught:
            await oidc.discover(http, ISSUER)
    assert caught.value.code == "unavailable" and "did not answer within" in caught.value.detail


async def test_a_provider_that_trickles_its_answer_is_refused_within_the_total_time(monkeypatch):
    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.05)
                yield b" "

    def handler(request):
        return httpx.Response(200, stream=Trickle(), request=request)

    monkeypatch.setattr(oidc, "REQUEST_SECONDS", 0.3)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(SignInError, match="did not answer within"):
            await oidc.discover(http, ISSUER)


# --- A provider at a name with several addresses: the next one checked, when the connection to the one before failed --


def _issuer_at(server: Server, scheme: str = "https") -> str:
    return f"{scheme}://localhost:{server.port}/realms/acme"


def _client(issuer: str) -> httpx.AsyncClient:
    """The client the server reaches the provider with (``Oidc._client``)."""
    return httpx.AsyncClient(transport=PinnedTransport(issuer), timeout=15.0, trust_env=False, follow_redirects=False)


@pytest.fixture
def trusted(monkeypatch, tmp_path):
    """A provider's certificate for ``localhost`` alone, which the server's own store (truststore) trusts here."""
    cert, key = certificate(tmp_path)
    monkeypatch.setattr(oidc.truststore, "SSLContext", lambda protocol: trusting(cert))
    return serving(cert, key)


async def test_a_provider_on_this_computer_is_reached_at_the_next_address_its_lookup_checked(monkeypatch, trusted):
    """Windows resolves ``localhost`` to ``::1`` first, and Keycloak in Docker listens on 127.0.0.1 alone (the release
    checklist's item 4): the issuer `https://localhost:…` is reached at 127.0.0.1 once ``::1`` refused the connection,
    its Host and TLS's name ``localhost`` (the certificate names nothing else), and nothing is tried after it."""
    server = Server(answering(b'{"issuer":"acme"}'), tls=trusted)
    asked = resolving(monkeypatch, *LOCALHOST)
    connects = async_connects(monkeypatch)
    issuer = _issuer_at(server)
    try:
        with refused_on_v6(server.port):
            async with _client(issuer) as http:
                answer = await http.get(issuer + "/.well-known/openid-configuration")
    finally:
        server.close()
    assert answer.status_code == 200 and answer.json() == {"issuer": "acme"}
    assert connects == ["::1", "127.0.0.1"] and asked == ["localhost"]
    assert server.connections == 1 and len(server.requests) == 1
    assert f"\r\nhost: localhost:{server.port}\r\n".encode() in server.requests[0].lower()


async def test_a_provider_is_connected_at_no_address_its_lookup_did_not_check(monkeypatch):
    """The addresses tried are those egress's lookup checked, in its order: a list the request carries of its own is
    not followed (egress's takes its place); a name with an address the server never reaches among its answers is
    refused whole, nothing connected, this computer's as the company's."""
    server = Server(answering(b"{}"))
    connects = async_connects(monkeypatch, refused=("::1", PAST))
    issuer = _issuer_at(server, "http")
    discovery = issuer + "/.well-known/openid-configuration"
    try:
        resolving(monkeypatch, *LOCALHOST)
        async with _client(issuer) as http:
            answer = await http.get(discovery, extensions={egress.ADDRESSES: ("::1", PAST)})
        assert answer.status_code == 200 and connects == ["::1", "127.0.0.1"]
        connects.clear()
        resolving(monkeypatch, "::1", "127.0.0.1", METADATA)
        async with _client(issuer) as http:
            with pytest.raises(httpx.ConnectError, match="refused"):
                await http.get(discovery)
        resolving(monkeypatch, "10.0.3.9", METADATA)
        async with _client(ISSUER) as http:
            with pytest.raises(httpx.ConnectError, match="refused"):
                await http.get(DISCOVERY)
    finally:
        server.close()
    assert connects == [] and len(server.requests) == 1


async def test_a_provider_is_asked_at_no_other_address_once_a_connection_to_it_was_made(monkeypatch, trusted, tmp_path):
    """Reached at 127.0.0.1, the provider reads the request and closes the connection without an answer: the request
    is not sent again, at the next address or anywhere. Nor is another address tried when TLS fails on the connection
    made (a certificate for another name)."""
    connects = async_connects(monkeypatch, refused=("::1", PAST))
    resolving(monkeypatch, *LOCALHOST)
    silent = Server(None, tls=trusted)
    try:
        async with _client(_issuer_at(silent)) as http:
            with pytest.raises(httpx.RemoteProtocolError):
                await http.get(_issuer_at(silent) + "/.well-known/openid-configuration")
    finally:
        silent.close()
    assert connects == ["::1", "127.0.0.1"] and silent.connections == 1 and len(silent.requests) == 1
    connects.clear()
    elsewhere = Server(answering(b"{}"), tls=serving(*certificate(tmp_path / "elsewhere", "elsewhere.acme.lan")))
    try:
        async with _client(_issuer_at(elsewhere)) as http:
            with pytest.raises(httpx.ConnectError):
                await http.get(_issuer_at(elsewhere) + "/.well-known/openid-configuration")
    finally:
        elsewhere.close()
    assert connects == ["::1", "127.0.0.1"] and elsewhere.requests == []
