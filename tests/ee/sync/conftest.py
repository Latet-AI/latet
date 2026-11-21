"""E6's tests: `store`, each kind of store the same tests run against (in memory, and on PostgreSQL: marker `server`,
E1's `server` fixture, an instance on a schema of the test's own, its terms sealed with the server's own master key;
skipped, saying why, without the test services), and E3's cluster of two instances of one licensed server with its
clock, as E5's tests have them."""

import pytest

from latet.ee.sync.store import MemoryTermStore

from ..people.server_support import Clock, start, stop
from ..servers import services


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


@pytest.fixture(params=["memory", pytest.param("postgres", marks=[pytest.mark.server, services])])
def store(request):
    if request.param == "memory":
        made = MemoryTermStore()
        yield made
        made.close()
        return
    from latet.ee.sync.pgstore import SEALER_PURPOSE, PgTermStore

    server = request.getfixturevalue("server")  # E1's: one instance, on a schema of this test's own
    yield PgTermStore(server.db, server.master.sealer(SEALER_PURPOSE))
