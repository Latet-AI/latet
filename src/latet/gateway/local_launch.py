"""Local gateway launch carrier adapter: a computer's ingress and its launch authority (``LocalLaunchAuthority``),
injected as the app is built; the request path asks it through ``launch_authority.RequestLaunch`` alone."""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal
from urllib.parse import unquote_to_bytes

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from latet.engine.pipeline import RequestScope, request_scope
from latet.gateway.coverage import EnvelopeRefused, preflight_scope
from latet.gateway.launch_authority import (
    CLAIMED,
    KEY_MISSING,
    REFUSED,
    REQUEST_LAUNCH,
    LaunchChoice,
    RequestLaunch,
    key_project,
)
from latet.gateway.launch_keys import Authority, Binding, LocalLaunchKeys, canonical_launch_key

# The ingress's check, between the outermost layer and the dispatch after LocalOnly; the request path gets a
# ``RequestLaunch`` under ``REQUEST_LAUNCH`` instead, as on a server.
_INGRESS = "latet.local_launch_ingress"
_HEADER = b"x-latet-launch-key"
_PREFIX = "/_k/"
_RAW_PREFIX = b"/_k/"

Status = Literal["absent", "claimed", "verified", "invalid", "mismatch", "ended", "pending", "unavailable"]

# How long the plugin's binding of a hand-configured Claude Code's session lives without a bridge call for it (R-85,
# R-88): its hooks call the bridge before each model request of a turn (``turn.step``'s ``verify``, P1-0 D-8), so a
# turn's requests find it fresh; past this, its requests are decided as claimed (identity-level exceptions, no
# loosening). Only the bridge refreshes it, never a gateway request. ``plugin_clock``: the clock it is measured by.
PLUGIN_IDLE_SECONDS = 30 * 60
plugin_clock = time.monotonic


@dataclass(frozen=True, slots=True, repr=False)
class LocalRequestLaunch:
    status: Status
    owner: LocalLaunchKeys | None = None
    authority: Authority | None = None
    binding: Binding | None = None

    def current(self) -> bool:
        return self.status in ("absent", "claimed") or (
            self.status == "verified"
            and self.owner is not None
            and self.authority is not None
            and self.owner.validate(self.authority)
        )


_ABSENT = LocalRequestLaunch("absent")


async def _validated(launch: LocalRequestLaunch) -> bool:
    return launch.current()


def plugin_bound(session: Any) -> str | None:
    """The project the plugin's bridge bound ``session`` to, while that binding lives (``PLUGIN_IDLE_SECONDS`` since the
    bridge's last call for it); None once it has expired, or never was."""
    if session is None or session.local_plugin_project is None:
        return None
    if plugin_clock() - session.local_plugin_seen >= PLUGIN_IDLE_SECONDS:
        return None
    return session.local_plugin_project


async def _registered(session: Any, project: str) -> bool:
    """The plugin's registration of a hand-configured Claude Code is still the one the request was decided by, and
    still alive."""
    return plugin_bound(session) == project and not session.launch_required


class LocalLaunchAuthority:
    """A computer's launch authority (``launch_authority.LaunchAuthority``): the carrier as its ingress checked it
    against the computer's launch keys, and the plugin's registration of a session's project."""

    __slots__ = ()

    def decide(
        self, presented: Any, session: Any, claimed: str | None, *, legacy_eligible: bool, projects: Any
    ) -> LaunchChoice:
        from latet.projects import Projects, _a_folder

        launch = presented if presented is not None else _ABSENT
        if not isinstance(projects, Projects):
            return REFUSED if launch.status == "verified" else CLAIMED
        if launch.status == "verified":
            binding = launch.binding
            if binding is None or not launch.current():
                return REFUSED
            project = key_project(
                projects, binding.folder.decode("utf-8"), binding.root.decode("utf-8"), binding.project, claimed
            )
            if project is None:
                return REFUSED
            return LaunchChoice("verified", project, partial(_validated, launch), "launched")
        marked = session is not None and session.launch_required
        registered = plugin_bound(session)
        if launch.status == "absent" and legacy_eligible and not marked and registered is not None:
            project = projects.get(registered)
            if (
                project is not None
                and (project.personal is not None or (project.repo_file is not None and project.repo is not None))
                and (claimed is None or (_a_folder(claimed) and projects.resolve(claimed).id == registered))
            ):
                return LaunchChoice("legacy", project, partial(_registered, session, registered), "trusted")
        # Missing and unknown carriers are claims; a key Latet no longer knows, or a session it launched, said so.
        return KEY_MISSING if launch.status == "claimed" or marked else CLAIMED


LOCAL_AUTHORITY = LocalLaunchAuthority()


def local_request(launch: LocalRequestLaunch) -> RequestLaunch:
    """A checked carrier as the request path takes it, on a computer."""
    return RequestLaunch(LOCAL_AUTHORITY, launch if launch.status != "absent" else None)


def take_local_launch(scope: Scope, owner: LocalLaunchKeys | None, *, computer: bool) -> LocalRequestLaunch:
    """Remove the reserved carrier before a Request or Envelope takes a reference."""
    supplied = None
    duplicate = False
    remaining = []
    for name, value in scope.get("headers", ()):
        if name.lower() == _HEADER:
            duplicate |= supplied is not None
            supplied = value
        else:
            remaining.append((name, value))
    scope["headers"] = remaining

    path, raw = scope.get("path", ""), scope.get("raw_path")
    decoded_reserved = path == "/_k" or path.startswith(_PREFIX)
    raw_reserved = raw is not None and (raw == b"/_k" or raw.startswith(_RAW_PREFIX))
    if decoded_reserved or raw_reserved:
        # A refusal never hands the original path to routing or formatting.
        scope["path"], scope["raw_path"] = "/", b"/"
        if duplicate or supplied is not None or not computer or raw is None:
            return LocalRequestLaunch("invalid")
        if not path.startswith(_PREFIX) or not raw.startswith(_RAW_PREFIX):
            return LocalRequestLaunch("invalid")
        decoded_key, slash, suffix = path[len(_PREFIX) :].partition("/")
        raw_key, raw_slash, raw_suffix = raw[len(_RAW_PREFIX) :].partition(b"/")
        if not slash or not raw_slash or not suffix or not raw_suffix or not decoded_key.isascii():
            return LocalRequestLaunch("invalid")
        if decoded_key.encode("ascii") != raw_key:
            return LocalRequestLaunch("invalid")
        try:
            if unquote_to_bytes(raw_suffix).decode("utf-8") != suffix:
                return LocalRequestLaunch("invalid")
        except UnicodeError:
            return LocalRequestLaunch("invalid")
        if ("/" + suffix).startswith("/_latet") or (b"/" + raw_suffix).startswith(b"/_latet"):
            return LocalRequestLaunch("invalid")
        scope["path"], scope["raw_path"] = "/" + suffix, b"/" + raw_suffix
        supplied = raw_key

    if duplicate:
        return LocalRequestLaunch("invalid")
    if supplied is None:
        return LocalRequestLaunch("absent")
    try:
        key = supplied.decode("ascii")
    except UnicodeDecodeError:
        return LocalRequestLaunch("invalid")
    if not canonical_launch_key(key):
        return LocalRequestLaunch("invalid")
    if not computer or owner is None:
        return LocalRequestLaunch("unavailable")
    check, binding = owner.verify_request(key, "computer")
    if check.code == "unknown":
        return LocalRequestLaunch("claimed")
    if check.code == "active" and check.authority is not None and binding is not None:
        return LocalRequestLaunch("verified", owner, check.authority, binding)
    if check.code == "ended":
        return LocalRequestLaunch("ended")
    if check.code == "recovering":
        # A recovered launch whose hold is not back yet (a restarted daemon): its agent runs, and its key is not refused
        # for that alone, but decided as claimed, which only tightens (R-107; the review's Minor 2).
        return LocalRequestLaunch("claimed")
    if check.code == "pending":
        return LocalRequestLaunch("pending")
    if check.code == "mismatch":
        return LocalRequestLaunch("mismatch")
    if check.code == "unavailable":
        return LocalRequestLaunch("unavailable")
    return LocalRequestLaunch("invalid")


class LocalLaunchIngress:
    """Sanitize the carrier before the local gateway constructs a Request."""

    def __init__(self, app: ASGIApp, owner: LocalLaunchKeys | None) -> None:
        self.app = app
        self.owner = owner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        try:
            preflight_scope(scope)
        except EnvelopeRefused as error:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                refusal = JSONResponse({"error": "request envelope refused"}, status_code=error.status)
                await refusal(scope, receive, send)
            return

        scope[_INGRESS] = take_local_launch(scope, self.owner, computer=True)
        await self.app(scope, receive, send)


class LocalLaunchDispatch:
    """Refuse denied carriers after LocalOnly and own the actual HTTP ASGI lifetime."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        launch = scope.pop(_INGRESS, None)
        if launch is None:
            await self.app(scope, receive, send)
            return

        if launch.status not in ("absent", "claimed", "verified"):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            status = 503 if launch.status == "unavailable" else 400
            reason = "request launch authority unavailable" if status == 503 else "request launch authority refused"
            refusal = JSONResponse({"error": reason}, status_code=status, headers={"x-should-retry": "false"})
            await refusal(scope, receive, send)
            return
        if scope["path"].startswith("/_latet"):
            await self.app(scope, receive, send)
            return
        scope[REQUEST_LAUNCH] = local_request(launch)
        token = request_scope.set(RequestScope())
        try:
            await self.app(scope, receive, send)
        finally:
            request_scope.reset(token)
