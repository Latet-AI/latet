"""An API key's tokens and what each may do (scan, mask, unmask): a token made before permissions existed holds all
three and keeps its row as it was, as a key's first token does, and serves agents; another token keeps its name and its
permissions, all three too (Ruling T13c), which its row's keyed tag covers, so that a writer of the database can neither
widen nor narrow them; rotating a key with several tokens names the one, and a token whose row does not hold is not
rotated into one that does."""

import psycopg
import pytest

from latet.server.grants import DEFAULT
from latet.server.identities import LABEL_LENGTH, NoSuchIdentity, NoSuchToken, SeveralTokens
from latet.server.tokens import KEY_PREFIX, keyed_hash, new_id, new_secret, route_key

pytestmark = pytest.mark.server


def _added(server, key_id, name, permissions):
    with server.identities.changing() as changes:
        return server.identities.add_token(key_id, name, permissions, within=changes)


def _column(server, token_id):
    with server.db.connection() as conn:
        row = conn.execute("SELECT permissions FROM tokens WHERE id = %s", (token_id,)).fetchone()
    assert row is not None
    return row[0]


def test_a_key_made_before_has_every_permission(server):
    key, secret = server.identities.add_key("Billing CI")
    assert server.identities.grant_by_secret(secret).permissions == DEFAULT
    assert server.identities.tokens(key.id)[0].permissions == DEFAULT
    assert _column(server, server.identities.tokens(key.id)[0].id) is None  # its row as every row made before


def test_a_persons_token_keeps_todays_row(server):
    emily, code = server.identities.add_person("Emily Carter")
    _, token, _ = server.identities.enrol(code, "laptop")
    grant = server.identities.grant_by_secret(token)
    assert grant.identity.id == emily.id and grant.permissions == DEFAULT and grant.gateway and grant.agents
    [made] = server.identities.tokens(emily.id)
    assert grant.token_id == made.id and _column(server, made.id) is None and made.agents


def test_add_token_keeps_its_name_and_permissions(server):
    key, _ = server.identities.add_key("Shipper")
    with server.identities.changing() as changes:
        info, secret = server.identities.add_token(key.id, "logs", ["mask"], within=changes)
    grant = server.identities.grant_by_secret(secret)
    assert grant.identity.id == key.id and grant.token_id == info.id
    assert grant.permissions == {"mask", "scan"}
    assert {t.label for t in server.identities.tokens(key.id)} == {"key", "logs"}
    assert (info.label, info.permissions, info.tampered, info.last_used) == ("logs", {"mask", "scan"}, False, None)
    assert server.identities.grant_by_route(route_key(secret)) == grant  # its own address, the same grant
    assert server.identities.by_secret(secret) == server.identities.by_route(route_key(secret)) == grant.identity
    assert _column(server, info.id) == "mask,scan"


def test_what_an_instance_holds_is_the_grant_and_forgetting_the_identity_drops_it(server):
    key, _ = server.identities.add_key("Shipper")
    _, secret = _added(server, key.id, "audit", ["scan"])
    assert server.identities.remembered_grant(secret=secret) is None  # not looked up yet
    grant = server.identities.grant_by_secret(secret)
    assert server.identities.remembered_grant(secret=secret) == grant
    assert server.identities.remembered(secret=secret) == grant.identity  # what the gate asks today
    server.identities.grant_by_route(route_key(secret))
    assert server.identities.remembered_grant(route=route_key(secret)) == grant
    server.identities.forget(key.id)
    assert server.identities.remembered_grant(secret=secret) is None
    assert server.identities.remembered_grant(route=route_key(secret)) is None


def test_a_person_gets_no_token_this_way(server):
    emily, _ = server.identities.add_person("Emily Carter")
    for identity_id in (emily.id, "u_nosuchidxx"):
        with pytest.raises(NoSuchIdentity) as refused:
            _added(server, identity_id, "logs", ["scan"])
        assert identity_id not in str(refused.value)
    assert server.identities.tokens(emily.id) == []


@pytest.mark.parametrize(("name", "permissions"), [(" \x1b ", ["scan"]), ("logs", []), ("logs", ["read"])])
def test_a_token_needs_a_name_and_permissions_of_latets(server, name, permissions):
    key, _ = server.identities.add_key("Shipper")
    with pytest.raises(ValueError, match=r"a token's (name|permissions)"):
        _added(server, key.id, name, permissions)
    assert [t.label for t in server.identities.tokens(key.id)] == ["key"]  # nothing made


def test_a_name_longer_than_the_label_is_refused_and_never_cut(server):
    """One limit: the label a token keeps. A name of that many characters is kept whole (what is taken out of it does
    not count), one more is refused in words that give the limit, and nothing is made."""
    key, _ = server.identities.add_key("Shipper")
    full = "n" * LABEL_LENGTH
    info, _ = _added(server, key.id, full[:40] + "​\x1b" * 30 + full[40:], ["scan"])
    assert info.label == full and {t.label for t in server.identities.tokens(key.id)} == {"key", full}
    with pytest.raises(ValueError, match=rf"a token's name is {LABEL_LENGTH} characters at most") as refused:
        _added(server, key.id, full + "n", ["scan"])
    assert full not in str(refused.value)
    assert len(server.identities.tokens(key.id)) == 2  # the refused one made nothing


def test_a_row_written_by_0_9_still_has_every_permission(server):
    """A token row as 0.9.x wrote it (no permissions column: NULL; the tag over the row's other columns alone) is what
    it was: all three permissions, found by its secret and its address, rotated into a row of the same form."""
    key, _ = server.identities.add_key("Billing CI")
    credentials = server.master.derive(b"credentials")  # the key every hash and tag of a token is made under
    secret, token_id = new_secret(KEY_PREFIX), new_id("t_")
    hashed = keyed_hash(credentials, "secret", secret)
    route = keyed_hash(credentials, "route", route_key(secret))
    tag = keyed_hash(credentials, "token-tag", token_id, key.id, hashed, route)
    with server.db.connection() as conn:
        conn.execute(
            "INSERT INTO tokens (id, identity_id, hash, route, label, created, tag, computer_key)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, NULL)",
            (token_id, key.id, hashed, route, "billing", 1.0, tag),
        )
    assert _column(server, token_id) is None
    grant = server.identities.grant_by_secret(secret)
    assert grant is not None and grant.permissions == DEFAULT and grant.gateway
    assert (grant.identity.id, grant.token_id) == (key.id, token_id)
    assert server.identities.by_secret(secret) == key
    assert server.identities.grant_by_route(route_key(secret)) == grant
    assert server.identities.holds_token(key.id, server.identities.secret_digest(secret))
    [listed] = [t for t in server.identities.tokens(key.id) if t.id == token_id]
    assert (listed.label, listed.tampered, listed.permissions) == ("billing", False, DEFAULT)
    new = server.identities.rotate_token(key.id, token_id=token_id)[1]
    assert server.identities.grant_by_secret(secret) is None
    assert server.identities.grant_by_secret(new).permissions == DEFAULT
    [rotated] = [t for t in server.identities.tokens(key.id) if t.label == "billing"]
    assert _column(server, rotated.id) is None  # the same form: the rows that did not hold permissions still do not


def test_permissions_changed_in_the_database_find_no_one(server, db):
    key, full = server.identities.add_key("Shipper")
    audit, scanning = _added(server, key.id, "audit", ["scan"])
    logs, masking = _added(server, key.id, "logs", ["mask"])
    [first] = [t.id for t in server.identities.tokens(key.id) if t.label == "key"]
    for secret in (full, scanning, masking):
        assert server.identities.holds_token(key.id, server.identities.secret_digest(secret))
    with db.connection() as conn:  # written by hand, without the server's key
        conn.execute("UPDATE tokens SET permissions = NULL WHERE id = %s", (audit.id,))  # widened to all three
        conn.execute("UPDATE tokens SET permissions = 'scan' WHERE id = %s", (first,))  # narrowed
        conn.execute("UPDATE tokens SET permissions = 'scan,unmask' WHERE id = %s", (logs.id,))  # swapped
    server.identities.forget()  # the instance has heard of nothing: it reads the database
    for secret in (full, scanning, masking):
        assert server.identities.grant_by_secret(secret) is None
        assert server.identities.grant_by_route(route_key(secret)) is None
        assert not server.identities.holds_token(key.id, server.identities.secret_digest(secret))
    listed = server.identities.tokens(key.id)
    assert all(t.tampered and t.permissions == frozenset() for t in listed)  # listed, flagged, and allowed nothing
    assert server.identities.token_counts()[key.id] == (3, 3)
    assert len(server.identities._cache) == 0  # the tag is checked before anything is kept


def test_a_token_that_may_forget_keeps_it_in_its_row(server):
    """Task 11b: forget is given by name and kept in the row, even with the three a key's first token holds as NULL."""
    key, _ = server.identities.add_key("Shipper")
    eraser, erasing = _added(server, key.id, "erasure", ["forget"])
    every, everything = _added(server, key.id, "all of it", ["mask", "unmask", "forget"])
    assert server.identities.grant_by_secret(erasing).permissions == {"forget"}
    grant = server.identities.grant_by_secret(everything)
    assert grant.permissions == {"scan", "mask", "unmask", "forget"} and not grant.gateway  # an added token's (T13b)
    assert (_column(server, eraser.id), _column(server, every.id)) == ("forget", "forget,mask,scan,unmask")
    rotated = server.identities.rotate_token(key.id, token_id=every.id)[1]  # its name and its permissions kept
    assert server.identities.grant_by_secret(rotated).permissions == {"scan", "mask", "unmask", "forget"}


def test_forget_written_into_a_row_by_hand_finds_no_one(server, db):
    """A writer of the database who gives a token forget (from NULL, or added to its list) makes a row whose tag does
    not hold: it finds no one, and forgets nothing."""
    key, full = server.identities.add_key("Shipper")
    logs, masking = _added(server, key.id, "logs", ["mask"])
    [first] = [t.id for t in server.identities.tokens(key.id) if t.label == "key"]
    with db.connection() as conn:  # written by hand, without the server's key
        conn.execute("UPDATE tokens SET permissions = 'forget,mask,scan,unmask' WHERE id = %s", (first,))
        conn.execute("UPDATE tokens SET permissions = 'forget,mask,scan' WHERE id = %s", (logs.id,))
    server.identities.forget()
    for secret in (full, masking):
        assert server.identities.grant_by_secret(secret) is None
    assert all(t.tampered and t.permissions == frozenset() for t in server.identities.tokens(key.id))


@pytest.mark.parametrize(
    "form", ["mask", "scan,mask,unmask", "scan,mask", "SCAN", "", "mask,forget", "forget,mask", "FORGET"]
)
def test_the_database_refuses_a_form_stored_cannot_make(server, db, form):
    key, _ = server.identities.add_key("Shipper")
    with db.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE tokens SET permissions = %s WHERE identity_id = %s", (form, key.id))


def test_a_token_added_with_all_three_keeps_them_in_its_row(server):
    """Ruling T13c: a token added to an API key keeps its permissions in its row even when they are scan, mask and
    unmask, so that only the key's first token keeps NULL's row and serves agents (Ruling T13b); each keeps its row's
    form when it is rotated, and so does a key's first token made again for a key left with none."""
    key, first = server.identities.add_key("Shipper")
    [made] = server.identities.tokens(key.id)
    info, assistant = _added(server, key.id, "assistant", ["mask", "unmask"])
    assert (_column(server, made.id), _column(server, info.id)) == (None, "mask,scan,unmask")
    grant = server.identities.grant_by_secret(assistant)
    assert grant.permissions == DEFAULT and not grant.agents and not grant.gateway
    assert server.identities.grant_by_route(route_key(assistant)) == grant
    assert server.identities.grant_by_secret(first).agents and server.identities.grant_by_secret(first).gateway
    listed = {t.id: t for t in server.identities.tokens(key.id)}
    assert (listed[info.id].permissions, listed[info.id].agents, listed[made.id].agents) == (DEFAULT, False, True)
    assert not info.agents
    with server.identities.changing() as changes:
        rotated, again, _ = server.identities.rotate_token(key.id, token_id=info.id, within=changes)
    assert _column(server, rotated.id) == "mask,scan,unmask" and not rotated.agents
    assert not server.identities.grant_by_secret(again).agents
    with server.identities.changing() as changes:
        renewed, anew, _ = server.identities.rotate_token(key.id, token_id=made.id, within=changes)
    assert _column(server, renewed.id) is None and renewed.agents and server.identities.grant_by_secret(anew).agents
    server.identities.revoke(key.id)
    remade, fresh, _ = server.identities.rotate_token(key.id)  # none left: made as a key's first
    assert _column(server, remade.id) is None and server.identities.grant_by_secret(fresh).agents


def test_rotate_names_the_token_when_there_are_several(server):
    key, first = server.identities.add_key("Shipper")
    info, logs = _added(server, key.id, "logs", ["mask"])
    with pytest.raises(SeveralTokens) as several:
        server.identities.rotate_token(key.id)[1]
    assert several.value.count == 2 and str(several.value) == "this key has 2 tokens: name the one to rotate"
    assert server.identities.grant_by_secret(first) is not None and server.identities.grant_by_secret(logs) is not None
    new = server.identities.rotate_token(key.id, token_id=info.id)[1]
    assert server.identities.grant_by_secret(logs) is None and server.identities.grant_by_route(route_key(logs)) is None
    assert server.identities.grant_by_secret(first).permissions == DEFAULT
    grant = server.identities.grant_by_secret(new)
    assert grant.permissions == {"mask", "scan"} and grant.token_id != info.id and grant.identity.id == key.id
    assert {(t.label, t.permissions) for t in server.identities.tokens(key.id)} == {
        ("key", DEFAULT),
        ("logs", frozenset({"mask", "scan"})),
    }
    with pytest.raises(NoSuchToken):
        server.identities.rotate_token(key.id, token_id=info.id)[1]  # gone with its rotation


def test_rotate_keeps_todays_meaning_for_one_token(server):
    key, old = server.identities.add_key("Billing CI")
    new = server.identities.rotate_token(key.id)[1]
    assert server.identities.grant_by_secret(old) is None
    grant = server.identities.grant_by_secret(new)
    assert grant.identity.id == key.id and grant.permissions == DEFAULT
    [token] = server.identities.tokens(key.id)
    assert token.label == "key" and _column(server, token.id) is None
    server.identities.revoke(key.id)
    again = server.identities.rotate_token(key.id)[1]  # no token left: a new one, as the key's first
    assert server.identities.grant_by_secret(again).permissions == DEFAULT


def test_a_rotation_says_the_token_it_retired_and_the_one_that_replaces_it(server):
    """``rotate_token`` answers the new token (as ``tokens`` lists it), its secret and the id of the token taken away:
    the one named, the only one a key has, or none when the key had none left."""
    key, _ = server.identities.add_key("Shipper")
    [first] = server.identities.tokens(key.id)
    logs, _ = _added(server, key.id, "logs", ["mask"])
    with server.identities.changing() as changes:
        info, secret, replaced = server.identities.rotate_token(key.id, token_id=logs.id, within=changes)
    assert replaced == logs.id and info.id != logs.id
    assert (info.label, info.permissions, info.tampered) == ("logs", {"mask", "scan"}, False)
    assert server.identities.grant_by_secret(secret).token_id == info.id
    server.identities.revoke(key.id, first.id)
    server.identities.revoke(key.id, info.id)
    again_info, _, none = server.identities.rotate_token(key.id)  # no token left
    assert none is None and (again_info.label, again_info.permissions) == ("key", DEFAULT)
    info, secret, replaced = server.identities.rotate_token(key.id)  # the only one, named by no one
    assert replaced == again_info.id and info.id != again_info.id and info.label == "key"
    with pytest.raises(NoSuchToken):
        server.identities.rotate_token(key.id, token_id=replaced)  # retired: gone


def test_rotate_takes_no_token_of_another_key(server):
    key, _ = server.identities.add_key("Shipper")
    other, theirs = server.identities.add_key("Other CI")
    [their_token] = server.identities.tokens(other.id)
    for token_id in (their_token.id, "t_nosuchtokn"):
        with pytest.raises(NoSuchToken) as refused:
            server.identities.rotate_token(key.id, token_id=token_id)[1]
        assert token_id not in str(refused.value)
    assert server.identities.grant_by_secret(theirs).identity.id == other.id
    person, _ = server.identities.add_person("Emily Carter")
    with pytest.raises(NoSuchIdentity):
        server.identities.rotate_token(person.id)[1]


def test_a_token_whose_row_does_not_hold_is_not_rotated_into_one_that_does(server, db):
    """A writer of the database who sets a restricted token's permissions to NULL makes it find no one; an administrator
    who rotates it then must not get it back with all three. Revoked first, the key is rotated as a new one."""
    key, _ = server.identities.add_key("Shipper")
    info, _ = _added(server, key.id, "audit", ["scan"])
    server.identities.revoke(key.id, next(t.id for t in server.identities.tokens(key.id) if t.label == "key"))
    with db.connection() as conn:
        conn.execute("UPDATE tokens SET permissions = NULL WHERE id = %s", (info.id,))
    for token_id in (info.id, None):
        with pytest.raises(NoSuchToken):
            server.identities.rotate_token(key.id, token_id=token_id)[1]
    assert [(t.id, t.tampered) for t in server.identities.tokens(key.id)] == [(info.id, True)]  # nothing made
    server.identities.revoke(key.id, info.id)
    assert server.identities.grant_by_secret(server.identities.rotate_token(key.id)[1]).permissions == DEFAULT


def test_the_other_instance_reads_the_same_permissions(instances):
    a, b = instances
    key, _ = a.identities.add_key("Shipper")
    info, secret = _added(a, key.id, "logs", ["unmask"])
    grant = b.identities.grant_by_secret(secret)
    assert (grant.identity.id, grant.token_id, grant.permissions) == (key.id, info.id, {"unmask"})
    assert not grant.gateway and not grant.allows("scan")
