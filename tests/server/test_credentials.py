"""Someone who can write the database and has not the master key: what he writes finds no one. The server keeps, of every
token, API key, one-time code, administration token and route key, a hash made with a key derived from the master key
(HMAC-SHA256), and with each row a tag over what the row says; a lookup believes a row whose tag holds and no other. The
writer can compute the plain SHA-256 of any secret he chooses, and he can copy what he reads of the table: neither makes
a row that holds."""

import logging
from contextlib import closing

import pytest

from latet.server import identities as identities_module
from latet.server.db import Database, migrate
from latet.server.identities import CODE_TTL, AdminTokens, Identities
from latet.server.tokens import ADMIN_PREFIX, KEY_PREFIX, TOKEN_PREFIX, new_id, new_secret, route_key, secret_hash

from .conftest import CREDENTIALS_KEY

pytestmark = pytest.mark.server

OTHER_KEY = b"\x0d" * 32  # the credentials' key of another server


@pytest.fixture
def identities(db, bus, clock):
    return Identities(db, bus, key=CREDENTIALS_KEY, clock=clock)


@pytest.fixture
def admin(db, clock):
    return AdminTokens(db, key=CREDENTIALS_KEY, clock=clock)


def _run(db, query, *args):
    """A statement of the writer's own: how many rows it changed."""
    with db.connection() as conn:
        return conn.execute(query, args).rowcount


def _read(db, query, *args):
    with db.connection() as conn:
        return conn.execute(query, args).fetchall()


def _said(caplog):
    """What the identities' module said in the log, line by line."""
    return [r.getMessage() for r in caplog.records if r.name == identities_module.__name__]


def _forged_token(db, identity_id, secret):
    """A row of ``tokens`` such as a writer without the key makes: the SHA-256 of a secret he chose and of its route key,
    and a tag that he can only guess."""
    _run(
        db,
        "INSERT INTO tokens (id, identity_id, hash, route, label, created, tag) VALUES (%s, %s, %s, %s, 'forged', 0, %s)",
        new_id("t_"),
        identity_id,
        secret_hash(secret),
        secret_hash(route_key(secret)),
        secret_hash("a tag" + secret),
    )


# --- tokens, API keys and route keys --------------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", [TOKEN_PREFIX, KEY_PREFIX])
def test_a_token_written_with_the_plain_hash_of_a_secret_he_chose_finds_no_one(identities, db, prefix):
    emily, _ = identities.add_person("Emily Carter")
    chosen = new_secret(prefix)
    _forged_token(db, emily.id, chosen)
    assert identities.by_secret(chosen) is None
    assert identities.by_route(route_key(chosen)) is None


def test_rows_made_by_a_server_with_another_key_find_no_one(identities, admin, db, bus, clock):
    other, elsewhere = Identities(db, bus, key=OTHER_KEY, clock=clock), AdminTokens(db, key=OTHER_KEY, clock=clock)
    _, code = other.add_person("Emily Carter")
    key, secret = other.add_key("Billing CI")
    adm = elsewhere.issue("elsewhere")
    assert other.by_secret(secret).id == key.id and elsewhere.check(adm) is not None  # they work where they were made
    assert identities.by_secret(secret) is None and identities.by_route(route_key(secret)) is None
    assert admin.check(adm) is None and admin.lookup(adm) is None
    assert identities.enrol(code, "laptop") is None


def test_a_token_re_pointed_to_another_identity_opens_nothing(identities, db):
    emily, _ = identities.add_person("Emily Carter")
    mallory, code = identities.add_person("Mallory Hart")
    _, mine, _ = identities.enrol(code, "laptop")
    key, billing = identities.add_key("Billing CI")
    assert identities.by_secret(mine).id == mallory.id and identities.by_secret(billing).id == key.id
    _run(db, "UPDATE tokens SET identity_id = %s WHERE identity_id = %s", emily.id, mallory.id)  # his token is Emily's
    _run(db, "UPDATE tokens SET identity_id = %s WHERE identity_id = %s", emily.id, key.id)  # and a key moves to her
    identities.forget()  # the instance has heard of nothing: it reads the database
    for secret in (mine, billing):
        assert identities.by_secret(secret) is None and identities.by_route(route_key(secret)) is None
    assert len(identities._cache) == 0  # the tag is checked before anything is kept


def test_a_route_copied_onto_another_row_opens_nothing(identities, db):
    emily, code = identities.add_person("Emily Carter")
    _, hers, _ = identities.enrol(code, "laptop")
    key, mine = identities.add_key("Mallory CI")
    [(route,)] = _read(db, "SELECT route FROM tokens WHERE identity_id = %s", key.id)
    _run(db, "UPDATE tokens SET route = 'freed' WHERE identity_id = %s", key.id)  # frees the unique value
    _run(db, "UPDATE tokens SET route = %s WHERE identity_id = %s", route, emily.id)  # and puts it on Emily's row
    assert identities.by_route(route_key(mine)) is None  # his address does not reach her space
    assert identities.by_route(route_key(hers)) is None  # and hers does not: a row that was changed is not believed
    assert identities.by_secret(hers) is None and identities.by_secret(mine) is None


def test_a_token_whose_id_was_changed_opens_nothing(identities, db):
    key, secret = identities.add_key("Billing CI")
    _run(db, "UPDATE tokens SET id = 't_forged0000' WHERE identity_id = %s", key.id)
    assert identities.by_secret(secret) is None and identities.by_route(route_key(secret)) is None


def test_hashes_swapped_between_two_rows_open_nothing(identities, db):
    one, first = identities.add_key("One")
    two, second = identities.add_key("Two")
    [(h1,)] = _read(db, "SELECT hash FROM tokens WHERE identity_id = %s", one.id)
    [(h2,)] = _read(db, "SELECT hash FROM tokens WHERE identity_id = %s", two.id)
    _run(db, "UPDATE tokens SET hash = 'swapping' WHERE identity_id = %s", one.id)
    _run(db, "UPDATE tokens SET hash = %s WHERE identity_id = %s", h1, two.id)
    _run(db, "UPDATE tokens SET hash = %s WHERE identity_id = %s", h2, one.id)
    assert identities.by_secret(first) is None and identities.by_secret(second) is None  # each would be the other
    assert identities.by_route(route_key(first)) is None and identities.by_route(route_key(second)) is None


def test_a_row_that_was_changed_and_changed_back_holds_again(identities, db):
    key, secret = identities.add_key("Billing CI")
    emily, _ = identities.add_person("Emily Carter")
    _run(db, "UPDATE tokens SET identity_id = %s WHERE identity_id = %s", emily.id, key.id)
    assert identities.by_secret(secret) is None
    _run(db, "UPDATE tokens SET identity_id = %s WHERE identity_id = %s", key.id, emily.id)
    assert identities.by_secret(secret).id == key.id  # nothing was kept of the refusal


# --- one-time codes -----------------------------------------------------------------------------------------------


def test_a_code_written_with_the_plain_hash_of_a_text_he_chose_opens_nothing(identities, db, clock):
    emily, _ = identities.add_person("Emily Carter")
    chosen = "a code he chose"
    _run(
        db,
        "INSERT INTO codes (hash, identity_id, expires, tag) VALUES (%s, %s, %s, 'x')",
        secret_hash(chosen),
        emily.id,
        clock.now + CODE_TTL,
    )
    assert identities.enrol(chosen, "laptop") is None
    assert identities.tokens(emily.id) == []


def test_a_code_re_pointed_to_another_person_opens_nothing(identities, db):
    emily, _ = identities.add_person("Emily Carter")
    mallory, mine = identities.add_person("Mallory Hart")
    _run(db, "UPDATE codes SET identity_id = %s WHERE identity_id = %s", emily.id, mallory.id)
    assert identities.enrol(mine, "laptop") is None
    assert identities.tokens(emily.id) == [] and identities.tokens(mallory.id) == []


def test_a_code_re_dated_opens_nothing(identities, db, clock):
    emily, code = identities.add_person("Emily Carter")
    clock.now += CODE_TTL + 1
    _run(db, "UPDATE codes SET expires = %s WHERE identity_id = %s", clock.now + 3600, emily.id)
    assert identities.enrol(code, "laptop") is None
    assert identities.tokens(emily.id) == []


def test_a_code_moved_onto_another_persons_row_opens_nothing(identities, db):
    emily, _ = identities.add_person("Emily Carter")
    mallory, mine = identities.add_person("Mallory Hart")
    [(hashed,)] = _read(db, "SELECT hash FROM codes WHERE identity_id = %s", mallory.id)
    _run(db, "DELETE FROM codes WHERE identity_id = %s", mallory.id)  # his own row goes; his code's hash is Emily's
    _run(db, "UPDATE codes SET hash = %s WHERE identity_id = %s", hashed, emily.id)
    assert identities.enrol(mine, "laptop") is None and identities.tokens(emily.id) == []


# --- administration tokens ------------------------------------------------------------------------------------------


def test_an_administration_token_written_with_the_plain_hash_of_a_secret_he_chose_finds_no_one(admin, db):
    chosen = new_secret(ADMIN_PREFIX)
    _run(
        db,
        "INSERT INTO admin_tokens (id, hash, label, created, expires, tag) VALUES (%s, %s, 'forged', 0, NULL, 'x')",
        new_id("a_"),
        secret_hash(chosen),
    )
    assert admin.check(chosen) is None and admin.lookup(chosen) is None and admin.list() == []


def test_an_expired_administration_token_revived_or_made_endless_by_hand_finds_no_one(admin, db, clock):
    revived, endless = admin.issue("revived", days=7), admin.issue("endless", days=7)
    first, second = admin.lookup(revived).id, admin.lookup(endless).id
    clock.now += 8 * 86400
    assert admin.check(revived) is None and admin.check(endless) is None  # they are late
    _run(db, "UPDATE admin_tokens SET expires = %s WHERE id = %s", clock.now + 86400, first)
    _run(db, "UPDATE admin_tokens SET expires = NULL WHERE id = %s", second)
    assert admin.check(revived) is None and admin.check(endless) is None
    assert admin.lookup(revived) is None and admin.lookup(endless) is None


def test_administration_tokens_whose_ids_were_swapped_name_no_one(admin, db):
    one, two = admin.issue("one"), admin.issue("two")
    first, second = admin.lookup(one).id, admin.lookup(two).id
    _run(db, "UPDATE admin_tokens SET id = 'swapping' WHERE id = %s", first)
    _run(db, "UPDATE admin_tokens SET id = %s WHERE id = %s", first, second)
    _run(db, "UPDATE admin_tokens SET id = %s WHERE id = 'swapping'", second)
    assert admin.check(one) is None and admin.check(two) is None  # the log's actor would name the wrong token


def test_a_known_administration_secret_moved_onto_another_row_opens_nothing(admin, db):
    old, new = admin.issue("old", days=7), admin.issue("new", days=None)  # a former administrator knows `old`
    first, second = admin.lookup(old).id, admin.lookup(new).id
    [(hashed,)] = _read(db, "SELECT hash FROM admin_tokens WHERE id = %s", first)
    _run(db, "DELETE FROM admin_tokens WHERE id = %s", first)
    _run(db, "UPDATE admin_tokens SET hash = %s WHERE id = %s", hashed, second)  # onto a row that never expires
    assert admin.check(old) is None and admin.lookup(old) is None


def test_the_administrations_list_leaves_out_a_row_that_does_not_hold(admin, db, caplog):
    admin.issue("good")
    admin.issue("changed")
    _run(db, "UPDATE admin_tokens SET expires = NULL WHERE label = 'changed'")
    with caplog.at_level(logging.WARNING, logger=identities_module.__name__):
        assert [t.label for t in admin.list()] == ["good"]
    assert len(_said(caplog)) == 1 and "administration token" in _said(caplog)[0]


# --- what is said of a row that does not hold ---------------------------------------------------------------------


def test_a_row_that_does_not_hold_is_said_once_a_minute_without_a_word_of_it(identities, db, clock, caplog):
    emily, _ = identities.add_person("Emily Carter")
    key, secret = identities.add_key("Billing CI")
    [(token_id,)] = _read(db, "SELECT id FROM tokens WHERE identity_id = %s", key.id)
    _run(db, "UPDATE tokens SET identity_id = %s WHERE identity_id = %s", emily.id, key.id)
    with caplog.at_level(logging.WARNING, logger=identities_module.__name__):
        for _ in range(3):
            assert identities.by_secret(secret) is None
        assert len(_said(caplog)) == 1  # the first at once, the others held back
        clock.now += 61
        assert identities.by_secret(secret) is None
    said = _said(caplog)
    assert len(said) == 2 and "token" in said[0] and "3 since the line before" in said[1]
    for kept in (secret, route_key(secret), emily.id, key.id, token_id, secret_hash(secret)):
        assert kept not in caplog.text  # nothing of the row, which a writer could have filled with anything


# --- a tag over a time -----------------------------------------------------------------------------------------------


def test_a_tag_over_a_time_holds_in_sessions_that_default_to_a_short_float(role_url, schema, bus, clock):
    """A code and an administration token end at a time with microseconds, and their tags cover it. With
    ``extra_float_digits`` at 0 the database would give it back rounded to 15 digits, and the tag over what was read would
    not hold: the connections of the server are told 3 whatever the role defaults to."""
    clock.now = 1_760_431_234.123456
    with closing(Database(role_url(extra_float_digits="0"), schema=schema)) as db:
        migrate(db)
        people, administration = (
            Identities(db, bus, key=CREDENTIALS_KEY, clock=clock),
            AdminTokens(db, key=CREDENTIALS_KEY, clock=clock),
        )
        _, code = people.add_person("Emily Carter")
        assert people.enrol(code, "laptop") is not None  # the code's tag, over when it ends
        assert administration.check(administration.issue("ci", days=7)) is not None  # and the administration token's


# --- what a tag cannot stop -----------------------------------------------------------------------------------------


def test_what_a_tag_cannot_stop_is_said_in_the_module_and_is_so(identities, db):
    """A row the key made, copied before its deletion, holds again when it is written back; ``disabled`` is not tagged:
    set back by hand it enables the identity. Only state kept outside the database stops a rollback."""
    said = identities_module.__doc__
    assert said is not None and "written back" in said and "disabled" in said and "outside the database" in said
    emily, code = identities.add_person("Emily Carter")
    _, token, _ = identities.enrol(code, "laptop")
    [row] = _read(db, "SELECT id, identity_id, hash, route, label, created, last_used, tag FROM tokens")
    assert identities.revoke(emily.id) == 1 and identities.by_secret(token) is None
    _run(
        db,
        "INSERT INTO tokens (id, identity_id, hash, route, label, created, last_used, tag)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        *row,
    )
    identities.forget()
    assert identities.by_secret(token).id == emily.id  # revoked, and written back as it was
    assert identities.disable(emily.id) and identities.by_secret(token) is None
    _run(db, "UPDATE identities SET disabled = FALSE WHERE id = %s", emily.id)
    identities.forget()
    assert identities.by_secret(token).id == emily.id  # disabled, and set back by hand
