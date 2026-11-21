"""The scan of ``latet leak``: the secrets of the agents' sessions, grouped by value, never shown.

Each distinct text is read once by the user's own detectors (``build_masker`` with a scratch vault: Latet's real vault
learns nothing), for what ``Masker.mask`` would replace in it and by which policy (``Masker.planned``), and on its own:
the scratch vault learns nothing either, so no text is read for the values or the fakes of those before it (every
text read for all of them, and for their fakes, was nearly the whole cost of a long scan), and the findings are the
same whatever order the sessions are read in. A value found in one text by what is written around it (``password:``)
and written bare in another is found in the first alone. The local name model is left out: it reads none of a tool's
results, the kind each text is read as. Half of a pair of UTF-16 surrogates alone, which a JSON string may hold and the
detectors cannot read, is read as the replacement character, one for one, so that the text's other characters keep
their places; a text the detectors fail on all the same is said once, however many copies of it there are. What the
scan remembers of a text it read is a digest of it, never the text.

The values live in a dictionary of this module's ``scan`` alone, keyed by the value alone, to put one secret pasted in
three sessions on one line whatever types it was found as (a JWT in a curl option's header and bare, a GitHub token in
a curl option's header and assigned to a name): the type shown is a vendor's (``vendor``) first, then the one it was
found as in the most texts, then the first, and its hint is that type's; ``Finding.types`` holds them all. What leaves
is a ``Finding`` with a type, a count and a hint (the typed public prefix and the length), and no part of a value beyond
that prefix. Each of its lists (in clear, replaced, redacted) has the agents and the folders of its own sessions.

With what Latet's own vault and history say of a value (``Known``), each session it is in counts as replaced by Latet
or as in clear. Each comparison of times allows ``SINCE_SLACK``: a transcript writes the prompt or the tool's result
down a moment before the request that carries it reaches Latet, which keeps the value and records the request then.

* A session the history names (``Known.masked_in``, with the first request it shows the value masked in there) is
  replaced and confirmed when it held the value from that request on. A value forgotten in the vault and met again has
  a newer entry, and the history still names the sessions before. One that held it since well before: where the history
  sees back to that session's start (``Known.history_horizon``, its oldest request still kept) and shows no masking
  then, it sent the value in clear first (Latet put in front of its agent halfway, a transcript sent again), and is in
  clear whatever the vault's date; where its first requests are older than the history keeps (14 days by default), the
  vault's date tells, as for any other session, and it is not confirmed.
* Any other session is replaced, if it went through Latet, from the vault's first sight of the value on
  (``Known.since``); before it, or with no time, in clear.

A secret of a type the user's settings redact, or one longer than a vault keeps (``VALUE_LONGEST``), is one Latet
replaced with a marker and kept nowhere, so the vault has no date for it (a value found as several types is one where
every one of them is: found as one the settings mask, it is masked, and the vault keeps it). Its sessions from the
earliest date Latet has on this computer on (``Known.latet_since``) are ``Finding.redacted``: never replaced, never in
clear; those before it, Latet not there yet, or with no time to place, in clear. Where the vault does know it (masked
once, before the type was redacted), the vault's date stands in for Latet's, and the sessions the history names are
replaced and confirmed as above. Without Latet's answers (a computer connected to a Latet server, a vault that cannot
be read), or where it has no date, every session of it is redacted; on a computer with no vault and no server, Latet's
date is now (``known.NothingHere``): it masked nothing there, and every session is in clear. The command lists these as
"your settings redact, or Latet redacts as too long to keep", with "may": "Latet may have sent a marker in its place,
if the session went through it", and says that the settings may have been different then. Latet's date can be later
than the day it began here: a vault emptied or forgotten, and a history pruned past its 14 days, keep nothing older; a
redacted secret's sessions before it are counted in clear, and the command says that Latet may have been there
already.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Protocol

from latet.config import Config
from latet.engine.build import build_masker
from latet.engine.detectors.gitleaks import rule_types
from latet.engine.detectors.latet import LATET_TYPES
from latet.engine.surrogates.prefixes import ANY_VALUE, public_prefix
from latet.engine.types import EntityClass, Policy, SlotKind, Span
from latet.engine.vault import Vault
from latet.leak.sources import Skipped, Text

# A single string longer than this is not scanned (a saved session's pasted file, a binary blob): said, not read.
MAX_TEXT = 8 * 1024 * 1024
# How long before the vault's first sight of a value a session's text may be dated and still be the one Latet saw it in:
# the agent writes a prompt or a tool's result to its transcript, then sends the request that carries it, a moment
# later; minutes, with a slow tool call between the two or a request sent again.
SINCE_SLACK = timedelta(minutes=5)
# How often, in texts, the scan tells its progress within a session (``scan``'s ``progress``): a long session too.
PROGRESS_EVERY = 4096
# Half of a pair of UTF-16 surrogates alone, which a JSON string may hold and the detectors cannot read (Gitleaks fails
# on it, and the whole text with it): read as the replacement character, one code point for one.
_LONE_SURROGATE = re.compile(f"[{chr(0xD800)}-{chr(0xDFFF)}]")

Progress = Callable[[str, int], None]
"""Told the agent whose sessions the scan reads and how many of them it has read so far: counts alone, never a text."""
# What a text holds (``scan``'s cache): its secrets, each with the types it was found as there, and the personal types.
_Held = tuple[tuple[tuple["_Secret", tuple[str, ...]], ...], tuple[str, ...]]
_NOTHING: _Held = ((), ())  # what most texts hold, shared


class Known(Protocol):
    """What Latet's own vault and history say about one value, asked inside the scan so that no value leaves it."""

    def since(self, etype: str, value: str) -> datetime | None:
        """The date Latet's vault first saw the value, or None where it does not know it."""
        ...

    def masked_in(self, etype: str, value: str) -> dict[str, datetime]:
        """The agents' session ids whose requests Latet's history shows the value masked in, each with the time of the
        first of them."""
        ...

    def latet_since(self) -> datetime | None:
        """The earliest date Latet has on this computer (its vault's oldest entry, its history's oldest request), or
        None where it cannot tell."""
        ...

    def history_horizon(self) -> datetime | None:
        """The oldest request Latet's history still keeps: what it shows nothing of before, or None where unknown."""
        ...


@dataclass(frozen=True, slots=True)
class Occurrences:
    sessions: int
    first: datetime | None
    last: datetime | None
    confirmed: int = 0
    agents: frozenset[str] = frozenset()  # the agents of these sessions
    folders: tuple[str, ...] = ()  # their project folders, sorted


@dataclass(frozen=True, slots=True)
class Finding:
    etype: str
    eclass: str
    hint: str
    agents: frozenset[str]
    folders: tuple[str, ...]
    in_clear: Occurrences
    replaced: Occurrences
    # Of a type the settings redact, or too long for a vault: the sessions Latet may have sent a marker in.
    redacted: Occurrences = Occurrences(0, None, None)
    # Every type the value was found as, the one shown (``etype``) first.
    types: tuple[str, ...] = ()


@dataclass(slots=True)
class Report:
    sessions: dict[str, int] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    personal: dict[str, int] = field(default_factory=dict)
    skipped: list[Skipped] = field(default_factory=list)
    unread: int = 0


def label(etype: str) -> str:
    """A type as a person reads it (``GITHUB_TOKEN`` as ``Github token``)."""
    words = etype.replace("_", " ").strip().lower()
    return words[:1].upper() + words[1:] if words else "Secret"


# Gitleaks rules that find a value by where it is written, whichever service it is for: the credentials of a curl
# option, a Kubernetes secret's data. No vendor's, as a password or a bearer token is none.
_ANY_SERVICE = frozenset({"CURL_AUTH_HEADER", "CURL_AUTH_USER", "KUBERNETES_SECRET_YAML"})


@cache
def _vendor_types() -> frozenset[str]:
    return frozenset(rule_types() - ANY_VALUE - _ANY_SERVICE) | LATET_TYPES


def vendor(etype: str) -> bool:
    """Whether ``etype`` is a vendor's: one of Latet's own tokens, or the type of a Gitleaks rule written for one
    vendor's keys (`ghp_`, `AKIA`, an Adafruit key found by the name written before it), not one any value can have
    (``prefixes.ANY_VALUE``: Gitleaks' generic rule, a JWT, a private key) nor one any service's can (``_ANY_SERVICE``:
    a curl option's credentials). A key to rotate at its vendor: the command lists these first."""
    return etype in _vendor_types()


def hint(etype: str, value: str) -> str:
    """The vendor's public prefix, as the value's fake keeps it (``prefixes.public_prefix``: `ghp_`, `sk-ant-api03-`,
    `AKIA`, `latet_adm_`), an ellipsis and the length; a value without one shows the type's label and the length."""
    size = f"{len(value)} characters"
    prefix = public_prefix(etype, value)
    if 0 < len(prefix) < len(value):
        return f"{prefix}… ({size})"
    return f"{label(etype)} ({size})"


def _aware(when: datetime | None) -> datetime | None:
    if when is not None and when.tzinfo is None:
        return when.replace(tzinfo=UTC)
    return when


@dataclass(slots=True)
class _Session:
    agent: str
    first: datetime | None = None
    last: datetime | None = None
    folders: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _Secret:
    """One value's sessions and types: in memory for the scan alone, and never in a report."""

    sessions: dict[tuple[str, str], _Session] = field(default_factory=dict)
    # Each type the value was found as, in the order first found, with the number of texts it was found as it in.
    types: Counter[str] = field(default_factory=Counter)
    # Each of those types': whether the scan's own masker, of the same settings, redacts the value found as it.
    redacting: dict[str, bool] = field(default_factory=dict)

    def seen(self, item: Text, types: tuple[str, ...]) -> None:
        key = (item.agent, item.session)
        session = self.sessions.get(key)
        if session is None:
            session = self.sessions[key] = _Session(item.agent)
        when = _aware(item.time)
        if when is not None:
            if session.first is None or when < session.first:
                session.first = when
            if session.last is None or when > session.last:
                session.last = when
        if item.folder:
            session.folders.add(item.folder)
        self.types.update(types)

    @property
    def redacted(self) -> bool:
        """Redacted wherever it was found (``Finding.redacted``): a value found as one type the settings redact and as
        one they mask is masked where it is the second, and the vault keeps it."""
        return all(self.redacting.values())

    def shown(self) -> tuple[str, ...]:
        """Its types, the one shown first: a vendor's (``vendor``), then the one found in the most texts, then the
        first found."""
        order = {etype: n for n, etype in enumerate(self.types)}
        return tuple(sorted(self.types, key=lambda etype: (not vendor(etype), -self.types[etype], order[etype])))


def _occurrences(sessions: list[_Session], confirmed: int = 0) -> Occurrences:
    firsts = [s.first for s in sessions if s.first is not None]
    lasts = [s.last for s in sessions if s.last is not None]
    return Occurrences(
        len(sessions),
        min(firsts, default=None),
        max(lasts, default=None),
        confirmed,
        frozenset(s.agent for s in sessions),
        tuple(sorted({folder for s in sessions for folder in s.folders})),
    )


def _from(first: datetime | None, moment: datetime | None) -> bool:
    """Whether a session that first held a value at ``first`` held it from ``moment`` on, within ``SINCE_SLACK``."""
    return first is not None and moment is not None and first >= moment - SINCE_SLACK


def _finding(
    value: str,
    secret: _Secret,
    known: Known | None,
    began: Callable[[], datetime | None],
    horizon: Callable[[], datetime | None],
) -> Finding:
    """``secret``'s finding, each of its sessions in clear, replaced (confirmed where the history names it from its
    first sight of the value on) or, a redacted secret's, redacted. ``began``: when Latet began on this computer;
    ``horizon``: the oldest request its history still keeps."""
    clear: list[_Session] = []
    replaced: list[_Session] = []
    redacted: list[_Session] = []
    confirmed = 0
    types = secret.shown()
    etype = types[0]
    since = _aware(known.since(etype, value)) if known else None
    masked = {session: _aware(at) for session, at in known.masked_in(etype, value).items()} if known else {}
    for (_, session_id), session in secret.sessions.items():
        if session_id in masked:
            if _from(session.first, masked[session_id]):  # masked there from its first sight of the value on
                replaced.append(session)
                confirmed += 1
                continue
            sees = horizon()
            if session.first is None or (sees is not None and sees <= session.first - SINCE_SLACK):
                # The history sees back to the session's start and shows no masking then: in clear first (Latet put in
                # front of its agent halfway, a transcript sent again).
                clear.append(session)
                continue
            # Its first requests are older than the history keeps: what Latet did then, the vault's date tells, below.
        if not secret.redacted:
            (replaced if _from(session.first, since) else clear).append(session)
        elif since is not None:
            # Masked once, the vault's entry says, then redacted: before it, in clear; from it on, a fake or a marker.
            (redacted if _from(session.first, since) else clear).append(session)
        elif (start := began()) is None:
            redacted.append(session)  # nothing to tell when Latet began here: the command says "may"
        else:
            (redacted if _from(session.first, start) else clear).append(session)
    return Finding(
        etype=etype,
        eclass="secret",
        hint=hint(etype, value),
        agents=frozenset(agent for agent, _ in secret.sessions),
        folders=tuple(sorted({folder for s in secret.sessions.values() for folder in s.folders})),
        in_clear=_occurrences(clear),
        replaced=_occurrences(replaced, confirmed),
        redacted=_occurrences(redacted),
        types=types,
    )


def scan(
    texts: Iterable[Text | Skipped],
    config: Config,
    personal: bool = False,
    known: Known | None = None,
    progress: Progress | None = None,
) -> Report:
    """The secrets found in ``texts`` (and, with ``personal``, the personal data counted by type), grouped by value.

    ``known`` answers for one value inside this function: a secret is asked about once, and when Latet began on this
    computer once in a scan, where a redacted secret needs it. Without it every session counts as in clear, and a
    redacted secret's as redacted. ``progress`` is told the agent and how many of its sessions were read so far, at each
    new session and every ``PROGRESS_EVERY`` texts: counts alone."""
    # The local name model reads none of a tool's results, the kind each text is read as: not loaded.
    masker, _ = build_masker(config.model_copy(update={"ner": "off"}), Vault(os.urandom(32)))
    report = Report()
    secrets: dict[str, _Secret] = {}
    # A text, by a 16-byte digest of it (never the text: memory stays bounded), to what it holds: its secrets, each with
    # the types it was found as there, and the personal types; None where the detectors failed on it, said once. The
    # same string, in a session twice or in several, goes through the detectors once.
    cache: dict[bytes, _Held | None] = {}
    seen_sessions: set[tuple[str, str]] = set()
    personal_sessions: dict[str, set[tuple[str, str]]] = {}

    def held(planned: list[tuple[Span, Policy]], body: str) -> _Held:
        """What a text holds: its secrets (each kept in ``secrets``), with the types each was found as there, and, with
        ``personal``, the personal types."""
        values: dict[str, dict[str, None]] = {}
        kinds: dict[str, None] = {}
        for span, policy in planned:
            if span.eclass == EntityClass.SECRET:
                value = body[span.start : span.end]  # as written, as the vault keys it
                values.setdefault(value, {})[span.etype] = None
                secret = secrets.get(value)
                if secret is None:
                    secret = secrets[value] = _Secret()
                secret.redacting[span.etype] = secret.redacting.get(span.etype, False) or policy == Policy.REDACT
            elif personal:
                kinds[span.etype] = None
        if not values and not kinds:
            return _NOTHING
        return tuple((secrets[value], tuple(types)) for value, types in values.items()), tuple(kinds)

    read = 0
    key: tuple[str, str] = ("", "")
    for item in texts:
        if isinstance(item, Skipped):
            report.skipped.append(item)
            continue
        read += 1
        if item.session != key[1] or item.agent != key[0]:  # a session's texts come one after the other
            key = (item.agent, item.session)
            if key not in seen_sessions:
                seen_sessions.add(key)
                report.sessions[item.agent] = report.sessions.get(item.agent, 0) + 1
                if progress is not None:
                    progress(item.agent, report.sessions[item.agent])
        if progress is not None and read % PROGRESS_EVERY == 0:
            progress(item.agent, report.sessions[item.agent])
        body = item.text
        if len(body) > MAX_TEXT:
            report.skipped.append(Skipped(f"a text of {item.agent} session {item.session}", "too long to scan"))
            continue
        digest = hashlib.blake2b(body.encode("utf-8", "surrogatepass"), digest_size=16).digest()
        if digest in cache:
            found = cache[digest]
        else:
            readable = body if body.isascii() else _LONE_SURROGATE.sub("\N{REPLACEMENT CHARACTER}", body)
            try:
                found = cache[digest] = held(masker.planned(readable, SlotKind.TOOL_RESULT), readable)
            except Exception as exc:
                # The class alone: an exception's message may hold the text.
                why = f"the detectors failed ({type(exc).__name__})"
                report.skipped.append(Skipped(f"a text of {item.agent} session {item.session}", why))
                found = cache[digest] = None
        if found is None:  # the detectors failed on it, as the first copy said
            continue
        for secret, types in found[0]:
            secret.seen(item, types)
        for etype in found[1]:
            personal_sessions.setdefault(etype, set()).add(key)
    report.unread = len(report.skipped)
    report.personal = dict(sorted(Counter({k: len(v) for k, v in personal_sessions.items()}).items()))
    began: list[datetime | None] = []  # when Latet began on this computer, asked once, where a finding needs it

    def latet_began() -> datetime | None:
        if not began:
            began.append(_aware(known.latet_since()) if known else None)
        return began[0]

    seen: list[datetime | None] = []  # the oldest request the history keeps, asked once, where a finding needs it

    def history_horizon() -> datetime | None:
        if not seen:
            seen.append(_aware(known.history_horizon()) if known else None)
        return seen[0]

    findings = [_finding(value, secret, known, latet_began, history_horizon) for value, secret in secrets.items()]
    findings.sort(key=lambda f: (-(f.in_clear.sessions + f.replaced.sessions + f.redacted.sessions), f.etype, f.hint))
    report.findings = findings
    return report
