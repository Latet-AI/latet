"""The Company approvals page's API (Latet Enterprise) on a server: the security team sees what waits (a change by what
it lowers, a text when opened), decides, and the asking identity's space carries it out; nobody else sees the queue or
a text; nobody approves their own request; each decision in the administration log; decisions refused after a trial's
end, to a session the company's sign-in did not open, and when their entry cannot be written."""

import base64
import json

import pytest

from latet.ee.access import EE, IN_A_KEYS_SPACE, rule_for
from latet.ee.approvals import Row
from latet.ee.approvals_api import UNLOGGED, UNRECORDED
from latet.ee.events.admin import ADMINISTERING, PERMISSIONS
from latet.ee.roles import Role
from latet.server.tokens import route_key

from ..events.servers import company_session
from ..licenses import PAYLOAD
from ..people.fake_provider import SSO
from ..people.server_support import (
    ADMIN_EE,
    API,
    BY_HAND,
    SERVER_TESTS,
    UI,
    add_person,
    end_trial,
    license,
    on_trial,
    settle,
)

pytestmark = SERVER_TESTS
QUEUE = f"{API}/ee/approvals"
SIGN_IN_SET = "latet server sign-in set --company-changes sso"  # the command that exists (the review of Task 8, M1)
PROMPT = "Send the overdue notice to laura.bennett@whitfield.co.uk about Project Falcon"


@pytest.fixture
def company(cluster):
    """Emily asks; Mark and Daniel decide for the security team; Alice is an auditor. Who decides: the security team."""
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    mark = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com", Role.SECURITY)
    daniel = add_person(cluster, "Daniel Hayes", "daniel.hayes@acme.com", Role.SECURITY)
    alice = add_person(cluster, "Alice Wong", "alice.wong@acme.com", Role.AUDITOR)
    both = {"changes": "security", "live_review": "security"}
    cluster.server.edition.settings_store.set_section("approvals", both, actor=BY_HAND)
    for server in cluster.servers:
        server.edition.refresh()
    return cluster, emily, mark, daniel, alice


def _pruned_once(server) -> bool:
    """Whether the instance's jobs' thread ran E5's prune once (its next turn set), its lock let go before."""
    jobs = server.edition.events.jobs
    index = [run.__qualname__ for _, run in jobs.jobs].index("CentralApprovals.prune")
    return jobs._next[index] > 0.0


def _lowering(client, path=f"{API}/config"):
    """Emily's save that lowers protection, with the etag the page read (on a server every save names it)."""
    etag = client.get(path).json()["etag"]
    return client.put(path, json={"allow_values": ["ACME"], "etag": etag}, headers=UI)


def test_every_route_of_e5_is_under_its_rule_and_mapped_in_the_log():
    for path in ("/approvals", "/approvals/count", "/approvals/history", "/approvals/changes/x",
                 "/approvals/batches/x/images/0"):  # fmt: skip
        rule = rule_for(f"{EE}{path}")
        assert rule is not None and rule.path == "/ee/approvals" and rule.feature == "approvals"
    assert PERMISSIONS["approval.decided"] == "approvals.decide" and "approval.decided" in ADMINISTERING
    # A decider's opening of another person's text (N2): the security role's act, named as its decisions are.
    assert PERMISSIONS["approval.opened"] == "approvals.decide" and "approval.opened" in ADMINISTERING
    # Who decides, set by the administration (`latet server approvals set`): the security role's matter, named.
    assert PERMISSIONS["approvals.changed"] == "approvals.decide" and "approvals.changed" in ADMINISTERING
    assert "key.approval.decided" not in PERMISSIONS  # a controller's own work, kept as reports.people keeps one


def test_the_security_team_approves_a_change_for_the_person_who_asked(company):
    cluster, emily, mark, _, _ = company
    company_session(cluster, emily, "code")
    saved = _lowering(cluster.a)
    assert saved.status_code == 409 and saved.json()["pending"]["deciders"] == ["Daniel Hayes", "Mark Ellis"]
    me = cluster.a.get(f"{API}/me").json()["edition"]
    assert me["approvals"]["changes"] == "security" and "company-approvals" not in me["pages"]
    assert cluster.a.get(QUEUE).status_code == 403  # the queue is the deciders'
    company_session(cluster, mark, "code")
    assert "company-approvals" in cluster.a.get(f"{API}/me").json()["edition"]["pages"]
    queue = cluster.a.get(QUEUE)
    assert queue.status_code == 200 and "ACME" not in queue.text
    [change] = queue.json()["changes"]
    assert change["identity"]["name"] == "Emily Carter" and change["source"] == "dashboard" and not change["mine"]
    approved = cluster.a.post(f"{QUEUE}/changes/{change['id']}", json={"action": "approve"}, headers=UI)
    assert approved.json() == {"ok": True, "state": "approved"}
    assert cluster.a.post(f"{QUEUE}/changes/{change['id']}", json={"action": "approve"}, headers=UI).status_code == 409
    [decided] = cluster.a.get(f"{QUEUE}/history").json()["items"]
    assert decided["by"]["name"] == "Mark Ellis" and decided["state"] == "approved"
    [entry] = cluster.server.audit.query(action="approval.decided")
    assert (entry.actor, entry.target) == (mark.id, emily.id)
    assert entry.details == {"kind": "change", "decision": "approve", "route": "security"}
    company_session(cluster, emily, "code")
    assert cluster.a.get(f"{API}/config").json()["allow_values"] == ["ACME"]


def test_nobody_approves_their_own_request(company):
    cluster, _, mark, daniel, _ = company
    company_session(cluster, mark, "code")
    assert _lowering(cluster.a).status_code == 409
    [change] = cluster.a.get(QUEUE).json()["changes"]
    assert change["mine"]
    refused = cluster.a.post(f"{QUEUE}/changes/{change['id']}", json={"action": "approve"}, headers=UI)
    assert refused.status_code == 403 and "someone else decides" in refused.json()["error"]
    assert refused.json()["code"] == "yours"  # what the dashboard says in each language (the review of Task 7, m5)
    assert cluster.server.audit.query(action="approval.decided") == []  # nothing recorded for a refusal
    company_session(cluster, daniel, "code")
    approved = cluster.a.post(f"{QUEUE}/changes/{change['id']}", json={"action": "approve"}, headers=UI)
    assert approved.status_code == 200


def test_a_text_is_seen_by_its_deciders_alone_from_the_company_sign_in_and_only_when_opened(company):
    cluster, emily, mark, _, alice = company
    member = cluster.server.identities.enrol(cluster.server.identities.code(emily.id), "laptop")
    assert member is not None
    _, token, _ = member
    route = f"/u/{route_key(token)}"
    company_session(cluster, emily, "code")
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    on = cluster.a.put(f"{API}/config", json={"live_review": {"enabled": True}, "etag": etag}, headers=UI)
    assert on.status_code == 200
    items = {"session": "team-1", "items": [{"text": PROMPT, "kind": "user_text"}]}
    first = cluster.a.post(f"{route}/_latet/engine/v1/mask", json=items, headers={"x-latet-token": token}).json()
    assert first["decides"] == "security"
    company_session(cluster, mark, "code")
    queue = cluster.a.get(QUEUE)
    [batch] = queue.json()["batches"]
    assert PROMPT not in queue.text and batch["items"] == 1
    # [D6] (the plan review's H1): a session `latet dashboard`'s code opened (an agent can ask for one) opens no text.
    for path in (f"{QUEUE}/batches/{batch['id']}", f"{QUEUE}/batches/{batch['id']}/images/0"):
        refused = cluster.a.get(path)
        assert refused.status_code == 403 and refused.json()["code"] == "company-sign-in"
    # Nor decides on it (R62, the re-review's RR-m1): deciding on a text is reading it.
    undecided = cluster.a.post(f"{QUEUE}/batches/{batch['id']}", json={"action": "reject"}, headers=UI)
    assert undecided.status_code == 403 and undecided.json()["code"] == "company-sign-in"
    company_session(cluster, mark, "sso")
    opened = cluster.a.get(f"{QUEUE}/batches/{batch['id']}")
    assert opened.status_code == 200 and opened.json()["items"][0]["original"] == PROMPT
    company_session(cluster, alice, "sso")  # an auditor reads reports, never a text
    assert cluster.a.get(f"{QUEUE}/batches/{batch['id']}").status_code == 403
    assert cluster.a.get(f"{QUEUE}/batches/{batch['id']}/images/0").status_code == 403


@pytest.mark.parametrize("who", ["a person with no role", "another API key's controller"])
def test_a_text_and_its_image_are_refused_to_a_signed_in_person_who_does_not_decide_on_it(company, who):
    """The whole-branch review's m5 (Review Focus 7): signed in with the company account, so that the company's sign-in
    is not what refuses them, a person with no role and a person who controls another API key open neither Emily's
    text nor its image: neither decides on it."""
    cluster, emily, _, _, _ = company
    member = cluster.server.identities.enrol(cluster.server.identities.code(emily.id), "laptop")
    assert member is not None
    _, token, _ = member
    company_session(cluster, emily, "code")
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    on = cluster.a.put(f"{API}/config", json={"live_review": {"enabled": True}, "etag": etag}, headers=UI)
    assert on.status_code == 200
    items = {"session": "team-1", "items": [{"text": PROMPT, "kind": "user_text"}]}
    route = f"/u/{route_key(token)}/_latet/engine/v1/mask"
    sent = cluster.a.post(route, json=items, headers={"x-latet-token": token}).json()
    assert sent["decides"] == "security"
    nora = add_person(cluster, "Nora Quinn", "nora.quinn@acme.com")
    if who == "another API key's controller":
        key, _ = cluster.server.identities.add_key("Billing CI")
        cluster.people.add_controller(key.id, nora.id, actor=BY_HAND)
    company_session(cluster, nora, "sso")
    for path in (f"{QUEUE}/batches/{sent['pending']}", f"{QUEUE}/batches/{sent['pending']}/images/0"):
        refused = cluster.a.get(path)
        assert refused.status_code == 403 and refused.json()["code"] == "not-yours" and PROMPT not in refused.text


def test_an_approval_of_a_text_names_the_version_its_decider_read(company):
    """The review of Task 10, I1: a text opened says its version (how it would leave, never a value of it); an approval
    names the version its decider read, and one that names none is refused, nothing recorded; stopping it names none."""
    cluster, emily, mark, _, _ = company
    member = cluster.server.identities.enrol(cluster.server.identities.code(emily.id), "laptop")
    assert member is not None
    _, token, _ = member
    route = f"/u/{route_key(token)}"
    company_session(cluster, emily, "code")
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    on = cluster.a.put(f"{API}/config", json={"live_review": {"enabled": True}, "etag": etag}, headers=UI)
    assert on.status_code == 200
    items = {"session": "team-1", "items": [{"text": PROMPT, "kind": "user_text"}]}
    sent = cluster.a.post(f"{route}/_latet/engine/v1/mask", json=items, headers={"x-latet-token": token}).json()
    company_session(cluster, mark, "sso")
    opened = cluster.a.get(f"{QUEUE}/batches/{sent['pending']}").json()
    version = opened.get("version")
    assert isinstance(version, str) and len(version) >= 16 and "laura" not in version
    unnamed = cluster.a.post(f"{QUEUE}/batches/{sent['pending']}", json={"action": "approve"}, headers=UI)
    assert unnamed.status_code == 400 and "version" in unnamed.json()["error"]
    assert cluster.server.audit.query(action="approval.decided") == []  # refused before anything is recorded
    stopped = cluster.a.post(f"{QUEUE}/batches/{sent['pending']}", json={"action": "reject"}, headers=UI)
    assert stopped.json() == {"ok": True, "state": "rejected"}


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _text_waiting(cluster, person, text: str = PROMPT) -> str:
    """``person``'s plugin sends ``text`` from a computer of theirs, their Live Reviewer on: the batch that waits for the
    security team."""
    member = cluster.server.identities.enrol(cluster.server.identities.code(person.id), "laptop")
    assert member is not None
    _, token, _ = member
    company_session(cluster, person, "code")
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    on = cluster.a.put(f"{API}/config", json={"live_review": {"enabled": True}, "etag": etag}, headers=UI)
    assert on.status_code == 200
    items = {"session": "team-1", "items": [{"text": text, "kind": "user_text"}]}
    route = f"/u/{route_key(token)}/_latet/engine/v1/mask"
    sent = cluster.a.post(route, json=items, headers={"x-latet-token": token}).json()
    assert sent["decides"] == "security", sent
    return sent["pending"]


def test_a_decider_s_opening_of_another_person_s_text_is_in_the_administration_log_once(company, monkeypatch):
    """The final security review's N2 (the owner's decision): a decider who opens another person's waiting text, its
    words or its images, reads its values, and that wrote no entry in the administration log (only a decision did).
    Each decider's first opening of a text is now an entry (`approval.opened`) naming the decider and the person, with
    no text and no image in it: one for each decider and each text, however many of its images they open and however
    often, through any instance. Whoever opens their own text writes none, in their own dashboard or on Company
    approvals."""
    cluster, emily, mark, daniel, _ = company
    batch_id = _text_waiting(cluster, emily)
    assert [b["id"] for b in cluster.a.get(f"{API}/approvals").json()["batches"]] == [batch_id]  # her own reading

    async def pictured(row, item, sent, run):  # the text's image, as her queue gives it
        return base64.b64encode(PNG).decode(), "image/png"

    for server in cluster.servers:
        monkeypatch.setattr(server.edition.approvals, "image", pictured)
    company_session(cluster, mark, "sso")
    for client in (cluster.a, cluster.b, cluster.a):  # opened, read again, and through the other instance
        assert client.get(f"{QUEUE}/batches/{batch_id}").status_code == 200
        for view in ("", "?view=sent"):
            shown = client.get(f"{QUEUE}/batches/{batch_id}/images/0{view}")
            assert shown.status_code == 200 and shown.content == PNG
    [entry] = cluster.server.audit.query(action="approval.opened")
    assert (entry.actor, entry.target) == (mark.id, emily.id)
    assert entry.details == {"kind": "batch", "item": batch_id, "route": "security"}
    company_session(cluster, daniel, "sso")  # another decider, who opens an image first: an entry of his own
    assert cluster.b.get(f"{QUEUE}/batches/{batch_id}/images/0").status_code == 200
    assert cluster.a.get(f"{QUEUE}/batches/{batch_id}").status_code == 200
    opened = cluster.server.audit.query(action="approval.opened")
    assert sorted((e.actor, e.target) for e in opened) == sorted([(mark.id, emily.id), (daniel.id, emily.id)])
    assert PROMPT not in json.dumps([e.details for e in opened])
    own = _text_waiting(cluster, mark, "Send the renewal to laura.bennett@whitfield.co.uk about Project Heron")
    company_session(cluster, mark, "sso")  # his own text, opened on Company approvals: marked his, no entry
    mine = cluster.a.get(f"{QUEUE}/batches/{own}")
    assert mine.status_code == 200 and mine.json()["mine"] is True
    assert cluster.a.get(f"{QUEUE}/batches/{own}/images/0").status_code == 200
    company_session(cluster, emily, "code")
    assert batch_id in [b["id"] for b in cluster.a.get(f"{API}/approvals").json()["batches"]]  # hers again
    assert len(cluster.server.audit.query(action="approval.opened")) == 2


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


async def _waiting_with_an_image(spaces, identity, came: bytes, leaves: bytes) -> str:
    """A text of ``identity``'s with an image, waiting in their space on this instance for the security team: the image
    as it came, and as it would leave when it began to wait."""
    from latet.gateway.approvals import Batch

    from ...unit.state.contracts import _Image

    runtime = await spaces.runtime(identity)
    item = {"kind": "image_text", "path": None, "tool": "Read", "original": PROMPT, "masked": PROMPT,
            "replacements": [], "suggestions": [], "failed": None, "image": {"width": 64, "height": 32}}  # fmt: skip
    batch = Batch("cc:pictured", None, "claude-code", [item])
    batch.images = {0: _Image(_b64(came), _b64(leaves))}
    await runtime.mediator.approvals.open(batch)
    return batch.id


async def _painted_again(spaces, identity, batch_id: str, picture: bytes) -> bool:
    """Its previews made again on the instance that holds it, its image painted again as it would leave now, as its
    space does when the person's rules or vault change while it waits (``Mediator.refresh_live``)."""
    approvals = (await spaces.runtime(identity)).mediator.approvals
    batch = await approvals.backend.fetch(batch_id)
    assert batch is not None
    return await approvals.update(batch_id, batch.items, {0: (_b64(picture), "image/png")})


def test_a_decider_sees_an_image_as_it_leaves_now_once_its_text_is_masked_again(company):
    """The final security review, approvals L4 (the decider's side): the view of an image as it leaves now was the
    picture painted when its text first waited. Once its text is masked again while it waits (here a value let
    through, so that the image would leave with it visible), Mark, on the other instance, sees the image painted again,
    never the old bars, and the image as it came is as it was."""
    cluster, emily, mark, _, _ = company
    first = cluster.servers[0]
    hers = first.identities.get(emily.id)
    came, leaves, now = PNG + b"came", PNG + b"covered", PNG + b"let through"
    batch_id = cluster.a.portal.call(_waiting_with_an_image, first.spaces, hers, came, leaves)
    company_session(cluster, mark, "sso")
    sent = f"{QUEUE}/batches/{batch_id}/images/0?view=sent"
    assert cluster.b.get(sent).content == leaves
    assert cluster.a.portal.call(_painted_again, first.spaces, hers, batch_id, now)
    assert cluster.b.get(sent).content == now and cluster.a.get(sent).content == now
    assert cluster.b.get(f"{QUEUE}/batches/{batch_id}/images/0").content == came


def test_a_text_opens_only_from_the_server_s_own_pages(company, monkeypatch):
    """The re-review of the fix rounds, R2: an opening writes an entry from a GET that no header guards (an image is an
    `<img>`), and the session's cookie, SameSite=Strict, still goes with a request from a sibling host of the same
    domain: Emily's page there could make Mark's browser write that he opened her text. A text and its images open
    only for a request of the server's own pages (`Sec-Fetch-Site: same-origin`), or one that says nothing of where it
    comes from (a client with no fetch metadata); any other is refused with nothing read and nothing written."""
    cluster, emily, mark, _, _ = company
    batch_id = _text_waiting(cluster, emily)

    async def pictured(row, item, sent, run):
        return base64.b64encode(PNG).decode(), "image/png"

    for server in cluster.servers:
        monkeypatch.setattr(server.edition.approvals, "image", pictured)
    company_session(cluster, mark, "sso")
    paths = (f"{QUEUE}/batches/{batch_id}", f"{QUEUE}/batches/{batch_id}/images/0?view=sent")
    for site in ("same-site", "none", "cross-site"):
        for path in paths:
            refused = cluster.a.get(path, headers={"sec-fetch-site": site})
            assert refused.status_code == 403, (site, path, refused.text)
            assert PROMPT not in refused.text and refused.content != PNG
    assert cluster.server.audit.query(action="approval.opened") == []
    for path in paths:
        assert cluster.a.get(path, headers={"sec-fetch-site": "same-origin"}).status_code == 200
    [entry] = cluster.server.audit.query(action="approval.opened")
    assert (entry.actor, entry.target) == (mark.id, emily.id)


def test_a_text_whose_opening_cannot_be_written_in_the_log_is_not_shown(company, monkeypatch):
    """N2's other side: an opening the administration log cannot keep shows nothing of the text (503, in fixed words
    and a code of its own), as a decision whose entry cannot be written is not carried out."""
    cluster, emily, mark, _, _ = company
    batch_id = _text_waiting(cluster, emily)

    def unwritten(*args, **kwargs):
        raise ConnectionError("the log's database does not answer")

    company_session(cluster, mark, "sso")
    with monkeypatch.context() as patch:  # never ``undo``: it would take the tests' license key away too
        patch.setattr(cluster.server.audit, "record", unwritten)
        refused = cluster.a.get(f"{QUEUE}/batches/{batch_id}")
    assert refused.status_code == 503 and refused.json() == {"error": UNLOGGED, "code": "opening-unrecorded"}
    assert PROMPT not in refused.text and cluster.server.audit.query(action="approval.opened") == []
    assert cluster.a.get(f"{QUEUE}/batches/{batch_id}").status_code == 200  # written now: shown


def test_a_text_masked_again_between_the_two_checks_answers_changed_with_its_entry_written(company, monkeypatch):
    """The review of Task 10 (fix round 2, n3): the text passes the check before the record, then is masked again
    before the decision is carried out (another instance's space made its previews anew). It answers 409 `changed`:
    nothing decided and the text still waits, while the administration log's entry, written before the decision is
    carried out (R43, R50), stands as the decision taken."""
    cluster, emily, mark, _, _ = company
    member = cluster.server.identities.enrol(cluster.server.identities.code(emily.id), "laptop")
    assert member is not None
    _, token, _ = member
    route = f"/u/{route_key(token)}"
    company_session(cluster, emily, "code")
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    on = cluster.a.put(f"{API}/config", json={"live_review": {"enabled": True}, "etag": etag}, headers=UI)
    assert on.status_code == 200
    items = {"session": "team-1", "items": [{"text": PROMPT, "kind": "user_text"}]}
    sent = cluster.a.post(f"{route}/_latet/engine/v1/mask", json=items, headers={"x-latet-token": token}).json()
    company_session(cluster, mark, "sso")
    read = cluster.a.get(f"{QUEUE}/batches/{sent['pending']}").json()
    central = cluster.server.edition.approvals
    checked = central.check

    async def masked_again_after(row, masks, images, run, version=None):
        await checked(row, masks, images, run, version=version)
        approvals = (await cluster.server.spaces.runtime(cluster.server.identities.get(emily.id))).mediator.approvals
        batch = await approvals.backend.fetch(row.id)
        assert await approvals.update(row.id, [item | {"masked": item["original"]} for item in batch.items])

    monkeypatch.setattr(central, "check", masked_again_after)
    approval = {"action": "approve", "version": read["version"]}
    refused = cluster.a.post(f"{QUEUE}/batches/{sent['pending']}", json=approval, headers=UI)
    assert refused.status_code == 409 and refused.json()["code"] == "changed", refused.text
    assert len(cluster.server.audit.query(action="approval.decided")) == 1  # the decision taken, as R43 keeps it
    assert [b["id"] for b in cluster.a.get(QUEUE).json()["batches"]] == [sent["pending"]]  # it still waits


def test_decisions_stop_when_a_trial_ends_and_what_waits_stays_readable(company):
    cluster, emily, mark, _, _ = company
    on_trial(cluster)
    company_session(cluster, emily, "code")
    pending = _lowering(cluster.a).json()["pending"]
    end_trial(cluster)
    company_session(cluster, mark, "code")
    assert cluster.a.get(QUEUE).status_code == 200  # read, not changed
    refused = cluster.a.post(f"{QUEUE}/changes/{pending['id']}", json={"action": "approve"}, headers=UI)
    assert refused.status_code == 403 and refused.json()["code"] == "trial-ended"
    company_session(cluster, emily, "code")  # whoever asked still withdraws it, in their own dashboard
    undone = cluster.a.post(f"{API}/changes/{pending['id']}", json={"action": "reject"}, headers=UI)
    assert undone.json() == {"ok": True, "state": "rejected"}


def test_no_text_opens_once_a_trial_ended(company):
    """R61 (the re-review's RR-L2): a text is read to decide on it; once a trial ended nobody decides, and no text or
    image opens, from any session; the queue still lists it."""
    cluster, emily, mark, _, _ = company
    on_trial(cluster)
    member = cluster.server.identities.enrol(cluster.server.identities.code(emily.id), "laptop")
    assert member is not None
    _, token, _ = member
    company_session(cluster, emily, "code")
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    on = cluster.a.put(f"{API}/config", json={"live_review": {"enabled": True}, "etag": etag}, headers=UI)
    assert on.status_code == 200
    items = {"session": "team-1", "items": [{"text": PROMPT, "kind": "user_text"}]}
    plugin = {"x-latet-token": token}
    assert cluster.a.post(f"/u/{route_key(token)}/_latet/engine/v1/mask", json=items, headers=plugin).status_code == 200
    end_trial(cluster)
    company_session(cluster, mark, "sso")
    [batch] = cluster.a.get(QUEUE).json()["batches"]
    for path in (f"{QUEUE}/batches/{batch['id']}", f"{QUEUE}/batches/{batch['id']}/images/0"):
        refused = cluster.a.get(path)
        assert refused.status_code == 403 and refused.json()["code"] == "trial-ended"


def test_a_session_of_a_one_time_code_decides_nothing_while_company_changes_say_sso(company):
    cluster, emily, mark, _, _ = company
    cluster.server.edition.settings_store.set_section("sso", SSO, actor=BY_HAND)  # company_changes: sso, by default
    for server in cluster.servers:
        server.edition.refresh()
    company_session(cluster, emily, "code")
    pending = _lowering(cluster.a).json()["pending"]
    company_session(cluster, mark, "code")
    refused = cluster.a.post(f"{QUEUE}/changes/{pending['id']}", json={"action": "approve"}, headers=UI)
    assert refused.status_code == 403 and refused.json()["code"] == "company-sign-in"
    company_session(cluster, mark, "sso")
    assert cluster.a.post(f"{QUEUE}/changes/{pending['id']}", json={"action": "approve"}, headers=UI).status_code == 200


def test_a_decision_whose_entry_cannot_be_written_is_not_carried_out(company, monkeypatch):
    cluster, emily, mark, _, _ = company
    company_session(cluster, emily, "code")
    pending = _lowering(cluster.a).json()["pending"]

    def unwritten(*args, **kwargs):
        raise ConnectionError("the log's database does not answer")

    company_session(cluster, mark, "code")
    with monkeypatch.context() as patch:  # never ``undo``: it would take the tests' license key away too
        patch.setattr(cluster.server.audit, "record", unwritten)
        refused = cluster.a.post(f"{QUEUE}/changes/{pending['id']}", json={"action": "approve"}, headers=UI)
    assert refused.status_code == 503  # R50: nothing decided, said in fixed words
    assert (refused.json()["error"], refused.json()["code"]) == (UNRECORDED, "unrecorded")
    assert cluster.server.audit.query(action="approval.decided") == []
    assert cluster.server.edition.approvals.records.get(pending["id"]).state is None
    company_session(cluster, emily, "code")
    assert [c["id"] for c in cluster.a.get(f"{API}/changes").json()["changes"]] == [pending["id"]]


def test_a_decision_leaves_the_people_s_copies_alone_and_a_withdrawal_says_so(company):
    """R38 (the plan review's M4): a decision names the asking person and changes no person: People's generation in
    Valkey stays, no key's space stream is woken. R43 (L2): a withdrawal on Company approvals is marked so."""
    cluster, emily, mark, _, _ = company
    from latet.ee.people import GENERATION

    company_session(cluster, mark, "code")
    own = _lowering(cluster.a).json()["pending"]  # Mark's own change, which he withdraws on Company approvals
    company_session(cluster, emily, "code")
    pending = _lowering(cluster.a).json()["pending"]
    people = cluster.people

    def generations():
        """The server's generation and each person's own (E3's release gate 4), as Valkey holds them: a read sets one
        that is absent, so each is read through first."""
        for person in (emily, mark):
            people.roles_of(person.id)
        bus = cluster.server.bus
        return [bus.get(GENERATION), *(bus.get(people.generation_name(person.id)) for person in (emily, mark))]

    before = generations()
    company_session(cluster, mark, "code")
    assert cluster.a.post(f"{QUEUE}/changes/{pending['id']}", json={"action": "approve"}, headers=UI).status_code == 200
    assert cluster.a.post(f"{QUEUE}/changes/{own['id']}", json={"action": "reject"}, headers=UI).status_code == 200
    assert generations() == before
    entries = {entry.target: entry.details for entry in cluster.server.audit.query(action="approval.decided")}
    assert entries[mark.id] == {"kind": "change", "decision": "reject", "route": "security", "withdrawn": True}
    assert entries[emily.id] == {"kind": "change", "decision": "approve", "route": "security"}


def test_an_admin_deciding_a_key_nobody_else_controls_is_named_as_an_admin(company):
    """R47 (the plan review's L7, D5): an admin decides a key's change as an admin, an act E4 names in the SIEM."""
    cluster, emily, _, _, _ = company
    paul = add_person(cluster, "Paul Ferris", "paul.ferris@acme.com", Role.ADMIN)
    cluster.server.edition.settings_store.set_section("approvals", {"changes": "self"}, actor=BY_HAND)
    key, _secret = cluster.server.identities.add_key("Billing CI")
    cluster.people.add_controller(key.id, emily.id, actor=BY_HAND)  # she alone controls it
    company_session(cluster, emily, "code")
    settle(lambda: cluster.a.get(f"{API}/me").json()["edition"]["approvals"]["changes"] == "self")
    pending = _lowering(cluster.a, f"{API}/ee/keys/{key.id}/space/config").json()["pending"]
    assert pending["decides"] == "controllers" and "Paul Ferris" in pending["deciders"]  # [D2]: the admins
    company_session(cluster, paul, "code")
    assert cluster.a.get(f"{QUEUE}/count").json() == {"waiting": 1}
    assert cluster.a.post(f"{QUEUE}/changes/{pending['id']}", json={"action": "approve"}, headers=UI).status_code == 200
    [entry] = cluster.server.audit.query(action="key.approval.admin-decided")
    assert (entry.actor, entry.target, entry.details["as"]) == (paul.id, key.id, "admin")
    assert PERMISSIONS["key.approval.admin-decided"] == "keys.manage" and "key.approval.admin-decided" in ADMINISTERING


def test_no_route_of_e5_is_reached_from_an_api_key_s_space(company):
    cluster, emily, _, _, _ = company
    key, _secret = cluster.server.identities.add_key("Billing CI")
    cluster.people.add_controller(key.id, emily.id, actor=BY_HAND)
    company_session(cluster, emily, "code")
    refused = cluster.a.get(f"{API}/ee/keys/{key.id}/space/ee/approvals")  # the key's app's gate: acting is set
    assert refused.status_code == 403 and refused.json()["error"] == IN_A_KEYS_SPACE


def test_who_decides_is_set_by_the_administration_and_kept_in_its_log(company):
    cluster, _, mark, _, _ = company
    headers = {"x-latet-admin": cluster.admin_token}
    # The fixture's own change, by hand (E1's set_section names it `approvals.changed` too): only what follows counts.
    by_hand = {entry.id for entry in cluster.server.audit.query(action="approvals.changed")}

    def entries():
        return [entry for entry in cluster.server.audit.query(action="approvals.changed") if entry.id not in by_hand]

    # [D6] (the plan review's H1): texts go to the security team only where the company's sign-in opens them.
    unopened = cluster.a.put(f"{ADMIN_EE}/approvals/settings", json={"live_review": "security"}, headers=headers)
    assert unopened.status_code == 409 and SIGN_IN_SET in unopened.json()["error"]
    assert entries() == []
    cluster.server.edition.settings_store.set_section("sso", SSO, actor=BY_HAND)  # company_changes: sso, by default
    on_b = cluster.servers[1].edition
    settle(lambda: on_b.settings().sso is not None)  # B heard the sign-in's change: its copy holds who decides now
    assert on_b.settings().approvals.changes == "security"
    changed = cluster.a.put(
        f"{ADMIN_EE}/approvals/settings", json={"changes": "self", "live_review": "security"}, headers=headers
    )
    assert changed.json() == {"approvals": {"changes": "self", "live_review": "security"}, "problems": []}
    [entry] = entries()
    assert entry.actor.startswith("admin:") and entry.details == {
        "fields": ["changes"],
        "changes": "self",
        "live_review": "security",
        "status": 200,
    }  # fmt: skip  (AdminGate.entry adds the status, as E4's entries have it: R37)
    assert cluster.a.put(f"{ADMIN_EE}/approvals/settings", json={"timeout": 60}, headers=headers).status_code == 400
    settle(
        lambda: (
            cluster.b.get(f"{ADMIN_EE}/approvals/settings", headers=headers).json()["approvals"]["changes"] == "self"
        )
    )
    company_session(cluster, mark, "sso")  # [D3]: the security team's own dashboard has no such route either
    assert cluster.a.put(f"{API}/ee/approvals/settings", json={"changes": "self"}, headers=UI).status_code in (404, 405)
    assert cluster.a.get(f"{API}/ee/approvals/settings").status_code in (404, 405)


def test_who_decides_set_while_instances_of_another_release_are_alive_says_so(company):
    """The whole-branch security review's L5, as `latet server policy set` says it: an instance of a release before
    central approvals, left running across the migration, routes nothing to whoever decides and lets a person approve
    what waits for the security team there; one without the Enterprise Edition routes nothing either. Who decides is
    set all the same, and the answer says how many such instances are alive."""
    from latet import __version__

    cluster = company[0]
    headers = {"x-latet-admin": cluster.admin_token}
    bus = cluster.server.bus
    # Instances by their ids, as each draws one (12 hex digits): 0.11 lists no other key as an instance.
    alive = {"instance:0a0b0c0d0e01": b"0.9.9 enterprise", "instance:0a0b0c0d0e02": f"{__version__} community".encode()}
    for name, said in alive.items():
        bus.client.set(bus.key(name), said, px=60_000)
    try:
        changed = cluster.a.put(f"{ADMIN_EE}/approvals/settings", json={"changes": "self"}, headers=headers)
    finally:
        bus.client.delete(*(bus.key(name) for name in alive))
    assert changed.status_code == 200 and changed.json()["approvals"]["changes"] == "self"
    [problem] = changed.json()["problems"]
    assert problem.startswith("2 instances do not run this release of Latet with the Enterprise Edition")
    again = cluster.a.put(f"{ADMIN_EE}/approvals/settings", json={"changes": "security"}, headers=headers)
    assert again.json()["problems"] == []


def _waiting_on_the_settings(cluster) -> bool:
    """Whether a transaction waits for a lock of this test's ee_settings (its schema's table alone)."""
    with cluster.server.db.connection() as conn:
        found = conn.execute(
            "SELECT count(*) FROM pg_locks WHERE relation = to_regclass('ee_settings') AND NOT granted"
        ).fetchone()
    return found is not None and found[0] > 0


def _behind_a_held_change(cluster, change, body):
    """``body`` sent to who decides while another change of the Enterprise settings holds their lock, as each one
    takes it (``change(conn)`` made in that transaction, the sign-in's own or another administrator's), committed once
    the request waits for it: the request's answer."""
    from concurrent.futures import ThreadPoolExecutor

    headers = {"x-latet-admin": cluster.admin_token}
    with ThreadPoolExecutor(1) as pool:
        with cluster.server.db.transaction() as holder:
            holder.execute("LOCK TABLE ee_settings IN SHARE ROW EXCLUSIVE MODE")
            change(holder)
            answer = pool.submit(cluster.a.put, f"{ADMIN_EE}/approvals/settings", json=body, headers=headers)
            settle(lambda: _waiting_on_the_settings(cluster), seconds=10.0)
        return answer.result(timeout=30)


def test_who_decides_waits_for_any_other_change_of_the_settings_and_is_weighed_after_it(company):
    """The review of Task 8, I1: a change of who decides takes the settings' lock first, as every change of the
    Enterprise settings does, and reads them after it: another administrator's change made meanwhile is never lost (nor
    misreported in the log), and a sign-in set meanwhile to let any session change what applies to everyone cannot
    race past [D6]'s refusal."""
    from latet.ee.server import settings_in

    cluster, *_ = company
    store = cluster.server.edition.settings_store
    store.set_section("sso", SSO, actor=BY_HAND)  # company_changes: sso: texts may go to the security team

    def now():
        with cluster.server.db.connection() as conn:
            return settings_in(conn)

    store.set_section("approvals", {"changes": "self", "live_review": "self"}, actor=BY_HAND)
    other = {"changes": "security", "live_review": "self"}  # another administrator's, under the lock
    changed = _behind_a_held_change(
        cluster,
        lambda conn: store.set_section("approvals", other, actor=BY_HAND, conn=conn),
        {"live_review": "security"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["approvals"] == {"changes": "security", "live_review": "security"}  # nothing lost
    [entry] = [e for e in cluster.server.audit.query(action="approvals.changed") if "fields" in e.details]
    assert entry.details == {"fields": ["live_review"], "changes": "security", "live_review": "security", "status": 200}
    assert now().approvals.changes == "security"

    store.set_section("approvals", {"changes": "security", "live_review": "self"}, actor=BY_HAND)
    loosened = {**SSO, "company_changes": "any-session"}  # the sign-in's change, under the lock
    refused = _behind_a_held_change(
        cluster, lambda conn: store.set_section("sso", loosened, actor=BY_HAND, conn=conn), {"live_review": "security"}
    )
    assert refused.status_code == 409 and SIGN_IN_SET in refused.json()["error"]
    assert now().approvals.live_review == "self" and now().sso.company_changes == "any-session"


def test_who_decides_is_refused_what_is_not_its_and_read_not_changed_once_a_trial_ended(company):
    """E1's ``manage`` kind: a read in every standing of a license that lists approvals, a change while the license can
    manage; 400 for anything but the two keys and their values, nothing changed."""
    cluster, *_ = company
    headers = {"x-latet-admin": cluster.admin_token}
    path = f"{ADMIN_EE}/approvals/settings"
    for wrong in ({}, {"changes": "boss"}, {"live_review": "controllers"}, {"changes": ["security"]}):
        assert cluster.a.put(path, json=wrong, headers=headers).status_code == 400, wrong
    assert cluster.a.get(path, headers=headers).json() == {
        "approvals": {"changes": "security", "live_review": "security"},
        "api_keys": False,  # L1' (the final security review's re-review): whether the server has an API key
    }
    on_trial(cluster)
    end_trial(cluster)
    refused = cluster.a.put(path, json={"changes": "self"}, headers=headers)
    assert refused.status_code == 403 and refused.json()["code"] == "trial-ended"
    assert cluster.a.get(path, headers=headers).json()["approvals"]["changes"] == "security"


def test_the_checks_warn_while_texts_go_to_the_security_team_and_no_sign_in_opens_them(company):
    """[D6]: the fixture sets `live_review: security` by hand, with no single sign-on: nobody can open a text."""
    cluster, *_ = company
    edition = cluster.server.edition

    def lines():
        return [check for check in edition.checks() if check.title == "Company approvals"]

    [line] = lines()
    assert line.status == "warn" and SIGN_IN_SET in line.detail
    assert "is not set up" in line.detail  # nobody can open a text: no session the company's sign-in opened exists
    edition.settings_store.set_section("sso", SSO, actor=BY_HAND)
    assert lines() == []


def test_a_sign_in_that_lets_any_session_change_what_applies_to_everyone_opens_no_text_for_the_team(company):
    """[D6]: single sign-on set up with ``company_changes: any-session`` (a session `latet dashboard`'s code opens may
    change what applies to everyone) is not the company's sign-in asked for it: texts are not sent to the security
    team, and the checks warn while they go there."""
    cluster, *_ = company
    edition = cluster.server.edition
    edition.settings_store.set_section("sso", {**SSO, "company_changes": "any-session"}, actor=BY_HAND)
    [line] = [check for check in edition.checks() if check.title == "Company approvals"]
    # M2 (the review of Task 8): what happens there, said as such: a security person signed in with it opens a text.
    assert SIGN_IN_SET in line.detail and "lets any session" in line.detail and "is not set up" not in line.detail
    assert "signed in with it" in line.detail
    refused = cluster.a.put(
        f"{ADMIN_EE}/approvals/settings",
        json={"live_review": "security"},
        headers={"x-latet-admin": cluster.admin_token},
    )
    assert refused.status_code == 409 and SIGN_IN_SET in refused.json()["error"]


def test_the_checks_say_nothing_of_texts_while_the_license_does_not_route_them(company):
    """Without a license that lists approvals, texts go to whoever sends them (``live_review_goes_to``): no text waits
    for the security team, so the line of [D6] would tell of nothing."""
    cluster, *_ = company

    def lines():
        return [check for check in cluster.server.edition.checks() if check.title == "Company approvals"]

    assert len(lines()) == 1
    license(cluster, features=[feature for feature in PAYLOAD["features"] if feature != "approvals"])
    assert lines() == []


def test_a_license_without_approvals_on_the_license_page_asks_first_while_the_security_team_decides(company):
    """The final security review's L1: the License page's change that sends what the security team decides on back to
    whoever asks answers 409 ``approvals: "stop"``, nothing changed, as one that ends the policy does; the same
    confirmation makes it, and its entry says central approvals stop. Nothing is asked once nothing goes there."""
    cluster, *_ = company
    admin_person = add_person(cluster, "Ada Lin", "ada.lin@acme.com", Role.ADMIN)
    company_session(cluster, admin_person, "sso")
    features = [feature for feature in PAYLOAD["features"] if feature != "approvals"]
    without = cluster.signer.make(id="L-2027-0002", features=features).decode()
    answer = cluster.a.put(f"{API}/ee/license", json={"license": without}, headers=UI)
    assert answer.status_code == 409 and answer.json().get("approvals") == "stop", answer.text
    assert "policy" not in answer.json() and "terms" not in answer.json()
    assert "central approvals stop" in answer.json()["error"] and '"policy_stops": true' in answer.json()["error"]
    assert cluster.server.edition.standing().license.id != "L-2027-0002"  # nothing changed
    done = cluster.a.put(f"{API}/ee/license", json={"license": without, "policy_stops": True}, headers=UI)
    assert done.status_code == 200, done.text
    [entry] = cluster.server.audit.query(action="license.installed", actor=admin_person.id)
    assert entry.details["approvals"] == "stop"
    full = cluster.signer.make(id="L-2027-0003").decode()
    assert cluster.a.put(f"{API}/ee/license", json={"license": full}, headers=UI).status_code == 200
    cluster.server.edition.settings_store.set_section("approvals", {}, actor=BY_HAND)  # whoever asks decides
    assert cluster.a.put(f"{API}/ee/license", json={"license": without}, headers=UI).status_code == 200


def test_a_license_that_keeps_roles_and_drops_approvals_on_the_license_page_asks_of_an_api_keys_controllers(company):
    """L1' (the final security review's re-review): with `changes: self` and an API key on the server, a license that
    keeps `roles` and drops approvals lets whoever works in the key's space approve their own change: the License page
    answers 409 `approvals: "stop"`, nothing changed, and the same confirmation makes it."""
    cluster, *_ = company
    cluster.server.edition.settings_store.set_section("approvals", {}, actor=BY_HAND)  # whoever's settings they are
    admin_person = add_person(cluster, "Ada Lin", "ada.lin@acme.com", Role.ADMIN)
    company_session(cluster, admin_person, "sso")
    without = cluster.signer.make(id="L-2027-0002", features=[f for f in PAYLOAD["features"] if f != "approvals"])
    cluster.server.identities.add_key("Billing CI")
    answer = cluster.a.put(f"{API}/ee/license", json={"license": without.decode()}, headers=UI)
    assert answer.status_code == 409 and answer.json().get("approvals") == "stop", answer.text
    assert cluster.server.edition.standing().license.id != "L-2027-0002"  # nothing changed
    done = cluster.a.put(f"{API}/ee/license", json={"license": without.decode(), "policy_stops": True}, headers=UI)
    assert done.status_code == 200, done.text
    [entry] = cluster.server.audit.query(action="license.installed", actor=admin_person.id)
    assert entry.details["approvals"] == "stop"


def test_the_record_is_pruned_by_a_job_of_its_own(company):
    """R73: what ended past the retention goes in E4's jobs' thread, whether or not anything scrapes the metrics or
    opens the history."""
    cluster, emily, mark, _, _ = company
    central = cluster.server.edition.approvals
    # Each instance's own prune at its start (E5's job, last in E4's thread) ran and let its lock go: none holds it
    # while this one is asked (the review of Task 8, M4).
    settle(lambda: all(_pruned_once(server) for server in cluster.servers), seconds=30.0)
    [prune] = [
        run for _, run in cluster.server.edition.events.jobs.jobs if run.__qualname__ == "CentralApprovals.prune"
    ]
    old = cluster.server.clock() - (central.keep_days + 1) * 86400.0
    central.records.add(Row("c_old", emily.id, "Emily Carter", "person", "change", "security", {}, old))
    central.records.end(emily.id, "c_old", "approved", old, mark)
    assert prune() is False and central.records.get("c_old") is None
