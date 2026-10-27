"""What a Latet keeps, behind protocols: the vault and the history, the conversations and the blocks they recorded,
their texts as they left, what waits for a person, the live feeds, the dashboard's sign-ins, the settings, the AWS
sessions and the locks of jobs.

On a computer they are this process's memory, SQLite and files (``latet.state.local``); on a Latet server PostgreSQL
and Valkey (``latet.server``), so that any instance serves any request. The engine, the gateway, the bridge and the
dashboard reach what they keep through these and nothing else.

The methods are plain calls, not coroutines, and each protocol says which thread makes them today. The event loop
serves every stream of the daemon between two of its own steps: a method called on it must answer without waiting on
a network (from this process's memory, kept current some other way), or its caller must first be moved to a thread.
The worker threads (the engine's, the plugin's, the recorder's, the dashboard's) may wait."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, MutableMapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    import asyncio

    from latet.config import Config
    from latet.engine.pipeline import Session
    from latet.engine.types import Replacement
    from latet.engine.vault import Entity
    from latet.gateway.approvals import Batch, Change, Decision
    from latet.gateway.sigv4 import AwsCredentials
    from latet.storage.exchanges import ExchangeRecord


@dataclass(frozen=True)
class IdentityRef:
    """Whose space a runtime is, on a Latet server: a person or an API key. None on a computer."""

    id: str  # "u_" and 10 lowercase base32 characters
    name: str
    kind: Literal["person", "key"]


class VaultStore(Protocol):
    """The values masked and their fakes, the local reviewer's answers and your decisions on images, sealed with the
    root key (``storage.db.Store`` on a computer).

    Who calls: ``save_entity`` and ``save_rule`` the thread that masks (the engine's, the plugin's, a worker of the
    dashboard), inside the vault's lock, so one new value is kept at a time and every other new value waits for it;
    the image decisions those threads too. ``load_review`` and ``save_review`` both those threads and the event loop
    (the local reviewer runs on it), and ``forget_entity`` the dashboard's work threads (its Vault page) and the start:
    on the loop the first two must not wait on a network. ``load_entities`` and ``unreadable`` the start,
    ``forget_reviews`` a worker thread, ``load_entity`` nobody yet (an instance of a server will, to learn a value
    another one kept)."""

    unreadable: int  # values sealed with another root key, skipped (`latet doctor` says so)

    def load_entities(self) -> list[Entity]: ...

    def load_entity(self, entity_id: str) -> Entity | None:
        """One value by its id (one another process kept, say); None when there is none. An entry that is there and
        cannot be opened is not the same as none: a server's store raises ``UnreadableEntry`` for it (its callers leave
        it out, and mask its value as it is found)."""
        ...

    def save_entity(self, e: Entity) -> Entity | None:
        """Keep ``e``. None: kept as given. An entity: the same value kept first by another process, which the vault
        uses in its place. Raises ``FakeTaken`` when ``e``'s fake is another value's (a server's store, also when a fake
        in escapes reads as a string the vault knows, ``vault.fake_reading``): the vault tries its next one."""
        ...

    def save_rule(self, e: Entity) -> None: ...

    def replace_fake(self, old: Entity, new: Entity) -> Entity | None:
        """Keep ``new``'s fake in place of ``old``'s, in the entry of their value (one whose fake shows its value, drawn
        again at load: ``Vault``'s ``on_replace``), sealed as every fake is. None: kept so. An entity: the entry as
        another process changed it first, which the vault uses in its place. Raises ``FakeTaken`` when ``new``'s fake is
        another value's, or a string the vault knows: the vault tries its next one; ``EntryGone`` when the entry is gone
        (another process forgot it meanwhile): the vault forgets it too."""
        ...

    def forget_entity(self, entity_id: str) -> None: ...

    def forget_entities(self, entity_ids: Iterable[str]) -> None:
        """``forget_entity`` for many values at once (the names today's filters reject, at the start): a computer's
        vault does them, and its upgrade copies, in one pass."""
        ...

    def load_review(self, key: bytes) -> dict[str, Any] | None:
        """What the local reviewer answered for a text it read before, by a keyed hash of that text:
        ``{"findings": [[value, category], ...], "block": bool}``, with ``"unlocated"`` (as ``findings``) for values it
        saw in an image and Latet could not place. The values are in clear: kept sealed. None when there is none, or it
        cannot be opened. An implementation may forget an answer nobody asked for in a long while (a computer: 90
        days): the text is then reviewed again."""
        ...

    def save_review(self, key: bytes, answer: dict[str, Any]) -> None:
        """Keep that answer, sealed, in place of an earlier one for the same text."""
        ...

    def forget_reviews(self) -> None:
        """Every answer of the local reviewer: what it read is read again."""
        ...

    def load_image_decision(self, key: str) -> dict[str, Any] | None:
        """What you decided on an image in the Live Reviewer, by the image's own key (a hash of its bytes, which the
        store keys again): ``{"rects": [[x0, y0, x1, y1], ...], "withhold": bool}``, with ``"reason"`` when the local
        reviewer had it withheld. None when you decided nothing on it, or it cannot be opened. Kept, sealed, for as long
        as the vault is: the same image sent again is painted or withheld the same way."""
        ...

    def save_image_decision(self, key: str, decision: dict[str, Any]) -> None:
        """Keep that decision, sealed, in place of an earlier one on the same image."""
        ...

    def close(self) -> None: ...


# Which of an entity's two strings a word is of: its value's or its fake's.
Role = Literal["original", "surrogate"]


@runtime_checkable
class KeyVaultStore(VaultStore, Protocol):
    """An API key's vault, read from the store as it is needed rather than whole (a server's ``PgVaultStore``): how
    many values it holds, its newest ones, those whose value or fake has no anchor word (``engine.anchors``), one value
    by itself or by its fake, and the values and fakes a text may hold, by their anchor words. The store keeps each word
    as a keyed hash alone, and every lookup is the identity's own.

    An entry that does not open (sealed with another root key, changed in the database) is left out of what these
    answer, and counted in ``unreadable``, as ``load_entities`` leaves it out.

    Who calls: the thread that builds the space (``backfill_words``, ``count_entities``, ``load_recent``,
    ``load_unanchored``), the threads that mask and unmask (the lookups) and the one that catches the space up
    (``by_ids``, ``count_entities``), which may wait; never the event loop (``engine.stored.StoredVault`` answers
    there from its memory alone)."""

    # The values of the vault, as ``save_entity`` holds them to its limit (``VaultFull``): a space that does not load
    # the vault whole sets it from ``count_entities``, and the space keeps it true as values arrive and go.
    count: int
    # The generation the vault draws its new fakes in (``engine.vault.Vault._seed``), as the store last read it: moved
    # by each forget of values by themselves (Task 11b). ``save_entity`` refuses a value drawn in an older one
    # (``server.vault.OlderGeneration``, a ``FakeTaken``), reading the new one, so that the vault draws it again.
    generation: int

    def load_generation(self) -> int:
        """The generation as the store has it now, kept as ``generation`` and answered."""
        ...

    def count_entities(self) -> int:
        """How many values the vault holds, as the store counts them now."""
        ...

    def load_recent(self, n: int) -> list[Entity]:
        """The ``n`` values kept last, the newest first."""
        ...

    def load_unanchored(self) -> list[Entity]:
        """The values whose value or fake has no anchor word, which no text's words find: the oldest first."""
        ...

    def load_held(self, n: int) -> list[Entity]:
        """The ``n`` newest secrets that no text's words find glued to letters or digits (``stored.held_glued``), the
        newest first: those a space holds in memory for good (``key_held_secrets``, Ruling 10b-R3)."""
        ...

    def by_original(self, value: str) -> Entity | None:
        """The entity of ``value``, written as it was kept; None when there is none."""
        ...

    def by_surrogate(self, fake: str) -> Entity | None:
        """The entity whose fake is ``fake``, written as it was given; None when there is none."""
        ...

    def by_reading(self, reading: str) -> Entity | None:
        """The entity whose fake, written in escapes, reads as ``reading`` (``vault.fake_reading``), found by that
        string alone, never by opening the fakes that share a word with it; None when there is none."""
        ...

    def with_words(self, role: Role | tuple[Role, ...], words: Iterable[str]) -> list[Entity]:
        """The entities whose value (``original``) or fake (``surrogate``), or either (both roles, in one statement),
        has one of ``words`` among its words (``stored.words_of``: its anchor or a fake's dotted word, and its edges; a
        text's words are ``anchors.words_in``, with ``dotted``), each once, however many of its words are asked. A
        store may refuse a call that would open more entities than it allows (a ``MaskingError`` whose words name no
        value): never answered with a part of them."""
        ...

    def by_ids(self, ids: Iterable[str]) -> list[Entity]:
        """The entities of ``ids`` that the vault holds, each once (one that does not open left out): what a space
        holds in memory, asked whether the vault still has it, never the whole vault."""
        ...

    def backfill_words(self) -> int:
        """Make the anchor words that are not there or not as ``anchors.anchor`` makes them now, a part of the vault at
        a time: those of the values kept before the store kept any (a vault of an earlier Latet), those made by an
        older version of the anchors (``anchors.ANCHORS_VERSION``), and the NULL ones (an entry that did not open when
        its words were made); how many values got a word. Safe to call again, and on two instances at once: a word of
        the current version is left as it is, and a NULL one with no word to give stays NULL."""
        ...


class Together(Protocol):
    """The values one thread keeps in a key's vault (``save_entity``) from when a store's ``together()`` gave it
    until ``commit`` or ``rollback``, kept together (a server's ``PgVaultStore``: in one transaction): none of them
    kept, nor told to the other instances, before it commits; that thread's own lookups find them meanwhile, no
    other's. A store that keeps them so has ``together()`` beside ``KeyVaultStore``'s methods
    (``engine.stored.StoredVault`` asks for it in an applications call, Ruling T10i); one that does not, or that has
    no room for another call keeping its values so (Ruling 10b-R2), keeps each at once."""

    # The store had no room for one more call keeping its values together (Ruling 10b-R2): each is kept at once, and
    # none is the call's to undo if it fails (review 10b's N4).
    at_once: bool

    def commit(self) -> None:
        """Keep every change, and tell each once kept. Raises when they are not kept (none is)."""
        ...

    def rollback(self) -> None:
        """Keep none of them. Never raises: a store that cannot answer keeps none either."""
        ...


class HistoryStore(Protocol):
    """The dashboard's record of what left: exchanges, the blocks they carried, the images as sent, the local
    reviewer's records (``storage.exchanges.ExchangeStore`` on a computer). Its public methods are today's. It prunes
    nothing when it opens: whoever owns it calls ``prune()`` once the retention is known (a store that pruned at
    connection with a default retention would drop what a longer retention keeps).

    What its methods answer is said below; ``ExchangeStore`` is the reference for every key (the recorder writes
    them, the dashboard reads them), and what it keeps is masked text only, sealed.

    Who calls: never the event loop. The recorder's thread (``record``, ``known_blocks``, ``block_types``,
    ``block_id``, ``image_id``, and ``prune`` under the job lock), the threads that mask (``record_review``,
    ``block_id``), worker threads of the dashboard (every read, ``clear``) and the start (``prune``);
    ``retention_days`` is assigned under the runtime's lock when the setting changes."""

    retention_days: int

    def block_id(self, masked: str) -> str:
        """A block's id: a keyed hash of its masked text, the same in every process of one Latet."""
        ...

    def image_id(self, data: str) -> str:
        """An image's id, ``im_`` and a keyed hash of its base64 as it was sent."""
        ...

    def known_blocks(self, ids: Iterable[str]) -> set[str]:
        """Those of ``ids`` whose block is stored already."""
        ...

    def last_used(self) -> dict[str, float]:
        """When each route (a summary's ``upstream``) last carried an exchange."""
        ...

    def block_types(self, ids: Iterable[str]) -> dict[str, list[tuple[str, str, str, int]]]:
        """The values masked in stored blocks, by block id: ``(type, class, module, count)``, sorted."""
        ...

    def record(self, rec: ExchangeRecord) -> str:
        """Keep an exchange whole or not at all, and answer its id: ``rec.summary["id"]``, or a new one put there
        (``ex_`` and hexadecimal digits). A block whose ``masked`` is None is stored already, and every block it
        carries counts as seen now."""
        ...

    def record_review(self, summary: dict[str, Any]) -> str:
        """Keep what the local reviewer said of one block (categories and places, never a value), and answer its id
        (``rv_`` and hexadecimal digits)."""
        ...

    def reviews(self, limit: int = 100, session: str | None = None) -> list[dict[str, Any]]:
        """The reviews recorded, the newest first: each summary as recorded, with its ``id``."""
        ...

    def prune(self, now: float | None = None) -> int:
        """Drop the exchanges and the reviews older than ``retention_days``, and the blocks and images no exchange
        carried since; how many exchanges went."""
        ...

    def clear(self) -> None: ...

    def in_order(self, session: str) -> list[str]:
        """The ids of one conversation's exchanges, in the order they were recorded (not by their time)."""
        ...

    def detail(self, exchange_id: str) -> dict[str, Any] | None:
        """One exchange: ``{"summary", "request": {"headers", "slots", "images", "body_bytes"}, "response", "leaks"}``.
        A slot is ``{"block", "path", "kind", "from_model", "new", "masked", "replacements", "tool", "source"}``
        (``tool``: the tool whose output a tool result is, ``source``: the file or the web address it came from, None
        when not known; a block pruned since is left out); an image
        ``{"id", "path", "state", "block", "kept"}``. None for an id it does not have."""
        ...

    def image(self, image_id: str) -> tuple[str, bytes] | None:
        """An image as it was sent: its media type and its bytes."""
        ...

    def sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """The conversations, the latest first: ``{"key", "agent", "first_ts", "last_ts", "exchanges", "masked",
        "blocked"}``."""
        ...

    def summaries_since(self, ts: float) -> list[dict[str, Any]]:
        """The summaries of the exchanges from ``ts`` on, the oldest first."""
        ...

    def summaries_parts(self, ts: float, most: int) -> Iterator[list[dict[str, Any]]]:
        """The summaries of the exchanges from ``ts`` on, the latest ``most`` at most, the newest first, a part at a
        time (the dashboard's stats, which aggregate each part before the next is read): never the whole history at
        once, which on a server is up to some millions of exchanges in the memory every identity shares."""
        ...

    def close(self) -> None: ...

    # Last: a method named `list` hides the builtin in the annotations written after it.
    def list(
        self,
        limit: int = 100,
        before: str | None = None,
        session: str | None = None,
        agent: str | None = None,
        flag: str | None = None,
        project: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """A page of summaries as the recorder wrote them, the newest first, and the cursor of the next page: the id
        of this page's last exchange, None when it is the last page. ``before``: a cursor; the others filter (a
        ``flag`` also matches its ``flag:detail`` forms)."""
        ...


class Conversations(Protocol):
    """What the engine knows of each conversation (the entities it was shown, its project, the texts approved in it),
    what the model wrote by its restored form, and the conversation of each response id.

    It is one Latet's: a computer's, or one identity's space on a server, where a conversation's key, a response id
    or a text of the model of one identity answers nothing to another.

    What it may forget: a computer keeps the last 10,000 conversations, 50,000 texts of the model and 50,000 response
    ids, and a server as many for each identity, which it also lets go by age and by what they take (an entity past a
    conversation's own ceiling is restored only where the conversation is held). A conversation forgotten starts again:
    the fakes it was shown are no longer restored in what the model writes next, and the texts approved in it wait once
    more.

    Who calls: the threads that mask (the engine's and the plugin's) ``session``, ``update``, ``named`` and
    ``recall``; the event loop ``name`` and ``remember`` while a response is restored (the engine's thread, for an
    answer, an event or a message too large to restore on the loop: ``gateway.mediate.RESTORED_ON_LOOP``),
    ``sessions`` for the dashboard, and at times ``session`` and ``update`` too (a WebSocket opening, a decision that
    carries your masks, a plugin's route check). On the loop none may wait on a network."""

    def session(self, key: str) -> Session:
        """The conversation, made at its first use. The same object each time it is asked in one process: the engine,
        the recorder and a WebSocket's rewriter read the entities of the one they hold from one request to the next
        (``Engine.restores``), so what another process adds must reach that object, not a copy handed out later."""
        ...

    def update(self, session: Session, shown: Iterable[str] = ()) -> None:
        """After its entities, its project or its approved texts changed: kept for every process that serves it.
        Entities and approved texts only add up (two processes adding to one conversation keep both). An
        implementation may forget the oldest approved texts of a conversation (the session it is given holds at most
        2,000, ``gateway.mediate.APPROVED_KEPT``): one forgotten only waits for you again. ``shown``: the entities the
        call just added to it (``Engine.show``), which an implementation that does not hold the conversation writes
        alone."""
        ...

    def sessions(self) -> list[Session]:
        """The conversations this space knows (the dashboard's vault page counts them)."""
        ...

    def remember(self, key: bytes, masked: str) -> None:
        """What the model wrote, by a keyed hash of its restored form. It never waits on a network: another process may
        learn it a moment later (a server writes it off the caller's thread), and until then masks anew the text sent
        back, the vault giving a known value the same fake."""
        ...

    def recall(self, key: bytes) -> str | None:
        """What the model wrote for that key; None when it is not kept (any more)."""
        ...

    def name(self, response_id: str, key: str) -> None:
        """The conversation a response belongs to (the Responses API's ``previous_response_id``). It never waits on a
        network: another process may learn it a moment later (a server writes it off the caller's thread)."""
        ...

    def named(self, response_id: str) -> str | None:
        """That conversation's key; None when the response is not known (any more)."""
        ...


class SentBlocks(Protocol):
    """Which blocks each conversation recorded already (a block's first time counts as new). A computer keeps the
    last 200,000 pairs; one forgotten shows its block as new once more in the dashboard, nothing else.

    Who calls: the recorder's thread, which may wait."""

    def first(self, session: str, block: str) -> bool:
        """True the first time ``block`` is recorded in ``session``, which then counts as recorded."""
        ...


@dataclass(frozen=True)
class Left:
    """A text as it left: masked, what was replaced in it, and whether the offsets fit the text (a ``Masked``'s). The
    replacements hold the values (but as a server keeps them: see ``Given``), and a text kept before the vault learnt a
    value holds that value in clear: no ``repr`` shows either (a log line, an error, a failed assertion)."""

    text: str = field(repr=False)
    replacements: tuple[Replacement, ...] = field(repr=False)
    exact: bool


class Given(Protocol):
    """What a conversation's texts were when they left, and what the final check saw leave, for every process that
    serves the conversation (on a server in Valkey, sealed with the identity's key, the replacements without their
    values: below). A text sent again leaves as it left, whatever the vault learnt since; a block that left once never
    blocks the conversation again.

    A text is kept as it is masked, before its request leaves (it may never leave: you decline it, its client gives
    up), and kept again when it is masked again (with what the local reviewer or you found in it, or what a later text
    of its request taught the vault). So before the mediator sends a text as it was kept, it asks whether the text
    shows in clear no value the vault knows, or the final check saw that very text leave in its conversation: when the
    text is masked, and again once every text of its request is, if a later one taught the vault a value
    (``Mediator.mask_value``, ``Mediator.settle``). Otherwise the text is masked anew.

    What it may forget: a computer keeps the last 50,000 texts and 200,000 blocks, a server as many for each identity,
    which it also lets go by age and by the bytes the texts take, and no text larger than a limit. A text forgotten is
    masked anew with what the vault knows; a block forgotten counts as new once more. What it may leave out: a
    replacement's value (a server keeps none: a text read back holds, in its place, nothing where the replacement has an
    entity, whose id is there, and a keyed hash of the value where it has none, which tells values apart as the dump
    guard counts them).

    Who calls: the threads that mask and check (the engine's and the plugin's: every ``Mediator.mask_value`` and
    ``Verifier.verify`` runs in one of them), which may wait."""

    def masked(self, key: bytes) -> Left | None:
        """How the text of ``key`` (the mediator's: conversation, rules, kind, text) was kept last; None: never kept,
        or forgotten."""
        ...

    def keep(self, key: bytes, left: Left) -> None:
        """How the text of ``key`` is masked to leave from now on, in place of what was kept for it before."""
        ...

    def checked(self, session: str, key: bytes) -> bool:
        """Whether the final check saw the block of ``key`` leave in ``session``."""
        ...

    def keep_checked(self, session: str, keys: Iterable[bytes]) -> None:
        """The blocks of ``keys`` left in ``session``: the final check let their request out."""
        ...


class Feed(Protocol):
    """A dashboard's live feed: what one process publishes, every viewer hears (``recorder.Broadcaster`` here).

    Who calls: the event loop alone (a thread hands ``publish`` over to it), but for the Live Reviewer's "pending"
    event, which its queue's ``store`` publishes where it keeps the batch (``ApprovalsBackend.store``): a server's
    queue does it in its own thread, whose feed (``server.live.ValkeyFeed``) sends to Valkey and touches no viewer's
    queue. ``publish`` returns at once whatever a viewer does: one that stopped reading misses events (a computer's
    holds 500), and neither the others nor the one that publishes wait for it."""

    def subscribe(self) -> asyncio.Queue[Any]:
        """A queue of what is published from now on, for one viewer on the running loop."""
        ...

    def unsubscribe(self, queue: asyncio.Queue[Any]) -> None: ...

    def publish(self, payload: Any, event: str = "exchange") -> None:
        """An exchange's summary, or another ``event`` (``(event, payload)`` on the queues)."""
        ...


@dataclass(frozen=True, slots=True)
class Timing:
    """What the Live Reviewer's reaper reads of a batch waiting (``ApprovalsBackend.timings``), and nothing else of it,
    never its texts: when it was made, its time limit, whether the plugin asks for it, how many ask now in every
    process, and when one last did (``seen``, on this process's monotonic clock)."""

    id: str
    created: float
    timeout_s: int | None
    polled: bool
    waiting: int
    seen: float

    @classmethod
    def of(cls, batch: Batch) -> Timing:
        """A batch's timing, as this process holds it."""
        return cls(batch.id, batch.created, batch.timeout_s, batch.polled, batch.waiting, batch.seen)


class ApprovalsBackend(Protocol):
    """What waits for a person, as every process that serves them sees it: the Live Reviewer's batches and the
    decisions on them, the answers the plugin asks for, the changes to the settings and how they ended, and how many
    dashboards watch (``gateway.approvals.Approvals`` keeps its queue here).

    Who calls: every method the event loop of the process, and that loop alone. ``store`` makes the batch's future on
    it, ``settle``, ``settle_all`` and ``give`` wake what waits on it, ``store``, ``listed``, ``fetch``,
    ``fetch_image``, ``settle_all``, ``decision`` and ``answer`` are awaited on it (a dashboard's decision reads its
    batch, and its view of an image that image), and ``polled``, ``get``, ``waiting``, ``timings`` (the reaper's, every
    few seconds), ``image`` and ``viewers`` are asked between two of its steps. So only those awaited may wait, and none
    of the others on a network. ``changes`` and ``settled`` are read and written by worker threads, one at a time under
    the runtime's lock, and read on the loop (a command that waits asks whether its change still waits, then how it
    ended)."""

    changes: MutableMapping[str, Change]  # the change waiting for you, by id (at most one)
    # How the latest changes ended, by id. The backend keeps the 100 written last and forgets the oldest by itself:
    # whoever writes one neither counts them nor relies on the order they iterate in.
    settled: MutableMapping[str, str]

    def open(self, change: Change) -> tuple[Change, list[str]]:
        """Atomically keep equivalent config/base or supersede all waiting changes and insert this one. Return the
        waiting change and superseded ids after commit. Return the supplied object only when inserted; a kept change
        is returned as another object, allowing the caller to announce only its own insert. Worker threads only,
        under the runtime's lock on a computer and the identity's database lock on a server."""
        ...

    def end(self, change_id: str, state: str) -> bool:
        """End a still-waiting change once, keeping the first terminal state. True only for the winning write.
        Missing/already-ended changes are untouched. Called under the runtime's lock, in worker threads."""
        ...

    async def store(self, batch: Batch, announce: Callable[[], None] | None = None) -> None:
        """``batch`` waits, listed for every dashboard and decided from any, its future and images kept here; then
        ``announce``, once the batch can be read by every process, to tell the dashboards. A backend that bounds what it
        keeps refuses a batch past its bounds (a server's ``BatchRefused``), nothing kept and nothing announced then.
        Awaited by the Live Reviewer's requests (``Approvals.open``): where keeping a batch costs the loop (a server
        makes its records, seals them and writes them to Valkey), that work runs in a thread, ``announce`` too, and the
        loop serves the other requests meanwhile."""
        ...

    async def replace(self, batch_id: str, items: list[dict[str, Any]]) -> Batch | None:
        """The previews of a batch this process opened and that still waits, made again (the rules or the vault changed
        while it waited, ``Mediator.refresh_live``): stored for every process to read, the batch as old as it was; the
        batch, or None when it no longer waits here (decided, being decided, or held by another process), nothing
        changed then. A backend that bounds what it keeps refuses previews past its bounds the same way."""
        ...

    def get(self, batch_id: str) -> Batch | None:
        """A batch still waiting: its summary and what deciding needs, as any process sees it."""
        ...

    async def fetch(self, batch_id: str) -> Batch | None:
        """``get`` for a caller on the event loop that awaits it (a dashboard's decision on the batch): where reading a
        batch costs the loop (a server opens its record, its texts with it), it is read in a thread."""
        ...

    def waiting(self) -> list[Batch]:
        """Every batch waiting, oldest first."""
        ...

    async def listed(self) -> list[Batch]:
        """``waiting`` for a caller on the event loop that awaits it (what a dashboard asks of the whole queue): where
        reading every batch costs the loop (a server opens each one's record, its texts with it), it is read in a
        thread."""
        ...

    def timings(self) -> list[Timing]:
        """What the reaper reads, every few seconds, of each batch this process holds (opened here, waiting): its
        ``Timing``, its polls and when it was last seen as every process counts them, and never its texts. A server
        reads it from its memory and the polls' small keys, no record: a batch another instance holds is that
        instance's to give up, and leaves the queue within ``HELD_MS`` once that instance stops."""
        ...

    def settle(self, batch_id: str, decision: Decision) -> bool:
        """The decision on a batch, the first one only: it leaves the queue and wakes whoever waits for it, in any
        process. False when it was not waiting."""
        ...

    async def settle_all(self, decisions: list[tuple[str, Decision]], told: Callable[[str, Decision], None]) -> int:
        """``settle`` for each of ``decisions`` (a dashboard's "approve all"), ``told`` called with each batch settled
        (the dashboards' "settled"); how many were. Where settling costs the loop (a server's round trips, fifty
        batches' at most), it runs in a thread, ``told`` too, and the waits of this process are woken on the loop."""
        ...

    async def decision(self, batch: Batch, hold: float | None) -> Decision | None:
        """The decision on ``batch`` once taken, here or in another process; None when ``hold`` seconds pass. Where no
        decision can come, a server's answers one not approved (``state`` ``abandoned``): the batch left its queue with
        none, what is in its decision's place does not open, or its space closed in this process."""
        ...

    def polled(self, batch: Batch, delta: int) -> None:
        """A poll on ``batch`` begins (1) or ends (-1): a batch the plugin asks about is not given up."""
        ...

    def image(self, batch_id: str, item: int, sent: bool) -> tuple[str, str] | None:
        """An image of a waiting batch, base64 and media type: as it came, or as it would leave (``sent``)."""
        ...

    async def fetch_image(self, batch_id: str, item: int, sent: bool) -> tuple[str, str] | None:
        """``image`` for a caller on the event loop that awaits it (a dashboard's view of the image): where reading an
        image costs the loop (a server opens its record, the image as it came and as it would leave), it is read in a
        thread."""
        ...

    def give(self, batch_id: str, answer: dict[str, Any]) -> None:
        """What the plugin gets for a batch decided, for whichever process it asks. Raises ``ValueError``, and keeps
        nothing, while the batch still waits: the answer is what leaves, and only your decision lets it. Where no call
        waits for an answer with this id (never stored, not one the plugin asks about, or its answer handed over or
        given up and forgotten already) it does nothing: no error, and nothing kept for a later poll."""
        ...

    async def answer(self, batch_id: str, hold: float) -> dict[str, Any] | None:
        """That answer once given; None when ``hold`` seconds pass first (asking keeps the batch alive). Raises
        ``KeyError`` when nothing waits with this id. The answer is handed over once: a later call for the same batch
        raises ``KeyError`` too."""
        ...

    def viewers(self, delta: int = 0) -> int:
        """How many dashboards watch, after adding ``delta``."""
        ...


class SignIns(Protocol):
    """The dashboard's one-time codes and sessions, each for a subject: an identity's id on a server, "" on a computer
    (you). An implementation keeps a code and a session id only as their hash (what is stolen from the store opens no
    session).

    Who calls: the event loop. ``subject_of`` at every request of the dashboard, ``redeem`` and ``open`` at a sign-in,
    ``issue_code`` when `latet dashboard` asks for a code (the tutorial makes its first before its loop runs). None
    may wait on a network."""

    def issue_code(self, subject: str) -> str:
        """A one-time code, good for two minutes."""
        ...

    def redeem(self, code: str) -> str | None:
        """The code's subject, once; None for a code unknown, used or late."""
        ...

    def open(self, subject: str) -> str:
        """A new session's id, for the cookie."""
        ...

    def subject_of(self, sid: str) -> str | None:
        """The session's subject; None for one unknown, ended or expired. A computer's session lasts twelve hours from
        its last use (each call starts them again), a server's twelve hours from its opening, whatever its use."""
        ...

    def end(self, sid: str | None = None, subject: str | None = None) -> None:
        """End a session, or every session and code of a subject."""
        ...


class SettingsStore(Protocol):
    """The settings and the last ones approved: ``config.yaml`` and ``config.reviewed.json`` on a computer.

    Who calls: worker threads, one at a time under the runtime's lock, and the thread that starts the runtime. Never
    the event loop: they may wait."""

    def load(self) -> Config:
        """The settings kept. Raises ``ValueError`` when what is kept is not valid settings and ``OSError`` when it
        cannot be read: a reload answers those two as "cannot read the configuration" and applies nothing, and a start
        stops on them before a key is made or a store opened. An implementation raises its store's own failures as one
        of the two."""
        ...

    def save(self, config: Config) -> None:
        """Keep them. Raises ``OSError`` when they cannot be kept (a store's own failure, as that): whoever asked for
        the change is told it failed, and the settings do not count as approved."""
        ...

    def reviewed(self) -> Config | None:
        """The last settings approved; None when there are none, or when what is kept is not valid settings (another
        version of Latet's). Ones that are there and cannot be opened: None on a computer; a server's raises
        ``ValueError`` (the space does not start), so that a row changed by hand never counts as no approval. Whoever
        writes a server's database can still delete the row, which makes the approval look absent (the settings kept
        then count as approved, whatever they hold), or write back an older one he kept: no seal shows either, and a
        copy of the database kept elsewhere does (``latet.server.space_settings``)."""
        ...

    def save_reviewed(self, config: Config) -> None:
        """Keep ``config`` as the settings approved last. Raises ``OSError`` when it cannot: the runtime says so in its
        log and goes on with them."""
        ...

    def prepare(self, config: Config, *, strict: bool = False) -> Config:
        """The settings as they apply: ``config`` itself on a computer; on a server the shared settings with the
        identity's part of ``config`` on top. With ``strict``, raises when ``config`` drops an item set for
        everyone."""
        ...


class JobLock(Protocol):
    """A job that runs in one process at a time (the history's pruning; later the term sync and the SIEM).

    Who calls: the thread that runs the job (the recorder's, for the pruning), which may wait."""

    def hold(self, name: str, seconds: float) -> AbstractContextManager[bool]:
        """True inside when this process runs the job now; ``seconds``: when another may, if this one stops."""
        ...


class AwsSessions(Protocol):
    """The AWS credentials `latet claude` hands over for its sessions: never written in clear.

    Who calls: the event loop. The plugin bridge's routes (``add_session``, ``drop_session``, and ``has_session`` at
    each beat of the connection `latet claude` holds its session with, ``bridge.api.SESSION_BEAT``) and the route that
    signs a request (``session``). None may wait on a network."""

    def add_session(self, credentials: AwsCredentials) -> str:
        """Keep them for a new session, and answer its id: only who holds the id is given them."""
        ...

    def session(self, session: str) -> AwsCredentials:
        """A session's credentials; raises ``AwsError`` (``aws-session-gone``) once dropped or ended."""
        ...

    def has_session(self, session: str) -> bool:
        """Whether a session's credentials are still here: not dropped, not past their end."""
        ...

    def drop_session(self, session: str) -> bool:
        """Forget them; whether they were here."""
        ...


@dataclass(frozen=True)
class StateBackends:
    """Where one Latet (a computer's, or an identity's space on a server) keeps what it keeps: ``make_runtime``'s."""

    vault: VaultStore
    history: HistoryStore | None  # None: nothing recorded (`latet start --no-record`)
    conversations: Conversations
    sent: SentBlocks
    given: Given  # the conversations' texts as they left, and what the final check saw leave
    approvals: ApprovalsBackend
    feed: Feed  # the exchanges' live feed
    approvals_feed: Feed  # the Live Reviewer's
    sign_ins: SignIns
    settings: SettingsStore
    jobs: JobLock
    aws: AwsSessions
