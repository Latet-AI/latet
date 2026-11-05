"""The local engine bridge for the Claude Code plugin (client mode).

Every route needs the bridge token (``x-latet-token``) and refuses browser requests; in an identity's space on a Latet
server, the server checks the identity's own token and says whose request it is. Masking and restoring are scoped to
the Claude Code session the plugin names: a surrogate is only restored in the session that was shown it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from functools import partial
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from latet import __version__
from latet.bridge.launch_controls import LocalLaunchControls
from latet.bridge.review import inline_review
from latet.engine import wellformed
from latet.engine.masker import MaskReport
from latet.engine.pipeline import Session
from latet.engine.rehydrate import restore_text
from latet.engine.response_guard import safe_text
from latet.engine.types import SlotKind
from latet.engine.vault import MaskingError
from latet.engine.verifier import VerifyResult
from latet.gateway import local_launch, outbound
from latet.gateway.approvals import HOLD, Batch, Decision
from latet.gateway.aws import AwsCredentialStore
from latet.gateway.body_coverage import PROTECTED_LAUNCH, declared_string_safe
from latet.gateway.launch_ingress import flat_launch_json, valid_launch_scope
from latet.gateway.launch_keys import HeldLaunch, LaunchControls, LaunchPrepare, LaunchRefused, LocalLaunchKeys
from latet.gateway.mediate import LiveItem, Masked, Mediator, ReviewItem
from latet.gateway.outcome import Observation, ObservedText, observed
from latet.gateway.recorder import Outcome, Recorder
from latet.gateway.request_audit import RequestAudit, emit_advisory
from latet.gateway.sigv4 import AwsCredentials
from latet.integrations.routes import check_claude_route
from latet.logline import Paced, described
from latet.protocols.base import ImageRef, bounded_source
from latet.vision.masking import WITHHELD, ImageWork

if TYPE_CHECKING:
    from latet.gateway.app import OnServer
    from latet.server.plugin_bindings import PluginBindings

log = logging.getLogger(__name__)

_KINDS = {k.value: k for k in SlotKind}
_MAX_BODY = 32 * 1024 * 1024
SESSION_BEAT = 15.0  # seconds between the newlines a held session's connection gets while its credentials last
# How a call's answer is given to the queue that hands it to the plugin: tried this many times, this many seconds apart
# (a store that did not answer once), so that the plugin's poll gets its answer.
GIVE_TRIES = 3
GIVE_PAUSE = 0.5


# What a plugin call is answered when a store does not answer: the plugin tries again.
STORE_UNAVAILABLE = "Latet's store did not answer: try again in a moment"


class _ProtectedRequest(ValueError):
    pass


def _protected_request() -> JSONResponse:
    return JSONResponse({"error": PROTECTED_LAUNCH}, status_code=400)


def _screen_text(value: Any, *, json_mode: bool = False) -> None:
    if isinstance(value, str) and not (declared_string_safe(value) if json_mode else safe_text(value)):
        raise _ProtectedRequest(PROTECTED_LAUNCH)


def _screen_session(data: dict[str, Any]) -> None:
    for name in ("session", "cwd"):
        _screen_text(data.get(name))


def _screen_echo(value: Any) -> None:
    # Rehydrate passes through non-text item values in its response. Screen
    # that exact echo under the bridge's existing parsed-body ownership.
    stack: list[Iterator[Any]] = [iter((value,))]
    while stack:
        try:
            item = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if isinstance(item, dict):
            stack.append(chain.from_iterable(item.items()))
        elif isinstance(item, list):
            stack.append(iter(item))
        else:
            _screen_text(item)


def _kind(value: Any) -> SlotKind:
    return _KINDS.get(str(value), SlotKind.OTHER)


# The output of a command you run with ! in Claude Code (bash mode), as it puts it in the conversation.
_COMMAND_OUTPUT = re.compile(r"\s*<bash-(?:stdout|stderr)>")


def _object(raw: bytes, session_fields: bool = False) -> dict[str, Any] | None:
    """A plugin call's body as an object, or None when it is not one (in the bridge's thread)."""
    try:
        data = wellformed.loads(raw or b"{}")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if session_fields:
        _screen_session(data)
    return data


def a_store_not_answering(error: BaseException) -> bool:
    """Whether ``error`` is a store that did not answer, in time or at all: a server's Valkey (redis-py's connection
    and timeout errors, a full pool among them) or PostgreSQL (psycopg's operational errors, its pool's timeout), or a
    connection that timed out or broke; what the plugin's bridge and the applications API answer 503. Their drivers are
    a server's alone: looked for only when they are there."""
    kinds: list[type[BaseException]] = [TimeoutError, ConnectionError]
    with contextlib.suppress(ImportError):
        from redis.exceptions import ConnectionError as ValkeyGone
        from redis.exceptions import TimeoutError as ValkeyTimeout

        kinds += [ValkeyGone, ValkeyTimeout]
    with contextlib.suppress(ImportError):
        from psycopg import OperationalError

        kinds.append(OperationalError)
    return isinstance(error, tuple(kinds))


def _session_key(session: Any) -> str:
    return f"cc:{session}" if isinstance(session, str) and session else "cc:unknown"


def _launch_marked(data: dict[str, Any]) -> bool:
    """Whether a plugin's call names a launch by its id (32 hex digits)."""
    launch_id = data.get("launch_id")
    return type(launch_id) is str and re.fullmatch(r"[0-9a-f]{32}", launch_id) is not None


def _observation(session_id: Any, path: str, seen: Observation) -> Outcome:
    """What the plugin's bridge read of a request it stopped (``seen``), as the dashboard records it: an exchange of
    Claude Code's plugin, never sent, which holds an observation and no arena (``Outcome.observed``), so that nothing of
    it can be decided or sent."""
    session = session_id if isinstance(session_id, str) else ""
    headers = {"user-agent": "claude-cli/plugin", "x-claude-code-session-id": session}
    return Outcome(headers, "anthropic", path, "plugin", time.time(), observed=seen)


# Why a request is stopped when its values in clear come from the conversation as Claude Code had it.
HISTORY_REASON = (
    "stopped by the Claude Code plugin: this conversation already held values in clear before Latet "
    "checked it (it began before Latet was on, or was resumed from then), and every request sends that "
    "history again. Start a new conversation (/clear): from there everything is masked as it comes in"
)


class _Released(StreamingResponse):
    """Release a held session when its client leaves or it expires, retaining it when only its server space ends."""

    def __init__(self, content: AsyncIterator[bytes], release: Callable[[], object], media_type: str) -> None:
        super().__init__(content, media_type=media_type)
        self.release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            if not scope.get("latet.space_ended", False):
                self.release()


class _LaunchHold(Response):
    """A key-free physical hold. Its response owns the lease through actual ASGI detach."""

    def __init__(self, lease: HeldLaunch) -> None:
        super().__init__(media_type="application/x-ndjson")
        self.raw_headers = [(b"content-type", b"application/x-ndjson"), (b"cache-control", b"no-store")]
        self.lease = lease
        self.drain_task: asyncio.Task[None] | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        tasks: list[asyncio.Task[Any]] = []
        try:
            await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
            self.lease.acknowledge()
            acknowledgment = bytes(JSONResponse({"held": True, "generation": self.lease.generation}).body) + b"\n"
            await send({"type": "http.response.body", "body": acknowledgment, "more_body": True})

            async def disconnected() -> None:
                while (await receive())["type"] != "http.disconnect":
                    pass
                self.lease.stop()

            tasks = [asyncio.create_task(disconnected()), asyncio.create_task(_launch_hold_wait(self.lease))]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except LaunchRefused:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            self.lease.stop()  # authority ends now; physical accounting waits for the actual drain below
            for task in tasks:
                task.cancel()
            if tasks:
                self.drain_task = asyncio.create_task(
                    _launch_hold_drained(self.lease, tasks), name="latet-launch-drain"
                )
                await asyncio.shield(self.drain_task)
            else:
                await self.lease.drain()
                self.lease.close()


async def _launch_hold_wait(lease: HeldLaunch) -> None:
    """Finish the private lease waiter without retaining a cancellation traceback."""
    try:
        await lease.wait()
    except asyncio.CancelledError:
        return


async def _launch_hold_drained(lease: HeldLaunch, tasks: list[asyncio.Task[Any]]) -> None:
    """One prepaid physical owner until even a cancellation-delaying receive has detached."""
    settled = asyncio.gather(*tasks, return_exceptions=True)
    while not settled.done():
        try:
            await asyncio.shield(settled)
        except asyncio.CancelledError:
            continue  # loop/caller cancellation cannot refund a still-live physical owner
    tasks.clear()
    del settled
    await lease.drain()
    lease.close()


@dataclass(slots=True)
class _Waiting:
    """A plugin call whose new texts wait for you in the Live Reviewer, in the process where it waits (its images are
    there)."""

    batch: Batch
    session: Session
    items: list[LiveItem]
    out: list[dict[str, Any]]
    degraded: list[str] = field(default_factory=list)


def _rendered(answer: dict[str, Any]) -> JSONResponse:
    """In the bridge's thread: an answer of texts as the plugin gets it, rendered there (tens of MiB of texts take a
    tenth of a second, which the event loop that every request shares would wait for)."""
    return JSONResponse(answer)


def _items_rendered(out: list[dict[str, Any]], degraded: list[str]) -> JSONResponse:
    """In the bridge's thread: the items of ``mask``'s answer, their images as they may be sent, rendered there."""
    return _rendered({"items": _answered(out), "degraded": degraded})


def _answered(out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The items as the plugin gets them: an image item carries the image as it may be sent, or none
    when it was withheld (with why), and not its text."""
    answered = []
    for entry in out:
        work = entry.get("_work")
        if not isinstance(work, ImageWork):
            answered.append(entry)
            continue
        if entry.get("blocked") and work.state != WITHHELD:
            work.withhold(str(entry["blocked"]))
        clean = {k: v for k, v in entry.items() if k not in ("_work", "text")}
        if work.state == WITHHELD:
            clean |= {"image": None, "withheld": work.reason or "it could not be masked"}
        else:
            clean["image"] = {"data": work.ref.data(), "media_type": work.ref.media_type}
        answered.append(clean)
    return answered


def _no_model_report(session: str, items: list[str]) -> None:
    """This authenticated input source grants no model-authorship reporting authority."""
    return None


class _PluginBindingUnavailable(RuntimeError):
    pass


class Bridge:
    def __init__(
        self,
        mediator: Mediator,
        token: str,
        recorder: Recorder | None = None,
        aws: AwsCredentialStore | None = None,
        on_server: OnServer | None = None,
        *,
        request_audit: RequestAudit | None = None,
        launch_keys: LocalLaunchKeys | None = None,
        launch_prepare: LaunchPrepare | None = None,
        launch_controls: LaunchControls | None = None,
        plugin_bindings: PluginBindings | None = None,
    ) -> None:
        self.request_audit = request_audit
        self.aws = aws  # the sessions' AWS credentials live there, in memory
        self.mediator = mediator
        self.engine = mediator.engine
        self.token = token
        self.recorder = recorder if recorder is not None and recorder.enabled else None
        # The calls whose answer is worked out here once you decide: any process hands it to the plugin.
        self._answering: set[asyncio.Task[None]] = set()
        self.on_server = on_server  # an identity's space on a Latet server: whose requests the server hands on
        # A server never borrows volatile computer authority, even if a caller supplied one by mistake.
        self.launch_keys = launch_keys if on_server is None else None
        self._launch_prepare = launch_prepare if launch_prepare is not None else self.launch_keys
        # Construction chooses the launch controls (R-114): a server's, injected with its space, else the computer's
        # over its own launch keys. Every launch route asks this one.
        self._launch_controls: LaunchControls = (
            launch_controls if launch_controls is not None else LocalLaunchControls(self.launch_keys, mediator)
        )
        self._plugin_bindings = plugin_bindings
        # Construction chooses how a plugin's session is registered (R-114): a server's plugin bindings, a computer's
        # local project. No request's handler asks which machine it runs on.
        self._session_of: Callable[..., Session] = self._computer_session if on_server is None else self._server_session
        # Construction selects the authenticated input authority; request content has one path.
        self._model_reporter: Callable[[str, list[str]], None] = (
            partial(mediator.verifier.wrote, reported=True) if on_server is None else _no_model_report
        )
        self._unanswered = Paced()  # the calls answered 503 because a store did not answer, said once a minute

    def _authorized(self, request: Request) -> bool:
        if "origin" in request.headers or request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            return False
        if self.on_server is not None:  # the server checked the identity's token, and says whose request it is
            return request.scope.get("latet.identity") == self.on_server.identity
        given = request.headers.get("x-latet-token", "")
        return bool(self.token) and hmac.compare_digest(given, self.token)

    async def _json(self, request: Request, *, session_fields: bool = False) -> dict[str, Any] | None:
        raw = await request.body()
        if len(raw) > _MAX_BODY:
            return None
        # Tens of MiB hold the GIL in the parser a while.
        return await self.mediator.run_bridge(_object, raw, session_fields)

    def _or_503(self, handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        """``handler``, whose call answers 503 when a store does not answer (a server's Valkey or PostgreSQL), as the
        dashboard's do: its client tries again. Said in the log once a minute with how many, by the error's type: a
        driver's words name the address it could not reach. Any other failure is raised, as it was."""

        async def endpoint(request: Request) -> Response:
            try:
                return await handler(request)
            except _PluginBindingUnavailable:
                return JSONResponse({"error": STORE_UNAVAILABLE}, status_code=503)
            except Exception as exc:
                if not a_store_not_answering(exc):
                    raise
                if count := self._unanswered.due():
                    many = f" ({count} since the line before)" if count > 1 else ""
                    log.warning("a plugin call was answered 503: a store did not answer (%s)%s", described(exc), many)
                return JSONResponse({"error": STORE_UNAVAILABLE}, status_code=503)

        return endpoint

    def routes(self) -> list[Route]:
        base, or_503 = "/_latet/engine/v1", self._or_503
        return [
            Route(f"{base}/ping", self.ping, methods=["GET"]),
            Route(f"{base}/launches", self.open_launch, methods=["POST"]),
            Route(f"{base}/launches/prepare", self.prepare_launch, methods=["GET"]),
            Route(f"{base}/launches/recover", self.recover_launch, methods=["POST"]),
            Route(f"{base}/launches/{{id}}/hold", self.hold_launch, methods=["GET"]),
            Route(f"{base}/launches/{{id}}/end", self.end_launch, methods=["POST"]),
            Route(f"{base}/launches/{{id}}/exit", self.exit_launch, methods=["POST"]),
            Route(f"{base}/mask", or_503(self.mask), methods=["POST"]),
            Route(f"{base}/rehydrate", or_503(self.rehydrate), methods=["POST"]),
            Route(f"{base}/scope", or_503(self.scope), methods=["POST"]),
            Route(f"{base}/verify", or_503(self.verify), methods=["POST"]),
            Route(f"{base}/instructions", or_503(self.instructions), methods=["POST"]),
            Route(f"{base}/approvals/wait", or_503(self.wait), methods=["POST"]),
            Route(f"{base}/outbound", or_503(self.outbound), methods=["POST"]),
            Route(f"{base}/route-check", or_503(self.route_check), methods=["POST"]),
            Route(f"{base}/aws-sessions", or_503(self.add_aws_session), methods=["POST"]),
            Route(f"{base}/aws-sessions/{{id}}", or_503(self.drop_aws_session), methods=["DELETE"]),
            Route(f"{base}/aws-sessions/{{id}}/hold", or_503(self.hold_aws_session), methods=["GET"]),
        ]

    async def _guard(
        self, request: Request, *, session_fields: bool = False
    ) -> tuple[dict[str, Any] | None, Response | None]:
        if not self._authorized(request):
            return None, JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            data = await self._json(request, session_fields=session_fields)
        except _ProtectedRequest:
            return None, _protected_request()
        if data is None:
            return None, JSONResponse({"error": "invalid body"}, status_code=400)
        return data, None

    async def route_check(self, request: Request) -> Response:
        """The plugin, at the start of a session: ``{bridge, session, env, sources, entrypoint, surface}``
        → whether its requests go through Latet (``{ok, reason, toast}``). One that does not is
        recorded, for the dashboard."""
        data, error = await self._guard(request, session_fields=True)
        if error is not None:
            return error
        assert data is not None
        verdict = (
            check_claude_route(data)
            if self.on_server is None
            else check_claude_route(data, request.scope.get("latet.space"), server_origin=self.on_server.url)
        )
        try:
            _screen_text(verdict.reason)
            _screen_text(verdict.toast)
        except _ProtectedRequest:
            return _protected_request()
        if verdict.ok:
            await self.mediator.run_bridge(self._session, data)
        if not verdict.ok:
            emit_advisory(self.request_audit, 4, reason_present=verdict.reason is not None)
        if not verdict.ok and self.recorder is not None:
            # In the bridge's thread, as a call's texts are: the folder's project worked out reads its latet.yaml, and a
            # repository seen is remembered (its list read and written), never on the event loop.
            session = await self.mediator.run_bridge(self._session, data, register=False)
            seen = observed("route-check", session, verdict.reason, bypass=True)
            self.recorder.finish(_observation(data.get("session"), "/plugin/route-check", seen))
        return JSONResponse({"ok": verdict.ok, "reason": verdict.reason, "toast": verdict.toast})

    async def add_aws_session(self, request: Request) -> Response:
        """``latet claude``: AWS credentials found only in its shell (aws-vault...), kept in memory for
        its session's Bedrock route. ``{access_key, secret_key, token?, expires?}`` → ``{id}``."""
        data, error = await self._guard(request)
        if error is not None:
            return error
        assert data is not None
        if self.aws is None:
            return JSONResponse({"error": "this Latet signs nothing for AWS"}, status_code=404)
        try:
            expires = float(data["expires"]) if data.get("expires") is not None else None
            credentials = AwsCredentials(str(data["access_key"]), str(data["secret_key"]), data.get("token"), expires)
        except (KeyError, TypeError, ValueError):
            return JSONResponse({"error": "access_key and secret_key"}, status_code=400)
        return JSONResponse({"id": self.aws.add_session(credentials)})

    async def drop_aws_session(self, request: Request) -> Response:
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        dropped = self.aws is not None and self.aws.drop_session(request.path_params["id"])
        return JSONResponse({"dropped": dropped})

    async def hold_aws_session(self, request: Request) -> Response:
        """``latet claude`` holds its session for as long as this request stays open: a newline now, and
        every ``SESSION_BEAT`` seconds while the credentials last. When the client goes away (the wrapper
        ended, or was killed: the operating system closes its connection) the session is dropped."""
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        aws, session = self.aws, request.path_params["id"]
        if aws is None or not aws.has_session(session):
            return JSONResponse({"error": "no such session"}, status_code=404)

        async def beats() -> AsyncIterator[bytes]:
            while aws.has_session(session):
                yield b"\n"
                await asyncio.sleep(SESSION_BEAT)

        return _Released(beats(), lambda: aws.drop_session(session), "text/plain")

    async def ping(self, request: Request) -> Response:
        if not self._authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"ok": True, "version": __version__})

    def _authorize_launch(self, request: Request, *, hold: bool = False) -> None:
        if not valid_launch_scope(request.scope, hold=hold):
            raise LaunchRefused("launch-invalid")
        if not self._authorized(request):
            raise LaunchRefused("unauthorized")

    @staticmethod
    def _launch_failure(error: LaunchRefused) -> Response:
        status = 401 if error.code == "unauthorized" else 503 if error.code == "launch-unavailable" else 400
        if error.code in {"launch-stale", "launch-ended", "launch-mismatch", "launch-busy"}:
            status = 409
        return JSONResponse({"error": error.code}, status_code=status, headers={"cache-control": "no-store"})

    def _launch_reader(self, request: Request) -> Callable[[frozenset[str]], Awaitable[dict[str, Any]]]:
        """How the launch controls read a control's body when they are ready for it (a computer reserves first)."""

        async def read(fields: frozenset[str]) -> dict[str, Any]:
            return await self._launch_json(request, fields)

        return read

    async def _launch_json(self, request: Request, fields: frozenset[str]) -> dict[str, Any]:
        # Short private control only: no Request.body cache and no raw key in a parser/executor Future.
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 8192:
                raise LaunchRefused("launch-invalid")
            raw.extend(chunk)
        if not flat_launch_json(raw):
            raise LaunchRefused("launch-invalid")
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            raise LaunchRefused("launch-invalid") from None
        del raw
        try:
            read = wellformed.read(text)
        except (ValueError, RecursionError):
            raise LaunchRefused("launch-invalid") from None
        if read.mended or read.repeated or type(read.doc) is not dict or set(read.doc) != fields:
            raise LaunchRefused("launch-invalid")
        return read.doc

    async def open_launch(self, request: Request) -> Response:
        try:
            self._authorize_launch(request)
            opened = await self._launch_controls.open(request.scope, self._launch_reader(request))
            return JSONResponse(opened, headers={"cache-control": "no-store"})
        except LaunchRefused as error:
            if error.code == "launch-capacity":
                return JSONResponse({"key": None, "launch_id": None, "reason": error.code})
            return self._launch_failure(error)

    async def prepare_launch(self, request: Request) -> Response:
        try:
            self._authorize_launch(request)
            if self._launch_prepare is None:
                raise LaunchRefused("launch-unavailable")
            return JSONResponse(self._launch_prepare.prepare(), headers={"cache-control": "no-store"})
        except LaunchRefused as error:
            return self._launch_failure(error)

    async def recover_launch(self, request: Request) -> Response:
        try:
            self._authorize_launch(request)
            recovered = await self._launch_controls.recover(request.scope, self._launch_reader(request))
            return JSONResponse(recovered, headers={"cache-control": "no-store"})
        except LaunchRefused as error:
            return self._launch_failure(error)

    async def hold_launch(self, request: Request) -> Response:
        try:
            self._authorize_launch(request, hold=True)
            values = request.query_params.getlist("generation")
            if (
                len(values) != 1
                or set(request.query_params) != {"generation"}
                or not re.fullmatch(r"[0-9]{1,20}", values[0])
            ):
                raise LaunchRefused("launch-invalid")
            generation = int(values[0])
            lease = self._launch_controls.hold(request.scope, request.path_params["id"], generation)
            try:
                await lease.ready(generation)
            except BaseException:
                lease.stop()
                await _launch_hold_drained(lease, [])
                raise
            return _LaunchHold(lease)
        except LaunchRefused as error:
            return self._launch_failure(error)

    async def end_launch(self, request: Request) -> Response:
        try:
            self._authorize_launch(request)
            await self._launch_json(request, frozenset())
            ended = await self._launch_controls.end(request.scope, request.path_params["id"])
            return JSONResponse({"ended": ended}, headers={"cache-control": "no-store"})
        except LaunchRefused as error:
            return self._launch_failure(error)

    async def exit_launch(self, request: Request) -> Response:
        try:
            self._authorize_launch(request)
            data = await self._launch_json(request, frozenset(("epoch",)))
            if type(data["epoch"]) is not str or self._launch_prepare is None:
                raise LaunchRefused("launch-invalid")
            retired = await self._launch_controls.end(
                request.scope, request.path_params["id"], exit_epoch=data["epoch"]
            )
            return JSONResponse(
                {"retired": retired, **self._launch_prepare.prepare()}, headers={"cache-control": "no-store"}
            )
        except LaunchRefused as error:
            return self._launch_failure(error)

    async def mask(self, request: Request) -> Response:
        """``{session, items: [{text, kind?, path?, tool?, from_model?, json?}]}`` → ``{items: [{text, count}]}``.

        ``tool`` names the tool a tool result comes from (strict data mode leaves the files it read
        alone). An item the dump guard withheld also says ``withheld: true``; one the local reviewer
        stopped says why in ``blocked``, and its text is a notice instead.

        With the Live Reviewer on, new texts wait for you: the answer is ``{pending, viewers}`` at once,
        and the plugin asks ``approvals/wait`` for the items until you decide."""
        data, error = await self._guard(request, session_fields=True)
        if error is not None:
            return error
        assert data is not None
        try:
            session, out, degraded, reviews, live = await self.mediator.run_bridge(self._mask, data)
            if reviews:
                degraded = await self._review(session, reviews, out, degraded)
            if live:
                waiting = await self._live(session, live, reviews, out, degraded)
                if waiting is not None:
                    return JSONResponse({"pending": waiting.batch.id, "viewers": self.mediator.approvals.viewers})
        except _ProtectedRequest:
            return _protected_request()
        except MaskingError as exc:  # a vault at its limit says so: a MaskingError's words name no value
            log.error("bridge masking failed: %s", exc)
            return JSONResponse({"error": f"masking failed: {exc}"}, status_code=500)
        return await self.mediator.run_bridge(_items_rendered, out, degraded)

    async def outbound(self, request: Request) -> Response:
        """``{session, tool, values: [str]}`` → ``{found: [type], reason}``: whether a tool that runs outside
        this computer (WebSearch, WebFetch, a claude.ai connector) would get stand-ins Latet put in place
        of real values. Then the plugin stops it, the model is told ``reason``, and the dashboard shows it."""
        data, error = await self._guard(request, session_fields=True)
        if error is not None:
            return error
        assert data is not None
        try:
            found, reason = await self.mediator.run_bridge(self._outbound, data)
        except _ProtectedRequest:
            return _protected_request()
        return JSONResponse({"found": found, "reason": reason})

    def _outbound(self, data: dict[str, Any]) -> tuple[list[str], str | None]:
        tool = str(data.get("tool") or "a tool")
        _screen_text(tool)
        values = [v for v in data.get("values") or [] if isinstance(v, str)]
        for value in values:
            _screen_text(value)
        session = self._session(data)
        types = outbound.stand_ins(self.mediator._masker(session), values)
        if not types:
            return [], None
        reason = outbound.reason(tool, types)
        emit_advisory(self.request_audit, 5, reason_present=reason is not None)
        if self.recorder is not None:
            texts = [ObservedText(value, SlotKind.OUTBOUND_TOOL, True) for value in values]
            seen = observed("outbound", session, reason, blocks=texts, outbound=[tool])
            self.recorder.finish(_observation(data.get("session"), "/plugin/tool-call", seen))
        return sorted(set(types)), reason

    async def wait(self, request: Request) -> Response:
        """``{id}`` → ``{pending, viewers}`` while you have not decided (held up to 20 s), then what
        ``mask`` answers: the items as you approved them, or each withheld when you stopped them. Any process that
        serves you answers it: the answer is worked out where the call waits, and handed over by the queue."""
        data, error = await self._guard(request)
        if error is not None:
            return error
        assert data is not None
        batch_id = str(data.get("id"))
        approvals = self.mediator.approvals
        try:
            answer = await approvals.answer(batch_id, HOLD)
        except KeyError:
            return JSONResponse({"error": "nothing waits with this id"}, status_code=404)
        except MaskingError as exc:  # as ``mask`` answers it
            log.error("bridge masking failed: %s", exc)
            return JSONResponse({"error": f"masking failed: {exc}"}, status_code=500)
        if answer is None:
            return JSONResponse({"pending": batch_id, "viewers": approvals.viewers})
        if "error" in answer:
            return JSONResponse({"error": answer["error"]}, status_code=500)
        return await self.mediator.run_bridge(_rendered, answer)

    async def _live(
        self,
        session: Session,
        items: list[LiveItem],
        reviews: list[ReviewItem],
        out: list[dict[str, Any]],
        degraded: list[str],
    ) -> _Waiting | None:
        """Open a batch for the new texts of this call; None when they may leave without asking you."""
        config = self.mediator.live_config(session)
        items = [i for i in items if not out[i.index].get("blocked")]
        if config is None or not items:
            return None
        for item in items:
            item.sent = out[item.index]["text"]
        batch = await self.mediator.run_bridge(self._batch, session, config, items, reviews)
        if batch is None:
            return None
        waiting = _Waiting(batch, session, items, out, degraded)
        await self.mediator.approvals.open(batch)
        self.mediator.hold_live(batch, session, items, keep=True)  # its previews made again here while it waits
        task = asyncio.get_running_loop().create_task(self._answer(waiting))
        self._answering.add(task)
        task.add_done_callback(lambda ended: self._answer_ended(ended, batch.id))
        return waiting

    async def _answer(self, waiting: _Waiting) -> None:
        """Once you decide on a call's texts: what the plugin gets, worked out here (its images are here), and given
        to the queue for whichever process the plugin asks; given again when the queue did not take it, a few times."""
        approvals = self.mediator.approvals
        try:
            decision = await approvals.decided(waiting.batch)
            self.mediator.release_live(waiting.batch.id)  # decided: its texts are masked as they leave
            out = await self.mediator.run_bridge(self._decided, waiting, decision)
            answer = {"items": await self.mediator.run_bridge(_answered, out), "degraded": waiting.degraded}
        except MaskingError as exc:  # logged by its own words, which name no value, as `mask` does: no traceback
            log.error("bridge masking failed: %s", exc)
            answer = {"error": f"masking failed: {exc}"}
        except Exception as exc:
            # Whatever fails, the decision not heard too, the plugin is told: its poll must never wait for an answer
            # that cannot come. Logged by its type and its batch, as `_answer_ended` does: its message and its traceback
            # could quote a text. A batch whose decision was not heard is over, unsent: the queue then takes its answer.
            log.error("bridge masking failed: %s (batch %s)", type(exc).__name__, waiting.batch.id)
            answer = {"error": "masking failed"}
            with contextlib.suppress(Exception):  # decided already, or its store does not answer: given as it is
                approvals.abandon(waiting.batch)
        for attempt in range(1, GIVE_TRIES + 1):
            try:
                approvals.give(waiting.batch.id, answer)
                return
            except Exception as exc:  # the last failure ends the task, which `_answer_ended` logs by its type
                if attempt == GIVE_TRIES:
                    raise
                log.warning("the answer to a plugin call was not taken (%s): given again", type(exc).__name__)
                await asyncio.sleep(GIVE_PAUSE)

    def _answer_ended(self, task: asyncio.Task[None], batch_id: str) -> None:
        """A call's task is over, and no longer held. One that ended with an error of its own (its queue could not be
        asked or told) is logged by the error's type and the batch alone: its message and its traceback could quote a
        text. A cancelled one (the daemon stops) is no error."""
        self._answering.discard(task)
        self.mediator.release_live(batch_id)
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("the answer to a plugin call could not be given: %s (batch %s)", type(error).__name__, batch_id)

    def _batch(self, session: Session, config: Any, items: list[LiveItem], reviews: list[ReviewItem]) -> Batch | None:
        self.mediator.with_answers(session, items, reviews)
        return self.mediator.live_batch(session, config, items, "claude-code", polled=True)

    def _decided(self, waiting: _Waiting, decision: Decision) -> list[dict[str, Any]]:
        """The plugin's items as you decided: masked again with your values, or withheld."""
        out = waiting.out
        if not decision.approved:
            for item in waiting.items:
                out[item.index] |= {"blocked": decision.reason, "text": f"[withheld by Latet: {decision.reason}]"}
            return out
        done = self.mediator.apply_live(waiting.session, waiting.items, decision, keep=True)
        self.mediator.decide_images(waiting.items, decision)
        for item, masked in zip(waiting.items, done, strict=True):
            out[item.index] |= {"text": masked.text, "count": len(masked.replacements)}
            if isinstance(work := out[item.index].get("_work"), ImageWork):
                work.masked("text", masked.text, masked.replacements)
        return out

    async def _review(
        self, session: Session, reviews: list[ReviewItem], out: list[dict[str, Any]], degraded: list[str]
    ) -> list[str]:
        """The local reviewer on the plugin's items (``review.inline_review``): inline ones before they are handed
        back, within the settings' budget, the rest in the background."""
        return await inline_review(self.mediator, session, reviews, out, degraded, budget_s=None, background=True)

    def _session(self, data: dict[str, Any], *, register: bool = True) -> Session:
        """The plugin's session, registered as construction chose (``_session_of``): with ``register`` False, an
        observation's (a failed route-check), never registered and never bound to a launch."""
        return self._session_of(data, register=register)

    def _computer_session(self, data: dict[str, Any], *, register: bool = True) -> Session:
        """A computer's: the session attached to the folder Claude Code runs in (its project worked out, its repository
        remembered), and, registered, its local plugin project."""
        from latet.projects import Projects, _a_folder

        supplied = data.get("session")
        cwd = data.get("cwd")
        session = self.engine.session(_session_key(supplied))
        if not register:
            self.engine.attach(session, cwd)
            return session
        if _launch_marked(data):
            session.launch_required = True
            session.local_plugin_project = None
        self.engine.attach(session, cwd)
        projects = self.engine.scopes
        if supplied and type(supplied) is str and _a_folder(cwd):
            attached = (
                projects.get(session.project_id)
                if isinstance(projects, Projects)
                and session.project_id is not None
                and session.cwd == session.reported == cwd
                else None
            )
            session.local_plugin_project = (
                attached.id
                if attached is not None
                and (attached.personal is not None or (attached.repo_file is not None and attached.repo is not None))
                and "launch_id" not in data
                and not session.launch_required
                else None
            )
            if session.local_plugin_project is not None:  # the bridge's call refreshes its binding, nothing else does
                session.local_plugin_seen = local_launch.plugin_clock()
        return session

    def _server_session(self, data: dict[str, Any], *, register: bool = True) -> Session:
        """A server's: a registration commits before the session is attached or acknowledged."""
        from latet.projects import Projects, _a_folder

        supplied = data.get("session")
        key = _session_key(supplied)
        if not register:
            return self.engine.session(key)
        cwd = data.get("cwd")
        marked = _launch_marked(data)
        if "launch_id" in data and not marked:
            raise _PluginBindingUnavailable()
        plugins = self._plugin_bindings
        projects = self.engine.scopes
        accepted = False
        if (
            plugins is not None
            and isinstance(projects, Projects)
            and _a_folder(cwd)
            and type(supplied) is str
            and supplied
            and plugins.session_digest(key) is not None
        ):
            project = projects.resolve(cwd)
            if project.personal is not None:
                configured = projects.resolve(project.personal.path)
                accepted = plugins.refresh(key, configured.id, configured.root, required=marked)
        # A launched session whose binding could not be written (its identity's quota full, the store not taking it)
        # starts claimed: unbound, it only tightens, and is never refused for it (root's ruling I-3).
        session = self.engine.session(key)
        if accepted:
            self.engine.attach(session, cwd)
        return session

    def _mask(
        self, data: dict[str, Any]
    ) -> tuple[Session, list[dict[str, Any]], list[str], list[ReviewItem], list[LiveItem]]:
        """The plugin's items masked (``_masked``), the values and the fakes their texts hold asked for first, at once,
        where the vault keeps most of them in its store (an API key's: ``Vault.prefetch``, ``prefetch_fakes``), and
        held while they are masked: a resumed conversation's fakes are adopted whatever their age."""
        # The authenticated bridge already owns this bounded body. Each selected
        # string is read in place, without an HTTP occurrence ledger or a second tree.
        items = data.get("items") or []
        texts = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                # Masked below, a live launch key in it as a known secret (R-103); what is answered is screened again.
                texts.append(text)
            else:
                _screen_echo(text)
            for name in ("path", "tool", "kind"):
                _screen_text(item.get(name))
        vault = self.engine.vault
        with vault.holding():
            # What is asked for first, at once (a text with a live launch key in it never is: ``Vault.prefetch``).
            vault.prefetch(texts)
            vault.prefetch_fakes(texts)
            return self._masked(data)

    def _masked(
        self, data: dict[str, Any]
    ) -> tuple[Session, list[dict[str, Any]], list[str], list[ReviewItem], list[LiveItem]]:
        session = self._session(data)
        report = MaskReport()
        out: list[dict[str, Any]] = []
        config = self.mediator.review_config(session)
        masker = self.mediator._masker(session) if config is not None else None
        reviews: list[ReviewItem] = []
        live_config = self.mediator.live_config(session)
        live: list[LiveItem] = []
        call = self.mediator.call()
        texts: list[tuple[int, int, Masked, ImageWork | None, SlotKind, str | None, str | None, bool, bool]] = []
        for index, item in enumerate(data.get("items") or []):
            text = item.get("text") if isinstance(item, dict) else None
            work: ImageWork | None = None
            if isinstance(item, dict) and isinstance(item.get("image"), dict):
                work, entry = self._image(session, item)
                if work is None or work.slot is None:
                    out.append(entry)
                    continue
                text = work.text
            if not isinstance(text, str):
                out.append({"text": text, "count": 0})
                continue
            kind = SlotKind.IMAGE_TEXT if work is not None else _kind(item.get("kind"))
            path = bounded_source(item.get("path"))  # where an output came from, no longer than a request's can be
            tool = item.get("tool") if isinstance(item.get("tool"), str) else None
            if kind in (SlotKind.ATTACHMENT, SlotKind.OTHER) and tool is None and _COMMAND_OUTPUT.match(text):
                # A command you ran with ! in Claude Code: its output, which Claude Code puts among your texts, is the
                # Bash tool's all the same (strict data mode masks its values). A prompt stays a prompt.
                kind, tool = SlotKind.TOOL_RESULT, "Bash"
            from_model = bool(item.get("from_model"))
            json_mode = bool(item.get("json"))
            if work is not None:
                _screen_text(text, json_mode=json_mode)
            done = self.mediator.mask_value(
                text,
                kind,
                session,
                report,
                from_model=from_model,
                path=path,
                tool=tool,
                json_mode=json_mode,
                keep_surrogates=True,
                call=call,
            )
            texts.append((index, len(out), done, work, kind, path, tool, from_model, json_mode))
            out.append({})
        self.mediator.settle(call)  # a text masked before a later one of the call taught the vault a value in it
        for index, at, done, work, kind, path, tool, from_model, json_mode in texts:
            _screen_text(done.text, json_mode=json_mode)
            out[at] = {"text": done.text, "count": len(done.replacements)} | (
                {"withheld": True} if done.withheld else {}
            )
            if work is not None:
                work.masked("text", done.text, done.replacements)
                out[at]["_work"] = work
            if config is not None and masker is not None:
                review = self.mediator.review_item(
                    config, masker, index, kind, done, tool=tool, path=path, json_mode=json_mode,
                    from_model=from_model, image=work, left=partial(self.mediator.verifier.sent, session.key),
                )  # fmt: skip
                if review is not None:
                    reviews.append(review)
            if live_config is not None:
                waits = self.mediator.live_item(
                    live_config, session, index, kind, done, tool=tool, path=path, json_mode=json_mode,
                    from_model=from_model,
                )  # fmt: skip
                if waits is not None:
                    waits.image = work
                    live.append(waits)
        return session, out, report.degraded, reviews, live

    def _image(self, session: Session, item: dict[str, Any]) -> tuple[ImageWork | None, dict[str, Any]]:
        """An image item ``{image: {data, media_type}, kind?, tool?}``: read, its text masked by the caller.
        Without text recognition here the image comes back as it was, ``unread``."""
        given = item["image"]
        data, media = given.get("data"), given.get("media_type")
        if not isinstance(data, str) or not isinstance(media, str):
            return None, {"image": None, "count": 0, "withheld": "not an image"}
        holder = {"data": data, "media_type": media}
        tool = item.get("tool") if isinstance(item.get("tool"), str) else None
        ref = ImageRef(holder, "data", "base64", media, _kind(item.get("kind")), 0, tool)
        work = self.mediator.read_image(ref, session, modify=True)
        if work is None:
            return None, {"image": {"data": data, "media_type": media}, "count": 0, "unread": True}
        return work, {"_work": work, "count": 0}

    async def rehydrate(self, request: Request) -> Response:
        """``{session, items: [str], json?: bool, model?: bool}`` → ``{items: [str], restored: int}``. ``model``: the
        items are a tool call's input, as the model wrote it, restored before the tool runs."""
        data, error = await self._guard(request, session_fields=True)
        if error is not None:
            return error
        assert data is not None
        try:
            return await self.mediator.run_bridge(self._rehydrated, data)
        except _ProtectedRequest:
            return _protected_request()

    def _rehydrated(self, data: dict[str, Any]) -> JSONResponse:
        """In the bridge's thread: the texts restored, and the answer rendered there."""
        out, total = self._rehydrate(data)
        return _rendered({"items": out, "restored": total})

    def _rehydrate(self, data: dict[str, Any]) -> tuple[list[Any], int]:
        items = data.get("items") or []
        json_mode = bool(data.get("json"))
        for text in items:
            if isinstance(text, str):
                _screen_text(text, json_mode=json_mode)
            else:
                _screen_echo(text)
        session = self._session(data)
        if data.get("model") is True and isinstance(items, list):
            # A report is never an observation. The authenticated input adapter decides whether
            # this source may report at all; identities on a server have no such capability.
            self._model_reporter(session.key, [text for text in items if isinstance(text, str)])
        restores = self.engine.restores(session, json_mode=json_mode)
        out, total = [], 0
        for text in items:
            if not isinstance(text, str):
                out.append(text)
                continue
            restored, ids = restore_text(text, restores)
            _screen_text(restored, json_mode=json_mode)
            total += len(ids)
            out.append(restored)
        return out, total

    async def scope(self, request: Request) -> Response:
        """``{session, items: [str]}``: register the surrogates found in these texts with the session
        (a resumed conversation whose history is already in surrogate space)."""
        data, error = await self._guard(request, session_fields=True)
        if error is not None:
            return error
        assert data is not None
        try:
            return JSONResponse({"added": await self.mediator.run_bridge(self._scope, data)})
        except _ProtectedRequest:
            return _protected_request()

    def _scope(self, data: dict[str, Any]) -> int:
        items = data.get("items") or []
        for text in items:
            _screen_text(text)
        session = self._session(data)
        vault = self.engine.vault
        before = len(session.entity_ids)
        found: list[str] = []
        for text in items:
            if isinstance(text, str):
                for span in self.engine.masker.surrogates_in(text):
                    entity = vault.by_surrogate(span.of(text))
                    if entity is not None:
                        found.append(entity.id)
        self.engine.show(session, found)
        return len(session.entity_ids) - before

    async def instructions(self, request: Request) -> Response:
        """``{session, cwd}`` → the values in the CLAUDE.md files Claude Code loads there that its auto
        mode reviewer would receive unmasked: file, line, type and length, never the values."""
        data, error = await self._guard(request, session_fields=True)
        if error is not None:
            return error
        assert data is not None
        found = await self.mediator.run_bridge(self._instructions, data)
        items = [{"file": str(f.path), "line": f.line, "type": f.etype, "length": f.length} for f in found]
        return JSONResponse({"findings": items})

    def _instructions(self, data: dict[str, Any]) -> list[Any]:
        from latet.instructions import findings, instruction_files

        if self.on_server is not None:
            return []  # the server has none of the computer's files: no CLAUDE.md of its own is read
        cwd = data.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            return []
        scope = self.engine.scope(self._session(data))
        if scope is not None and not scope.enabled:
            return []  # Latet is off in this project: nothing is stopped there
        masker = scope.masker if scope is not None else self.engine.masker
        return findings(instruction_files(Path(cwd)), masker)

    async def verify(self, request: Request) -> Response:
        """``{session, slots: [{text, kind?, from_model?}], history?}`` → leaks, never their values.

        ``history`` is how many of the slots come from the conversation as the plugin found it, before
        the turn it checks. When every value that blocks is there, the conversation began before
        Latet masked it (or was resumed from then): ``inherited`` says so."""
        data, error = await self._guard(request, session_fields=True)
        if error is not None:
            return error
        assert data is not None
        key = _session_key(data.get("session"))
        raw = [s for s in data.get("slots") or [] if isinstance(s, dict) and isinstance(s.get("text"), str)]
        slots = [(s["text"], _kind(s.get("kind")), bool(s.get("from_model"))) for s in raw]
        history = data.get("history")
        history = history if isinstance(history, int) and not isinstance(history, bool) and history > 0 else 0
        try:
            result, slots = await self.mediator.run_bridge(self._verify, data, slots, key)
        except _ProtectedRequest:
            return _protected_request()
        blocks = [x for x in result.leaks if x.blocks]
        inherited = bool(blocks) and all(0 <= x.slot < history for x in blocks)
        if blocks:
            emit_advisory(self.request_audit, 6, reason_present=True)
        if blocks and self.recorder is not None:
            self._record_block(data.get("session"), key, slots, result, inherited)
        leaks = [({"type": x.etype, "kind": x.kind.value, "preview": x.preview, "new": x.new}, x) for x in result.leaks]
        blocking = [d for d, x in leaks if x.blocks]
        flagged = [d for d, x in leaks if not x.blocks]
        return JSONResponse({"blocking": blocking, "flagged": flagged, "inherited": inherited})

    def _verify(
        self, data: dict[str, Any], slots: list[tuple[str, SlotKind, bool]], key: str
    ) -> tuple[VerifyResult, list[tuple[str, SlotKind, bool]]]:
        """The texts, and the text read in each image (an image Latet painted itself is not read again)."""
        for text, _, _ in slots:
            _screen_text(text)
        session = self._session(data)
        slots = list(slots)
        for given in data.get("images") or []:
            if not isinstance(given, dict) or not isinstance(given.get("data"), str):
                continue
            media = str(given.get("media_type") or "image/png")
            holder = {"data": given["data"], "media_type": media}
            ref = ImageRef(holder, "data", "base64", media, _kind(given.get("kind")), 0)
            work = self.mediator.read_image(ref, session, modify=False)
            if work is not None and work.slot is not None:
                _screen_text(work.text)
                slots.append((work.text, SlotKind.IMAGE_TEXT, False))
        verifier = self.mediator.verifier
        written = partial(verifier.written, session.key)  # what the model wrote, exactly so, leaves
        return verifier.verify(slots, key, scope=self.engine.scope(session), written=written), slots

    def _record_block(
        self, session_id: Any, key: str, slots: list[tuple[str, SlotKind, bool]], result: Any, inherited: bool
    ) -> None:
        """The plugin's final check stopped a request inside Claude Code: show it in the dashboard too
        (it never reached the proxy)."""
        assert self.recorder is not None
        reason = HISTORY_REASON if inherited else "stopped by the Claude Code plugin before it was sent"
        texts = [ObservedText(text, kind, from_model) for text, kind, from_model in slots]
        session = self.engine.session(key)
        seen = observed("final-check", session, reason, blocks=texts, leaks=result.leaks, history=inherited)
        self.recorder.finish(_observation(session_id, "/plugin/final-check", seen))
