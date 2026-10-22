"""The master key where the database keeps what it seals: each identity's root key, and the check value that stops a start
with a master key other than the one the database was first started with."""

import base64
import traceback
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.exceptions import InvalidTag

from latet.server.db import Database, DatabaseError
from latet.server.master import MasterKey, MasterKeyError, check_master_key, root_key

from .conftest import dump, identity_in

pytestmark = pytest.mark.server

FIRST, OTHER, LATER = b"\x05" * 32, b"\x06" * 32, b"\x07" * 32


def _shown(error: BaseException) -> str:
    """All an error shows of itself: its words, its repr, and its chain as a traceback prints it (no source lines)."""
    return f"{error}\n{error!r}\n{''.join(traceback.format_exception(error, limit=0))}"


def _raised(call) -> Exception:
    try:
        call()
    except Exception as error:
        return error
    pytest.fail("nothing was raised")


def test_a_root_key_is_made_once_and_kept_sealed_with_the_master_key(db):
    identity = identity_in(db)
    master = MasterKey([FIRST])
    key = root_key(db, master, identity)
    assert root_key(db, master, identity) == key and len(key) == 32 and key not in dump(db)
    with pytest.raises(InvalidTag):
        root_key(db, MasterKey([OTHER]), identity)  # another master key opens nothing


def test_a_root_key_sealed_for_one_identity_opens_for_no_other(db):
    master = MasterKey([FIRST])
    emily, daniel = identity_in(db), identity_in(db, "u_danielokaf", "Daniel Okafor")
    root_key(db, master, emily)
    with db.connection() as conn:  # someone with the database alone copies a sealed root key to another identity
        conn.execute(
            "INSERT INTO root_keys (identity_id, sealed, created) SELECT %s, sealed, created FROM root_keys "
            "WHERE identity_id = %s",
            (daniel, emily),
        )
    with pytest.raises(InvalidTag):
        root_key(db, master, daniel)


def test_two_instances_making_a_root_key_at_once_keep_the_first(db):
    master, identity = MasterKey([FIRST]), identity_in(db)
    with ThreadPoolExecutor(4) as pool:
        keys = list(pool.map(lambda _: root_key(db, master, identity), range(4)))
    assert len(set(keys)) == 1


def test_a_master_key_other_than_the_first_one_stops_the_start(db):
    first, other = MasterKey([FIRST]), MasterKey([OTHER])
    check_master_key(db, first)  # the first start seals the check value
    check_master_key(db, first)  # every start opens it
    with pytest.raises(MasterKeyError, match=r"LATET_MASTER_KEY_FILE.*LATET_MASTER_KEY") as stopped:
        check_master_key(db, other)
    shown = _shown(stopped.value)
    for key in (FIRST, OTHER):  # the words name the setting and never a key
        assert base64.b64encode(key).decode() not in shown and key.hex() not in shown and repr(key) not in shown
    assert stopped.value.__cause__ is None and stopped.value.__context__ is None
    assert b"master key check" not in dump(db)  # the check value is sealed like the rest


def test_a_master_key_with_a_version_added_still_opens_the_check_but_not_one_put_first(db):
    check_master_key(db, MasterKey([FIRST]))
    check_master_key(db, MasterKey([FIRST, LATER]))  # a rotation adds a line: the old one still opens what it sealed
    with pytest.raises(MasterKeyError, match="LATET_MASTER_KEY"):
        check_master_key(db, MasterKey([LATER, FIRST]))  # a version is a line's place: the first line is another key


def test_a_check_value_that_is_not_one_stops_the_start(db):
    master = MasterKey([FIRST])
    check_master_key(db, master)
    with db.connection() as conn:  # the table written by hand: junk, then a value sealed for another place...
        conn.execute("UPDATE master_check SET sealed = %s", (b"not a sealed value",))
    with pytest.raises(MasterKeyError, match="LATET_MASTER_KEY"):
        check_master_key(db, master)
    with db.connection() as conn:
        conn.execute("UPDATE master_check SET sealed = %s", (master.seal(b"the master key check", "elsewhere"),))
    with pytest.raises(MasterKeyError, match="LATET_MASTER_KEY"):
        check_master_key(db, master)
    with db.connection() as conn:  # sealed with the right key for the right place, and it says something else
        conn.execute("UPDATE master_check SET sealed = %s", (master.seal(b"another text", "master-check"),))
    with pytest.raises(MasterKeyError, match="LATET_MASTER_KEY"):
        check_master_key(db, master)


def test_two_instances_starting_at_once_with_the_same_key_both_start(db):
    master = MasterKey([FIRST])
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: check_master_key(db, master), range(4)))


def test_two_instances_starting_at_once_with_two_keys_leave_one_of_them_standing(db):
    keys = [MasterKey([FIRST]), MasterKey([OTHER])] * 2

    def starts(master):
        try:
            check_master_key(db, master)
        except MasterKeyError:
            return False
        return True

    with ThreadPoolExecutor(4) as pool:
        started = list(pool.map(starts, keys))
    assert started.count(True) == 2 and started[0] == started[2] and started[1] == started[3]  # the first one's key
    assert started[0] != started[1]


def test_a_lost_check_row_does_not_let_a_wrong_key_in(db):
    first, other = MasterKey([FIRST]), MasterKey([OTHER])
    root_key(db, first, identity_in(db))  # an identity has its root key, sealed with the first key
    check_master_key(db, first)
    with db.connection() as conn:  # a partial restore, a manual delete
        conn.execute("DELETE FROM master_check")
    with pytest.raises(MasterKeyError, match=r"LATET_MASTER_KEY_FILE.*LATET_MASTER_KEY"):
        check_master_key(db, other)
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM master_check").fetchone() == (0,)  # the wrong key sealed nothing
    check_master_key(db, first)  # the right one is let in, and seals the check value again
    with pytest.raises(MasterKeyError):
        check_master_key(db, other)


def test_a_database_with_no_root_key_yet_takes_the_first_key_it_is_given(db):
    check_master_key(db, MasterKey([OTHER]))  # nothing to vouch for it, nothing to contradict it
    with pytest.raises(MasterKeyError):
        check_master_key(db, MasterKey([FIRST]))


def test_a_driver_error_at_the_check_is_said_by_its_type_and_not_by_its_words(settings, schema):
    db = Database(settings.database_url, schema=schema)  # never migrated: the table is not there
    try:
        error = _raised(lambda: check_master_key(db, MasterKey([FIRST])))
    finally:
        db.close()
    assert type(error) is DatabaseError and "UndefinedTable" in str(error)
    shown = _shown(error)  # the server's words quote the table and the statement
    assert "master_check" not in shown and "does not exist" not in shown and "LINE" not in shown
    assert error.__cause__ is None and error.__context__ is None
    closed = Database(settings.database_url, schema=schema, min_size=0)
    closed.close()
    error = _raised(lambda: check_master_key(closed, MasterKey([FIRST])))
    assert type(error) is DatabaseError and "PoolClosed" in str(error)
