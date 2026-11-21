"""The Enterprise Edition, a Latet server's alone: it is never made for a computer, shows nothing of itself without a
license (no upgrade banner), says the license in /me and in `latet server status`'s lines when there is one, and tells
a person on the server nothing of its headcount. Its license here is kept in memory (tests/ee/editions.py); what needs
PostgreSQL is in tests/ee/test_server.py. (A computer without a server is the Community Edition:
tests/ee/test_computer.py.)"""

import gc
import logging
import time
import weakref

import pytest

from latet.config import Config, TermConfig
from latet.edition import EditionContext, load_edition
from latet.ee import make_edition
from latet.ee.edition import EnterpriseEdition

from .editions import memory_edition
from .licenses import ENDED_TRIAL, TRIAL, MemoryLicenses, at

LASTING = {"expires": "2099-12-31"}
NOTHING = {
    "name": "enterprise", "license": None, "features": [], "pages": [], "roles": [], "actions": [], "policy": None,
}  # fmt: skip


def _license_lines(edition):
    """The License lines of `latet server status` (an edition in memory hears no Valkey: its own line is beside)."""
    return [line for line in edition.checks() if line.title == "License"]


def test_the_enterprise_edition_is_never_made_for_a_computer(tmp_path):
    computer = EditionContext(data=tmp_path / "data", config_dir=tmp_path, server=None, clock=time.time)
    with pytest.raises(ValueError, match="a Latet server's"):
        EnterpriseEdition(computer)
    with pytest.raises(ValueError, match="a Latet server's"):
        make_edition(computer)  # latet.ee's entry point: a later part calling it for a computer fails too


def test_an_edition_refused_for_a_computer_starts_nothing_and_reads_no_store(tmp_path, monkeypatch):
    """The refusal comes first: no store read, no listener, no policy made for a computer."""
    started = []
    monkeypatch.setattr(EnterpriseEdition, "_on_server", lambda self, *args: started.append("server"))
    monkeypatch.setattr("latet.ee.edition.make_policy", lambda *args, **given: started.append("policy"))
    computer = EditionContext(data=tmp_path / "data", config_dir=tmp_path, server=None, clock=time.time)
    with pytest.raises(ValueError, match="a computer without a server runs the Community Edition"):
        EnterpriseEdition(computer)
    assert started == []


def test_with_ee_there_a_servers_edition_is_the_enterprise_edition(tmp_path, monkeypatch):
    edition, _, _ = memory_edition(monkeypatch, tmp_path)
    made = load_edition(edition.context)
    try:
        assert edition.name == "enterprise"
        assert isinstance(made, EnterpriseEdition)
    finally:
        made.close()
        edition.close()


def test_the_policy_is_made_with_the_server_it_runs_on(tmp_path, monkeypatch):
    edition, _, source = memory_edition(monkeypatch, tmp_path)
    assert source.server is edition.context.server  # never the context: a computer's folders are no policy's
    assert "record" not in source.made and source.made["changed"] == edition.floor_changed  # R9: E4 records it
    edition.close()


def test_without_a_license_it_shows_nothing_of_itself(tmp_path, monkeypatch, licensed):
    edition, _, _ = memory_edition(monkeypatch, tmp_path)
    assert edition.me(None) == NOTHING and _license_lines(edition) == []


def test_with_a_license_me_says_it_and_lists_the_license_page(tmp_path, monkeypatch, licensed):
    edition, store, _ = memory_edition(monkeypatch, tmp_path, clock=lambda: at("2027-03-01"))
    store.write(licensed.make(features=["reports", "locked-rules", "from-a-newer-latet"]))
    me = edition.me(None)
    assert me["pages"][0] == "license" and me["roles"] == [] == me["actions"]  # later parts' pages follow (E2's policy)
    assert me["features"] == ["locked-rules", "reports"]
    assert me["license"] == {
        "state": "valid", "licensee": "Acme Corp", "users": 300, "users_in_use": None, "expires": "2027-10-31",
        "days_left": 244, "trial": False, "notices": [], "pauses_on": None,
    }  # fmt: skip


def test_a_file_that_cannot_be_used_still_lists_the_page_where_it_is_replaced(tmp_path, monkeypatch, licensed):
    edition, store, _ = memory_edition(monkeypatch, tmp_path)
    store.write(b"not a license")
    me = edition.me(None)
    assert me["pages"] == ["license"] and me["features"] == []
    assert me["license"]["state"] == "invalid" and me["license"]["notices"] == ["invalid"]


@pytest.mark.parametrize(
    ("moment", "status", "words"),
    [
        (at("2027-03-01"), "ok", "Acme Corp (L-2027-0001): 300 users, until 2027-10-31"),
        (at("2027-10-21"), "warn", "it ends in 10 days"),
        (at("2027-11-05"), "warn", "it ended on 2027-10-31; reports, events and term sync pause on 2027-12-01"),
        (at("2028-01-10"), "fail", "reports, events and term sync are paused, protection is unchanged"),
    ],
)
def test_the_servers_status_says_the_license_and_its_notices(tmp_path, monkeypatch, licensed, moment, status, words):
    edition, store, _ = memory_edition(monkeypatch, tmp_path, clock=lambda: moment)
    store.write(licensed.make())
    [line] = _license_lines(edition)
    assert line.status == status and words in line.detail, line.detail


@pytest.mark.parametrize(
    ("moment", "status", "words"),
    [
        (at("2026-11-25"), "warn", "it ends in 7 days, with no grace"),
        (at("2026-12-01"), "warn", "it ends in 1 day, with no grace"),
        (at("2026-12-03"), "fail", "the trial ended on 2026-12-02; the Enterprise settings can be read, not changed"),
    ],
)
def test_the_servers_status_warns_before_a_trial_ends_and_says_that_it_ended(
    tmp_path, monkeypatch, licensed, moment, status, words
):
    edition, store, _ = memory_edition(monkeypatch, tmp_path, clock=lambda: moment)
    store.write(licensed.make(**TRIAL))
    [line] = _license_lines(edition)
    assert line.status == status and words in line.detail and "(L-2027-0001, trial)" in line.detail, line.detail


def test_after_a_trial_ended_me_still_lists_the_license_page_and_the_features_protect(tmp_path, monkeypatch, licensed):
    edition, store, _ = memory_edition(monkeypatch, tmp_path)
    store.write(licensed.make(**ENDED_TRIAL))
    me = edition.me(None)
    assert me["pages"][0] == "license" and me["features"][0] == "locked-rules"
    assert (me["license"]["state"], me["license"]["notices"], me["license"]["trial"]) == (
        "trial-ended", ["trial-ended"], True
    )  # fmt: skip


def test_the_servers_status_says_why_a_license_file_cannot_be_used(tmp_path, monkeypatch, licensed):
    edition, store, _ = memory_edition(monkeypatch, tmp_path)
    store.write(licensed.make().replace(b'"users": 300', b'"users": 3000'))
    [line] = _license_lines(edition)
    assert line.status == "fail" and "signature does not match" in line.detail
    # The server's words: every instance keeps what the installer verified, and its administrators install it again.
    assert "every instance keeps what protects" in line.detail and "latet server license install" in line.detail


def test_a_store_that_fails_is_said_by_me_and_the_status_never_raised(tmp_path, monkeypatch, licensed):
    edition, store, _ = memory_edition(monkeypatch, tmp_path)

    def refused(*args, **kwargs):
        raise PermissionError(13, "Permission denied", "/home/emily.carter/latet")

    monkeypatch.setattr(store, "read", refused)
    me = edition.me(None)
    # No copy of a license was ever read here: a database that does not answer lists no page (tests/ee/test_server.py
    # has the copy that keeps it listed).
    assert me["pages"] == [] and me["features"] == []
    assert me["license"]["state"] == "invalid" and me["license"]["notices"] == ["invalid"]
    [line] = _license_lines(edition)
    assert line.status == "fail" and "the server's database could not be read (PermissionError)" in line.detail
    assert "emily" not in line.detail and "PostgreSQL" in line.detail  # the server's own words


def test_a_store_that_fails_at_start_is_logged_by_its_type_and_the_edition_still_starts(tmp_path, caplog, monkeypatch):
    def refused(self):
        raise PermissionError(13, "Permission denied", "/home/emily.carter/latet")

    monkeypatch.setattr(MemoryLicenses, "read", refused)
    with caplog.at_level(logging.WARNING, logger="latet.ee.edition"):
        edition, _, _ = memory_edition(monkeypatch, tmp_path)
    [record] = [record for record in caplog.records if "weighed at start" in record.getMessage()]
    assert "PermissionError" in record.getMessage() and "emily" not in record.getMessage() and not record.exc_info
    assert edition.me(None)["license"]["state"] == "invalid"  # said where it is looked at


def test_a_person_on_a_server_is_told_no_headcount(tmp_path, monkeypatch, licensed):
    edition, store, _ = memory_edition(monkeypatch, tmp_path, clock=lambda: at("2027-03-01"), count_users=lambda: 120)
    store.write(licensed.make(users=100, expires="2099-12-31"))
    told = edition.me(None)["license"]
    # The notice stays; the count, and since when it is over (which the day of the pause would give back), are the
    # administrators'.
    assert told["users_in_use"] is None and told["pauses_on"] is None and "users-over" in told["notices"]
    standing = edition.standing()
    assert standing.users_in_use == 120 and standing.over_since is not None  # the administrators' (E3)
    assert edition.told(standing).users_in_use is None and edition.told(standing).over_since is None


def test_a_person_on_a_server_is_told_the_day_the_licenses_end_pauses_what_produces(tmp_path, monkeypatch, licensed):
    edition, store, _ = memory_edition(monkeypatch, tmp_path, clock=lambda: at("2027-11-05"), count_users=lambda: 120)
    store.write(licensed.make(users=100))  # its last day 2027-10-31
    told = edition.me(None)["license"]
    assert (told["state"], told["pauses_on"], told["users_in_use"]) == ("grace", "2027-12-01", None)


def test_the_edition_reaches_for_no_network(tmp_path, monkeypatch, licensed, offline):
    edition, store, _ = memory_edition(monkeypatch, tmp_path, count_users=lambda: 3)
    store.write(licensed.make(**LASTING))
    assert edition.me(None)["license"]["state"] == "valid" and edition.standing().license is not None
    assert [line.status for line in _license_lines(edition)] == ["ok"]


def test_the_hooks_of_the_later_parts_do_nothing_yet(tmp_path, monkeypatch, licensed):
    edition, store, _ = memory_edition(monkeypatch, tmp_path)
    store.write(licensed.make(**LASTING))
    assert edition.floor(None) is None and edition.changes_go_to(None) == "self" == edition.live_review_goes_to(None)
    assert edition.keeps_events() is False and edition.record({"type": "license"}) is None
    assert edition.synced_terms(None) is None
    # No rules for everyone until E6, a license installed or not: the rules a row kept stay kept, in force for nobody.
    assert edition.shared_rules(Config(terms=[TermConfig(value="Project Falcon")], profile="max")) == {}


def test_a_floor_that_changes_is_told_to_every_runtime_watching_until_it_stops(tmp_path, monkeypatch):
    edition, _, _ = memory_edition(monkeypatch, tmp_path)
    heard = []

    def broken():
        raise RuntimeError("this runtime cannot apply it")

    stop_emily = edition.watch(lambda: heard.append("emily"))
    edition.watch(broken)  # one that fails: the others are still told
    edition.watch(lambda: heard.append("james"))
    edition.floor_changed()
    stop_emily()
    stop_emily()  # stopping twice does nothing more
    edition.floor_changed()
    assert heard == ["emily", "james", "james"]


def test_a_runtime_never_closed_is_not_kept_alive_by_the_floor_it_watches(tmp_path, monkeypatch):
    edition, _, _ = memory_edition(monkeypatch, tmp_path)
    heard = []

    class Space:
        def __init__(self, name):
            self.name = name

        def floor_changed(self):
            heard.append(self.name)

    emily, james = Space("emily"), Space("james")
    edition.watch(emily.floor_changed)
    stop_james = edition.watch(james.floor_changed)
    gone = weakref.ref(james)
    del james  # a space whose build failed after its runtime was made: never closed
    gc.collect()
    assert gone() is None
    # A floor that never changes (until E2) still drops what is gone: the next runtime that watches prunes it.
    olivia = Space("olivia")
    edition.watch(olivia.floor_changed)
    assert len(edition._watchers) == 2
    edition.floor_changed()
    stop_james()  # its stop, called late, does nothing
    assert heard == ["emily", "olivia"]


def test_closing_the_edition_twice_does_nothing_more(tmp_path, monkeypatch):
    edition, _, _ = memory_edition(monkeypatch, tmp_path)
    edition.close()
    edition.close()


def test_the_enterprise_tables_are_migrated_after_the_servers():
    from latet.server.db import MIGRATIONS

    assert MIGRATIONS.index("latet.ee.migrations") > MIGRATIONS.index("latet.server.migrations")
