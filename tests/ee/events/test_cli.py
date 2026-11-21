"""`latet server reports` and `latet server siem`, through the administration API of a server (answered here by E3's
cluster): a report exported, the people switch shown and changed, outputs set from a file with a pasted credential
refused, a secret kept out of the settings, a test that says whether the event arrived, each call recorded as the
administration token's; and the lines `latet server status` shows."""

import json
import uuid
from datetime import date

import pytest
from typer.testing import CliRunner

import latet.cli.admin  # noqa: F401 - register commands before a test replaces admin_call
from latet.cli.agent import MARKERS
from latet.doctor.checks import FAIL, OK, WARN
from latet.ee.events.schema import checked
from latet.ee.events.store import MemoryEvents, gap_event
from latet.ee.license import License, Standing, State
from latet.ee.settings import EnterpriseSettings, SiemOutput, SiemSettings, siem_bound
from latet.events import make_event
from latet.server.client import ServerError

from ..people.server_support import BY_HAND, NOW, as_admin, paused
from .receivers import HttpsReceiver, certificate
from .servers import quiet

DAY = ["--from", "2027-03-01", "--to", "2027-03-01"]  # E3's clock's day


def _run(*args, input=None):
    from latet.cli.main import app

    return CliRunner().invoke(app, ["server", *args], input=input)


@pytest.fixture
def cli(cluster, monkeypatch):
    """`latet server …` in-process, run by you (no agent's marker), its calls answered by the server under test."""
    quiet(cluster)
    for markers in MARKERS.values():
        for name in markers:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("AGENT", raising=False)

    def admin_call(method, path, body=None, *, query=None):
        r = cluster.a.request(
            method, f"/_latet/server/v1/admin/{path}", json=body, params=query, headers=as_admin(cluster)
        )
        if r.status_code >= 400:
            found = r.json()
            details = tuple(item["msg"] for item in found.get("details", []) if isinstance(item, dict))
            raise ServerError(r.status_code, found.get("error", r.text), details)
        return r.json() if r.content else None

    from latet.ee.events import cli as events_cli

    monkeypatch.setattr(events_cli, "admin_call", admin_call)
    store = cluster.server.edition.events.store
    event = make_event("exchange", clock=lambda: NOW, group="g_paymentsaa", agent="codex", project="billing-api",
                       masked={"EMAIL": 2})  # fmt: skip
    store.write(uuid.uuid4().hex, [checked(event)], at=NOW)
    return _run


@pytest.mark.server
def test_a_report_is_exported_from_the_command_line(cli, cluster, tmp_path):
    result = cli("reports", "export", *DAY)
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[:2] == [
        "day,group,kind,name,project,agent,exchanges,stopped,masked,EMAIL",
        "2027-03-01,g_paymentsaa,,,billing-api,codex,1,0,2,2",  # a group no longer known: by its id (R8)
    ]
    out = tmp_path / "march.json"
    result = cli("reports", "export", *DAY, "--format", "json", "-o", str(out))
    assert result.exit_code == 0, result.output
    body = json.loads(out.read_text("utf-8"))
    assert body["totals"]["masked"] == 2 and "administration" in body
    entries = cluster.server.audit.query(action="reports.exported", limit=10)
    assert [e.actor.startswith("admin:") for e in entries] == [True, True]


@pytest.mark.server
def test_a_license_that_produces_nothing_exports_nothing_and_says_why(cli, cluster):
    paused(cluster)
    result = cli("reports", "export")
    assert result.exit_code == 2 and "expired on 2027-03-10" in result.output


@pytest.mark.server
def test_the_people_switch_is_shown_changed_and_recorded(cli, cluster):
    result = cli("reports", "settings")
    assert result.exit_code == 0 and "people: group-only" in result.output
    result = cli("reports", "settings", "--people", "pseudonymous")
    assert result.exit_code == 0 and cluster.server.edition.settings().reports.people == "pseudonymous"
    [entry] = [e for e in cluster.server.audit.query(action="reports.changed", limit=10) if e.actor != BY_HAND]
    assert entry.details["people"] == "pseudonymous" and entry.actor.startswith("admin:")
    assert cli("reports", "settings", "--people", "everyone").exit_code == 2


@pytest.mark.server
def test_outputs_are_set_from_a_file_and_a_pasted_credential_is_refused(cli, tmp_path):
    pasted = tmp_path / "pasted.yaml"
    pasted.write_text(
        "siem:\n  - name: splunk\n    kind: webhook\n    url: https://siem.acme.com/in\n"
        "    headers:\n      Authorization: Splunk 5f0c9a2e\n",
        "utf-8",
    )
    result = cli("siem", "set", str(pasted))
    assert result.exit_code == 2 and "carries a credential" in result.output and "5f0c9a2e" not in result.output
    good = tmp_path / "outputs.yaml"
    good.write_text(
        "- name: elastic\n  kind: webhook\n  url: https://elastic.acme.com/in\n"
        "  headers:\n    Authorization: ApiKey {secret}\n  secret: siem-elastic\n",
        "utf-8",
    )
    result = cli("siem", "set", str(good))
    assert result.exit_code == 0 and "elastic" in result.output
    assert "elastic: webhook https://elastic.acme.com/in" in cli("siem", "show").output


@pytest.mark.server
def test_a_secret_is_kept_on_the_server_never_in_the_settings_and_its_change_recorded(cli, cluster, caplog):
    elastic = {"name": "elastic", "kind": "webhook", "url": "https://elastic.acme.com/in"}
    cluster.server.edition.settings_store.set_section("siem", {"outputs": [elastic]}, actor=BY_HAND)
    result = cli("siem", "secret", "elastic", input="5f0c9a2e\n")
    assert result.exit_code == 0, result.output
    bound = siem_bound(SiemOutput.model_validate(elastic | {"secret": "siem-elastic"}))
    assert cluster.server.edition.secrets.load("siem-elastic", bound=bound) == "5f0c9a2e"  # sealed for it (I1)
    assert cluster.server.edition.settings().siem.outputs[0].secret == "siem-elastic"
    entries = cluster.server.audit.query(action="siem.*", limit=10)
    assert "siem.secret.changed" in [e.action for e in entries]
    assert "5f0c9a2e" not in json.dumps([e.details for e in entries]) and "5f0c9a2e" not in result.output
    assert "5f0c9a2e" not in caplog.text
    assert cli("siem", "secret", "elastic", "--remove").exit_code == 0
    assert cluster.server.edition.secrets.load("siem-elastic") is None


@pytest.mark.server
def test_siem_test_says_whether_the_event_arrived(cli, cluster, tmp_path, request):
    cert, key = certificate(tmp_path)
    receiver = HttpsReceiver(cert, key, tls=False).start()
    request.addfinalizer(receiver.stop)
    outputs = [{"name": name, "kind": "webhook", "url": receiver.url} for name in ("collector", "backup")]
    cluster.server.edition.settings_store.set_section("siem", {"outputs": outputs}, actor=BY_HAND)
    result = cli("siem", "test")
    assert result.exit_code == 0 and "collector: arrived" in result.output and "backup: arrived" in result.output
    receiver.status = 503
    result = cli("siem", "test", "collector")  # within 10 seconds of the last: the server's limit (I2)
    assert result.exit_code == 2 and "once every 10 seconds" in result.output
    cluster.server.bus.purge("rate:siem-test")  # the window over, as 10 seconds later
    result = cli("siem", "test", "collector")
    assert result.exit_code == 1 and "collector: did not arrive: it answered 503" in result.output


@pytest.mark.server
def test_siem_skip_sends_no_more_the_entry_a_receiver_refuses(cli, cluster):
    store = cluster.server.edition.events.store
    store.failed("collector", "it answered 400: an entry of the administration log", at=NOW, retry_at=NOW + 3600,
                 blocked=True)  # not due: the delivery leaves its lease alone  # fmt: skip
    outputs = [{"name": "collector", "kind": "webhook", "url": "https://siem.acme.com/in"}]
    cluster.server.edition.settings_store.set_section("siem", {"outputs": outputs}, actor=BY_HAND)
    result = cli("siem", "skip", "collector")
    assert result.exit_code == 2 and "nothing to skip" in result.output  # nothing held
    entry = {"id": "f" * 32, "type": "admin", "chain": {"prev": "", "hash": "a" * 64}}
    store.put(["collector"], [entry], at=NOW, refs=[7])
    with cluster.server.bus.lease("siem:collector", 60) as sending:  # another instance sends to it now (R3-4)
        assert sending
        result = cli("siem", "skip", "collector")
    assert result.exit_code == 2 and "another instance" in result.output
    assert store.state("collector").blocked  # nothing skipped meanwhile
    result = cli("siem", "skip", "collector")
    assert result.exit_code == 0 and "skipped entry 7" in result.output and not store.state("collector").blocked
    [skipped] = cluster.server.audit.query(action="siem.skipped", limit=10)
    assert skipped.details == {"output": "collector", "from": 7, "to": 7, "count": 1, "dropped": 0, "status": 200}
    assert skipped.actor.startswith("admin:")


def _verified(gaps):
    """E0c's `log/verify` answer with the chain whole and ``gaps`` as given (`missing`: a server that said nothing)."""

    def answered(method, path, body=None, **kwargs):
        assert (method, path) == ("GET", "log/verify")
        found = {"ok": True, "checked": 9, "broken_at": None, "head": {"id": 9, "hash": "c" * 64}}
        return found if gaps == "missing" else found | {"gaps": gaps}

    return answered


def test_log_verify_says_which_gap_markers_of_a_siems_copy_are_not_latets(monkeypatch, tmp_path):
    gaps = {"recorded": [[1, 3, 3]], "first_kept": 1, "unrecorded": [], "unchecked": []}
    monkeypatch.setattr("latet.cli.server.admin_call", _verified(gaps))
    genuine = gap_event(1, 3, 3, "", "a" * 64, origin="https://latet.acme.lan", at=NOW)
    forged = gap_event(100, 200, 101, "a" * 64, "b" * 64, origin="https://latet.acme.lan", at=NOW)
    own = {"v": 1, "id": "e" * 32, "type": "admin", "outcome": "siem.gap", "reason": "server", "rule": "hook"}
    entry = {"v": 1, "id": "f" * 32, "type": "admin", "outcome": "person.added", "reason": "admin:3f9a"}
    copy = tmp_path / "siem.ndjson"  # each line as a SIEM exports it: HEC's envelope, Splunk's search, Elastic's hit
    copy.write_text("\n".join(json.dumps(line) for line in (
        entry, own, {"event": genuine}, {"result": {"_raw": json.dumps(forged)}},
        {"_source": genuine | {"stopped": "3"}}, genuine | {"stopped": 3.0}, genuine | {"reason": "audit:1-3x"},
    )) + "\n", encoding="utf-8")  # R4-1: its count a string, a float, a range that does not read  # fmt: skip
    result = _run("log", "verify", "--siem", str(copy))
    assert result.exit_code == 1 and result.output.count("not Latet's") == 4  # the log's own entry passes
    assert "no entry of the log names its range): audit:100-200, 101 entries" in result.output  # R3-1
    assert result.output.count("its range cannot be read") == 3
    whole = tmp_path / "siem.json"
    whole.write_text(json.dumps([entry, genuine]), encoding="utf-8")
    result = _run("log", "verify", "--siem", str(whole))
    assert result.exit_code == 0 and "1 in siem.json" in result.output


def test_log_verify_tells_a_marker_it_cannot_check_and_a_file_it_cannot_read_from_a_failure(monkeypatch, tmp_path):
    old = gap_event(1, 3, 3, "", "a" * 64, origin="https://latet.acme.lan", at=NOW)
    copy = tmp_path / "siem.json"
    copy.write_text(json.dumps({"hits": {"hits": [{"_source": old}]}}, indent=2), encoding="utf-8")  # Elastic's answer
    kept = {"recorded": [], "first_kept": 41, "unrecorded": [], "unchecked": []}  # a prune kept entries from 41 on
    monkeypatch.setattr("latet.cli.server.admin_call", _verified(kept))
    result = _run("log", "verify", "--siem", str(copy))
    assert result.exit_code == 0 and "cannot be checked: the log keeps entries from 41" in result.output  # R4-2
    other = tmp_path / "other.ndjson"
    other.write_text('{"message": "a line of another program"}\n', encoding="utf-8")
    result = _run("log", "verify", "--siem", str(other))
    assert result.exit_code == 2 and "no Latet event found in other.ndjson" in result.output  # R4-3: never a pass
    monkeypatch.setattr("latet.cli.server.admin_call", _verified("missing"))  # a walk that failed, or an older server
    result = _run("log", "verify", "--siem", str(copy))
    assert result.exit_code == 1 and "did not say which gap markers its log names" in result.output


def test_the_status_fails_once_the_bound_collapsed_entries_of_the_log_and_warns_before(monkeypatch):
    from latet.ee.events import checks as checks_module
    from latet.ee.events.checks import check_events

    monkeypatch.setattr(checks_module, "ADMIN_KEPT", 30)  # a tenth of it: 3
    store = MemoryEvents()
    settings = EnterpriseSettings(siem=SiemSettings(outputs=[SiemOutput(name="elastic", kind="webhook",
                                                                        url="https://elastic.acme.com/in")]))  # fmt: skip
    lic = License("L-2027-0001", "Acme Corp", 300, frozenset({"siem"}), date(2026, 11, 1), date(2027, 10, 31))
    standing = Standing(State.VALID, lic, None, 200, None, None, ())
    entries = [{"id": f"{i:032x}", "type": "admin", "chain": {"prev": "", "hash": f"{i:064x}"}} for i in range(1, 5)]
    store.put(["elastic"], entries, at=NOW, refs=[1, 2, 3, 4])

    def line():
        found = check_events(
            queue=store, settings=settings, standing=standing, secret_state=lambda output: (True, False)
        )
        return {check.title: check for check in found}["SIEM elastic"]

    assert line().status == WARN and "4 entries of the administration log waiting" in line().detail
    assert store.collapse("elastic", 2, origin="https://latet.acme.lan", at=NOW) == (1, 3, 3)
    assert line().status == FAIL and "3 entries of the administration log collapsed" in line().detail
    store.done("elastic", [seq for seq, _ in store.peek("elastic", 1)], at=NOW)  # it delivers again
    assert line().status == OK


def test_the_status_says_since_when_an_output_fails_what_waits_and_a_secret_not_set():
    from latet.ee.events.checks import check_events

    store = MemoryEvents()
    outputs = [
        SiemOutput(name="elastic", kind="webhook", url="https://elastic.acme.com/in"),
        SiemOutput(name="splunk", kind="splunk", url="https://splunk.acme.com:8088/services/collector/event",
                   secret="siem-splunk"),
    ]  # fmt: skip
    store.put(["elastic"], [{"id": "a" * 32}, {"id": "b" * 32}, {"id": "c" * 32}], at=NOW)
    store.trim("elastic", 2)
    store.failed("elastic", "it could not be reached", at=NOW, retry_at=NOW + 5)
    lic = License(
        "L-2027-0001", "Acme Corp", 300, frozenset({"reports", "siem"}), date(2026, 11, 1), date(2027, 10, 31)
    )
    standing = Standing(State.VALID, lic, None, 200, None, None, ())
    settings = EnterpriseSettings(siem=SiemSettings(outputs=outputs))
    lines = check_events(queue=store, settings=settings, standing=standing, secret_state=lambda output: (False, False))
    checks = {check.title: check for check in lines}
    assert checks["Event log"].status == OK and "people group-only" in checks["Event log"].detail
    elastic = checks["SIEM elastic"]
    assert elastic.status == WARN and "failing since 2027-03-01" in elastic.detail
    assert "(it could not be reached)" in elastic.detail and "2 waiting, 1 dropped" in elastic.detail
    assert checks["SIEM splunk"].status == FAIL and "latet server siem secret splunk" in checks["SIEM splunk"].detail
    moved = {c.title: c for c in check_events(queue=store, settings=settings, standing=standing,
                                              secret_state=lambda output: (False, True))}  # fmt: skip
    assert moved["SIEM splunk"].status == FAIL and "set for another address" in moved["SIEM splunk"].detail  # I1
    store.put(["elastic"], [{"id": "d" * 32, "type": "admin"}], at=NOW)
    store.failed(
        "elastic", "it answered 400: an entry of the administration log", at=NOW, retry_at=NOW + 5, blocked=True
    )
    store.put(["gone"], [{"id": "e" * 32, "type": "admin"}], at=NOW)
    store.forget(["gone"])  # an output removed took an entry of the log with it (I5)
    held_at = {c.title: c for c in check_events(queue=store, settings=settings, standing=standing,
                                                secret_state=lambda output: (True, False))}  # fmt: skip
    assert held_at["SIEM elastic"].status == FAIL and "administration log" in held_at["SIEM elastic"].detail
    assert "`latet server siem skip elastic`" in held_at["SIEM elastic"].detail  # R2-12
    assert held_at["Event log"].status == WARN and "1 entry of the administration log" in held_at["Event log"].detail
    paused_standing = Standing(State.PAUSED, lic, None, -45, None, None, ("paused",))
    held = {c.title: c for c in check_events(queue=store, settings=settings, standing=paused_standing,
                                             secret_state=lambda output: (True, False))}  # fmt: skip
    assert held["Event log"].status == WARN and "expired" in held["Event log"].detail


@pytest.mark.server
@pytest.mark.parametrize("kind", ["marker", "events", "mixed"])
def test_cli_skip_preserves_the_servers_held_run_contract(cli, cluster, kind):
    store = cluster.server.edition.events.store
    cluster.server.edition.settings_store.set_section(
        "siem",
        {"outputs": [{"name": "collector", "kind": "webhook", "url": "https://siem.acme.com/in"}]},
        actor=BY_HAND,
    )
    event = make_event("exchange", clock=lambda: NOW, masked={"EMAIL": 1})
    marker = gap_event(1, 3, 3, "", "a" * 64, origin="https://latet.acme.lan", at=NOW)
    entries = [marker] if kind == "marker" else [event, event | {"id": "b" * 32}]
    refs = [None] * len(entries)
    if kind == "mixed":
        entries.append({"id": "c" * 32, "type": "admin", "chain": {"prev": "", "hash": "a" * 64}})
        refs.append(7)
    store.put(["collector"], entries, at=NOW, refs=refs)
    rows = store.peek("collector", 10)
    store.failed(
        "collector",
        "it answered 400",
        at=NOW,
        retry_at=NOW + 3600,
        blocked=kind == "marker",
        held_to=rows[-1][0] if kind != "marker" else None,
    )
    done = cli("siem", "skip", "collector")
    assert done.exit_code == 0, done.output
    dropped = 0 if kind == "marker" else 2
    assert f"{dropped} non-admin events dropped" in done.output
    assert ("administration log" in done.output) is (kind != "events")
    [entry] = cluster.server.audit.query(action="siem.skipped", limit=10)
    assert entry.details["dropped"] == dropped and entry.actor.startswith("admin:")
    assert not store.state("collector").blocked and store.state("collector").held_to is None


@pytest.mark.server
@pytest.mark.parametrize("failure", ["expired", "unavailable"])
def test_cli_skip_refuses_an_expired_or_unreadable_lease(cli, cluster, monkeypatch, caplog, failure):
    from .test_events_api_server import _Lease

    store = cluster.server.edition.events.store
    cluster.server.edition.settings_store.set_section(
        "siem",
        {"outputs": [{"name": "collector", "kind": "webhook", "url": "https://siem.acme.com/in"}]},
        actor=BY_HAND,
    )
    store.put(
        ["collector"], [{"id": "c" * 32, "type": "admin", "chain": {"prev": "", "hash": "a" * 64}}], at=NOW, refs=[7]
    )
    store.failed("collector", "it answered 400", at=NOW, retry_at=NOW + 3600, blocked=True)
    lease = _Lease(held=False)
    if failure == "unavailable":

        def unavailable(margin):
            raise ConnectionError("secret-canary")

        monkeypatch.setattr(lease, "held", unavailable)
    monkeypatch.setattr(cluster.server.bus, "lease", lambda *args: lease)
    done = cli("siem", "skip", "collector")
    assert done.exit_code == 2, done.output
    assert store.state("collector").blocked
    assert cluster.server.audit.query(action="siem.skipped", limit=10) == []
    assert "secret-canary" not in done.output + caplog.text


@pytest.mark.server
def test_status_reads_event_queue_and_secrets_off_the_event_loop(cli, cluster, monkeypatch):
    import asyncio

    cluster.server.edition.settings_store.set_section(
        "siem",
        {
            "outputs": [
                {"name": "collector", "kind": "webhook", "url": "https://siem.acme.com/in", "secret": "siem-collector"}
            ]
        },
        actor=BY_HAND,
    )
    read = []

    def off_loop(name):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        read.append(name)

    original = cluster.server.edition.events.store.state

    def state(name):
        off_loop("queue")
        return original(name)

    def secret(*args, **kwargs):
        off_loop("secret")
        return "synthetic-secret"

    monkeypatch.setattr(cluster.server.edition.events.store, "state", state)
    monkeypatch.setattr(cluster.server.edition.secrets, "load", secret)
    response = cluster.a.get("/_latet/server/v1/admin/status", headers=as_admin(cluster))
    assert response.status_code == 200
    assert "Event log" in response.text and "SIEM collector" in response.text
    assert sorted(read) == ["queue", "secret"]
    assert "synthetic-secret" not in response.text
