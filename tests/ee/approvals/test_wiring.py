"""Central approvals wired to a server (Latet Enterprise), on real PostgreSQL and Valkey: who is who read from E0b's
identities and E3's people, a read that fails raised and never taken for an identity gone; a decision reaching the
asking identity's space with its settings read again, and what stops it answered; /me in an API key's space never
naming the person working there among who decides on what they make there; the record kept and read whatever the
license says, while deciding follows it; and a change whose record cannot be written neither waiting nor applying."""

import time

import psycopg
import pytest

from latet.ee.approvals import PostgresRecords, ServerDirectory
from latet.ee.approvals_api import GONE, NOT_YOURS_TO_SEE, UNAPPLIED, UNKEPT, UNREACHED
from latet.ee.roles import Role
from latet.floor import FLOOR_UNAVAILABLE, FloorUnavailable
from latet.gateway.approvals import Batch
from latet.runtime import Runtime
from latet.server.space_settings import PgSettings
from latet.server.spaces import SpaceUnavailable
from latet.server.tokens import route_key

from ..events.servers import company_session
from ..licenses import PAYLOAD
from ..people.server_support import API, BY_HAND, SERVER_TESTS, UI, add_person, end_trial, license, on_trial

pytestmark = SERVER_TESTS
QUEUE = f"{API}/ee/approvals"
PROMPT = "Send the overdue notice to laura.bennett@whitfield.co.uk about Project Falcon"


@pytest.fixture
def company(cluster):
    """Emily asks; Mark decides for the security team, on changes and texts."""
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    mark = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com", Role.SECURITY)
    both = {"changes": "security", "live_review": "security"}
    cluster.server.edition.settings_store.set_section("approvals", both, actor=BY_HAND)
    for server in cluster.servers:
        server.edition.refresh()
    return cluster, emily, mark


def _lowering(client, path=f"{API}/config"):
    etag = client.get(path).json()["etag"]
    return client.put(path, json={"allow_values": ["ACME"], "etag": etag}, headers=UI)


def _unread(*args, **kwargs):
    raise psycopg.OperationalError("the database does not answer")


def test_the_server_s_directory_lists_enabled_identities_and_raises_what_it_cannot_read(company, monkeypatch):
    """The review of Task 6's carry: lists of identities, never sets of ids; a disabled key controlled by nobody's
    request (R57); and a read that fails raised, never None (an identity gone, which would end what it asked for)."""
    cluster, emily, mark = company
    identities, people = cluster.server.identities, cluster.people
    directory = ServerDirectory(identities, people)
    daniel = add_person(cluster, "Daniel Hayes", "daniel.hayes@acme.com", Role.SECURITY)
    key, _ = identities.add_key("Billing CI")
    retired, _ = identities.add_key("Old CI")
    for one in (key, retired):
        people.add_controller(one.id, emily.id, actor=BY_HAND)
    identities.disable(retired.id)
    holding = directory.holding(Role.SECURITY)
    assert isinstance(holding, list) and sorted(p.id for p in holding) == sorted([mark.id, daniel.id])
    assert [p.id for p in directory.controllers(key.id)] == [emily.id]
    assert [k.id for k in directory.controlled(emily.id)] == [key.id]
    assert directory.roles(key.id) is None and Role.SECURITY in (directory.roles(mark.id) or ())
    assert directory.identity(retired.id) is None and directory.group_of(emily.id) is None
    monkeypatch.setattr(identities, "_one", _unread)
    with pytest.raises(psycopg.OperationalError):
        directory.identity("u_nobodyhere")


def test_who_controls_a_key_is_read_in_the_decision_s_transaction_alone(company, monkeypatch):
    """The review of Task 7, m4: in the decision's transaction, a key's controllers are read through its connection,
    with no lookup of this instance's copies (none takes a second connection while the transaction holds one)."""
    cluster, emily, _ = company
    identities, people = cluster.server.identities, cluster.people
    key, _ = identities.add_key("Billing CI")
    people.add_controller(key.id, emily.id, actor=BY_HAND)
    directory = ServerDirectory(identities, people)
    with monkeypatch.context() as patch, cluster.server.db.transaction() as conn:
        patch.setattr(identities, "get", _unread)
        assert [(p.id, p.name, p.kind) for p in directory.controllers(key.id, conn=conn)] == [
            (emily.id, "Emily Carter", "person")
        ]


@pytest.mark.parametrize(
    "taken_away",
    [
        "DELETE FROM roles WHERE subject = %s",  # his role taken away
        "UPDATE identities SET disabled = true WHERE id = %s",  # he is disabled
    ],
)
def test_a_decider_no_longer_one_behind_every_copy_decides_nothing(company, taken_away):
    """The review of Task 7, I2 (R13, Review Focus 6): his role taken away, or he disabled, in PostgreSQL behind this
    instance's copies, as on another instance a moment before it is heard here. The page and its gates still see him as
    a decider; the decision's transaction does not: 403, no entry in the administration log, and it still waits."""
    cluster, emily, mark = company
    central = cluster.server.edition.approvals
    assert central is not None
    company_session(cluster, emily, "code")
    pending = _lowering(cluster.a).json()["pending"]
    company_session(cluster, mark, "code")
    assert cluster.a.get(QUEUE).status_code == 200  # his copies say he decides for the security team
    with cluster.server.db.transaction() as conn:
        conn.execute(taken_away, (mark.id,))  # past People and E0b's identities: no copy is told
    assert cluster.a.get(QUEUE).status_code == 200  # still, as this instance's copies have it
    refused = cluster.a.post(f"{QUEUE}/changes/{pending['id']}", json={"action": "approve"}, headers=UI)
    assert (refused.status_code, refused.json()) == (403, {"error": NOT_YOURS_TO_SEE, "code": "not-yours"})
    assert cluster.server.audit.query(action="approval.decided") == []
    assert central.records.get(pending["id"]).state is None


def test_a_queue_read_while_who_is_who_cannot_be_read_ends_nothing(company, monkeypatch):
    """The review of Task 6's carry: an identity that cannot be read is never taken for one removed, whose items the
    queue ends ``gone`` at once."""
    cluster, emily, mark = company
    company_session(cluster, emily, "code")
    pending = _lowering(cluster.a).json()["pending"]
    central = cluster.server.edition.approvals
    assert central is not None
    identities = cluster.server.identities
    found = identities.get

    def unread_for_her(identity_id):
        if identity_id == emily.id:
            raise psycopg.OperationalError("the database does not answer")
        return found(identity_id)

    with monkeypatch.context() as patch:
        patch.setattr(identities, "get", unread_for_her)
        with pytest.raises(psycopg.OperationalError):
            central.queue(mark.ref())
    assert central.records.get(pending["id"]).state is None
    [listed] = central.queue(mark.ref())["changes"]
    assert listed["id"] == pending["id"]


def test_a_decision_reaches_the_asking_space_with_its_settings_read_again(company, monkeypatch):
    """The review of Task 6's carry: a decision on a change reaches the asking identity's space through
    ``Spaces.reapplied`` (its settings read again from PostgreSQL first, in its worker: R41)."""
    cluster, emily, mark = company
    spaces = cluster.server.spaces
    reached = []
    reapplied = spaces.reapplied

    async def read_again(identity):
        reached.append(identity.id)
        return await reapplied(identity)

    monkeypatch.setattr(spaces, "reapplied", read_again)
    company_session(cluster, emily, "code")
    pending = _lowering(cluster.a).json()["pending"]
    company_session(cluster, mark, "code")
    approved = cluster.a.post(f"{QUEUE}/changes/{pending['id']}", json={"action": "approve"}, headers=UI)
    assert approved.json() == {"ok": True, "state": "approved"} and reached == [emily.id]


def test_what_stops_a_decision_from_reaching_the_asking_space_is_answered(company, monkeypatch):
    """The review of Task 6's carry: a space that cannot be had on this instance (SpaceUnavailable) and settings that
    cannot be read (OSError) answer 503 in fixed words, nothing decided; an identity disabled meanwhile ends it gone."""
    cluster, emily, mark = company
    spaces, central = cluster.server.spaces, cluster.server.edition.approvals
    assert central is not None
    company_session(cluster, emily, "code")
    pending = _lowering(cluster.a).json()["pending"]
    company_session(cluster, mark, "code")
    decide = f"{QUEUE}/changes/{pending['id']}"

    async def closed(identity):
        raise SpaceUnavailable("closed while it was built")

    async def unread(identity):
        raise OSError("its settings could not be read")

    async def unruled(identity):
        raise FloorUnavailable("a policy this instance cannot read")

    stops = (
        (closed, UNREACHED, "unreached"),
        (unread, UNKEPT, "unkept"),
        (unruled, FLOOR_UNAVAILABLE, "floor-unavailable"),
    )
    for stop, words, code in stops:
        with monkeypatch.context() as patch:  # never ``undo``: it would take the tests' license key away too
            patch.setattr(spaces, "reapplied", stop)
            refused = cluster.a.post(decide, json={"action": "approve"}, headers=UI)
        assert (refused.status_code, refused.json()) == (503, {"error": words, "code": code})
        assert central.records.get(pending["id"]).state is None
    cluster.server.identities.disable(emily.id)
    gone = cluster.a.post(decide, json={"action": "approve"}, headers=UI)
    assert (gone.status_code, gone.json()) == (409, {"error": GONE, "code": "gone"})
    assert central.records.get(pending["id"]).state == "gone"


def test_me_in_an_api_key_s_space_names_its_other_controllers_never_whoever_works_there(company):
    """The review of Task 6's carry: in an API key's space, what the person working there saves never waits for
    them, so /me names the key's other controllers as who decides."""
    cluster, emily, _ = company
    cluster.server.edition.settings_store.set_section("approvals", {"changes": "self"}, actor=BY_HAND)
    for server in cluster.servers:
        server.edition.refresh()
    key, _ = cluster.server.identities.add_key("Billing CI")
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    for person in (emily, daniel):
        cluster.people.add_controller(key.id, person.id, actor=BY_HAND)
    space = f"{API}/ee/keys/{key.id}/space/me"
    company_session(cluster, emily, "code")
    told = cluster.a.get(space).json()["edition"]["approvals"]
    assert (told["changes"], told["deciders"]) == ("controllers", ["Daniel Okafor"])
    company_session(cluster, daniel, "code")
    assert cluster.a.get(space).json()["edition"]["approvals"]["deciders"] == ["Emily Carter"]


async def _held(spaces, identity):
    """A text of the key's agent waiting in its space on this instance, as its plugin's request opens one there."""
    item = {"kind": "user_text", "path": None, "tool": None, "original": PROMPT, "masked": PROMPT, "replacements": [],
            "suggestions": [], "failed": None}  # fmt: skip
    approvals = (await spaces.runtime(identity)).mediator.approvals
    return (await approvals.open(Batch("ci-1", None, "claude-code", [item]))).id


def test_a_stop_in_an_api_key_s_space_is_recorded_with_who_stopped_it(company):
    """The whole-branch review's m1, through the routes: in an API key's space, Daniel undoes the change Emily made
    there and stops the key's text: the record names him, as a rejection, never as whoever asked withdrawing it."""
    cluster, emily, _ = company
    key, _ = cluster.server.identities.add_key("Billing CI")
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    for person in (emily, daniel):
        cluster.people.add_controller(key.id, person.id, actor=BY_HAND)
    space, records = f"{API}/ee/keys/{key.id}/space", cluster.server.edition.approvals.records
    company_session(cluster, emily, "code")
    pending = _lowering(cluster.a, f"{space}/config").json()["pending"]
    assert pending["asked_by"] == emily.id
    company_session(cluster, daniel, "code")
    undone = cluster.a.post(f"{space}/changes/{pending['id']}", json={"action": "reject"}, headers=UI)
    assert undone.json() == {"ok": True, "state": "rejected"}
    row = records.get(pending["id"])
    assert (row.state, row.by_id, row.by_name) == ("rejected", daniel.id, "Daniel Okafor")
    batch_id = cluster.a.portal.call(_held, cluster.server.spaces, cluster.server.identities.get(key.id))
    stopped = cluster.a.post(f"{space}/approvals/{batch_id}", json={"action": "reject"}, headers=UI)
    assert stopped.json() == {"ok": True}
    deadline = time.monotonic() + 15
    while (ended := records.get(batch_id)).state is None:  # its end reaches the record through the work threads
        assert time.monotonic() < deadline, "the text's end never reached the record"
        time.sleep(0.05)
    assert (ended.state, ended.by_id, ended.by_name) == ("rejected", daniel.id, "Daniel Okafor")


def test_deciding_follows_the_license_while_the_record_stays(company):
    """The reviews' carries (Task 2: ``central()`` is the record whatever the license; Task 6: deciding is gated by
    the license, not by ``admitted``): a license that stops listing approvals sends nothing new to anyone else and
    lets nobody decide, while what waits keeps waiting and its end is recorded [D4]."""
    cluster, emily, mark = company
    edition = cluster.server.edition
    company_session(cluster, emily, "code")
    pending = _lowering(cluster.a).json()["pending"]
    license(cluster, features=[feature for feature in PAYLOAD["features"] if feature != "approvals"])
    assert edition.central() is edition.approvals is not None
    assert edition.changes_go_to(emily.ref()) == "self" and edition.live_review_goes_to(emily.ref()) == "self"
    company_session(cluster, mark, "code")
    assert cluster.a.get(QUEUE).status_code == 403
    assert cluster.a.post(f"{QUEUE}/changes/{pending['id']}", json={"action": "approve"}, headers=UI).status_code == 403
    assert "company-approvals" not in cluster.a.get(f"{API}/me").json()["edition"]["pages"]
    assert edition.approvals.records.get(pending["id"]).state is None
    company_session(cluster, emily, "code")
    assert cluster.a.get(f"{API}/me").json()["edition"]["approvals"]["changes"] == "self"
    undone = cluster.a.post(f"{API}/changes/{pending['id']}", json={"action": "reject"}, headers=UI)
    assert undone.json() == {"ok": True, "state": "rejected"}
    assert edition.approvals.records.get(pending["id"]).state == "withdrawn"


def test_an_approval_its_space_does_not_take_is_said_as_such(company, monkeypatch):
    """R41, R60 through the route: an approval whose save the asking space then refuses (another instance wrote her
    settings meanwhile) answers 409 in fixed words, never "nothing was decided", and the record says what it is."""
    cluster, emily, mark = company
    central = cluster.server.edition.approvals
    assert central is not None
    company_session(cluster, emily, "code")
    pending = _lowering(cluster.a).json()["pending"]

    def written_since(self, config):
        raise OSError("written since on another instance")

    company_session(cluster, mark, "code")
    with monkeypatch.context() as patch:
        patch.setattr(PgSettings, "save", written_since)
        refused = cluster.a.post(f"{QUEUE}/changes/{pending['id']}", json={"action": "approve"}, headers=UI)
    assert (refused.status_code, refused.json()) == (409, {"error": UNAPPLIED, "code": "approved-unapplied"})
    assert central.records.get(pending["id"]).state == "approved-unapplied"


def test_an_approval_below_the_company_s_floor_of_now_is_rejected_and_says_why(company, monkeypatch):
    """An approval of a change that the company's floor refuses now (raised since it was asked for) ends it rejected:
    409 with the floor's reasons and a code, and the record says rejected."""
    cluster, emily, mark = company
    central = cluster.server.edition.approvals
    assert central is not None
    company_session(cluster, emily, "code")
    pending = _lowering(cluster.a).json()["pending"]
    reasons = ["values never to mask: set by the company"]
    company_session(cluster, mark, "code")
    with monkeypatch.context() as patch:
        patch.setattr(Runtime, "_refused", lambda self, before, after, **kwargs: reasons)
        refused = cluster.a.post(f"{QUEUE}/changes/{pending['id']}", json={"action": "approve"}, headers=UI)
    assert refused.status_code == 409
    assert (refused.json()["floor"], refused.json()["code"]) == (reasons, "below-floor")
    assert central.records.get(pending["id"]).state == "rejected"


def test_a_decider_s_mask_that_does_not_fit_the_text_is_refused_before_anything_is_recorded(company):
    """A decider's masks are checked against the text before the decision is weighed and its entry written: one that
    does not fit answers 422, nothing is recorded, and the text still waits."""
    cluster, emily, mark = company
    identities = cluster.server.identities
    member = identities.enrol(identities.code(emily.id), "laptop")
    assert member is not None
    token = member[1]
    company_session(cluster, emily, "code")
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    on = cluster.a.put(f"{API}/config", json={"live_review": {"enabled": True}, "etag": etag}, headers=UI)
    assert on.status_code == 200
    items = {"session": "team-1", "items": [{"text": PROMPT, "kind": "user_text"}]}
    plugin = {"x-latet-token": token}
    assert cluster.a.post(f"/u/{route_key(token)}/_latet/engine/v1/mask", json=items, headers=plugin).status_code == 200
    company_session(cluster, mark, "sso")
    [batch] = cluster.a.get(QUEUE).json()["batches"]
    version = cluster.a.get(f"{QUEUE}/batches/{batch['id']}").json()["version"]  # read to decide on it (I1)
    unknown = {"item": 0, "start": PROMPT.index("Falcon"), "end": len(PROMPT), "category": "nonsense"}
    body = {"action": "approve", "masks": [unknown], "version": version}
    refused = cluster.a.post(f"{QUEUE}/batches/{batch['id']}", json=body, headers=UI)
    assert refused.status_code == 422 and "category" in refused.json()["error"]
    assert cluster.server.audit.query(action="approval.decided") == []
    assert [b["id"] for b in cluster.a.get(QUEUE).json()["batches"]] == [batch["id"]]


def test_after_a_trial_s_end_what_lowers_protection_still_waits_and_only_its_author_withdraws_it(company):
    """The review of Task 6's carry, confirmed: after a trial's end the license still lists approvals for what
    protects (``licensed``), so a change that lowers protection keeps its route and waits for the security team, while
    nobody may decide on it (E1's gate of kind serve): only whoever asked withdraws it. Protection never drops."""
    cluster, emily, mark = company
    on_trial(cluster)
    end_trial(cluster)
    company_session(cluster, emily, "code")
    saved = _lowering(cluster.a)
    assert saved.status_code == 409 and saved.json()["pending"]["decides"] == "security"
    pending = saved.json()["pending"]
    company_session(cluster, mark, "code")
    refused = cluster.a.post(f"{QUEUE}/changes/{pending['id']}", json={"action": "approve"}, headers=UI)
    assert refused.status_code == 403 and refused.json()["code"] == "trial-ended"
    company_session(cluster, emily, "code")
    assert cluster.a.get(f"{API}/config").json()["allow_values"] == []
    undone = cluster.a.post(f"{API}/changes/{pending['id']}", json={"action": "reject"}, headers=UI)
    assert undone.json() == {"ok": True, "state": "rejected"}


def test_a_change_whose_record_cannot_be_written_neither_waits_nor_applies(company, monkeypatch):
    """Review Focus 5 on a server (Task 3's review): a record's failure there is PostgreSQL's, never an OSError, and
    the server's work threads refuse the request closed, as for any store that does not answer (503, by the error's
    type alone: ``machines._answered``). Nothing waits and nothing applies."""
    cluster, emily, _ = company

    def unwritten(self, row):
        raise psycopg.OperationalError("the record's database does not answer")

    company_session(cluster, emily, "code")
    with monkeypatch.context() as patch:
        patch.setattr(PostgresRecords, "add", unwritten)
        assert _lowering(cluster.a).status_code == 503
    assert cluster.a.get(f"{API}/changes").json()["changes"] == []
    assert cluster.a.get(f"{API}/config").json()["allow_values"] == []


def test_the_metrics_follow_the_edition_made_last_and_one_closed_counts_nothing(company, monkeypatch):
    """E5's metrics as E4's (I10): once a registry, bound to the record of the edition made last on the server, the
    record's prune among its jobs; an edition that closes lets them go (its ends count nothing), one made again binds
    the same, and one that fails to start leaves nothing of it bound."""
    from latet.edition import server_context
    from latet.ee import edition as edition_module
    from latet.ee.approvals import ApprovalMetrics
    from latet.ee.edition import EnterpriseEdition

    from ..licenses import Clock as Ticker

    cluster, *_ = company
    b = cluster.servers[1]
    metrics, first = ApprovalMetrics.of(b.metrics), b.edition.approvals
    assert metrics._bound is first and first.counted == metrics.ended
    assert "CentralApprovals.prune" in b.edition.events.jobs._names
    b.edition.close()
    assert metrics._bound is None and first.counted != metrics.ended
    b.edition = EnterpriseEdition(server_context(b), listen=False, ticker=Ticker(0.0))  # as E1's tests start one again
    again = b.edition.approvals
    assert ApprovalMetrics.of(b.metrics) is metrics and metrics._bound is again and again.counted == metrics.ended
    assert "# TYPE latet_approvals_company_decided_total counter" in b.metrics.render()
    b.edition.close()

    def failing(server, **options):
        raise ConnectionError("the database at 10.0.0.5 does not answer")

    made: list[EnterpriseEdition] = []
    real_e5 = EnterpriseEdition._e5_on_server

    def kept(self, server):
        made.append(self)
        real_e5(self, server)

    with monkeypatch.context() as patch:  # never ``undo``: it would take the tests' license key away too
        patch.setattr(edition_module, "make_policy", failing)
        patch.setattr(EnterpriseEdition, "_e5_on_server", kept)
        with pytest.raises(ConnectionError):
            EnterpriseEdition(server_context(b), listen=False, ticker=Ticker(0.0))
    [failed] = made
    assert metrics._bound is None and failed.approvals is not None and failed.approvals.counted != metrics.ended
    b.edition = EnterpriseEdition(server_context(b), listen=False, ticker=Ticker(0.0))  # for the cluster to close
