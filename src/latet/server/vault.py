"""An identity's vault in PostgreSQL (``storage.db.Store`` on a computer): its values and their fakes sealed with its
root key, beside a keyed hash of each, so that the database refuses a second entity for one value (the first type
wins, as in a vault's memory) and a second value for one fake (the vault then tries its next counter); and every string
the vault uses, a value's or a fake's, by its keyed hash (``vault_strings``), so that a fake is never a value the vault
knows, whichever instance makes it. The local reviewer's answers and the decisions on images, sealed too.

What is sealed opens in its own row alone, and for its own purpose: a value or a fake for its entry, as that entry's
value or fake, and while the row says the type and the class it was found as (a class changed to STRUCT would leave the
value out of those the masker finds in clear); a review's answer for the text it answers; a decision for the image it
was made on. Someone who writes the database without the master key can move no sealed value to another row, nor
change the type or the class it was found as, without it ceasing to open (an entry's rule, counter and time are in
clear and bound to nothing; a rule names the field a value sat in by its kind alone, since a field's name can be a
value: ``modules.kept``). An entry that does not open is counted in ``unreadable`` and said in the log once, by its
id; its value, found again, is masked as it is found, in memory, and the entry left as it is.

What sealing cannot stop, he can still do: put back into its own row a sealed value he kept, which opens there again.
For a review's answer or a decision on an image, replaced in place, that restores an older verdict ("nothing to mask",
"send"), even after ``forget_reviews``: no count he cannot roll back binds them to a generation. Nor are the keyed
hashes beside a value and its fake (``original_key``, ``surrogate_key``) bound to anything: swapping two entries'
hashes, he makes two values of one identity share one fake (an instance that does not hold the value in memory then
finds the other's entry for it), as he can corrupt or roll back any row (the accepted limits S-14 and S-18): no value
in clear, nothing of another identity's, and a value a rule covers masked all the same. E4's export of what is kept and
E0c's least-privileged role for the database are the answer, as for the rest of what the database's writer can roll
back.

A change the other instances must hear of (a value kept, the rule of one, a value forgotten) bumps the identity's change
counter (``change_counters``) in its own transaction, and is told once committed, with that count: ``changed(kind,
entity_id, counter)``, kind ``entity``, ``rule`` or ``forgotten``. An instance that missed it finds it by the counter.

Who calls (``latet.state.VaultStore``): ``save_entity`` and ``save_rule`` the threads that mask, inside the vault's
lock. A new value costs six round trips to PostgreSQL (its transaction, the entry, its two strings, the count), the
last a commit that waits for PostgreSQL's disk, and its announcement; the space's other new values wait for it.
``load_review``, ``save_review`` and ``forget_entity`` are called on the event loop too, and there each waits for
PostgreSQL: accepted on a server for now. The pool bounds the wait for a connection (ten seconds) and for a new one
(``connect_timeout``); a PostgreSQL that stops answering on a connection already open holds the loop until the network
gives up."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import math
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from cryptography.exceptions import InvalidTag

from latet.engine import modules
from latet.engine.known import LET_GO
from latet.engine.types import EntityClass
from latet.engine.vault import Entity, EntryGone, FakeTaken, MaskingError
from latet.server.sealing import DERIVE, Sealer, derived

if TYPE_CHECKING:
    from psycopg import Connection

    from latet.server.db import Database

log = logging.getLogger(__name__)
_REVIEW_DAYS = 90  # a text not sent again for this long is reviewed again if it ever comes back
# Values one identity's vault may hold (ServerSettings.vault_limit): at 2.4 to 2.8 KiB a value of a few dozen
# characters (measured, the parts that match it compiled), a full vault is about 550 MiB, a quarter of the default
# memory budget; a hundred new values a working day reach it after eight years.
VAULT_LIMIT = 200_000
# The rows of a vault made Python's and opened between two lets-go of the GIL, as a vault is read: a few milliseconds.
ROWS_AT_ONCE = 1_000

_SELECT: Final = (
    "SELECT id, etype, eclass, original, surrogate, counter, created, rule FROM vault_entities WHERE identity_id = %s"
)
_INSERT: Final = (
    "INSERT INTO vault_entities (identity_id, id, etype, eclass, original, original_key, surrogate, surrogate_key,"
    " counter, created, rule) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"
)
# A fake's string: not kept when the vault knows the string already, as a value or as another fake.
_FAKE: Final = (
    "INSERT INTO vault_strings (identity_id, key, role, entity_id) VALUES (%s, %s, 'surrogate', %s)"
    " ON CONFLICT DO NOTHING"
)
# A value's string. When it is a fake given before, the fake's row stays the fake's and names this value's entity as
# well, so that the string stays known as a value once the fake's entity is forgotten (``_STAYS_A_VALUE``).
_VALUE: Final = (
    "INSERT INTO vault_strings (identity_id, key, role, entity_id) VALUES (%s, %s, 'original', %s)"
    " ON CONFLICT (identity_id, key) DO UPDATE SET also = EXCLUDED.entity_id"
)
_STAYS_A_VALUE: Final = (
    "UPDATE vault_strings SET role = 'original', entity_id = also, also = NULL"
    " WHERE identity_id = %s AND entity_id = %s AND also IS NOT NULL"
)
# An entry's fake given anew (``replace_fake``): the entry's row, while it holds the fake it is replacing.
_REFAKE: Final = (
    "UPDATE vault_entities SET surrogate = %s, surrogate_key = %s, counter = %s"
    " WHERE identity_id = %s AND id = %s AND surrogate_key = %s"
)
# The old fake's string: a value's as well, it stays that value's (as ``_STAYS_A_VALUE`` does for an entry forgotten);
# else it goes.
_OLD_FAKE_A_VALUE: Final = (
    "UPDATE vault_strings SET role = 'original', entity_id = also, also = NULL"
    " WHERE identity_id = %s AND key = %s AND entity_id = %s AND also IS NOT NULL"
)
_OLD_FAKE: Final = "DELETE FROM vault_strings WHERE identity_id = %s AND key = %s AND entity_id = %s"
# The count of the identity's announced changes, bumped last in a change's transaction: its row stays locked until the
# change commits, so the counts follow the order the changes are kept in, and the change waits for nothing after it.
_COUNT: Final = (
    "INSERT INTO change_counters (owner, counter) VALUES (%s, 1)"
    " ON CONFLICT (owner) DO UPDATE SET counter = change_counters.counter + 1 RETURNING counter"
)


class VaultFull(MaskingError):
    """The identity's vault holds as many values as the server allows: the request is stopped, never sent unmasked."""


class UnreadableEntry(Exception):
    """An entry of the vault is there and does not open: sealed with another root key, or changed in the database. Its
    words name the entry by its id, never a value."""


class _Changed(Exception):
    """An entry whose fake was to be given anew holds another one, or is gone: another instance changed it first. Its
    transaction is rolled back."""


class PgVaultStore:
    def __init__(
        self,
        db: Database,
        identity_id: str,
        root_key: bytes,
        *,
        changed: Callable[[str, str, int], None] | None = None,
        limit: int = VAULT_LIMIT,
    ) -> None:
        self.db, self.identity = db, identity_id
        self._sealer = Sealer(root_key, b"storage")
        self._images = derived(root_key, DERIVE, b"image-decisions")  # what names a decision's row
        self._changed = changed or (lambda _kind, _entity, _counter: None)
        self._unreadable: set[str] = set()  # the entries met that do not open, by id
        self.unreadable = 0  # how many: `latet doctor` says so
        self.limit = limit
        self.count = 0  # the vault's values: all of them when loaded, then those kept here (the space keeps it true)

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
            found = Entity(entity_id, etype, EntityClass(eclass), original, surrogate, row[5], row[6], rule)
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
        with self.db.connection() as conn:
            row = conn.execute(_SELECT + " AND id = %s", (self.identity, entity_id)).fetchone()
        return self._opened(row) if row is not None else None

    def save_entity(self, e: Entity) -> Entity | None:
        """Keep ``e``: None when kept as given; the entity of the same value when another instance kept it first
        (whatever its type: the value alone is the key). Raises FakeTaken when ``e``'s fake is a string the vault
        knows, a value's or another fake's, and VaultFull when the vault holds as many values as the server allows, or
        more.
        When an entry of the same value is there and does not open, None too: ``e`` is masked as given, in the vault's
        memory, and kept nowhere (the entry stays as it is, counted in ``unreadable``)."""
        if self.count >= self.limit:  # more than the limit too: lowered since, or passed by instances at once
            raise VaultFull(
                f"this identity's vault holds {self.count} values, and this server allows {self.limit} at most "
                "(vault_limit): an administrator raises it, or values are forgotten in the dashboard"
            )
        original_key = self._sealer.keyed(f"original\x00{e.original}")
        row = (
            self.identity, e.id, e.etype, int(e.eclass),
            self._sealer.seal(e.original.encode(), self._aad(e.id, "original", e.etype, e.eclass)), original_key,
            self._sealer.seal(e.surrogate.encode(), self._aad(e.id, "surrogate", e.etype, e.eclass)),
            self._sealer.keyed(f"surrogate\x00{e.surrogate}"), e.counter, e.created, modules.kept(e.rule),
        )  # fmt: skip
        counter, kept = 0, None
        with self.db.transaction() as conn:
            done = conn.execute(_INSERT, row).rowcount
            if done:
                if not conn.execute(_FAKE, (self.identity, self._sealer.keyed(e.surrogate), e.id)).rowcount:
                    # The fake is a string this vault knows, a value or another fake: nothing is kept.
                    raise FakeTaken(f"the fake made for a {e.etype} is a value already known, or another value's fake")
                # A value equal to a fake given before is real: kept and masked under a fake of its own.
                conn.execute(_VALUE, (self.identity, self._sealer.keyed(e.original), e.id))
                counter = self._count(conn)
            else:  # the same value kept first (by its id, or under another type), or the fake taken
                kept = conn.execute(
                    _SELECT + " AND (id = %s OR original_key = %s) ORDER BY seq LIMIT 1",
                    (self.identity, e.id, original_key),
                ).fetchone()
        if done:
            self.count += 1
            self._changed("entity", e.id, counter)
            return None
        if kept is None:
            raise FakeTaken(f"the fake made for a {e.etype} is another value's")
        try:
            return self._opened(kept)
        except UnreadableEntry:
            return None  # masked as given: an entry that does not open is not "the fake is taken"

    def save_rule(self, e: Entity) -> None:
        """The rule of a value kept before rules were: kept when its entry has none yet, and told."""
        if e.rule is None:
            return
        with self.db.transaction() as conn:
            done = conn.execute(
                "UPDATE vault_entities SET rule = %s WHERE identity_id = %s AND id = %s AND rule IS NULL",
                (modules.kept(e.rule), self.identity, e.id),
            ).rowcount
            counter = self._count(conn) if done else 0
        if done:
            self._changed("rule", e.id, counter)

    def replace_fake(self, old: Entity, new: Entity) -> Entity | None:
        """Keep ``new``'s fake in place of ``old``'s, in their value's entry (one whose fake showed its value, drawn
        again at load): sealed for the entry as every fake is, the new fake's string known in place of the old one's (a
        string that is another entry's value as well stays, as that value's), counted and told as a value kept is, so
        that the other instances learn it. None when kept so; the entry as another instance changed it first. Raises
        FakeTaken when the new fake is a string the vault knows, a value's or a fake's; EntryGone when the entry is
        gone, or no longer opens (written by hand, moved from another key): the vault then knows the value no more."""
        sealed = self._sealer.seal(new.surrogate.encode(), self._aad(old.id, "surrogate", old.etype, old.eclass))
        row = (
            sealed, self._sealer.keyed(f"surrogate\x00{new.surrogate}"), new.counter,
            self.identity, old.id, self._sealer.keyed(f"surrogate\x00{old.surrogate}"),
        )  # fmt: skip
        try:
            with self.db.transaction() as conn:
                if not conn.execute(_FAKE, (self.identity, self._sealer.keyed(new.surrogate), old.id)).rowcount:
                    raise FakeTaken(
                        f"the fake made for a {new.etype} is a value already known, or another value's fake"
                    )
                if not conn.execute(_REFAKE, row).rowcount:
                    raise _Changed
                was = self._sealer.keyed(old.surrogate)
                conn.execute(_OLD_FAKE_A_VALUE, (self.identity, was, old.id))
                conn.execute(_OLD_FAKE, (self.identity, was, old.id))
                counter = self._count(conn)
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
        """Forget a value, its fake and its strings, and tell it. A string of it that is another entry's value as well
        (that value was equal to its fake) stays, as that value's."""
        with self.db.transaction() as conn:
            conn.execute(_STAYS_A_VALUE, (self.identity, entity_id))
            conn.execute(
                "UPDATE vault_strings SET also = NULL WHERE identity_id = %s AND also = %s", (self.identity, entity_id)
            )
            conn.execute(
                "DELETE FROM vault_strings WHERE identity_id = %s AND entity_id = %s", (self.identity, entity_id)
            )
            done = conn.execute(
                "DELETE FROM vault_entities WHERE identity_id = %s AND id = %s", (self.identity, entity_id)
            ).rowcount
            counter = self._count(conn)
        self.count = max(0, self.count - done)
        self._changed("forgotten", entity_id, counter)

    def _count(self, conn: Connection[Any]) -> int:
        """Bump the identity's change counter in ``conn``'s transaction: the count the change is told with."""
        row = conn.execute(_COUNT, (self.identity,)).fetchone()
        assert row is not None  # RETURNING gives the row it counted
        return int(row[0])

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
        sealed = self._sealer.seal(json.dumps(answer, ensure_ascii=False).encode(), self._review_aad(key))
        now = time.time()
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO reviews (identity_id, key, data, ts) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (identity_id, key) DO UPDATE SET data = EXCLUDED.data, ts = EXCLUDED.ts",
                (self.identity, key, sealed, now),
            )
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
        """Nothing: the pool is the server's."""


def vault_usage(db: Database, limit: int, *, share: float = 0.9) -> list[tuple[str, int]]:
    """The identities whose vault holds ``share`` of ``limit`` or more, with their counts, the fullest first (at most
    a hundred): for the administration's status and E0c's metrics."""
    fewest = math.ceil(round(limit * share, 6))  # rounded first: in binary, 0.7 of 1,290 is 902.9999999999999
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT identity_id, COUNT(*) FROM vault_entities GROUP BY identity_id HAVING COUNT(*) >= %s"
            " ORDER BY COUNT(*) DESC, identity_id LIMIT 100",
            (fewest,),
        ).fetchall()
    return [(str(row[0]), int(row[1])) for row in rows]
