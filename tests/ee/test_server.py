"""On a server: one edition for every space on an instance, whose rules for everyone the shared settings ask; the
license in PostgreSQL for every instance, each told at once on Valkey or reading it again within REFRESH_SECONDS;
people and API keys counted once each; going over the users bought brings a notice at once and, after the grace, the
pause, with nobody kept out; a trial that ends changes nothing a space masks, and stays ended for every instance when
a clock is moved back."""

import asyncio
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from latet.config import Config, TermConfig
from latet.edition import Community, server_context
from latet.ee.api import license_routes
from latet.ee.edition import EnterpriseEdition
from latet.ee.license import DAY, LicenseError, State
from latet.ee.server import REFRESH_SECONDS
from latet.server.app import create_server_app

from ..server.support import listening
from .licenses import Clock, at
from .servers import URL, services

pytestmark = [pytest.mark.server, services]
LASTING = {"expires": "2099-12-31"}


class Counting(Community):
    """An edition that counts how often it is closed, and keeps who watches its floor."""

    def __init__(self) -> None:
        self.closed = 0
        self.watchers: list = []

    def watch(self, changed):
        self.watchers.append(changed)
        return lambda: self.watchers.remove(changed)

    def close(self) -> None:
        self.closed += 1


def _until(condition, nudge, seconds=5.0):
    """Wait for ``condition``, calling ``nudge`` (a message published again) while it is not there."""
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "the other instance never heard"
        nudge()
        time.sleep(0.1)


async def test_every_space_on_an_instance_has_the_servers_edition(server):
    emily, _ = server.identities.add_person("Emily Carter", "emily.carter@acme.com")
    billing, _ = server.identities.add_key("Billing CI")
    async with listening(server):
        assert isinstance(server.edition, EnterpriseEdition)
        first, second = await server.spaces.runtime(emily), await server.spaces.runtime(billing)
        assert first.edition is server.edition is second.edition and not first.owns_edition


def test_the_shared_settings_ask_the_servers_edition_for_the_rules_for_everyone(server, licensed):
    assert server.shared.rules == server.edition.shared_rules  # the same bound method
    server.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    server.shared.set(Config(terms=[TermConfig(value="Project Falcon")], history="metadata"), actor="admin:t1")
    shown = server.shared.get()
    assert shown.terms == [] and shown.history == "metadata"  # E1 gives no rules: kept, in force for nobody
    assert set(server.shared.kept()) == {"terms"}


async def test_a_trial_that_ends_while_a_space_is_in_use_changes_nothing_it_masks(instances, licensed, clock):
    clock.now = at("2026-12-02", "23:59:59")  # the last second of the trial's last day
    server = instances.open()
    server.edition.install_license(licensed.make(kind="trial", issued="2026-11-01", expires="2026-12-02"),
                                   actor="admin:t1")  # fmt: skip
    emily, code = server.identities.add_person("Emily Carter")
    secret = server.identities.enrol(code, "laptop")[1]
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        before, told = runtime.config, []
        server.edition.watch(lambda: told.append("floor"))
        clock.now = at("2026-12-03", "00:00:00")  # the trial ends while the space serves a request
        server.edition.refresh()
        assert server.edition.licensing.standing().state is State.TRIAL_ENDED
        assert told == [] and runtime.config == before  # nothing applied again: a request in flight goes on as it began
        assert server.edition.licensing.licensed("locked-rules") and server.edition.shared_rules(Config()) == {}
        client = TestClient(create_server_app(server), base_url=URL)  # no lifespan: the instance is not stopped here
        assert client.get("/_latet/server/v1/whoami", headers={"x-latet-token": secret}).status_code == 200
        assert await server.spaces.runtime(emily) is runtime  # the space stays, with its own rules, masking as before


def test_a_trial_that_ended_stays_ended_for_every_instance_when_a_clock_is_moved_back(instances, licensed, clock):
    a = instances.open()
    clock.now = at("2020-01-10")
    a.edition.install_license(licensed.make(kind="trial", issued="2020-01-01", expires="2020-02-01"), actor="admin:t1")
    clock.now = at("2020-02-05")  # past its last day, as PostgreSQL's own clock says too
    a.edition.refresh()
    assert a.edition.licensing.standing().state is State.TRIAL_ENDED
    clock.now = at("2020-01-20")  # the servers' clock set back
    b = instances.open()  # an instance started after
    for instance in (a, b):
        instance.edition.refresh()
        assert instance.edition.licensing.standing().state is State.TRIAL_ENDED
    with pytest.raises(LicenseError, match="trial ended on 2020-02-01"):
        b.edition.install_license(licensed.make(kind="trial", issued="2020-01-01", expires="2020-02-01"),
                                  actor="admin:t1")  # fmt: skip


def test_an_install_reads_the_trials_kept_as_ended_in_its_own_transaction(instances, licensed, clock):
    """M2: an instance whose copy is older than the trial's end, kept by another instance, still refuses that trial's
    file: what the install checks is read in its transaction, never from the copy."""
    trial = licensed.make(kind="trial", issued="2020-01-01", expires="2020-02-01")
    a, b = instances.open(), instances.open()
    clock.now = at("2020-01-10")
    a.edition.install_license(trial, actor="admin:t1")
    b.edition.close()
    b.edition = EnterpriseEdition(server_context(b), listen=False, ticker=Clock(0.0))  # its copies never grow old
    assert b.edition.licensing.standing().state is State.VALID  # b's copy: no trial kept as ended
    clock.now = at("2020-02-05")
    a.edition.refresh()
    assert a.edition.licensing.standing().state is State.TRIAL_ENDED  # kept as ended, PostgreSQL's clock agreeing
    a.edition.remove_license(actor="admin:t1")
    clock.now = at("2020-01-20")  # the clock set back
    with pytest.raises(LicenseError, match="trial ended on 2020-02-01"):
        b.edition.install_license(trial, actor="admin:t1")


def test_an_instance_whose_clock_runs_ahead_ends_no_trial_for_the_others(instances, licensed, clock):
    clock.now = at("2099-11-15")
    a = instances.open()
    a.edition.install_license(licensed.make(kind="trial", issued="2099-11-01", expires="2099-12-02"), actor="admin:t1")
    b = instances.open()
    b.edition.close()
    b.edition = EnterpriseEdition(replace(server_context(b), clock=Clock(at("2099-12-10"))))  # its clock runs ahead
    assert b.edition.licensing.standing().state is State.TRIAL_ENDED  # for itself alone: PostgreSQL disagrees
    a.edition.refresh()
    standing = a.edition.licensing.standing()
    assert standing.state is State.VALID and standing.days_left == 17


async def test_the_spaces_watch_the_floor_once_and_no_space_closes_the_edition(server):
    assert [watcher() for watcher in server.edition._watchers] == [server.spaces.floor_changed]  # E2: M3
    server.edition.close()  # the server's own, replaced in this test
    counting = server.edition = Counting()
    emily, _ = server.identities.add_person("Emily Carter")
    james, _ = server.identities.add_person("James Miller")
    async with listening(server):
        await server.spaces.runtime(emily)
        await server.spaces.runtime(james)
        assert counting.watchers == []  # a new floor reaches every space through the spaces, in each one's worker
        await server.spaces.drop(emily.id)
        assert counting.closed == 0


def test_the_license_is_the_servers_for_every_instance(instances, licensed):
    a, b = instances.open(), instances.open()
    a.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    b.edition.refresh()
    standing = b.edition.licensing.standing()
    assert standing.license.id == "L-2027-0001" and standing.source == "server"


def test_installing_and_removing_are_recorded_and_told_to_every_instance(server, licensed, monkeypatch):
    published = []
    monkeypatch.setattr(server.bus, "publish", lambda channel, message: published.append(channel))
    server.edition.install_license(licensed.make(**LASTING), actor="admin:t1", address="192.0.2.10")
    assert server.edition.remove_license(actor="admin:t1") is True
    assert published == ["ee:license", "ee:license"]
    [installed] = server.audit.query(action="license.installed")
    assert (installed.actor, installed.target, installed.address) == ("admin:t1", "L-2027-0001", "192.0.2.10")
    assert installed.details == {"users": 300, "expires": "2099-12-31", "trial": False}
    [removed] = server.audit.query(action="license.removed")
    assert (removed.actor, removed.target) == ("admin:t1", "L-2027-0001")


def test_a_removal_records_the_license_the_server_holds_not_an_old_copy(instances, licensed):
    a, b = instances.open(), instances.open()
    a.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    b.edition.close()
    b.edition = EnterpriseEdition(server_context(b), listen=False, ticker=Clock(0.0))  # its copies never grow old
    assert b.edition.licensing.standing().license.id == "L-2027-0001"  # b's copy
    a.edition.install_license(licensed.make(id="L-2027-0002", users=50, **LASTING), actor="admin:t1")  # unheard
    assert b.edition.remove_license(actor="admin:t2") is True
    [removed] = b.audit.query(action="license.removed")
    assert (removed.actor, removed.target) == ("admin:t2", "L-2027-0002")
    assert removed.details == {"users": 50, "expires": "2099-12-31", "trial": False}


def test_a_removal_weighs_nothing_and_starts_no_grace(server, licensed, monkeypatch):
    """The removal itself counts no user and keeps no moment from which more users than bought are counted. The
    instance's listener, hearing of the license or of the people added, may weigh it meanwhile on its own thread and
    keep one, as any look does: that look is not the removal's, so only what the removal's own thread does is read.
    The count is read as well as the moment, since a look keeps no new moment where one is kept already."""
    server.edition.install_license(licensed.make(users=1, **LASTING), actor="admin:t1")
    server.identities.add_person("Emily Carter")
    server.identities.add_key("Billing CI")  # over the users bought
    licensing, here, counted, kept = server.edition.licensing, threading.get_ident(), [], []
    count, keep = licensing.count_users, licensing.store.keep_over_since

    def counting():
        if threading.get_ident() == here:
            counted.append(True)
        return count()

    def keeping(moment):
        if threading.get_ident() == here:
            kept.append(moment)
        return keep(moment)

    monkeypatch.setattr(licensing, "count_users", counting)
    monkeypatch.setattr(licensing.store, "keep_over_since", keeping)
    assert server.edition.remove_license(actor="admin:t1") is True
    assert counted == []
    assert [moment for moment in kept if moment is not None] == []


def test_a_database_that_cannot_be_read_is_said_as_the_servers(server, licensed, monkeypatch):
    import psycopg

    server.edition.install_license(licensed.make(**LASTING), actor="admin:t1")

    def refused(*args, **kwargs):
        raise psycopg.OperationalError('connection to server at "10.0.0.5", port 5432 failed')

    monkeypatch.setattr(server.edition.licensing.store, "read", refused)
    line = next(check for check in server.edition.checks() if check.title == "License")
    assert line.status == "fail" and "OperationalError" in line.detail and "10.0.0.5" not in line.detail
    assert "the server's database" in line.detail and "PostgreSQL answers" in line.detail, line.detail
    assert "license file cannot be used" not in line.detail  # no file is at fault: the database did not answer
    # Neither a computer's command nor its restart: an instance's restart reads the same database.
    assert "latet license install" not in line.detail and "Latet restarts" not in line.detail
    told = server.edition.me(None)["license"]
    assert told["state"] == "invalid" and told["notices"] == ["invalid"]


@pytest.mark.parametrize("installed", [False, True])
def test_a_database_that_does_not_answer_lists_the_license_page_only_where_a_license_was_read(
    server, licensed, monkeypatch, installed
):
    """M3 of the final review: a blip of the database makes no License page appear on a server without a license, and
    takes none away from a server whose license this instance read."""
    import psycopg

    if installed:
        server.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    assert server.edition.me(None)["pages"] == (["license"] if installed else [])  # read: the copy holds it, or not

    def refused(*args, **kwargs):
        raise psycopg.OperationalError("the database is restarting")

    monkeypatch.setattr(server.edition.licensing.store, "read", refused)
    me = server.edition.me(None)
    assert me["license"]["state"] == "invalid" and me["pages"] == (["license"] if installed else [])


def test_a_license_change_in_a_callers_transaction_writes_no_entry_and_is_told_once_it_commits(
    server, licensed, monkeypatch
):
    """The caller (the administration) writes the change's one entry in its transaction: the edition writes none."""
    published = []
    monkeypatch.setattr(server.bus, "publish", lambda channel, message: published.append(channel))

    def failing() -> None:
        with server.db.transaction() as conn:
            server.edition.install_license(licensed.make(**LASTING), actor="admin:t1", conn=conn)
            raise RuntimeError("the change failed after it was written")

    with pytest.raises(RuntimeError, match="after it was written"):
        failing()
    server.edition.refresh()
    assert server.edition.licensing.standing().state is State.NONE and published == []  # rolled back, told nothing
    with server.db.transaction() as conn:
        server.edition.install_license(licensed.make(**LASTING), actor="admin:t1", conn=conn)
        assert published == []  # nobody is told of what is not committed
    assert server.audit.query(action="license.installed") == []  # the caller's entry alone
    server.edition.committed("license.installed")  # what its caller does once it committed
    assert published == ["ee:license"]
    assert server.edition.licensing.standing().license.id == "L-2027-0001"
    with server.db.transaction() as conn:
        assert server.edition.held_license(conn).id == "L-2027-0001"
        assert server.edition.remove_license(actor="admin:t1", conn=conn) is True
    assert server.audit.query(action="license.removed") == [] and published == ["ee:license"]


def _all_end(*works, seconds=10.0):
    """Each of ``works`` in a thread of its own: all must end within ``seconds``, none waiting for another for good
    (PostgreSQL cannot see a Python lock, so only its statement timeout would end such a wait), and none failing."""
    failures = []

    def running(work):
        def run():
            try:
                work()
            except BaseException as exc:
                failures.append(exc)

        return run

    threads = [threading.Thread(target=running(work), daemon=True) for work in works]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + seconds
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    assert not any(thread.is_alive() for thread in threads), "one waits for the other"
    assert failures == []


def test_a_removal_holding_the_license_row_and_an_install_never_wait_for_each_other(server, licensed):
    """I1: the removal holds the license's row (as the administration's does) while an install takes this instance's
    license lock and waits for that row: the removal goes on without that lock."""
    edition, held = server.edition, threading.Event()
    edition.install_license(licensed.make(**LASTING), actor="admin:t1")

    def removing():
        with server.db.transaction() as conn:
            edition.held_license(conn)  # the row held
            held.set()
            time.sleep(0.5)  # the install meanwhile checks its file and waits for the row
            assert edition.remove_license(conn=conn) is True

    def installing():
        assert held.wait(5)
        with server.db.transaction() as conn:
            edition.install_license(licensed.make(id="L-2027-0002", **LASTING), conn=conn)

    _all_end(removing, installing)
    edition.refresh()
    assert edition.licensing.standing().license.id == "L-2027-0002"  # the install, once the removal committed


def test_a_removal_holding_the_license_row_and_a_look_that_keeps_the_users_grace_never_wait_for_each_other(
    server, licensed
):
    """I1: a look that would keep since when more users than bought are counted skips the row a removal holds."""
    edition, held = server.edition, threading.Event()
    edition.install_license(licensed.make(users=1, **LASTING), actor="admin:t1")
    server.identities.add_person("Emily Carter")
    server.identities.add_key("Billing CI")  # over the users bought
    with server.db.transaction() as conn:  # as nobody had looked yet: the server's own look (its license job, its
        conn.execute("UPDATE ee_license SET over_since = NULL")  # listener hearing the install) may have kept it

    def removing():
        with server.db.transaction() as conn:
            edition.held_license(conn)
            held.set()
            time.sleep(0.5)  # the look meanwhile holds this instance's license lock
            assert edition.remove_license(conn=conn) is True

    def looking():
        assert held.wait(5)
        edition.refresh()
        assert edition.licensing.standing().state is State.GRACE  # over the users: said, kept at a later look

    _all_end(removing, looking)


def test_a_removal_holding_the_license_row_never_waits_for_this_instances_license_lock(server, licensed):
    """I1: whatever holds the license lock a while (a look counting the users), a removal that holds the license's row
    goes on: it takes no lock of the Licensing's."""
    edition = server.edition
    edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    locked, done = threading.Event(), threading.Event()

    def looking_slowly():
        with edition.licensing._lock:
            locked.set()
            done.wait(8)

    def removing():
        assert locked.wait(5)
        with server.db.transaction() as conn:
            edition.held_license(conn)
            assert edition.remove_license(conn=conn) is True
        done.set()

    started = time.monotonic()
    _all_end(looking_slowly, removing)
    assert time.monotonic() - started < 5, "the removal waited for the lock"


def test_what_protects_answers_while_an_install_waits_for_the_license_row(server, licensed):
    """I1: an install waiting for the row a removal holds does not hold the license lock meanwhile: ``licensed``
    answers at once."""
    edition, held, answered = server.edition, threading.Event(), threading.Event()
    edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    edition.licensing.licensed("locked-rules")

    def holding():
        with server.db.transaction() as conn:
            edition.held_license(conn)
            held.set()
            answered.wait(8)  # held until what protects answered, or for good

    def installing():
        assert held.wait(5)
        with server.db.transaction() as conn:
            edition.install_license(licensed.make(id="L-2027-0002", **LASTING), conn=conn)

    def asking():
        assert held.wait(5)
        time.sleep(0.5)  # the install waits for the row by now
        started = time.monotonic()
        assert edition.licensing.licensed("locked-rules")
        assert time.monotonic() - started < 2, "what protects waited behind the install"
        answered.set()

    _all_end(holding, installing, asking)


@pytest.mark.parametrize("kept", ["users", "trial"])
def test_a_look_never_waits_for_the_license_row_a_change_holds(server, licensed, clock, kept):
    """I1: what a look keeps (since when more users than bought are counted, a trial's end) is left to the next look
    while a change holds the license's row, so that what protects never waits behind this instance's license lock."""
    edition, held, looked = server.edition, threading.Event(), threading.Event()
    if kept == "users":
        edition.install_license(licensed.make(users=1, **LASTING), actor="admin:t1")
        server.identities.add_person("Emily Carter")
        server.identities.add_key("Billing CI")
    else:
        clock.now = at("2026-11-10")
        edition.install_license(licensed.make(kind="trial", issued="2026-11-01", expires="2026-12-02"),
                                actor="admin:t1")  # fmt: skip
        clock.now = at("2026-12-03")  # ended: the look would keep it

    def holding():
        with server.db.transaction() as conn:
            edition.held_license(conn)
            held.set()
            looked.wait(8)  # held until the look ends, or for good

    def looking():
        assert held.wait(5)
        edition.refresh()
        edition.licensing.standing()
        assert edition.licensing.licensed("locked-rules")
        looked.set()

    started = time.monotonic()
    _all_end(holding, looking)
    assert time.monotonic() - started < 5, "the look waited for the row"


@pytest.mark.parametrize("change", ["install", "remove"])
def test_a_license_change_whose_entry_cannot_be_written_is_not_made_nor_told(server, licensed, monkeypatch, change):
    """Made by the edition alone, the change and its entry are one transaction too: an entry that cannot be written
    raises, and leaves the license as it was, nobody told."""
    server.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    published = []
    monkeypatch.setattr(server.bus, "publish", lambda channel, message: published.append(channel))

    def refused(*args, **kwargs):
        raise OSError("the log cannot be written")

    monkeypatch.setattr(server.audit, "record", refused)
    if change == "install":
        with pytest.raises(OSError, match="cannot be written"):
            server.edition.install_license(licensed.make(id="L-2027-0002", **LASTING), actor="admin:t1")
    else:
        with pytest.raises(OSError, match="cannot be written"):
            server.edition.remove_license(actor="admin:t1")
    server.edition.refresh()
    assert server.edition.licensing.standing().license.id == "L-2027-0001" and published == []


def test_people_and_api_keys_are_counted_once_each(server, licensed):
    server.edition.install_license(licensed.make(users=5, **LASTING), actor="admin:t1")
    emily, code = server.identities.add_person("Emily Carter", "emily.carter@acme.com")
    server.identities.enrol(code, "laptop")
    server.identities.enrol(server.identities.code(emily.id), "desktop")  # a second computer: the same person
    server.identities.add_key("Billing CI")
    james, _ = server.identities.add_person("James Miller")
    server.identities.disable(james.id)
    server.edition.refresh()
    assert server.edition.licensing.standing().users_in_use == 2
    server.identities.enable(james.id)
    server.edition.refresh()
    assert server.edition.licensing.standing().users_in_use == 3


def test_over_the_users_a_notice_at_once_the_pause_after_the_grace_and_nobody_kept_out(instances, licensed, clock):
    server = instances.open()
    server.edition.install_license(licensed.make(users=2, **LASTING), actor="admin:t1")
    secrets = {}
    for name in ("Emily Carter", "James Miller"):
        _, code = server.identities.add_person(name)
        secrets[name] = server.identities.enrol(code, "laptop")[1]
    billing, secrets["Billing CI"] = server.identities.add_key("Billing CI")
    server.edition.refresh()
    license = server.edition.me(None)["license"]
    assert license["state"] == "grace" and "users-over" in license["notices"]
    # A person learns nothing of the headcount, not even through the day the users' grace would pause what produces:
    # the administrators do (`latet server license show`, `latet server status`).
    assert license["users_in_use"] is None and license["pauses_on"] is None
    standing = server.edition.licensing.standing()
    assert standing.users_in_use == 3 and standing.pauses_on().isoformat() == "2027-03-31"
    clock.now += 30 * DAY
    server.edition.refresh()
    assert server.edition.licensing.standing().state is State.PAUSED
    assert server.edition.licensing.licensed("locked-rules") and not server.edition.licensing.producing("reports")
    olivia, code = server.identities.add_person("Olivia Brown")  # paused: a new person still gets in
    secrets["Olivia Brown"] = server.identities.enrol(code, "laptop")[1]
    deploy, secrets["Deploy bot"] = server.identities.add_key("Deploy bot")  # and a new API key
    client = TestClient(create_server_app(server), base_url=URL)  # no lifespan: the instance is not stopped here
    for name, secret in secrets.items():
        assert client.get("/_latet/server/v1/whoami", headers={"x-latet-token": secret}).status_code == 200, name
    restarted = instances.open()  # an instance started now: the same grace, from the same moment
    assert restarted.edition.licensing.standing().state is State.PAUSED
    for identity in (olivia, billing, deploy):  # three places freed
        server.identities.disable(identity.id)
    server.edition.refresh()
    standing = server.edition.licensing.standing()
    assert standing.state is State.VALID and standing.over_since is None


def test_removing_and_installing_the_license_again_starts_no_new_grace(server, licensed, clock):
    server.edition.install_license(licensed.make(users=1, **LASTING), actor="admin:t1")
    server.identities.add_person("Emily Carter")
    server.identities.add_key("Billing CI")
    server.edition.refresh()
    started = server.edition.licensing.standing().over_since
    assert started == clock.now
    clock.now += 10 * DAY
    server.edition.remove_license(actor="admin:t1")
    server.edition.install_license(licensed.make(users=1, **LASTING), actor="admin:t1")
    server.edition.refresh()
    assert server.edition.licensing.standing().over_since == started


def test_an_instance_that_starts_over_the_users_starts_their_grace_before_anyone_looks(instances, licensed, clock):
    a = instances.open()
    a.edition.install_license(licensed.make(users=1, **LASTING), actor="admin:t1")
    a.identities.add_person("Emily Carter")
    a.identities.add_key("Billing CI")
    with a.db.connection() as conn:
        assert conn.execute("SELECT over_since FROM ee_license").fetchone() == (None,)  # nobody looked yet
    instances.open()
    with a.db.connection() as conn:
        assert conn.execute("SELECT over_since FROM ee_license").fetchone() == (clock.now,)


def test_another_instance_hears_the_new_license_at_once(instances, licensed):
    a, b = instances.open(), instances.open()
    b.edition.close()
    b.edition = EnterpriseEdition(server_context(b), ticker=Clock(0.0))  # its copies never grow old: only messages
    assert b.edition.licensing.standing().state is State.NONE
    a.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    _until(
        lambda: b.edition.licensing.standing().state is State.VALID,
        lambda: a.bus.publish("ee:license", {"change": "license.installed"}),  # again, should b not listen yet
    )


def test_an_instance_that_missed_the_message_takes_the_license_within_its_refresh(instances, licensed):
    a, b = instances.open(), instances.open()
    ticks = Clock(0.0)
    b.edition.close()
    b.edition = EnterpriseEdition(server_context(b), listen=False, ticker=ticks)  # it hears nothing
    assert b.edition.licensing.standing().state is State.NONE
    a.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    assert b.edition.licensing.standing().state is State.NONE  # its copy, younger than REFRESH_SECONDS
    ticks.now += REFRESH_SECONDS
    assert b.edition.licensing.standing().state is State.VALID


def test_a_message_on_identities_counts_the_users_again(instances, licensed):
    a, b = instances.open(), instances.open()
    a.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    b.edition.close()
    b.edition = EnterpriseEdition(server_context(b), ticker=Clock(0.0))
    assert b.edition.licensing.standing().users_in_use == 0
    emily, _ = a.identities.add_person("Emily Carter")  # E0b publishes {"id", "event": "added"} on identities
    _until(
        lambda: b.edition.licensing.standing().users_in_use == 1,
        lambda: a.bus.publish("identities", {"id": emily.id, "event": "added"}),  # again, as E0b's message
    )


def test_a_message_only_makes_an_instance_read_postgresql_again(instances, licensed):
    """Whoever can publish on Valkey (an instance, or someone with Valkey's password and the bus's key) makes another
    instance read PostgreSQL again, never take what the message holds: a license, a count of users."""
    a, b = instances.open(), instances.open()
    b.edition.close()
    b.edition = EnterpriseEdition(server_context(b), ticker=Clock(0.0))  # its copies never grow old: only messages
    store = b.edition.licensing.store
    heard = store._dropped
    forged = {"change": "license.installed", "license": licensed.make(**LASTING).decode(), "users": 1000}
    _until(lambda: store._dropped > heard, lambda: a.bus.publish("ee:license", forged))
    assert b.edition.licensing.standing().state is State.NONE  # PostgreSQL holds no license: none
    a.edition.install_license(licensed.make(users=5, **LASTING), actor="admin:t1")
    _until(lambda: b.edition.licensing.standing().state is State.VALID, lambda: None)
    assert b.edition.licensing.standing().users_in_use == 0
    with a.db.transaction() as conn:  # a person PostgreSQL has, of whom no instance was told
        conn.execute(
            "INSERT INTO identities (id, kind, name, created) VALUES ('u_emilycarte', 'person', 'Emily Carter', 0)"
        )
    assert b.edition.licensing.standing().users_in_use == 0  # its count, kept until a message or REFRESH_SECONDS
    claim = {"id": "u_emilycarte", "event": "added", "users_in_use": 1000, "enabled": 1000}
    _until(
        lambda: b.edition.licensing.standing().users_in_use == 1,  # PostgreSQL's count, never the message's
        lambda: a.bus.publish("identities", claim),
    )


def test_weighing_on_several_threads_while_copies_drop_never_waits_forever(server, licensed):
    """The count of users is asked while the license's lock is held: it never calls back into that Licensing."""
    server.edition.install_license(licensed.make(users=1, **LASTING), actor="admin:t1")
    server.identities.add_person("Emily Carter")
    server.identities.add_key("Billing CI")
    states, failures = [], []

    def weigh() -> None:
        try:
            for _ in range(20):
                server.edition.refresh()
                states.append(server.edition.licensing.standing().state)
                assert server.edition.licensing.licensed("locked-rules")
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=weigh, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(thread.is_alive() for thread in threads) and failures == []
    assert len(states) == 60 and set(states) == {State.GRACE}


def test_an_instance_not_hearing_the_others_says_so_and_closing_stops_its_ear(server, licensed):
    server.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    assert [check.title for check in server.edition.checks()] == ["License", "Event log"]
    ear = server.edition._listener
    assert ear is not None and ear.alive()
    ear.stop()  # as if its thread had ended
    line = next(check for check in server.edition.checks() if check.title == "Enterprise messages")
    assert line.status == "warn" and f"every {REFRESH_SECONDS:g} seconds" in line.detail, line.detail
    edition = EnterpriseEdition(server_context(server))
    ear = edition._listener
    edition.close()
    edition.close()  # closing again does nothing more
    assert ear is not None and not ear.alive()


def test_an_instance_whose_edition_cannot_be_made_closes_all_it_opened(instances, monkeypatch):
    from latet.server import app as app_module

    closed, close = [], app_module.Server.close

    def closing(self):
        closed.append(self)
        close(self)

    def broken(server, **options):  # as server_edition is called: the server, then whether it is a one-shot's
        raise RuntimeError("the edition cannot be made")

    monkeypatch.setattr(app_module.Server, "close", closing)
    monkeypatch.setattr(app_module, "server_edition", broken)
    with pytest.raises(RuntimeError, match="cannot be made"):
        app_module.open_server(
            instances.settings, clock=instances.clock, schema=instances.schema, prefix=instances.prefix
        )
    assert len(closed) == 1  # its edition, its metrics, its spaces' threads, Valkey and PostgreSQL


def test_the_servers_edition_writes_no_file(demo_section, instances, licensed, tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config" / "config.yaml"))
    server = instances.open()
    server.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    server.edition.settings_store.set_section("demo", {"level": 2}, actor="admin:t1")
    server.edition.secrets.save("sso", "client-secret")
    assert [check.title for check in server.edition.checks()] == ["License", "Event log"]
    server.edition.remove_license(actor="admin:t1")
    assert not (tmp_path / "data" / "license.json").exists() and not (tmp_path / "data" / "secrets").exists()
    assert not (tmp_path / "config" / "ee.yaml").exists()


def test_on_a_server_a_persons_license_page_shows_the_license_and_changes_nothing(server, licensed):
    server.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    emily, _ = server.identities.add_person("Emily Carter")
    server.edition.refresh()
    dashboard = SimpleNamespace(
        guarded=lambda handler: handler, rt=SimpleNamespace(identity=emily.ref()), _in_pool=asyncio.to_thread
    )
    routes = license_routes(server.edition, dashboard)
    with TestClient(Starlette(routes=routes)) as client:
        shown = client.get("/_latet/api/v1/ee/license").json()
        # How many people and API keys the server has are its administrators' (E3 gives them back to them).
        assert (shown["source"], shown["users_in_use"]) == ("server", None) and "changes" not in shown
    gated = Starlette(routes=server.edition.access.gated(routes, run=asyncio.to_thread))

    async def handed(scope, receive, send):  # as the server's gate hands a request on to the person's space
        await gated({**scope, "latet.identity": emily.id}, receive, send)

    with TestClient(handed) as client:
        for answer in (  # a person with no role changes nothing: the License page is its administrators' (E3)
            client.put("/_latet/api/v1/ee/license", json={"license": "{}"}),
            client.delete("/_latet/api/v1/ee/license"),
        ):
            assert answer.status_code == 403 and "license.manage" in answer.json()["error"]
    assert server.edition.me(emily.ref())["pages"] == []  # the License page is license.manage's once roles decide (E3)
    assert server.edition.standing().license is not None  # still installed
