"""The license file: checked here with the public keys Latet carries, then weighed into a standing. Its days and the
count of users bring notices, a grace and a pause; a trial's days bring two warnings and its end, with no grace, which
stops what manages and decides, never what protects (licensed never depends on days or counts). A file that cannot be
used stops what manages, produces and decides too, and what protects keeps the features its installer verified, else
the last license verified. The license is a server's: the users' grace and the trials that ended are kept with it, for
every instance (here in a store in memory, which keeps what the server's database keeps). Nothing reaches the
network."""

import codecs
import json
import logging
from datetime import date

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from latet.ee import license as license_module
from latet.ee.license import (
    DAY,
    KEY_ID,
    PUBLIC_KEYS,
    REFRESH_SECONDS,
    LicenseError,
    Licensing,
    NotLicensed,
    State,
    Stored,
    TrialEnded,
    canonical,
    describe,
    details,
    parse_license,
    require,
    require_serving,
    standing_of,
)

from .licenses import ENDED_TRIAL, TRIAL, Clock, MemoryLicenses, Signer, at

CAFE = "Acme Caf\N{LATIN SMALL LETTER E WITH ACUTE} Ltd"


def _licensing(store, signer, clock=None, count=None):
    return Licensing(
        store, clock=clock or Clock(at("2027-03-01")), count_users=count or (lambda: None), keys=signer.keys
    )


# --- reading a file ---------------------------------------------------------------------------------------------


def test_a_license_made_as_the_licensor_makes_it_is_read(signer):
    lic = parse_license(signer.make(), signer.keys)
    assert (lic.id, lic.licensee, lic.users, lic.issued, lic.expires, lic.trial) == (
        "L-2027-0001", "Acme Corp", 300, date(2026, 11, 1), date(2027, 10, 31), False
    )  # fmt: skip
    assert lic.features == frozenset({"locked-rules", "approvals", "reports", "siem", "term-sync", "sso", "roles"})
    trial = parse_license(signer.make(**TRIAL), signer.keys)
    assert (trial.trial, trial.issued, trial.expires) == (True, date(2026, 11, 1), date(2026, 12, 2))


def test_the_signature_is_made_on_the_canonical_payload():
    payload = {"v": 1, "licensee": CAFE, "users": 300}
    assert canonical(payload) == b'{"licensee":"Acme Caf' + bytes([0xC3, 0xA9]) + b' Ltd","users":300,"v":1}'


def test_a_license_saved_again_with_a_bom_crlf_and_other_spacing_is_still_valid(signer):
    document = signer.document(licensee=CAFE)
    notepad = "\N{ZERO WIDTH NO-BREAK SPACE}" + json.dumps(document, indent=4, ensure_ascii=True).replace("\n", "\r\n")
    reordered = json.dumps(dict(reversed(list(document.items()))))
    for data in (notepad.encode("utf-8"), reordered.encode("utf-8")):
        assert parse_license(data, signer.keys).licensee == CAFE


@pytest.mark.parametrize(
    ("bom", "encoding"),
    [
        (codecs.BOM_UTF16_LE, "utf-16-le"),  # as Windows PowerShell 5.1's Out-File and > write it
        (codecs.BOM_UTF16_BE, "utf-16-be"),
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF32_BE, "utf-32-be"),
    ],
    ids=["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"],
)
def test_a_license_saved_in_utf_16_or_utf_32_with_its_bom_is_still_valid(signer, bom, encoding):
    data = bom + signer.make(licensee=CAFE).decode("utf-8").encode(encoding)
    assert parse_license(data, signer.keys).licensee == CAFE


@pytest.mark.parametrize(
    "change",
    [lambda text: text.replace('"users": 300', '"users": 3000'), lambda text: text.replace("Acme Corp", "Acme Corq")],
)
def test_one_character_changed_breaks_the_signature(signer, change):
    with pytest.raises(LicenseError, match="signature does not match") as refused:
        parse_license(change(signer.make().decode()).encode(), signer.keys)
    assert refused.value.code == "signature"


@pytest.mark.parametrize(
    "data",
    [
        b"%PDF-1.7 an invoice",
        b"[]",
        b'{"payload": {"v": 1}}',
        b'{"payload": [], "key": "test", "signature": "x"}',
        b"{" + b" " * 70_000 + b"}",
        b"[" * 60_000,
        "not a license".encode("utf-16-le"),  # UTF-16 with no BOM: not UTF-8 JSON
    ],
    # Named: a 70 KB input as a test's id would not fit in PYTEST_CURRENT_TEST on Windows (32767 characters).
    ids=[
        "an-invoice",
        "a-list",
        "no-key",
        "a-payload-not-an-object",
        "too-large",
        "nested-too-deeply",
        "utf-16-no-bom",
    ],
)
def test_a_file_that_is_no_license_is_refused_as_such(signer, data):
    with pytest.raises(LicenseError, match="not a license file") as refused:
        parse_license(data, signer.keys)
    assert refused.value.code == "format"


def test_a_payload_holding_a_lone_surrogate_is_no_license(signer):
    lone = json.dumps(signer.document()).replace("Acme Corp", "\N{REVERSE SOLIDUS}ud800")  # JSON's escape of one
    with pytest.raises(LicenseError, match="not a license file") as refused:
        parse_license(lone.encode(), signer.keys)
    assert refused.value.code == "format"


def test_a_license_signed_with_a_key_latet_does_not_know_is_refused(signer):
    with pytest.raises(LicenseError, match="'2019z', which this version of Latet does not know") as refused:
        parse_license(Signer(key_id="2019z").make(), signer.keys)
    assert refused.value.code == "key"


def test_with_no_public_key_every_license_is_refused_saying_so(signer):
    with pytest.raises(LicenseError, match="carries no key to check licenses with") as refused:
        parse_license(signer.make(), {})
    assert refused.value.code == "no-keys"


def test_a_license_of_another_version_is_refused(signer):
    with pytest.raises(LicenseError, match="version 2") as refused:
        parse_license(signer.make(v=2), signer.keys)
    assert refused.value.code == "version"


def test_a_refusal_never_quotes_what_the_file_holds(signer):
    with pytest.raises(LicenseError, match="signed with a key Latet does not know") as refused:
        parse_license(Signer(key_id="Acme Corp's key").make(), signer.keys)
    assert "Acme" not in str(refused.value) and refused.value.code == "key"
    for version in ("2, Acme Corp", 10**40, 1.0, True, None):
        with pytest.raises(LicenseError, match="a license of another version") as refused:
            parse_license(signer.make(v=version), signer.keys)
        assert "Acme" not in str(refused.value) and "0000" not in str(refused.value)
        assert refused.value.code == "version"


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"users": 0}, "number of users"),
        ({"users": "300"}, "number of users"),
        ({"users": True}, "number of users"),
        ({"licensee": " "}, "licensee is missing"),
        ({"features": "all"}, "features"),
        ({"issued": "2026-13-01"}, "issued is not a date"),
        ({"expires": "2026-10-31"}, "ends before it starts"),
        ({"kind": None}, "its kind is not subscription or trial"),  # left out: never read as a paid license
        ({"kind": "free"}, "its kind is not subscription or trial"),
        ({"kind": "trial"}, "a trial runs 32 days at most"),  # PAYLOAD's year
        ({**TRIAL, "expires": "2026-12-03"}, "a trial runs 32 days at most"),
    ],
)
def test_a_license_whose_content_is_wrong_is_refused(signer, changes, words):
    with pytest.raises(LicenseError, match=words) as refused:
        parse_license(signer.make(**changes), signer.keys)
    assert refused.value.code == "content"


# No case while Latet carries no key: the run shows it skipped, until the licensor's first key is in PUBLIC_KEYS.
@pytest.mark.parametrize(("key_id", "raw"), sorted(PUBLIC_KEYS.items()))
def test_the_keys_latet_carries_are_ed25519_public_keys(key_id, raw):
    assert KEY_ID.fullmatch(key_id) and len(raw) == 32
    Ed25519PublicKey.from_public_bytes(raw)


# --- the standing -------------------------------------------------------------------------------------------------


def test_no_file_is_no_license(signer):
    licensing = _licensing(MemoryLicenses(), signer)
    standing = licensing.standing()
    assert standing.state is State.NONE and standing.license is None and standing.notices == ()
    assert not licensing.licensed("locked-rules") and not licensing.producing("reports") and not licensing.manageable()


def test_a_valid_license_switches_on_the_features_it_lists_and_no_other(signer):
    licensing = _licensing(MemoryLicenses(), signer)
    licensing.install(signer.make(features=["locked-rules", "reports", "from-a-newer-latet"]))
    standing = licensing.standing()
    assert standing.state is State.VALID and standing.days_left == 244 and standing.source == "server"
    assert standing.users_in_use is None  # nothing counted them
    assert licensing.licensed("locked-rules") and licensing.producing("reports") and licensing.manageable()
    assert not licensing.licensed("sso")
    require(licensing, "locked-rules")
    with pytest.raises(NotLicensed, match="sso needs a Latet Enterprise license"):
        require(licensing, "sso")


@pytest.mark.parametrize(
    ("moment", "state", "notices"),
    [
        (at("2027-09-30"), State.VALID, ()),
        (at("2027-10-01"), State.EXPIRING, ("expiring",)),
        (at("2027-10-31", "23:59:59"), State.EXPIRING, ("expiring",)),
        (at("2027-11-01", "00:00:00"), State.GRACE, ("grace",)),
        (at("2027-11-30", "23:59:59"), State.GRACE, ("grace",)),
        (at("2027-12-01", "00:00:00"), State.PAUSED, ("paused",)),
    ],
)
def test_the_last_day_counts_whole_then_30_days_of_grace_then_the_pause(signer, moment, state, notices):
    clock = Clock(at("2027-03-01"))
    licensing = _licensing(MemoryLicenses(), signer, clock)
    licensing.install(signer.make())
    clock.now = moment
    standing = licensing.standing()
    assert standing.state is state and standing.notices == notices


def test_after_the_end_what_protects_stays_and_what_produces_pauses(signer):
    clock = Clock(at("2027-03-01"))
    licensing = _licensing(MemoryLicenses(), signer, clock)
    licensing.install(signer.make())
    clock.now = at("2027-11-10")
    assert licensing.standing().pauses_on() == date(2027, 12, 1)
    assert licensing.producing("reports") and licensing.manageable()
    clock.now = at("2029-01-01")
    assert licensing.licensed("locked-rules") and licensing.licensed("sso")
    assert not licensing.producing("reports") and not licensing.manageable()
    require(licensing, "locked-rules")


def test_a_paid_license_that_ended_still_serves_what_lets_people_in(signer):
    clock = Clock(at("2027-03-01"))
    licensing = _licensing(MemoryLicenses(), signer, clock)
    licensing.install(signer.make())
    clock.now = at("2029-01-01")  # paused: past its end and its grace
    assert licensing.serving("sso") and licensing.trial_ended_on() is None
    require_serving(licensing, "sso")  # people still sign in, approvals keep their route


@pytest.mark.parametrize(
    ("moment", "state", "notices"),
    [
        (at("2026-11-24", "23:59:59"), State.VALID, ()),
        (at("2026-11-25", "00:00:00"), State.EXPIRING, ("trial-week",)),  # 7 days before its last day
        (at("2026-11-30", "23:59:59"), State.EXPIRING, ("trial-week",)),
        (at("2026-12-01", "00:00:00"), State.EXPIRING, ("trial-day",)),  # 1 day before
        (at("2026-12-02", "23:59:59"), State.EXPIRING, ("trial-day",)),  # its last day
        (at("2026-12-03", "00:00:00"), State.TRIAL_ENDED, ("trial-ended",)),  # no grace
    ],
)
def test_a_trial_is_warned_7_days_and_1_day_before_and_ends_the_day_after(signer, moment, state, notices):
    clock = Clock(at("2026-11-01"))
    licensing = _licensing(MemoryLicenses(), signer, clock)
    licensing.install(signer.make(**TRIAL))
    clock.now = moment
    standing = licensing.standing()
    assert standing.state is state and standing.notices == notices


def test_a_trial_that_ended_stops_what_manages_produces_and_decides_never_what_protects(signer):
    clock = Clock(at("2026-11-01"))
    licensing = _licensing(MemoryLicenses(), signer, clock)
    licensing.install(signer.make(**TRIAL))
    assert licensing.serving("sso") and licensing.manageable() and licensing.producing("reports")
    clock.now = at("2026-12-03")
    assert licensing.licensed("locked-rules") and licensing.licensed("sso")  # what protects: exactly as it was
    assert not licensing.producing("reports") and not licensing.manageable() and not licensing.serving("sso")
    assert licensing.trial_ended_on() == date(2026, 12, 2) and licensing.standing().pauses_on() is None
    require(licensing, "locked-rules")
    with pytest.raises(TrialEnded, match="trial ended on 2026-12-02") as ended:
        require_serving(licensing, "sso")
    assert ended.value.ended_on == date(2026, 12, 2) and "protection is unchanged" in str(ended.value)
    with pytest.raises(NotLicensed):
        require_serving(_licensing(MemoryLicenses(), signer, clock), "sso")


ENDED_LINE = (
    "the trial ended on 2026-12-02; the Enterprise settings can be read, not changed, until a license is installed or "
    "the trial's file removed, protection is unchanged"
)


@pytest.mark.parametrize(
    ("moment", "status", "words"),
    [
        (
            at("2026-11-25"),
            "warn",
            "until 2026-12-02; it ends in 7 days, with no grace: then the Enterprise management",
        ),
        (at("2026-12-02"), "warn", "it ends today, with no grace"),
        (at("2026-12-03"), "fail", ENDED_LINE),
    ],
)
def test_the_line_of_a_trial_says_its_warnings_and_its_end(signer, moment, status, words):
    clock = Clock(at("2026-11-01"))
    licensing = _licensing(MemoryLicenses(), signer, clock)
    licensing.install(signer.make(**TRIAL))
    clock.now = moment
    said, line = describe(licensing.standing())
    assert said == status and words in line and "Acme Corp (L-2027-0001, trial)" in line, line


def test_a_clock_moved_back_after_a_trial_ended_revives_it_nowhere_its_end_is_kept(signer):
    clock, store = Clock(at("2026-11-01")), MemoryLicenses()
    licensing = _licensing(store, signer, clock)
    licensing.install(signer.make(**TRIAL))
    clock.now = at("2026-12-03")
    assert licensing.standing().state is State.TRIAL_ENDED and store.ended == {"L-2027-0001"}
    clock.now = at("2026-11-20")  # the server's clock set back
    standing = licensing.standing()
    assert standing.state is State.TRIAL_ENDED and standing.notices == ("trial-ended",)
    with pytest.raises(LicenseError, match="trial ended on 2026-12-02") as refused:
        licensing.install(signer.make(**TRIAL))  # its file again: refused, whatever the clock says
    assert refused.value.code == "ended"


def test_a_clock_that_runs_ahead_ends_a_trial_only_for_itself_and_keeps_nothing(signer):
    clock, store = Clock(at("2026-11-01")), MemoryLicenses()
    store.today = date(2026, 11, 20)  # the store's own clock (a server's PostgreSQL) says the trial runs
    licensing = _licensing(store, signer, clock)
    licensing.install(signer.make(**TRIAL))
    clock.now = at("2026-12-10")  # this instance's clock runs ahead
    assert licensing.standing().state is State.TRIAL_ENDED and store.ended == set()
    clock.now = at("2026-11-20")  # set right again: the trial runs, with its own days, and ended nowhere early
    standing = licensing.standing()
    assert standing.state is State.VALID and standing.days_left == 12


class _Asked(MemoryLicenses):
    """A server's store that counts how often it is asked to keep a trial as ended."""

    def __init__(self) -> None:
        super().__init__()
        self.asked = 0

    def keep_trial_ended(self, license_id, last_day):
        self.asked += 1
        return super().keep_trial_ended(license_id, last_day)


def test_an_instance_ahead_of_the_stores_clock_asks_it_to_keep_the_end_once_every_refresh(signer):
    clock, store = Clock(at("2026-11-01")), _Asked()
    store.today = date(2026, 11, 20)  # PostgreSQL's clock: the trial runs
    licensing = _licensing(store, signer, clock)
    licensing.install(signer.make(**TRIAL))
    clock.now = at("2026-12-10")  # this instance's clock runs ahead
    for _ in range(5):
        assert licensing.standing().state is State.TRIAL_ENDED
    assert store.asked == 1 and store.ended == set()  # its refusal is taken as said, not asked at every look
    clock.now += REFRESH_SECONDS - 1
    licensing.standing()
    assert store.asked == 1
    clock.now += 1
    store.today = date(2026, 12, 3)  # and now PostgreSQL agrees
    licensing.standing()
    assert store.asked == 2 and store.ended == {"L-2027-0001"}
    licensing.standing()
    assert store.asked == 2  # kept: nothing more to ask


def test_the_same_trial_installed_again_keeps_its_own_dates(signer):
    clock = Clock(at("2026-11-01"))
    licensing = _licensing(MemoryLicenses(), signer, clock)
    licensing.install(signer.make(**TRIAL))
    clock.now = at("2026-11-28")
    licensing.install(signer.make(**TRIAL))  # again, days later: its dates are the file's, signed
    standing = licensing.standing()
    assert standing.license.expires == date(2026, 12, 2) and standing.days_left == 4
    assert standing.notices == ("trial-week",)


def test_from_90_percent_of_the_users_bought_a_notice_says_so(signer):
    count = [269]
    licensing = _licensing(MemoryLicenses(), signer, count=lambda: count[0])
    licensing.install(signer.make())
    assert licensing.standing().notices == ()
    count[0] = 270
    standing = licensing.standing()
    assert standing.notices == ("users-near",) and standing.users_in_use == 270 and standing.state is State.VALID


def test_more_users_than_bought_a_notice_at_once_the_pause_after_30_days_and_a_place_freed_clears_it(signer):
    clock, count, store = Clock(at("2027-03-01")), [301], MemoryLicenses()
    licensing = _licensing(store, signer, clock, lambda: count[0])
    licensing.install(signer.make())
    start = clock.now
    standing = licensing.standing()
    assert standing.state is State.GRACE and standing.notices == ("users-over",) and standing.over_since == start
    assert standing.pauses_on() == date(2027, 3, 31)
    clock.now = start + 30 * DAY - 1
    assert licensing.standing().state is State.GRACE
    clock.now = start + 30 * DAY
    standing = licensing.standing()
    assert standing.state is State.PAUSED and standing.notices == ("users-over", "paused")
    assert licensing.licensed("locked-rules") and not licensing.producing("reports")
    count[0] = 300  # a person or an API key disabled: a place freed
    standing = licensing.standing()
    assert standing.state is State.VALID and standing.over_since is None and store.since is None


def test_the_grace_is_the_stores_for_every_instance_and_a_new_count_over_starts_a_new_one(signer):
    clock, count, store = Clock(at("2027-03-01")), [301], MemoryLicenses()
    _licensing(store, signer, clock, lambda: count[0]).install(signer.make())
    first = _licensing(store, signer, clock, lambda: count[0]).standing()  # counted over: the grace starts
    clock.now += 10 * DAY
    other = _licensing(store, signer, clock, lambda: count[0]).standing()  # another instance, or a restart
    assert other.over_since == first.over_since == at("2027-03-01")
    clock.now += 21 * DAY
    assert _licensing(store, signer, clock, lambda: count[0]).standing().state is State.PAUSED
    count[0] = 299
    _licensing(store, signer, clock, lambda: count[0]).standing()
    count[0] = 302
    again = _licensing(store, signer, clock, lambda: count[0]).standing()
    assert again.state is State.GRACE and again.over_since == clock.now


def test_removing_and_installing_the_license_again_starts_no_new_grace(signer):
    clock, store = Clock(at("2027-03-01")), MemoryLicenses()
    licensing = _licensing(store, signer, clock, lambda: 301)
    licensing.install(signer.make())
    started = licensing.standing().over_since
    clock.now += 10 * DAY
    assert licensing.remove() is True and licensing.standing().state is State.NONE
    licensing.install(signer.make())
    assert licensing.standing().over_since == started


def test_a_count_that_fails_says_nothing_of_the_users_and_keeps_the_grace(signer, caplog):
    store = MemoryLicenses()
    _licensing(store, signer, count=lambda: 301).install(signer.make())
    _licensing(store, signer, count=lambda: 301).standing()

    def broken():
        raise ConnectionError("the database at 10.1.2.3:5432 is restarting")

    with caplog.at_level(logging.WARNING, logger="latet.ee.license"):
        standing = _licensing(store, signer, count=broken).standing()
    assert standing.state is State.VALID and standing.users_in_use is None and store.since == at("2027-03-01")
    assert "ConnectionError" in caplog.text and "10.1.2.3" not in caplog.text  # said by its type alone


def test_a_file_that_cannot_be_used_is_no_license_and_says_why(signer):
    store = MemoryLicenses()
    licensing = _licensing(store, signer)
    store.data = b"not a license"  # written by what verified nothing: no features said
    standing = licensing.standing()
    assert standing.state is State.INVALID and standing.notices == ("invalid",) and standing.reason_code == "format"
    assert "not a license file" in standing.reason
    assert not licensing.licensed("locked-rules") and not licensing.manageable()  # no license was ever verified


def test_a_license_that_cannot_be_read_says_so(signer):
    class Unreadable:
        def read(self):
            return Stored(None, "server", ("x",), "the server's database could not be read (OperationalError)")

    standing = _licensing(Unreadable(), signer).standing()
    assert standing.state is State.INVALID and standing.reason_code == "unreadable" and standing.source == "server"
    assert "OperationalError" in standing.reason
    said, line = describe(standing)
    assert said == "fail" and "could not be weighed" in line and "PostgreSQL answers" in line


@pytest.mark.parametrize(
    "spoil",
    [
        lambda data: b"not a license",  # written in the database by hand
        lambda data: data.decode("utf-8").encode("utf-16-le"),  # no BOM
        lambda data: b"[" * 60_000,
    ],
    ids=["garbage", "utf-16-no-bom", "nested-too-deeply"],
)
@pytest.mark.parametrize("kept", ["installers", "none"])
def test_a_file_that_cannot_be_used_stops_what_manages_never_what_protects(signer, spoil, kept):
    """Kept: the features the instance that installed it verified are kept beside the file (``Stored.features``), or
    none are (a document written by what verified nothing): what protects keeps them, else the last license this
    instance verified."""
    store = MemoryLicenses()
    _licensing(store, signer).install(signer.make(features=["locked-rules", "reports", "sso"]))
    licensing = _licensing(store, signer)  # an instance that read it
    assert licensing.licensed("locked-rules")
    store.data = spoil(store.data)
    if kept == "none":
        store.features = None
    else:
        licensing = _licensing(store, signer)  # one that verified nothing: only the stored features can answer
    standing = licensing.standing()
    assert standing.state is State.INVALID and standing.reason_code == "format" and standing.license is None
    assert licensing.licensed("locked-rules") and licensing.licensed("sso")  # what its installer, or it, verified
    assert not licensing.licensed("approvals")
    require(licensing, "locked-rules")
    assert not licensing.manageable() and not licensing.producing("reports") and not licensing.serving("sso")
    with pytest.raises(NotLicensed):
        require_serving(licensing, "sso")
    said, line = describe(standing)  # one wording for `latet server status` and `latet server license show`
    assert said == "fail" and "the license file cannot be used" in line and "is off" in line
    assert "every instance keeps what protects as the features the instance that installed it verified" in line
    assert "until the file is fixed" in line and "latet server license install" in line


def test_on_a_server_a_file_that_cannot_be_used_keeps_what_its_installer_verified_on_every_instance(signer):
    store = MemoryLicenses()
    Licensing(store, keys=signer.keys).install(signer.make(features=["locked-rules"], expires="2099-12-31"))
    store.data = b"not a license"  # written in PostgreSQL by hand, the features the installer verified kept beside it
    restarted = Licensing(store, keys=signer.keys)  # an instance that never verified it itself
    assert restarted.licensed("locked-rules") and not restarted.licensed("reports")
    said, line = describe(restarted.standing())  # `latet server status`, `latet server license show`
    assert said == "fail" and "the license file cannot be used" in line and "is off" in line
    assert "every instance keeps what protects as the features the instance that installed it verified" in line
    assert "until the file is fixed" in line and "Latet restarts" not in line
    assert "latet server license install" in line


def test_only_a_removal_or_a_valid_license_without_a_feature_switches_off_what_protects(signer):
    store = MemoryLicenses()
    licensing = _licensing(store, signer)
    licensing.install(signer.make())
    licensing.install(signer.make(features=["reports"]))  # another license, valid: what it lists, nothing else
    assert not licensing.licensed("locked-rules") and licensing.licensed("reports")
    store.data = b"not a license"  # its installer's features kept beside it
    assert licensing.standing().state is State.INVALID  # the broken document was read, not a copy of the last one
    assert not licensing.licensed("locked-rules") and licensing.licensed("reports")
    assert licensing.remove() is True
    assert licensing.standing().state is State.NONE and not licensing.licensed("reports")


def test_a_removal_then_a_file_that_cannot_be_used_keeps_the_last_license_verified(signer):
    store = MemoryLicenses()
    licensing = _licensing(store, signer)
    licensing.install(signer.make())
    assert licensing.remove() is True
    assert licensing.standing().state is State.NONE and not licensing.licensed("locked-rules")
    store.data = b"not a license"  # the removal took the features said with it away: none kept
    assert licensing.standing().state is State.INVALID and not licensing.manageable()
    assert licensing.licensed("locked-rules")  # a file that cannot be used only ever adds protection, never lowers it


class _Failing(MemoryLicenses):
    """A server's store whose count of users and trials' bookkeeping fail (the database restarting)."""

    def over_since(self):
        raise ConnectionError("the database is restarting")

    def keep_over_since(self, moment):
        raise ConnectionError("the database is restarting")

    def ended_trials(self):
        raise ConnectionError("the database is restarting")

    def keep_trial_ended(self, license_id, last_day):
        raise ConnectionError("the database is restarting")


def test_a_license_this_latet_cannot_verify_answers_with_the_features_its_installer_verified(signer):
    store = MemoryLicenses()  # a server's, shared by an instance of a newer release and one of an older
    newer = Signer(key_id="newer")
    here = _licensing(store, signer)  # the older: it does not know the newer release's key
    there = Licensing(store, clock=Clock(at("2027-03-01")), keys={**signer.keys, **newer.keys})
    here.install(signer.make(features=["reports"]))
    assert here.licensed("reports") and not here.licensed("locked-rules")
    there.install(newer.make(id="L-2027-0002", features=["locked-rules"]))
    assert here.standing().state is State.INVALID and not here.manageable()
    assert here.licensed("locked-rules") and not here.licensed("reports")  # the installer's, not its own last one
    store.write(b"not a license")  # written by what verified nothing: no features said
    assert here.licensed("reports") and not here.licensed("locked-rules")  # the last license it verified


def test_what_protects_is_weighed_from_the_file_alone(signer):
    counted = []

    def count():
        counted.append(1)
        return 301

    store = _Failing()
    store.write(signer.make(**ENDED_TRIAL))  # a trial that ended, over the users bought
    licensing = _licensing(store, signer, count=count)
    assert licensing.licensed("locked-rules") and not licensing.licensed("from-a-newer-latet")
    require(licensing, "locked-rules")
    assert counted == []  # no count of the users, no trial kept as ended: only the file, read and checked
    with pytest.raises(ConnectionError):
        licensing.standing()  # the standing weighs them, and says when it cannot
    store.write(b"not a license")
    assert licensing.licensed("locked-rules")  # the last license verified
    store.delete()
    assert not licensing.licensed("locked-rules")


class _Down(MemoryLicenses):
    """A server's store whose database stops answering when told (``down``)."""

    down = False

    def read(self):
        if self.down:
            raise ConnectionError("connection to server at 10.0.0.5, port 5432 failed")
        return super().read()


def test_a_store_that_does_not_answer_keeps_the_features_last_read(signer, caplog, monkeypatch):
    """M1 for a store that fails: what protects keeps the features the store last answered, never raises, and says
    the failure by its type alone."""
    from latet.logline import Paced

    monkeypatch.setattr(license_module, "_UNANSWERED", Paced())  # whatever an earlier test made happen
    store, clock = _Down(), Clock(at("2027-03-01"))
    store.write(signer.make(features=["locked-rules"]), frozenset({"locked-rules"}))
    licensing = Licensing(store, clock=clock, keys={})  # it verifies nothing: the stored features
    assert licensing.licensed("locked-rules")
    store.down = True
    with caplog.at_level(logging.WARNING, logger="latet.ee.license"):
        assert licensing.licensed("locked-rules") and not licensing.licensed("reports")
    assert "ConnectionError" in caplog.text and "10.0.0.5" not in caplog.text
    store.down = False
    store.delete()
    clock.now += REFRESH_SECONDS  # the store is asked again once REFRESH_SECONDS passed since it failed
    assert not licensing.licensed("locked-rules")  # removed: what it last read is no license
    store.down = True
    assert not licensing.licensed("locked-rules")


class _Counting(_Down):
    """``_Down``, counting how many times it is read."""

    reads = 0

    def read(self):
        self.reads += 1
        return super().read()


def test_during_an_outage_what_protects_asks_the_store_at_most_once_every_refresh(signer):
    """L-B: a store that fails is asked again REFRESH_SECONDS later, not at every look of what protects; meanwhile the
    features last read answer."""
    store, clock = _Counting(), Clock(at("2027-03-01"))
    store.write(signer.make(features=["locked-rules"]))
    licensing = _licensing(store, signer, clock)
    assert licensing.licensed("locked-rules") and store.reads == 1
    store.down = True
    for _ in range(50):
        assert licensing.licensed("locked-rules") and not licensing.licensed("reports")
    assert store.reads == 2  # the failing read, once
    clock.now += REFRESH_SECONDS - 1
    assert licensing.licensed("locked-rules") and store.reads == 2
    clock.now += 1
    assert licensing.licensed("locked-rules") and store.reads == 3  # asked again, failing again
    store.down = False
    clock.now += REFRESH_SECONDS
    assert licensing.licensed("locked-rules") and store.reads == 4
    assert licensing.licensed("locked-rules") and store.reads == 5  # answering: read at each look, as before


def test_what_protects_never_waits_behind_a_weighing(signer):
    """L-B / M5: a weighing that waits for a slow count of the users (a database that answers slowly) holds no lock
    ``licensed`` needs: what protects answers meanwhile."""
    import threading
    import time

    counting, released = threading.Event(), threading.Event()

    def slow_count():
        counting.set()
        released.wait(8)
        return 3

    store = MemoryLicenses()
    store.write(signer.make(features=["locked-rules"]))
    licensing = _licensing(store, signer, count=slow_count)
    weighing = threading.Thread(target=licensing.standing, daemon=True)
    weighing.start()
    try:
        assert counting.wait(5)
        started = time.monotonic()
        assert licensing.licensed("locked-rules")
        assert time.monotonic() - started < 2, "what protects waited behind the weighing"
    finally:
        released.set()
        weighing.join(10)


def test_a_store_that_never_answered_keeps_the_last_license_verified_or_none(signer):
    store = _Down()
    store.down = True
    assert not _licensing(store, signer).licensed("locked-rules")  # never verified anything: off
    store.down = False
    verified = _licensing(store, signer)
    verified.install(signer.make(features=["reports"]))  # verified here, and not read back since
    store.down = True
    assert verified.licensed("reports") and not verified.licensed("locked-rules")


def test_whatever_reading_a_license_raises_never_leaves_the_standing(signer, monkeypatch, caplog):
    store = MemoryLicenses()
    licensing = _licensing(store, signer)
    licensing.install(signer.make())

    def raising(data, keys):
        raise RecursionError("maximum recursion depth exceeded while decoding a JSON array")

    monkeypatch.setattr(license_module, "parse_license", raising)
    store.data = signer.make(licensee="Acme Corp UK")  # changed: read again
    with caplog.at_level(logging.WARNING, logger="latet.ee.license"):
        standing = licensing.standing()
    assert standing.state is State.INVALID and standing.reason_code == "format"
    assert "RecursionError" in caplog.text and "maximum recursion" not in caplog.text


# --- installing, removing ------------------------------------------------------------------------------------------


def test_installing_checks_the_file_first_and_a_wrong_one_leaves_the_license_there(signer):
    licensing = _licensing(MemoryLicenses(), signer)
    assert licensing.install(signer.make()).id == "L-2027-0001"
    wrong = (
        b"%PDF-1.7",
        b"x" * 300_000,
        Signer().make(id="L-2027-0002"),
        signer.make(id="L-2027-0003", issued="2025-11-01", expires="2026-10-31"),
    )
    for data in wrong:
        with pytest.raises(LicenseError):
            licensing.install(data)
    assert licensing.standing().license.id == "L-2027-0001"


def test_a_license_that_has_ended_is_not_installed(signer):
    with pytest.raises(LicenseError, match="it ended on 2026-10-31") as refused:
        _licensing(MemoryLicenses(), signer).install(signer.make(issued="2025-11-01", expires="2026-10-31"))
    assert refused.value.code == "ended"


def test_check_refuses_what_install_refuses_and_keeps_nothing(signer):
    store = MemoryLicenses()
    licensing = _licensing(store, signer)
    assert licensing.check(signer.make()).id == "L-2027-0001"
    assert store.data is None and store.version == 0 and licensing.standing().license is None
    with pytest.raises(LicenseError, match="it ended on 2026-10-31"):
        licensing.check(signer.make(issued="2025-11-01", expires="2026-10-31"))
    with pytest.raises(LicenseError):
        licensing.check(b"%PDF-1.7")


def test_the_file_is_read_again_when_it_changes(signer):
    store = MemoryLicenses()
    licensing = _licensing(store, signer)
    licensing.install(signer.make())
    store.data = signer.make(licensee="Acme Corp UK")  # another document under the same row version: read again
    assert licensing.standing().license.licensee == "Acme Corp UK"
    store.data = signer.make(licensee="Acme Corp US")  # the same size too
    assert licensing.standing().license.licensee == "Acme Corp US"


def test_removing_takes_the_installed_license_away(signer):
    licensing = _licensing(MemoryLicenses(), signer)
    licensing.install(signer.make())
    assert licensing.remove() is True and licensing.remove() is False
    assert licensing.standing().state is State.NONE


def test_what_the_api_says_of_a_standing_reads_back_as_the_same_standing(signer):
    clock, store = Clock(at("2026-11-01")), MemoryLicenses()
    licensing = _licensing(store, signer, clock)
    licensing.install(signer.make(**TRIAL))
    valid = licensing.standing()
    clock.now = at("2026-12-03")
    ended = licensing.standing()
    store.data = b"{}"
    invalid = licensing.standing()
    assert invalid.state is State.INVALID and invalid.source == "server"
    for standing in (valid, ended, invalid):
        assert standing_of(json.loads(json.dumps(details(standing)))) == standing


def test_checking_a_license_reaches_for_no_network(signer, offline):
    licensing = _licensing(MemoryLicenses(), signer, count=lambda: 3)
    licensing.install(signer.make())
    assert licensing.standing().state is State.VALID and licensing.licensed("reports")
    parse_license(signer.make(), signer.keys)
