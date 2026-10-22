"""The administration of a Latet server, under ``/_latet/server/v1/admin``: an administration token in
``x-latet-admin``, from any address, over HTTPS when the server's address is https, never from a web page. People and
API keys with their tokens, codes and keys; an API key's settings (a person's are theirs, in their dashboard), which the
administration approves as it sets them; the shared settings; the administration log; the server's state; and the
administration tokens themselves, listed (the one a call is made with marked), issued and revoked.

Every call is recorded in the administration log, reads and refusals too, with its status, its address and its actor
(``admin:<token id>``; ``unknown`` for a call refused before its token was known). What is recorded never holds a code,
a key, a token, a value, a name or an email, nor any text a caller gave but an id of the server's own shape. A call is
recorded whatever happens to it: one whose handler raises is recorded under its own name with status 500, and one whose
entry cannot be written answers 500, its answer withheld, the instance's log saying so by the call's name. A change is
written in one transaction with its entry, last there, so that both are kept or neither: the shared settings', an
identity's (``Identities.changing``, which tells the instances once it has committed) and the approval of an API key's
settings (in the transaction of its snapshot, ``PgSettings.recording``). A change whose entry cannot be written is not
made, and the call answers 500. An API key's settings are applied, and their own part kept, before their approval: when
the approval cannot be kept, the call's entry says so (``approved: false``, with the keys it changed), its status the
refusal's. A call cancelled while its change is being made (the server stopping) can be recorded twice: by its change's
entry, when the change ends first, and by its own, which says it did not answer.

A write checks its token after waiting for its body, a thread, an identity's turn or a lock. Ordinary writes read and
lock the token's row first in their mutation transaction (``AdminTokens.held``), and check it again before the audit
entry written last there. Revocation waits for that transaction or finds the change made. An API key's settings use
a separate transaction holding only the token's row: checked before apply, with the settings turn and runtime lock
held, and retained through both the own-part save and approval commits on other connections. Its idle timeout is
disabled only inside that held transaction (``still``); its inner settings transactions never reacquire the token's
lock. Expiry for this path is judged at the initial held check, without a later recheck. A token gone or expired at
the applicable check makes no change (401, recorded as revoked or expired). Token rows precede other rows in ordinary
writes, the held transaction takes no settings or audit rows, and no transaction takes a token after the audit chain's
lock. Reads are not checked again.

An administration token issues tokens that end no later than itself: ``days`` that would end later, or ``never`` from a
token that ends, are refused (403), and the server's default (``admin_token_days``) is cut at the caller's end. Any
token may be revoked, the one the call is made with and the last one included (the way back is `latet server init
--new-admin-token`, on a machine with the database's access and the master key); two that revoke each other at once
take their rows in the order of their ids, so that one waits for the other and is then refused. A new token is shown
once, in the answer that issues it: its entry names its id and how long it lasts, never its label, a caller's text.

A call is refused before its token is read when it comes from a web page or in clear, or carries no administration
token at all: such a refusal costs no lookup, and never counts toward the limit that guards the lookup (a page can make
one at will, and would shut an administrator's address out). Its address records the first ``LOOKUP_LIMIT`` of them in
a window, then one entry for the window. An administration token is looked up only once its address has taken a slot
of that limit (``Bus.take``, ``machines.miss_key``: one budget of ``LOOKUP_LIMIT`` in ``LOOKUP_WINDOW`` for every kind
of miss of the address, the gate's and the machines' too): one step in Valkey, so that no more lookups are made in a
window than the limit, however many calls come at once. Beyond it a call is refused (429, with ``Retry-After``) with
nothing read, and one entry for the window says that the address was limited. A token that proves valid gives its slot
back: an administrator's calls cost their address nothing. A token found valid in the last hour, on any instance, takes
no slot at all (``machines.Known``), so that strangers sharing its holder's address cannot shut it out, even past the
address's limit, when Valkey is asked about it with the others refused a batch at a time (``machines.admitted``); it is
looked up all the same, and one revoked or expired meanwhile is refused, forgotten and counted. An address is counted
by its bucket (``machines.address``: an IPv6 one by its /64, and its misses by its /48 too). When Valkey cannot take
the slot, the call is refused (503, with ``Retry-After``), and one entry a minute says how many were. Whatever their
addresses, the whole server records at most ``REFUSED_ENTRIES`` calls refused before their token was known in a window:
past it a refused call is counted and its entry left out, one entry saying how many (``Refusals``: the first at once,
then once a minute, the beat writing what is left), so that a flood from many addresses holds no administrator's call
behind the chain's lock. No refusal asks those threads anything to be recorded unless its address was charged a slot: a
call shed while the credentials' threads were busy (``machines.CREDENTIALS_BUSY``), whatever the queue is by the time
it is recorded, and any refused while they are busy, are counted so at once (``busy``).

Settings are written whole: a body that is not a JSON object is refused, never read as empty settings (which would set
them back to their defaults). A PUT that carries the etag of what it read is refused (409) when they changed meanwhile,
the etag compared under the lock every change takes: the runtime's for an API key's, the shared settings' own in
PostgreSQL for theirs. What is wrong with settings given is said by field and kind of error, never by what they hold. No
settings name a file or a variable of the server's to read the reviewer's key from (``shared.reviewer_key_named``).
The shared settings are read as PostgreSQL has them, never from the instance's copy (which an announcement lost leaves
behind), and weigh ``SETTINGS_LIMIT`` at most sealed (413 past it). A row of them that does not open (written by hand,
sealed with another key) is answered as such (500, nothing of it said) and replaced only by settings written whole
without an etag (with one, 409), nothing of it read: the entry says so (``unreadable``).

A body is read whole within the server's bound, and parsed, made settings and answered in the administration's threads,
never on the event loop: tens of thousands of shared terms, or a log of thousands of entries, hold no identity's
requests while they are read or written (the terms made settings a part at a time, ``shared.validated``). ``GET log``
answers ``LOG_MOST`` entries at most per page: an older page uses its ``cursor`` with the same original filters;
``until`` remains the exclusive upper time bound.

Who calls: the event loop the routes, and two pools of threads, neither the default executor the spaces build in (R-6,
R-7, Task 14's review). The limiter and the lookup of a token not yet known, which answer in milliseconds, run in the
few threads of ``machines.off_loop``, whose calls waiting are bounded (past the bound, 503 with ``Retry-After``); an
address the limiter found past its limit is refused before them, on the event loop, until its window ends
(``machines.Limits``), so that a flood of refused calls from one address costs them nothing more. Every other call to
PostgreSQL, Valkey or the name resolver, every entry of the log (a refusal's too), a settings save and the end of an
identity's sessions run in the administration's own (``ADMIN_THREADS``), one save of an identity's settings at a time,
so that a save waiting on its identity's lock holds one thread and delays no other call. The space an API key's settings
need is built by the spaces, in their threads."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from psycopg import OperationalError
from psycopg.errors import QueryCanceled
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from latet import __version__
from latet.config import Config
from latet.gateway.security import from_browser
from latet.http_inputs import same_etag
from latet.log_paging import LogCursor, LogFilters, finite_time, page_limit
from latet.logline import Paced
from latet.protection import changed_keys
from latet.runtime import digest
from latet.server import egress
from latet.server.audit import LogReadRefused
from latet.server.identities import (
    ADMIN_TOKEN_ID,
    IDENTITY_ID,
    LABEL_LENGTH,
    NAME_LENGTH,
    TOKEN_ID,
    AdminTokenEnded,
    EmailTaken,
    NoSuchIdentity,
    OutlivesIssuer,
    clean,
)
from latet.server.logs import REFUSED
from latet.server.machines import (
    ENROL_BYTES,
    LABEL_CHARS,
    LOOKUP_LIMIT,
    LOOKUP_WINDOW,
    QUEUED_RETRY_AFTER,
    RETRY_AFTER,
    CredentialsBusy,
    Known,
    _within,
    address,
    admitted,
    client_of,
    counted,
    credentials_busy,
    given_back,
    miss_key,
    off_loop,
    routes_of,
)
from latet.server.settings import SERVER_API
from latet.server.shared import (
    REVIEWER_KEY,
    RULE_KEYS,
    RULES_FOR_EVERYONE,
    SETTING_KEYS,
    SettingsRefused,
    SharedItemsError,
    SharedTooLarge,
    StaleSettings,
    UnreadableShared,
    kept_rules,
    kept_said,
    own_destinations_refused,
    reviewer_key_named,
    shared_refusals,
    validated,
    with_kept_rules,
    wrong_fields,
)
from latet.server.space_settings import PgSettings, SettingsStale, SettingsTooLarge
from latet.server.spaces import IdentityUnavailable, SpaceUnavailable
from latet.server.tokens import ADMIN_PREFIX, COMPUTER_PREFIX, KEY_PREFIX, OUTLIVES_ISSUER, TOKEN_PREFIX
from latet.server.vault import vault_usage

if TYPE_CHECKING:
    from latet.runtime import Runtime
    from latet.server.app import Server
    from latet.server.audit import AuditLog
    from latet.server.identities import AdminTokenInfo, Changes, Identity

log = logging.getLogger(__name__)

ADMIN_API = f"{SERVER_API}/admin"
# The administration's own threads, for every call it makes to a store once a token is known and for every entry of its
# log, refusals' too: a settings save waiting on its identity's lock, or an entry on the chain's, holds one of them,
# never one of the lookups' (``machines.off_loop``), nor one of the default executor, which the spaces build in.
ADMIN_THREADS = 4
_POOL = ThreadPoolExecutor(ADMIN_THREADS, thread_name_prefix="latet-admin")
# Settings are written whole: a PUT that carries the etag of what it read is refused when they changed meanwhile.
STALE = "these settings changed since you read them: read them again"
UNKNOWN = "unknown"  # who made a call refused before its token was known
# The idle timeout of the transaction that holds a token's row while an API key's settings are applied and approved
# (``still``), whatever the role's: far past what a save takes (its statements are bounded by a minute each), so that
# a save that hangs holds a revocation of its token this long at most, never for good.
HELD_IDLE_SECONDS = 600
HELD_IDLE = "SELECT set_config('idle_in_transaction_session_timeout', %s, true)"  # in its transaction alone
LOG_LIMIT = 500  # the entries GET log answers when it is not told how many
LOG_MOST = 10_000  # the entries GET log answers at most: an older page is read with ``until``
NOT_SETTINGS = "a JSON object: the settings, all of them as GET answers them, and the etag GET answered if you have it"
FORGET_KEPT = "forget_kept_rules is true, or left out"
UNRECORDED = (
    "this call could not be recorded in the administration log, so its answer is withheld (a change it made stands): "
    "try again once the log can be written"
)
UNREADABLE_SHARED = (
    "the shared settings kept in the database cannot be read (written there by hand, or sealed with another key): "
    "write new ones whole, without an etag, to replace them"
)
_TRIES = 3  # a shared change written blind reads them again when another lands between its reading and its lock
_HEADERS = {"cache-control": "no-store", "x-content-type-options": "nosniff"}  # an answer may hold a code or a key
# The entries of calls refused before their token was known that the whole server writes in a window of
# ``LOOKUP_WINDOW``: each address has its limit, and each entry takes the chain's lock, one at a time for the server,
# and is kept a year unless the settings say otherwise (audit_retention_days). Sixty a minute is ten administrators'
# mistyped calls, and what an outsider makes with a few addresses grows the chain by sixty entries a minute at most,
# and one that counts the rest (it was six hundred). Past it, a refused call is refused as before, and its entry is
# left out; one entry a window says how many were (``Refusals``).
REFUSED_ENTRIES = 60
# The days a new administration token may be asked to last at most: ten years, the most admin_token_days takes.
TOKEN_DAYS_MOST = 3650
# What a new administration token's label may not hold, whatever its case: the beginning of a secret of Latet's, pasted
# in the wrong place, which the store would keep in clear and every administrator would read in the list.
_SECRET_STARTS = (TOKEN_PREFIX, COMPUTER_PREFIX, KEY_PREFIX, ADMIN_PREFIX)


class Refusals:
    """What the administration counts of the refusals it does not write one by one, each said once a minute with how
    many (``Paced``): the calls refused because Valkey could not take their slot (``unavailable``), the refusals Valkey
    could not count against their address's budget of entries (``uncounted``), those past the server's ceiling of
    entries for the window (``unrecorded``), and those refused, or recorded, while the credentials' threads were busy
    (``busy``: ``machines.CREDENTIALS_BUSY``, which nothing is asked of then). The first of each is written at once;
    what came since is written by ``flush``, which the beat calls once a minute, so that the end of a burst or of an
    outage is in the log too. Each entry is a count and a reason: no address, no token. An instance keeps one
    (``Server.refusals``)."""

    REASONS = ("unavailable", "uncounted", "ceiling", "busy")

    def __init__(self) -> None:
        self.unavailable, self.uncounted, self.unrecorded, self.busy = Paced(), Paced(), Paced(), Paced()

    def _counters(self) -> tuple[tuple[str, Paced], ...]:
        return tuple(zip(self.REASONS, (self.unavailable, self.uncounted, self.unrecorded, self.busy), strict=True))

    def flush(self, audit: AuditLog) -> None:
        """In a worker thread: each count not written yet, written now as one entry (``calls.refused``) and said in the
        instance's log. One that cannot be written is said in the log by its error's type, and its count with it."""
        for reason, paced in self._counters():
            if count := paced.flush():
                self.write(audit, count, reason)
                log.warning(
                    "the administration refused %d more calls not recorded one by one (%s) since the line before",
                    count,
                    reason,
                )

    @staticmethod
    def write(audit: AuditLog, count: int, reason: str) -> None:
        """In a worker thread: one entry for ``count`` refused calls not recorded one by one, and why. Its failure is
        said by the error's type and not raised: the calls were refused all the same."""
        try:
            audit.record(UNKNOWN, "calls.refused", None, {"count": count, "reason": reason})
        except Exception as exc:  # by its type: its words can quote what the database was given
            log.error(
                "%d refused calls (%s) could not be recorded in the administration log (%s)",
                count,
                reason,
                type(exc).__name__,
            )


async def _in_pool[T](work: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """``work`` in the administration's own threads (``ADMIN_THREADS``)."""
    return await asyncio.get_running_loop().run_in_executor(_POOL, functools.partial(work, *args, **kwargs))


def _error(message: str, status: int, **more: Any) -> JSONResponse:
    return JSONResponse({"error": message, **more}, status_code=status)


def _missing() -> JSONResponse:
    return _error("there is no such identity", 404)


def _same(given: str, expected: str) -> bool:
    """Whether caller text is the generated etag, using the shared byte comparison."""
    return same_etag(given, expected)


async def _object(request: Request) -> dict[str, Any] | None:
    """The body as a JSON object; None for anything else: none, truncated, ``null``, a list, a text, a number, or nested
    deeper than Python reads. Never ``{}`` in their place, which settings would read as their defaults (N1). Parsed in
    the administration's threads: a body as large as the server's bound lets through holds no event loop."""
    return await _in_pool(_parsed, await request.body())


def _parsed(body: bytes) -> dict[str, Any] | None:
    """In a worker thread: ``body`` as a JSON object, or None."""
    try:
        found = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return found if isinstance(found, dict) else None


def _settings_of(given: dict[str, Any]) -> Config | list[str]:
    """In a worker thread: ``given`` as settings (a long list of terms a part at a time, the GIL let go between two), or
    what is wrong with them (``shared.wrong_fields``: by field and kind of error, never what they hold)."""
    wrong: list[str] = []
    try:
        return validated(given)
    except ValidationError as exc:
        wrong = wrong_fields(exc)
    return wrong


def _no_setting(given: dict[str, Any]) -> str | None:
    """Words for the keys of ``given`` that are no setting: counted, never named (a key can be a value pasted by
    mistake). None when every one is a setting."""
    count = sum(key not in Config.model_fields for key in given)
    if not count:
        return None
    many = count != 1
    return f"these name {count} key{'s' if many else ''} that {'are' if many else 'is'} no setting (never quoted)"


def _merged(base: Config | None, given: dict[str, Any], keys: Any) -> Config:
    """In a worker thread: the settings ``given`` names, over ``base``'s ``keys`` (a key it leaves out keeps its
    value; with no ``base``, its default). Raises ValidationError when they are no settings together."""
    kept = base.model_dump(mode="json", include=set(keys)) if base is not None else {}
    return validated({**kept, **given})


def _settings_answer(config: Config) -> JSONResponse:
    """In a worker thread: settings as the administration answers them, with their etag: tens of thousands of terms
    rendered off the event loop."""
    return JSONResponse({"settings": config.model_dump(mode="json"), "etag": digest(config)})


def _shared_answer(stored: Config, rules: Any) -> JSONResponse:
    """In a worker thread: the shared settings as the administration answers them: the settings the server shares, the
    etag of the row as it is, and apart the rules for everyone an earlier version kept in it (``kept_rules``), which no
    space applies."""
    dumped = stored.model_dump(mode="json")
    kept = kept_rules(stored, rules(stored))
    return JSONResponse(
        {
            "settings": {key: dumped[key] for key in SETTING_KEYS},
            "etag": digest(stored),
            "kept_rules": {key: dumped[key] for key in kept},
        }
    )


def _log_answer(entries: list[Any]) -> JSONResponse:
    """In a worker thread: the entries of the log as ``GET log`` answers them."""
    return JSONResponse({"entries": [asdict(e) for e in entries]})


def _token_asked(body: bytes) -> tuple[str, int, bool] | str:
    """In a worker thread: what ``body`` asks a new administration token to be, as a JSON object holding ``label`` and
    at most one of ``days`` and ``never``: its label (a text of ``LABEL_CHARS`` characters at most as sent, something
    left once cleaned as the store keeps a label, holding no beginning of a secret of Latet's), its days (a whole
    number from 1 to ``TOKEN_DAYS_MOST``, never a bool, a float, a text or null; 0, when neither is given: the server's
    default) and whether it lasts for good (``never``, exactly true). Or the words of the refusal, which name the fields
    and never what they hold."""
    given = _parsed(body)
    if given is None or not set(given) <= {"label", "days", "never"}:
        return "a JSON object: label, and days or never if either"
    label = given.get("label")
    if not isinstance(label, str) or len(label) > LABEL_CHARS or not clean(label, LABEL_LENGTH):
        return (
            f"label is a text of {LABEL_CHARS} characters at most, with something left once its invisible characters "
            "are taken out"
        )
    if any(start in clean(label, LABEL_CHARS).casefold() for start in _SECRET_STARTS):
        return (
            "label holds what a token or a key begins with (latet_pat_, latet_ck_, latet_key_, latet_adm_): a label "
            "is no place for a secret"
        )
    if "days" in given and "never" in given:
        return "days and never: one of them at most"
    if "days" in given:
        days = given["days"]
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= TOKEN_DAYS_MOST:
            return f"days is a whole number from 1 to {TOKEN_DAYS_MOST}"
        return label, days, False
    if "never" in given and given["never"] is not True:
        return "never is true, when it is given"
    return label, 0, "never" in given


def _time(moment: Any) -> float | None:
    """A time of an administration token's row as an answer gives it: a finite number, else none (a row written by hand
    can hold anything its column takes, ``NaN`` among them, which JSON has no name for)."""
    return float(moment) if isinstance(moment, int | float) and math.isfinite(moment) else None


def _tokens_answer(tokens: list[AdminTokenInfo], caller: str | None) -> tuple[JSONResponse, int, int]:
    """In a worker thread: the administration tokens as ``GET admin-tokens`` answers them, with how many there are and
    how many of them do not hold. Ids, labels, times and what issued each, the one the call is made with marked
    (``this``, never a row that does not hold), each row that does not hold flagged: never a secret, never a hash."""
    listed = [
        {
            "id": t.id,
            "label": t.label,
            "created": _time(t.created),
            "last_used": _time(t.last_used),
            "expires": _time(t.expires),
            "this": not t.tampered and t.id == caller,
            "tampered": t.tampered,
            "issued_by": t.issued_by,
        }
        for t in tokens
    ]
    return JSONResponse({"tokens": listed}), len(listed), sum(t.tampered for t in tokens)


def _in_utc(moment: float) -> str:
    """A time as the administration's words say it: in UTC, to the second."""
    try:
        return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return f"{moment:.0f} seconds after 1970"


def _moment(text: str | None) -> float | None:
    """A time given in a query, in seconds since 1970: finite. Raises ValueError."""
    if text is None:
        return None
    found = float(text)
    if not math.isfinite(found):
        raise ValueError("a time is finite")
    return found


def _count(text: str | None) -> int:
    """How many entries a query asks for: a whole number from 1 to ``LOG_MOST``. Raises ValueError."""
    if text is None:
        return LOG_LIMIT
    found = int(text)
    if not 1 <= found <= LOG_MOST:
        raise ValueError("a limit is from one to LOG_MOST")
    return found


def _from_a_page(request: Request) -> bool:
    """Made by a web page: what ``from_browser`` says (an ``Origin``, a cross-site or same-site fetch), and a fetch from
    the server's own pages too (``sec-fetch-site: same-origin``), or any request a browser makes. Browsers mark every
    request with ``sec-fetch-site``; a program (E0c's commands, a script) does not, and the administration is a
    program's."""
    return from_browser(request.scope) or "sec-fetch-site" in request.headers


def _identity_of(request: Request) -> str | None:
    """The identity a call's path names, when it is an id of the server's own shape: what its entry records as its
    target. Any other text is no one's, and is recorded as nothing (it may be a secret pasted in the wrong place)."""
    given = request.path_params.get("id")
    return given if isinstance(given, str) and IDENTITY_ID.fullmatch(given) else None


@dataclass
class _Call:
    """One administration call, for the entry that records it: who made it (``unknown`` until its token is known),
    from where, on what, what it did (None: nothing, and it is recorded under the call's own name) and its details."""

    name: str
    address: str = field(repr=False)  # printed, a call names neither where it came from nor what it holds
    target: str | None = None
    actor: str = UNKNOWN
    token: str | None = None  # the id of the administration token it is made with, once known
    done: str | None = None
    details: dict[str, Any] = field(default_factory=dict, repr=False)
    recorded: bool = False  # written with its change, in one transaction
    quiet: bool = False  # said by the entry of its window already
    # Refused because the credentials' threads were busy (``machines.CREDENTIALS_BUSY``), before its address took a
    # slot: counted into the entry of those refusals (``Refusals.busy``), whatever the queue is by the time it is
    # recorded.
    shed: bool = False
    # Refused before its address took a slot because Valkey did not answer (said once a minute): written, when it is,
    # without asking the threads for the server's ceiling of entries.
    uncharged: bool = False


@dataclass(eq=False)
class _Turn:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0  # holding it or waiting for it


class _Turns:
    """One turn at a time for each key (an identity's id), on the event loop: the others wait for it there, holding no
    thread. A key's entry goes once no one holds or waits for its turn, so that what is kept is bounded by the turns
    under way, never by every identity ever saved."""

    def __init__(self) -> None:
        self._turns: dict[str, _Turn] = {}

    @contextlib.asynccontextmanager
    async def turn(self, key: str) -> AsyncIterator[None]:
        held = self._turns.setdefault(key, _Turn())
        held.users += 1
        try:
            async with held.lock:
                yield
        finally:
            held.users -= 1
            if not held.users:
                del self._turns[key]

    def __len__(self) -> int:
        return len(self._turns)


class _Stale(Exception):
    """An API key's settings changed since the etag given was read."""


class _HoldLost(Exception):
    """The transaction that held the call's token's row through an API key's settings save is gone (PostgreSQL ended it
    past ``HELD_IDLE_SECONDS``, and a revocation of the token may have gone on meanwhile): the approval is not kept."""


class _Unapproved(Exception):
    """An API key's settings applied, their own part kept, and their approval refused: ``refused`` is the store's error
    (an OSError naming its type alone), ``keys`` what the settings changed."""

    def __init__(self, refused: OSError, keys: list[str]) -> None:
        super().__init__("the settings were applied, and their approval could not be kept")
        self.refused, self.keys = refused, keys


Handler = Callable[[Request, _Call], Awaitable[Response]]


def _key_settings(
    runtime: Runtime,
    config: Config,
    etag: str | None,
    entry: Callable[[Any, list[str]], None],
    still: Callable[[], contextlib.AbstractContextManager[Callable[[], None]]],
) -> tuple[Config, Config, bool]:
    """In a worker thread: ``config`` applied as an API key's settings and approved, under its runtime's lock from the
    etag's check to the approval, so that no other change lands between them (R-24 (1)). An API key's settings are
    approved by the administration that sets them (Task 12): an approval that cannot be kept is raised, never only said
    in the log. The change's ``entry`` (given the transaction's connection and the keys changed) is written last in the
    transaction of the snapshot that approves it: the one E0a's apply writes, or the one written here when that one
    failed (the apply says it in the log and goes on). ``still`` holds the call's token's row from before the settings
    are applied, under the lock, until the approval has committed (a revocation meanwhile waits for both), and raises
    AdminTokenEnded, nothing applied, when the token is gone or past its day then; what it gives says, before the
    approval's entry, whether that row is held still (``_HoldLost`` otherwise: a save that hung past the held
    transaction's idle timeout, which a revocation of the token may have passed, is not approved). The settings before
    and after, and whether that entry was kept. Raises AdminTokenEnded, _Stale, SharedItemsError, SettingsRefused,
    _Unapproved when the approval cannot be kept, and what the store raises."""
    settings = runtime.settings
    with runtime._lock, still() as held:  # the token's row first, held until the approval below has committed
        before, written = runtime.config, []

        def change(current: Config) -> Config:
            if etag is not None and not _same(etag, digest(current)):
                raise _Stale
            return config

        def recording(conn: Any) -> None:
            if keys := changed_keys(before, runtime.config):  # the settings it approves are in force already
                held()  # the token's row held still, or the approval's transaction rolls back (``_HoldLost``)
                entry(conn, keys)
                written.append(True)

        if isinstance(settings, PgSettings):
            settings.recording = recording
        try:
            runtime.update_config(change)
            after = runtime.config
            try:
                settings.save_reviewed(after)
            except OSError as exc:
                raise _Unapproved(exc, changed_keys(before, after)) from None
            # The store drops it once a snapshot written with it is kept: an entry written in a transaction that failed
            # is not one.
            kept = bool(written) and isinstance(settings, PgSettings) and settings.recording is None
        finally:
            if isinstance(settings, PgSettings):
                settings.recording = None
    return before, after, kept


def admin_routes(server: Server) -> list[Route]:
    url = server.settings.url
    https = url.startswith("https://")
    # Said once a minute, with how many since (the beat says the rest): the calls refused because Valkey could not take
    # their slot, the refusals that could not be counted against their address's budget of entries, and those past the
    # server's ceiling of entries.
    refusals = server.refusals
    # The administration tokens found valid in the last hour, here or on another instance (kept in Valkey too).
    known = Known(server.bus, server.master.derive(b"known"), kind="admin")
    # One save of an identity's settings at a time, by its id: the others wait for it on the event loop, holding no
    # thread of the administration's (each would hold one on the identity's lock, and the next administrator's call
    # would find none free).
    saving = _Turns()

    def said(paced: Paced, call: _Call, why: str, exc: Exception) -> None:
        """A call Valkey could not count, recorded and said in the instance's log once a minute, with how many since."""
        count = paced.due()
        call.quiet = not count
        call.details["count"] = count
        if count:
            log.warning(
                "the administration %s: Valkey could not count it for its address (%s)%s",
                why,
                type(exc).__name__,
                f" ({count} times since the line before)" if count > 1 else "",
            )

    async def once_a_window(key: str) -> bool:
        """Whether this is the first time ``key`` is asked in its window: the entry that says it for the window."""
        try:
            return await off_loop(server.bus.allow, key, 1, LOOKUP_WINDOW)
        except Exception:  # not known: recorded, the side of the log
            return True

    async def apart(call: _Call, bucket: str, reason: str, response: Response) -> Response:
        """A call refused before its token is read (a page's, one in clear, one with no administration token): it costs
        no lookup, and never counts toward the limit that guards one, which a page could fill at will and shut an
        administrator's address out with. Its address records the first ``LOOKUP_LIMIT`` of them in a window, then one
        entry for the window (``limited``); past them, as this instance remembers its address so (``machines.Limits``),
        nothing is asked of a thread or of Valkey, and its entry is the window's."""
        call.details = {"reason": reason}
        key = f"admin-refused:{bucket}"
        if server.limits.refuses(key):
            call.details["limited"], call.quiet = True, True
            return response
        try:
            within = await off_loop(counted, server.bus, server.limits, key, LOOKUP_LIMIT, LOOKUP_WINDOW)
        except CredentialsBusy:  # its slot not taken: counted with the calls refused while the threads are busy
            call.shed = True
            return response
        except Exception as exc:  # its words can name Valkey's address: only its type goes
            call.uncharged = True
            said(refusals.uncounted, call, "refused a call", exc)
            return response
        if not within:
            call.details["limited"] = True
            call.quiet = not await once_a_window(f"admin-refused-limited:{bucket}")
        return response

    async def refused(request: Request, call: _Call) -> Response | None:
        """The answer to a call refused before its token is known; None once its token is (``call.actor`` is then its
        administrator). A token is looked up only in a slot its address took first (R-6, as the review's ruling has
        it), and a valid one gives the slot back; but a token found valid in the last hour (``known``, on any instance)
        takes no slot, so that strangers at its holder's address cannot shut it out, and is looked up all the same:
        one revoked or expired meanwhile is refused, forgotten, and counted as any miss (Task 14's review)."""
        bucket = address(request)
        if _from_a_page(request):
            return await apart(call, bucket, "page", _error("browser requests are not accepted here", 403))
        if https and request.url.scheme != "https":
            return await apart(call, bucket, "in clear", _error("the administration answers over HTTPS only", 403))
        given = request.headers.get("x-latet-admin", "")
        if not given.startswith(ADMIN_PREFIX):  # no lookup reads anything for it
            return await apart(call, bucket, "no token", _error("unauthorized", 401))
        miss = miss_key(bucket)  # one budget with the gate's misses and the machines' calls

        def limited() -> Response:
            refused = _error("too many refused calls from this address: wait a minute", 429)
            refused.headers["retry-after"] = RETRY_AFTER
            return refused

        # Past its limit as this instance remembers it: refused here, no thread taken, no round trip, its entry the
        # window's (written when the limit first said so); but a token found valid elsewhere, read in Valkey a batch at
        # a time (``machines.admitted``), which an administrator behind the same address as a flood keeps.
        if (elsewhere := await admitted(known, server.limits, given, miss)) is None:
            call.details, call.quiet = {"reason": "limited"}, True
            return limited()
        found_here = elsewhere or await off_loop(known.holds, given)  # Valkey's, past this instance's memory
        if not found_here:
            try:
                within = await off_loop(counted, server.bus, server.limits, miss, LOOKUP_LIMIT, LOOKUP_WINDOW)
            except CredentialsBusy:
                raise
            except Exception as exc:  # its words can name Valkey's address: only its type goes
                call.details, call.uncharged = {"reason": "unavailable"}, True
                said(refusals.unavailable, call, "refused a call", exc)
                request.scope[REFUSED] = True  # the server's own refusal, which anyone makes at will: paced in the log
                unavailable = _error("the administration cannot answer now: try again", 503)
                unavailable.headers["retry-after"] = QUEUED_RETRY_AFTER  # as the credentials' threads' 503 says it
                return unavailable
            if not within:
                call.details = {"reason": "limited"}
                call.quiet = not await once_a_window(f"admin-limited:{bucket}")
                return limited()
        token_id, expired = await off_loop(server.admin.checked, given)

        def forgotten() -> None:  # valid before, refused now: forgotten, and counted as any miss
            known.forget(given)
            with contextlib.suppress(Exception):
                counted(server.bus, server.limits, miss, LOOKUP_LIMIT, LOOKUP_WINDOW)

        def kept() -> None:
            known.keep(given)
            if not found_here:
                with contextlib.suppress(Exception):  # a slot that is not given back costs its address one miss
                    given_back(server.bus, miss)

        if token_id is None:  # the slot it took stays taken: a miss of its address
            if found_here:
                await off_loop(forgotten)
            if expired:
                call.details = {"reason": "expired"}  # a token past its day: refused, and the log says why
            return _error("unauthorized", 401)
        call.actor, call.token = f"admin:{token_id}", token_id
        await off_loop(kept)
        return None

    async def within_ceiling() -> bool:
        """Whether the entry of a call refused before its token was known, its address charged a slot, is within the
        server's ceiling for the window (``REFUSED_ENTRIES``): one step in Valkey's. When Valkey cannot say, it is: the
        side of the log; when the credentials' threads are too busy to ask it, it is not."""
        try:
            return await off_loop(server.bus.take, "admin-refusals", REFUSED_ENTRIES, LOOKUP_WINDOW)
        except CredentialsBusy:  # counted, and said in the entry of the refusals not written one by one
            return False
        except Exception:  # not known: recorded
            return True

    async def record(call: _Call, status: int, raised: bool) -> bool:
        """The call in the administration log with its status (unless its change's transaction wrote it, or the entry
        of its window says it): whether it is there. One that raised is recorded under its own name, what it did first,
        if anything, in its details. Every entry is written in the administration's own threads, a refusal's too: a
        flood of refused calls holds none of the lookups' threads with the chain's lock. A call refused before its
        token was known is recorded within the server's ceiling for the window; past it, it is counted (``Refusals``),
        the first said at once. No refusal asks the credentials' threads anything to be recorded unless its address
        was charged a slot: one shed while they were busy (``_Call.shed``), whatever the queue is by now, and any while
        they are busy (``machines.CREDENTIALS_BUSY``), are counted (``busy``); one whose slot Valkey did not take
        (``_Call.uncharged``, said once a minute) is written without asking the ceiling."""
        if call.recorded or call.quiet:
            return True
        if call.actor == UNKNOWN:
            if call.shed or credentials_busy():
                if count := refusals.busy.due():
                    await _in_pool(refusals.write, server.audit, count, "busy")
                return True
            if not call.uncharged and not await within_ceiling():
                if count := refusals.unrecorded.due():
                    await _in_pool(refusals.write, server.audit, count, "ceiling")
                return True
        details = {**call.details, "status": status}
        action = call.done or call.name
        if raised and call.done is not None:
            action, details["done"] = call.name, call.done
        entry = (call.actor, action, call.target, details, call.address)
        try:
            await _in_pool(server.audit.record, *entry)
        except Exception as exc:  # by its type: its words can quote what the database was given
            log.error(
                "the administration's %s (%d) could not be recorded in its log (%s): its answer is withheld",
                action,
                status,
                type(exc).__name__,
            )
            return False
        return True

    def admin(name: str, handler: Handler) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            call = _Call(name, client_of(request.scope), _identity_of(request))  # its own address: the bucket counts
            response: Response = _error("internal error", 500)  # until the call answers
            raised = True
            try:
                response = await refused(request, call) or await handler(request, call)
                raised = False
            except CredentialsBusy as busy:  # too many calls wait for the credentials' threads: said there
                call.shed = True  # recorded through none of them, whatever the queue is by then (``record``)
                request.scope[REFUSED] = True  # the server's own refusal, which anyone makes at will: paced in the log
                response = _error("the administration cannot answer now: try again", 503)
                response.headers["retry-after"] = busy.retry_after or RETRY_AFTER
                raised = False
            except AdminTokenEnded as gone:  # its token revoked or past its day before its change committed: none made
                call.details = {"reason": gone.reason}  # under the call's own name, with the token's actor
                response = _error("unauthorized", 401)
                raised = False
            except Exception as exc:  # by its type: its words can quote what was sent
                log.error("the administration's %s failed (%s)", name, type(exc).__name__)
            finally:  # every call is recorded, one that raised too (R-24 (2))
                kept = await record(call, response.status_code, raised)
            if not kept:
                response = _error(UNRECORDED, 500)
            response.headers.update(_HEADERS)
            return response

        return endpoint

    def holding(call: _Call, conn: Any) -> AdminTokenInfo:
        """In a worker thread, first in the transaction that makes the call's change, before any other row is touched:
        the token the call was made with, still there, its tag holding and its day not past, its row locked until that
        transaction ends (``AdminTokens.held``), so that a revocation on any instance waits for the change or finds it
        made. Taken before any other row, so that a call waiting for its token's row holds nothing another waits for.
        Raises AdminTokenEnded otherwise: the transaction rolls back, nothing changed or told."""
        info = server.admin.held(call.token, conn=conn) if call.token is not None else None
        reason = server.admin.ended(info)
        if info is None or reason is not None:
            raise AdminTokenEnded(reason or "revoked")
        return info

    @contextlib.contextmanager
    def still(call: _Call) -> Iterator[Callable[[], None]]:
        """``holding`` in a transaction of its own, held open while the block runs: for an API key's settings, applied
        and approved in transactions of their own, so that the token's row stays locked from the check (made before
        anything is applied, once their turn and their runtime's lock are held) until the approval has committed. That
        transaction sits idle meanwhile, while its thread works on other connections: no other row is taken in it, and
        its idle timeout is ``HELD_IDLE_SECONDS`` until it ends, whatever the role's, far past what a save takes: a save
        that hangs past it has its transaction ended by PostgreSQL, and a revocation of its token waits no longer. What
        it gives the block raises ``_HoldLost`` when that transaction is gone: asked before the approval's entry, so
        that a revoked token's settings are never approved. The inner settings transactions do not reacquire the token
        row; expiry is judged at this initial check. Other writes take token rows before their other rows, so none
        waits on it holding what this work waits for."""
        with server.db.transaction() as conn:
            conn.execute(HELD_IDLE, (str(HELD_IDLE_SECONDS * 1000),))
            holding(call, conn)

            def held() -> None:
                try:
                    conn.execute("SELECT 1")  # in the held transaction: an error once PostgreSQL has ended it
                except Exception:  # its connection closed, or the transaction aborted: no row is held either way
                    raise _HoldLost() from None

            yield held

    def entry(
        call: _Call,
        changes: Changes,
        done: str,
        details: dict[str, Any],
        *,
        target: str | None = None,
        status: int = 200,
    ) -> None:
        """In a worker thread, last in the change's own transaction, which took the call's token's row first: the token
        checked again there (``holding``: its row is this transaction's already, its day judged now), then the call's
        entry, so that the change and the entry that records it are kept together or not at all (H2), and only while
        the token it was made with serves."""
        holding(call, changes.conn)
        record = (call.actor, done, target or call.target, {**details, "status": status}, call.address)
        server.audit.record(*record, conn=changes.conn)

    def recorded(call: _Call, done: str, details: dict[str, Any], target: str | None = None) -> None:
        """In the worker thread, once the change's transaction has committed with its entry: the call is recorded, and
        its end writes no other entry. Set there, not once the event loop hears of it, so that a call cancelled
        meanwhile (the server stopping) is not recorded again when its change ended first."""
        call.done, call.details, call.recorded = done, details, True
        if target is not None:
            call.target = target

    async def found(call: _Call) -> Identity | None:
        """The identity the call names, as PostgreSQL has it now: read past the instance's memory, and touching none of
        it (N3). None for an id the server never made, and for one that is not there."""
        if call.target is None:
            return None
        return await _in_pool(server.identities.current, call.target)

    async def ended(identity_id: str, *, removed: bool = False) -> None:
        """The identity's space on this instance closed (Ruling 22: disabling, removing, revoking a token and rotating a
        key close it), and its dashboard sessions and codes and its AWS sessions ended, before the call answers,
        whatever its change counted: the broadcast that does it on every instance comes later, only for a row that
        changed, and may be lost (the beat's check finds it then, at its next round). When Valkey fails, the server's
        clean-up tries again at each beat. ``removed``: every key Valkey keeps for it goes too, once (R-21). The call
        waits for its space here to close (``spaces.CLOSE_SECONDS`` at most, what its recorder holds written first):
        kept so after Task 15's re-review asked whether the wait is worth it, since the instance that acts answers only
        once nothing of the identity runs on it; the other instances close theirs when the broadcast reaches them."""
        await server.spaces.drop(identity_id)  # made again, or never, at its next use
        try:
            await _in_pool(server._ended, identity_id)
        except Exception as exc:  # its words can name Valkey's address: only its type goes
            log.warning(
                "what %s kept in Valkey could not all be ended at once (%s): the beat tries again",
                identity_id,
                type(exc).__name__,
            )
            server._clean(identity_id, removed=removed)
            return
        if removed:
            server._clean(identity_id, removed=True)

    # --- people and API keys ------------------------------------------------------------------------------------------

    async def list_identities(request: Request, call: _Call) -> Response:
        kind = request.query_params.get("kind") or None
        if kind not in (None, "person", "key"):
            return _error("kind is person or key", 400)

        def listed() -> tuple[list[Identity], dict[str, tuple[int, int]]]:
            return server.identities.all(kind), server.identities.token_counts()  # a person's computers

        identities, counts = await _in_pool(listed)
        # Every token's row is counted; those whose tag does not hold (made or changed without the server's key, and
        # finding no one) are counted apart too, never hidden.
        found_ = [
            asdict(i) | dict(zip(("tokens", "tampered_tokens"), counts.get(i.id, (0, 0)), strict=True))
            for i in identities
        ]
        call.details = {"count": len(found_)}
        return JSONResponse({"identities": found_})

    async def add_identity(request: Request, call: _Call) -> Response:
        given = await _object(request)
        if given is None or not set(given) <= {"kind", "name", "email"}:
            return _error("a JSON object: kind (person or key), name, and a person's email if any", 400)
        kind, name, email = given.get("kind"), given.get("name"), given.get("email")
        if kind not in ("person", "key"):
            return _error("kind is person or key", 400)
        if not isinstance(name, str) or not clean(name, NAME_LENGTH):  # N4: the store's own cleaning
            return _error("name is a text, with something left once its invisible characters are taken out", 400)
        if email is not None and (not isinstance(email, str) or kind == "key"):
            return _error("email is a text, and a person's only", 400)

        done = f"{kind}.added"

        def added() -> tuple[Identity, str] | EmailTaken:
            with server.identities.changing() as changes:
                holding(call, changes.conn)  # the token's row first, before any other
                if kind == "person":
                    try:
                        made, secret = server.identities.add_person(name, email or None, within=changes)
                    except EmailTaken as taken:  # nothing made: the refusal is recorded as the call ends
                        return taken
                else:
                    made, secret = server.identities.add_key(name, within=changes)
                entry(call, changes, done, {}, target=made.id, status=201)
            recorded(call, done, {}, made.id)
            return made, secret

        result = await _in_pool(added)
        if isinstance(result, EmailTaken):
            return _error(str(result), 409)
        made, secret = result
        if kind == "person":
            return JSONResponse({"identity": asdict(made), "code": secret}, status_code=201)
        return JSONResponse({"identity": asdict(made), "key": secret, **routes_of(url, secret)}, status_code=201)

    async def get_identity(request: Request, call: _Call) -> Response:
        if (identity := await found(call)) is None:
            return _missing()
        tokens = await _in_pool(server.identities.tokens, identity.id)
        return JSONResponse({"identity": asdict(identity), "tokens": [asdict(t) for t in tokens]})

    async def change_identity(request: Request, call: _Call) -> Response:
        if (identity := await found(call)) is None:
            return _missing()
        given = await _object(request)
        if given is None or not set(given) <= {"name", "email", "disabled"}:
            return _error("a JSON object: name, email or disabled", 400)
        name, email, disabled = given.get("name"), given.get("email"), given.get("disabled")
        if name is not None and (not isinstance(name, str) or not clean(name, NAME_LENGTH)):
            return _error("name is a text, with something left once its invisible characters are taken out", 400)
        if email is not None and (not isinstance(email, str) or (identity.kind == "key" and email)):
            return _error("email is a text, and a person's only", 400)
        if disabled is not None and not isinstance(disabled, bool):
            return _error("disabled is true or false", 400)

        def change() -> bool | EmailTaken | None:
            """In one transaction with its entry: whether the change disabled (True) or enabled (False) the identity.
            The fields it names are the ones this change changed, as the store says them, never another administrator's
            at the same moment; a change that changes nothing is recorded as such (R-24 (3)), once it answers. A person
            disabled is disabled before their email changes, and enabled after: the email is checked against the other
            enabled people as it will be (EmailTaken: nothing changed, the refusal recorded as the call ends)."""
            try:
                with server.identities.changing() as changes:
                    holding(call, changes.conn)  # the token's row first, before any other
                    switched = None
                    if disabled is True and server.identities.disable(identity.id, within=changes):
                        switched = True
                    fields: list[str] = []
                    if name is not None or email is not None:
                        _, fields = server.identities.update(identity.id, name=name, email=email, within=changes)
                    if disabled is False and server.identities.enable(identity.id, within=changes):
                        switched = False
                    if switched is not None:
                        fields.append("disabled")
                    details: dict[str, Any] = {"fields": sorted(fields)}
                    done = None
                    if switched is not None:
                        done = f"{identity.kind}.{'disabled' if switched else 'enabled'}"
                    elif fields:
                        done = f"{identity.kind}.updated"
                    if done is not None:
                        entry(call, changes, done, details)
            except EmailTaken as taken:
                return taken
            if done is not None:
                recorded(call, done, details)
            else:
                call.details = details
            return switched

        switched = await _in_pool(change)
        if isinstance(switched, EmailTaken):
            return _error(str(switched), 409)
        if switched:
            await ended(identity.id)  # their dashboards' sessions end with it, on this instance before the answer
        now = await _in_pool(server.identities.current, identity.id)
        if now is None:
            return _missing()
        return JSONResponse({"identity": asdict(now)})

    async def remove_identity(request: Request, call: _Call) -> Response:
        if (identity := await found(call)) is None:
            return _missing()
        done = f"{identity.kind}.removed"

        def removed() -> bool:
            with server.identities.changing() as changes:
                holding(call, changes.conn)  # the token's row first, before the cascade's
                if not server.identities.remove(identity.id, within=changes):  # its space too: the database cascades
                    return False
                entry(call, changes, done, {})
            recorded(call, done, {})
            return True

        if not await _in_pool(removed):
            return _missing()  # removed meanwhile, elsewhere: nothing was done here (N6)
        await ended(identity.id, removed=True)
        return JSONResponse({"ok": True})

    async def list_tokens(request: Request, call: _Call) -> Response:
        if (identity := await found(call)) is None:
            return _missing()
        tokens = await _in_pool(server.identities.tokens, identity.id)
        return JSONResponse({"tokens": [asdict(t) for t in tokens]})

    async def revoke_tokens(request: Request, call: _Call) -> Response:
        if (identity := await found(call)) is None:
            return _missing()
        token = request.query_params.get("token")
        if token is not None and not TOKEN_ID.fullmatch(token):  # N2: never a secret in the log; never "every token"
            return _error("token is a token's id (t_ and ten letters or digits), as GET …/tokens lists them", 400)

        def revoked() -> int:
            with server.identities.changing() as changes:
                holding(call, changes.conn)  # first, whatever the count: a token that ended ends no session either
                count = server.identities.revoke(identity.id, token, within=changes)
                details = {"count": count} | ({"token": token} if token else {})
                if count:
                    entry(call, changes, "token.revoked", details)
            if count:
                recorded(call, "token.revoked", details)
            else:
                call.details = details
            return count

        count = await _in_pool(revoked)
        await ended(identity.id)  # whatever the count (H8)
        return JSONResponse({"revoked": count})

    async def new_code(request: Request, call: _Call) -> Response:
        if (identity := await found(call)) is None:
            return _missing()
        if identity.kind != "person":
            return _error("an API key has no code: rotate it", 409)

        def issued() -> str | None:
            with server.identities.changing() as changes:
                holding(call, changes.conn)  # the token's row first, before any other
                code = server.identities.code(identity.id, within=changes)
                if code is not None:
                    entry(call, changes, "code.issued", {})
            if code is not None:
                recorded(call, "code.issued", {})
            return code

        code = await _in_pool(issued)
        if code is None:
            return _missing()  # removed meanwhile: no code was made (N6)
        return JSONResponse({"code": code})

    async def rotate(request: Request, call: _Call) -> Response:
        if (identity := await found(call)) is None:
            return _missing()
        if identity.kind != "key":
            return _error("a person's tokens are revoked, not rotated", 409)

        def rotated() -> str:
            with server.identities.changing() as changes:
                holding(call, changes.conn)  # the token's row first, before the key's
                secret = server.identities.rotate_key(identity.id, within=changes)
                entry(call, changes, "key.rotated", {})
            recorded(call, "key.rotated", {})
            return secret

        try:
            secret = await _in_pool(rotated)
        except NoSuchIdentity:
            return _missing()  # removed meanwhile
        await ended(identity.id)  # whatever the broadcast does (H8)
        return JSONResponse({"key": secret, **routes_of(url, secret)})

    # --- an API key's settings ----------------------------------------------------------------------------------------

    async def a_key(call: _Call) -> Identity | Response:
        if (identity := await found(call)) is None:
            return _missing()
        if identity.kind != "key":
            return _error("a person's settings are theirs: they change them in their dashboard", 409)
        if identity.disabled:
            return _error("this API key is disabled: enable it to read or change its settings", 409)
        return identity

    async def space_of(identity: Identity) -> Runtime | Response:
        """The API key's runtime on this instance, built at its first use."""
        try:
            return await server.spaces.runtime(identity)
        except IdentityUnavailable:
            return _missing()  # removed or disabled meanwhile
        except SpaceUnavailable:  # this instance does not hear Valkey now, or the space closed while it was built
            return _error("this instance cannot open the key's space now: try again", 503)

    async def get_settings(request: Request, call: _Call) -> Response:
        if isinstance(identity := await a_key(call), Response):
            return identity
        if isinstance(runtime := await space_of(identity), Response):
            return runtime
        return await _in_pool(_settings_answer, runtime.config)

    async def put_settings(request: Request, call: _Call) -> Response:
        if isinstance(identity := await a_key(call), Response):
            return identity
        given = await _object(request)
        if given is None:
            return _error(NOT_SETTINGS, 400)
        etag = given.pop("etag", None)  # the settings' keys, and the etag of what was read
        if etag is not None and not isinstance(etag, str):
            return _error("etag is the text GET answered", 400)
        if isinstance(config := await _in_pool(_settings_of, given), list):
            return _error("these are not settings", 400, details=config)
        if no_setting := _no_setting(given):
            return _error(no_setting, 400)
        if named := reviewer_key_named(config):  # the coordinator's ruling on the review
            details = [f"{name}: {REVIEWER_KEY}" for name in named]
            return _error("these settings name a file or a variable of the server's", 400, details=details)

        def refused_destinations() -> list[tuple[int, str]]:
            # Task 11's check, the dashboard's: names resolved in the egress threads for a bounded time (H4).
            shared = server.shared.get()
            return own_destinations_refused(shared, config, resolve=egress.bounded(identity.id), server=url)

        if refusals := await _in_pool(refused_destinations):
            details = [f"upstreams.{i}: {problem}" for i, problem in refusals]
            return _error("these destinations cannot be an API key's own", 400, details=details)

        def changed(conn: Any, keys: list[str]) -> None:
            """The change's entry, last in the transaction that keeps its approval (H2)."""
            details: dict[str, Any] = {"keys": keys, "status": 200}
            server.audit.record(call.actor, "settings.changed", call.target, details, call.address, conn=conn)

        async with saving.turn(identity.id):  # the space read in its turn: the save before may have closed it
            if isinstance(runtime := await space_of(identity), Response):
                return runtime
            # What the write names, over the settings in use (which GET answered): a key it leaves out keeps its value.
            try:
                whole = await _in_pool(_merged, runtime.config, given, Config.model_fields)
            except ValidationError as exc:
                return _error("these are not settings", 400, details=wrong_fields(exc))
            try:
                serving = functools.partial(still, call)  # its token's row, held from the check to the approval
                before, after, written = await _in_pool(_key_settings, runtime, whole, etag, changed, serving)
            except _Stale:
                return _error(STALE, 409)
            except SettingsRefused as exc:  # too many projects, a term too long: the request's own, not a conflict
                return _error(str(exc), 400)
            except SharedItemsError as exc:  # its words say how many of each kind, never which
                return _error(str(exc), 409)
            except (OSError, _Unapproved) as failure:
                # Not kept, stale ones too (H7): E0a's apply puts settings in force before it saves them, so the space
                # here, which runs them already, is closed before the answer, and made again from PostgreSQL at its
                # next use. Applied, with their own part kept, and not approved: the entry says so, and what changed.
                exc = failure
                if isinstance(failure, _Unapproved):
                    exc, call.details = failure.refused, {"keys": failure.keys, "approved": False}
                await server.spaces.drop(identity.id)
                if isinstance(exc, SettingsStale):  # another instance wrote them since this one read them
                    return _error(STALE, 409)
                if isinstance(exc, SettingsTooLarge):
                    return _error("these settings weigh more than a server keeps for an API key", 413)
                log.warning("the settings of %s could not be kept (%s)", identity.id, type(exc).__name__)
                return _error("these settings could not be kept now: try again", 503)
        keys = changed_keys(before, after)
        call.details = {"keys": keys}
        if keys:
            call.done, call.recorded = "settings.changed", written
        return await _in_pool(_settings_answer, after)

    # --- the shared settings ------------------------------------------------------------------------------------------

    async def get_shared(request: Request, call: _Call) -> Response:
        def answered() -> Response:
            try:
                config = server.shared.stored()  # as PostgreSQL has them: never a copy left behind by a lost message
            except UnreadableShared:
                return _error(UNREADABLE_SHARED, 500)
            return _shared_answer(config, server.shared.rules)

        return await _in_pool(answered)

    def write_shared(given: dict[str, Any], etag: str | None, call: _Call, forget: bool = False) -> Config | None:
        """In a worker thread: ``config`` kept as the shared settings with its entry, in one transaction (H2), the etag
        compared inside it under the lock every change takes (``SharedSettings.set``, H1), then announced. False when
        the etag given is not theirs; nothing is written when they are ``config`` already. Written blind, it compares
        what it read as PostgreSQL has them, and reads them again when another change landed meanwhile: what its entry
        says it changed is exactly that. Written blind over a row that does not open, it replaces it, nothing of it
        read, and its entry says so (``unreadable``); with an etag such a row raises UnreadableShared. Raises
        SharedTooLarge, nothing written, and AdminTokenEnded when the call's token is gone or past its day as the
        transaction finds it (``holding``, first there). What is written is the settings ``given`` names over the row's
        (``_merged``: a setting it leaves out keeps its value; over a row that does not open, its default), with the
        rules for everyone an earlier version kept in the row (``with_kept_rules``), unless ``forget``. Answers the row
        written, or None when the etag given is not theirs; raises ValidationError when they are no settings
        together."""
        for _ in range(_TRIES):
            try:
                before: Config | None = server.shared.stored()
            except UnreadableShared:
                if etag is not None:
                    raise
                before = None
            read: str | None = None
            details: dict[str, Any] = {"unreadable": True}
            written = with_kept_rules(_merged(before, given, SETTING_KEYS), None if forget else before)
            if before is not None:
                read = digest(before)
                if etag is not None and not _same(etag, read):
                    return None
                if not (keys := changed_keys(before, written)):
                    return before
                details = {"keys": keys}
            try:
                with server.db.transaction() as conn:
                    holding(call, conn)  # first: a token gone or past its day changes nothing
                    counter = server.shared.set(
                        written, actor=call.actor, etag=read, conn=conn, unreadable=before is None
                    )
                    entry = (call.actor, "shared.changed", None, {**details, "status": 200}, call.address)
                    server.audit.record(*entry, conn=conn)
            except StaleSettings:
                if etag is not None:
                    return None
                continue
            call.done, call.details, call.recorded = "shared.changed", details, True
            server.shared.announce(counter)
            return written
        return None

    async def put_shared(request: Request, call: _Call) -> Response:
        given = await _object(request)
        if given is None:
            return _error(NOT_SETTINGS, 400)
        etag = given.pop("etag", None)  # the settings' keys, and the etag of what was read
        if etag is not None and not isinstance(etag, str):
            return _error("etag is the text GET answered", 400)
        forget = given.pop("forget_kept_rules", False)
        if forget is not True and forget is not False:
            return _error(FORGET_KEPT, 400)
        if rules := sorted(key for key in given if key in RULE_KEYS):  # the Enterprise Edition's: named, never shown
            return _error(RULES_FOR_EVERYONE, 400, details=rules)
        if isinstance(config := await _in_pool(_settings_of, given), list):
            return _error("these are not settings", 400, details=config)
        if no_setting := _no_setting(given):
            return _error(no_setting, 400)
        if refusals := shared_refusals(config):
            return _error("the shared settings cannot hold these", 400, details=refusals)
        try:
            written = await _in_pool(write_shared, given, etag, call, forget)
        except UnreadableShared:  # an etag can be no etag of theirs: read, they cannot be
            return _error(UNREADABLE_SHARED, 409)
        except ValidationError as exc:  # what it names, over the settings kept, are no settings together
            return _error("these are not settings", 400, details=wrong_fields(exc))
        except SharedTooLarge as exc:
            return _error(str(exc), 413)
        if written is None:
            return _error(STALE, 409)
        if not call.recorded:
            call.details = {"keys": []}  # they were these already
        return await _in_pool(_shared_answer, written, server.shared.rules)

    # --- the administration tokens ------------------------------------------------------------------------------------

    async def list_admin_tokens(request: Request, call: _Call) -> Response:
        def answered() -> tuple[Response, int, int]:
            return _tokens_answer(server.admin.listed(), call.token)

        answer, count, tampered = await _in_pool(answered)
        call.details = {"count": count, "tampered": tampered}
        return answer

    async def issue_admin_token(request: Request, call: _Call) -> Response:
        raw = await _within(request, ENROL_BYTES)  # a label and a choice: past this, refused unparsed
        if raw is None:
            return _error(f"a new administration token is asked for in {ENROL_BYTES} bytes at most", 413)
        if isinstance(asked := await _in_pool(_token_asked, raw), str):
            return _error(asked, 400)
        label, days, never = asked

        def issued() -> tuple[str, str, float | None] | float:
            """In one transaction: the caller's token read first and held until it commits (``holding``), the new one
            made, ending no later than the caller, and the entry that records it, last (H2). The caller's end when
            what is asked would end after it: nothing made."""
            with server.db.transaction() as conn:
                mine = holding(call, conn)
                try:
                    made = server.admin.issued(
                        label, days=days, never=never, by=mine.id, not_after=mine.expires, conn=conn
                    )
                except OutlivesIssuer as outlives:
                    return outlives.not_after
                token_id, _, expires = made
                details: dict[str, Any] = {"days": days} if days else {"never": True} if never else {}
                if not days and not never and expires is not None and expires == mine.expires:
                    details["cut"] = True  # the server's default, cut at the caller's end
                entry = (call.actor, "admin-token.issued", token_id, {**details, "status": 201}, call.address)
                server.audit.record(*entry, conn=conn)  # its id and how long it lasts: never its label
            recorded(call, "admin-token.issued", details, token_id)
            return made

        made = await _in_pool(issued)
        if not isinstance(made, tuple):  # the caller's end
            return _error(f"{OUTLIVES_ISSUER}, and the one this call is made with ends on {_in_utc(made)}", 403)
        token_id, secret, expires = made
        return JSONResponse({"id": token_id, "token": secret, "expires": expires}, status_code=201)  # shown once

    async def revoke_admin_token(request: Request, call: _Call) -> Response:
        given = request.path_params.get("token")
        named = given if isinstance(given, str) and ADMIN_TOKEN_ID.fullmatch(given) else None
        if named is None:  # never recorded: a text that is no id of the server's may be a token pasted in its place
            return _error("there is no such administration token", 404)

        def revoked(target: str) -> bool:
            """In one transaction: the row ``target`` names (its own id, or the listed id of a row written by hand with
            an id of another shape), then the caller's row and that row locked in the order of their ids, so that two
            tokens revoking each other wait rather than deadlock, the caller's checked (``holding``'s rule), the row
            deleted, whether its tag holds or not, and the entry that records it by ``target``, last (H2). Whether it
            was there."""
            caller = call.token
            if caller is None:
                raise AdminTokenEnded("revoked")
            with server.db.transaction() as conn:
                row = server.admin.row_named(target, conn=conn)  # read, nothing locked yet
                named = {caller} if row is None else {caller, row}
                rows = {one: server.admin.held(one, conn=conn, update=True) for one in sorted(named)}
                if (reason := server.admin.ended(rows[caller])) is not None:
                    raise AdminTokenEnded(reason)
                if row is None or rows[row] is None or not server.admin.revoke(row, conn=conn):
                    return False
                entry = (call.actor, "admin-token.revoked", target, {"status": 200}, call.address)
                server.audit.record(*entry, conn=conn)
            recorded(call, "admin-token.revoked", {}, target)
            return True

        if not await _in_pool(revoked, named):
            return _error("there is no such administration token", 404)
        return JSONResponse({"revoked": True})

    # --- the log and the state ----------------------------------------------------------------------------------------

    async def read_log(request: Request, call: _Call) -> Response:
        query = request.query_params
        try:
            if any(len(query.getlist(field)) > 1 for field in ("since", "until", "limit", "action", "actor", "cursor")):
                raise ValueError
            filters = LogFilters(
                finite_time(query.get("since")),
                finite_time(query.get("until")),
                query.get("action") or None,
                query.get("actor") or None,
            )
            limit = page_limit(query.get("limit"), LOG_LIMIT)
            token = query.get("cursor")
            cursor = None if token is None else LogCursor.decode(token, filters)
        except ValueError:
            request.scope[REFUSED] = True
            return _error("invalid administration log query or cursor", 400)
        try:
            body, count = await _in_pool(server.audit.read_page, filters, cursor, limit)
        except LogReadRefused as refused:
            request.scope[REFUSED] = True
            call.details = {"reason": refused.reason}
            return _error(refused.message, 422, reason=refused.reason)
        except (TimeoutError, OperationalError, QueryCanceled):
            request.scope[REFUSED] = True
            response = _error("the administration log cannot be read now: try again", 503)
            response.headers["retry-after"] = RETRY_AFTER
            return response
        call.details = {"count": count}
        return Response(body, media_type="application/json")

    async def verify_log(request: Request, call: _Call) -> Response:
        try:
            checked = await _in_pool(server.audit.verify)
        except Exception as exc:  # a walk that fails is a failed check, never an unchecked one (Task 6)
            log.warning("the administration log could not be walked (%s)", type(exc).__name__)
            call.details = {"ok": False, "checked": 0}
            return JSONResponse({"ok": False, "checked": 0, "broken_at": None, "head": None})
        call.details = {"ok": checked.ok, "checked": checked.checked}
        # The newest entry walked: what a copy kept elsewhere (E4's export) is compared with, since nothing in the table
        # shows entries taken off its end.
        head = None if checked.head is None else {"id": checked.head[0], "hash": checked.head[1]}
        answer = {"ok": checked.ok, "checked": checked.checked, "broken_at": checked.broken_at, "head": head}
        return JSONResponse(answer)

    def _kept_rules_line() -> list[dict[str, str]]:
        """In a worker thread: a line on the rules for everyone an earlier version kept in the shared settings, when it
        kept any: by their counts and names, never a value."""
        try:
            kept = server.shared.kept()
        except UnreadableShared:  # said where the shared settings are read
            return []
        if not kept:
            return []
        detail = (
            f"the shared settings keep rules for everyone from an earlier version ({kept_said(kept)}), applied by no "
            "space: API keys run without them, and each person keeps what protected her until she decides in her "
            "dashboard. `latet server rules export` writes them to a file, `latet server rules forget` deletes them"
        )
        return [{"status": "warn", "title": "Rules for everyone", "detail": detail}]

    async def status(request: Request, call: _Call) -> Response:
        limit = server.settings.vault_limit

        def state() -> dict[str, Any]:
            try:  # the beat's hourly verdict, sealed: ok or broken while fresh (never an older one), else unknown
                chain = server.chain.read()
            except Exception:  # Valkey does not answer: no verdict is known
                chain = "unknown"
            return {
                "identities": {
                    "people": len(server.identities.all("person")),
                    "keys": len(server.identities.all("key")),
                    "enabled": server.identities.enabled_count(),
                },
                "vault": {"limit": limit, "near": [{"id": i, "values": n} for i, n in vault_usage(server.db, limit)]},
                "audit": {"chain": chain},
                "database": server.db.ping(),
                "valkey": server.bus.ping(),
                "kept": _kept_rules_line(),
            }

        found_ = await _in_pool(state)
        answer = {
            "url": url, "version": __version__, "instance": server.instance, "identities": found_["identities"],
            "spaces": server.spaces.count(), "vault": found_["vault"],  # the identities at 90% of the limit or more
            "audit": found_["audit"], "database": found_["database"], "valkey": found_["valkey"],
            "edition": found_["kept"],  # E1: and the lines of Edition.checks()
        }  # fmt: skip
        return JSONResponse(answer)

    one = f"{ADMIN_API}/identities/{{id}}"
    return [
        Route(f"{ADMIN_API}/identities", admin("identities.list", list_identities), methods=["GET"]),
        Route(f"{ADMIN_API}/identities", admin("identity.add", add_identity), methods=["POST"]),
        Route(one, admin("identity.read", get_identity), methods=["GET"]),
        Route(one, admin("identity.change", change_identity), methods=["PATCH"]),
        Route(one, admin("identity.remove", remove_identity), methods=["DELETE"]),
        Route(f"{one}/tokens", admin("tokens.list", list_tokens), methods=["GET"]),
        Route(f"{one}/tokens", admin("tokens.revoke", revoke_tokens), methods=["DELETE"]),
        Route(f"{one}/code", admin("code.issue", new_code), methods=["POST"]),
        Route(f"{one}/rotate", admin("key.rotate", rotate), methods=["POST"]),
        Route(f"{one}/settings", admin("settings.read", get_settings), methods=["GET"]),
        Route(f"{one}/settings", admin("settings.change", put_settings), methods=["PUT"]),
        Route(f"{ADMIN_API}/shared", admin("shared.read", get_shared), methods=["GET"]),
        Route(f"{ADMIN_API}/shared", admin("shared.change", put_shared), methods=["PUT"]),
        Route(f"{ADMIN_API}/log", admin("log.read", read_log), methods=["GET"]),
        Route(f"{ADMIN_API}/log/verify", admin("log.verify", verify_log), methods=["GET"]),
        Route(f"{ADMIN_API}/status", admin("status.read", status), methods=["GET"]),
        Route(f"{ADMIN_API}/admin-tokens", admin("admin-tokens.list", list_admin_tokens), methods=["GET"]),
        Route(f"{ADMIN_API}/admin-tokens", admin("admin-token.issue", issue_admin_token), methods=["POST"]),
        # Named ``token``, not ``id``: the identity a call names (``_identity_of``) is read from ``id`` alone.
        Route(
            f"{ADMIN_API}/admin-tokens/{{token}}", admin("admin-token.revoke", revoke_admin_token), methods=["DELETE"]
        ),
    ]
