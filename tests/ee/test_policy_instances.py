"""On a server, the policy is set through the administration API of any instance: sealed in PostgreSQL, recorded in
the administration log with no value in the same transaction, and every space, people's and API keys', on every
instance, is raised to it at once: told on Valkey, or at the next check when the message was lost. Once a trial ends
it is frozen: shown, in force, never changed."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from typer.testing import CliRunner

from latet import __version__
from latet.cli.agent import MARKERS
from latet.config import Config, Destination, Profile
from latet.ee import policy_server
from latet.ee.admin import ADMIN_EE
from latet.ee.policy_routes import ADMIN_POLICY, NOT_UNICODE
from latet.ee.policy_server import PgPolicyStore, PolicyUnavailable, StoredPolicy
from latet.server.app import create_server_app

from ..server.support import listening
from .licenses import TRIAL, Clock, at
from .servers import URL, services

pytestmark = [pytest.mark.server, services]

POLICY = "latet_rules: 1\nprofile: max\nterms: [{value: Project Falcon, type: PROJECT}]\n"
TEXT = "Ship Project Falcon on Monday"
LASTING = {"expires": "2099-12-31"}


@pytest.fixture
def opened(instances, licensed, monkeypatch):
    """Instances of one server whose license has locked rules (E1's test license): `opened(check)` opens two, each
    checking the stored policy every `check` seconds (60 by default: then only a message on ee:policy tells them in
    time)."""

    def open_two(check=60.0):
        monkeypatch.setattr(policy_server, "CHECK", check)
        first, second = instances.open(), instances.open()
        first.edition.install_license(licensed.make(expires="2099-12-31"), actor="admin:t1")
        for server in (first, second):
            server.edition.refresh()  # the license read again, the policy weighed with it
        return first, second

    return open_two


def _admin(server):
    """The instance's application, called with an administration token (no lifespan: the instance is not stopped
    here, as in E1's tests)."""
    client = TestClient(create_server_app(server), base_url=URL)
    client.headers["x-latet-admin"] = server.admin.issue("tests")
    return client


async def _eventually(check, seconds=10.0):
    """Until ``check()`` (or the coroutine it answers) is true: what one instance does, the other hears soon."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        result = check()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        assert loop.time() < deadline, "not in time"
        await asyncio.sleep(0.05)


def _rows(server):
    with server.db.connection() as conn:
        return conn.execute("SELECT count(*) FROM ee_policy").fetchone()[0]


async def test_a_policy_set_through_one_instance_raises_every_space_on_the_other(opened, caplog):
    first, second = opened()
    emily, _ = first.identities.add_person("Emily Carter", "emily.carter@acme.com")
    billing, _ = first.identities.add_key("Billing CI")
    async with listening(second):  # its ear on Valkey: no space is built without it
        person = await second.spaces.runtime(emily)  # both spaces built on the second instance, before the policy
        await second.spaces.runtime(billing)
        assert person.in_use().profile == Profile.BALANCED and "Project Falcon" in person.built.masker.mask(TEXT).text
        answer = _admin(first).put(ADMIN_POLICY, json={"policy": POLICY})
        assert answer.status_code == 200, answer.text
        assert answer.json()["keys"] == ["profile", "terms"] and answer.json()["problems"] == []
        for identity in (emily, billing):

            async def raised(identity=identity):
                space = await second.spaces.runtime(identity)
                return space.in_use().profile == Profile.MAX and space.config.profile == Profile.BALANCED

            await _eventually(raised)  # heard on ee:policy: the check is a minute away; their own settings stay theirs
        person = await second.spaces.runtime(emily)
        assert "Project Falcon" not in person.built.masker.mask(TEXT).text
    [entry] = first.audit.query(action="policy.changed")
    assert entry.actor.startswith("admin:") and entry.details["keys"] == ["profile", "terms"]
    assert "Project Falcon" not in json.dumps(entry.details) + caplog.text


def test_the_policy_is_sealed_in_the_database_and_its_text_goes_to_administrators_only(opened):
    first, _ = opened()
    admin = _admin(first)
    assert TestClient(create_server_app(first), base_url=URL).get(ADMIN_POLICY).status_code == 401
    assert admin.put(ADMIN_POLICY, json={"policy": "profile: [max"}).status_code == 400
    assert admin.put(ADMIN_POLICY, json={"policies": POLICY}).status_code == 400
    loose = admin.put(ADMIN_POLICY, json={"policy": POLICY + "exceptions: everyone\n"}).json()
    assert loose["exceptions"] == "company" and len(loose["notices"]) == 1  # read as the stricter, and said
    assert admin.put(ADMIN_POLICY, json={"policy": POLICY}).status_code == 200
    shown = admin.get(ADMIN_POLICY).json()["policy"]
    assert shown["text"] == POLICY and shown["in_force"] is True and shown["set_by"].startswith("admin:")
    with first.db.connection() as conn:
        (sealed,) = conn.execute("SELECT sealed FROM ee_policy").fetchone()
    assert b"Project Falcon" not in bytes(sealed)
    assert PgPolicyStore(first.db, first.master).load().text == POLICY
    assert [entry.action for entry in first.audit.query(action="policy.changed")] == ["policy.changed"] * 2
    assert admin.delete(ADMIN_POLICY).json() == {"removed": True}
    assert admin.get(ADMIN_POLICY).json() == {"policy": None, "problems": [], "etag": None}
    assert admin.delete(ADMIN_POLICY).json() == {"removed": False}
    assert len(first.audit.query(action="policy.changed")) == 3  # a removal of nothing records nothing
    assert first.audit.query(action="ee.policy.change")[0].details == {"removed": False, "status": 200}


def test_a_policy_with_keys_left_out_whole_is_set_only_when_the_body_says_they_were_seen(opened):
    """Security review, L-a: one malformed term leaves out every term. The administration API never sets that unseen:
    refused, the keys named (one Latet does not know counted, never quoted), unless the body accepts it."""
    first, _ = opened()
    admin = _admin(first)
    text = POLICY.replace("type: PROJECT}", "type: PROJECT, case_sensitive: often}") + "Falcon-key: 1\n"
    refused = admin.put(ADMIN_POLICY, json={"policy": text})
    assert refused.status_code == 400 and "terms, 1 key that is not a rule" in refused.json()["error"]
    assert "Falcon" not in refused.text and admin.get(ADMIN_POLICY).json()["policy"] is None
    assert admin.put(ADMIN_POLICY, json={"policy": text, "accept_notices": "yes"}).status_code == 400  # true alone
    done = admin.put(ADMIN_POLICY, json={"policy": text, "accept_notices": True})
    assert done.status_code == 200 and done.json()["keys"] == ["profile"] and len(done.json()["notices"]) == 2
    assert admin.put(ADMIN_POLICY, json={"policy": POLICY + "exceptions: everyone\n"}).status_code == 200  # a key read


def test_a_policy_with_a_key_written_with_no_value_is_set_only_when_the_body_says_it_was_seen(opened):
    """Final review L1: `profile:` with no value locks nothing; the PUT never sets that unseen, as a key with a mistake
    in it; `exceptions:` with no value reads as `company`, the stricter, and is said."""
    first, _ = opened()
    admin = _admin(first)
    text = "latet_rules: 1\nexceptions:\nprofile:\nocr: auto\n"
    refused = admin.put(ADMIN_POLICY, json={"policy": text})
    assert refused.status_code == 400 and "could not read in profile," in refused.json()["error"], refused.text
    assert admin.get(ADMIN_POLICY).json()["policy"] is None
    done = admin.put(ADMIN_POLICY, json={"policy": text, "accept_notices": True}).json()
    assert done["keys"] == ["ocr"] and done["exceptions"] == "company" and len(done["notices"]) == 2


def test_a_set_that_names_the_policy_it_replaces_is_refused_when_that_changed_meanwhile(opened):
    """Final review L3: `latet server policy set` names the etag of the policy its GET answered (null: none stored),
    checked in the change's transaction: a policy set in between, from the Policy page or another administrator, is
    never replaced unseen (409, nothing changed). Without an etag the PUT checks nothing, as before."""
    from latet.ee.policy_routes import CHANGED

    first, second = opened()
    admin = _admin(first)
    assert admin.get(ADMIN_POLICY).json() == {"policy": None, "problems": [], "etag": None}
    assert admin.put(ADMIN_POLICY, json={"policy": POLICY, "etag": None}).status_code == 200
    seen = admin.get(ADMIN_POLICY).json()
    assert seen["etag"] == seen["policy"]["etag"] and len(seen["etag"]) == 64
    stricter = POLICY + "exceptions: company\n"
    assert _admin(second).put(ADMIN_POLICY, json={"policy": stricter, "etag": seen["etag"]}).status_code == 200
    stale = admin.put(ADMIN_POLICY, json={"policy": POLICY + "ocr: auto\n", "etag": seen["etag"]})
    assert stale.status_code == 409 and stale.json() == {"error": CHANGED, "code": "policy-changed"}, stale.text
    again = admin.put(ADMIN_POLICY, json={"policy": POLICY, "etag": None})  # none stored, it said: one is
    assert again.status_code == 409
    assert PgPolicyStore(first.db, first.master).load().text == stricter
    assert len(first.audit.query(action="policy.changed")) == 2  # the refused ones record no change
    for etag in ("", "a" * 129, 7, ["x"], True):
        assert admin.put(ADMIN_POLICY, json={"policy": POLICY, "etag": etag}).status_code == 400
    now = admin.get(ADMIN_POLICY).json()["etag"]
    assert admin.put(ADMIN_POLICY, json={"policy": POLICY, "etag": now}).status_code == 200
    assert admin.put(ADMIN_POLICY, json={"policy": stricter}).status_code == 200  # no etag: nothing checked
    assert PgPolicyStore(first.db, first.master).load().text == stricter


def test_a_removal_that_names_the_policy_it_removes_is_refused_when_that_changed_meanwhile(opened):
    """`latet server policy remove` names the etag of the policy its GET answered, as `set` does: a policy set in
    between is never removed unseen (409, nothing changed). A body with no etag, or none at all, removes as before."""
    from latet.ee.policy_routes import CHANGED

    first, second = opened()
    admin = _admin(first)
    assert admin.put(ADMIN_POLICY, json={"policy": POLICY}).status_code == 200
    seen = admin.get(ADMIN_POLICY).json()["etag"]
    stricter = POLICY + "exceptions: company\n"
    assert _admin(second).put(ADMIN_POLICY, json={"policy": stricter}).status_code == 200
    stale = admin.request("DELETE", ADMIN_POLICY, json={"etag": seen})
    assert stale.status_code == 409 and stale.json() == {"error": CHANGED, "code": "policy-changed"}, stale.text
    assert admin.request("DELETE", ADMIN_POLICY, json={"etag": None}).status_code == 409  # none stored, it said
    for body in (b"{", b"[]", b'{"etag": 7}', b'{"etag": ""}', b'{"etag": "' + b"a" * 129 + b'"}'):
        assert admin.request("DELETE", ADMIN_POLICY, content=body).status_code == 400, body
    assert admin.request("DELETE", ADMIN_POLICY, content=b" " * 2048).status_code == 413
    assert PgPolicyStore(first.db, first.master).load().text == stricter
    assert len(first.audit.query(action="policy.changed")) == 2  # the refused ones record no change
    now = admin.get(ADMIN_POLICY).json()["etag"]
    assert admin.request("DELETE", ADMIN_POLICY, json={"etag": now}).json() == {"removed": True}
    assert admin.request("DELETE", ADMIN_POLICY, json={"etag": None}).json() == {"removed": False}  # none, as it said
    assert admin.put(ADMIN_POLICY, json={"policy": POLICY}).status_code == 200
    assert admin.request("DELETE", ADMIN_POLICY, json={}).json() == {"removed": True}  # no etag: nothing checked
    assert _rows(first) == 0


def test_a_license_without_locked_rules_shows_the_policy_and_changes_nothing(instances, licensed):
    server = instances.open()
    server.edition.install_license(licensed.make(expires="2099-12-31", features=["reports"]), actor="admin:t1")
    server.edition.refresh()
    admin = _admin(server)
    refused = admin.put(ADMIN_POLICY, json={"policy": POLICY})
    assert refused.status_code == 403 and refused.json()["feature"] == "locked-rules", refused.text
    assert admin.delete(ADMIN_POLICY).status_code == 403
    unread = admin.put(ADMIN_POLICY, content=b" " * (5 * 1024 * 1024))  # said before the body is read
    assert unread.status_code == 403 and unread.json()["feature"] == "locked-rules"
    assert admin.get(ADMIN_POLICY).json() == {"policy": None, "problems": [], "etag": None}
    assert server.audit.query(action="policy.changed") == []


def test_a_license_without_locked_rules_installed_elsewhere_meanwhile_lets_no_policy_change(instances, licensed):
    """F1: the license is weighed again in the change's own transaction, from its row read there: an instance whose
    copy still lists locked rules (another instance installed one without them, its message lost) changes nothing."""
    from latet.edition import server_context
    from latet.ee.edition import EnterpriseEdition

    a, b = instances.open(), instances.open()
    a.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    PgPolicyStore(a.db, a.master).save(StoredPolicy(POLICY, "1a2b3c4d5e6f7081", 1_791_191_520.0, "admin:tests"))
    b.edition.close()
    b.edition = EnterpriseEdition(server_context(b), listen=False, ticker=Clock(0.0))  # its copies never grow old
    assert b.edition.licensing.licensed("locked-rules") and b.edition.licensing.manageable()
    a.edition.install_license(licensed.make(id="L-2027-0002", features=["reports"], **LASTING), actor="admin:t1")
    assert b.edition.licensing.licensed("locked-rules")  # still b's copy: it heard nothing
    admin = _admin(b)
    for answer in (admin.put(ADMIN_POLICY, json={"policy": "latet_rules: 1\nprofile: balanced\n"}),
                   admin.delete(ADMIN_POLICY)):  # fmt: skip
        assert answer.status_code == 403 and answer.json()["feature"] == "locked-rules", answer.text
        assert "code" not in answer.json()  # valid, without the feature
    a.edition.remove_license(actor="admin:t1")  # none at all now, b's copy says otherwise still
    for answer in (admin.put(ADMIN_POLICY, json={"policy": POLICY}), admin.delete(ADMIN_POLICY)):
        assert answer.status_code == 403 and answer.json()["code"] == "not-manageable", answer.text
    assert b.audit.query(action="policy.changed") == []
    assert PgPolicyStore(b.db, b.master).load().text == POLICY  # neither replaced nor removed


async def test_a_license_installed_on_one_instance_brings_the_policy_on_the_other(instances, licensed, monkeypatch):
    monkeypatch.setattr(policy_server, "CHECK", 60.0)  # only the message on ee:license tells the other in time
    first, second = instances.open(), instances.open()
    stored = StoredPolicy(POLICY, "1a2b3c4d5e6f7081", 1_791_191_520.0, "admin:tests")
    PgPolicyStore(first.db, first.master).save(stored)  # a policy stored before any license: not in force
    for server in (first, second):
        server.edition.policy.refresh()
    assert second.edition.floor(None) is None
    first.edition.install_license(licensed.make(expires="2099-12-31"), actor="admin:t1")

    def in_force():
        return second.edition.floor(None) is not None

    await _eventually(in_force)


async def test_once_a_trial_ended_the_policy_is_frozen_shown_and_in_force(instances, licensed, clock):
    clock.now = at("2026-11-10")
    server = instances.open()
    server.edition.install_license(licensed.make(**TRIAL), actor="admin:t1")
    server.edition.refresh()
    admin = _admin(server)
    assert admin.put(ADMIN_POLICY, json={"policy": POLICY}).status_code == 200
    emily, _ = server.identities.add_person("Emily Carter", "emily.carter@acme.com")
    clock.now = at("2026-12-03")  # the day after the trial's last
    server.edition.refresh()
    for answer in (admin.put(ADMIN_POLICY, json={"policy": "latet_rules: 1\nprofile: balanced\n"}),
                   admin.delete(ADMIN_POLICY),
                   admin.put(ADMIN_POLICY, content=b" " * (5 * 1024 * 1024))):  # fmt: skip
        assert answer.status_code == 403 and answer.json()["code"] == "trial-ended", answer.text
    shown = admin.get(ADMIN_POLICY).json()["policy"]
    assert shown["text"] == POLICY and shown["in_force"] is True
    async with listening(server):
        space = await server.spaces.runtime(emily)
        assert space.in_use().profile == Profile.MAX  # protection never touched
    assert len(server.audit.query(action="policy.changed")) == 1


async def test_an_instance_that_missed_the_message_catches_up_at_its_next_check(opened):
    first, second = opened(check=0.5)
    emily, _ = first.identities.add_person("Emily Carter", "emily.carter@acme.com")
    async with listening(second):
        await second.spaces.runtime(emily)
        stored = StoredPolicy(POLICY, "1a2b3c4d5e6f7081", 1_791_191_520.0, "admin:tests")
        PgPolicyStore(first.db, first.master).save(stored)  # written, never announced: a message lost

        async def raised():
            return (await second.spaces.runtime(emily)).in_use().profile == Profile.MAX

        await _eventually(raised)


async def test_two_administrators_setting_it_through_two_instances_leave_the_last_on_both(opened):
    first, second = opened()
    stricter = POLICY + "policies: {IBAN: redact}\n"
    assert _admin(first).put(ADMIN_POLICY, json={"policy": POLICY}).status_code == 200
    last = _admin(second).put(ADMIN_POLICY, json={"policy": stricter}).json()["etag"]

    def both_hold_the_last():
        return all(server.edition.floor(None).etag == last for server in (first, second))

    await _eventually(both_hold_the_last)


async def test_a_change_waiting_for_approval_stays_waiting_across_a_policy_set(opened):
    """F4: the floor is applied to the settings as they apply: a change of the shared settings that lowers protection,
    which waits for her approval, is not applied when a policy is set."""
    first, _ = opened()
    emily, _ = first.identities.add_person("Emily Carter", "emily.carter@acme.com")
    gateways = [Destination(name=name, url=f"https://{name}.acme.com") for name in ("atlas", "falcon")]
    first.shared.set(Config(upstreams=gateways[:1]), actor="admin:a_first00000")
    async with listening(first):
        await first.spaces.runtime(emily)  # her approved settings: atlas
        await first.spaces.drop(emily.id)
        first.shared.set(Config(upstreams=gateways[1:]), actor="admin:a_first00000")  # atlas taken away, falcon added
        runtime = await first.spaces.runtime(emily)  # built afterwards: falcon applies, atlas's removal waits for her
        waiting = list(runtime.mediator.approvals.changes)
        assert waiting and {"atlas", "falcon"} <= {d.name for d in runtime.config.upstreams}
        assert _admin(first).put(ADMIN_POLICY, json={"policy": POLICY}).status_code == 200
        await _eventually(lambda: runtime.in_use().profile == Profile.MAX)
        assert {"atlas", "falcon"} <= {d.name for d in runtime.config.upstreams}  # still waiting, not applied
        assert list(runtime.mediator.approvals.changes) == waiting


def test_an_instance_that_cannot_read_the_stored_policy_starts_and_its_administration_removes_it(instances, licensed):
    """L2, end to end: a policy stored that an instance cannot read (made by a newer release) stops no instance, so
    its administration API removes it; meanwhile it builds no space."""
    first = instances.open()
    first.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    newer = StoredPolicy("latet_rules: 99\nprofile: max\n", "e" * 64, 1_791_191_520.0, "admin:tests")
    PgPolicyStore(first.db, first.master).save(newer)
    second = instances.open()  # starts all the same
    with pytest.raises(PolicyUnavailable):
        second.edition.floor(None)
    admin = _admin(second)
    shown = admin.get(ADMIN_POLICY).json()
    assert shown["policy"] is None and len(shown["problems"]) == 1
    assert shown["etag"] == "e" * 64  # what a set replacing it names (final review L3), its text unread
    assert admin.delete(ADMIN_POLICY).json() == {"removed": True}
    assert second.edition.floor(None) is None and _rows(second) == 0
    [entry] = second.audit.query(action="policy.changed")
    assert entry.details == {"removed": True, "status": 200}


def test_a_body_that_is_no_policy_is_refused_and_changes_nothing(opened):
    """F15: a lone surrogate (JSON's escape of one), a body past twice a rules file's size, a body that is not JSON:
    400 or 413, nothing written, never a 500."""
    from latet.transfer import MAX_BYTES

    first, _ = opened()
    admin = _admin(first)
    surrogate = ('{"policy": "latet_rules: 1' + chr(92) + "n" + "terms: [{value: " + chr(92) + 'ud800}]"}').encode()
    answer = admin.put(ADMIN_POLICY, content=surrogate, headers={"content-type": "application/json"})
    assert answer.status_code == 400 and answer.json()["error"] == NOT_UNICODE, answer.text  # refused as a body
    huge = admin.put(ADMIN_POLICY, content=b" " * (2 * MAX_BYTES + 1), headers={"content-type": "application/json"})
    assert huge.status_code == 413
    for body in (b"{", b"\xff\xfe{}", b"[" * 10_000 + b"]" * 10_000, b'{"policy": 3}', b'{"policy": NaN}'):
        assert admin.put(ADMIN_POLICY, content=body).status_code == 400
    assert _rows(first) == 0 and first.audit.query(action="policy.changed") == []


def test_while_an_instance_that_applies_no_policy_runs_setting_one_says_so(opened):
    """M3's rolling upgrade: an instance of a release before locked rules says it is alive as ``1``, and one of this
    release without the Enterprise Edition names its edition; while either runs, a policy set answers a problem saying
    such instances do not apply it until they run this release with the Enterprise Edition."""
    first, _ = opened()
    bus = first.bus
    bus.client.set(bus.key("instance:0a1b2c3d4e5f"), f"{__version__} enterprise".encode(), px=60_000)
    assert _admin(first).put(ADMIN_POLICY, json={"policy": POLICY}).json()["problems"] == []
    bus.client.set(bus.key("instance:abcdef012345"), b"1", px=60_000)  # an older release's
    answer = _admin(first).put(ADMIN_POLICY, json={"policy": POLICY + "ocr: auto\n"})
    assert answer.status_code == 200, answer.text
    [problem] = answer.json()["problems"]
    assert problem.startswith("1 instance does not apply the policy") and "as it starts" in problem
    bus.client.set(bus.key("instance:abcdef012346"), f"{__version__} community".encode(), px=60_000)
    [problem] = _admin(first).put(ADMIN_POLICY, json={"policy": POLICY}).json()["problems"]
    assert problem.startswith("2 instances do not apply the policy")


def test_a_policy_set_while_valkey_cannot_tell_the_others_says_they_take_it_at_their_check(opened, monkeypatch):
    """The change committed, this instance reads it again first; the others are told on ee:policy, and when that
    message cannot go out the answer says they take it within the check's seconds."""
    first, _ = opened()

    def refused(channel, message):
        raise ConnectionError("Valkey at 10.0.0.7 refused")

    monkeypatch.setattr(first.bus, "publish", refused)
    answer = _admin(first).put(ADMIN_POLICY, json={"policy": POLICY})
    assert answer.status_code == 200, answer.text
    [problem] = answer.json()["problems"]
    assert "within 60 seconds" in problem and "10.0.0.7" not in answer.text
    assert first.edition.floor(None) is not None  # read again here at once


def test_without_any_license_both_checks_say_the_policy_is_not_manageable(instances, licensed):
    """R1: no license is `not-manageable` before the body is read and in the change's transaction alike; a license
    without the feature is `feature` in both (the test above and the F1 test)."""
    from latet.edition import server_context
    from latet.ee.edition import EnterpriseEdition

    a, b = instances.open(), instances.open()
    admin = _admin(a)
    for answer in (admin.put(ADMIN_POLICY, json={"policy": POLICY}), admin.delete(ADMIN_POLICY),
                   admin.put(ADMIN_POLICY, content=b" " * (5 * 1024 * 1024))):  # fmt: skip
        assert answer.status_code == 403 and answer.json()["code"] == "not-manageable", answer.text
    a.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    b.edition.close()
    b.edition = EnterpriseEdition(server_context(b), listen=False, ticker=Clock(0.0))  # its copies never grow old
    a.edition.remove_license(actor="admin:t1")  # none at all now; b's copy still holds it
    answer = _admin(b).put(ADMIN_POLICY, json={"policy": POLICY})
    assert answer.status_code == 403 and answer.json()["code"] == "not-manageable", answer.text
    assert _rows(a) == 0


def test_a_policy_is_read_before_its_change_takes_any_row(opened, monkeypatch):
    """A text that is no policy is refused before the transaction holds the token's and the license's rows."""
    first, _ = opened()
    held = []
    standing_in = first.edition.licensing.standing_in
    monkeypatch.setattr(first.edition.licensing, "standing_in", lambda conn: held.append(1) or standing_in(conn))
    assert _admin(first).put(ADMIN_POLICY, json={"policy": "profile: [max"}).status_code == 400
    assert held == []
    assert _admin(first).put(ADMIN_POLICY, json={"policy": POLICY}).status_code == 200 and held == [1]


def test_a_database_that_does_not_answer_is_said_in_fixed_words(opened, monkeypatch):
    first, _ = opened()

    def unanswered():
        raise ConnectionError("connection to 10.0.0.5 refused")

    monkeypatch.setattr(first.edition.policy._store, "load", unanswered)
    answer = _admin(first).get(ADMIN_POLICY)
    assert answer.status_code == 503 and answer.headers["retry-after"] == "1", answer.text
    assert answer.json() == {"error": "the server's policy cannot be read now: try again"}


async def test_shared_settings_below_the_policy_are_raised_in_every_space_never_refused(opened):
    """Planning ruling 11: settings that arrive are raised, never refused. Shared settings set below the policy on one
    instance reach a space on the other, which stays open, raised to the floor."""
    first, second = opened()
    billing, _ = first.identities.add_key("Billing CI")
    assert _admin(first).put(ADMIN_POLICY, json={"policy": POLICY + "ocr: auto\n"}).status_code == 200
    async with listening(second):
        await _eventually(lambda: second.edition.floor(None) is not None)
        space = await second.spaces.runtime(billing)
        first.shared.set(Config(ocr="off"), actor="admin:a_first00000")  # below the policy
        await _eventually(lambda: space.config.ocr == "off")  # heard on latet:shared, applied as set
        assert space.in_use().ocr == "auto" and space.in_use().profile == Profile.MAX  # raised
        assert await second.spaces.runtime(billing) is space and second.spaces.count() == 1  # open, never closed


# --- the license: a change that ends the policy says so (L4) ------------------------------------------------------


def test_a_license_change_that_ends_the_policy_says_so_in_its_entry(opened, licensed):
    first, _ = opened()
    admin = _admin(first)
    reports = {"license": licensed.make(id="L-2027-0002", features=["reports"], **LASTING).decode()}
    full = {"license": licensed.make(id="L-2027-0003", **LASTING).decode()}
    assert admin.put(f"{ADMIN_EE}/license", json=reports).status_code == 200  # no policy stored: nothing ends
    assert admin.put(f"{ADMIN_EE}/license", json=full).status_code == 200
    assert admin.put(ADMIN_POLICY, json={"policy": POLICY}).status_code == 200
    assert admin.put(f"{ADMIN_EE}/license", json=full).status_code == 200  # with locked rules: it stays
    assert admin.put(f"{ADMIN_EE}/license", json=reports).status_code == 200  # the policy stops here
    assert admin.put(f"{ADMIN_EE}/license", json=reports).status_code == 200  # it was not in force: nothing stops
    assert admin.delete(f"{ADMIN_EE}/license").status_code == 200  # nor here
    assert admin.put(f"{ADMIN_EE}/license", json=full).status_code == 200
    assert admin.delete(f"{ADMIN_EE}/license").status_code == 200  # in force until this removal
    said = [e.details.get("policy") for e in reversed(first.audit.query(action="license.installed", limit=10))]
    assert said == [None, None, None, None, "stops", None, None]  # E1's own install, then the six here
    removed = [e.details.get("policy") for e in reversed(first.audit.query(action="license.removed"))]
    assert removed == [None, "stops"]


def _damaged(server):
    """The license's row as a damaged row, or one signed with a key this release does not know, leaves it: a document
    that does not verify, beside the features the instance that installed it verified (M1 keeps protecting with
    them). Every instance of the server reads it again."""
    with server.db.transaction() as conn:
        conn.execute("UPDATE ee_license SET document = %s, version = version + 1 WHERE id = 1", (b"not a license",))
    server.edition.refresh()


def test_over_a_license_file_this_instance_cannot_verify_a_change_says_the_policy_stops(opened, licensed):
    """The final security review's L2: while the stored file does not verify, the policy stays in force with the
    features its installer verified (M1), so a license change that ends it is weighed from those features, and its
    entry says the policy stops, as it does over a file that verifies."""
    first, _ = opened()
    admin = _admin(first)
    reports = {"license": licensed.make(id="L-2027-0002", features=["reports"], **LASTING).decode()}
    full = licensed.make(id="L-2027-0003", **LASTING)
    assert admin.put(ADMIN_POLICY, json={"policy": POLICY}).status_code == 200
    _damaged(first)
    assert first.edition.standing().state.value == "invalid" and first.edition.licensing.licensed("locked-rules")
    assert admin.put(f"{ADMIN_EE}/license", json=reports).status_code == 200
    [entry] = [e for e in first.audit.query(action="license.installed") if e.target == "L-2027-0002"]
    assert entry.details.get("policy") == "stops", entry.details
    first.edition.install_license(full, actor="admin:t1")
    _damaged(first)
    assert admin.delete(f"{ADMIN_EE}/license").json()["removed"] is True
    [removed] = first.audit.query(action="license.removed")
    assert removed.details.get("policy") == "stops", removed.details


def test_a_license_change_that_switches_central_approvals_off_says_so_in_its_entry(opened, licensed):
    """The final security review's L1: while the security team decides on changes or on the Live Reviewer's texts, a
    license without approvals sends each back to whoever asks; the change's entry says central approvals stop, as one
    that ends the policy says it. Nothing of it while nobody routes anything to the security team."""
    first, _ = opened()
    admin = _admin(first)
    without = {"license": licensed.make(id="L-2027-0002", features=["locked-rules", "reports"], **LASTING).decode()}
    full = {"license": licensed.make(id="L-2027-0003", **LASTING).decode()}
    store = first.edition.settings_store
    assert admin.put(f"{ADMIN_EE}/license", json=without).status_code == 200  # whoever asks decides: nothing stops
    assert admin.put(f"{ADMIN_EE}/license", json=full).status_code == 200
    store.set_section("approvals", {"changes": "security"}, actor="admin:tests")
    assert admin.put(f"{ADMIN_EE}/license", json=full).status_code == 200  # it lists approvals: they go on
    assert admin.put(f"{ADMIN_EE}/license", json=without).status_code == 200  # central approvals stop here
    assert admin.put(f"{ADMIN_EE}/license", json=without).status_code == 200  # they were off already
    assert admin.put(f"{ADMIN_EE}/license", json=full).status_code == 200
    store.set_section("approvals", {"changes": "self", "live_review": "security"}, actor="admin:tests")
    assert admin.delete(f"{ADMIN_EE}/license").status_code == 200  # the texts' route stops here
    said = [e.details.get("approvals") for e in reversed(first.audit.query(action="license.installed", limit=10))]
    assert said == [None, None, None, None, "stop", None, None]  # E1's own install, then the six here
    [removed] = first.audit.query(action="license.removed")
    assert removed.details.get("approvals") == "stop" and "policy" not in removed.details, removed.details


@pytest.fixture
def cli(opened, monkeypatch, tmp_path):
    """`latet server …`, E0c's administration call answered by the first instance; run by you, not by an agent."""
    from latet.cli import server as server_cli
    from latet.server.client import _refusal

    first, _ = opened()
    admin = _admin(first)

    def call(method, path, body=None, **given):
        answer = admin.request(method, f"/_latet/server/v1/admin/{path}", json=body, params=given.get("query"))
        if answer.status_code >= 400:
            raise _refusal(answer)
        return answer.json()

    monkeypatch.setattr(server_cli, "admin_call", call)
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", admin.headers["x-latet-admin"])
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config" / "config.yaml"))
    for name in ("LATET_CONNECTION", "LATET_SERVER_FINGERPRINT", "AGENT"):
        monkeypatch.delenv(name, raising=False)
    for markers in MARKERS.values():
        for name in markers:
            monkeypatch.delenv(name, raising=False)

    def run(*args, **kwargs):
        import latet.cli.admin  # noqa: F401 - registers the commands
        from latet.cli.main import app

        return CliRunner().invoke(app, ["server", *args], **kwargs)

    run.admin, run.server = admin, first
    return run


def test_removing_the_license_while_a_policy_is_stored_says_the_policy_stops_and_asks(cli, monkeypatch):
    assert cli.admin.put(ADMIN_POLICY, json={"policy": POLICY}).status_code == 200
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: True)
    kept = cli("license", "remove", input="n\n")
    assert kept.exit_code == 1 and "the server's policy stops being in force" in kept.output, kept.output
    assert cli.server.audit.query(action="license.removed") == []
    removed = cli("license", "remove", "--yes")
    assert removed.exit_code == 0 and "License removed" in removed.output, removed.output
    assert "The server's policy stops being in force" in removed.output  # said with --yes too


def test_removing_a_license_without_locked_rules_says_nothing_of_the_policy(cli, licensed, monkeypatch):
    assert cli.admin.put(ADMIN_POLICY, json={"policy": POLICY}).status_code == 200
    reports = {"license": licensed.make(id="L-2027-0002", features=["reports"], **LASTING).decode()}
    assert cli.admin.put(f"{ADMIN_EE}/license", json=reports).status_code == 200  # the policy is no longer in force
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: True)
    removed = cli("license", "remove", input="y\n")
    assert removed.exit_code == 0 and "policy" not in removed.output, removed.output


def test_installing_a_license_without_locked_rules_while_a_policy_is_stored_asks_first(cli, licensed, tmp_path,
                                                                                         monkeypatch):  # fmt: skip
    path = tmp_path / "reports.json"
    path.write_bytes(licensed.make(id="L-2027-0002", features=["reports"], **LASTING))
    with_them = tmp_path / "full.json"
    with_them.write_bytes(licensed.make(id="L-2027-0003", **LASTING))
    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli("license", "install", str(path)).exit_code == 0  # no policy stored: nothing ends, nothing to ask
    assert cli("license", "install", str(with_them)).exit_code == 0
    assert cli.admin.put(ADMIN_POLICY, json={"policy": POLICY}).status_code == 200
    by_an_agent = cli("license", "install", str(path), "--yes")
    assert by_an_agent.exit_code == 2 and "run it yourself" in by_an_agent.output, by_an_agent.output
    assert cli("license", "install", str(with_them)).exit_code == 0  # it keeps locked rules: nothing to ask
    monkeypatch.delenv("CLAUDECODE")
    not_at_a_terminal = cli("license", "install", str(path))
    assert not_at_a_terminal.exit_code == 2 and "--yes" in not_at_a_terminal.output
    assert "the server's policy stops being in force" in not_at_a_terminal.output
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: True)
    kept = cli("license", "install", str(path), input="n\n")
    assert kept.exit_code == 1 and "the server's policy stops being in force. Install it?" in kept.output
    assert len(cli.server.audit.query(action="license.installed")) == 4  # none of the three refused here
    installed = cli("license", "install", str(path), "--yes")
    assert installed.exit_code == 0 and "L-2027-0002" in installed.output, installed.output
    assert "The server's policy stops being in force" in installed.output  # said with --yes too
    monkeypatch.setenv("CLAUDECODE", "1")
    again = cli("license", "install", str(path))  # the policy is not in force already: nothing stops, nothing asked
    assert again.exit_code == 0 and "stops" not in again.output, again.output


def test_a_server_of_a_release_before_locked_rules_is_asked_nothing_of_a_policy(cli, monkeypatch):
    from latet.cli import server as server_cli
    from latet.server.client import ServerError

    asked = server_cli.admin_call

    def older(method, path, body=None, **given):
        if path == "ee/policy":
            raise ServerError(404, "not found")
        return asked(method, path, body, **given)

    monkeypatch.setattr(server_cli, "admin_call", older)
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: True)
    removed = cli("license", "remove", input="y\n")
    assert removed.exit_code == 0 and "License removed" in removed.output, removed.output
    assert "policy" not in removed.output


def test_over_a_license_file_the_server_cannot_verify_the_command_asks_of_a_stored_policy(cli, licensed, tmp_path,
                                                                                          monkeypatch):  # fmt: skip
    """The final security review's L2: the server's answer names no license while its file does not verify, yet the
    policy stays in force with the features its installer verified: a change asks of a stored policy all the same."""
    assert cli.admin.put(ADMIN_POLICY, json={"policy": POLICY}).status_code == 200
    _damaged(cli.server)
    path = tmp_path / "reports.json"
    path.write_bytes(licensed.make(id="L-2027-0002", features=["reports"], **LASTING))
    not_at_a_terminal = cli("license", "install", str(path))
    assert not_at_a_terminal.exit_code == 2, not_at_a_terminal.output
    assert "the server's policy stops being in force" in not_at_a_terminal.output
    assert [e.target for e in cli.server.audit.query(action="license.installed")] == ["L-2027-0001"]  # opened's own
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: True)
    kept = cli("license", "remove", input="n\n")
    assert kept.exit_code == 1 and "the server's policy stops being in force" in kept.output, kept.output
    assert cli.server.audit.query(action="license.removed") == []


def test_a_license_without_approvals_while_the_security_team_decides_asks_first(cli, licensed, tmp_path, monkeypatch):
    """The final security review's L1: `latet server license install` and `remove` say which of what goes to the
    security team would go back to whoever asks, ask first, and refuse an agent; nothing while nothing goes there, or
    while the license in place lists no approvals."""
    path = tmp_path / "without.json"
    path.write_bytes(licensed.make(id="L-2027-0002", features=["locked-rules", "reports"], **LASTING))
    full = tmp_path / "full.json"
    full.write_bytes(licensed.make(id="L-2027-0003", **LASTING))
    store = cli.server.edition.settings_store
    assert cli("license", "install", str(path)).exit_code == 0  # whoever asks decides: nothing to ask
    assert cli("license", "install", str(full)).exit_code == 0
    store.set_section("approvals", {"changes": "security"}, actor="admin:tests")
    changes = "changes that lower protection no longer go to the security team"
    refused = cli("license", "install", str(path))
    assert refused.exit_code == 2 and changes in refused.output and "--yes" in refused.output, refused.output
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: True)
    kept = cli("license", "install", str(path), input="n\n")
    assert kept.exit_code == 1 and f"This license does not include approvals: {changes}. Install it?" in kept.output
    assert "policy" not in kept.output, kept.output
    installed = cli("license", "install", str(path), "--yes")
    assert installed.exit_code == 0 and "Changes that lower protection no longer go to the security team." in (
        installed.output
    ), installed.output
    entry = next(e for e in cli.server.audit.query(action="license.installed") if e.target == "L-2027-0002")  # last
    assert entry.details.get("approvals") == "stop"
    again = cli("license", "install", str(path), input="n\n")  # the license in place lists no approvals: nothing asked
    assert again.exit_code == 0 and "security team" not in again.output, again.output
    assert cli("license", "install", str(full)).exit_code == 0
    store.set_section("approvals", {"changes": "security", "live_review": "security"}, actor="admin:tests")
    both = "changes that lower protection and the Live Reviewer's texts no longer go to the security team"
    kept = cli("license", "remove", input="n\n")
    assert kept.exit_code == 1 and f"everyone on it, and {both}." in kept.output, kept.output
    store.set_section("approvals", {"changes": "self", "live_review": "security"}, actor="admin:tests")
    removed = cli("license", "remove", input="y\n")
    assert removed.exit_code == 0 and "The Live Reviewer's texts no longer go to the security team." in removed.output
    [removal] = cli.server.audit.query(action="license.removed")
    assert removal.details.get("approvals") == "stop"


KEEPS_ROLES = ["locked-rules", "reports", "siem", "term-sync", "sso", "roles"]  # every feature but approvals


def test_a_license_that_keeps_roles_and_drops_approvals_says_an_api_keys_changes_stop_going_to_its_controllers(
    opened, licensed
):
    """The final security review's L1' (its re-review): with `changes: self`, a license with approvals sends an API
    key's change to another person who controls the key; one that keeps `roles` (the key's space stays open) and drops
    approvals lets whoever works there approve their own change. While the server has an API key, that change's entry
    says central approvals stop; a license without `roles` closes the key's space, and a removal too: nothing of it."""
    first, _ = opened()
    admin = _admin(first)
    keeps = {"license": licensed.make(id="L-2027-0002", features=KEEPS_ROLES, **LASTING).decode()}
    closes = {"license": licensed.make(id="L-2027-0004", features=["locked-rules", "reports"], **LASTING).decode()}
    full = {"license": licensed.make(id="L-2027-0003", **LASTING).decode()}
    assert admin.put(f"{ADMIN_EE}/license", json=keeps).status_code == 200  # no API key: nothing stops
    assert admin.put(f"{ADMIN_EE}/license", json=full).status_code == 200
    first.identities.add_key("Billing CI")
    assert admin.put(f"{ADMIN_EE}/license", json=keeps).status_code == 200  # the key's four eyes stop here
    assert admin.put(f"{ADMIN_EE}/license", json=full).status_code == 200
    assert admin.put(f"{ADMIN_EE}/license", json=closes).status_code == 200  # the key's space closes: nothing
    assert admin.put(f"{ADMIN_EE}/license", json=full).status_code == 200
    assert admin.delete(f"{ADMIN_EE}/license").status_code == 200  # nor here
    said = [e.details.get("approvals") for e in reversed(first.audit.query(action="license.installed", limit=10))]
    assert said == [None, None, None, "stop", None, None, None]  # E1's own install, then the six here
    [removed] = first.audit.query(action="license.removed")
    assert "approvals" not in removed.details, removed.details
    assert admin.get(f"{ADMIN_EE}/approvals/settings").status_code == 403  # no license: approvals' route is closed
    first.edition.install_license(licensed.make(id="L-2027-0005", **LASTING), actor="admin:t1")
    assert admin.get(f"{ADMIN_EE}/approvals/settings").json()["api_keys"] is True  # what `latet server` reads


def test_a_license_that_keeps_roles_and_drops_approvals_asks_of_an_api_keys_controllers(cli, licensed, tmp_path,
                                                                                        monkeypatch):  # fmt: skip
    """L1' at the command line: `latet server license install` reads from `GET ee/approvals/settings` that the server
    has an API key, says that its changes no longer go to its other controllers, and asks; nothing of it for a file
    without `roles`, nor for a removal."""
    keeps = tmp_path / "keeps.json"
    keeps.write_bytes(licensed.make(id="L-2027-0002", features=KEEPS_ROLES, **LASTING))
    closes = tmp_path / "closes.json"
    closes.write_bytes(licensed.make(id="L-2027-0004", features=["locked-rules", "reports"], **LASTING))
    full = tmp_path / "full.json"
    full.write_bytes(licensed.make(id="L-2027-0003", **LASTING))
    cli.server.identities.add_key("Billing CI")
    controllers = "an API key's changes that lower protection no longer go to its other controllers"
    refused = cli("license", "install", str(keeps))
    assert refused.exit_code == 2 and controllers in refused.output and "--yes" in refused.output, refused.output
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: True)
    installed = cli("license", "install", str(keeps), "--yes")
    assert installed.exit_code == 0 and f"{controllers[:1].upper()}{controllers[1:]}." in installed.output
    assert cli("license", "install", str(full)).exit_code == 0
    quiet = cli("license", "install", str(closes), input="n\n")  # the key's space closes: nothing asked
    assert quiet.exit_code == 0 and "controllers" not in quiet.output, quiet.output
    assert cli("license", "install", str(full)).exit_code == 0
    kept = cli("license", "remove", input="n\n")
    assert kept.exit_code == 1 and "controllers" not in kept.output, kept.output


SECRET_POLICY = (
    "latet_rules: 1\nexceptions: company\nprofile: max\n"
    "terms: [{value: Project Falcon, type: PROJECT}]\nallow_values: [acme-staging.example]\n"
    "data_files: ['northwind/*.csv']\ndata_hosts: ['*.northwind.example']\n"
    "fields: [{name: falcon_notes}, {name: raw_notes, mask: false}]\n"
)
SECRETS = ("Falcon", "acme-staging", "northwind", "raw_notes")  # a field rule is named, never one kept in clear


def _person_sees(server, identity):
    """What a person's dashboard answers at `GET /ee/policy` on this server: the real edition's routes, the real
    ServerPolicy, the person's own settings below the policy's."""
    dashboard = SimpleNamespace(
        guarded=lambda handler: handler,
        rt=SimpleNamespace(identity=identity.ref(), config=Config()),
        _in_pool=asyncio.to_thread,
    )
    app = Starlette(routes=server.edition.dashboard_routes(dashboard))

    async def handed(scope, receive, send):  # as the server's gate hands a request on to the person's space
        await app({**scope, "latet.identity": identity.id}, receive, send)

    with TestClient(handed) as client:
        answer = client.get("/_latet/api/v1/ee/policy")
    assert answer.status_code == 200, answer.text
    return answer.json()


@pytest.mark.parametrize("state", ["licensed", "no license", "trial ended"])
def test_a_person_is_told_the_servers_policy_by_the_real_route_and_no_value_of_it(instances, licensed, clock, state):
    clock.now = at("2026-11-10")
    server = instances.open()
    server.edition.install_license(licensed.make(**(TRIAL if state == "trial ended" else LASTING)), actor="admin:t1")
    server.edition.refresh()
    assert _admin(server).put(ADMIN_POLICY, json={"policy": SECRET_POLICY}).status_code == 200
    emily, _ = server.identities.add_person("Emily Carter", "emily.carter@acme.com")
    if state == "no license":
        server.edition.remove_license(actor="admin:t1")
    if state == "trial ended":
        clock.now = at("2026-12-03")
    server.edition.refresh()
    view = _person_sees(server, emily)
    for secret in SECRETS:
        assert secret not in json.dumps(view), secret
    if state == "no license":
        assert view["floors"] == [] and view["below"] == [] and len(view["problems"]) == 1  # not in force, said
        return
    (floor,) = view["floors"]  # a trial that ended leaves it shown and in force
    assert (floor["source"], floor["exceptions"], "etag" in floor) == ("server", "company", False)
    rules = floor["rules"]
    assert (rules["terms"], rules["data_files"], rules["data_hosts"]) == (1, 1, 1)
    assert (rules["fields"], rules["fields_clear"]) == (["falcon_notes"], 1)
    assert "profile: from max to balanced" in view["below"] and view["problems"] == []
