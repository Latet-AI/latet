"""The administration tokens, in E0b's administration (Task 8): listed with the token in use marked, issued for some
days, for the server's default or for good, never to end after the token that issues them, and revoked, the token in use
and the last one included; every call recorded as the administration records it, refusals too, and neither a secret nor
a label in the log. Each change the administration makes commits only while the token it was made with is still there
and within its day, as the change's own transaction finds it. Served as `latet server run` serves them: E0b's app behind
``Operations`` (``server_app``)."""

import asyncio
import contextlib
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import psycopg
import pytest
import redis
from psycopg import sql
from starlette.testclient import TestClient

from latet.gateway.sigv4 import AwsCredentials
from latet.server import admin as admin_module
from latet.server import egress, machines
from latet.server.app import create_server_app
from latet.server.identities import ADMIN_TOKEN_ID
from latet.server.live import ValkeyAwsSessions
from latet.server.machines import ENROL_BYTES, LABEL_CHARS
from latet.server.operations import server_app

from .conftest import dump
from .support import URL, captured_logs, close_test_server, open_test_server

pytestmark = pytest.mark.server

ADMIN = "/_latet/server/v1/admin"
TOKENS = f"{ADMIN}/admin-tokens"
DAY = 86400.0
FIELDS = {"id", "label", "created", "last_used", "expires", "this", "tampered", "issued_by"}
# An escape, what shows as nothing, a right-to-left override.
ESC, ZWSP, RLO = chr(0x1B), chr(0x200B), chr(0x202E)


@pytest.fixture(autouse=True)
def _egress(monkeypatch):
    """Each test its own lookups under way, and no test resolves ipv4only.arpa."""
    monkeypatch.setattr(egress, "_LOOKUPS", egress._Lookups())
    monkeypatch.setattr(egress, "_NAT64", egress._Nat64(()))


@pytest.fixture
def root(server):
    """A token for good, as `latet server init --never` makes one: it issues tokens of any length (T8.8)."""
    return {"x-latet-admin": server.admin.issue("root", days=None)}


@pytest.fixture
def client(server):
    """The routes as `latet server run` serves them: E0b's app behind Operations, with its lifespan (its ear and beat)."""
    with TestClient(server_app(server), base_url=URL) as found:
        yield found


@pytest.fixture
def bare(server):
    """E0b's app with no lifespan."""
    return TestClient(create_server_app(server), base_url=URL)


@pytest.fixture
def clocked(schema, prefix, clock):
    """An instance whose clock the test moves."""
    found = open_test_server(schema, prefix, clock=clock)
    yield found
    close_test_server(found)


def _id(server, headers):
    return server.admin.check(headers["x-latet-admin"])


def _calls(server, name):
    """The entries of the administration log under ``name``, oldest first."""
    return list(reversed(server.audit.query(action=name)))


def _from(server, where):
    """An asynchronous client of the app with no lifespan, whose calls come from the address ``where``."""
    transport = httpx.ASGITransport(app=create_server_app(server), client=(where, 40000))
    return httpx.AsyncClient(transport=transport, base_url=URL)


def _said(moment):
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


# --- issued, listed, revoked --------------------------------------------------------------------------------------


def test_a_token_is_issued_shown_once_listed_with_the_one_in_use_marked_and_revoked(server, root):
    with captured_logs() as logs, TestClient(server_app(server), base_url=URL) as client:
        made = client.post(TOKENS, json={"label": "Daniel's laptop"}, headers=root)
        assert made.status_code == 201 and set(made.json()) == {"id", "token", "expires"}
        assert made.headers["cache-control"] == "no-store"  # the one answer that holds a secret: kept by no cache
        assert made.headers["x-content-type-options"] == "nosniff"
        issued = made.json()
        assert issued["token"].startswith("latet_adm_") and ADMIN_TOKEN_ID.fullmatch(issued["id"])
        mine = {"x-latet-admin": issued["token"]}
        assert client.get(f"{ADMIN}/status", headers=mine).status_code == 200  # E0b's administration takes it
        seen = client.get(TOKENS, headers=root)
        listed = seen.json()["tokens"]
        assert all(set(t) == FIELDS for t in listed)
        assert [(t["label"], t["this"], t["tampered"]) for t in listed] == [
            ("root", True, False), ("Daniel's laptop", False, False),
        ]  # fmt: skip
        assert [t["this"] for t in client.get(TOKENS, headers=mine).json()["tokens"]] == [False, True]
        with server.db.connection() as conn:
            hashes = [row[0] for row in conn.execute("SELECT hash FROM admin_tokens")]
        for secret in (issued["token"], root["x-latet-admin"], *hashes):
            assert secret not in seen.text  # ids, labels and times: never a secret, never a hash
        assert client.delete(f"{TOKENS}/{issued['id']}", headers=root).json() == {"revoked": True}
        assert client.get(f"{ADMIN}/status", headers=mine).status_code == 401  # stops at once
        assert client.post(TOKENS, json={"label": "again"}, headers=mine).status_code == 401
    root_id = f"admin:{_id(server, root)}"
    done = [(e.actor, e.action, e.target, e.details) for e in reversed(server.audit.query(action="admin-token*"))]
    assert done == [
        (root_id, "admin-token.issued", issued["id"], {"status": 201}),
        (root_id, "admin-tokens.list", None, {"count": 2, "tampered": 0, "status": 200}),
        (f"admin:{issued['id']}", "admin-tokens.list", None, {"count": 2, "tampered": 0, "status": 200}),
        (root_id, "admin-token.revoked", issued["id"], {"status": 200}),
        ("unknown", "admin-token.issue", None, {"status": 401}),
    ]
    assert len(server.audit.query(action="server.started")) == 1  # Operations serves E0b's app, its lifespan too
    told = logs.getvalue() + repr(server.audit.query(limit=100))
    assert issued["token"] not in told and "Daniel" not in told  # never a secret, nor a label, in a log


def test_a_token_lasts_the_servers_default_some_days_or_for_good_and_says_what_issued_it(clocked, clock):
    server = clocked
    root = {"x-latet-admin": server.admin.issue("root", days=None)}
    clock.now += 1  # tokens are listed by when they were made
    client = TestClient(create_server_app(server), base_url=URL)
    default = client.post(TOKENS, json={"label": "the default"}, headers=root).json()
    assert default["expires"] == clock.now + server.settings.admin_token_days * DAY  # exactly, from now (T8.9)
    clock.now += 1
    month = client.post(TOKENS, json={"label": "a month", "days": 30}, headers=root).json()
    assert month["expires"] == clock.now + 30 * DAY
    clock.now += 1
    forever = client.post(TOKENS, json={"label": "the backup", "never": True}, headers=root).json()
    assert forever["expires"] is None
    listed = client.get(TOKENS, headers=root).json()["tokens"]
    root_id = _id(server, root)
    assert [(t["label"], t["expires"], t["issued_by"]) for t in listed] == [
        ("root", None, None),  # issued before any issuer was kept: none
        ("the default", default["expires"], root_id),
        ("a month", month["expires"], root_id),
        ("the backup", None, root_id),
    ]
    assert [(e.target, e.details) for e in _calls(server, "admin-token.issued")] == [
        (default["id"], {"status": 201}), (month["id"], {"days": 30, "status": 201}),
        (forever["id"], {"never": True, "status": 201}),
    ]  # fmt: skip


def test_a_token_issues_tokens_that_end_no_later_than_itself(clocked, clock):
    server = clocked
    month = {"x-latet-admin": server.admin.issue("a month", days=30)}
    ends = server.admin.lookup(month["x-latet-admin"]).expires
    clock.now += DAY
    client = TestClient(create_server_app(server), base_url=URL)
    for body in ({"label": "longer", "days": 30}, {"label": "for good", "never": True}):
        refused = client.post(TOKENS, json=body, headers=month)
        assert refused.status_code == 403
        assert "no later than itself" in refused.json()["error"] and _said(ends) in refused.json()["error"]
    within = client.post(TOKENS, json={"label": "within", "days": 29}, headers=month)
    assert within.status_code == 201 and within.json()["expires"] == clock.now + 29 * DAY == ends
    cut = client.post(TOKENS, json={"label": "the default"}, headers=month)
    assert cut.status_code == 201 and cut.json()["expires"] == ends  # admin_token_days, cut at the caller's end
    assert [e.details for e in _calls(server, "admin-token.issued")] == [
        {"days": 29, "status": 201}, {"cut": True, "status": 201},
    ]  # fmt: skip
    assert [e.details for e in _calls(server, "admin-token.issue")] == [{"status": 403}, {"status": 403}]
    assert sorted(t.label for t in server.admin.list()) == ["a month", "the default", "within"]
    issued = {"x-latet-admin": cut.json()["token"]}  # a token cut so issues none that outlives it in its turn
    assert client.post(TOKENS, json={"label": "x", "never": True}, headers=issued).status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"days": 30},
        {"label": ""},
        {"label": f" {ZWSP}{RLO} "},
        {"label": 7},
        {"label": None},
        {"label": "x" * (LABEL_CHARS + 1)},
        {"label": "Daniel", "days": None},
        {"label": "Daniel", "days": 0},
        {"label": "Daniel", "days": 3651},
        {"label": "Daniel", "days": -1},
        {"label": "Daniel", "days": 30.0},
        {"label": "Daniel", "days": "30"},
        {"label": "Daniel", "days": True},
        {"label": "Daniel", "never": False},
        {"label": "Daniel", "never": "true"},
        {"label": "Daniel", "never": None},
        {"label": "Daniel", "never": 1},
        {"label": "Daniel", "days": 30, "never": True},
        {"label": "Daniel", "expires": 1.0},
        {"label": "my latet_adm_" + "Q" * 43},
        {"label": "LATET_PAT_" + "Q" * 43},
        {"label": "a latet_key_" + "Q" * 43 + " in it"},
        {"label": "latet_ad" + ZWSP + "m_" + "Q" * 43},
        {"label": "latet_ck_" + "Q" * 43},
    ],
    ids=[
        "empty",
        "no label",
        "label empty",
        "label of nothing",
        "label a number",
        "label null",
        "label too long",
        "days null",
        "days zero",
        "days past ten years",
        "days negative",
        "days a float",
        "days a text",
        "days a bool",
        "never false",
        "never a text",
        "never null",
        "never one",
        "days and never",
        "another key",
        "label holding a token",
        "label holding a token in capitals",
        "label holding a key",
        "label holding a token split by nothing",
        "label holding a computer key",
    ],
)
def test_a_body_that_does_not_ask_for_a_token_is_refused_with_no_token_made(server, root, bare, body):
    answer = bare.post(TOKENS, json=body, headers=root)
    assert answer.status_code == 400 and answer.json()["error"]
    for given in body.values():  # the words name the fields, never what they hold
        assert not isinstance(given, str) or len(given) < 8 or given not in answer.text
    assert "Q" * 43 not in answer.text
    assert [t.label for t in server.admin.listed()] == ["root"]  # nothing made
    [entry] = _calls(server, "admin-token.issue")
    assert (entry.actor, entry.details) == (f"admin:{_id(server, root)}", {"status": 400})
    assert "Q" * 43 not in repr(entry) and "Daniel" not in repr(entry)


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b"null", b'"label"', b'{"label": "Dan', b"[" * 10_000],
    ids=["none", "not json", "list", "null", "text", "truncated", "deep"],
)
def test_a_body_that_is_no_json_object_is_refused(server, root, bare, body):
    headers = {**root, "content-type": "application/json"}
    assert bare.post(TOKENS, content=body, headers=headers).status_code == 400
    assert [t.label for t in server.admin.listed()] == ["root"]


def test_a_body_past_its_bound_is_refused_unread_and_one_within_it_is_read_in_the_administrations_threads(
    server, root, bare, monkeypatch
):
    threads, parsed = [], admin_module._parsed
    monkeypatch.setattr(
        admin_module, "_parsed", lambda raw: threads.append(threading.current_thread().name) or parsed(raw)
    )
    json_body = {**root, "content-type": "application/json"}

    def padded(size):  # a body of exactly ``size`` bytes, which asks for nothing (``pad`` is no field of it)
        start, end = b'{"label": "Daniel", "pad": "', b'"}'
        return start + b" " * (size - len(start) - len(end)) + end

    assert len(padded(ENROL_BYTES + 1)) == ENROL_BYTES + 1
    too_long = bare.post(TOKENS, content=padded(ENROL_BYTES + 1), headers=json_body)
    assert too_long.status_code == 413 and threads == []  # nothing parsed
    assert bare.post(TOKENS, content=padded(ENROL_BYTES), headers=json_body).status_code == 400  # read, and refused
    assert bare.post(TOKENS, json={"label": "Daniel"}, headers=root).status_code == 201
    assert len(threads) == 2 and all(name.startswith("latet-admin") for name in threads)  # never on the event loop
    assert [e.details["status"] for e in _calls(server, "admin-token.issue")] == [413, 400]


def test_a_revocation_names_a_token_by_its_id_and_records_no_other_text(server, root, bare, monkeypatch):
    named, row_named = [], server.admin.row_named
    monkeypatch.setattr(server.admin, "row_named", lambda one, **kwargs: named.append(one) or row_named(one, **kwargs))
    for given in ("u_abcdefghij", "latet_adm_" + "Q" * 43, "a_ABCDEFGHIJ", "a_abcdefghijk", "a_1", "a_abcdefghij"):
        answer = bare.delete(f"{TOKENS}/{given}", headers=root)
        assert (answer.status_code, set(answer.json())) == (404, {"error"}), given
    assert named == ["a_abcdefghij"]  # nothing but an id of the server's shape is looked up
    assert bare.delete(f"{TOKENS}/u_abcdefghij").status_code == 401  # refused before its token: no target either
    entries = _calls(server, "admin-token.revoke")
    assert [(e.target, e.details["status"]) for e in entries] == [(None, 404)] * 6 + [(None, 401)]
    assert "u_abcdefghij" not in repr(entries) and "Q" * 43 not in repr(entries)
    assert [t.label for t in server.admin.listed()] == ["root"]


def test_any_token_may_be_revoked_the_one_in_use_and_the_last_one_included(server, root, bare):
    other = bare.post(TOKENS, json={"label": "another"}, headers=root).json()
    assert bare.delete(f"{TOKENS}/{other['id']}", headers=root).json() == {"revoked": True}
    assert bare.delete(f"{TOKENS}/{other['id']}", headers=root).status_code == 404  # gone already
    root_id = _id(server, root)
    assert bare.delete(f"{TOKENS}/{root_id}", headers=root).json() == {"revoked": True}  # its own, and the last
    assert server.admin.listed() == []
    assert bare.get(f"{ADMIN}/status", headers=root).status_code == 401  # the way back: init --new-admin-token
    assert [(e.actor, e.target) for e in _calls(server, "admin-token.revoked")] == [
        (f"admin:{root_id}", other["id"]), (f"admin:{root_id}", root_id),
    ]  # fmt: skip


def test_a_row_that_does_not_hold_is_listed_flagged_and_revoked_by_its_id(server, root, bare):
    made = bare.post(TOKENS, json={"label": "changed by hand"}, headers=root).json()
    with server.db.connection() as conn:  # written without the server's key: made endless, and one made up
        conn.execute(
            "UPDATE admin_tokens SET expires = NULL, label = %s WHERE id = %s", (f"{ESC}[31m{'x' * 300}", made["id"])
        )
        conn.execute(
            "INSERT INTO admin_tokens (id, hash, label, created, expires, tag)"
            " VALUES ('a_forgedforg', 'h', 'forged', 'NaN', NULL, 't')"
        )
    listed = bare.get(TOKENS, headers=root).json()["tokens"]
    flagged = {t["id"]: t for t in listed if t["tampered"]}
    assert set(flagged) == {made["id"], "a_forgedforg"} and not any(t["this"] for t in flagged.values())
    assert flagged[made["id"]]["label"] == "[31m" + "x" * 96  # cleaned and cut as the store keeps a label
    assert flagged["a_forgedforg"]["created"] is None  # a time that is no number is said as none
    assert [t.label for t in server.admin.list()] == ["root"]  # the store's list leaves them out (init, T12.4)
    assert server.admin.check(made["token"]) is None  # it opens nothing
    for token_id in flagged:
        assert bare.delete(f"{TOKENS}/{token_id}", headers=root).json() == {"revoked": True}
    assert [t["id"] for t in bare.get(TOKENS, headers=root).json()["tokens"]] == [_id(server, root)]
    assert [e.details for e in _calls(server, "admin-tokens.list")] == [
        {"count": 3, "tampered": 2, "status": 200}, {"count": 1, "tampered": 0, "status": 200},
    ]  # fmt: skip


def test_a_row_written_by_hand_with_an_id_of_another_shape_is_listed_and_revoked_by_one_of_the_servers(
    server, root, bare
):
    """Its id can hold anything (a slash, a run of 300 letters, an escape, capitals): it is listed by an id of the
    server's shape made from it, the same at every reading, revoked by that id, and recorded by it, never by its own."""
    written = ["not/a/server id", "x" * 300, f"{ESC}[2Jcleared", "a_ABCDEFGHIJ"]
    with server.db.connection() as conn:
        for n, one in enumerate(written):
            conn.execute(
                "INSERT INTO admin_tokens (id, hash, label, created, expires, tag)"
                " VALUES (%s, %s, 'by hand', %s, NULL, 't')",
                (one, f"hand-{n}", n + 1.0),
            )
    seen = bare.get(TOKENS, headers=root)
    flagged = [t["id"] for t in seen.json()["tokens"] if t["tampered"]]
    assert len(set(flagged)) == len(written) and all(ADMIN_TOKEN_ID.fullmatch(one) for one in flagged)
    assert [t["id"] for t in bare.get(TOKENS, headers=root).json()["tokens"] if t["tampered"]] == flagged
    for one in (*written, "[2Jcleared"):
        assert one not in seen.text  # none of its writer's text
    for one in flagged:
        assert bare.delete(f"{TOKENS}/{one}", headers=root).json() == {"revoked": True}
    assert [t["id"] for t in bare.get(TOKENS, headers=root).json()["tokens"]] == [_id(server, root)]
    revoked = _calls(server, "admin-token.revoked")
    assert sorted(e.target for e in revoked) == sorted(flagged)  # recorded by the id it is listed by
    for one in written:
        assert one not in repr(revoked)


def test_the_database_holds_no_secret_and_the_log_no_label(server, root, bare):
    made = [bare.post(TOKENS, json={"label": label}, headers=root).json() for label in ("Daniel's laptop", "backup")]
    assert bare.delete(f"{TOKENS}/{made[0]['id']}", headers=root).status_code == 200
    seen = dump(server.db)
    for secret in (root["x-latet-admin"], *(m["token"] for m in made)):
        assert secret.encode() not in seen
    with server.db.connection() as conn:
        logged = repr(conn.execute("SELECT * FROM audit").fetchall())
    assert "Daniel" not in logged and "backup" not in logged  # in admin_tokens alone, as names are kept (S12)


def test_an_issued_token_works_on_every_instance_and_stops_on_every_one_once_revoked(instances):
    root = {"x-latet-admin": instances[0].admin.issue("root", days=None)}
    one, two = (TestClient(server_app(instance), base_url=URL) for instance in instances)
    made = one.post(TOKENS, json={"label": "laptop"}, headers=root).json()
    mine = {"x-latet-admin": made["token"]}
    assert [c.get(f"{ADMIN}/status", headers=mine).status_code for c in (one, two)] == [200, 200]
    assert two.delete(f"{TOKENS}/{made['id']}", headers=root).json() == {"revoked": True}
    assert [c.get(f"{ADMIN}/status", headers=mine).status_code for c in (one, two)] == [401, 401]


# --- refused as the administration refuses ------------------------------------------------------------------------

ROUTES = {
    "list": ("GET", TOKENS, None, "admin-tokens.list"),
    "issue": ("POST", TOKENS, {"label": "Daniel"}, "admin-token.issue"),
    "revoke": ("DELETE", f"{TOKENS}/a_abcdefghij", None, "admin-token.revoke"),
}


@pytest.mark.parametrize("route", list(ROUTES))
def test_each_route_refuses_as_the_administration_refuses_and_records_it_under_its_own_name(server, root, route):
    method, path, body, name = ROUTES[route]
    late = {"x-latet-admin": server.admin.issue("late", days=-1)}
    client = TestClient(create_server_app(server), base_url=URL)
    in_clear = TestClient(create_server_app(server), base_url="http://latet.acme.lan")
    answers = [
        client.request(method, path, json=body, headers={**root, "origin": URL}),
        client.request(method, path, json=body, headers={**root, "sec-fetch-site": "same-origin"}),
        in_clear.request(method, path, json=body, headers=root),
        client.request(method, path, json=body),
        client.request(method, path, json=body, headers={"x-latet-admin": "latet_adm_" + "0" * 43}),
        client.request(method, path, json=body, headers=late),
    ]
    assert [a.status_code for a in answers] == [403, 403, 403, 401, 401, 401]
    for answer in answers:
        assert answer.headers["cache-control"] == "no-store" and answer.headers["x-content-type-options"] == "nosniff"
    assert [(e.actor, e.target, e.details) for e in _calls(server, name)] == [
        ("unknown", None, {"reason": "page", "status": 403}),
        ("unknown", None, {"reason": "page", "status": 403}),
        ("unknown", None, {"reason": "in clear", "status": 403}),
        ("unknown", None, {"reason": "no token", "status": 401}),
        ("unknown", None, {"status": 401}),
        ("unknown", None, {"reason": "expired", "status": 401}),
    ]
    assert server.audit.query(action="admin-token.refused") == []  # E0b's names, not the plan's (T8.2)
    assert sorted(t.label for t in server.admin.listed()) == ["late", "root"]  # nothing made, nothing revoked


async def test_a_call_shed_while_the_credentials_threads_are_busy_is_counted_and_a_known_token_passes(
    server, root, monkeypatch
):
    async with _from(server, "203.0.113.9") as client:
        assert (await client.get(TOKENS, headers=root)).status_code == 200  # found valid here: known from now on
        monkeypatch.setattr(machines._WAITING, "count", machines.CREDENTIALS_BUSY)  # the threads past their mark
        junk = [
            await client.post(TOKENS, json={"label": "x"}, headers={"x-latet-admin": f"latet_adm_{n:043d}"})
            for n in range(3)
        ]
        assert [a.status_code for a in junk] == [503] * 3
        assert {a.headers["retry-after"] for a in junk} == {machines.QUEUED_RETRY_AFTER}
        mine = await client.post(TOKENS, json={"label": "while busy"}, headers=root)
        assert mine.status_code == 201  # a token known still passes
    assert await asyncio.to_thread(_calls, server, "admin-token.issue") == []  # none written one by one
    await asyncio.to_thread(server.refusals.flush, server.audit)  # what the beat does once a minute
    counted = await asyncio.to_thread(server.audit.query, action="calls.refused")
    assert sum(e.details["count"] for e in counted) == 3 and {e.details["reason"] for e in counted} == {"busy"}


def test_a_call_whose_slot_valkey_cannot_take_is_told_when_to_try_again(server, bare, monkeypatch):
    def silent(*_args):
        raise redis.ConnectionError("Error connecting to valkey://:s3cret-pw@10.0.0.9")

    monkeypatch.setattr(server.bus, "slot", silent)
    for path in (TOKENS, f"{ADMIN}/status"):  # the new routes, and E0b's
        answer = bare.get(path, headers={"x-latet-admin": "latet_adm_" + "0" * 43})
        assert answer.status_code == 503 and answer.headers["retry-after"] == "1"  # as when the threads are busy
        assert "s3cret-pw" not in answer.text


# --- a call's token, checked where its change commits ------------------------------------------------------------


def _revoked_once_found(monkeypatch, server, headers):
    """The token of ``headers`` revoked as soon as the administration has found it valid, before the call's change
    commits: what a revocation on another instance does to a call whose body is still coming, or that waits for a
    thread, a turn or a lock."""
    secret, checked = headers["x-latet-admin"], server.admin.checked

    def then_revoked(given):
        found = checked(given)
        if given == secret and found[0] is not None:
            server.admin.revoke(found[0])
        return found

    monkeypatch.setattr(server.admin, "checked", then_revoked)


# What each table holds that a change writes, every row: the times of a last use, which a note writes when it likes,
# left out.
_KEPT = {
    "identities": "id, kind, name, email, disabled, created, source, external_id",
    "tokens": "id, identity_id, hash, route, label, created, tag",
    "codes": "hash, identity_id, expires, tag",
    "settings": "owner, identity_id, kind, data, changed, changed_by, version",
    "change_counters": "owner, counter",
    "admin_tokens": "id, hash, label, created, expires, tag, issued_by",
}


def _kept(server, without):
    """What the administration's changes write, its log aside, and the administration token ``without``."""
    out = {}
    with server.db.connection() as conn:
        for table, columns in _KEPT.items():
            query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(columns), sql.Identifier(table))
            out[table] = sorted(repr(row) for row in conn.execute(query) if row[0] != without)
    return out


WRITES = [
    "person.add", "key.add", "identity.change", "identity.remove", "tokens.revoke", "code.issue", "key.rotate",
    "settings.change", "shared.change", "admin-token.issue", "admin-token.revoke",
]  # fmt: skip


def _a_write(server, client, root, write):
    """Emily (a person with a computer and a dashboard's session), Billing CI (an API key, its space built) and another
    administration token, made; and the call that makes the write ``write``: method, path, body and the call's name."""
    emily, code = server.identities.add_person("Emily Carter")
    _, laptop, _ = server.identities.enrol(code, "laptop")
    session = server.sign_ins.open(emily.id)
    key, _ = server.identities.add_key("Billing CI")
    path = f"{ADMIN}/identities/{key.id}/settings"
    settings = client.get(path, headers=root).json()["settings"]  # its space built here, before
    other = client.post(TOKENS, json={"label": "another"}, headers=root).json()
    call = {
        "person.add": ("POST", f"{ADMIN}/identities", {"kind": "person", "name": "Daniel Okafor"}, "identity.add"),
        "key.add": ("POST", f"{ADMIN}/identities", {"kind": "key", "name": "Ops CI"}, "identity.add"),
        "identity.change": ("PATCH", f"{ADMIN}/identities/{emily.id}", {"disabled": True}, "identity.change"),
        "identity.remove": ("DELETE", f"{ADMIN}/identities/{emily.id}", None, "identity.remove"),
        "tokens.revoke": ("DELETE", f"{ADMIN}/identities/{emily.id}/tokens", None, "tokens.revoke"),
        "code.issue": ("POST", f"{ADMIN}/identities/{emily.id}/code", None, "code.issue"),
        "key.rotate": ("POST", f"{ADMIN}/identities/{key.id}/rotate", None, "key.rotate"),
        "settings.change": ("PUT", path, {**settings, "profile": "max"}, "settings.change"),
        "shared.change": ("PUT", f"{ADMIN}/shared", {"history": "metadata"}, "shared.change"),
        "admin-token.issue": ("POST", TOKENS, {"label": "made by a revoked token"}, "admin-token.issue"),
        "admin-token.revoke": ("DELETE", f"{TOKENS}/{other['id']}", None, "admin-token.revoke"),
    }[write]
    return SimpleNamespace(emily=emily, laptop=laptop, session=session, key=key, call=call)


@pytest.mark.parametrize("write", WRITES)
def test_a_change_whose_token_was_revoked_before_it_committed_is_not_made(server, client, root, monkeypatch, write):
    made = _a_write(server, client, root, write)
    method, where, body, name = made.call
    mine = {"x-latet-admin": server.admin.issue("mine", days=None)}
    mine_id = _id(server, mine)
    before = _kept(server, mine_id)
    _revoked_once_found(monkeypatch, server, mine)
    answer = client.request(method, where, json=body, headers=mine)
    assert (answer.status_code, answer.json()) == (401, {"error": "unauthorized"})
    assert _kept(server, mine_id) == before  # nothing changed
    emily = made.emily.id
    assert server.identities.by_secret(made.laptop) is not None and server.sign_ins.subject_of(made.session) == emily
    assert server.spaces._spaces[made.key.id].runtime.config.profile == "balanced"  # nothing applied either
    [entry] = _calls(server, name)
    assert (entry.actor, entry.details) == (f"admin:{mine_id}", {"reason": "revoked", "status": 401})


# The first call each change makes to a store, inside its transaction: where the test below pauses it. None: the API
# key's own settings (``PgSettings``), whose own part is the first thing its save writes.
FIRST = {
    "person.add": ("identities", "add_person"), "key.add": ("identities", "add_key"),
    "identity.change": ("identities", "disable"), "identity.remove": ("identities", "remove"),
    "tokens.revoke": ("identities", "revoke"), "code.issue": ("identities", "code"),
    "key.rotate": ("identities", "rotate_key"), "settings.change": (None, "_keep_own"),
    "shared.change": ("shared", "set"), "admin-token.issue": ("admin", "issued"), "admin-token.revoke": ("admin", "revoke"),
}  # fmt: skip


def _lockable(server, token_id):
    """Whether a revocation could take the token's row at once (``FOR UPDATE NOWAIT``): never while a change holds it."""
    try:
        with server.db.transaction() as conn:
            conn.execute("SELECT 1 FROM admin_tokens WHERE id = %s FOR UPDATE NOWAIT", (token_id,))
    except psycopg.errors.LockNotAvailable:
        return False
    return True


@pytest.mark.parametrize("write", WRITES)
def test_a_change_takes_its_tokens_row_before_any_row_of_its_own(server, client, root, monkeypatch, write):
    """Paused at its first call to a store, each change holds its token's row already: a call that waits for its
    token's row (a revocation of it holds that row) holds nothing that another call waits for, so that no two calls
    wait on each other unseen until the statements' bound."""
    made = _a_write(server, client, root, write)
    method, where, body, _ = made.call
    monkeypatch.setattr(server.admin.notes, "note", lambda *_args: None)  # a note's write would lock the row too
    mine = {"x-latet-admin": server.admin.issue("mine", days=None)}
    mine_id = _id(server, mine)
    owner, name = FIRST[write]
    store = server.spaces._spaces[made.key.id].runtime.settings if owner is None else getattr(server, owner)
    first, entered, release = getattr(store, name), threading.Event(), threading.Event()

    def paused(*args, **kwargs):
        entered.set()
        release.wait(10)
        return first(*args, **kwargs)

    monkeypatch.setattr(store, name, paused)
    answers = []
    calling = threading.Thread(target=lambda: answers.append(client.request(method, where, json=body, headers=mine)))
    calling.start()
    try:
        assert entered.wait(10)
        assert not _lockable(server, mine_id)  # its token's row is the change's already
    finally:
        release.set()
        calling.join(30)
    assert len(answers) == 1 and answers[0].status_code in (200, 201)
    assert _lockable(server, mine_id)  # and let go once the change has committed


def test_a_change_whose_token_passed_its_day_before_it_committed_is_not_made(clocked, clock, monkeypatch):
    server = clocked
    day = {"x-latet-admin": server.admin.issue("a day", days=1)}
    checked = server.admin.checked

    def then_late(given):
        found = checked(given)
        clock.now += 2 * DAY  # its day ends while the call waits for its body, or for a thread
        return found

    monkeypatch.setattr(server.admin, "checked", then_late)
    answer = TestClient(create_server_app(server), base_url=URL).post(TOKENS, json={"label": "x"}, headers=day)
    assert (answer.status_code, answer.json()) == (401, {"error": "unauthorized"})
    assert [t.label for t in server.admin.listed()] == ["a day"]
    [entry] = _calls(server, "admin-token.issue")
    assert (entry.actor, entry.details) == (
        f"admin:{server.admin.listed()[0].id}",
        {"reason": "expired", "status": 401},
    )


def test_a_change_whose_tokens_day_ends_while_it_is_made_is_not_made(clocked, clock, monkeypatch):
    """Its token's row taken first, its day judged there and again just before its entry, last in its transaction."""
    server = clocked
    emily, _ = server.identities.add_person("Emily Carter")
    day = {"x-latet-admin": server.admin.issue("a day", days=1)}
    disable = server.identities.disable

    def late(identity_id, **kwargs):  # the token's day ends while the change is being made
        clock.now += 2 * DAY
        return disable(identity_id, **kwargs)

    monkeypatch.setattr(server.identities, "disable", late)
    bare = TestClient(create_server_app(server), base_url=URL)
    answer = bare.patch(f"{ADMIN}/identities/{emily.id}", json={"disabled": True}, headers=day)
    assert (answer.status_code, answer.json()) == (401, {"error": "unauthorized"})
    assert not server.identities.current(emily.id).disabled  # rolled back with the entry it could not write
    [entry] = _calls(server, "identity.change")
    assert entry.details == {"reason": "expired", "status": 401}


def test_a_change_whose_tokens_row_was_changed_by_hand_before_it_committed_is_not_made(server, bare, monkeypatch):
    day = {"x-latet-admin": server.admin.issue("a day", days=1)}
    checked = server.admin.checked

    def then_changed(given):
        found = checked(given)
        with server.db.connection() as conn:  # made endless by hand, without the server's key: it holds no more
            conn.execute("UPDATE admin_tokens SET expires = NULL WHERE id = %s", (found[0],))
        return found

    monkeypatch.setattr(server.admin, "checked", then_changed)
    answer = bare.post(TOKENS, json={"label": "x"}, headers=day)
    assert (answer.status_code, answer.json()) == (401, {"error": "unauthorized"})
    [kept] = server.admin.listed()
    assert kept.tampered and kept.label == "a day"  # nothing made
    [entry] = _calls(server, "admin-token.issue")
    assert (entry.actor, entry.details) == (f"admin:{kept.id}", {"reason": "revoked", "status": 401})


async def test_a_revocation_waits_for_the_change_its_token_is_making(server, root, monkeypatch):
    """The token's row is held from the check to the commit: a revocation that comes meanwhile, on any instance, waits
    for the change, which is made by a token still there, and recorded before the revocation."""
    issued, entered, release = server.admin.issued, threading.Event(), threading.Event()

    def paused(*args, **kwargs):  # inside the change's transaction, its token checked
        entered.set()
        release.wait(10)
        return issued(*args, **kwargs)

    monkeypatch.setattr(server.admin, "issued", paused)
    root_id = _id(server, root)
    async with _from(server, "203.0.113.9") as client:
        making = asyncio.ensure_future(client.post(TOKENS, json={"label": "made meanwhile"}, headers=root))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            revoking = asyncio.ensure_future(asyncio.to_thread(server.admin.revoke, root_id))  # elsewhere, at once
            await asyncio.sleep(0.5)
            assert not revoking.done()  # it waits for the change
        finally:
            release.set()
        assert (await making).status_code == 201
        assert await revoking is True
    [made] = await asyncio.to_thread(_calls, server, "admin-token.issued")
    assert made.actor == f"admin:{root_id}"


async def test_two_tokens_revoking_each_other_at_once_wait_and_one_is_refused(server, monkeypatch):
    one, two = ({"x-latet-admin": server.admin.issue(label, days=None)} for label in ("one", "two"))
    ids = (_id(server, one), _id(server, two))
    held, barrier, waited = server.admin.held, threading.Barrier(2, timeout=1.5), set()

    def together(token_id, *, conn, update=False):
        found = held(token_id, conn=conn, update=update)
        if update and id(conn) not in waited:  # each revocation's first lock taken, both then go on together
            waited.add(id(conn))
            with contextlib.suppress(threading.BrokenBarrierError):  # one waits for the other's lock in PostgreSQL
                barrier.wait()
        return found

    monkeypatch.setattr(server.admin, "held", together)
    async with _from(server, "203.0.113.9") as first, _from(server, "203.0.113.10") as second:
        answers = await asyncio.gather(
            first.delete(f"{TOKENS}/{ids[1]}", headers=one), second.delete(f"{TOKENS}/{ids[0]}", headers=two)
        )
    assert sorted(a.status_code for a in answers) == [200, 401]  # no deadlock, no error: one waits, then is refused
    [kept] = await asyncio.to_thread(server.admin.listed)
    [revoked] = await asyncio.to_thread(_calls, server, "admin-token.revoked")
    [refused] = await asyncio.to_thread(_calls, server, "admin-token.revoke")
    assert revoked.actor == f"admin:{kept.id}" and revoked.target != kept.id
    assert (refused.actor, refused.details) == (f"admin:{revoked.target}", {"reason": "revoked", "status": 401})


def _paused_saving(monkeypatch, runtime):
    """An API key's settings saved from now on wait, before their own part is written, for the release: the event that
    says one waits, and the release."""
    keep_own, saving, release = runtime.settings._keep_own, threading.Event(), threading.Event()

    def paused(own):
        saving.set()
        release.wait(30)
        return keep_own(own)

    monkeypatch.setattr(runtime.settings, "_keep_own", paused)
    return saving, release


async def test_a_revocation_waits_for_the_api_key_settings_its_token_is_saving(server, monkeypatch):
    """An API key's settings are applied, and approved, in transactions of their own: the saving token's row is held
    from its check, before they are applied, until their approval has committed, so a revocation meanwhile, on any
    instance, waits, and the settings are recorded before it."""
    server.listening.set()  # spaces are built here
    try:
        key, _ = server.identities.add_key("Billing CI")
        runtime = await server.spaces.runtime(key)
        saver = {"x-latet-admin": server.admin.issue("saver", days=None)}
        other = {"x-latet-admin": server.admin.issue("other", days=None)}
        saving, release = _paused_saving(monkeypatch, runtime)
        settings = {**runtime.config.model_dump(mode="json"), "profile": "max"}
        async with _from(server, "203.0.113.9") as mine, _from(server, "203.0.113.10") as theirs:
            saved = asyncio.ensure_future(
                mine.put(f"{ADMIN}/identities/{key.id}/settings", json=settings, headers=saver)
            )
            try:
                assert await asyncio.to_thread(saving.wait, 10)  # checked, held, and nothing written yet
                revoking = asyncio.ensure_future(theirs.delete(f"{TOKENS}/{_id(server, saver)}", headers=other))
                await asyncio.sleep(0.5)
                assert not revoking.done()  # it waits for the settings to be kept and approved
            finally:
                release.set()
            assert (await saved).status_code == 200
            assert (await revoking).json() == {"revoked": True}
        actions = [e.action for e in reversed(await asyncio.to_thread(server.audit.query))]
        assert actions.index("settings.changed") < actions.index("admin-token.revoked")
    finally:
        await server.spaces.close()


@pytest.mark.parametrize("change", ["remove", "rotate"])
async def test_a_key_changed_by_a_token_revoked_meanwhile_while_its_settings_are_saved_ends_well_within_its_time(
    server, monkeypatch, change
):
    """Two tokens: one saves an API key's settings, its row held from the check to the approval, and revokes the other,
    which removes (or rotates) that key meanwhile. The revocation takes the other's row and waits for the saver's; the
    removal waits for its own token's row, which the revocation holds. Taken before any other row, it holds none of
    the key's, which the save writes next: all three end at once, the removal refused, never waiting on each other
    unseen until the statements' bound (60 s)."""
    server.listening.set()
    release = threading.Event()
    try:
        key, key_secret = server.identities.add_key("Billing CI")
        runtime = await server.spaces.runtime(key)
        given = [server.admin.issue(label, days=None) for label in ("one", "two")]
        tokens = {server.admin.check(secret): {"x-latet-admin": secret} for secret in given}
        low, high = sorted(tokens)  # a revocation takes the lower id's row first
        saving, release = _paused_saving(monkeypatch, runtime)
        held, revocation_holds, change_asks = server.admin.held, threading.Event(), threading.Event()

        def watched(token_id, *, conn, update=False):
            if token_id == low and not update:
                change_asks.set()  # the change asks for its token's row
            found = held(token_id, conn=conn, update=update)
            if token_id == low and update:
                revocation_holds.set()  # the revocation holds that row, and asks for the saver's next
            return found

        monkeypatch.setattr(server.admin, "held", watched)
        one = f"{ADMIN}/identities/{key.id}"
        method, where = ("DELETE", one) if change == "remove" else ("POST", f"{one}/rotate")
        settings = {**runtime.config.model_dump(mode="json"), "profile": "max"}
        started = time.monotonic()
        async with (
            _from(server, "203.0.113.9") as first,
            _from(server, "203.0.113.10") as second,
            _from(server, "203.0.113.11") as third,
        ):
            saved = asyncio.ensure_future(first.put(f"{one}/settings", json=settings, headers=tokens[high]))
            assert await asyncio.to_thread(saving.wait, 10)  # the saver's row held, the key's settings not written yet
            revoking = asyncio.ensure_future(second.delete(f"{TOKENS}/{low}", headers=tokens[high]))
            assert await asyncio.to_thread(revocation_holds.wait, 10)
            changing = asyncio.ensure_future(third.request(method, where, headers=tokens[low]))
            assert await asyncio.to_thread(change_asks.wait, 10)
            await asyncio.sleep(0.3)  # the change as far as it goes
            release.set()
            answers = await asyncio.wait_for(asyncio.gather(saved, revoking, changing), 20)
        assert time.monotonic() - started < 15  # well within the statements' 60 s: nothing waited unseen
        assert [a.status_code for a in answers] == [200, 200, 401]
        assert (
            await asyncio.to_thread(server.identities.by_secret, key_secret) is not None
        )  # neither removed nor rotated
        [refused] = await asyncio.to_thread(_calls, server, "identity.remove" if change == "remove" else "key.rotate")
        assert (refused.actor, refused.details) == (f"admin:{low}", {"reason": "revoked", "status": 401})
    finally:
        release.set()
        await server.spaces.close()


def test_a_revocation_of_no_token_by_a_token_that_ended_ends_nothing_of_the_persons(server, client, monkeypatch):
    """A person with no computer (no token to revoke), or a token id that is none of hers: a call whose token ended
    before its change ends neither her dashboard's sessions nor her AWS sessions (they end only with a change made)."""
    emily, _ = server.identities.add_person("Emily Carter")
    session = server.sign_ins.open(emily.id)
    aws = ValkeyAwsSessions(server.bus, server.master, emily.id)
    aws_session = aws.add_session(AwsCredentials("AKIAEXAMPLE", "secret-of-emily", None))
    for query in ("", "?token=t_abcdefghij"):  # every token of hers, or one that is none of hers
        mine = {"x-latet-admin": server.admin.issue("mine", days=None)}
        _revoked_once_found(monkeypatch, server, mine)
        answer = client.delete(f"{ADMIN}/identities/{emily.id}/tokens{query}", headers=mine)
        assert (answer.status_code, answer.json()) == (401, {"error": "unauthorized"})
    assert server.sign_ins.subject_of(session) == emily.id and aws.has_session(aws_session)  # nothing of hers ended
    assert [e.details for e in _calls(server, "tokens.revoke")] == [{"reason": "revoked", "status": 401}] * 2
