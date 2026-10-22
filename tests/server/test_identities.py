"""The identities of a Latet server: a person's one-time code gives each computer a token once; an API key is an identity
of its own; disabling stops every secret at once and every instance hears it; the server keeps hashes, never secrets;
what a caller writes in a name, an email or a label is kept without control characters and no longer than a limit."""

import asyncio
import contextlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
import redis
from psycopg import sql

from latet.server import identities as identities_module
from latet.server.identities import (
    CACHE_SECONDS,
    CODE_TTL,
    EMAIL_LENGTH,
    EXTERNAL_ID_LENGTH,
    IDENTITY_ID,
    LABEL_LENGTH,
    NAME_LENGTH,
    TOKEN_ID,
    AdminTokens,
    Identities,
    NoSuchIdentity,
)
from latet.server.tokens import TOKEN_PREFIX, route_key, secret_hash
from latet.state import IdentityRef

from .conftest import CREDENTIALS_KEY, dump, identity_in

pytestmark = pytest.mark.server

# What a terminal acts on, and what makes one name read as another: an escape, a bell, a zero byte, the 8-bit
# introducer of a sequence, and the bidirectional override, mark and isolate.
ESC, BELL, NUL, CSI = chr(0x1B), chr(0x07), chr(0), chr(0x9B)
RLO, LRM, RLI = chr(0x202E), chr(0x200E), chr(0x2067)
# What shows as nothing, or splits a line: a zero-width space, a word joiner, a byte order mark, the line and paragraph
# separators. (The joiner and the non-joiner, which scripts and emoji need, are not among them.)
ZWSP, WJ, BOM, LS, PS = chr(0x200B), chr(0x2060), chr(0xFEFF), chr(0x2028), chr(0x2029)
ZWNJ, ZWJ = chr(0x200C), chr(0x200D)


@pytest.fixture
def identities(db, bus, clock):
    return Identities(db, bus, key=CREDENTIALS_KEY, clock=clock)


@pytest.fixture
def admin(db, clock):
    return AdminTokens(db, key=CREDENTIALS_KEY, clock=clock)


def test_a_code_gives_a_person_one_token_once(identities):
    emily, code = identities.add_person("Emily Carter", "emily.carter@acme.com")
    assert emily.id.startswith("u_") and len(emily.id) == 12 and emily.kind == "person" and not emily.disabled
    assert emily.ref() == IdentityRef(emily.id, "Emily Carter", "person")
    enrolled = identities.enrol(code, "emily-laptop")
    assert enrolled is not None and enrolled[0].id == emily.id and enrolled[1].startswith("latet_pat_")
    assert identities.enrol(code, "someone-else") is None
    assert identities.by_secret(enrolled[1]).id == emily.id
    [token] = identities.tokens(emily.id)
    assert IDENTITY_ID.fullmatch(emily.id) and TOKEN_ID.fullmatch(token.id)  # the shapes the server's calls accept


def test_a_computer_key_is_issued_with_its_token_and_holds_for_that_token_alone(identities, db):
    emily, code = identities.add_person("Emily Carter")
    _, laptop, laptop_key = identities.enrol(code, "laptop")
    _, desktop, desktop_key = identities.enrol(identities.code(emily.id), "desktop")
    assert laptop_key.startswith("latet_ck_") and len(laptop_key) == 52 and laptop_key != desktop_key
    assert identities.computer_key(emily.id, laptop, laptop_key) == "held"
    assert identities.computer_key(emily.id, laptop, desktop_key) == "wrong"  # another computer's of hers
    assert identities.computer_key(emily.id, laptop, "") == "wrong"
    assert identities.computer_key("u_somebodyels", laptop, laptop_key) == "gone"  # another identity's token
    identities.add_key("Billing CI")
    with db.connection() as conn:  # an API key has no computer, and so no key
        assert conn.execute("SELECT computer_key FROM tokens WHERE label = 'key'").fetchone() == (None,)
        conn.execute("UPDATE tokens SET computer_key = NULL WHERE label = 'desktop'")  # as before computer keys
    assert identities.computer_key(emily.id, desktop, desktop_key) == "keyless"
    with db.connection() as conn:  # its row changed without the key: it finds no one, its key held or not
        conn.execute("UPDATE tokens SET label = 'x', route = 'tampered' WHERE label = 'laptop'")
    assert identities.computer_key(emily.id, laptop, laptop_key) == "gone"
    identities.revoke(emily.id)
    assert identities.computer_key(emily.id, desktop, desktop_key) == "gone"


def test_a_code_lasts_a_day_and_a_new_one_replaces_it(identities, clock):
    emily, first = identities.add_person("Emily Carter")
    second = identities.code(emily.id)
    assert identities.enrol(first, "laptop") is None
    clock.now += CODE_TTL + 1
    assert identities.enrol(second, "laptop") is None and identities.code("u_nobody0000") is None


def test_each_computer_has_its_own_token_and_address(identities, clock):
    emily, code = identities.add_person("Emily Carter")
    _, laptop, _ = identities.enrol(code, "laptop")
    clock.now += 1  # tokens are listed by the time they were made: two made at one reading of the clock have no order
    _, desktop, _ = identities.enrol(identities.code(emily.id), "desktop")
    assert route_key(laptop) != route_key(desktop)
    assert identities.by_route(route_key(laptop)).id == identities.by_route(route_key(desktop)).id == emily.id
    assert [t.label for t in identities.tokens(emily.id)] == ["laptop", "desktop"]
    assert identities.token_counts() == {emily.id: (2, 0)}  # every token, and none whose tag fails


def test_an_api_key_is_an_identity_of_its_own_and_rotating_it_gives_a_new_secret_and_address(identities):
    key, secret = identities.add_key("Billing CI")
    assert key.kind == "key" and secret.startswith("latet_key_") and identities.by_secret(secret).id == key.id
    assert identities.by_route(route_key(secret)).id == key.id and identities.code(key.id) is None  # no computer
    new = identities.rotate_key(key.id)
    assert identities.by_secret(secret) is None and identities.by_route(route_key(secret)) is None
    assert identities.by_secret(new).id == key.id and [t.label for t in identities.tokens(key.id)] == ["key"]
    assert [i.kind for i in identities.all()] == ["key"] and identities.all("person") == []


def test_rotating_what_is_no_api_key_says_so_without_quoting_what_was_asked(identities):
    emily, _ = identities.add_person("Emily Carter")
    for asked in (emily.id, "u_nobody0000", f"{ESC}[31mu_nobody"):  # a person, no one, and a name with an escape
        with pytest.raises(LookupError, match="no such API key") as raised:
            identities.rotate_key(asked)
        assert ESC not in str(raised.value) and asked not in str(raised.value)


def test_disabling_stops_every_secret_at_once_and_enabling_brings_them_back(identities):
    emily, code = identities.add_person("Emily Carter")
    _, token, _ = identities.enrol(code, "laptop")
    key, secret = identities.add_key("Billing CI")
    assert identities.enabled_count() == 2
    assert identities.disable(emily.id) and identities.enabled_count() == 1
    assert identities.by_secret(token) is None and identities.by_route(route_key(token)) is None
    assert identities.enrol(identities.code(emily.id), "desktop") is None  # no new computer while disabled
    assert identities.enable(emily.id) and identities.by_secret(token) is not None
    assert identities.disable(key.id) and identities.by_secret(secret) is None and identities.enabled_count() == 1


def test_another_instance_keeps_what_it_looked_up_until_it_hears_of_a_change(db, buses, clock):
    here = Identities(db, buses[0], key=CREDENTIALS_KEY, clock=clock)
    there = Identities(db, buses[1], key=CREDENTIALS_KEY, clock=clock)
    emily, code = here.add_person("Emily Carter")
    _, token, _ = here.enrol(code, "laptop")
    assert there.by_secret(token) is not None
    here.disable(emily.id)
    assert there.by_secret(token) is not None  # kept there a few seconds: the message is what drops it
    there.forget(emily.id)  # what an instance does when `identities` says the identity changed
    assert there.by_secret(token) is None
    here.enable(emily.id)
    clock.now += CACHE_SECONDS + 1  # a message lost: a few seconds at worst
    assert there.by_secret(token) is not None


def test_lookups_that_find_nothing_are_not_kept_and_the_cache_is_bounded(identities, monkeypatch):
    monkeypatch.setattr("latet.server.identities.CACHE_SIZE", 3)
    for n in range(50):
        assert identities.by_secret(f"latet_pat_{n:043d}") is None  # a stranger's guesses
        assert identities.by_route("b" * 25 + "abcdefghij"[n % 10]) is None  # and addresses
    assert len(identities._cache) == 0  # none of them kept
    people = [identities.add_person(f"Person {n}")[0] for n in range(5)]
    for person in people:
        identities.get(person.id)
    assert len(identities._cache) == 3  # the three looked up last


async def test_every_change_to_an_identity_is_told_to_every_instance(identities, bus):
    ready, heard = asyncio.Event(), []

    async def ear():
        async with contextlib.aclosing(bus.listen("identities", ready=ready)) as messages:
            async for _, message in messages:
                heard.append(message)

    task = asyncio.create_task(ear())
    await asyncio.wait_for(ready.wait(), 5)
    emily, code = identities.add_person("Emily Carter")
    _, token, _ = identities.enrol(code, "laptop")
    key, _ = identities.add_key("Billing CI")
    identities.update(emily.id, name="Emily J. Carter")
    identities.disable(emily.id)
    identities.enable(emily.id)
    identities.rotate_key(key.id)
    identities.revoke_secret(token)
    identities.remove(key.id)
    for _ in range(100):
        if len(heard) == 8:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert [(m["id"], m["event"]) for m in heard] == [
        (emily.id, "added"), (key.id, "added"), (emily.id, "updated"), (emily.id, "disabled"),
        (emily.id, "enabled"), (key.id, "rotated"), (emily.id, "revoked"), (key.id, "removed"),
    ]  # fmt: skip


def test_revoking_removing_and_a_computer_forgetting_its_own_token(identities, clock):
    emily, code = identities.add_person("Emily Carter")
    _, laptop, _ = identities.enrol(code, "laptop")
    clock.now += 1  # the first token is the oldest: two made at one reading of the clock have no order
    _, desktop, _ = identities.enrol(identities.code(emily.id), "desktop")
    first = identities.tokens(emily.id)[0]
    assert identities.revoke(emily.id, first.id) == 1 and identities.by_secret(laptop) is None
    assert identities.revoke_secret(desktop) and identities.by_secret(desktop) is None
    assert identities.remove(emily.id) and identities.get(emily.id) is None and not identities.remove(emily.id)


def test_use_is_written_at_most_once_a_minute(identities, clock):
    emily, _ = identities.add_person("Emily Carter")
    identities.seen(emily.id).result(5)  # written in the notes' thread
    identities.forget(emily.id)  # read it as the database has it
    first = identities.get(emily.id).last_seen
    clock.now += 30
    assert identities.seen(emily.id) is None  # not due: nothing written
    identities.forget(emily.id)
    assert first == clock.now - 30 and identities.get(emily.id).last_seen == first
    clock.now += 31
    identities.seen(emily.id).result(5)
    identities.forget(emily.id)
    assert identities.get(emily.id).last_seen == clock.now


def test_a_name_is_required_and_identities_are_listed_by_name(identities):
    with pytest.raises(ValueError, match="name"):
        identities.add_person("  ")
    identities.add_person("daniel okafor")
    identities.add_person("Emily Carter")
    assert [i.name for i in identities.all()] == ["daniel okafor", "Emily Carter"]


def test_the_administrations_tokens_expire_unless_issued_for_good(admin, clock):
    secret = admin.issue("first administrator")
    token_id = admin.check(secret)
    assert secret.startswith("latet_adm_") and token_id is not None and admin.check("latet_adm_nope") is None
    [info] = admin.list()
    assert (info.id, info.label, info.expires) == (token_id, "first administrator", clock.now + 90 * 86400)
    clock.now += 1  # tokens are listed by the time they were made: two made at one reading of the clock have no order
    forever = admin.issue("the backup", days=None)
    clock.now += 1
    short = admin.issue("a week", days=7)
    clock.now += 8 * 86400
    assert admin.check(short) is None and admin.lookup(short).expired(clock.now)  # refused, still listed
    assert admin.check(secret) is not None
    clock.now += 83 * 86400
    assert admin.check(secret) is None and admin.check(forever) is not None and admin.lookup(forever).expires is None
    assert [t.label for t in admin.list()] == ["first administrator", "the backup", "a week"]
    assert admin.revoke(token_id) and admin.lookup(secret) is None and not admin.revoke(token_id)


def test_the_server_keeps_hashes_never_secrets_codes_or_route_keys(identities, admin, db):
    emily, _ = identities.add_person("Emily Carter")
    _, token, _ = identities.enrol(identities.code(emily.id), "laptop")
    waiting = identities.code(emily.id)  # a code not used yet: kept by its hash
    _, secret = identities.add_key("Billing CI")
    adm = admin.issue("first administrator")
    seen = dump(db)
    for kept in (token, secret, adm, waiting, route_key(token), route_key(secret)):
        assert kept.encode() not in seen
    # nor the plain SHA-256 of any of them, which whoever reads the database could test a guess against, and write
    for kept in (token, secret, adm, waiting, route_key(token), route_key(secret)):
        assert secret_hash(kept).encode() not in seen


# --- a lookup that began before a change keeps nothing of what it found ---------------------------------------------


def _held(identities, monkeypatch):
    """Make the lookups that run in a thread of their own stop once they have read the database, and wait for the test
    to let them go: a lookup that began before a change and ends after it, in the order the test chooses (two events,
    no waiting for time to pass). ``read`` is set when a lookup has read; it goes on when the test sets ``go``. What the
    test's own thread looks up is not held."""
    read, go = threading.Event(), threading.Event()

    def hold(real):
        def held(query, *args):
            found = real(query, *args)  # the database as it is now
            if threading.current_thread() is not threading.main_thread():
                read.set()
                assert go.wait(10), "the test did not let the lookup go"
            return found

        return held

    for name in ("_one", "_owner"):  # the two ways a lookup reads an identity: by its id, and by a token's row
        monkeypatch.setattr(identities, name, hold(getattr(identities, name)))
    return read, go


def _race(identities, monkeypatch, lookup, change):
    """``lookup()`` reads the database in a thread, ``change()`` is made and told meanwhile, and the lookup ends after."""
    read, go = _held(identities, monkeypatch)
    with ThreadPoolExecutor(1) as pool:
        racing = pool.submit(lookup)
        try:
            assert read.wait(10)  # it has read the identity as it was, and waits
            change()
        finally:
            go.set()
        racing.result(10)


LOOKUPS = {
    "secret": lambda ids, secret: ids.by_secret(secret),
    "address": lambda ids, secret: ids.by_route(route_key(secret)),
}
CHANGES = {
    "disabled": lambda ids, key: ids.disable(key.id),
    "revoked": lambda ids, key: ids.revoke(key.id),
    "rotated": lambda ids, key: ids.rotate_key(key.id),
    "removed": lambda ids, key: ids.remove(key.id),
}


@pytest.mark.parametrize("change", CHANGES)
@pytest.mark.parametrize("lookup", LOOKUPS)
def test_a_lookup_that_began_before_a_change_and_ends_after_it_keeps_nothing(identities, monkeypatch, lookup, change):
    key, secret = identities.add_key("Billing CI")
    _, other = identities.add_key("Other CI")
    identities.forget()  # nothing is kept: the lookup goes to the database
    _race(
        identities, monkeypatch, lambda: LOOKUPS[lookup](identities, secret), lambda: CHANGES[change](identities, key)
    )
    assert LOOKUPS[lookup](identities, secret) is None  # the next request is refused, on this instance too
    assert len(identities._cache) == 0  # what the lookup in flight read was old before it ended: nothing was kept
    assert identities.by_secret(other) is not None and len(identities._cache) == 1  # a lookup after the change is kept


def test_an_identity_looked_up_while_it_is_disabled_is_not_kept_as_enabled(identities, monkeypatch):
    emily, _ = identities.add_person("Emily Carter")
    identities.forget()
    _race(identities, monkeypatch, lambda: identities.get(emily.id), lambda: identities.disable(emily.id))
    assert identities.get(emily.id).disabled  # the next one reads the database again


# --- what is kept of the text a caller gives --------------------------------------------------------------------


def test_a_name_an_email_and_a_label_are_kept_without_control_characters(identities, admin, db):
    name = f"Emily {ESC}[31mCarter{ESC}[0m{NUL}{CSI}{RLO}{LRM}{RLI}"
    email = f"emily{ESC}]0;pwned{BELL}@acme.com"
    emily, code = identities.add_person(name, email)
    assert (emily.name, emily.email) == ("Emily [31mCarter[0m", "emily]0;pwned@acme.com")
    assert emily.ref().name == "Emily [31mCarter[0m"
    _, token, _ = identities.enrol(code, f"laptop{ESC}[2J{RLO}")
    assert [t.label for t in identities.tokens(emily.id)] == ["laptop[2J"]
    key, _ = identities.add_key(f"Billing{ESC} CI{NUL}")
    assert key.name == "Billing CI"
    changed, fields = identities.update(emily.id, name=f"Emily J. {RLO}Carter{CSI}", email=f"{NUL}ej@acme.com")
    assert (changed.name, changed.email, fields) == ("Emily J. Carter", "ej@acme.com", ["email", "name"])
    admin.issue(f"{ESC}[31mthe backup{RLO}")
    assert [t.label for t in admin.list()] == ["[31mthe backup"]
    identities.forget()  # read as the database has it
    assert identities.get(emily.id).name == "Emily J. Carter"
    seen = _texts(db)
    for bad in (ESC, BELL, NUL, CSI, RLO, LRM, RLI):
        assert bad not in seen
    assert identities.by_secret(token).id == emily.id


def _texts(db) -> str:
    """Every value of every text column of the test's schema, one after the other: what the tables show of the text a
    caller gave. The bytea columns, which hold sealed bytes, are left out: any single byte may be in them by chance."""
    out = []
    with db.connection() as conn:
        query = "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = %s AND data_type = 'text'"
        for table, column in conn.execute(query, (db.schema,)).fetchall():
            select = sql.SQL("SELECT {} FROM {}").format(sql.Identifier(column), sql.Identifier(table))
            out += [value for (value,) in conn.execute(select) if value is not None]
    return "\n".join(out)


def test_a_single_byte_is_looked_for_in_the_text_columns_only(db):
    identity_in(db)
    sealed = ESC.encode() + NUL.encode() + BELL.encode() + CSI.encode()  # bytes such as a sealed value holds, by chance
    with db.connection() as conn:
        conn.execute("INSERT INTO root_keys (identity_id, sealed, created) VALUES ('u_emilycarte', %s, 0)", (sealed,))
    assert ESC.encode() in dump(db) and NUL.encode() in dump(db)  # a dump of every column finds them: it is no test
    assert not any(bad in _texts(db) for bad in (ESC, BELL, NUL, CSI))
    assert "Emily Carter" in _texts(db)  # and the text columns are read


def test_a_lone_surrogate_in_a_name_is_dropped_where_the_driver_could_not_write_it(identities):
    lone = chr(0xD83D)  # half of a pair: it is no text, and cannot be written as UTF-8
    person, _ = identities.add_person(f"Emily {lone}Carter", f"emily{lone}@acme.com")
    assert (person.name, person.email) == ("Emily Carter", "emily@acme.com")


def test_a_name_an_email_and_a_label_are_cut_at_their_limits(identities, admin):
    assert (NAME_LENGTH, EMAIL_LENGTH, LABEL_LENGTH) == (200, 320, 100)
    emily, code = identities.add_person("n" * 500, "e" * 400)
    assert (emily.name, emily.email) == ("n" * 200, "e" * 320)
    identities.enrol(code, "l" * 300)
    assert [t.label for t in identities.tokens(emily.id)] == ["l" * 100]
    assert identities.update(emily.id, name="m" * 201, email="f" * 321)[0].name == "m" * 200
    identities.forget()
    assert identities.get(emily.id).email == "f" * 320
    key, _ = identities.add_key("k" * 250)
    assert key.name == "k" * 200
    admin.issue("a" * 150)
    assert [t.label for t in admin.list()] == ["a" * 100]


def test_a_text_that_is_nothing_once_cleaned_is_refused_or_given_a_name_of_its_own(identities, admin):
    for empty in (ESC, f"{NUL}{RLO}", f" {BELL} ", f"{ZWSP}{WJ}{BOM}", f"{LS} {PS}", ""):
        with pytest.raises(ValueError, match="name"):
            identities.add_person(empty)
        with pytest.raises(ValueError, match="name"):
            identities.add_key(empty)
    emily, code = identities.add_person("Emily Carter", "emily.carter@acme.com")
    assert identities.update(emily.id, name=f"{ESC}{RLO}{ZWSP}")[0].name == "Emily Carter"  # a blank name: as it was
    assert identities.update(emily.id, email=f"{ESC}{RLO}{BOM}")[0].email is None  # a blank email takes it away
    identities.enrol(code, f"{ESC}{RLO}{WJ}{LS}")
    assert [t.label for t in identities.tokens(emily.id)] == ["a computer"]
    admin.issue(f"{NUL}{ZWSP}")
    assert [t.label for t in admin.list()] == ["an administrator"]


# --- update tells the instances only what changed -------------------------------------------------------------------


async def test_an_update_is_told_only_when_a_row_changed_and_an_identity_that_is_not_there_gets_none(identities, bus):
    emily, _ = identities.add_person("Emily Carter", "emily.carter@acme.com")
    ready, told, heard = asyncio.Event(), asyncio.Event(), []

    async def ear():
        async with contextlib.aclosing(bus.listen("identities", ready=ready)) as messages:
            async for _, message in messages:
                heard.append((message["id"], message["event"]))
                if message["event"] == "disabled":
                    told.set()

    task = asyncio.create_task(ear())
    await asyncio.wait_for(ready.wait(), 5)
    same = ({"name": "Emily Carter"}, {"email": "emily.carter@acme.com"}, {}, {"name": "  "}, {"name": f"{ESC}{RLO}"})
    for nothing_new in same:  # what changes no row is told to no one
        assert identities.update(emily.id, **nothing_new) == (identities.current(emily.id), [])
    assert identities.update("u_nobody0000", name="Nobody") == (None, [])
    assert identities.update("u_nobody0000") == (None, [])
    changed, fields = identities.update(emily.id, name="Emily J. Carter", email="")
    assert fields == ["email", "name"]
    assert (changed.name, changed.email) == ("Emily J. Carter", None)
    identities.disable(emily.id)  # told after the update, on the same connection: it is heard last
    await asyncio.wait_for(told.wait(), 10)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert heard == [(emily.id, "updated"), (emily.id, "disabled")]


async def test_disabling_and_enabling_are_told_only_when_they_changed_something(identities, bus):
    emily, _ = identities.add_person("Emily Carter")
    ready, told, heard = asyncio.Event(), asyncio.Event(), []

    async def ear():
        async with contextlib.aclosing(bus.listen("identities", ready=ready)) as messages:
            async for _, message in messages:
                heard.append((message["id"], message["event"]))
                if message["event"] == "removed":
                    told.set()

    task = asyncio.create_task(ear())
    await asyncio.wait_for(ready.wait(), 5)
    assert identities.enable(emily.id) is False  # enabled already: nothing changed
    assert identities.disable(emily.id) is True
    assert identities.disable(emily.id) is False  # disabled already
    assert identities.enable(emily.id) is True
    assert identities.enable(emily.id) is False
    assert identities.disable("u_nobody0000") is False and identities.enable("u_nobody0000") is False  # no one
    assert identities.remove(emily.id)  # told last, on the same connection
    await asyncio.wait_for(told.wait(), 10)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert heard == [(emily.id, "disabled"), (emily.id, "enabled"), (emily.id, "removed")]


# --- what the memory holds, for the gate ------------------------------------------------------------------------------


def test_holds_tells_what_the_memory_serves_without_a_lookup(identities, clock):
    emily, code = identities.add_person("Emily Carter")
    _, token, _ = identities.enrol(code, "laptop")
    route = route_key(token)
    assert not identities.holds(secret=token) and not identities.holds(route=route) and not identities.holds()
    identities.by_secret(token)
    assert identities.holds(secret=token) and not identities.holds(route=route)  # the address was not looked up
    identities.by_route(route)
    assert identities.holds(route=route) and identities.holds(secret=token, route=route)
    assert not identities.holds(secret=token, route="b" * 26)  # one of the two is not held: a lookup would be made
    for guess in (TOKEN_PREFIX + "x" * 43, "not a token", ""):  # a stranger's guesses are never held
        assert not identities.holds(secret=guess)
    assert not identities.holds(route="b" * 26) and not identities.holds(route="short")
    order = list(identities._cache)
    assert identities.holds(secret=token) and list(identities._cache) == order  # it touches nothing of the memory
    identities.forget(emily.id)
    assert not identities.holds(secret=token) and not identities.holds(route=route)  # told of a change: gone
    identities.by_secret(token)
    clock.now += CACHE_SECONDS + 1
    assert not identities.holds(secret=token)  # past its seconds
    identities.by_secret(token)
    clock.now -= 3600
    assert not identities.holds(secret=token)  # and a clock that stepped back does not lengthen them


def test_remembered_answers_from_the_memory_alone_and_never_reads_the_database(identities, clock, monkeypatch):
    """What the gate serves a request it holds with, on the event loop: no store is asked (the pre-read's I1)."""
    emily, code = identities.add_person("Emily Carter")
    _, token, _ = identities.enrol(code, "laptop")
    route = route_key(token)
    assert identities.remembered(secret=token) is None and identities.remembered(route=route) is None
    identities.by_secret(token)
    identities.by_route(route)

    def read(*args, **kwargs):
        raise AssertionError("the database was read")

    monkeypatch.setattr(identities.db, "connection", read)
    monkeypatch.setattr(identities.db, "transaction", read)
    assert identities.remembered(secret=token) == emily and identities.remembered(route=route) == emily
    assert identities.remembered() is None
    for guess in (TOKEN_PREFIX + "x" * 43, "not a token", ""):
        assert identities.remembered(secret=guess) is None
    assert identities.remembered(route="b" * 26) is None and identities.remembered(route="short") is None
    identities.forget(emily.id)
    assert identities.remembered(secret=token) is None and identities.remembered(route=route) is None  # told: gone
    monkeypatch.undo()
    identities.by_secret(token)
    clock.now += CACHE_SECONDS + 1
    assert identities.remembered(secret=token) is None  # past its seconds


def test_holds_reads_the_memory_under_its_lock(identities):
    _, code = identities.add_person("Emily Carter")
    _, token, _ = identities.enrol(code, "laptop")
    identities.by_secret(token)
    started = threading.Event()

    def ask():
        started.set()
        return identities.holds(secret=token)

    with ThreadPoolExecutor(1) as pool, identities._lock:
        asking = pool.submit(ask)
        assert started.wait(10)
        with pytest.raises(TimeoutError):
            asking.result(timeout=0.3)  # it waits while the memory is being changed
    assert asking.result(10) is True


# --- a person disabled while a computer enrols gets no token ----------------------------------------------------------


def _until(check, seconds=10):
    """Wait until ``check()`` is true, looking every few milliseconds, for ``seconds`` at most."""
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "it did not come"
        time.sleep(0.005)


def _said(caplog):
    """What the identities' module said in the log, line by line."""
    return [r.getMessage() for r in caplog.records if r.name == identities_module.__name__]


def _blocked_by(db, pid):
    """How many sessions of PostgreSQL wait for a lock that the session ``pid`` holds."""
    with db.connection() as conn:
        query = "SELECT count(*) FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))"
        return conn.execute(query, (pid,)).fetchone()[0]


def test_a_person_disabled_while_a_computer_enrols_gets_no_token(identities, db):
    emily, code = identities.add_person("Emily Carter")
    pool = ThreadPoolExecutor(1)
    try:
        with db.transaction() as disabling:  # an administrator disabling Emily: made, not committed yet
            disabling.execute("UPDATE identities SET disabled = TRUE WHERE id = %s", (emily.id,))
            pid = disabling.execute("SELECT pg_backend_pid()").fetchone()[0]
            enrolling = pool.submit(identities.enrol, code, "laptop")
            _until(lambda: enrolling.done() or _blocked_by(db, pid))  # it runs through, or it waits for her row
        assert enrolling.result(10) is None  # the disabling is committed: the computer saw Emily disabled
    finally:
        pool.shutdown()
    assert identities.tokens(emily.id) == []


# --- a clock that steps back ------------------------------------------------------------------------------------------


def test_a_clock_that_steps_back_does_not_lengthen_what_the_memory_keeps(identities, db, clock):
    emily, code = identities.add_person("Emily Carter")
    _, token, _ = identities.enrol(code, "laptop")
    assert identities.by_secret(token).id == emily.id  # kept for CACHE_SECONDS
    with db.connection() as conn:  # disabled where this instance hears nothing of it: a message that was lost
        conn.execute("UPDATE identities SET disabled = TRUE WHERE id = %s", (emily.id,))
    clock.now += CACHE_SECONDS - 1
    assert identities.by_secret(token) is not None  # still within its seconds
    clock.now -= 3600  # the wall clock steps back an hour
    assert identities.by_secret(token) is None  # what was kept for five seconds is not kept for an hour: it asks again


# --- a change that is made is answered even when it cannot be told -----------------------------------------------------


@pytest.mark.parametrize("error", [redis.ConnectionError, redis.TimeoutError, OSError])
def test_a_change_that_is_made_is_answered_even_when_it_cannot_be_told(identities, bus, monkeypatch, caplog, error):
    def refuse(*args):
        raise error("cannot reach 10.0.0.9:6379 with the password s3cret-pw")

    monkeypatch.setattr(bus.client, "publish", refuse)
    with caplog.at_level(logging.WARNING, logger=identities_module.__name__):
        emily, code = identities.add_person("Emily Carter")  # made once, and answered: a retry would make a second
        _, token, _ = identities.enrol(code, "laptop")
        key, secret = identities.add_key("Billing CI")
        assert identities.update(emily.id, name="Emily J. Carter")[0].name == "Emily J. Carter"
        assert identities.disable(emily.id) and identities.enable(emily.id)
        new = identities.rotate_key(key.id)  # the old secret stopped; the new one is not lost with the message
        assert identities.by_secret(secret) is None and identities.by_secret(new).id == key.id
        assert identities.revoke_secret(token) and identities.revoke(key.id) == 1
        assert identities.remove(key.id)
    said = _said(caplog)
    assert len(said) == 9 and all(error.__name__ in line for line in said)  # one for each change, by the error's type
    assert "10.0.0.9" not in caplog.text and "s3cret-pw" not in caplog.text
    assert [i.name for i in identities.all()] == ["Emily J. Carter"]
    # Each line names the identity and its change, and what the instances that were not told keep doing: a revocation
    # is not undone in five seconds by them, it closes a space there at their next check.
    told = f"could not be told to the other instances ({error.__name__})"
    assert said[0] == f"a change of {emily.id} (added) {told}: they find it at their first lookup"
    [revoked] = [line for line in said if line.startswith(f"a change of {emily.id} (revoked)")]
    assert revoked.endswith("and a space of it there is closed at their next check of their spaces")


# --- what a change decides on, and when it is told ----------------------------------------------------------------


def test_a_code_or_a_rotation_asked_after_a_removal_elsewhere_finds_no_one(db, buses, clock):
    """Decided on the database as it is now, never on what this instance keeps of the identity for a few seconds."""
    here = Identities(db, buses[0], key=CREDENTIALS_KEY, clock=clock)
    there = Identities(db, buses[1], key=CREDENTIALS_KEY, clock=clock)
    emily, _ = here.add_person("Emily Carter")
    key, _ = here.add_key("Billing CI")
    assert here.get(emily.id) is not None and here.get(key.id) is not None  # kept here
    assert there.remove(emily.id) and there.remove(key.id)  # removed on another instance: not heard here yet
    assert here.code(emily.id) is None  # no code made for no one, and no driver's error
    with pytest.raises(NoSuchIdentity, match="no such API key"):
        here.rotate_key(key.id)


def test_changes_made_in_a_transaction_are_told_once_it_commits_and_never_when_it_fails(identities, bus, monkeypatch):
    emily, _ = identities.add_person("Emily Carter")
    told = []

    def publish(channel, message):
        told.append((channel, message["id"], message["event"]))

    def rolled_back():
        with identities.changing() as changes:
            assert identities.disable(emily.id, within=changes)
            raise RuntimeError("the entry that records it could not be written")

    monkeypatch.setattr(bus, "publish", publish)
    with pytest.raises(RuntimeError):
        rolled_back()
    assert told == [] and not identities.current(emily.id).disabled  # neither kept nor told
    with identities.changing() as changes:
        assert identities.disable(emily.id, within=changes)
        assert identities.update(emily.id, name="Emily J. Carter", within=changes)[1] == ["name"]
        assert told == []  # not before the commit: an instance told then could read the row as it was
    assert told == [("identities", emily.id, "disabled"), ("identities", emily.id, "updated")]
    assert identities.current(emily.id).disabled and "Emily" not in repr(changes)


def test_a_token_whose_tag_does_not_hold_is_listed_flagged_and_counted_apart(identities, db, clock):
    """Listed and flagged, never hidden: an administrator sees what the database holds. It finds no one."""
    emily, code = identities.add_person("Emily Carter")
    _, laptop, _ = identities.enrol(code, "laptop")
    clock.now += 1
    _, desktop, _ = identities.enrol(identities.code(emily.id), "desktop")
    first = identities.tokens(emily.id)[0]
    with db.connection() as conn:  # written by hand, without the server's key
        conn.execute("UPDATE tokens SET tag = %s WHERE id = %s", ("forged", first.id))
    assert [(t.label, t.tampered) for t in identities.tokens(emily.id)] == [("laptop", True), ("desktop", False)]
    assert identities.token_counts() == {emily.id: (2, 1)}
    identities.forget()
    assert identities.by_secret(laptop) is None and identities.by_secret(desktop).id == emily.id


def test_rows_whose_tag_does_not_hold_are_said_once_a_minute_for_each_kind(identities, db, clock, caplog):
    """A count for each kind of row: a token's line does not silence a code's, and each says how many of its own."""
    emily, code = identities.add_person("Emily Carter")
    _, token, _ = identities.enrol(code, "laptop")
    with db.connection() as conn:
        conn.execute("UPDATE tokens SET tag = 'forged' WHERE identity_id = %s", (emily.id,))

    def forged_code():
        fresh = identities.code(emily.id)
        with db.connection() as conn:
            conn.execute("UPDATE codes SET tag = 'forged' WHERE identity_id = %s", (emily.id,))
        assert identities.enrol(fresh, "desktop") is None

    with caplog.at_level(logging.WARNING, logger=identities_module.__name__):
        assert identities.by_secret(token) is None  # a token's row: said at once
        forged_code()  # a code's: said at once too
        identities.by_secret(token)
        forged_code()
        forged_code()
        clock.now += 61
        identities.by_secret(token)
        forged_code()
    refused = "whose tag does not hold was refused: its row was made or changed without this server's key"
    assert _said(caplog) == [
        f"A token {refused}",
        f"A one-time code {refused}",
        f"A token {refused} (2 since the line before)",
        f"A one-time code {refused} (3 since the line before)",
    ]


# --- an identity printed shows no name and no email -----------------------------------------------------------------


def test_an_identity_shows_neither_its_name_nor_its_email_nor_its_external_id_when_it_is_printed(identities):
    emily, _ = identities.add_person("Emily Carter", "emily.carter@acme.com", source="scim", external_id="ec-1")
    for shown in (repr(emily), str(emily), f"{emily!r}", repr([emily]), f"{emily} {emily!r}"):
        for private in ("Emily", "Carter", "emily.carter@acme.com", "ec-1"):
            assert private not in shown
    assert emily.id in repr(emily)  # what a log may name
    assert emily == identities.get(emily.id) and (emily.name, emily.email, emily.external_id) == (
        "Emily Carter",
        "emily.carter@acme.com",
        "ec-1",
    )


# --- where an identity came from, and the id the provider gave it ----------------------------------------------------


@pytest.mark.parametrize("source", ["local", "scim", "oidc"])
def test_a_source_is_one_of_three_and_is_kept(identities, source):
    person, _ = identities.add_person("Emily Carter", source=source, external_id="ec-1")
    assert person.source == source and identities.by_external("ec-1").id == person.id


@pytest.mark.parametrize("source", ["ldap", "", "Local", "scim ", f"oidc{chr(10)}", f"{ESC}scim"])
def test_a_source_that_is_not_one_of_the_three_is_refused_and_not_quoted(identities, source):
    with pytest.raises(ValueError, match=r"^a source is local, scim or oidc$"):  # a fixed sentence: it quotes nothing
        identities.add_person("Emily Carter", source=source)
    assert identities.all() == []


def test_the_database_refuses_a_source_that_is_not_one_of_the_three(db):
    with pytest.raises(psycopg.errors.CheckViolation), db.connection() as conn:
        conn.execute(
            "INSERT INTO identities (id, kind, name, created, source) VALUES ('u_forged0000', 'person', 'x', 0, 'ldap')"
        )


def test_an_external_id_is_kept_as_it_was_given(identities):
    exact = f" Ec-1 {chr(0xE9)}{ZWJ}{ZWNJ} "  # spaces at its ends, an accent, a joiner and a non-joiner
    person, _ = identities.add_person("Emily Carter", source="scim", external_id=exact)
    assert person.external_id == exact and identities.by_external(exact).id == person.id
    assert identities.by_external(exact.strip()) is None  # it must match the provider's byte for byte
    longest, _ = identities.add_person("Daniel Okafor", source="oidc", external_id="e" * EXTERNAL_ID_LENGTH)
    assert longest.external_id == "e" * EXTERNAL_ID_LENGTH


@pytest.mark.parametrize(
    "bad",
    [
        f"ec{ESC}1",
        f"ec{NUL}",
        f"ec{chr(10)}1",
        f"ec{CSI}",
        f"ec{RLO}1",
        f"ec{ZWSP}",
        f"ec{LS}",
        f"ec{chr(0xD83D)}",
        "e" * (EXTERNAL_ID_LENGTH + 1),
    ],
)
def test_an_external_id_with_a_control_character_or_over_the_cap_is_refused_and_not_cleaned(identities, bad):
    with pytest.raises(ValueError, match="external id") as raised:
        identities.add_person("Emily Carter", source="scim", external_id=bad)
    assert bad not in str(raised.value) and ESC not in str(raised.value)
    assert identities.all() == []  # nothing was made with the cleaned id


# --- what an instance remembers of use is bounded -------------------------------------------------------------------


def test_what_an_instance_remembers_of_use_is_bounded(identities, monkeypatch):
    monkeypatch.setattr("latet.server.identities.CACHE_SIZE", 3)
    for n in range(10):
        identities.seen(f"u_person{n:04d}")
    assert len(identities.notes._written) == 3  # the three written last
    assert identities.notes.due("seen:u_person0000")  # one dropped is written again, early: never later than a minute
    assert not identities.notes.due("seen:u_person0009")  # one kept is not


def test_the_uses_waiting_to_be_written_are_bounded(identities, monkeypatch):
    """A use past ``NOTES_WAITING`` waiting is dropped, never waited for: written at a later use."""
    monkeypatch.setattr("latet.server.identities.NOTES_WAITING", 2)
    release = threading.Event()
    held = identities_module._NOTING.submit(release.wait, 10)  # the notes' thread busy
    try:
        emily, _ = identities.add_person("Emily Carter")
        daniel, _ = identities.add_person("Daniel Okafor")
        oliver, _ = identities.add_person("Oliver Jackson")
        waiting = [identities.seen(emily.id), identities.seen(daniel.id)]
        assert all(future is not None for future in waiting) and identities.seen(oliver.id) is None  # dropped
    finally:
        release.set()
        held.result(10)
    for future in waiting:
        future.result(10)
    assert identities.current(emily.id).last_seen is not None and identities.current(oliver.id).last_seen is None
