"""Two running instances share reports and one ordered SIEM queue; each policy or license state change is one
event, even when both instances observe it. The gateway uses UTC wall time; the jobs use the server's clock."""

import json
import time

import pytest

from latet.ee.roles import Role

from ..people.server_support import API, BY_HAND, SERVER_TESTS, license, sign_in_as
from .receivers import HttpsReceiver, certificate, until
from .servers import AUTH, a_morning, drained, member, ready

pytestmark = [*SERVER_TESTS, pytest.mark.e2e]
EE = f"{API}/ee"
REQUESTS = ("exchange", "stopped", "apps")
POLICY = "latet_rules: 1" + chr(10) + "profile: max" + chr(10)


@pytest.fixture
def receiver(tmp_path):
    cert, key = certificate(tmp_path)
    found = HttpsReceiver(cert, key, tls=False).start()
    yield found
    found.stop()


def _running(cluster) -> None:
    threads = [server.edition.events.jobs._thread for server in cluster.servers]
    assert len(threads) == 2 and threads[0] is not threads[1]
    assert all(thread.is_alive() for thread in threads)


def _spied(server, kept: list[str], monkeypatch) -> None:
    """Observe the pipeline's incoming ids without changing its real writes or delivery."""
    pipeline = server.edition.events.pipeline
    put = pipeline.put

    def spy(event):
        if event.get("type") in REQUESTS:
            kept.append(event["id"])
        put(event)

    monkeypatch.setattr(pipeline, "put", spy)


def _look(cluster) -> None:
    """Both instances look now, while their job threads continue running too."""
    _running(cluster)
    for server in cluster.servers:
        server.edition.refresh()
        server.edition.events.watch.round()
        assert server.edition.events.pipeline.flush()


def _noted(cluster, **expected) -> bool:
    _look(cluster)
    _, text = cluster.server.edition.events.store.job("license")
    note = json.loads(text) if text else {}
    return bool(text) and all(note.get(key) == value for key, value in expected.items())


def test_two_instances_add_up_in_one_report_and_one_receiver_gets_each_event_once_in_order(
    cluster, upstream, receiver, tmp_path, monkeypatch
):
    ready(cluster, upstream, receiver=receiver, people="named")
    _running(cluster)
    a, b = cluster.servers
    emily = member(cluster, "Emily Carter", "emily.carter@acme.com", Role.SECURITY, group="Payments")
    daniel = member(cluster, "Daniel Okafor", "daniel.okafor@acme.com", group="Platform")
    on_a_ids: list[str] = []
    on_b_ids: list[str] = []
    _spied(a, on_a_ids, monkeypatch)
    _spied(b, on_b_ids, monkeypatch)
    first_day = time.strftime("%Y-%m-%d", time.gmtime())

    def received() -> list[str]:
        return [e["id"] for e in receiver.events() if e["type"] in REQUESTS]

    a_morning(cluster.a, emily, tmp_path / "emily")
    drained(cluster.a, a, emily)
    assert a.edition.events.pipeline.flush()  # A's writes commit before B's requests begin
    a_morning(cluster.b, daniel, tmp_path / "daniel")
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "one more question"}]}
    assert cluster.b.post(f"{emily.route}/llm/v1/chat/completions", json=body, headers=AUTH).status_code == 200
    drained(cluster.b, b, daniel)
    drained(cluster.b, b, emily)
    assert b.edition.events.pipeline.flush()
    sign_in_as(cluster, emily.identity)
    today = {"from": first_day, "to": time.strftime("%Y-%m-%d", time.gmtime())}
    responses = [client.get(f"{EE}/reports", params=today) for client in (cluster.a, cluster.b)]
    assert [response.status_code for response in responses] == [200, 200]
    from_a, from_b = [response.json() for response in responses]
    written = on_a_ids + on_b_ids
    assert len(on_a_ids) == 5 and len(on_b_ids) == 6
    assert len(set(written)) == len(written) == 11
    assert until(lambda: len(received()) >= len(written), timeout=30)
    time.sleep(2)  # allow both live senders another turn after acknowledgement
    _running(cluster)
    assert from_a["totals"] == from_b["totals"] and from_a["totals"]["exchanges"] == 11
    for report in (from_a, from_b):
        assert {r["label"]: r["exchanges"] for r in report["by_person"]} == {"Emily Carter": 6, "Daniel Okafor": 5}
        assert {r["label"]: r["exchanges"] for r in report["by_group"]} == {"Payments": 6, "Platform": 5}
    assert received() == written


def test_a_change_of_the_policy_in_force_is_one_event_whatever_the_number_of_instances(cluster, upstream, receiver):
    ready(cluster, upstream, receiver=receiver)
    assert until(lambda: _noted(cluster, policy=None), timeout=10)
    cluster.clock.advance(1)
    changed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cluster.clock()))
    policy = cluster.server.edition.policy
    floor = policy.read(POLICY)
    with cluster.server.db.transaction() as conn:
        policy.save(POLICY, floor, actor=BY_HAND, conn=conn)
    policy.committed()
    _look(cluster)
    assert all(server.edition.policy.floor() == floor for server in cluster.servers)

    def changed():
        return [e for e in receiver.events() if e["type"] == "policy" and e["ts"] >= changed_at]

    assert until(lambda: len(changed()) >= 1, timeout=30)
    _look(cluster)  # each instance sees the same policy again, without stopping either job thread
    time.sleep(2)
    _running(cluster)
    assert [(e["reason"], e["outcome"], e["ts"]) for e in changed()] == [("changed", "set", changed_at)]


def test_a_change_of_the_license_state_is_one_event_whatever_the_number_of_instances(cluster, upstream, receiver):
    ready(cluster, upstream, receiver=receiver)
    license(cluster, expires="2027-03-01")
    assert until(lambda: _noted(cluster, id="L-2027-0001", state="expiring"), timeout=10)
    assert all(server.edition.licensing.standing().state.value == "expiring" for server in cluster.servers)
    cluster.clock.advance(86_400)  # the paid license enters grace; reporting and delivery remain licensed
    changed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cluster.clock()))
    _look(cluster)
    assert all(server.edition.licensing.standing().state.value == "grace" for server in cluster.servers)

    def changed():
        # Setup's installation/expiring events were made at the old clock, even if delivered later.
        return [e for e in receiver.events() if e["type"] == "license" and e["ts"] >= changed_at]

    assert until(lambda: len(changed()) >= 1, timeout=30)
    _look(cluster)
    time.sleep(2)
    _running(cluster)
    assert [(e["reason"], e["outcome"], e["ts"]) for e in changed()] == [(None, "grace", changed_at)]
