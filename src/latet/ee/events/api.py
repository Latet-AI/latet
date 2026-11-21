"""The routes of Reports, the administration log and the SIEM outputs (E4), served twice as E3's are (ee/routes.py):
to a server's dashboard (/_latet/api/v1/ee/…, the person signed in, behind E3's gate and its rows of RULES, by role: R3)
and to its administration API (/_latet/server/v1/admin/ee/…, `latet server reports|siem`, behind E0b's one gate, which
checks the administration token and records each call). One handler for both, ``(request, who)``.

The license is weighed as E1's gates weigh it (R2): every route is ``manage``, a read answering in every standing of a
license that lists its feature, a change only while the license can manage. What produces something new (a report, an
export, the administration log's page, a test of an output) also asks whether the license produces: 403
``not-producing`` otherwise, with why (``paused``). A change, and an export, is one transaction (``who.change``): the
license, the person's roles and their session weighed there (decision (c): an export is a company-wide act, R4), the
settings' and the secrets' rows written there, the entries last (on the administration API the last is the call's own).
An export and a test weigh the person first, in a transaction with nothing to write (M15), before anything is read or
reached: a role taken away, or a session the company's sign-in did not open, costs the server nothing. Paths take no
parameter: an output is named in the body. No answer holds a secret, and no refusal quotes what the request sent.
Everything that reads a store runs in the request's threads (``who.run``)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import ssl
import threading
import weakref
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from latet.ee.events.delivery import MARGIN
from latet.ee.events.locks import Lease
from latet.ee.events.reports import (
    Filters,
    Names,
    NotAllowed,
    Period,
    ReportError,
    TooLarge,
    Viewer,
    audit_file,
    audit_rows,
    build_report,
    export_file,
    report_viewer,
)
from latet.ee.events.siem import SecretElsewhere, secret_of, send_test
from latet.ee.events.store import EventStore, QueueState, SiemQueue
from latet.ee.license import MANAGEABLE, Standing, State
from latet.ee.people import GROUP_ID
from latet.ee.roles import Role, can
from latet.ee.routes import EeRoute, Refused, Who, json_body
from latet.ee.server import DatabaseSecrets, DatabaseSettings, settings_in
from latet.ee.settings import (
    SIEM_SECRETS,
    EnterpriseSettings,
    EnterpriseSettingsError,
    ReportSettings,
    SiemOutput,
    SiemSettings,
    _field_names,
    siem_bound,
    siem_problem,
)
from latet.server.audit import PAGE as LOG_PAGE  # entries the log gives at once, by id

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send

    from latet.ee.access import Access
    from latet.ee.edition import EnterpriseEdition
    from latet.ee.people import Changing, People
    from latet.server.app import Server

log = logging.getLogger(__name__)

REPORTS: Final = "reports"  # the license's feature of the reports and of the administration log's page
SIEM: Final = "siem"
AUDIT_LIMIT: Final = 5_000  # entries of the administration log a page or a report's JSON export holds at most
AUDIT_EXPORT_MOST: Final = 100_000  # entries the log's own export holds at most: past them, 413, never cut (I11)
# I2: tests of an output, each a connection the server makes where an output says: one an output every 10 s and 30 an
# hour, and 30 an hour for each person (or administration token), so that a test is no scanner of the company's network.
TESTS: Final = (("siem-test", 1, 10.0), ("siem-tests", 30, 3600.0))
TESTS_BY_WHOM: Final = ("siem-tests-by", 30, 3600.0)
DESTINATIONS: Final = ("siem-destinations", 10, 3600.0)  # R2-4: outputs added or moved from the dashboard, a person's
MOST_SECRET: Final = 4096  # characters of an output's secret: a token or a key is a few dozen
MOST_ETAG: Final = 128
MOST_FILTER: Final = 200  # characters of an action or an actor the administration log is filtered by
SKIP_SECONDS: Final = 60.0  # the output's lease a skip holds, as long as a delivery's (R3-4)
SKEW: Final = 60.0  # seconds an entry's time may run behind an entry of a lower id (taken before the chain's lock)
START_TRIES: Final = 100  # entries read back for one whose keyed hash holds, where the log's export starts (R2-9)
# An export's answer is sent within a minute and a second for each megabyte (a reader taking 1 MB a second): past it
# the sending ends, and what the export took is given back (a reader that never reads holds it no longer).
SEND_BASE: Final = 60.0
SEND_RATE: Final = 1_000_000.0  # bytes a second
NOT_PRODUCING: Final = "not-producing"
HIDDEN: Final = "\N{HORIZONTAL ELLIPSIS}"  # what a reader of the outputs sees in place of a path or a header's value
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")  # an output's name, as SiemOutput takes one
_IDENTITY = re.compile(r"u_[a-z0-9]{1,32}")  # an identity's id, as an event and the log carry one: else never looked up
_BODY_KEYS = frozenset({"outputs", "secrets", "etag"})
_WHAT = {REPORTS: "Reports and exports", SIEM: "Events to the SIEM"}
# Fixed words: a refusal never quotes what the request said (a path, a name, a body's text, a secret).
NO_REPORTS: Final = "reports are for group leads (the groups they belong to), the security role and auditors"
NO_LOG: Final = "the administration log is for auditors, the security role and administrators"
_SIEM_BODY = 'the body is {"outputs": [...], "secrets": {"<output>": "<secret>" or null}, "etag": "..."}'
_AN_ETAG = "etag is the etag of the SIEM outputs the change was made from, as they were shown"
_CHANGED_SINCE = (
    "not changed: the SIEM outputs were changed since they were read here: read them again, then change them"
)
_A_NAME = "name is a SIEM output's name: lowercase letters, digits and hyphens"
_NO_OUTPUT = "there is no SIEM output of that name"
_A_SECRET = f"a secret is one line of printable characters, {MOST_SECRET} at most"
_NO_SECRET = "no secret is kept for this SIEM output"  # noqa: S105 - words
_FORMAT = "format is csv or json"
_A_FILTER = f"action and actor are {MOST_FILTER} characters at most"
_REPORTS_BODY = "the body is {people, retention_days}: either, or both"
_BY_COMMAND = (
    "an output at http:// or at this computer (localhost, 127.0.0.1, [::1]) is set with `latet server siem set`, "
    "never from the dashboard"
)
_ITS_CREDENTIAL_BY_COMMAND = (  # L2 of the whole-branch review: I2 reserves such an output's secret too
    "the secret of an output at http:// or at this computer (localhost, 127.0.0.1, [::1]) is set with "
    "`latet server siem secret`, never from the dashboard"
)
BY_COMMAND_CODE: Final = "by-command"  # what the dashboard says in its own words, in its five languages
_TESTED_BY_COMMAND = (
    "an output at http:// or at this computer (localhost, 127.0.0.1, [::1]) is tested with `latet server siem test`, "
    "never from the dashboard"
)
_UNREACHED = (
    "this SIEM output is at an address the server never reaches now (link-local, reserved, unspecified or multicast, "
    "or an IPv6 form that carries an IPv4 address): set another address"
)
_TOO_MANY_TESTS = "a SIEM output is tested once every 10 seconds and 30 times an hour at most: try again in a moment"
_NO_PACE = "the SIEM tests cannot be counted now: try again in a moment"
_LOG_TOO_LARGE = (
    f"the administration log's export holds {AUDIT_EXPORT_MOST:,} entries at most: narrow the period or the filters"
)
_ONE_EXPORT = "this instance of the server is making another export now: try again in a moment"
_ONE_AT_A_TIME = "a report or an export of yours is still being made: wait for it, then ask again"
_ROLES_CHANGED = "your roles on this server changed while the export was made: it is not given; export again"
_ELSEWHERE = "its secret was set for another address: set it again (`latet server siem secret {name}`)"
_UNREAD_SECRET = "its secret could not be read now ({kind})"  # noqa: S105 - words
_TOO_MANY_DESTINATIONS = (
    "the dashboard adds or moves 10 SIEM destinations an hour at most for each person: try again later, or set them "
    "with `latet server siem set`"
)
_NOT_HELD = "this SIEM output waits at nothing its receiver refused: there is nothing to skip"
_NO_COUNT = "the SIEM destinations added or moved cannot be counted now: try again in a moment"
_SENDING = "another instance of the server is sending this SIEM output's events now: try again in a minute"
_NO_LEASE = "whether another instance sends this SIEM output's events cannot be told now: try again in a moment"


def pause_of(standing: Standing, feature: str) -> dict[str, Any] | None:
    """Why ``feature`` (``reports``, ``siem``) produces nothing now, as ``standing`` says (R2): ``{why, expires,
    error}``; None while it produces (E1's ``Licensing.producing``). ``why``: ``expired`` (past the grace after its last
    day), ``users`` (more people and API keys than bought, past the grace), ``trial-ended``, or ``unusable`` (a file
    that cannot be used, or none). A license that does not list the feature is the gate's 403 before any route of it."""
    lic = standing.license
    if lic is not None and feature in lic.features and standing.state not in (State.PAUSED, State.TRIAL_ENDED):
        return None
    expires = lic.expires.isoformat() if lic is not None else None
    what = _WHAT[feature]
    if lic is not None and standing.state is State.TRIAL_ENDED:
        error = (
            f"{what} stopped when the Latet Enterprise trial ended on {expires}: they start again once a license is "
            "installed; the event log keeps what it counted"
        )
        return {"why": "trial-ended", "expires": expires, "error": error}
    if lic is not None and standing.state is State.PAUSED:
        if standing.over_since is not None and (standing.days_left is None or standing.days_left >= 0):
            error = (
                f"{what} are paused: more people and API keys use Latet than the license covers, for over 30 days. "
                "They start again once a place is freed or the license grows"
            )
            return {"why": "users", "expires": expires, "error": error}
        error = (
            f"{what} are paused: the license expired on {expires} and its 30 days of grace are over. They start again "
            "once it is renewed"
        )
        return {"why": "expired", "expires": expires, "error": error}
    error = (
        f"{what} produce nothing while the license file cannot be used or there is none (`latet server license show`)"
    )
    return {"why": "unusable", "expires": expires, "error": error}


def not_producing(standing: Standing, feature: str) -> Refused | None:
    """The 403 of what produces nothing now, with why (``paused``); None while it produces."""
    pause = pause_of(standing, feature)
    if pause is None:
        return None
    return Refused(403, pause["error"], code=NOT_PRODUCING, paused={"why": pause["why"], "expires": pause["expires"]})


@asynccontextmanager
async def _leased(who: Who, taken: AbstractContextManager[Lease]) -> AsyncIterator[Lease]:
    """A lease of Valkey's held across a request's awaits, taken and given back in the request's threads (``who.run``),
    as the delivery holds the same one while it sends (R3-4); 503 in fixed words when Valkey does not answer."""
    try:
        lease = await who.run(taken.__enter__)
    except Exception as exc:  # by its type (R14): Valkey's words can name its address
        log.warning("a SIEM output's lease could not be taken (%s)", type(exc).__name__)
        raise Refused(503, _NO_LEASE) from None
    try:
        yield lease
    finally:
        try:
            await who.run(taken.__exit__, None, None, None)
        except Exception as exc:  # by its type (R14): what was done is done, and the lease runs out by itself
            log.warning("a SIEM output's lease could not be given back (%s): it runs out by itself", type(exc).__name__)


def _once(release: Callable[[], None]) -> Callable[[], None]:
    """``release``, done the first time it is called alone, whichever thread calls it."""
    done = threading.Lock()

    def once() -> None:
        if done.acquire(blocking=False):
            release()

    return once


def _both(first: Callable[[], None], second: Callable[[], None]) -> Callable[[], None]:
    def both() -> None:
        try:
            first()
        finally:
            second()

    return both


def answer_body(name: str, kind: str, text: str) -> bytes:
    """An export's answer, ``{name, type, file}``, as JSONResponse writes one: made in the request's threads, never on
    the event loop (the file may be hundreds of megabytes)."""

    def encoded(value: str) -> bytes:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")

    return b'{"name":' + encoded(name) + b',"type":' + encoded(kind) + b',"file":' + encoded(text) + b"}"


def send_deadline(size: int) -> float:
    """Seconds an export's answer of ``size`` bytes is given to be sent: a minute, and a second for each megabyte."""
    return SEND_BASE + size / SEND_RATE


class Sent(Response):
    """An export's answer, which holds what it took (the instance's one export, the person's one report) until its body
    is sent, the client is gone, or ``send_deadline`` passes, whose sending then ends (the finally of ``__call__``:
    never a background task, which a client that leaves never runs); one never sent (another answer took its place)
    gives it back once it is dropped."""

    media_type = "application/json"

    def __init__(self, body: bytes, release: Callable[[], None]) -> None:
        super().__init__(body, media_type=self.media_type)
        self._release = release
        weakref.finalize(self, release)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with asyncio.timeout(send_deadline(len(self.body))):
                await super().__call__(scope, receive, send)
        except TimeoutError:  # a reader that does not read: never the export's holder for good
            log.warning("an export's answer was not taken in its time: its sending ended, and the export is given back")
        finally:
            self._release()


class _Building:
    """What an export took, given back once the request is done with it, or, when the request is left (cancelled)
    while the file is built in a thread, once that build ends, as the dashboard's ``_in_pool`` keeps its turn until the
    job it submitted ends: a request left early never frees the slot for another export while its build still holds
    the memory. A build the request left before a thread took it never begins."""

    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self._lock = threading.Lock()
        self._left = self._running = self._ended = False

    def build(self, work: Callable[..., Any]) -> Callable[..., Any]:
        """``work`` as the job the request hands its threads."""

        def job(*args: Any) -> Any:
            with self._lock:
                if self._left:
                    return None  # its requester is gone: nothing is built
                self._running = True
            try:
                return work(*args)
            finally:
                with self._lock:
                    self._ended = True
                    left = self._left
                if left:
                    self._release()

        return job

    def left(self) -> None:
        """The request ends without its answer: what the export took is given back now, or once its build ends."""
        with self._lock:
            self._left = True
            building = self._running and not self._ended
        if not building:
            self._release()


@dataclass(frozen=True)
class Shown:
    """What an export showed, which the roles that made it are weighed against again in its own transaction: every
    group (``reports.all``), the administration log (``audit.read``), its addresses (an administrator)."""

    every_group: bool
    log: bool
    addresses: bool


def origin(url: str) -> str:
    """An address's scheme, host and port: what a reader of the outputs sees of it, and what the log says of it."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def redacted(url: str) -> str:
    """An address as one who only reads the outputs sees it: its origin alone (I3), since a path, as a query, may carry
    a token or a signature."""
    parts = urlsplit(url)
    return f"{origin(url)}/{HIDDEN}" if parts.path not in ("", "/") or parts.query else origin(url)


def _changed(outputs: Sequence[SiemOutput]) -> dict[str, Any]:
    """``siem.changed``'s details: each output by its name and where it sends (its origin: never a path or a query,
    which may carry a token)."""
    return {"section": "siem", "outputs": [{"name": o.name, "origin": origin(o.url)} for o in outputs]}


def one_line(value: object) -> bool:
    """A secret as it may be kept: one line of visible ASCII (it goes in a header or an address: I8), ``MOST_SECRET``
    characters at most."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= MOST_SECRET
        and all(32 <= ord(c) <= 126 for c in value)
    )


def siem_etag(siem: SiemSettings) -> str:
    """The etag of the SIEM outputs (R12, E3's ``sso_etag``'s twin), which a change made from them names: a digest of
    what the page shows of them, never of a secret (the outputs name their secrets, never hold one)."""
    shown = json.dumps(siem.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(shown.encode("utf-8")).hexdigest()


def _problems(exc: ValidationError) -> list[dict[str, Any]]:
    """Each problem by its place and pydantic's words, which quote nothing given (E3's ``_invalid``); a place that names
    none of the settings' fields (a header's name) is shown as ``*``."""
    names = _field_names()
    return [
        {
            "loc": [part if isinstance(part, int) or part in names else "*" for part in error["loc"]],
            "msg": error["msg"].removeprefix("Value error, "),
            "type": error["type"],
        }
        for error in exc.errors(include_url=False, include_context=False, include_input=False)
    ]


def parse_siem(data: Mapping[str, Any]) -> tuple[list[SiemOutput], dict[str, str | None]]:
    """The outputs and the secrets a body gives (``{outputs, secrets?, etag?}``), checked as the settings check them
    (Task 5: a credential where the secret belongs, an address the server never reaches, egress's verdict on a literal
    address among them: ``siem_problem``, R10); an output given a secret and naming none names ``siem-<name>``. Refused
    400, the problems by their place, never repeating a value given."""
    outputs, secrets = data.get("outputs"), data.get("secrets", {})
    if set(data) - _BODY_KEYS or not isinstance(outputs, list) or not isinstance(secrets, dict):
        raise Refused(400, _SIEM_BODY)
    named: list[Any] = []
    for item in outputs:  # given a secret and naming none: named after the output, before the settings check it
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and item.get("secret") is None and secrets.get(name) is not None:
            item = {**item, "secret": f"{SIEM_SECRETS}{name}"}
        named.append(item)
    try:
        siem = SiemSettings.model_validate({"outputs": named})
    except ValidationError as exc:
        raise Refused(400, "invalid SIEM outputs", details=_problems(exc)) from None
    if (problem := siem_problem(siem)) is not None:  # fixed words with the output's place (Task 5)
        raise Refused(400, f"invalid SIEM outputs: {problem}")
    by_name = {output.name for output in siem.outputs}
    for name, value in secrets.items():
        if name not in by_name:
            raise Refused(400, _NO_OUTPUT)
        if value is not None and not one_line(value):
            raise Refused(400, _A_SECRET)
    return list(siem.outputs), {str(name): value for name, value in secrets.items()}


def keep_secrets(
    secrets: Any,
    before: Sequence[SiemOutput],
    after: Sequence[SiemOutput],
    given: Mapping[str, str | None],
    *,
    conn: Any,
) -> tuple[list[str], list[str]]:
    """In the change's transaction (``conn``): each secret given kept under its output's secret name, sealed for where
    the output sends (``siem_bound``: I1), one given as null removed, and a secret no output names any more (an output
    removed, or one that names its secret no more) removed with it; the names of the outputs whose secret was saved, and
    removed. Only names beginning ``siem-`` (R11): never the sign-in's, nor another part's."""
    by_name = {output.name: output for output in after}
    saved: list[str] = []
    removed: list[str] = []
    for name, value in given.items():
        output = by_name[name]
        secret_name = output.secret or f"{SIEM_SECRETS}{name}"
        if not secret_name.startswith(SIEM_SECRETS):
            continue  # the settings refuse such a name already: a second guard
        if value is None:
            if secrets.remove(secret_name, conn=conn):
                removed.append(name)
        else:
            secrets.save(secret_name, value.strip(), bound=siem_bound(output), conn=conn)
            saved.append(name)
    still = {output.secret for output in after}
    for output in before:
        own = f"{SIEM_SECRETS}{output.name}"
        if output.secret == own and own not in still and secrets.remove(own, conn=conn) and output.name not in removed:
            removed.append(output.name)
    return saved, removed


def siem_view(
    siem: SiemSettings,
    states: Mapping[str, QueueState],
    secret_state: Callable[[SiemOutput], tuple[bool, bool]],
    *,
    editor: bool,
    editable: bool,
    paused: dict[str, Any] | None,
) -> dict[str, Any]:
    """The SIEM outputs as ``editor`` (whoever may change them) or a reader sees them, how each is doing, whether they
    may be changed now, and why nothing is sent (``paused``). A reader sees an address's origin alone and the headers
    without their values: either may carry a token or a signature (I3), and no etag (a digest of the whole addresses).
    ``secret_state``: whether the output's secret is kept for where it sends, and whether one is kept that was set for
    another address (I1, E3's ``secret_issuer_changed``)."""
    items = []
    for output in siem.outputs:
        state = states.get(output.name) or QueueState(0)
        kept, elsewhere = secret_state(output) if output.secret is not None else (False, False)
        items.append(
            {
                "name": output.name,
                "kind": output.kind,
                "format": output.format,
                "url": output.url if editor else redacted(output.url),
                "headers": dict(output.headers) if editor else dict.fromkeys(output.headers, HIDDEN),
                "secret": output.secret,
                "has_secret": kept,
                "secret_destination_changed": elsewhere,
                "queued": state.queued,
                "dropped": state.dropped,
                "last_ok": state.last_ok,
                "last_error": state.last_error,
                "failing_since": state.failing_since,
                "blocked": state.blocked,
                # The queue waits at what its receiver refuses (an entry of the log, or a run refused each alone):
                # `latet server siem skip` passes it (R2-12).
                "held": state.blocked or state.held_to is not None,
                "collapsed": state.collapsed,  # entries a gap marker at the head stands for (the bound)
            }
        )
    return {
        "outputs": items,
        "etag": siem_etag(siem) if editor else None,
        "editable": editor and editable,
        "paused": paused,
    }


class Looked(Mapping[str, str]):
    """A mapping whose values are looked up as they are asked for (an identity's name by its id), each once: a report
    names the ids it shows, never every identity or group of the server (R8). A key ``find`` knows nothing of is
    missing."""

    def __init__(self, find: Callable[[str], str | None]) -> None:
        self._find = find
        self._found: dict[str, str | None] = {}

    def __getitem__(self, key: str) -> str:
        if key not in self._found:
            self._found[key] = self._find(key)
        value = self._found[key]
        if value is None:
            raise KeyError(key)
        return value

    def __iter__(self) -> Iterator[str]:
        return iter([key for key, value in self._found.items() if value is not None])

    def __len__(self) -> int:
        return sum(1 for value in self._found.values() if value is not None)


def _nothing(within: Changing) -> None:
    """A change with nothing to write: the license, the person's roles and their session weighed in a transaction
    before an export is read (R4), or a test is counted (M15)."""


class EventsApi:
    """The routes (the module's words). ``store`` and ``queue``: the event log and the SIEM's queue (one PostgreSQL
    store on a server); ``refreshed``: what this instance does once the outputs or the reports' settings changed (its
    copy of the settings and its outputs made again). One export
    at a time on an instance until its answer is sent (Task 9's carry: a report's export holds up to 500,000 rows in
    memory), and one report or export at a time for each person (the dashboard's threads are few)."""

    def __init__(
        self,
        edition: EnterpriseEdition,
        server: Server,
        people: People,
        access: Access,
        *,
        store: EventStore,
        queue: SiemQueue,
        refreshed: Callable[[], None],
        context: ssl.SSLContext | None = None,
    ) -> None:
        settings, secrets = edition.settings_store, edition.secrets
        if not isinstance(settings, DatabaseSettings) or not isinstance(secrets, DatabaseSecrets):
            raise TypeError("the reports' settings are a server's: kept in PostgreSQL")
        self.edition, self.server, self.people, self.access = edition, server, people, access
        self.settings, self.secrets = settings, secrets
        self.store, self.queue, self.refreshed, self.context = store, queue, refreshed, context
        self._exporting = threading.Lock()
        self._busy: set[str] = set()  # the people whose report or export is being made
        self._busy_lock = threading.Lock()

    def routes(self) -> list[EeRoute]:
        return [
            EeRoute("GET", "/reports", self.report, feature=REPORTS),
            EeRoute("GET", "/reports/export", self.report_export, feature=REPORTS, marked=True),
            EeRoute("GET", "/reports/settings", self.reports_settings, feature=REPORTS, dashboard=False),
            EeRoute("PUT", "/reports/settings", self.reports_change, feature=REPORTS, dashboard=False),
            EeRoute("GET", "/audit", self.audit_log, feature=REPORTS),
            EeRoute("GET", "/audit/export", self.audit_export, feature=REPORTS, marked=True),
            EeRoute("GET", "/siem", self.siem_show, feature=SIEM),
            EeRoute("PUT", "/siem", self.siem_save, feature=SIEM),
            EeRoute("PUT", "/siem/secret", self.siem_secret, feature=SIEM),
            EeRoute("POST", "/siem/test", self.siem_test, feature=SIEM),
            EeRoute("POST", "/siem/skip", self.siem_skip, feature=SIEM, dashboard=False),
        ]

    # --- what every route reads, in the request's threads -------------------------------------------------------------

    def _settings_now(self) -> EnterpriseSettings:
        """The Enterprise settings as PostgreSQL has them now, never this instance's copy (seconds behind a change made
        on another instance): the etag a page is given names the outputs a change replaces, and a report shows people
        as the company chose them last."""
        with self.server.db.connection() as conn:
            return settings_in(conn)

    def _producing(self, feature: str) -> None:
        """Refused 403 ``not-producing`` while the license produces nothing of ``feature`` (R2)."""
        if (refused := not_producing(self.edition.licensing.standing(), feature)) is not None:
            raise refused

    def _viewer(self, who: Who) -> Viewer:
        """What ``who`` sees of the reports (R3): everything for the administration (``ADMINISTRATION``), a person's
        roles' otherwise (403 when none: E3's gate let the person through by a copy of their roles); the SIEM outputs
        only while the license lists them."""
        people_mode = self._settings_now().reports.people
        siem_on = self.edition.licensing.licensed(SIEM)
        if not who.person:
            return Viewer(None, people_mode, "edit" if siem_on else None)
        identity = str(who.identity_id)
        roles = self.people.roles_of(identity)
        member_of = [group.id for group in self.people.groups_of(identity)]
        viewer = report_viewer(roles=roles, member_of=member_of, people=people_mode)
        if viewer is None:
            raise Refused(403, NO_REPORTS)
        return viewer if siem_on else replace(viewer, siem=None)

    def _reads_log(self, who: Who) -> bool:
        return self.access.allows(who.identity_id, "audit.read")

    def _addresses(self, who: Who) -> bool:
        """Whether ``who`` reads the administration log's addresses: an administrator (the administration token
        included), never the security role or an auditor (Decision 1 (d): I12)."""
        return not who.person or Role.ADMIN in self.people.roles_of(str(who.identity_id))

    def _names(self) -> Names:
        """How the report names what it shows: each id looked up as it is asked for (R8), a removed identity or group
        shown by its id, a person kept by a pseudonym by it. Only an id of the shape an identity or a group has is
        looked up: anything else (a row written by hand) names nothing."""
        identities, people = self.server.identities, self.people

        def name_of(identity_id: str) -> str | None:
            found = identities.get(identity_id) if _IDENTITY.fullmatch(identity_id) else None
            return found.name if found is not None else None

        def kind_of(identity_id: str) -> str | None:
            found = identities.get(identity_id) if _IDENTITY.fullmatch(identity_id) else None
            return found.kind if found is not None else None

        def group_of(group_id: str) -> str | None:
            found = people.group(group_id) if GROUP_ID.fullmatch(group_id) else None  # never a group found by name
            return found.name if found is not None else None

        return Names(groups=Looked(group_of), identities=Looked(name_of), kinds=Looked(kind_of))

    def _today(self) -> date:
        return date.fromtimestamp(self.server.clock())

    def _period(self, request: Request) -> Period:
        q = request.query_params
        try:
            return Period.parse(q.get("from"), q.get("to"), self._today())
        except ReportError as exc:  # fixed words of Period's
            raise Refused(400, str(exc)) from None

    def _asked(self, request: Request) -> tuple[Period, Filters]:
        q = request.query_params
        filters = Filters(*(q.get(name) or None for name in ("group", "project", "agent", "category")))
        return self._period(request), filters

    @staticmethod
    def _format(request: Request) -> str:
        fmt = request.query_params.get("format", "csv")
        if fmt not in ("csv", "json"):
            raise Refused(400, _FORMAT)
        return fmt

    def _export_slot(self) -> Callable[[], None]:
        """One export at a time on this instance, from its making to the end of its answer's sending (Task 9's carry:
        a report's export holds up to 500,000 rows, some 800 MB, in memory); 429 in fixed words while another is made.
        What gives it back."""
        if not self._exporting.acquire(blocking=False):
            raise Refused(429, _ONE_EXPORT)
        return _once(self._exporting.release)

    def _person_slot(self, who: Who) -> Callable[[], None]:
        """One report or export at a time for each person (the dashboard's threads are few: a person asking for many at
        once would hold them all); 429 in fixed words while theirs is being made. What gives it back."""
        if not who.person:
            return lambda: None
        actor = who.actor
        with self._busy_lock:
            if actor in self._busy:
                raise Refused(429, _ONE_AT_A_TIME)
            self._busy.add(actor)

        def free() -> None:
            with self._busy_lock:
                self._busy.discard(actor)

        return _once(free)

    def _shown_still(self, who: Who, shown: Shown, within: Changing) -> None:
        """In the export's own transaction: the person's roles as it finds them still allow what the export showed,
        which was read through E3's copies (every group, the log, its addresses); 403 otherwise, nothing given. The
        groups of a group lead are E3's copy's (no read of them in a transaction yet)."""
        if not who.person:
            return
        roles = self.people.roles_in(within.conn, str(who.identity_id)) or frozenset()
        if (
            (shown.every_group and not can(roles, "reports.all"))
            or (shown.log and not can(roles, "audit.read"))
            or (shown.addresses and Role.ADMIN not in roles)
        ):
            raise Refused(403, _ROLES_CHANGED)

    # --- reports ------------------------------------------------------------------------------------------------------

    async def report(self, request: Request, who: Who) -> Response:
        release = self._person_slot(who)
        try:
            viewer = await who.run(self._viewer, who)
            await who.run(self._producing, REPORTS)
            period, filters = self._asked(request)
            answer = await who.run(self._report, period, filters, viewer)
        finally:
            release()
        groups = None if viewer.groups is None else sorted(viewer.groups)
        return JSONResponse(answer | {"viewer": {"groups": groups, "people": viewer.people, "siem": viewer.siem}})

    def _report(self, period: Period, filters: Filters, viewer: Viewer) -> dict[str, Any]:
        try:
            return build_report(self.store, period, filters, viewer, self._names())
        except NotAllowed as exc:  # a group the viewer does not belong to: fixed words
            raise Refused(403, str(exc)) from None
        except ReportError as exc:  # a filter the log could not hold: fixed words that never repeat it
            raise Refused(400, str(exc)) from None

    async def report_export(self, request: Request, who: Who) -> Response:
        fmt = self._format(request)
        period, filters = self._asked(request)
        details = {"format": fmt, "from": period.start.isoformat(), "to": period.end.isoformat()}
        release = self._person_slot(who)
        building: _Building | None = None
        try:
            await who.run(self._producing, REPORTS)
            await who.change(_nothing)  # R4: the roles and the session weighed before anything is read
            viewer = await who.run(self._viewer, who)  # read after: a role taken away before is seen
            release = _both(release, self._export_slot())
            building = _Building(release)
            made: tuple[bytes, Shown] = await who.run(building.build(self._export), who, viewer, period, filters, fmt)
            body, shown = made

            def exported(within: Changing) -> None:  # R4: the roles weighed again, and decision (c) with them
                self._shown_still(who, shown, within)
                within.note("reports.exported", None, details)

            await who.change(exported)
        except BaseException:
            if building is not None:
                building.left()  # given back now, or once a build still under way ends
            else:
                release()
            raise
        return Sent(body, release)  # what it took, given back once its answer is sent

    def _export(self, who: Who, viewer: Viewer, period: Period, filters: Filters, fmt: str) -> tuple[bytes, Shown]:
        """In the request's threads: the export's answer, ready to send, and what it showed."""
        names = self._names()
        administration, cut, addresses = None, False, False
        if fmt == "json" and self._reads_log(who):
            entries = self._entries(period, None, None, AUDIT_LIMIT + 1)
            cut = len(entries) > AUDIT_LIMIT  # I11: said in the file; the log's own export has them all
            addresses = not cut and self._addresses(who)
            administration = None if cut else audit_rows(entries, names, addresses=addresses)
        try:
            name, kind, text = export_file(self.store, period, filters, viewer, names, fmt,
                                           administration=administration, administration_truncated=cut)  # fmt: skip
        except NotAllowed as exc:
            raise Refused(403, str(exc)) from None
        except TooLarge as exc:  # M3: fixed words, the most an export holds
            raise Refused(413, str(exc)) from None
        except ReportError as exc:
            raise Refused(400, str(exc)) from None
        shown = Shown(every_group=viewer.groups is None, log=administration is not None, addresses=addresses)
        return answer_body(name, kind, text), shown

    def _start(self, since: float) -> int:
        """R2-9: the id after which every entry from ``since`` on lies: the newest entry dated ``SKEW`` before it whose
        keyed hash holds (one written by hand with an old date and a recent id never moves the start past the period's
        entries); the log's start when none does."""
        audit = self.server.audit
        for most in (1, START_TRIES):
            found = audit.query(until=since - SKEW, limit=most)
            for entry in found:
                if audit.holds(entry):
                    return int(entry.id)
            if len(found) < most:
                break
        return 0

    def _entries(self, period: Period, action: str | None, actor: str | None, most: int) -> list[Any]:
        """The period's entries of the administration log, read by id a page at a time, oldest first: ``most`` at most,
        never the newest of a larger set taken for all of it (I11). It starts where every entry of the period has a
        higher id (``_start``, R2-9): a month ago is not read from the log's first entry."""
        since, until = period.bounds(None)
        found: list[Any] = []
        after = self._start(since)
        while len(found) < most:
            page = self.server.audit.after(after, LOG_PAGE, since=since, until=until, action=action, actor=actor)
            if not page:
                break
            found += page
            after = page[-1].id
            if len(page) < LOG_PAGE:
                break
        return found[:most]

    async def reports_settings(self, request: Request, who: Who) -> Response:
        """The administration's: how people appear in reports and how long the log keeps its counts."""
        settings = await who.run(self._settings_now)
        return JSONResponse(settings.reports.model_dump(mode="json"))

    async def reports_change(self, request: Request, who: Who) -> Response:
        """The administration's alone (R4): a change of how people appear, and of the retention, recorded with what it
        set (a mode and a number of days, which name nobody)."""
        data = await json_body(request, who)
        if not data or set(data) - {"people", "retention_days"}:
            raise Refused(400, _REPORTS_BODY)

        def change(within: Changing) -> ReportSettings:
            conn = within.conn
            conn.execute("LOCK TABLE ee_settings IN SHARE ROW EXCLUSIVE MODE")
            before = settings_in(conn).reports
            try:
                after = ReportSettings.model_validate(before.model_dump() | data)
            except ValidationError as exc:
                raise Refused(400, "invalid reports settings", details=_problems(exc)) from None
            value = after.model_dump(mode="json")
            try:
                self.settings.set_section("reports", value, actor=within.actor, address=within.address, conn=conn)
            except EnterpriseSettingsError as exc:  # by field and kind of error, never what was given
                raise Refused(400, f"not changed: {exc}") from None
            was = before.model_dump(mode="json")
            fields = sorted(name for name in value if value[name] != was[name])
            within.note("reports.changed", "reports", {"section": "reports", "fields": fields} | value)
            within.after.append(lambda: self.settings.changed("reports"))
            within.after.append(self.refreshed)
            return after

        written = await who.change(change)
        return JSONResponse(written.model_dump(mode="json"))

    # --- the administration log -------------------------------------------------------------------------------------

    def _log_asked(self, request: Request) -> tuple[Period, str | None, str | None]:
        q = request.query_params
        period = self._period(request)
        action, actor = q.get("action") or None, q.get("actor") or None
        if len(action or "") > MOST_FILTER or len(actor or "") > MOST_FILTER:
            raise Refused(400, _A_FILTER)
        return period, action, actor

    def _log(self, who: Who, period: Period, action: str | None, actor: str | None) -> dict[str, Any]:
        if not self._reads_log(who):  # E3's gate checked it by a copy of the roles: a second guard
            raise Refused(403, NO_LOG)
        since, until = period.bounds(None)
        entries = list(
            self.server.audit.query(since=since, until=until, action=action, actor=actor, limit=AUDIT_LIMIT + 1)
        )
        addresses = self._addresses(who)
        rows = audit_rows(entries[:AUDIT_LIMIT], self._names(), addresses=addresses)
        actors = {row["actor"]["id"]: row["actor"] for row in rows}
        return {
            "period": {"from": period.start.isoformat(), "to": period.end.isoformat()},
            "entries": rows,
            "actions": sorted({row["action"] for row in rows}),
            "actors": sorted(actors.values(), key=lambda a: (a["name"] or a["id"]).lower()),
            "truncated": len(entries) > AUDIT_LIMIT,  # the page shows the newest, and says so
            "addresses": addresses,
        }

    def _whole_log(
        self, who: Who, period: Period, action: str | None, actor: str | None, *, addresses: bool
    ) -> list[dict[str, Any]]:
        """Every entry of the period for the log's export, oldest first: 413 past ``AUDIT_EXPORT_MOST`` (I11)."""
        if not self._reads_log(who):
            raise Refused(403, NO_LOG)
        entries = self._entries(period, action, actor, AUDIT_EXPORT_MOST + 1)
        if len(entries) > AUDIT_EXPORT_MOST:
            raise Refused(413, _LOG_TOO_LARGE)
        return audit_rows(entries, self._names(), addresses=addresses)

    async def audit_log(self, request: Request, who: Who) -> Response:
        await who.run(self._producing, REPORTS)
        period, action, actor = self._log_asked(request)
        return JSONResponse(await who.run(self._log, who, period, action, actor))

    async def audit_export(self, request: Request, who: Who) -> Response:
        fmt = self._format(request)
        period, action, actor = self._log_asked(request)
        details = {"format": fmt, "from": period.start.isoformat(), "to": period.end.isoformat()}
        release = self._person_slot(who)
        building: _Building | None = None
        try:
            await who.run(self._producing, REPORTS)
            await who.change(_nothing)  # R4, as a report's export
            release = _both(release, self._export_slot())
            building = _Building(release)
            made: tuple[bytes, Shown] = await who.run(building.build(self._log_file), who, period, action, actor, fmt)
            body, shown = made

            def exported(within: Changing) -> None:  # R4, as a report's export
                self._shown_still(who, shown, within)
                within.note("audit.exported", None, details)

            await who.change(exported)
        except BaseException:
            if building is not None:
                building.left()
            else:
                release()
            raise
        return Sent(body, release)

    def _log_file(
        self, who: Who, period: Period, action: str | None, actor: str | None, fmt: str
    ) -> tuple[bytes, Shown]:
        """In the request's threads: the log's export, ready to send, and what it showed."""
        addresses = self._addresses(who)
        rows = self._whole_log(who, period, action, actor, addresses=addresses)
        name, kind, text = audit_file(rows, period, fmt)
        return answer_body(name, kind, text), Shown(every_group=False, log=True, addresses=addresses)

    # --- the SIEM outputs ---------------------------------------------------------------------------------------------

    def _view(self, siem: SiemSettings, who: Who) -> dict[str, Any]:
        """The outputs as ``who`` may see them (``siem_view``): whoever may change them sees them whole."""
        editor = self.access.allows(who.identity_id, "siem.edit")
        states = {output.name: self.queue.state(output.name) for output in siem.outputs}
        standing = self.edition.licensing.standing()
        pause = pause_of(standing, SIEM)
        paused = {"why": pause["why"], "expires": pause["expires"]} if pause is not None else None

        def secret_state(output: SiemOutput) -> tuple[bool, bool]:
            """Whether its secret is kept for where it sends, and whether one set for another address is (M7: asked as
            E3 asks it, by opening it bound; read, never answered)."""
            try:
                return secret_of(self.secrets, output) is not None, False
            except SecretElsewhere:
                return False, True

        return siem_view(
            siem, states, secret_state, editor=editor, editable=standing.state in MANAGEABLE, paused=paused
        )

    async def siem_show(self, request: Request, who: Who) -> Response:
        settings = await who.run(self._settings_now)
        return JSONResponse(await who.run(self._view, settings.siem, who))

    async def siem_save(self, request: Request, who: Who) -> Response:
        data = await json_body(request, who)
        named_etag, etag = "etag" in data, data.get("etag")
        if named_etag and not (isinstance(etag, str) and 0 < len(etag) <= MOST_ETAG):
            raise Refused(400, _AN_ETAG)
        outputs, given = await who.run(parse_siem, data)
        if who.person:  # R2-4, counted before the transaction (R3-3): never in Valkey under the settings' lock
            await who.run(self._counted, outputs, who)

        def save(within: Changing) -> SiemSettings:
            conn = within.conn
            # Every other change of the Enterprise settings waits for this one from here; reads never wait (E3's).
            conn.execute("LOCK TABLE ee_settings IN SHARE ROW EXCLUSIVE MODE")
            before = settings_in(conn).siem
            if named_etag and siem_etag(before) != etag:
                raise Refused(409, _CHANGED_SINCE, code="siem-changed")
            was = {output.name: output for output in before.outputs}
            if who.person and any(o.local and was.get(o.name) != o for o in outputs):  # I2: the command's alone
                raise Refused(403, _BY_COMMAND, code=BY_COMMAND_CODE)
            if who.person and any(o.local and o.name in given for o in outputs):  # its secret too, set or removed (L2)
                raise Refused(403, _ITS_CREDENTIAL_BY_COMMAND, code=BY_COMMAND_CODE)
            after = SiemSettings(outputs=outputs)
            saved, removed = keep_secrets(self.secrets, before.outputs, outputs, given, conn=conn)
            try:
                self.settings.set_section("siem", after.model_dump(mode="json"), actor=within.actor,
                                          address=within.address, conn=conn)  # fmt: skip
            except EnterpriseSettingsError as exc:  # by place and kind of error, never what was given
                raise Refused(400, f"not changed: {exc}") from None
            if saved or removed:  # the outputs' names, never a secret
                within.note("siem.secret.changed", "siem", {"saved": saved, "removed": removed})
            within.note("siem.changed", "siem", _changed(outputs))
            within.after.append(lambda: self.settings.changed("siem"))
            within.after.append(self.refreshed)
            return after

        written = await who.change(save)
        return JSONResponse(await who.run(self._view, written, who))

    @staticmethod
    def _named(data: Mapping[str, Any]) -> str:
        name = data.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise Refused(400, _A_NAME)
        return name

    async def siem_secret(self, request: Request, who: Who) -> Response:
        """``{name, secret}``: the secret of one output kept (named after it when it names none, R11), or removed
        (``secret: null``); never answered, never recorded."""
        data = await json_body(request, who)
        name, value = self._named(data), data.get("secret")
        if value is not None and not one_line(value):
            raise Refused(400, _A_SECRET)

        def change(within: Changing) -> None:
            conn = within.conn
            conn.execute("LOCK TABLE ee_settings IN SHARE ROW EXCLUSIVE MODE")
            siem = settings_in(conn).siem
            output = next((o for o in siem.outputs if o.name == name), None)
            if output is None:
                raise Refused(404, _NO_OUTPUT)
            if who.person and output.local:  # I2: such an output's secret is the command's alone (L2)
                raise Refused(403, _ITS_CREDENTIAL_BY_COMMAND, code=BY_COMMAND_CODE)
            secret_name = output.secret
            if value is None:
                if secret_name is None or not self.secrets.remove(secret_name, conn=conn):
                    raise Refused(404, _NO_SECRET)
            else:
                if secret_name is None:  # named after its output, in the settings: the value never is
                    secret_name = f"{SIEM_SECRETS}{name}"
                    named = [
                        o.model_copy(update={"secret": secret_name}) if o.name == name else o for o in siem.outputs
                    ]
                    value_of = SiemSettings(outputs=named).model_dump(mode="json")
                    try:
                        self.settings.set_section("siem", value_of, actor=within.actor, address=within.address,
                                                  conn=conn)  # fmt: skip
                    except EnterpriseSettingsError as exc:
                        raise Refused(400, f"not changed: {exc}") from None
                    within.note("siem.changed", "siem", _changed(named))
                    within.after.append(lambda: self.settings.changed("siem"))
                # Sealed for where the output sends now (I1): one moved later never sends it elsewhere.
                self.secrets.save(secret_name, value.strip(), bound=siem_bound(output), conn=conn)
            within.note("siem.secret.changed", "siem", {"saved": [name] if value is not None else [],
                                                        "removed": [] if value is not None else [name]})  # fmt: skip
            within.after.append(self.refreshed)

        await who.change(change)
        return JSONResponse({"ok": True})

    async def siem_test(self, request: Request, who: Who) -> Response:
        """``{name}``: one test event sent to a saved output now, whether it arrived and why not (fixed words of Task 5,
        which never repeat the address). Only an output saved is tested: no address a body names is ever reached (R10).
        The person is weighed before the test is counted (a person refused spends no one's tests), then weighed again
        in the transaction that records the attempt, before the address is reached (M15); an output at `http://` or at
        this computer is tested by `latet server siem test` alone (I2), one at an address egress refuses now is not
        tested, and tests are limited (I2)."""
        name = self._named(await json_body(request, who))
        await who.run(self._producing, SIEM)
        await who.change(_nothing)  # M15: a person refused counts no test of anyone's
        self._testable(await who.run(self._saved, name), who)  # refused before a test is counted
        await who.run(self._paced, name, who)

        def attempted(within: Changing) -> SiemOutput:
            """M15: the person, their session and the license weighed, and the attempt recorded, in one transaction
            before the address is reached: a demotion, a license changed or a failure afterwards never leaves a
            connection that no entry names. The output tested is the one this transaction reads."""
            found = next((o for o in settings_in(within.conn).siem.outputs if o.name == name), None)
            if found is None:
                raise Refused(404, _NO_OUTPUT)
            self._testable(found, who)
            within.note("siem.tested", "siem", {"output": name})
            return found

        output = await who.change(attempted)
        ok, detail = await who.run(self._tested, output)
        return JSONResponse({"ok": ok, "detail": detail})

    @staticmethod
    def _testable(output: SiemOutput, who: Who) -> None:
        """An output at `http://` or at this computer is tested by `latet server siem test` alone, as it is set (I2);
        one at an address egress refuses now is not tested (``siem_problem``: it may have been saved before)."""
        if who.person and output.local:
            raise Refused(403, _TESTED_BY_COMMAND)
        if siem_problem(SiemSettings(outputs=[output])) is not None:
            raise Refused(400, _UNREACHED)

    def _saved(self, name: str) -> SiemOutput:
        """The output ``name`` as PostgreSQL has it now; 404 when there is none."""
        output = next((o for o in self._settings_now().siem.outputs if o.name == name), None)
        if output is None:
            raise Refused(404, _NO_OUTPUT)
        return output

    def _paced(self, name: str, who: Who) -> None:
        """I2: one test an output every 10 s and 30 an hour, 30 an hour for each person (or administration token);
        429 past them, in fixed words. Counted in Valkey for every instance (E0b's ``Bus.allow``); 503 in fixed words
        when Valkey does not answer."""
        bus = self.server.bus
        kind, most, per = TESTS_BY_WHOM
        try:
            allowed = all(bus.allow(f"{key}:{name}", limit, seconds) for key, limit, seconds in TESTS)
            allowed = allowed and bus.allow(f"{kind}:{who.actor}", most, per)
        except Exception as exc:  # by its type (R14): Valkey's words can name its address
            log.warning("the SIEM tests could not be counted (%s)", type(exc).__name__)
            raise Refused(503, _NO_PACE) from None
        if not allowed:
            raise Refused(429, _TOO_MANY_TESTS)

    def _tested(self, output: SiemOutput) -> tuple[bool, str]:
        """In the request's threads: the test of ``output``, its secret as it was sealed for where it sends (I1)."""
        try:
            secret = secret_of(self.secrets, output)
        except SecretElsewhere:
            return False, _ELSEWHERE.format(name=output.name)
        except Exception as exc:  # by its type: a store's words can name where it is
            return False, _UNREAD_SECRET.format(kind=type(exc).__name__)
        return send_test(output, secret, self.context, host=urlsplit(self.server.settings.url).hostname)

    def _counted(self, outputs: Sequence[SiemOutput], who: Who) -> None:
        """R2-4: a person's outputs added, or moved to another scheme, host or port, against the settings as they are
        now, each counted against ten an hour (``Bus.allow``); 429 past them. Asked before the save's transaction
        (R3-3): a slow Valkey never holds the settings' lock, and one that does not answer is 503 in fixed words."""
        was = {output.name: output for output in self._settings_now().siem.outputs}
        moved = [o for o in outputs if o.name not in was or was[o.name].destination != o.destination]
        if not moved:
            return
        kind, most, per = DESTINATIONS
        try:
            allowed = all(self.server.bus.allow(f"{kind}:{who.actor}", most, per) for _ in moved)
        except Exception as exc:  # by its type (R14): Valkey's words can name its address
            log.warning("the SIEM destinations could not be counted (%s)", type(exc).__name__)
            raise Refused(503, _NO_COUNT) from None
        if not allowed:
            raise Refused(429, _TOO_MANY_DESTINATIONS)

    async def siem_skip(self, request: Request, who: Who) -> Response:
        """The administration's alone (`latet server siem skip`, R2-12): what holds an output's queue because its
        receiver refuses it is passed. The entry of the administration log it waits at is sent no more: it stands
        behind a gap marker of its own (the receiver's chain links through it), or, a marker refused in its turn, is
        taken off; a run of other events refused each alone is dropped and counted. Only while the queue is held
        (``blocked``, or a run refused: 409 in fixed words otherwise, read under the queue's lock in the skip's
        transaction), under the output's lease, the one its delivery holds while it sends (R3-4: 409 while another
        instance holds it, or when it ran out before the commit), and in one transaction with its entry of the log,
        `siem.skipped {output, from, to, count, dropped}` written after the queue's rows (R3-1: the chain's lock after
        the rows' locks)."""
        name = self._named(await json_body(request, who))
        await who.run(self._saved, name)
        async with _leased(who, self.server.bus.lease(f"siem:{name}", SKIP_SECONDS)) as lease:
            if not lease:
                raise Refused(409, _SENDING)

            def skipped(within: Changing) -> dict[str, Any]:
                url, now = self.server.settings.url, self.server.clock()
                passed = self.queue.skip(name, origin=url, at=now, conn=within.conn, held_only=True)
                if passed is None:
                    raise Refused(409, _NOT_HELD)
                span = {"from": passed.first, "to": passed.last, "count": passed.entries, "dropped": passed.dropped}
                within.note("siem.skipped", "siem", {"output": name, **span})
                self._still_held(lease)  # as the delivery asks before `done`: never a skip under a lease run out
                return span

            span = await who.change(skipped)
        return JSONResponse({"ok": True, **span})

    @staticmethod
    def _still_held(lease: Lease) -> None:
        """Last in the skip's transaction: the output's lease still this instance's, with ``MARGIN`` seconds of it left
        (as the delivery asks before a batch leaves the queue, C1); 409 in fixed words otherwise, nothing skipped, and
        503 when Valkey cannot tell."""
        try:
            still = lease.held(MARGIN)
        except Exception as exc:  # by its type (R14): Valkey's words can name its address
            log.warning("whether a SIEM output's lease is still held could not be told (%s)", type(exc).__name__)
            raise Refused(503, _NO_LEASE) from None
        if not still:
            raise Refused(409, _SENDING)
