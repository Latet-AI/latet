"""A server's administration API for the Enterprise Edition, and `latet server license`: the license shown, installed
and removed with an administration token, refusals that say why and change nothing, the Enterprise settings set
section by section, each change in the administration log with it; and the license in `latet server status`. The
Enterprise routes are behind the administration's own gate (E0b's): its limiter, its ceiling of refused entries, its
threads and its log."""

import json
import threading

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from latet.cli.agent import MARKERS
from latet.ee.admin import ADMIN_EE
from latet.server.admin import REFUSED_ENTRIES, UNRECORDED
from latet.server.app import create_server_app
from latet.server.machines import LOOKUP_LIMIT

from .licenses import TRIAL, at
from .servers import URL, services

pytestmark = [pytest.mark.server, services]
LASTING = {"expires": "2099-12-31"}
NONE = {"level": 1, "note": None}


@pytest.fixture
def admin(server):
    """The server's application, called with an administration token."""
    client = TestClient(create_server_app(server), base_url=URL)
    client.headers["x-latet-admin"] = server.admin.issue("tests")
    return client


@pytest.fixture
def cli(admin, monkeypatch, tmp_path):
    """`latet server …`, E0c's administration call answered by this test's server; the server named as E0c's own
    command line tests name it, and no certificate fetched (F5)."""
    from latet.cli import server as server_cli
    from latet.server.client import ServerError

    def call(method, path, body=None):
        answer = admin.request(method, f"/_latet/server/v1/admin/{path}", json=body)
        if answer.status_code >= 400:
            raise ServerError(answer.status_code, answer.json().get("error", answer.text))
        return answer.json()

    def unreachable(url):
        raise OSError("no network in the tests")

    monkeypatch.setattr(server_cli, "admin_call", call)
    monkeypatch.setattr("latet.engine.reviewer.certificate.fetch", unreachable)
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", admin.headers["x-latet-admin"])
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))  # no connection of this computer's is read
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config" / "config.yaml"))
    for name in ("LATET_CONNECTION", "LATET_SERVER_FINGERPRINT", "AGENT"):
        monkeypatch.delenv(name, raising=False)
    for markers in MARKERS.values():  # run by you, not by an agent
        for name in markers:
            monkeypatch.delenv(name, raising=False)

    def run(*args):
        import latet.cli.admin  # noqa: F401 - registers the commands
        from latet.cli.main import app

        return CliRunner().invoke(app, ["server", *args])

    return run


def _guesses(n):
    return [{"x-latet-admin": f"latet_adm_{i:043d}"} for i in range(n)]


def test_the_license_is_installed_shown_and_removed_with_an_administration_token(admin, server, licensed):
    assert admin.get(f"{ADMIN_EE}/license").json()["state"] == "none"
    answer = admin.put(f"{ADMIN_EE}/license", json={"license": licensed.make(**LASTING).decode()})
    assert answer.status_code == 200, answer.text
    shown = answer.json()
    assert (shown["license"]["id"], shown["source"]) == ("L-2027-0001", "server") and "changes" not in shown
    [entry] = server.audit.query(action="license.installed")
    assert entry.actor.startswith("admin:") and entry.target == "L-2027-0001"
    assert entry.details == {"users": 300, "expires": "2099-12-31", "trial": False, "status": 200}  # no licensee
    removed = admin.delete(f"{ADMIN_EE}/license").json()
    assert removed["state"] == "none" and removed["removed"] is True
    [entry] = server.audit.query(action="license.removed")
    assert (entry.target, entry.details["users"]) == ("L-2027-0001", 300)
    assert admin.delete(f"{ADMIN_EE}/license").json()["removed"] is False
    assert len(server.audit.query(action="license.removed")) == 1  # nothing removed: no entry says it was


def test_without_an_administration_token_nothing_is_shown_or_changed(server, licensed):
    client = TestClient(create_server_app(server), base_url=URL)
    for headers in ({}, {"x-latet-admin": "latet_adm_" + "x" * 43}):
        assert client.get(f"{ADMIN_EE}/license", headers=headers).status_code == 401
        body = {"license": licensed.make(**LASTING).decode()}
        assert client.put(f"{ADMIN_EE}/license", json=body, headers=headers).status_code == 401
        assert client.get(f"{ADMIN_EE}/settings", headers=headers).status_code == 401
        assert client.delete(f"{ADMIN_EE}/settings/demo", headers=headers).status_code == 401
    server.edition.refresh()
    assert server.edition.licensing.standing().license is None


def test_the_administration_answers_over_https_only_and_never_to_a_web_page(server, admin):
    token = {"x-latet-admin": admin.headers["x-latet-admin"]}
    assert admin.get(f"{ADMIN_EE}/license", headers={"origin": URL}).status_code == 403
    assert admin.get(f"{ADMIN_EE}/license", headers={"sec-fetch-site": "same-origin"}).status_code == 403
    in_clear = TestClient(create_server_app(server), base_url="http://latet.acme.lan")  # E0b's rule: HTTPS only
    assert in_clear.get(f"{ADMIN_EE}/license", headers=token).status_code == 403
    answer = admin.get(f"{ADMIN_EE}/license")
    assert answer.status_code == 200
    assert answer.headers["cache-control"] == "no-store" and answer.headers["x-content-type-options"] == "nosniff"


def test_every_call_of_the_administration_is_in_its_log_reads_and_refusals_too(server, admin):
    admin.get(f"{ADMIN_EE}/license")
    TestClient(create_server_app(server), base_url=URL).get(f"{ADMIN_EE}/license")  # no token
    seen = [(e.action, e.actor.split(":")[0], e.details["status"]) for e in server.audit.query(limit=20)]
    assert ("ee.license.read", "admin", 200) in seen
    assert ("ee.license.read", "unknown", 401) in seen


def test_guesses_on_the_enterprise_routes_share_the_administrations_limit_and_ceiling(server, monkeypatch):
    """H1/F6: the Enterprise routes are behind E0b's own gate, so one address's guesses are limited after
    LOOKUP_LIMIT lookups, as on any administration route, and what they add to the log stays within the server's
    ceiling of refused entries."""
    lookups, looked = [], server.admin.lookup
    monkeypatch.setattr(server.admin, "lookup", lambda secret: lookups.append(secret) or looked(secret))
    client = TestClient(create_server_app(server), base_url=URL)
    statuses = [client.get(f"{ADMIN_EE}/license", headers=guess).status_code for guess in _guesses(200)]
    assert statuses == [401] * LOOKUP_LIMIT + [429] * (200 - LOOKUP_LIMIT)
    assert len(lookups) == LOOKUP_LIMIT
    assert len(server.audit.query(action="ee.license.read", limit=500)) <= REFUSED_ENTRIES


def test_guesses_on_e0bs_routes_and_e1s_together_share_one_limit_and_one_ceiling(server, monkeypatch):
    """M4: one address's budget of lookups, and the server's ceiling of refused entries, are the same for the
    administration's own routes and the Enterprise ones."""
    lookups, looked = [], server.admin.lookup
    monkeypatch.setattr(server.admin, "lookup", lambda secret: lookups.append(secret) or looked(secret))
    client = TestClient(create_server_app(server), base_url=URL)
    paths = ["/_latet/server/v1/admin/status", f"{ADMIN_EE}/license", f"{ADMIN_EE}/settings"]
    statuses = [client.get(paths[n % 3], headers=guess).status_code for n, guess in enumerate(_guesses(200))]
    assert statuses == [401] * LOOKUP_LIMIT + [429] * (200 - LOOKUP_LIMIT)
    assert len(lookups) == LOOKUP_LIMIT
    entries = [e for e in server.audit.query(limit=1000) if e.action in ("status.read", "ee.license.read",
                                                                          "ee.settings.read")]  # fmt: skip
    assert len(entries) <= REFUSED_ENTRIES and {e.action for e in entries} == {
        "status.read",
        "ee.license.read",
        "ee.settings.read",
    }


def test_nobody_is_told_of_a_change_before_it_commits(demo_section, admin, server, licensed, monkeypatch):
    """M4: the copies are dropped and the other instances told once the change and its entry are committed: what a
    connection of its own reads then is the change."""
    seen = []

    def committed_now(what):
        with server.db.connection() as conn:  # not the change's: it sees only what is committed
            if what == "license":
                return conn.execute("SELECT document IS NOT NULL FROM ee_license").fetchone()[0]
            return conn.execute("SELECT count(*) FROM ee_settings WHERE section = 'demo'").fetchone()[0] == 1

    refresh, drop = server.edition.refresh, server.edition.settings_store.drop
    monkeypatch.setattr(
        server.edition, "refresh", lambda: seen.append(("refresh", committed_now("license"))) or refresh()
    )
    monkeypatch.setattr(
        server.edition.settings_store, "drop", lambda: seen.append(("drop", committed_now("demo"))) or drop()
    )
    monkeypatch.setattr(server.bus, "publish", lambda channel, message: seen.append((channel, committed_now(
        "license" if channel == "ee:license" else "demo"))))  # fmt: skip
    assert admin.put(f"{ADMIN_EE}/license", json={"license": licensed.make(**LASTING).decode()}).status_code == 200
    assert seen == [("refresh", True), ("ee:license", True)]
    seen.clear()
    assert admin.put(f"{ADMIN_EE}/settings/demo", json={"level": 3}).status_code == 200
    assert seen == [("drop", True), ("ee:settings", True)]


def test_a_license_removed_elsewhere_meanwhile_lets_no_setting_change(demo_section, instances, licensed):
    """M3: the license is weighed again in the change's own transaction, from its row read there: an instance whose
    copy still says it can manage (another instance removed it, its message lost) changes nothing."""
    from latet.edition import server_context
    from latet.ee.edition import EnterpriseEdition

    from .licenses import Clock

    a, b = instances.open(), instances.open()
    a.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    b.edition.close()
    b.edition = EnterpriseEdition(server_context(b), listen=False, ticker=Clock(0.0))  # its copies never grow old
    assert b.edition.licensing.manageable()  # b's copy, from now on
    a.edition.remove_license(actor="admin:t1")
    assert b.edition.licensing.manageable()  # still b's copy: it heard nothing
    client = TestClient(create_server_app(b), base_url=URL)
    client.headers["x-latet-admin"] = b.admin.issue("tests")
    answer = client.put(f"{ADMIN_EE}/settings/demo", json={"level": 3})
    assert answer.status_code == 403 and answer.json()["code"] == "not-manageable", answer.text
    assert b.audit.query(action="demo.changed") == []
    with b.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM ee_settings").fetchone() == (0,)


def test_a_path_holding_a_token_is_never_an_entrys_target(demo_section, admin, server, licensed, caplog):
    from latet.server.logs import _path

    admin.put(f"{ADMIN_EE}/license", json={"license": licensed.make(**LASTING).decode()})
    pasted = "latet_adm_" + "y" * 43
    with caplog.at_level("INFO"):
        answer = admin.put(f"{ADMIN_EE}/settings/{pasted}", json={"level": 2})
    assert answer.status_code == 404 and pasted not in answer.text
    assert admin.delete(f"{ADMIN_EE}/settings/{pasted}").status_code == 404
    told = repr([(e.action, e.target, e.details) for e in server.audit.query(limit=50)])
    assert "latet_adm_" not in told and "y" * 43 not in told
    # The request log (E0b's) takes a secret of Latet's out of a path it writes.
    lines = [r.getMessage() for r in caplog.records if r.name == "latet.server.requests"]
    assert lines and all("y" * 43 not in line for line in lines), lines
    assert "y" * 43 not in _path({"raw_path": f"{ADMIN_EE}/settings/{pasted}".encode()})


@pytest.mark.parametrize(
    ("text", "code"),
    [("%PDF-1.7 an invoice", "format"), ('{"payload": {"v": 1}, "key": "2019z", "signature": "AAAA"}', "key")],
)
def test_a_wrong_file_is_refused_with_its_reason_and_the_license_stays(admin, server, licensed, text, code):
    admin.put(f"{ADMIN_EE}/license", json={"license": licensed.make(**LASTING).decode()})
    answer = admin.put(f"{ADMIN_EE}/license", json={"license": text})
    assert answer.status_code == 400 and answer.json()["code"] == code
    assert "invoice" not in answer.text  # never the file's text
    assert admin.put(f"{ADMIN_EE}/license", json={"license": "x" * 300_000}).status_code == 413
    assert admin.put(f"{ADMIN_EE}/license", content=b"\xff\xfe not json").status_code == 400
    infinite = admin.put(f"{ADMIN_EE}/license", content=b'{"license": "x", "n": Infinity}')
    assert infinite.status_code == 400 and "code" not in infinite.json()  # refused as a body, before any file
    assert admin.get(f"{ADMIN_EE}/license").json()["license"]["id"] == "L-2027-0001"
    assert len(server.audit.query(action="license.installed")) == 1


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"license": chr(0xD800)}).encode(),  # a lone surrogate, as JSON escapes one: no UTF-8 holds it
        b'{"license": ' + b"[" * 60_000 + b"]" * 60_000 + b"}",  # nested past what Python reads
        bytes([0xFF, 0xFE]) + b" not UTF-8",
        b"[1, 2]",
        b'{"file": "{}"}',
        b"",
    ],
    ids=["lone-surrogate", "nested", "not-utf-8", "not-an-object", "no-license", "empty"],
)
def test_a_body_that_cannot_be_read_as_a_license_is_refused_with_400_never_500(admin, server, licensed, body):
    admin.put(f"{ADMIN_EE}/license", json={"license": licensed.make(**LASTING).decode()})
    answer = admin.put(f"{ADMIN_EE}/license", content=body, headers={"content-type": "application/json"})
    assert answer.status_code == 400, answer.text
    assert admin.get(f"{ADMIN_EE}/license").json()["license"]["id"] == "L-2027-0001"
    assert len(server.audit.query(action="license.installed")) == 1


@pytest.mark.parametrize("failing", ["every entry", "the change's entry"])
def test_a_change_whose_entry_cannot_be_written_is_not_made_nor_answered_as_done(
    demo_section, admin, server, licensed, monkeypatch, failing
):
    """H1: the change and its entry are one transaction; an entry that cannot be written rolls the change back, and
    nobody is told of it. When no entry at all can be written, the answer is withheld (UNRECORDED)."""
    admin.put(f"{ADMIN_EE}/license", json={"license": licensed.make(**LASTING).decode()})
    published, record = [], server.audit.record
    monkeypatch.setattr(server.bus, "publish", lambda channel, message: published.append(channel))

    def refused(*args, **kwargs):
        if failing == "every entry" or kwargs.get("conn") is not None:
            raise OSError("the log cannot be written")
        return record(*args, **kwargs)

    monkeypatch.setattr(server.audit, "record", refused)
    changes = (
        admin.put(f"{ADMIN_EE}/license", json={"license": licensed.make(id="L-2027-0002", **LASTING).decode()}),
        admin.delete(f"{ADMIN_EE}/license"),
        admin.put(f"{ADMIN_EE}/settings/demo", json={"level": 3}),
    )
    for answer in changes:
        assert answer.status_code == 500, answer.text
        if failing == "every entry":
            assert answer.json()["error"] == UNRECORDED
    monkeypatch.setattr(server.audit, "record", record)
    server.edition.refresh()
    assert server.edition.licensing.standing().license.id == "L-2027-0001"  # neither replaced nor removed
    assert server.edition.settings().demo.level == 1 and published == []


def test_the_enterprise_settings_are_changed_section_by_section(demo_section, admin, server, licensed):
    assert admin.get(f"{ADMIN_EE}/settings").json()["demo"] == NONE  # read in every state
    admin.put(f"{ADMIN_EE}/license", json={"license": licensed.make(**LASTING).decode()})  # changed with one (L10)
    answer = admin.put(f"{ADMIN_EE}/settings/demo", json={"level": 3})
    assert answer.status_code == 200 and answer.json() == {"demo": {"level": 3, "note": None}}
    assert admin.put(f"{ADMIN_EE}/settings/teleport", json={}).status_code == 404
    wrong = admin.put(f"{ADMIN_EE}/settings/demo", json={"level": "high"})
    assert wrong.status_code == 400 and "level" in wrong.json()["error"] and "high" not in wrong.text
    assert admin.put(f"{ADMIN_EE}/settings/demo", json=["level"]).status_code == 400
    assert admin.delete(f"{ADMIN_EE}/settings/demo").json() == {"demo": NONE}
    entries = server.audit.query(action="demo.changed")
    assert [(e.action, e.target, e.details) for e in entries] == [
        ("demo.changed", "demo", {"section": "demo", "status": 200})
    ] * 2
    assert all(e.actor.startswith("admin:") for e in entries)


@pytest.mark.parametrize(
    ("section", "body", "command"),
    [
        ("sso", {"issuer": "https://login.acme.com"}, "latet server sign-in set"),
        ("siem", {"outputs": [{"name": "hook", "kind": "webhook", "url": "https://siem.acme.com/in"}]}, "siem set"),
        ("reports", {"people": "named"}, "latet server reports settings"),
    ],
)
def test_the_sections_set_through_their_own_routes_are_never_set_through_the_generic_one(
    admin, server, licensed, section, body, command
):
    """F9 and R11: what the sign-in's, the SIEM's and the reports' own routes check, keep apart and record is never
    set, nor put back to its defaults, through the generic route: 409, naming the command, nothing changed."""
    admin.put(f"{ADMIN_EE}/license", json={"license": licensed.make(**LASTING).decode()})
    before = server.edition.settings().model_dump(mode="json")
    for answer in (
        admin.put(f"{ADMIN_EE}/settings/{section}", json=body),
        admin.delete(f"{ADMIN_EE}/settings/{section}"),
    ):
        assert answer.status_code == 409 and command in answer.json()["error"], answer.text
    server.edition.settings_store.drop()
    assert server.edition.settings().model_dump(mode="json") == before
    assert server.audit.query(action=f"{section}.changed") == []


@pytest.mark.parametrize(
    "body",
    [b"{", b"\xff\xfe{}", b"[" * 10_000 + b"]" * 10_000, b"null", b'"level"', b"", b"3", b'{"level": NaN}'],
    ids=["truncated", "not-utf8", "nested", "null", "text", "empty", "number", "nan"],
)
def test_a_section_body_that_cannot_be_read_is_refused_never_an_error(demo_section, admin, server, licensed, body):
    """L1: whatever the body, 400 and nothing changed, never a 500."""
    admin.put(f"{ADMIN_EE}/license", json={"license": licensed.make(**LASTING).decode()})
    answer = admin.put(f"{ADMIN_EE}/settings/demo", content=body, headers={"content-type": "application/json"})
    assert answer.status_code == 400, answer.text
    assert server.audit.query(action="demo.changed") == []


def test_without_a_license_that_manages_the_settings_are_read_not_changed(demo_section, admin, server):
    """L10/F8: no license (or one paused, or one that cannot be used): 403 ``not-manageable``, nothing changed."""
    assert admin.get(f"{ADMIN_EE}/settings").status_code == 200
    for answer in (
        admin.put(f"{ADMIN_EE}/settings/demo", json={"level": 3}),
        admin.delete(f"{ADMIN_EE}/settings/demo"),
    ):
        assert answer.status_code == 403 and answer.json()["code"] == "not-manageable", answer.text
    assert server.edition.settings().demo.level == 1 and server.audit.query(action="demo.changed") == []


def test_the_servers_status_says_the_license(admin, server, licensed):
    server.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    server.identities.add_person("Emily Carter")
    server.edition.refresh()
    lines = admin.get("/_latet/server/v1/admin/status").json()["edition"]
    license_lines = [line for line in lines if line["title"] == "License"]
    detail = "Acme Corp (L-2027-0001): 300 users, 1 in use, until 2099-12-31"
    assert license_lines == [{"status": "ok", "title": "License", "detail": detail}]


def test_the_servers_status_says_when_an_instance_does_not_hear_the_others(admin, server, monkeypatch):
    """The edition's lines are read in the administration's threads (F11), the L9 line among them."""
    threads, checks = [], server.edition.checks
    monkeypatch.setattr(server.edition, "checks", lambda: threads.append(threading.current_thread().name) or checks())
    server.edition._listener.stop()  # as if its thread had ended
    [line] = admin.get("/_latet/server/v1/admin/status").json()["edition"]
    assert (line["status"], line["title"]) == ("warn", "Enterprise messages")
    assert len(threads) == 1 and threads[0].startswith("latet-admin"), threads


def test_the_servers_status_warns_before_a_trial_ends_and_says_that_it_ended(admin, server, licensed, clock):
    clock.now = at("2026-11-10")
    server.edition.install_license(licensed.make(**TRIAL), actor="admin:t1")
    for moment, status, words in (
        (at("2026-11-25"), "warn", "it ends in 7 days, with no grace"),
        (at("2026-12-01"), "warn", "it ends in 1 day, with no grace"),
        (at("2026-12-03"), "fail", "the trial ended on 2026-12-02; the Enterprise settings can be read, not changed"),
    ):
        clock.now = moment
        server.edition.refresh()
        lines = admin.get("/_latet/server/v1/admin/status").json()["edition"]
        [line] = [line for line in lines if line["title"] == "License"]
        assert (line["title"], line["status"]) == ("License", status) and words in line["detail"], line


def test_after_a_trial_ended_the_settings_are_read_not_changed_and_the_license_still_changes(
    demo_section, admin, server, licensed, clock
):
    clock.now = at("2026-11-10")
    admin.put(f"{ADMIN_EE}/license", json={"license": licensed.make(**TRIAL).decode()})
    assert admin.put(f"{ADMIN_EE}/settings/demo", json={"level": 3}).status_code == 200
    clock.now = at("2026-12-03")
    server.edition.refresh()
    shown = admin.get(f"{ADMIN_EE}/settings")
    assert shown.status_code == 200 and shown.json()["demo"]["level"] == 3, shown.text  # read, as it stood at the end
    for answer in (
        admin.put(f"{ADMIN_EE}/settings/demo", json={"level": 2}),
        admin.delete(f"{ADMIN_EE}/settings/demo"),
    ):
        assert answer.status_code == 403 and answer.json()["code"] == "trial-ended", answer.text
        assert "trial ended on 2026-12-02" in answer.json()["error"]
    assert server.edition.settings().demo.level == 3  # frozen as it was
    assert admin.get(f"{ADMIN_EE}/license").json()["state"] == "trial-ended"
    installed = admin.put(f"{ADMIN_EE}/license", json={"license": licensed.make(id="L-2027-0002", **LASTING).decode()})
    assert installed.json()["state"] == "valid"
    assert admin.put(f"{ADMIN_EE}/settings/demo", json={"level": 2}).status_code == 200


def test_latet_server_license_installs_shows_and_removes(cli, licensed, tmp_path):
    path = tmp_path / "acme.json"
    path.write_bytes(licensed.make(**LASTING))
    installed = cli("license", "install", str(path))
    assert installed.exit_code == 0, installed.output
    assert "Installed on the server: L-2027-0001, Acme Corp, 300 users, until 2099-12-31." in installed.output
    shown = cli("license", "show").output
    assert "users: 300 bought, 0 in use (people and API keys)" in shown and "kept in: the server's database" in shown
    removed = cli("license", "remove", "--yes")
    assert removed.exit_code == 0 and "License removed" in removed.output, removed.output
    assert "`latet server license install <file>` installs one." in cli("license", "show").output
    again = cli("license", "remove")  # none: nothing is asked
    assert again.exit_code == 0 and "No license was installed on the server." in again.output


def test_latet_server_license_install_refuses_a_wrong_file_and_says_why(cli, tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF-1.7")
    result = cli("license", "install", str(path))
    assert result.exit_code == 2 and "Not installed: it is not a license file" in result.output


def test_latet_server_license_remove_is_yours_and_asks_first(cli, licensed, server, monkeypatch):
    server.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    monkeypatch.setenv("CLAUDECODE", "1")
    by_an_agent = cli("license", "remove", "--yes")
    assert by_an_agent.exit_code == 2 and "run it yourself" in by_an_agent.output
    monkeypatch.delenv("CLAUDECODE")
    not_at_a_terminal = cli("license", "remove")
    assert not_at_a_terminal.exit_code == 2 and "run it at your own terminal" in not_at_a_terminal.output
    server.edition.refresh()
    assert server.edition.licensing.standing().license is not None  # neither removed it


def test_latet_server_license_says_a_refusal_in_the_servers_words_escaped(cli, monkeypatch):
    from latet.cli import server as server_cli
    from latet.server.client import ServerError
    from latet.server.connection import NotConnected

    def refusing(method, path, body=None):
        raise ServerError(403, "refused \x1b]0;owned\x07")

    monkeypatch.setattr(server_cli, "admin_call", refusing)
    refused = cli("license", "show")
    assert refused.exit_code == 2 and "Not shown: refused" in refused.output and "\x1b" not in refused.output

    def unnamed(method, path, body=None):
        raise NotConnected("which server? --server https://latet.acme.lan, or LATET_SERVER_URL")

    monkeypatch.setattr(server_cli, "admin_call", unnamed)
    alone = cli("license", "show")
    assert alone.exit_code == 2 and "Not shown: which server?" in alone.output  # F12


def test_latet_server_status_prints_the_license_line(cli, server, licensed):
    server.edition.install_license(licensed.make(**LASTING), actor="admin:t1")
    server.edition.refresh()
    done = cli("status")
    assert "License: Acme Corp (L-2027-0001): 300 users, 0 in use, until 2099-12-31" in done.output, done.output
