"""The Enterprise Edition, a Latet server's alone (a computer without a server runs the Community Edition): the hooks
of latet.edition.Edition, acting as far as the license goes. E1 gives it its license, its settings and their secrets
(the server's PostgreSQL), says the license in /me, on the License page and in `latet server status`, and keeps the
runtimes that watch its floor. Each later part fills its hook: E2 floor (calling floor_changed when it changes), E3
provider_name, E4 keeps_events and record, E5 changes_go_to and live_review_goes_to, E6 synced_terms and shared_rules.
What protects asks licensed(), so that a trial's end changes none of it; what manages or decides for the company asks
serving() or manageable() (latet.ee.license).

``me()`` and ``checks()`` never raise for the license, the settings or their store: /me would answer 500 as a whole
and the dashboard would not open. A file that cannot be used, a store that fails, a count that fails are said in what
they answer (the standing ``invalid``, a line)."""

from __future__ import annotations

import inspect
import logging
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from starlette.routing import Route

from latet.ee.access import EE, UNSEEN
from latet.ee.license import (
    FEATURES,
    PUBLIC_KEYS,
    REFRESH_SECONDS,
    License,
    LicenseError,
    Licensing,
    Standing,
    State,
    describe,
    parse_license,
    summary,
)
from latet.ee.policy import FEATURE
from latet.ee.policy_sources import PolicySource, make_policy, policy_checks, policy_page
from latet.ee.roles import PAGES
from latet.ee.secrets import Secrets
from latet.ee.settings import EnterpriseSettings, EnterpriseSettingsStore
from latet.events import HIDDEN_PROJECT, make_event
from latet.floor import TERMS_UNAVAILABLE, AccountUnavailable, TermsUnavailable
from latet.logline import Paced
from latet.server.identities import IDENTITY_ID

if TYPE_CHECKING:
    from psycopg import Connection
    from starlette.routing import BaseRoute

    from latet.api.rest import Dashboard
    from latet.config import Config
    from latet.doctor.checks import Check
    from latet.edition import EditionContext
    from latet.ee.access import Access, Seen
    from latet.ee.approvals import CentralApprovals
    from latet.ee.approvals_api import ApprovalsApi
    from latet.ee.events.api import EventsApi
    from latet.ee.events.service import EventService
    from latet.ee.keys import KeysApi, KeySpace
    from latet.ee.listener import Listener
    from latet.ee.oidc import Oidc
    from latet.ee.people import People
    from latet.ee.people_api import PeopleApi
    from latet.ee.policy_api import PolicyApi
    from latet.ee.routes import EeRoute
    from latet.ee.scim import Scim, ScimTokens
    from latet.ee.server import DatabaseLicenses
    from latet.ee.sign_in_api import SignInApi
    from latet.ee.sync.api import TermSyncApi
    from latet.ee.sync.service import TermSync
    from latet.engine.detectors.base import Detector
    from latet.floor import Floor
    from latet.gateway.approvals import Central, Decides
    from latet.server.admin import AdminGate
    from latet.server.app import Server
    from latet.state import IdentityRef

log = logging.getLogger(__name__)

# A watcher as the edition keeps it: called, it gives the function to call, or None once the runtime it belongs to is
# gone (a runtime never closed is not kept alive by the edition it watches).
_Watcher = Callable[[], Callable[[], None] | None]
# A license store that fails fails at each look: said once a minute.
_UNREAD = Paced()
# E5's prune of the record of approvals among E4's jobs (R73): once an hour, under a lock of its own in Valkey.
APPROVALS_PRUNE_EVERY = 3600.0
APPROVALS_RETENTION_LOCK, APPROVALS_RETENTION_SECONDS = "approvals:retention", 600.0


class EnterpriseEdition:
    name: Literal["community", "enterprise"] = "enterprise"

    def __init__(
        self,
        context: EditionContext,
        *,
        keys: Mapping[str, bytes] | None = None,
        listen: bool = True,
        ticker: Callable[[], float] = time.monotonic,
    ) -> None:
        self.context = context
        server = context.server
        if server is None:  # before anything starts or is read: no computer is left half an Enterprise Edition
            raise ValueError(
                "the Enterprise Edition is a Latet server's: a computer without a server runs the Community Edition"
            )
        self._server: Server = server
        self._watchers: list[_Watcher] = []
        self._watching = threading.Lock()
        self._listener: Listener | None = None
        self._licenses: DatabaseLicenses | None = None
        self._copies: list[Callable[[], None]] = []  # what refresh() drops: the server's copies
        self.licensing: Licensing
        self.settings_store: EnterpriseSettingsStore
        self.secrets: Secrets
        # E3's objects, a server's: made by _e3_on_server, which _on_server calls. They stay None in the tests'
        # in-memory edition (tests/ee/editions.py), which replaces _on_server whole.
        self.people: People | None = None
        self.access: Access | None = None
        self.scim_tokens: ScimTokens | None = None
        self.oidc: Oidc | None = None
        self.scim: Scim | None = None
        self.people_api: PeopleApi | None = None
        self.keys_api: KeysApi | None = None
        self.key_space: KeySpace | None = None
        self.sign_in_api: SignInApi | None = None
        self.policy_api: PolicyApi | None = None
        # E4's, a server's: made by _e4_on_server, which _on_server calls after E3's, with nothing running. None in the
        # tests' in-memory edition; ``events`` None again once the edition is closed.
        self.events: EventService | None = None
        self.events_api: EventsApi | None = None
        # E5's, a server's: made by _e5_on_server, which _on_server calls after E4's. None in the tests' in-memory
        # edition.
        self.approvals: CentralApprovals | None = None
        self.approvals_api: ApprovalsApi | None = None
        # E6's, a server's: made by _e6_on_server, which _on_server calls after E5's, nothing running; started last.
        # None in the tests' in-memory edition.
        self.terms: TermSync | None = None
        self.terms_api: TermSyncApi | None = None  # its routes, which _e3_routes serves with E3's to E5's
        self._terms_closed = False  # set by close(): a closed server's edition gives no space its terms (B-M9)
        self._unapproved = Paced()  # who decides not read for /me: said once a minute
        self._unpeopled_said = Paced()  # the people's roles not read for /me, at each look: said once a minute
        self._unaccounted = Paced()  # a person's account not read for the name they are masked under: once a minute
        self._untold = Paced()  # what a person's visit is told (O6) not read for /me: said once a minute
        self._unlinked = Paced()  # whether a person is linked to the provider not read for /me: said once a minute
        self._unlisted = Paced()  # a person's computers not read for /me: said once a minute
        self._looking = threading.local()  # within one /me, the person's roles as it read them (``_seen``)
        try:
            self._on_server(server, PUBLIC_KEYS if keys is None else keys, listen, ticker)
            # Locked rules (E2): where the floor comes from; it calls floor_changed() when the floor changes. It reads
            # the server's PostgreSQL now: one that does not answer stops the instance's start. Made without a recorder
            # (R9): E4's license job records one `policy` event a change, whatever the number of instances.
            self.policy: PolicySource = make_policy(
                server,
                licensed=self._locks_rules,  # weighed at each look, never when the floor is asked
                changed=self.floor_changed,
            )
            self._weigh_now()
            if self.events is not None:  # E4's threads last, once the edition is whole (I7); no job in a one-shot's
                self.events.start(jobs=not context.one_shot)
            if self.terms is not None and not context.one_shot:  # a one-shot command's server serves nothing
                self.terms.start()  # E6's threads, once the edition is whole (I7)
        except BaseException:  # nothing it started runs on: a server's listener stops, and E4's service
            self.close()
            raise

    def _on_server(self, server: Server, keys: Mapping[str, bytes], listen: bool, ticker: Callable[[], float]) -> None:
        """A server's: PostgreSQL for what lasts, the same for every instance, and Valkey's messages for what another
        instance changed. A message only drops a copy, whatever it holds: what is read again is PostgreSQL's. Each
        later part adds its channel to the listener here (E2 ee:policy, E3 ee:people, E6 ee:terms). E3's objects are
        made before the listener starts (``_e3_on_server``)."""
        from latet.ee.listener import Listener
        from latet.ee.server import Counted, DatabaseLicenses, DatabaseSecrets, DatabaseSettings

        clock = self.context.clock
        licenses = DatabaseLicenses(server.db, clock=clock, ticker=ticker)
        # Asked by a weighing, outside the Licensing's lock; it never calls back into that Licensing.
        count = Counted(server.identities.enabled_count, ticker=ticker)
        settings = DatabaseSettings(server.db, server.bus, server.audit, clock=clock, ticker=ticker)
        self._licenses = licenses
        self.licensing = Licensing(licenses, clock=clock, count_users=count, keys=keys)
        self.settings_store = settings
        self.secrets = DatabaseSecrets(server.db, server.master, clock=clock)
        self._copies = [licenses.drop, count.drop, settings.drop]
        self._e3_on_server(server)  # before the listener: a failure there leaves none running
        self._e4_on_server(server)  # before the listener too, and nothing of it running before the edition is whole
        self._e5_on_server(server)  # before the listener too
        self._e6_on_server(server)  # before the listener too

        def license_changed(message: Mapping[str, Any]) -> None:
            licenses.drop()
            self._weigh_policy()  # whether the policy is in force is weighed again at once (R4)
            self._events_heard()  # E4: whether events are kept and sent, weighed again at once
            self._terms_heard()  # E6: a license installed or removed: the term sync looks now

        def settings_changed(message: Mapping[str, Any]) -> None:
            settings.drop()
            self._events_heard()  # E4: the outputs and how people appear, read again
            self._terms_heard()  # E6: the sources may have changed: the term sync looks now

        def identities_changed(message: Mapping[str, Any]) -> None:
            count.drop()
            self._identity_heard(message)  # E3: a person or a key removed or disabled, which no change of People's said

        if listen:
            self._listener = Listener(
                server.bus,
                {
                    "ee:license": license_changed,
                    "ee:settings": settings_changed,
                    "identities": identities_changed,  # a person or a key added, disabled, removed
                    # E2: a policy set or removed on an instance. Heard before the policy is made, it is read then.
                    "ee:policy": lambda message: self._weigh_policy(),
                    "ee:people": self._people_heard,  # E3: an instance changed people
                    # E6: another instance changed the synced terms or asked for a sync
                    "ee:terms": lambda message: self._terms_heard(),
                },
            )
            self._listener.start()

    def _e3_on_server(self, server: Server) -> None:
        """E3, a server's: its groups, roles, the control of its API keys and its people's accounts (PostgreSQL), and
        what E3's later tasks add below, in their order. Nothing here reads a store."""
        from latet.ee.access import Access
        from latet.ee.people import People  # psycopg: the server extra

        self.people = People(server.identities, server.db, server.bus, server.audit, clock=self.context.clock)
        self.access = Access(self.people, self.licensing)
        # Task 5: SCIM's tokens and single sign-on.
        from latet.ee.oidc import Oidc  # PyJWT: the server extra
        from latet.ee.scim import Scim, ScimTokens

        self.scim_tokens = ScimTokens(
            server.db,
            key=server.master.derive(b"credentials"),  # E0b's: every token kept by its keyed hash
            days=server.settings.admin_token_days,
            clock=self.context.clock,
        )
        self.oidc = Oidc(self, server, self.people, clock=self.context.clock)
        # Task 6: SCIM's users (and Task 7 its groups).
        self.scim = Scim(server, self.people, self.scim_tokens, self.licensing, clock=self.context.clock)
        # Task 8: people, groups and roles, for the dashboard and `latet server`.
        from latet.ee.people_api import PeopleApi

        self.people_api = PeopleApi(server, self.people, self.scim_tokens)
        # Task 9: API keys and their control, and a key's space for its controllers and its administrators.
        from latet.ee.keys import KeysApi, KeySpace

        self.keys_api = KeysApi(server, self.people, self.access)
        self.key_space = KeySpace(server, self.people, self.access, self.licensing)
        # Task 10: the Sign-in page (single sign-on's settings, its secret, SCIM's tokens) and the code to connect a
        # computer.
        from latet.ee.sign_in_api import SignInApi

        self.sign_in_api = SignInApi(self, server, self.people, self.oidc, self.scim_tokens)
        # Task 13: the Policy page's editor, for the security role (E2's policy, made after these, read per request).
        from latet.ee.policy_api import PolicyApi

        self.policy_api = PolicyApi(self, server)

    def _e3_routes(self) -> list[EeRoute]:
        """E3's routes of the People and groups, Sign-in and Policy pages, served to the dashboard and, but the policy's
        editor (`latet server policy` is E2's), to `latet server`; E4's, E5's and E6's after them."""
        apis = [
            self.people_api,
            self.keys_api,
            self.sign_in_api,
            self.policy_api,
            self.events_api,
            self.approvals_api,
            self.terms_api,
        ]
        return [route for api in apis if api is not None for route in api.routes()]

    def _e4_on_server(self, server: Server) -> None:
        """E4, a server's: this instance's events (the event log in PostgreSQL, the pipeline that writes what happens
        here, the jobs that run on one instance at a time) and the routes of Reports, the administration log and the
        SIEM outputs, which ``_e3_routes`` serves with E3's. Made as the edition starts (R17), the license weighed once
        here, nothing of it running: ``__init__`` starts it last (I7); closed first by ``close``."""
        from latet.ee.events.api import EventsApi
        from latet.ee.events.service import make_events  # psycopg: the server extra

        if self.people is None or self.access is None:
            return
        events = make_events(self, server)
        try:
            self.events_api = EventsApi(
                self, server, self.people, self.access, store=events.store, queue=events.store,
                refreshed=events.refreshed,
            )  # fmt: skip
        except BaseException:  # the edition does not start: nothing of E4 is left behind, its metrics bound to none
            events.close()
            raise
        self.events = events

    def _e5_on_server(self, server: Server) -> None:
        """E5, a server's: central approvals (who decides, the company's record, the deciders' routes, which
        ``_e3_routes`` serves with E3's and E4's), the record's prune among E4's jobs (once an hour, on one instance at
        a time: R73), and its metrics bound to this edition's record. Nothing here reads a store."""
        from latet.ee.approvals import ApprovalMetrics, make_approvals
        from latet.ee.approvals_api import ApprovalsApi

        if self.people is None:
            return
        central = self.approvals = make_approvals(self, server, self.people)
        self.approvals_api = ApprovalsApi(self, server, central)
        lock = server.bus.lock

        def prune() -> bool:
            # Valkey's lock, as E4's retention takes its own; never busy, so the thread waits its turn again.
            with lock(APPROVALS_RETENTION_LOCK, APPROVALS_RETENTION_SECONDS) as mine:
                if mine:
                    central.prune_now()
            return False

        prune.__qualname__ = "CentralApprovals.prune"  # as the jobs' thread names a job that failed
        if self.events is not None:  # E4's service, made before E5's on a server, its jobs not started yet
            self.events.jobs.add(APPROVALS_PRUNE_EVERY, prune)
        ApprovalMetrics.of(server.metrics).bind(central)  # last: only a complete record is read by the registry

    def _e6_on_server(self, server: Server) -> None:
        """E6, a server's: term sync (the synced terms in PostgreSQL, one live detector for every space of this
        instance, the schedule on a thread of its own, started last by __init__), its routes (``_e3_routes`` serves them
        with E3's to E5's), and its metrics bound to it. Nothing here reads a store."""
        from latet.ee.sync.api import TermSyncApi
        from latet.ee.sync.metrics import TermMetrics
        from latet.ee.sync.service import make_term_sync

        self.terms = make_term_sync(self, server)
        self.terms_api = TermSyncApi(self, server, self.terms)
        TermMetrics.of(server.metrics).bind(self.terms)  # last: only a complete term sync is read by the registry

    def _terms_heard(self) -> None:
        """E6: the synced terms, the license or the sources changed, or a sync was asked for, on this instance or
        another: the term sync's own thread looks now (nothing is read on the listener's thread)."""
        terms = self.terms
        if terms is not None:
            terms.wake()

    def _events_heard(self) -> None:
        """E4: the license or the Enterprise settings changed, on this instance or another: the settings' copy and
        the outputs are made again, and the pipeline weighs the license again (on the listener's thread, or a change's
        own). Nothing before E4's service is made, nor once it is closed."""
        events = self.events
        if events is not None:
            events.refreshed()
            events.pipeline.look()

    @property
    def server(self) -> Server:
        """The server this edition is of: every Enterprise Edition is a server's."""
        return self._server

    def _people_heard(self, message: Mapping[str, Any]) -> None:
        """An instance changed people (ee:people): this instance forgets the people the message names, or every copy
        (``People.heard``; the generations in Valkey, which every read checks, say it too), then every stream open in an
        API key's space here asks again whether it may stay open (O2), against what is read afresh. Nothing before
        People is made."""
        people = self.people
        if people is not None:
            people.heard(message)
        self._streams_ask()

    def _identity_heard(self, message: Mapping[str, Any]) -> None:
        """An identity added, changed, disabled, enabled or removed, a token revoked, a key rotated (Community's
        ``identities`` message, ``{id, event}``): this instance forgets that identity, and every copy that holds it as a
        key, or every copy when the message names no well-formed id; then the streams open in API keys' spaces ask
        again (gate 4, M1: forgotten first, so a woken stream reads afresh)."""
        people = self.people
        if people is not None:
            identity = message.get("id") if isinstance(message, Mapping) else None
            if isinstance(identity, str) and IDENTITY_ID.fullmatch(identity):
                people.forget(identity)
            else:
                people.drop()
        self._streams_ask()

    def _streams_ask(self) -> None:
        key_space = self.key_space
        if key_space is not None:
            key_space.heard()

    def _weigh_now(self) -> None:
        """The license weighed from the start: on a server, going over the users bought is noticed, and its grace
        started, at once rather than at the first look."""
        try:
            self.licensing.standing()
        except Exception as exc:  # the database not answering yet: weighed at the first look. Its words can name it
            log.warning("the license could not be weighed at start (%s): it is at the first look", type(exc).__name__)

    # --- the floor: E2 gives it, and calls floor_changed() when it changes -----------------------------------------

    def floor(self, identity: IdentityRef | None) -> Floor | None:
        return self.policy.floor()  # the same floor for every identity: people and API keys

    def provider_name(self, identity: IdentityRef | None) -> str | None:
        """E3's step 6b (decision (b) of 2026-10-25): the name of a person whose account is linked to the company's
        identity provider (SCIM manages it, or it holds the subject of their sign-in), as Latet keeps it
        (``Identity.name``: SCIM's, the ID token's at the account's creation, else an administrator's), while the
        floor is `exceptions: company` and lets PERSON through (a full name is an identity value of that type alone).
        The runtime puts it in use where the settings in use leave PERSON in clear everywhere (``runtime.named_in``):
        the person's own name (their settings', their computers') is then never left in clear in its place. None
        otherwise: no identity, an API key, the tests' in-memory edition, another floor, an account not linked. The
        floor's own refusal (``PolicyUnavailable``) as it is; People's database not answering is said in the log by its
        type and raised as ``AccountUnavailable`` (a ``FloorUnavailable`` whose 503 names the person's account, never
        the policy): never the person's own name in its place."""
        people = self.people
        if identity is None or identity.kind != "person" or people is None:
            return None
        floor = self.policy.floor()
        if floor is None or floor.exceptions != "company" or "PERSON" not in floor.company.allowed:
            return None
        try:
            account = people.account(identity.id)
        except Exception as exc:  # its words can name the database's address
            if count := self._unaccounted.due():  # each build and save asks: said once a minute
                log.warning(
                    "a person's account could not be read (%s): nothing is built or changed for them now%s",
                    type(exc).__name__,
                    f" ({count} times since the line before)" if count > 1 else "",
                )
            raise AccountUnavailable("a person's account could not be read") from None
        if account is None or not (account.subject is not None or account.scim_managed):
            return None
        return identity.name

    def _locks_rules(self) -> bool | None:
        """Whether the license switches locked rules on (``licensed``: never dates or counts, R1), as the policy weighs
        it at each look; None while this instance never read the license (its store not answering since it started),
        when a policy stored builds no space (L4)."""
        if self.licensing.licensed(FEATURE):
            return True
        return False if self.licensing.knows() else None

    def _weigh_policy(self) -> None:
        """The policy looked at again now (a license installed or removed: whether it is in force is weighed only in its
        looks, R4). Nothing before the policy is made."""
        policy: PolicySource | None = getattr(self, "policy", None)
        if policy is not None:
            policy.refresh()

    def watch(self, changed: Callable[[], None]) -> Callable[[], None]:
        """``changed()`` is called each time the floor changes, until the function this answers is called. On a server
        the runtime of every space on this instance watches the one edition. A runtime's method is kept by a weak
        reference: a runtime that is never closed (a space whose build failed after it was made) is not kept alive by
        the edition, and is told nothing once it is gone."""
        watcher: _Watcher
        if inspect.ismethod(changed):
            watcher = weakref.WeakMethod(changed)
        else:

            def watcher() -> Callable[[], None] | None:
                return changed

        with self._watching:
            self._alive()  # what is gone is dropped here too: a floor that never changes keeps nothing of it
            self._watchers.append(watcher)

        def stop() -> None:
            with self._watching:
                self._watchers = [kept for kept in self._watchers if kept is not watcher]

        return stop

    def floor_changed(self) -> None:
        """Tell every watcher that the floor changed. A watcher that fails is logged by its type, and the others are
        still told."""
        with self._watching:
            told = self._alive()
        for changed in told:
            try:
                changed()
            except Exception as exc:  # its words can quote a setting: said by its type
                log.warning("a runtime could not apply the new floor (%s): the others are told", type(exc).__name__)

    def _alive(self) -> list[Callable[[], None]]:
        """Under ``_watching``: the watchers whose runtime is gone dropped, and the functions of the others, each looked
        up once."""
        kept: list[_Watcher] = []
        alive: list[Callable[[], None]] = []
        for watcher in self._watchers:
            if (changed := watcher()) is not None:
                kept.append(watcher)
                alive.append(changed)
        self._watchers = kept
        return alive

    # --- the hooks the later parts fill -------------------------------------------------------------------------------

    def central(self) -> Central | None:
        return self.approvals

    def changes_go_to(self, identity: IdentityRef | None) -> Decides:
        return self.approvals.changes_go_to(identity) if self.approvals is not None else "self"

    def live_review_goes_to(self, identity: IdentityRef | None) -> Literal["self", "security"]:
        return self.approvals.live_review_goes_to(identity) if self.approvals is not None else "self"

    def keeps_events(self) -> bool:
        """Whether events are kept now (the license lists reports or the SIEM): in memory, never a read of the license
        or of PostgreSQL (the event loop asks it for every exchange)."""
        events = self.events
        return events is not None and events.pipeline.keeps()

    def record(self, event: Mapping[str, Any]) -> None:
        # E4 keeps them, at once: the pipeline's thread writes them (it drops every one while no license keeps them).
        # Never waits on PostgreSQL: a runtime records a refusal below the floor under its lock (E2's carry).
        events = self.events
        if events is not None:
            events.pipeline.put(event)

    def hidden_project(self, name: str) -> str:
        """E4: a project whose name holds a term its space masks, as its events name it: keyed with the server's events
        key (``Pseudonyms.project_of``), the same for every person and folder of that name (R3 of the final security
        review's re-review); one fixed code without E4's events. Reads the key the first time (PostgreSQL), in the
        recorder's thread: a key that cannot be read raises, and the runtime sends the fixed code."""
        events = self.events
        return events.pipeline.pseudonyms.project_of(name) if events is not None else HIDDEN_PROJECT

    def gaps(self) -> dict[str, Any] | None:
        """E4 (R3-1), for `latet server log verify`: what ``EventService.gaps`` says; None without E4's events. Raises
        when the log or the queues cannot be read: the route says so."""
        events = self.events
        return events.gaps() if events is not None else None

    def synced_terms(self, identity: IdentityRef | None) -> Detector | None:
        """E6: the terms the company syncs, one live detector for every space of this instance, loaded before the first
        is built (``TermsUnavailable`` while they or the license cannot be read: no space without them). None in the
        tests' in-memory edition alone: a server's edition once closed refuses (the design security review's B-M9)."""
        terms = self.terms
        if terms is None:
            if self._terms_closed:
                raise TermsUnavailable(TERMS_UNAVAILABLE)
            return None
        return terms.ready()

    def shared_rules(self, stored: Config) -> Mapping[str, Any]:
        """The rules for everyone in force from the shared settings: none. The company's rules for everyone are its
        policy (E2, `floor`); its synced terms reach every space beside the settings (`synced_terms`, E6). A row's kept
        rules stay kept."""
        return {}

    def permanent_exceptions(self) -> bool:
        """Whether a request exception may be made permanent on purpose (0.11's D3): allowed, as in the Community
        Edition, until a policy of the company's forbids them (the seam 0.11 leaves for it)."""
        return True

    def dashboard_routes(self, dashboard: Dashboard) -> list[BaseRoute]:
        from latet.ee.api import license_routes
        from latet.ee.policy_routes import policy_routes

        routes: list[BaseRoute] = [*license_routes(self, dashboard)]  # each later part adds its own to this list
        routes += policy_routes(dashboard, self.policy)  # E2: what the floor locks, as a person sees it
        if self.people is not None:  # E3's: people, groups, roles (and the later tasks' APIs), a server's
            from latet.ee.routes import dashboard_route  # psycopg: the server extra

            routes += [dashboard_route(route, dashboard, self) for route in self._e3_routes() if route.dashboard]
        if self.key_space is not None:  # an API key's space, for its controllers and its administrators
            space = self.key_space.served(dashboard)
            methods = ["GET", "POST", "PUT", "DELETE"]  # the dashboard's API's, as its catch-all takes them
            routes.append(Route(f"{EE}/keys/{{key}}/space/{{rest:path}}", space, methods=methods))
        # Each later part adds its routes to `routes` above these lines, and its rows to RULES (ee/access.py).
        if self.access is None:  # the tests' in-memory edition (R22): no E3 object, so no route of E3 either
            return routes
        return self.access.gated(routes, run=dashboard._in_pool)

    def server_routes(self, server: Server) -> list[BaseRoute]:
        # E1 has none outside the administration; E3 single sign-on's and SCIM's (a server's, outside its
        # administration: R1).
        return [
            *(self.oidc.routes() if self.oidc is not None else []),
            *(self.scim.routes() if self.scim is not None else []),
        ]

    def admin_routes(self, server: Server, gate: AdminGate) -> list[BaseRoute]:
        from latet.ee.admin import admin_routes
        from latet.ee.policy_routes import policy_admin_routes
        from latet.ee.policy_server import ServerPolicy

        routes: list[BaseRoute] = [*admin_routes(self, server, gate)]  # each later part adds its own to this list
        if isinstance(self.policy, ServerPolicy):  # E2: the server's policy, set, shown and removed
            routes += policy_admin_routes(self, server, gate, self.policy)
        if self.people is not None:  # E3's, for `latet server` (R7): never in server_routes
            from latet.ee.routes import admin_route  # psycopg: the server extra

            routes += [admin_route(route, self, gate) for route in self._e3_routes() if route.admin]
        return routes

    def close(self) -> None:
        """The policy stops looking, nobody is told of the floor any more, and a server's listener stops (before the
        server's connections go). E5's metrics let go of its record first, then E6's term sync stops (its metrics let
        go, its threads stopped; no space is given terms after), then E4's events (R17): what waits is written, then
        its threads stop. Closing again does nothing more. People first: what still reads someone's roles or groups
        while the rest closes (E4's events written as they close) reads PostgreSQL alone, and creates nothing in Valkey
        (E3's release gates, fix round 2)."""
        people: People | None = getattr(self, "people", None)  # none on a computer, or when making it failed
        if people is not None:
            try:
                people.close()
            except Exception as exc:  # by its type: the rest still closes
                log.warning("the people of this instance could not be closed cleanly (%s)", type(exc).__name__)
        central = self.approvals
        if central is not None:
            from latet.ee.approvals import ApprovalMetrics

            try:
                ApprovalMetrics.of(self._server.metrics).unbind(central)
            except Exception as exc:  # by its type: the rest still closes
                log.warning("the approvals' metrics of this instance could not be let go (%s)", type(exc).__name__)
        terms, self.terms, self._terms_closed = self.terms, None, True  # a space asked of a closed edition: refused
        # E6: its metrics let go, then its threads stop (a last run's event reaches E4's pipeline, closed after)
        if terms is not None:
            from latet.ee.sync.metrics import TermMetrics

            try:
                TermMetrics.of(self._server.metrics).unbind(terms)
            except Exception as exc:  # by its type: its threads still stop
                log.warning("the term sync's metrics of this instance could not be let go (%s)", type(exc).__name__)
            try:
                terms.close()
            except Exception as exc:  # by its type: the rest still closes
                log.warning("the term sync of this instance could not be closed cleanly (%s)", type(exc).__name__)
        events, self.events = self.events, None
        if events is not None:
            try:
                events.close()
            except Exception as exc:  # by its type: the rest still closes
                log.warning("the events of this instance could not be closed cleanly (%s)", type(exc).__name__)
        policy: PolicySource | None = getattr(self, "policy", None)  # none when making it failed
        if policy is not None:
            policy.close()
        with self._watching:
            self._watchers = []
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.stop()

    # --- what E1 gives ------------------------------------------------------------------------------------------------

    def refresh(self) -> None:
        """Read the license, the count of users and the settings again at the next look (this instance's copies), and
        the policy looked at again now, the license read anew."""
        for drop in self._copies:
            drop()
        self._weigh_policy()
        self._events_heard()  # E4: the license weighed again and the outputs made again, at once
        self._terms_heard()  # E6: the term sync looks now, at the license and the sources read anew

    def standing(self) -> Standing:
        """The license as it stands now. A store that fails is said as a license that cannot be used (``invalid``, the
        reason naming the error's type alone), never raised: /me, the License page and `latet server status` still
        answer."""
        try:
            return self.licensing.standing()
        except Exception as exc:
            reason = f"the server's database could not be read ({type(exc).__name__})"
            if count := _UNREAD.due():  # each /me asks: said once a minute, not at each
                log.warning(
                    "%s: said as one that cannot be used%s",
                    reason,
                    f" ({count} times since the line before)" if count > 1 else "",
                )
            return Standing(
                state=State.INVALID,
                license=None,
                users_in_use=None,
                days_left=None,
                over_since=None,
                reason=reason,
                notices=("invalid",),
                source="server",  # its line says the server's database
                reason_code="unreadable",
            )

    def told(self, standing: Standing, identity_id: str | None = None) -> Standing:
        """``standing`` as a person on the server is told it, in /me and on the License page. How many people and API
        keys the server has, and since when more than bought, are its administrators': told whole to whoever's roles
        allow license.manage (R19), and taken out for everyone else (no identity, roles that cannot be read, the tests'
        in-memory edition) before anything is made of it, so that the day a pause would start (``Standing.pauses_on``)
        is the license's end's alone, never the users' grace, which would give the day back. The notices stay."""
        seen = self._seen(identity_id)
        whole = seen is not None and seen.allows("license.manage")
        return standing if whole else replace(standing, users_in_use=None, over_since=None)

    def _seen(self, identity_id: str | None) -> Seen | None:
        """What ``identity_id`` may do (``Access.seen``): None for no identity and in the tests' in-memory edition;
        ``UNSEEN`` when People's database or Valkey cannot be read (said by its type, once a minute: never raised, R17).
        Within one /me, the one read ``me()`` made for the whole answer."""
        if self.access is None or identity_id is None:
            return None
        held = getattr(self._looking, "seen", None)
        if held is not None and held[0] == identity_id:
            return held[1]
        try:
            return self.access.seen(identity_id)
        except Exception as exc:  # its words can name the database's or Valkey's address
            self._unpeopled(exc)
            return UNSEEN

    def me(self, identity: IdentityRef | None, acting: str | None = None) -> dict[str, Any]:
        """The edition in /me: the person's roles read once for the whole answer (roles, actions, the pages, the
        headcount), in this thread, and nothing of them kept after it. ``acting``: in an API key's space, the person
        working there (E5: never named among who decides on what they make there)."""
        identity_id = identity.id if identity is not None else None
        self._looking.seen = (identity_id, self._seen(identity_id))
        try:
            return self._me(identity, acting)
        finally:
            self._looking.seen = None

    def _me(self, identity: IdentityRef | None, acting: str | None = None) -> dict[str, Any]:
        standing = self.told(self.standing(), identity.id if identity is not None else None)
        lic = standing.license
        answer: dict[str, Any] = {
            "name": self.name,
            "license": summary(standing),
            "features": [f for f in FEATURES if lic is not None and f in lic.features],
            "pages": ["license"] if self._license_page(standing) else [],  # nothing Enterprise shows without one
        }
        answer["roles"], answer["actions"] = self._access(identity)  # E3 gives them here, before any page weighs them
        answer["policy"] = self.policy.summary()  # E2: where the floor comes from and what is wrong, or None
        # Each later part adds its keys and its actions above this line; the pages listed below weigh them.
        if policy_page(identity, answer, self.licensing.licensed(FEATURE), reads=self._reads(identity, "policy.read")):
            answer["pages"] = [*answer["pages"], "policy"]
        if self.events is not None and identity is not None:  # E4: E3's filter keeps each for the roles that read it
            pages = [*answer["pages"]]
            if self.licensing.licensed("reports"):
                pages += ["reports", "audit"]
            if self.licensing.licensed("siem"):
                pages.append("siem")  # M13: the SIEM outputs' page, for a license with the SIEM and no reports too
            answer["pages"] = pages
        if self.terms is not None and identity is not None and self.licensing.licensed("term-sync"):
            answer["pages"] = [*answer["pages"], "term-sync"]  # E6: E3's filter keeps it for terms.manage (PAGES)
        # Each later part adds its pages above this line; a page no role decides (E5's) is added after it.
        answer["pages"] = self._pages(identity, answer["pages"])
        if "reports" in answer["pages"] and not self._reads_reports(identity):  # it would answer 403 (M13)
            answer["pages"] = [page for page in answer["pages"] if page != "reports"]
        self._approvals_in(identity, answer, acting)  # E5: a page no role decides, after E3's filter (R12)
        if (connected := self._connected(identity)) is not None:  # O6: told until the person says they saw it
            answer["connected"] = connected
        if (computers := self._computers(identity)) is not None:  # F4: the person's computers, always listed
            answer["computers"] = computers
        if (linked := self._linked(identity)) is not None:  # H1: whether Link my company account has anything to do
            answer["linked"] = linked
        return answer

    def _approvals_in(self, identity: IdentityRef | None, answer: dict[str, Any], acting: str | None = None) -> None:
        """/me's ``approvals`` (who decides on this identity's changes and texts; in an API key's space never
        ``acting``, the person working there, who never approves what they make there) and, for a person who decides
        something (the security team, a key's controllers, the admins), the Company approvals page: added after E3's
        page filter, which would drop it for a controller (no role says so). Never raises (R17): nothing is said of it
        then, and when the roles could not be read."""
        central = self.approvals
        if central is None or identity is None or self._seen(identity.id) is UNSEEN:
            return
        try:
            routes = central.me(identity, acting)
            decides = identity.kind == "person" and central.decides_anything(identity)
        except Exception as exc:  # its words can name the database's address
            if count := self._unapproved.due():
                log.warning(
                    "who decides on approvals could not be read for /me (%s): nothing is said of it%s",
                    type(exc).__name__,
                    f" ({count} times since the line before)" if count > 1 else "",
                )
            return
        answer["approvals"] = routes
        if decides:
            answer["pages"] = [*answer["pages"], "company-approvals"]

    def _reads_reports(self, identity: IdentityRef | None) -> bool:
        """Whether the person sees any report (``report_viewer`` answers): a group lead in no group sees none (M13).
        The roles are those ``me()`` read once for the whole answer (``_seen``); the person's groups are read here only
        when the roles alone do not answer, and when they cannot be, the Reports page is not listed: never raised
        (R2-7, R17)."""
        from latet.ee.events.reports import report_viewer

        if identity is None or self.people is None:
            return True  # E3's filter decided
        seen = self._seen(identity.id)
        if seen is None:
            return True
        roles = set(seen.roles)
        if report_viewer(roles=roles, member_of=(), people="group-only") is not None:
            return True  # every report: no group needed
        try:
            member_of = [group.id for group in self.people.groups_of(identity.id)]
        except Exception as exc:  # its words can name the database's address
            self._unpeopled(exc)
            return False
        return report_viewer(roles=roles, member_of=member_of, people="group-only") is not None

    def _linked(self, identity: IdentityRef | None) -> bool | None:
        """Whether the person's account holds the subject of their company sign-ins (``People.account``): the person's
        own, in /me, for the dashboard's **Link my company account** to be offered to whoever it is not linked for, and
        to no one else; never what links it. None for no identity, an API key (it has no account) and the tests'
        in-memory edition; while the license does not serve sso (no sign-in to link: nothing is read for it at each
        /me); and when it cannot be read (said by its type once a minute: never raised, R17): the dashboard then offers
        nothing it cannot be sure of."""
        people = self.people
        if people is None or identity is None or identity.kind != "person":
            return None
        try:
            if not self.licensing.serving("sso"):
                return None
            account = people.account(identity.id)
        except Exception as exc:  # its words can name the database's address
            if count := self._unlinked.due():  # each /me asks: said once a minute, not at each
                log.warning(
                    "whether a person is linked to the company's sign-in could not be read for /me (%s): nothing is "
                    "offered for it%s",
                    type(exc).__name__,
                    f" ({count} times since the line before)" if count > 1 else "",
                )
            return None
        return account is not None and account.subject is not None

    def _connected(self, identity: IdentityRef | None) -> dict[str, Any] | None:
        """The computers connected as the person and the codes made for them, for 14 days or until they say they saw
        them (``SignInApi.connected``, O6), read alone; while the license serves roles or sso (the People page's code
        for someone, one's own Sign-in page's). None for anyone else, for nothing to tell, and when it cannot be read
        (said by its type once a minute: never raised, R17)."""
        sign_in_api = self.sign_in_api
        if sign_in_api is None or identity is None or identity.kind != "person":
            return None
        try:
            if not (self.licensing.serving("roles") or self.licensing.serving("sso")):
                return None
            return sign_in_api.connected(identity.id)
        except Exception as exc:  # its words can name the database's address
            if count := self._untold.due():
                log.warning(
                    "the computers connected and the codes made for a person could not be read for their visit (%s): "
                    "told at a later one%s",
                    type(exc).__name__,
                    f" ({count} times since the line before)" if count > 1 else "",
                )
            return None

    def _computers(self, identity: IdentityRef | None) -> list[dict[str, Any]] | None:
        """The computers connected to the person's account (``SignInApi.computers``, the final review's F4), for their
        own dashboard to list for good, while the license serves roles or sso as the notice of ``_connected`` is. None
        for anyone else (an API key has computers of none) and when it cannot be read (said by its type once a minute:
        never raised, R17); an empty list when none is connected."""
        sign_in_api = self.sign_in_api
        if sign_in_api is None or identity is None or identity.kind != "person":
            return None
        try:
            if not (self.licensing.serving("roles") or self.licensing.serving("sso")):
                return None
            return sign_in_api.computers(identity.id)
        except Exception as exc:  # its words can name the database's address
            if count := self._unlisted.due():
                log.warning(
                    "the computers connected to a person's account could not be read for their visit (%s): listed at "
                    "a later one%s",
                    type(exc).__name__,
                    f" ({count} times since the line before)" if count > 1 else "",
                )
            return None

    def _access(self, identity: IdentityRef | None) -> tuple[list[str], list[str]]:
        """The roles of ``identity`` and the actions they give it, which /me says and the pages listed after them weigh
        (E3, ee/access.py). None for no identity (a server's /me always names its own) and in the tests' in-memory
        edition. Never raises (R17): /me would answer 500 and the dashboard would not open."""
        seen = self._seen(identity.id if identity is not None else None)
        if seen is None:
            return [], []
        return sorted(role.value for role in seen.roles), sorted(seen.actions)

    def _reads(self, identity: IdentityRef | None, action: str) -> bool | None:
        """Whether ``identity`` reads what ``action`` covers (everyone while the license has no roles, but ROLES_ONLY);
        None in the tests' in-memory edition (E2's own check on the actions then), False for no identity or when People
        cannot be read."""
        if self.access is None:
            return None
        seen = self._seen(identity.id if identity is not None else None)
        return seen is not None and seen.reads(action)

    def _pages(self, identity: IdentityRef | None, pages: list[str]) -> list[str]:
        """The pages the person reads of those listed above (E3's filter: the last lines of /me but for a page no role
        decides, as E5's company-approvals). For no identity, and in the in-memory edition, as they were listed; when
        People cannot be read, only the pages PAGES does not name."""
        seen = self._seen(identity.id if identity is not None else None)
        if self.access is None or identity is None or seen is None:
            return pages
        if seen is UNSEEN:
            return [page for page in pages if page not in PAGES]
        return self.access.pages(identity, pages, sign_in_ready=self._sign_in_ready(), seen=seen)

    def _sign_in_ready(self) -> bool:
        """Whether single sign-on can be used now (the Sign-in page's way in), False when it cannot be told: never
        raised (R17)."""
        try:
            return self.oidc is not None and self.oidc.ready()
        except Exception as exc:  # its words can name the database's address
            self._unpeopled(exc)
            return False

    def _unpeopled(self, exc: Exception) -> None:
        if count := self._unpeopled_said.due():
            log.warning(
                "the people's roles could not be read (%s): no role, no action and no page of a role is told%s",
                type(exc).__name__,
                f" ({count} times since the line before)" if count > 1 else "",
            )

    def _license_page(self, standing: Standing) -> bool:
        """The License page is listed once a license is there, valid or not (so that one that cannot be used can be
        replaced). A server's database that does not answer (the standing ``standing()`` makes of it) says nothing of
        whether one is there: the page is listed only when the last copy this instance read held one, so that a blip
        makes no page appear on a server without a license, and takes none away from one with a license."""
        if standing.state is State.NONE:
            return False
        if standing.source == "server" and standing.reason_code == "unreadable":
            return self._licenses is not None and self._licenses.held_one()
        return True

    def checks(self) -> list[Check]:
        """The edition's lines of `latet server status`: the license, whether this instance hears the others, the
        policy, the sign-in (E3), the event log (E4), texts that go to the security team while no company sign-in
        opens them (E5, [D6]), and term sync on this instance (E6: ``term_sync_check``). The Enterprise settings are the
        server's administrators', who read them through its administration."""
        from latet.doctor.checks import WARN, Check
        from latet.ee.events.checks import check_events
        from latet.ee.events.siem import SecretElsewhere, secret_of
        from latet.ee.settings import SiemOutput

        out: list[Check] = []
        standing = self.standing()
        if standing.state is not State.NONE:
            status, detail = describe(standing)
            out.append(Check(status, "License", detail))
        if self._listener is None or not self._listener.alive():
            detail = (
                "this instance does not hear the other instances' changes on Valkey (the license, the Enterprise "
                f"settings, the people and API keys): it reads them again every {REFRESH_SECONDS:g} seconds"
            )
            out.append(Check(WARN, "Enterprise messages", detail))
        out += policy_checks(self.policy)
        if self.sign_in_api is not None and (line := self.sign_in_api.check()) is not None:  # E3: what is set up
            out.append(line)
        events = self.events
        if events is not None:

            def secret_state(output: SiemOutput) -> tuple[bool, bool]:
                try:
                    return secret_of(self.secrets, output) is not None, False
                except SecretElsewhere:
                    return False, True

            try:
                out += check_events(
                    queue=events.store, settings=self.settings(), standing=standing, secret_state=secret_state
                )
            except Exception as exc:
                out.append(Check(WARN, "Event log", f"its state cannot be read now ({type(exc).__name__})"))
        if self.approvals is not None:  # E5 [D6]: texts the security team decides on, which nobody can open
            try:
                from latet.ee.approvals_api import texts_open

                held = self.settings()
                # The license weighed as the route of texts weighs it (``live_review_goes_to``): without approvals,
                # texts go to whoever sends them, and none waits for the security team.
                if (
                    held.approvals.live_review == "security"
                    and self.licensing.licensed("approvals")
                    and not texts_open(held)
                ):
                    # What happens, by case (the review of Task 8, M2): a text opens only from a session the company's
                    # sign-in opened, whatever its settings say.
                    if held.sso is None:
                        detail = (
                            "the security team decides on the Live Reviewer's texts, but the company's sign-in is not "
                            "set up: a text opens only from a session it opened, so each waits unsent and nobody can "
                            "decide on it; set it up, with company changes asked from it (latet server sign-in set "
                            "--company-changes sso)"
                        )
                    else:
                        detail = (
                            "the security team decides on the Live Reviewer's texts, while the company's sign-in lets "
                            "any session change what applies to everyone: each text waits for a security person "
                            "signed in with it, and is never opened from a session `latet dashboard` opened; ask the "
                            "sign-in for those changes (latet server sign-in set --company-changes sso)"
                        )
                    out.append(Check(WARN, "Company approvals", detail))
            except Exception as exc:
                out.append(Check(WARN, "Company approvals", f"who decides cannot be read now ({type(exc).__name__})"))
        terms = self.terms
        if terms is not None:  # E6: how term sync goes on this instance (it never raises)
            from latet.ee.sync.api import term_sync_check

            if (line := term_sync_check(terms, self._server, self.policy)) is not None:
                out.append(line)
        return out  # each later part adds its lines before this one

    def settings(self) -> EnterpriseSettings:
        """The Enterprise settings in use (each part reads its section here)."""
        return self.settings_store.load()

    def install_license(
        self,
        data: bytes,
        *,
        identity: IdentityRef | None = None,
        actor: str | None = None,
        address: str | None = None,
        conn: Connection[Any] | None = None,
    ) -> License:
        """Check ``data`` and keep it as the server's license in use (LicenseError). ``identity``: who did it, when an
        identity did; ``actor`` and ``address``: who and from where, for the server's administration log.

        The license and its entry in the administration log are written in one transaction, so that both are kept or
        neither (an entry that cannot be written raises, nothing changed, nobody told), and every instance is told once
        it commits. Given ``conn``, a connection in the caller's transaction (the administration's), the license alone
        is written there: the caller writes its entry there, and calls ``committed`` once it commits."""
        server = self._server
        if conn is not None:
            return self.licensing.install(data, conn=conn)
        with server.db.transaction() as own:
            lic = self.licensing.install(data, conn=own)
            self._entry(server, "license.installed", lic, identity, actor, address, own)
        self.committed("license.installed", identity)
        return lic

    def remove_license(
        self,
        *,
        identity: IdentityRef | None = None,
        actor: str | None = None,
        address: str | None = None,
        conn: Connection[Any] | None = None,
    ) -> bool:
        """Take the server's installed license away; whether there was one. As ``install_license``: given ``conn``,
        nothing but the removal is written there (``held_license`` says, in that transaction, which license it
        removes)."""
        server = self._server
        if conn is not None:
            return self.licensing.remove(conn=conn)
        with server.db.transaction() as own:
            lic = self.held_license(own)
            removed = self.licensing.remove(conn=own)
            if removed:
                self._entry(server, "license.removed", lic, identity, actor, address, own)
        if removed:
            self.committed("license.removed", identity)
        return removed

    def _entry(
        self,
        server: Server,
        action: str,
        lic: License | None,
        identity: IdentityRef | None,
        actor: str | None,
        address: str | None,
        conn: Connection[Any],
    ) -> None:
        """A server's change of the license in the administration log, last in the transaction that makes it."""
        who = actor or (identity.id if identity is not None else "server")
        target = lic.id if lic is not None else None
        server.audit.record(who, action, target=target, details=license_details(lic), address=address, conn=conn)

    def held_license(self, conn: Connection[Any]) -> License | None:
        """On a server, the license PostgreSQL holds as the transaction of ``conn`` finds it, its row locked until that
        transaction ends (``DatabaseLicenses.held``): the one a removal written there removes, read past this instance's
        copy and weighed with nothing (no count of the users, so that reading it starts no grace). None where none is
        held or it cannot be verified (the entry then names none)."""
        licenses = self._licenses
        if licenses is None:
            return None
        data = licenses.held(conn)
        if data is None:
            return None
        try:
            return parse_license(data, self.licensing.keys)
        except LicenseError:
            return None

    def held_features(self, conn: Connection[Any]) -> frozenset[str]:
        """On a server, the features what protects keeps (``Licensing.licensed``) for the license PostgreSQL holds as
        the transaction of ``conn`` finds it, its row locked until that transaction ends as ``held_license`` locks it:
        the verified file's, else, for a file this instance cannot verify, those its installer verified
        (``Licensing.protected_in``). What a change of the license stops is weighed from them, read before the change
        is written (the final security review's L2): ``held_license`` names no license for such a file, while the
        policy it keeps in force stops with the change."""
        licenses = self._licenses
        if licenses is None:
            return frozenset()
        licenses.held(conn)  # the row locked first, as a change locks it: no other change lands before this one ends
        return self.licensing.protected_in(conn)

    def committed(self, action: str, identity: IdentityRef | None = None) -> None:
        """Once a server's change of the license (``action``: ``license.installed``, ``license.removed``) is committed:
        this instance's copies dropped, every other instance told on ee:license (a message that is lost is made up for
        within REFRESH_SECONDS), and the change recorded as an event."""
        from latet.ee.server import publish

        self.refresh()
        publish(self._server.bus, "ee:license", {"change": action})
        self.record(self._event(action.removeprefix("license."), identity))

    def _event(self, outcome: str, identity: IdentityRef | None) -> dict[str, Any]:
        return make_event(
            "license",
            clock=self.context.clock,
            identity=identity.id if identity is not None else None,
            kind=identity.kind if identity is not None else None,
            outcome=outcome,
        )


def license_details(lic: License | None) -> dict[str, Any]:
    """What a server's administration log says of a license installed or removed, beside its id (the entry's target):
    its users, its last day and whether it is a trial. Never the licensee."""
    if lic is None:
        return {}
    return {"users": lic.users, "expires": lic.expires.isoformat(), "trial": lic.trial}
