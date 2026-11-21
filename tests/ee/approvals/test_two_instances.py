"""Central approvals on two instances of one server (Latet Enterprise), on real PostgreSQL and Valkey: what Emily asks
through one instance is decided through the other, and the first hears it: her dashboard's feed, told who decided, her
plugin's long poll, her settings; and what she changes in an API key's space waits for the key's other controller."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from latet.config import Config, Destination
from latet.ee.roles import Role
from latet.gateway.mediate import Mediator
from latet.server.tokens import route_key

from ...e2e.fake_upstream import FakeUpstream
from ..events.servers import company_session
from ..people.server_support import API, BY_HAND, SERVER_TESTS, UI, add_person

pytestmark = SERVER_TESTS
QUEUE = f"{API}/ee/approvals"
PROMPT = "Send the overdue notice to laura.bennett@whitfield.co.uk about Project Falcon"


def _eventually(check, seconds: float = 15.0) -> None:
    """What another instance hears through Valkey arrives soon, not at once (E1 reads its settings again within ten
    seconds even without the message): asked until it holds."""
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "the other instance never heard it"
        time.sleep(0.05)


@pytest.fixture
def company(cluster):
    """Two instances of one licensed server (E3's cluster: `cluster.a` serves the first, `cluster.b` the second, one
    cookie jar switched between people). Emily (with her laptop's token) and Mark, in the Security group, which holds
    the security role; the security team decides on changes and texts."""
    first = cluster.servers[0]
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    member = first.identities.enrol(first.identities.code(emily.id), "laptop")
    assert member is not None
    mark = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com")
    team = cluster.people.add_group("Security", actor=BY_HAND)
    cluster.people.add_member(team.id, mark.id, actor=BY_HAND)
    cluster.people.grant(team.id, Role.SECURITY, actor=BY_HAND)  # a group's role is its members'
    both = {"changes": "security", "live_review": "security"}
    first.edition.settings_store.set_section("approvals", both, actor=BY_HAND)

    # His session opened once (the review of Task 7, I1): one each try would pass the sessions a person keeps
    # (SESSIONS_KEPT), all ending at once under the tests' clock, and the newest could be the one let go.
    company_session(cluster, mark, "code")

    def heard() -> bool:  # the second instance heard of the role and of who decides
        answer = cluster.b.get(f"{API}/me")
        if answer.status_code != 200:  # an answer that says nothing yet is asked again, never raised
            return False
        me = answer.json()["edition"]
        return "company-approvals" in me["pages"] and me.get("approvals", {}).get("changes") == "security"

    _eventually(heard)
    return cluster, emily, mark, member[1]


async def _watch(spaces, identity):
    """Her dashboard's feed of her queue on an instance, as `/approvals/stream` opens it there (on its event loop)."""
    return await (await spaces.runtime(identity)).mediator.approvals.watch()


async def _drain(feed):
    return [feed.get_nowait() for _ in range(feed.qsize())]


async def _masked_again(spaces, identity, batch_id):
    """Her text's previews made anew on the instance that holds it, as its space does when her rules or her vault
    change while it waits (``Mediator.refresh_live`` → ``Approvals.update``): here masking nothing any more (a change
    that lowered protection was approved meanwhile)."""
    approvals = (await spaces.runtime(identity)).mediator.approvals
    batch = await approvals.backend.fetch(batch_id)
    assert batch is not None
    items = [item | {"masked": item["original"], "replacements": []} for item in batch.items]
    return await approvals.update(batch_id, items)


async def _own_part(spaces, identity):
    """Her own part as PostgreSQL keeps it now (``PgSettings._own`` reads the row again, past any copy)."""
    runtime = await spaces.runtime(identity)
    return await asyncio.to_thread(runtime.settings._own)


def test_a_change_asked_through_one_instance_is_decided_through_the_other(company):
    cluster, emily, mark, _ = company
    first = cluster.servers[0]
    company_session(cluster, emily, "code")
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    saved = cluster.a.put(f"{API}/config", json={"allow_values": ["ACME"], "etag": etag}, headers=UI)
    assert saved.status_code == 409 and saved.json()["pending"]["deciders"] == ["Mark Ellis"]
    change_id = saved.json()["pending"]["id"]
    feed = cluster.a.portal.call(_watch, first.spaces, first.identities.get(emily.id))
    company_session(cluster, mark, "code")
    [change] = cluster.b.get(QUEUE).json()["changes"]
    assert change["id"] == change_id and change["identity"]["name"] == "Emily Carter" and "ACME" not in str(change)
    decided = cluster.b.post(f"{QUEUE}/changes/{change_id}", json={"action": "approve"}, headers=UI)
    assert decided.json() == {"ok": True, "state": "approved"}
    heard: list = []

    def told() -> bool:  # what her dashboard's feed on the first instance has heard
        heard.extend(cluster.a.portal.call(_drain, feed))
        return ("change_settled", {"id": change_id, "state": "approved", "by": "Mark Ellis"}) in heard

    _eventually(told)
    company_session(cluster, emily, "code")
    _eventually(lambda: cluster.a.get(f"{API}/changes").json()["changes"] == [])
    _eventually(lambda: cluster.a.get(f"{API}/config").json()["allow_values"] == ["ACME"])
    company_session(cluster, mark, "code")
    [history] = cluster.b.get(f"{QUEUE}/history").json()["items"]
    assert history["by"]["name"] == "Mark Ellis" and history["identity"]["id"] == emily.id


def test_a_text_held_by_one_instance_is_released_by_a_decision_through_the_other(company):
    cluster, emily, mark, token = company
    route, plugin = f"/u/{route_key(token)}", {"x-latet-token": token}
    company_session(cluster, emily, "code")
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    on = cluster.a.put(f"{API}/config", json={"live_review": {"enabled": True}, "etag": etag}, headers=UI)
    assert on.status_code == 200  # it adds protection: applied at once
    items = {"session": "team-1", "items": [{"text": PROMPT, "kind": "user_text"}]}
    first = cluster.a.post(f"{route}/_latet/engine/v1/mask", json=items, headers=plugin).json()
    assert first["decides"] == "security" and first["deciders"] == ["Mark Ellis"]
    company_session(cluster, mark, "sso")  # [D6]: a text is opened from the company sign-in's session alone
    [batch] = cluster.b.get(QUEUE).json()["batches"]
    opened = cluster.b.get(f"{QUEUE}/batches/{batch['id']}").json()
    assert opened["items"][0]["original"] == PROMPT
    start = PROMPT.index("Falcon")
    mask = {"item": 0, "start": start, "end": start + len("Falcon"), "category": "confidential"}
    approval = {"action": "approve", "masks": [mask], "version": opened["version"]}  # the text as he read it
    with ThreadPoolExecutor(1) as pool:
        wait = {"id": first["pending"]}
        polled = pool.submit(cluster.a.post, f"{route}/_latet/engine/v1/approvals/wait", json=wait, headers=plugin)
        decided = cluster.b.post(f"{QUEUE}/batches/{batch['id']}", json=approval, headers=UI)
        assert decided.json() == {"ok": True, "state": "approved"}
        [item] = polled.result(timeout=25).json()["items"]
    assert "Falcon" not in item["text"] and "laura.bennett@whitfield.co.uk" not in item["text"]


def test_a_change_the_security_team_decides_never_becomes_the_person_s_own_by_a_computer_s_call(company, monkeypatch):
    """R31 (the plan review's C1), able to fail (R59, the re-review's RR-I2): Emily's save waits for the security team;
    her computer then registers a destination new to her settings (its device token, `POST
    /u/<route>/_latet/admin/destinations`), which reaches `add_destinations`' branch for what waits; her space is built
    on the second instance. What waits still waits for the security team, PostgreSQL's own part never holds it, and
    she cannot approve it."""
    cluster, emily, _, token = company
    first = cluster.servers[0]
    route, device = f"/u/{route_key(token)}", {"x-latet-token": token}
    new = "https://gateway.acme.example/v1"  # in no settings of hers or the server's: a destination she adds
    # The route judges a new address with the resolver (`egress.bounded`), which a test has not: taken as her own.
    monkeypatch.setattr("latet.api.rest.personal_destination_problem", lambda *args, **kwargs: None)
    company_session(cluster, emily, "code")
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    body = {"terms": [{"value": "Orion", "type": "PROJECT"}], "allow_values": ["ACME"], "etag": etag}
    assert cluster.a.put(f"{API}/config", json=body, headers=UI).status_code == 409
    destination = {"destinations": [{"url": new, "agent": "claude"}]}
    named = cluster.a.post(f"{route}/_latet/admin/destinations", json=destination, headers=device)
    assert named.status_code == 200 and named.json()["names"]
    own = cluster.a.portal.call(_own_part, first.spaces, first.identities.get(emily.id))
    assert new in str(own) and "ACME" not in str(own) and "Orion" not in str(own)  # saved: hers, and nothing waiting
    for client in (cluster.b, cluster.a):  # her space built on the second instance too, and the first read again
        [waiting] = client.get(f"{API}/changes").json()["changes"]
        assert waiting["decides"] == "security" and waiting["source"] == "dashboard"
        assert client.get(f"{API}/config").json()["allow_values"] == []
        refused = client.post(f"{API}/changes/{waiting['id']}", json={"action": "approve"}, headers=UI)
        assert refused.status_code == 403


def test_a_change_made_in_an_api_key_s_space_waits_for_its_other_controller(company):
    """[D2] With each identity deciding, a key's change waits for another of its controllers."""
    cluster, emily, _, _ = company
    first = cluster.servers[0]
    first.edition.settings_store.set_section("approvals", {"changes": "self"}, actor=BY_HAND)
    key, _secret = first.identities.add_key("Billing CI")
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    for person in (emily, daniel):
        cluster.people.add_controller(key.id, person.id, actor=BY_HAND)
    space = f"{API}/ee/keys/{key.id}/space"  # E3: Emily's dashboard working in the key's space, on the first instance
    company_session(cluster, emily, "code")
    _eventually(lambda: cluster.a.get(f"{API}/me").json()["edition"]["approvals"]["changes"] == "self")
    etag = cluster.a.get(f"{space}/config").json()["etag"]
    saved = cluster.a.put(f"{space}/config", json={"allow_values": ["ACME"], "etag": etag}, headers=UI)
    assert saved.status_code == 409, saved.text
    pending = saved.json()["pending"]
    assert pending["decides"] == "controllers" and pending["deciders"] == ["Daniel Okafor"]
    assert pending["asked_by"] == emily.id
    assert cluster.a.post(f"{QUEUE}/changes/{pending['id']}", json={"action": "approve"}, headers=UI).status_code == 403
    company_session(cluster, daniel, "code")
    [waiting] = cluster.b.get(QUEUE).json()["changes"]
    assert waiting["identity"]["name"] == "Billing CI" and waiting["asked_by"]["name"] == "Emily Carter"
    approved = cluster.b.post(f"{QUEUE}/changes/{pending['id']}", json={"action": "approve"}, headers=UI)
    assert approved.json() == {"ok": True, "state": "approved"}
    [entry] = first.audit.query(action="key.approval.decided")
    assert (entry.actor, entry.target) == (daniel.id, key.id)
    company_session(cluster, emily, "code")
    _eventually(lambda: cluster.a.get(f"{space}/config").json()["allow_values"] == ["ACME"])


def test_her_dashboard_on_one_instance_hears_who_decided_on_her_text_through_the_other(company):
    """Task 1's review, C6 (Review Focus 3 for a text): her dashboard's feed on the first instance hears the text
    settled, with who decided it on the second."""
    cluster, emily, mark, token = company
    first = cluster.servers[0]
    route, plugin = f"/u/{route_key(token)}", {"x-latet-token": token}
    company_session(cluster, emily, "code")
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    on = cluster.a.put(f"{API}/config", json={"live_review": {"enabled": True}, "etag": etag}, headers=UI)
    assert on.status_code == 200
    items = {"session": "team-1", "items": [{"text": PROMPT, "kind": "user_text"}]}
    sent = cluster.a.post(f"{route}/_latet/engine/v1/mask", json=items, headers=plugin).json()
    feed = cluster.a.portal.call(_watch, first.spaces, first.identities.get(emily.id))
    company_session(cluster, mark, "sso")
    [batch] = cluster.b.get(QUEUE).json()["batches"]
    assert batch["id"] == sent["pending"]
    version = cluster.b.get(f"{QUEUE}/batches/{batch['id']}").json()["version"]  # read to decide on it
    approval = {"action": "approve", "version": version}
    decided = cluster.b.post(f"{QUEUE}/batches/{batch['id']}", json=approval, headers=UI)
    assert decided.json() == {"ok": True, "state": "approved"}
    heard: list = []

    def told() -> bool:
        heard.extend(cluster.a.portal.call(_drain, feed))
        return ("settled", {"id": batch["id"], "state": "approved", "by": "Mark Ellis"}) in heard

    _eventually(told)


def test_a_text_masked_again_after_the_decider_read_it_is_not_approved_as_he_read_it(company):
    """The review of Task 10, I1: Emily's text waits on the first instance; Mark reads it through the second; her space
    then masks it again on the first (here masking nothing any more). His approval of what he read decides nothing:
    409 `changed`, no entry in the administration log, and the text still waits, unsent. Read again, it shows as it
    would leave now, and that reading he approves."""
    cluster, emily, mark, token = company
    first = cluster.servers[0]
    route, plugin = f"/u/{route_key(token)}", {"x-latet-token": token}
    company_session(cluster, emily, "code")
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    on = cluster.a.put(f"{API}/config", json={"live_review": {"enabled": True}, "etag": etag}, headers=UI)
    assert on.status_code == 200
    items = {"session": "team-1", "items": [{"text": PROMPT, "kind": "user_text"}]}
    sent = cluster.a.post(f"{route}/_latet/engine/v1/mask", json=items, headers=plugin).json()
    company_session(cluster, mark, "sso")
    read = cluster.b.get(f"{QUEUE}/batches/{sent['pending']}").json()
    assert "laura.bennett@whitfield.co.uk" not in read["items"][0]["masked"]  # what he saw: the address masked
    assert cluster.a.portal.call(_masked_again, first.spaces, first.identities.get(emily.id), sent["pending"])
    as_read = {"action": "approve", "version": read.get("version")}
    refused = cluster.b.post(f"{QUEUE}/batches/{sent['pending']}", json=as_read, headers=UI)
    assert refused.status_code == 409 and refused.json()["code"] == "changed", refused.text
    assert first.audit.query(action="approval.decided") == []  # nothing decided, nothing recorded
    assert [b["id"] for b in cluster.b.get(QUEUE).json()["batches"]] == [sent["pending"]]  # it still waits, unsent
    now = cluster.b.get(f"{QUEUE}/batches/{sent['pending']}").json()
    assert now["version"] != read["version"] and "laura.bennett@whitfield.co.uk" in now["items"][0]["masked"]
    as_now = {"action": "approve", "version": now["version"]}
    assert cluster.b.post(f"{QUEUE}/batches/{sent['pending']}", json=as_now, headers=UI).json() == {
        "ok": True, "state": "approved"
    }  # fmt: skip


def test_a_text_masked_otherwise_when_it_is_sent_than_the_decider_approved_it_is_withheld_and_waits_again(
    company, monkeypatch
):
    """The review of Task 10 (fix round 2), on two instances: Emily's text waits on the first; Mark reads it through the
    second. A change that lowers protection (her address let through, approved by the security team) then applies to
    her space before its preview is made again (here never: the previews are not made again in this test). Mark
    approves the text as he read it: both checks of the version pass, and the first instance, sending it, finds it would
    leave masked otherwise than he approved. It is withheld (nothing of it in clear reaches her plugin), the record
    says so (`approved-changed`), and her agent's next call with it waits for the security team again."""
    cluster, emily, mark, token = company
    route, plugin = f"/u/{route_key(token)}", {"x-latet-token": token}
    address = "laura.bennett@whitfield.co.uk"
    monkeypatch.setattr(Mediator, "_refresh_soon", lambda self: None)  # no preview made again, on either instance
    company_session(cluster, emily, "code")
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    on = cluster.a.put(f"{API}/config", json={"live_review": {"enabled": True}, "etag": etag}, headers=UI)
    assert on.status_code == 200
    items = {"session": "team-1", "items": [{"text": PROMPT, "kind": "user_text"}]}
    sent = cluster.a.post(f"{route}/_latet/engine/v1/mask", json=items, headers=plugin).json()
    company_session(cluster, mark, "sso")
    read = cluster.b.get(f"{QUEUE}/batches/{sent['pending']}").json()
    assert address not in read["items"][0]["masked"]
    company_session(cluster, emily, "code")  # her address let through: it lowers protection, the team approves it
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    lowered = cluster.a.put(f"{API}/config", json={"allow_values": [address], "etag": etag}, headers=UI)
    assert lowered.status_code == 409
    company_session(cluster, mark, "sso")
    change = lowered.json()["pending"]["id"]
    assert cluster.b.post(f"{QUEUE}/changes/{change}", json={"action": "approve"}, headers=UI).status_code == 200
    company_session(cluster, emily, "code")
    _eventually(lambda: cluster.a.get(f"{API}/config").json()["allow_values"] == [address])  # in force where it waits
    company_session(cluster, mark, "sso")
    with ThreadPoolExecutor(1) as pool:
        wait = {"id": sent["pending"]}
        polled = pool.submit(cluster.a.post, f"{route}/_latet/engine/v1/approvals/wait", json=wait, headers=plugin)
        approval = {"action": "approve", "version": read["version"]}
        decided = cluster.b.post(f"{QUEUE}/batches/{sent['pending']}", json=approval, headers=UI)
        assert decided.json() == {"ok": True, "state": "approved"}  # as he read it: both checks pass
        answer = polled.result(timeout=25)
    assert address not in answer.text and "masked otherwise" in answer.json().get("error", ""), answer.text

    def recorded() -> bool:
        history = cluster.b.get(f"{QUEUE}/history").json()["items"]
        return any(item["id"] == sent["pending"] and item["state"] == "approved-changed" for item in history)

    _eventually(recorded)
    again = cluster.a.post(f"{route}/_latet/engine/v1/mask", json=items, headers=plugin).json()
    assert again.get("pending") and again["pending"] != sent["pending"] and again["decides"] == "security", again


def test_a_save_over_a_composition_supersedes_her_request_waiting_for_the_security_team(company):
    """Task 3's review, m1 (Review Focus 1 under shared settings), on two instances: a destination the shared settings
    removed waits for her ([D1]: her settings in use are a composition); her save that lowers protection waits for
    the security team instead; then a save of hers that adds protection, laid over the composition, makes the space
    apply its settings again, which offers the composition's own change: it supersedes her request (at most one
    waits). Nothing she asked applies, the security team's queue no longer lists it, its record ends ``superseded``,
    and the change that waits is hers to decide, as both instances say."""
    cluster, emily, mark, _ = company
    first = cluster.servers[0]
    shared = Config(upstreams=[Destination(name="acme-llm", url="https://llm.acme.example/v1")])
    first.shared.set(shared, actor=BY_HAND)
    company_session(cluster, emily, "code")
    _eventually(lambda: [d["name"] for d in cluster.a.get(f"{API}/config").json()["upstreams"]] == ["acme-llm"])
    first.shared.set(Config(), actor=BY_HAND)  # removed for everyone: what she approved had it

    def waiting(client):
        return client.get(f"{API}/changes").json()["changes"]

    _eventually(lambda: [(c["source"], c["decides"]) for c in waiting(cluster.a)] == [("file", "self")])
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    saved = cluster.a.put(f"{API}/config", json={"allow_values": ["ACME"], "etag": etag}, headers=UI)
    assert saved.status_code == 409 and saved.json()["pending"]["decides"] == "security"
    asked = saved.json()["pending"]["id"]
    assert [c["id"] for c in waiting(cluster.a)] == [asked]
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    term = {"terms": [{"value": "Orion", "type": "PROJECT"}], "etag": etag}
    assert cluster.a.put(f"{API}/config", json=term, headers=UI).status_code == 200  # adds protection: applied
    for client in (cluster.a, cluster.b):
        _eventually(lambda client=client: [(c["source"], c["decides"]) for c in waiting(client)] == [("file", "self")])
        assert "ACME" not in str(client.get(f"{API}/config").json()["allow_values"])
    company_session(cluster, mark, "code")
    _eventually(lambda: cluster.b.get(QUEUE).json()["changes"] == [])
    [ended] = [item for item in cluster.b.get(f"{QUEUE}/history").json()["items"] if item["id"] == asked]
    assert ended["state"] == "superseded" and ended["identity"]["id"] == emily.id


# --- a decider's approval at the gateway, kept only for texts that left (the merge of 0.12.0, fix round 2) -----------

AUTH = {"authorization": "Bearer sk-test"}
CHAT = "/llm/v1/chat/completions"
_PROXIES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


async def _routed(spaces, identity) -> bool:
    """Whether her space on this instance routes the shared destination `llm` and runs the Live Reviewer."""
    runtime = await spaces.runtime(identity)
    live = runtime.mediator.live_review
    return runtime.gateway.routes.match(CHAT) is not None and live is not None and live.enabled


async def _runtime_of(spaces, identity):
    return await spaces.runtime(identity)


@pytest.fixture
def model(company, monkeypatch):
    """The company's shared destination `llm` sent to a fake model, heard by both instances, and Emily's Live Reviewer
    on: a proxy agent's request through either instance waits for the security team."""
    for name in _PROXIES:
        monkeypatch.delenv(name, raising=False)
    fake = FakeUpstream().start()
    cluster, emily, _mark, _token = company
    server = cluster.server
    shared = server.shared.get()
    server.shared.set(shared.model_copy(update={"upstreams": [Destination(name="llm", url=fake.url)]}), actor=BY_HAND)
    company_session(cluster, emily, "code")
    etag = cluster.a.get(f"{API}/config").json()["etag"]
    on = cluster.a.put(f"{API}/config", json={"live_review": {"enabled": True}, "etag": etag}, headers=UI)
    assert on.status_code == 200, on.text
    for client, each in zip(cluster.clients, cluster.servers, strict=True):
        found = each.identities.get(emily.id)
        _eventually(lambda client=client, each=each, found=found: client.portal.call(_routed, each.spaces, found))
    try:
        yield fake
    finally:
        fake.stop()


def _send(client, route: str, text: str):
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": text}]}
    return client.post(f"{route}{CHAT}", json=body, headers=AUTH)


def _waits(cluster, mark, but: frozenset[str] = frozenset(), seconds: float = 15.0) -> dict:
    """The batch that waits for the security team, read through the second instance as Mark: one not in ``but``."""
    company_session(cluster, mark, "sso")
    deadline = time.monotonic() + seconds
    while True:
        listed = cluster.b.get(QUEUE).json()["batches"]
        fresh = [b["id"] for b in listed if b["id"] not in but]
        if fresh:
            return cluster.b.get(f"{QUEUE}/batches/{fresh[0]}").json() | {"id": fresh[0]}
        assert time.monotonic() < deadline, "nothing waits for the security team"
        time.sleep(0.05)


def _decide(cluster, batch: dict, approve: bool) -> None:
    action = {"action": "approve", "version": batch["version"]} if approve else {"action": "reject"}
    decided = cluster.b.post(f"{QUEUE}/batches/{batch['id']}", json=action, headers=UI)
    assert decided.status_code == 200, decided.text


def _waits_again(cluster, mark, pool, client, route: str, text: str, seen: set[str]) -> None:
    """The same text sent again through ``client``: it waits for the security team again (a new batch), and is
    rejected so that the request ends."""
    again = pool.submit(_send, client, route, text)
    batch = _waits(cluster, mark, frozenset(seen))
    seen.add(batch["id"])
    _decide(cluster, batch, False)
    assert again.result(timeout=25).status_code >= 400


def _approved_after_review(cluster, emily, mark, route, pool, text, refuse):
    """Emily's agent sends ``text`` through the first instance; Mark approves it as he read it through the second; then,
    after its reviews and before it leaves, ``refuse(prepared, runtime)`` runs on the first instance (its settings
    moved, or it refused there). The request's answer and the batch."""
    first = cluster.servers[0]
    runtime = cluster.a.portal.call(_runtime_of, first.spaces, first.identities.get(emily.id))
    mediator = runtime.mediator
    reviewed = mediator.live_prepared
    done: list[bool] = []

    async def after(prepared, *args, **kwargs):
        prepared = await reviewed(prepared, *args, **kwargs)
        if not done:  # this request alone
            done.append(True)
            refuse(prepared, runtime)
        return prepared

    mediator.live_prepared = after
    try:
        sent = pool.submit(_send, cluster.a, route, text)
        batch = _waits(cluster, mark)
        _decide(cluster, batch, True)
        answer = sent.result(timeout=25)
    finally:
        mediator.live_prepared = reviewed
    return answer, batch


def _recorded_withheld(cluster, batch_id: str) -> bool:
    history = cluster.b.get(f"{QUEUE}/history").json()["items"]
    return any(item["id"] == batch_id and item["state"] == "approved-changed" for item in history)


def _moved(prepared, runtime) -> None:
    runtime.built.engine.rules_changed()  # the settings moved after its reviews, before it leaves


def _refused_otherwise(prepared, runtime) -> None:
    prepared.block_reason = "Latet did not send this request: stopped after its reviews"  # not its settings


def test_a_decider_s_approval_of_a_request_refused_because_its_settings_changed_waits_again_on_every_instance(
    company, model
):
    """The security re-review of the merge of 0.12.0 (fix round 1, R1): Emily's proxy agent sends a text through the
    first instance, the security team approves it as Mark read it, then its settings move after its reviews and before
    it leaves, so that the gateway refuses it because its settings changed (400, nothing sent). Fix round 1 took its
    approval back from the session in memory alone: on a server the conversation's approved texts are Valkey's, read
    again at each request, so sent again on either instance the text did not wait. A decider's approval is now taken
    only once the texts have left: the batch is recorded withheld (`approved-changed`), and the text sent again through
    either instance waits for the security team again."""
    cluster, emily, mark, token = company
    route = f"/u/{route_key(token)}"
    text = "ask emily.carter@acme.com about the Orion contract"
    with ThreadPoolExecutor(1) as pool:
        answer, batch = _approved_after_review(cluster, emily, mark, route, pool, text, _moved)
        assert answer.status_code == 400 and "the settings changed" in answer.text, answer.text
        _eventually(lambda: _recorded_withheld(cluster, batch["id"]))
        seen = {batch["id"]}
        _waits_again(cluster, mark, pool, cluster.a, route, text, seen)  # on the instance that refused it
        _waits_again(cluster, mark, pool, cluster.b, route, text, seen)  # and on the other one
    assert "emily.carter@acme.com" not in model.all_received()


def test_a_decider_s_approval_of_a_request_refused_otherwise_before_it_leaves_leaves_nothing_approved(company, model):
    """R2 of the same re-review: a request refused after the security team's approval for another reason than its
    settings (here its preparation stopped after its reviews) kept the approval: sent again, the text did not wait. It
    is now taken only once the texts have left, so sent again through either instance it waits again."""
    cluster, emily, mark, token = company
    route = f"/u/{route_key(token)}"
    text = "ask emily.carter@acme.com about the Halden renewal"
    with ThreadPoolExecutor(1) as pool:
        answer, batch = _approved_after_review(cluster, emily, mark, route, pool, text, _refused_otherwise)
        assert answer.status_code >= 400 and "stopped after its reviews" in answer.text, answer.text
        _eventually(lambda: _recorded_withheld(cluster, batch["id"]))  # fix round 3: never kept approved
        seen = {batch["id"]}
        _waits_again(cluster, mark, pool, cluster.a, route, text, seen)
        _waits_again(cluster, mark, pool, cluster.b, route, text, seen)
    assert "emily.carter@acme.com" not in model.all_received()


def test_a_decider_s_approval_of_a_request_that_left_is_kept_on_every_instance(company, model):
    """The other side: a request the security team approved that left (its provider answered) keeps its approval in
    the conversation, which every instance reads: the same text sent again through either instance does not wait."""
    cluster, emily, mark, token = company
    route = f"/u/{route_key(token)}"
    text = "ask emily.carter@acme.com about the Kestrel audit"
    with ThreadPoolExecutor(1) as pool:
        answer, batch = _approved_after_review(cluster, emily, mark, route, pool, text, lambda prepared, runtime: None)
        assert answer.status_code == 200, answer.text
        for client in cluster.clients:
            again = pool.submit(_send, client, route, text)
            try:
                answered = again.result(timeout=20)  # it does not wait: no decision comes
            except TimeoutError:
                _decide(cluster, _waits(cluster, mark, frozenset({batch["id"]})), False)
                again.result(timeout=25)
                raise AssertionError("the approved text waited again") from None
            assert answered.status_code == 200, answered.text
    assert not _recorded_withheld(cluster, batch["id"])
    assert "emily.carter@acme.com" not in model.all_received()


def test_a_decider_s_approval_of_a_request_refused_before_it_leaves_is_recorded_withheld(company, model, monkeypatch):
    """Fix round 3 of the merge of 0.12.0 (the security re-review's note on R2): a request the security team approved
    as Mark read it is refused before it leaves for another reason than its settings, here because the status of the
    exceptions it is sent under could not be kept visible (503, nothing sent). Its texts wait again when they are sent
    again (fix round 2), and now the company's record no longer keeps the batch approved: it is recorded withheld
    (`approved-changed`), as one refused because its settings changed is."""
    from latet.gateway import forward

    cluster, emily, mark, token = company
    route = f"/u/{route_key(token)}"
    text = "ask emily.carter@acme.com about the Osprey bid"
    real, refused = forward._visible, []

    def once(*args, **kwargs):
        if not refused:  # the first request alone
            refused.append(True)
            return False
        return real(*args, **kwargs)

    monkeypatch.setattr(forward, "_visible", once)
    with ThreadPoolExecutor(1) as pool:
        answer, batch = _approved_after_review(cluster, emily, mark, route, pool, text, lambda prepared, runtime: None)
        assert answer.status_code == 503 and "could not be kept visible" in answer.text, answer.text
        _eventually(lambda: _recorded_withheld(cluster, batch["id"]))
        _waits_again(cluster, mark, pool, cluster.b, route, text, {batch["id"]})
    assert "emily.carter@acme.com" not in model.all_received()


async def _relay_a_turn_refused_after_its_review(spaces, identity, text: str) -> tuple[str, list[str], list[bool]]:
    """A WebSocket turn of hers through this instance's relay (Codex's Responses over a WebSocket, to a model that keeps
    what it is sent), approved as a decider read it (``Decision.version``), then its rules moved after its reviews and
    before it leaves: the socket ends with nothing of the turn sent. Its session's key, and the frames the model got."""
    from latet.gateway import ws
    from latet.gateway.approvals import Decision, batch_version
    from latet.gateway.capture import Capture
    from latet.gateway.routes import RouteTable, Upstream

    from ...unit.test_request_outcomes import _Client, _Upstream

    runtime = await spaces.runtime(identity)
    mediator = runtime.mediator

    async def approved(batch, gone, held=None):
        session, items = held
        return Decision(True, version=batch_version(await mediator.run(mediator._views_now, session, items, False)))

    reviewed, waited = mediator.live_prepared, mediator.await_decision
    sessions: list[str] = []
    taken: list[bool] = []

    async def moved_after(prepared, *args, **kwargs):
        prepared = await reviewed(prepared, *args, **kwargs)
        sessions.append(prepared.session.key)
        taken.append(bool(prepared.session.approved) or prepared.decider_approval is not None)  # it was approved
        mediator.engine.rules_changed()
        return prepared

    mediator.live_prepared, mediator.await_decision = moved_after, approved
    upstream, client = _Upstream(), _Client()
    try:
        expected = frozenset({("model", "m")})
        routes = RouteTable(
            [Upstream("relay", "/relay", f"http://127.0.0.1:{upstream.port}", envelope_expected=expected)]
        )
        turn = {
            "type": "response.create",
            "model": "m",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
        }
        relaying = asyncio.create_task(ws.relay(client.socket, routes, Capture(None), mediator))
        client.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(turn)})
        await asyncio.wait_for(relaying, 10)
    finally:
        mediator.live_prepared, mediator.await_decision = reviewed, waited
        upstream.close()
    [key] = sessions
    return key, list(upstream.got), taken


async def _approved_texts(spaces, identity, key: str) -> set[str]:
    """What the conversation ``key`` holds as approved, as this instance's next request reads it (from Valkey)."""
    runtime = await spaces.runtime(identity)
    conversations = runtime.built.engine.conversations
    return set(await runtime.mediator.run(lambda: conversations.session(key).approved))


def test_a_decider_s_approval_of_a_websocket_turn_that_never_left_is_on_no_instance(company, model):
    """R1 of the re-review, over a WebSocket: a turn the security team approved as read, whose rules move after its
    reviews, never leaves (the relay ends the socket). Fix round 1 took its approval back in memory alone: the
    conversation's approved texts in Valkey kept it, on every instance. It is now taken only once the turn has left,
    so neither instance's conversation holds it."""
    cluster, emily, _mark, _token = company
    first, second = cluster.servers
    text = "write to emily.carter@acme.com about the Falcon launch"
    on_a, on_b = first.identities.get(emily.id), second.identities.get(emily.id)
    key, got, taken = cluster.a.portal.call(_relay_a_turn_refused_after_its_review, first.spaces, on_a, text)
    assert taken == [True], "the security team's approval was not reached"
    assert got == [], "the turn left"
    assert cluster.a.portal.call(_approved_texts, first.spaces, on_a, key) == set()
    assert cluster.b.portal.call(_approved_texts, second.spaces, on_b, key) == set()
