"""Term sync's routes (Latet Enterprise, on a server), E3's EeRoutes: served to `latet server terms` through the
administration's gate, and to the Term sync page through E3's gate for whoever terms.manage allows (Task 12). Each
change is written with its entry in the administration log in one transaction (Who.change), and carried out on every
instance once it commits. No answer holds a term but the gone list, nor a secret ever."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

import yaml
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from latet.config import load_yaml, yaml_error_at
from latet.ee.admin import MAX_SECTION
from latet.ee.routes import VIA, EeRoute, Refused, Who, company_sign_in, json_body
from latet.ee.secrets import value_problem
from latet.ee.server import DatabaseSettings, settings_in
from latet.ee.settings import (
    SOURCE_NAME,
    SOURCES_CHANGED,
    TERM_SYNC_SECRETS,
    EnterpriseSettings,
    EnterpriseSettingsError,
    LdapSource,
    TermSource,
    TermSyncSettings,
    destination_addresses,
    needs_secret,
    source_destinations,
    source_local,
    source_secret,
    terms_bound,
    with_section,
)
from latet.ee.sync.records import ago_words
from latet.ee.sync.service import FEATURE, SyncPaused, TermSync, trailing
from latet.ee.sync.store import NewerKey, StoredTerm

if TYPE_CHECKING:
    from latet.doctor.checks import Check
    from latet.ee.edition import EnterpriseEdition
    from latet.ee.people import Changing
    from latet.ee.policy_sources import PolicySource
    from latet.server.app import Server

log = logging.getLogger(__name__)
GONE_LISTED: Final = 1000  # the gone list's longest page; the total is said beside it
MOST_ETAG: Final = 128
_ID = re.compile(r"[0-9a-f]{24}")
_NAME = re.compile(SOURCE_NAME)
# R22, fix round 2's N2: sources added, or moved to other destinations, from the dashboard, for each person, as E4's
# outputs are counted (the administration's calls are not).
DESTINATIONS: Final = ("term-sync-destinations", 10, 3600.0)
BY_COMMAND_CODE: Final = "by-command"  # what the dashboard says in its own words, in its five languages
# Fixed words: a refusal never quotes what the request said (a name, a path, an address, a term, a secret).
BY_COMMAND: Final = (
    "a source at this computer (localhost, 127.0.0.1, [::1]) is set with `latet server terms sources set`, never from "
    "the dashboard"
)
CREDENTIAL_BY_COMMAND: Final = (
    "the secret of a source at this computer (localhost, 127.0.0.1, [::1]) is set with `latet server terms secret`, "
    "never from the dashboard"
)
TOO_MANY_DESTINATIONS: Final = (
    "the dashboard adds or moves 10 sources' destinations an hour at most for each person: try again later, or set "
    "them with `latet server terms sources set`"
)
NO_COUNT: Final = "the sources' destinations added or moved cannot be counted now: try again in a moment"
NO_SOURCE: Final = "no source has that name"
NO_SECRET: Final = "no secret is kept for that source"  # noqa: S105 - words
NEEDS_NONE: Final = "this source reads with no secret"
NEWER_KEY: Final = (
    "a term names a newer master key version than this server's: give this server that version, or run it where every "
    "version is held"
)
PAUSED: Final = "term sync is paused by the license: the terms synced so far stay masked"
# What the dashboard reads of the terms and of the sources' details (addresses, tenants, DNs, filters, exclusions) is
# read as the policy's text is: from a session the company's sign-in opened, while the sign-in settings ask for one.
READ_BY_COMPANY_SIGN_IN: Final = (
    "the terms are shown to a session your company's sign-in opened: sign in with your company account"
)
SOURCES_BY_COMPANY_SIGN_IN: Final = (
    "the sources are shown to a session your company's sign-in opened: sign in with your company account"
)
SIGN_IN_UNREAD: Final = "the sign-in settings cannot be read now: try again"
# A person's change names the sources it was made from (root's carry of Task 11's review, Minor 2): the page always
# sends their etag, so that a secret is sealed for the source as its setter read it, or not at all.
ETAG_NEEDED: Final = 'a change from the dashboard names the "etag" of the sources it was made from, as they were read'
TITLE: Final = "Term sync"  # its line in `latet server status`
NAMED_AT_MOST: Final = 5  # the failing sources a line names, then how many more
PAUSED_SAID: Final = "paused by the license: the terms synced so far stay masked"
NOT_LISTED: Final = "the license does not list term sync: the synced terms are not masked, and no source is read"
_SOURCES_BODY = 'the body is {"sources": [...]} or {"text": "<the section in YAML>"}, and the "etag" of those read'
_TEXT = "the text is the section term_sync in YAML: `sources:` and the list of sources"
_AN_ETAG = "etag is the etag of the sources the change was made from, as they were read"
_RUN_BODY = "give a source's name, or nothing for every source"
_REMOVE_BODY = "say which terms: their ids, a source, all, or those no key opens"
_A_NAME = "a source's name is lowercase letters, digits and hyphens, 40 at most"
_SECRET_BODY = 'the body is {"value": "<the secret>"}, and the "etag" of the sources read'  # noqa: S105 - words
_REMOVAL_BODY = 'the body is nothing, or {"etag": "<the etag of the sources read>"}'


def gone_view(term: StoredTerm) -> dict[str, Any]:
    return {"id": term.id, "source": term.source, "type": term.type, "value": term.value, "gone": term.gone}


def sources_change(old: Sequence[TermSource], new: Sequence[TermSource]) -> dict[str, Any]:
    """What a new list of sources changed, by the sources' names, and for each source changed the names of its fields
    that changed (the design security review's B-I3: "directory: filter, mapping"; a source of another kind: every field
    either kind has that differs); the entry's details, never a path, an address, a DN or any field's value."""
    before, after = {s.name: s for s in old}, {s.name: s for s in new}
    changed = sorted(name for name in after if name in before and after[name] != before[name])

    def fields(name: str) -> list[str]:
        was, now = before[name], after[name]
        names = {*type(was).model_fields, *type(now).model_fields}
        return sorted(field for field in names if getattr(now, field, None) != getattr(was, field, None))

    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": changed,
        "fields": {name: fields(name) for name in changed},
    }


def sources_etag(settings: TermSyncSettings) -> str:
    """The etag of the sources (as E4's ``siem_etag``), which a change made from them names: a digest of the section as
    JSON, its keys sorted (a source names its secret, never holds one)."""
    shown = json.dumps(settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(shown.encode("utf-8")).hexdigest()[:32]


def not_syncing(server: Server) -> list[str]:
    """In a worker thread: the problem said while instances alive say another release or edition than this one's
    (``instances_said``, as E5's ``not_routing``): one of a release before term sync, or without the Enterprise
    Edition, masks no synced term in the spaces it serves. A listing cut at its bound says "at least", or that one
    it does not show may not (``some_instances``). Valkey not answering says nothing (logged by its type)."""
    from latet import __version__
    from latet.server.app import instances_said, some_instances

    this = f"{__version__} {server.edition.name}"
    try:
        listed, whole = instances_said(server)
    except Exception as exc:  # its words can name Valkey's address
        log.warning("whether every instance masks the synced terms could not be read (%s)", type(exc).__name__)
        return []
    count = sum(1 for said in listed if said != this)
    if not count and whole:
        return []
    some = some_instances(count, whole)
    return [
        f"{some} not run this release of Latet with the Enterprise Edition: it masks no synced term in the spaces it "
        "serves; stop it, or replace it with this release"
    ]


def section_of(text: str) -> dict[str, Any]:
    """In a worker thread: the section ``term_sync`` a YAML text writes (`latet server terms sources set` sends its
    file, the dashboard its editor); 400 in fixed words for one that is not YAML (its line alone, never the text) or
    not the section's shape."""
    try:
        found = load_yaml(text)
    except yaml.YAMLError as exc:
        raise Refused(400, f"not valid YAML{yaml_error_at(exc, text)}") from None  # where, never what
    except RecursionError:
        raise Refused(400, "not valid YAML: it nests too deeply") from None
    if not isinstance(found, dict):
        raise Refused(400, _TEXT)
    return found


def _etag(body: dict[str, Any]) -> str | None:
    """The etag of the sources a change was made from, when the body names one (400 for one that is no etag); None
    when it names none."""
    if "etag" not in body:
        return None
    etag = body["etag"]
    if not (isinstance(etag, str) and 0 < len(etag) <= MOST_ETAG):
        raise Refused(400, _AN_ETAG)
    return etag


def _unchanged(conn: Any, etag: str | None) -> TermSyncSettings:
    """In a change's transaction: the settings' lock taken (every other change of the Enterprise settings waits for
    this one from here; reads never wait, E3's), then the sources as it reads them, refused 409 `sources-changed` when
    the change was made from another version of them (``etag``)."""
    conn.execute("LOCK TABLE ee_settings IN SHARE ROW EXCLUSIVE MODE")
    before = settings_in(conn).term_sync
    if etag is not None and etag != sources_etag(before):
        raise Refused(409, SOURCES_CHANGED, code="sources-changed")
    return before


def _checked(section: dict[str, Any]) -> list[TermSource]:
    """In a worker thread: the sources ``section`` sets, checked as a save checks them; 400 by place and kind of error,
    never what was given."""
    try:
        return with_section(EnterpriseSettings(), "term_sync", section).term_sync.sources
    except EnterpriseSettingsError as exc:
        raise Refused(400, f"not changed: {exc}") from None


def _rebound(old: Sequence[TermSource], new: Sequence[TermSource]) -> list[str]:
    """A-R7: a line for each source kept whose secret is bound otherwise now (where it goes or what it reads changed):
    its secret does not open for it until it is set again."""
    before = {s.name: s for s in old}
    return [
        f"{s.name}: its secret must be set again (`latet server terms secret {s.name}`): it is kept for where the "
        "source went and what it read"
        for s in new
        if s.name in before and needs_secret(s) and terms_bound(s) != terms_bound(before[s.name])
    ]


def _failing(configured: Sequence[dict[str, Any]], now: float) -> str | None:
    """The sources set up whose last run failed, by their names and codes alone (never a reason's words: a reader's
    error can quote a record), five at most, then how many more."""
    failed = [s for s in configured if isinstance(s.get("last"), dict) and s["last"].get("outcome") == "failed"]
    if not failed:
        return None
    said = [
        f"{s['name']}: failed {ago_words(now - s['last']['finished'])} ({s['last'].get('code') or 'no code'})"
        for s in failed[:NAMED_AT_MOST]
    ]
    if len(failed) > NAMED_AT_MOST:
        said.append(f"and {len(failed) - NAMED_AT_MOST} more failed")
    return ", ".join(said)


def unread_here(sources: Sequence[TermSource], sync: TermSync) -> list[str]:
    """The final review's M5: a line for the sources set up that this instance cannot read (LDAP without Latet's `ldap`
    extra), by their names alone: it leaves their turns to the instances that read them, and where none does they fail
    `not-installed` (``TermSync._takes``). Nothing while it reads every one."""
    names = [s.name for s in sources if not sync.installed(s.kind)]
    if not names:
        return []
    one = len(names) == 1
    return [
        f"{', '.join(names)}: this instance has no `ldap` extra, which LDAP needs: it leaves "
        f"{'the source' if one else 'these sources'} to the instances that have it, and where none has it "
        f"{'the source fails' if one else 'they fail'} `not-installed` (docs.latet.ai/enterprise/term-sync/)"
    ]


def _let_through(sources: Sequence[TermSource], policy: PolicySource) -> list[str]:
    """R30: a line for each source set up that maps a type the policy in force lets through (``Floor.company.allowed``,
    as ``EnterpriseEdition.provider_name`` reads it): synced terms are explicit lists, masked all the same. Nothing
    while the policy cannot be read (its own line says so)."""
    try:
        floor = policy.floor()
    except Exception:
        return []
    allowed = set(floor.company.allowed) if floor is not None else set()
    lines = []
    for source in sources:
        types = sorted({etype for etype in source.mapping.values() if etype in allowed})
        if types:
            lines.append(
                f"{source.name} maps {' and '.join(types)}, which the policy lets through: those terms are masked all "
                "the same"
            )
    return lines


def term_sync_check(sync: TermSync, server: Server, policy: PolicySource) -> Check | None:
    """`latet server status`'s line of term sync on this instance (``EnterpriseEdition.checks``), from
    ``sync.status()``: `fail` while some of the terms cannot be opened here (B-I2); `warn` while the terms trail the
    store for more than two ticks (B-I1), a source set up last failed, an instance does not mask them (``not_syncing``),
    the license does not list term sync or pauses it, or a source maps a type the policy lets through (R30); `ok` with
    the count kept otherwise. What is masked and how far it trails are left out until this instance first loaded the
    terms (``TermSync.behind``, as Task 10's metrics). None while no source is set up and the store keeps no term.
    Never raises: a failure is said by its type alone (its words can name the database). Names and codes, never a term,
    a secret, an address, a path, a DN or a filter."""
    from latet.doctor.checks import FAIL, OK, WARN, Check

    try:
        state = sync.status()
        configured = [s for s in state["sources"] if s["configured"]]
        kept, most = int(state["kept"]), int(state["most"])
        if not configured and not kept:
            return None
        problems: list[str] = []
        if unreadable := int(state.get("unreadable") or 0):
            them = "term cannot be opened on this instance: its master key lacks the version it was"
            if unreadable != 1:
                them = "terms cannot be opened on this instance: its master key lacks the version they were"
            problems.append(f"{unreadable:,} synced {them} sealed with; it masks the terms it had, or builds no space")
        behind = state.get("behind")
        if trailing(behind):
            problems.append(
                f"this instance masks the synced terms of version {behind['loaded']} while the server holds "
                f"{behind['store']}, for {int(behind['seconds']):,} seconds: it cannot read them again (its log says "
                "why)"
            )
        if not state["licensed"]:
            problems.append(NOT_LISTED)
        elif state["paused"] and configured:
            problems.append(PAUSED_SAID)
        if (failing := _failing(configured, sync.clock())) is not None:
            problems.append(failing)
        problems += unread_here(sync.sources(), sync)
        problems += not_syncing(server)
        if state["licensed"]:  # without term sync no synced term is masked: none "masked all the same" (review Minor 1)
            problems += _let_through(sync.sources(), policy)
    except Exception as exc:
        return Check(WARN, TITLE, f"its state cannot be read now ({type(exc).__name__})")
    if problems:
        return Check(FAIL if unreadable else WARN, TITLE, "; ".join(problems))
    count = len(configured)
    if count:
        return Check(OK, TITLE, f"{kept:,} of {most:,} terms kept from {count} source{'s' if count != 1 else ''}")
    return Check(
        OK,
        TITLE,
        f"{kept:,} of {most:,} terms kept, no longer in a source: they stay masked until they are removed (`latet "
        "server terms gone` lists them)",
    )


class TermSyncApi:
    def __init__(self, edition: EnterpriseEdition, server: Server, sync: TermSync) -> None:
        self.edition, self.server, self.sync = edition, server, sync

    def routes(self) -> list[EeRoute]:
        """`latet server terms`'s, through the administration's gate, and the Term sync page's, through E3's gate and
        its row in ee/access.py (``RULES``: terms.manage, the security role; nobody's in the dashboard without roles in
        the license, ``ROLES_ONLY``). The sources and their secrets are the security role's on the page too ([D4]), the
        spec's "terms" and E4's SIEM precedent, with E4's two guards (a loopback source the command's alone; a count of
        destinations)."""
        manage: dict[str, Any] = {"kind": "manage", "feature": FEATURE}
        return [
            EeRoute("GET", "/term-sync", self.status, **manage),
            EeRoute("GET", "/term-sync/gone", self.gone, **manage),
            EeRoute("POST", "/term-sync/run", self.run, answers=202, **manage),
            EeRoute("POST", "/term-sync/remove", self.remove, **manage),
            EeRoute("GET", "/term-sync/sources", self.show_sources, **manage),
            EeRoute("PUT", "/term-sync/sources", self.set_sources, **manage),
            EeRoute("PUT", "/term-sync/secrets/{source}", self.set_secret, **manage),
            EeRoute("DELETE", "/term-sync/secrets/{source}", self.remove_secret, **manage),
        ]

    @staticmethod
    def _person(who: Who) -> str | None:
        """Who made it, when a person did (a run's or a removal's event names them); None for the administration."""
        return who.identity_id if isinstance(who.identity_id, str) else None

    def _store(self) -> DatabaseSettings:
        store = self.edition.settings_store
        if not isinstance(store, DatabaseSettings):  # a server's edition keeps them in PostgreSQL
            raise Refused(503, "the Enterprise settings are a server's")
        return store

    def _sources_now(self) -> TermSyncSettings:
        """The sources as PostgreSQL has them now, never this instance's copy (seconds behind a change made on another
        instance): the etag a reader is given names the sources a change replaces."""
        with self.server.db.connection() as conn:
            return settings_in(conn).term_sync

    def _company_read(self, via: object) -> Refused | None:
        """In a worker thread: whether a session opened as ``via`` may read the terms or the sources' details
        (``company_sign_in``, as ``PolicyApi._company_read``), under the sign-in settings as PostgreSQL has them now,
        never this instance's copy, which may be seconds behind a change of ``company_changes`` made on another
        instance."""
        with self.server.db.connection() as conn:
            return company_sign_in(lambda: settings_in(conn), via)

    async def _read_by_company(self, request: Request, who: Who, words: str) -> None:
        """For a person (the administration's calls have no session): 403 `company-sign-in` in ``words`` to a session
        the company's sign-in did not open while the sign-in settings ask for one (the user's decision (c), read as the
        policy's text is); 503 in fixed words while those settings cannot be read (its words can name the database)."""
        if self._person(who) is None:
            return
        try:
            refused = await who.run(self._company_read, request.scope.get(VIA))
        except Exception as exc:
            log.warning("the sign-in settings could not be read for term sync's page (%s)", type(exc).__name__)
            raise Refused(503, SIGN_IN_UNREAD) from None
        if refused is not None:
            raise Refused(403, words, code="company-sign-in")

    # --- what is read -------------------------------------------------------------------------------------------------

    async def status(self, request: Request, who: Who) -> Response:
        state = await who.run(self.sync.status)
        unread = await who.run(lambda: unread_here(self.sync.sources(), self.sync))
        problems = [*unread, *await who.run(not_syncing, self.server)]
        return JSONResponse({**state, "problems": problems})

    async def gone(self, request: Request, who: Who) -> Response:
        """The terms no longer in their source (their values: the one answer that holds terms), for the administration
        and, from the page, for a session the company's sign-in opened while the sign-in settings ask for one."""
        await self._read_by_company(request, who, READ_BY_COMPANY_SIGN_IN)
        source = request.query_params.get("source")
        if source is not None and not _NAME.fullmatch(source):
            raise Refused(400, _A_NAME)
        items, total = await who.run(self.sync.gone, GONE_LISTED, source)
        return JSONResponse({"items": [gone_view(t) for t in items], "total": total})

    async def show_sources(self, request: Request, who: Who) -> Response:
        """The sources as PostgreSQL has them, never a secret, read from the page as the gone list is ([D4]): their
        addresses, tenants, DNs, filters and exclusions are the company's. ``where``: where each connects and the DN a
        directory signs in as, which the page's secret dialog says before it asks for the secret, as `latet server
        terms secret` does (the final review's M1)."""
        await self._read_by_company(request, who, SOURCES_BY_COMPANY_SIGN_IN)
        section = await who.run(self._sources_now)
        text = await who.run(
            lambda: yaml.safe_dump(section.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
        )
        where = {
            s.name: {"addresses": destination_addresses(s), "bind": s.bind if isinstance(s, LdapSource) else None}
            for s in section.sources
        }
        return JSONResponse(
            {
                "sources": [s.model_dump(mode="json") for s in section.sources],
                "text": text,
                "etag": sources_etag(section),
                "where": where,
            }
        )

    # --- a sync asked for ---------------------------------------------------------------------------------------------

    async def run(self, request: Request, who: Who) -> Response:
        body = await json_body(request, who)
        source = body.get("source")
        if set(body) - {"source"} or not (source is None or isinstance(source, str)):
            raise Refused(400, _RUN_BODY)
        by = self._person(who)

        def asked(within: Changing) -> list[str]:
            within.people = False  # names no person or group: People's copies stay (E5's carry)
            sources = settings_in(within.conn).term_sync.sources  # as this transaction reads them (Task 11)
            try:
                names = self.sync.ask(source, by=by, conn=within.conn, sources=sources)
            except SyncPaused:  # a defence: E1's gate (`held`, kind manage) refuses every such standing first, 403
                raise Refused(409, PAUSED, code="paused") from None
            except KeyError:
                raise Refused(404, NO_SOURCE) from None
            within.note("terms.sync.asked", "term_sync", {"source": source})
            within.after.append(self.sync.asked)  # every instance woken once it commits
            return names

        return JSONResponse({"asked": await who.change(asked)}, status_code=202)

    # --- the terms gone, removed --------------------------------------------------------------------------------------

    def _removal(self, body: dict[str, Any], who: Who) -> tuple[list[str] | None, str | None, bool]:
        """Which terms a removal names: ``{ids}``, or for the administration alone ``{source}``, ``{all: true}`` or
        ``{unreadable: true}``; 400 in fixed words otherwise."""
        if len(body) == 1:
            ids = body.get("ids")
            if (
                isinstance(ids, list)
                and len(ids) <= GONE_LISTED
                and all(isinstance(i, str) and _ID.fullmatch(i) for i in ids)
            ):
                return list(ids), None, False
            if self._person(who) is None:
                source = body.get("source")
                if isinstance(source, str) and _NAME.fullmatch(source):
                    return None, source, False
                if body.get("all") is True:
                    return None, None, False
                if body.get("unreadable") is True:
                    return None, None, True
        raise Refused(400, _REMOVE_BODY)

    async def remove(self, request: Request, who: Who) -> Response:
        ids, source, unreadable = self._removal(await json_body(request, who), who)
        by = self._person(who)

        def removed(within: Changing) -> dict[str, int]:
            within.people = False
            if unreadable:
                try:  # B-R5: the gone terms no key opens alone, never one a source still gives
                    counts = self.sync.remove_unreadable(conn=within.conn)
                except NewerKey:  # its words name the versions: fixed words here
                    raise Refused(409, NEWER_KEY, code="newer-key") from None
            else:
                counts = self.sync.remove(ids, source=source, conn=within.conn)
            if counts:  # the counts by type, never a term
                details: dict[str, Any] = {"count": sum(counts.values()), "by_type": counts, "source": source}
                if unreadable:
                    details["unreadable"] = True
                within.note("terms.removed", "term_sync", details)
                within.after.append(lambda: self.sync.removed(counts, source=source, by=by))
            return counts

        counts = await who.change(removed)
        return JSONResponse({"removed": sum(counts.values()), "by_type": counts})

    # --- the sources --------------------------------------------------------------------------------------------------

    def _counted(self, sources: Sequence[TermSource], who: Who) -> None:
        """R22 (fix round 2's N2), as E4's ``EventsApi._counted``: a person's sources added, or whose destinations
        changed, against the sources as they are now, each that connects somewhere counted against ten an hour
        (``Bus.allow``); 429 past them. Asked before the save's transaction: a slow Valkey never holds the settings'
        lock, and one that does not answer is 503 in fixed words."""
        was = {s.name: s for s in self._sources_now().sources}
        moved = [
            s
            for s in sources
            if source_destinations(s)
            and (s.name not in was or source_destinations(was[s.name]) != source_destinations(s))
        ]
        if not moved:
            return
        kind, most, per = DESTINATIONS
        try:
            allowed = all(self.server.bus.allow(f"{kind}:{who.actor}", most, per) for _ in moved)
        except Exception as exc:  # by its type: Valkey's words can name its address
            log.warning("the term sync's destinations could not be counted (%s)", type(exc).__name__)
            raise Refused(503, NO_COUNT) from None
        if not allowed:
            raise Refused(429, TOO_MANY_DESTINATIONS)

    async def set_sources(self, request: Request, who: Who) -> Response:
        body = await json_body(request, who)
        if await who.run(lambda: len(json.dumps(body)) > MAX_SECTION):
            raise Refused(413, "the request is too large")
        if set(body) - {"sources", "text", "etag"} or ("sources" in body) == ("text" in body):
            raise Refused(400, _SOURCES_BODY)
        etag = self._etag_of(body, who)
        if "text" in body:
            if not isinstance(body["text"], str):
                raise Refused(400, _SOURCES_BODY)
            section = await who.run(section_of, body["text"])
        else:
            if not isinstance(body["sources"], list):
                raise Refused(400, _SOURCES_BODY)
            section = {"sources": body["sources"]}
        store = self._store()
        sources = await who.run(_checked, section)
        person = self._person(who) is not None
        if person:  # counted before the transaction: never in Valkey under the settings' lock
            await who.run(self._counted, sources, who)

        def changed(within: Changing) -> tuple[dict[str, Any], list[str], str]:
            conn = within.conn
            before = _unchanged(conn, etag)
            was, now = {s.name: s for s in before.sources}, {s.name: s for s in sources}
            # A source at this computer is the command's alone: a person neither adds, changes nor takes one out (Task
            # 13's review), nor keeps its name for a source elsewhere or of another kind (the final review's I1: its
            # terms would become gone, listed and removable from the page); the page may change the others while it
            # keeps that one exactly as it is.
            if person and (
                any(source_local(s) and was.get(s.name) != s for s in sources)
                or any(source_local(s) and now.get(s.name) != s for s in before.sources)
            ):
                raise Refused(403, BY_COMMAND, code=BY_COMMAND_CODE)
            after = store.set_section(
                "term_sync", section, actor=within.actor, address=within.address, conn=conn
            ).term_sync  # with_section checks it, terms_problem included
            change = sources_change(before.sources, after.sources)
            self.sync.retire([s.name for s in after.sources], conn=conn)  # a source taken out: its terms gone
            # Its last run, and that of a source changed or set up again, forgotten with it (the final review's M4).
            self.sync.forget([*change["added"], *change["removed"], *change["changed"]], conn=conn)
            within.people = False
            within.note("terms.source.changed", "term_sync", change)
            within.after.append(lambda: store.changed("term_sync"))  # every instance told once it commits
            within.after.append(self.sync.changed)
            return change, _rebound(before.sources, after.sources), sources_etag(after)

        try:
            change, rebound, now = await who.change(changed)
        except EnterpriseSettingsError as exc:  # by field and kind of error, never what was given
            raise Refused(400, f"not changed: {exc}") from None
        # The sources this instance cannot read (the final review's M5), then the instances that mask no synced term.
        problems = [*rebound, *await who.run(unread_here, sources, self.sync), *await who.run(not_syncing, self.server)]
        return JSONResponse({**change, "problems": problems, "etag": now})

    # --- a source's secret --------------------------------------------------------------------------------------------

    def _etag_of(self, body: dict[str, Any], who: Who) -> str | None:
        """The etag of the sources a change was made from (``_etag``): a person's change names one (400 otherwise:
        the page always sends it, root's carry of Task 11's review, Minor 2); the administration's may leave it out,
        a blind write as the other administration routes allow."""
        etag = _etag(body)
        if etag is None and self._person(who) is not None:
            raise Refused(400, ETAG_NEEDED)
        return etag

    @staticmethod
    def _named(request: Request) -> str:
        name = request.path_params["source"]
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise Refused(404, NO_SOURCE)  # never said back
        return name

    async def set_secret(self, request: Request, who: Who) -> Response:
        """``{value, etag}``: the secret of one source kept, sealed for where it goes and what it reads (B-I3), never
        answered, never recorded. Given the etag of the sources it was set from (a person's change always names it),
        refused 409 `sources-changed` when they changed since (the review's Minor 2): sealed for the source as its
        setter read it, or not at all. A sync of the source is asked for in the same transaction, with its entry (root's
        ruling of 2026-11-06): one that failed `no-secret` runs at once, not at its next try up to 15 minutes later."""
        name = self._named(request)
        body = await json_body(request, who)
        if "value" not in body or set(body) - {"value", "etag"}:
            raise Refused(400, _SECRET_BODY)
        etag, value = self._etag_of(body, who), body["value"]
        if (problem := value_problem(value)) is not None:
            raise Refused(400, problem)
        if not str(value).strip():
            raise Refused(400, "a secret is not empty")
        by = self._person(who)

        def kept(within: Changing) -> None:
            conn = within.conn
            sources = _unchanged(conn, etag).sources
            source = next((s for s in sources if s.name == name), None)  # sealed for it
            if source is None:
                raise Refused(404, NO_SOURCE)
            if not needs_secret(source):
                raise Refused(409, NEEDS_NONE)
            if by is not None and source_local(source):  # such a source's secret is the command's alone, as it is
                raise Refused(403, CREDENTIAL_BY_COMMAND, code=BY_COMMAND_CODE)
            within.people = False
            self.edition.secrets.save(source_secret(source), value, bound=terms_bound(source), conn=conn)
            try:  # the sources as this transaction reads them, as the run route's ask
                self.sync.ask(name, by=by, conn=conn, sources=sources)
            except SyncPaused:  # a defence: E1's gate refuses every such standing first; it runs once term sync can
                pass
            else:
                within.note("terms.sync.asked", "term_sync", {"source": name})
                within.after.append(self.sync.asked)  # every instance woken once it commits
            # Last: on the administration API the last entry is the call's own, with its status (``admin_route``).
            within.note("terms.secret.changed", "term_sync", {"saved": [source.name], "removed": []})

        await who.change(kept)
        return JSONResponse({"saved": name})

    async def remove_secret(self, request: Request, who: Who) -> Response:
        """The secret of one source removed; a name no source has may be removed too (a secret left by a source taken
        out). 404 when none is kept. Its body names the etag of the sources read, as the secret's PUT does (a person's
        always)."""
        name = self._named(request)
        body = await json_body(request, who)
        if set(body) - {"etag"}:
            raise Refused(400, _REMOVAL_BODY)
        etag = self._etag_of(body, who)
        person = self._person(who) is not None

        def removed(within: Changing) -> None:
            conn = within.conn
            source = next((s for s in _unchanged(conn, etag).sources if s.name == name), None)
            if person and source is not None and source_local(source):
                raise Refused(403, CREDENTIAL_BY_COMMAND, code=BY_COMMAND_CODE)
            secret = source_secret(source) if source is not None else f"{TERM_SYNC_SECRETS}{name}"
            if not self.edition.secrets.remove(secret, conn=conn):
                raise Refused(404, NO_SECRET)
            within.people = False
            within.note("terms.secret.changed", "term_sync", {"saved": [], "removed": [name]})

        await who.change(removed)
        return JSONResponse({"removed": name})
