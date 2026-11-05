"""A server's launch authority (``launch_authority.LaunchAuthority``), between the gateway and the protected
authority: injected into an identity's space as it is built; the request path asks it through ``RequestLaunch``."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal

from latet.gateway.launch_authority import KEY_MISSING, REFUSED, UNAVAILABLE, LaunchChoice, key_project
from latet.server.launch_binding import Binding, BindingCodec
from latet.server.launch_records import LaunchUnavailable
from latet.server.launches import LaunchAuthority, Receipt
from latet.server.plugin_bindings import PluginBinding, PluginBindings


@dataclass(frozen=True, slots=True, repr=False)
class ServerLaunchInput:
    locator: bytes
    verifier: bytes


@dataclass(frozen=True, slots=True, repr=False)
class ServerFreshness:
    authority: LaunchAuthority | PluginBindings
    receipt: Receipt | PluginBinding

    def current(self) -> bool:
        if isinstance(self.authority, LaunchAuthority) and type(self.receipt) is Receipt:
            try:
                return self.authority.revalidate(self.receipt)
            except LaunchUnavailable:
                return False
        if isinstance(self.authority, PluginBindings) and type(self.receipt) is PluginBinding:
            return self.authority.current(self.receipt)
        return False


@dataclass(frozen=True, slots=True, repr=False)
class ServerLaunchSelection:
    status: Literal["claimed", "verified", "legacy", "refused", "unavailable"]
    binding: Binding | None = None
    legacy_project: str | None = None
    freshness: ServerFreshness | None = None
    key_missing: bool = False  # a session Latet launched (a key it does not know, a marked session) decided as claimed


async def _in_thread(fn: Callable[[], bool]) -> bool:
    return await asyncio.to_thread(fn)


@dataclass(frozen=True, slots=True, repr=False)
class ServerLaunchRequests:
    authority: LaunchAuthority
    codec: BindingCodec
    plugins: PluginBindings
    # Where a freshness check that reads the protected store runs: the space's engine thread (``Mediator.run``), never
    # the event loop.
    run: Callable[[Callable[[], bool]], Awaitable[bool]] = field(default=_in_thread, repr=False)

    def derive(self, key: bytes) -> ServerLaunchInput:
        """Called synchronously at ingress. The worker only receives derived input."""
        return ServerLaunchInput(self.authority.locator(key), self.codec.verifier(key))

    def decide(
        self, presented: Any, session: Any, claimed: str | None, *, legacy_eligible: bool, projects: Any
    ) -> LaunchChoice:
        """The request path's one question (``RequestLaunch.decide``), in the engine's thread."""
        from latet.projects import Projects, _a_folder

        selected = self.select(
            presented,
            session.key if session is not None else None,
            claimed_folder=claimed,
            legacy_eligible=legacy_eligible,
        )
        if selected.status == "unavailable":
            return UNAVAILABLE
        if selected.status == "refused":
            return REFUSED
        project = None
        if selected.status == "verified":
            binding = selected.binding
            if binding is None or not isinstance(projects, Projects):
                return REFUSED
            project = key_project(projects, binding.root, binding.root, binding.project, claimed)
            if project is None:
                return REFUSED
        elif selected.status == "legacy" and isinstance(projects, Projects):
            known = projects.get(selected.legacy_project) if selected.legacy_project is not None else None
            if (
                known is not None
                and known.personal is not None
                and self.matches_legacy(selected, known.id, known.root)
                and (claimed is None or (_a_folder(claimed) and projects.resolve(claimed).personal == known.personal))
            ):
                project = known
        if project is not None and selected.freshness is not None:
            current = partial(self.run, selected.freshness.current)
            return LaunchChoice(selected.status, project, current, "launched" if selected.binding else "trusted")
        return KEY_MISSING if selected.key_missing else LaunchChoice("claimed")

    def select(
        self,
        presented: ServerLaunchInput | None,
        session: str | None,
        *,
        claimed_folder: str | None,
        legacy_eligible: bool,
    ) -> ServerLaunchSelection:
        if presented is not None:
            decided = self.authority.decide(
                self.plugins.issuer,
                presented.locator,
                presented.verifier,
                self.codec,
                claimed_folder=claimed_folder,
            )
            if decided.state == "live" and decided.receipt is not None and decided.binding is not None:
                return ServerLaunchSelection(
                    "verified", decided.binding, freshness=ServerFreshness(self.authority, decided.receipt)
                )
            if decided.state == "claimed":
                # A supplied canonical key with absent PG authority is claimed.
                # Never fall through to a plugin grant on this request.
                return ServerLaunchSelection("claimed", key_missing=True)
            return ServerLaunchSelection("unavailable" if decided.state == "unavailable" else "refused")
        if legacy_eligible and session is not None:
            known = self.plugins.read(session)
            if known is not None and not known.required:
                return ServerLaunchSelection(
                    "legacy", legacy_project=known.project.hex(), freshness=ServerFreshness(self.plugins, known)
                )
            if known is not None:  # a session Latet launched, without its key: a claim (R-106)
                return ServerLaunchSelection("claimed", key_missing=True)
        return ServerLaunchSelection("claimed")

    def matches_legacy(self, selected: ServerLaunchSelection, project: str, root: str) -> bool:
        if selected.status != "legacy" or selected.freshness is None or selected.legacy_project != project:
            return False
        proof = selected.freshness.receipt
        digest = self.plugins.binding_digest(project, root)
        return type(proof) is PluginBinding and digest is not None and hmac.compare_digest(digest, proof.binding_digest)
