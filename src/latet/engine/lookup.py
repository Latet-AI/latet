"""The bounds of a stored vault's lookup of one call, and what is read past them: never a refusal (the owner's ruling
of 2026-10-29, Task 8's fix round 6: a request is never refused for its size where the previous release masked it).

Within the bounds a call's lookup is the fast one: its texts' words and their interior windows asked of the store a
statement at a time, each answer bounded before it is sent. Past them:

- the interior windows (``admit_windows``): the remaining runs of the call's texts have none, their words asked all
  the same; windows past their share of ``LOOKUP_WORD_ROLES``, or a lookup of windows past the candidates, characters
  or bytes, are dropped whole (``engine.stored``). 0.10.x had no windows;
- the words, candidates, characters and bytes (``past``): the call reads as the release before read, an API key's
  words as 0.10.2 asked them (its ``TooManyCandidates`` where it was), a person's whole vault as 0.10.0 held it.

``LookupLimitExceeded`` is how a store's bounded lookup says it met a bound; the stored vault reads past it, and no
caller of the vault sees it."""

from __future__ import annotations

from dataclasses import dataclass, field

from latet.engine.vault import Entity, MaskingError

INTERIOR_WINDOWS = 1_000_000
LOOKUP_WORD_ROLES = 100_000
LOOKUP_CANDIDATES = 100_000
LOOKUP_CHARACTERS = 16_777_216
LOOKUP_BYTES = 64 << 20
LOOKUP_BATCH = 5_000
LOOKUP_EXHAUSTED = "the vault lookup exceeded its safe work limit; no text was masked or restored"


class LookupLimitExceeded(MaskingError):
    """A store's bounded lookup met a bound of its call (fixed words, no values): the stored vault reads past it as the
    release before read (``LookupBudget.past``)."""

    def __init__(self) -> None:
        super().__init__(LOOKUP_EXHAUSTED)


class TooMany(MaskingError):
    """A store's refusal of a lookup whose words name more entries than one call may weigh (0.10.2's
    ``server.vault.TooManyCandidates``, 422): an API key's call is refused with it where 0.10.2 refused it; a person's
    reads the whole vault instead, as 0.10.0 held it (``StoredVault.whole_past_bounds``)."""


@dataclass(slots=True)
class LookupBudget:
    """One outer call's work, shared by masking and restoring and by all nested calls.

    Windows include duplicates; words count each requested role; candidates count repeated reads and unreadable
    rows too, and each per-text candidate selection. Characters count all candidate string fields; bytes count
    sealed rows before they are opened. Cached/memory candidates count again when selected for a new text, bounding
    the per-text matcher objects held until the call ends as well. Nothing here is logged.

    ``windows_off``: past ``INTERIOR_WINDOWS`` (or a lookup of windows dropped), no run of the call has windows from
    then on. ``past``: past a bound of words, candidates, characters or bytes, the call reads as the release before
    (``engine.stored``) and no bound raises. ``plain``: the words of the call's texts as 0.10.x read them (not their
    windows), which are asked whatever happens.
    """

    windows: int = 0
    words: int = 0
    candidates: int = 0
    characters: int = 0
    bytes: int = 0
    failed: bool = False
    windows_off: bool = False
    past: bool = False
    plain: set[str] = field(default_factory=set)

    def admit_windows(self, count: int) -> bool:
        """Whether a run's ``count`` interior positions get their windows: counted before they are made, and none from
        the first run that does not fit on (the remaining runs read in the same order in every process)."""
        if self.windows_off or self.windows + count > INTERIOR_WINDOWS:
            self.windows_off = True
            return False
        self.windows += count
        return True

    interior = admit_windows  # ``anchors.words_and_windows``'s admission

    def querying(self, count: int) -> None:
        self.words += count
        self.check(self.words, LOOKUP_WORD_ROLES)

    def row(self, size: int = 0) -> None:
        self.candidates += 1
        self.bytes += size
        self.check(self.candidates, LOOKUP_CANDIDATES)
        self.check(self.bytes, LOOKUP_BYTES)

    def entity(self, entity: Entity, *, counted: bool = False) -> None:
        if not counted:
            self.row()
        self.characters += sum(
            len(value) for value in (entity.id, entity.etype, entity.original, entity.surrogate, entity.rule or "")
        )
        self.check(self.characters, LOOKUP_CHARACTERS)

    def relax(self) -> None:
        """Past a bound: the call reads as the release before read, and no bound raises from now on."""
        self.past, self.failed = True, False

    @property
    def row_room(self) -> int:
        return LOOKUP_CANDIDATES + 1 - self.candidates

    @property
    def byte_room(self) -> int:
        return LOOKUP_BYTES - self.bytes

    def check(self, value: int, limit: int) -> None:
        if self.past:
            return
        if self.failed or value > limit:
            self.failed = True
            raise LookupLimitExceeded()
