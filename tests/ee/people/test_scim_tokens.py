"""SCIM's bearer tokens: kept by their keyed hash, a keyed tag over each row, each with its maker and its end
(ee/scim.py)."""

import pytest

from latet.ee.scim import DAY, TOKEN_PREFIX

from .server_support import BY_HAND, SERVER_TESTS

pytestmark = SERVER_TESTS


def test_a_token_is_kept_by_its_keyed_hash_with_its_maker_and_ends(cluster, clock):
    tokens = cluster.server.edition.scim_tokens
    made, token = tokens.make(made_by=BY_HAND)
    assert token.startswith(TOKEN_PREFIX) and tokens.check(token) == made.id
    with cluster.server.db.connection() as conn:
        rows = conn.execute("SELECT hash, tag, made_by FROM scim_tokens").fetchall()
    assert [row[2] for row in rows] == [BY_HAND] and all(token not in row[0] + row[1] for row in rows)
    [listed] = tokens.all()
    assert (listed.id, listed.made_by, listed.expires) == (made.id, BY_HAND, clock.now + 90 * DAY)
    assert tokens.any()
    clock.advance(90 * DAY)
    assert tokens.check(token) is None and not tokens.any()


def test_a_row_whose_end_or_maker_was_moved_finds_nothing(cluster):
    tokens = cluster.server.edition.scim_tokens
    _made, token = tokens.make(made_by=BY_HAND, days=1)
    with cluster.server.db.transaction() as conn:
        conn.execute("UPDATE scim_tokens SET expires = NULL")
    assert tokens.check(token) is None
    _made, other = tokens.make(made_by=BY_HAND, days=1)
    with cluster.server.db.transaction() as conn:
        conn.execute("UPDATE scim_tokens SET made_by = 'u_emilycartr' WHERE expires IS NOT NULL")
    assert tokens.check(other) is None


def test_a_token_names_its_maker_and_a_life_within_bounds(cluster):
    tokens = cluster.server.edition.scim_tokens
    for made_by, days in (("", 0), (BY_HAND, -1), (BY_HAND, 3651)):
        with pytest.raises(ValueError, match=r"names who made it|lasts 1 to"):
            tokens.make(made_by=made_by, days=days)
    made, token = tokens.make(made_by=BY_HAND, days=None)
    assert made.expires is None and tokens.check(token) == made.id
    assert tokens.remove(made.id) and tokens.check(token) is None and not tokens.remove(made.id)
    assert tokens.check("latet_pat_" + token) is None and tokens.check(TOKEN_PREFIX + "x\N{EM DASH}") is None


def test_a_token_made_in_a_transaction_that_fails_is_not_kept(cluster):
    tokens = cluster.server.edition.scim_tokens
    with pytest.raises(RuntimeError, match="its entry"):
        _made_then_a_failure(cluster.server.db, tokens)
    assert tokens.all() == []


def _made_then_a_failure(db, tokens):
    with db.transaction() as conn:
        tokens.make(made_by=BY_HAND, conn=conn)
        raise RuntimeError("its entry could not be written")
