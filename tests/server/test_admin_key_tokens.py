"""An API key's tokens through the administration: another token added under a name with the permissions it is given
(scan, mask, unmask), the tokens listed with both, and a rotation that names the token it replaces when the key has
several. Each change is recorded in the administration log with its permissions and never a secret or a name; a person's
tokens are their computers', and a body that is wrong says which field."""

import threading

import psycopg
import pytest
from starlette.testclient import TestClient

from latet.server.app import create_server_app
from latet.server.identities import IDENTITY_ID, LABEL_LENGTH, TOKEN_ID
from latet.server.tokens import route_key

from .support import URL

pytestmark = pytest.mark.server

ADMIN = "/_latet/server/v1/admin"
EVERY = ["mask", "scan", "unmask"]
CANARY = "Orchid Ledger 7731"  # a token's name, a caller's text: in no entry of the log and no event


@pytest.fixture
def admin(server):
    return {"x-latet-admin": server.admin.issue("first administrator")}


@pytest.fixture
def client(server):
    with TestClient(create_server_app(server), base_url=URL) as found:
        yield found


def _key(client, admin, name="Billing CI"):
    """An API key made by the administration, and the path of its identity."""
    made = client.post(f"{ADMIN}/identities", json={"kind": "key", "name": name}, headers=admin).json()
    return made, f"{ADMIN}/identities/{made['identity']['id']}"


def _added(client, admin, one, name, permissions):
    return client.post(f"{one}/tokens", json={"name": name, "permissions": permissions}, headers=admin)


def _listed(client, admin, one):
    return client.get(f"{one}/tokens", headers=admin).json()["tokens"]


def test_add_a_mask_only_token(server, client, admin):
    made, one = _key(client, admin)
    added = _added(client, admin, one, "logs", ["mask"])
    assert added.status_code == 201
    body = added.json()
    token = body["token"]
    assert (token["name"], token["permissions"]) == ("logs", ["mask", "scan"])  # mask brings scan, sorted
    assert TOKEN_ID.fullmatch(token["id"]) and isinstance(token["created"], float)
    assert body["key"].startswith("latet_key_")
    assert not {"base", "anthropic", "openai"} & set(body)  # it may not call a model through Latet: no address
    listed = sorted(_listed(client, admin, one), key=lambda t: t["name"])
    assert [(t["name"], t["permissions"]) for t in listed] == [("key", EVERY), ("logs", ["mask", "scan"])]
    assert [t["label"] for t in listed] == ["key", "logs"]  # what the list said before it said "name"
    assert listed[1]["id"] == token["id"] and listed[1]["tampered"] is False
    grant = server.identities.grant_by_secret(body["key"])  # the secret opens exactly what was asked
    assert grant.identity.id == made["identity"]["id"] and grant.token_id == token["id"]
    assert grant.permissions == {"mask", "scan"}
    [entry] = server.audit.query(action="token.added")
    assert entry.target == made["identity"]["id"]
    assert entry.details == {"token": token["id"], "permissions": ["mask", "scan"], "status": 201}
    assert body["key"] not in repr(entry) and route_key(body["key"]) not in repr(entry)


def test_a_revoked_tokens_entry_says_what_it_could_do(server, client, admin):
    """The final review's M10 (the spec's line 97): ``token.revoked`` names the token and what it could do, as
    ``token.added`` does, so that the log says which permissions left with it; never its name or its secret."""
    made, one = _key(client, admin)
    added = _added(client, admin, one, "logs", ["mask"]).json()["token"]
    [first] = [t for t in _listed(client, admin, one) if t["name"] == "key"]
    for token, permissions in ((added["id"], ["mask", "scan"]), (first["id"], EVERY)):
        assert client.delete(f"{one}/tokens", params={"token": token}, headers=admin).json() == {"revoked": 1}
        entry = server.audit.query(action="token.revoked")[0]
        assert entry.target == made["identity"]["id"]
        assert entry.details == {"count": 1, "token": token, "permissions": permissions, "status": 200}
    # A token no longer there: nothing revoked, nothing recorded as revoked.
    assert client.delete(f"{one}/tokens", params={"token": added["id"]}, headers=admin).json() == {"revoked": 0}
    assert len(server.audit.query(action="token.revoked")) == 2


@pytest.mark.parametrize(
    ("permissions", "granted"),
    [
        (["mask", "unmask"], EVERY),
        (["scan", "mask", "unmask"], EVERY),
        (["unmask"], ["unmask"]),
        (["scan"], ["scan"]),
        (["scan", "unmask"], ["scan", "unmask"]),
    ],
)
def test_an_added_token_gets_no_address(client, admin, permissions, granted):
    """Ruling T13b: a token added to a key calls the applications API alone, whatever it may do, mask and unmask
    included, so the answer gives it no address and its listing says it serves no agent; the key's first token has its
    own (the same ones a new key gets), which a rotation of it gives again."""
    _, one = _key(client, admin)
    body = _added(client, admin, one, "gateway", permissions).json()
    assert body["token"]["permissions"] == granted
    assert not {"base", "anthropic", "openai"} & set(body)
    listed = {t["name"]: t for t in _listed(client, admin, one)}
    assert (listed["key"]["agents"], listed["gateway"]["agents"]) == (True, False)
    rotated = client.post(f"{one}/rotate", json={"token": listed["key"]["id"]}, headers=admin).json()
    base = f"{URL}/u/{route_key(rotated['key'])}"
    assert (rotated["base"], rotated["anthropic"], rotated["openai"]) == (
        base,
        f"{base}/anthropic",
        f"{base}/openai/v1",
    )
    again = client.post(f"{one}/rotate", json={"token": body["token"]["id"]}, headers=admin).json()
    assert again["permissions"] == granted and not {"base", "anthropic", "openai"} & set(again)


_WORDS = "a JSON object: name and permissions"
_BAD = [
    pytest.param({"permissions": ["scan"]}, "name", id="no name"),
    pytest.param({"name": None, "permissions": ["scan"]}, "name", id="a name that is null"),
    pytest.param({"name": 5, "permissions": ["scan"]}, "name", id="a name that is a number"),
    pytest.param({"name": ["logs"], "permissions": ["scan"]}, "name", id="a name that is a list"),
    pytest.param({"name": " \x1b​ ", "permissions": ["scan"]}, "name", id="a name left empty once cleaned"),
    pytest.param({"name": "Q" * (LABEL_LENGTH + 1), "permissions": ["scan"]}, "name", id="a name past the limit"),
    pytest.param({"name": "logs"}, "permissions", id="no permissions"),
    pytest.param({"name": "logs", "permissions": []}, "permissions", id="empty permissions"),
    pytest.param({"name": "logs", "permissions": ["read"]}, "permissions", id="a permission that is none of Latet's"),
    pytest.param({"name": "logs", "permissions": ["MASK"]}, "permissions", id="a permission in capitals"),
    pytest.param({"name": "logs", "permissions": ["scan", "CANARY-perm"]}, "permissions", id="one good, one not"),
    pytest.param({"name": "logs", "permissions": "mask"}, "permissions", id="permissions that are a text"),
    pytest.param({"name": "logs", "permissions": {"mask": True}}, "permissions", id="permissions that are an object"),
    pytest.param({"name": "logs", "permissions": [1]}, "permissions", id="a permission that is a number"),
    pytest.param({"name": "logs", "permissions": [["mask"]]}, "permissions", id="a permission that is a list"),
    pytest.param({"name": "logs", "permissions": None}, "permissions", id="permissions that are null"),
    pytest.param({"name": "logs", "permissions": ["scan"], "tag": "x"}, "name", id="a field that is none of its own"),
]


@pytest.mark.parametrize(("given", "field"), _BAD)
def test_bad_bodies(server, client, admin, given, field):
    _, one = _key(client, admin)
    answer = client.post(f"{one}/tokens", json=given, headers=admin)
    assert answer.status_code == 400
    words = answer.json()["error"]
    assert field in words
    assert "CANARY-perm" not in words and "Q" * 5 not in words  # what was given is never quoted back
    assert [t["name"] for t in _listed(client, admin, one)] == ["key"]  # nothing made
    assert not server.audit.query(action="token.added")
    [refused] = server.audit.query(action="token.add")
    assert refused.details["status"] == 400


@pytest.mark.parametrize("content", [b"", b"{", b"[]", b'"logs"', b"null", b"5"])
def test_a_body_that_is_no_object_is_refused_naming_both_fields(client, admin, content):
    _, one = _key(client, admin)
    answer = client.post(f"{one}/tokens", content=content, headers=admin)
    assert answer.status_code == 400 and answer.json() == {"error": _WORDS}


def test_the_names_limit_is_the_labels_and_nothing_is_cut(client, admin):
    """A name is kept as it is given, once its invisible characters are taken out: up to the label's length, and a
    longer one is refused, never cut."""
    _, one = _key(client, admin)
    full = "n" * LABEL_LENGTH
    invisible = "​" * 50 + "\x1b" * 50  # nothing of it is kept, so nothing of it counts
    ok = _added(client, admin, one, full[:60] + invisible + full[60:], ["scan"])
    assert ok.status_code == 201 and ok.json()["token"]["name"] == full
    assert _added(client, admin, one, full + "n", ["scan"]).status_code == 400
    assert sorted(t["name"] for t in _listed(client, admin, one)) == sorted(["key", full])


def test_a_person_gets_409(server, client, admin):
    emily = client.post(f"{ADMIN}/identities", json={"kind": "person", "name": "Emily Carter"}, headers=admin)
    person = emily.json()["identity"]["id"]
    answer = _added(client, admin, f"{ADMIN}/identities/{person}", "logs", ["scan"])
    assert answer.status_code == 409 and answer.json() == {"error": "a person's tokens come from their computers"}
    assert server.identities.tokens(person) == []
    assert not server.audit.query(action="token.added")


@pytest.mark.parametrize("identity", ["u_aaaaaaaaaa", "not-an-identity", "latet_key_" + "z" * 43])
def test_no_such_identity_gets_404_and_no_token(server, client, admin, identity):
    answer = _added(client, admin, f"{ADMIN}/identities/{identity}", "logs", ["scan"])
    assert answer.status_code == 404 and answer.json() == {"error": "there is no such identity"}
    assert not server.audit.query(action="token.added")
    if not IDENTITY_ID.fullmatch(identity):  # text of any other shape (a secret pasted in the path) is no one's
        assert all(identity not in repr(entry) for entry in server.audit.query())


def test_no_name_and_no_secret_reaches_the_log_or_an_event(server, client, admin, monkeypatch):
    told = []
    monkeypatch.setattr(server.bus, "publish", lambda channel, message, *a, **k: told.append((channel, message)))
    _, one = _key(client, admin)
    told.clear()
    body = _added(client, admin, one, CANARY, ["mask", "unmask"]).json()
    rotated = client.post(f"{one}/rotate", json={"token": body["token"]["id"]}, headers=admin).json()
    assert CANARY in str(_listed(client, admin, one))  # the list says it: that is what it is for
    shown = repr([(e.action, e.target, e.details) for e in server.audit.query()]) + repr(told)
    for secret in (CANARY, body["key"], rotated["key"], route_key(body["key"]), route_key(rotated["key"])):
        assert secret not in shown
    assert "token.added" in shown and "key.rotated" in shown


def test_rotate_with_several_tokens_names_them(server, client, admin):
    made, one = _key(client, admin)
    added = _added(client, admin, one, "logs", ["mask"]).json()
    first = next(t for t in _listed(client, admin, one) if t["name"] == "key")
    before = client.post(f"{one}/rotate", headers=admin)
    assert before.status_code == 409
    assert before.json() == {"error": "this key has 2 tokens: name the one to rotate"} and "2 tokens" in before.text
    for secret in (made["key"], added["key"]):  # nothing changed
        assert server.identities.grant_by_secret(secret) is not None
    rotated = client.post(f"{one}/rotate", json={"token": added["token"]["id"]}, headers=admin)
    assert rotated.status_code == 200
    body = rotated.json()
    assert (body["name"], body["permissions"]) == ("logs", ["mask", "scan"]) and body["key"] != added["key"]
    assert not {"base", "anthropic", "openai"} & set(body)  # still a token with no address
    assert server.identities.grant_by_secret(added["key"]) is None  # the one named stopped
    assert server.identities.grant_by_route(route_key(added["key"])) is None
    assert server.identities.grant_by_secret(made["key"]).permissions == set(EVERY)  # the other did not change
    assert server.identities.grant_by_secret(body["key"]).permissions == {"mask", "scan"}
    now = {t["name"]: t for t in _listed(client, admin, one)}
    assert set(now) == {"key", "logs"} and now["key"]["id"] == first["id"]
    assert now["logs"]["id"] != added["token"]["id"] and now["logs"]["permissions"] == ["mask", "scan"]
    assert body["token"] == now["logs"]["id"]  # the answer names the new token, by its id
    [entry] = server.audit.query(action="key.rotated")  # and the log both: the one that stands and the one retired
    assert entry.details == {"token": now["logs"]["id"], "replaced": added["token"]["id"], "status": 200}
    assert sorted(e.details["status"] for e in server.audit.query(action="key.rotate")) == [409]  # the refused call


def test_the_log_says_which_token_a_rotation_retired(server, client, admin):
    """``replaced`` is the id of the token the rotation took away: the one named, the only one a key has (named by
    no one), and none when the key had none left."""
    made, one = _key(client, admin)
    [first] = _listed(client, admin, one)
    one_token = client.post(f"{one}/rotate", headers=admin).json()  # a key with one token: it names none
    [now] = _listed(client, admin, one)
    assert one_token["token"] == now["id"] != first["id"]
    client.delete(f"{one}/tokens", headers=admin)
    none_left = client.post(f"{one}/rotate", headers=admin).json()
    [last] = _listed(client, admin, one)
    assert none_left["token"] == last["id"]
    entries = list(reversed(server.audit.query(action="key.rotated")))
    assert [e.details for e in entries] == [
        {"token": now["id"], "replaced": first["id"], "status": 200},
        {"token": last["id"], "replaced": None, "status": 200},
    ]
    shown = repr([e.details for e in entries])
    for secret in (made["key"], one_token["key"], none_left["key"]):  # ids only: no secret in the log
        assert secret not in shown and route_key(secret) not in shown


def test_the_rotation_and_its_entry_are_one_transaction(server, client, admin, monkeypatch):
    """When the entry cannot be written the rotation is not made: the token it would have replaced still works, and no
    new row is there."""
    made, one = _key(client, admin)
    added = _added(client, admin, one, "logs", ["mask"]).json()
    before = sorted((t["id"], t["name"], t["permissions"]) for t in _listed(client, admin, one))
    record = server.audit.record

    def failing(actor, action, *args, **kwargs):
        if action == "key.rotated":
            raise psycopg.OperationalError("the log's chain is out of reach")
        return record(actor, action, *args, **kwargs)

    monkeypatch.setattr(server.audit, "record", failing)
    answer = client.post(f"{one}/rotate", json={"token": added["token"]["id"]}, headers=admin)
    assert answer.status_code == 500 and "key" not in answer.json()  # no new secret leaves
    for secret in (made["key"], added["key"]):  # the token named still works, and so does the other
        assert server.identities.grant_by_secret(secret) is not None
    assert sorted((t["id"], t["name"], t["permissions"]) for t in _listed(client, admin, one)) == before  # no new row
    assert not server.audit.query(action="key.rotated")


def test_rotating_the_only_token_answers_its_name_and_permissions(server, client, admin):
    made, one = _key(client, admin)
    full = client.post(f"{one}/rotate", headers=admin).json()
    assert (full["name"], full["permissions"]) == ("key", EVERY) and TOKEN_ID.fullmatch(full["token"])
    assert full["base"] == f"{URL}/u/{route_key(full['key'])}" and full["openai"].endswith("/openai/v1")
    assert server.identities.grant_by_secret(made["key"]) is None
    restricted = _added(client, admin, one, "audit", ["scan"]).json()
    for token in _listed(client, admin, one):
        if token["name"] == "key":
            client.delete(f"{one}/tokens?token={token['id']}", headers=admin)
    again = client.post(f"{one}/rotate", headers=admin).json()  # one token left, a restricted one: kept as it is
    assert (again["name"], again["permissions"]) == ("audit", ["scan"]) and not {"base", "openai"} & set(again)
    assert server.identities.grant_by_secret(restricted["key"]) is None
    assert server.identities.grant_by_secret(again["key"]).permissions == {"scan"}


def test_a_key_with_no_token_left_gets_a_new_full_one(server, client, admin):
    made, one = _key(client, admin)
    client.delete(f"{one}/tokens", headers=admin)
    assert _listed(client, admin, one) == []
    body = client.post(f"{one}/rotate", headers=admin).json()
    assert (body["name"], body["permissions"]) == ("key", EVERY) and "base" in body
    assert server.identities.grant_by_secret(body["key"]).identity.id == made["identity"]["id"]


def test_a_token_that_is_not_the_keys_is_a_404_with_the_servers_words(server, client, admin):
    _, one = _key(client, admin)
    other, theirs = _key(client, admin, "Other CI")
    [their_token] = _listed(client, admin, theirs)
    _added(client, admin, one, "logs", ["mask"])  # two tokens, so that the rotation must name one
    for token in ("t_aaaaaaaaaa", their_token["id"]):
        answer = client.post(f"{one}/rotate", json={"token": token}, headers=admin)
        assert answer.status_code == 404
        assert answer.json() == {"error": "there is no such token of this API key"}
        assert token not in answer.text
    assert server.identities.grant_by_secret(other["key"]).identity.id == other["identity"]["id"]
    assert sorted(e.details["status"] for e in server.audit.query(action="key.rotate")) == [404, 404]
    assert not server.audit.query(action="key.rotated")


def test_a_token_whose_row_does_not_hold_is_a_404_that_says_what_to_do(server, client, admin):
    _, one = _key(client, admin)
    added = _added(client, admin, one, "audit", ["scan"]).json()
    with server.db.connection() as conn:  # written by hand, without the server's key
        conn.execute("UPDATE tokens SET permissions = NULL WHERE id = %s", (added["token"]["id"],))
    server.identities.forget()
    listed = {t["name"]: t for t in _listed(client, admin, one)}
    assert listed["audit"]["tampered"] is True and listed["audit"]["permissions"] == []
    answer = client.post(f"{one}/rotate", json={"token": added["token"]["id"]}, headers=admin)
    assert answer.status_code == 404
    assert "revoke it" in answer.json()["error"] and "another token" in answer.json()["error"]
    assert not server.audit.query(action="key.rotated")
    assert server.identities.grant_by_secret(added["key"]) is None  # and it was not made whole


@pytest.mark.parametrize(
    "given",
    [
        {"token": "latet_key_" + "z" * 43},
        {"token": "t_short"},
        {"token": 5},
        {"token": ["t_aaaaaaaaaa"]},
        {"token": "t_aaaaaaaaaa", "all": True},
        {"tokens": "t_aaaaaaaaaa"},
    ],
)
def test_a_rotation_names_a_token_by_its_id_alone(server, client, admin, given):
    made, one = _key(client, admin)
    answer = client.post(f"{one}/rotate", json=given, headers=admin)
    assert answer.status_code == 400
    assert "token" in answer.json()["error"] and "z" * 10 not in answer.text
    assert server.identities.grant_by_secret(made["key"]) is not None  # nothing was rotated


@pytest.mark.parametrize("content", [b"[]", b"{", b'"t_aaaaaaaaaa"', b"null"])
def test_a_rotation_body_that_is_no_object_is_refused(server, client, admin, content):
    made, one = _key(client, admin)
    assert client.post(f"{one}/rotate", content=content, headers=admin).status_code == 400
    assert server.identities.grant_by_secret(made["key"]) is not None


@pytest.mark.parametrize("content", [b"", b"  ", b"{}", b'{"token": null}'])
def test_a_rotation_that_names_none_rotates_the_only_token(server, client, admin, content):
    made, one = _key(client, admin)
    answer = client.post(f"{one}/rotate", content=content, headers=admin)
    assert answer.status_code == 200 and answer.json()["key"] != made["key"]
    assert server.identities.grant_by_secret(made["key"]) is None


def test_a_person_is_not_rotated(client, admin):
    emily = client.post(f"{ADMIN}/identities", json={"kind": "person", "name": "Emily Carter"}, headers=admin)
    one = f"{ADMIN}/identities/{emily.json()['identity']['id']}"
    answer = client.post(f"{one}/rotate", json={"token": "t_aaaaaaaaaa"}, headers=admin)
    assert answer.status_code == 409


def _revoked_once_found(monkeypatch, server, headers):
    """The administration token of ``headers`` revoked as soon as the administration has found it valid, before the
    call's change commits: what a revocation on another instance does to a call that waits for its body or a thread."""
    secret, checked = headers["x-latet-admin"], server.admin.checked

    def then_revoked(given):
        found = checked(given)
        if given == secret and found[0] is not None:
            server.admin.revoke(found[0])
        return found

    monkeypatch.setattr(server.admin, "checked", then_revoked)


def test_a_token_added_with_a_revoked_administration_token_is_not_made(server, client, admin, monkeypatch):
    made, one = _key(client, admin)
    mine = {"x-latet-admin": server.admin.issue("mine", days=None)}
    _revoked_once_found(monkeypatch, server, mine)
    answer = client.post(f"{one}/tokens", json={"name": "logs", "permissions": ["scan"]}, headers=mine)
    assert (answer.status_code, answer.json()) == (401, {"error": "unauthorized"})
    assert [t.label for t in server.identities.tokens(made["identity"]["id"])] == ["key"]  # nothing made
    assert not server.audit.query(action="token.added")


def test_a_rotation_with_a_revoked_administration_token_is_not_made(server, client, admin, monkeypatch):
    _, one = _key(client, admin)
    added = _added(client, admin, one, "logs", ["mask"]).json()
    mine = {"x-latet-admin": server.admin.issue("mine", days=None)}
    _revoked_once_found(monkeypatch, server, mine)
    answer = client.post(f"{one}/rotate", json={"token": added["token"]["id"]}, headers=mine)
    assert (answer.status_code, answer.json()) == (401, {"error": "unauthorized"})
    assert server.identities.grant_by_secret(added["key"]) is not None and not server.audit.query(action="key.rotated")


def _lockable(server, token_id):
    """Whether a revocation could take the administration token's row at once (``FOR UPDATE NOWAIT``): never while a
    change holds it."""
    try:
        with server.db.transaction() as conn:
            conn.execute("SELECT 1 FROM admin_tokens WHERE id = %s FOR UPDATE NOWAIT", (token_id,))
    except psycopg.errors.LockNotAvailable:
        return False
    return True


def test_a_token_is_added_holding_the_administration_tokens_row_before_any_row_of_its_own(
    server, client, admin, monkeypatch
):
    """Paused at its first call to the identities, the change holds its administration token's row already: a call that
    waits for that row (a revocation of it) holds nothing that another waits for."""
    _, one = _key(client, admin)
    monkeypatch.setattr(server.admin.notes, "note", lambda *_args: None)  # a note's write would lock the row too
    mine = {"x-latet-admin": server.admin.issue("mine", days=None)}
    mine_id = server.admin.check(mine["x-latet-admin"])
    first, entered, release = server.identities.add_token, threading.Event(), threading.Event()

    def paused(*args, **kwargs):
        entered.set()
        release.wait(10)
        return first(*args, **kwargs)

    monkeypatch.setattr(server.identities, "add_token", paused)
    answers = []
    calling = threading.Thread(target=lambda: answers.append(_added(client, mine, one, "logs", ["scan"])))
    calling.start()
    try:
        assert entered.wait(10)
        assert not _lockable(server, mine_id)  # its administration token's row is the change's already
    finally:
        release.set()
        calling.join(30)
    assert len(answers) == 1 and answers[0].status_code == 201
    assert _lockable(server, mine_id)  # and let go once the change has committed


def test_the_add_and_its_entry_are_one_transaction(server, client, admin, monkeypatch):
    """When the entry cannot be written the token is not made: both are kept or neither."""
    made, one = _key(client, admin)
    record = server.audit.record

    def failing(actor, action, *args, **kwargs):
        if action == "token.added":
            raise psycopg.OperationalError("the log's chain is out of reach")
        return record(actor, action, *args, **kwargs)

    monkeypatch.setattr(server.audit, "record", failing)
    answer = _added(client, admin, one, "logs", ["scan"])
    assert answer.status_code == 500
    assert [t.label for t in server.identities.tokens(made["identity"]["id"])] == ["key"]
