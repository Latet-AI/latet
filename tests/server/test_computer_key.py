"""A computer's key (R16, X-I2): a second secret of each computer, issued with its token at enrolment and kept by the
CLI alone. The token that Claude Code holds in its environment keeps the bridge, the space's proxy, ``whoami`` and its
own revocation; a sign-in link to the dashboard and a change of the person's identity also ask for the computer's key
(``x-latet-computer-key``), which the server keeps by a keyed hash bound to its token's row."""

import re

import psycopg
import pytest
from psycopg import sql
from starlette.testclient import TestClient

from latet.server import machines
from latet.server.app import create_server_app
from latet.server.db import Database, SchemaMismatch, check_schema, migrate
from latet.server.tokens import route_key

from .support import URL

pytestmark = pytest.mark.server

SERVER = "/_latet/server/v1"
ENGINE = "/_latet/engine/v1"
KEY = re.compile(r"latet_ck_[A-Za-z0-9_-]{43}")
REQUIRED = {"error": machines.COMPUTER_KEY_WORDS, "reason": "computer_key_required"}


@pytest.fixture
def client(server):
    with TestClient(create_server_app(server), base_url=URL) as found:
        yield found


def _enrolled(server, client, person=None, label="laptop"):
    """A computer of ``person`` (a new person when none is given) enrolled: the person, its token and its key."""
    if person is None:
        person, code = server.identities.add_person("Emily Carter")
    else:
        code = server.identities.code(person.id)
    answer = client.post(f"{SERVER}/enrol", json={"code": code, "label": label})
    assert answer.status_code == 200
    found = answer.json()
    return person, found["token"], found["computer_key"]


def _both(token, key):
    return {"x-latet-token": token, "x-latet-computer-key": key}


def test_an_enrolment_gives_a_key_once_and_the_server_keeps_only_its_keyed_hash(server, client):
    person, token, key = _enrolled(server, client)
    assert KEY.fullmatch(key) and key != token
    with server.db.connection() as conn:
        rows = conn.execute("SELECT id, computer_key FROM tokens WHERE identity_id = %s", (person.id,)).fetchall()
    [(token_id, kept)] = rows
    assert kept and key not in kept and kept != key
    assert kept == server.identities._hash("computer-key", token_id, key)  # keyed, and bound to its token's row
    every = b"".join(row for row in _cells(server.db))
    assert key.encode() not in every and key.removeprefix("latet_ck_").encode() not in every


def _cells(db):
    with db.connection() as conn:
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (db.schema,)
        ).fetchall()
        for (table,) in tables:
            for row in conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))):
                yield b"\n".join(v if isinstance(v, bytes) else str(v).encode() for v in row)


def test_a_sign_in_link_and_an_identity_ask_for_the_computers_key(server, client):
    _, token, key = _enrolled(server, client)
    alone = {"x-latet-token": token}
    identity = {"usernames": ["emily"]}
    for answer in (
        client.post(f"{SERVER}/login-code", headers=alone),
        client.put(f"{SERVER}/identity", json=identity, headers=alone),
        client.post(f"{SERVER}/login-code", headers={**alone, "x-latet-computer-key": "latet_ck_" + "A" * 43}),
        client.put(f"{SERVER}/identity", json=identity, headers={**alone, "x-latet-computer-key": token}),
        client.post(f"{SERVER}/login-code", headers={**alone, "x-latet-computer-key": ""}),
    ):
        assert answer.status_code == 403 and answer.json() == REQUIRED  # never which part was wrong
        assert answer.headers["cache-control"] == "no-store"
    linked = client.post(f"{SERVER}/login-code", headers=_both(token, key))
    assert linked.status_code == 200 and linked.json()["url"].startswith(f"{URL}/_latet/ui/login?code=")
    assert client.put(f"{SERVER}/identity", json=identity, headers=_both(token, key)).json() == {"ok": True}


def test_a_key_given_twice_is_none(server, client):
    _, token, key = _enrolled(server, client)
    headers = [("x-latet-token", token), ("x-latet-computer-key", key), ("x-latet-computer-key", key)]
    answer = client.post(f"{SERVER}/login-code", headers=headers)
    assert answer.status_code == 403 and answer.json() == REQUIRED


def test_another_computers_key_of_the_same_person_opens_nothing(server, client):
    person, laptop, laptop_key = _enrolled(server, client)
    _, desktop, desktop_key = _enrolled(server, client, person, "desktop")
    assert laptop_key != desktop_key
    for token, key in ((laptop, desktop_key), (desktop, laptop_key)):
        answer = client.post(f"{SERVER}/login-code", headers=_both(token, key))
        assert answer.status_code == 403 and answer.json() == REQUIRED
    for token, key in ((laptop, laptop_key), (desktop, desktop_key)):
        assert client.post(f"{SERVER}/login-code", headers=_both(token, key)).status_code == 200


def test_the_token_alone_keeps_the_bridge_the_proxy_whoami_and_its_own_revocation(server, client):
    person, token, key = _enrolled(server, client)
    alone = {"x-latet-token": token}
    assert client.get(f"{SERVER}/whoami", headers=alone).json()["id"] == person.id
    item = {"text": "deploy to billing-api", "kind": "user_text"}
    masked = client.post(f"/u/{route_key(token)}{ENGINE}/mask", json={"session": "s1", "items": [item]}, headers=alone)
    assert masked.status_code == 200
    named = client.post(
        f"/u/{route_key(token)}/_latet/admin/user-name", json={"name": "emily"}, headers=alone
    )  # the CLI's two at an agent's start: a fake for a value, as the bridge's mask gives
    assert named.status_code == 200
    assert client.delete(f"{SERVER}/token", headers=alone).json() == {"ok": True}
    after = client.post(f"{SERVER}/login-code", headers=_both(token, key))
    assert after.status_code == 401  # revoking the token ended its key with it
    assert client.get(f"{SERVER}/whoami", headers=alone).status_code == 401


def test_a_revocation_by_an_administrator_ends_the_key_on_every_instance(server, client):
    person, token, key = _enrolled(server, client)
    assert client.post(f"{SERVER}/login-code", headers=_both(token, key)).status_code == 200
    server.identities.revoke(person.id)
    with server.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM tokens WHERE identity_id = %s", (person.id,)).fetchone() == (0,)
    answer = client.post(f"{SERVER}/login-code", headers=_both(token, key))
    assert answer.status_code == 401  # the instance's memory may hold the token a moment: PostgreSQL is asked


def test_a_token_made_before_computer_keys_is_told_to_connect_again(server, client):
    person, token, key = _enrolled(server, client)
    with server.db.connection() as conn:
        conn.execute("UPDATE tokens SET computer_key = NULL WHERE identity_id = %s", (person.id,))
    for headers in ({"x-latet-token": token}, _both(token, key)):
        answer = client.post(f"{SERVER}/login-code", headers=headers)
        assert answer.status_code == 403
        assert answer.json() == {"error": machines.KEYLESS_WORDS, "reason": "computer_key_missing"}
        assert "connect" in answer.json()["error"]
        assert client.put(f"{SERVER}/identity", json={"usernames": ["e"]}, headers=headers).status_code == 403
    assert client.get(f"{SERVER}/whoami", headers={"x-latet-token": token}).status_code == 200


def test_a_key_hash_copied_onto_another_row_opens_nothing(server, client):
    """Someone who writes the database without the master key cannot move a key of theirs onto another token's row:
    its keyed hash is bound to its own token's id."""
    person, laptop, _ = _enrolled(server, client)
    _, _, desktop_key = _enrolled(server, client, person, "desktop")
    with server.db.connection() as conn:
        rows = dict(conn.execute("SELECT label, computer_key FROM tokens WHERE identity_id = %s", (person.id,)))
        conn.execute("UPDATE tokens SET computer_key = %s WHERE label = 'laptop'", (rows["desktop"],))
    answer = client.post(f"{SERVER}/login-code", headers=_both(laptop, desktop_key))
    assert answer.status_code == 403 and answer.json() == REQUIRED


def test_wrong_keys_count_as_misses_of_their_address_and_spend_no_sign_in_link(server, client, monkeypatch):
    monkeypatch.setattr(machines, "LOOKUP_LIMIT", 3)
    monkeypatch.setattr(machines, "LOGIN_CODES", 2)
    _, token, key = _enrolled(server, client)
    wrong, right = _both(token, "latet_ck_" + "B" * 43), _both(token, key)
    assert [client.post(f"{SERVER}/login-code", headers=wrong).status_code for _ in range(2)] == [403, 403]
    # Neither refused try took one of the person's two sign-in links a minute.
    links = [client.post(f"{SERVER}/login-code", headers=right) for _ in range(3)]
    assert [answer.status_code for answer in links] == [200, 200, 429]
    assert links[2].json()["error"].startswith("too many sign-in links")
    # The third wrong key is the address's last miss; past it, a wrong key waits out the window.
    tries = [client.put(f"{SERVER}/identity", json={}, headers=wrong) for _ in range(3)]
    assert [answer.status_code for answer in tries] == [403, 429, 429]
    assert tries[2].headers["retry-after"] == machines.RETRY_AFTER
    assert tries[2].json()["error"] == "too many credentials refused from this address: wait a minute"
    assert server.limits.refuses(machines.miss_key("testclient"))
    # A valid token with its own key passes all the same: a colleague behind an address a stranger floods.
    assert client.put(f"{SERVER}/identity", json={"usernames": ["emily"]}, headers=right).json() == {"ok": True}
    unknown = client.get(f"{SERVER}/whoami", headers={"x-latet-token": "latet_pat_" + "u" * 43})
    assert unknown.status_code == 429  # what the address presents that no one knows is refused meanwhile


def test_an_api_key_has_no_computer_key_and_no_dashboard(server, client):
    _, secret = server.identities.add_key("Billing CI")
    answer = client.post(f"{SERVER}/login-code", headers={"x-latet-token": secret})
    assert answer.status_code == 403 and answer.json() == {"error": "an API key has no dashboard"}


def test_the_migration_adds_the_key_to_an_e0b_shaped_database(settings, schema):
    """A database of E0b's (three migrations, tokens without a key) takes the fourth: its tokens there keep working
    for their agents, and are told to connect again for the dashboard; ``check_schema`` passes once it is applied."""
    db = Database(settings.database_url, schema=schema, max_size=2)
    try:
        migrate(db)
        with db.connection() as conn:
            conn.execute("ALTER TABLE tokens DROP COLUMN computer_key")
            conn.execute("DELETE FROM schema_migrations WHERE name = 'latet.server.migrations:004_computer_key'")
            conn.execute(
                "INSERT INTO identities (id, kind, name, created) VALUES ('u_emilycarte', 'person', 'Emily', 0)"
            )
            conn.execute(
                "INSERT INTO tokens (id, identity_id, hash, route, label, created, tag)"
                " VALUES ('t_aaaaaaaaaa', 'u_emilycarte', 'h', 'r', 'laptop', 0, 'g')"
            )
        with pytest.raises(SchemaMismatch, match="older than this Latet"):
            check_schema(db)
        assert migrate(db) == ["latet.server.migrations:004_computer_key"]
        check_schema(db)
        with db.connection() as conn:
            assert conn.execute("SELECT computer_key FROM tokens").fetchall() == [(None,)]
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("UPDATE tokens SET computer_key = 'not a hash'")
    finally:
        db.close()
