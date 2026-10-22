"""Where a destination of an identity's own may lead, on a server: never into the server's own network. An address
that is private (RFC 1918), unique-local, carrier-grade NAT, loopback, link-local, multicast, reserved or unspecified
is refused, as a literal and as a name that resolves to one. In IPv6, what Python's registry calls reserved is every
block outside the global unicast space but the unique-local, link-local, site-local and multicast ones, which are
refused as well: unallocated space, which a network may number itself from, the segment routing SIDs among it. So are
the IPv6 forms that carry an IPv4 address or lead to a gateway (IPv4-compatible, IPv4-translated, 6to4, Teredo, NAT64
on the well-known and the local prefixes), whatever address they carry, the discard prefix, an address written with a
zone, and whatever else Python's registry of special addresses does not call global (the documentation and
benchmarking ranges, which a network may use inside, and any range a later registry adds). An IPv4-mapped address
is judged as the IPv4 address it is; an address under a NAT64 prefix of the server's network, which the server's NAT64
turns into the IPv4 address it carries, is refused when that address is. The prefixes are discovered as RFC 7050 does
(``_Nat64``), again after a minute when the network did not answer and every ten minutes when it did (a prefix the
network changed to is unknown until then); until it has answered once, no IPv6 address a NAT64 could translate is
reached (a name that has an IPv4 address too goes there).

A name is resolved again when the server connects, and the request goes to the address that was checked (``pin``), so
that a name pointed elsewhere after it was saved does not get through. Each lookup runs in a thread of this module's
own, which ends with it, and is waited for ``RESOLVE_SECONDS`` at most: past that, the request is refused. A name whose
server does not answer holds its thread until the system gives up on it, so a name asked for by several requests within
that time is resolved once (one asked for later is resolved anew: the lookup under way may never answer), each holder
(an identity) has at most ``LOOKUPS_PER_HOLDER`` lookups under way, counted until their thread ends, and every holder
together ``RESOLVER_THREADS``: one identity's names hold none of the threads the server's other work runs in, nor any
that another identity's names need; it would take a hundred and twenty-eight identities at once, each with its share of
names that do not answer, to delay the others'. A save of settings resolves its names the same way (``bounded``), all
of them waited for that long at most, and one that has not answered by then is judged when the server connects. Only an
administrator sets a destination that leads inside, in the shared settings. Nothing here needs the `server` extra: a
computer's install imports it too (the forwarder does)."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import itertools
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], list[Address]]
# Refused whatever Python's registry says of them (``forbidden`` also refuses what it does not call global, and what it
# calls reserved).
_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
        "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
        "::/128", "::1/128", "::/96", "::ffff:0:0/96", "::ffff:0:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48",
        "100::/64", "2001::/23", "2002::/16", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
    )
)  # fmt: skip
NOT_PUBLIC = (
    "it leads into the server's own network (a private, local or reserved address): only its administrator sets such "
    "a destination, for everyone"
)
UNREADABLE = (
    "its address cannot be used: it needs a host, a port from 0 to 65535 when one is written, and no user or password"
)
# Said of an IPv6 address while the network has not said its NAT64 prefixes: any address of the global space could be
# under one, and carry an address of the server's network.
NAT64_UNKNOWN = (
    "its IPv6 address is not reached while the server's network has not said its NAT64 prefixes (ipv4only.arpa did "
    "not resolve): asked again within a minute"
)
RESOLVE_SECONDS = 5.0  # how long a connection waits for its name to resolve: past it, the request is not sent
# The lookups under way at once, every holder's together, each in a thread of its own that ends with it: past it a name
# is refused at once. The event loop's own threads (``asyncio.to_thread``) are every space's, and a name whose server
# does not answer would hold one of them until the system gives up.
RESOLVER_THREADS = 512
LOOKUPS_PER_HOLDER = 4  # the lookups one holder may have under way at once
# RFC 7050: the name whose AAAA answers show the prefix a DNS64 puts before an IPv4 address, and the two IPv4 addresses
# it has (192.0.0.170, 192.0.0.171), which those answers carry.
NAT64_NAME = "ipv4only.arpa"
_NAT64_KNOWN = (0xC00000AA, 0xC00000AB)
NAT64_RETRY = 60.0  # seconds after a discovery that did not answer began, before the next is made
NAT64_REFRESH = 600.0  # seconds after a discovery answered, before the next is made (nobody waits for it)
_NUMBERS = itertools.count(1)  # the names of the lookups' threads
# RFC 6052: the places of the four bytes of the IPv4 address that an IPv6 address carries under a NAT64 prefix of each
# length. The byte at place 8 (bits 64 to 71) is zero, and comes after the prefix but for one of 96 bits.
_V4_PLACES = {
    32: (4, 5, 6, 7), 40: (5, 6, 7, 9), 48: (6, 7, 9, 10), 56: (7, 9, 10, 11), 64: (9, 10, 11, 12),
    96: (12, 13, 14, 15),
}  # fmt: skip


class EgressRefused(Exception):
    """The address a destination of an identity's own leads to is not one the server may reach for it."""


def _carried(address: ipaddress.IPv6Address, length: int) -> ipaddress.IPv4Address:
    """The IPv4 address that ``address`` carries under a NAT64 prefix of ``length`` bits (RFC 6052). The byte at place 8
    is not looked at: what a NAT64 does with one that is not zero is its own."""
    raw = address.packed
    return ipaddress.IPv4Address(bytes(raw[place] for place in _V4_PLACES[length]))


def _under(address: ipaddress.IPv6Address, prefix: int, length: int) -> bool:
    return int(address) >> (128 - length) == prefix >> (128 - length)


def _prefixes(answers: list[Address]) -> tuple[tuple[int, int], ...]:
    """The NAT64 prefixes the answers for ``ipv4only.arpa`` show (RFC 7050), as ``(prefix, length)``: for each IPv6
    answer, the longest prefix under which it carries one of the name's own IPv4 addresses, the byte at place 8 zero.
    (Shorter places can only match by chance: RFC 6052 leaves the bits after the IPv4 address at zero.)"""
    found: list[tuple[int, int]] = []
    for answer in answers:
        if not isinstance(answer, ipaddress.IPv6Address) or answer.ipv4_mapped is not None:
            continue
        for length in sorted(_V4_PLACES, reverse=True):
            if (length == 96 or answer.packed[8] == 0) and int(_carried(answer, length)) in _NAT64_KNOWN:
                prefix = (int(answer) >> (128 - length)) << (128 - length)
                if (prefix, length) not in found:
                    found.append((prefix, length))
                break
    return tuple(found)


def _settle(answer: asyncio.Future[list[Address]], lookup: Future[list[Address]]) -> None:
    """Hand what ``lookup`` answered to the request that waits for it, unless its wait has ended."""
    if answer.done():
        return
    if lookup.cancelled():
        answer.set_result([])
    elif (error := lookup.exception()) is not None:
        answer.set_exception(error)
    else:
        answer.set_result(lookup.result())


async def _answer(lookup: Future[list[Address]], host: str, seconds: float) -> list[Address]:
    """What ``lookup`` answers, waited for ``seconds`` at most on this event loop. The lookup is never cancelled: other
    requests may wait for it, and it counts for its holder until it ends. Raises EgressRefused when it has not answered
    by then."""
    loop = asyncio.get_running_loop()
    answer: asyncio.Future[list[Address]] = loop.create_future()

    def hand(done: Future[list[Address]]) -> None:  # in the thread that ends the lookup (here, when it has ended)
        with contextlib.suppress(RuntimeError):  # the loop has closed: nobody waits for the answer
            loop.call_soon_threadsafe(_settle, answer, done)

    lookup.add_done_callback(hand)
    try:
        return await asyncio.wait_for(answer, seconds)
    except TimeoutError:
        raise EgressRefused(f"{host} did not resolve in time ({seconds:g} seconds)") from None


def _in_thread(host: str, port: int) -> Future[list[Address]]:
    """``resolve(host, port)`` in a thread of this module's own, which ends with the lookup: what it answers, in the
    future returned (none found when the thread is stopped by what no lookup raises). Raises RuntimeError when the
    system starts no thread."""
    lookup: Future[list[Address]] = Future()
    lookup.set_running_or_notify_cancel()
    find = resolve  # as it is now (the tests put a resolver of their own in its place)

    def run() -> None:
        try:
            lookup.set_result(find(host, port))
        except Exception as exc:  # handed to whoever waits: a request is refused with it
            lookup.set_exception(exc)
        finally:
            if not lookup.done():
                lookup.set_result([])

    threading.Thread(target=run, name=f"latet-egress-{next(_NUMBERS)}", daemon=True).start()
    return lookup


class _Lookups:
    """The names being resolved for ``pin``: one lookup for a name that several requests ask for within
    ``RESOLVE_SECONDS`` of its start, and the lookups each holder has under way, counted until their thread ends (after
    the requests stopped waiting for them), with every holder's together."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, int], tuple[Future[list[Address]], float]] = {}  # with when each began
        self._held: dict[str, int] = {}
        self._under_way = 0  # every holder's

    def held(self, holder: str | None) -> int:
        """How many lookups ``holder`` has under way."""
        with self._lock:
            return self._held.get(holder or "", 0)

    def start(self, host: str, port: int, holder: str | None) -> Future[list[Address]]:
        """The lookup of ``host`` begun less than ``RESOLVE_SECONDS`` ago, or a new one in a thread of its own, counted
        for ``holder`` (the lookups made without one count as one holder's). Raises EgressRefused, at once, when
        ``holder`` has ``LOOKUPS_PER_HOLDER`` under way, or every holder ``RESOLVER_THREADS``: nothing is resolved."""
        key, who, now = (host, port), holder or "", time.monotonic()
        with self._lock:
            found = self._pending.get(key)
            if found is not None and now - found[1] < RESOLVE_SECONDS:
                return found[0]
            if self._held.get(who, 0) >= LOOKUPS_PER_HOLDER:
                raise EgressRefused(
                    f"{host} was not resolved: an identity has {LOOKUPS_PER_HOLDER} names resolved at once at most, "
                    "and that many of its own are being resolved"
                )
            if self._under_way >= RESOLVER_THREADS:
                raise EgressRefused(
                    f"{host} was not resolved: the server resolves {RESOLVER_THREADS} names at once at most, and that "
                    "many are being resolved: try again in a moment"
                )
            try:
                lookup = _in_thread(host, port)
            except RuntimeError:  # the system starts no more threads now
                raise EgressRefused(f"{host} was not resolved: the server could not start a lookup now") from None
            self._held[who] = self._held.get(who, 0) + 1
            self._under_way += 1
            self._pending[key] = (lookup, now)
        lookup.add_done_callback(lambda done: self._ended(key, who, done))
        return lookup

    def waited_out(self, host: str, port: int, lookup: Future[list[Address]]) -> None:
        """A request waited ``RESOLVE_SECONDS`` for ``lookup`` in vain: no later request joins it, whatever the clock
        says (Windows' moves in steps of about 16 ms, and one that comes as the bound ends could read it as begun less
        than ``RESOLVE_SECONDS`` ago). It still counts for its holder until its thread ends."""
        with self._lock:
            if (found := self._pending.get((host, port))) is not None and found[0] is lookup:
                del self._pending[(host, port)]

    def _ended(self, key: tuple[str, int], who: str, lookup: Future[list[Address]]) -> None:
        """In the thread that ends a lookup: its name may be asked for anew, and its holder's count goes down."""
        with self._lock:
            if (found := self._pending.get(key)) is not None and found[0] is lookup:
                del self._pending[key]
            if (left := self._held.get(who, 0) - 1) > 0:
                self._held[who] = left
            else:
                self._held.pop(who, None)
            self._under_way -= 1


class _Nat64:
    """The NAT64 prefixes of the server's network, discovered as RFC 7050 does (``_prefixes``), in a thread of this
    module's, when an IPv6 address is judged at a connection. A discovery answers when the name resolves at all (to its
    IPv4 addresses alone, where the network has no DNS64); one that does not answer (no address, an error, never)
    changes nothing, and the next is made ``NAT64_RETRY`` seconds after it began; one that answered is made again
    ``NAT64_REFRESH`` seconds later, the prefixes known before in force until it answers. A connection waits for a
    discovery while none has ever answered, until ``RESOLVE_SECONDS`` after it began, and never for a later one; until
    one has answered, ``answered`` is false (``pin`` then reaches no IPv6 address a NAT64 could translate).
    ``prefixes``, when given, are the ones discovered already: no lookup is ever made."""

    def __init__(self, prefixes: tuple[tuple[int, int], ...] | None = None) -> None:
        self._lock = threading.Lock()
        self._lookup: Future[list[Address]] | None = None
        self._until = 0.0  # on time.monotonic(): no request waits for the lookup after it
        self._next = 0.0  # on time.monotonic(): when the next discovery is due
        self._given = prefixes is not None
        self.answered = prefixes is not None
        self.prefixes: tuple[tuple[int, int], ...] = prefixes or ()

    def carried(self, address: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
        """The IPv4 addresses ``address`` carries under the prefixes known (none when it is under none of them)."""
        return [_carried(address, length) for prefix, length in self.prefixes if _under(address, prefix, length)]

    async def known(self) -> None:
        """The prefixes discovered: a discovery that is due is begun, and one begun while none has answered yet is
        waited for until ``RESOLVE_SECONDS`` after it began. Raises nothing: a failure changes nothing."""
        if self._given:
            return
        begun: Future[list[Address]] | None = None
        with self._lock:
            now = time.monotonic()
            if (self._lookup is None or self._lookup.done()) and now >= self._next:
                with contextlib.suppress(RuntimeError):  # no thread now: asked again at the next connection
                    begun = _in_thread(NAT64_NAME, 0)
                    self._lookup, self._until, self._next = begun, now + RESOLVE_SECONDS, now + NAT64_RETRY
            lookup, remaining, waits = self._lookup, self._until - now, not self.answered
        if begun is not None:
            # Out of the lock (a lookup that has ended already runs it here, and it takes the lock), and before any
            # request waits for it: what it shows is kept before its answer is handed to them. Only the event loop
            # comes here, and nothing is awaited in between.
            begun.add_done_callback(self._learned)
        if waits and lookup is not None and remaining > 0 and not lookup.done():
            with contextlib.suppress(Exception):
                await _answer(lookup, NAT64_NAME, remaining)

    def _learned(self, lookup: Future[list[Address]]) -> None:
        """In the thread that ends a discovery: the prefixes its answer shows, and the next discovery ten minutes on;
        when it did not answer, nothing (the next is due a minute after it began)."""
        found = lookup.result() if not lookup.cancelled() and lookup.exception() is None else []
        if not found:
            return
        with self._lock:
            self.prefixes, self.answered = _prefixes(found), True
            self._next = time.monotonic() + NAT64_REFRESH


_LOOKUPS = _Lookups()
_NAT64 = _Nat64()


def forbidden(address: Address) -> bool:
    """Whether ``address`` is one a destination of an identity's own may not lead to. An IPv6 address written with a
    zone (``%eth0``) is: the zone picks an interface of the server's."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id:
            return True
        if address.ipv4_mapped is not None:
            return forbidden(address.ipv4_mapped)
        if any(forbidden(carried) for carried in _NAT64.carried(address)):
            return True
    return not address.is_global or address.is_reserved or any(address in net for net in _NETWORKS)


def _translatable(address: Address) -> bool:
    """Whether a NAT64 of the server's network could take ``address`` to an IPv4 address it carries: an IPv6 address of
    the global space (what is not, or is IPv4-mapped, is judged without the prefixes)."""
    return isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is None and not forbidden(address)


def resolve(host: str, port: int) -> list[Address]:
    """The addresses ``host`` resolves to now (none when it does not resolve, or is no name the system can look up)."""
    try:
        found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError):  # ValueError: a name the system cannot encode (a label too long, a zero byte)
        return []
    out: list[Address] = []
    for info in found:
        try:
            address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            continue
        if address not in out:
            out.append(address)
    return out


def bounded(holder: str | None, seconds: float | None = None) -> Resolver:
    """A resolver for a check made in a worker thread when settings are saved (the dashboard's, the administration's):
    each name resolved in this module's threads as ``pin``'s are, counted for ``holder`` (whose settings they are), and
    every name it is asked waited for ``seconds`` at most together (``RESOLVE_SECONDS``), so that a save holds no thread
    of the server's other work for longer. A name that has not answered by then, one asked once the time is up, or one
    that may not be resolved now (``LOOKUPS_PER_HOLDER``, ``RESOLVER_THREADS``) resolves to nothing here: it is judged
    when the server connects (``pin``)."""
    deadline = time.monotonic() + (RESOLVE_SECONDS if seconds is None else seconds)

    def resolved(host: str, port: int) -> list[Address]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        try:
            return _LOOKUPS.start(host, port, holder).result(timeout=remaining)
        except Exception:  # not in time, or too many of the holder's under way: not judged here, nor said in the log
            return []

    return resolved


def literal(host: str) -> Address | None:
    """``host`` as an address, when it is one written as a number (a dot after it, as after a name, is no part of
    it)."""
    try:
        return ipaddress.ip_address(host.strip("[]").rstrip("."))
    except ValueError:
        return None


def _target(url: str) -> tuple[SplitResult, str, int] | None:
    """``url`` taken apart, its host in small letters and its port (the scheme's when none is written); None when it
    names no host, a port that is not a number from 0 to 65535, a user or a password, or a bracket it does not close."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host or parts.username is not None or parts.password is not None:
        return None
    return parts, host, port or (443 if parts.scheme == "https" else 80)


def problem(url: str, resolver: Resolver | None = None) -> str | None:
    """Why a destination of an identity's own is refused, judged by its host: a literal address by itself; a name, with
    ``resolver`` (``resolve``), by what it resolves to now, and without one not at all (``pin`` judges it when the
    server connects). An address that cannot be used is refused. None: not refused here."""
    target = _target(url)
    if target is None:
        return UNREADABLE
    _, host, port = target
    if (found := literal(host)) is not None:
        return NOT_PUBLIC if forbidden(found) else None
    if resolver is None:
        return None
    return NOT_PUBLIC if any(forbidden(a) for a in resolver(host, port)) else None


async def pin(url: str, *, holder: str | None = None) -> tuple[str, str, dict[str, Any]]:
    """``url`` with its host replaced by the address checked now, the ``Host`` header to send (the host and port as
    written), and the httpx extensions that keep TLS's name (``sni_hostname``) the one in ``url``. A name is resolved in
    this module's own threads, for ``RESOLVE_SECONDS`` at most, its lookup counted for ``holder``: whose request it is
    (an identity's id). While the network has never said its NAT64 prefixes, an IPv6 address a NAT64 could translate
    is not chosen. Raises EgressRefused: the request is not sent."""
    target = _target(url)
    if target is None:
        raise EgressRefused(UNREADABLE)
    parts, host, port = target
    address = literal(host)
    if address is not None:
        found = [address]
    else:
        lookup = _LOOKUPS.start(host, port, holder)
        try:
            found = await _answer(lookup, host, RESOLVE_SECONDS)
        except EgressRefused:
            _LOOKUPS.waited_out(host, port, lookup)
            raise
        if not found:
            raise EgressRefused(f"{host} does not resolve")
    if any(_translatable(a) for a in found):
        await _NAT64.known()  # what such an address would carry under a NAT64 prefix of the server's network
    if any(forbidden(a) for a in found):
        raise EgressRefused(f"{host}: {NOT_PUBLIC}")
    if not _NAT64.answered:  # no prefix is known to be none: such an address could lead inside
        found = [a for a in found if not _translatable(a)]
        if not found:
            raise EgressRefused(f"{host}: {NAT64_UNKNOWN}")
    chosen = found[0]
    where = f"[{chosen}]" if chosen.version == 6 else str(chosen)
    netloc = f"{where}:{parts.port}" if parts.port else where
    pinned = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return pinned, parts.netloc.rpartition("@")[2], {"sni_hostname": host} if parts.scheme == "https" else {}
