"""The Enterprise tables of a server, in PostgreSQL: its license (since when more users than bought are counted, and
the trials that ended), its settings section by section, and their secrets sealed with the master key. Every instance
reads the same ones, and keeps a copy for a few seconds, or until it is dropped."""

import contextlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from cryptography.exceptions import InvalidTag

from latet.ee.license import Licensing, State
from latet.ee.secrets import MAX_BYTES as SECRET_BYTES
from latet.ee.server import REFRESH_SECONDS, DatabaseLicenses, DatabaseSecrets, DatabaseSettings
from latet.ee.settings import EnterpriseSettingsError, UnknownSection
from latet.server.sealing import FORMAT, version_of

from .licenses import ENDED_TRIAL, Clock
from .servers import services

pytestmark = [pytest.mark.server, services]


def test_the_enterprise_tables_are_migrated_with_the_servers(server):
    with server.db.connection() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        ).fetchall()
    assert {"ee_license", "ee_settings", "ee_secrets"} <= {row[0] for row in rows}


def test_the_license_is_kept_with_its_version_and_removing_it_keeps_the_grace(server, signer):
    licenses = DatabaseLicenses(server.db, clock=lambda: 1.0)
    assert licenses.read() is None and licenses.delete() is False and licenses.over_since() is None
    licenses.write(signer.make())
    first = licenses.read()
    assert first is not None and first.source == "server" and first.data == signer.make()
    assert licenses.keep_over_since(100.0) == 100.0 and licenses.keep_over_since(200.0) == 100.0  # the first kept
    assert licenses.delete() is True and licenses.read() is None and licenses.over_since() == 100.0
    licenses.write(signer.make(id="L-2027-0002"))
    assert licenses.read().version != first.version and licenses.over_since() == 100.0
    assert licenses.keep_over_since(None) is None and licenses.over_since() is None


def test_the_features_the_installing_instance_verified_are_kept_with_the_document_and_removed_with_it(server, signer):
    licenses = DatabaseLicenses(server.db, clock=lambda: 1.0)
    licenses.write(signer.make(), frozenset({"sso", "reports"}))
    assert licenses.read().features == frozenset({"reports", "sso"})
    licenses.write(signer.make(), frozenset())  # a license that switches nothing on
    assert licenses.read().features == frozenset()
    licenses.write(signer.make())  # written by what verified nothing: no features said
    assert licenses.read().features is None
    licenses.write(signer.make(), frozenset({"reports"}))
    assert licenses.delete() is True
    with server.db.connection() as conn:
        assert conn.execute("SELECT document, features FROM ee_license WHERE id = 1").fetchone() == (None, None)


def test_another_document_under_a_version_number_seen_before_is_read_as_itself(server, signer, clock):
    licensing = Licensing(DatabaseLicenses(server.db, seconds=0), clock=clock, keys=signer.keys)
    licensing.install(signer.make())  # version 1
    assert licensing.standing().license.licensee == "Acme Corp"
    with server.db.transaction() as conn:
        conn.execute("DELETE FROM ee_license")  # the row made again (a restore, a schema made again): version 1 again
    licensing.install(signer.make(licensee="Other Corp"))
    assert licensing.standing().license.licensee == "Other Corp"


def test_a_license_larger_than_a_license_may_be_is_not_kept(server):
    licenses = DatabaseLicenses(server.db)
    with pytest.raises(ValueError, match="64 KB"):
        licenses.write(b"{" + b" " * (64 * 1024))
    assert licenses.read() is None


def test_a_trial_is_kept_as_ended_only_when_postgresqls_clock_agrees_and_removing_keeps_it(server, signer):
    licenses = DatabaseLicenses(server.db, clock=lambda: 1.0)
    assert licenses.keep_trial_ended("L-2027-0001", date(2020, 2, 1)) is False  # no license row yet: nothing kept
    licenses.write(signer.make(**ENDED_TRIAL))
    assert licenses.keep_trial_ended("L-2027-0001", date(2020, 2, 1)) is True  # long past, by any clock
    assert licenses.keep_trial_ended("L-2027-0001", date(2020, 2, 1)) is True  # kept once
    assert licenses.keep_trial_ended("L-2099-0001", date(2099, 12, 2)) is False  # PostgreSQL says it still runs
    assert DatabaseLicenses(server.db).ended_trials() == frozenset({"L-2027-0001"})  # every instance reads it
    assert licenses.delete() is True and licenses.ended_trials() == frozenset({"L-2027-0001"})
    with server.db.connection() as conn:
        [kept] = conn.execute("SELECT trials_ended FROM ee_license WHERE id = 1").fetchone()
    assert kept == ["L-2027-0001"]


def _alone(server) -> None:
    """E4's events of the server stopped (Task 11: every server's edition starts them, whose jobs read the license and
    the settings in a thread of their own): a test that counts or times the connections asked of ``server.db`` sees
    its stores' alone."""
    events = server.edition.events
    if events is not None:
        events.close()


def _reads(db, monkeypatch):
    """How many times ``db`` was asked for a connection of its own (what a copy read again asks for)."""
    asked = []
    real = db.connection

    def connection(*args, **kwargs):
        asked.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "connection", connection)
    return asked


def test_a_trial_postgresql_does_not_keep_as_ended_drops_no_copy(server, signer, monkeypatch):
    _alone(server)
    licenses = DatabaseLicenses(server.db, ticker=Clock(0.0))
    licenses.write(signer.make(**ENDED_TRIAL))
    asked = _reads(server.db, monkeypatch)
    assert licenses.ended_trials() == frozenset() and len(asked) == 1
    assert licenses.keep_trial_ended("L-2099-0001", date(2099, 12, 2)) is False  # nothing changed: the copy stays
    assert licenses.ended_trials() == frozenset() and len(asked) == 1
    assert licenses.keep_trial_ended("L-2027-0001", date(2020, 2, 1)) is True  # changed: read again
    assert licenses.ended_trials() == frozenset({"L-2027-0001"}) and len(asked) == 2
    assert licenses.keep_trial_ended("L-2027-0001", date(2020, 2, 1)) is True  # kept already, and in the copy
    assert licenses.ended_trials() == frozenset({"L-2027-0001"}) and len(asked) == 2


def test_an_instance_keeps_its_copy_for_a_few_seconds_or_until_it_is_dropped(server, signer):
    ticks = Clock(0.0)
    mine = DatabaseLicenses(server.db, ticker=ticks)
    assert mine.read() is None
    DatabaseLicenses(server.db).write(signer.make())  # another instance installs it
    assert mine.read() is None  # its copy, younger than REFRESH_SECONDS
    ticks.now += REFRESH_SECONDS
    assert mine.read() is not None
    DatabaseLicenses(server.db).delete()
    mine.drop()  # a message heard
    assert mine.read() is None


def _changed_after_the_read(db, monkeypatch, change):
    """``db``'s next connection, once its read is done and before the store keeps what it read, sees ``change`` made
    (this instance's own change, which drops its copy)."""
    real = db.connection
    done = []

    @contextlib.contextmanager
    def connection(*args, **kwargs):
        with real(*args, **kwargs) as conn:
            yield conn
        if not done:
            done.append(1)
            change()

    monkeypatch.setattr(db, "connection", connection)


def test_a_license_read_while_it_changed_is_not_kept_as_the_copy(server, signer, monkeypatch):
    _alone(server)
    mine = DatabaseLicenses(server.db, ticker=Clock(0.0))
    _changed_after_the_read(server.db, monkeypatch, lambda: mine.write(signer.make()))
    assert mine.read() is None  # what it read, before the change
    assert mine.read() is not None  # that read was not kept: the change dropped the copy while it was made


def test_settings_read_while_they_changed_are_not_kept_as_the_copy(demo_section, server, monkeypatch):
    _alone(server)
    mine = DatabaseSettings(server.db, server.bus, server.audit, ticker=Clock(0.0))
    other = DatabaseSettings(server.db, server.bus, server.audit)

    def change():
        other.set_section("demo", {"level": 3}, actor="admin:t1")
        mine.drop()  # the message heard

    _changed_after_the_read(server.db, monkeypatch, change)
    assert mine.load().demo.level == 1
    assert mine.load().demo.level == 3


def test_two_threads_weighing_the_license_at_once_never_wait_for_each_other_forever(server, signer, clock):
    licensing = Licensing(
        DatabaseLicenses(server.db, seconds=0), clock=clock, count_users=lambda: 301, keys=signer.keys
    )
    licensing.install(signer.make())  # 300 users bought, 301 counted: each weighing keeps the grace's start

    def weigh() -> list[State]:
        return [licensing.standing().state for _ in range(25)]

    with ThreadPoolExecutor(2) as pool:
        weighed = [pool.submit(weigh) for _ in range(2)]
        states = [state for done in weighed for state in done.result(timeout=60)]
    assert states == [State.GRACE] * 50 and licensing.licensed("locked-rules")


def test_a_section_is_kept_recorded_in_the_administration_log_and_published(demo_section, server, monkeypatch):
    published = []
    monkeypatch.setattr(server.bus, "publish", lambda channel, message: published.append((channel, dict(message))))
    store = DatabaseSettings(server.db, server.bus, server.audit, clock=lambda: 1.0)
    assert store.load().demo.level == 1 and store.where() == "server"
    assert store.set_section("demo", {"level": 3}, actor="admin:t1", address="192.0.2.10").demo.level == 3
    assert DatabaseSettings(server.db, server.bus, server.audit).load().demo.level == 3  # every instance reads it
    assert published == [("ee:settings", {"section": "demo"})]
    [entry] = server.audit.query(action="demo.changed")
    assert (entry.actor, entry.target, entry.details) == ("admin:t1", "demo", {"section": "demo"})
    assert entry.address == "192.0.2.10"
    assert store.set_section("demo", None, actor="admin:t1").demo.level == 1
    assert DatabaseSettings(server.db, server.bus, server.audit).load().demo.level == 1


def test_a_section_that_does_not_exist_or_a_wrong_value_is_refused_and_changes_nothing(demo_section, server):
    store = DatabaseSettings(server.db, server.bus, server.audit)
    with pytest.raises(UnknownSection):
        store.set_section("teleport", {}, actor="admin:t1")
    with pytest.raises(EnterpriseSettingsError, match="level"):
        store.set_section("demo", {"level": "high"}, actor="admin:t1")
    assert store.load().demo.level == 1 and server.audit.query(action="demo.changed") == []


def test_a_change_whose_entry_cannot_be_written_is_not_kept_nor_told(demo_section, server, monkeypatch):
    published = []
    monkeypatch.setattr(server.bus, "publish", lambda channel, message: published.append(channel))

    def unwritable(*args, **kwargs):
        raise ConnectionError("the administration log cannot be written")

    monkeypatch.setattr(server.audit, "record", unwritable)
    store = DatabaseSettings(server.db, server.bus, server.audit)
    with pytest.raises(ConnectionError):
        store.set_section("demo", {"level": 3}, actor="admin:t1")
    assert DatabaseSettings(server.db, server.bus, server.audit).load().demo.level == 1 and published == []


def test_a_section_larger_than_an_ee_yaml_may_be_is_refused_and_changes_nothing(demo_section, server):
    store = DatabaseSettings(server.db, server.bus, server.audit)
    with pytest.raises(EnterpriseSettingsError, match="1024 KB"):
        store.set_section("demo", {"note": "x" * (1024 * 1024)}, actor="admin:t1")
    assert store.load().demo.note is None and server.audit.query(action="demo.changed") == []


def test_a_change_made_in_the_callers_transaction_is_kept_or_lost_with_it_and_told_after(
    demo_section, server, signer, monkeypatch
):
    published = []
    monkeypatch.setattr(server.bus, "publish", lambda channel, message: published.append(channel))
    store = DatabaseSettings(server.db, server.bus, server.audit)
    licenses = DatabaseLicenses(server.db)

    def unrecorded() -> None:
        with server.db.transaction() as conn:
            store.set_section("demo", {"level": 3}, actor="admin:t1", conn=conn)
            licenses.write(signer.make(), frozenset({"reports"}), conn=conn)
            raise RuntimeError("the call's entry could not be written")

    with pytest.raises(RuntimeError):
        unrecorded()
    assert DatabaseSettings(server.db, server.bus, server.audit).load().demo.level == 1 and licenses.read() is None
    assert published == [] and server.audit.query(action="demo.changed") == []
    with server.db.transaction() as conn:
        store.set_section("demo", {"level": 3}, actor="admin:t1", conn=conn)
        licenses.write(signer.make(), conn=conn)
        assert published == []  # nothing told before the commit
        server.audit.record("admin:t1", "demo.changed", target="demo", details={"section": "demo"}, conn=conn)
    store.changed("demo")
    licenses.drop()
    assert store.load().demo.level == 3 and licenses.read() is not None and published == ["ee:settings"]
    assert len(server.audit.query(action="demo.changed")) == 1  # the caller's entry alone

    def removed_unrecorded() -> None:
        with server.db.transaction() as conn:
            assert licenses.delete(conn=conn) is True
            raise RuntimeError("the call's entry could not be written")

    with pytest.raises(RuntimeError):
        removed_unrecorded()
    licenses.drop()
    assert licenses.read() is not None


def test_a_change_in_the_callers_transaction_reads_the_settings_there_with_no_second_connection(
    demo_section, server, monkeypatch
):
    _alone(server)
    store = DatabaseSettings(server.db, server.bus, server.audit)
    with server.db.transaction() as conn:
        asked = _reads(server.db, monkeypatch)
        store.set_section("demo", {"level": 3}, actor="admin:t1", conn=conn)
        assert asked == []
    assert DatabaseSettings(server.db, server.bus, server.audit).load().demo.level == 3


def test_a_section_a_newer_latet_left_is_skipped(demo_section, server):
    with server.db.transaction() as conn:
        conn.execute(
            "INSERT INTO ee_settings (section, value, changed, changed_by) "
            "VALUES ('from_a_newer_latet', '{}', 1, 'admin:t1')"
        )
    assert DatabaseSettings(server.db, server.bus, server.audit).load().demo.level == 1


def test_a_secret_is_sealed_with_the_master_key(server):
    secrets = DatabaseSecrets(server.db, server.master, clock=lambda: 1.0)
    assert secrets.save("sso", "client-secret-123") == "server" and secrets.load("sso") == "client-secret-123"
    with server.db.connection() as conn:
        [sealed] = conn.execute("SELECT sealed FROM ee_secrets WHERE name = 'sso'").fetchone()
    assert b"client-secret-123" not in bytes(sealed)
    assert bytes(sealed)[0] == FORMAT and version_of(bytes(sealed)) == server.master.version  # the master key's frame
    assert secrets.remove("sso") is True and secrets.load("sso") is None and secrets.remove("sso") is False
    with pytest.raises(ValueError, match="lowercase letters"):
        secrets.save("../root", "value")


def test_a_sealed_secret_moved_to_another_name_does_not_open(server):
    secrets = DatabaseSecrets(server.db, server.master)
    secrets.save("sso", "client-secret-123")
    with server.db.transaction() as conn:
        conn.execute("INSERT INTO ee_secrets (name, sealed, changed) SELECT 'siem', sealed, 1 FROM ee_secrets")
    with pytest.raises(InvalidTag):
        secrets.load("siem")


def test_a_secret_holds_64_kb_at_most_as_on_a_computer(server):
    secrets = DatabaseSecrets(server.db, server.master)
    with pytest.raises(ValueError, match="64 KB at most"):
        secrets.save("siem", "x" * (SECRET_BYTES + 1))
    assert secrets.load("siem") is None
    assert secrets.save("siem", "x" * SECRET_BYTES) == "server" and secrets.load("siem") == "x" * SECRET_BYTES


def test_a_secret_saved_in_the_callers_transaction_is_kept_or_lost_with_it(server):
    secrets = DatabaseSecrets(server.db, server.master)
    with pytest.raises(RuntimeError, match="the entry beside it"):
        _then_a_failure(server.db, lambda conn: secrets.save("sso-client-secret", "client-secret-123", conn=conn))
    assert secrets.load("sso-client-secret") is None  # not kept
    with server.db.transaction() as conn:
        secrets.save("sso-client-secret", "client-secret-123", conn=conn)
    assert secrets.load("sso-client-secret") == "client-secret-123"  # kept with the transaction
    with pytest.raises(RuntimeError, match="the entry beside it"):
        _then_a_failure(server.db, lambda conn: secrets.remove("sso-client-secret", conn=conn))
    assert secrets.load("sso-client-secret") == "client-secret-123"  # the removal went with the transaction
    with server.db.transaction() as conn:
        assert secrets.remove("sso-client-secret", conn=conn) is True
    assert secrets.load("sso-client-secret") is None


def _then_a_failure(db, change):
    """``change`` made in a transaction that then fails, as when the entry that records it cannot be written."""
    with db.transaction() as conn:
        assert change(conn)
        raise RuntimeError("the entry beside it could not be written")


def test_a_secret_bound_to_what_it_is_for_opens_only_for_that(server):
    """M4: the client secret is sealed for its issuer's origin: an issuer changed since leaves it unusable."""
    secrets = DatabaseSecrets(server.db, server.master)
    secrets.save("sso-client-secret", "client-secret-123", bound="https://login.acme.com")
    assert secrets.load("sso-client-secret", bound="https://login.acme.com") == "client-secret-123"
    for other in ("https://evil.example", None):
        with pytest.raises(InvalidTag):
            secrets.load("sso-client-secret", bound=other)
    secrets.save("siem", "token-1")
    with pytest.raises(InvalidTag):
        secrets.load("siem", bound="https://login.acme.com")
