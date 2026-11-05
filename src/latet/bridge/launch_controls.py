"""A computer's launch controls (``launch_keys.LaunchControls``): the bridge's launch routes ask one implementation of
the controls, injected as the bridge is built (R-114; root's ruling I-1 on P2-6a's review), a server's
(``server.launch_lifecycle.ProtectedLaunchControls``) or this one, over the computer's own launch keys. No handler asks
which machine it runs on."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from latet.gateway.launch_keys import Binding, Hold, LaunchRefused, LocalLaunchKeys

if TYPE_CHECKING:
    from starlette.types import Scope

ReadJson = Callable[[frozenset[str]], Awaitable[dict[str, Any]]]
_OPEN = frozenset(("folder",))
_RECOVER = frozenset(("key", "launch_id", "folder", "epoch", "retirement"))


def _abandoned(owner: LocalLaunchKeys, ticket: Any, _work: Future[str | None]) -> None:
    """A cancelled short control still owns its prepaid worker until it actually ends."""
    owner.abort(ticket)


def _observe(view: asyncio.Future[str | None]) -> None:
    if not view.cancelled():
        view.exception()


class LocalLaunchControls:
    """The launch routes over a computer's launch keys. ``owner`` None (a bridge built without them): every control is
    unavailable."""

    fields = _OPEN
    recovery_fields = _RECOVER

    def __init__(self, owner: LocalLaunchKeys | None, mediator: Any) -> None:
        self.owner = owner
        self._mediator = mediator

    def _owner(self) -> LocalLaunchKeys:
        if self.owner is None:
            raise LaunchRefused("launch-unavailable")
        return self.owner

    def _launch_binding(self, folder: Any) -> Binding:
        from latet.projects import Projects, project_id

        if type(folder) is not str:
            raise LaunchRefused("launch-invalid")
        try:
            if not folder or len(folder) > 4096 or len(folder.encode("utf-8")) > 4096:
                raise LaunchRefused("launch-invalid")
            canonical = Path(folder).resolve(strict=True)
            if not canonical.is_dir():
                raise LaunchRefused("launch-unregistered")
            projects = self._mediator.engine.scopes
            if not isinstance(projects, Projects):
                raise LaunchRefused("launch-unregistered")
            project = projects.resolve(str(canonical))
            if project.repo_file is not None and project.repo is not None:
                root = Path(project.root).resolve(strict=True)
            elif project.personal is not None:
                root = Path(project.personal.path).resolve(strict=True)
            else:
                raise LaunchRefused("launch-unregistered")
            # The binding owns bytes/IDs only, not the Project or its settings/masker graph.
            return Binding(str(canonical).encode("utf-8"), str(root).encode("utf-8"), project_id(str(root)))
        except (OSError, UnicodeError, ValueError) as error:
            if isinstance(error, LaunchRefused):
                raise
            raise LaunchRefused("launch-unregistered") from None

    def _launch_stage(self, owner: LocalLaunchKeys, ticket: Any, folder: str) -> str | None:
        # An exception shared with the request awaiter can retain the request in its traceback.
        try:
            owner.stage_binding(ticket, self._launch_binding(folder))
        except LaunchRefused as error:
            match error.code:
                case ("launch-invalid" | "launch-unregistered" | "launch-stale" | "launch-mismatch") as code:
                    return code
                case _:
                    return "launch-unavailable"
        except Exception:
            return "launch-unavailable"
        return None

    def _launch_work(self, owner: LocalLaunchKeys, ticket: Any, folder: Any) -> Future[str | None]:
        # Reject credential-shaped control metadata before a worker/Future could retain it.
        if type(folder) is not str or not folder or len(folder) > 4096:
            raise LaunchRefused("launch-invalid")
        try:
            encoded = folder.encode("utf-8")
        except UnicodeError:
            raise LaunchRefused("launch-invalid") from None
        if len(encoded) > 4096 or re.search(rb"latet_lk_[A-Za-z0-9_-]{43}", encoded):
            raise LaunchRefused("launch-invalid")
        return self._mediator.submit_launch(self._launch_stage, owner, ticket, folder)

    @staticmethod
    def _launch_view(work: Future[str | None]) -> asyncio.Future[str | None]:
        # Cancelling this private view must never cancel the wrapped executor Future.
        wrapped = asyncio.wrap_future(work)
        wrapped.add_done_callback(_observe)
        view = asyncio.shield(wrapped)
        view.add_done_callback(_observe)
        return view

    async def _staged(self, owner: LocalLaunchKeys, ticket: Any, folder: Any, commit: Callable[[Any], Any]) -> Any:
        work = None
        try:
            work = self._launch_work(owner, ticket, folder)
            refused = await asyncio.shield(self._launch_view(work))
            if refused is not None:
                raise LaunchRefused(refused)
            return commit(ticket)
        finally:
            if work is None or work.done():
                owner.abort(ticket)
            else:
                work.add_done_callback(partial(_abandoned, owner, ticket))

    async def open(self, scope: Scope, read: ReadJson) -> dict[str, Any]:
        owner = self._owner()
        ticket = owner.begin_open("computer")  # reserves before body/file awaits, and before mint/disclosure
        try:
            data = await read(self.fields)
        except BaseException:
            owner.abort(ticket)
            raise
        return await self._staged(owner, ticket, data["folder"], owner.commit_open)

    async def recover(self, scope: Scope, read: ReadJson) -> dict[str, Any]:
        owner = self._owner()
        data = await read(self.recovery_fields)
        ticket = owner.begin_recovery(data.pop("key"), data["launch_id"], "computer", data["epoch"], data["retirement"])
        return await self._staged(owner, ticket, data["folder"], owner.commit_recovery)

    def hold(self, scope: Scope, launch_id: str, generation: int) -> Hold:
        return self._owner().reserve_hold(launch_id, "computer", generation)

    async def end(self, scope: Scope, launch_id: str, *, exit_epoch: str | None = None) -> bool:
        owner = self._owner()
        if exit_epoch is None:
            return owner.end(launch_id, "computer")
        return owner.confirmed_exit(launch_id, "computer", exit_epoch)
