"""The server's policy in PostgreSQL: one row whose text is sealed with the master key, bound to the row's etag and
time, written in a caller's transaction; heard by every instance on Valkey, its license weighed again when a license is
installed anywhere; and an instance that cannot read a policy stored after its spaces were built closes them and
builds none below it."""

import hashlib
import threading
import time

import pytest

from latet.ee.policy import PolicyError
from latet.ee.policy_server import CHANNEL, PgPolicyStore, PolicyUnavailable, ServerPolicy, StoredPolicy

from ..server.support import listening, until
from .servers import services

pytestmark = [pytest.mark.server, services]

POLICY = "latet_rules: 1\nprofile: max\nterms: [{value: Project Falcon, type: PROJECT}]\n"
LASTING = {"expires": "2099-12-31"}


def _stored(text=POLICY, set_at=1_791_191_520.0, by="admin:t1"):
    return StoredPolicy(text, hashlib.sha256(text.encode("utf-8")).hexdigest(), set_at, by)


def _row(server):
    with server.db.connection() as conn:
        return conn.execute("SELECT etag, sealed, set_at, set_by FROM ee_policy").fetchall()


def test_the_policy_is_one_row_its_text_sealed(server):
    store = PgPolicyStore(server.db, server.master)
    assert (store.stamp(), store.load()) == (None, None)
    store.save(_stored())
    store.save(_stored(set_at=1_791_191_580.0, by="admin:t2"))  # set again: the one row
    [(etag, sealed, set_at, set_by)] = _row(server)
    assert b"Project Falcon" not in bytes(sealed) and (set_at, set_by) == (1_791_191_580.0, "admin:t2")
    assert store.stamp() == (etag, 1_791_191_580.0)
    loaded = store.load()
    assert (loaded.text, loaded.etag, loaded.set_by) == (POLICY, etag, "admin:t2")
    assert store.delete() is True and store.delete() is False and store.load() is None


@pytest.mark.parametrize(
    ("column", "changed"), [("etag", "'e' || etag"), ("set_at", "set_at + 1"), ("set_by", "'admin:t_ff99'")]
)
def test_a_sealed_text_does_not_open_once_its_row_is_changed_apart_from_it(server, column, changed):
    """L3: the sealing names the row's etag, time and author: a row changed in PostgreSQL alone is no policy, nor
    does `latet server policy show` name another administrator than the one who set it."""
    store = PgPolicyStore(server.db, server.master)
    store.save(_stored())
    with server.db.transaction() as conn:
        conn.execute(f"UPDATE ee_policy SET {column} = {changed}")
    with pytest.raises(PolicyError, match="cannot be opened with this master key") as raised:
        store.load()
    assert "Project Falcon" not in str(raised.value)


def test_a_policy_set_by_a_clock_of_whole_seconds_opens_on_every_instance(server):
    """Whole-branch review, Minor 4: the sealing names the time as it is written. A clock answering an int sealed
    `...:1791191520:...`, and every instance read the row's float back as `...:1791191520.0:...`: closed everywhere."""
    store = PgPolicyStore(server.db, server.master)
    store.save(_stored(set_at=1_791_191_520))
    assert store.load().text == POLICY
    policy = ServerPolicy(
        store, publish=lambda channel, message: True, origin="", licensed=lambda: True, clock=lambda: 1_791_191_580
    )
    assert isinstance(policy.set(POLICY, actor="admin:t1").received, float)
    assert store.load().set_at == 1_791_191_580.0 and policy.refresh() is False and policy.floor() is not None


def test_a_policy_set_in_a_transaction_that_rolls_back_is_not_kept(server):
    store = PgPolicyStore(server.db, server.master)

    def route() -> None:
        with server.db.transaction() as conn:
            store.save(_stored(), conn=conn)
            raise RuntimeError("the route's entry could not be written")

    with pytest.raises(RuntimeError):
        route()
    assert store.load() is None


async def test_an_instance_that_cannot_read_a_policy_stored_since_its_spaces_were_built_closes_them(server, licensed):
    """M3: no space goes on below a policy the instance cannot read; none is built below it either."""
    server.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        await server.spaces.runtime(emily)
        # Set by an instance of a newer release: sealed for this server, in a format this one cannot read.
        PgPolicyStore(server.db, server.master).save(_stored("latet_rules: 99\nprofile: max\n"))
        assert server.edition.policy.refresh() is True
        await until(lambda: server.spaces.count() == 0)
        with pytest.raises(PolicyUnavailable, match="newer Latet"):
            await server.spaces.runtime(emily)
        assert server.edition.policy.summary()["problems"][0]["level"] == "fail"


async def test_a_policy_set_on_one_instance_raises_every_space_of_another_at_once(instances, licensed):
    a, b = instances.open(), instances.open()
    a.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    b.edition.refresh()
    emily, _ = b.identities.add_person("Emily Carter")
    async with listening(b):
        runtime = await b.spaces.runtime(emily)
        assert not runtime.in_use().locked_terms
        a.edition.policy.set(POLICY, actor="admin:t1")
        a.edition.policy.committed()  # told on ee:policy: b's listener reads it, b's spaces raise
        await until(lambda: bool(runtime.in_use().locked_terms))
        assert "Project Falcon" not in runtime.built.masker.mask("the Project Falcon plan").text
        assert b.edition.floor(None).etag == a.edition.floor(None).etag


def test_a_license_installed_on_one_instance_brings_the_policy_in_on_another(instances, licensed):
    a, b = instances.open(), instances.open()
    a.edition.policy.set(POLICY, actor="admin:t1")
    a.edition.policy.committed()
    b.edition.refresh()
    assert b.edition.floor(None) is None and "not in force" in b.edition.policy.summary()["problems"][0]["text"]
    a.edition.install_license(licensed.make(**LASTING), actor="admin:t1")  # told on ee:license
    for _ in range(250):  # b's listener weighs the policy again at once, not at its next check
        if b.edition.floor(None) is not None:
            break
        time.sleep(0.02)
    assert b.edition.floor(None).rules.profile == "max"


def test_an_edition_whose_policy_cannot_be_read_at_start_leaves_nothing_running(server, monkeypatch):
    """The database not answering when the policy is made stops the start: the listener started before stops too."""
    from latet.edition import server_context
    from latet.ee import edition as edition_module

    def unanswered(server, **given):
        raise ConnectionError("connection to 10.0.0.5 refused")

    monkeypatch.setattr(edition_module, "make_policy", unanswered)
    before = {t for t in threading.enumerate() if t.name == "latet-ee-listener"}
    with pytest.raises(ConnectionError):
        edition_module.EnterpriseEdition(server_context(server))
    assert {t for t in threading.enumerate() if t.name == "latet-ee-listener" and t.is_alive()} <= before


def test_the_check_of_an_instance_stops_with_its_edition(server):
    policy = server.edition.policy
    assert policy._thread.is_alive() and policy._thread.name == "latet-policy"
    server.edition.close()
    assert not policy._thread.is_alive()


def test_a_policy_change_is_announced_with_nothing_in_it(server, monkeypatch):
    published = []
    monkeypatch.setattr(server.bus, "publish", lambda channel, message: published.append((channel, dict(message))))
    server.edition.policy.set(POLICY, actor="admin:t1")
    server.edition.policy.committed()
    assert published == [(CHANNEL, {})]
