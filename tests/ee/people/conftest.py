"""The fixtures of the tests of people, groups, roles and sign-in on a server (server_support.py): E1's clock, which
these tests also move by seconds, and a server of two instances on E1's `instances`, a license of every feature
installed (E1's `licensed`: Latet carries the tests' public key)."""

import pytest

from .server_support import Clock, start, stop


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cluster(instances, licensed, clock):
    started = start(instances, licensed, clock)
    yield started
    stop(started)
