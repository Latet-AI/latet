"""What the Reports, Audit and SIEM routes are made of, without a server: why the license produces nothing, in its
codes; the outputs a body gives, checked as the settings check them and refused in fixed words; the secrets kept and
removed under their own names alone; what a reader of the outputs is shown; the etag of the outputs; names looked up as
a report asks for them; the rows of E3's gate; the permission each recorded action needs; and the limits of tests,
destinations and exports."""

import asyncio
import functools
import gc
import inspect
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from latet.ee.access import ADMINISTRATION, EE, rule_for
from latet.ee.events import api as api_module
from latet.ee.events.admin import ADMINISTERING, PERMISSIONS
from latet.ee.events.api import (
    EventsApi,
    Looked,
    Sent,
    answer_body,
    keep_secrets,
    not_producing,
    parse_siem,
    pause_of,
    redacted,
    siem_etag,
    siem_view,
)
from latet.ee.events.store import QueueState
from latet.ee.license import License, Standing, State
from latet.ee.routes import Refused, Who
from latet.ee.settings import EnterpriseSettings, SiemOutput, SiemSettings, siem_bound

from ..editions import MemorySecrets

FEATURES = frozenset({"reports", "siem", "roles"})
SENTINEL = SiemOutput(name="sentinel", kind="webhook", url="https://x.logic.azure.com/workflows/1/triggers?sig={secret}",
                      secret="siem-sentinel", headers={"X-Source": "latet"})  # fmt: skip
HIDDEN = "\N{HORIZONTAL ELLIPSIS}"
EMILY = "u_emilycarte"


def _standing(state, *, days_left=10, over_since=None, features=FEATURES, expires=date(2027, 10, 31)):
    lic = License(
        "L-2027-0001", "Acme Corp", 300, features, date(2026, 11, 1), expires, trial=state is State.TRIAL_ENDED
    )
    return Standing(state, lic, None, days_left, over_since, None, ())


def test_why_the_license_produces_nothing_is_said_in_its_codes():
    assert pause_of(_standing(State.VALID), "reports") is None and pause_of(_standing(State.GRACE), "siem") is None
    assert pause_of(_standing(State.PAUSED, days_left=-45), "reports")["why"] == "expired"
    assert pause_of(_standing(State.PAUSED, over_since=1.0), "reports")["why"] == "users"
    ended = pause_of(_standing(State.TRIAL_ENDED, expires=date(2026, 12, 2)), "siem")
    assert ended["why"] == "trial-ended" and ended["expires"] == "2026-12-02"
    assert (
        pause_of(Standing(State.INVALID, None, None, None, None, "unreadable", ("invalid",)), "reports")["why"]
        == "unusable"
    )
    refused = not_producing(_standing(State.PAUSED, days_left=-45), "reports")
    assert isinstance(refused, Refused) and refused.status == 403
    assert refused.extra == {"code": "not-producing", "paused": {"why": "expired", "expires": "2027-10-31"}}
    assert not_producing(_standing(State.EXPIRING), "siem") is None


def test_the_outputs_a_body_gives_are_checked_and_refused_in_fixed_words():
    pasted = {"outputs": [{"name": "splunk", "kind": "webhook", "url": "https://siem.acme.com/in",
                           "headers": {"Authorization": "Splunk 5f0c9a2e"}}]}  # fmt: skip
    with pytest.raises(Refused) as caught:
        parse_siem(pasted)
    body = caught.value.answer().body.decode()
    assert caught.value.status == 400 and "carries a credential" in body and "5f0c9a2e" not in body
    outputs, given = parse_siem({"outputs": [{"name": "collector", "kind": "webhook", "url": "https://c.acme.com/in",
                                              "headers": {"Authorization": "Bearer {secret}"}}],
                                 "secrets": {"collector": "c0ll-ector"}})  # fmt: skip
    assert outputs[0].secret == "siem-collector" and given == {"collector": "c0ll-ector"}
    for body in ({"outputs": "none"}, {"outputs": [], "secrets": {"other": "x"}},
                 {"outputs": [SENTINEL.model_dump()], "secrets": {"sentinel": "two" + chr(10) + "lines"}},
                 {"outputs": [], "url": "https://elsewhere.acme.com"}):  # fmt: skip
        with pytest.raises(Refused):
            parse_siem(body)


def test_an_output_at_an_address_egress_refuses_is_refused_before_the_change_in_fixed_words():
    """R10, at a save (Task 5's carry): egress's verdict on a literal address, a clean 400 before the transaction."""
    metadata = {"outputs": [{"name": "meta", "kind": "webhook", "url": "https://[fe80::1]/latest"}]}
    with pytest.raises(Refused) as caught:
        parse_siem(metadata)
    assert caught.value.status == 400 and "never reaches" in caught.value.message
    assert "fe80" not in caught.value.answer().body.decode()


def test_secrets_are_kept_and_removed_under_the_siems_own_names_in_the_changes_transaction():
    secrets = MemorySecrets()
    secrets.save("siem-sentinel", "s1g-9d2c")
    secrets.save("sso-client-secret", "kept")  # the sign-in's: never touched here (R11)
    collector = SiemOutput(name="collector", kind="webhook", url="https://c.acme.com/in", secret="siem-collector")
    saved, removed = keep_secrets(secrets, [SENTINEL], [collector], {"collector": " c0ll-ector "}, conn=None)
    assert (saved, removed) == (["collector"], ["sentinel"])
    assert secrets.kept == {"siem-collector": "c0ll-ector", "sso-client-secret": "kept"}
    assert secrets.bound["siem-collector"] == siem_bound(collector)  # sealed for where it sends (I1)


def test_a_secret_no_output_names_any_more_goes_and_one_given_as_null_is_removed():
    secrets = MemorySecrets()
    secrets.save("siem-sentinel", "s1g-9d2c")
    secrets.save("siem-collector", "c0ll-ector")
    collector = SiemOutput(name="collector", kind="webhook", url="https://c.acme.com/in", secret="siem-collector")
    unnamed = SENTINEL.model_copy(update={"secret": None, "url": "https://x.logic.azure.com/workflows/1/triggers"})
    saved, removed = keep_secrets(secrets, [SENTINEL, collector], [unnamed, collector], {"collector": None}, conn=None)
    assert (saved, removed) == ([], ["collector", "sentinel"]) and secrets.kept == {}


def test_who_only_reads_the_outputs_sees_their_origin_alone():
    siem = SiemSettings(outputs=[SENTINEL])
    states = {"sentinel": QueueState(3, dropped=1, failing_since=5.0, last_error="it answered 503")}
    read = siem_view(siem, states, lambda output: (True, False), editor=False, editable=True, paused=None)
    [output] = read["outputs"]
    assert output["url"] == f"https://x.logic.azure.com/{HIDDEN}" and output["headers"] == {"X-Source": HIDDEN}  # I3
    assert (output["has_secret"], output["queued"], output["failing_since"], read["editable"]) == (True, 3, 5.0, False)
    assert read["etag"] is None  # a digest of the whole addresses: an editor's alone
    edited = siem_view(siem, {}, lambda output: (False, True), editor=True, editable=True, paused=None)
    [whole] = edited["outputs"]
    assert whole["url"] == SENTINEL.url and edited["editable"] and edited["etag"] == siem_etag(siem)
    assert whole["has_secret"] is False and whole["secret_destination_changed"] is True  # set again for it (I1)
    assert siem_etag(siem) != siem_etag(SiemSettings())
    assert redacted("tls://siem.acme.com:6514") == "tls://siem.acme.com:6514"
    assert redacted("https://endpoint4.collection.sumologic.com/receiver/v1/http/{secret}") == (
        f"https://endpoint4.collection.sumologic.com/{HIDDEN}"
    )
    assert redacted("https://hooks.acme.com?token={secret}") == f"https://hooks.acme.com/{HIDDEN}"
    held = siem_view(siem, {"sentinel": QueueState(1, held_to=9)}, lambda o: (True, False), editor=True, editable=False,
                     paused={"why": "expired", "expires": "2027-10-31"})  # fmt: skip
    assert held["outputs"][0]["held"] is True and held["editable"] is False and held["paused"]["why"] == "expired"


def test_names_are_looked_up_as_a_report_asks_for_them_each_once():
    asked: list[str] = []

    def find(identity):
        asked.append(identity)
        return {"u_emilycarte": "Emily Carter"}.get(identity)

    names = Looked(find)
    assert names.get("u_emilycarte") == "Emily Carter" and names.get("u_emilycarte") == "Emily Carter"
    assert names.get("u_removed000", "u_removed000") == "u_removed000" and asked == ["u_emilycarte", "u_removed000"]
    assert list(names) == ["u_emilycarte"] and len(names) == 1


def test_only_an_id_shaped_as_an_identitys_or_a_groups_is_looked_up():
    """A row written by hand names nothing: a group's name is never looked up as one (M9), nor any text as an id."""
    found: list[str] = []
    identities = SimpleNamespace(get=lambda i: found.append(i) or SimpleNamespace(name="Emily Carter", kind="person"))
    people = SimpleNamespace(group=lambda g: found.append(g) or SimpleNamespace(name="Payments"))
    api = _bare(server=SimpleNamespace(identities=identities), people=people)
    names = api._names()
    assert names.identities.get("u_emilycarte") == "Emily Carter" and names.kinds.get("u_emilycarte") == "person"
    assert names.groups.get("g_abcdefghij") == "Payments"
    assert names.groups.get("Payments") is None and names.identities.get("admin:3f9a") is None
    assert names.identities.get("u_" + chr(0)) is None
    assert found == ["u_emilycarte", "u_emilycarte", "g_abcdefghij"]


@pytest.mark.parametrize(
    ("path", "read", "change"),
    [
        ("/_latet/api/v1/ee/reports", "reports.group", "reports.all"),
        ("/_latet/api/v1/ee/reports/export", "reports.group", "reports.all"),
        ("/_latet/api/v1/ee/audit", "audit.read", "audit.read"),
        ("/_latet/api/v1/ee/audit/export", "audit.read", "audit.read"),
        ("/_latet/api/v1/ee/siem", "reports.all", "siem.edit"),
        ("/_latet/api/v1/ee/siem/test", "reports.all", "siem.edit"),
        ("/_latet/api/v1/ee/siem/secret", "reports.all", "siem.edit"),
        ("/_latet/api/v1/ee/siem/skip", "reports.all", "siem.edit"),
    ],
)
def test_each_route_is_under_its_row_of_the_gate(path, read, change):
    rule = rule_for(path)
    assert rule is not None and (rule.read, rule.change) == (read, change)


def test_every_route_is_e3s_manage_route_and_the_administrations_alone_never_the_dashboards():
    routes = {(route.method, route.path): route for route in _bare().routes()}
    assert all(route.kind == "manage" and route.admin for route in routes.values())
    assert {key for key, route in routes.items() if not route.dashboard} == {
        ("GET", "/reports/settings"), ("PUT", "/reports/settings"), ("POST", "/siem/skip")
    }  # fmt: skip
    for (_method, path), route in routes.items():
        assert rule_for(EE + path) is not None and "{" not in path  # paths take no parameter (R1)
        assert route.feature == ("siem" if path.startswith("/siem") else "reports")


# Each action the routes record, and the route it is recorded by: the permission of E3's role table that route's rule
# needs (a GET's read, any other method's change).
RECORDED = {
    "reports.exported": ("GET", "/reports/export"),
    "audit.exported": ("GET", "/audit/export"),
    "reports.changed": ("PUT", "/reports/settings"),
    "siem.changed": ("PUT", "/siem"),
    "siem.secret.changed": ("PUT", "/siem/secret"),
    "siem.tested": ("POST", "/siem/test"),
    "siem.skipped": ("POST", "/siem/skip"),
}


def test_every_action_the_routes_record_is_mapped_to_the_permission_its_route_needs():
    """Task 8's carry: the SIEM names who acts by E3's role table, through ``PERMISSIONS``; each action recorded here
    is in it, with the permission its route's rule asks."""
    noted = set(re.findall(r"\.note\(\s*\"([a-z.-]+)\"", inspect.getsource(api_module)))
    assert noted == set(RECORDED)
    for action, (method, path) in RECORDED.items():
        rule = rule_for(EE + path)
        assert rule is not None and PERMISSIONS[action] == (rule.read if method == "GET" else rule.change), action
    assert {
        "siem.changed",
        "siem.secret.changed",
        "siem.tested",
        "siem.skipped",
    } <= ADMINISTERING  # the security role's
    assert not {"reports.exported", "audit.exported", "reports.changed"} & ADMINISTERING


# --- the limits, with a Valkey of the test's own -------------------------------------------------------------------


class _Bus:
    """``Bus.allow`` counted in memory, or failing as a Valkey that does not answer."""

    def __init__(self, down: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self.down = down

    def allow(self, key: str, limit: int, per: float) -> bool:
        if self.down:
            raise ConnectionError("Error 111 connecting to valkey.acme.lan:6379")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key] <= limit


def _bare(**given):
    """An EventsApi with only what a test gives it (no server's stores)."""
    api = EventsApi.__new__(EventsApi)
    api.__dict__.update({"_exporting": threading.Lock(), "_busy": set(), "_busy_lock": threading.Lock()} | given)
    return api


def _who(identity=EMILY):
    return Who(identity, identity, None, None, None)  # type: ignore[arg-type]


def _output(name, host, path="/in"):
    return SiemOutput(name=name, kind="webhook", url=f"https://{host}{path}")


def test_tests_are_limited_for_each_output_and_for_each_person():
    """I2: one an output every 10 s and 30 an hour, 30 an hour for each person; a Valkey that does not answer is 503 in
    fixed words, never a 500 nor its words."""
    bus = _Bus()
    api = _bare(server=SimpleNamespace(bus=bus))
    api._paced("hook", _who())
    with pytest.raises(Refused) as again:
        api._paced("hook", _who())
    assert again.value.status == 429
    api._paced("splunk", _who())  # another output: its own count
    for i in range(28):
        api._paced(f"out{i}", _who())
    with pytest.raises(Refused) as person:
        api._paced("one-more", _who())  # the person's thirty-first in the hour
    assert person.value.status == 429
    api._paced("one-more-still", _who("u_danielokaf"))  # another person's own
    hourly = _bare(server=SimpleNamespace(bus=_Bus()))
    for i in range(30):
        hourly.server.bus.counts.pop("siem-test:hook", None)  # ten seconds later each time
        hourly._paced("hook", _who(f"u_person{i:04d}"))
    hourly.server.bus.counts.pop("siem-test:hook", None)
    with pytest.raises(Refused) as output:
        hourly._paced("hook", _who("u_person9999"))  # the output's thirty-first in the hour
    assert output.value.status == 429
    with pytest.raises(Refused) as down:
        _bare(server=SimpleNamespace(bus=_Bus(down=True)))._paced("hook", _who())
    assert down.value.status == 503 and "valkey" not in down.value.message


def test_a_person_adds_or_moves_ten_destinations_an_hour_counted_before_the_change():
    """R2-4: each output added, or moved to another scheme, host or port, counts; a path changed does not; past ten
    an hour, 429; a Valkey that does not answer, 503 in fixed words (R3-3)."""
    saved = [_output(f"siem{i}", f"siem{i}.acme.com") for i in range(3)]
    bus = _Bus()
    api = _bare(server=SimpleNamespace(bus=bus))
    api._settings_now = lambda: EnterpriseSettings(siem=SiemSettings(outputs=saved))
    api._counted([*saved[:2], _output("siem2", "siem2.acme.com", "/elsewhere")], _who())  # none moved
    assert bus.counts == {}
    api._counted([_output(f"new{i}", f"new{i}.acme.com") for i in range(9)], _who())  # nine added
    api._counted([_output("siem0", "other.acme.com")], _who())  # one moved: the tenth
    with pytest.raises(Refused) as past:
        api._counted([_output("siem1", "siem1.acme.com:8443")], _who())  # another port: the eleventh
    assert past.value.status == 429 and "latet server siem set" in past.value.message
    api._counted([_output("siem1", "siem1.acme.com:8443")], _who("u_danielokaf"))  # another person's own count
    down = _bare(server=SimpleNamespace(bus=_Bus(down=True)))
    down._settings_now = api._settings_now
    with pytest.raises(Refused) as unanswered:
        down._counted([_output("x", "x.acme.com")], _who())
    assert unanswered.value.status == 503 and "valkey" not in unanswered.value.message


def test_one_export_at_a_time_on_an_instance():
    """Task 9's carry: an export holds up to 500,000 rows in memory; a second one at the same time is 429."""
    api = _bare()
    release = api._export_slot()
    with pytest.raises(Refused) as second:
        api._export_slot()
    assert second.value.status == 429
    release()
    release()  # given back once, whoever calls it again
    api._export_slot()()  # the first one ended: the next goes


def test_one_report_or_export_at_a_time_for_each_person():
    """The dashboard's threads are few: a person's second report or export while theirs is made is 429; another
    person's, and the administration's, go."""
    api = _bare()
    release = api._person_slot(_who())
    with pytest.raises(Refused) as again:
        api._person_slot(_who())
    assert again.value.status == 429 and "yours" in again.value.message
    api._person_slot(_who("u_danielokaf"))()
    token = Who(ADMINISTRATION, "admin:3f9a", None, None, None)  # type: ignore[arg-type]
    api._person_slot(token)
    api._person_slot(token)  # `latet server`'s calls are no person's
    release()
    api._person_slot(_who())()


def test_an_exports_answer_is_the_contracts_shape_made_off_the_event_loop():
    text = "day,group" + chr(13) + chr(10) + '"=1+1",' + "\N{LATIN SMALL LETTER E WITH ACUTE}quipe"
    body = answer_body("latet-report-2027-02-01-2027-03-01.csv", "text/csv", text)
    assert json.loads(body) == {"name": "latet-report-2027-02-01-2027-03-01.csv", "type": "text/csv", "file": text}
    assert body.startswith(b'{"name":"latet-report-') and isinstance(body, bytes)


def _sending(answer, *, slow=None, gone=False):
    """``answer`` sent as Starlette sends it: ``slow``, an event the body's sending waits for (a reader that reads
    slowly); ``gone``: the client left while its body was sent."""

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            if slow is not None:
                await slow.wait()
            if gone:
                raise OSError("the client is gone")

    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    return answer(scope, receive, send)


def test_an_export_holds_what_it_took_until_its_answer_is_sent_or_its_reader_is_gone():
    """Task 10's review: the instance's one export and the person's one report are held until the body is sent, never
    given back while a slow reader still holds hundreds of megabytes; a reader that leaves gives them back, and so
    does an answer never sent."""
    api = _bare()
    release = api._person_slot(_who())

    def taken():
        try:
            api._person_slot(_who())()
        except Refused:
            return True
        return False

    async def slowly():
        slow = asyncio.Event()
        sending = asyncio.ensure_future(_sending(Sent(b"{}", release), slow=slow))
        await asyncio.sleep(0.05)
        held = taken()  # the body is still being sent
        slow.set()
        await sending
        return held

    assert asyncio.run(slowly()) is True and taken() is False  # given back once sent
    gone = api._person_slot(_who())
    with pytest.raises(OSError, match="the client is gone"):
        asyncio.run(_sending(Sent(b"{}", gone), gone=True))
    assert taken() is False  # the reader left: given back
    never = Sent(b"{}", api._person_slot(_who()))
    assert taken() is True
    del never
    gc.collect()
    assert taken() is False  # an answer another took the place of: given back once dropped


def test_a_reader_that_never_reads_holds_an_export_no_longer_than_its_deadline(monkeypatch, caplog):
    """Task 10's re-review: an export's answer is sent within a minute and a second for each megabyte (made shorter
    here); past it the sending ends, said by its kind, and what the export took is given back."""
    assert (api_module.send_deadline(0), api_module.send_deadline(800_000_000)) == (60.0, 860.0)
    monkeypatch.setattr(api_module, "SEND_BASE", 0.2)
    api = _bare()
    release = api_module._both(api._person_slot(_who()), api._export_slot())
    never = asyncio.Event()  # a reader that never reads: never set

    async def stuck():
        await asyncio.wait_for(_sending(Sent(b"{}", release), slow=never), 10)  # the test's own bound, never reached

    with caplog.at_level(logging.WARNING, logger="latet.ee.events.api"):
        asyncio.run(stuck())
    assert not api._exporting.locked() and EMILY not in api._busy
    said = [record.getMessage() for record in caplog.records if record.name == "latet.ee.events.api"]
    assert len(said) == 1 and "not taken" in said[0]


@pytest.mark.parametrize(("route", "made"), [("report_export", "_export"), ("audit_export", "_log_file")])
def test_an_export_left_while_its_file_is_built_holds_what_it_took_until_the_build_ends(route, made):
    """Task 10's re-review: a request cancelled while its file is built in a thread gives back nothing until that job
    ends, as the dashboard's ``_in_pool`` keeps its turn: never a second export's memory beside the first's."""
    api = _bare(server=SimpleNamespace(clock=lambda: 1_803_945_600.0))
    started, go, built = threading.Event(), threading.Event(), threading.Event()

    def export(*args):
        started.set()
        go.wait(10)
        built.set()
        return b"{}", api_module.Shown(every_group=False, log=False, addresses=False)

    api.__dict__.update({made: export, "_producing": lambda feature: None,
                         "_viewer": lambda who: api_module.Viewer(None, "group-only", None)})  # fmt: skip
    pool = ThreadPoolExecutor(2)

    async def run(work, *args):  # as the dashboard's ``_in_pool``: the job goes on when its requester is cancelled
        return await asyncio.shield(asyncio.get_running_loop().run_in_executor(pool, functools.partial(work, *args)))

    async def change(work):
        return work(SimpleNamespace(conn=None, note=lambda *args: None))

    who = Who(EMILY, EMILY, None, run, change)
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"format=csv"})

    async def leave():
        task = asyncio.create_task(getattr(api, route)(request, who))
        assert await asyncio.to_thread(started.wait, 10)  # the file is being built in a thread
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        held = (api._exporting.locked(), EMILY in api._busy)  # the build still runs
        go.set()
        return held

    try:
        assert asyncio.run(leave()) == (True, True)
    finally:
        go.set()
        pool.shutdown(wait=True)  # the build's job has ended
    assert built.is_set() and not api._exporting.locked() and EMILY not in api._busy


def test_a_build_left_before_it_began_never_begins_and_gives_back_at_once():
    given, worked = [], []
    building = api_module._Building(lambda: given.append(True))
    job = building.build(lambda: worked.append(True))
    building.left()  # the requester left while the job waited for a thread
    assert given == [True] and job() is None and worked == []
    ended = api_module._Building(lambda: given.append(True))
    ended.build(lambda: worked.append(True))()
    ended.left()  # left once it had ended: given back at once
    assert given == [True, True] and worked == [True]
