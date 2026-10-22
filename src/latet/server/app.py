"""An instance of a Latet server: its parts (``Server``, made by ``open_server``), its ear on Valkey (``listen``), its
beat (``beat``) and its app (``create_server_app``), the gate (``ServerGate``) in front of every request.

``open_server`` starts an instance only on the database it belongs to: the schema this Latet's, the master key the one
the database was first started with, Valkey answering with the server's password. What it opened is closed when any
step fails. Every key it gives its parts comes from the master key: the bus's, the credentials' (tokens, keys, codes,
administration tokens), the administration log's, the sign-ins'.

The ear hands every message to ``Server.heard``. A space applies what concerns it in order, in its own time
(``Spaces``); an identity disabled, removed, its token revoked or its key rotated has its space closed at once on every
instance (Ruling 22; a rename updates the space in place; the instance that makes the change closes it first, whatever
becomes of the message: the machines' ``DELETE token``, the administration), then its dashboard sessions and codes and
its AWS sessions ended off the ear, and on ``removed`` every family of keys Valkey keeps for it, once, by the instance
that takes the purge's lock; a clean-up that fails is said by its error's type and tried again by the beat's check
(``Server.check``, which also catches up the spaces that missed a change). When the connection to Valkey drops, or the
beat finds the ear silent (no beat of its own heard for three beats: R-14), what was published meanwhile is lost: the
instance closes its spaces and forgets what it kept, then listens again.

The gate answers only the server's own name, but for health and readiness, which a load balancer asks by an instance's
address and which say their state and nothing else (Ruling 12, S15). An identity's address (``/u/<route key>/...``)
leads to its space, never asked of a web page: its proxy routes by the route key alone (Ruling 17), its computer's calls
(``computer_call``: the bridge, ``/_latet/engine/v1/``, and the CLI's two by POST, each written as it is read) with its
own token only, a person's alone for the CLI's two, one at a time and some a minute for each identity, and nothing else
of ``/_latet``, which its space's app also mounts and would trust for it, nor what a proxy added to the request (the
client's address, the server's own name: ``_unproxied``). The dashboard's API leads to the space of the person signed
in, and refuses a request a page of another site made before anything is looked up. A token, an address or a dashboard's
session this instance holds in its memory (a few seconds after it found it, dropped at once on every instance when it
ends or its identity changes: ``live.SIGNINS``, ``Server.heard``) costs no store; a token, an address or a session it
does not hold is checked by the limiter the machines' calls and the administration share (``machines.checked``: the
client's address takes a slot of its window before anything is looked up, R-6, and one found good within the hour is
looked up past it), one request at a time for each (``Server.lookups``), in the gate's own threads, and a store that
does not answer refuses the request (503), as do those threads when too many calls wait for them already (with
``Retry-After``). A client's address that the limiter found past its limit (or its IPv6 /48) is refused on the event
loop until its window ends, no thread taken (``machines.Limits``), and so is what the instance does not hold while those
threads are busy (``machines.CREDENTIALS_BUSY``, 503), but a credential found valid on any instance, which Valkey is
asked about a batch at a time (``machines.admitted``). A dashboard's sign-in link takes a slot of its address's one
budget of misses too (``machines.miss_key``): its codes that open nothing are misses, and past the limit they are
refused on the event loop, for as long as anyone behind the address keeps spending it; a browser opened it, so each of
its refusals sends it back to the dashboard's page, which says why (``?login=expired``, ``limited``, ``busy``). Every
429 of a limit says ``Retry-After``, and every refusal of the server's own is paced in the log (``REFUSED``). An
identity has ``requests_in_flight`` requests to its destinations in flight on an instance at most (the answers being
restored cost the event loop that every identity shares): past it, 429 with ``Retry-After``. Every answer says
``nosniff``, the machines' calls, the sign-in and every answer under an identity's address ``no-store``
(``AnswerHeaders``); a body is bounded on the wire (``BodyLimit``) and once decoded (the space's mediator).

Who calls: the event loop ``heard`` and ``listen`` (no lock taken, no round trip made on it but the Live Reviewer's and
the feeds', bounded by the bus's ``SOCKET_TIMEOUT``: R-7); the beat ``check`` (its query in a thread); the gate and the
routes, whose calls that answer in milliseconds (the limits, the lookups, the sessions) run in the gate's threads
(``machines.pre_auth``) and whose writes in threads of their own (``machines.in_store``), readiness's pings and the
beat's own message each in threads of their own."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import secrets
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request, cookie_parser
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from latet import __version__
from latet.api.rest import (
    _SECURITY_HEADERS,
    API,
    COOKIE,
    DOCS,
    DOCS_DIR,
    NO_COMPUTER,
    UI,
    WEB_DIR,
    docs_file,
    docs_headers,
    ui_file,
)
from latet.config import data_dir
from latet.edition import Community, Edition
from latet.gateway.security import BodyLimit, refuse, url_hosts
from latet.logline import Paced, described
from latet.server import live, logs, queue
from latet.server.admin import Refusals, admin_routes
from latet.server.audit import AuditLog
from latet.server.bus import Bus
from latet.server.db import Database, check_schema
from latet.server.identities import IDENTITY_ID, AdminTokens, Identities
from latet.server.live import SIGNINS, ChainVerdicts, ValkeyAwsSessions, ValkeySignIns
from latet.server.logs import REFUSED, RequestLog
from latet.server.machines import (
    LOOKUP_LIMIT,
    LOOKUP_WINDOW,
    RETRY_AFTER,
    Known,
    Limits,
    OneAtATime,
    StoresUnavailable,
    TooManyMisses,
    admitted,
    bucket_of,
    checked,
    client_of,
    counted,
    from_a_page,
    given_back,
    in_store,
    machine_routes,
    miss_key,
    pre_auth,
    secret_identity,
    token_of,
)
from latet.server.master import MasterKey, check_master_key
from latet.server.metrics import Metrics
from latet.server.settings import SERVER_API, ServerSettings, read_secret_file
from latet.server.shared import SharedSettings
from latet.server.spaces import IdentityUnavailable, Spaces, SpaceUnavailable
from latet.server.spaces import _long as space_long
from latet.server.tokens import ADMIN_PREFIX, COMPUTER_PREFIX, KEY_PREFIX, TOKEN_PREFIX, is_route_key
from latet.state.local import CODE_TTL, SESSION_TTL

if TYPE_CHECKING:
    from latet.server.identities import Identity
    from latet.vision.pool import ServerReading  # NumPy: the ocr extra

log = logging.getLogger(__name__)

# What an instance hears: E1 appends its own channels ("ee:*").
CHANNELS = ["identity:*", "shared", "identities", SIGNINS, "approvals:*", "feed:*", "beat"]
RETRY_SECONDS = 1.0  # after the connection to Valkey drops, before listening again
# The events of an identity that close its space on every instance (Ruling 22): what a revoked token or a rotated key
# opened ends with it, its long connections (a WebSocket, a stream, the plugin's poll) too. A rename updates it.
_CLOSING = ("disabled", "removed", "revoked", "rotated")
# The events that end an identity's dashboard sessions and codes and its AWS sessions, on every instance that hears
# them (a token revoked ends the person's sign-ins too: the computer that held it may be gone).
_ENDING = ("disabled", "removed", "revoked", "rotated")
# Every family of keys Valkey keeps for an identity, each named ``<family>:<identity>`` or begun so: what goes when it
# is removed (R-21). Its jobs' locks, its rate limits and the purge's own mark end by themselves.
FAMILIES = (*live.FAMILIES, *queue.FAMILIES)
PURGE_SECONDS = 300.0  # the purge's lock, for the instance that takes it (one scan of Valkey's keys)
PURGED_SECONDS = 86_400.0  # the mark that an identity's keys are purged, for the instances that come late


class ValkeyUnavailable(RuntimeError):
    """Valkey refused the instance, or did not answer in time, at its start: the instance does not start. Its words
    name the setting and the driver's error by its type, never the address or its password."""


def server_reading(settings: ServerSettings) -> ServerReading | None:
    """The bounds of the text recognitions an instance's spaces share, its settings': ``ocr_readers`` left unset is its
    default, or the processors this instance may use when they are fewer (``vision.pool.usable_cpus``). None where the
    ``ocr`` extra is not installed: its spaces read no image. Raises ValueError, in words that name the setting and the
    numbers, for ``ocr_readers`` given past those processors, which would share them past what each reader was given."""
    from latet.vision import models

    if not models.runtime_available():
        return None
    from latet.vision import pool

    processors = pool.usable_cpus()
    readers = settings.ocr_readers
    if "ocr_readers" in settings.model_fields_set:
        if readers > processors:
            raise ValueError(
                f"ocr_readers is {readers}, more than the {processors} processors this instance may use: each text "
                "recognition reads with a share of them, so set it to that many at most"
            )
    else:
        readers = min(readers, processors)
    return pool.ServerReading(readers, settings.ocr_wait_seconds, settings.ocr_read_seconds)


@dataclass(eq=False)
class Server:
    settings: ServerSettings
    db: Database
    bus: Bus
    master: MasterKey
    identities: Identities
    admin: AdminTokens
    audit: AuditLog
    shared: SharedSettings
    sign_ins: ValkeySignIns
    edition: Edition = field(default_factory=Community)  # E1 gives each server its own (open_server)
    # E0c: this instance's metrics, which later parts add to as soon as the server is open, served or not.
    metrics: Metrics = field(default_factory=Metrics, repr=False)
    clock: Callable[[], float] = time.time
    data: Path = field(default_factory=data_dir)  # the instance's models
    reviewer_key: str | None = field(default=None, repr=False)  # read once; never an identity's setting
    listening: asyncio.Event = field(default_factory=asyncio.Event)  # Valkey's messages are heard
    # Made by each process, never given: what this instance's spaces' announcements and its counts in Valkey name.
    instance: str = field(init=False, default_factory=lambda: secrets.token_hex(6))
    spaces: Spaces = field(init=False)
    # When this instance last heard its own beat (time.monotonic), or last subscribed: R-14's silence is measured so.
    beat_heard: float = field(init=False, default=0.0)
    # What was found valid in the last hour, here or on another instance (``machines.Known``, kept in Valkey too):
    # tokens and API keys, route keys, dashboard sessions. One of them skips its bucket's limit, and is looked up all
    # the same.
    known_secrets: Known = field(init=False, repr=False)
    known_routes: Known = field(init=False, repr=False)
    known_sessions: Known = field(init=False, repr=False)
    known_codes: Known = field(init=False, repr=False)
    redeeming: set[bytes] = field(init=False, default_factory=set, repr=False)  # event loop only; no sessions shared
    _redeeming_tasks: set[asyncio.Task[tuple[str | None, bool]]] = field(init=False, default_factory=set, repr=False)
    # The buckets the shared limiter found past their limit, remembered so until their window ends: their credentials
    # are refused on the event loop, no thread taken, no round trip (``machines.Limits``).
    limits: Limits = field(init=False, default_factory=Limits, repr=False)
    # The lookups of tokens, route keys and dashboard sessions under way: one at a time for each credential
    # (``machines.OneAtATime``).
    lookups: OneAtATime = field(init=False, default_factory=OneAtATime, repr=False)
    # The beat's verdict on the administration log's chain: one reader for the instance, which never goes back to a
    # verdict older than one it kept or read (``live.ChainVerdicts``).
    chain: ChainVerdicts = field(init=False, repr=False)
    # The administration's refusals not recorded one by one, counted, and written once a minute by the beat.
    refusals: Refusals = field(init=False, default_factory=Refusals, repr=False)
    _ear: asyncio.Task[None] | None = field(init=False, default=None, repr=False)
    _cleanups: dict[str, bool] = field(init=False, default_factory=dict, repr=False)  # id: whether it was removed
    _cleaning: asyncio.Task[None] | None = field(init=False, default=None, repr=False)
    _unended: Paced = field(init=False, default_factory=Paced, repr=False)  # failed clean-ups, said once a minute
    # The bounds of the text recognitions this instance's spaces share, its settings' (``server_reading``), read once
    # when it is made: every space of the instance reads through the one pool they make, whatever its processors become
    # later. None where the ``ocr`` extra is not installed.
    reading: ServerReading | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self.reading = server_reading(self.settings)
        self.spaces = Spaces(self)
        self.chain = ChainVerdicts(self.bus, self.master, clock=lambda: self.clock())  # the instance's clock, as it is
        known = self.master.derive(b"known")  # the same on every instance: what one found valid, another knows
        self.known_secrets = Known(self.bus, known, kind="secret")
        self.known_routes = Known(self.bus, known, kind="route")
        self.known_sessions = Known(self.bus, known, kind="session")
        self.known_codes = Known(self.bus, known, kind="code", seconds=CODE_TTL, memory=False)
        self.sign_ins.code_redeemed = self.known_codes.forget

    def ear_silent(self, after: float) -> bool:
        """Whether this instance listens and has heard no beat of its own (the beat publishes one every few seconds,
        R-14) for ``after`` seconds since it subscribed: its connection is open and delivers nothing."""
        return self.listening.is_set() and time.monotonic() - self.beat_heard > after

    def silenced(self) -> None:
        """The beat found the ear silent: it is cancelled, and ``listen`` does what it does for a lost connection (the
        spaces closed, what the instance kept forgotten, ``listening`` cleared), then listens again."""
        ear = self._ear
        if ear is not None and not ear.done():
            ear.cancel()

    def _ended(self, identity_id: str) -> None:
        """The identity's dashboard sessions and codes and its AWS sessions, gone: any instance may do it (the keys are
        shared), and each is listed for its identity, so no scan of Valkey's keys is made. Each step is taken whatever
        the other does, as ``_end`` takes them; the first failure is raised once both were tried."""
        aws = ValkeyAwsSessions(self.bus, self.master, identity_id, clock=self.clock)
        failure: Exception | None = None
        for step in (functools.partial(self.sign_ins.end, subject=identity_id), aws.drop_all):
            try:
                step()
            except Exception as exc:  # raised once the other step was tried: its caller says it by its type
                failure = failure or exc
        if failure is not None:
            raise failure

    def _purge(self, identity_id: str) -> bool:
        """On ``removed`` (R-21): every family of keys Valkey keeps for the identity gone, once, by the instance that
        takes the purge's lock (its change counts, its vault's and its settings', went with its row in PostgreSQL). True
        when it is done, here or before; False when another instance purges it now (asked again at the next check).
        Whoever has Valkey's password can hold the lock or write the mark that says it is done: what is left then ends
        by its own lifetimes, a week and a day at most."""
        if self.bus.get(f"purged:{identity_id}") is not None:
            return True
        with self.bus.lock(f"purge:{identity_id}", PURGE_SECONDS) as mine:
            if not mine:
                return False
            self.bus.purge_families(identity_id, FAMILIES)
            self.bus.set(f"purged:{identity_id}", b"1", PURGED_SECONDS)
        return True

    def _end(self, identity_id: str, removed: bool) -> tuple[bool, list[str]]:
        """In a worker thread: what the identity kept in Valkey ended, each part whatever another's failure (a key of
        the wrong type, written by hand under one of its lists, stops no other part, the purge least of all). Whether
        it is all done, and the types of the errors met."""
        aws = ValkeyAwsSessions(self.bus, self.master, identity_id, clock=self.clock)
        steps: list[Callable[[], object]] = [functools.partial(self.sign_ins.end, subject=identity_id), aws.drop_all]
        if removed:
            steps.append(functools.partial(self._purge, identity_id))
        done, failed = True, []
        for step in steps:
            try:
                done = step() is not False and done
            except Exception as exc:  # its words can name Valkey's address: only its type goes
                failed.append(type(exc).__name__)
        return done and not failed, failed

    def _clean(self, identity_id: str, *, removed: bool) -> None:
        """What the identity kept in Valkey ended, off the ear; a failure is tried again by the beat's check."""
        if IDENTITY_ID.fullmatch(identity_id) is None:
            return
        self._cleanups[identity_id] = self._cleanups.get(identity_id, False) or removed
        if self._cleaning is None or self._cleaning.done():
            self._cleaning = asyncio.get_running_loop().create_task(self._cleaned(), name="latet-cleanups")

    async def _cleaned(self) -> None:
        tried: set[tuple[str, bool]] = set()
        while todo := [(i, removed) for i, removed in self._cleanups.items() if (i, removed) not in tried]:
            for identity_id, removed in todo:
                tried.add((identity_id, removed))
                try:
                    done, failed = await asyncio.to_thread(self._end, identity_id, removed)
                except RuntimeError:  # the threads of the process are let go: it stops
                    return
                if failed and (count := self._unended.due()):
                    log.warning(
                        "what %s kept in Valkey could not all be ended on this instance (%s): the beat tries again%s",
                        identity_id,
                        ", ".join(failed),
                        f" ({count} times since the line before)" if count > 1 else "",
                    )
                if done and self._cleanups.get(identity_id) == removed:
                    del self._cleanups[identity_id]

    async def heard(self, channel: str, message: Mapping[str, Any]) -> None:
        """A message published by an instance, this one too: what it changes here."""
        if channel == "beat":
            if message.get("instance") == self.instance:
                self.beat_heard = time.monotonic()
            return
        if channel == SIGNINS:  # a session or a person's sessions ended on an instance: dropped from this one's memory
            self.sign_ins.heard(message)
            return
        if channel == "identities":
            identity_id = message.get("id")
            if not isinstance(identity_id, str):
                return
            self.identities.forget(identity_id)
            self.sign_ins.forget(subject=identity_id)  # its sessions are looked up again: disabled, removed, revoked
            event = message.get("event")
            if event in _CLOSING:  # first, whatever follows fails
                await self.spaces.drop(identity_id, wait=False)
            elif event == "updated":
                self.spaces.renamed(identity_id)
            if event in _ENDING:
                self._clean(identity_id, removed=event == "removed")
            elif event == "enabled" and self._cleanups.get(identity_id) is False:
                # A clean-up still pending from before is over: tried again now, it would end the sessions the person
                # opens from here on.
                del self._cleanups[identity_id]
            return
        if channel == "shared":
            self.shared.forget()
        await self.spaces.message(channel, message)

    async def check(self) -> None:
        """The beat's check (Task 14 runs it every few seconds): ``Spaces.check``, an identity it finds disabled, gone
        or short of a token its space was built with (its message lost) forgotten and ended as its message would have,
        and every clean-up that failed tried again."""
        for identity_id, gone in await self.spaces.check():
            self.identities.forget(identity_id)
            self._clean(identity_id, removed=gone)
        if self._cleanups and (self._cleaning is None or self._cleaning.done()):
            self._cleaning = asyncio.get_running_loop().create_task(self._cleaned(), name="latet-cleanups")

    def close(self) -> None:
        """Its metrics, its edition, its spaces' threads, then its connections to Valkey and PostgreSQL (its app's
        lifespan has closed its spaces)."""
        self.metrics.close()  # first: no scrape asks the stores once they close, and no report reaches a closed server
        self.edition.close()  # E1's listens on Valkey in a thread of its own: it stops before the connections go
        self.spaces.shutdown()
        self.bus.close()
        self.db.close()


def _asked(bus: Bus) -> None:
    """Valkey asked once, with the server's password. Raises ValkeyUnavailable, raised once the driver's error is
    handled, so that no traceback shows its words."""
    kind = ""
    try:
        bus.client.ping()
    except Exception as exc:  # its words can name the address: only its type goes
        kind = type(exc).__name__
    if kind:
        raise ValkeyUnavailable(
            f"Valkey (valkey_url) refused this instance or did not answer in time ({kind}): check its address and its "
            "password (valkey_password_file)"
        )


def reviewer_key_of(settings: ServerSettings, master: MasterKey) -> str | None:
    """The reviewer's own key, never another setting's file, a master-key version or a server credential."""
    path = settings.reviewer_key_file
    if path is None:
        return None
    for name in ("master_key_file", "database_password_file", "valkey_password_file", "metrics_token_file"):
        other = getattr(settings, name)
        same = False
        with contextlib.suppress(OSError, ValueError):  # unusable files are refused by their own read below
            same = other is not None and path.samefile(other)
        if same:
            raise ValueError(f"reviewer_key_file names the file of {name}: the reviewer needs a key of its own")
    key = read_secret_file(path, "reviewer_key_file")
    if master.is_version(key):
        raise ValueError("reviewer_key_file holds a version of the master key: the reviewer needs a key of its own")
    if key.startswith((ADMIN_PREFIX, TOKEN_PREFIX, COMPUTER_PREFIX, KEY_PREFIX)):
        raise ValueError("reviewer_key_file holds a server token or API key: the reviewer needs a key of its own")
    return key


def open_server(
    settings: ServerSettings,
    *,
    clock: Callable[[], float] = time.time,
    schema: str = "latet",
    prefix: str = "latet:",
    data: Path | None = None,
) -> Server:
    """An instance of a Latet server: its master key, PostgreSQL in ``schema`` (which must be this Latet's: `latet
    server migrate` brings it there), Valkey under ``prefix``, its models in ``data`` (Latet's data folder). Raises
    MasterKeyError (no master key, or not the one the database was first started with), SchemaMismatch, DatabaseError
    (PostgreSQL cannot be reached or asked, by the type of its error), ValueError (an address or a schema the driver
    refuses, quoting none of it; more text recognitions than processors: ``server_reading``; an unusable
    ``reviewer_key_file``) or ValkeyUnavailable: the
    instance does not start, and what it opened is closed."""
    server_reading(settings)  # first: nothing is opened for an instance that would not start
    if os.environ.get("SSLKEYLOGFILE"):  # said, never where: Python's TLS writes every session's keys to that file
        log.warning(
            "SSLKEYLOGFILE is set: the keys of this instance's TLS connections (to PostgreSQL, Valkey, the providers) "
            "are written to that file, and whoever reads it reads what they carry. Unset it"
        )
    master = MasterKey.load(settings)
    reviewer_key = reviewer_key_of(settings, master)  # before any service is opened
    if "LATET_LLM_API_KEY" in os.environ:
        log.warning(
            "LATET_LLM_API_KEY is set: a Latet server does not read it; the local reviewer's key comes from "
            "reviewer_key_file (LATET_SERVER_REVIEWER_KEY_FILE)"
        )
    db = Database(settings.database_url, schema=schema, connect_timeout=settings.database_connect_timeout)
    bus: Bus | None = None
    try:
        check_schema(db)
        check_master_key(db, master, clock=clock)  # before the bus: no message is signed or heard with a wrong key
        # Every message signed and checked; its calls share ``valkey_connections`` connections at most.
        bus = Bus(
            settings.valkey_url, prefix=prefix, key=master.derive(b"bus"), connections=settings.valkey_connections
        )
        _asked(bus)
        credentials = master.derive(b"credentials")
        edition = Community()  # the rules for everyone in force are the edition's: none in the Community Edition
        return Server(
            settings, db, bus, master, Identities(db, bus, key=credentials, clock=clock),
            AdminTokens(db, key=credentials, clock=clock, default_days=settings.admin_token_days),
            AuditLog(db, key=master.derive(b"audit"), clock=clock),
            SharedSettings(db, bus, master, clock=clock, rules=edition.shared_rules),
            ValkeySignIns(bus, master, clock=clock), clock=clock, data=data or data_dir(), reviewer_key=reviewer_key,
            edition=edition,
        )  # fmt: skip
    except BaseException:
        if bus is not None:
            bus.close()
        db.close()
        raise


async def listen(server: Server) -> None:
    """The instance's ear: every message of ``CHANNELS`` handed to ``server.heard``, ``server.listening`` set while they
    are heard. When the connection to Valkey drops, or the beat finds the ear silent (``Server.silenced``), what was
    published meanwhile is lost: the instance closes its spaces (in the background: it listens again without waiting
    for them) and forgets what it kept, so that all is read again from PostgreSQL, and listens again after
    ``RETRY_SECONDS``. Cancelled, it stops listening, ``listening`` cleared."""
    try:
        while True:
            ear = asyncio.create_task(_hear(server), name="latet-ear")
            server._ear = ear
            try:
                await asyncio.wait([ear])
            except asyncio.CancelledError:
                ear.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await ear
                raise
            if ear.cancelled():
                log.warning("this instance's ear went silent: its spaces are closed, and it listens again")
            elif (error := ear.exception()) is not None:
                log.warning(
                    "the connection to Valkey was lost (%s): this instance's spaces are closed", type(error).__name__
                )
            else:
                log.warning("the connection to Valkey ended: this instance's spaces are closed")
            server.listening.clear()
            await server.spaces.close(wait=False)
            server.identities.forget()
            server.shared.forget()
            await asyncio.sleep(RETRY_SECONDS)
    finally:
        server.listening.clear()
        server._ear = None


async def _hear(server: Server) -> None:
    """One subscription of the ear, until its connection drops (it raises) or it is cancelled."""
    server.beat_heard = time.monotonic()  # a subscription that begins has heard nothing yet, its own beat included
    async with contextlib.aclosing(server.bus.listen(*CHANNELS, ready=server.listening)) as messages:
        async for channel, message in messages:
            try:
                await server.heard(channel, message)
            except Exception as exc:  # one message that cannot be applied stops none of the others
                log.warning(
                    "a message on the %s channel could not be applied (%s)",
                    channel.partition(":")[0],
                    type(exc).__name__,
                )


# --- the app: its gate, its beat, its routes --------------------------------------------------------------------------

ANY_HOST = ("/_latet/health", "/_latet/ready")  # a load balancer asks an instance by its own address
STARTUP_SECONDS = 10.0  # how long an instance waits to hear Valkey before it answers
BEAT_SECONDS = 10.0
ALIVE_MS = 30_000  # an instance that has not said it is alive for this long counts no dashboard
FLUSH_SECONDS = 60.0  # what the log counted and has not said yet is said once a minute (``latet.server.logs.flush``)
READY_SECONDS = 1.0  # how long one look at the stores answers readiness, whoever asks (the pre-read's M2)
CROWDED_RETRY_AFTER = 2  # seconds an identity past its requests in flight is told to wait (``Retry-After``)
# WebSockets one identity holds open on an instance at most: each holds a connection to its destination, and only a turn
# being answered counts in its requests in flight.
SOCKETS_PER_IDENTITY = 64
LONG_PER_IDENTITY = 64
STREAMS_PER_IDENTITY = 4
_ENGINE = "/_latet/engine/v1/"  # the plugin's bridge, under /_latet where an identity's address leads
# The bridge's methods (``Bridge.routes``): by any other, or as a WebSocket, nothing of Latet's answers under /_latet.
_ENGINE_METHODS = frozenset({"GET", "POST", "DELETE"})
# What a computer calls under /u/<route>/_latet with the identity's own token (x-latet-token), written as it is read:
# the plugin's bridge (E0b), and the CLI's two (E0c), by POST alone.
_COMPUTER_CALLS = frozenset({"/_latet/admin/destinations", "/_latet/admin/user-name"})
# The CLI's two calls, which a computer makes at an agent's start: one at a time for each identity on an instance
# (``Retry-After: 1``), and this many in this window for each identity on the server, both counted together.
COMPUTER_CALL_LIMIT = 30
COMPUTER_CALL_WINDOW = 60.0
_UNAVAILABLE = "the server cannot answer now: try again in a moment"
_CROWDED_KEPT = 10_000  # identities whose requests past the cap are said once a minute each, at most
# The beat's own message, in a thread no other work holds up: a beat that waited behind a slow job (or behind the
# spaces' builds, in the default executor) would make the instance's ear look silent (R-14) and close every space.
_BEATING = ThreadPoolExecutor(1, thread_name_prefix="latet-beat")
# Readiness's pings, one for each store, in threads of their own: behind the credentials' threads, which anyone can keep
# busy with credentials refused, a load balancer's probe would time out and take the instance out of rotation.
_READINESS = ThreadPoolExecutor(2, thread_name_prefix="latet-ready")
_PRUNES = Paced()  # the administration log's prunes that failed: once an hour at most, each said


_SIGN_IN = f"{UI}/login"
# What a proxy in front of the server adds to a request (the client's address, the server's own name and network, the
# proxies on the way), by name and by the start of a name: the server's own, never handed to an identity's space, which
# would send it on to the identity's destination.
_PROXY_HEADERS = frozenset(
    {b"forwarded", b"via", b"x-real-ip", b"true-client-ip", b"x-client-ip", b"x-cluster-client-ip", b"cf-connecting-ip"}
)
_PROXY_PREFIX = b"x-forwarded"


def _plain(rest: str, raw: bytes) -> bool:
    """Whether a path under an identity's ``/_latet`` is written as it is read: no escape, no empty, ``.`` or ``..``
    segment. The gate judges the very path its space's router reads, and lets through only one written so."""
    return b"%" not in raw and all(segment not in ("", ".", "..") for segment in rest.split("/")[1:])


def computer_call(rest: str, method: str | None, raw: bytes) -> bool:
    """Whether ``rest``, the path after /u/<route> (``raw`` as the client wrote it), is a call a computer makes with the
    identity's own token: the bridge, by its routes' methods, or one of the CLI's two by POST. Written otherwise (an
    escape, an empty, ``.`` or ``..`` segment), by another method, or as a WebSocket (no method): none."""
    if method is None or not _plain(rest, raw):
        return False
    if rest.startswith(_ENGINE):
        return method in _ENGINE_METHODS
    return method == "POST" and rest in _COMPUTER_CALLS


def _unproxied(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """``headers`` without what a proxy added to them (``_PROXY_HEADERS``), in their order."""
    return [
        (name, value)
        for name, value in headers
        if (lowered := name.lower()) not in _PROXY_HEADERS and not lowered.startswith(_PROXY_PREFIX)
    ]


def _from_another_site(scope: Scope) -> bool:
    """Whether a browser says that a page of another site made the request (``sec-fetch-site``: ``cross-site`` or
    ``same-site``, which no page can write itself). A link a person types, opens from a bookmark or from the CLI says
    ``none``, or nothing at all."""
    return any(
        name == b"sec-fetch-site" and value.lower() in (b"cross-site", b"same-site")
        for name, value in scope.get("headers") or []
    )


class AnswerHeaders:
    """Every answer of the server says ``x-content-type-options: nosniff`` (the pre-read's M4): no JSON answer, refusal
    or file is ever read by a browser as a page of its own. The machines' calls, the sign-in and every answer under an
    identity's address (``/u/``: the bridge's originals and fakes, the user name's fake, the models' answers restored,
    the gate's own refusals there) also say ``cache-control: no-store``, whatever their app said: a token, a sign-in
    link, a session's cookie or a value is kept by no cache on the way, nor by the browser. No CORS header is added
    anywhere: nothing of the server is to be read from another origin."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        unkept = path in (SERVER_API, _SIGN_IN) or path.startswith((f"{SERVER_API}/", "/u/"))

        async def sending(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                if unkept:
                    headers = [(name, value) for name, value in headers if name.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"no-store"))
                if not any(name.lower() == b"x-content-type-options" for name, _ in headers):
                    headers.append((b"x-content-type-options", b"nosniff"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, sending)


class ServerGate:
    """In front of every request (the module's words): the server's own name only, but for health and readiness; an
    identity's address to its space, the dashboard's API to the space of the person signed in, everything else to the
    server's own routes. The identity's app gets ``scope["latet.identity"]``. What the gate answers itself is marked in
    the request's scope (``REFUSED``, the scope ``RequestLog`` holds) before it answers."""

    def __init__(self, app: ASGIApp, server: Server) -> None:
        self.app, self.server = app, server
        self.hosts = url_hosts(server.settings.url)
        self.origin = server.settings.url.lower()  # as a browser names the server's own pages (the settings' form)
        # Each identity's requests to its destinations that this instance serves now (none: not there), a WebSocket's
        # turn being answered among them; and its WebSockets open.
        self._in_flight: dict[str, int] = {}
        self._sockets: dict[str, int] = {}
        self._long: dict[str, int] = {}
        self._streams: dict[str, int] = {}
        self._dashboard_in_flight: dict[str, int] = {}
        self._bridge_in_flight: dict[str, int] = {}
        self._crowded: OrderedDict[tuple[str, str], Paced] = OrderedDict()  # past a cap, said once a minute each
        self._unbuilt = Paced()  # a space that could not be built, for a reason none of its own: once a minute
        self._calling: set[str] = set()  # the identities whose computer's call (one of the CLI's two) is under way here

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        if path not in ANY_HOST and not self._own_name(scope):
            await self._refuse(scope, send, 403, "host not allowed")
        elif path.startswith("/u/"):
            await self._address(scope, receive, send)
        elif path.startswith(f"{API}/"):
            await self._dashboard(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    def _own_name(self, scope: Scope) -> bool:
        """Whether the request names the server's own host, once (two Host headers name none)."""
        hosts = [value for name, value in scope.get("headers") or [] if name == b"host"]
        return len(hosts) == 1 and hosts[0].decode("latin-1").lower() in self.hosts

    async def _refuse(
        self, scope: Scope, send: Send, status: int, reason: str, *, retry_after: str | None = None
    ) -> None:
        """``status``, marked as the gate's own; ``retry_after``: the seconds a limit tells its client to wait."""
        scope[REFUSED] = True  # the log paces the 429s the gate gives apart from an upstream's (Task 13)
        headers = [(b"retry-after", retry_after.encode())] if retry_after else []
        await refuse(scope, send, status, reason, headers=headers)

    async def _address(self, scope: Scope, receive: Receive, send: Send) -> None:
        """``/u/<route key>/...``: the identity whose address it is; its proxy routes by the key alone, and under
        ``/_latet`` its computer's calls alone (``computer_call``: its bridge, and the CLI's two by POST), with its own
        token, nothing else. A web page is refused before anything is looked up (it would spend the limit of its
        visitor's address), and so is an address written otherwise than the key and a ``/``. An address this instance's
        memory does not hold is looked up one request at a time (``_route_checked``), as its token is
        (``machines.secret_identity``): one identity's flood under way when its few seconds in the memory end takes one
        lookup of each, not one for each request. Its space gets the address the request came by (``latet.space``,
        which holds the route key: compared, never written), which no client can set."""
        path: str = scope["path"]
        route, _, rest = path.removeprefix("/u/").partition("/")
        rest = "/" + rest
        prefix = f"/u/{route}".encode()
        raw: bytes = scope.get("raw_path") or path.encode()
        if from_a_page(scope):
            await self._refuse(scope, send, 403, "browser requests are not accepted on an identity's address")
            return
        if not is_route_key(route) or not (raw == prefix or raw.startswith(prefix + b"/")):
            await self._refuse(scope, send, 403, "there is no such address")
            return
        inner_raw = raw[len(prefix) :] or b"/"
        bridge = rest.startswith("/_latet")
        if bridge and not computer_call(rest, scope.get("method"), inner_raw):
            await self._refuse(scope, send, 404, "there is no such route")
            return
        bucket = bucket_of(client_of(scope))
        server = self.server
        identity = server.identities.remembered(route=route)
        if identity is None:
            try:
                identity, limited = await server.lookups.run(
                    "route", server.known_routes.hashed(route), server.identities.generation,
                    functools.partial(self._route_checked, route, bucket),
                )  # fmt: skip
            except StoresUnavailable as exc:
                await self._refuse(scope, send, 503, _UNAVAILABLE, retry_after=exc.retry_after)
                return
            if limited:
                reason = "too many unknown addresses from this address: wait a minute"
                await self._refuse(scope, send, 429, reason, retry_after=RETRY_AFTER)
                return
            if identity is None:
                await self._refuse(scope, send, 403, "there is no such address")
                return
        if bridge:
            try:
                owner = await secret_identity(server, token_of(scope), bucket)
            except TooManyMisses:
                reason = "too many tokens refused from this address: wait a minute"
                await self._refuse(scope, send, 429, reason, retry_after=RETRY_AFTER)
                return
            except StoresUnavailable as exc:
                await self._refuse(scope, send, 503, _UNAVAILABLE, retry_after=exc.retry_after)
                return
            if owner is None or owner.id != identity.id:
                await self._refuse(scope, send, 401, "unauthorized")
                return
        changes = {"path": rest, "raw_path": inner_raw, "latet.space": f"{server.settings.url}/u/{route}"}
        if rest in _COMPUTER_CALLS:  # computer_call let it through: by POST, with the owner's token
            await self._computer(scope, receive, send, identity, changes)
            return
        await self._serve(scope, receive, send, identity, changes, counted=not bridge)

    async def _computer(
        self, scope: Scope, receive: Receive, send: Send, identity: Identity, changes: dict[str, Any]
    ) -> None:
        """One of the CLI's two calls, with its identity's own token: an API key has no computer, and its settings are
        the administration's (403). One at a time for each identity on this instance (429, ``Retry-After: 1``), and
        ``COMPUTER_CALL_LIMIT`` a ``COMPUTER_CALL_WINDOW`` for each on the server, both counted together in Valkey's
        limiter (429, ``Retry-After``; Valkey not answering, 503 with ``Retry-After``); an identity the limiter found
        past it is remembered so in this instance's memory until its window ends (``Server.limits``), its calls refused
        on the event loop meanwhile, no thread taken, no round trip. Each refusal is the gate's own, made before the
        space is asked; then its space, its mark released however the call ends. The space's handler reads the body
        and does the work in the server's work threads."""
        if identity.kind != "person":
            scope[REFUSED] = True
            await JSONResponse({"error": NO_COMPUTER}, status_code=403)(scope, receive, send)
            return
        key, limited = f"computer-call:{identity.id}", "too many calls of this identity's computers: wait a minute"
        if self.server.limits.refuses(key):
            await self._refuse(scope, send, 429, limited, retry_after=str(int(COMPUTER_CALL_WINDOW)))
            return
        if identity.id in self._calling:
            reason = "this identity's computer is making this call already: try again in a moment"
            await self._refuse(scope, send, 429, reason, retry_after="1")
            return
        self._calling.add(identity.id)
        try:
            try:
                left = await pre_auth(self.server.bus.slot, key, COMPUTER_CALL_LIMIT, COMPUTER_CALL_WINDOW)
            except StoresUnavailable as exc:
                await self._refuse(scope, send, 503, _UNAVAILABLE, retry_after=exc.retry_after or "1")
                return
            if left is not None:  # past its limit, for ``left`` seconds more
                self.server.limits.past(key, left)
                await self._refuse(scope, send, 429, limited, retry_after=str(int(COMPUTER_CALL_WINDOW)))
                return
            await self._serve(scope, receive, send, identity, changes, counted=False)
        finally:  # answered, refused, failed, its client gone, cancelled
            self._calling.discard(identity.id)

    async def _route_checked(self, route: str, bucket: str) -> tuple[Identity | None, bool]:
        """The identity whose address ``route`` is, checked by the limiter of ``bucket`` (``machines.checked``), and
        whether the limit refused it; one lookup at a time for each address (``Server.lookups``). Past its limit as this
        instance remembers it: refused here, no thread taken, no round trip, but an address found valid elsewhere, read
        in Valkey a batch at a time (``machines.admitted``). Raises StoresUnavailable."""
        server, key = self.server, miss_key(bucket)
        if (elsewhere := await admitted(server.known_routes, server.limits, route, key)) is None:
            return None, True
        return await pre_auth(
            checked, server.bus, server.known_routes, server.limits, route, key, LOOKUP_LIMIT, LOOKUP_WINDOW,
            self._route_of, elsewhere,
        )  # fmt: skip

    def _route_of(self, route: str) -> Identity | None:
        """In the gate's threads: the enabled identity whose address ``route`` is, its visit noted (in a thread of its
        own: ``Identities.seen``)."""
        identity = self.server.identities.by_route(route)
        if identity is not None:
            self.server.identities.seen(identity.id)
        return identity

    def _from_elsewhere(self, scope: Scope) -> bool:
        """Whether a page of another site made the request, as a browser says: ``sec-fetch-site`` ``cross-site`` or
        ``same-site``, or an origin other than the server's own (a browser that sends no ``sec-fetch-site`` still
        names one on a request of another origin's page)."""
        if _from_another_site(scope):
            return True
        origins = [value for name, value in scope.get("headers") or [] if name == b"origin"]
        return any(origin.decode("latin-1").lower() != self.origin for origin in origins)

    async def _dashboard(self, scope: Scope, receive: Receive, send: Send) -> None:
        """The dashboard's API: the space of the person signed in (the cookie read as the space's app reads it, from the
        first Cookie header), whose app checks the rest (``_guarded``: the origin, the header of a change). A request a
        page of another site made is refused first, nothing looked up: a sibling site can plant a session cookie of the
        server's name in its visitor's browser, and its every request would spend the limit of the visitor's address
        (the dashboard's own requests are its page's, ``same-origin``). A session this instance found open in the last
        few seconds is answered for from its memory (``_session_held``); any other is checked by the limiter tokens and
        addresses have (``machines.checked``), one lookup at a time for each session (``Server.lookups``), joined only
        within the sign-ins' generation (an end heard since: a lookup afresh)."""
        if self._from_elsewhere(scope):
            await self._refuse(scope, send, 403, "requests from another site are not accepted here")
            return
        server = self.server
        sid = cookie_parser(Headers(scope=scope).get("cookie", "")).get(COOKIE, "")
        identity = self._session_held(sid) if sid else None
        if sid and identity is None:
            try:
                identity, limited = await server.lookups.run(
                    "session", server.known_sessions.hashed(sid), server.sign_ins.generation,
                    functools.partial(self._session_checked, sid, bucket_of(client_of(scope))),
                )  # fmt: skip
            except StoresUnavailable as exc:
                await self._refuse(scope, send, 503, _UNAVAILABLE, retry_after=exc.retry_after)
                return
            if limited:
                reason = "too many sessions refused from this address: wait a minute"
                await self._refuse(scope, send, 429, reason, retry_after=RETRY_AFTER)
                return
        if identity is None:
            await self._refuse(scope, send, 401, "unauthorized")
            return
        await self._serve(scope, receive, send, identity, {}, counted=False)

    def _session_held(self, sid: str) -> Identity | None:
        """On the event loop: the enabled person a dashboard session is of, from this instance's memory alone, the
        session found open and the person found in the last few seconds (``ValkeySignIns.held``, ``Identities.held``):
        no thread, no store, so that one person's flood of dashboard requests takes one lookup of the credentials'
        threads every few seconds and no more (``Server.lookups``). A session's end, a person disabled, removed or a
        token of theirs revoked, on any instance, drops what the memory holds at once (``Server.heard``). None when it
        holds neither."""
        subject = self.server.sign_ins.held(sid)
        identity = self.server.identities.held(subject) if subject else None
        if identity is None or identity.disabled or identity.kind != "person":
            return None
        return identity

    async def _session_checked(self, sid: str, bucket: str) -> tuple[Identity | None, bool]:
        """The person ``sid`` is a session of, checked by the limiter of ``bucket`` (``machines.checked``), and whether
        the limit refused it. Past its limit as this instance remembers it: refused here, no thread taken, no round
        trip, but a session found valid elsewhere (opened on another instance), read in Valkey a batch at a time
        (``machines.admitted``). Raises StoresUnavailable."""
        server, key = self.server, miss_key(bucket)
        if (elsewhere := await admitted(server.known_sessions, server.limits, sid, key)) is None:
            return None, True
        return await pre_auth(
            checked, server.bus, server.known_sessions, server.limits, sid, key, LOOKUP_LIMIT, LOOKUP_WINDOW,
            self._signed_in, elsewhere,
        )  # fmt: skip

    def _signed_in(self, sid: str) -> Identity | None:
        """In the gate's threads: the enabled person a dashboard session is of, while the session lasts."""
        subject = self.server.sign_ins.subject_of(sid)
        identity = self.server.identities.get(subject) if subject else None
        if identity is None or identity.disabled or identity.kind != "person":
            return None
        return identity

    async def _serve(
        self, scope: Scope, receive: Receive, send: Send, identity: Identity, changes: dict[str, Any], *, counted: bool
    ) -> None:
        """The request handed to the identity's space, as its app reads it (``changes``, and whose it is), without what
        a proxy added to it (``_unproxied``). ``counted``: a request to the identity's destinations, held within
        ``requests_in_flight`` until it ends."""
        inner = {**scope, **changes, "headers": _unproxied(scope.get("headers") or []), "latet.identity": identity.id}
        limits: list[tuple[dict[str, int], int, str]] = []
        if scope["type"] == "http":
            dashboard = inner["path"].startswith(f"{API}/")
            if space_long(inner):
                limits.append((self._long, LONG_PER_IDENTITY, "long connections"))
                if dashboard:
                    limits.append((self._streams, STREAMS_PER_IDENTITY, "dashboard streams"))
            elif dashboard:
                limits.append(
                    (self._dashboard_in_flight, self.server.settings.requests_in_flight, "dashboard requests")
                )
            elif inner["path"].startswith(_ENGINE):
                limits.append((self._bridge_in_flight, self.server.settings.requests_in_flight, "bridge requests"))
        for counts, cap, what in limits:
            if counts.get(identity.id, 0) >= cap:
                self._crowd(identity.id, what, cap, "")
                reason = f"latet: this identity has {cap} {what} on this instance, the most it may: try again soon"
                await self._too_many(scope, send, reason, f"too many {what}: try again")
                return
        for counts, _, _ in limits:
            counts[identity.id] = counts.get(identity.id, 0) + 1
        try:
            try:
                app = await self.server.spaces.app(identity)
            except SpaceUnavailable:  # NotListening too: no listening space on this instance now
                await self._refuse(
                    scope, send, 503, "this identity's space is not available here now: try again in a moment"
                )
                return
            except IdentityUnavailable:  # gone or disabled, as PostgreSQL has it now
                await self._refuse(scope, send, 401, "unauthorized")
                return
            except Exception as exc:  # its build failed: refused, said by its type
                if count := self._unbuilt.due():
                    log.warning(
                        "a space could not be built on this instance (%s): its request was refused%s",
                        described(exc),
                        f" ({count} times since the line before)" if count > 1 else "",
                    )
                await self._refuse(scope, send, 503, _UNAVAILABLE)
                return
            if not counted:
                await app(inner, receive, send)
                return
            if scope["type"] == "websocket":
                await self._socket(scope, receive, send, app, inner, identity.id)
                return
            if (refused := self.begin(identity.id)) is not None:
                await self._too_many(scope, send, refused, "too many requests in flight: try again")
                return
            try:
                await app(inner, receive, send)
            finally:
                self.end(identity.id)
        finally:  # release synchronously, including cancellation or failure during the space lookup
            self._release_surface(identity.id, limits)

    @staticmethod
    def _release_surface(identity_id: str, limits: list[tuple[dict[str, int], int, str]]) -> None:
        for counts, _, _ in limits:
            left = counts[identity_id] - 1
            if left:
                counts[identity_id] = left
            else:
                del counts[identity_id]

    def begin(self, identity_id: str) -> str | None:
        """A request of the identity's to its destinations, or a WebSocket's turn, counted in its requests in flight:
        None; past ``requests_in_flight``, the words that refuse it (said in the log once a minute for each)."""
        cap, held = self.server.settings.requests_in_flight, self._in_flight.get(identity_id, 0)
        if held >= cap:
            self._crowd(identity_id, "requests in flight", cap, "(requests_in_flight)")
            return (
                f"latet: this identity has {cap} requests in flight on this instance, the most it may: try again soon"
            )
        self._in_flight[identity_id] = held + 1
        return None

    def end(self, identity_id: str) -> None:
        """A request or a turn that ``begin`` counted has ended, however it ended."""
        left = self._in_flight.get(identity_id, 1) - 1
        if left > 0:
            self._in_flight[identity_id] = left
        else:
            self._in_flight.pop(identity_id, None)

    async def _socket(
        self, scope: Scope, receive: Receive, send: Send, app: ASGIApp, inner: dict[str, Any], identity_id: str
    ) -> None:
        """A WebSocket to the identity's destinations: ``SOCKETS_PER_IDENTITY`` open on this instance at most, each
        holding a connection to its destination, and each of its turns counted in its requests in flight while it is
        answered (``_Turns``, which ``gateway.ws`` asks), never the socket's whole life: an idle session holds no
        place."""
        held = self._sockets.get(identity_id, 0)
        if held >= SOCKETS_PER_IDENTITY:
            self._crowd(identity_id, "WebSockets open", SOCKETS_PER_IDENTITY, "")
            reason = (
                f"latet: this identity has {SOCKETS_PER_IDENTITY} WebSockets open on this instance, the most it may: "
                "close one"
            )
            await self._too_many(scope, send, reason, "too many WebSockets open: close one")
            return
        self._sockets[identity_id] = held + 1
        try:
            await app({**inner, "latet.turns": _Turns(self, identity_id)}, receive, send)
        finally:
            left = self._sockets.get(identity_id, 1) - 1
            if left > 0:
                self._sockets[identity_id] = left
            else:
                self._sockets.pop(identity_id, None)

    def _crowd(self, identity_id: str, what: str, cap: int, setting: str) -> None:
        """An identity past a cap (``what``: its requests in flight, its WebSockets open): said once a minute for each,
        with how many since the line before."""
        paced = self._crowded.pop((identity_id, what), None) or Paced()
        self._crowded[(identity_id, what)] = paced  # the latest last: the one forgotten first is the one longest quiet
        while len(self._crowded) > _CROWDED_KEPT:
            self._crowded.popitem(last=False)
        if count := paced.due():
            log.warning(
                "%s has %d %s on this instance, the most one identity may%s: more are refused%s",
                identity_id,
                cap,
                what,
                f" {setting}" if setting else "",
                f" ({count} since the line before)" if count > 1 else "",
            )

    async def _too_many(self, scope: Scope, send: Send, reason: str, short: str) -> None:
        """429 with ``Retry-After`` and ``reason``, over HTTP and to a WebSocket's handshake when the server can answer
        one so (else it is closed, 4429, with ``short``: a close's reason holds 123 bytes at most)."""
        scope[REFUSED] = True
        body = json.dumps({"error": reason}, separators=(",", ":")).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"retry-after", str(CROWDED_RETRY_AFTER).encode()),
        ]
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 429, "headers": headers})
            await send({"type": "http.response.body", "body": body})
        elif "websocket.http.response" in (scope.get("extensions") or {}):
            await send({"type": "websocket.http.response.start", "status": 429, "headers": headers})
            await send({"type": "websocket.http.response.body", "body": body})
        else:
            await send({"type": "websocket.close", "code": 4429, "reason": short})


class _Turns:
    """A WebSocket's turns, each counted in its identity's requests in flight while it is answered (``latet.turns`` in
    the scope its space's app gets: ``gateway.ws`` begins one when the client asks, and ends it at the answer's last
    event, or when the socket ends). On the event loop, as the gate."""

    def __init__(self, gate: ServerGate, identity_id: str) -> None:
        self._gate, self._identity = gate, identity_id

    def begin(self) -> str | None:
        """A turn counted: None; past the cap, the words that refuse it."""
        return self._gate.begin(self._identity)

    def end(self) -> None:
        self._gate.end(self._identity)


class _Readiness:
    """Whether the instance is ready (``GET /_latet/ready``): it hears Valkey (``listening``, looked at each time), and
    PostgreSQL and Valkey answer, asked in readiness's own threads (``_READINESS``) at most once every
    ``READY_SECONDS`` whoever asks, one look at a time (the pre-read's M2: anyone who reaches the port may ask, by any
    address, and a flood costs no store more than that). Why it is not ready is said in the log, once a minute
    (``Paced``), never in the answer (S15)."""

    def __init__(self, server: Server) -> None:
        self.server = server
        self._at: float | None = None
        self._stores = (False, False)
        self._asking: asyncio.Task[tuple[bool, bool]] | None = None
        self._unready = Paced()

    async def ready(self) -> bool:
        database, valkey = await self._answered()
        listening = self.server.listening.is_set()
        if database and valkey and listening:
            return True
        if count := self._unready.due():
            log.warning(
                "not ready: database %s, valkey %s, listening %s%s",
                database,
                valkey,
                listening,
                f" ({count} times since the line before)" if count > 1 else "",
            )
        return False

    async def _answered(self) -> tuple[bool, bool]:
        if self._at is not None and time.monotonic() - self._at < READY_SECONDS:
            return self._stores
        if self._asking is None or self._asking.done():
            self._asking = asyncio.create_task(self._ask(), name="latet-ready")
        return await asyncio.shield(self._asking)

    async def _ask(self) -> tuple[bool, bool]:
        database, valkey = await asyncio.gather(_answers(self.server.db.ping), _answers(self.server.bus.ping))
        self._at, self._stores = time.monotonic(), (database, valkey)
        return self._stores


async def _answers(ping: Callable[[], bool]) -> bool:
    """Whether a store answers its ping, asked in readiness's own threads: whatever waits for the credentials' threads,
    a probe does not wait behind it."""
    try:
        return bool(await asyncio.get_running_loop().run_in_executor(_READINESS, ping))
    except Exception:  # not answering, however: not ready (the readiness's line says which store)
        return False


async def beat(server: Server) -> None:
    """Every ``BEAT_SECONDS`` while the instance runs, in this order: its ear is cancelled when it has heard no beat of
    its own for three beats (R-14: a connection that delivers nothing, which ``listen`` then treats as lost); it says it
    is alive (the dashboards it counts are counted while it is) and publishes its beat on ``beat``, ``{"instance":
    <id>}``, in a thread of its own that no other work holds up; the spaces' check (``Server.check``) and the
    administration log's prune and chain check (once an hour, on one instance of the server) run, each as a job of its
    own, one at a time, so that neither holds up the next beat; and once a minute what the log counted and has not said
    yet is said (``latet.server.logs.flush``), and what the administration counted of its refusals and has not written
    yet is written, as a job too (``admin.Refusals.flush``). A step that fails, and a round that fails anywhere else,
    is said by its error's type, once a minute each, and the next beat comes all the same.

    A beat that wakes more than a beat late was held with the event loop, and so was the ear, which reads what was
    published meanwhile once the loop is let go: its silence is judged a beat later, never at once (two large restores
    at once held an instance's loop past three beats, and it closed every space), and the lateness is said once a
    minute."""
    jobs: dict[str, asyncio.Task[None]] = {}
    failures = {name: Paced() for name in ("beat", "check", "audit", "refusals", "round")}
    held = Paced()  # the loop held past a beat, said once a minute
    flushed = time.monotonic()
    loop = asyncio.get_running_loop()
    slept = time.monotonic() - BEAT_SECONDS  # as if it had slept one beat: on time
    judged_from = 0.0  # the ear's silence is judged from then on
    try:
        while True:
            now = time.monotonic()
            if (late := now - slept - BEAT_SECONDS) > BEAT_SECONDS:
                judged_from = now + BEAT_SECONDS
                if count := held.due():
                    log.warning(
                        "this instance's event loop was held %.1f seconds past its beat: its ear's silence is judged "
                        "a beat later%s",
                        late,
                        f" ({count} times since the line before)" if count > 1 else "",
                    )
            try:
                if now >= judged_from and server.ear_silent(3 * BEAT_SECONDS):
                    server.silenced()
                try:
                    await loop.run_in_executor(_BEATING, _alive, server)
                except Exception as exc:  # Valkey away: the ear notices it too, and the next beat tries again
                    _failed(failures["beat"], "beat", exc)
                _start(jobs, failures, "check", server.check)
                _start(jobs, failures, "audit", lambda: asyncio.to_thread(_audit, server))
                if time.monotonic() - flushed >= FLUSH_SECONDS:
                    flushed = time.monotonic()
                    logs.flush()
                    _start(jobs, failures, "refusals", lambda: asyncio.to_thread(server.refusals.flush, server.audit))
            except Exception as exc:  # whatever it was, the beat goes on
                _failed(failures["round"], "beat's round", exc)
            slept = time.monotonic()
            await asyncio.sleep(BEAT_SECONDS)
    finally:
        for task in jobs.values():
            task.cancel()
        for task in jobs.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def _alive(server: Server) -> None:
    """In the beat's thread: the instance alive for ``ALIVE_MS``, and its beat published, for its own ear to hear."""
    bus = server.bus
    bus.client.set(bus.key(f"instance:{server.instance}"), b"1", px=ALIVE_MS)
    bus.publish("beat", {"instance": server.instance})


def _audit(server: Server) -> None:
    """In a worker thread, on the one instance that takes the hour's key: the administration log pruned of what is
    older than the settings' ``audit_retention_days``, and its chain walked from its anchor whatever the prune did, the
    verdict kept sealed and stamped in ``audit:chain`` (``Server.chain``: the status and E0c's metrics read it, and
    believe it while it is fresh, never one older than they read). A prune made to fail (a row a writer of the table
    holds, past the statement's bound) is said by its error's type and stops no verdict: one that stopped would let an
    alarm on a broken chain clear by itself. A chain broken is said by the number of the entry, nothing of it."""
    bus = server.bus
    if not bus.client.set(bus.key("job:audit-prune"), server.instance, nx=True, px=3_600_000):  # one instance an hour
        return
    try:
        server.audit.prune(server.settings.audit_retention_days)
    except Exception as exc:  # said by its type, as the beat says a job's failure: a driver's words can quote a row
        if count := _PRUNES.due():
            log.warning(
                "the administration log's prune failed (%s): its chain is checked all the same, and the next hour's "
                "job prunes it%s",
                described(exc),
                f" ({count} times since the line before)" if count > 1 else "",
            )
    checked = server.audit.verify()
    server.chain.keep(checked)
    if not checked.ok:
        log.error("the administration log's chain is broken at entry %s", checked.broken_at)


def _failed(paced: Paced, name: str, error: BaseException) -> None:
    if count := paced.due():
        log.warning(
            "this instance's %s failed (%s): the next beat tries again%s",
            name,
            described(error),
            f" ({count} times since the line before)" if count > 1 else "",
        )


def _start(
    jobs: dict[str, asyncio.Task[None]],
    failures: dict[str, Paced],
    name: str,
    work: Callable[[], Awaitable[object]],
) -> None:
    """``work`` as the beat's job ``name``, unless the one before it has not ended."""
    running = jobs.get(name)
    if running is not None and not running.done():
        return

    async def job() -> None:
        try:
            await work()
        except Exception as exc:  # the next beat runs it again
            _failed(failures[name], name, exc)

    jobs[name] = asyncio.get_running_loop().create_task(job(), name=f"latet-{name}")


def create_server_app(server: Server) -> Starlette:
    """An instance's ASGI app (`latet server run` serves it, behind E0c's ``Operations``): ``RequestLog`` (outermost: it
    writes each request's line), ``AnswerHeaders``, ``BodyLimit`` with the settings' ``max_request_mb``, then
    ``ServerGate`` in front of the server's own routes: health and readiness, the dashboard's page and its sign-in
    (which a page of another site opens for no one), the machines' calls. Its lifespan starts the instance's ear and
    beat, and closes its spaces at the end (``Server.close`` is its owner's)."""
    url = server.settings.url
    readiness = _Readiness(server)

    async def health(_: Request) -> Response:
        # Its state and nothing else (anyone who reaches the port may ask): `server` tells the dashboard's sign-in
        # page to say `latet dashboard` on a connected computer (E0c). The version is the administration's (`status`).
        # `docs`: whether it serves the documentation of its version, which the sign-in page opens from here (it
        # cannot ask /me); not identity-specific.
        return JSONResponse({"ok": True, "server": True, "docs": docs_headers(DOCS_DIR) is not None})

    async def ready(_: Request) -> Response:
        if await readiness.ready():
            return JSONResponse({"ready": True})
        return JSONResponse({"ready": False}, status_code=503)  # why is for the log and the metrics, not whoever asks

    async def root(_: Request) -> Response:
        return RedirectResponse(f"{UI}/", status_code=307)

    async def ui_root(_: Request) -> Response:
        return RedirectResponse(f"{UI}/", status_code=308)

    async def ui(request: Request) -> Response:
        return ui_file(WEB_DIR, request.path_params.get("path", ""))

    async def docs_root(_: Request) -> Response:
        return RedirectResponse(f"{DOCS}/", status_code=308)

    async def docs(request: Request) -> Response:
        # The documentation of the server's version, public as on a computer: its links on the dashboard open it here.
        return docs_file(DOCS_DIR, request.path_params.get("path", ""))

    def signed(code: str) -> str | None:
        """In the gate's threads: a session for the person the one-time code was issued to (the code spent whatever it
        answers), when they are an enabled person as PostgreSQL has them now, and the token that asked for the code is
        theirs still (revoked meanwhile, on any instance, whatever the memory of the one that issued it held: none);
        None otherwise. The token is checked again once the session is opened, and the session ended when it is gone: a
        revocation whose end of the person's sessions ran between the check and the opening leaves none. The session is
        known valid from its first request (``Server.known_sessions``)."""
        found = server.sign_ins.redeemed(code)
        subject, token = found if found is not None else ("", None)
        identity = server.identities.current(subject) if subject else None
        if identity is None or identity.disabled or identity.kind != "person":
            return None
        if token is None or not server.identities.holds_token(identity.id, token):
            return None
        sid = server.sign_ins.open(identity.id)
        if not server.identities.holds_token(identity.id, token):
            server.sign_ins.end(sid=sid)
            return None
        server.known_sessions.keep(sid)
        return sid

    def signed_in(code: str, key: str, known: bool) -> tuple[str | None, bool]:
        """In the credentials threads: a code admitted as issued takes no slot and bypasses remembered limits. Other
        codes take a slot before redeem, returned only on success; an issued code opening nothing counts one miss
        after redeem. No known set is read here, and a code is never re-kept."""
        if not known and (
            server.limits.refuses(key) or not counted(server.bus, server.limits, key, LOOKUP_LIMIT, LOOKUP_WINDOW)
        ):
            return None, True
        sid = signed(code)
        if known and sid is None:
            counted(server.bus, server.limits, key, LOOKUP_LIMIT, LOOKUP_WINDOW)
        elif not known and sid is not None:
            given_back(server.bus, key)
        return sid, False

    async def redeem(code: str, key: str, hashed: bytes) -> tuple[str | None, bool]:
        try:
            return await pre_auth(signed_in, code, key, True)
        finally:
            server.redeeming.discard(hashed)

    def redeem_done(task: asyncio.Task[tuple[str | None, bool]]) -> None:
        server._redeeming_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # pre_auth already reports store failures; cancelled requesters no longer await them

    def back(request: Request, why: Literal["expired", "limited", "busy"]) -> Response:
        """A link that signs no one in, back to the dashboard's page, which says why in its reader's language: it opens
        nothing (``expired``: used, late, of no one, opened from another site), too many links were tried from its
        address (``limited``), or the server could not look it up now (``busy``). A browser shows a page, never the
        words of a JSON body; the address it is sent to holds nothing of the code. Marked as the server's own refusal:
        anyone makes one at will, and the log paces them."""
        request.scope[REFUSED] = True
        return RedirectResponse(f"{UI}/?login={why}", status_code=303, headers=_SECURITY_HEADERS)

    async def login(request: Request) -> Response:
        if _from_another_site(request.scope):  # login CSRF: a page, a mail or a chat opened a person's link in another
            # person's browser; nobody is signed in there, and the code is left for its owner
            return back(request, "expired")
        code = request.query_params.get("code", "")
        if not code:
            return back(request, "expired")
        # Issued codes have a short-lived known set of their own, with no instance memory. Admission happens once
        # on the loop; the credentials job reads no known set and returns only a slot it actually took.
        key, limited, sid = miss_key(bucket_of(client_of(request.scope))), True, None
        try:
            known = await admitted(server.known_codes, server.limits, code, key)
            if known is None:
                return back(request, "limited")
            if known:
                hashed = server.known_codes.hashed(code)
                if hashed in server.redeeming:
                    return back(request, "expired")
                server.redeeming.add(hashed)
                job = redeem(code, key, hashed)
                try:
                    task = asyncio.create_task(job)
                except BaseException:
                    job.close()
                    server.redeeming.discard(hashed)
                    raise
                server._redeeming_tasks.add(task)
                task.add_done_callback(redeem_done)
                sid, limited = await asyncio.shield(task)
            else:
                sid, limited = await pre_auth(signed_in, code, key, known)
        except StoresUnavailable:  # a store that did not answer, or the credentials' threads busy
            return back(request, "busy")
        if limited:  # its address past its misses: the code is not redeemed
            return back(request, "limited")
        if sid is None:
            return back(request, "expired")
        response = RedirectResponse(f"{UI}/", status_code=303, headers=_SECURITY_HEADERS)
        response.set_cookie(
            COOKIE, sid, max_age=int(SESSION_TTL), path="/_latet", httponly=True, samesite="strict",
            secure=url.startswith("https://"),
        )  # fmt: skip
        return response

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        server.listening = asyncio.Event()
        tasks = [
            asyncio.create_task(listen(server), name="latet-listen"),
            asyncio.create_task(beat(server), name="latet-beat"),
        ]
        try:
            try:
                await asyncio.wait_for(server.listening.wait(), STARTUP_SECONDS)
            except TimeoutError:
                log.warning("Valkey's messages are not heard yet: /_latet/ready answers 503 until they are")
            started = {"instance": server.instance, "version": __version__}
            await in_store(server.audit.record, "server", "server.started", None, started)
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            await server.spaces.close()

    return Starlette(
        routes=[
            Route("/_latet/health", health, methods=["GET"]),
            Route("/_latet/ready", ready, methods=["GET"]),
            Route("/", root, methods=["GET"]),
            Route(UI, ui_root, methods=["GET", "HEAD"]),
            Route(f"{UI}/login", login, methods=["GET"]),
            Route(f"{UI}/{{path:path}}", ui, methods=["GET", "HEAD"]),
            Route(DOCS, docs_root, methods=["GET", "HEAD"]),
            Route(f"{DOCS}/{{path:path}}", docs, methods=["GET", "HEAD"]),
            *machine_routes(server),
            *admin_routes(server),  # last, behind the same gate, body limit and log
        ],
        middleware=[
            Middleware(RequestLog),  # outermost: the line of every request, the gate's refusals included
            Middleware(AnswerHeaders),
            Middleware(BodyLimit, limit=server.settings.max_request_mb * 1024 * 1024),  # before anything is buffered
            Middleware(ServerGate, server=server),
        ],
        lifespan=lifespan,
    )
