"""What the gate and the machines' calls count a client by (Task 15's ruling for the one limiter): an IPv4 address as
itself, an IPv6 one by its /64, an IPv4-mapped one as its IPv4; and the memory of the credentials an instance found
valid, kept as keyed hashes within a bound."""

import pytest

from latet.config import IdentityConfig
from latet.server import machines


@pytest.mark.parametrize(
    ("client", "bucket"),
    [
        ("198.51.100.7", "198.51.100.7"),
        ("::ffff:198.51.100.7", "198.51.100.7"),
        ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
        ("2001:0db8:0001:0002:0000:0000:0000:0001", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
        ("fe80::1%eth0", "fe80::/64"),
        ("testclient", "testclient"),
    ],
)
def test_a_client_is_counted_by_its_address_an_ipv6_one_by_its_64(client, bucket):
    assert machines.bucket_of(client) == bucket


def test_a_client_with_no_address_is_counted_as_unknown():
    assert machines.client_of({}) == "unknown" == machines.bucket_of(machines.client_of({}))


def test_the_credentials_found_valid_are_kept_by_keyed_hash_for_an_hour_within_a_bound(monkeypatch):
    now = [1_000_000.0]
    known = machines.Known(clock=lambda: now[0])
    known.keep("latet_pat_alpha")
    assert known.holds("latet_pat_alpha") and not machines.Known().holds("latet_pat_alpha")  # each memory its own
    assert not any(b"alpha" in hashed for hashed in known._found)  # a keyed hash, never the credential
    now[0] += machines.KNOWN_SECONDS
    assert not known.holds("latet_pat_alpha")  # an hour on: it meets the limit again
    monkeypatch.setattr(machines, "KNOWN_SIZE", 3)
    for name in ("one", "two", "three", "four"):
        known.keep(name)
    assert not known.holds("one") and known.holds("four")  # the oldest gone past the bound
    known.forget("four")
    assert not known.holds("four")


class _Slots:
    """The shared limiter in memory (``Bus.slot`` and ``give_back``): None within the limit, else the window left."""

    def __init__(self) -> None:
        self.count: dict[str, int] = {}

    def slot(self, key: str, limit: int, window: float) -> float | None:
        self.count[key] = self.count.get(key, 0) + 1
        return None if self.count[key] <= limit else window

    def give_back(self, key: str) -> None:
        self.count[key] = max(0, self.count.get(key, 0) - 1)


@pytest.mark.parametrize("addresses", [4, 300])
def test_addresses_that_spend_their_budget_are_refused_within_their_own_buckets_and_nowhere_else(addresses):
    """F-R3 (the residuals' check, its probe) and A2: addresses that spend their one budget of misses, every kind
    together, are refused within their own buckets alone: a fresh address's sign-in link, enrolment and lookups are not
    refused, where a ceiling of 1,000 misses a minute, every address together, refused them all after the fourth."""
    bus, limits = _Slots(), machines.Limits()
    for n in range(addresses):
        key, spent = machines.miss_key(f"203.0.{n // 256}.{n % 256}"), 0
        while not limits.refuses(key):
            machines.counted(bus, limits, key, machines.LOOKUP_LIMIT, machines.LOOKUP_WINDOW)
            spent += 1
        assert spent == machines.LOOKUP_LIMIT + 1  # its budget, and the miss that found it past
    fresh = "198.51.100.200"
    assert not limits.refuses(machines.miss_key(fresh)) and not limits.refuses(f"enrol:{fresh}")


def test_the_single_user_name_counts_toward_the_cap_as_the_single_host_name_does():
    names = [f"user{n}" for n in range(machines.NAMES_KEPT)]
    assert not machines._past_the_cap(IdentityConfig(usernames=names))
    assert machines._past_the_cap(IdentityConfig(username="emily", usernames=names))
    assert not machines._past_the_cap(IdentityConfig(username="emily", usernames=names[1:]))
    assert machines._past_the_cap(IdentityConfig(hostname="emily-laptop", hostnames=names))
