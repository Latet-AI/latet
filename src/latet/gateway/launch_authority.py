"""The one launch authority a request asks (the owner's Revision 9, R-113..R-116; root's ruling I-1 on P2-6a's review).

A request's launch is decided by one interface on both machines. What differs is what is injected at construction: a
computer's ingress hands the request path its carrier as its launch keys checked it, with the computer's authority
(``local_launch.LocalLaunchAuthority``); a server's space hands it the derived input with the server's protected
authority (``server.launch_requests.ServerLaunchRequests``). The request path (``mediate``, ``forward``, ``ws``) only
calls ``RequestLaunch.decide`` and applies its ``LaunchChoice``; the one freshness check is the choice's ``current``,
awaited where the request is sent (``request_transport``)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

REQUEST_LAUNCH = "latet.request_launch"

Status = Literal["verified", "legacy", "claimed", "refused", "unavailable"]

# What a request's launch was decided as, by fixed categories (root's ruling I-5), never a value: ``launched`` its
# launch key verified; ``launched-key-missing`` a session Latet launched (a launch key it does not know, or a session
# marked "launched, key required") without a key it can verify, decided as claimed; ``trusted`` the plugin's binding of
# a hand-configured Claude Code; ``claimed`` anything else, whose claimed folder only tightens. P2-7 shows it.
LAUNCH_CATEGORIES = ("launched", "launched-key-missing", "trusted", "claimed")
Category = Literal["launched", "launched-key-missing", "trusted", "claimed"]


@dataclass(frozen=True, slots=True, repr=False)
class LaunchChoice:
    """What one authority decided for one request. ``project`` is the project a verified launch or the plugin's binding
    names (checked against the settings in force); ``current`` the one freshness check of that authority, awaited
    before the request (and each of its frames) is sent. A claim carries neither: the request path resolves its folder,
    which only tightens."""

    status: Status
    project: Any = None
    current: Callable[[], Awaitable[bool]] | None = None
    category: Category = "claimed"


REFUSED = LaunchChoice("refused")
UNAVAILABLE = LaunchChoice("unavailable")
CLAIMED = LaunchChoice("claimed")
KEY_MISSING = LaunchChoice("claimed", category="launched-key-missing")


class LaunchAuthority(Protocol):
    def decide(
        self, presented: Any, session: Any, claimed: str | None, *, legacy_eligible: bool, projects: Any
    ) -> LaunchChoice: ...


@dataclass(frozen=True, slots=True, repr=False)
class RequestLaunch:
    """What an ingress hands the request path, the same type on both machines: the authority construction injected and
    what it derived from the request's carrier (None when the request carried none). Neither holds the key."""

    authority: LaunchAuthority | None
    presented: Any = None

    def decide(self, session: Any, claimed: str | None, *, legacy_eligible: bool, projects: Any) -> LaunchChoice:
        if self.authority is None:  # no launch authority here: every request is a claim, which only tightens
            return KEY_MISSING if self.presented is not None else CLAIMED
        return self.authority.decide(
            self.presented, session, claimed, legacy_eligible=legacy_eligible, projects=projects
        )


ABSENT = RequestLaunch(None)


def key_project(projects: Any, folder: str, root: str, project: str, claimed: str | None) -> Any:
    """The project a verified launch key binds, the same function for both authorities (R-105, D7): the project of the
    key's ``folder``, when it is still the key's (its id, its root), and when what the request claims, if anything, is
    that same project. Projects are compared, never folders: a claimed subfolder of the key's project is that project,
    and a claim into another one (a project of its own inside the key's, say) refuses the request, so that it gets
    neither the key's loosenings nor escapes the other project's added rules. None: refused."""
    from latet.projects import _a_folder

    found = projects.resolve(folder)
    if found.id != project or found.root != root:
        return None
    if claimed is not None and (not _a_folder(claimed) or projects.compared_id(claimed) != found.id):
        return None
    return found
