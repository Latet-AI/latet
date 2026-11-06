"""The rules a request's decision is made under (the request-coverage plan's RC-REC-1, Phase 2).

They are built in: the rules a ``Pass`` may name (what the final check alone read, an image's remainder after its text
was read, a text sent before in the conversation, what the plugin reported as the model's, a provider's opaque echo
observed or its encrypted field), at one revision, the same for every request, worker and instance. Nothing sets,
widens or turns them off: no setting of ``config.yaml``, no variable of the environment, no command's option, no
repository file, no header (``current`` takes no argument). Phase 1's internal rule that named 0.10's behaviour is gone
(P2-6): a request is sent with a complete proof, or under one of these rules visibly, or not at all."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

BUILT_IN: frozenset[str] = frozenset(
    {
        "checked only",
        "image remainder after text recognition",
        "sent before in this conversation",
        "reported as written by the model",
        "observed opaque provider echo (1)",
        "provider encrypted field (1)",
    }
)
REVISION = "phase-2-policy-1"
# What the history and the dashboard say a ``Pass`` rested on (P2-7), one fixed category for each built-in rule, and
# ``exception`` for a request exception (never its id): the category alone, never what the rule let through.
PASS_CATEGORIES: dict[str, str] = {
    "checked only": "checked-only",
    "image remainder after text recognition": "image-remainder",
    "sent before in this conversation": "sent-before",
    "reported as written by the model": "model-written",
    "observed opaque provider echo (1)": "provider-echo",
    "provider encrypted field (1)": "provider-encrypted",
}
PASSES: frozenset[str] = frozenset({*PASS_CATEGORIES.values(), "exception"})


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    """The rules one decision is made under, by name, at one revision: built-in rules alone."""

    rules: tuple[str, ...] = tuple(sorted(BUILT_IN))
    revision: str = REVISION

    def __post_init__(self) -> None:
        if type(self.rules) is not tuple or len(set(self.rules)) != len(self.rules):
            raise ValueError("a policy's rules are a tuple of distinct names")
        if not set(self.rules) <= BUILT_IN:
            raise ValueError("a policy names built-in rules alone")
        if self.revision != REVISION:
            raise ValueError("not a revision of the rules this version has")

    def allows(self, rule: str) -> bool:
        return rule in self.rules


BUILT_IN_POLICY = PolicySnapshot()


def current() -> PolicySnapshot:
    """The rules every decision is made under now: the built-in ones, the same for every request, worker and
    instance."""
    return BUILT_IN_POLICY


def body_protocol(profile: str) -> str | None:
    """The protocol a request of ``profile`` has its body read by (``mediate.protocol_for``), whose body revisions an
    exception's body components are stamped and carried over by (R-100): Anthropic's messages and count of tokens,
    Bedrock's and Vertex's Claude calls, a company's Bedrock and Vertex routes (``anthropic``); chat completions and
    completions, Azure's, Foundry's and Gemini's OpenAI-compatible ones (``chat``); responses, Codex's included
    (``responses``); embeddings (``embeddings``); Gemini's own calls (``gemini``). None: a profile with no body (a
    model list, a model read, Bedrock's inference profiles), where no body component applies."""
    parts = profile.split(".")
    operation = parts[-2] if len(parts) >= 3 else ""
    if operation in {"models", "model", "inference-profile", "inference-profiles"}:
        return None
    if parts[0] in {"anthropic", "bedrock", "vertex"} or profile.startswith(("company.bedrock.", "company.vertex.")):
        return "anthropic"
    if operation in {"chat", "completions"}:
        return "chat"
    if operation in {"responses", "embeddings"}:
        return operation
    if parts[0] == "gemini":
        return "gemini"
    return None


def body_selectors(row: Any) -> tuple[Any, ...]:
    """An exception's body components as the selectors they are (``body_selectors.BodySelector``)."""
    from latet.gateway.body_selectors import BodySelector

    return tuple(BodySelector(c.name, c.type) for c in row.components if c.kind == "body")


def carried(row: Any) -> bool:
    """``row`` approved under the request profiles this version has, or a revision whose exceptions carry over to it
    unchanged (``request_profiles.CARRIES_FROM``, R-100), and its body components, if any, approved under its body
    protocol's revision that still names them (``body_selectors.carries_over``): else suspended, needing approval
    again. A body component with no body revision stamped (or a profile that has no body) carries nothing."""
    from latet.gateway import body_selectors as selectors
    from latet.gateway import request_profiles

    if row.revision != request_profiles.REVISION and row.revision not in request_profiles.CARRIES_FROM:
        return False
    if not any(c.kind == "body" for c in row.components):
        return True
    protocol = body_protocol(row.profile)
    if protocol is None or row.body_revision is None:
        return False
    try:
        return selectors.carries_over(body_selectors(row), protocol, row.body_revision, history=selectors.HISTORY)
    except selectors.SelectorRefused:
        return False


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class ProfileGrants:
    """The body selectors of one profile's exceptions, built once for the rules in force (``body_grants``): ``every``,
    those of all of them (what the launch-credential screen reads); ``identity``, those that apply to any request of
    the identity (``shared``, each one's); ``projects``, for each folder a project's exception names, that exception's.
    A request takes a project's only when Latet trusts its project (RC-REC-7, D7), else the identity's (``chosen``)."""

    every: Any
    identity: Any | None
    shared: tuple[Any, ...]
    projects: tuple[tuple[str, tuple[Any, ...]], ...]
    _unions: dict[tuple[str, ...], Any] = field(default_factory=dict)

    def chosen(self, trusted: frozenset[str]) -> Any | None:
        """The selectors a request takes: the identity's, with those of each folder in ``trusted`` (the folders of the
        project exceptions that name the project Latet trusts the request's to be, ``trusted_folders``): all of them,
        a project's exceptions on its root and on a subfolder of it together, built once for each set."""
        from latet.gateway.body_selectors import BodyGrants

        matched = tuple(folder for folder, _ in self.projects if folder in trusted)
        if not matched:
            return self.identity
        found = self._unions.get(matched)
        if found is None:
            grants = [*self.shared, *(grant for folder, own in self.projects if folder in trusted for grant in own)]
            found = self._unions[matched] = BodyGrants(grants)
        return found


def trusted_folders(
    rows: tuple[Any, ...], *, binding: str | None, compared: str | None, projects: Any
) -> frozenset[str]:
    """The folders of the project exceptions among ``rows`` that belong to the project Latet trusts a request's to be
    (RC-REC-7, D7): none unless its binding is ``trusted``; each folder compared with ``compared``, the trusted
    project's id, as projects are compared (R-105, ``Projects.compared_id``), so that an exception on a project's
    root and one on a subfolder of it both apply, and a project of its own inside it is not that project. Called where
    the request is prepared, never on the event loop: on a computer, comparing reads the repository's files."""
    if binding != "trusted" or compared is None:
        return frozenset()
    compare = getattr(projects, "compared_id", None)
    if compare is None:
        return frozenset()
    folders = {row.project for row in rows if row.project is not None}
    return frozenset(folder for folder in folders if compare(folder) == compared)


def body_grants(rows: tuple[Any, ...]) -> tuple[tuple[str, ProfileGrants], ...]:
    """The approved exceptions of one destination with body components, as each profile's ``ProfileGrants``. A row
    whose selectors this version cannot read is left out (it grants nothing)."""
    from latet.gateway.body_selectors import BodyGrant, BodyGrants, SelectorRefused

    by_profile: dict[str, list[tuple[Any, Any]]] = {}
    for row in rows:
        if not any(c.kind == "body" for c in row.components):
            continue
        try:
            grant = BodyGrant(row.id, body_selectors(row))
        except SelectorRefused:
            continue
        by_profile.setdefault(row.profile, []).append((row, grant))
    out = []
    for profile, found in by_profile.items():
        shared = [grant for row, grant in found if row.project is None]
        folders: dict[str, list[Any]] = {}
        for row, grant in found:
            if row.project is not None:
                folders.setdefault(row.project, []).append(grant)
        out.append(
            (
                profile,
                ProfileGrants(
                    BodyGrants([grant for _, grant in found]),
                    BodyGrants(shared) if shared else None,
                    tuple(shared),
                    tuple((folder, tuple(grants)) for folder, grants in folders.items()),
                ),
            )
        )
    return tuple(out)


def body_exception_ids(rules: tuple[str, ...]) -> tuple[str, ...]:
    """The exceptions a body's rules name (``body_selectors.exception_rule``), in order, each once."""
    return tuple(dict.fromkeys(rule[len("exception ") :] for rule in rules if rule.startswith("exception ")))


def in_force(config: Any, now: datetime, *, permanent_allowed: bool = True) -> tuple[Any, ...]:
    """The request exceptions of ``config`` that grant something at ``now`` (D3, R-100): approved (stamped by Latet as
    you approved them: one pending, rejected or written by hand grants nothing), within their lifetime (a permanent
    one always, unless the edition forbids permanent ones: ``permanent_allowed``), and approved under the request
    profiles this version has or one carried over (one approved under another revision is suspended: it needs approval
    again). ``now`` is the decision's clock: PostgreSQL's on a server, this computer's otherwise."""
    out = []
    for row in config.request_exceptions:
        if not row.approved or not carried(row):
            continue
        if row.permanent and not permanent_allowed:
            continue
        ends = row.expires()
        if ends is None and not row.permanent:
            continue
        if ends is not None and now >= ends:
            continue
        out.append(row)
    return tuple(out)


# How long before its end an exception is said to be expiring (D3): the dashboard, `latet doctor` and the server's
# status warn from then on, with the days left.
EXPIRY_WARNING = timedelta(days=7)


def _now() -> datetime:
    return datetime.now(UTC)


def listed(config: Any, now: datetime, *, permanent_allowed: bool = True) -> list[dict[str, Any]]:
    """Each request exception of ``config`` as it stands at ``now``, for `latet doctor`, the server's status and the
    dashboard: Latet's id, its destination, its scope's category (``identity`` or ``project``), whether it is
    permanent, its state (``pending``, ``needs approval again (profile revised)``, ``expired``, ``expiring``,
    ``active``, ``permanent``, ``forbidden``) and its end with the days left. Never its description, its project's
    folder or what it names."""
    out: list[dict[str, Any]] = []
    for row in config.request_exceptions:
        ends = row.expires()
        if not row.approved:
            state = "pending"
        elif not carried(row):
            state = "needs approval again (profile revised)"
        elif row.permanent:
            state = "permanent" if permanent_allowed else "forbidden"
        elif ends is not None and now >= ends:
            state = "expired"
        elif ends is not None and ends - now <= EXPIRY_WARNING:
            state = "expiring"
        else:
            state = "active"
        out.append(
            {
                "id": row.id,
                "destination": row.destination,
                "scope": "project" if row.project is not None else "identity",
                "permanent": row.permanent,
                "state": state,
                "ends": ends.date().isoformat() if ends is not None else None,
                "days_left": max(0, (ends - now).days) if ends is not None else None,
            }
        )
    return out


@dataclass(frozen=True, slots=True, repr=False)
class LocalAuthority:
    """Applied local producers only; checking this never loads settings or contacts a store."""

    policy: PolicySnapshot
    engine: Any = field(repr=False)
    rules: int
    upstream: Any = field(repr=False)
    routes: Any = field(default=None, repr=False)
    path: str = field(default="", repr=False)
    session: Any = field(default=None, repr=False)
    facts: tuple[object, ...] = field(default=(), repr=False)
    off: bool = False
    empty: bool = False

    @classmethod
    def capture(cls, engine: Any, upstream: Any, routes: Any = None, path: str = "") -> LocalAuthority:
        return cls(current(), engine, engine.rules_version, upstream, routes, path)

    def rederivable(self) -> bool:
        """Invalid only because the settings moved (a newer policy or rules version) while its route still selects the
        same destination: it may be taken again under the rules now in force. Its launch is the launch authority's one
        freshness check, awaited where it is sent (``RequestScope.protected_current``)."""
        return (self.policy is not current() or self.rules != self.engine.rules_version) and self._same_route()

    def valid(self) -> bool:
        return (
            self.policy is current()
            and self.rules == self.engine.rules_version
            and self._same_route()
            and (self.session is None or self.facts == session_facts(self.session))
        )

    def _same_route(self) -> bool:
        """Whether its path still selects the route it was captured with, compared by what the route is: settings
        applied again make a new ``Upstream`` for every configured destination (``RouteTable.use``), equal to the one
        before when nothing of it changed (its address, profile, exceptions, request resources: every field an
        ``Upstream`` compares). One whose content changed is another route (minors after 0.11, item 2)."""
        if self.routes is None:
            return True
        found = self.routes.match(self.path)
        return found is self.upstream or (found is not None and found == self.upstream)


def session_facts(session: Any) -> tuple[object, ...]:
    return (
        session.key,
        session.project_id,
        session.cwd,
        session.reported,
        session.kept,
        getattr(session, "launch_required", False),
    )


def granted(
    used: tuple[tuple[tuple[Any, ...], Any], ...], now: datetime, *, trusted: frozenset[str]
) -> tuple[tuple[Any, ...], Any]:
    """The exception each component of a request is sent under, and None; or nothing, and why it is not sent, as an
    ``EnvelopeRefused``. ``used``: each component a stored exception lets through (``Admission.exceptions``, and its
    body's), with the exceptions that name it (the identity's first) and the refusal it would have had without them.

    A component takes the first of its exceptions that applies: the identity's, or a project's whose folder is in
    ``trusted`` (``trusted_folders``: the project Latet trusts the request's to be, RC-REC-7, D7), approved no later
    than ``now`` and not past its lifetime by it (the decision's clock: PostgreSQL's on a server). None applying, a
    project's alone naming it gets the refusal it would have had without it (a header, a folder in its text, no binding
    at all: nothing is said of the exception); one that ended is refused naming its id and the day it ended (D3), never
    its description or what it names."""
    from latet.gateway.coverage import EnvelopeRefused

    chosen = []
    for rows, original in used:
        scoped = [row for row in rows if row.project is None or row.project in trusted]
        found = next((row for row in scoped if _within(row, now)), None)
        if found is not None:
            chosen.append(found)
            continue
        ended = next((row for row in scoped if row.granted is not None and row.granted <= now), None)
        ends = ended.expires() if ended is not None else None
        if ended is None or ends is None:
            return (), original
        return (), EnvelopeRefused("envelope-exception-expired", expired=(ended.id, ends.date().isoformat()))
    return tuple(chosen), None


def _within(row: Any, now: datetime) -> bool:
    """Approved no later than ``now`` (a time ahead of it is never Latet's stamp: it grants nothing), and permanent or
    not past its end."""
    if row.granted is None or row.granted > now:
        return False
    ends = row.expires()
    return ends is None or now < ends


_REFERENCES: OrderedDict[str, Any] = OrderedDict()
_REFERENCES_KEPT = 4096
_SCOPES = {"destination-credential": 1, "custom-header": 2, "query-parameter": 3, "other": 4}


def reference_of(row: Any) -> Any:
    """The status reference an exception's uses are counted under in this process (M1, ``request_audit``): one per
    exception, made once, its scope its reason's fixed category; never its description or what it names."""
    from latet.gateway.request_audit import new_exception_reference

    found = _REFERENCES.get(row.id)
    if found is None:
        found = _REFERENCES[row.id] = new_exception_reference(1, _SCOPES[row.reason])
        while len(_REFERENCES) > _REFERENCES_KEPT:
            _REFERENCES.popitem(last=False)
    else:
        _REFERENCES.move_to_end(row.id)
    return found
