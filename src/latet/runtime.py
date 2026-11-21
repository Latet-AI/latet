"""Wire configuration, keys, storage and engine into a ready gateway configuration."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import hmac
import json
import logging
import threading
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from latet import repository_io
from latet.config import (
    Config,
    IdentityConfig,
    LlmReview,
    ProjectSettings,
    UnknownMembers,
    config_path,
    data_dir,
    detect_identity,
    in_container,
)
from latet.edition import Community, Edition, EditionContext, load_edition
from latet.engine import stored as stored_vaults
from latet.engine.build import Built, build, build_masker
from latet.engine.detectors.base import DetectContext
from latet.engine.pipeline import SPACE_BLOCK_BYTES, SPACE_CACHE_BYTES
from latet.engine.review import ReviewPolicy
from latet.engine.reviewer import Reviewer
from latet.engine.reviewer.client import ReviewClient, key_id
from latet.engine.reviewer.reviewer import KeyReader, api_key_of, client_for, fixed_key
from latet.engine.stored import DETECTORS_EXTRA_MOST, HELD_SECRETS, WORKING_VALUES, StoredVault
from latet.engine.vault import Entity, MaskingError, Vault
from latet.events import HIDDEN_PROJECT, HIDDEN_SHAPE, code_of, exchange_event, make_event, name_of
from latet.floor import COMPANY_DECIDES, PROVIDER_NAME, BelowFloor, Floor, company_decides, fresh_reasons, raised_to
from latet.gateway.app import GatewayConfig
from latet.gateway.approvals import Approvals, Batch, Change, Decides, Decision, Waits
from latet.gateway.aws import AwsCredentialStore
from latet.gateway.launch_keys import LocalLaunchKeys
from latet.gateway.mediate import Mediator, ReviewRecord
from latet.gateway.provenance import NoEchoes
from latet.gateway.recorder import Recorder
from latet.gateway.request_audit import RequestAudit as AuditOwner
from latet.gateway.routes import Upstream, destination_upstream
from latet.logline import Paced, described
from latet.projects import (
    Projects,
    RepositoriesUnchecked,
    RepositoryScan,
    SeenRepositories,
    _left_in_clear,
)
from latet.protection import (
    changed_keys,
    destination_changes,
    effective_unknown_members,
    exception_changes,
    guidance_changes,
    lets_through,
    loosened,
    new_members_checked,
    stamp_exceptions,
    weighs_repositories,
)
from latet.server import egress
from latet.server.shared import (
    CHANGED_MEANWHILE,
    ChangedMeanwhile,
    ChangeWaits,
    OwnSettings,
    SettingsSavedChangePending,
    SharedItemsError,
    own_lowers,
)
from latet.state.local import (  # noqa: F401 - REVIEWED_FILE: its old name
    REVIEWED_FILE,
    LocalRepositories,
    LocalSettings,
    local_backends,
)
from latet.state.protocols import HistoryStore, IdentityRef, KeyVaultStore, SettingsStore, StateBackends, VaultStore
from latet.storage.exchanges import valid_project_id
from latet.storage.keys import load_bridge_token, load_root_key, new_bridge_token
from latet.vision.masking import ImageMasking, OcrUnreadReason
from latet.vision.policy import image_policy

if TYPE_CHECKING:
    from latet.engine.detectors.base import Detector
    from latet.vision.pool import ServerReading  # NumPy: the ocr extra

log = logging.getLogger(__name__)
# Settings that raised before they were all in place, and those that ran before raising too as they were put back
# (``Runtime._put_back``): what runs is a mix of both, and no request is sent until settings are applied whole. Fixed
# words, said once as it happens; and once when settings are applied whole again.
_STRANDED = (
    "the settings could not be applied, nor those that ran before put back: no request is sent until settings are"
    " applied whole"
)
_UNSTRANDED = "settings are applied whole again: requests are sent again"
# What ``Runtime.apply_config`` changes on the mediator and the gateway, each kept as it was before its first change
# (``_InForce``) and put back so if the apply raises before its settings are whole.
_MEDIATOR_SETTINGS = (
    "images", "ocr", "vision", "dump_guard", "review", "live_review", "llm_review", "on_review", "unknown_members",
)  # fmt: skip
_GATEWAY_SETTINGS = ("personal_names", "destinations", "request_resources", "request_exceptions")


@dataclass(frozen=True, slots=True)
class _InForce:
    """What ``Runtime.apply_config`` changes, as it ran before its first change: the very objects, put back as they
    were (``Runtime._put_back``), never built again from the settings that ran, so that a failure that persists (image
    models unreadable, the reviewer's key) fails no put-back (minors after 0.11, item 23's second fix round, R1)."""

    config: Config
    built: Built
    projects: tuple[Config, IdentityConfig | None] | None
    masker: Any
    rules_tag: str
    verifier: Any
    mediator: tuple[tuple[str, Any], ...]
    reviewer: Reviewer | None
    reviewer_config: LlmReview | None
    history: Any
    gateway: tuple[tuple[str, Any], ...]
    routes: tuple[Upstream, ...] | None
    stranded: bool
    # Latet Enterprise's: the settings raised to the company's floor, that floor, and the name the company's identity
    # provider gives and the one the engine was built to mask under (``Runtime.effective`` and the fields beside it),
    # which the dashboard reads (``in_use``, the request exceptions under ``floor_used``).
    effective: Config | None
    floor_used: Floor | None
    provided_name: str | None
    named_by_provider: str | None


_REPROPOSAL_FAILED = Paced()
_UNREAD_SUMMARY = Paced()  # exchanges whose summary an event could not be made from, sent uncounted
_UNWEIGHED_PROJECT = Paced()  # project names that could not be read for the space's terms: hidden
_UNNAMED_PROJECT = Paced()  # hidden projects the edition could not name: sent as HIDDEN_PROJECT
_UNDROPPED = Paced()  # texts the company's record could not forget, recorded and never kept (E5): said once a minute
_UNTOLD_LATER = Paced()  # ends of texts the company's record was not told of (E5): said once a minute
# The detectors of the terms a space masks: yours, the company's locked terms and the terms it syncs (Latet Enterprise).
# A project whose name holds one of them goes to the events hidden, never by its name (L3 of the final security
# review): the request's body was masked, its project's name reached the reports and the SIEM in clear.
_TERM_DETECTORS: Final = frozenset({"terms", "locked-terms", "synced-terms"})


def holds_a_term(name: str, maskers: Iterable[Any]) -> bool:
    """Whether ``name`` holds a term one of ``maskers`` masks (its term detectors alone: ``_TERM_DETECTORS``), as the
    masking finds one in a text."""
    context = DetectContext()
    for masker in maskers:
        for detector in getattr(masker, "detectors", ()):
            if getattr(detector, "id", None) in _TERM_DETECTORS and next(iter(detector.detect(name, context)), None):
                return True
    return False


def _untold_later(error: BaseException) -> None:
    """The company's record of approvals was not told how a text ended (``Runtime._later``): said by its type, once a
    minute (its words could name the record's database)."""
    if count := _UNTOLD_LATER.due():
        log.warning(
            "the company's record of approvals was not told (%s)%s",
            type(error).__name__,
            f" ({count} times since the line before)" if count > 1 else "",
        )


def digest(config: Config) -> str:
    return hashlib.sha256(json.dumps(config.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


def rules_digest(config: Config) -> str:
    """What names the rules an engine is built from (``Engine.rules_tag``, which names the texts a conversation sent):
    the settings in use as ``digest`` names them, with the terms a company locks (Latet Enterprise), which no dump holds
    (``Config.locked_terms``), and whether every span of yours left in clear is cut around what is masked without them
    (``Config.carves_terms``).
    ``digest`` itself without either; etags and approvals name your own settings by ``digest``."""
    if not config.locked_terms and not config.carves_terms:
        return digest(config)
    locked = [term.model_dump(mode="json") for term in config.locked_terms]
    named: dict[str, object] = {"settings": config.model_dump(mode="json"), "locked_terms": locked}
    if config.carves_terms:
        named["carves_terms"] = True
    return hashlib.sha256(json.dumps(named, sort_keys=True).encode()).hexdigest()


def named_in(config: Config, name: str | None) -> str | None:
    """``name``, the one the company's identity provider gives a person linked to it (Latet Enterprise:
    ``Edition.provider_name``), where ``config`` (the settings in use, raised to the floor) leaves names (PERSON) in
    clear everywhere: in its own scope and in each of its folders, as their maskers are built (``projects``). None
    where it masks them anywhere: a full name is an identity value of type PERSON alone, so there the person's own name
    (their git author, as their computers send it) stays in use, and masked; the provider's in its place would leave
    theirs undetected, in clear."""
    if name is None or "PERSON" not in lets_through(config):
        return None
    if any("PERSON" not in _left_in_clear(config, None, entry, False) for entry in config.projects):
        return None
    return name


def provider_named(config: Config, name: str | None) -> Config:
    """``config`` masked under ``name`` in place of the person's own (``IdentityConfig.full_name``): the name the
    company's identity provider gives a person linked to it where names are left in clear (``named_in``), laid over
    the settings in use once raised to the floor, never over the person's own settings (``below`` would take it for a
    lowering of theirs); ``config`` itself for None. A copy keeps the company's terms and the carving
    (``Config.locked_terms``, ``Config.carves_terms``)."""
    if name is None:
        return config
    return config.model_copy(update={"identity": config.identity.model_copy(update={"full_name": name})})


class SpaceFull(MaskingError):
    """An identity's space on a Latet server weighs its share of the instance's memory, or more: it learns no new value,
    and a request that brings one is stopped, never sent unmasked. Its words name no value."""


class RequestAudit(Protocol):
    @property
    def retained_bytes(self) -> int: ...


class NewValues:
    """What a new value of the vault goes through to be kept (its ``on_insert``): the store, unless the space learns
    none now; and what a value another instance kept goes through to be learnt (its ``on_learn``). On a Latet server
    each value is weighed as it is kept or learnt, its length too, against its space's share of the instance's memory
    (``ceiling``, ``server.spaces``), so that no masking call, however long its text, and no value heard from another
    instance carries the space past it: the call stops at the value that would, and a value not learnt leaves the vault
    refusing every text (``Vault.lacking``) until the space is made again. Its projects' maskers are weighed alike
    before they are built (``Projects.past_share``). On a computer nothing is weighed: it learns."""

    def __init__(self, store: VaultStore) -> None:
        self.store = store
        # On a server: the words of why the space, holding this value too, would weigh past its share; None when it
        # fits. Asked of each value as it is kept, under the vault's lock: values kept at once are weighed one by one.
        self.ceiling: Callable[[Entity], str | None] | None = None

    def keep(self, entity: Entity) -> Entity | None:
        if self.ceiling is not None and (words := self.ceiling(entity)) is not None:
            raise SpaceFull(words)
        return self.store.save_entity(entity)

    def learn(self, entity: Entity) -> None:
        if self.ceiling is not None and (words := self.ceiling(entity)) is not None:
            raise SpaceFull(words)


def _nothing() -> None:
    return None


@dataclass
class Runtime:
    config: Config
    built: Built
    store: VaultStore
    gateway: GatewayConfig
    data: Path
    exchanges: HistoryStore | None = None
    recorder: Recorder | None = None
    config_file: Path | None = None
    projects: Projects | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)  # one change to the settings at a time
    # Whether the settings that ran before could not be put back was said (``_put_back``): said once, until settings
    # are applied whole again.
    _stranded_said: bool = field(default=False, repr=False)
    # The destinations the agents' wrappers registered since the daemon started (a session of theirs may still use
    # them, and no file of the agent says so): they stay in use until it restarts.
    wrapped: set[str] = field(default_factory=set)
    # The client a new local reviewer asks with, for its settings (None: its server's, as the settings say). The
    # tutorial's sandbox gives the Tutorial Reviewer's, reached in-process.
    review_client: Callable[[LlmReview], ReviewClient] | None = None
    # An omitted reader is safe even for a runtime constructed directly for a server space.
    reviewer_key: KeyReader = field(default=fixed_key(None), repr=False)
    # A computer's: the repositories remembered as its start read them, once (``_started``), which a repository held
    # back since the start is weighed in (``_held_back``), never read again on a conversation's thread. None: none read.
    start_scan: RepositoryScan | None = field(default=None, repr=False)
    # Where it keeps what it keeps (None: this computer's files, made by make_runtime since E0a), and whose space it is
    # on a Latet server (None on a computer).
    backends: StateBackends | None = None
    identity: IdentityRef | None = None
    # What a new value of its vault goes through to be kept: on a server, refused where it would carry its space past
    # its share of the instance's memory (``NewValues.ceiling``). None: its vault keeps through its store alone.
    new_values: NewValues | None = None
    # On a server, the bounds of the text recognitions its spaces share (its settings'): an image masking made later,
    # when the models are installed after the space was built, reads with them too. None on a computer.
    reading: ServerReading | None = None
    ocr_unread: Callable[[OcrUnreadReason], None] | None = field(default=None, repr=False)
    # The edition (latet.edition): the Community Edition's hooks do nothing, the Enterprise Edition's act as far as its
    # license goes. Given on a server (the server's one, for every space) and in the tutorial's sandbox; else this
    # computer's, made by make_runtime, which the runtime then closes with itself (owns_edition).
    edition: Edition = field(default_factory=Community)
    owns_edition: bool = False
    # Stops watching the edition's floor (make_runtime starts it): called when the runtime closes.
    _unwatch: Callable[[], None] = field(default=_nothing, repr=False)
    # Your settings raised to the company's floor (Latet Enterprise): what the engine is built from, never saved nor
    # approved. The settings themselves when there is no floor. And the floor they were raised to (None: none), which a
    # start compares with the edition's once it watches it: one that changed meanwhile is applied then.
    effective: Config | None = field(default=None, repr=False)
    floor_used: Floor | None = field(default=None, repr=False)
    # The name the company's identity provider gives the person (Latet Enterprise: ``Edition.provider_name``, read with
    # the floor at each build), whether or not it is in use; and the name the engine in use was built to mask them under
    # in place of their own: that one where the settings in use leave names in clear everywhere (``named_in``), else
    # None (their own, masked where names are). The Rules page shows the name in use; a change of their own name is
    # refused on the edition's answer of the moment (``_refused``), whichever name is in use.
    provided_name: str | None = field(default=None, repr=False)
    named_by_provider: str | None = field(default=None, repr=False)
    # What a hook handed to the work threads (``_later``): held until done, so that none is collected half-way.
    _later_tasks: set[asyncio.Task[None]] = field(default_factory=set, repr=False)
    # The changes being opened, under the lock, as another asked again (``propose``'s ``route_of``): they keep the
    # route of the one they replace, which ``_route_change`` does not weigh anew.
    _carried: set[str] = field(default_factory=set, repr=False)
    request_audit: RequestAudit | None = field(default=None, repr=False)
    retained_ceiling: Callable[[int], str | None] | None = field(default=None, repr=False)
    launch_keys: LocalLaunchKeys | None = field(default=None, repr=False)
    # The clock a request exception's approval is stamped by (D3): this computer's; on a server PostgreSQL's, so that no
    # instance's clock decides how long an exception lasts.
    exception_clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)
    # Whether the edition allows permanent request exceptions (``Edition.permanent_exceptions``): the Community Edition
    # does; a server's space asks its server's edition.
    permanent_allowed: Callable[[], bool] = field(default=lambda: True, repr=False)
    # On a server, the administration log an approved permanent exception is written to (R-108): rule id and scope
    # category, never its description. None on a computer. (The index `latet server status` lists from is written with
    # the approved settings themselves: ``PgSettings.index``.)
    exception_log: Callable[[Mapping[str, Any]], None] | None = field(default=None, repr=False)
    # On a server, the administration log an approval that moves `unknown_members` from `refuse` to `check` is written
    # to (design §3): the setting, its value, its scope's category and a count of projects, never a folder. None on a
    # computer.
    setting_log: Callable[[Mapping[str, Any]], None] | None = field(default=None, repr=False)
    # The edition's detectors in every masker of the runtime (the terms a company syncs), its projects' and the
    # Playground's too: none in the Community Edition.
    extra: tuple[Detector, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if self.request_audit is None:
            self.request_audit = AuditOwner()  # counted before publication/first Spaces weighing
        # Borrow before publication.
        self.gateway.request_audit = self.request_audit if type(self.request_audit) is AuditOwner else None
        self.gateway.launch_keys = self.launch_keys if self.identity is None else None
        if self.launch_keys is not None:
            self.launch_keys.ceiling = lambda added: self.retained_ceiling(added) if self.retained_ceiling else None
        # The gateway asks the clock the runtime has now (a server's space sets PostgreSQL's after it is made).
        self.gateway.exception_clock = lambda: self.exception_clock()
        self.mediator.echoes.ceiling = lambda added: self.retained_ceiling(added) if self.retained_ceiling else None
        # The changes to your settings are opened and settled here, in worker threads: the dashboards hear of
        # them on the event loop.
        approvals = self.mediator.approvals
        approvals.publish_change = lambda summary, event: self._on_loop(approvals.broadcaster.publish, summary, event)
        self.wire_events()
        # What starts to wait is routed here (you, or someone else with Latet Enterprise), and how it ends is kept.
        approvals.route_change, approvals.route_batch = self._route_change, self._route_batch
        approvals.dropped_change, approvals.dropped_batch = self._dropped_change, self._dropped_batch
        approvals.on_change_settled, approvals.on_batch_settled = self._change_settled, self._batch_settled
        approvals.on_batch_withheld = self._batch_withheld

    def __repr__(self) -> str:
        # Whose it is, and nothing of what it holds: its settings hold every term, and the names and emails its
        # computers sent; its routes the addresses of its destinations.
        return f"Runtime({self.identity.id if self.identity is not None else 'this computer'})"

    @property
    def request_retained_bytes(self) -> int:
        return (
            (self.launch_keys.retained_bytes if self.launch_keys is not None else 0)
            + self.mediator.opaque_provenance_bytes
            + (self.request_audit.retained_bytes if self.request_audit is not None else 0)
        )

    @property
    def settings(self) -> SettingsStore:
        """The settings' store: config.yaml and config.reviewed.json on a computer."""
        if self.backends is not None:
            return self.backends.settings
        return LocalSettings(self.config_file, self.data)

    @property
    def mediator(self) -> Mediator:
        assert self.gateway.mediator is not None
        return self.gateway.mediator

    def _on_loop(self, publish: Callable[..., None], *args: Any) -> None:
        """``publish(*args)`` on the event loop, the only place the dashboards' live feeds (a ``Broadcaster``)
        work: handed over to it from a worker thread. At once when no loop runs yet (at start)."""
        loop = self.mediator.loop
        if loop is None:
            publish(*args)
            return
        with contextlib.suppress(RuntimeError):  # the loop closed meanwhile (shutdown)
            loop.call_soon_threadsafe(publish, *args)

    def wire_events(self) -> None:
        """What happens here reaches the edition as events: each exchange, whatever the history keeps. The edition and
        the identity are read at each event, so those given after the runtime was made are the ones used."""
        if self.recorder is not None:
            self.recorder.on_summary = self._exchanged
            self.recorder.wanted = lambda: self.edition.keeps_events()

    def _exchanged(self, summary: dict[str, Any]) -> None:
        """An exchange's summary (the recorder's), as the edition's event. A summary its event cannot be made from (a
        type of value an event cannot name, a count that is none) is the exchange's event all the same, uncounted, said
        in the log by the error's type: one event an exchange, always. What the edition raises is the recorder's to
        say."""
        who = self._who()
        summary = self._project_shown(summary)
        try:
            event = exchange_event(summary, *who)
        except (KeyError, TypeError, ValueError) as exc:  # by its type: its words could quote what it was given
            if count := _UNREAD_SUMMARY.due():
                many = f" ({count} since the line before)" if count > 1 else ""
                log.error("an exchange's event could not be made: it is sent uncounted (%s)%s", described(exc), many)
            event = exchange_event(summary | {"new_by_type": {}, "uncounted": True}, *who)
        self.edition.record(event)

    def _project_shown(self, summary: dict[str, Any]) -> dict[str, Any]:
        """``summary`` with its project named as its event may name it: hidden when its name, as an event would carry
        it, holds a term this space masks, in its rules or in its project's (L3 of the final security review); as it is
        otherwise. A name that cannot be weighed is hidden too, said by the error's type once a minute: never in clear
        for a failure."""
        project = summary.get("project")
        name = name_of(project.get("name")) if isinstance(project, Mapping) else None
        if name is None or not isinstance(project, Mapping):
            return summary
        try:
            held = holds_a_term(name, self._maskers_of(project.get("id")))
        except Exception as exc:  # by its type: its words could quote the name
            if count := _UNWEIGHED_PROJECT.due():
                many = f" ({count} since the line before)" if count > 1 else ""
                log.error("a project's name could not be weighed for the terms: it is hidden (%s)%s",
                          described(exc), many)  # fmt: skip
            held = True
        return summary | {"project": {"name": self._hidden(name)}} if held else summary

    def _hidden(self, name: str) -> str:
        """What the edition names a project hidden for a term, the same for every person and folder of that name (R3
        of the final security review's re-review: never its folder's path, nor a hash of it, which is one per person and
        per clone); ``HIDDEN_PROJECT`` for an answer of another shape (``HIDDEN_SHAPE``), and for an edition that
        raises, said by the error's type once a minute."""
        try:
            given = self.edition.hidden_project(name)
        except Exception as exc:  # by its type: its words could quote the name
            if count := _UNNAMED_PROJECT.due():
                many = f" ({count} since the line before)" if count > 1 else ""
                log.error("a hidden project could not be named by the edition: it goes as %s (%s)%s", HIDDEN_PROJECT,
                          described(exc), many)  # fmt: skip
            return HIDDEN_PROJECT
        return given if isinstance(given, str) and HIDDEN_SHAPE.fullmatch(given) else HIDDEN_PROJECT

    def _maskers_of(self, pid: object) -> list[Any]:
        """The space's masker and, when its project has rules of its own, the project's: the terms either masks."""
        maskers: list[Any] = [self.built.masker]
        peek = getattr(getattr(self.built.engine, "scopes", None), "peek", None)
        project = peek(pid) if isinstance(pid, str) and pid and peek is not None else None
        scope = getattr(project, "scope", None)
        if scope is not None:
            maskers.append(scope.masker)
        return maskers

    def _who(self) -> tuple[str | None, str | None]:
        """The identity this runtime is for, as an event names it (its id and kind): nobody on a computer of one's
        own."""
        identity = self.identity
        return (identity.id, identity.kind) if identity is not None else (None, None)

    # --- who decides on what waits (Latet Enterprise: E5) ---------------------------------------------------------

    def _route_change(self, change: Change) -> None:
        """Who decides on a change that opens in this space, in the worker thread that opens it, under the runtime's
        lock: the edition says. With Latet Enterprise on a server it may be the company's security team, or an API
        key's controllers, who are named on it, and the company's record takes it (raising, it does not open:
        protection never drops). A person's settings that arrive from the server (a change of source file or start)
        stay theirs when the server's shared settings alone lowered them [D1]. A change asked again (``propose``'s
        ``route_of``) keeps the route of the one it replaces: what waits keeps its route ([D4]), and neither [D1], which
        a re-ask that saved settings as approved first would fool (the review of Task 2, I1), nor the policy of now
        weighs it anew; the record takes it as the new item it is."""
        central = self.edition.central()
        if change.id in self._carried:
            if central is not None and change.decides != "self":
                central.opened(self.identity, change)
            return
        if central is None:
            return
        identity = self.identity
        arrived = identity is not None and identity.kind == "person" and change.source in ("file", "start")
        # [D1], R30, R58: settings that arrive lower stay the person's to decide only when the server's shared settings
        # alone lowered them: their own part kept lowers nothing against the own part they approved, both over the
        # shared settings of now. Never by the source alone: a re-application offers whatever PostgreSQL holds as their
        # own part (the plan review's C1). Weighed as the change holds it, never as read again now: another instance may
        # have written it since (the whole-branch security review's L1).
        if arrived and not own_lowers(self.settings, change.config):
            return
        route = self.edition.changes_go_to(identity)
        if route != "security" and change.exceptions and company_decides(self.floor()):
            # The company decides on request exceptions (Latet Enterprise's `exceptions: company`): a change that adds,
            # widens, renews or makes one permanent goes to its security team, whoever decides on the rest.
            route = "security"
        if route == "self":
            return
        change.decides = route
        change.deciders = central.deciders(identity, route, change.asked_by)
        central.opened(identity, change)

    async def _route_batch(self, batch: Batch) -> None:
        """Who decides on a text of the Live Reviewer before anyone sees it: on a server the edition reads its settings
        and the record is written in the work threads, never on the event loop (raising, the text is not sent)."""
        central = self.edition.central()
        if central is None:
            return

        def routed() -> None:
            route = self.edition.live_review_goes_to(self.identity)
            if route == "self":
                return
            batch.decides = route
            batch.deciders = central.deciders(self.identity, route)
            central.opened(self.identity, batch)

        await self._off_loop(routed)

    async def _off_loop(self, job: Callable[[], object]) -> None:
        """``job`` in the server's work threads (``GatewayConfig.work``), else a worker thread: never the event loop."""
        work = self.gateway.work
        if work is not None:
            await work(job)
        else:
            await asyncio.to_thread(job)

    def _later(self, job: Callable[[], object]) -> None:
        """``job`` handed to the work threads and never waited for: from the event loop, or from a server's queue
        thread (``settle_all``). Its failure is said by its type, once a minute, never raised."""
        loop = self.mediator.loop
        if loop is None:  # no loop yet (a start): here
            try:
                job()
            except Exception as exc:  # never in the way of the decision, nor of its event (the review of Task 2, m1)
                _untold_later(exc)
            return

        def start() -> None:
            task = loop.create_task(self._off_loop(job))
            self._later_tasks.add(task)
            task.add_done_callback(self._ended_later)

        with contextlib.suppress(RuntimeError):  # the loop closed meanwhile (shutdown)
            loop.call_soon_threadsafe(start)

    def _ended_later(self, task: asyncio.Task[None]) -> None:
        self._later_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            _untold_later(error)

    def _dropped_change(self, change_id: str) -> None:
        if (central := self.edition.central()) is not None:
            central.dropped(self.identity, change_id)

    async def _dropped_batch(self, batch_id: str) -> None:
        """A text recorded for someone else that never waited (the queue refused it): the record forgets it, in the
        work threads. Never raised (Task 1's review, C2): why the text was not kept is what its request is told, and
        this failure is said by its type, once a minute."""
        if (central := self.edition.central()) is None:
            return
        try:
            await self._off_loop(lambda: central.dropped(self.identity, batch_id))
        except Exception as exc:  # by its type: its words could name the record's database
            if count := _UNDROPPED.due():
                log.warning(
                    "a text that was never kept is still in the company's record of approvals (%s)%s",
                    type(exc).__name__,
                    f" ({count} times since the line before)" if count > 1 else "",
                )

    def _whose(self) -> dict[str, Any]:
        identity = self.identity
        return {"identity": identity.id, "kind": identity.kind} if identity is not None else {}

    def _change_settled(
        self, change_id: str, change: Change | None, state: str, by: IdentityRef | None, stopped_by: str | None = None
    ) -> None:
        """How a change ended (in a worker thread): an event for the company's reports (kept by Latet Enterprise,
        nothing on the Community Edition) and, when someone else was to decide on it, the company's record. Never in
        the way of the decision, which stands (``Approvals``' ``_quietly`` says a failure), and a record that cannot be
        told never keeps the event from going out (R17; the review of Task 2, m1). ``stopped_by``: in an API key's
        space, who stopped it there without asking for it, named in both as who ended it (the whole-branch review's
        m1)."""
        central = self.edition.central()
        try:
            if central is not None and (change is None or change.decides != "self"):
                # First: what waits for the deciders.
                central.ended(self.identity, change_id, "change", state, by, stopped_by)
        finally:
            who = by.id if by is not None else stopped_by
            fields: dict[str, Any] = {"outcome": state, "by": who, **self._whose()}
            if change is not None:
                fields |= {"agent": code_of(change.agent), "reason": change.source}
            self.edition.record(make_event("change", clock=time.time, **fields))

    def _batch_settled(self, batch_id: str, decision: Decision, decides: Decides, agent: str | None) -> None:
        """How a text ended (on the event loop, or a server's queue thread): its event at once (E4's pipeline takes it
        without waiting), the company's record in the work threads when someone else decided on it. Who stopped it in
        an API key's space (``Decision.stopped_by``) is named in both as who ended it (the whole-branch review's m1)."""
        by, stopped_by, state = decision.by, decision.stopped_by, decision.state
        fields = {
            "outcome": state, "stopped": 0 if decision.approved else 1, "agent": code_of(agent),
            "by": by.id if by is not None else stopped_by, **self._whose(),
        }  # fmt: skip
        central = self.edition.central()
        if central is not None and decides != "self":
            self._later(lambda: central.ended(self.identity, batch_id, "batch", state, by, stopped_by))
        self.edition.record(make_event("approval", clock=time.time, **fields))

    def _batch_withheld(self, batch_id: str, decides: Decides) -> None:
        """A text a decider of the company approved, withheld where it was sent (masked then, it was not what they
        approved): the company's record says so, in the work threads, never waited for (the review of Task 10, fix
        round 2). Its event said ``approved`` already: the decision taken."""
        central = self.edition.central()
        if central is not None and decides != "self":
            self._later(lambda: central.withheld(self.identity, batch_id))

    def _decided_elsewhere(self) -> bool:
        """Whether someone other than whoever saves decides on what lowers protection here (Latet Enterprise)."""
        return self.edition.central() is not None and self.edition.changes_go_to(self.identity) != "self"

    # --- the company's floor (Latet Enterprise) ---------------------------------------------------------------------

    def in_use(self) -> Config:
        """The settings that protect: yours, raised to the company's floor when there is one."""
        return self.effective if self.effective is not None else self.config

    def unknown_members(self, project: ProjectSettings | None = None) -> UnknownMembers:
        """How a request member no request profile names is read here (``protection.effective_unknown_members``): as
        the settings that protect say (``in_use``: a company's floor forces `refuse` there) or ``project``'s own
        settings do (a repository's latet.yaml, your settings for the folder), never as a request says."""
        return effective_unknown_members(self.in_use(), project)

    def floor(self) -> Floor | None:
        """The company's floor for these settings (Latet Enterprise), or None. On a server, the same for every space;
        it raises PolicyUnavailable where a policy is stored that this instance cannot read (ruling 12)."""
        return self.edition.floor(self.identity)

    def raised(self, config: Config) -> Config:
        """``config`` raised to the company's floor, as an engine is built from it (``floor.raised_to``), and masked
        under the name from the company's identity provider read at the last build where it leaves names in clear
        (``named_in``)."""
        raised = raised_to(self.floor(), config)
        return provider_named(raised, named_in(raised, self.provided_name))

    def _below(self, reasons: list[str]) -> BelowFloor:
        """The refusal of a change below the floor, told to the company's events (no value in it)."""
        who = self.identity
        self.edition.record(
            make_event(
                "policy",
                clock=time.time,
                reason="below-floor",
                outcome="refused",
                identity=who.id if who is not None else None,
                kind=who.kind if who is not None else None,
            )
        )
        return BelowFloor(reasons)

    def check_floor(self, config: Config, before: Config | None = None, *, fills_name: bool = False) -> None:
        """``BelowFloor`` when ``config`` goes further below the company's floor than ``before`` (the settings now), or
        changes the person's own name while the company's identity provider names them: refused, never held for
        approval, since the person cannot approve it. ``fills_name``: a computer's identity merged in, whose name fills
        an empty one only (``_refused``)."""
        if reasons := self._refused(before if before is not None else self.config, config, fills_name=fills_name):
            raise self._below(reasons)

    def _refused(self, before: Config, after: Config, *, fills_name: bool = False) -> list[str]:
        """Why the change from ``before`` to ``after`` is refused: what takes it further below the company's floor
        (``fresh_reasons``), and any change of the person's own name while the company's identity provider names them
        (``PROVIDER_NAME``): the edition answers a name for them now (read again here, never a copy;
        ``FloorUnavailable`` when it cannot be said), whether or not their settings let names through (where they do
        not, in a folder or everywhere, their own name is in use and masked, and stays as it was). The one exemption,
        ``fills_name``: a computer's name filling an empty one (as a computer fills it in the Community Edition, never
        replacing a name), so that the person's real name is masked wherever the provider's is not in use."""
        floor = self.floor()
        reasons = fresh_reasons(floor, before, after)
        name, was = after.identity.full_name, before.identity.full_name
        filled = fills_name and not was
        if name != was and not filled and self.edition.provider_name(self.identity) is not None:
            reasons = [*reasons, PROVIDER_NAME]
        return reasons

    def apply_config(self, config: Config, save: bool = True, *, as_is: bool = False) -> int:
        """Switch to new rules at once; sessions and the vault are kept. Returns the rules version. ``as_is``: settings
        already made as they apply (on a server, those its spaces compose: laid over the shared settings again, an item
        the shared settings add would come back where the composition left it out).

        On a server, settings their store refuses to keep (another instance wrote them since, they weigh too much, the
        store does not answer: an OSError; or what they hold, a SettingsRefused) never run: what ran before runs again,
        as it ran, before the refusal is raised (the store has the space read what it holds when another instance's
        write refused them). A computer runs them, as before, keeping the history's retention as it was.

        Settings that raise before they are all in place (their image models cannot be read, the reviewer's
        certificate, a route), on either machine, never run half applied: what ran before runs again, the very
        objects that ran, before the error is raised (``_put_back``); when that raises too, no request is sent until
        settings are applied whole (``Engine.stranded``).

        ``config`` is yours: the engine is built on it raised to your company's floor (Latet Enterprise), and masked
        under the name your company's identity provider gives where it gives one and names are left in clear
        (``named_by_provider``), kept in ``effective``, raised last before the build (a store's ``prepare`` reads the
        settings again: the company's terms would not survive it); what is saved and approved, the history, its
        retention and the destinations are ``config`` as you set it."""
        with self._lock:
            if not as_is:
                config = self.settings.prepare(config)
            # Raised before anything changes: a floor that cannot be read (or settings that lost the company's terms)
            # leaves the settings in use as they are, and so does a name from the identity provider that cannot be.
            floor = self.floor()
            provided = self.edition.provider_name(self.identity)
            raised = raised_to(floor, config)
            named = named_in(raised, provided)  # judged on the settings applied, never those that ran before
            effective = provider_named(raised, named)
            # Read before anything changes: a store that does not answer leaves the settings in use as they are.
            personal = personal_names(self.settings, config)
            identity = resolve_identity(effective, detect=self.identity is None)
            # The rules' masker alone: the engine, its conversations and the final check stay the ones that run.
            masker, recognizers = build_masker(effective, self.built.vault, identity, extra=self.extra)
            engine = self.built.engine
            was = self._in_force()  # what runs, as it is before the first change: put back so if these raise midway
            # From its first change of state to its last move, no request's authority is valid (``Engine.applying``):
            # a request taken or sent meanwhile never leaves with these settings half applied (minors after 0.11, item
            # 23). Reading the settings, raising them to the floor and building the masker change nothing, and stay
            # outside it.
            engine.applying += 1
            engine.applies += 1  # one more begun: an application's call answered across it is not given
            whole = False
            try:
                if self.projects is not None:
                    self.projects.use(effective, identity)
                engine.use(masker)
                engine.rules_tag = rules_digest(effective)
                self.mediator.verifier.use(masker)
                self.built = Built(self.built.vault, masker, engine, self.mediator.verifier, recognizers)
                self.mediator.images = image_policy(effective)
                self.mediator.ocr = effective.ocr == "auto"
                # A move to `refuse` reads again every body checked while new members were read (its content epoch).
                self.mediator.unknown_members = effective_unknown_members(effective, None)
                installed = self.mediator.vision or image_masking(
                    self.data,
                    self.store,
                    space=self.identity is not None,
                    reading=self.reading,
                    ocr_unread=self.ocr_unread,
                )
                self.mediator.vision = installed  # the models may have been installed since
                self.mediator.dump_guard = effective.dump_guard
                self.mediator.review = review_policy(effective)
                self.mediator.live_review = effective.live_review
                self.use_reviewer(effective)
                if self.recorder is not None:
                    self.recorder.history = config.history
                # Weighed against the settings running; the history takes the new retention once they are kept.
                shorter = self.exchanges is not None and config.retention_days < self.config.retention_days
                if self.gateway.fixed_routes is None:  # the tutorial's sandbox routes to its pretend model only
                    # A destination of the identity's own is judged at connect from the moment its route exists: its
                    # route says so (``Upstream.own``), and the names of both settings are its own until the routes are
                    # swapped.
                    self.gateway.personal_names = self.gateway.personal_names | personal
                    self.gateway.destinations = routes_of(config, personal)
                    self.gateway.request_resources = tuple(
                        (row.destination, row.url, row.role, row.value) for row in config.request_resources
                    )
                    self.gateway.request_exceptions = approved_exceptions(
                        config, self.permanent_allowed(), company=company_decides(floor)
                    )
                    if self.gateway.routes is not None:
                        self.gateway.routes.use(
                            [*self.gateway.upstreams, *self.gateway.destinations],
                            self.gateway.request_resources,
                            self.gateway.request_exceptions,
                        )
                self.gateway.personal_names = personal
                self.config = config
                self.effective, self.floor_used = effective, floor
                self.provided_name, self.named_by_provider = provided, named
                whole = True
                if engine.stranded:  # settings whole again: the mark held while what ran before was put back goes
                    engine.stranded = False
                    engine.applying -= 1
                if save:
                    try:
                        self.settings.save(config)
                    except (OSError, ValueError):  # not kept: the store failed, or refused what they hold
                        if self.identity is not None:  # a server's: the refused settings never run
                            self._put_back(was)
                        raise
                if self.exchanges is not None:  # kept: a save that fails leaves the history's retention as it was
                    self.exchanges.retention_days = config.retention_days
                if self.projects is not None:
                    # Settings weighed as they applied (against those you approved and the repositories seen), and
                    # kept as approved: the ones from before the start are weighed against no longer.
                    self.projects.before = None
                self._reviewed(config)
                self.warm_up()
                if shorter and self.recorder is not None:
                    # Last, once the settings are kept (the save can fail): what is older than the new retention goes.
                    self.recorder.prune_now()
                # The Live Reviewer's texts still waiting shown masked with the rules of now, as they will leave; the
                # content epoch renewed with them.
                self.mediator.live_changed()
            except BaseException:
                if not whole:  # raised midway: what ran before runs again, as it ran (fix rounds I1 and R1)
                    self._put_back(was)
                raise
            finally:
                # Every part of these settings in place (the masker, the image, dump guard and review settings, the
                # route table, the content epoch), or an apply that raised (a save refused, or midway: what ran before
                # put back, or a mark held as it could not be): the rules version moves once more, so that a request's
                # authority taken while they were applied is never valid; its request is prepared again under the
                # settings in force, whole, or refused with the settings-changed words (fix round of the minors after
                # 0.11, S-M1; after ``live_changed`` since its re-review, so that a request prepared before the content
                # epoch was renewed is never refused with the coverage words; on any exit since item 23). Then this
                # apply's mark is lowered, after the move: a check that finds it lowered finds the move too
                # (``LocalAuthority.valid`` reads the mark first).
                engine.rules_changed()
                engine.applying -= 1
                # Said over once the apply is past its save, never as its settings are whole: a server's store that
                # then refuses them has the mix that ran before put back, held again and not said again (the
                # whole-branch review of 0.12, Minor 2). A computer runs settings it could not write: said over too.
                if self._stranded_said and not engine.stranded:
                    self._stranded_said = False
                    log.warning(_UNSTRANDED)
            return engine.rules_version

    def _in_force(self) -> _InForce:
        """What runs, as it is: read before an apply's first change, and nothing of it changed by the reading."""
        mediator, engine, gateway = self.mediator, self.built.engine, self.gateway
        reviewer = mediator.reviewer
        return _InForce(
            config=self.config,
            built=self.built,
            projects=self.projects.in_use() if self.projects is not None else None,
            masker=engine.masker,
            rules_tag=engine.rules_tag,
            verifier=mediator.verifier.masker,
            mediator=tuple((name, getattr(mediator, name)) for name in _MEDIATOR_SETTINGS),
            reviewer=reviewer,
            reviewer_config=reviewer.config if reviewer is not None else None,
            history=self.recorder.history if self.recorder is not None else None,
            gateway=tuple((name, getattr(gateway, name)) for name in _GATEWAY_SETTINGS),
            routes=gateway.routes.table if gateway.routes is not None else None,
            stranded=engine.stranded,
            effective=self.effective,
            floor_used=self.floor_used,
            provided_name=self.provided_name,
            named_by_provider=self.named_by_provider,
        )

    def _restore(self, was: _InForce) -> None:
        """``was`` in use again, as it ran: each object put back, nothing built nor read again (the image masking, the
        reviewer and its key, the masker, the route table). A reviewer this apply made is retired, the one put back
        taken out of the retired ones."""
        mediator, engine, gateway = self.mediator, was.built.engine, self.gateway
        if self.projects is not None and was.projects is not None:
            self.projects.use(*was.projects)
        engine.masker = was.masker
        engine.rules_tag = was.rules_tag
        mediator.verifier.use(was.verifier)  # what it found clean under the apply's masker is checked again
        self.built = was.built
        for name, value in was.mediator:
            setattr(mediator, name, value)
        made, reviewer = mediator.reviewer, was.reviewer
        mediator.reviewer = reviewer
        if reviewer is not None:
            if was.reviewer_config is not None:  # a reviewer of the same server took the apply's settings in place
                reviewer.config = was.reviewer_config
            if reviewer in mediator.retired:
                mediator.retired.remove(reviewer)
        if made is not None and made is not reviewer and made not in mediator.retired:
            mediator.retired.append(made)  # closed on the event loop, as a reviewer of earlier settings is
        if self.recorder is not None:
            self.recorder.history = was.history
        for name, value in was.gateway:
            setattr(gateway, name, value)
        if gateway.routes is not None and was.routes is not None:
            gateway.routes.put(was.routes)
        self.config = was.config
        self.effective, self.floor_used = was.effective, was.floor_used
        self.provided_name, self.named_by_provider = was.provided_name, was.named_by_provider

    def _put_back(self, was: _InForce) -> None:
        """What ran before (``was``) put back as it ran (under the lock): after a save a server's store refused, or
        settings that raised before they were all in place. Meanwhile the apply's own mark is up, as for any apply: a
        request waits for it (``request_policy.settled``) and is answered under what ran before (the second fix round,
        R2). When putting it back raises (fix round I1), what runs is a mix of both, and a mark is held for it
        (``Engine.stranded``), past the apply's own: every request is refused because its settings changed, none
        prepared under the mix, until settings are applied whole; said once, in fixed words, as it happens. Its own
        failure is said in the log by its type, never raised over the first. What was put back while a mix ran is that
        mix again: held so too."""
        try:
            self._restore(was)
        except Exception as exc:
            log.warning("the settings that ran before could not be put back: %s", described(exc))
            self._strand()
            return
        if was.stranded:
            self._strand()

    def _strand(self) -> None:
        """A mark held for a mix of settings in force (``Engine.stranded``), past every apply's own, until an apply has
        its settings whole; said once, in fixed words."""
        engine = self.built.engine
        if not engine.stranded:
            engine.stranded = True
            engine.applying += 1
        if not self._stranded_said:
            self._stranded_said = True
            log.error(_STRANDED)

    def update_config(
        self,
        change: Callable[[Config], Config | None],
        *,
        save: bool = True,
        fills_name: bool = False,
        from_dashboard: bool = False,
        asked_by: str | None = None,
        approving: bool = False,
    ) -> Config | None:
        """Change the settings in use, read and applied under one lock: two changes made at once (a save in
        the dashboard, an agent's wrapper adding a destination) both stay, one on top of the other.
        ``change`` gets the settings as they are now and answers the new ones, or None (or the same ones) to
        leave them. The new settings; None when nothing changed. ``fills_name``: a computer's identity merged in,
        whose name fills an empty one only, never refused for it (``check_floor``).

        ``from_dashboard``: a save in the dashboard; when someone else decides on what lowers protection here (Latet
        Enterprise: the company's security team, or an API key's controllers), new settings that lower it wait for
        them instead (``Waits``), nothing applied. ``asked_by``: in an API key's space, the person who made the change
        (E3), who never approves it.

        On a server, a person's settings in use may be a composition, stricter than those kept while a change waits for
        her (``OwnSettings.composition``): a save of them (the dashboard's, a computer's destination or identity) is
        weighed against them, composed again with what she approved (``OwnSettings.prepare``) so that what waits is
        never applied nor approved by it, and the change is asked again over what is kept (``_asked_again``). Failure
        of that final refresh raises SettingsSavedChangePending: the save already succeeded and must not be replayed."""
        with self._lock:
            current = self.config
            new = change(current)
            if new is None or new is current:
                return None
            self.check_floor(
                new, current, fills_name=fills_name
            )  # below your company's floor: refused, nothing changes
            over = self._held() is not None
            new = self.settings.prepare(new, strict=True)  # on a server: refused when it drops what all share
            approved_before = current
            if approving and save and self.identity is not None and self.identity.kind == "key":
                # An API key's settings are the administration's: its save of them, on its settings route alone
                # (``approving``), approves the exceptions it adds (D3); the key's own calls (its computer's identity, a
                # destination its agent names) leave a pending one pending (R-2).
                approved_before = self.reviewed() or current
                new = self._stamped(approved_before, new)
            if over and new == current:  # nothing of what is in use changes: kept as it is, it would reject what waits
                return None
            if from_dashboard and self._decided_elsewhere():
                # Whoever saves does not approve what lowers protection: your company's security team does, or, in an
                # API key's space, another of the people who control the key. So the save waits for them (what adds
                # protection still applies at once). Below the floor it was refused above, never routed (E2's carry).
                waiting = self.propose(new, "dashboard", asked_by=asked_by)
                if waiting is not None and waiting.decides != "self":
                    raise Waits(waiting)
                if waiting is not None:
                    # R42 (the plan review's L1): an equal change opened while the person decided keeps its route and
                    # waits for them: said as the Community says it ("decide on the change first"), never as the
                    # security team's.
                    raise ChangeWaits()
            self.apply_config(new, save=save, as_is=over)  # composed again, it is as it applies
            if save and not approving:
                # Kept, it is approved: her own save that applies at once (weighed against the settings in use before,
                # never looser than those approved: nothing read again). The administration's save of an API key's
                # settings (``approving``) is its route's `settings.changed` alone, its token the actor, written once
                # the approval is kept.
                self._log_new_members(approved_before, new)
            if save and self.identity is not None and self.identity.kind == "key":
                # An API key's settings are the administration's or its controllers': its save approves them, each
                # exception it made permanent written to the administration log as a person's approval writes it
                # (R-108), once that approval is kept (one that is not is logged by nobody), and a change of its own
                # left waiting is over; one that waits for its other controllers or the security team stays, and is
                # found stale at its decision (R6).
                if approving:
                    self._log_permanent(approved_before, self.reviewed() or approved_before)
                approvals = self.mediator.approvals
                for waiting_change in approvals.waiting_changes():
                    if waiting_change.decides == "self":
                        approvals.settle_change(waiting_change.id, "superseded")
            elif save and over:
                try:
                    self._asked_again()
                except Exception as exc:
                    if count := _REPROPOSAL_FAILED.due():
                        log.warning(
                            "settings were kept but their pending change could not be refreshed (%s)%s",
                            described(exc),
                            f" ({count} times since the line before)" if count > 1 else "",
                        )
                    raise SettingsSavedChangePending() from None
            return new

    def floor_changed(self) -> None:
        """The edition's floor changed (a server's policy heard: E2): the settings in use are applied again, at once, as
        they apply (``as_is``: on a server a composition laid over the shared settings again would apply what waits for
        approval), which raises them to the new floor."""
        with self._lock:
            self.apply_config(self.config, save=False, as_is=True)

    def _held(self) -> tuple[Config, Config] | None:
        """The settings kept and those in use, when those are a composition over which a save is laid back on these: a
        person's on a server while a change waits for her (``OwnSettings.composition``); None otherwise."""
        settings = self.settings
        return settings.composition if isinstance(settings, OwnSettings) else None

    def _asked_again(self) -> None:
        """Under the lock, after a person's save made over a composition was applied and kept: the change waiting is
        asked again over the settings kept now, which hold what waits and the save's own changes, as
        ``add_destinations`` asks a change again with its destinations; settled as superseded when what is kept lowers
        nothing against what is approved now (the composition is over). A save in the dashboard that waits (for the
        company's security team, or an API key's controllers) is not in the settings kept, never written there (R29),
        so it is never asked again as their content (the re-review of Task 2, n1): it waits as it was made until a
        re-application offers the composition's own change, which supersedes it (at most one waits), as on a server the
        one this very save queues does (``PgSettings._laid_back``)."""
        waiting = next(iter(self.mediator.approvals.waiting_changes()), None)
        if waiting is None or waiting.source == "dashboard":
            return
        held = self._held()
        kept = held[0] if held is not None else self.config
        if held is not None and self.waits(kept, over_these=True):  # asked already, over the settings in use now
            return
        try:
            proposed = self.propose(kept, waiting.source, waiting.agent, asked_by=waiting.asked_by, route_of=waiting)
        except BelowFloor as refused:  # what waits goes below your company's floor now: it can only be rejected
            log.warning("a change waiting goes below your company's floor: it is rejected (%s)", type(refused).__name__)
            self.mediator.approvals.settle_change(waiting.id, "rejected")
            self.settings.save(self.config)  # the settings kept back to those in use, as a rejection leaves them
            return
        if proposed is None:
            self.mediator.approvals.settle_change(waiting.id, "superseded")

    # --- changes that lower protection ------------------------------------------------------------

    def _reviewed(self, config: Config) -> None:
        """Remember ``config`` as approved: what the daemon runs is, as it runs it."""
        try:
            self.settings.save_reviewed(config)
        except OSError as exc:
            log.warning("could not remember the settings in use: %s", described(exc))

    def reviewed(self) -> Config | None:
        """The last settings you approved, or None when there are none yet (or they cannot be read)."""
        return self.settings.reviewed()

    def waits(self, config: Config, *, over_these: bool = False) -> bool:
        """Whether a change with these very settings waits for you already (on a server another instance may have
        opened it): opened again, it would replace that one, and a command waiting on it would answer superseded.
        ``over_these``: and made over the settings in use now (its base), so that your approval applies it: one made
        over settings that changed since would be found stale (``decide_change``), and is to be asked again."""
        wanted, changes = digest(config), self.mediator.approvals.changes
        applied: str | None = None
        for change_id in list(changes):
            change = changes.get(change_id)  # settled meanwhile (on a server, by another instance): none
            if change is None or change.config is None or digest(change.config) != wanted:
                continue
            if not over_these:
                return True
            applied = applied or digest(self.config)
            if change.base == applied:
                return True
        return False

    def repositories_for(self, candidate: Config | None = None) -> RepositoryScan | None:
        """The repositories a change to ``candidate`` is weighed in (``Projects.repositories``), read now: outside every
        lock, never on the event loop, before ``propose`` takes the settings' lock. None on a server's space, which
        reads no repository. A candidate that could lower nothing in a repository (``weighs_repositories``) has none
        read: an empty snapshot marked incomplete, which ``propose`` refuses only for a change that could."""
        if self.projects is None or self.identity is not None:
            return None
        if candidate is not None and not weighs_repositories(self.reviewed() or self.config, candidate):
            return RepositoryScan(complete=False)
        return self.projects.repositories()

    def propose(
        self,
        config: Config,
        source: Literal["command", "file", "start", "dashboard"],
        agent: str | None = None,
        *,
        repositories: RepositoryScan | None = None,
        asked_by: str | None = None,
        route_of: Change | None = None,
    ) -> Change | None:
        """A change that lowers protection waits for you (the dashboards are told), or for whoever decides on it here
        (Latet Enterprise: ``_route_change``); None when it lowers nothing, and may apply. Weighed under the lock: a
        caller holding it applies it in the same step. ``asked_by``: in an API key's space, the person who made it.
        ``route_of``: the change waiting that this one asks again, whose route (who decides, named) it keeps.

        ``repositories``: the snapshot of the repositories seen, read before the lock (``repositories_for``), weighed
        with the projects of this process as they are now; None: read here (a server's space, which reads none). One
        not complete, for a change that could lower protection in a repository, raises RepositoriesUnchecked: nothing
        is applied, opened or kept as approved, and a change waiting stays as it is.

        A command's change, or a save in the dashboard (Task 1's review, C1: the person's own, as a command is), that
        goes further below your company's floor raises BelowFloor (``check_floor``): never held for approval, which
        could not lift it. Settings that arrive (a file read again, a start) are weighed as ever and raised where they
        apply, never refused here (ruling 11)."""
        with self._lock:
            if source in ("command", "dashboard"):
                self.check_floor(config)
            base = self.reviewed() or self.config
            # The repositories seen with a latet.yaml of their own, before the daemon's start too, each file read again:
            # weighed as a folder of your settings is.
            if self.projects is None:
                scan = RepositoryScan()
            elif repositories is None:
                scan = self.projects.repositories()
            else:
                scan = self.projects.with_active(repositories)
            if not scan.complete and weighs_repositories(base, config):
                raise RepositoriesUnchecked()
            reasons = loosened(base, config, scan)
            if not reasons:
                return None
            change = Change(
                config, digest(self.config), source, agent, reasons, changed_keys(self.config, config),
                guidance_changes(base, config), destination_changes(base, config), exception_changes(base, config),
                asked_by=asked_by,
            )  # fmt: skip
            if route_of is None:
                return self.mediator.approvals.open_change(change)
            # Asked again: it waits for whoever the change it replaces waited for ([D4]; the review of Task 2, I1).
            change.decides, change.deciders = route_of.decides, list(route_of.deciders)
            self._carried.add(change.id)
            try:
                return self.mediator.approvals.open_change(change)
            finally:
                self._carried.discard(change.id)

    def _held_back(self) -> None:
        """A repository seen since the start whose own latet.yaml the settings the daemon started on lower against
        those you approved before it (``Projects.before``; it was worked out under these): what you approved runs, and
        the settings it started on wait for you, as at a start that knew the repository. Told by ``Projects`` outside
        its lock, maybe twice: once is enough. Told on the thread of a conversation (the engine's, the bridge's), it
        reads no repository there (R24 M4): the change is weighed in the start's snapshot of the repositories remembered
        (``start_scan``) with this run's projects as they are now, the repository just reached among them. Were it not
        complete, what you approved runs, and the settings it started on wait for a reload."""
        projects = self.projects
        if projects is None or projects.before is None:
            return
        scan = self.start_scan if self.start_scan is not None else RepositoryScan(complete=False)
        with self._lock:
            approved = projects.before
            if approved is None:
                return
            found = self.config
            try:
                self.apply_config(approved, save=False)  # config.yaml holds what waits for you
            except Exception as exc:  # the repository stays worked out under them; told again at its next build
                log.error("could not run the settings you approved: %s", described(exc))
                return
            try:
                change = self.propose(found, "start", repositories=scan)
            except RepositoriesUnchecked:
                log.warning("the settings you approved run: the repositories Latet has seen could not all be read now")
                return
            except BelowFloor as refused:  # nothing opened: what you approved runs
                log.warning(
                    "the settings you approved run: the others go below your company's floor (%s)",
                    type(refused).__name__,
                )
                return
            if change is None:  # the repository's file changed meanwhile: they lower nothing
                self.apply_config(found, save=False)

    def add_destinations(
        self, wanted: list[tuple[str, Literal["claude", "codex"] | None]], *, judged: Collection[str] | None = None
    ) -> list[str]:
        """Destinations for the addresses an agent's settings name, in use at once; their names. An
        address already known keeps its destination. A change waiting for you is asked again with them
        in it, what it lowers weighed against the settings they make: approving it neither drops them
        nor counts as stale, and a destination of theirs it would move is shown moved. Each name stays
        in ``wrapped``, a new destination or a known one: removing it waits until the daemon restarts.

        On a server: ``judged``, the addresses (each as a saved one is written) that the computer's call judged as an
        identity's own destination is judged; one that was not, and that the settings do not have when the change is
        made under the lock (they changed meanwhile: a shared destination removed), refuses the whole call
        (ChangedMeanwhile), nothing added. A person's settings in use over a composition keep what waits for her when
        saved (``update_config``), so the destinations are saved with it there. Otherwise what waits is never saved as
        the identity's own part there (R29: it may wait for someone else, and a re-application would offer it to them):
        the destinations are saved as approved settings, and what waits is asked again with them."""
        from latet.destinations import add_destination

        names: list[str] = []
        # Read before the lock: what a change waiting, asked again with the destinations, is weighed in.
        computer = self.projects is not None and self.identity is None
        waiting_now = next(iter(self.mediator.approvals.waiting_changes()), None)
        # None read when what waits could lower nothing in a repository (``repositories_for``: its destinations, the
        # only thing added, are weighed in none).
        scan = self.repositories_for(waiting_now.config) if computer and waiting_now is not None else None

        def add(current: Config) -> Config:
            if judged is not None:
                have = {d.url for d in current.upstreams}
                if any(url not in judged and url not in have for url, _ in wanted):
                    raise ChangedMeanwhile(CHANGED_MEANWHILE)
            config = current
            for url, agent in wanted:
                config, name, _ = add_destination(config, url, agent)
                names.append(name)
            return config  # the same settings when every address was known

        with self._lock:  # from reading the settings to the end: a save made meanwhile waits, then lands on top
            before = self.config
            waiting = next(iter(self.mediator.approvals.waiting_changes()), None)
            apart = waiting is not None and self._held() is None  # what waits is not a composition of what is kept
            # On a computer config.yaml holds what waits for you. On a server what waits is never written as the
            # identity's own part (a re-application would offer it to whoever's settings they are): the destinations are
            # saved as approved settings, and what waits is asked again with them.
            aside = apart and self.identity is None
            reasked = apart and self.identity is not None
            try:
                config = self.update_config(add, save=not aside)
            except SettingsSavedChangePending:
                self.wrapped.update(names)  # the destinations were saved and applied before the refresh failed
                raise
            self.wrapped.update(names)  # under the lock: a removal that comes next sees them in use
            if config is None or waiting is None or not (aside or reasked):
                return names
            # Asked again with the destinations added, but one whose address it has, or whose name it gives
            # another address: that move is what you must see.
            added = [d for d in config.upstreams if d not in before.upstreams]
            urls = {d.url for d in waiting.config.upstreams}
            taken = {d.name for d in waiting.config.upstreams}
            extra = [d for d in added if d.url not in urls and d.name not in taken]
            again = waiting.config.model_copy(update={"upstreams": [*waiting.config.upstreams, *extra]})
            if computer and scan is None:  # a change opened since the look before the lock: nothing read for it
                scan = RepositoryScan(complete=False)
            try:
                proposed = self.propose(
                    again, waiting.source, waiting.agent, repositories=scan, asked_by=waiting.asked_by, route_of=waiting
                )
            except RepositoriesUnchecked:
                # The destinations are in use now; the change waiting stays as it is, asked again at a later reload.
                log.warning("a change waiting was not asked again: the repositories seen could not all be read now")
                return names
            except BelowFloor:  # the change waiting goes below the company's floor now: it can only be rejected
                self.mediator.approvals.settle_change(waiting.id, "rejected")
                self.settings.save(self.config)  # config.yaml back to the settings in use, as a rejection leaves it
                return names
            if proposed is not None:  # it replaces the one waiting
                if aside:
                    self.settings.save(again)  # config.yaml holds what waits for you
            else:  # it lowers nothing against the settings in use now: it applies, as a reload would
                self.apply_config(again, save=True)
                self.mediator.approvals.settle_change(waiting.id, "superseded")
            return names

    def decide_change(
        self, change_id: str, approve: bool, by: IdentityRef | None = None, stopped_by: str | None = None
    ) -> str:
        """``approved``, ``rejected``, ``gone`` (not waiting) or ``stale`` (the settings changed since: it
        would undo that). A change you reject is undone in config.yaml too. One that goes below your company's
        floor since it was asked for can only be rejected: it is, and ``BelowFloor`` says why. ``not-yours``: someone
        else decides on it, and you may undo it, not approve it; ``by``: who decided, when it was not you.
        ``stopped_by``: in an API key's space, the person working there (E3's ``latet.acting``), never weighed to
        approve: when they undo a change they did not make, it is rejected by them in how it ended alone (the record,
        the event: the whole-branch review's m1); whoever made it withdraws it."""
        with self._lock:  # no other change between the check and the apply
            approvals = self.mediator.approvals
            change = approvals.changes.get(change_id)
            if change is None:
                return "gone"
            if approve and change.decides != "self" and by is None:
                return "not-yours"
            refused = self._refused(self.config, change.config) if approve else []
            if approve and not refused:
                refused = self._not_the_companys(change, by)
            if refused:
                approve = False
            if approve and change.base != digest(self.config):
                approvals.settle_change(change_id, "stale", by)
                return "stale"
            # Who undid it in an API key's space, when they did not make it (m1): named, never weighed to approve.
            theirs = (change.asked_by, self.identity.id if self.identity is not None else None)
            stopper = stopped_by if not approve and by is None and stopped_by not in theirs else None
            if approvals.settle_change(change_id, "approved" if approve else "rejected", by, stopper) is None:
                return approvals.settled.get(change_id, "gone")
            if approve:
                # The exceptions it adds, widens, renews or makes permanent are approved now: stamped by Latet (D3),
                # with who approved them when the company's security team or an API key's other controllers decided.
                base = self.reviewed() or self.config
                decider = by if by is not None and change.decides in ("security", "controllers") else None
                stamped = self._stamped(base, change.config, decider, change.decides, decided=True)
                try:
                    self.apply_config(stamped, save=True)
                except (OSError, SharedItemsError):
                    if by is not None:
                        # Ended approved (the claim among instances comes first), and its save then refused: another
                        # instance wrote the settings since (OSError), or they hold what a server refuses now
                        # (SettingsRefused, a SharedItemsError: R70). The person's dashboard is told what it is.
                        told = {"id": change_id, "state": "approved-unapplied", "by": by.name}
                        approvals.publish_change(told, "change_settled")
                    raise
                self._log_permanent(base, stamped, decider)
                self._log_new_members(base, stamped, decider)
                return "approved"
            self.settings.save(self.config)
            if refused:
                raise self._below(refused)
            return "rejected"

    def _not_the_companys(self, change: Change, by: IdentityRef | None) -> list[str]:
        """Where the company decides on request exceptions (``Floor.decides_exceptions``: Latet Enterprise's
        `exceptions: company`), why approving ``change`` is refused: one reason for each destination of an exception it
        adds, widens, renews or makes permanent, unless its security team approves it (``by``, on a change that waits
        for it). What the floor judges is the approval, whenever it comes: a change asked for before the policy said so
        is judged by the policy of now."""
        if not company_decides(self.floor()) or (by is not None and change.decides == "security"):
            return []
        base = self.reviewed() or self.config
        destinations = dict.fromkeys(str(row["destination"]) for row in exception_changes(base, change.config))
        return [COMPANY_DECIDES.format(destination=destination) for destination in destinations]

    def _log_permanent(self, base: Config, approved: Config, decider: IdentityRef | None = None) -> None:
        """On a server, each exception the approval made permanent written to the administration log (R-108): its id
        and its scope's category, never its description or what it names, and ``by``, the decider of the company's who
        approved it (Latet Enterprise), the entry's actor then. A log that is not written is said by its type: the
        approval stands (it is in the settings, and listed by the server's status)."""
        if self.exception_log is None:
            return
        before = {row.id for row in base.request_exceptions if row.permanent}
        for row in approved.request_exceptions:
            if row.approved and row.permanent and row.id not in before:
                details = {"exception": row.id, "scope": "project" if row.project else "identity"}
                if decider is not None:
                    details["by"] = decider.id
                try:
                    self.exception_log(details)
                except Exception as exc:
                    log.warning("a permanent exception's approval was not logged: %s", type(exc).__name__)

    def _log_new_members(self, base: Config, approved: Config, decider: IdentityRef | None = None) -> None:
        """On a server, an approval that moves ``unknown_members`` from `refuse` to `check` against ``base`` (the
        settings approved before) written to the administration log (design §3): the setting, its value, its scope's
        category and how many projects stop refusing (``protection.new_members_checked``), never a folder; and ``by``,
        the decider of the company's who approved it (Latet Enterprise), the entry's actor then. A log that is not
        written is said by its type: the approval stands. Nothing on a computer, nor for an approval that moves nothing
        so."""
        if self.setting_log is None or (details := new_members_checked(base, approved)) is None:
            return
        if decider is not None:
            details["by"] = decider.id
        try:
            self.setting_log(details)
        except Exception as exc:
            log.warning("an approval of new request members read and checked was not logged: %s", type(exc).__name__)

    def _stamped(
        self,
        base: Config,
        config: Config,
        decider: IdentityRef | None = None,
        decides: Decides = "self",
        *,
        decided: bool = False,
    ) -> Config:
        """``config`` with the request exceptions it adds, widens, renews or makes permanent against ``base`` stamped
        as approved now (``protection.stamp_exceptions``); ``decider``: who approved them for the company (its security
        team, or an API key's other controllers: ``decides``), named on each. ``decided``: approved as a change that
        waited (``decide_change``), never the administration's own save of an API key's settings: in a key's space a
        person working there approved it, a controller's approval, not the administration's (fix round 1 of the release
        minors: under `exceptions: company` it then grants nothing, ``request_policy.counted``)."""
        from latet.gateway.request_profiles import REVISION

        if decider is not None and decides in ("security", "controllers"):
            issuer: str = decides
            approver: tuple[str, str] | None = (decider.name, decider.id)
        else:
            # Who approves it, as Latet knows them, never as the settings say: you on your computer, a server's person,
            # the administration that keeps an API key's settings (its save), or a person in the key's space (a change
            # that waited for it there, with no central approvals).
            identity = self.identity
            if identity is None:
                issuer = "owner"
            elif identity.kind == "key":
                issuer = "controllers" if decided else "administration"
            else:
                issuer = "person"
            approver = None
        return stamp_exceptions(base, config, self.exception_clock(), REVISION, issuer, approver)

    def use_reviewer(self, config: Config) -> None:
        """The local reviewer of these settings: the one running stays when its server is the same."""
        review = config.llm_review
        mediator = self.mediator
        mediator.llm_review = review
        mediator.on_review = self._record_review if self.exchanges is not None else None
        old = mediator.reviewer
        if not review.enabled:
            mediator.reviewer = None
        elif (
            old is not None
            and _same_server(old.config, review)
            and old.client.key_id == key_id(self.reviewer_key(review))
        ):
            old.config = review
            return
        else:
            if review.in_clear():
                log.warning(
                    "the local reviewer is reached over http: the original texts cross your network unencrypted"
                )
            key = hmac.new(self.built.vault.cache_key, b"llm-review", hashlib.sha256).digest()
            client = (
                self.review_client(review)
                if self.review_client is not None
                else client_for(review, key_of=self.reviewer_key)
            )
            mediator.reviewer = Reviewer(review, key, client, memory=self.store)
        if old is not None:
            mediator.retired.append(old)  # closed on the event loop, after what it is doing

    def forget_reviews(self) -> None:
        """What the local reviewer read is read again: its remembered answers deleted, and the reviewer running retired
        (the next settings make a new one, with nothing remembered). The tutorial's sandbox, at a chapter's start."""
        with self._lock:  # as the settings that make the next reviewer: one at a time
            self.store.forget_reviews()
            mediator = self.mediator
            if mediator.reviewer is not None:
                mediator.retired.append(mediator.reviewer)
                mediator.reviewer = None

    def _record_review(self, record: ReviewRecord) -> None:
        if self.exchanges is None or self.config.history == "off":
            return
        vault = self.built.vault
        found = []
        masked = {f.value for f in record.masked}
        # A value you marked Not sensitive is reported and left in clear: said so, not offered again.
        allow = self.mediator._masker(self.built.engine.session(record.session)).allow
        for finding in record.answer.findings:
            entity = vault.by_original(finding.value) if finding.value in masked else None
            needle = entity.surrogate if entity is not None else finding.value
            at = record.sent.find(needle)
            found.append(
                {
                    "category": finding.category,
                    "masked": finding.value in masked,
                    "allowed": finding.value not in masked and allow.allows(finding.value),
                    "entity": entity.id if entity is not None else None,
                    "at": [at, at + len(needle)] if at >= 0 else None,
                }
            )
        project_id = valid_project_id(record.project)
        project = self.projects.peek(project_id) if self.projects is not None and project_id is not None else None
        project_id = valid_project_id(project.id) if project is not None else None
        summary: dict[str, Any] = {
            "ts": time.time(),
            "session": self.exchanges.history_group(record.session),
            "project": project_id,
            "block": self.exchanges.block_id(record.sent),
            "kind": record.kind,
            "inline": record.inline,
            "findings": found,
            "failed": record.answer.failed,
            "block_asked": record.answer.block,
            "seconds": round(record.answer.seconds, 2),
        }
        summary["id"] = self.exchanges.record_review(summary, session_source=record.session, project_id=project_id)
        if self.recorder is not None:
            self._on_loop(self.recorder.broadcaster.publish, summary, "review")

    def forget_rejected_names(self) -> int:
        """Drop names the local model found that its current filters reject (they would stay masked). A person's vault
        on a server's store (``StoredVault``) has the store read every name, those out of its memory too
        (``of_class``), as its whole vault was read before; an API key's reads the names its memory holds, as
        before."""
        from latet.engine.detectors.ner import rejected_now
        from latet.engine.types import EntityClass

        vault = self.built.vault
        of_class = getattr(self.store, "of_class", None)
        person = self.identity is not None and self.identity.kind == "person"
        whole = person and isinstance(vault, StoredVault) and of_class is not None
        names = of_class(EntityClass.NER) if whole and of_class is not None else vault.entities()
        # The ids alone kept, the names given a part at a time (``of_class``): never every name in memory at once.
        stale = [e.id for e in names if e.eclass == EntityClass.NER and rejected_now(e.etype, e.original)]
        return len(self.forget_all(stale))

    def warm_up(self) -> None:
        """Start loading slow detectors (the local name model, text recognition) now, not on the first request."""
        for detector in self.built.masker.detectors:
            start = getattr(getattr(detector, "model", None), "start_loading", None)
            if start is not None:
                start()
        if self.mediator.vision is not None and self.mediator.ocr:
            self.mediator.vision.start()

    def forget(self, entity_id: str) -> Entity | None:
        """Remove a value from the vault (and so from the dashboard's history)."""
        with self._lock:
            entity = self.built.vault.forget(entity_id)
            if entity is not None:
                self.store.forget_entity(entity_id)
                self.built.engine.rules_changed()  # cached masked blocks may hold its surrogate
            return entity

    def forget_all(self, entity_ids: Iterable[str]) -> list[Entity]:
        """Remove values from the vault at once: the store asked once for all of them (a computer's upgrade copies
        opened once each), the masked blocks cached dropped once."""
        with self._lock:
            gone = [entity for entity_id in entity_ids if (entity := self.built.vault.forget(entity_id)) is not None]
            if gone:
                self.store.forget_entities([entity.id for entity in gone])
                self.built.engine.rules_changed()  # cached masked blocks may hold their surrogates
            return gone

    def close(self) -> None:
        # First: a floor that changes while the runtime closes applies nothing. Stopped once: closing twice is no harm.
        unwatch, self._unwatch = self._unwatch, _nothing
        unwatch()
        if self.launch_keys is not None:
            self.launch_keys.close()  # physical holds/active mutations remain charged until their actual detach
        if self.recorder is not None:
            self.recorder.close()
        if self.exchanges is not None:
            self.exchanges.close()
        self.store.close()
        self.mediator.close()  # its worker threads end: none is left for the process to wait for
        if type(self.request_audit) is AuditOwner:
            self.request_audit.close()  # reject new work; running workers retain their active leases
        if self.identity is None:  # a computer's: a helper still reading its repositories ended, never waited for
            repository_io.READER.stop()
        owns, self.owns_edition = self.owns_edition, False  # the edition it made, closed once
        if owns:
            self.edition.close()


def _unstamped(row: Any) -> Any:
    """``row`` without Latet's stamps, who approved it among them (R-3): pending, as an exception you have not
    approved is."""
    unstamped = {"id": None, "granted": None, "revision": None, "body_revision": None, "issuer": None}
    return row.model_copy(update={**unstamped, "approver": None, "approver_id": None})


def _allowed() -> bool:
    """A computer's edition, the Community Edition, allows permanent request exceptions."""
    return True


def approved_exceptions(config: Config, permanent_allowed: bool = True, *, company: bool) -> tuple[Any, ...]:
    """Your request exceptions that may grant something: approved, under the request profiles this version has or one
    carried over (their lifetime and project are judged as each request is decided). A pending, rejected or revised
    one is not routed, nor a permanent one the edition forbids (``permanent_allowed``), nor, where the company decides
    on request exceptions (``company``: the floor's ``decides_exceptions``, ``company_decides``), one that its
    security team or the administration did not approve (``request_policy.counted``: one you or an API key's
    controllers approved before the policy said so grants nothing, permanent or not)."""
    from latet.gateway.request_policy import carried, counted

    return tuple(
        row
        for row in config.request_exceptions
        if row.approved and carried(row) and (permanent_allowed or not row.permanent) and counted(row, company=company)
    )


def review_policy(config: Config) -> ReviewPolicy:
    review = config.auto_mode_review
    return ReviewPolicy(review.allowed(), surrogate_user_name=review.user_name == "surrogate")


def resolve_identity(config: Config, detect: bool = True) -> IdentityConfig | None:
    ident = config.identity
    if not ident.enabled:
        return None
    if ident.username or ident.usernames or ident.full_name or ident.emails or ident.hostname or ident.hostnames:
        return ident
    # The container's own user and host name are not yours (`latet identity set` gives them), nor a server's its
    # identities' (``detect`` False: each computer sends its own when it connects).
    return None if in_container() or not detect else detect_identity()


def synced_detectors(edition: Edition, identity: IdentityRef | None) -> tuple[Detector, ...]:
    """What the edition adds to every masker of a runtime: the terms the company syncs (Latet Enterprise, on a server),
    one detector shared by every runtime of the instance, which changes its terms in place; nothing in the Community
    Edition. Raises ``FloorUnavailable`` (``TermsUnavailable``) when they cannot be said: nothing is built then."""
    synced = edition.synced_terms(identity)
    return () if synced is None else (synced,)


def personal_names(settings: SettingsStore, config: Config) -> set[str]:
    """On a server, the destinations of the identity's own: every one the shared settings do not set as it is (that
    name at that address), whose address is checked again when the server connects to it; on a computer, none. A shared
    name at another address counts as the identity's own: the shared settings read here may be later than those
    ``config`` was laid over."""
    if not isinstance(settings, OwnSettings):
        return set()
    shared = {(d.name, d.url) for d in settings.shared().upstreams}
    return {d.name for d in config.upstreams if (d.name, d.url) not in shared}


def routes_of(config: Config, personal: set[str]) -> list[Upstream]:
    """The routes of the destinations of ``config``, those of the identity's own (``personal``) marked as such."""
    return [destination_upstream(d, own=d.name in personal) for d in config.upstreams]


def _same_server(a: LlmReview, b: LlmReview) -> bool:
    keys = ("url", "model", "api_key_env", "api_key_file", "chunk_chars", "slots", "certificate")
    return all(getattr(a, k) == getattr(b, k) for k in keys)


def image_masking(
    data: Path,
    store: VaultStore | None = None,
    *,
    space: bool = False,
    reading: ServerReading | None = None,
    ocr_unread: Callable[[OcrUnreadReason], None] | None = None,
) -> ImageMasking | None:
    """Reading and masking images, when the ``ocr`` extra and its models are installed; your decisions on images (Live
    Reviewer) are kept in ``store``. On a computer one text recognition reads for every runtime of the process, with no
    bound; ``space``: an identity's space on a server reads through its process's pool of readers with the server's
    bounds (``reading``: its settings'; none, the defaults), one image of the identity at a time, and no image larger
    than a server's ceiling."""
    from latet.vision import models

    if not models.runtime_available() or not models.installed(data):
        return None
    from latet.vision.ocr import shared_ocr

    if space:
        from latet.vision.pool import SPACE_MAX_PIXELS, ServerReading, SpaceReads, shared_pool

        bounds = reading or ServerReading()
        pool = shared_pool(*models.folders(data), bounds.readers)
        reads = SpaceReads(pool, bounds.wait, bounds.budget)
        return ImageMasking(reads, store, max_pixels=SPACE_MAX_PIXELS, space=True, ocr_unread=ocr_unread)
    return ImageMasking(shared_ocr(*models.folders(data)), store)


def _key_vault(
    key: bytes, store: KeyVaultStore, new_values: NewValues, working: int, held: int, detectors_most: int
) -> StoredVault:
    """An API key's vault: its newest ``working`` values in memory, every other one found in its store as a text needs
    it (``StoredVault``, R7). The values kept by a Latet before the store kept their anchor words (or kept them by an
    older version of the anchors) get them first, said once in the log when there were any; and the store counts the
    vault's values itself, for its limit, as the space no longer loads them all, and the generation its new fakes are
    drawn in (Task 11b: moved by each forget of values by themselves). ``detectors_most``: what its detectors may take
    past the ordinary RE2 budget at most (``StoredVault.detectors_extra_most``)."""
    filled = store.backfill_words()
    if filled:
        log.info("the words of %d values of an API key's vault were made, so that they are found in its texts", filled)
    store.count = store.count_entities()
    store.load_generation()
    return StoredVault(
        key, store, working=working, held_at_most=held, detectors_extra_most=detectors_most,
        on_insert=new_values.keep, on_rule=store.save_rule,
        on_learn=new_values.learn,
        on_replace=store.replace_fake,
    )  # fmt: skip


def _person_vault(
    key: bytes, store: KeyVaultStore, new_values: NewValues, working: int, held: int, detectors_most: int
) -> StoredVault:
    """A person's vault on a server whose values all have their words (``words_complete``): as an API key's
    (``_key_vault``), its ``working`` values used last in memory and every other one in its store, and, for good, every
    value no word of a text finds glued, whatever its age (``glued_all``: those glued on both sides and every secret
    held glued, Ruling T9-S3), as its whole vault held them before. Its store counts its values, for its limit. Its
    detectors get an API key's RE2 budget with the same ceiling (``detectors_most``, Task 8's fix round 9): what they
    take past the ordinary budget is weighed in the space's size, never in its share, and never refuses a value. A vault
    of ``stored.WHOLE_VALUES`` values or fewer (as it is at this build) is held whole, every value in memory and none
    asked of the store for a text (``StoredVault.holds_all``), until it holds more."""
    store.count = store.count_entities()
    store.load_generation()
    return StoredVault(
        key, store, working=working, held_at_most=held, glued_all=True, whole_past_bounds=True,
        detectors_extra_most=detectors_most, whole_at_most=stored_vaults.WHOLE_VALUES, counted=store.count,
        on_insert=new_values.keep,
        on_rule=store.save_rule, on_learn=new_values.learn, on_replace=store.replace_fake,
    )  # fmt: skip


def _words_complete(store: KeyVaultStore) -> bool:
    """Whether every value of ``store`` has its words, so that a person's vault is found by them (``words_complete``);
    a store that cannot say, no."""
    complete = getattr(store, "words_complete", None)
    return complete is not None and bool(complete())


def make_runtime(
    config: Config | None = None,
    data: Path | None = None,
    capture: Path | None = None,
    record: bool = True,
    config_file: Path | None = None,
    root_key: bytes | None = None,
    backends: StateBackends | None = None,
    identity: IdentityRef | None = None,
    reading: ServerReading | None = None,
    reviewer_key: KeyReader | None = None,
    ocr_unread: Callable[[OcrUnreadReason], None] | None = None,
    working: int = WORKING_VALUES,
    held: int = HELD_SECRETS,
    edition: Edition | None = None,
    watch_floor: bool = True,
    detectors_most: int = DETECTORS_EXTRA_MOST,
    permanent_allowed: Callable[[], bool] | None = None,
) -> Runtime:
    """A Latet's runtime, over ``backends`` (None: this computer's, ``local_backends``); ``identity``: whose space it is
    on a Latet server (None on a computer), ``reading``: the bounds of the text recognitions the server's spaces share
    (its settings'; read only with ``identity``). Backends are given with ``root_key``, the key their vault is sealed
    with: never this computer's own, which another instance would not have. Without ``record`` nothing is recorded, not
    even in a history that is given. ``reviewer_key``: on a server, where the space's reviewer takes its key from;
    none given, it asks with none. A computer reads its own key as before. ``working``: how many values an API key's
    space and a person's keep in memory (``StoredVault``; the server's ``key_working_values``), ``held``: the secrets an
    API key's space holds glued for good (``key_held_secrets``; a person's holds every one), ``detectors_most``: what
    their detectors may take past the ordinary RE2 budget at most (``StoredVault.detectors_extra_most``).
    ``permanent_allowed``: whether the edition allows permanent request exceptions (a server's; None: this computer's
    Community Edition, which does), the space's from its first build (Minor 1). ``edition``: the edition it runs, whose
    floor it watches; none given, this computer's (``load_edition``), which the runtime closes with itself: a space with
    an ``identity`` or over given ``backends`` is a server's, refused without one (ValueError). ``watch_floor`` False: a
    server's space, which watches nothing itself: the server's spaces watch the edition once and apply a new floor in
    each space's own worker (``Spaces.floor_changed``)."""
    from latet.server.logs import frames_only

    if edition is None and (identity is not None or backends is not None):
        # A space on a Latet server, or over a server's backends: the edition looked up here would be a computer's
        # Community Edition, a space with no floor. Refused before anything is made (the tutorial passes its own).
        raise ValueError("a server's space runs the server's edition: pass edition=")
    frames_only()  # an error this process logs is shown by its types and frames, never its words (a console too)
    data = data or data_dir()
    # The edition: given (on a server, the server's one for every space; the tutorial's Community Edition), else this
    # computer's, which the runtime then closes with itself.
    owns_edition = edition is None
    if edition is None:
        edition = load_edition(
            EditionContext(data=data, config_dir=(config_file or config_path()).parent, server=None, clock=time.time)
        )
    try:
        if backends is None:
            # Read first, as before: a config.yaml that cannot be read stops the start before a key or a store is made.
            config = config or LocalSettings(config_file, data).load()
        elif root_key is None:
            raise ValueError("a runtime over given backends needs their root key (root_key), which seals their vault")
        from latet.storage.backup import RESTORE_JOURNAL, restoring

        # Never on half a restore, whoever keeps the state: with no key yet, a new one would be made.
        if restoring(data):
            raise RuntimeError(
                f"a restore is under way ({data / RESTORE_JOURNAL}): finish it, or `latet restore --abort`"
            )
        key = root_key if root_key is not None else load_root_key(data)  # given: the tutorial's sandbox, never saved
        if backends is not None:
            return _started(
                config, data, capture, record, config_file, key, backends, identity, reading, reviewer_key, ocr_unread,
                working=working, held=held, detectors_most=detectors_most, permanent_allowed=permanent_allowed,
                edition=edition, owns_edition=owns_edition, watch_floor=watch_floor,
            )  # fmt: skip
        backends = local_backends(data, config_file, key, record=record)
        try:
            return _started(
                config, data, capture, record, config_file, key, backends, identity, reading, reviewer_key, ocr_unread,
                working=working, held=held, detectors_most=detectors_most, permanent_allowed=permanent_allowed,
                edition=edition, owns_edition=owns_edition, watch_floor=watch_floor,
            )  # fmt: skip
        except BaseException:  # the start failed: the databases opened here are closed here, not when the process ends
            backends.vault.close()
            if backends.history is not None:
                backends.history.close()
            raise
    except BaseException:  # the start failed: an edition made here is closed here too
        if owns_edition:
            edition.close()
        raise


def _started(
    config: Config | None,
    data: Path,
    capture: Path | None,
    record: bool,
    config_file: Path | None,
    key: bytes,
    backends: StateBackends,
    identity: IdentityRef | None,
    reading: ServerReading | None = None,
    reviewer_key: KeyReader | None = None,
    ocr_unread: Callable[[OcrUnreadReason], None] | None = None,
    working: int = WORKING_VALUES,
    held: int = HELD_SECRETS,
    detectors_most: int = DETECTORS_EXTRA_MOST,
    permanent_allowed: Callable[[], bool] | None = None,
    *,
    edition: Edition,
    owns_edition: bool,
    watch_floor: bool = True,
) -> Runtime:
    """The runtime ``make_runtime`` answers, over backends that are open (its own to close, or given), running
    ``edition``, which it closes with itself when it ``owns_edition``, and whose floor it watches when
    ``watch_floor``."""
    allowed = permanent_allowed or _allowed
    # An identity's space on a Latet server: nothing of the server's machine is the identity's (its user, host name and
    # project files), the bridge trusts the server (its own token opens nothing), and a destination of the identity's
    # own is checked again when the server connects to it.
    space = identity is not None
    settings = backends.settings
    config = config or settings.load()
    # A computer's: the repositories it has seen with a latet.yaml of their own, before this start too, each weighed
    # with a change of the settings (a server reads no repository file).
    seen = None if space else SeenRepositories(LocalRepositories(data))
    # Settings that lower protection against those you approved last (config.yaml edited while the daemon was off; on a
    # server, an own part written while the space was closed) wait for you: until then it runs with those you approved.
    found = config
    approved = settings.reviewed()
    if approved is None and any(row.approved for row in found.request_exceptions):
        # Latet's stamps with no approval of yours behind them (a first start, a snapshot that cannot be read): each
        # request exception waits for you as a new one, never in force on the stamps it carries (Minor 5); approved,
        # Latet stamps it then.
        config = found.model_copy(update={"request_exceptions": [_unstamped(row) for row in found.request_exceptions]})
    # A computer's repositories, read once (the list as read a moment ago, each file once): the snapshot the start
    # weighs its settings in and opens their change with. Read only where they could weigh: with settings you approved
    # that the ones found could lower something against in a repository. With none approved there is nothing to weigh
    # against, so none are read and nothing is refused for them (R24 M2).
    scan: RepositoryScan | None = None
    if seen is not None and approved is not None and weighs_repositories(approved, found):
        scan = seen.weighed(again=False)
    unchecked = scan is not None and not scan.complete
    if approved is not None and isinstance(settings, OwnSettings):
        # A server's: the settings of now but where they lower protection against the approved ones (``composed``),
        # so that what the shared settings add since applies, what they remove waits, and the server's own keys follow.
        config = settings.composed(found, approved)
    elif approved is not None and (unchecked or loosened(approved, config, scan or ())):
        config = approved  # an incomplete snapshot weighs as a change that waits: what you approved runs
    # A computer's settings that lower nothing it can weigh at its start, but are not those you approved (config.yaml
    # written while it was off): what you approved before is kept, as the snapshot and for the repositories it has not
    # seen yet, each weighed against it when first seen (``Projects.before``). So too while a repository could not be
    # read at the start: what you approved runs, and is what each repository is worked out against.
    before = None
    if approved is not None and not isinstance(settings, OwnSettings):
        if unchecked:
            before = approved
        elif config is found:
            before = approved if digest(approved) != digest(found) else None
    # The engine is built on the settings raised to the company's floor (Latet Enterprise; ``config`` itself without
    # one), last before the build: the settings as a store reads them hold no term the company locks. A person linked to
    # the company's identity provider is masked under the name it gives where names are left in clear (``named_in``,
    # ``provider_named``), never in the settings.
    floor_used = edition.floor(identity)
    provided = edition.provider_name(identity)
    extra = synced_detectors(edition, identity)  # read before anything is built: nothing is, without them
    raised = raised_to(floor_used, config)
    named = named_in(raised, provided)
    effective = provider_named(raised, named)
    store = backends.vault
    new_values = NewValues(store)
    # A fake kept before the vault refused one that shows its value is drawn again as the vault loads (``on_replace``).
    vault: Vault
    if identity is not None and identity.kind == "key" and isinstance(store, KeyVaultStore):
        vault = _key_vault(key, store, new_values, working, held, detectors_most)
    elif identity is not None and isinstance(store, KeyVaultStore) and _words_complete(store):
        vault = _person_vault(key, store, new_values, working, held, detectors_most)
    else:  # a computer; a person's whose words are not all made yet (its space makes them in the background, and the
        # space built after is on its store); a space over a store that reads no vault by parts: all of it in memory
        vault = Vault(
            key, store.load_entities(), on_insert=new_values.keep, on_rule=store.save_rule, on_learn=new_values.learn,
            on_replace=store.replace_fake,
        )  # fmt: skip
    identity_config = resolve_identity(effective, detect=not space)
    built = build(
        effective, vault, identity_config, conversations=backends.conversations, given=backends.given, extra=extra
    )
    built.engine.rules_tag = rules_digest(effective)
    if space:  # one of many in its process's memory
        built.engine.bound(SPACE_CACHE_BYTES, SPACE_BLOCK_BYTES)
        built.verifier.shared = backends.conversations  # what the model wrote, for every instance

    projects = Projects(effective, vault, identity_config, files=not space, seen=seen, extra=extra)
    projects.before = before
    built.engine.scopes = projects
    exchanges = backends.history if record else None  # without `record`, a history that is given is left alone too
    if exchanges is not None:  # your retention (it opened knowing none)
        exchanges.retention_days = config.retention_days
        if not space:  # pruned at start on a computer; a space's by its recorder, under the identity's job lock
            exchanges.prune()
    recorder = (
        Recorder(exchanges, built.engine, backends.feed, config.history, sent=backends.sent, jobs=backends.jobs)
        if exchanges is not None
        else None
    )
    personal = personal_names(settings, config)
    gateway = GatewayConfig(
        port=config.port,
        capture_dir=capture or config.capture_dir,
        mediator=Mediator(
            built.engine,
            built.verifier,
            Approvals(backends.approvals, backends.approvals_feed),
            given=backends.given,
            echoes=backends.echoes or NoEchoes(),
        ),
        bridge_token=load_bridge_token(data) if not space else new_bridge_token(),
        recorder=recorder,
        unreadable=store.unreadable,
        aws=AwsCredentialStore(sessions=backends.aws),
        destinations=routes_of(config, personal),
        request_resources=tuple((row.destination, row.url, row.role, row.value) for row in config.request_resources),
        request_exceptions=approved_exceptions(config, allowed(), company=company_decides(floor_used)),
        personal_names=personal,
        # Each name resolved for the identity counted for it: one identity's names hold none of another's lookups.
        pin=functools.partial(egress.pin, holder=identity.id) if identity is not None else None,
    )
    gateway.mediator.images = image_policy(effective)  # type: ignore[union-attr]
    gateway.mediator.ocr = effective.ocr == "auto"  # type: ignore[union-attr]
    gateway.mediator.unknown_members = effective_unknown_members(effective, None)  # type: ignore[union-attr]
    gateway.mediator.vision = image_masking(  # type: ignore[union-attr]
        data, store, space=space, reading=reading, ocr_unread=ocr_unread
    )
    gateway.mediator.dump_guard = effective.dump_guard  # type: ignore[union-attr]
    gateway.mediator.review = review_policy(effective)  # type: ignore[union-attr]
    gateway.mediator.live_review = effective.live_review  # type: ignore[union-attr]
    runtime = Runtime(
        config,
        built,
        store,
        gateway,
        data,
        exchanges,
        recorder,
        config_file,
        projects,
        backends=backends,
        identity=identity,
        new_values=new_values,
        reading=reading if space else None,
        ocr_unread=ocr_unread if space else None,
        reviewer_key=reviewer_key if reviewer_key is not None else (fixed_key(None) if space else api_key_of),
        edition=edition,
        owns_edition=owns_edition,
        effective=effective,
        floor_used=floor_used,
        provided_name=provided,
        named_by_provider=named,
        launch_keys=LocalLaunchKeys(key) if not space else None,
        permanent_allowed=allowed,
        extra=extra,
    )
    runtime.use_reviewer(effective)
    projects.on_held_back = runtime._held_back
    projects.raised = runtime.raised
    runtime.start_scan = scan
    if found is not config:
        if unchecked:
            # Neither applied nor opened for you: weighed again at a reload, once every repository can be read.
            log.warning(
                "the settings you approved run: config.yaml differs from them, and the repositories Latet has seen "
                "could not all be read to weigh it (`latet reload` once they can)"
            )
        elif not (space and runtime.waits(found, over_these=True)):  # a space is built on many instances, repeatedly
            # The start's snapshot: nothing read again. None read (they could not weigh): none weighed.
            unread = None if space else RepositoryScan(complete=False)
            try:
                runtime.propose(found, "start", repositories=scan if scan is not None else unread)
            except BelowFloor as refused:  # nothing opened: the settings approved last run, raised
                log.warning(
                    "config.yaml is not applied: it goes below your company's floor (%s)", type(refused).__name__
                )
    elif before is None:  # else what you approved before stays the snapshot, until settings apply
        runtime._reviewed(config)
    runtime.forget_rejected_names()
    runtime.warm_up()
    from latet.api.rest import Dashboard  # the dashboard needs the runtime it manages

    gateway.dashboard = Dashboard(runtime)
    # Last, once nothing of the start can fail: a new floor applies at once (a runtime not made watches nothing). A
    # server's space watches nothing itself: its spaces watch the edition, and apply a new floor in the space's worker.
    if watch_floor:
        runtime._unwatch = edition.watch(runtime.floor_changed)
    try:
        if edition.floor(identity) is not floor_used:  # changed while it was built, before it watched (M4)
            runtime.floor_changed()
    except BaseException:  # the start fails: never a runtime below the floor; it watches nothing more
        unwatch, runtime._unwatch = runtime._unwatch, _nothing
        unwatch()
        raise
    return runtime
