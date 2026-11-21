"""E5's tests: E3's cluster of two instances of one licensed server, and its clock (E1's licenses are valid then)."""

import pytest

from ..people.server_support import Clock, start, stop


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cluster(instances, licensed, clock):
    found = start(instances, licensed, clock)
    try:
        yield found
    finally:
        stop(found)
