"""The engine facade used by the gateway and the plugin bridge: cached, session-scoped masking."""

from __future__ import annotations

import hashlib
import hmac
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from latet import observe
from latet.engine import document
from latet.engine.masker import ExtraValue, Masker, MaskReport
from latet.engine.rehydrate import Restore, restore_text, restores_for
from latet.engine.types import EntityClass, Replacement, SlotKind, Span
from latet.engine.vault import Vault
from latet.vision.policy import ALLOW, ImagePolicy

if TYPE_CHECKING:
    from latet.config import LiveReview, LlmReview
    from latet.state.protocols import Conversations

# What the dump guard counts: values about people (e-mail, phone, IBAN, ID numbers, names).
PERSONAL = frozenset({EntityClass.PII, EntityClass.NER})
# The start of the text a tool output withheld by the dump guard is replaced with.
WITHHELD_DUMP = "[withheld by Latet: this tool output holds"
# An identity's space on a Latet server is one of many in its process's memory: its engine keeps at most this much of
# the blocks it masked, and none larger than this (masked again when it comes back); and at most this much of its
# conversations' restores, and no conversation's heavier than this (made again, whole, at each use).
SPACE_CACHE_BYTES = 32 * 1024 * 1024
SPACE_BLOCK_BYTES = 1024 * 1024
SPACE_RESTORES_BYTES = 16 * 1024 * 1024
SPACE_RESTORE_LARGEST = 8 * 1024 * 1024
# Where the engine keeps the restores of every fake of its vault (``Engine.unmask``), beside each conversation's: a
# conversation's key is a string, so none is this.
_WHOLE_VAULT: tuple[None, bool] = (None, False)


def _field_tag(field: str | None) -> str:
    """A field's name in the key of the cache of masked texts: its digest, of a fixed length, made once for every value
    of a document (``document.once``). The name itself made each value's key cost the length of a long JSON key."""
    if not field:
        return ""
    return document.once(
        ("field-tag", field),
        lambda: hashlib.blake2b(field.encode("utf-8", "surrogatepass"), digest_size=16).hexdigest(),
    )


def personal_values(replacements: Iterable[Replacement]) -> int:
    """How many different values about people the replacements stand for."""
    return len({r.entity_id or r.original for r in replacements if r.eclass in PERSONAL})


def dump_notice(count: int, limit: int) -> str:
    """The text a tool output is replaced with when it holds more personal values than allowed."""
    return (
        f"{WITHHELD_DUMP} {count} personal values (names, e-mail addresses, phone numbers and the like), "
        f"more than the {limit} allowed in one output. Ask for fewer rows, or for counts and aggregates "
        "instead of the records.]"
    )


WITHHELD_UNREAD = "[withheld by Latet: the local name model could not read"


def unread_notice(count: int, seconds: float) -> str:
    """The text a tool output is replaced with when the local name model could not read its texts in the time it has
    for one output: what it would have found there never leaves in clear."""
    return (
        f"{WITHHELD_UNREAD} the {count} texts of this tool output within {seconds:g} seconds. Ask for fewer rows or "
        "fields, or for counts and aggregates instead of the records.]"
    )


class LRU[K, V]:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._data: OrderedDict[K, V] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: K) -> V | None:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key: K, value: V) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)

    def values(self) -> list[V]:
        """What it holds, the least recently used first: a copy, taken under its lock."""
        with self._lock:
            return list(self._data.values())

    def discard_if(self, gone: Callable[[V], bool] | None = None) -> int:
        """What ``gone`` says of dropped, under its lock; every value when None. How many went."""
        with self._lock:
            keys = [key for key, value in self._data.items() if gone is None or gone(value)]
            for key in keys:
                self._discard(key)
            return len(keys)

    def _discard(self, key: K) -> None:
        self._data.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)


class WeighedLRU[K, V](LRU[K, V]):
    """An ``LRU`` that weighs what it keeps (``weigh``: bytes, roughly), and may be bounded by that too: past ``budget``
    the least recently used go, and a value heavier than ``largest`` is not kept (asked for again, it is made again).
    None: bounded by the count alone."""

    def __init__(
        self, capacity: int, weigh: Callable[[V], int], budget: int | None = None, largest: int | None = None
    ) -> None:
        super().__init__(capacity)
        self._weigh = weigh
        self.budget, self.largest = budget, largest
        self.weight = 0  # what the values kept weigh
        self._weights: dict[K, int] = {}

    def put(self, key: K, value: V) -> None:
        weight = self._weigh(value)
        with self._lock:
            self._drop(key)
            if self.largest is not None and weight > self.largest:
                return  # not kept, and what was kept under its key before is gone too: this replaced it
            self._data[key] = value
            self._weights[key] = weight
            self.weight += weight
            self._trim()

    def bound(self, budget: int | None, largest: int | None) -> None:
        """New bounds, which what is kept already meets at once."""
        with self._lock:
            self.budget, self.largest = budget, largest
            if largest is not None:
                for key in [k for k, weight in self._weights.items() if weight > largest]:
                    self._drop(key)
            self._trim()

    def _drop(self, key: K) -> None:
        self._data.pop(key, None)
        self.weight -= self._weights.pop(key, 0)

    def _discard(self, key: K) -> None:
        self._drop(key)

    def _trim(self) -> None:
        while len(self._data) > self.capacity or (self.budget is not None and self.weight > self.budget):
            oldest, _ = self._data.popitem(last=False)
            self.weight -= self._weights.pop(oldest)


def _block_weight(block: tuple[str, tuple[Replacement, ...], int]) -> int:
    """What a masked block the engine keeps takes in memory, roughly: its text, and each replacement with its value
    and its fake."""
    text, replacements, _ = block
    return sys.getsizeof(text) + sum(
        sys.getsizeof(r) + sys.getsizeof(r.original) + sys.getsizeof(r.replacement) for r in replacements
    )


def _restores_weight(kept: tuple[int, int, list[Restore]]) -> int:
    """What a conversation's restores the engine keeps take in memory, roughly: the list, and each restore with its
    two texts (counted even where it shares one with the vault's entity)."""
    restores = kept[2]
    return sys.getsizeof(restores) + sum(
        sys.getsizeof(r) + sys.getsizeof(r.pattern) + sys.getsizeof(r.replacement) for r in restores
    )


@dataclass(slots=True)
class Session:
    """The entities a conversation has been shown: only these may be restored in its responses."""

    key: str
    entity_ids: set[str] = field(default_factory=set)
    project_id: str | None = None  # the project of its working directory, once known
    cwd: str | None = None
    approved: set[str] = field(default_factory=set)  # keyed hashes of the texts you approved in it (Live Reviewer)
    # The folder a client of the conversation reported to this process (``ScopeSource.attach``). On a Latet server the
    # project and folder may come from another instance, through Valkey: what that project lowers applies here only once
    # a client has reported its folder here (``Projects.scope_for``). Never kept with the conversation.
    reported: str | None = None
    # Whether what it is shown and its project are kept with the conversation (``Conversations.update``), for every
    # process that serves it. False: a call that is no conversation (an application's, on the applications API), whose
    # session lives as long as the call and holds what it was shown in its own memory alone.
    kept: bool = True

    def add(self, ids: Iterable[str]) -> None:
        self.entity_ids.update(ids)


@dataclass(slots=True)
class Scope:
    """The rules of one project: a masker of its own, or Latet turned off there."""

    key: str  # changes whenever the rules do (this process's own: it counts the settings it applied)
    tag: str  # the rules alone, the same on every instance of a Latet server (``Mediator._given_key``)
    masker: Masker
    enabled: bool = True
    images: ImagePolicy = ALLOW  # whether requests that carry images and documents are sent
    dump_guard: int | None = None  # at most this many personal values in one tool output (0: no limit)
    review: LlmReview | None = None  # the local reviewer here (None: as the global settings say)
    live: LiveReview | None = None  # the Live Reviewer here (None: as the global settings say)
    ocr: bool = True  # the text in images is read here, when the models are installed


class ScopeSource(Protocol):
    def scope_for(self, session: Session) -> Scope | None: ...

    def attach(self, session: Session, cwd: Any) -> object: ...


@dataclass(slots=True)
class SlotResult:
    text: str
    replacements: list[Replacement]
    cached: bool


class Engine:
    def __init__(
        self, vault: Vault, masker: Masker, cache_size: int = 50_000, conversations: Conversations | None = None
    ) -> None:
        self.vault = vault
        self.masker = masker
        self.rules_version = 0
        # A digest of the rules in use, which ``Runtime.apply_config`` sets: the same on every instance of a Latet
        # server, where ``rules_version`` is this process's own count. It names the rules a text left under
        # (``Mediator._given_key``).
        self.rules_tag = ""
        # Each text as masked, and the vault version at which it held none of the vault's values in clear; weighed, and
        # in an identity's space on a server bounded by that too (``bound``).
        self._frozen: WeighedLRU[bytes, tuple[str, tuple[Replacement, ...], int]] = WeighedLRU(
            cache_size, _block_weight
        )
        if conversations is None:
            from latet.state.local import LocalConversations  # here: latet.state.local imports this module

            conversations = LocalConversations(reverse=cache_size)
        # What each conversation was shown, and what the model wrote: this process's memory on a computer, Valkey on a
        # Latet server (any instance restores what another masked).
        self.conversations: Conversations = conversations
        # Each conversation's restores in each mode, with the count of its values and the vault's version they were
        # made at: the newer replaces the older (none is found again once either moved). Weighed, and in an identity's
        # space on a server bounded by that too (``bound``). The whole vault's, which ``unmask`` reads, are kept with
        # them, under a key no conversation has (``_WHOLE_VAULT``).
        self._restores: WeighedLRU[tuple[str | None, bool], tuple[int, int, list[Restore]]] = WeighedLRU(
            1_000, _restores_weight
        )
        self.scopes: ScopeSource | None = None  # projects with rules of their own

    def _key(self, tag: str, text: str) -> bytes:
        return hmac.new(self.vault.cache_key, f"{tag}\x00{text}".encode(), hashlib.sha256).digest()

    def bound(self, budget: int, largest: int) -> None:
        """Keep at most ``budget`` bytes of the blocks masked, and none over ``largest`` (masked again when it comes
        back), and at most ``SPACE_RESTORES_BYTES`` of the conversations' restores, none over ``SPACE_RESTORE_LARGEST``
        (made again, whole, at each use): an identity's space on a Latet server, one of many in its process's memory."""
        self._frozen.bound(budget, largest)
        self._restores.bound(SPACE_RESTORES_BYTES, SPACE_RESTORE_LARGEST)

    @property
    def cached_bytes(self) -> int:
        """What the masked blocks and the restores kept take, roughly (a server's budget of spaces counts it)."""
        return self._frozen.weight + self._restores.weight

    def session(self, key: str) -> Session:
        return self.conversations.session(key)

    def sessions(self) -> list[Session]:
        return self.conversations.sessions()

    def attach(self, session: Session, cwd: Any) -> None:
        """Give a session its project, from the working directory a client reported (``scopes.attach``): a project
        it gets, or another one, is kept with the conversation (not for a session not ``kept``)."""
        if self.scopes is None:
            return
        before = (session.project_id, session.cwd)
        self.scopes.attach(session, cwd)
        if (session.project_id, session.cwd) != before and session.kept:
            self.conversations.update(session)

    def show(self, session: Session, ids: Iterable[str]) -> None:
        """Entities a conversation is shown: kept with it when they are new to it, so that every process serving it
        restores them. A session not ``kept`` holds them in its own memory alone."""
        new = set(ids) - session.entity_ids
        if new:
            session.add(new)
            if session.kept:
                self.conversations.update(session, shown=new)

    def hold(self, session: Session) -> object | None:
        """The entities ``session`` was shown, in memory and used now, and kept there until its answer is restored (on
        the event loop, which finds them in memory alone): a vault that keeps most of its values elsewhere brings those
        that left it back (``Vault.hold``), and lets them go at ``release``, given what this returns. In the thread
        that masks."""
        return self.vault.hold(tuple(session.entity_ids), session.key)  # tuple(): a copy at once, as ``restores``

    def release(self, session: Session, held: object | None) -> None:
        """``session``'s answer is restored: what ``hold`` kept for it (``held``, its answer) may leave memory again."""
        self.vault.release(session.key, held)

    def scope(self, session: Session) -> Scope | None:
        """The rules of the session's project, when they differ from the global ones."""
        if self.scopes is None or session.project_id is None:
            return None
        return self.scopes.scope_for(session)

    def json_numbers(self, session: Session, kind: SlotKind, tool: str | None, path: str | None = None) -> bool:
        """The numbers of a JSON document are masked too, in this session's project."""
        scope = self.scope(session)
        if scope is not None and not scope.enabled:
            return False
        return (scope.masker if scope is not None else self.masker).json_numbers(kind, tool, path)

    def use(self, masker: Masker) -> None:
        """Switch to a masker built from new rules, keeping sessions (what each may restore)."""
        self.masker = masker
        self.rules_changed()

    def rules_changed(self) -> None:
        """New rules apply to blocks masked from now on (old cache entries stop matching)."""
        self.rules_version += 1

    def find(self, text: str, kind: SlotKind, session: Session) -> list[Span]:
        """What masking ``text`` in ``session`` would replace (``Masker.findings``), by the rules ``mask_text`` would
        mask it by (its project's, when they differ from the global ones), without masking it: the vault learns nothing
        and the session is shown nothing. Nothing where Latet is off in its project, as ``mask_text`` replaces nothing
        there."""
        if not text:
            return []
        scope = self.scope(session)
        if scope is not None and not scope.enabled:
            return []
        return (scope.masker if scope is not None else self.masker).findings(text, kind)

    def mask_text(
        self,
        text: str,
        kind: SlotKind,
        session: Session,
        from_model: bool = False,
        path: str | None = None,
        report: MaskReport | None = None,
        keep_surrogates: bool = False,
        tool: str | None = None,
        field: str | None = None,
        extra: Sequence[ExtraValue] = (),
        again: bool = False,
        context: str = "",
    ) -> SlotResult:
        """Mask one text, the way it was masked before (the cache is keyed by its text), unless the vault
        has learnt since a value it left in clear: then it is masked again, and kept that way. A
        conversation sends its own texts again as they left (``Mediator.mask_value``).

        ``extra`` are values the local reviewer or you (in the Live Reviewer) found in it: the block is
        masked again with them, and that becomes what it is sent as from now on. ``again`` masks it
        again with what the vault knows now, and that too becomes what it is sent as: a value of a
        JSON document, masked before a later value of the document taught the vault a value in it.
        ``context``: the text around it, which a fake drawn for a value in it must not hold (a key's, the other keys of
        its object).
        """
        if not text:
            return SlotResult(text, [], True)
        scope = self.scope(session)
        masker = scope.masker if scope is not None else self.masker
        if keep_surrogates:
            self.adopt(session, text, masker)
        if scope is not None and not scope.enabled:
            return SlotResult(text, [], True)  # Latet is off in this project: what is there is restored, nothing more
        if from_model:
            exact = self.conversations.recall(self._key("R", text))
            if exact is not None:
                return SlotResult(exact, [], True)  # the bytes the model produced, restored earlier
        fkey = self._frozen_key(text, kind, scope, from_model, path, keep_surrogates, tool, field)
        # The values the text may hold, brought into memory where the vault keeps most of its values elsewhere (an API
        # key's: ``Vault.prefetch``), held there until it is masked: a value found once is masked for ever.
        with self.vault.holding():
            version = self.vault.version  # read before masking: a value learnt meanwhile has it checked next time
            hit = self._frozen.get(fkey) if not extra and not again else None
            if hit is not None and not self.vault.kept_since(hit[2]):
                hit = None  # it may hold a fake of a call that did not keep its values: masked again below
            if hit is not None and hit[2] != version:
                if not self.vault.holds_since(hit[2]):
                    self.vault.prefetch([text])  # values left memory since: memory alone no longer vouches for it
                if masker.known_in(hit[0]):
                    hit = None  # it left a value the vault learnt since in clear: masked again below
                else:
                    hit = (hit[0], hit[1], version)
                    self._frozen.put(fkey, hit)
            if hit is not None:
                masked, replacements, _ = hit
                self.show(session, (r.entity_id for r in replacements if r.entity_id))
                return SlotResult(masked, list(replacements), True)
            self.vault.prefetch([text])
            result = masker.mask(
                text,
                kind,
                path=path,
                from_model=from_model,
                report=report,
                keep_surrogates=keep_surrogates,
                tool=tool,
                field=field,
                extra=extra,
                context=context,
            )
        observe.masked(result.replacements)  # a server's metrics: the values of a text masked for the first time here
        self.show(session, (r.entity_id for r in result.replacements if r.entity_id))
        self._frozen.put(fkey, (result.text, tuple(result.replacements), version))
        return SlotResult(result.text, result.replacements, False)

    def _frozen_key(
        self,
        text: str,
        kind: SlotKind,
        scope: Scope | None,
        from_model: bool,
        path: str | None,
        keep_surrogates: bool,
        tool: str | None,
        field: str | None,
    ) -> bytes:
        scope_key = scope.key if scope is not None else ""
        where = f"{path or ''}|{tool or ''}|{_field_tag(field)}"
        flags = f"{int(from_model)}{int(keep_surrogates)}"
        return self._key(f"F{self.rules_version}|{scope_key}|{kind}|{flags}|{where}", text)

    def cached(
        self,
        text: str,
        kind: SlotKind,
        session: Session,
        from_model: bool = False,
        path: str | None = None,
        keep_surrogates: bool = False,
        tool: str | None = None,
        field: str | None = None,
    ) -> bool:
        """Whether ``mask_text`` gives ``text`` as it was masked before, its masker not run for it: one the cache keeps,
        in which the vault has learnt nothing since that it leaves in clear; one recalled as the model's own bytes; any,
        where Latet is off in the session's project. Not one masked before values left the vault's memory: memory alone
        cannot say (``Vault.holds_since``); nor one masked before a call failed to keep its values (``kept_since``)."""
        if not text:
            return True
        scope = self.scope(session)
        if scope is not None and not scope.enabled:
            return True
        if from_model and self.conversations.recall(self._key("R", text)) is not None:
            return True
        hit = self._frozen.get(self._frozen_key(text, kind, scope, from_model, path, keep_surrogates, tool, field))
        if hit is None or not self.vault.kept_since(hit[2]):
            return False
        masker = scope.masker if scope is not None else self.masker
        return hit[2] == self.vault.version or (self.vault.holds_since(hit[2]) and not masker.known_in(hit[0]))

    def adopt(self, session: Session, text: str, masker: Masker) -> None:
        """Surrogates already in ``text`` (a resumed conversation) belong to ``session`` too, those its vault keeps out
        of memory as well (``Vault.fakes_out_of_memory``)."""
        found = [e.id for s in masker.surrogates_in(text) if (e := self.vault.by_surrogate(s.of(text)))]
        self.show(session, [*found, *(e.id for e in self.vault.fakes_out_of_memory(text))])

    def remember_model_output(self, masked: str, restored: str) -> None:
        """When the client sends back ``restored`` as history, send the model's exact ``masked``."""
        if masked != restored:
            self.conversations.remember(self._key("R", restored), masked)

    def restores(self, session: Session, json_mode: bool) -> list[Restore]:
        """What the conversation's fakes restore to, every one it was shown: kept while neither its values nor the
        vault moved, one list for each conversation and mode."""
        key, count, version = (session.key, json_mode), len(session.entity_ids), self.vault.version
        kept = self._restores.get(key)
        if kept is not None and kept[0] == count and kept[1] == version:
            return kept[2]
        # tuple(): a copy taken at once, while the engine thread may be adding to the set.
        entities = [e for i in tuple(session.entity_ids) if (e := self.vault.get(i)) is not None]
        found = restores_for(entities, json_mode=json_mode)
        self._restores.put(key, (count, version, found))
        return found

    def unmask(self, text: str) -> tuple[str, int]:
        """``text`` with every fake of the vault in it restored, whatever conversation or call it was issued in, and
        how many different values were restored. A person's name is restored whole only: one of its parts alone
        (``Bianchi``, of ``Laura Bianchi``) may be an ordinary word, or another person's
        (``restores_for(parts=False)``)."""
        if not text:
            return text, 0
        restored, ids = restore_text(text, self._unmasking(text))
        return restored, len(set(ids))

    def _unmasking(self, text: str) -> list[Restore]:
        """The restores of the entities whose fake may be in ``text`` (``Vault.candidates_in``). Where the vault gives
        every entity it holds, whatever the text, they are kept while neither their count nor its version moved, with
        the conversations' restores (``_WHOLE_VAULT``); a vault that narrows them to the text has them made for each."""
        if type(self.vault).candidates_in is not Vault.candidates_in:  # overridden: they depend on the text
            return restores_for(self.vault.candidates_in(text), parts=False)
        count, version = self.vault.count, self.vault.version  # read first: a value kept meanwhile moves them past it
        kept = self._restores.get(_WHOLE_VAULT)
        if kept is not None and kept[0] == count and kept[1] == version:
            return kept[2]
        found = restores_for(self.vault.candidates_in(text), parts=False)
        self._restores.put(_WHOLE_VAULT, (count, version, found))
        return found
