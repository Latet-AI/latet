"""Where the floor comes from (Latet Enterprise, locked rules), and how the edition hears that it changed.

The Enterprise Edition is a Latet server's alone: its policy is the one in the server's database
(latet.ee.policy_server). A source calls ``changed`` (the edition's ``floor_changed``) when the floor in force changes:
every space on the server is built on the new floor at once. The floor is never read from the database or weighed
against the license when it is asked: the source looks again in ``refresh``."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from latet.doctor.checks import Check
    from latet.ee.policy import Floor
    from latet.server.app import Server
    from latet.state import IdentityRef


class PolicySource(Protocol):
    def floor(self) -> Floor | None:
        """The floor in force, as looked at last: the same for every identity, and the same object until a look finds
        it changed. None: no floor. A server's raises PolicyUnavailable (``latet.floor.FloorUnavailable``) rather than
        answer None while a policy is stored that it never read, or before it ever read the license (ruling 12, L4):
        no space is built below a policy it cannot say."""
        ...

    def refresh(self) -> bool: ...  # look again now: True when the floor in force changed (``changed`` was called)
    def summary(self) -> dict[str, Any] | None: ...  # where it comes from and what is wrong; None: nothing to say
    def start(self) -> None: ...  # look again by itself from now on
    def close(self) -> None: ...


def _nothing() -> None:
    return None


def _ignore(event: Mapping[str, Any]) -> None:
    return None


def local_time(seconds: float) -> str:
    """A moment as `latet server policy show` prints it: in the time of the administrator's own computer."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))


def utc_time(seconds: float) -> str:
    """A moment as a server says it (`latet server status`): in UTC, said so (an instance's own zone is nobody's)."""
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(seconds))


def source_view(floor: Floor) -> dict[str, Any]:
    """Where a floor comes from, as /me and `latet server status` say it. /me is every person's: the notices are
    counted, never quoted (their words stay with the administrators, M6)."""
    return {"source": floor.source, "origin": floor.origin, "received": floor.received, "notices": len(floor.notices)}


def make_policy(
    server: Server,
    *,
    licensed: Callable[[], bool | None],
    changed: Callable[[], None] = _nothing,
    record: Callable[[Mapping[str, Any]], None] = _ignore,
) -> PolicySource:
    """Where the edition's floor comes from: the server's database, never a folder of the machine it runs on, started.
    ``licensed``: whether the license switches locked rules on (None: never read here); ``changed``: the edition's
    floor_changed."""
    from latet.ee.policy_server import ServerPolicy

    return ServerPolicy.of(server, licensed=licensed, changed=changed, record=record)


def policy_page(
    identity: IdentityRef | None, answer: Mapping[str, Any], licensed: bool, reads: bool | None = None
) -> bool:
    """Whether /me lists the Policy page: on a server, for whoever reads it, when a policy is in force or said to be
    wrong, or the license has locked rules. Without an identity it is never listed. ``reads``: whether the identity
    reads the page, as E3's roles say (everyone while the license has no roles); None: the actions decide (`policy.read`
    in ``answer``)."""
    there = answer.get("policy") is not None or licensed
    return identity is not None and there and (("policy.read" in answer.get("actions", [])) if reads is None else reads)


def policy_checks(policy: PolicySource) -> list[Check]:
    """The policy's lines of `latet server status`: where it comes from, when it was set, and what in it is wrong or
    not read (counted: a notice's words stay with the administrators, M6). None without a policy."""
    from latet.doctor.checks import OK, WARN
    from latet.doctor.checks import Check as Line

    summary = policy.summary()
    if summary is None:
        return []
    out = [Line(problem["level"], "Policy", problem["text"]) for problem in summary["problems"]]
    for source in summary["sources"]:
        out.append(Line(OK, "Policy", f"{source['origin']}, set {utc_time(source['received'])}"))
        if count := source["notices"]:
            out.append(Line(WARN, "Policy", f"{count} setting{' in it is' if count == 1 else 's in it are'} not read"))
    return out
