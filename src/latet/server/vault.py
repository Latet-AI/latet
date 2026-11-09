"""An identity's vault in PostgreSQL (``storage.db.Store`` on a computer): its values and their fakes sealed with its
root key, beside a keyed hash of each, so that the database refuses a second entity for one value (the first type
wins, as in a vault's memory) and a second value for one fake (the vault then tries its next counter); and every string
the vault uses, a value's or a fake's, and how a fake written in escapes reads (``vault.fake_reading``: `Giulia
Martini` for `Giulia%20Martini`), by its keyed hash (``vault_strings``), so that a fake is never a value the vault
knows, nor reads as one, whichever instance makes it. A fake its entry no longer has (its value forgotten, or the fake
given anew) is retired, never another value's: its string's row stays, of no entry, as long as the vault (``_RETIRED``,
the final security review's H2), so that a text masked before is never unmasked to another value. The local reviewer's
answers and the decisions on images, sealed too.

What is sealed opens in its own row alone, and for its own purpose: a value or a fake for its entry, as that entry's
value or fake, and while the row says the type and the class it was found as (a class changed to STRUCT would leave the
value out of those the masker finds in clear); a review's answer for the text it answers; a decision for the image it
was made on. Someone who writes the database without the master key can move no sealed value to another row, nor
change the type or the class it was found as, without it ceasing to open (an entry's rule, how its value was found
(``decoded``, migration 005), counter and time are in clear and bound to nothing; a rule names the field a value sat in
by its kind alone, since a field's name can be a value: ``modules.kept``). An entry that does not open is counted in
``unreadable`` and said in the log once, by its id; its value, found again, is masked as it is found, in memory, and the
entry left as it is.

What sealing cannot stop, he can still do: put back into its own row a sealed value he kept, which opens there again.
For a review's answer or a decision on an image, replaced in place, that restores an older verdict ("nothing to mask",
"send"), even after ``forget_reviews``: no count he cannot roll back binds them to a generation. Nor are the keyed
hashes beside a value and its fake (``original_key``, ``surrogate_key``) bound to anything: swapping two entries'
hashes, he makes two values of one identity share one fake (an instance that does not hold the value in memory then
finds the other's entry for it), as he can corrupt or roll back any row (the accepted limits S-14 and S-18): no value
in clear, nothing of another identity's, and a value a rule covers masked all the same. E4's export of what is kept and
E0c's least-privileged role for the database are the answer, as for the rest of what the database's writer can roll
back.

An API key's space reads its vault as it needs it rather than whole (``latet.state.protocols.KeyVaultStore``): its
count, its newest values, one value by itself or by its fake (their keyed hashes above), and the values and fakes a
text may hold, by their anchor words (``engine.anchors``: the longest run of plain letters and digits of each, or of
letters of another script; in a script written without spaces, the first two characters of its run). Beside each
entry, ``vault_words`` keeps the keyed hash of its value's word and of its fake's (``stored.word_of``: an address
that names a person, an IPv4 address or a domain by its dotted word, Task 10's F3 to F5, an address's fake by its local
part with its domain's name, version 8), each under a label of its own (a value's word and a fake's are never one hash),
NULL for a string that has none, and the edges of a string the vault finds inside a word (``stored.edges_of``, Ruling
T10h): a word and its edge are the two halves of one keyed pair (``Sealer.keyed_pair``, version 9), so that no edge is
the hash of a word and the hashes do not show that a string begins or ends with another's anchor (review 10b's M5).
A secret held glued (``stored.held_glued``: no word of a text finds it glued to letters or digits) has a row of the
role ``held`` too, beside its anchor and its edges, its word NULL, by which a space loads the newest of them
(``load_held``, the server's ``key_held_secrets``, Ruling 10b-R3). Each row has the version of the anchors it was made
with (``ANCHORS_VERSION``): written with the entry, the fake's again with a fake given anew, dropped with the entry by
the foreign key. A word is kept as its keyed hash alone, which says nothing of it without the root key; what the hashes
show is which entries share a word (as several addresses of one domain do), and how many, and for a name in a script
written without spaces, whose word is its first two characters (a surname and the next one), the groups that share one
are larger: the accepted cost of finding a value in a text without holding the whole vault. A ``held`` row says one bit
more of a value's shape: a short secret, or one whose anchor touches its edge. An entry kept before the words were, or
with the words of an older version, gets them once, from whichever instance builds the space first
(``backfill_words``).

Beside each word's keyed hash, its print (``latet.server.prints``, migration 013): a cheap keyed fingerprint of the word
under a key of its own derived from the root key, the slot of its role, which an instance holds in memory for the whole
vault in a few megabytes (``latet.server.index``) to find which words of a text are any value's or fake's; and the count
of the change that wrote the row last (``counted``). A print says nothing a keyed hash does not: which rows share a word
of one role. A vault kept by 0.10.0 gets its prints once, in the background, on one instance at a time
(``fill_prints``), a print written only beside the word whose keyed hash is the row's.

A change the other instances must hear of (a value kept, the rule of one, a value forgotten) bumps the identity's change
counter (``change_counters``) in its own transaction, and is told once committed, with that count: ``changed(kind,
entity_id, counter)``, kind ``entity``, ``rule`` or ``forgotten``. The statement that moves the count stamps the word
rows of the entries it counts with it, and a part of the backfill of the words or of the prints moves it once and is
told as ``words`` (no entry's): an instance's index of prints catches up by the rows counted past what it read. An
instance that missed a change finds it by the counter.
An applications call keeps its new values together (``together``, Ruling T10i): in one transaction, the counter
moved once, last, by as many as they are, and each told once they are committed, with a count of its own. A few calls
of an instance at once (``Database.together``, Ruling 10b-R2); past them, a call keeps each value at once.

Who calls (``latet.state.VaultStore``): ``save_entity`` and ``save_rule`` the threads that mask, inside the vault's
lock. A new value costs seven round trips to PostgreSQL (its transaction, the entry, its two strings, its two words,
the count), the last a commit that waits for PostgreSQL's disk, and its announcement; the space's other new values wait
for it. In an applications call, its savepoint, the entry, its strings and its words (one round trip each), and the
call's one commit. A rule is kept at once, in a transaction of its own. ``load_review`` and ``save_review`` are called
on the event loop too, and there each waits for PostgreSQL: accepted on a server for now; they write no row of the
vault, which a call's transaction holds. ``forget_entity`` runs in the dashboard's work threads, and waits for a row
another transaction holds ``FORGET_LOCK_SECONDS`` at most (review 10b's M2). The pool bounds the wait for a connection
(ten seconds) and for a new one (``connect_timeout``); a PostgreSQL that stops answering on a connection already open
holds the loop until the network gives up."""

from __future__ import annotations

import collections
import contextlib
import hashlib
import hmac
import json
import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, Final, LiteralString, NamedTuple

import psycopg
from cryptography.exceptions import InvalidTag
from psycopg import sql

from latet.engine import modules
from latet.engine.anchors import ANCHORS_VERSION, EDGE
from latet.engine.known import LET_GO
from latet.engine.lookup import LookupBudget, TooMany
from latet.engine.response_guard import latet_key
from latet.engine.stored import WORKING_VALUES, edges_of, held_glued, word_of
from latet.engine.types import EntityClass
from latet.engine.vault import Entity, EntryGone, FakeTaken, MaskingError, fake_reading
from latet.server.index import BUILD_TURNS, IndexTooLarge, PrintCache, PrintIndex, _reading, gone_owner
from latet.server.prints import SLOT, print_key, prints
from latet.server.sealing import DERIVE, Sealer, derived

if TYPE_CHECKING:
    from psycopg import Connection

    from latet.server.db import Database
    from latet.state.protocols import JobLock, Role

log = logging.getLogger(__name__)
_REVIEW_DAYS = 90  # a text not sent again for this long is reviewed again if it ever comes back
# Values one identity's vault may hold (ServerSettings.vault_limit; an API key may have a limit of its own,
# ``Identity.vault_limit``): at 2.4 to 2.8 KiB a value of a few dozen characters (measured, the parts that match it
# compiled), a full vault is about 550 MiB, a quarter of the default memory budget; a hundred new values a working day
# reach it after eight years.
VAULT_LIMIT = 200_000
# The rows of a vault made Python's and opened between two lets-go of the GIL, as a vault is read: a few milliseconds.
ROWS_AT_ONCE = 1_000
# The words one statement asks for (``with_words``): a text of more is asked for a part at a time. The ids of entries,
# as many (``by_ids``).
WORDS_AT_ONCE = 5_000
# The different words one call asks for by statements at most (Ruling T7c). A text of more (two megabytes of Chinese:
# two million pairs of characters, four hundred statements) has the vault's own words read instead, once, and met with
# its words in memory: a call costs the words of the text or those of the vault, whichever are fewer, and drops none.
WORDS_ASKED_AT_MOST = 50_000
# The entries one call opens at most (Ruling T7c): past it the call is refused (``TooManyCandidates``), never answered
# with a part of them, which would leave a value of the vault unmasked or a fake unrestored. A text's words find many
# values when it holds words that many share (a common first name, a domain), and each found is opened and held in
# memory while the call lasts: a hundred thousand is half of a vault at the default limit, about 280 MiB of values at
# 2.8 KiB a value and a second or two of opening, where an ordinary call (a thousand lines of a log) finds a few
# thousand. Task 10 measures it.
CANDIDATES_AT_MOST = 100_000
# Seconds a forget waits for a row another transaction holds (``forget_entity``): a call that keeps its values together
# holds its new rows until it ends, a few seconds; a forget waits no longer than this, then fails, to be tried again.
FORGET_LOCK_SECONDS = 5
# Seconds an instance holds the job of giving a vault's word rows their prints (``fill_prints``) before another may take
# it, should the instance stop meanwhile. One that runs longer (a vault of millions of values) may meet another
# instance's run on its last parts: each writes only the prints that are missing, and nothing is written twice.
PRINTS_HOLD_SECONDS = 900
# Why a vault's index is not ready, as the space's log line says it (``PgVaultStore.index_failure``): the prints its
# words lack, or the type of the error that stopped a build or a catch-up.
PRINTS_UNMADE = "its prints are not all made"
INDEX_TOO_LARGE = "its vault is past the size an index is built for"  # ``index.IndexTooLarge``
# What changed by name (``PgVaultStore.named_after``), kept for the vaults that resync their memory by it: so many
# tellings and so many names at most, the oldest let go first (a vault further behind confirms its whole memory). A
# vault takes in every telling since its last at each call, so that it is rarely more than a few behind; and names
# past a lookup's part buy nothing a whole resync does not (a vault holds its working set, and confirms no more of it by
# name than whole): the log is about 650 KB at most (20-character ids in sets), counted in the space's weight
# (``index_bytes``).
NAMED_KEPT = 256
NAMED_IDS_KEPT = WORDS_AT_ONCE
_TELLING_BYTES: Final = 64  # a telling's tuple and its place in the log, besides its names

_SELECT: Final = (
    "SELECT id, etype, eclass, original, surrogate, counter, created, rule, decoded FROM vault_entities"
    " WHERE identity_id = %s"
)
# The keyed hashes of an entry's anchor words, its value's and its fake's (NULL: a string without one), written with
# the entry in its transaction, each with its print (``prints``: its role's of the word's four; NULL with the word).
_WORDS: Final = (
    "INSERT INTO vault_words (identity_id, entity_id, role, word, version, print)"
    " VALUES (%s, %s, 'original', %s, %s, %s), (%s, %s, 'surrogate', %s, %s, %s)"
    " ON CONFLICT (identity_id, entity_id, role) DO UPDATE SET word = EXCLUDED.word, version = EXCLUDED.version,"
    " print = EXCLUDED.print"
)
# The edges of an entry's value and fake (``stored.words_of``: the head and the tail of a string the vault finds inside
# a word, Ruling T10h), each a row of its own role (``original-head``...), its word a keyed hash under its string's
# label, with its print.
_EDGES: Final = (
    "INSERT INTO vault_words (identity_id, entity_id, role, word, version, print)"
    " SELECT %s, %s, r.role, r.word, %s, r.print"
    " FROM unnest(%s::text[], %s::bytea[], %s::int8[]) AS r (role, word, print)"
    " ON CONFLICT (identity_id, entity_id, role) DO UPDATE SET word = EXCLUDED.word, version = EXCLUDED.version,"
    " print = EXCLUDED.print"
)
# An entry's fake's edges, gone with its fake given anew.
_FAKE_EDGES_GONE: Final = (
    "DELETE FROM vault_words WHERE identity_id = %s AND entity_id = %s AND role IN ('surrogate-head', 'surrogate-tail')"
)
# Its fake's word, with its print, when its fake is given anew (``replace_fake``): in place of the old one's, or the
# first word of an entry kept before the words were (``backfill_words`` adds its value's, and leaves this one as it is).
_FAKE_WORD: Final = (
    "INSERT INTO vault_words (identity_id, entity_id, role, word, version, print)"
    " VALUES (%s, %s, 'surrogate', %s, %s, %s)"
    " ON CONFLICT (identity_id, entity_id, role) DO UPDATE SET word = EXCLUDED.word, version = EXCLUDED.version,"
    " print = EXCLUDED.print"
)
# The statements that read an API key's entries by their words name the identity as a value PostgreSQL's planner is
# not shown (``(SELECT %s::text)``, known once the statement runs), so that it plans for an identity of the average
# size. Shown the value, it plans for what its statistics say of that identity, and a key made since the tables were
# last analyzed (once a tenth of a table has changed) is one row to them: its entries were then read in the order of
# ``seq``, each with a scan of the key's words, in time that grows with the square of its values (a minute for a call at
# 12,000 values, Task 10's F2; the values without anchor of a space's build, two minutes). Planned for many rows, a
# statement reads the words first, by their index, then their entries by their key: time that grows with what the words
# find.
#
# The entries whose value's (or fake's) word, or one of its edges, is one of those asked for (the roles asked for:
# ``_roles``), as many as a call may still open and one more.
_WITH_WORDS: Final = (
    "SELECT id, etype, eclass, original, surrogate, counter, created, rule, decoded FROM vault_entities"
    " WHERE identity_id = (SELECT %s::text) AND id IN (SELECT w.entity_id FROM vault_words w"
    " WHERE w.identity_id = (SELECT %s::text) AND w.role = ANY(%s) AND w.word = ANY(%s)) ORDER BY seq LIMIT %s"
)
# The stored vault's bounded path. The predicates and row order are the ordinary word lookup's. The limited
# candidate set is sized inside PostgreSQL; payload past the remaining aggregate allowance becomes NULL before
# transfer. Even fetchmany(ROWS_AT_ONCE) can therefore transfer no more than that allowance, not N times it.
# A NULL sentinel is refused, never mistaken for an unreadable entry or an absent candidate. The entries of the words
# found are asked as an array of their ids (``id = ANY(ARRAY(...))``, the same entries as ``id IN (...)``): PostgreSQL
# reads the words by their index, then the entries by their key, whatever the vault's size, where ``IN`` let it
# hash-join a scan of every entry of the key (Task 8's fix round 4: 68 ms a statement at 200,000 values, 25 this way;
# 18 and 17 at 2,000). Sent unprepared (``_unprepared``), as every bounded lookup.
_BOUNDED_WORDS: Final = (
    "WITH candidates AS MATERIALIZED ("
    " SELECT id, etype, eclass, original, surrogate, counter, created, rule, decoded, seq,"
    " octet_length(original)::bigint + octet_length(surrogate) + octet_length(id) + octet_length(etype)"
    " + coalesce(octet_length(rule), 0) AS payload FROM vault_entities"
    " WHERE identity_id = (SELECT %s::text) AND id = ANY(ARRAY(SELECT w.entity_id FROM vault_words w"
    " WHERE w.identity_id = (SELECT %s::text) AND w.role = ANY(%s) AND w.word = ANY(%s))) ORDER BY seq LIMIT %s"
    "), sized AS (SELECT *, sum(payload) OVER (ORDER BY seq) <= %s AS fits FROM candidates)"
    " SELECT CASE WHEN fits THEN id END, CASE WHEN fits THEN etype END, eclass,"
    " CASE WHEN fits THEN original END, CASE WHEN fits THEN surrogate END, counter, created,"
    " CASE WHEN fits THEN rule END, decoded, payload FROM sized ORDER BY seq"
)
# Every role of each requested print is fetched. A print-only cache must never mark a role-filtered subset
# complete: two roles can collide. Decrypted anchors/edges are validated after this complete fetch.
_BY_PRINTS: Final = (
    "WITH candidates AS MATERIALIZED ("
    " SELECT id, etype, eclass, original, surrogate, counter, created, rule, decoded, seq,"
    " ARRAY(SELECT DISTINCT p.print FROM vault_words p WHERE p.identity_id = e.identity_id"
    " AND p.entity_id = e.id AND p.print = ANY(%s)) AS prints,"
    " octet_length(original)::bigint + octet_length(surrogate) + octet_length(id) + octet_length(etype)"
    " + coalesce(octet_length(rule), 0) AS payload FROM vault_entities e"
    " WHERE identity_id = (SELECT %s::text) AND id IN (SELECT w.entity_id FROM vault_words w"
    " WHERE w.identity_id = (SELECT %s::text) AND w.print = ANY(%s)) ORDER BY seq LIMIT %s"
    "), sized AS (SELECT *, sum(payload + 8 * cardinality(prints)) OVER (ORDER BY seq) <= %s AS fits FROM candidates)"
    " SELECT CASE WHEN fits THEN id END, CASE WHEN fits THEN etype END, eclass,"
    " CASE WHEN fits THEN original END, CASE WHEN fits THEN surrogate END, counter, created,"
    " CASE WHEN fits THEN rule END, decoded, payload + 8 * cardinality(prints),"
    " CASE WHEN fits THEN prints END FROM sized ORDER BY seq"
)
_BOUNDED_IDS: Final = (
    "WITH candidates AS MATERIALIZED ("
    " SELECT id, etype, eclass, original, surrogate, counter, created, rule, decoded, seq,"
    " octet_length(original)::bigint + octet_length(surrogate) + octet_length(id) + octet_length(etype)"
    " + coalesce(octet_length(rule), 0) AS payload FROM vault_entities"
    " WHERE identity_id = (SELECT %s::text) AND id = ANY(%s) ORDER BY seq LIMIT %s"
    "), sized AS (SELECT *, sum(payload) OVER (ORDER BY seq) <= %s AS fits FROM candidates)"
    " SELECT CASE WHEN fits THEN id END, CASE WHEN fits THEN etype END, eclass,"
    " CASE WHEN fits THEN original END, CASE WHEN fits THEN surrogate END, counter, created,"
    " CASE WHEN fits THEN rule END, decoded, payload FROM sized ORDER BY seq"
)
# Every word of the vault's values (or of its fakes), their edges too, with its entry: what a text of too many words is
# met with.
_ALL_WORDS: Final = (
    "SELECT word, entity_id FROM vault_words WHERE identity_id = %s AND role = ANY(%s) AND word IS NOT NULL"
)
_BY_IDS: Final = _SELECT + " AND id = ANY(%s) ORDER BY seq"
_UNANCHORED: Final = (
    "SELECT id, etype, eclass, original, surrogate, counter, created, rule, decoded FROM vault_entities"
    " WHERE identity_id = (SELECT %s::text) AND id IN (SELECT w.entity_id FROM vault_words w"
    " WHERE w.identity_id = (SELECT %s::text) AND w.word IS NULL AND w.role IN ('original', 'surrogate')) ORDER BY seq"
)
# The newest secrets held glued (a `held` row, version 9, Ruling 10b-R3), as many as asked: the newest first.
_GLUED_NEWEST: Final = (
    "SELECT id, etype, eclass, original, surrogate, counter, created, rule, decoded FROM vault_entities"
    " WHERE identity_id = (SELECT %s::text) AND id IN (SELECT w.entity_id FROM vault_words w"
    " WHERE w.identity_id = (SELECT %s::text) AND w.word IS NULL AND w.role = 'held') ORDER BY seq DESC LIMIT %s"
)
# The values whose value or fake no word finds glued on both sides (``stored.glued_both``: a head and a tail row of one
# role), the oldest first: what a space that keeps them all holds in memory for good (``load_glued``).
_GLUED_BOTH: Final = (
    "SELECT id, etype, eclass, original, surrogate, counter, created, rule, decoded FROM vault_entities"
    " WHERE identity_id = (SELECT %s::text) AND id IN (SELECT h.entity_id FROM vault_words h JOIN vault_words t"
    " ON t.identity_id = h.identity_id AND t.entity_id = h.entity_id AND t.role = replace(h.role, '-head', '-tail')"
    " WHERE h.identity_id = (SELECT %s::text) AND h.role IN ('original-head', 'surrogate-head')) ORDER BY seq"
)


def _unprepared(cursor: psycopg.Cursor[Any], query: LiteralString | sql.Composed, args: tuple[Any, ...]) -> None:
    """A bounded lookup sent never prepared, planned for its own arguments each time: psycopg prepares a statement on a
    connection after five executions, and PostgreSQL then plans it once for any arguments (a generic plan), which on a
    small vault walks every entry of the key in its order of keeping (Task 8's fix round 4: 227 ms a statement at 2,000
    values, against 18 planned for its own words; 47 against 12 for ``_BY_PRINTS``)."""
    cursor.execute(query, args, prepare=False)


def _guarded(query: LiteralString) -> sql.Composed:
    """Compose only this module's literal retention selectors; identities and limits remain bound parameters."""
    sized = query.replace(
        "decoded FROM",
        "decoded, seq, octet_length(original)::bigint + octet_length(surrogate)"
        " + octet_length(id) + octet_length(etype) + coalesce(octet_length(rule), 0) AS payload FROM",
    )
    return sql.SQL(
        "WITH candidates AS MATERIALIZED ({} LIMIT %s),"
        " sized AS (SELECT *, sum(payload) OVER (ORDER BY seq) <= %s AS fits FROM candidates)"
        " SELECT CASE WHEN fits THEN id END, CASE WHEN fits THEN etype END, eclass,"
        " CASE WHEN fits THEN original END, CASE WHEN fits THEN surrogate END, counter, created,"
        " CASE WHEN fits THEN rule END, decoded, payload FROM sized ORDER BY seq"
    ).format(sql.SQL(sized))


_BOUNDED_UNANCHORED: Final = _guarded(_UNANCHORED)
_BOUNDED_HELD: Final = _guarded(_GLUED_NEWEST.removesuffix(" LIMIT %s"))
_BOUNDED_GLUED: Final = _guarded(_GLUED_BOTH)
# How many values the vault holds, those that do not open as well (``count_entities``).
_COUNTED: Final = "SELECT COUNT(*) FROM vault_entities WHERE identity_id = %s"
# The dashboard's page of a vault kept in PostgreSQL (``listed``): how many values of each type it holds, whatever is
# shown; the entries of a type and a class (an empty type, a NULL class: any), the newest first, by id among those of
# the same instant, in the order of its characters (as Python sorts them); and how many there are.
_TYPES: Final = "SELECT etype, COUNT(*) FROM vault_entities WHERE identity_id = %s GROUP BY etype"
_OF_KIND: Final = _SELECT + " AND (%s::text = '' OR etype = %s) AND (%s::int IS NULL OR eclass = %s)"
_NEWEST: Final = ' ORDER BY created DESC, id COLLATE "C"'
_OF_KIND_COUNTED: Final = (
    "SELECT COUNT(*) FROM vault_entities WHERE identity_id = %s"
    " AND (%s::text = '' OR etype = %s) AND (%s::int IS NULL OR eclass = %s)"
)
# The entries of one class, the oldest first (``of_class``).
_OF_CLASS: Final = _SELECT + " AND eclass = %s ORDER BY seq"
# How many words of the current version (or a later one) the entries lack: each has two, its value's and its fake's
# (a word row names an entry that is there, once for each role; the edges are not counted, as few entries have them),
# so none when there are twice as many of them as entries. Two counts, which a vault of millions answers in a fraction
# of the time that looking for the entries without them takes (``_WORDLESS``, every entry's words read).
_MISSING: Final = (
    "SELECT (SELECT COUNT(*) FROM vault_entities WHERE identity_id = %s) * 2"
    " - (SELECT COUNT(*) FROM vault_words WHERE identity_id = %s AND role IN ('original', 'surrogate')"
    " AND version >= %s)"
)
# The entries that lack a word of the current version, a part at a time after the one read last: ``_SELECT``'s
# columns, then ``seq`` and the keyed hashes of their value and fake (``_HELD``). A word of a later version (written
# by an instance of a newer Latet, while the instances are upgraded one by one) is not lacking: it stays as it is.
_WORDLESS: Final = (
    "SELECT id, etype, eclass, original, surrogate, counter, created, rule, decoded, seq, original_key,"
    " surrogate_key"
    " FROM vault_entities e WHERE identity_id = %s AND seq > %s"
    " AND (SELECT COUNT(*) FROM vault_words w WHERE w.identity_id = e.identity_id AND w.entity_id = e.id"
    " AND w.role IN ('original', 'surrogate') AND w.version >= %s) < 2 ORDER BY seq LIMIT %s"
)
# The entries with a NULL word of their value or fake, found by the index of their words: a value or a fake with no
# anchor, or one of an entry that did not open when its words were made, whose word is read again. Not a secret held
# glued (its `held` row is NULL by design): a build would read every one of them again (review 10b's N1).
_NULL_WORDS: Final = (
    "SELECT id, etype, eclass, original, surrogate, counter, created, rule, decoded, seq, original_key,"
    " surrogate_key"
    " FROM vault_entities WHERE identity_id = (SELECT %s::text) AND seq > %s"
    " AND id IN (SELECT w.entity_id FROM vault_words w WHERE w.identity_id = (SELECT %s::text) AND w.word IS NULL"
    " AND w.role IN ('original', 'surrogate')) ORDER BY seq LIMIT %s"
)
# A part's entries as they are now, held until their words are written: one forgotten or given a new fake since it was
# read waits, or is left out (a word whose string's keyed hash is no longer the one read is not written). Held in the
# order of ``seq``, as the store's other writes of several entries hold them (``_cut_field_names``, in that order
# whichever way the entries were read, and another instance's part): no two of them wait for each other.
_HELD: Final = (
    "SELECT id, original_key, surrogate_key FROM vault_entities WHERE identity_id = %s AND id = ANY(%s)"
    " ORDER BY seq FOR SHARE"
)
# Their edges of an older version, gone: those of the current version are written with their words (``_ADD_WORDS``).
_OLD_EDGES: Final = (
    "DELETE FROM vault_words WHERE identity_id = %s AND entity_id = ANY(%s)"
    " AND role NOT IN ('original', 'surrogate') AND version < %s"
)
# Their words and edges, each with its print: each one not there yet, each one of an older version, and each NULL one
# that has a word now (of no later version: a newer Latet's NULL stays its own); one of the current version written
# meanwhile stays as it is (one kept by 0.10.0 gets its print from ``fill_prints``). The words written, by entry.
_ADD_WORDS: Final = (
    "INSERT INTO vault_words (identity_id, entity_id, role, word, version, print)"
    " SELECT %s, w.entity_id, w.role, w.word, %s, w.print"
    " FROM unnest(%s::text[], %s::text[], %s::bytea[], %s::int8[]) AS w (entity_id, role, word, print)"
    " ON CONFLICT (identity_id, entity_id, role) DO UPDATE SET word = EXCLUDED.word, version = EXCLUDED.version,"
    " print = EXCLUDED.print"
    " WHERE vault_words.version < EXCLUDED.version"
    " OR (vault_words.word IS NULL AND EXCLUDED.word IS NOT NULL AND vault_words.version <= EXCLUDED.version)"
    " RETURNING entity_id, word"
)
# Whether a NULL word of the current version (or an older one) is one the entry has now (``words_complete``): the rows
# of a value or a fake with no anchor, or of an entry that did not open when its words were made, by their entry and
# role, through the index of the words.
_NULL_ROLES: Final = (
    "SELECT entity_id, role FROM vault_words WHERE identity_id = %s AND word IS NULL"
    " AND role IN ('original', 'surrogate') AND version <= %s"
)
# Whether a word row of the identity has no print (``prints_complete``), through ``vault_words_printless``: one kept by
# 0.10.0, which ``fill_prints`` gives one.
_PRINTLESS: Final = (
    "SELECT EXISTS (SELECT 1 FROM vault_words WHERE identity_id = %s AND word IS NOT NULL AND print IS NULL)"
)
# The entries with a word and no print, a part at a time after the one read last, in the order of ``seq``: ``_SELECT``'s
# columns, then ``seq``. Each entry's rows are counted by their key, as ``_WORDLESS`` counts them: a part costs the
# entries it walks, where a join with every row without a print would read them all again for each part.
_PRINTLESS_ENTRIES: Final = (
    "SELECT id, etype, eclass, original, surrogate, counter, created, rule, decoded, seq"
    " FROM vault_entities e WHERE identity_id = %s AND seq > %s"
    " AND (SELECT COUNT(*) FROM vault_words w WHERE w.identity_id = e.identity_id AND w.entity_id = e.id"
    " AND w.word IS NOT NULL AND w.print IS NULL) > 0 ORDER BY seq LIMIT %s"
)
# A part's entries held while their prints are written, in the order of ``seq`` as every write of several entries holds
# them: neither a backfill of the words on another instance (``_HELD``, which shares them), nor a fake given anew, a
# rule or a forget writes their rows meanwhile, so that no two transactions wait for each other's rows.
_HELD_FOR_PRINTS: Final = (
    "SELECT id FROM vault_entities WHERE identity_id = %s AND id = ANY(%s::text[]) ORDER BY seq FOR NO KEY UPDATE"
)
# The prints of a part's rows (``fill_prints``), each written only beside the word whose keyed hash is the one its
# entry makes now (a row written by hand, or moved from another entry or role, gets none), and only where none is (a
# fake given anew meanwhile wrote its own).
_PRINTS: Final = (
    "UPDATE vault_words AS w SET print = p.print"
    " FROM unnest(%s::text[], %s::text[], %s::bytea[], %s::int8[]) AS p (entity_id, role, word, print)"
    " WHERE w.identity_id = %s AND w.entity_id = p.entity_id AND w.role = p.role AND w.word = p.word"
    " AND w.print IS NULL"
)
# The entry a string of the vault's names as a fake's (``by_reading``: how a fake in escapes reads, ``_read_as``).
_BY_READING: Final = (
    _SELECT  # noqa: S608 - built from this module's constants alone
    + " AND id = (SELECT s.entity_id FROM vault_strings s WHERE s.identity_id = %s AND s.key = %s"
    " AND s.role = 'surrogate')"
)
# How the fakes written in escapes of the entries whose words are made read (``backfill_words``): an entry kept by
# 0.9.9 or 0.9.10 has no such string (``_read_as`` writes it with the entry since); one the vault knows already stays.
_READINGS: Final = (
    "INSERT INTO vault_strings (identity_id, key, role, entity_id)"
    " SELECT %s, r.key, 'surrogate', r.entity_id FROM unnest(%s::bytea[], %s::text[]) AS r (key, entity_id)"
    " ON CONFLICT DO NOTHING"
)
# Every column named, as 0.9.9's statement did: an instance of a version before a column's keeps its entries all the
# same (``decoded`` NULL there, migration 005). Kept only while the vault's generation (``_GENERATION``, Task 11b) is
# the one its fake was drawn in: a value forgotten, drawn again by an instance that has not heard of it yet, would get
# the fake it had, which the texts masked before hold. Read as the statement starts, and again by the next one
# (``_FIRST_FAKE``), which sees a forget this one waited for.
_INSERT: Final = (
    "INSERT INTO vault_entities (identity_id, id, etype, eclass, original, original_key, surrogate, surrogate_key,"
    " counter, created, rule, decoded) SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s"
    " WHERE COALESCE((SELECT counter FROM change_counters WHERE owner = %s), 0) = %s ON CONFLICT DO NOTHING"
)
# The generation an API key's vault draws its new fakes in (``engine.vault.Vault._seed``): how many forgets of values by
# themselves forgot something (``forget_values``), a count of its own beside the vault's changes (owner
# ``<id>:forgets``). It names no value: the administration log counts the same forgets.
_GENERATION: Final = "SELECT counter FROM change_counters WHERE owner = %s"
_NEXT_GENERATION: Final = (
    "INSERT INTO change_counters (owner, counter) VALUES (%s, 1)"
    " ON CONFLICT (owner) DO UPDATE SET counter = change_counters.counter + 1 RETURNING counter"
)
# The count of the forgets that deleted an entry (owner ``<id>:gone``, ``index.gone_owner``), moved in the forget's
# transaction: deleted rows cannot name themselves to an index's catch-up, which empties its cache whole when it finds
# this count moved (``PrintIndex.catch_up``), and drops only what the rows stamped name otherwise.
_GONE: Final = (
    "INSERT INTO change_counters (owner, counter) VALUES (%s, 1)"
    " ON CONFLICT (owner) DO UPDATE SET counter = change_counters.counter + 1"
)
# A fake's string: not kept when the vault knows the string already, as a value, as another fake, or as a fake retired
# (``_RETIRED``).
_FAKE: Final = (
    "INSERT INTO vault_strings (identity_id, key, role, entity_id) VALUES (%s, %s, 'surrogate', %s)"
    " ON CONFLICT DO NOTHING"
)
# A new entry's fake's string (``save_entity``), kept as ``_FAKE`` keeps it, and only while the vault's generation is
# still the one the fake was drawn in, read again: ``_INSERT`` reads it as it starts, and an INSERT that waited on the
# value's unique index for a forget (its entry deleted, not yet committed) goes on once the forget commits, with what it
# read before the forget moved it (review 11b's I1). This statement starts after that commit: it reads the generation
# the forget left, and keeps nothing when the fake is of an older one.
_FIRST_FAKE: Final = (
    "INSERT INTO vault_strings (identity_id, key, role, entity_id) SELECT %s, %s, 'surrogate', %s"
    " WHERE COALESCE((SELECT counter FROM change_counters WHERE owner = %s), 0) = %s ON CONFLICT DO NOTHING"
)
# A value's string. When it is a fake given before (or retired), the fake's row stays the fake's and names this value's
# entity as well, so that the string stays known as a value once the fake's entity is forgotten.
_VALUE: Final = (
    "INSERT INTO vault_strings (identity_id, key, role, entity_id) VALUES (%s, %s, 'original', %s)"
    " ON CONFLICT (identity_id, key) DO UPDATE SET also = EXCLUDED.entity_id"
)
# The strings of the fakes of entries forgotten (a fake as written, and how one written in escapes reads), retired
# rather than deleted (migration 012, the final security review's H2): a fake freed could be drawn again for another
# value (a person's name after 3,036 new ones), and an unmask of a text masked before would then give that other value.
# Each row stays, its keyed hash alone, of no entry, as long as the vault (R7), and refuses that string to any fake
# (``_FAKE``, ``_FIRST_FAKE``: the vault draws again); one that is another entry's value as well keeps naming it
# (``also``). Updated in place, never deleted and written again: an instance that draws the same fake meanwhile waits
# on the row, and finds it there.
_RETIRED: Final = (
    "UPDATE vault_strings SET role = 'retired', entity_id = NULL"
    " WHERE identity_id = %s AND entity_id = ANY(%s) AND role = 'surrogate'"
)
# Values forgotten by the values themselves (``forget_values``, Task 11b): the entries of their keyed hashes, held until
# the transaction ends, in the order of ``seq``, as every write of several entries holds them (``_HELD``).
_HOLDING_VALUES: Final = (
    "SELECT id FROM vault_entities WHERE identity_id = %s AND original_key = ANY(%s) ORDER BY seq FOR UPDATE"
)
# ``forget_entity``'s statements, for those entries at once: the strings of their fakes retired (``_RETIRED``); a
# string that is the value of one of them as well no longer names it; then their other strings (their values') and
# their entries go, their words with them (the foreign key); and the local reviewer's answers that hold a value.
_NO_LONGER_ALSO: Final = "UPDATE vault_strings SET also = NULL WHERE identity_id = %s AND also = ANY(%s)"
_STRINGS_GONE: Final = "DELETE FROM vault_strings WHERE identity_id = %s AND entity_id = ANY(%s)"
_ENTRIES_GONE: Final = "DELETE FROM vault_entities WHERE identity_id = %s AND id = ANY(%s)"
# The local reviewer's remembered answers that found one of the values, by the keyed hashes each answer is kept with
# (``save_review``), through their index (review 11b's I2): none is opened.
_ANSWERS_GONE: Final = "DELETE FROM reviews WHERE identity_id = %s AND value_keys && %s::bytea[]"
# An answer, kept with the keyed hashes of the values it found, so that a forget finds it by them; and those kept
# before (migration 010: NULL), each given its hashes once (``index_answers``), a part at a time, in the order of their
# keys from where the part before ended (what an index scan from the start would walk again: the rows it just gave
# theirs, until a vacuum). The condition keeps an answer kept again meanwhile (its hashes made with it) as it is.
_ANSWER: Final = (
    "INSERT INTO reviews (identity_id, key, data, ts, value_keys) VALUES (%s, %s, %s, %s, %s::bytea[])"
    " ON CONFLICT (identity_id, key) DO UPDATE SET data = EXCLUDED.data, ts = EXCLUDED.ts,"
    " value_keys = EXCLUDED.value_keys"
)
_UNINDEXED: Final = (
    "SELECT key, data FROM reviews WHERE identity_id = %s AND value_keys IS NULL AND key > %s ORDER BY key LIMIT %s"
)
# A part's answers given their hashes in one statement: each answer's hashes joined in one string (32 bytes each, a
# keyed hash's length), cut back into an array there.
_INDEXED: Final = (
    "UPDATE reviews AS r SET value_keys = ARRAY("
    "SELECT substring(given.joined FROM 1 + 32 * n FOR 32) FROM generate_series(0, length(given.joined) / 32 - 1) AS n"
    " ORDER BY n) FROM unnest(%s::bytea[], %s::bytea[]) AS given (key, joined)"
    " WHERE r.identity_id = %s AND r.key = given.key AND r.value_keys IS NULL"
)
# An entry's fake given anew (``replace_fake``): the entry's row, while it holds the fake it is replacing.
_REFAKE: Final = (
    "UPDATE vault_entities SET surrogate = %s, surrogate_key = %s, counter = %s"
    " WHERE identity_id = %s AND id = %s AND surrogate_key = %s"
)
# The old fake's string, retired as an entry's forgotten is (``_RETIRED``): never another value's; a value's as well,
# it keeps naming that value (``also``).
_OLD_FAKE: Final = (
    "UPDATE vault_strings SET role = 'retired', entity_id = NULL WHERE identity_id = %s AND key = %s AND entity_id = %s"
)
# The count of the identity's announced changes, moved last in a change's transaction, by one, or by as many changes as
# a call kept together (``_Together``: the count of the last of them, the others' the ones before it, in the order they
# were kept): its row stays locked until the change commits, so the counts follow the order the changes are kept in,
# and the change waits for nothing after it. The same statement writes that count into the word rows of the entries it
# counts (``counted``, the plan's S3), in one round trip as the count alone took: an instance whose index of prints
# read the count before them catches up with them by it (``latet.server.index``), whether or not it heard the change.
_COUNT: Final = (
    "WITH c AS (INSERT INTO change_counters (owner, counter) VALUES (%s, %s)"
    " ON CONFLICT (owner) DO UPDATE SET counter = change_counters.counter + EXCLUDED.counter RETURNING counter),"
    " s AS (UPDATE vault_words SET counted = (SELECT counter FROM c)"
    " WHERE identity_id = %s AND entity_id = ANY(%s::text[]))"
    " SELECT counter FROM c"
)


class VaultFull(MaskingError):
    """The identity's vault holds as many values as its limit allows (an API key's own, else the server's): the request
    is stopped, never sent unmasked. ``limit``: how many it may hold, when the vault says (the applications API's
    refusal names it)."""

    def __init__(self, message: str, limit: int | None = None) -> None:
        super().__init__(message)
        self.limit = limit


class TooManyCandidates(TooMany):
    """The texts of one call name more of the vault's values (or fakes) than one call may open (``CANDIDATES_AT_MOST``):
    the call is refused, nothing of it masked or restored (an API key's, as 0.10.2 refused it; a person's space reads
    its whole vault instead, ``StoredVault.whole_past_bounds``). Its words name no value."""


# What a lookup read as 0.10.2 read it may transfer (``_LegacyBudget``): no bound but its candidates'.
_UNBOUNDED_BYTES: Final = 1 << 62


class _LegacyBudget(LookupBudget):
    """0.10.2's bound of a lookup alone (``with_words``): ``CANDIDATES_AT_MOST`` rows read, unreadable rows and cached
    candidates counted, past them ``TooManyCandidates``; no bound of characters or bytes, which 0.10.2 did not have
    (Task 8's fix round 6: an API key's call past Task 8's own bounds reads as 0.10.2 did, never refused for them)."""

    def row(self, size: int = 0) -> None:
        if self.candidates >= CANDIDATES_AT_MOST:
            raise _too_many()
        self.candidates += 1
        self.bytes += size

    def entity(self, entity: Entity, *, counted: bool = False) -> None:
        if not counted:
            self.row()

    def check(self, value: int, limit: int) -> None:
        """No bound of Task 8's: 0.10.2's alone (``row``)."""

    @property
    def row_room(self) -> int:
        return CANDIDATES_AT_MOST + 1 - self.candidates

    @property
    def byte_room(self) -> int:
        return _UNBOUNDED_BYTES


class OlderGeneration(FakeTaken):
    """A value's fake was drawn in an older generation of the vault than the store's (values were forgotten by
    themselves since, Task 11b): nothing is kept, the store's generation is read, and the vault draws the value again in
    it (``Vault.intern`` tries the next fake, as for one taken)."""


class NotRecorded(Exception):
    """The entry of the administration log that records values forgotten was not written (``forget_values``'s
    ``last``): the transaction that forgot them was rolled back, and nothing was forgotten. Its words name the error's
    type alone."""


class Forgotten(NamedTuple):
    """What ``forget_values`` forgot: the ids, oldest first; how many of the values asked (each counted once) the vault
    does not hold; and the generation new fakes are drawn in from then on, the forget's own (None when nothing was
    forgotten: the generation did not move)."""

    ids: list[str]
    unknown: int
    generation: int | None


class UnreadableEntry(Exception):
    """An entry of the vault is there and does not open: sealed with another root key, or changed in the database. Its
    words name the entry by its id, never a value."""


class _Changed(Exception):
    """An entry whose fake was to be given anew holds another one, or is gone: another instance changed it first. Its
    transaction is rolled back."""


class _Together:
    """The changes one thread keeps together in a key's vault (``PgVaultStore.together``, ``state.protocols.Together``):
    in one transaction, on a connection of the pool taken at the first of them, each in a savepoint of its own, so that
    a value refused (its fake taken) is undone alone; the identity's count moved once, last, for all of them, and each
    told once they are committed, with a count of its own. From the first change on, the thread's lookups read through
    that connection too: they find what it kept, and the call holds no second connection of the pool. The instance
    lets a few calls at once hold one (``Database.together``, Ruling 10b-R2): a call that finds none free keeps each
    of its values at once, as before, and leaves the pool to every other identity's work."""

    def __init__(self, store: PgVaultStore, thread: int) -> None:
        self._store, self._thread = store, thread
        self._stack: contextlib.ExitStack | None = None  # the connection and its transaction, once open
        self.conn: Connection[Any] | None = None
        self.changes: list[tuple[str, str]] = []  # what to tell once committed: its kind, the entry's id
        self.kept = 0  # the values among them, counted once committed
        self.inserted: set[str] = set()  # the entries it kept, whose rows are its own until it commits
        self.at_once = False  # the instance had no slot for it: each value kept at once (``state.protocols.Together``)

    def open(self) -> Connection[Any] | None:
        """Its connection, its transaction begun: taken at the first change, with one of the instance's slots. None
        when every slot is taken: the call keeps each change at once from then on (it is no longer this thread's)."""
        if self.conn is None:
            slots = self._store.db.together
            if not slots.acquire(blocking=False):
                self._store._together.pop(self._thread, None)
                self.at_once = True
                return None
            stack = contextlib.ExitStack()
            stack.callback(slots.release)  # the last thing undone: once the connection is back in the pool
            try:
                conn = stack.enter_context(self._store.db.connection())
                stack.enter_context(conn.transaction())
            except BaseException:
                stack.close()
                raise
            self._stack, self.conn = stack, conn
        return self.conn

    def changed(self, kind: str, entity_id: str, kept: int = 0) -> None:
        """A change made (``kind`` as ``changed`` tells it), told once committed; ``kept``: a value kept."""
        self.changes.append((kind, entity_id))
        self.kept += kept

    def commit(self) -> None:
        store = self._store
        store._together.pop(self._thread, None)
        stack, conn, self._stack, self.conn = self._stack, self.conn, None, None
        if stack is None or conn is None:
            return  # nothing was written
        last = 0
        try:
            if self.changes:  # their entries' rows stamped with the last count (``_COUNT``)
                entries = list(dict.fromkeys(entity_id for _, entity_id in self.changes))
                last = store._count(conn, entries, len(self.changes))
        except BaseException as exc:
            stack.__exit__(type(exc), exc, exc.__traceback__)  # rolled back, the connection handed back
            raise
        stack.close()  # the commit: one that fails is rolled back, its connection handed back, and raised
        store._moved_by(self.kept)
        first = last - len(self.changes) + 1
        for n, (kind, entity_id) in enumerate(self.changes):
            store._changed(kind, entity_id, first + n)

    def rollback(self) -> None:
        self._store._together.pop(self._thread, None)
        stack, self._stack, self.conn = self._stack, None, None
        if stack is not None:
            with contextlib.suppress(Exception):  # a connection that broke: the pool makes another, nothing was kept
                stack.__exit__(psycopg.Rollback, psycopg.Rollback(), None)


class PgVaultStore:
    # Its lookups bound each statement themselves (``bounded_words``: ``WORDS_AT_ONCE`` words, or prints, a statement,
    # every answer bounded before it is sent): a stored vault asks it for all of a call's words at once.
    lookup_batch: ClassVar[int | None] = None

    def __init__(
        self,
        db: Database,
        identity_id: str,
        root_key: bytes,
        *,
        changed: Callable[[str, str | None, int], None] | None = None,
        limit: int = VAULT_LIMIT,
        forgets_values: bool = True,
        jobs: JobLock | None = None,
        index: bool = False,
        cache: int = WORKING_VALUES,
    ) -> None:
        """``forgets_values``: whether its values are forgotten by themselves (an API key's, Task 11b), so that the
        local reviewer's answers are kept with the keyed hashes of the values they found (``save_review``); a person's
        are kept with none (the final review's M6, Ruling F1). ``jobs``: what runs a job on one instance at a time (the
        server's Valkey, ``Bus.hold``), so that a vault's prints are made by one (``fill_prints``); None: by whichever
        asks."""
        self.db, self.identity = db, identity_id
        self.forgets_values = forgets_values
        self._sealer = Sealer(root_key, b"storage")
        self._images = derived(root_key, DERIVE, b"image-decisions")  # what names a decision's row
        self._prints = print_key(root_key)  # the key of its words' prints (``latet.server.prints``)
        self._jobs = jobs
        self._changed = changed or (lambda _kind, _entity, _counter: None)
        self._unreadable: set[str] = set()  # the entries met that do not open, by id
        self.unreadable = 0  # how many: `latet doctor` says so
        self.limit = limit
        self.count = 0  # the vault's values: all of them when loaded, then those kept here (the space keeps it true)
        # What moves the count by what this store kept or forgot (``_moved_by``): the values kept in the thread that
        # masks and those forgotten in the forget's own, at once, none of either lost (review 11b's M3).
        self._counting = threading.Lock()
        self._together: dict[int, _Together] = {}  # the threads that keep their changes together (``together``)
        # The generation the vault that keeps its values here draws its new fakes in (Task 11b), as last read
        # (``load_generation``, ``save_entity``): read and written under that vault's lock alone.
        self.generation = 0
        self._generations = f"{identity_id}:forgets"  # whose count of ``change_counters`` it is
        self.index = PrintIndex(db, identity_id, self._prints) if index else None
        self._cache = PrintCache(max(0, cache))
        self._index_lock = threading.RLock()
        self._index_build = threading.Lock()
        self._building_index: PrintIndex | None = None
        # Why its index was last found not ready (``PRINTS_UNMADE``, or an error's type: never its words), for the
        # space's log line; None once one is ready.
        self.index_failure: str | None = None
        self._closed = threading.Event()
        self._counter_seen = 0
        self._observed: tuple[int, int] = (0, 0)
        # What changed, by name, each time ``freshness`` moves (``_telling``, ``named_after``): the entries each
        # catch-up of the published index named, told with the move of its cache, under the log's lock; None where
        # nothing says what changed (a forget, a barrier, a counter read with no index, a fresh index). Each telling
        # with what it weighs.
        self._named: collections.deque[tuple[int, frozenset[str] | None, int]] = collections.deque()
        self._named_seq = 0  # the last told
        self._named_floor = 0  # the last let go of the log
        self._named_ids = 0  # the ids the log holds
        self._named_bytes = 0  # what it weighs (``index_bytes``)
        self._named_lock = threading.Lock()

    @property
    def freshness(self) -> tuple[int, int]:
        """Each vault observes this itself, even when a message worker already consumed the counter."""
        index = self.index
        return max(self._counter_seen, index.watermark if index is not None else 0), self._cache.epoch

    @property
    def named_seq(self) -> int:
        """How far what changed was told (``named_after``): where a vault made now starts. Read under the log's lock,
        so that every telling up to it has its fence up (``_telling``)."""
        with self._named_lock:
            return self._named_seq

    @contextlib.contextmanager
    def _telling(self, entities: Iterable[str] | None) -> Iterator[None]:
        """What changed: ``entities`` by name, or None, not known by name; told as ``freshness`` moves for it, the move
        made inside (its fence: the cache told or emptied, the index published, the counter seen), under the log's
        lock, which its readers take too (``named_seq``, ``named_after``). No vault reads a telling before what it
        tells of is done, which would let it confirm its memory and then bring in from the cache what the fence was to
        take away, with nothing left to name it (the review of fix round 4's I-1). Taken inside an index's own lock (a
        catch-up's) or the store's index lock (a publication), never the other way, and before the cache's; nothing it
        holds calls out. At most ``NAMED_KEPT`` tellings and ``NAMED_IDS_KEPT``
        names are kept, the oldest let go first; a telling of more names than that is kept as not known by name. What
        the log holds is weighed (``index_bytes``)."""
        names = None if entities is None else frozenset(entities)
        if names is not None and len(names) > NAMED_IDS_KEPT:
            names = None
        weight = _TELLING_BYTES + (sys.getsizeof(names) + sum(map(sys.getsizeof, names)) if names is not None else 0)
        with self._named_lock:
            self._named_seq += 1
            self._named.append((self._named_seq, names, weight))
            self._named_ids += len(names) if names is not None else 0
            self._named_bytes += weight
            while len(self._named) > NAMED_KEPT or self._named_ids > NAMED_IDS_KEPT:
                seq, dropped, weighed = self._named.popleft()
                self._named_floor = seq
                self._named_ids -= len(dropped) if dropped is not None else 0
                self._named_bytes -= weighed
            yield

    def named_after(self, seq: int) -> tuple[int, set[str] | None]:
        """What changed since the telling ``seq`` (``named_seq`` when a vault last confirmed its memory), and how far
        that goes: the entries named, or None where one telling since did not know them by name, or where the log no
        longer reaches back to ``seq``; then a vault confirms its whole memory (``StoredVault.resync``). The tellings
        after ``seq`` alone are read, the newest first; none of them before its fence is up (``_telling``)."""
        with self._named_lock:
            if seq < self._named_floor:
                return self._named_seq, None
            names: set[str] = set()
            for told, entities, _ in reversed(self._named):
                if told <= seq:
                    break
                if entities is None:
                    return self._named_seq, None
                names |= entities
            return self._named_seq, names

    @property
    def index_bytes(self) -> int:
        """What the vault's index, the one being built, the cache of its answers and the log of what changed weigh in
        this process: the space's weight counts it (``Spaces``)."""
        index, building = self.index, self._building_index
        return (
            (index.nbytes if index is not None else 0)
            + (building.nbytes + building.building_bytes if building is not None else 0)
            + self._cache.nbytes
            + self._named_bytes
        )

    def invalidate(self) -> None:
        """A lost message/resync barrier, including for fetches still in flight."""
        with self._telling(None):
            self._cache.clear()

    def build_index(self) -> bool:
        """Build a fresh index, then catch up and publish it while the store is still open.

        Masking keeps the published index or authoritative SQL throughout. Prints not all made, an
        allocation/aggregate limit or a database failure never makes a partial index ready: why is kept in
        ``index_failure`` (``PRINTS_UNMADE``, ``INDEX_TOO_LARGE``, or the error's type), which the space says once and
        builds it again for, but for its size (``spaces.Spaces._index_judged``). A build makes no print: one that finds
        them not all made (a vault kept by 0.10) ends at once, its turn handed on, and its space has them made apart
        (``fill_prints``, in ``spaces.Spaces._prints_space``), so that a fill of minutes holds no build of another
        vault's index. Once published, and its build's turn handed on, whether a fake has a reading is read
        (``PrintIndex.check_readings``): until then, and when that read fails, every fake is looked up exactly.
        """
        if self.index is None or self._closed.is_set() or not self._index_build.acquire(blocking=False):
            return False
        fresh = PrintIndex(self.db, self.identity, self._prints)
        while not BUILD_TURNS.acquire(timeout=0.1):
            if self._closed.is_set():
                self._index_build.release()
                return False
        self._building_index = fresh
        try:
            if self._closed.is_set():
                return False
            if not self.prints_complete() or not fresh.build():
                self.index_failure = PRINTS_UNMADE  # their fill not over, here or on another instance, or not begun
                return False
            with self._index_lock:
                if self._closed.is_set():
                    return False
                # The live index may have advanced while the new snapshot was being built.
                fresh.catch_up()
                if self._closed.is_set():
                    return False
                previous = self.index
                if previous is not None and fresh.watermark < previous.watermark:
                    return False
                # A fresh index: what changed since the old one's watermark is not known by name. Told with what it
                # tells of, the cache emptied and the index published.
                with self._telling(None):
                    self._cache.clear()
                    fresh.told = fresh.watermark  # the cache, emptied, holds nothing its rows changed
                    self.index = fresh
                self.index_failure = None
        except Exception as exc:
            # Driver messages can contain source text. Only the exception class is kept, said by the space once.
            self.index_failure = INDEX_TOO_LARGE if isinstance(exc, IndexTooLarge) else type(exc).__name__
            return False
        finally:
            self._building_index = None
            BUILD_TURNS.release()
            self._index_build.release()
        if not self._closed.is_set():
            try:
                fresh.check_readings()
            except Exception as exc:
                why = type(exc).__name__
                log.info("the readings of the fakes of %s were not read (%s): each is looked up", self.identity, why)
        return True

    def _moved_by(self, change: int) -> None:
        """The count moved by ``change`` (values kept, or forgotten when negative), never below none."""
        with self._counting:
            self.count = max(0, self.count + change)

    # --- changes kept together (an applications call's, Ruling T10i) --------------------------------

    def together(self) -> _Together:
        """The values this thread keeps from now on (``save_entity``) kept in one transaction, until its ``commit`` or
        ``rollback`` (``state.protocols.Together``): an applications call's new values cost one commit, which waits for
        PostgreSQL's disk, where each cost its own (half of a new key's mask, Task 10b). Its values are seen by no other
        connection before it commits (another instance keeping one of them meanwhile waits for its commit, as for any
        transaction's), and the count of the changes moves once as it does. A rule is kept at once all the same, in a
        transaction of its own (review 10b's M2: the call's transaction writes new rows alone). The instance lets a
        few calls hold such a transaction at once (``Database.together``): past them, each value is kept at once."""
        thread = threading.get_ident()
        stale = self._together.get(thread)
        if stale is not None:  # one its call never ended: nothing of it was told
            stale.rollback()
        together = self._together[thread] = _Together(self, thread)
        return together

    def _joined(self) -> _Together | None:
        """This thread's changes kept together, when it keeps them so."""
        return self._together.get(threading.get_ident())

    @contextlib.contextmanager
    def _connection(self) -> Iterator[Connection[Any]]:
        """A connection to read with: that of this thread's changes kept together, once it made one (what it kept is
        found, and no second connection is held), else one of the pool's."""
        together = self._joined()
        if together is not None and together.conn is not None:
            yield together.conn
            return
        with self.db.connection() as conn:
            yield conn

    def _autocommit(self) -> contextlib.AbstractContextManager[Connection[Any]]:
        """A connection of the pool in autocommit, for reads of a statement each (``index._reading``): never this
        thread's changes kept together, which only ``_connection`` reads through."""
        return _reading(self.db)

    def _keeping(self) -> _Together | None:
        """This thread's changes kept together, their transaction open (at their first change); None when it keeps
        each change at once (it keeps none together, or the instance had no slot free for it)."""
        together = self._joined()
        if together is not None and together.open() is None:
            return None
        return together

    @contextlib.contextmanager
    def _transaction(self, together: _Together | None) -> Iterator[Connection[Any]]:
        """A connection whose statements are kept together, or not at all: a transaction of their own, or, for a
        thread that keeps its changes ``together``, a savepoint in theirs."""
        if together is None or together.conn is None:
            with self.db.transaction() as conn:
                yield conn
            return
        with together.conn.transaction():
            yield together.conn

    # --- what a row is sealed for -----------------------------------------------------------------

    def _aad(self, entity_id: str, part: str, etype: str, eclass: int) -> str:
        """What a value (``part`` "original") or a fake ("surrogate") is sealed for: the identity, the entry, which of
        the two it is, and the type and the class it was found as. In JSON, so that no two rows' fields give the same
        text, whatever a type (which a person names) holds."""
        return json.dumps(["vault", self.identity, entity_id, part, etype, int(eclass)])

    def _review_aad(self, key: bytes) -> str:
        return f"{self.identity}:review:{key.hex()}"

    def _image_aad(self, name: bytes) -> str:
        return f"{self.identity}:image:{name.hex()}"

    def _opened(self, row: Any) -> Entity:
        """The entity of a row of ``vault_entities``. Raises UnreadableEntry when it does not open: the entry is
        counted, and said in the log, the first time it is met."""
        entity_id, etype, eclass = row[0], row[1], row[2]
        found: Entity | None = None
        # Another root key, a row changed or moved by hand, a version of the key not held: nothing of it is used.
        with contextlib.suppress(InvalidTag, ValueError):
            original = self._sealer.open(row[3], self._aad(entity_id, "original", etype, eclass)).decode()
            surrogate = self._sealer.open(row[4], self._aad(entity_id, "surrogate", etype, eclass)).decode()
            rule = modules.kept(row[7])
            found = Entity(entity_id, etype, EntityClass(eclass), original, surrogate, row[5], row[6], rule, row[8])
        if found is not None:
            return found
        if entity_id not in self._unreadable:
            self._unreadable.add(entity_id)
            self.unreadable = len(self._unreadable)
            log.warning(
                "vault entry %s does not open (sealed with another root key, or changed in the database): it is left "
                "out, and its value masked as it is found",
                entity_id,
            )
        raise UnreadableEntry(f"the vault's entry {entity_id} does not open with this identity's root key")

    # --- values -------------------------------------------------------------------------------------

    def load_entities(self) -> list[Entity]:
        """Every value of the vault, the oldest first: what masks is always the whole vault. One over its limit (the
        limit lowered since, or passed by instances adding at once) is loaded whole too, said in the log, and learns
        nothing new: a request that brings a new value stops (``VaultFull``). An entry that does not open is left out
        and counted, and a rule kept with a field's name in it by an earlier Latet is kept by the field's kind from now
        on (``_cut_field_names``).

        Read through a cursor of PostgreSQL's, ``ROWS_AT_ONCE`` rows at a time, each made Python's and opened before the
        next, the GIL let go between two: every row made Python's in one call held the event loop every identity of
        the instance shares 0.2 s at the limit (M-09), and the rows waited whole in memory for their opening."""
        found = []
        cut: list[tuple[str | None, str, str]] = []
        self._unreadable, self.unreadable = set(), 0
        count = 0
        with self.db.connection() as conn, conn.cursor(name="vault_entities") as cursor:
            cursor.execute(_SELECT + " ORDER BY seq", (self.identity,))
            while rows := cursor.fetchmany(ROWS_AT_ONCE):
                count += len(rows)
                cut += self._field_names_cut(rows)
                for row in rows:
                    with contextlib.suppress(UnreadableEntry):
                        found.append(self._opened(row))
                time.sleep(LET_GO)  # the GIL let go: the event loop takes its turn (on Windows too, as no pause does)
        self._cut_field_names(cut)  # once the cursor's connection is back in the pool, which may hold that one alone
        self.count = count
        if self.count > self.limit:
            log.warning(
                "the vault of %s holds %d values, %d over the most this server allows (vault_limit, %d): each is "
                "masked, and a request that brings a new value is stopped until the limit is raised or values are "
                "forgotten",
                self.identity,
                self.count,
                self.count - self.limit,
                self.limit,
            )
        return found

    def _field_names_cut(self, rows: list[Any]) -> list[tuple[str | None, str, str]]:
        """The rules of ``rows`` kept in clear with a field's name in them, which can be a value, by an earlier Latet,
        each as it is to be kept (``modules.kept``: by the field's kind), with the row it is to be kept in."""
        return [(rule, self.identity, row[0]) for row in rows if (rule := modules.kept(row[7])) != row[7]]

    def _cut_field_names(self, cut: list[tuple[str | None, str, str]]) -> None:
        """Each rule of ``cut`` (``_field_names_cut``) written in its row: kept by the field's kind from now on, as
        every rule is written."""
        if cut:
            with self.db.transaction() as conn:
                conn.cursor().executemany("UPDATE vault_entities SET rule = %s WHERE identity_id = %s AND id = %s", cut)

    def load_entity(self, entity_id: str) -> Entity | None:
        """One value by its id (one another instance kept, say); None when the vault has no entry of that id. Raises
        UnreadableEntry when it has one and that entry does not open: not the same as none."""
        with self._connection() as conn:
            row = conn.execute(_SELECT + " AND id = %s", (self.identity, entity_id)).fetchone()
        return self._opened(row) if row is not None else None

    def save_entity(self, e: Entity) -> Entity | None:
        """Keep ``e``, its strings and its anchor words with it: None when kept as given; the entity of the same value
        when another instance kept it first (whatever its type: the value alone is the key). Raises FakeTaken when
        ``e``'s fake is a string the vault knows, a value's or another fake's, and VaultFull when the vault holds as
        many values as its limit allows, or more.
        When an entry of the same value is there and does not open, None too: ``e`` is masked as given, in the vault's
        memory, and kept nowhere (the entry stays as it is, counted in ``unreadable``). While this thread keeps its
        changes together (``together``), ``e`` is kept in their transaction (a savepoint: refused alone), counted and
        told once they commit."""
        together = self._keeping()
        held = self.count + (together.kept if together is not None else 0)
        if held >= self.limit:  # more than the limit too: lowered since, or passed by instances at once
            # How values are forgotten, by whose the vault is: an API key has no dashboard (the final review's M4).
            forgotten = (
                "values are forgotten with `latet server keys forget` or the applications API's forget"
                if self.forgets_values
                else "values are forgotten in the dashboard"
            )
            raise VaultFull(
                f"this identity's vault holds {held} values, and its limit is {self.limit} (the server's "
                f"vault_limit, or an API key's own: `latet server keys limit`): an administrator raises it, or "
                f"{forgotten}",
                self.limit,
            )
        original_key = self._sealer.keyed(f"original\x00{e.original}")
        row = (
            self.identity, e.id, e.etype, int(e.eclass),
            self._sealer.seal(e.original.encode(), self._aad(e.id, "original", e.etype, e.eclass)), original_key,
            self._sealer.seal(e.surrogate.encode(), self._aad(e.id, "surrogate", e.etype, e.eclass)),
            self._sealer.keyed(f"surrogate\x00{e.surrogate}"), e.counter, e.created, modules.kept(e.rule), e.decoded,
            self._generations, self.generation,
        )  # fmt: skip
        (value_word, value_print), (fake_word, fake_print) = self._words(e)
        edged = self._edges(e, "original") + self._edges(e, "surrogate")
        counter, kept, generation = 0, None, self.generation
        fake = (self.identity, self._sealer.keyed(e.surrogate), e.id, self._generations, self.generation)
        with self._transaction(together) as conn:
            done = conn.execute(_INSERT, row).rowcount
            if done:
                if not conn.execute(_FIRST_FAKE, fake).rowcount:
                    found = conn.execute(_GENERATION, (self._generations,)).fetchone()
                    generation = int(found[0]) if found is not None else 0
                    if generation != self.generation:  # a forget committed while the INSERT waited for it
                        self.generation = generation  # read now: the vault draws the value again in it
                        raise OlderGeneration(
                            f"the fake made for a {e.etype} was drawn in an older generation of the vault"
                        )
                    # The fake is a string this vault knows, a value or another fake: nothing is kept.
                    raise FakeTaken(f"the fake made for a {e.etype} is a value already known, or another value's fake")
                self._read_as(conn, e)
                # A value equal to a fake given before is real: kept and masked under a fake of its own.
                conn.execute(_VALUE, (self.identity, self._sealer.keyed(e.original), e.id))
                conn.execute(
                    _WORDS,
                    (
                        self.identity, e.id, value_word, ANCHORS_VERSION, value_print,
                        self.identity, e.id, fake_word, ANCHORS_VERSION, fake_print,
                    ),
                )  # fmt: skip
                if edged:
                    roles, words, printed = zip(*edged, strict=True)
                    edges = (self.identity, e.id, ANCHORS_VERSION, list(roles), list(words), list(printed))
                    conn.execute(_EDGES, edges)
                if together is None:
                    counter = self._count(conn, [e.id])
            else:  # the same value kept first (by its id, or under another type), the fake taken, an older generation
                kept = conn.execute(
                    _SELECT + " AND (id = %s OR original_key = %s) ORDER BY seq LIMIT 1",
                    (self.identity, e.id, original_key),
                ).fetchone()
                if kept is None:
                    found = conn.execute(_GENERATION, (self._generations,)).fetchone()
                    generation = int(found[0]) if found is not None else 0
        if not done and kept is None and generation != self.generation:
            self.generation = generation  # read now: the vault draws the value again in it
            raise OlderGeneration(f"the fake made for a {e.etype} was drawn in an older generation of the vault")
        if done:
            if together is not None:
                together.changed("entity", e.id, kept=1)
                together.inserted.add(e.id)
                return None
            self._moved_by(1)
            self._changed("entity", e.id, counter)
            return None
        if kept is None:
            raise FakeTaken(f"the fake made for a {e.etype} is another value's")
        try:
            return self._opened(kept)
        except UnreadableEntry:
            return None  # masked as given: an entry that does not open is not "the fake is taken"

    def _read_as(self, conn: Connection[Any], e: Entity) -> None:
        """How ``e``'s fake reads, where it is written in escapes and reads otherwise (``fake_reading``: `Giulia
        Martini` for `Giulia%20Martini`, 0.9.9), kept among the vault's strings as a fake is, in ``e``'s transaction.
        Raises FakeTaken when the vault knows that string, as a value, a fake or how another fake reads: a fake that
        reads as a real name, or as another value's fake, would bring the wrong value back (0.9.9's N3 and I-1), and
        an API key's vault holds few of its strings in memory to tell (``engine.stored``)."""
        reading = fake_reading(e)
        if reading is not None and not conn.execute(_FAKE, (self.identity, self._sealer.keyed(reading), e.id)).rowcount:
            raise FakeTaken(f"the fake made for a {e.etype} reads as a value already known, or as another value's fake")

    def save_rule(self, e: Entity) -> None:
        """The rule of a value kept before rules were: kept when its entry has none yet, and told."""
        if e.rule is None:
            return
        # The rule of a value this thread's call keeps: its row is the call's alone until it commits, which no other
        # connection sees (review 10b's N2), so it is written in the call's transaction and told with its values.
        together = self._joined()
        own = together is not None and together.conn is not None and e.id in together.inserted
        with self._transaction(together if own else None) as conn:
            done = conn.execute(
                "UPDATE vault_entities SET rule = %s WHERE identity_id = %s AND id = %s AND rule IS NULL",
                (modules.kept(e.rule), self.identity, e.id),
            ).rowcount
            counter = self._count(conn, [e.id]) if done and not own else 0  # its rows name a changed entry
        if done and own and together is not None:
            together.changed("rule", e.id)
        elif done:
            self._changed("rule", e.id, counter)

    def replace_fake(self, old: Entity, new: Entity) -> Entity | None:
        """Keep ``new``'s fake in place of ``old``'s, in their value's entry (one whose fake showed its value, drawn
        again at load): sealed for the entry as every fake is, the new fake's string known in place of the old one's (a
        string that is another entry's value as well stays, as that value's), counted and told as a value kept is, so
        that the other instances learn it, and the old one retired (``_OLD_FAKE``), never another value's. None when
        kept so; the entry as another instance changed it first. Raises FakeTaken when the new fake is a string the
        vault knows, a value's, a fake's or a fake retired; EntryGone when the entry is
        gone, or no longer opens (written by hand, moved from another key): the vault then knows the value no more.
        The new fake's anchor word takes the old one's place in the entry's words, and its edges the old one's."""
        sealed = self._sealer.seal(new.surrogate.encode(), self._aad(old.id, "surrogate", old.etype, old.eclass))
        row = (
            sealed, self._sealer.keyed(f"surrogate\x00{new.surrogate}"), new.counter,
            self.identity, old.id, self._sealer.keyed(f"surrogate\x00{old.surrogate}"),
        )  # fmt: skip
        drawn = replace(new, etype=old.etype, eclass=old.eclass)
        fake = word_of(drawn, "surrogate")
        fake_word, fake_print = self._word("surrogate", fake), self._print("surrogate", fake)
        edged = self._edges(drawn, "surrogate")
        try:
            with self.db.transaction() as conn:
                if not conn.execute(_FAKE, (self.identity, self._sealer.keyed(new.surrogate), old.id)).rowcount:
                    raise FakeTaken(
                        f"the fake made for a {new.etype} is a value already known, or another value's fake"
                    )
                if not conn.execute(_REFAKE, row).rowcount:
                    raise _Changed
                conn.execute(_FAKE_WORD, (self.identity, old.id, fake_word, ANCHORS_VERSION, fake_print))
                conn.execute(_FAKE_EDGES_GONE, (self.identity, old.id))
                if edged:
                    roles, words, printed = zip(*edged, strict=True)
                    conn.execute(
                        _EDGES, (self.identity, old.id, ANCHORS_VERSION, list(roles), list(words), list(printed))
                    )
                gone = [old.surrogate] + [read for read in (fake_reading(old),) if read is not None]
                for was in map(self._sealer.keyed, gone):
                    conn.execute(_OLD_FAKE, (self.identity, was, old.id))
                self._read_as(conn, replace(new, id=old.id))
                counter = self._count(conn, [old.id])
        except _Changed:
            try:
                now = self.load_entity(old.id)
            except UnreadableEntry:
                now = None
            if now is None:
                raise EntryGone(f"the entry of a {old.etype} was forgotten meanwhile") from None
            return now
        self._changed("entity", old.id, counter)
        return None

    def forget_entity(self, entity_id: str) -> None:
        """Forget a value, its fake, its strings and its words (which the entry's foreign key takes with it), and tell
        it. Its fake's strings are retired (``_RETIRED``), never another value's; one that is another entry's value as
        well (that value was equal to its fake) keeps naming it. A row another transaction holds (a call keeping its
        values together, whose new value is this one's fake) is waited for ``FORGET_LOCK_SECONDS`` at most (review
        10b's M2): past them nothing is forgotten, and it raises (OperationalError), to be tried again."""
        with self.db.transaction() as conn:
            conn.execute("SELECT set_config('lock_timeout', %s, true)", (f"{FORGET_LOCK_SECONDS * 1000:.0f}",))
            conn.execute(_RETIRED, (self.identity, [entity_id]))
            conn.execute(_NO_LONGER_ALSO, (self.identity, [entity_id]))
            conn.execute(_STRINGS_GONE, (self.identity, [entity_id]))
            done = conn.execute(
                "DELETE FROM vault_entities WHERE identity_id = %s AND id = %s", (self.identity, entity_id)
            ).rowcount
            if done:  # its rows went with it: they name nothing to a catch-up, this count does
                conn.execute(_GONE, (gone_owner(self.identity),))
            counter = self._count(conn, [entity_id])  # its rows gone with it: none stamped
        self._moved_by(-done)
        self._changed("forgotten", entity_id, counter)

    def forget_entities(self, entity_ids: Iterable[str]) -> None:
        """Each forgotten as ``forget_entity`` forgets it, and told: a server's vault has no upgrade copies to open."""
        for entity_id in dict.fromkeys(entity_ids):
            self.forget_entity(entity_id)

    def forget_values(
        self,
        values: Iterable[str],
        *,
        first: Callable[[Connection[Any]], None] | None = None,
        gone: Callable[[Connection[Any], list[str]], object] | None = None,
        last: Callable[[Connection[Any], int, int], None] | None = None,
    ) -> Forgotten:
        """Forget values of an API key's vault by the values themselves (Task 11b: its administrator's, or an
        application's with ``forget``): each found exactly as it is given, by its keyed hash as ``by_original`` finds
        it (another case, another written form is another value, which the vault keeps as an entry of its own), and
        deleted with everything that names it, as ``forget_entity`` deletes it (its strings, its fake's retired, never
        another value's; its words and edges, by the foreign key), and with the local
        reviewer's remembered answers that found one of the values asked, whether the vault held it or not: found by
        the keyed hashes each answer is kept with, through their index, none of them opened (review 11b's I2), the
        answers kept before those hashes were given theirs first (``index_answers``, outside the transaction). The ids
        forgotten, oldest first, how many of the values asked (each counted once) the vault does not hold, and the
        generation new fakes are drawn in from then on (``Forgotten``).

        All of it in one transaction, ``first`` (given its connection) before any row of the vault is touched, ``gone``
        (given its connection and the ids forgotten, when there are) after them, and ``last`` (given its connection, how
        many forgotten, how many not held) after everything: the administration's token's row held; the rows of the
        key's history that name the entries forgotten (``PgHistoryStore.forget_entities``: Ruling 11b-R1), which a
        forget tried again could no longer find, their values' entries gone; and the entry of the administration log
        that records it, written last. One that raises rolls the whole back, nothing forgotten and nothing told. A row
        another transaction holds is waited for ``FORGET_LOCK_SECONDS`` at most, then it raises (OperationalError), as
        ``forget_entity`` does, with nothing forgotten. Once committed the count of the identity's changes has moved by
        as many, last in the transaction, and each is told with a count of its own (``changed``), as a call's values
        kept together are."""
        asked = list(dict.fromkeys(values))
        hashed = [self._sealer.keyed(f"original\x00{value}") for value in asked]
        found = [self._found_key(value) for value in asked if value]
        changes, generation = 0, None
        self.index_answers()
        with self.db.transaction() as conn:
            if first is not None:
                first(conn)
            conn.execute("SELECT set_config('lock_timeout', %s, true)", (f"{FORGET_LOCK_SECONDS * 1000:.0f}",))
            ids = [str(row[0]) for row in conn.execute(_HOLDING_VALUES, (self.identity, hashed))] if hashed else []
            if ids:
                conn.execute(_RETIRED, (self.identity, ids))
                conn.execute(_NO_LONGER_ALSO, (self.identity, ids))
                conn.execute(_STRINGS_GONE, (self.identity, ids))
                conn.execute(_ENTRIES_GONE, (self.identity, ids))
                conn.execute(_GONE, (gone_owner(self.identity),))  # their rows name nothing to a catch-up
            if found:
                conn.execute(_ANSWERS_GONE, (self.identity, found))
            if ids and gone is not None:
                gone(conn, ids)
            if ids:
                # A value forgotten and met again gets a new fake, never the one the texts masked before hold: new
                # fakes are drawn in the next generation from now on, on every instance (``save_entity``).
                moved = conn.execute(_NEXT_GENERATION, (self._generations,)).fetchone()
                assert moved is not None  # RETURNING gives the row it counted
                generation = int(moved[0])
                changes = self._count(conn, ids, len(ids))  # their rows gone with them: none stamped
            conn.execute("SET LOCAL lock_timeout TO DEFAULT")  # what ``last`` waits for is its own (the log's chain)
            if last is not None:
                last(conn, len(ids), len(asked) - len(ids))
        self._moved_by(-len(ids))
        for n, entity_id in enumerate(ids):
            self._changed("forgotten", entity_id, changes - len(ids) + 1 + n)
        return Forgotten(ids, len(asked) - len(ids), generation)

    def _found_key(self, value: str) -> bytes:
        """The keyed hash a remembered answer of the local reviewer is kept with for a value it found (``reviews``'
        ``value_keys``): under a label of its own, so that it is never the hash of the vault's entry of that value."""
        return self._sealer.keyed(f"review-value\x00{value}")

    def _found_keys(self, answer: Any) -> list[bytes]:
        """The keyed hashes of the values an answer of the local reviewer found (its findings, and those it could not
        place: values in clear in its sealed JSON), each once; none for what is no answer."""
        if not isinstance(answer, dict):
            return []
        values = {
            str(entry[0])
            for group in ("findings", "unlocated")
            if isinstance(found := answer.get(group), list)
            for entry in found
            if isinstance(entry, list) and entry and entry[0]
        }
        return sorted(map(self._found_key, values))

    def index_answers(self) -> int:
        """The remembered answers of the local reviewer kept before they were kept with the keyed hashes of their
        values (migration 010, or an older instance's meanwhile) given theirs, each opened once, ``ROWS_AT_ONCE`` at a
        time, each part in a transaction of its own, the GIL let go between two: what a forget runs before its own
        transaction, so that it finds every answer by its index (review 11b's I2). One that does not open (another root
        key, another text's answer moved there), or is no answer, gets none, and is never opened again. How many were
        given theirs; none, in one statement, once every answer has its own."""
        done, after = 0, b""
        while True:
            with self.db.transaction() as conn:
                rows = conn.execute(_UNINDEXED, (self.identity, after, ROWS_AT_ONCE)).fetchall()
                answers, joined = [], []
                for key, data in rows:
                    answer = None
                    with contextlib.suppress(InvalidTag, ValueError):
                        answer = json.loads(self._sealer.open(bytes(data), self._review_aad(bytes(key))))
                    answers.append(bytes(key))
                    joined.append(b"".join(self._found_keys(answer)))
                if answers:
                    conn.execute(_INDEXED, (answers, joined, self.identity))
            done += len(rows)
            if len(rows) < ROWS_AT_ONCE:
                return done
            after = bytes(rows[-1][0])
            time.sleep(LET_GO)  # more may follow: the event loop takes its turn first

    def _count(self, conn: Connection[Any], entity_ids: list[str], by: int = 1) -> int:
        """Move the identity's change counter by ``by`` in ``conn``'s transaction, and stamp the word rows of
        ``entity_ids`` with the count it reaches (``_COUNT``): the count the change (the last of ``by``) is told
        with."""
        row = conn.execute(_COUNT, (self.identity, by, self.identity, entity_ids)).fetchone()
        assert row is not None  # RETURNING gives the row it counted
        return int(row[0])

    # --- an API key's vault, read as it is needed (latet.state.protocols.KeyVaultStore) ---------------

    def _word(self, role: Role, word: str | None) -> bytes | None:
        """The keyed hash of an anchor word as a value's (``original``) or a fake's (``surrogate``), under a label of
        its own, so that a value's word and a fake's are never one hash; None for a string without one."""
        return None if word is None else self._sealer.keyed_pair(f"{role}-word\x00{word}")[0]

    def _print(self, role: str, word: str | None) -> int | None:
        """The print a row of ``role`` keeps of ``word`` (``latet.server.prints``: its role's slot of the word's four);
        None for none, as the row's word is."""
        return None if word is None else prints(self._prints, word)[SLOT[role]]

    def _words(self, e: Entity) -> tuple[tuple[bytes | None, int | None], tuple[bytes | None, int | None]]:
        """The keyed hashes of ``e``'s two words (``stored.word_of``), its value's and its fake's, each with its
        print."""
        value, fake = word_of(e, "original"), word_of(e, "surrogate")
        return (
            (self._word("original", value), self._print("original", value)),
            (self._word("surrogate", fake), self._print("surrogate", fake)),
        )

    def _rows_of(self, e: Entity) -> list[tuple[str, bytes | None, int | None]]:
        """Every word row ``e`` has, each by its role, with its keyed hash and its print: its two words, then its edges
        (``_edges``, a `held` row too)."""
        (value, value_print), (fake, fake_print) = self._words(e)
        return [
            ("original", value, value_print),
            ("surrogate", fake, fake_print),
            *self._edges(e, "original"),
            *self._edges(e, "surrogate"),
        ]

    def _edge(self, role: Role, word: str | None) -> bytes | None:
        """The keyed hash of an edge (``stored.edges_of``) of a value's or a fake's: the second half of the word's
        keyed pair (``Sealer.keyed_pair``, version 9; version 8 hashed it under a label of its own, review 10b's M5),
        never the hash of a word, so that a reader of the database does not see that a string's head or tail is
        another's anchor. None for none."""
        return None if word is None else self._sealer.keyed_pair(f"{role}-word\x00{word}")[1]

    def _hashed(self, roles: tuple[Role, ...], words: Iterable[str]) -> set[bytes]:
        """A text's words as a store finds the strings of ``roles`` by them: each as a word, and each of ``EDGE``
        letters as an edge too (an edge has as many), both from one keyed hash (``Sealer.keyed_pair``)."""
        found: set[bytes] = set()
        for role in roles:
            for word in set(words):
                hashed, edge = self._sealer.keyed_pair(f"{role}-word\x00{word}")
                found.add(hashed)
                if len(word) == EDGE:
                    found.add(edge)
        return found

    def _hashed_parts(self, roles: tuple[Role, ...], words: Iterable[str]) -> Iterator[list[bytes]]:
        """``_hashed``'s hashes of ``words``, a part at a time, each of WORDS_AT_ONCE hashes at most and sorted: a word
        gives two a role at most, and a part is given before the next word could take it past that, so that no more
        than WORDS_AT_ONCE are made and held at once, however many words a call asks (RESUME-5)."""
        most = max(1, WORDS_AT_ONCE - 2 * len(roles))
        part: set[bytes] = set()
        for word in sorted(set(words)):
            for role in roles:
                hashed, edge = self._sealer.keyed_pair(f"{role}-word\x00{word}")
                part.add(hashed)
                if len(word) == EDGE:
                    part.add(edge)
            if len(part) >= most:
                done, part = sorted(part), set()
                yield done
        if part:
            yield sorted(part)

    def _printed_parts(self, roles: tuple[Role, ...], words: Iterable[str]) -> Iterator[list[int]]:
        """The prints of ``words`` in the slots of ``roles`` (each as a word, and one of ``EDGE`` letters as an edge
        too), a part at a time, each of WORDS_AT_ONCE prints at most and sorted, made as ``_hashed_parts`` makes its
        hashes."""
        most = max(1, WORDS_AT_ONCE - 2 * len(roles))
        part: set[int] = set()
        for word in words:
            made = prints(self._prints, word)
            for role in roles:
                part.add(made[SLOT[role]])
                if len(word) == EDGE:
                    part.add(made[SLOT[f"{role}-head"]])
            if len(part) >= most:
                done, part = sorted(part), set()
                yield done
        if part:
            yield sorted(part)

    def _edges(self, e: Entity, role: Role) -> list[tuple[str, bytes | None, int | None]]:
        """The edges of ``e``'s value (``original``) or fake (``surrogate``), where the vault finds it inside a word
        (``stored.edges_of``, Ruling T10h), each with its role (``original-head``...), its keyed hash (``_edge``) and
        its print (``_print``): a text's words, hashed so, find the string by its word or by an edge. A value held in
        memory for good because no word finds it glued (``stored.held_glued``) has a `held` row as well (its word and
        its print NULL), by which a space loads the newest of them (``load_held``)."""
        head, tail = edges_of(e, role)
        found: list[tuple[str, bytes | None, int | None]] = []
        for side, word in (("head", head), ("tail", tail)):
            hashed = self._edge(role, word)
            if hashed is not None:
                name = f"{role}-{side}"
                found.append((name, hashed, self._print(name, word)))
        if role == "original" and held_glued(e):
            found.append(("held", None, None))
        return found

    def _read(
        self,
        query: LiteralString,
        args: tuple[Any, ...],
        *,
        held: bool,
        newest_first: bool = False,
        rows_read: list[int] | None = None,
    ) -> list[Entity]:
        """The entities of ``query``'s rows (``_SELECT``'s columns), in its order, made Python's and opened
        ``ROWS_AT_ONCE`` at a time, the GIL let go between two parts, as ``load_entities`` reads the vault: an entry
        that does not open left out and counted, a rule kept with a field's name in it kept by the field's kind from
        now on. ``held``: read through a cursor of PostgreSQL's, for what may be many rows, none of which then waits
        whole in memory; otherwise in one answer, for a lookup's few (one round trip). ``newest_first``: ``query``
        reads by ``seq`` from the newest; the rules are written from the oldest all the same, in the order every write
        of several entries holds them (``_HELD``). ``rows_read``: told how many rows were read, those that do not open
        too."""
        found: list[Entity] = []
        cut: list[tuple[str | None, str, str]] = []
        with self._connection() as conn, conn.cursor(name="vault_entities") if held else conn.cursor() as cursor:
            cursor.execute(query, args)
            while rows := cursor.fetchmany(ROWS_AT_ONCE):
                if rows_read is not None:
                    rows_read.append(len(rows))
                cut += self._field_names_cut(rows)
                for row in rows:
                    with contextlib.suppress(UnreadableEntry):
                        found.append(self._opened(row))
                if len(rows) == ROWS_AT_ONCE:
                    time.sleep(LET_GO)  # more may follow: the event loop takes its turn first
        self._cut_field_names(cut[::-1] if newest_first else cut)
        return found

    def load_generation(self) -> int:
        """The generation the vault draws its new fakes in, as PostgreSQL has it now (Task 11b: how many forgets of
        values by themselves forgot something), kept as the store's (``generation``) and answered."""
        with self._connection() as conn:
            found = conn.execute(_GENERATION, (self._generations,)).fetchone()
        self.generation = int(found[0]) if found is not None else 0
        return self.generation

    def count_entities(self) -> int:
        """How many values the vault holds, as PostgreSQL counts them now (those that do not open as well)."""
        with self._connection() as conn:
            row = conn.execute(_COUNTED, (self.identity,)).fetchone()
        return int(row[0]) if row is not None else 0

    def load_recent(self, n: int) -> list[Entity]:
        """The ``n`` values kept last, the newest first."""
        if n <= 0:
            return []
        return self._read(_SELECT + " ORDER BY seq DESC LIMIT %s", (self.identity, n), held=True, newest_first=True)

    def load_unanchored(self) -> list[Entity]:
        """The values whose value or fake has no anchor word (``ab-1``), which no text's words find: the oldest
        first."""
        return self._read(_UNANCHORED, (self.identity, self.identity), held=True)

    def load_held(self, n: int) -> list[Entity]:
        """The ``n`` newest secrets held glued (``stored.held_glued``: a `held` row), the newest first: what a space
        holds of them in memory for good (Ruling 10b-R3), never all of them."""
        return self._read(_GLUED_NEWEST, (self.identity, self.identity, n), held=True, newest_first=True)

    @property
    def closed(self) -> bool:
        """Whether its space closed it (``close``, ``revoke_index``): its background work stops at its next part."""
        return self._closed.is_set()

    def of_class(self, eclass: EntityClass) -> Iterator[Entity]:
        """Every value of the vault of the class ``eclass``, the oldest first, those out of a space's memory too (the
        names a person's space forgets once its filters reject them, ``Runtime.forget_rejected_names``), given one at a
        time as a cursor of PostgreSQL's reads them ``ROWS_AT_ONCE`` rows at a time (``streamed``): never every name
        of the vault in memory at once (Task 9's I4). One that does not open is left out and counted."""
        return self.streamed(_OF_CLASS, (self.identity, int(eclass)))

    def whole_entities(self) -> list[Entity]:
        """Every value of the vault, the oldest first, through ``streamed``: what a person's vault reads past a bound of
        a call (``StoredVault._whole_read``), with none of ``load_entities``' doings (the counts set anew, the rules
        written, the warning of a vault over its limit said at each read)."""
        return list(self.streamed(_SELECT + " ORDER BY seq", (self.identity,)))

    def streamed(self, query: LiteralString, args: tuple[Any, ...]) -> Iterator[Entity]:
        """The entities of ``query``'s rows (``_SELECT``'s columns), in its order, opened ``ROWS_AT_ONCE`` at a time
        through a cursor of PostgreSQL's (its connection held until the last is given or the iterator closed), the GIL
        let go between two parts, and nothing else done: no count of the vault or of what does not open set anew (one
        that does not open is counted, and said once, as ``_opened`` says it), no rule written, nothing logged but
        that."""
        with self._connection() as conn, conn.cursor(name="vault_streamed") as cursor:
            cursor.execute(query, args)
            while rows := cursor.fetchmany(ROWS_AT_ONCE):
                for row in rows:
                    with contextlib.suppress(UnreadableEntry):
                        yield self._opened(row)
                if len(rows) == ROWS_AT_ONCE:
                    time.sleep(LET_GO)

    def listed(
        self, *, q: str, etype: str, eclass: str, page: int, per_page: int
    ) -> tuple[list[Entity], int, dict[str, int]]:
        """The dashboard's page of the vault (a person's space, which holds its working set alone in memory): the
        ``page``-th of ``per_page`` values, the newest first (by ``created``, then by id), a page past the last being
        the last; how many values match; and how many values of each type the whole vault holds, whatever matches. The
        type (``etype``, as kept) and the class (``eclass``, its name; one no class has matches nothing) are filtered by
        PostgreSQL; a search (``q``, in lower case: part of a value, of a fake or of a type, or an entry's id) opens
        the entries of that type and class ``ROWS_AT_ONCE`` at a time, the newest first, keeping the page's alone and
        the last page's: never the whole vault in memory, nor in the working set. A value that does not open is left
        out of a search and of its count, and counted (``unreadable``); without a search it is counted among those that
        match, as PostgreSQL counts them."""
        with self._connection() as conn:
            types = {str(found): int(n) for found, n in conn.execute(_TYPES, (self.identity,)).fetchall()}
        chosen = EntityClass.__members__.get(eclass) if eclass else None
        if eclass and chosen is None:
            return [], 0, types
        number = int(chosen) if chosen is not None else None
        args = (self.identity, etype, etype, number, number)
        if not q:
            with self._connection() as conn:
                row = conn.execute(_OF_KIND_COUNTED, args).fetchone()
            total = int(row[0]) if row is not None else 0
            pages = max(1, -(-total // per_page))
            at = (min(max(1, page), pages) - 1) * per_page
            shown = self._read(_OF_KIND + _NEWEST + " LIMIT %s OFFSET %s", (*args, per_page, at), held=False)
            return shown, total, types
        start, total = (max(1, page) - 1) * per_page, 0
        shown, last = [], deque[Entity](maxlen=per_page)
        with self._connection() as conn, conn.cursor(name="vault_listed") as cursor:
            cursor.execute(_OF_KIND + _NEWEST, args)
            while rows := cursor.fetchmany(ROWS_AT_ONCE):
                for row in rows:
                    try:
                        e = self._opened(row)
                    except UnreadableEntry:
                        continue
                    # Latet's own launch key is never found by its words (N-1): its original is not searched.
                    in_original = not latet_key(e.etype, e.original) and q in e.original.lower()
                    if not (q in e.surrogate.lower() or q in e.etype.lower() or q == e.id or in_original):
                        continue
                    if start <= total < start + per_page:
                        shown.append(e)
                    last.append(e)
                    total += 1
                time.sleep(LET_GO)  # the event loop takes its turn between two parts
        pages = max(1, -(-total // per_page))
        if total and page > pages:  # past the last: the last, the matches after the pages before it
            shown = list(last)[-(total - (pages - 1) * per_page) :]
        return shown, total, types

    def load_glued(self) -> list[Entity]:
        """The values whose value or fake no word finds glued on both sides (``stored.glued_both``: a head and a tail
        row of one role), the oldest first: what a space that keeps them all whatever their age holds in memory for
        good (``StoredVault(glued_all=True)``)."""
        return self._read(_GLUED_BOTH, (self.identity, self.identity), held=True)

    def current(self) -> bool:
        """Observe the counter, with safe SQL after an index failure. Each vault also observes ``freshness``.

        The counter is authoritative even with no index: negative engine caches and an open call's final check
        need to notice remote commits. A local transaction reads through its own connection, never publishing
        its uncommitted rows into the process index or cache.
        """
        if self._closed.is_set():
            raise psycopg.OperationalError("the vault store is closed")
        # No store lock while waiting for a pool connection: a transaction owning the last connection may
        # itself be doing this final check. PrintIndex serializes its own catch-ups and only publishes arrays.
        index, joined = self.index, self._joined()
        indexed = False
        if index is not None and index.ready and (joined is None or joined.conn is None):
            try:
                if index.told < index.watermark:
                    # Built, or caught up by a caller that told no cache (``told`` moves before the watermark): what
                    # its rows named is not known here.
                    index.tell(self._cache, self._telling)
                index.catch_up(cache=self._cache, named=self._telling)
                with self._index_lock:
                    if self.index is index:
                        self._counter_seen = max(self._counter_seen, index.watermark)
                        indexed = True
            except Exception as exc:  # its type alone, said by the space (``Spaces._build_index_again``), built again
                self.index_failure = type(exc).__name__
                with self._telling(None):
                    index.ready = False
                    self._cache.clear()
        if not indexed:
            # A detached catch-up certifies no published index. Read the authority without the publication lock;
            # lookups below reject any replacement whose watermark is behind this observed counter.
            with self._connection() as conn:
                row = conn.execute("SELECT counter FROM change_counters WHERE owner = %s", (self.identity,)).fetchone()
            counter = int(row[0]) if row else 0
            if counter > self._counter_seen:
                # A count read alone names nothing: a vault that sees it confirms its whole memory.
                with self._telling(None):
                    # The cache is read only while the published index is ready and its watermark is at least this
                    # counter (``bounded_words``), and every catch-up of that index tells the cache what its rows name:
                    # past this counter, the cache waits for the index to catch up, and is told then. With no index
                    # ready, nothing tells it.
                    ready = self.index
                    if ready is None or not ready.ready:
                        self._cache.clear()
                    self._counter_seen = max(self._counter_seen, counter)
        now = self.freshness
        changed = now != self._observed
        self._observed = now
        return changed if self.index is not None else False  # SQL-only stores retain KeyVaultStore.current's contract

    def may_be_fake(self, string: str) -> bool:
        """False only for a current, complete index and an anchored string with no fake-role print.

        Wordless and escaped/short legacy fakes retain authoritative exact lookup. The caller checks freshness
        at its operation boundary; a standalone exact lookup checks here too.
        """
        from latet.engine.anchors import address_word, anchor, dotted

        index = self.index
        joined = self._joined()
        if (
            index is None
            or not index.ready
            or index.watermark < self._counter_seen
            or index.may_have_readings
            or self._closed.is_set()
            or (joined is not None and joined.conn is not None)
        ):
            return True
        words = {word for word in (address_word(string), dotted(string), anchor(string)) if word is not None}
        if not words:
            return True
        wanted = [prints(self._prints, word)[SLOT["surrogate"]] for word in words]
        return bool(index.present(wanted).any())

    def by_original(self, value: str) -> Entity | None:
        """The entity of ``value``, by its keyed hash; None when there is none, or it does not open."""
        found = self._read(
            _SELECT + " AND original_key = %s", (self.identity, self._sealer.keyed(f"original\x00{value}")), held=False
        )
        return found[0] if found else None

    def by_surrogate(self, fake: str) -> Entity | None:
        """The entity whose fake is ``fake``, by its keyed hash; None when there is none, or it does not open."""
        found = self._read(
            _SELECT + " AND surrogate_key = %s", (self.identity, self._sealer.keyed(f"surrogate\x00{fake}")), held=False
        )
        return found[0] if found else None

    def by_reading(self, reading: str) -> Entity | None:
        """The entity whose fake, written in escapes, reads as ``reading`` (``vault.fake_reading``: `Giulia Martini` for
        `Giulia%20Martini`), by the keyed hash the vault keeps of that reading among its strings (``_read_as``,
        ``backfill_words``): one probe, whatever shares a word with it. None when there is none, or it does not open."""
        found = self._read(_BY_READING, (self.identity, self.identity, self._sealer.keyed(reading)), held=False)
        return next((e for e in found if fake_reading(e) == reading), None)

    def with_words(self, role: Role | tuple[Role, ...], words: Iterable[str]) -> list[Entity]:
        """The entities whose value (``original``) or fake (``surrogate``) has one of ``words`` as its anchor, or as one
        of its edges (Ruling T10h), each once, the oldest first within each part: asked for by the words' keyed hashes,
        ``WORDS_AT_ONCE`` at a time. Of
        more than ``WORDS_ASKED_AT_MOST`` different words, the vault's own words are read once instead and met with
        them in memory (Ruling T7c). Raises TooManyCandidates when more than ``CANDIDATES_AT_MOST`` entities have one
        of them: no more than that and one are opened."""
        asked: tuple[Role, ...] = (role,) if isinstance(role, str) else tuple(role)
        if not asked or any(r not in ("original", "surrogate") for r in asked):
            raise ValueError("a word is a value's (original) or a fake's (surrogate)")
        if self.index is not None:
            self.current()
            if self.index.ready:
                return self.bounded_words(asked, words, _LegacyBudget())
        hashed = sorted(self._hashed(asked, words))  # a role's label is in each hash: one statement asks for all
        roles = [name for r in asked for name in (r, f"{r}-head", f"{r}-tail")]
        if len(hashed) > WORDS_ASKED_AT_MOST:
            return self.by_ids(self._met(roles, set(hashed)))
        found: dict[str, Entity] = {}
        # An entry has an anchor and two edges at most: it is in three parts at most, so each part may read what is
        # left (an entry read twice counted twice). The rows read are counted, an entry that does not open as well: a
        # part cut short by the bound is refused, never answered without the entries it did not read (Review Minor 1).
        read: list[int] = []
        for start in range(0, len(hashed), WORDS_AT_ONCE):
            part, room = hashed[start : start + WORDS_AT_ONCE], CANDIDATES_AT_MOST + 1 - sum(read)
            args = (self.identity, self.identity, roles, part, room)
            for e in self._read(_WITH_WORDS, args, held=False, rows_read=read):
                found.setdefault(e.id, e)
            if sum(read) > CANDIDATES_AT_MOST:
                raise _too_many()
        return list(found.values())

    def bounded_words(self, role: Role | tuple[Role, ...], words: Iterable[str], budget: LookupBudget) -> list[Entity]:
        """The same candidates as ``with_words``, bounded by the outer masking/restoring call. Never reads all
        vault words. Charge unreadable rows too, and reject before opening a row beyond either cumulative bound
        (LookupLimitExceeded: the stored vault reads past it as the release before read, ``engine.lookup``). The SQL
        guard limits the entire encrypted payload transferred, even for one arbitrarily large row. More than
        ``CANDIDATES_AT_MOST`` rows read for these words: ``TooManyCandidates``, where 0.10.2's ``with_words`` refused
        them.
        """
        asked: tuple[Role, ...] = (role,) if isinstance(role, str) else tuple(role)
        if not asked or any(r not in ("original", "surrogate") for r in asked):
            raise ValueError("a word is a value's (original) or a fake's (surrogate)")
        budget.check(0, 0)
        if self._closed.is_set():
            raise psycopg.OperationalError("the vault store is closed")
        before = budget.candidates
        found = self._bounded_words(asked, set(words), budget)
        if budget.candidates - before > CANDIDATES_AT_MOST:
            raise _too_many()
        return found

    def _bounded_words(self, asked: tuple[Role, ...], wanted: set[str], budget: LookupBudget) -> list[Entity]:
        index, joined = self.index, self._joined()
        if (
            index is not None
            and index.ready
            and index.watermark >= self._counter_seen
            and (joined is None or joined.conn is None)
        ):
            return self._indexed_words(index, asked, wanted, budget)
        roles = [name for r in asked for name in (r, f"{r}-head", f"{r}-tail")]
        found: dict[str, Entity] = {}
        cut: list[tuple[str | None, str, str]] = []
        # One connection for every part, each answered whole by a cursor of the client's: an answer is bounded in rows
        # and in bytes before it is sent (``_BOUNDED_WORDS``), so that the whole of it, up to the call's remaining byte
        # allowance (64 MiB of payload a call at most), is held at once where a cursor of PostgreSQL's held a part of
        # ``ROWS_AT_ONCE`` rows; it saves the round trips of declaring and closing one. The words are hashed a part at
        # a time (``_hashed_parts``), never more than WORDS_AT_ONCE hashes at once (RESUME-5).
        with self._connection() as conn:
            for hashed in self._hashed_parts(asked, wanted):
                # (a part is WORDS_AT_ONCE hashes at most, but where WORDS_AT_ONCE is below one word's hashes)
                for start in range(0, len(hashed), WORDS_AT_ONCE):
                    part = hashed[start : start + WORDS_AT_ONCE]
                    budget.check(0, 0)
                    args = (self.identity, self.identity, roles, part, budget.row_room, budget.byte_room)
                    with conn.cursor() as cursor:
                        _unprepared(cursor, _BOUNDED_WORDS, args)
                        while rows := cursor.fetchmany(ROWS_AT_ONCE):
                            for row in rows:
                                budget.row(int(row[9]))
                                # Oversized rows have no string fields on the wire. row() has already refused their
                                # size.
                                assert row[0] is not None
                                plain = row[:9]
                                cut += self._field_names_cut([plain])
                                with contextlib.suppress(UnreadableEntry):
                                    entity = self._opened(plain)
                                    budget.entity(entity, counted=True)
                                    found.setdefault(entity.id, entity)
                            if len(rows) == ROWS_AT_ONCE:
                                time.sleep(LET_GO)
        self._cut_field_names(cut)
        return list(found.values())

    def _indexed_words(
        self, index: PrintIndex, roles: tuple[Role, ...], words: set[str], budget: LookupBudget
    ) -> list[Entity]:
        if index.told < index.watermark:  # moved by no catch-up that told this cache (``current``'s own check)
            index.tell(self._cache, self._telling)
        # Capture the epoch BEFORE both cache lookup and SQL. A catch-up or resync may race with either.
        since = self._cache.epoch
        # The words printed a part at a time, never more than WORDS_AT_ONCE prints at once (RESUME-5's bounded
        # hashing), each part asked of the index, and only the prints it holds kept.
        wanted: set[int] = set()
        for part in self._printed_parts(roles, words):
            wanted.update(printed for printed, present in zip(part, index.present(part), strict=True) if present)
        found, unknown = self._cache.get(wanted)
        for entity in found:
            budget.entity(entity)
        by_id = {entity.id: entity for entity in found}
        pending = sorted(unknown)
        if not pending:
            return self._matching(by_id, roles, words)
        cut: list[tuple[str | None, str, str]] = []
        # One connection of the pool for every part, in autocommit: each statement is a read of its own, as each part's
        # transaction was, with no BEGIN and COMMIT around it; and each answered whole by a cursor of the client's, as
        # it is bounded in rows and in bytes before it is sent (``_BY_PRINTS``): the whole of it, up to the call's
        # remaining byte allowance, is held at once. Never this thread's open transaction: the caller reads the index
        # only outside one (``bounded_words``).
        with self._autocommit() as conn:
            for start in range(0, len(pending), WORDS_AT_ONCE):
                part = pending[start : start + WORDS_AT_ONCE]
                fetched: dict[int, list[Entity]] = {printed: [] for printed in part}
                unreadable = False
                budget.check(0, 0)
                args = (part, self.identity, self.identity, part, budget.row_room, budget.byte_room)
                with conn.cursor() as cursor:
                    _unprepared(cursor, _BY_PRINTS, args)
                    while rows := cursor.fetchmany(ROWS_AT_ONCE):
                        for row in rows:
                            budget.row(int(row[9]))
                            assert row[0] is not None
                            cut += self._field_names_cut([row[:9]])
                            try:
                                entity = self._opened(row[:9])
                            except UnreadableEntry:
                                # Cache entries cannot represent unreadable-row charges. Re-fetch this group next time.
                                unreadable = True
                                continue
                            budget.entity(entity, counted=True)
                            by_id.setdefault(entity.id, entity)
                            for printed in row[10]:
                                fetched[int(printed)].append(entity)
                        if len(rows) == ROWS_AT_ONCE:
                            time.sleep(LET_GO)
                if not unreadable:
                    self._cache.put(fetched, since)
        self._cut_field_names(cut)
        return self._matching(by_id, roles, words)

    @staticmethod
    def _matching(by_id: dict[str, Entity], roles: tuple[Role, ...], words: set[str]) -> list[Entity]:
        """Those of ``by_id`` whose decrypted word or edge of one of ``roles`` is one of ``words``."""
        # All roles shared the complete cache group; only the actual decrypted role/word may leave this method.
        return [
            entity
            for entity in by_id.values()
            if any(
                word_of(entity, role) in words or any(edge in words for edge in edges_of(entity, role))
                for role in roles
            )
        ]

    def bounded_ids(self, ids: Iterable[str], budget: LookupBudget) -> list[Entity]:
        """Authoritative by-id confirmation with the same pre-transfer and cumulative row guards."""
        budget.check(0, 0)
        if self._closed.is_set():
            raise psycopg.OperationalError("the vault store is closed")
        asked = sorted(set(ids))
        found: list[Entity] = []
        # One connection, each part answered whole by a cursor of the client's: bounded before it is sent.
        with self._connection() as conn:
            for start in range(0, len(asked), WORDS_AT_ONCE):
                budget.check(0, 0)
                args = (self.identity, asked[start : start + WORDS_AT_ONCE], budget.row_room, budget.byte_room)
                with conn.cursor() as cursor:
                    _unprepared(cursor, _BOUNDED_IDS, args)
                    while rows := cursor.fetchmany(ROWS_AT_ONCE):
                        for row in rows:
                            budget.row(int(row[9]))
                            assert row[0] is not None
                            with contextlib.suppress(UnreadableEntry):
                                entity = self._opened(row[:9])
                                budget.entity(entity, counted=True)
                                found.append(entity)
        return found

    def bounded_retained(
        self, held: int, glued: bool, budget: LookupBudget
    ) -> tuple[list[Entity], list[Entity], list[Entity]]:
        """Refresh retained values under the same row, character and pre-wire payload limits as the call: on one
        connection, each answer bounded before it is sent and read whole by a cursor of the client's."""
        with self._connection() as conn:
            unanchored = self._retained(conn, _BOUNDED_UNANCHORED, budget, budget.row_room)
            newest = self._retained(conn, _BOUNDED_HELD, budget, min(held, budget.row_room)) if held else []
            both = self._retained(conn, _BOUNDED_GLUED, budget, budget.row_room) if glued else []
        return unanchored, newest[::-1], both  # load_held's newest-first contract

    def _retained(self, conn: Connection[Any], query: sql.Composed, budget: LookupBudget, room: int) -> list[Entity]:
        budget.check(0, 0)
        found = []
        with conn.cursor() as cursor:
            _unprepared(cursor, query, (self.identity, self.identity, room, budget.byte_room))
            while rows := cursor.fetchmany(ROWS_AT_ONCE):
                for row in rows:
                    budget.row(int(row[9]))
                    assert row[0] is not None
                    with contextlib.suppress(UnreadableEntry):
                        entity = self._opened(row[:9])
                        budget.entity(entity, counted=True)
                        found.append(entity)
        return found

    def _met(self, roles: list[str], hashed: set[bytes]) -> list[str]:
        """The ids of the entries whose value's (or fake's) word or edge (``roles``) is one of ``hashed``: every such
        word of the vault's read once, through a cursor of PostgreSQL's, ``WORDS_AT_ONCE`` rows at a time, the GIL let
        go between two parts. Raises TooManyCandidates past ``CANDIDATES_AT_MOST`` of them."""
        ids: list[str] = []
        with self._connection() as conn, conn.cursor(name="vault_words") as cursor:
            cursor.execute(_ALL_WORDS, (self.identity, roles))
            while rows := cursor.fetchmany(WORDS_AT_ONCE):
                ids += [str(entity_id) for word, entity_id in rows if bytes(word) in hashed]
                if len(ids) > CANDIDATES_AT_MOST:
                    raise _too_many()
                time.sleep(LET_GO)  # the event loop takes its turn between two parts
        return ids

    def by_ids(self, ids: Iterable[str]) -> list[Entity]:
        """The entities of ``ids`` that the vault holds and that open, each once, the oldest first within each part of
        ``WORDS_AT_ONCE`` ids: what a space holds in memory, asked whether the vault still has it (``StoredVault``)."""
        asked = sorted(set(ids))
        found: list[Entity] = []
        for start in range(0, len(asked), WORDS_AT_ONCE):
            found += self._read(_BY_IDS, (self.identity, asked[start : start + WORDS_AT_ONCE]), held=False)
        return found

    def backfill_words(self) -> int:
        """Make the words that are not there or not as ``anchor`` makes them now: an entry's that has none (kept by a
        Latet before 0.10.0, or whose fake was given anew since and has its fake's alone), one of an older version of
        the anchors (``ANCHORS_VERSION``), and a NULL one (an entry that did not open when its words were made): how
        many values got a word. ``ROWS_AT_ONCE`` at a time, each opened in memory alone. A word of the current version
        stays as it is, and a NULL one with no word to give stays NULL, so that it is safe to run again (it gives
        nothing then), and on two instances at once. An entry that does not open gets two NULL words of the current
        version, so that the counts no longer find it missing and no later build reads the vault for it (it is counted,
        in ``unreadable``). A word whose string was forgotten or given a new fake between the reading of its part and
        the writing of its words is left out (it is the change's). A rule kept with a field's name in it is kept by the
        field's kind from now on, as ``load_entities`` keeps it. How a fake written in escapes reads joins the vault's
        strings with its words (``_READINGS``: an entry kept by 0.9.9 or 0.9.10 has none), so that no fake drawn later
        is equal to it (Review Important 3)."""
        filled = 0
        with self.db.connection() as conn:
            missing = conn.execute(_MISSING, (self.identity, self.identity, ANCHORS_VERSION)).fetchone()
        if missing is not None and missing[0] > 0:  # else every value has its two words: the vault is not read
            filled += self._fill(_WORDLESS, lambda after: (self.identity, after, ANCHORS_VERSION, ROWS_AT_ONCE))
        return filled + self._fill(_NULL_WORDS, lambda after: (self.identity, after, self.identity, ROWS_AT_ONCE))

    def _fill(self, query: LiteralString, args: Callable[[int], tuple[Any, ...]]) -> int:
        """The words of the entries ``query`` reads (``_WORDLESS``'s columns), a part at a time after the ``seq`` read
        last (``args``): how many got one. Each part's entries are held (``_HELD``) while their words are written, a
        word written only while its own string's keyed hash is the one read (the value's word of an entry whose fake
        changed meanwhile is written), and in the order they were read, as another instance writing the same ones
        writes them; each word with its print. A part that wrote a row moves the identity's count once, its entries'
        rows stamped with it (``_COUNT``), and tells it as `words` once committed (no entry's: the index of prints of
        each instance catches up by it, the plan's S3)."""
        filled, after = 0, 0
        while True:
            if self._closed.is_set():  # its space closed: the next build of it makes the rest (a person's, Task 9)
                return filled
            with self.db.connection() as conn:
                rows = conn.execute(query, args(after)).fetchall()
            if not rows:
                return filled
            # By entry, in the order read: the keyed hashes of its value and fake, and their words with their prints;
            # their edges; and the keyed hash of how its fake reads, where it is written in escapes (an entry kept by
            # 0.9.9 or 0.9.10 has no such row).
            read: dict[str, tuple[tuple[bytes, bytes], tuple[tuple[bytes | None, int | None], ...]]] = {}
            edged: dict[str, tuple[list[tuple[str, bytes | None, int | None]], ...]] = {}
            readings: dict[str, bytes] = {}
            for row in rows:
                try:
                    opened = self._opened(row)
                    both = self._words(opened)
                    edged[row[0]] = (self._edges(opened, "original"), self._edges(opened, "surrogate"))
                    if (reading := fake_reading(opened)) is not None:
                        readings[row[0]] = self._sealer.keyed(reading)
                except UnreadableEntry:
                    both = ((None, None), (None, None))  # its words cannot be known
                read[row[0]] = ((row[10], row[11]), both)
            counter = 0
            with self.db.transaction() as conn:
                held = {row[0]: (row[1], row[2]) for row in conn.execute(_HELD, (self.identity, list(read)))}
                ids: list[str] = []
                roles: list[str] = []
                words: list[bytes | None] = []
                printed: list[int | None] = []
                for entity_id, (keys, both) in read.items():
                    now = held.get(entity_id)
                    if now is None:
                        continue  # forgotten meanwhile
                    sides = edged.get(entity_id, ([], []))
                    for role, key, current, (word, its_print), its in zip(
                        ("original", "surrogate"), keys, now, both, sides, strict=True
                    ):
                        if key == current:  # else its string changed meanwhile, and the change wrote its word
                            for name, hashed, its_printed in ((role, word, its_print), *its):
                                ids.append(entity_id)
                                roles.append(name)
                                words.append(hashed)
                                printed.append(its_printed)
                conn.execute(_OLD_EDGES, (self.identity, list(read), ANCHORS_VERSION))
                added = conn.execute(
                    _ADD_WORDS, (self.identity, ANCHORS_VERSION, ids, roles, words, printed)
                ).fetchall()
                # How each fake still as it was read reads: a string the vault knows from now on (``_read_as``), but
                # where it is one already, as a value or a fake (a collision kept before, left as it is).
                fakes = [
                    e for e, key in readings.items() if (now := held.get(e)) is not None and read[e][0][1] == now[1]
                ]
                conn.execute(_READINGS, (self.identity, [readings[e] for e in fakes], fakes))
                if added:  # rows written: counted, last, as a change is
                    counter = self._count(conn, list(read))
            if counter:
                self._changed("words", None, counter)
            filled += len({entity_id for entity_id, word in added if word is not None})
            self._cut_field_names(self._field_names_cut(rows))
            after = rows[-1][9]
            time.sleep(LET_GO)  # the event loop takes its turn between two parts

    # --- the prints of the words (latet.server.prints, the server performance plan's Task 5) -----------

    def words_complete(self) -> bool:
        """Whether every value has its words, so that the backfill (``backfill_words``) would make none: each entry its
        two of the current version of the anchors (or a later one), and no NULL one that its entry has a word for now
        (a value or a fake with no anchor, and an entry that does not open, keep theirs NULL: those are complete). The
        two counts of ``_MISSING``, then the few entries with a NULL word, opened."""
        with self._connection() as conn:
            missing = conn.execute(_MISSING, (self.identity, self.identity, ANCHORS_VERSION)).fetchone()
            if missing is not None and missing[0] > 0:
                return False
            nulls = conn.execute(_NULL_ROLES, (self.identity, ANCHORS_VERSION)).fetchall()
        roles: dict[str, list[Role]] = {}
        for entity_id, role in nulls:
            roles.setdefault(str(entity_id), []).append("original" if role == "original" else "surrogate")
        return not any(word_of(e, role) is not None for e in self.by_ids(roles) for role in roles[e.id])

    def prints_complete(self) -> bool:
        """Whether every word row has its print (``vault_words_printless`` holds none of the identity's rows): what an
        index of the vault's prints needs to stand for its words (``latet.server.index``)."""
        with self._connection() as conn:
            row = conn.execute(_PRINTLESS, (self.identity,)).fetchone()
        return row is not None and not row[0]

    def fill_prints(self) -> int:
        """Give a print to each word row that has none (a vault kept by 0.10.0): how many rows got one. On one instance
        at a time (``jobs``: ``prints:<identity>`` held ``PRINTS_HOLD_SECONDS``); none while another holds the job,
        which makes them. ``ROWS_AT_ONCE`` entries at a time, in the order of ``seq``, the GIL let go between two parts:
        each entry opened, its words made as ``stored.word_of`` and ``stored.edges_of`` make them, and a print written
        only beside the word whose keyed hash is the row's (``_PRINTS``): a row whose hash its entry does not make now
        (written by hand, an older version's that the backfill makes again) and the rows of an entry that does not open
        (counted in ``unreadable``) get their unusable word NULLed; the encrypted entry stays. Each part's entries are
        held while their prints
        are written (``_HELD_FOR_PRINTS``); a part that wrote one moves the identity's count once, its rows stamped with
        it, and tells it as `words` once committed. A fill that wrote any and walked every row tells `prints` at its
        end, with the last count, so that the other instances' spaces build their indexes again
        (``spaces.Spaces._changed``). Safe to run again, and on two instances at once: it gives nothing twice."""
        jobs = self._jobs
        # The measured million-value fill exceeds five minutes. Size the lease from the actual population,
        # with a wide per-entry margin; expiration only permits a second idempotent walk, never partial readiness.
        seconds = max(PRINTS_HOLD_SECONDS, self.count_entities() * 0.002)
        holding = jobs.hold(f"prints:{self.identity}", seconds) if jobs is not None else contextlib.nullcontext(True)
        with holding as mine:
            if not mine:
                return 0
            return self._fill_prints()

    def _fill_prints(self) -> int:
        filled, after, told = 0, 0, 0
        while True:
            if self._closed.is_set():
                return filled
            with self.db.connection() as conn:
                rows = conn.execute(_PRINTLESS_ENTRIES, (self.identity, after, ROWS_AT_ONCE)).fetchall()
            if not rows:
                break
            ids: list[str] = []
            roles: list[str] = []
            words: list[bytes] = []
            printed: list[int | None] = []
            for row in rows:
                try:
                    opened = self._opened(row)
                except UnreadableEntry:
                    continue  # under the row lock below, its unusable words become NULL
                for role, hashed, its_print in self._rows_of(opened):
                    if hashed is not None:
                        ids.append(opened.id)
                        roles.append(role)
                        words.append(hashed)
                        printed.append(its_print)
            part = [str(row[0]) for row in rows]
            done = counter = 0
            if part:
                with self.db.transaction() as conn:
                    conn.execute(_HELD_FOR_PRINTS, (self.identity, part))
                    done = conn.execute(_PRINTS, (ids, roles, words, printed, self.identity)).rowcount
                    nulled = conn.execute(
                        "UPDATE vault_words SET word = NULL WHERE identity_id = %s AND entity_id = ANY(%s)"
                        " AND word IS NOT NULL AND print IS NULL",
                        (self.identity, part),
                    ).rowcount
                    if done or nulled:  # rows written: counted, last, as a change is
                        counter = self._count(conn, part)
            if counter:
                self._changed("words", None, counter)
                told = counter
            filled += done
            if len(rows) < ROWS_AT_ONCE:
                break
            after = rows[-1][9]
            time.sleep(LET_GO)  # the event loop takes its turn between two parts
        if told:  # every row walked: the fill is over
            self._changed("prints", None, told)
        return filled

    # --- the local reviewer's answers ---------------------------------------------------------------

    def load_review(self, key: bytes) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute(
                "UPDATE reviews SET ts = %s WHERE identity_id = %s AND key = %s RETURNING data",
                (time.time(), self.identity, key),
            ).fetchone()
        if row is None:
            return None
        data = None
        with contextlib.suppress(InvalidTag, ValueError):  # another root key, or another text's answer moved here
            data = json.loads(self._sealer.open(row[0], self._review_aad(key)))
        return data if isinstance(data, dict) else None

    def save_review(self, key: bytes, answer: dict[str, Any]) -> None:
        """Keep an answer of the local reviewer, sealed, with the keyed hashes of the values it found (review 11b's
        I2: what a forget finds it by), and drop those not read again for ``_REVIEW_DAYS``. A vault whose values are
        not forgotten by themselves (a person's: ``forgets_values``) keeps it with none, an empty list (never NULL,
        which a forget would index first): nothing that tells a reader of the database which answers found the same
        value (the final review's M6, Ruling F1)."""
        sealed = self._sealer.seal(json.dumps(answer, ensure_ascii=False).encode(), self._review_aad(key))
        now = time.time()
        found = self._found_keys(answer) if self.forgets_values else []
        with self.db.connection() as conn:
            conn.execute(_ANSWER, (self.identity, key, sealed, now, found))
            conn.execute(
                "DELETE FROM reviews WHERE identity_id = %s AND ts < %s", (self.identity, now - _REVIEW_DAYS * 86400)
            )

    def forget_reviews(self) -> None:
        with self.db.connection() as conn:
            conn.execute("DELETE FROM reviews WHERE identity_id = %s", (self.identity,))

    # --- your decisions on images -------------------------------------------------------------------

    def _image_name(self, key: str) -> bytes:
        """What names the row of a decision on the image of ``key``: a keyed hash of it."""
        return hmac.new(self._images, key.encode(), hashlib.sha256).digest()

    def load_image_decision(self, key: str) -> dict[str, Any] | None:
        name = self._image_name(key)
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT data FROM image_decisions WHERE identity_id = %s AND key = %s", (self.identity, name)
            ).fetchone()
        if row is None:
            return None
        found = None
        with contextlib.suppress(InvalidTag, ValueError):  # another root key, or another image's decision moved here
            found = json.loads(self._sealer.open(row[0], self._image_aad(name)))
        return found if isinstance(found, dict) else None

    def save_image_decision(self, key: str, decision: dict[str, Any]) -> None:
        name = self._image_name(key)
        sealed = self._sealer.seal(json.dumps(decision).encode(), self._image_aad(name))
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO image_decisions (identity_id, key, data, ts) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (identity_id, key) DO UPDATE SET data = EXCLUDED.data, ts = EXCLUDED.ts",
                (self.identity, name, sealed, time.time()),
            )

    def close(self) -> None:
        """Revoke index publication immediately; the pool remains the server's."""
        with self._index_lock:
            self._closed.set()
            if self.index is not None:
                self.index.ready = False
            self._cache.clear()

    def revoke_index(self) -> None:
        """Stop a pending build at once on the event loop; close completes its synchronization in a worker."""
        self._closed.set()

    def drop_cached(self, entity_id: str) -> None:
        """An explicit forget notification also revokes older fetches of this id, and names it to the vaults' resync."""
        with self._telling([entity_id]):
            self._cache.drop([entity_id], [])


def _too_many() -> TooManyCandidates:
    return TooManyCandidates(
        f"the texts of this call hold the words of more than {CANDIDATES_AT_MOST} values or fakes of this vault, more "
        "than one call may weigh: nothing of it was masked or restored"
    )


def vault_values(db: Database, identity_id: str) -> int:
    """How many values the identity's vault holds, as PostgreSQL counts them now (those that do not open as well)."""
    with db.connection() as conn:
        row = conn.execute(_COUNTED, (identity_id,)).fetchone()
    return int(row[0]) if row is not None else 0


# Each identity's values, and its fakes retired (``_RETIRED``, through migration 012's partial index), weighed together
# against its limit: an API key's own (``Identity.vault_limit``) when it has one, else the server's.
_USAGE: Final = (
    "WITH v AS (SELECT identity_id, COUNT(*) AS n FROM vault_entities GROUP BY identity_id),"
    " r AS (SELECT identity_id, COUNT(*) AS n FROM vault_strings WHERE role = 'retired' GROUP BY identity_id),"
    " u AS (SELECT COALESCE(v.identity_id, r.identity_id) AS identity_id, COALESCE(v.n, 0) AS kept,"
    " COALESCE(r.n, 0) AS retired FROM v FULL JOIN r ON r.identity_id = v.identity_id)"
    " SELECT u.identity_id, u.kept, u.retired, COALESCE(i.vault_limit, %(limit)s) FROM u"
    " JOIN identities i ON i.id = u.identity_id"
    " WHERE u.kept + u.retired >= CEIL(COALESCE(i.vault_limit, %(limit)s)::numeric * %(share)s)"
    " ORDER BY (u.kept + u.retired)::numeric / COALESCE(i.vault_limit, %(limit)s) DESC, u.kept + u.retired DESC,"
    " u.identity_id LIMIT 100"
)


def vault_usage(db: Database, limit: int, *, share: float = 0.9) -> list[tuple[str, int, int, int]]:
    """The identities whose vault holds ``share`` of its limit or more, each with its values, its fakes retired and its
    limit: an API key's own (``Identity.vault_limit``) when it has one, else ``limit``, the server's. A retired fake
    takes no room (the limit counts values alone), but it stays as long as the vault, and a key that masks and forgets
    in a loop grows them for ever (the security re-review's R-L1): they are weighed with the values here, so that such
    a vault shows. The fullest first, by the share of its limit each holds (at most a hundred): for the
    administration's status and E0c's metrics."""
    # In decimals, rounded first: in binary, 0.7 of 1,290 is 902.9999999999999.
    shown = Decimal(str(round(share, 6)))
    with db.connection() as conn:
        rows = conn.execute(_USAGE, {"limit": limit, "share": shown}).fetchall()
    return [(str(row[0]), int(row[1]), int(row[2]), int(row[3])) for row in rows]
