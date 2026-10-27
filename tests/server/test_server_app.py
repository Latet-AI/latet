"""An instance of a Latet server: what it opens, keys and refuses at its start (``open_server``), its ear on Valkey
(``listen``, ``Server.heard``: a lost connection, a silent ear, an identity disabled, revoked, rotated, renamed or
removed) and the beat's check of every open space against PostgreSQL (``Server.check``)."""

import asyncio
import dataclasses
import logging
import time

import pytest
import redis

from latet.config import Config, Destination, TermConfig
from latet.engine.types import EntityClass
from latet.gateway.approvals import Batch
from latet.gateway.sigv4 import AwsCredentials
from latet.server import app as app_module
from latet.server.app import FAMILIES, Server, ValkeyUnavailable, listen, open_server
from latet.server.audit import AuditLog
from latet.server.db import Database
from latet.server.identities import AdminTokens, Identities
from latet.server.live import ValkeyAwsSessions
from latet.server.master import MasterKey, MasterKeyError
from latet.server.settings import ServerSettings

from .conftest import drop_listeners
from .support import DATABASE, URL, VALKEY, listening, master_key_file, open_test_server, until

pytestmark = pytest.mark.server


def _terms(runtime):
    return [t.value for t in runtime.config.terms]


def _add_term(value):
    return lambda current: current.model_copy(update={"terms": [*current.terms, TermConfig(value=value)]})


def _gateways(*names: str) -> Config:
    """Shared settings that lead agents to one destination of the company's for each of ``names``."""
    return Config(upstreams=[Destination(name=name, url=f"https://{name}.acme.com") for name in names])


def _destinations(runtime):
    return [d.name for d in runtime.config.upstreams]


def _settings(**changed) -> ServerSettings:
    base = {"url": URL, "database_url": DATABASE, "valkey_url": VALKEY, "master_key_file": master_key_file()}
    return ServerSettings(**{**base, **changed})


@pytest.fixture
def opened(monkeypatch):
    """Every Database an instance opens from now on."""
    found: list[Database] = []

    class Recorded(Database):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            found.append(self)

    monkeypatch.setattr(app_module, "Database", Recorded)
    return found


# --- the start ----------------------------------------------------------------------------------------------------


def test_an_instance_keys_its_parts_with_its_master_key_and_its_settings(schema, prefix):
    open_test_server(schema, prefix).close()  # migrated
    changed = _settings(database_connect_timeout=7, valkey_connections=7)
    server = open_server(changed, clock=lambda: 1_234.5, schema=schema, prefix=prefix)
    try:
        assert server.db._parameters["connect_timeout"] == "7"
        assert server.bus.client.connection_pool.max_connections == 7  # what its calls to Valkey share at most
        credentials = server.master.derive(b"credentials")
        _, secret = server.identities.add_key("Billing CI")
        assert Identities(server.db, server.bus, key=credentials).by_secret(secret) is not None
        assert Identities(server.db, server.bus, key=b"\x01" * 32).by_secret(secret) is None
        admin = server.admin.issue("first")
        assert AdminTokens(server.db, key=credentials, clock=lambda: 1_234.5).check(admin) is not None
        server.audit.record("server", "server.started", None)
        assert AuditLog(server.db, key=server.master.derive(b"audit")).verify().ok
        assert not AuditLog(server.db, key=b"\x02" * 32).verify().ok
        server.shared.set(Config(), actor="admin:a_first00000")
        with server.db.connection() as conn:
            row = conn.execute("SELECT changed FROM settings WHERE owner = '' AND kind = 'shared'").fetchone()
        assert row is not None and row[0] == 1_234.5  # the server's clock
    finally:
        server.close()


def test_an_instance_is_given_no_name_its_process_makes_one(schema, prefix):
    assert "instance" not in {field.name for field in dataclasses.fields(Server) if field.init}
    a, b = open_test_server(schema, prefix), open_test_server(schema, prefix)
    try:
        assert len(a.instance) == 12 and a.instance != b.instance
    finally:
        a.close()
        b.close()


def test_an_instance_with_another_master_key_does_not_start_and_leaves_nothing_open(schema, prefix, tmp_path, opened):
    open_test_server(schema, prefix).close()  # the database first started with the run's key
    other = tmp_path / "other.key"
    other.write_text(MasterKey.generate(), "utf-8")
    with pytest.raises(MasterKeyError, match="LATET_MASTER_KEY_FILE"):
        open_server(_settings(master_key_file=other), schema=schema, prefix=prefix)
    assert opened and opened[-1].pool.closed


def test_an_instance_that_valkey_refuses_does_not_start_and_leaves_nothing_open(schema, prefix, opened):
    open_test_server(schema, prefix).close()
    password = VALKEY.partition("://:")[2].partition("@")[0]
    wrong = VALKEY.replace(f":{password}@", ":not-the-password@") if password else VALKEY + "?not-a-db"
    with pytest.raises(ValkeyUnavailable) as caught:
        open_server(_settings(valkey_url=wrong), schema=schema, prefix=prefix)
    assert "valkey_url" in str(caught.value) and "not-the-password" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None  # no driver's words chained to it
    assert opened and opened[-1].pool.closed


def test_a_start_with_sslkeylogfile_set_says_so_without_its_path(schema, prefix, tmp_path, monkeypatch, caplog):
    keys = tmp_path / "tls-keys.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(keys))
    with caplog.at_level(logging.WARNING, logger="latet.server.app"):
        open_test_server(schema, prefix).close()
    said = [record.getMessage() for record in caplog.records if "SSLKEYLOGFILE" in record.getMessage()]
    assert said and all(str(keys) not in line and "tls-keys" not in line for line in said)


# --- the ear --------------------------------------------------------------------------------------------------------


async def test_a_lost_connection_closes_the_spaces_and_forgets_what_the_instance_kept(server):
    emily, code = server.identities.add_person("Emily Carter")
    _, token, _ = server.identities.enrol(code, "laptop")
    async with listening(server):
        await server.spaces.runtime(emily)
        assert server.identities.by_secret(token) is not None and server.identities.holds(secret=token)
        assert drop_listeners(server.bus) == 1
        await until(lambda: not server.listening.is_set() and server.spaces.count() == 0)
        assert not server.identities.holds(secret=token)  # read again from PostgreSQL
        await until(server.listening.is_set)
        assert await server.spaces.runtime(emily) is not None  # made again at its next use


async def test_an_ear_listens_again_without_waiting_for_its_spaces_to_close(server, monkeypatch):
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        await server.spaces.runtime(emily)
        close = server.spaces._close
        closed = asyncio.Event()

        async def slow(space):  # a space whose recorder takes long to end
            await asyncio.sleep(4.0)
            await close(space)
            closed.set()

        monkeypatch.setattr(server.spaces, "_close", slow)
        assert drop_listeners(server.bus) == 1
        await until(lambda: not server.listening.is_set())
        await until(server.listening.is_set, seconds=3.0)  # after RETRY_SECONDS, not after the closes
        assert not closed.is_set()
        await asyncio.wait_for(closed.wait(), 10)


async def test_an_ear_the_beat_finds_silent_listens_again_with_its_spaces_closed(server):
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        first = await server.spaces.runtime(emily)
        assert not server.ear_silent(30.0)
        server.beat_heard = time.monotonic() - 31.0  # its own beat not heard for three beats
        assert server.ear_silent(30.0)
        server.silenced()  # what the beat does then
        await until(lambda: not server.listening.is_set() and server.spaces.count() == 0)
        await until(server.listening.is_set)
        assert not server.ear_silent(30.0)  # a new subscription, heard afresh
        assert await server.spaces.runtime(emily) is not first


async def test_an_instance_notes_when_it_last_heard_its_own_beat(instances):
    a, b = instances
    async with listening(a, b):
        mine, theirs = a.beat_heard, b.beat_heard
        a.bus.publish("beat", {"instance": a.instance})
        await until(lambda: a.beat_heard > mine)
        await asyncio.sleep(0.2)
        assert b.beat_heard == theirs  # another instance's beat is not its own


async def test_an_ear_cancelled_leaves_the_instance_not_listening(server):
    server.listening = asyncio.Event()
    ear = asyncio.create_task(listen(server))
    await asyncio.wait_for(server.listening.wait(), 5)
    ear.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ear
    assert not server.listening.is_set() and not server.ear_silent(0.0)


# --- an identity that changes, on every instance (Ruling 22, I9, M1, M7, R-21) -----------------------------------------


async def test_disabling_closes_the_space_on_every_instance_even_when_ending_its_keys_fails(
    instances, monkeypatch, caplog
):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")

    def failing(sid=None, subject=None):
        raise redis.TimeoutError("Timeout reading from 127.0.0.1:56379")

    for server in (a, b):
        monkeypatch.setattr(server.sign_ins, "end", failing)
    async with listening(a, b):
        await a.spaces.runtime(emily)
        await b.spaces.runtime(emily)
        with caplog.at_level(logging.WARNING):
            a.identities.disable(emily.id)
            await until(lambda: a.spaces.count() == 0 and b.spaces.count() == 0)
            await until(lambda: any("TimeoutError" in r.getMessage() for r in caplog.records))
    for record in caplog.records:
        assert record.exc_info is None and "56379" not in record.getMessage()  # by its type, no traceback


async def test_ending_an_identitys_sessions_takes_each_step_whatever_the_other_does(server, monkeypatch):
    emily, _ = server.identities.add_person("Emily Carter")
    aws = ValkeyAwsSessions(server.bus, server.master, emily.id)
    session = aws.add_session(AwsCredentials("AKIAEXAMPLE", "secret-of-emily", None))

    def wrong_type(sid=None, subject=None):  # a list of hers written by hand as another kind of key
        raise redis.ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value")

    monkeypatch.setattr(server.sign_ins, "end", wrong_type)
    with pytest.raises(redis.ResponseError):  # what Tasks 14 and 15 call when a token is revoked or a key rotated
        await asyncio.to_thread(server._ended, emily.id)
    assert not aws.has_session(session)  # her AWS sessions ended all the same


async def test_ending_an_identitys_keys_makes_no_scan_of_valkey(instances, prefix, monkeypatch):
    a, b = instances
    emily, code = a.identities.add_person("Emily Carter")
    _, token, _ = a.identities.enrol(code, "laptop")
    client = a.bus.client
    for start in range(0, 100_000, 10_000):  # every other identity's keys, and more
        pipe = client.pipeline(transaction=False)
        for n in range(start, start + 10_000):
            pipe.set(f"{prefix}conv:u_someoneelse:{n}", b"x")
        pipe.execute()
    scans = []
    for server in (a, b):
        scan_iter = server.bus.client.scan_iter

        def counted(*args, scan_iter=scan_iter, **kwargs):
            scans.append(kwargs.get("match"))
            return scan_iter(*args, **kwargs)

        monkeypatch.setattr(server.bus.client, "scan_iter", counted)
    async with listening(a, b):
        sid = b.sign_ins.open(emily.id)
        aws = ValkeyAwsSessions(b.bus, b.master, emily.id)
        session = aws.add_session(AwsCredentials("AKIAEXAMPLE", "secret-of-emily", None))
        a.identities.revoke_secret(token)
        await until(lambda: b.sign_ins.subject_of(sid) is None and not aws.has_session(session))
        a.identities.disable(emily.id)
        await asyncio.sleep(0.5)
    assert scans == []


async def test_revoking_a_token_or_rotating_a_key_closes_the_space_and_a_rename_updates_it(instances):
    a, b = instances
    emily, code = a.identities.add_person("Emily Carter")
    _, token, _ = a.identities.enrol(code, "laptop")
    billing, _ = a.identities.add_key("Billing CI")
    daniel, _ = a.identities.add_person("Daniel Okafor")
    async with listening(a, b):
        for identity in (emily, billing, daniel):
            await b.spaces.runtime(identity)
        a.identities.revoke_secret(token)  # what the token opened ends with it, its long connections too
        await until(lambda: emily.id not in b.spaces._spaces)
        a.identities.rotate_token(billing.id)[1]
        await until(lambda: billing.id not in b.spaces._spaces)
        space = b.spaces._spaces[daniel.id]
        a.identities.update(daniel.id, name="Daniel Okafor-Brandt")
        await until(lambda: space.identity.name == "Daniel Okafor-Brandt")
        assert b.spaces._spaces[daniel.id] is space  # a rename updates the space in place


async def test_a_revocation_whose_announcement_was_lost_closes_the_space_at_the_next_check(instances, monkeypatch):
    a, b = instances
    emily, code = a.identities.add_person("Emily Carter")
    _, laptop, _ = a.identities.enrol(code, "laptop")
    publish = a.bus.publish

    def lost(channel, message):  # Valkey refuses that one publish: no instance hears the revocation
        if channel == "identities":
            raise redis.TimeoutError("Timeout reading from socket")
        publish(channel, message)

    async with listening(a, b):
        await a.spaces.runtime(emily)
        await b.spaces.runtime(emily)
        code = a.identities.code(emily.id)
        assert code is not None
        a.identities.enrol(code, "desktop")  # a new computer of hers closes nothing
        await a.check()
        await b.check()
        assert emily.id in a.spaces._spaces and emily.id in b.spaces._spaces
        monkeypatch.setattr(a.bus, "publish", lost)
        assert a.identities.revoke_secret(laptop)
        await asyncio.sleep(0.3)
        assert emily.id in b.spaces._spaces  # nobody heard it
        await a.check()  # one check: what its tokens were when it was built is no longer what they are
        await b.check()
        assert emily.id not in a.spaces._spaces and emily.id not in b.spaces._spaces


def _keys_of(server, prefix, identity_id) -> list[str]:
    """The raw keys of Valkey that a family of ``FAMILIES`` keeps for ``identity_id``."""
    found = []
    for raw in server.bus.client.scan_iter(match=prefix + "*", count=1000):
        name = raw.decode().removeprefix(prefix)
        family, _, rest = name.partition(":")
        if family in FAMILIES and (rest == identity_id or rest.startswith(identity_id + ":")):
            found.append(name)
    return found


async def test_removing_an_identity_leaves_nothing_of_it_in_valkey(instances, prefix, monkeypatch):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    daniel, _ = a.identities.add_person("Daniel Okafor")
    purged = []
    for server in (a, b):
        purge = server.bus.purge_families

        def counted(owner, families, purge=purge):
            purged.append(owner)
            return purge(owner, families)

        monkeypatch.setattr(server.bus, "purge_families", counted)
    async with listening(a, b):
        for person in (emily, daniel):
            runtime = await a.spaces.runtime(person)  # what a space keeps, as it keeps it
            await asyncio.to_thread(runtime.update_config, _add_term("Mercury Labs"))  # its settings' count too
            entity = runtime.built.vault.intern("EMAIL", EntityClass.PII, "someone@acme.com")
            conversations = runtime.built.engine.conversations
            session = conversations.session("cc:s1")
            session.add({entity.id})
            conversations.update(session)
            conversations.remember(b"r" * 32, "a masked answer")
            conversations.name("resp_1", "cc:s1")
            await runtime.mediator.approvals.open(Batch("cc:s1", None, "claude-code", [], polled=True))
            for family in FAMILIES:  # and a key of every family, by hand
                a.bus.set(f"{family}:{person.id}:x", b"1", 600)
        sid = b.sign_ins.open(emily.id)
        a.bus.set(f"lock:history-prune:{emily.id}", b"held", 600)  # a job's lock ends by itself
        assert len(_keys_of(a, prefix, emily.id)) > len(FAMILIES)
        a.identities.remove(emily.id)
        await until(lambda: not _keys_of(a, prefix, emily.id) and b.sign_ins.subject_of(sid) is None)
        await asyncio.sleep(0.3)
    assert len(_keys_of(a, prefix, daniel.id)) > len(FAMILIES)  # someone else's stay
    assert a.bus.get(f"lock:history-prune:{emily.id}") == b"held"
    assert purged == [emily.id]  # once, by one instance
    with a.db.connection() as conn:
        owners = {row[0] for row in conn.execute("SELECT owner FROM change_counters").fetchall()}
    assert not owners & {emily.id, f"{emily.id}:settings"}  # her vault's count and her settings' go with her
    assert {daniel.id, f"{daniel.id}:settings"} <= owners


async def test_a_purge_that_fails_is_tried_again_by_a_later_check(instances, prefix, monkeypatch):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    for family in FAMILIES:
        a.bus.set(f"{family}:{emily.id}:x", b"1", 600)
    failures, answering = [], []

    def wrong_type(sid=None, subject=None):  # a list of hers written by hand as another kind of key
        raise redis.ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value")

    for server in (a, b):
        purge = server.bus.purge_families

        def failing(owner, families, purge=purge):
            if not answering:  # Valkey times out until the test lets it answer: both instances' attempts fail
                failures.append(owner)
                raise redis.TimeoutError("Timeout reading from socket")
            return purge(owner, families)

        monkeypatch.setattr(server.bus, "purge_families", failing)
        monkeypatch.setattr(server.sign_ins, "end", wrong_type)  # it fails each time, and stops no other part
    async with listening(a, b):
        a.identities.remove(emily.id)
        await until(lambda: bool(failures))
        await asyncio.sleep(0.3)
        assert _keys_of(a, prefix, emily.id)  # not yet: every attempt so far failed
        answering.append(True)
        await a.check()
        await b.check()
        await until(lambda: not _keys_of(a, prefix, emily.id))


# --- the beat's check (I5) ---------------------------------------------------------------------------------------------


async def test_a_change_whose_announcement_was_lost_is_applied_by_the_check(instances, monkeypatch):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    daniel, _ = a.identities.add_person("Daniel Okafor")
    async with listening(a, b):
        here, there = await a.spaces.runtime(emily), await b.spaces.runtime(emily)
        danielle = await b.spaces.runtime(daniel)
        publish = a.bus.publish

        def lost(channel, message):  # Valkey does not answer A's announcements
            if channel.startswith(("identity:", "shared")):
                raise redis.TimeoutError("Timeout reading from socket")
            publish(channel, message)

        monkeypatch.setattr(a.bus, "publish", lost)
        await asyncio.to_thread(here.update_config, _add_term("Mercury Labs"))  # kept, and the call answers as one
        entity = await asyncio.to_thread(here.built.vault.intern, "PASSWORD", EntityClass.SECRET, "hunter2-orchid")
        a.shared.set(_gateways("falcon"), actor="admin:a_first00000")
        await asyncio.sleep(0.3)
        assert "Mercury Labs" not in _terms(there) and there.built.vault.get(entity.id) is None
        assert "falcon" not in _destinations(danielle)
        await b.check()  # a count PostgreSQL has ahead: it may be on its way
        await asyncio.sleep(0.3)
        assert "Mercury Labs" not in _terms(there) and "falcon" not in _destinations(danielle)
        await b.check()  # still ahead at the next beat: B's spaces catch up
        await until(lambda: "Mercury Labs" in _terms(there) and there.built.vault.get(entity.id) == entity)
        await until(lambda: "falcon" in _destinations(danielle) and "falcon" in _destinations(there))


def _shared_announcements_lost(server, monkeypatch) -> None:
    """Valkey does not answer ``server``'s announcements of the shared settings (``SharedSettings.announce`` says it in
    the log: "they find it by its counter")."""
    publish = server.bus.publish

    def lost(channel, message):
        if channel == "shared":
            raise redis.TimeoutError("Timeout reading from socket")
        publish(channel, message)

    monkeypatch.setattr(server.bus, "publish", lost)


async def test_a_space_built_on_an_instance_holding_a_stale_copy_of_the_shared_settings_reads_them_again(
    instances, monkeypatch
):
    a, b = instances
    a.shared.set(_gateways("atlas"), actor="admin:a_first00000")
    daniel, _ = a.identities.add_person("Daniel Okafor")
    async with listening(a, b):
        await asyncio.to_thread(a.shared.get)  # A's copy, as any read keeps it (a build, the administration)
        _shared_announcements_lost(b, monkeypatch)
        b.shared.set(_gateways("atlas", "falcon"), actor="admin:a_first00000")
        await asyncio.sleep(0.3)
        here = await a.spaces.runtime(daniel)  # no space of A's heard of it: built after it, on A's copy
        assert _destinations(here) == ["atlas", "falcon"]


async def test_a_space_counts_the_shared_settings_it_was_built_on_so_the_check_finds_it_behind(instances, monkeypatch):
    a, b = instances
    a.shared.set(_gateways("atlas"), actor="admin:a_first00000")
    daniel, _ = a.identities.add_person("Daniel Okafor")
    async with listening(a, b):
        stale = await asyncio.to_thread(a.shared.counted)  # A's copy, and the count it was read at
        _shared_announcements_lost(b, monkeypatch)
        b.shared.set(_gateways("atlas", "falcon"), actor="admin:a_first00000")
        # A space built on that copy all the same (a change that commits between its reading and the build's own):
        # what it counts is the copy's, which PostgreSQL's is ahead of.
        monkeypatch.setattr(a.shared, "counted", lambda *, at_least=0, copy=True: stale)
        here = await a.spaces.runtime(daniel)
        assert _destinations(here) == ["atlas"]
        monkeypatch.undo()
        await a.check()  # a count PostgreSQL has ahead: it may be on its way
        await a.check()  # still ahead: caught up
        await until(lambda: "falcon" in _destinations(here))


async def test_the_check_forgets_a_copy_of_the_shared_settings_older_than_postgresqls(instances, monkeypatch):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    async with listening(a, b):
        await a.spaces.runtime(emily)  # built, and A's copy kept
        _shared_announcements_lost(b, monkeypatch)
        counter = b.shared.set(Config(history="metadata"), actor="admin:a_first00000")
        await asyncio.sleep(0.3)
        assert a.shared.count is not None and a.shared.count < counter  # A's copy is the one before
        await a.check()  # once is enough for the copy: whatever reads the shared settings next reads them again
        assert a.shared.count is None or a.shared.count == counter
        assert (await asyncio.to_thread(a.shared.get)).history == "metadata"


def test_purging_an_owners_families_reaches_no_other_owner(bus, prefix):
    for name in (
        "conv:u_abcdefghij",
        "conv:u_abcdefghij:1",
        "batch:u_abcdefghij:b1",
        "lock:x:u_abcdefghij",
        "conv:u_abcdefghijk:1",
        "conv:u_abcdefghij2",
        "convs-size:u_abcdefghij",
        "other:u_abcdefghij",
    ):
        bus.set(name, b"1", 600)  # fmt: skip
    assert bus.purge_families("u_abcdefghij", ("conv", "batch", "convs-size")) == 4
    left = sorted(raw.decode().removeprefix(prefix) for raw in bus.client.scan_iter(match=prefix + "*"))
    assert left == ["conv:u_abcdefghij2", "conv:u_abcdefghijk:1", "lock:x:u_abcdefghij", "other:u_abcdefghij"]
    for owner in ("", "u_a:b"):
        with pytest.raises(ValueError, match="owner"):
            bus.purge_families(owner, ("conv",))


async def test_an_ill_formed_id_in_an_identitys_message_ends_nothing(server):
    async with listening(server):
        for identity_id in ("", "u_*", "conv", "u_emilycarte:x"):
            await server.heard("identities", {"id": identity_id, "event": "removed"})
        assert server._cleanups == {}


async def test_the_check_closes_the_space_of_an_identity_disabled_or_removed_unheard(instances):
    a, b = instances
    emily, _ = a.identities.add_person("Emily Carter")
    daniel, _ = a.identities.add_person("Daniel Okafor")
    olivia, _ = a.identities.add_person("Olivia Brandt")
    async with listening(a, b):
        for person in (emily, daniel, olivia):
            await b.spaces.runtime(person)
        sid = b.sign_ins.open(emily.id)
        with a.db.connection() as conn:  # no message: as when it was lost
            conn.execute("UPDATE identities SET disabled = TRUE WHERE id = %s", (emily.id,))
            conn.execute("DELETE FROM identities WHERE id = %s", (daniel.id,))
        await b.check()
        assert set(b.spaces._spaces) == {olivia.id}
        await until(lambda: b.sign_ins.subject_of(sid) is None)
