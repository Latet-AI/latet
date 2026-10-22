"""Shared settings and an identity's own: the shared lists with its own items added, a shared item never removed by it,
anything else its own when it set it, and kept so while it has the value it gave; identity and projects its own only;
what would reach the server's files, keys or network the server's: a destination of its own leads neither to the server
itself nor into its network, by number or by name, at save and at connect, and one identity's names that do not answer
hold none of the lookups another identity needs."""

import asyncio
import collections
import ipaddress
import threading
import time
import types

import pytest
from hypothesis import given
from hypothesis import strategies as st

from latet import protection
from latet.config import Config, FieldRule, IdentityConfig, LlmReview, Profile, ProjectEntry, TermConfig
from latet.engine.detectors.terms import TERM_LONGEST
from latet.protection import loosened
from latet.server import egress
from latet.server.egress import NOT_PUBLIC, UNREADABLE, EgressRefused
from latet.server.shared import (
    ON_SERVER,
    OWN_KEYS,
    OWN_RULES,
    RULE_KEYS,
    RULES_FOR_EVERYONE,
    SETTING_KEYS,
    SettingsRefused,
    SharedItemsError,
    in_force,
    kept_rules,
    kept_said,
    layered,
    own_destinations_refused,
    own_part,
    personal_destination_problem,
    pinned_reviewer,
    removed_shared,
    shared_refusals,
    with_kept_rules,
)

from .conftest import SHARED, URL, MemorySettings

# Addresses on the internet. (The documentation ranges, 203.0.113.0/24 among them, are refused: a network may use them
# inside.)
PUBLIC, PUBLIC_V6 = "1.2.3.4", "2a00::1"
# The shared settings with a reviewer the administrator set up (``SHARED``'s is not on).
REVIEWED = SHARED.model_copy(update={"llm_review": SHARED.llm_review.model_copy(update={"enabled": True})})


def _resolving(*addresses):
    """A resolver that answers these addresses for any name."""
    return lambda host, port: [ipaddress.ip_address(a) for a in addresses]


async def _until(check, seconds=5.0):
    """Wait, without holding the event loop, until ``check()`` is true; fail when it is not within ``seconds``."""
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "it did not come true in time"
        await asyncio.sleep(0.01)


def test_an_identity_adds_to_the_shared_lists_and_cannot_remove_from_them():
    mine = layered(SHARED, {"terms": [{"value": "Mercury Labs", "type": "CLIENT"}], "allow_values": []})
    assert [t.value for t in mine.terms] == ["Project Falcon", "Mercury Labs"]
    assert mine.allow_values == ["support@acme.com"]


def test_an_identitys_copy_of_a_shared_item_is_the_shared_one():
    mine = layered(SHARED, {"terms": [{"value": "Project Falcon", "type": "CLIENT"}]})
    assert [(t.value, t.type) for t in mine.terms] == [("Project Falcon", "PROJECT")]


def test_what_an_identity_sets_is_its_own_and_the_rest_follows_the_shared_value():
    assert layered(SHARED, {"profile": "max"}).profile == Profile.MAX
    assert layered(SHARED.model_copy(update={"profile": Profile.SECRETS}), {}).profile == Profile.SECRETS


def test_identity_and_projects_are_never_shared():
    mine = layered(SHARED, {})
    assert mine.identity == IdentityConfig() and mine.projects == []
    assert layered(SHARED, {"identity": {"usernames": ["emily"]}}).identity.usernames == ["emily"]


def test_where_the_reviewer_runs_and_its_key_stay_the_shared_ones():
    review = {"enabled": True, "url": "https://evil.example/v1", "remote": True, "guidance": "Ward names are secret.",
              "api_key_file": "/run/secrets/latet-master.key", "api_key_env": "LATET_MASTER_KEY"}  # fmt: skip
    mine = layered(REVIEWED, {"llm_review": review}).llm_review
    assert mine.enabled and mine.guidance == "Ward names are secret."
    assert (mine.url, mine.api_key_file, mine.api_key_env) == (
        SHARED.llm_review.url, SHARED.llm_review.api_key_file, SHARED.llm_review.api_key_env
    )  # fmt: skip
    assert pinned_reviewer(REVIEWED.llm_review, LlmReview.model_validate(review)).url == SHARED.llm_review.url


def test_an_identitys_reviewer_is_on_only_when_the_administrator_set_one_up():
    # No shared settings yet: their reviewer is the one of a computer's, on the server's own port 8080, and off.
    for shared in (Config(), SHARED):
        assert not layered(shared, {"llm_review": {"enabled": True}}).llm_review.enabled
        assert not pinned_reviewer(shared.llm_review, LlmReview(enabled=True)).enabled
    assert layered(REVIEWED, {}).llm_review.enabled  # set up and on: on for each identity...
    assert not layered(REVIEWED, {"llm_review": {"enabled": False}}).llm_review.enabled  # ...which may turn it off
    assert pinned_reviewer(REVIEWED.llm_review, LlmReview(enabled=True)).enabled


@pytest.mark.parametrize(
    "url", ["http://llm.example.com", "https://127.0.0.1:8443", "https://localhost/v1", "https://169.254.169.254",
            "https://[::1]:9443", "https://api.localhost", "https://0.0.0.0"],
)  # fmt: skip
def test_a_destination_of_ones_own_in_clear_or_on_the_server_is_dropped(url):
    assert personal_destination_problem(url) is not None
    assert [d.name for d in layered(SHARED, {"upstreams": [{"name": "mine", "url": url}]}).upstreams] == ["llm"]


@pytest.mark.parametrize(
    "url", ["https://10.0.3.9:8443", "https://192.168.1.20", "https://172.16.0.5", "https://100.64.0.1",
            "https://[fd00::5]", "https://[::ffff:10.0.0.5]", "https://224.0.0.1", "https://198.18.0.9",
            # The IPv6 forms that carry an IPv4 address or lead to a gateway (IPv4-compatible, 6to4, Teredo, local
            # NAT64, IPv4-translated), site-local, and the discard prefix.
            "https://[::10.0.0.5]", "https://[2002:a00:5::1]", "https://[2001:0:a00:5::1]", "https://[64:ff9b:1::a00:5]",
            "https://[fec0::1]", "https://[::ffff:0:10.0.0.5]", "https://[100::1]",
            # What Python's registry does not call global: the documentation and benchmarking ranges.
            "https://192.0.2.10", "https://198.51.100.7", "https://203.0.113.5", "https://[2001:db8::5]",
            # An address with the dot that may end a name; one with a zone, which picks an interface of the server's.
            "https://10.0.0.5./v1", "https://[2a00::1%25eth0]/v1",
            # IPv6 outside the global unicast space: unallocated, which a network may number itself from, and the
            # segment routing SIDs; NAT64 forms beside the well-known prefix's.
            "https://[4000::1]", "https://[dead:beef::1]", "https://[5f00::1]", "https://[fe00::1]", "https://[2::1]",
            "https://[64:ff9b::1:a00:5]", "https://[100:0:0:1::1]",
            # Forms refused all along: well-known NAT64, multicast to every node, reserved IPv4, IPv4's broadcast.
            "https://[64:ff9b::a00:5]", "https://[ff02::1]", "https://240.0.0.1", "https://255.255.255.255"],
)  # fmt: skip
def test_a_destination_of_ones_own_in_the_servers_own_network_is_dropped(url):
    assert personal_destination_problem(url) == NOT_PUBLIC
    assert [d.name for d in layered(SHARED, {"upstreams": [{"name": "mine", "url": url}]}).upstreams] == ["llm"]


@pytest.mark.parametrize("address", [PUBLIC, PUBLIC_V6, f"::ffff:{PUBLIC}"])
def test_an_address_on_the_internet_is_not_refused(address):
    assert not egress.forbidden(ipaddress.ip_address(address))
    host = f"[{address}]" if ":" in address else address
    assert personal_destination_problem(f"https://{host}/v1") is None


def test_a_destination_of_ones_own_at_the_servers_own_name_is_the_server_itself():
    for url in ("https://latet.acme.lan/v1", "https://LATET.acme.lan:8443", "https://latet.acme.lan./v1"):
        assert personal_destination_problem(url) is None  # a name, by its form alone
        assert personal_destination_problem(url, server=URL) == ON_SERVER
    assert personal_destination_problem("https://gw.acme.com", server=URL) is None
    assert personal_destination_problem(f"https://{PUBLIC}/v1", server=f"https://{PUBLIC}:5283") == ON_SERVER


def test_a_name_is_judged_by_what_it_resolves_to_when_a_resolver_is_given():
    assert personal_destination_problem("https://gw.acme.com", resolve=_resolving("10.0.3.9")) == NOT_PUBLIC
    assert personal_destination_problem("https://gw.acme.com", resolve=_resolving(PUBLIC, "10.0.3.9")) == NOT_PUBLIC
    assert personal_destination_problem("https://gw.acme.com", resolve=_resolving(PUBLIC)) is None
    assert personal_destination_problem("https://gw.acme.com", resolve=_resolving()) is None  # judged when it connects
    assert personal_destination_problem("https://gw.acme.com") is None  # by its form alone: a name


async def test_a_connection_goes_to_the_address_that_was_checked(monkeypatch):
    monkeypatch.setattr(egress, "resolve", _resolving(PUBLIC))
    url, host, extensions = await egress.pin("https://gw.acme.com/v1/chat")
    assert (url, host, extensions) == (f"https://{PUBLIC}/v1/chat", "gw.acme.com", {"sni_hostname": "gw.acme.com"})
    assert (await egress.pin(f"https://{PUBLIC}:8443/v1"))[0] == f"https://{PUBLIC}:8443/v1"  # a literal: as is
    monkeypatch.setattr(egress, "resolve", _resolving(PUBLIC_V6))
    assert (await egress.pin("https://gw.acme.com:8443/v1"))[:2] == (
        f"https://[{PUBLIC_V6}]:8443/v1",
        "gw.acme.com:8443",
    )
    monkeypatch.setattr(egress, "resolve", _resolving(PUBLIC, "10.0.3.9"))  # pointed inside since it was saved
    with pytest.raises(EgressRefused, match="own network"):
        await egress.pin("https://gw.acme.com/v1/chat")
    monkeypatch.setattr(egress, "resolve", _resolving())
    with pytest.raises(EgressRefused, match="resolve"):
        await egress.pin("https://gw.acme.com/v1/chat")
    with pytest.raises(EgressRefused, match="own network"):
        await egress.pin("https://169.254.169.254/latest/meta-data")
    with pytest.raises(EgressRefused, match="own network"):
        await egress.pin("https://10.0.3.9./v1")  # an address with the dot that may end a name: no name to resolve
    monkeypatch.setattr(egress, "resolve", _resolving(PUBLIC))
    # The Host header is the host and port as written, TLS's name the host.
    assert (await egress.pin("https://GW.Acme.com:8443/v1"))[1:] == (
        "GW.Acme.com:8443",
        {"sni_hostname": "gw.acme.com"},
    )
    for url in ("https://u:p@gw.acme.com/v1", "https://u@gw.acme.com/v1", "https://@gw.acme.com/v1"):
        with pytest.raises(EgressRefused, match="no user or password"):  # it would be sent in the Host header
            await egress.pin(url)


async def test_a_name_that_does_not_resolve_in_time_is_refused_and_holds_none_of_the_servers_other_threads(monkeypatch):
    stuck, threads = threading.Event(), set()

    def silent(host, port):  # a name server that does not answer
        threads.add(threading.current_thread().name)
        stuck.wait(30)
        return [ipaddress.ip_address(PUBLIC)]

    monkeypatch.setattr(egress, "resolve", silent)
    monkeypatch.setattr(egress, "RESOLVE_SECONDS", 0.2)
    try:
        # More connections at once than the event loop has threads for every other work (32 at most), each of an
        # identity of its own.
        waiting = [
            asyncio.create_task(egress.pin(f"https://gw{n}.acme.com/v1", holder=f"u_holder{n:05d}")) for n in range(40)
        ]
        await _until(lambda: len(threads) == 40)  # a thread of this module's own for each lookup
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), 5) == "free"  # the loop's threads are free
        for refused in await asyncio.gather(*waiting, return_exceptions=True):
            assert isinstance(refused, EgressRefused) and "in time" in str(refused)
        assert len(threads) == 40 and all(name.startswith("latet-egress") for name in threads)
    finally:
        stuck.set()


async def test_identities_whose_names_do_not_answer_delay_no_other_identity(monkeypatch):
    """Task 16 measured four identities, each with its share of lookups held by names that do not answer, take every
    thread of a pool of sixteen, and every other identity's destination named by host refused. Each lookup has a
    thread of its own, an identity ``LOOKUPS_PER_HOLDER`` at most: twice as many identities as filled that pool delay
    no other identity's name."""
    stuck = threading.Event()

    def resolver(host, port):
        if host.startswith("silent"):
            stuck.wait(30)
        return [ipaddress.ip_address(PUBLIC)]

    monkeypatch.setattr(egress, "resolve", resolver)
    monkeypatch.setattr(egress, "RESOLVE_SECONDS", 2.0)
    slow = [f"u_slow{n:07d}" for n in range(8)]
    try:
        held = [
            asyncio.create_task(egress.pin(f"https://silent{n}.{who}.example/v1", holder=who))
            for who in slow
            for n in range(egress.LOOKUPS_PER_HOLDER)
        ]
        await _until(lambda: sum(egress._LOOKUPS.held(who) for who in slow) == len(held))
        started = time.monotonic()
        assert (await egress.pin("https://gw.acme.com/v1", holder="u_danielokaf"))[0] == f"https://{PUBLIC}/v1"
        assert time.monotonic() - started < 1.0  # at once, never after the bound
        with pytest.raises(EgressRefused, match="at once"):  # each of the slow ones past its share: at once too
            await egress.pin("https://silent.more.example/v1", holder=slow[0])
    finally:
        stuck.set()
    await asyncio.gather(*held, return_exceptions=True)


async def test_the_lookups_of_every_identity_together_have_a_ceiling(monkeypatch):
    """What the threads of every identity's lookups take together is bounded too (``RESOLVER_THREADS``): past it a
    name is refused at once, never queued behind names that do not answer."""
    stuck = threading.Event()

    def silent(host, port):
        stuck.wait(30)
        return [ipaddress.ip_address(PUBLIC)]

    monkeypatch.setattr(egress, "resolve", silent)
    monkeypatch.setattr(egress, "RESOLVER_THREADS", 6)
    monkeypatch.setattr(egress, "RESOLVE_SECONDS", 0.3)
    holders = ["u_holder00000", "u_holder00001"]
    try:
        held = [
            asyncio.create_task(egress.pin(f"https://silent{n}.acme.com/v1", holder=holders[n % 2])) for n in range(6)
        ]
        await _until(lambda: sum(egress._LOOKUPS.held(who) for who in holders) == 6)
        started = time.monotonic()
        with pytest.raises(EgressRefused, match="the server resolves 6 names at once at most"):
            await egress.pin("https://gw.acme.com/v1", holder="u_danielokaf")
        assert time.monotonic() - started < 0.2  # at once
    finally:
        stuck.set()
    await asyncio.gather(*held, return_exceptions=True)


async def test_a_request_joins_a_lookup_begun_within_the_bound_and_never_an_older_one(monkeypatch):
    """A lookup whose name server has not answered within ``RESOLVE_SECONDS`` is not joined: a request that comes after
    it makes a lookup of its own, which may answer (a lost packet, a name server back), where joining would have made
    it wait for the one that holds, and be refused."""
    stuck, asked = threading.Event(), collections.Counter()

    def resolver(host, port):
        asked[host] += 1
        if asked[host] == 1:
            stuck.wait(30)  # the first lookup's answer never comes
        return [ipaddress.ip_address(PUBLIC)]

    monkeypatch.setattr(egress, "resolve", resolver)
    monkeypatch.setattr(egress, "RESOLVE_SECONDS", 0.3)
    try:
        with pytest.raises(EgressRefused, match="in time"):
            await egress.pin("https://gw.acme.com/v1", holder="u_emilycarte")
        assert (await egress.pin("https://gw.acme.com/v1", holder="u_emilycarte"))[0] == f"https://{PUBLIC}/v1"
        assert asked["gw.acme.com"] == 2
    finally:
        stuck.set()


async def test_a_lookup_a_request_has_waited_out_is_joined_by_no_later_one_whatever_the_clock_says(monkeypatch):
    """Windows' clock moves in steps of about 16 ms: a request that comes as the bound ends can read the stuck lookup
    as begun less than ``RESOLVE_SECONDS`` ago. A lookup a request has waited out is joined by no later one."""
    stuck, asked = threading.Event(), collections.Counter()

    def resolver(host, port):
        asked[host] += 1
        if asked[host] == 1:
            stuck.wait(30)
        return [ipaddress.ip_address(PUBLIC)]

    now = time.monotonic()
    monkeypatch.setattr(egress, "resolve", resolver)
    monkeypatch.setattr(egress, "RESOLVE_SECONDS", 0.3)
    monkeypatch.setattr(egress, "time", types.SimpleNamespace(monotonic=lambda: now))  # a clock that has not moved
    try:
        with pytest.raises(EgressRefused, match="in time"):
            await egress.pin("https://gw.acme.com/v1", holder="u_emilycarte")
        assert (await egress.pin("https://gw.acme.com/v1", holder="u_emilycarte"))[0] == f"https://{PUBLIC}/v1"
        assert asked["gw.acme.com"] == 2
    finally:
        stuck.set()


async def test_one_identitys_names_that_do_not_answer_hold_none_of_the_lookups_another_needs(monkeypatch):
    stuck, asked = threading.Event(), collections.Counter()

    def resolver(host, port):
        asked[host] += 1
        if host.startswith("silent"):  # names whose name server does not answer
            stuck.wait(30)
        return [ipaddress.ip_address(PUBLIC)]

    monkeypatch.setattr(egress, "resolve", resolver)
    monkeypatch.setattr(egress, "RESOLVE_SECONDS", 0.3)
    share = egress.LOOKUPS_PER_HOLDER
    assert share < egress.RESOLVER_THREADS
    try:
        hers = [
            asyncio.create_task(egress.pin(f"https://silent{n}.acme.com/v1", holder="u_emilycarte"))
            for n in range(share)
        ]
        await _until(lambda: sum(asked.values()) == share)
        with pytest.raises(EgressRefused, match="at once"):  # beyond her share: refused at once, nothing asked
            await egress.pin("https://gw.acme.com/v1", holder="u_emilycarte")
        assert "gw.acme.com" not in asked
        assert (await egress.pin("https://gw.acme.com/v1", holder="u_danielokaf"))[0] == f"https://{PUBLIC}/v1"
        for refused in await asyncio.gather(*hers, return_exceptions=True):
            assert isinstance(refused, EgressRefused) and "in time" in str(refused)
        # Her requests stopped waiting, her lookups still hold their threads: they count until they end.
        with pytest.raises(EgressRefused, match="at once"):
            await egress.pin("https://gw.acme.com/v1", holder="u_emilycarte")
    finally:
        stuck.set()
    await _until(lambda: egress._LOOKUPS.held("u_emilycarte") == 0)
    assert (await egress.pin("https://gw.acme.com/v1", holder="u_emilycarte"))[0] == f"https://{PUBLIC}/v1"


async def test_a_name_asked_for_by_several_at_once_is_resolved_once(monkeypatch):
    answer, asked = threading.Event(), collections.Counter()

    def resolver(host, port):
        asked[host] += 1
        answer.wait(30)
        return [ipaddress.ip_address(PUBLIC)]

    monkeypatch.setattr(egress, "resolve", resolver)
    try:
        together = [asyncio.create_task(egress.pin("https://gw.acme.com/v1")) for _ in range(3)]
        await _until(lambda: asked["gw.acme.com"] > 0)
        await asyncio.sleep(0.05)  # time for a second lookup to begin, if one were made
        answer.set()
        assert [found[0] for found in await asyncio.gather(*together)] == [f"https://{PUBLIC}/v1"] * 3
    finally:
        answer.set()
    assert asked["gw.acme.com"] == 1


@pytest.mark.parametrize(
    "url", ["https://gw.acme.com:99999", "https://gw.acme.com:https", "https://[::1", "https:///v1",
            "https://u:p@gw.acme.com/v1", "https://@gw.acme.com/v1"],
)  # fmt: skip
async def test_an_address_that_cannot_be_used_is_refused_at_save_and_at_connect(url):
    assert personal_destination_problem(url) == UNREADABLE
    assert personal_destination_problem(url, resolve=_resolving(PUBLIC)) == UNREADABLE
    assert [d.name for d in layered(SHARED, {"upstreams": [{"name": "mine", "url": url}]}).upstreams] == ["llm"]
    with pytest.raises(EgressRefused, match="cannot be used"):
        await egress.pin(url)


# Where RFC 6052 puts the four bytes of an IPv4 address in an IPv6 address under a NAT64 prefix of each length (the
# byte at place 8, bits 64 to 71, stays zero).
V4_PLACES = {32: (4, 5, 6, 7), 40: (5, 6, 7, 9), 48: (6, 7, 9, 10), 56: (7, 9, 10, 11), 64: (9, 10, 11, 12),
             96: (12, 13, 14, 15)}  # fmt: skip
# A network-specific prefix of each length, from the global unicast space.
NAT64_PREFIXES = {32: "2a00:1234::", 40: "2a00:1234:5600::", 48: "2a00:1234:5678::", 56: "2a00:1234:5678:9a00::",
                  64: "2a00:1234:5678:9abc::", 96: "2a00:1234:5678:9abc:def0:1234::"}  # fmt: skip


def _synthesized(length: int, v4: str) -> ipaddress.IPv6Address:
    """What a DNS64 on the prefix of ``length`` answers for an IPv4 address."""
    raw = bytearray(ipaddress.IPv6Address(NAT64_PREFIXES[length]).packed)
    for place, byte in zip(V4_PLACES[length], ipaddress.IPv4Address(v4).packed, strict=True):
        raw[place] = byte
    return ipaddress.IPv6Address(bytes(raw))


def _dns64(length: int) -> list:
    """What a DNS64 on the prefix of ``length`` answers for ipv4only.arpa: the name's two IPv4 addresses, and what it
    makes of them (RFC 7050)."""
    known = ("192.0.0.170", "192.0.0.171")
    return [*(ipaddress.ip_address(a) for a in known), *(_synthesized(length, a) for a in known)]


@pytest.mark.parametrize("length", sorted(V4_PLACES))
async def test_a_nat64_prefix_of_the_servers_network_is_found_once_and_an_address_judged_by_what_it_carries(
    monkeypatch, length
):
    monkeypatch.setattr(egress, "_NAT64", egress._Nat64())  # not discovered yet
    asked = collections.Counter()

    def dns64(host, port):
        asked[host] += 1
        if host == "ipv4only.arpa":  # its two IPv4 addresses, and what the DNS64 makes of them (RFC 7050)
            return [ipaddress.ip_address(a) for a in ("192.0.0.170", "192.0.0.171")] + [
                _synthesized(length, "192.0.0.170"), _synthesized(length, "192.0.0.171")
            ]  # fmt: skip
        return [_synthesized(length, "10.0.0.5" if host.startswith("inside") else PUBLIC)]

    monkeypatch.setattr(egress, "resolve", dns64)
    inside, outside = _synthesized(length, "10.0.0.5"), _synthesized(length, PUBLIC)
    assert not egress.forbidden(inside)  # a global address, while no prefix is known
    with pytest.raises(EgressRefused, match="own network"):  # the server's NAT64 would take it to 10.0.0.5
        await egress.pin("https://inside.acme.com/v1")
    assert asked["ipv4only.arpa"] == 1 and egress.forbidden(inside)
    assert (await egress.pin("https://outside.acme.com/v1"))[0] == f"https://[{outside}]/v1"  # it carries 1.2.3.4
    with pytest.raises(EgressRefused, match="own network"):  # written as a number too
        await egress.pin(f"https://[{inside}]/v1")
    assert personal_destination_problem(f"https://[{inside}]/v1") == NOT_PUBLIC  # and at save, once it is known
    assert asked["ipv4only.arpa"] == 1  # found once, and kept


async def test_no_nat64_prefix_is_known_when_none_is_found_and_the_discovery_is_made_once(monkeypatch):
    monkeypatch.setattr(egress, "_NAT64", egress._Nat64())
    asked = collections.Counter()

    def plain(host, port):  # no DNS64: ipv4only.arpa has its IPv4 addresses alone
        asked[host] += 1
        return [ipaddress.ip_address("192.0.0.170" if host == "ipv4only.arpa" else PUBLIC_V6)]

    monkeypatch.setattr(egress, "resolve", plain)
    assert (await egress.pin("https://gw.acme.com/v1"))[0] == f"https://[{PUBLIC_V6}]/v1"
    assert (await egress.pin("https://gw2.acme.com/v1"))[0] == f"https://[{PUBLIC_V6}]/v1"
    assert asked["ipv4only.arpa"] == 1 and egress._NAT64.prefixes == ()


async def test_the_nat64_discovery_is_waited_for_a_few_seconds_at_most_and_an_ipv6_address_waits_for_its_answer(
    monkeypatch,
):
    monkeypatch.setattr(egress, "_NAT64", egress._Nat64())
    stuck = threading.Event()

    def silent(host, port):
        stuck.wait(30)  # the network's name server does not answer, then does: no DNS64
        return [ipaddress.ip_address("192.0.0.170" if host == "ipv4only.arpa" else PUBLIC_V6)]

    monkeypatch.setattr(egress, "resolve", silent)
    monkeypatch.setattr(egress, "RESOLVE_SECONDS", 1.0)
    try:
        started = time.monotonic()
        with pytest.raises(EgressRefused, match="NAT64"):  # nothing known yet: the address is not reached
            await egress.pin(f"https://[{PUBLIC_V6}]/v1")
        assert 0.9 <= time.monotonic() - started < 5  # it waited for the discovery, the bound and no longer
        started = time.monotonic()
        with pytest.raises(EgressRefused, match="NAT64"):
            await egress.pin(f"https://[{PUBLIC_V6}]:8443/v1")
        assert time.monotonic() - started < 0.5  # once its time has passed, nobody waits for it again
    finally:
        stuck.set()
    await _until(lambda: egress._NAT64.answered)  # it answered at last: no prefix, and an IPv6 address is reached
    assert (await egress.pin(f"https://[{PUBLIC_V6}]/v1"))[0] == f"https://[{PUBLIC_V6}]/v1"
    assert egress._NAT64.prefixes == ()


async def test_while_the_nat64_discovery_has_not_answered_no_ipv6_address_a_nat64_could_translate_is_reached(
    monkeypatch,
):
    """Until the network says its NAT64 prefixes (ipv4only.arpa resolves, to its IPv4 addresses alone where there is no
    DNS64), any IPv6 address of the global space could be under one and carry an address of the server's network: none
    is reached, by name or written as a number. A name with an IPv4 address too goes there."""
    monkeypatch.setattr(egress, "_NAT64", egress._Nat64())

    def resolver(host, port):
        if host == "ipv4only.arpa":
            return []  # the network's name server does not answer
        return [ipaddress.ip_address(a) for a in ((PUBLIC_V6, PUBLIC) if host.startswith("both") else (PUBLIC_V6,))]

    monkeypatch.setattr(egress, "resolve", resolver)
    for url in ("https://gw.acme.com/v1", f"https://[{PUBLIC_V6}]/v1"):
        with pytest.raises(EgressRefused, match="NAT64"):
            await egress.pin(url)
    assert (await egress.pin("https://both.acme.com/v1"))[0] == f"https://{PUBLIC}/v1"


async def test_a_nat64_discovery_that_does_not_answer_is_made_again_after_a_while(monkeypatch):
    """One failed answer at the first IPv6 judgement left a NAT64 prefix of the network unknown for the life of the
    process, and an address under it that carries one of the network's own IPv4 addresses passed. A discovery that
    does not answer is made again ``NAT64_RETRY`` seconds after it began."""
    monkeypatch.setattr(egress, "_NAT64", egress._Nat64())
    monkeypatch.setattr(egress, "NAT64_RETRY", 0.2)
    network, asked = {"answers": False}, collections.Counter()

    def resolver(host, port):
        asked[host] += 1
        if host == "ipv4only.arpa":
            return _dns64(96) if network["answers"] else []  # its name server did not answer, then does
        if host == "both.acme.com":  # a name with an IPv4 address and an IPv6 one
            return [ipaddress.ip_address(PUBLIC), _synthesized(96, PUBLIC)]
        return [_synthesized(96, "10.0.0.5")]

    monkeypatch.setattr(egress, "resolve", resolver)
    assert (await egress.pin("https://both.acme.com/v1"))[0] == f"https://{PUBLIC}/v1"
    network["answers"] = True
    await asyncio.sleep(0.25)
    with pytest.raises(EgressRefused, match="own network"):  # found now: the server's NAT64 would take it to 10.0.0.5
        await egress.pin("https://inside.acme.com/v1")
    assert asked["ipv4only.arpa"] == 2


async def test_the_nat64_prefixes_are_discovered_again_from_time_to_time(monkeypatch):
    """A network's NAT64 can change while a server runs: what was found is discovered again ``NAT64_REFRESH`` seconds
    later, nobody waiting for it, the prefixes known before in force until it answers."""
    monkeypatch.setattr(egress, "_NAT64", egress._Nat64())
    monkeypatch.setattr(egress, "NAT64_REFRESH", 0.2)
    network, asked, slow = {"dns64": False}, collections.Counter(), threading.Event()

    def resolver(host, port):
        asked[host] += 1
        if host != "ipv4only.arpa":
            return [_synthesized(96, "10.0.0.5")]
        if not network["dns64"]:  # no DNS64 at first
            return [ipaddress.ip_address("192.0.0.170")]
        slow.wait(30)  # then one, whose answer takes a while
        return _dns64(96)

    monkeypatch.setattr(egress, "resolve", resolver)
    inside = f"https://[{_synthesized(96, '10.0.0.5')}]/v1"
    try:
        assert (await egress.pin("https://inside.acme.com/v1"))[0] == inside  # no NAT64 known: a global address
        network["dns64"] = True
        await asyncio.sleep(0.25)
        started = time.monotonic()
        assert (await egress.pin("https://inside.acme.com/v1"))[0] == inside  # the next begins, and is not waited for
        assert time.monotonic() - started < 0.5 and asked["ipv4only.arpa"] == 2
    finally:
        slow.set()
    await _until(lambda: egress._NAT64.prefixes != ())
    with pytest.raises(EgressRefused, match="own network"):
        await egress.pin("https://inside.acme.com/v1")
    assert asked["ipv4only.arpa"] == 2


def test_a_destination_of_ones_own_elsewhere_is_its_own_and_a_shared_name_is_kept():
    mine = {
        "upstreams": [{"name": "llm", "url": "https://other.example"}, {"name": "gw", "url": "https://gw.acme.com"}]
    }
    assert [(d.name, d.url) for d in layered(SHARED, mine).upstreams] == [
        ("llm", "https://llm.acme.com"),
        ("gw", "https://gw.acme.com"),
    ]


def test_one_check_judges_the_destinations_of_ones_own_and_never_a_shared_one():
    """What the dashboard and the administration both ask when settings are saved: each destination the shared settings
    do not set as it is (that name at that address) is judged, by where its name leads too; a shared one never is (its
    administrator may set one inside)."""
    config = Config(
        upstreams=[
            {"name": "llm", "url": "https://llm.acme.com"},  # the shared one, as it is
            {"name": "gw", "url": "https://gw.acme.com"},
            {"name": "me", "url": f"{URL}/v1"},
            {"name": "web", "url": f"https://{PUBLIC}/v1"},
        ]
    )
    found = own_destinations_refused(SHARED, config, resolve=_resolving("10.0.3.9"), server=URL)
    assert found == [(1, NOT_PUBLIC), (2, ON_SERVER)]
    assert own_destinations_refused(SHARED, config, resolve=_resolving(PUBLIC), server=URL) == [(2, ON_SERVER)]
    moved = Config(upstreams=[{"name": "llm", "url": "https://10.0.0.5"}])  # a shared name, at another address
    assert own_destinations_refused(SHARED, moved, server=URL) == [(0, NOT_PUBLIC)]


def test_a_save_resolves_names_in_the_egress_threads_counted_for_its_holder_and_waits_a_bounded_time(monkeypatch):
    release, threads = threading.Event(), []

    def stuck(host, port):
        threads.append(threading.current_thread().name)
        release.wait(10)
        return [ipaddress.ip_address("10.0.3.9")]

    monkeypatch.setattr(egress, "resolve", stuck)
    try:
        resolve = egress.bounded("u_emilycarte", seconds=0.2)
        started = time.monotonic()
        assert resolve("gw.acme.com", 443) == []  # not answered in time: judged when the server connects
        assert resolve("api.acme.com", 443) == []  # asked once the time is up: not looked up at all
        assert time.monotonic() - started < 1.5  # one bound for the whole save, not one per name
        assert egress._LOOKUPS.held("u_emilycarte") == 1 and len(threads) == 1
        assert threads[0].startswith("latet-egress")
    finally:
        release.set()
    monkeypatch.setattr(egress, "resolve", _resolving(PUBLIC))
    assert egress.bounded("u_emilycarte")("web.acme.com", 443) == [ipaddress.ip_address(PUBLIC)]


def test_the_port_and_the_capture_folder_are_the_servers():
    mine = layered(SHARED, {"port": 6000, "capture_dir": "/tmp/raw"})
    assert (mine.port, mine.capture_dir) == (SHARED.port, None)


def test_a_change_that_removes_shared_items_says_how_many_never_which():
    mine = layered(SHARED, {}).model_copy(update={"terms": [], "upstreams": []})
    assert removed_shared(SHARED, mine) == {"terms": 1, "upstreams": 1}
    said = str(SharedItemsError(removed_shared(SHARED, mine)))
    assert "1 term" in said and "1 destination" in said and "Falcon" not in said and "llm" not in said
    assert removed_shared(SHARED, layered(SHARED, {"terms": [{"value": "Orion"}]})) == {}


def test_the_own_part_is_what_a_config_sets_beyond_the_shared_one():
    mine = layered(SHARED, {"terms": [{"value": "Mercury Labs", "type": "CLIENT"}], "profile": "max"})
    part = own_part(SHARED, mine)
    assert part == {
        "profile": "max",
        "terms": [{"value": "Mercury Labs", "type": "CLIENT", "case_sensitive": False, "whole_word": True}],
    }
    assert layered(SHARED, part) == mine


def test_a_setting_of_ones_own_stays_while_it_has_the_value_one_gave():
    part = own_part(SHARED, layered(SHARED, {"profile": "max", "terms": [{"value": "Orion"}]}))
    later = SHARED.model_copy(update={"profile": Profile.MAX, "terms": [*SHARED.terms, TermConfig(value="Orion")]})
    again = own_part(later, layered(later, part), before=part)  # saved again while the server's are the same
    assert again["profile"] == "max" and [t["value"] for t in again["terms"]] == ["Orion"]
    back = layered(SHARED, again)  # and the server's drop back: Emily's stay
    assert back.profile == Profile.MAX and "Orion" in [t.value for t in back.terms]
    assert "profile" not in own_part(later, layered(later, part))  # without what it set before: it follows


# --- rules for everyone are an edition's: the Community Edition shares settings alone ------------------------------

CANARY = "Zanzibar Holdings 4417"


def _every_rule_set() -> Config:
    """Settings stored with every rule a server's administrators could once set for everyone, a canary among them, and
    a shared destination and the shared reviewer's history setting beside them."""
    return Config(
        terms=[TermConfig(value=CANARY)],
        recognizers=[{"name": "badge", "pattern": "BDG-[0-9]{6}"}],
        fields=[FieldRule(name="salary")],
        data_tools=["mcp__crm__*"],
        data_files=["exports/**"],
        data_hosts=["*.salesforce.com"],
        allow_values=["support@acme.com"],
        allow_patterns=["build-[0-9]+"],
        disabled_secret_rules=["generic-api-key"],
        profile=Profile.MAX,
        pii=["EMAIL"],
        id_regions=["DE"],
        phone_regions=["DE"],
        include_private_ips=True,
        policies={"EMAIL": "redact"},
        data="strict",
        dump_guard=20,
        ner="off",
        images="block",
        auto_mode_review={"user_name": "allow"},
        live_review={"enabled": True},
        upstreams=SHARED.upstreams,
        history="metadata",
        retention_days=3,
        ocr="off",
    )


def test_every_setting_is_a_rule_for_everyone_a_setting_the_server_shares_or_each_identitys_own():
    """A new setting must be put in one place: a rule (the Enterprise Edition shares those), a setting the server needs
    to run, shared in the Community Edition, or each identity's own."""
    rules, settings, own = set(RULE_KEYS), set(SETTING_KEYS), set(OWN_KEYS)
    assert rules | settings | own == set(Config.model_fields)
    assert not (rules & settings) and not (rules & own) and not (settings & own)
    assert settings == {"upstreams", "llm_review", "ocr", "history", "retention_days", "port", "capture_dir"}
    assert own == {"identity", "projects"}


def test_the_settings_in_force_take_the_servers_settings_and_only_the_rules_an_edition_gives():
    stored = _every_rule_set()
    shown = in_force(stored, {})
    default = Config()
    for key in RULE_KEYS:
        assert getattr(shown, key) == getattr(default, key), key
    assert (shown.upstreams, shown.history, shown.retention_days, shown.ocr) == (SHARED.upstreams, "metadata", 3, "off")
    # An edition gives rules alone: a destination or a reviewer it gave would pass by the checks they go through.
    given = in_force(stored, {"terms": [TermConfig(value="Project Falcon")], "upstreams": [], "history": "off"})
    assert [t.value for t in given.terms] == ["Project Falcon"]
    assert given.upstreams == SHARED.upstreams and given.history == "metadata"


def test_rules_kept_from_before_are_counted_and_named_never_shown():
    stored = _every_rule_set()
    kept = kept_rules(stored, {})
    default = Config()
    assert set(kept) == {key for key in RULE_KEYS if getattr(stored, key) != getattr(default, key)}
    assert {"terms", "profile", "allow_values", "live_review"} <= set(kept)
    said = kept_said(kept)
    assert "1 term" in said and "profile" in said and CANARY not in said and "support@acme.com" not in said
    assert kept_rules(stored, {"terms": stored.terms}).get("terms") is None  # an edition's rules are in force, not kept
    assert kept_rules(Config(upstreams=SHARED.upstreams), {}) == {}


def test_a_write_of_the_settings_keeps_the_rules_kept_from_before():
    stored = _every_rule_set()
    written = with_kept_rules(Config(history="full", upstreams=[]), stored)
    assert written.history == "full" and written.upstreams == []
    assert [t.value for t in written.terms] == [CANARY] and written.profile == Profile.MAX
    assert with_kept_rules(Config(history="full"), None).terms == []  # nothing kept: an unreadable row's defaults


def test_an_identity_laid_over_the_settings_in_force_holds_its_own_rules_alone():
    shown = in_force(_every_rule_set(), {})
    mine = layered(shown, {"terms": [{"value": "Northwind"}]})
    assert (
        [t.value for t in mine.terms] == ["Northwind"] and mine.allow_values == [] and mine.profile == Config().profile
    )
    assert removed_shared(shown, Config()) == {"upstreams": len(SHARED.upstreams)}  # a destination is still not hers
    assert "Enterprise Edition" in RULES_FOR_EVERYONE and "latet rules export" in RULES_FOR_EVERYONE


def test_the_shared_settings_hold_nothing_that_is_each_identitys_or_writes_on_the_server():
    assert shared_refusals(Config(terms=[TermConfig(value="Orion")])) == []
    found = shared_refusals(
        Config(
            identity=IdentityConfig(usernames=["emily"]), projects=[ProjectEntry(path="/srv")], capture_dir="/tmp/raw"
        )
    )
    assert [why.split(":")[0] for why in found] == ["identity", "projects", "capture_dir"]


def test_own_settings_refuse_a_change_that_drops_a_shared_item_and_write_their_part_alone():
    store = MemorySettings(SHARED)
    mine = store.prepare(store.load().model_copy(update={"profile": Profile.MAX}), strict=True)
    store.save(mine)
    assert store.own == {"profile": "max"} and store.load() == mine and store.shared() == SHARED
    with pytest.raises(SharedItemsError, match="1 term"):
        store.prepare(mine.model_copy(update={"terms": []}), strict=True)
    assert [t.value for t in store.prepare(mine.model_copy(update={"terms": []})).terms] == ["Project Falcon"]


def _over(shared: Config) -> tuple[MemorySettings, list[Config]]:
    """Emily's settings in memory over shared settings an administrator may change meanwhile: ``[0]`` of the list is
    what the instance reads as the shared settings from then on."""
    store, now = MemorySettings(shared), [shared]
    store._shared = lambda: now[0]
    return store, now


def test_a_save_made_over_older_shared_settings_keeps_nothing_of_theirs_as_her_own():
    """Emily's page shows her settings laid over the shared ones; the administrator then raises them (profile max,
    private addresses masked), drops a shared term and adds one, and her space has not applied that yet. Her save of a
    term of her own from that page keeps that term alone as her own part: none of the older shared values becomes
    hers, where each would have stayed hers for good (a protection set for everyone no longer hers), and the term
    added since is not one she removed. What applies is what she saw with her term, until her space applies the change
    and weighs what it lowers against her approval. Saved as they are (a change rejected), the settings in use keep
    nothing of the older shared values either."""
    before = SHARED.model_copy(update={"terms": [*SHARED.terms, TermConfig(value="Orion")]})
    after = SHARED.model_copy(
        update={"profile": Profile.MAX, "include_private_ips": True, "terms": [*SHARED.terms, TermConfig(value="Vega")]}
    )
    store, now = _over(before)
    page = store.load()
    now[0] = after
    draft = page.model_copy(update={"terms": [*page.terms, TermConfig(value="Northwind")]})
    prepared = store.prepare(draft, strict=True)
    store.save(prepared)
    assert store.own == {"terms": [TermConfig(value="Northwind").model_dump(mode="json")]}
    assert prepared == draft  # what she saw, with her term
    applied = store.load()  # her space applies the administrator's change
    assert applied.profile == Profile.MAX and applied.include_private_ips is True
    assert [t.value for t in applied.terms] == ["Project Falcon", "Vega", "Northwind"]
    assert loosened(layered(after, {}), applied) == []  # nothing lower than what the shared settings give everyone
    store, now = _over(before)
    in_use = store.load()
    now[0] = after
    store.save(in_use)
    assert store.own == {}


def test_a_term_too_long_to_be_matched_in_a_part_is_refused_where_settings_are_saved():
    """A term longer than a part of the terms' matcher is compiled alone, holding every identity's event loop, and one of
    about a million characters compiles within no budget: refused where the server keeps an identity's settings (its
    own, a project's), by its place and the limit, never quoted (S-03)."""
    long = "Orion" + "x" * TERM_LONGEST
    store = MemorySettings(SHARED)
    mine = store.load()
    entry = ProjectEntry(path="/home/emily/billing-api", terms=[TermConfig(value=long)])
    for wrong, place in (
        (mine.model_copy(update={"terms": [*mine.terms, TermConfig(value=long)]}), "terms.1"),
        (mine.model_copy(update={"projects": [entry]}), "projects.0.terms.0"),
    ):
        with pytest.raises(SettingsRefused) as raised:
            store.prepare(wrong, strict=True)
        assert isinstance(raised.value, SharedItemsError)  # refused by whatever refuses a change that drops one
        assert place in str(raised.value) and str(TERM_LONGEST) in str(raised.value) and "xxx" not in str(raised.value)
    assert store.prepare(mine.model_copy(update={"terms": [*mine.terms, TermConfig(value=long)]}))  # read as kept


def _recognizers(count: int, start: int = 0) -> list[dict]:
    return [{"name": f"r{i}", "pattern": f"ZQ{i:05d}-[0-9]{{6}}"} for i in range(start, start + count)]


def test_more_rules_than_a_server_keeps_for_one_identity_are_refused_where_settings_are_saved():
    """Seventy thousand recognizers saved once would make one identity's space weigh past every space's budget, and
    its every re-application compile them for minutes: an identity's own recognizers, field rules and patterns are
    counted where its settings are saved, its projects' with its own, and refused past what a server keeps for one
    (the shared ones are not its own), in words that name the kind, the count and the limit, never an item. Counted
    alone: none is compiled."""
    store = MemorySettings(SHARED.model_copy(update={"recognizers": _recognizers(OWN_RULES["recognizers"], 90_000)}))
    mine = store.load()
    started = time.perf_counter()
    with pytest.raises(SettingsRefused) as raised:
        store.prepare(mine.model_copy(update={"recognizers": [*mine.recognizers, *_recognizers(70_000)]}), strict=True)
    assert time.perf_counter() - started < 5.0
    said = str(raised.value)
    assert isinstance(raised.value, SharedItemsError) and "70000 recognizers" in said and "500" in said
    assert "ZQ" not in said and "r1" not in said
    at_the_cap = [*mine.recognizers, *_recognizers(OWN_RULES["recognizers"])]
    assert store.prepare(mine.model_copy(update={"recognizers": at_the_cap}), strict=True).recognizers == at_the_cap
    entry = ProjectEntry(path="/home/emily/billing-api", recognizers=_recognizers(1, 70_000))
    projects = mine.model_copy(update={"recognizers": at_the_cap, "projects": [entry]})
    with pytest.raises(SettingsRefused, match="501 recognizers"):  # a project's count with hers
        store.prepare(projects, strict=True)
    fields = [FieldRule(name=f"column_{i}") for i in range(OWN_RULES["fields"] + 1)]
    patterns = [f"build-{i}-[0-9]+" for i in range(OWN_RULES["allow_patterns"] + 1)]
    for key, items, name in (("fields", fields, "field rules"), ("allow_patterns", patterns, "patterns not to mask")):
        with pytest.raises(SettingsRefused, match=f"{len(items)} {name}"):
            store.prepare(mine.model_copy(update={key: items}), strict=True)
    with pytest.raises(SettingsRefused, match="501 recognizers"):  # a save that was not prepared strictly
        store.save(projects)
    assert store.own == {}


# As many recognizers as an edition that shares its rules (the Enterprise Edition's, E6) may add to an identity's own.
EDITION_RECOGNIZERS = 500


def test_a_weighing_of_settings_at_the_caps_compiles_each_recognizer_once_and_then_none():
    """``loosened`` compiles each recognizer as a build does, kept by its JSON: its cache holds both sides of a weighing
    of settings at the caps, an edition's shared recognizers and the identity's own, so that no call compiles them all
    again (past the cache's size each call compiled every one again, seconds a call)."""
    assert 2 * (OWN_RULES["recognizers"] + EDITION_RECOGNIZERS) <= protection.RECOGNIZERS_COMPILED
    shared = _recognizers(EDITION_RECOGNIZERS, 80_000)
    before = Config(recognizers=[*shared, *_recognizers(OWN_RULES["recognizers"])])
    after = Config(recognizers=[*shared, *_recognizers(OWN_RULES["recognizers"], 40_000)])
    protection._built.cache_clear()
    loosened(before, after)
    first = protection._built.cache_info()
    assert first.misses == len(shared) + 2 * OWN_RULES["recognizers"]
    loosened(before, after)
    assert protection._built.cache_info().misses == first.misses  # the next one compiles none


def test_settings_that_are_not_valid_name_the_field_and_never_its_value():
    for own, field in (
        ({"identity": {"emails": "emily.carter@acme.com"}}, "identity.emails (list_type)"),
        ({"terms": [{"value": "Mercury Labs", "type": 7}]}, "terms.1.type (string_type)"),
        ({"policies": {"emily.carter@acme.com": "nope"}}, "policies.* (enum)"),  # a key is what was given
    ):
        with pytest.raises(ValueError, match="the settings are not valid") as raised:
            layered(SHARED, own)
        said = str(raised.value)
        assert field in said and "emily" not in said and "Mercury" not in said
        assert raised.value.__context__ is None  # pydantic's own words, which quote the value, are not chained to it


@given(
    terms=st.lists(st.sampled_from(["Project Falcon", "Mercury Labs", "Orion"]), max_size=4),
    allow=st.lists(st.sampled_from(["support@acme.com", "noreply@acme.com"]), max_size=3),
    profile=st.sampled_from([None, "max", "balanced", "secrets"]),
)
def test_laying_an_own_part_again_changes_nothing(terms, allow, profile):
    own: dict = {"terms": [{"value": t} for t in terms], "allow_values": allow}
    if profile:
        own["profile"] = profile
    mine = layered(SHARED, own)
    assert layered(SHARED, own_part(SHARED, mine)) == mine
    assert layered(SHARED, own_part(SHARED, mine, before=own_part(SHARED, mine))) == mine
