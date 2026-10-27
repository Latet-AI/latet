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
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from latet import __version__
from latet.bridge.review import inline_review
from latet.engine import wellformed
from latet.engine.masker import MaskReport
from latet.engine.pipeline import Session
from latet.engine.rehydrate import restore_text
from latet.engine.types import SlotKind
from latet.engine.vault import MaskingError
from latet.engine.verifier import VerifyResult
from latet.gateway import outbound
from latet.gateway.approvals import HOLD, Batch, Decision
from latet.gateway.aws import AwsCredentialStore
from latet.gateway.mediate import VERIFY, LiveItem, Masked, Mediator, Prepared, ReviewItem
from latet.gateway.recorder import Outcome, Recorder
from latet.gateway.sigv4 import AwsCredentials
from latet.integrations.routes import check_claude_route
from latet.logline import Paced, described
from latet.protocols.base import ImageRef, SlotRef, bounded_source
from latet.vision.masking import WITHHELD, ImageWork

if TYPE_CHECKING:
    from latet.gateway.app import OnServer

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


def _kind(value: Any) -> SlotKind:
    return _KINDS.get(str(value), SlotKind.OTHER)


# The output of a command you run with ! in Claude Code (bash mode), as it puts it in the conversation.
_COMMAND_OUTPUT = re.compile(r"\s*<bash-(?:stdout|stderr)>")


def _object(raw: bytes) -> dict[str, Any] | None:
    """A plugin call's body as an object, or None when it is not one (in the bridge's thread)."""
    try:
        data = wellformed.loads(raw or b"{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


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


class Bridge:
    def __init__(
        self,
        mediator: Mediator,
        token: str,
        recorder: Recorder | None = None,
        aws: AwsCredentialStore | None = None,
        on_server: OnServer | None = None,
    ) -> None:
        self.aws = aws  # the sessions' AWS credentials live there, in memory
        self.mediator = mediator
        self.engine = mediator.engine
        self.token = token
        self.recorder = recorder if recorder is not None and recorder.enabled else None
        # The calls whose answer is worked out here once you decide: any process hands it to the plugin.
        self._answering: set[asyncio.Task[None]] = set()
        self.on_server = on_server  # an identity's space on a Latet server: whose requests the server hands on
        self._unanswered = Paced()  # the calls answered 503 because a store did not answer, said once a minute

    def _authorized(self, request: Request) -> bool:
        if "origin" in request.headers or request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            return False
        if self.on_server is not None:  # the server checked the identity's token, and says whose request it is
            return request.scope.get("latet.identity") == self.on_server.identity
        given = request.headers.get("x-latet-token", "")
        return bool(self.token) and hmac.compare_digest(given, self.token)

    async def _json(self, request: Request) -> dict[str, Any] | None:
        raw = await request.body()
        if len(raw) > _MAX_BODY:
            return None
        return await self.mediator.run_bridge(_object, raw)  # tens of MiB hold the GIL in the parser a while

    def _or_503(self, handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        """``handler``, whose call answers 503 when a store does not answer (a server's Valkey or PostgreSQL), as the
        dashboard's do: its client tries again. Said in the log once a minute with how many, by the error's type: a
        driver's words name the address it could not reach. Any other failure is raised, as it was."""

        async def endpoint(request: Request) -> Response:
            try:
                return await handler(request)
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

    async def _guard(self, request: Request) -> tuple[dict[str, Any] | None, Response | None]:
        if not self._authorized(request):
            return None, JSONResponse({"error": "unauthorized"}, status_code=401)
        data = await self._json(request)
        if data is None:
            return None, JSONResponse({"error": "invalid body"}, status_code=400)
        return data, None

    async def route_check(self, request: Request) -> Response:
        """The plugin, at the start of a session: ``{bridge, session, env, sources, entrypoint, surface}``
        → whether its requests go through Latet (``{ok, reason, toast}``). One that does not is
        recorded, for the dashboard."""
        data, error = await self._guard(request)
        if error is not None:
            return error
        assert data is not None
        verdict = (
            check_claude_route(data)
            if self.on_server is None
            else check_claude_route(data, request.scope.get("latet.space"), server_origin=self.on_server.url)
        )
        if not verdict.ok and self.recorder is not None:
            # In the bridge's thread, as a call's texts are: the folder's project worked out reads its latet.yaml, and a
            # repository seen is remembered (its list read and written), never on the event loop.
            session = await self.mediator.run_bridge(self._session, data)
            prepared = Prepared(VERIFY, "anthropic", b"", session, block_reason=verdict.reason, bypass=True)
            headers = {"user-agent": "claude-cli/plugin", "x-claude-code-session-id": str(data.get("session") or "")}
            self.recorder.finish(Outcome(headers, "anthropic", "/plugin/route-check", "plugin", time.time(), prepared))
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

    async def mask(self, request: Request) -> Response:
        """``{session, items: [{text, kind?, path?, tool?, from_model?, json?}]}`` → ``{items: [{text, count}]}``.

        ``tool`` names the tool a tool result comes from (strict data mode leaves the files it read
        alone). An item the dump guard withheld also says ``withheld: true``; one the local reviewer
        stopped says why in ``blocked``, and its text is a notice instead.

        With the Live Reviewer on, new texts wait for you: the answer is ``{pending, viewers}`` at once,
        and the plugin asks ``approvals/wait`` for the items until you decide."""
        data, error = await self._guard(request)
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
        except MaskingError as exc:  # a vault at its limit says so: a MaskingError's words name no value
            log.error("bridge masking failed: %s", exc)
            return JSONResponse({"error": f"masking failed: {exc}"}, status_code=500)
        return await self.mediator.run_bridge(_items_rendered, out, degraded)

    async def outbound(self, request: Request) -> Response:
        """``{session, tool, values: [str]}`` → ``{found: [type], reason}``: whether a tool that runs outside
        this computer (WebSearch, WebFetch, a claude.ai connector) would get stand-ins Latet put in place
        of real values. Then the plugin stops it, the model is told ``reason``, and the dashboard shows it."""
        data, error = await self._guard(request)
        if error is not None:
            return error
        assert data is not None
        found, reason = await self.mediator.run_bridge(self._outbound, data)
        return JSONResponse({"found": found, "reason": reason})

    def _outbound(self, data: dict[str, Any]) -> tuple[list[str], str | None]:
        session = self._session(data)
        tool = str(data.get("tool") or "a tool")
        values = [v for v in data.get("values") or [] if isinstance(v, str)]
        types = outbound.stand_ins(self.mediator._masker(session), values)
        if not types:
            return [], None
        reason = outbound.reason(tool, types)
        if self.recorder is not None:
            slots = [(SlotRef(values, i, SlotKind.OUTBOUND_TOOL, True), [], False) for i in range(len(values))]
            prepared = Prepared(
                VERIFY, "anthropic", b"", session, block_reason=reason, document=values, pending=slots, outbound=[tool]
            )
            headers = {"user-agent": "claude-cli/plugin", "x-claude-code-session-id": str(data.get("session") or "")}
            self.recorder.finish(Outcome(headers, "anthropic", "/plugin/tool-call", "plugin", time.time(), prepared))
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

    def _session(self, data: dict[str, Any]) -> Session:
        """The plugin's session, in the project of the folder Claude Code runs in (``cwd``)."""
        session = self.engine.session(_session_key(data.get("session")))
        self.engine.attach(session, data.get("cwd"))
        return session

    def _mask(
        self, data: dict[str, Any]
    ) -> tuple[Session, list[dict[str, Any]], list[str], list[ReviewItem], list[LiveItem]]:
        """The plugin's items masked (``_masked``), the values and the fakes their texts hold asked for first, at once,
        where the vault keeps most of them in its store (an API key's: ``Vault.prefetch``, ``prefetch_fakes``), and
        held while they are masked: a resumed conversation's fakes are adopted whatever their age."""
        items = data.get("items") or []
        texts = [item["text"] for item in items if isinstance(item, dict) and isinstance(item.get("text"), str)]
        vault = self.engine.vault
        with vault.holding():
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
        data, error = await self._guard(request)
        if error is not None:
            return error
        assert data is not None
        return await self.mediator.run_bridge(self._rehydrated, data)

    def _rehydrated(self, data: dict[str, Any]) -> JSONResponse:
        """In the bridge's thread: the texts restored, and the answer rendered there."""
        out, total = self._rehydrate(data)
        return _rendered({"items": out, "restored": total})

    def _rehydrate(self, data: dict[str, Any]) -> tuple[list[Any], int]:
        session = self._session(data)
        items = data.get("items") or []
        if data.get("model") is True and self.on_server is None and isinstance(items, list):
            # What the model wrote, as it wrote it: the next request carries it back in the model's place, and the
            # final check passes it (``Verifier.wrote``). A Latet server takes no computer's word for it: there, what
            # the model wrote is what the answers the server passed on said.
            self.mediator.verifier.wrote(session.key, [text for text in items if isinstance(text, str)])
        restores = self.engine.restores(session, json_mode=bool(data.get("json")))
        out, total = [], 0
        for text in items:
            if not isinstance(text, str):
                out.append(text)
                continue
            restored, ids = restore_text(text, restores)
            total += len(ids)
            out.append(restored)
        return out, total

    async def scope(self, request: Request) -> Response:
        """``{session, items: [str]}``: register the surrogates found in these texts with the session
        (a resumed conversation whose history is already in surrogate space)."""
        data, error = await self._guard(request)
        if error is not None:
            return error
        assert data is not None
        return JSONResponse({"added": await self.mediator.run_bridge(self._scope, data)})

    def _scope(self, data: dict[str, Any]) -> int:
        session = self._session(data)
        vault = self.engine.vault
        before = len(session.entity_ids)
        found: list[str] = []
        for text in data.get("items") or []:
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
        data, error = await self._guard(request)
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
        data, error = await self._guard(request)
        if error is not None:
            return error
        assert data is not None
        key = _session_key(data.get("session"))
        raw = [s for s in data.get("slots") or [] if isinstance(s, dict) and isinstance(s.get("text"), str)]
        slots = [(s["text"], _kind(s.get("kind")), bool(s.get("from_model"))) for s in raw]
        history = data.get("history")
        history = history if isinstance(history, int) and not isinstance(history, bool) and history > 0 else 0
        result, slots = await self.mediator.run_bridge(self._verify, data, slots, key)
        blocks = [x for x in result.leaks if x.blocks]
        inherited = bool(blocks) and all(0 <= x.slot < history for x in blocks)
        if result.blocking and self.recorder is not None:
            self._record_block(data.get("session"), key, slots, result, inherited)
        leaks = [({"type": x.etype, "kind": x.kind.value, "preview": x.preview, "new": x.new}, x) for x in result.leaks]
        blocking = [d for d, x in leaks if x.blocks]
        flagged = [d for d, x in leaks if not x.blocks]
        return JSONResponse({"blocking": blocking, "flagged": flagged, "inherited": inherited})

    def _verify(
        self, data: dict[str, Any], slots: list[tuple[str, SlotKind, bool]], key: str
    ) -> tuple[VerifyResult, list[tuple[str, SlotKind, bool]]]:
        """The texts, and the text read in each image (an image Latet painted itself is not read again)."""
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
        texts = [text for text, _, _ in slots]
        reason = HISTORY_REASON if inherited else "stopped by the Claude Code plugin before it was sent"
        prepared = Prepared(
            VERIFY,
            "anthropic",
            b"",
            self.engine.session(key),
            list(result.leaks),
            block_reason=reason,
            history=inherited,
            document=texts,
            pending=[(SlotRef(texts, i, kind, from_model), [], False) for i, (_, kind, from_model) in enumerate(slots)],
        )
        headers = {"user-agent": "claude-cli/plugin", "x-claude-code-session-id": str(session_id or "")}
        self.recorder.finish(Outcome(headers, "anthropic", "/plugin/final-check", "plugin", time.time(), prepared))
