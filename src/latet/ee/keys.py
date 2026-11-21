"""API keys and their control: made, rotated, disabled and enabled by a server's administrators from the People and
groups page, their control given to people or groups (`latet server controllers` too); and KeySpace, which hands a
controller's request to the key's own app (/_latet/api/v1/ee/keys/{id}/space/…), with the key as the identity. A key's
space lets people in: it closes once a trial ended, and stays open while a paid license is paused (R3, R4).

A path names a key, a person or a group by its id alone, and no refusal quotes what the request said (the request log
keeps every path; Task 8's rule). What a change of a key's control answers is read in its transaction. In a key's space
the person's dashboard's guard is asked first, then whether they may open it (they control the key, themselves or
through a group, or they may manage keys: a way into every key's history and vault, which applies to everyone, and so
takes a session opened with the company's sign-in while the sign-in settings ask for one, the user's decision (c); a
controller's way in is their own work, as the user role, and does not); a path that no route of the dashboard serves but
its catch-all is answered
404 here; then the key's app is built; a request that may change something there is then recorded in the
administration log, by that person, its path written as its route's template (F7: never a project's id, nor a
value's), before it is handed on: the key's app writes the change itself (F8). But for the forget of a value of the
key's vault (``DELETE /vault/{id}``), which the key's app takes as `latet server keys forget` does and records itself,
in the forget's own transaction (``key.forgotten`` by that person). A stream open there (a dashboard's live
view) is the person's, counted with their own by the server's gate (``spaces._long``), and asks again every
WATCH_SECONDS, and at the changes of people or of an identity this instance hears (once a second at most), whether the
person may still open the space, and ends when they may not (O2)."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import re
import threading
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Final

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Match, Route

from latet.ee.access import ACTING, API, EE, IDENTITY
from latet.ee.license import NotLicensed, TrialEnded, require_serving
from latet.ee.people import Changing
from latet.ee.people_api import subject_by_id, subject_of
from latet.ee.routes import VIA, EeRoute, Refused, Who, company_sign_in, json_body
from latet.ee.server import settings_in
from latet.floor import FloorUnavailable, unavailable
from latet.logline import Paced
from latet.server.grants import GATEWAY
from latet.server.identities import IDENTITY_ID, NAME_LENGTH, NoSuchIdentity, NoSuchToken, SeveralTokens, clean
from latet.server.machines import client_of
from latet.server.spaces import IdentityUnavailable, SpaceUnavailable
from latet.server.spaces import _long as space_long
from latet.server.tokens import route_key

if TYPE_CHECKING:
    from psycopg import Connection
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from latet.api.rest import Dashboard
    from latet.ee.access import Access
    from latet.ee.api import Runner
    from latet.ee.license import Licensing
    from latet.ee.people import People
    from latet.ee.settings import EnterpriseSettings
    from latet.server.app import Server
    from latet.server.identities import Identity

log = logging.getLogger(__name__)

Names = dict[str, tuple[str, str]]  # a person's or a group's id → its kind and its name
WATCH_SECONDS: Final = 5.0  # O2: how often a stream open in a key's space asks again whether it may stay open
_READS = ("GET", "HEAD")
# The change of a key's space recorded by the key's app itself, in its own transaction: a value of its vault forgotten
# (``key.forgotten`` by the person working there, ``api.rest.Dashboard._forget_kept``).
_RECORDED_THERE = frozenset({("DELETE", "/vault/***")})
_PARAMETER = re.compile(r"\{[^{}]*\}")  # a route's parameter in its template: "{id}", "{rest:path}"
# A template that is one path parameter: the dashboard's catch-all, which serves no route of Latet's.
_CATCH_ALL = re.compile(r"/\{[^{}/]*:path\}")
# A burst of changes heard (a SCIM provider's) asks a stream open in a key's space again once a second at most.
ASKED_AT_MOST_EVERY: Final = 1.0
_ROUTING = frozenset({"path_params", "endpoint", "route", "router"})  # the person's dashboard's, never the key's
# Fixed words: a refusal never quotes what the request said (a path, a name, a body's text).
_NO_KEY = "there is no such API key on this server"
_NOT_YOURS = "you do not control this API key"
_DISABLED = "this API key is disabled: an administrator enables it again first"
_GONE = "this API key is disabled or gone"
_UNBUILT = "this API key's space is not available here now: try again in a moment"
_UNREAD = "whether you may open this API key's space cannot be read now: try again"
_BY_COMPANY_SIGN_IN = "sign in with your company account to open the space of an API key you do not control"
_UNRECORDED = "the administration log does not take the change now: nothing was changed, try again"
_REMOVED = "the API key, the person or the group is no longer there"
_NOT_CONTROLLING = "this person or group does not control this API key"
_SEVERAL = "this API key has {count} tokens: rotate the one you mean with `latet server keys rotate --token`"


def _controllers(subjects: Iterable[str], names: Names) -> list[dict[str, str]]:
    """Who controls a key, each named (a person removed, or a group gone, since: left out), groups first."""
    found = [
        {"kind": names[subject][0], "id": subject, "name": names[subject][1]}
        for subject in subjects
        if subject in names
    ]
    return sorted(found, key=lambda controller: (controller["kind"], controller["name"].casefold(), controller["id"]))


class KeysApi:
    """The routes of API keys and their control (ee/routes.py serves them to the dashboard and to `latet server`)."""

    def __init__(self, server: Server, people: People, access: Access) -> None:
        self.server = server
        self.people = people
        self.access = access

    def routes(self) -> list[EeRoute]:
        return [
            EeRoute("GET", "/keys", self.list_keys),
            EeRoute("POST", "/keys", self.make, kind="serve", answers=201),
            EeRoute("POST", "/keys/{id}/rotate", self.rotate, kind="serve"),
            EeRoute("POST", "/keys/{id}/disable", self.disable, kind="serve"),
            EeRoute("POST", "/keys/{id}/enable", self.enable, kind="serve"),
            EeRoute("POST", "/keys/{id}/controllers", self.add_controller),
            EeRoute("DELETE", "/keys/{id}/controllers/{subject}", self.remove_controller),
        ]

    def _key(self, key_id: str) -> Identity:
        """The API key a path names: by its id alone, as PostgreSQL has it now (404 in fixed words otherwise)."""
        key = self.server.identities.current(key_id) if IDENTITY_ID.fullmatch(key_id) else None
        if key is None or key.kind != "key":
            raise Refused(404, _NO_KEY)
        return key

    def _address(self, secret: str) -> str:
        """The address a program uses, as `latet server keys add` prints it: …/u/<route key>/."""
        return f"{self.server.settings.url.rstrip('/')}/u/{route_key(secret)}/"

    def _view(self, key: Identity, controllers: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "id": key.id,
            "name": key.name,
            "disabled": key.disabled,
            "created": key.created,
            "last_seen": key.last_seen,
            "controllers": controllers,
        }

    def _controllers_in(self, conn: Connection[Any], key_id: str) -> list[dict[str, str]]:
        """Who controls the key, named, as ``conn``'s transaction finds them: what a change of its control answers, so
        that it is answered as made or not made."""
        subjects = self.people.controllers(key_id, conn=conn)
        people = conn.execute(
            "SELECT id, name FROM identities WHERE kind = 'person' AND id = ANY(%s)", (subjects,)
        ).fetchall()
        groups = conn.execute("SELECT id, name FROM groups WHERE id = ANY(%s)", (subjects,)).fetchall()
        names: Names = {i: ("person", name) for i, name in people} | {i: ("group", name) for i, name in groups}
        return _controllers(subjects, names)

    def _list(self, who: Who) -> dict[str, Any]:
        """Every key, with who controls it, to whoever may manage keys (`latet server` as ``ADMINISTRATION``, never as
        no one: L2); to anyone else the keys they control, themselves or through a group, without."""
        if self.access.allows(who.identity_id, "keys.manage"):
            directory = self.people.directory()
            names: Names = {person.id: ("person", person.name) for person in self.server.identities.all(kind="person")}
            names |= {group.id: ("group", group.name) for group in directory.groups}
            keys = self.server.identities.all(kind="key")
            views = [self._view(key, _controllers(directory.controllers.get(key.id, ()), names)) for key in keys]
            return {"keys": views, "manage": True}
        person = who.identity_id
        refs = self.people.controlled_by(person) if isinstance(person, str) else []
        keys = [key for ref in refs if (key := self.server.identities.get(ref.id)) is not None and key.kind == "key"]
        keys.sort(key=lambda key: (key.name.casefold(), key.id))
        return {"keys": [self._view(key, []) for key in keys], "manage": False}

    async def list_keys(self, request: Request, who: Who) -> Response:
        return JSONResponse(await who.run(self._list, who))

    async def make(self, request: Request, who: Who) -> Response:
        name = (await json_body(request, who)).get("name")
        if not isinstance(name, str) or len(name) > NAME_LENGTH or not clean(name, NAME_LENGTH):
            raise Refused(400, f"an API key needs a name, of {NAME_LENGTH} characters at most")
        wanted = name.strip()

        def make(within: Changing) -> tuple[Identity, str, str]:
            key, secret = self.server.identities.add_key(wanted, within=within.changes)
            within.note("key.added", key.id)
            return key, secret, self._address(secret)

        key, secret, address = await who.change(make)
        body = {"key": self._view(key, []), "secret": secret, "address": address}  # the secret shown this once
        return JSONResponse(body, status_code=201)

    async def rotate(self, request: Request, who: Who) -> Response:
        """A new secret for the key's token, as `latet server keys rotate` gives one (``Identities.rotate_token``): the
        token keeps its name, what it may do and its row's form, so that a rotation never makes a token that may do
        more. A key with several tokens (added with `latet server keys add-token`) has the one meant rotated there,
        where it is named (409 here); the entry names the token that stands and the one it replaced, as the server's
        own does. The address is the new secret's where its token serves agents (a key's first), else none."""
        key = await who.run(self._key, request.path_params["id"])

        def rotate(within: Changing) -> tuple[str, str | None]:
            try:
                info, secret, replaced = self.server.identities.rotate_token(key.id, within=within.changes)
            except NoSuchIdentity:  # removed meanwhile
                raise Refused(404, _NO_KEY) from None
            except SeveralTokens as several:  # nothing done: the dashboard names no token
                raise Refused(409, _SEVERAL.format(count=several.count)) from None
            except NoSuchToken as gone:  # nothing done; its words say what to do when its row does not hold
                raise Refused(404, str(gone)) from None
            within.note("key.rotated", key.id, {"token": info.id, "replaced": replaced})
            serves = info.agents and info.permissions >= GATEWAY
            return secret, self._address(secret) if serves else None

        secret, address = await who.change(rotate)
        return JSONResponse({"secret": secret, "address": address})

    async def disable(self, request: Request, who: Who) -> Response:
        return await self._state(request, who, enabled=False)

    async def enable(self, request: Request, who: Who) -> Response:
        return await self._state(request, who, enabled=True)

    async def _state(self, request: Request, who: Who, *, enabled: bool) -> Response:
        """The key disabled or enabled: once the change commits every instance hears it on `identities` (E0b), closes
        the key's space there and forgets what it kept of the key."""
        key = await who.run(self._key, request.path_params["id"])

        def change(within: Changing) -> None:
            identities = self.server.identities
            done = (identities.enable if enabled else identities.disable)(key.id, within=within.changes)
            if done:
                within.note("key.enabled" if enabled else "key.disabled", key.id)

        await who.change(change)
        return JSONResponse({"ok": True})

    async def add_controller(self, request: Request, who: Who) -> Response:
        data = await json_body(request, who)
        key = await who.run(self._key, request.path_params["id"])
        subject = await who.run(subject_of, self.server, self.people, data.get("subject"))

        def add(within: Changing) -> list[dict[str, str]]:
            try:
                self.people.add_controller(key.id, subject.id, within=within)
            except KeyError:  # removed meanwhile
                raise Refused(404, _REMOVED) from None
            except ValueError as exc:  # its words quote nothing given
                raise Refused(400, str(exc)) from None
            return self._controllers_in(within.conn, key.id)

        controllers = await who.change(add)
        return JSONResponse({"controllers": controllers, "subject": subject.view()})  # whom the name was taken for

    async def remove_controller(self, request: Request, who: Who) -> Response:
        key = await who.run(self._key, request.path_params["id"])
        subject = await who.run(subject_by_id, self.server, self.people, request.path_params["subject"])

        def remove(within: Changing) -> tuple[bool, list[dict[str, str]]]:
            gone = self.people.remove_controller(key.id, subject.id, within=within)
            return gone, (self._controllers_in(within.conn, key.id) if gone else [])

        gone, controllers = await who.change(remove)
        if not gone:
            raise Refused(404, _NOT_CONTROLLING)
        return JSONResponse({"controllers": controllers, "subject": subject.view()})


_Watch = tuple[asyncio.AbstractEventLoop, asyncio.Event]


def _said(paced: Paced, what: str, exc: BaseException) -> None:
    """A store's failure, said by its type alone (its words can name the database's or Valkey's address), once a
    minute."""
    if count := paced.due():
        log.warning(
            "%s (%s)%s", what, type(exc).__name__, f" ({count} times since the line before)" if count > 1 else ""
        )


class KeySpace:
    """``/_latet/api/v1/ee/keys/{key}/space/{rest}``: the key's own ``/_latet/api/v1/{rest}``, served by the key's app
    with the key as the identity, for a person who controls the key or who may manage keys (from a session the company's
    sign-in opened, while the sign-in settings ask for one), while the license serves roles (a trial that ended closes
    it; a paid license paused does not). A request that may change something there
    (any method but GET and HEAD) is recorded in the administration log, by that person, before it is handed on: the
    key's app writes the change itself. A stream open there is watched (``heard``, WATCH_SECONDS)."""

    def __init__(self, server: Server, people: People, access: Access, licensing: Licensing) -> None:
        self.server = server
        self.people = people
        self.access = access
        self.licensing = licensing
        self._watches: set[_Watch] = set()  # the streams open in a key's space on this instance
        self._lock = threading.Lock()  # ``heard`` is called in the listener's thread
        self._unread = Paced()
        self._unbuilt = Paced()
        self._unrecorded = Paced()
        self._failed = Paced()

    def opened(self, person: str, key_id: str, *, via: object) -> Identity:
        """The key whose space ``person`` opens, from a dashboard session opened as ``via`` (``scope["latet.via"]``):
        refused (``Refused``, ``space: "closed"``) while the license does not serve roles, to a person who neither
        controls the key nor may manage keys (whether or not there is such a key: a key's id is told to its controllers
        and administrators alone), for no key of that id, and for a key disabled. A person who opens it through
        ``keys.manage`` alone reaches a key that is not theirs to work with, every key's history and vault included: a
        company-wide act, refused (403 ``company-sign-in``) to a session the company's sign-in did not open while the
        sign-in settings ask for one (``company_sign_in``, the settings as PostgreSQL has them now, as the policy's text
        is read); a controller's way in is their own work (O1). In the dashboard's threads: it reads People and
        PostgreSQL, and raises as they do."""
        try:
            require_serving(self.licensing, "roles")
        except NotLicensed as exc:
            raise Refused(403, str(exc), feature="roles", space="closed") from None
        except TrialEnded as exc:
            raise Refused(403, str(exc), code="trial-ended", feature="roles", space="closed") from None
        controls = any(ref.id == key_id for ref in self.people.controlled_by(person))
        if not controls:
            if not self.access.allows(person, "keys.manage"):
                raise Refused(403, _NOT_YOURS, space="closed")
            if company_sign_in(self._settings_now, via) is not None:
                raise Refused(403, _BY_COMPANY_SIGN_IN, code="company-sign-in", space="closed")
        key = self.server.identities.current(key_id) if IDENTITY_ID.fullmatch(key_id) else None
        if key is None or key.kind != "key":
            raise Refused(404, _NO_KEY, space="closed")
        if key.disabled:
            raise Refused(409, _DISABLED, space="closed")
        return key

    def _settings_now(self) -> EnterpriseSettings:
        """The Enterprise settings as PostgreSQL has them now, never this instance's copy, which may be seconds behind a
        change of ``company_changes`` made on another instance."""
        with self.server.db.connection() as conn:
            return settings_in(conn)

    def still(self, person: str, key_id: str, *, via: object) -> Identity:
        """What a stream open in the key's space asks again: ``opened``, and the person enabled as PostgreSQL has them
        now (a person disabled keeps their roles and their groups, which ``opened`` weighs)."""
        found = self.server.identities.current(person)
        if found is None or found.disabled or found.kind != "person":
            raise Refused(401, "unauthorized", space="closed")
        return self.opened(person, key_id, via=via)

    def heard(self) -> None:
        """A change of people or of an identity (ee:people, identities), on any instance: every stream open in a key's
        space on this instance asks again at once whether it may stay open (O2). In the listener's thread."""
        with self._lock:
            watches = list(self._watches)
        for loop, woken in watches:
            with contextlib.suppress(RuntimeError):  # its event loop closed meanwhile: the stream is over
                loop.call_soon_threadsafe(woken.set)

    def _watch(self, watch: _Watch, on: bool) -> None:
        with self._lock:
            if on:
                self._watches.add(watch)
            else:
                self._watches.discard(watch)

    def served(self, dashboard: Dashboard) -> ASGIApp:
        """The space's ASGI app in ``dashboard`` (the person's own), behind its guard, its work in its threads
        (``Dashboard._in_pool``): an object, not a function, so that Starlette's Route hands it the request as an ASGI
        app."""
        return _Served(self, dashboard)


class _LetThrough(Response):
    """What the dashboard's guard hands back when it lets a request on to the key's space: never sent."""


async def _let_through(request: Request) -> Response:
    return _LetThrough()


async def _refuse(refused: Refused, scope: Scope, receive: Receive, send: Send) -> None:
    response = refused.answer()
    response.headers["cache-control"] = "no-store"
    if refused.status == 503:
        response.headers["retry-after"] = "1"
    await response(scope, receive, send)


def _handed(scope: Scope, key_id: str, rest: str, key: Identity, person: str) -> Scope:
    """The request as the key's app reads it: its own path under /_latet/api/v1 (its raw path as it was sent), the key
    its identity and the person working there ``ACTING``, nothing of the person's dashboard's routing."""
    prefix = f"{EE}/keys/{key_id}/space/".encode()
    raw: bytes = scope.get("raw_path") or scope["path"].encode()
    inner = {name: value for name, value in scope.items() if name not in _ROUTING}
    inner["path"] = f"{API}/{rest}"
    inner["raw_path"] = f"{API}/".encode() + raw[len(prefix) :] if raw.startswith(prefix) else inner["path"].encode()
    inner[IDENTITY] = key.id
    inner[ACTING] = person
    return inner


def _served_by(router: object, inner: Scope) -> str | None:
    """The template, below /_latet/api/v1, of the dashboard's route that serves ``inner`` in the key's app, each
    parameter written *** (F7: a project's id is a hash of its folder, a value's id names it): what the administration
    log says of a change's path. None when only the dashboard's catch-all serves it (a path no route of Latet's serves:
    answered 404 here, never handed on); ``/***`` when the routes cannot be told."""
    routes = getattr(router, "routes", None)
    if routes is None:
        return "/***"
    for route in routes:
        if isinstance(route, Route) and route.path.startswith(f"{API}/") and route.matches(inner)[0] is Match.FULL:
            below = route.path[len(API) :]
            return None if _CATCH_ALL.fullmatch(below) else _PARAMETER.sub("***", below)
    return None


class _Served:
    def __init__(self, space: KeySpace, dashboard: Dashboard) -> None:
        self.space = space
        self.run: Runner = dashboard._in_pool
        self.guard = dashboard.guarded(_let_through)  # its session, its origin, the header of a change

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        guarded = await self.guard(Request(scope, receive))
        if not isinstance(guarded, _LetThrough):  # the dashboard's own refusal
            await guarded(scope, receive, send)
            return
        person = scope.get(IDENTITY)
        if not isinstance(person, str) or not person:  # never a request by no one (L2)
            await _refuse(Refused(401, "unauthorized", space="closed"), scope, receive, send)
            return
        params = scope.get("path_params") or {}
        key_id, rest = str(params.get("key", "")), str(params.get("rest", ""))
        via = scope.get(VIA)  # how the person's session was opened, as the server's gate found it
        try:
            key = await self._opened(person, key_id, via)
            inner = _handed(scope, key_id, rest, key, person)
            template = _served_by(scope.get("router"), inner)
            if template is None:  # only the catch-all: nothing of the key's is reached, nothing recorded
                raise Refused(404, "not found")
            app = await self._built(key)  # before the entry: what building it raises is answered, nothing recorded (F8)
            changes = scope["method"] not in _READS and (scope["method"], template) not in _RECORDED_THERE
            if changes and not template.startswith("/ee/"):  # no key's space reaches /ee/
                await self._recorded(scope, person, key, template)
        except Refused as refused:
            await _refuse(refused, scope, receive, send)
            return
        if space_long(inner):
            await self._watched(app, inner, receive, send, person, key_id, via)
        else:
            await app(inner, receive, send)

    async def _opened(self, person: str, key_id: str, via: object) -> Identity:
        try:
            return await self.run(functools.partial(self.space.opened, person, key_id, via=via))
        except Refused:
            raise
        except Exception as exc:
            _said(self.space._unread, "whether a person may open an API key's space could not be read", exc)
            raise Refused(503, _UNREAD, space="closed") from None

    async def _built(self, key: Identity) -> ASGIApp:
        try:
            return await self.space.server.spaces.app(key)
        except SpaceUnavailable:  # NotListening too: no space is built or handed out on this instance now
            raise Refused(503, _UNBUILT, space="closed") from None
        except IdentityUnavailable:  # disabled or removed since it was opened, as PostgreSQL has it now
            raise Refused(409, _GONE, space="closed") from None
        except FloorUnavailable as exc:  # the company's policy cannot be read here: no space is built below it
            raise Refused(503, unavailable(exc), space="closed") from None
        except Exception as exc:  # its build failed
            _said(self.space._unbuilt, "an API key's space could not be built on this instance", exc)
            raise Refused(503, _UNBUILT, space="closed") from None

    async def _recorded(self, scope: Scope, person: str, key: Identity, path: str) -> None:
        """A request that may change something in the key's space, in the administration log by the person who makes
        it, its path the template of the route that serves it (F7), once the dashboard's guard let it through and the
        key's app is built (F8): an entry that cannot be written hands nothing on."""
        details = {"method": scope["method"], "path": path}
        try:
            await self.run(
                self.space.server.audit.record, person, "key.space.changed", key.id, details, client_of(scope)
            )
        except Exception as exc:
            _said(self.space._unrecorded, "a change in an API key's space could not be recorded: it was refused", exc)
            raise Refused(503, _UNRECORDED) from None

    async def _lost(self, person: str, key_id: str, via: object) -> Refused | None:
        """None while the person may still open the key's space from their session (opened as ``via``); else why not.
        Stores that do not answer end it too: the space stays open to no one it cannot be told is still let in."""
        try:
            await self.run(functools.partial(self.space.still, person, key_id, via=via))
        except Refused as refused:
            return refused
        except Exception as exc:
            _said(self.space._unread, "whether a person may open an API key's space could not be read", exc)
            return Refused(503, _UNREAD, space="closed")
        return None

    async def _watched(
        self, app: ASGIApp, inner: Scope, receive: Receive, send: Send, person: str, key_id: str, via: object
    ) -> None:
        """A long response of the key's space (a dashboard's stream), ended as soon as the person may no longer open the
        space (O2): asked again every WATCH_SECONDS, and when this instance hears a change of people or of an identity
        (``KeySpace.heard``), once every ASKED_AT_MOST_EVERY at most (a burst of changes heard is asked of together).
        What it was sending is closed as the spaces close their own: a stream begun gets its last, empty part; one not
        begun, the refusal. What its task raised is taken, and said by its type alone."""
        began = ended = False

        async def sending(message: Message) -> None:
            nonlocal began, ended
            if message["type"] == "http.response.start":
                began = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                ended = True
            await send(message)

        async def serve() -> None:
            await app(inner, receive, sending)

        loop = asyncio.get_running_loop()
        woken = asyncio.Event()
        watch: _Watch = (loop, woken)
        serving = asyncio.create_task(serve(), name="latet-key-space-stream")
        self.space._watch(watch, True)
        lost: Refused | None = None
        failed: BaseException | None = None
        asked = loop.time()  # ``opened`` asked as the request came
        try:
            while lost is None:
                waiting = asyncio.create_task(woken.wait())
                try:
                    await asyncio.wait({serving, waiting}, timeout=WATCH_SECONDS, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waiting.cancel()
                if not serving.done() and (wait := asked + ASKED_AT_MOST_EVERY - loop.time()) > 0:
                    await asyncio.wait({serving}, timeout=wait)  # what else is heard meanwhile is asked of with it
                if serving.done():
                    break
                woken.clear()  # before it asks: a change heard while it asks is asked of again
                asked = loop.time()
                lost = await self._lost(person, key_id, via)
        finally:
            self.space._watch(watch, False)
            if not serving.done():
                serving.cancel()
                await asyncio.wait({serving})  # its end, whatever it raises: taken below, never raised here
            if not serving.cancelled():
                failed = serving.exception()  # taken, so that asyncio never says it with its words
                if failed is not None:
                    _said(self.space._failed, "a stream of an API key's space failed: it is ended", failed)
        if lost is None and failed is None:
            return
        with contextlib.suppress(Exception):  # its client gone already
            if began and not ended:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            elif not began:
                await _refuse(lost or Refused(500, "internal error"), inner, receive, send)
