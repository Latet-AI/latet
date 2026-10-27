"""Wire configuration, keys, storage and engine into a ready gateway configuration."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import hmac
import json
import logging
import threading
import time
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from latet import repository_io
from latet.config import (
    Config,
    IdentityConfig,
    LlmReview,
    data_dir,
    detect_identity,
    in_container,
)
from latet.engine.build import Built, build, build_masker
from latet.engine.pipeline import SPACE_BLOCK_BYTES, SPACE_CACHE_BYTES
from latet.engine.review import ReviewPolicy
from latet.engine.reviewer import Reviewer
from latet.engine.reviewer.client import ReviewClient, key_id
from latet.engine.reviewer.reviewer import KeyReader, api_key_of, client_for, fixed_key
from latet.engine.stored import HELD_SECRETS, WORKING_VALUES, StoredVault
from latet.engine.vault import Entity, MaskingError, Vault
from latet.gateway.app import GatewayConfig
from latet.gateway.approvals import Approvals, Change
from latet.gateway.aws import AwsCredentialStore
from latet.gateway.mediate import Mediator, ReviewRecord
from latet.gateway.recorder import Recorder
from latet.gateway.routes import Upstream, destination_upstream
from latet.logline import Paced, described
from latet.projects import (
    Projects,
    RepositoriesUnchecked,
    RepositoryScan,
    SeenRepositories,
)
from latet.protection import changed_keys, destination_changes, guidance_changes, loosened, weighs_repositories
from latet.server import egress
from latet.server.shared import CHANGED_MEANWHILE, ChangedMeanwhile, OwnSettings, SettingsSavedChangePending
from latet.state.local import (  # noqa: F401 - REVIEWED_FILE: its old name
    REVIEWED_FILE,
    LocalRepositories,
    LocalSettings,
    local_backends,
)
from latet.state.protocols import HistoryStore, IdentityRef, KeyVaultStore, SettingsStore, StateBackends, VaultStore
from latet.storage.keys import load_bridge_token, load_root_key, new_bridge_token
from latet.vision.masking import ImageMasking, OcrUnreadReason
from latet.vision.policy import image_policy

if TYPE_CHECKING:
    from latet.vision.pool import ServerReading  # NumPy: the ocr extra

log = logging.getLogger(__name__)
_REPROPOSAL_FAILED = Paced()


def digest(config: Config) -> str:
    return hashlib.sha256(json.dumps(config.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


class SpaceFull(MaskingError):
    """An identity's space on a Latet server weighs its share of the instance's memory, or more: it learns no new value,
    and a request that brings one is stopped, never sent unmasked. Its words name no value."""


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

    def __post_init__(self) -> None:
        # The changes to your settings are opened and settled here, in worker threads: the dashboards hear of
        # them on the event loop.
        approvals = self.mediator.approvals
        approvals.publish_change = lambda summary, event: self._on_loop(approvals.broadcaster.publish, summary, event)

    def __repr__(self) -> str:
        # Whose it is, and nothing of what it holds: its settings hold every term, and the names and emails its
        # computers sent; its routes the addresses of its destinations.
        return f"Runtime({self.identity.id if self.identity is not None else 'this computer'})"

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

    def apply_config(self, config: Config, save: bool = True, *, as_is: bool = False) -> int:
        """Switch to new rules at once; sessions and the vault are kept. Returns the rules version. ``as_is``: settings
        already made as they apply (on a server, those its spaces compose: laid over the shared settings again, an item
        the shared settings add would come back where the composition left it out).

        On a server, settings their store refuses to keep (another instance wrote them since, they weigh too much, the
        store does not answer: an OSError; or what they hold, a SettingsRefused) never run: what ran before runs again,
        as it ran, before the refusal is raised (the store has the space read what it holds when another instance's
        write refused them). A computer runs them, as before, keeping the history's retention as it was."""
        with self._lock:
            ran = self.config
            if not as_is:
                config = self.settings.prepare(config)
            # Read before anything changes: a store that does not answer leaves the settings in use as they are.
            personal = personal_names(self.settings, config)
            identity = resolve_identity(config, detect=self.identity is None)
            # The rules' masker alone: the engine, its conversations and the final check stay the ones that run.
            masker, recognizers = build_masker(config, self.built.vault, identity)
            engine = self.built.engine
            if self.projects is not None:
                self.projects.use(config, identity)
            engine.use(masker)
            engine.rules_tag = digest(config)
            self.mediator.verifier.use(masker)
            self.built = Built(self.built.vault, masker, engine, self.mediator.verifier, recognizers)
            self.mediator.images = image_policy(config)
            self.mediator.ocr = config.ocr == "auto"
            installed = self.mediator.vision or image_masking(
                self.data, self.store, space=self.identity is not None, reading=self.reading, ocr_unread=self.ocr_unread
            )
            self.mediator.vision = installed  # the models may have been installed since
            self.mediator.dump_guard = config.dump_guard
            self.mediator.review = review_policy(config)
            self.mediator.live_review = config.live_review
            self.use_reviewer(config)
            if self.recorder is not None:
                self.recorder.history = config.history
            # Weighed against the settings running; the history takes the new retention once the settings are kept.
            shorter = self.exchanges is not None and config.retention_days < self.config.retention_days
            if self.gateway.fixed_routes is None:  # the tutorial's sandbox routes to its pretend model only
                # A destination of the identity's own is judged at connect from the moment its route exists: its route
                # says so (``Upstream.own``), and the names of both settings are its own until the routes are swapped.
                self.gateway.personal_names = self.gateway.personal_names | personal
                self.gateway.destinations = routes_of(config, personal)
                if self.gateway.routes is not None:
                    self.gateway.routes.use([*self.gateway.upstreams, *self.gateway.destinations])
            self.gateway.personal_names = personal
            self.config = config
            if save:
                try:
                    self.settings.save(config)
                except (OSError, ValueError):  # not kept: the store failed, or refused what they hold
                    if self.identity is not None:  # a server's: the refused settings never run
                        self._ran_again(ran)
                    raise
            if self.exchanges is not None:  # kept: a save that fails leaves the history's retention as it was
                self.exchanges.retention_days = config.retention_days
            if self.projects is not None:
                # Settings weighed as they applied (against those you approved and the repositories seen), and kept as
                # approved: the ones from before the start are weighed against no longer.
                self.projects.before = None
            self._reviewed(config)
            self.warm_up()
            if shorter and self.recorder is not None:
                # Last, once the settings are kept (the save can fail): what is older than the new retention goes now.
                self.recorder.prune_now()
            # The Live Reviewer's texts still waiting shown masked with the rules of now, as they will leave
            self.mediator.live_changed()
            return engine.rules_version

    def _ran_again(self, ran: Config) -> None:
        """``ran``, the settings that ran before a save their store refused, applied again as they ran (under the lock).
        Its own failure is said in the log by its type, never raised over the refusal: the refused settings then run
        until the space applies its settings again."""
        try:
            self.apply_config(ran, save=False, as_is=True)
        except Exception as exc:
            log.warning("the settings that ran before a refused save could not run again: %s", described(exc))

    def update_config(self, change: Callable[[Config], Config | None], *, save: bool = True) -> Config | None:
        """Change the settings in use, read and applied under one lock: two changes made at once (a save in
        the dashboard, an agent's wrapper adding a destination) both stay, one on top of the other.
        ``change`` gets the settings as they are now and answers the new ones, or None (or the same ones) to
        leave them. The new settings; None when nothing changed.

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
            over = self._held() is not None
            new = self.settings.prepare(new, strict=True)  # on a server: refused when it drops what all share
            if over and new == current:  # nothing of what is in use changes: kept as it is, it would reject what waits
                return None
            self.apply_config(new, save=save, as_is=over)  # composed again, it is as it applies
            if save and self.identity is not None and self.identity.kind == "key":
                # An API key's settings are the administration's: its save approves them, and a change left waiting
                # (a rollback of its own part, which nobody else can approve) is over.
                approvals = self.mediator.approvals
                for change_id in list(approvals.changes):
                    approvals.settle_change(change_id, "superseded")
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

    def _held(self) -> tuple[Config, Config] | None:
        """The settings kept and those in use, when those are a composition over which a save is laid back on these: a
        person's on a server while a change waits for her (``OwnSettings.composition``); None otherwise."""
        settings = self.settings
        return settings.composition if isinstance(settings, OwnSettings) else None

    def _asked_again(self) -> None:
        """Under the lock, after a person's save made over a composition was applied and kept: the change waiting is
        asked again over the settings kept now, which hold what waits and the save's own changes, as
        ``add_destinations`` asks a change again with its destinations; settled as superseded when what is kept lowers
        nothing against what is approved now (the composition is over)."""
        waiting = next(iter(self.mediator.approvals.waiting_changes()), None)
        if waiting is None:
            return
        held = self._held()
        kept = held[0] if held is not None else self.config
        if held is not None and self.waits(kept, over_these=True):  # asked already, over the settings in use now
            return
        if self.propose(kept, waiting.source, waiting.agent) is None:
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
        source: Literal["command", "file", "start"],
        agent: str | None = None,
        *,
        repositories: RepositoryScan | None = None,
    ) -> Change | None:
        """A change that lowers protection waits for you (the dashboards are told); None when it lowers
        nothing, and may apply. Weighed under the lock: a caller holding it applies it in the same step.

        ``repositories``: the snapshot of the repositories seen, read before the lock (``repositories_for``), weighed
        with the projects of this process as they are now; None: read here (a server's space, which reads none). One
        not complete, for a change that could lower protection in a repository, raises RepositoriesUnchecked: nothing
        is applied, opened or kept as approved, and a change waiting stays as it is."""
        with self._lock:
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
                guidance_changes(base, config), destination_changes(base, config),
            )  # fmt: skip
            return self.mediator.approvals.open_change(change)

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
        saved (``update_config``), so the destinations are saved with it there."""
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
            aside = waiting is not None and self._held() is None  # what waits is kept apart: config.yaml holds it
            try:
                config = self.update_config(add, save=not aside)
            except SettingsSavedChangePending:
                self.wrapped.update(names)  # the destinations were saved and applied before the refresh failed
                raise
            self.wrapped.update(names)  # under the lock: a removal that comes next sees them in use
            if config is None or waiting is None or not aside:
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
                proposed = self.propose(again, waiting.source, waiting.agent, repositories=scan)
            except RepositoriesUnchecked:
                # The destinations are in use now; the change waiting stays as it is, asked again at a later reload.
                log.warning("a change waiting was not asked again: the repositories seen could not all be read now")
                return names
            if proposed is not None:  # it replaces the one waiting
                self.settings.save(again)  # config.yaml holds what waits for you
            else:  # it lowers nothing against the settings in use now: it applies, as a reload would
                self.apply_config(again, save=True)
                self.mediator.approvals.settle_change(waiting.id, "superseded")
            return names

    def decide_change(self, change_id: str, approve: bool) -> str:
        """``approved``, ``rejected``, ``gone`` (not waiting) or ``stale`` (the settings changed since: it
        would undo that). A change you reject is undone in config.yaml too."""
        with self._lock:  # no other change between the check and the apply
            approvals = self.mediator.approvals
            change = approvals.changes.get(change_id)
            if change is None:
                return "gone"
            if approve and change.base != digest(self.config):
                approvals.settle_change(change_id, "stale")
                return "stale"
            if approvals.settle_change(change_id, "approved" if approve else "rejected") is None:
                return approvals.settled.get(change_id, "gone")
            if approve:
                self.apply_config(change.config, save=True)
                return "approved"
            self.settings.save(self.config)
            return "rejected"

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
        summary: dict[str, Any] = {
            "ts": time.time(),
            "session": record.session,
            "project": record.project,
            "block": self.exchanges.block_id(record.sent),
            "kind": record.kind,
            "inline": record.inline,
            "findings": found,
            "failed": record.answer.failed,
            "block_asked": record.answer.block,
            "seconds": round(record.answer.seconds, 2),
        }
        summary["id"] = self.exchanges.record_review(summary)
        if self.recorder is not None:
            self._on_loop(self.recorder.broadcaster.publish, summary, "review")

    def forget_rejected_names(self) -> int:
        """Drop names the local model found that its current filters reject (they would stay masked)."""
        from latet.engine.detectors.ner import rejected_now
        from latet.engine.types import EntityClass

        stale = [
            e.id
            for e in self.built.vault.entities()
            if e.eclass == EntityClass.NER and rejected_now(e.etype, e.original)
        ]
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
        if self.recorder is not None:
            self.recorder.close()
        if self.exchanges is not None:
            self.exchanges.close()
        self.store.close()
        self.mediator.close()  # its worker threads end: none is left for the process to wait for
        if self.identity is None:  # a computer's: a helper still reading its repositories ended, never waited for
            repository_io.READER.stop()


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


def _key_vault(key: bytes, store: KeyVaultStore, new_values: NewValues, working: int, held: int) -> StoredVault:
    """An API key's vault: its newest ``working`` values in memory, every other one found in its store as a text needs
    it (``StoredVault``, R7). The values kept by a Latet before the store kept their anchor words (or kept them by an
    older version of the anchors) get them first, said once in the log when there were any; and the store counts the
    vault's values itself, for its limit, as the space no longer loads them all, and the generation its new fakes are
    drawn in (Task 11b: moved by each forget of values by themselves)."""
    filled = store.backfill_words()
    if filled:
        log.info("the words of %d values of an API key's vault were made, so that they are found in its texts", filled)
    store.count = store.count_entities()
    store.load_generation()
    return StoredVault(
        key, store, working=working, held_at_most=held, on_insert=new_values.keep, on_rule=store.save_rule,
        on_learn=new_values.learn,
        on_replace=store.replace_fake,
    )  # fmt: skip


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
) -> Runtime:
    """A Latet's runtime, over ``backends`` (None: this computer's, ``local_backends``); ``identity``: whose space it is
    on a Latet server (None on a computer), ``reading``: the bounds of the text recognitions the server's spaces share
    (its settings'; read only with ``identity``). Backends are given with ``root_key``, the key their vault is sealed
    with: never this computer's own, which another instance would not have. Without ``record`` nothing is recorded, not
    even in a history that is given. ``reviewer_key``: on a server, where the space's reviewer takes its key from;
    none given, it asks with none. A computer reads its own key as before. ``working``: how many values an API key's
    space keeps in memory (``StoredVault``; the server's ``key_working_values``), ``held``: the secrets it holds glued
    for good (``key_held_secrets``)."""
    from latet.server.logs import frames_only

    frames_only()  # an error this process logs is shown by its types and frames, never its words (daemon.log too)
    data = data or data_dir()
    if backends is None:
        # Read first, as before: a config.yaml that cannot be read stops the start before a key or a store is made.
        config = config or LocalSettings(config_file, data).load()
    elif root_key is None:
        raise ValueError("a runtime over given backends needs their root key (root_key), which seals their vault")
    from latet.storage.backup import RESTORE_JOURNAL, restoring

    if restoring(data):  # never on half a restore, whoever keeps the state: with no key yet, a new one would be made
        raise RuntimeError(f"a restore is under way ({data / RESTORE_JOURNAL}): finish it, or `latet restore --abort`")
    key = root_key if root_key is not None else load_root_key(data)  # given: the tutorial's sandbox, never saved
    if backends is not None:
        return _started(
            config, data, capture, record, config_file, key, backends, identity, reading, reviewer_key, ocr_unread,
            working, held,
        )  # fmt: skip
    backends = local_backends(data, config_file, key, record=record)
    try:
        return _started(
            config, data, capture, record, config_file, key, backends, identity, reading, reviewer_key, ocr_unread
        )
    except BaseException:  # the start failed: the databases opened here are closed here, not when the process ends
        backends.vault.close()
        if backends.history is not None:
            backends.history.close()
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
) -> Runtime:
    """The runtime ``make_runtime`` answers, over backends that are open (its own to close, or given)."""
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
    store = backends.vault
    new_values = NewValues(store)
    # A fake kept before the vault refused one that shows its value is drawn again as the vault loads (``on_replace``).
    vault: Vault
    if identity is not None and identity.kind == "key" and isinstance(store, KeyVaultStore):
        vault = _key_vault(key, store, new_values, working, held)
    else:  # a person's space, a computer, and a key's over a store that reads no vault by parts: all of it in memory
        vault = Vault(
            key, store.load_entities(), on_insert=new_values.keep, on_rule=store.save_rule, on_learn=new_values.learn,
            on_replace=store.replace_fake,
        )  # fmt: skip
    identity_config = resolve_identity(config, detect=not space)
    built = build(config, vault, identity_config, conversations=backends.conversations, given=backends.given)
    built.engine.rules_tag = digest(config)
    if space:  # one of many in its process's memory
        built.engine.bound(SPACE_CACHE_BYTES, SPACE_BLOCK_BYTES)
        built.verifier.shared = backends.conversations  # what the model wrote, for every instance

    projects = Projects(config, vault, identity_config, files=not space, seen=seen)
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
            built.engine, built.verifier, Approvals(backends.approvals, backends.approvals_feed), given=backends.given
        ),
        bridge_token=load_bridge_token(data) if not space else new_bridge_token(),
        recorder=recorder,
        unreadable=store.unreadable,
        aws=AwsCredentialStore(sessions=backends.aws),
        destinations=routes_of(config, personal),
        personal_names=personal,
        # Each name resolved for the identity counted for it: one identity's names hold none of another's lookups.
        pin=functools.partial(egress.pin, holder=identity.id) if identity is not None else None,
    )
    gateway.mediator.images = image_policy(config)  # type: ignore[union-attr]
    gateway.mediator.ocr = config.ocr == "auto"  # type: ignore[union-attr]
    gateway.mediator.vision = image_masking(  # type: ignore[union-attr]
        data, store, space=space, reading=reading, ocr_unread=ocr_unread
    )
    gateway.mediator.dump_guard = config.dump_guard  # type: ignore[union-attr]
    gateway.mediator.review = review_policy(config)  # type: ignore[union-attr]
    gateway.mediator.live_review = config.live_review  # type: ignore[union-attr]
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
    )
    runtime.use_reviewer(config)
    projects.on_held_back = runtime._held_back
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
            runtime.propose(found, "start", repositories=scan if scan is not None else unread)
    elif before is None:  # else what you approved before stays the snapshot, until settings apply
        runtime._reviewed(config)
    runtime.forget_rejected_names()
    runtime.warm_up()
    from latet.api.rest import Dashboard  # the dashboard needs the runtime it manages

    gateway.dashboard = Dashboard(runtime)
    return runtime
