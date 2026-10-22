"""The shared settings in PostgreSQL: sealed with the master key, set on one instance, announced, and read on another
once it forgets its copy; each change counted in its own transaction and announced with its count once committed; a
change made from settings read before another change refused; a change written in the caller's transaction when it is
given one; a read that began before a change was told keeps nothing of what it read. Rules for everyone kept in the
row by an earlier version are in force nowhere, counted in the log once and never shown; an edition that shares rules
puts them in force through its hook."""

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from latet.config import Config, Destination, IdentityConfig, Profile, ProjectEntry, TermConfig
from latet.runtime import digest
from latet.server import shared as shared_module
from latet.server.master import MasterKey
from latet.server.shared import SharedSettings, SharedTooLarge, StaleSettings, UnreadableShared

from .conftest import DATABASE, deliver, dump

pytestmark = pytest.mark.server

MASTER = MasterKey([b"\x05" * 32])
ADMIN, OTHER_ADMIN = "admin:a_first00000", "admin:a_second0000"
# Three shared settings a server needs to run, told apart (rules for everyone are an edition's: none here).
FALCON = Config(history="metadata")
MERCURY = Config(retention_days=3)
ORION = Config(ocr="off")
CANARY = "Zanzibar Holdings 4417"


class Recorder:
    """A bus that keeps what it is asked to publish."""

    def __init__(self):
        self.sent = []

    def publish(self, channel, message):
        self.sent.append((channel, dict(message)))


class Silent:
    """A bus whose Valkey does not answer."""

    def publish(self, channel, message):
        raise ConnectionError("Valkey at 10.0.3.8:6379 does not answer")


def _read(db, query, *args):
    with db.connection() as conn:
        return conn.execute(query, args).fetchall()


def _counter(db) -> int | None:
    """The shared settings' count of changes, as PostgreSQL has it."""
    found = _read(db, "SELECT counter FROM change_counters WHERE owner = ''")
    return found[0][0] if found else None


def _fresh(db) -> Config:
    """The shared settings as an instance that kept nothing reads them."""
    return SharedSettings(db, Recorder(), MASTER).get()


def _until(check, seconds=20.0):
    """Whether ``check()`` came true within ``seconds``: asked again until it does (an effect on another connection)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


def _waiting(db, holder: int) -> int:
    """How many connections wait for a lock that the connection whose backend is ``holder`` holds: whatever their
    statement, and none of another test's (the shared PostgreSQL serves others at once)."""
    found = _read(db, "SELECT COUNT(*) FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))", holder)
    return found[0][0]


async def test_shared_settings_set_on_one_instance_are_announced_and_read_on_another(db, buses):
    a, b = buses
    master = MasterKey([b"\x05" * 32])
    here, there = SharedSettings(db, a, master), SharedSettings(db, b, master)
    assert there.get() == Config()
    heard = asyncio.Event()

    class Ear:
        def heard(self, message):
            heard.set()

    async with deliver(b, "shared", Ear()):
        gateway = Destination(name="mercury", url="https://llm.mercury-labs.example")
        here.set(Config(upstreams=[gateway]), actor="admin:a_first00000")
        await asyncio.wait_for(heard.wait(), 5)
    assert there.get() == Config()  # its copy, until it forgets it (on the message)
    there.forget()
    assert there.get().upstreams == [gateway]
    assert b"mercury" not in dump(db)


async def test_each_change_is_counted_in_its_transaction_and_announced_with_its_count(db, buses):
    a, b = buses
    here, heard, both = SharedSettings(db, a, MASTER), [], asyncio.Event()

    class Ear:
        def heard(self, message):
            heard.append(message)
            if len(heard) == 2:
                both.set()

    async with deliver(b, "shared", Ear()):
        assert here.set(FALCON, actor=ADMIN) == 1
        assert here.set(MERCURY, actor=ADMIN) == 2
        await asyncio.wait_for(both.wait(), 5)
    assert heard == [{"changed": True, "counter": 1}, {"changed": True, "counter": 2}]
    assert _read(db, "SELECT owner, counter FROM change_counters") == [("", 2)]
    assert here.get() == MERCURY  # the instance that made the change reads it again too


def test_a_change_is_stamped_with_the_servers_clock_and_who_made_it(db, bus, clock):
    SharedSettings(db, bus, MASTER, clock=clock).set(FALCON, actor=ADMIN)
    assert _read(db, "SELECT changed, changed_by FROM settings WHERE owner = ''") == [(clock.now, ADMIN)]


def test_a_change_made_from_settings_read_before_another_change_is_refused(db):
    first, second = SharedSettings(db, Recorder(), MASTER), SharedSettings(db, Recorder(), MASTER)
    read = digest(first.get())  # what the GET of the administration answers as the etag
    assert digest(second.get()) == read
    assert first.set(FALCON, actor=ADMIN, etag=read) == 1
    with pytest.raises(StaleSettings, match="read them again"):
        second.set(MERCURY, actor=OTHER_ADMIN, etag=read)
    assert _fresh(db) == FALCON and _counter(db) == 1  # neither written nor counted
    second.forget()
    assert second.set(MERCURY, actor=OTHER_ADMIN, etag=digest(second.get())) == 2  # read again, it goes
    assert _fresh(db) == MERCURY
    assert second.set(ORION, actor=OTHER_ADMIN) == 3 and _fresh(db) == ORION  # without an etag: written whole


@pytest.mark.parametrize("before", [None, FALCON], ids=["the first change", "a later change"])
def test_two_changes_made_at_once_from_one_reading_are_never_both_kept(db, before):
    first, second = SharedSettings(db, Recorder(), MASTER), SharedSettings(db, Recorder(), MASTER)
    if before is not None:
        first.set(before, actor=ADMIN)
    count = _counter(db) or 0
    read = digest(_fresh(db))
    with ThreadPoolExecutor(1) as pool:
        with db.transaction() as conn:
            first.set(MERCURY, actor=ADMIN, etag=read, conn=conn)  # its locks are held until this transaction ends
            holder = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
            racing = pool.submit(second.set, ORION, actor=OTHER_ADMIN, etag=read)
            assert _until(lambda: _waiting(db, holder) >= 1)  # the other change waits for them, whatever it read
            assert not racing.done()
        with pytest.raises(StaleSettings):
            racing.result(30)  # then reads what the first one kept, and is refused
    assert _fresh(db) == MERCURY and _counter(db) == count + 1


def _changed_and_stopped(db, shared):
    with db.transaction() as conn:
        shared.set(MERCURY, actor=ADMIN, conn=conn)
        raise RuntimeError("stopped after the change was written")


def test_a_change_in_the_callers_transaction_is_kept_or_lost_with_it_and_told_once_it_is_kept(db):
    bus = Recorder()
    shared = SharedSettings(db, bus, MASTER)
    with db.transaction() as conn:
        counter = shared.set(FALCON, actor=ADMIN, conn=conn)
        assert _fresh(db) == Config()  # not yet: its transaction has not ended
    assert bus.sent == [] and _fresh(db) == FALCON  # kept, and nothing told before its caller says it was kept
    shared.announce(counter)
    assert bus.sent == [("shared", {"changed": True, "counter": 1})]
    with pytest.raises(RuntimeError, match="stopped"):
        _changed_and_stopped(db, shared)
    assert _fresh(db) == FALCON and _counter(db) == 1  # neither the change nor its count
    assert len(bus.sent) == 1


def test_a_refused_change_in_the_callers_transaction_leaves_nothing_of_itself_there(db):
    """The caller goes on (it records the refused call, say) and commits: the count is the one before, so that no
    instance takes the refusal for a change and applies its settings again."""
    shared = SharedSettings(db, Recorder(), MASTER)
    shared.set(FALCON, actor=ADMIN)
    with db.transaction() as conn:
        with pytest.raises(StaleSettings):
            shared.set(MERCURY, actor=ADMIN, etag="the etag of an older reading", conn=conn)
        conn.execute("INSERT INTO identities (id, kind, name, created) VALUES ('u_emilycarte', 'person', 'Emily', 0)")
    assert _counter(db) == 1 and _fresh(db) == FALCON
    assert _read(db, "SELECT id FROM identities") == [("u_emilycarte",)]  # what the caller did is kept


def test_the_shared_settings_refuse_what_is_each_identitys_or_would_write_on_the_server(db):
    shared = SharedSettings(db, Recorder(), MASTER)
    refused = {
        "identity": Config(identity=IdentityConfig(usernames=["emily"])),
        "projects": Config(projects=[ProjectEntry(path="/srv/latet")]),
        "capture_dir": Config(capture_dir="/tmp/raw"),
    }
    for key, config in refused.items():
        with pytest.raises(ValueError, match=key):
            shared.set(config, actor=ADMIN)
    assert _fresh(db) == Config() and _counter(db) is None  # nothing written, nothing counted


def test_each_caller_gets_its_own_copy_of_the_shared_settings(db):
    shared = SharedSettings(db, Recorder(), MASTER)
    shared.set(FALCON, actor=ADMIN)
    read = shared.get()  # read from PostgreSQL, and kept
    read.terms.append(TermConfig(value="Orion"))
    kept = shared.get()  # from what this instance keeps
    kept.terms.clear()
    assert shared.get() == FALCON  # what a caller does with its copy changes nobody else's


def test_a_change_is_not_written_in_a_connection_in_autocommit_mode(db):
    """There its count and its settings would be kept apart, and the lock that orders two changes would end at once."""
    shared = SharedSettings(db, Recorder(), MASTER)
    options = f"-c search_path={db.schema}"
    with (
        psycopg.connect(DATABASE, autocommit=True, options=options) as conn,
        pytest.raises(ValueError, match="autocommit"),
    ):
        shared.set(FALCON, actor=ADMIN, conn=conn)
    assert _fresh(db) == Config() and _counter(db) is None


def test_a_change_that_cannot_be_told_is_kept_and_answered_and_the_log_says_why_by_its_type(db, caplog):
    shared = SharedSettings(db, Silent(), MASTER)
    with caplog.at_level(logging.WARNING, logger="latet.server.shared"):
        assert shared.set(FALCON, actor=ADMIN) == 1  # it took effect: it is answered as a change that did
    assert shared.get() == FALCON and _counter(db) == 1  # the other instances find it by its count
    [said] = [record.getMessage() for record in caplog.records if record.name == "latet.server.shared"]
    assert "ConnectionError" in said and "10.0.3.8" not in said


def test_a_read_that_began_before_a_change_was_told_keeps_nothing_of_what_it_read(db, monkeypatch):
    here, there = SharedSettings(db, Recorder(), MASTER), SharedSettings(db, Recorder(), MASTER)
    read, go = threading.Event(), threading.Event()
    real = there._read

    def held():
        found = real()  # PostgreSQL as it is now
        if threading.current_thread() is not threading.main_thread():
            read.set()
            assert go.wait(10), "the test did not let the read go"
        return found

    monkeypatch.setattr(there, "_read", held)
    with ThreadPoolExecutor(1) as pool:
        racing = pool.submit(there.get)  # a space being built here reads the shared settings
        try:
            assert read.wait(10)  # it has read them as they were, and waits
            here.set(FALCON, actor=ADMIN)  # changed on another instance...
            there.forget()  # ...and told: what an instance does when `shared` says they changed
        finally:
            go.set()
        assert racing.result(10) == Config()  # it answers what it read
    assert there.get() == FALCON  # and kept nothing of it: the next read has the change


def test_a_copy_is_kept_with_its_count_and_one_behind_a_count_read_since_is_read_again(db):
    here, there = SharedSettings(db, Recorder(), MASTER), SharedSettings(db, Silent(), MASTER)
    here.set(FALCON, actor=ADMIN)
    assert here.counted() == (FALCON, 1) and here.count == 1
    counter = there.set(MERCURY, actor=ADMIN)  # its announcement lost: here keeps the copy from before
    assert here.get() == FALCON and here.count == 1
    assert here.counted(at_least=counter) == (MERCURY, counter)  # a count read since: the copy is read again
    assert here.get() == MERCURY and here.count == counter


def test_a_read_of_older_settings_that_ends_last_keeps_no_copy_over_a_newer_one(db, monkeypatch):
    here, there = SharedSettings(db, Recorder(), MASTER), SharedSettings(db, Silent(), MASTER)
    here.set(FALCON, actor=ADMIN)
    read, go = threading.Event(), threading.Event()
    real = here._read

    def held():
        found = real()  # PostgreSQL as it is now
        if threading.current_thread() is not threading.main_thread():
            read.set()
            assert go.wait(10), "the test did not let the read go"
        return found

    monkeypatch.setattr(here, "_read", held)
    with ThreadPoolExecutor(1) as pool:
        slow = pool.submit(here.get)  # a space being built here reads the shared settings, and is slow
        try:
            assert read.wait(10)
            counter = there.set(MERCURY, actor=ADMIN)  # changed meanwhile, its announcement lost
            assert here.counted(at_least=counter) == (MERCURY, counter)  # a build that read the count since
        finally:
            go.set()
        assert slow.result(10) == FALCON  # it answers what it read
    assert here.get() == MERCURY and here.count == counter  # and keeps nothing older over the newer copy


def _kept(db, data: bytes) -> None:
    """The shared settings' row, written by hand."""
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO settings (owner, kind, data, changed) VALUES ('', 'shared', %s, 0)"
            " ON CONFLICT (owner, kind) DO UPDATE SET data = EXCLUDED.data",
            (data,),
        )


def test_shared_settings_that_do_not_open_or_are_not_valid_are_refused_without_a_word_of_them(db):
    shared = SharedSettings(db, Recorder(), MASTER)
    unknown = {"identity": {"emails": "emily.carter@acme.com"}}  # kept by another version of Latet, say
    _kept(db, MASTER.seal(json.dumps(unknown).encode(), "settings:shared"))
    with pytest.raises(ValueError, match=r"identity\.emails \(list_type\)") as raised:
        shared.get()
    assert "emily" not in str(raised.value) and raised.value.__context__ is None
    written = {
        "with another master key": MasterKey([b"\x06" * 32]).seal(b"{}", "settings:shared"),
        "for another place": MASTER.seal(b"{}", "settings:own"),
        "by hand": b'{"terms": [{"value": "Mercury Labs"}]}',
        "as no JSON": MASTER.seal(b"Mercury Labs", "settings:shared"),
    }
    for how, data in written.items():
        _kept(db, data)
        with pytest.raises(ValueError, match="cannot be read") as raised:
            shared.get()
        assert "Mercury" not in str(raised.value) and raised.value.__context__ is None, how
    with pytest.raises(ValueError, match="cannot be read"):  # a change from what was read needs it read
        shared.set(FALCON, actor=ADMIN, etag=digest(Config()))
    assert shared.set(FALCON, actor=ADMIN) == 1 and shared.get() == FALCON  # written whole, it is set right


def test_a_row_found_unreadable_is_replaced_only_while_it_still_is(db):
    shared = SharedSettings(db, Recorder(), MASTER)
    _kept(db, b'{"terms": [{"value": "Mercury Labs"}]}')  # written by hand
    with pytest.raises(UnreadableShared, match="cannot be read") as raised:
        shared.stored()
    assert "Mercury" not in str(raised.value)
    shared.set(FALCON, actor=ADMIN)  # another administrator wrote them whole meanwhile: they open now
    with pytest.raises(StaleSettings):
        shared.set(MERCURY, actor=OTHER_ADMIN, unreadable=True)  # read them again: nothing is written
    assert _fresh(db) == FALCON
    _kept(db, b"by hand again")
    assert shared.set(MERCURY, actor=OTHER_ADMIN, unreadable=True) and _fresh(db) == MERCURY


def test_stored_reads_postgresql_and_keeps_and_forgets_nothing(db):
    here = SharedSettings(db, Recorder(), MASTER)
    assert here.get() == Config() and here.count == 0
    SharedSettings(db, Recorder(), MASTER).set(FALCON, actor=ADMIN)  # another instance's change, not heard here
    assert here.stored() == FALCON
    assert here.count == 0 and here.get() == Config()  # its copy, as it was: kept and behind


def test_the_shared_settings_in_force_hold_none_of_the_rules_kept_in_the_row(db, caplog):
    """Rules an earlier version kept among the shared settings stay in the row as they are (the administration reads
    them, counted), in force for nobody: the instance's copy, a count read since, a copy read again all hold the
    server's settings alone; the log says once per change that rules are kept, by their counts, never a value."""
    stored = FALCON.model_copy(
        update={"terms": [TermConfig(value=CANARY)], "profile": Profile.MAX, "allow_values": ["support@acme.com"]}
    )
    here = SharedSettings(db, Recorder(), MASTER)
    with caplog.at_level(logging.WARNING, logger="latet.server.shared"):
        here.set(stored, actor=ADMIN)
        shown, count = here.counted()
        assert here.counted(at_least=count)[0] == shown and here.get(copy=False) == shown
        here.forget()
        assert here.get() == FALCON
    assert shown == FALCON and count == 1
    assert here.stored() == stored and set(here.kept()) == {"terms", "profile", "allow_values"}
    said = [r.getMessage() for r in caplog.records if "earlier version" in r.getMessage()]
    assert len(said) == 1 and "1 term" in said[0] and "profile" in said[0]
    assert CANARY not in caplog.text and "support@acme.com" not in caplog.text


def test_an_edition_that_shares_rules_puts_them_in_force_through_its_hook(db):
    stored = FALCON.model_copy(update={"terms": [TermConfig(value="Project Falcon")], "profile": Profile.MAX})
    sharing = SharedSettings(db, Recorder(), MASTER, rules=lambda row: {"terms": row.terms})
    sharing.set(stored, actor=ADMIN)
    shown = sharing.get()
    assert [t.value for t in shown.terms] == ["Project Falcon"] and shown.history == "metadata"
    assert shown.profile == Config().profile and set(sharing.kept()) == {"profile"}  # the edition gave terms alone


def _weight(config: Config) -> int:
    """What ``config`` weighs sealed as the shared settings."""
    return len(MASTER.seal(json.dumps(config.model_dump(mode="json")).encode(), "settings:shared"))


def test_shared_settings_past_the_limit_are_not_kept(db, monkeypatch):
    shared = SharedSettings(db, Recorder(), MASTER)
    many = Config(terms=[TermConfig(value=f"Client {n}") for n in range(50)])
    monkeypatch.setattr(shared_module, "SETTINGS_LIMIT", _weight(FALCON))
    assert shared.set(FALCON, actor=ADMIN) == 1  # at the limit: kept
    with pytest.raises(SharedTooLarge, match="more than a server keeps") as raised:
        shared.set(many, actor=ADMIN)
    assert "Client" not in str(raised.value) and _counter(db) == 1 and _fresh(db) == FALCON  # nothing written
