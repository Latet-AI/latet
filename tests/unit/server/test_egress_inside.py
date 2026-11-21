"""The addresses an administrator's own service may lead to (E3's identity provider): the company's own network, never
the cloud's metadata address or another link-local one, the server's loopback only when the address names it
(latet.server.egress, inside=True)."""

import ipaddress

import pytest

from latet.server import egress
from latet.server.egress import EgressRefused


def _resolving(*addresses):
    return lambda host, port: [ipaddress.ip_address(address) for address in addresses]


@pytest.mark.parametrize("address", ["10.0.3.9", "172.20.1.5", "192.168.10.4", "100.64.2.1", "fd12:3456:789a::5"])
def test_the_companys_own_network_is_allowed_inside_and_refused_for_an_identitys_own_destination(address):
    found = ipaddress.ip_address(address)
    assert not egress.forbidden(found, inside=True) and egress.forbidden(found)


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "fe80::1",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
        "224.0.0.1",
        "::ffff:169.254.169.254",
        "fe80::1%2",
        "::ffff:10.0.3.9",  # an IPv6 form that carries an IPv4 address: never, whatever it carries
        "64:ff9b::a00:309",
        "fd12::5%2",  # a zone picks an interface of the server's
        "240.0.0.1",
        "100.100.100.200",  # Alibaba Cloud's metadata, inside 100.64.0.0/10
        "fd00:ec2::254",  # AWS's IPv6 metadata, inside fc00::/7
    ],
)
def test_the_metadata_address_link_local_loopback_and_the_rest_stay_refused_inside(address):
    assert egress.forbidden(ipaddress.ip_address(address), inside=True)


async def test_a_name_is_pinned_inside_and_one_pointed_at_the_metadata_address_since_is_refused(monkeypatch):
    monkeypatch.setattr(egress, "resolve", _resolving("10.0.3.9"))
    url, host, extensions = await egress.pin("https://sso.acme.lan/realms/acme", holder="sso", inside=True)
    assert (url, host) == ("https://10.0.3.9/realms/acme", "sso.acme.lan")
    assert extensions == {"sni_hostname": "sso.acme.lan", egress.ADDRESSES: ("10.0.3.9",)}
    monkeypatch.setattr(egress, "resolve", _resolving("169.254.169.254"))
    with pytest.raises(EgressRefused):
        await egress.pin("https://sso2.acme.lan/realms/acme", holder="sso", inside=True)


async def test_the_company_network_is_refused_without_inside(monkeypatch):
    monkeypatch.setattr(egress, "resolve", _resolving("10.0.3.9"))
    with pytest.raises(EgressRefused):
        await egress.pin("https://sso3.acme.lan/realms/acme", holder="sso")


async def test_loopback_only_when_the_address_names_it(monkeypatch):
    monkeypatch.setattr(egress, "resolve", _resolving("127.0.0.1"))
    pinned = await egress.pin("http://localhost:8080/realms/acme", holder="sso", inside=True)
    assert pinned[0] == "http://127.0.0.1:8080/realms/acme"
    literal = await egress.pin("http://127.0.0.1:8080/realms/acme", holder="sso", inside=True)
    assert literal[0] == "http://127.0.0.1:8080/realms/acme"
    with pytest.raises(EgressRefused):  # a name that resolves to the server's own loopback
        await egress.pin("https://sso4.acme.lan/realms/acme", holder="sso", inside=True)
    with pytest.raises(EgressRefused):  # numbers the resolver reads as loopback, not written as it
        await egress.pin("http://127.1:8080/realms/acme", holder="sso", inside=True)
    with pytest.raises(EgressRefused):  # and never for an identity's own destination
        await egress.pin("http://localhost:8080/v1", holder="u_emilycartr")


async def test_a_name_of_loopback_that_resolves_elsewhere_too_is_refused(monkeypatch):
    monkeypatch.setattr(egress, "resolve", _resolving("127.0.0.1", "169.254.169.254"))
    with pytest.raises(EgressRefused):
        await egress.pin("http://localhost:8081/realms/acme", holder="sso", inside=True)
    monkeypatch.setattr(egress, "resolve", _resolving("10.0.3.9"))
    with pytest.raises(EgressRefused):  # localhost is this computer's alone
        await egress.pin("http://localhost:8082/realms/acme", holder="sso", inside=True)


async def test_an_administrators_own_service_is_handed_every_address_its_lookup_checked_inside(monkeypatch):
    """The addresses a connection to an administrator's own service may try (``egress.ADDRESSES``) are the same
    lookup's, checked by the rules inside: loopback ones alone for ``localhost``, in the order the system gave them
    (Windows gives ``::1`` first), the company's own network's for its names; one address the server never reaches
    among them refuses the name whole, and none is handed."""
    monkeypatch.setattr(egress, "resolve", _resolving("::1", "127.0.0.1"))
    url, host, extensions = await egress.pin("http://localhost:8080/realms/acme", holder="sso", inside=True)
    assert (url, host) == ("http://[::1]:8080/realms/acme", "localhost:8080")
    assert extensions == {egress.ADDRESSES: ("::1", "127.0.0.1")}
    monkeypatch.setattr(egress, "resolve", _resolving("10.0.3.9", "fd12:3456:789a::5"))
    extensions = (await egress.pin("https://sso5.acme.lan/realms/acme", holder="sso", inside=True))[2]
    assert extensions[egress.ADDRESSES] == ("10.0.3.9", "fd12:3456:789a::5")
    for answer, url in ((("::1", "127.0.0.1", "169.254.169.254"), "http://localhost:8083/realms/acme"),
                        (("10.0.3.9", "169.254.169.254"), "https://sso6.acme.lan/realms/acme")):  # fmt: skip
        monkeypatch.setattr(egress, "resolve", _resolving(*answer))
        with pytest.raises(EgressRefused):
            await egress.pin(url, holder="sso", inside=True)
