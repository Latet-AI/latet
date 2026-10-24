"""The shared settings of a Latet server and each identity's own.

What a server shares is what it needs to run (``SETTING_KEYS``: its destinations, the shared reviewer, text
recognition, history and retention, its port and capture folder) and the rules for everyone an edition gives
(``RULE_KEYS``, ``Edition.shared_rules``): the Enterprise Edition's; the Community Edition gives none, and people share
rules by hand (a rules file, a project's latet.yaml). Rules stored among the shared settings by an earlier version are
kept as they are, in force nowhere, and reported by counts (``in_force``, ``kept_rules``).

The shared settings in force are the base of every identity's; an identity's own part holds what it set. The settings in
use are the shared ones with the identity's on top: the lists that protect and the exceptions are the shared items, then
its own (a shared item cannot be removed by one identity, and its copy of one is that one); destinations are the shared
ones, then its own, a shared name kept; identity and projects are its own only; anything else is its own when it set it,
else the shared value. What would have the server read its own files or variables, or reach what only it reaches, stays
the server's: its port and capture folder, where the local reviewer runs with its key and certificate, and whether there
is one (an identity's reviewer is on only when the administrator turned the shared one on); no settings, shared or an
identity's, name a file or a variable to read the reviewer's key from (``reviewer_key_named``); an identity's own
destination is https, not the server itself (its machine, or the host of its own address), and never in the server's own
network (``egress``).

Settings that are not valid are refused in words that name each field that is wrong, by its path, and pydantic's type
of what is wrong with it, never what it holds: pydantic's own words quote the value they refuse, and a value of the
settings may be a customer's name. The shared settings live in PostgreSQL, sealed with the master key
(``SharedSettings``), and weigh ``SETTINGS_LIMIT`` at most sealed, as an identity's own part does (a person's snapshot,
which holds both, twice that). A row of them that does not open (written by hand, sealed with another key, or not
valid settings) is refused, never read as none (``UnreadableShared``); settings written whole without an etag replace
it, nothing of it read."""

from __future__ import annotations

import contextlib
import functools
import hmac
import ipaddress
import json
import logging
import socket
import threading
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from pydantic import TypeAdapter, ValidationError

from latet.config import (
    POLICY_STRICTNESS,
    Config,
    Destination,
    FieldRule,
    LlmReview,
    Policy,
    Profile,
    TermConfig,
)
from latet.engine.detectors.terms import TERM_LONGEST
from latet.logline import Paced
from latet.protection import folders_let_through, found_type, keeps_in_clear, lets_through, loosened
from latet.server import egress
from latet.server.master import MasterKeyError

if TYPE_CHECKING:
    from psycopg import Connection

    from latet.server.bus import Bus
    from latet.server.db import Database
    from latet.server.master import MasterKey

log = logging.getLogger(__name__)

UNION_KEYS = (
    "terms", "recognizers", "fields", "data_tools", "data_files", "data_hosts", "allow_values", "allow_patterns",
    "disabled_secret_rules",
)  # fmt: skip
OWN_KEYS = ("projects", "identity")
SERVER_KEYS = ("port", "capture_dir")
REVIEWER_SERVER_KEYS = ("url", "remote", "model", "api_key_env", "api_key_file", "certificate", "slots", "chunk_chars")
_LISTS = (*UNION_KEYS, "upstreams")
# The rules a server's administrators could set for everyone: in force only as an edition gives them
# (``Edition.shared_rules``, none in the Community Edition); the settings a server needs to run, shared in every
# edition; and, with ``OWN_KEYS``, every setting is in one of them (a test checks it).
RULE_KEYS = (
    *UNION_KEYS, "profile", "pii", "id_regions", "phone_regions", "include_private_ips", "policies", "data",
    "dump_guard", "ner", "images", "auto_mode_review", "live_review",
)  # fmt: skip
SETTING_KEYS = ("upstreams", "llm_review", "ocr", "history", "retention_days", *SERVER_KEYS)
RULES_FOR_EVERYONE = (
    "protection rules set for everyone are not part of the Community Edition (they are planned for the Enterprise "
    "Edition): share rules by hand with a rules file (`latet rules export`, then `latet rules import` or Import in the "
    "dashboard), an API key's with `latet server rules --key KEY import FILE`, or a project's `latet.yaml` on "
    "computers not connected to a server"
)
_NAMES = {
    "terms": ("term", "terms"),
    "recognizers": ("recognizer", "recognizers"),
    "fields": ("field rule", "field rules"),
    "data_tools": ("data tool", "data tools"),
    "data_files": ("data file rule", "data file rules"),
    "data_hosts": ("data address rule", "data address rules"),
    "allow_values": ("value not to mask", "values not to mask"),
    "allow_patterns": ("pattern not to mask", "patterns not to mask"),
    "disabled_secret_rules": ("secret rule turned off", "secret rules turned off"),
    "upstreams": ("destination", "destinations"),
}
IN_CLEAR = "a destination of your own on a server is https: its administrator sets one reached in clear, for everyone"
ON_SERVER = "it would reach the server itself: its administrator sets such a destination, for everyone"
# Settings that name where the local reviewer's key is read from would have the server read a file or a variable of
# its own (its secrets among them) and send it to the reviewer's address, which settings set too: on a server neither an
# identity's settings nor the shared ones name it, and the server gives the reviewer its key (E0c's server.yaml).
REVIEWER_KEY = "on a server no setting names where the reviewer's key is read from: the server gives it itself"
_SHOWN = 10  # the fields that a refusal of settings that are not valid names, at most
# Compositions that fell back to the approved settings whole: said once a minute, with how many since the line before
# (each re-application of every space that holds such settings would say it).
_UNCOMPOSED = Paced()
# Bytes the shared settings and an identity's own part may each weigh, sealed: far past any real settings, and each
# instance reads, opens and checks them whenever they change. A person's approved snapshot, which holds both, twice that
# (``space_settings``); an API key's, its own part, the same.
SETTINGS_LIMIT = 4 * 1024 * 1024

# The shared settings' row, sealed for this place: sealed for another, a value does not open here.
_AAD = "settings:shared"
# The shared settings and their change count, in one statement: one snapshot of the database, so that the count is the
# one of the change the settings read were written by (a change writes both in one transaction).
_READ: Final = (
    "SELECT (SELECT data FROM settings WHERE owner = '' AND kind = 'shared'),"
    " (SELECT counter FROM change_counters WHERE owner = '')"
)
_LOCKED: Final = "SELECT data FROM settings WHERE owner = '' AND kind = 'shared' FOR UPDATE"
# A change bumps the shared settings' counter first. The counter's row then stays locked until the change commits, so
# that a change made at the same moment waits for it, and reads what it kept: even the first time, when there is no
# row of the settings yet to lock.
_COUNT: Final = (
    "INSERT INTO change_counters (owner, counter) VALUES ('', 1)"
    " ON CONFLICT (owner) DO UPDATE SET counter = change_counters.counter + 1 RETURNING counter"
)
_WRITE: Final = (
    "INSERT INTO settings (owner, identity_id, kind, data, changed, changed_by)"
    " VALUES ('', NULL, 'shared', %s, %s, %s)"
    " ON CONFLICT (owner, kind) DO UPDATE SET data = EXCLUDED.data, changed = EXCLUDED.changed,"
    " changed_by = EXCLUDED.changed_by"
)
# Asked of the transaction a change is written in, as the administration log asks it: after the wait for the counter's
# lock, the row is read as the change before left it, which a stricter transaction would not see. PostgreSQL refuses to
# change one that has read already: it is not joined.
_READ_COMMITTED: Final = "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"


class SharedItemsError(ValueError):
    """A change would remove items set for everyone on the server: said with how many of each kind, never which."""

    def __init__(self, missing: Mapping[str, int]) -> None:
        self.missing = dict(missing)
        said = ", ".join(f"{count} {_NAMES[key][count != 1]}" for key, count in self.missing.items())
        super().__init__(f"this would remove {said} set for everyone on the server: only its administrator can")


class StaleSettings(Exception):
    """The shared settings changed since they were read (the etag given is not theirs any more): nothing was written,
    and whoever made the change reads them again."""


class SettingsStale(OSError):
    """A save of an identity's settings refused: another instance wrote its own part since this copy read it. Nothing
    was written, ``stale()`` is called, and the change is made anew once they are read again (the administration and
    the dashboard answer 409). Defined in this module, which a computer imports too and which needs no driver of the
    server's, so that the dashboard tells it apart (``space_settings`` raises it)."""


SAVED_CHANGE_PENDING = (
    "your settings were kept, but the pending change could not be refreshed. Refresh the dashboard before making "
    "another change."
)


class SettingsSavedChangePending(Exception):
    """The save and apply returned successfully, but refreshing its pending proposal failed. Never retry the save;
    the caller keeps its post-save bookkeeping and reports the kept state without the cause's text."""

    def __init__(self) -> None:
        super().__init__(SAVED_CHANGE_PENDING)


CHANGED_MEANWHILE = "your settings changed meanwhile: try again"


class ChangedMeanwhile(Exception):
    """On a server, a computer's destinations refused whole, nothing added (``Runtime.add_destinations``): an address
    its call did not judge, the settings in use having it then, is gone from them by the time the change is made (a
    shared destination removed meanwhile). Asked again, it is judged."""


class SettingsTooLarge(OSError):
    """An identity's own part that would weigh more sealed than ``SETTINGS_LIMIT``, or a snapshot more than its own
    limit (``space_settings.PgSettings._snapshot_limit``): not kept (the administration and the dashboard answer
    413)."""


class UnreadableShared(ValueError):
    """The shared settings kept in the database do not open with this server's master key, or are not valid settings:
    refused, never read as none. Its words name fields and kinds of error at most, never what the row holds."""


class SharedTooLarge(ValueError):
    """Shared settings that would weigh more sealed than ``SETTINGS_LIMIT``: not kept. Every identity's settings hold
    them: an identity's own part is bound alike, and a person's approved snapshot, which holds both, at twice that."""

    def __init__(self, weight: int) -> None:
        super().__init__(
            f"these shared settings would weigh {weight} bytes sealed, more than a server keeps ({SETTINGS_LIMIT}): "
            "they were not kept"
        )


@functools.cache
def _field_names() -> frozenset[str]:
    """The name of every field of the settings, at any depth: what the path of an error shows. A name that is none of
    them is the key of a mapping, which is what was given, and is shown as ``*``."""
    schema = Config.model_json_schema()
    found = set(schema.get("properties", {}))
    for model in schema.get("$defs", {}).values():
        found |= set(model.get("properties", {}))
    return frozenset(found)


def field_path(loc: tuple[int | str, ...]) -> str:
    """Where an error of settings is: each field by its name, each place in a list by its number, and a mapping's key,
    which is what was given, as ``*``."""
    return ".".join(str(p) if isinstance(p, int) or p in _field_names() else "*" for p in loc) or "(the settings)"


def wrong_fields(exc: ValidationError) -> list[str]:
    """What is wrong with settings given: each field by its path (``field_path``) and pydantic's kind of error, never
    what it holds (a value may be a customer's name, and pydantic's words quote it); ``_SHOWN`` of them at most, then
    how many more."""
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    wrong = [f"{field_path(error['loc'])} ({error['type']})" for error in errors]
    return wrong[:_SHOWN] + ([f"and {len(wrong) - _SHOWN} more"] if len(wrong) > _SHOWN else [])


def _settings(data: Any, what: str) -> Config:
    """``data`` as settings. Raises ValueError naming each field that is wrong and pydantic's type of what is wrong
    with it, and nothing it holds; raised once pydantic's error is handled, so that no traceback shows that error's
    words with it."""
    wrong: list[str] = []
    try:
        return validated(data)
    except ValidationError as exc:
        wrong = wrong_fields(exc)
    raise ValueError(f"{what} are not valid: {', '.join(wrong)}")


# Terms made settings at most this many at a time: pydantic holds the GIL for the whole of a call, and 30,000 terms of
# an identity's own held every identity's event loop for more than 100 ms at each load of her settings.
_TERMS_AT_ONCE = 2_000
_TERMS: Final = TypeAdapter(list[TermConfig])


def validated(data: Any) -> Config:
    """``data`` made settings (``Config.model_validate``), a long list of terms a part at a time, the GIL let go between
    two: the parts' terms are taken as they are by the whole (pydantic keeps an instance as given). A part that is not
    valid leaves the whole to say what is wrong, as it would have. Raises ValidationError."""
    items = data.get("terms") if isinstance(data, dict) else None
    if isinstance(items, list) and len(items) > _TERMS_AT_ONCE:
        made: list[Any] = []
        try:
            for start in range(0, len(items), _TERMS_AT_ONCE):
                made += _TERMS.validate_python(items[start : start + _TERMS_AT_ONCE])
        except ValidationError:
            return Config.model_validate(data)  # its errors named as the whole names them
        data = {**data, "terms": made}
    return Config.model_validate(data)


def _mark(key: str, item: Any) -> Any:
    """What makes two items of a list the same one: a term's value; a recognizer's, field's or destination's name."""
    if not isinstance(item, dict):
        return item
    if key == "terms":
        return str(item.get("value", ""))
    return str(item.get("name") or json.dumps(item, sort_keys=True))


def _marks(key: str, items: list[Any]) -> list[Any]:
    """``_mark`` of each item, a term's value read inline: a shared list of tens of thousands of terms is united with
    each identity's at every load."""
    if key == "terms":
        return [str(item.get("value", "")) if isinstance(item, dict) else item for item in items]
    return [_mark(key, item) for item in items]


def _union(key: str, shared: list[Any], own: list[Any]) -> list[Any]:
    """The shared items (as they are held, or as JSON), then the own part's (as JSON) whose mark none of them has."""
    seen = {_identity_of(key, item) for item in shared}
    out = list(shared)
    for item, mark in zip(own, _marks(key, own), strict=True):
        if mark not in seen:
            seen.add(mark)
            out.append(item)
    return out


def _host_of(url: str | None) -> str:
    """The host of ``url`` in small letters, without the dot that may end a name; empty when there is none, or when
    ``url`` cannot be read."""
    if not url:
        return ""
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _address_of(host: str) -> egress.Address | None:
    """``host`` as an address when it is one written as a number, an IPv4-mapped one as the IPv4 address it is."""
    found = egress.literal(host)
    if isinstance(found, ipaddress.IPv6Address) and found.ipv4_mapped is not None:
        return found.ipv4_mapped
    return found


def _same_host(host: str, server: str) -> bool:
    """Whether ``host`` is the server's host ``server``: the same name, or the same address however it is written."""
    if not host or not server:
        return False
    mine, its = _address_of(host), _address_of(server)
    if mine is None and its is None:
        return host == server
    return mine == its


def personal_destination_problem(
    url: str, *, resolve: egress.Resolver | None = None, server: str | None = None
) -> str | None:
    """Why an identity's own destination is refused on a server; None when it is not. In clear; the server itself, by
    its machine's names and addresses (``localhost``, its host name, a loopback, link-local or unspecified address) or
    by the host of ``server``, the server's own address (the dashboard and the administration give it); an address that
    cannot be read; or an address in the server's own network: a literal one by itself, a name by what it resolves to
    when ``resolve`` (``egress.resolve``) is given, as the dashboard and the administration give it when the setting is
    saved; without it (``layered``, which has no network) a name passes here and is judged when the server connects
    (``egress.pin``)."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return egress.UNREADABLE
    if parts.scheme != "https":
        return IN_CLEAR
    if not host:
        return egress.UNREADABLE
    machine = socket.gethostname().lower().rstrip(".")
    if host in ("localhost", machine) or host.endswith(".localhost") or _same_host(host, _host_of(server)):
        return ON_SERVER
    found = egress.literal(host)
    if found is not None and (found.is_loopback or found.is_link_local or found.is_unspecified):
        return ON_SERVER
    return egress.problem(url, resolve)


def own_destinations_refused(
    shared: Config, config: Config, *, resolve: egress.Resolver | None = None, server: str | None = None
) -> list[tuple[int, str]]:
    """Each destination of ``config`` that is the identity's own and is refused, by its place in ``config.upstreams``,
    with why (``personal_destination_problem``): the one check the dashboard and the administration make when settings
    are saved, called in a worker thread (``resolve`` waits on the network: ``egress.bounded``). A destination the
    shared settings set as it is (that name at that address) is never judged: its administrator may set one inside."""
    theirs = {(d.name, d.url) for d in shared.upstreams}
    return [
        (i, problem)
        for i, d in enumerate(config.upstreams)
        if (d.name, d.url) not in theirs
        and (problem := personal_destination_problem(d.url, resolve=resolve, server=server)) is not None
    ]


def _reviewer_kept(shared: Mapping[str, Any]) -> dict[str, Any]:
    """What an identity's reviewer takes from the shared one, ``shared`` as its JSON: where it runs, with its key and
    certificate; and, when the administrator has not turned it on, that it is off (it was never set up: the default is
    a server on the server's own port)."""
    kept = {k: shared[k] for k in REVIEWER_SERVER_KEYS}
    if not shared["enabled"]:
        kept["enabled"] = False
    return kept


def pinned_reviewer(shared: LlmReview, mine: LlmReview) -> LlmReview:
    """An identity's local reviewer: its use of it, on the shared one's server, model, key and certificate, and on only
    when the shared one is."""
    return LlmReview.model_validate(mine.model_dump(mode="json") | _reviewer_kept(shared.model_dump(mode="json")))


def layered(shared: Config, own: Mapping[str, Any]) -> Config:
    """The settings in use: ``shared`` with the identity's ``own`` part on top. Raises ValueError, naming the fields
    and never what they hold, when they are not valid settings.

    What ``shared`` holds is laid as it is, never written out as JSON and read back: each identity's settings hold the
    shared items themselves (nobody changes an item of settings: a change makes new settings), so that tens of thousands
    of shared terms cost a load, and every space that holds them, a list of the same items rather than a copy of each
    (a server's every collection of memory visits what its spaces hold)."""
    data = {key: getattr(shared, key) for key in Config.model_fields if key not in OWN_KEYS}
    for key, value in own.items():
        if key not in Config.model_fields or key in SERVER_KEYS:
            continue
        if key in UNION_KEYS:
            data[key] = _union(key, getattr(shared, key), list(value or []))
        elif key == "upstreams":
            mine = [
                d
                for d in value or []
                if isinstance(d, dict) and personal_destination_problem(str(d.get("url"))) is None
            ]
            data[key] = _union(key, shared.upstreams, mine)
        elif key == "llm_review" and isinstance(value, dict):
            reviewer = shared.llm_review.model_dump(mode="json")
            data[key] = reviewer | value | _reviewer_kept(reviewer)
        else:
            data[key] = value
    return _settings(data, "the settings")


def reused(data: Any, shared: Config) -> Any:
    """``data``, settings as JSON, with each item of its lists that ``shared`` holds the same replaced by that shared
    item itself: made settings, they take the shared items as they are (pydantic keeps an instance as given) rather
    than a copy of each, as ``layered`` does. An approved snapshot holds every shared term, and is read at every
    re-application of a space."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for key in _LISTS:
        items, theirs = data.get(key), getattr(shared, key)
        if not isinstance(items, list) or not theirs:
            continue
        by_mark = {_identity_of(key, item): item for item in theirs}
        out[key] = [_as_shared(key, item, by_mark) for item in items]
    return out


def _as_shared(key: str, item: Any, by_mark: Mapping[Any, Any]) -> Any:
    """The shared item that ``item`` (as JSON) is, when one of its mark is the same; else ``item``."""
    try:
        found = by_mark.get(_mark(key, item))
    except TypeError:  # an item no settings hold (a list, written by hand): validated as it is, and refused
        return item
    if found is None or isinstance(found, str | dict):
        return found if found is not None and found == item else item
    fields = getattr(found, "__dict__", None)
    if fields == item or (isinstance(item, dict) and found.model_dump(mode="json") == item):
        return found
    return item


def own_part(shared: Config, config: Config, *, before: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """What ``config`` sets beyond ``shared``: the identity's own part, as it is kept. ``before``, the part kept until
    now: what the identity set stays its own while it has the value it gave, even when the shared one is now the same
    (so that it stays when the shared one changes again)."""
    base, mine = shared.model_dump(mode="json"), config.model_dump(mode="json")
    kept = dict(before or {})
    default = Config().model_dump(mode="json")
    out: dict[str, Any] = {}
    for key, value in mine.items():
        if key in SERVER_KEYS:
            continue
        if key in _LISTS:
            marks = {_mark(key, item) for item in base.get(key) or []}
            ours = {_mark(key, item) for item in kept.get(key) or []} if isinstance(kept.get(key), list) else set()
            if own := [item for item in value or [] if _mark(key, item) not in marks or _mark(key, item) in ours]:
                out[key] = own
        elif key in OWN_KEYS:
            if value != default[key]:
                out[key] = value
        elif key == "llm_review":
            common = {k: v for k, v in base[key].items() if k not in REVIEWER_SERVER_KEYS}
            was = kept.get(key)
            if not isinstance(was, dict):
                was = {}
            theirs = {
                k: v for k, v in value.items()
                if k not in REVIEWER_SERVER_KEYS and (v != common.get(k) or (k in was and was[k] == v))
            }  # fmt: skip
            if theirs:
                out[key] = theirs
        elif value != base.get(key) or (key in kept and kept[key] == value):
            out[key] = value
    return out


def shared_in_force(shared: Config, config: Config) -> Config:
    """The shared items ``config`` holds: ``shared`` with each of its lists made of the items of ``config`` whose mark
    one of its own has (``_mark``), in ``config``'s order and as ``config`` holds them. ``config`` being the settings in
    use, those are the shared items in force: never one that ``config`` holds out (a value left in clear that an
    administrator added, waiting for the person in a composition), and, of a mark the shared settings changed since,
    the item in use. What is not a list is ``shared``'s."""
    update: dict[str, Any] = {}
    for key in _LISTS:
        marks = {_identity_of(key, item) for item in getattr(shared, key)}
        update[key] = [item for item in getattr(config, key) if _identity_of(key, item) in marks]
    return shared.model_copy(update=update)


def removed_shared(shared: Config, config: Config) -> dict[str, int]:
    """How many of the shared items of each list ``config`` lacks: what a change would remove."""
    base, mine = shared.model_dump(mode="json"), config.model_dump(mode="json")
    out: dict[str, int] = {}
    for key in _LISTS:
        marks = {_mark(key, item) for item in mine.get(key) or []}
        if missing := sum(_mark(key, item) not in marks for item in base.get(key) or []):
            out[key] = missing
    return out


def reviewer_key_named(config: Config) -> list[str]:
    """The settings of ``config`` that name where the local reviewer's key is read from, by their paths: a file
    (``llm_review.api_key_file``), or a variable but the default one, or none (``llm_review.api_key_env``). On a server
    no settings may (``REVIEWER_KEY``): the shared ones, and an identity's, which the dashboard and the administration
    refuse when they are saved."""
    review = config.llm_review
    named = []
    if review.api_key_file:
        named.append("llm_review.api_key_file")
    if review.api_key_env not in ("", LlmReview.model_fields["api_key_env"].default):
        named.append("llm_review.api_key_env")
    return named


def in_force(stored: Config, rules: Mapping[str, Any]) -> Config:
    """The shared settings every identity is laid over: the server's settings as ``stored``, only the rules for
    everyone an edition gives (``rules``, ``Edition.shared_rules``) and, for anything else, its default. A rule kept in
    the stored settings from an earlier version, and a setting no list places, are never in force; an edition gives
    rules alone, never a destination or a reviewer, which go through checks of their own."""
    default = Config()
    update = {key: getattr(default, key) for key in Config.model_fields if key not in SETTING_KEYS}
    update.update({key: value for key, value in rules.items() if key in RULE_KEYS})
    return stored.model_copy(update=update)


def kept_rules(stored: Config, rules: Mapping[str, Any]) -> dict[str, Any]:
    """The rules for everyone the stored shared settings hold and no edition puts in force (``rules``): kept from an
    earlier version, applied by no space."""
    default = Config()
    return {
        key: getattr(stored, key)
        for key in RULE_KEYS
        if key not in rules and getattr(stored, key) != getattr(default, key)
    }


def kept_said(kept: Mapping[str, Any]) -> str:
    """The rules ``kept`` in words: how many items of each list, the name of each other rule; never a value."""
    said = []
    for key, value in kept.items():
        if key in _NAMES:
            one, many = _NAMES[key]
            said.append(f"{len(value)} {one if len(value) == 1 else many}")
        else:
            said.append(key)
    return ", ".join(said)


def with_kept_rules(config: Config, stored: Config | None) -> Config:
    """The shared settings ``config`` as written: its settings, with the rules kept in ``stored`` (an earlier version's,
    which only an explicit forget deletes), or their defaults when there is no stored row to read."""
    source = stored if stored is not None else Config()
    return config.model_copy(update={key: getattr(source, key) for key in RULE_KEYS})


def shared_refusals(config: Config) -> list[str]:
    """What shared settings may not set: what is each identity's own, what would write on the server, and what would
    have it read its own files or variables (``reviewer_key_named``). Rules for everyone are refused where they are
    written (``RULES_FOR_EVERYONE``)."""
    default = Config()
    out = []
    if config.identity != default.identity:
        out.append("identity: each identity's own (a computer sends its own when it connects)")
    if config.projects:
        out.append("projects: each identity's own")
    if config.capture_dir is not None:
        out.append("capture_dir: it would write what people send, in clear, in a folder of the server")
    out += [f"{name}: {REVIEWER_KEY}" for name in reviewer_key_named(config)]
    return out


# The projects one identity's settings may name on a server: each is, in its space, a masker and a copy of the settings
# once a conversation uses its folder (the dashboard's list builds none).
PROJECTS_LIMIT = 200
# What one identity's own settings may hold, beyond the shared settings, of what its space compiles and weighs: its
# recognizers (a few patterns each, weighed 64 KiB: ``projects.masker_weight``), its field rules and its patterns of
# values never to mask (one pattern each, weighed 4 KiB), each counted over its settings and every project of them. As
# many as a rules file brings at once (``transfer.LIMITS``), five times a repository file's recognizers: far past what
# a person writes. At these its own rules weigh about 35 MiB, a re-application of its space compiles them in under a
# second, and ``protection`` keeps a whole weighing of them compiled (``RECOGNIZERS_COMPILED``), the shared ones with
# them; seventy thousand recognizers, which the size of the settings let through, weighed 4 GiB and took minutes.
OWN_RULES = {"recognizers": 500, "fields": 500, "allow_patterns": 500}
# What a composition unites (the lists that protect: both hold), and what it meets (the lists that leave values in
# clear: only what both leave). ``pii``, ``policies``, ``projects`` and ``upstreams`` have a composition of their own.
_PROTECTIVE = (
    "terms", "recognizers", "fields", "data_tools", "data_files", "data_hosts", "id_regions", "phone_regions",
)  # fmt: skip
_IN_CLEAR = ("allow_values", "allow_patterns", "disabled_secret_rules")


class SettingsRefused(SharedItemsError):
    """Settings refused for what they hold, before anything is applied or kept, as a change that drops a shared item is
    (the handlers that refuse one refuse these too): too many projects or rules, a term too long. Its words name the
    setting and a limit, never a value."""

    def __init__(self, said: str) -> None:
        self.missing = {}
        ValueError.__init__(self, said)


CHANGE_WAITS = (
    "a change to your settings waits for your decision in the dashboard: approve or reject it, then make this change "
    "again"
)


class ChangeWaits(SettingsRefused):
    """A person's save made while a change waits for her over a composition, refused because the settings she approved
    last cannot be read now, or a shared destination move is still held back. The separate edit cannot safely be kept
    over that composition. Its words say to decide on the change first."""

    def __init__(self) -> None:
        super().__init__(CHANGE_WAITS)


class TooManyProjects(SettingsRefused):
    """Settings that name more projects than a server keeps for one identity. Its words name the setting and the limit,
    never a project."""

    def __init__(self, count: int) -> None:
        super().__init__(
            f"these settings name {count} projects, and a server keeps {PROJECTS_LIMIT} for one identity at most "
            "(projects): remove some first"
        )


class TooManyRules(SettingsRefused):
    """Settings holding more recognizers, field rules or patterns of values never to mask of an identity's own than a
    server keeps for one (``OWN_RULES``). Its words name each kind, its count and the limit, never an item."""

    def __init__(self, over: Mapping[str, int]) -> None:
        said = "; ".join(
            f"{count} {_NAMES[key][count != 1]} (a server keeps {OWN_RULES[key]} for one identity)"
            for key, count in over.items()
        )
        super().__init__(f"these settings hold of their own, their projects' included, {said}: remove some first")


def _own_rules(shared: Config, config: Config) -> dict[str, int]:
    """How many recognizers, field rules and patterns of values never to mask ``config`` holds of its own: those the
    shared settings do not hold, and every one of its projects. Counted, none compiled."""
    out = {}
    for key in OWN_RULES:
        theirs = {_identity_of(key, item) for item in getattr(shared, key)}
        mine = sum(_identity_of(key, item) not in theirs for item in getattr(config, key))
        out[key] = mine + sum(len(getattr(entry, key)) for entry in config.projects)
    return out


def check_rules(shared: Config, config: Config) -> None:
    """Raises TooManyRules when ``config`` holds more recognizers, field rules or patterns of its own (``_own_rules``)
    than a server keeps for one identity: before any is compiled, so that a save of seventy thousand recognizers is
    refused at once and not after a minute of compiling them."""
    if over := {key: n for key, n in _own_rules(shared, config).items() if n > OWN_RULES[key]}:
        raise TooManyRules(over)


def _too_long(terms: list[TermConfig], where: str) -> list[str]:
    """The places, under ``where``, of the terms longer than a server keeps (``TERM_LONGEST``)."""
    return [f"{where}.{i}" for i, term in enumerate(terms) if len(term.value) > TERM_LONGEST]


TERM_TOO_LONG = (
    f"a term holds {TERM_LONGEST} characters at most on a server (a longer one is compiled alone, holding every "
    "identity's requests meanwhile)"
)


def _terms_too_long(shared: Config, config: Config) -> list[str]:
    """The places of the terms of ``config`` longer than a server keeps: its own and its projects', never one the shared
    settings hold (an edition's rules for everyone, or a row an earlier version kept: one there blocks no identity's
    save)."""
    theirs = {term.value for term in shared.terms}
    places = [
        f"terms.{i}"
        for i, term in enumerate(config.terms)
        if len(term.value) > TERM_LONGEST and term.value not in theirs
    ]
    for i, entry in enumerate(config.projects):
        places += _too_long(entry.terms, f"projects.{i}.terms")
    return places


class TermsTooLong(SettingsRefused):
    """Settings holding a term longer than ``TERM_LONGEST``, which the terms' matcher would compile alone, outside its
    bounded parts (S-03). Its words name each term's place, never the term; ``why``: what the limit protects where it
    is refused (a server's words by default; a computer's own are ``config.TERM_TOO_LONG``)."""

    def __init__(self, places: list[str], why: str = TERM_TOO_LONG) -> None:
        shown = ", ".join(places[:_SHOWN]) + (f" and {len(places) - _SHOWN} more" if len(places) > _SHOWN else "")
        super().__init__(f"{shown}: {why}")


def repinned(config: Config, shared: Config) -> Config:
    """``config`` with what is the server's taken from ``shared``, the shared settings of now: its port and capture
    folder, and where its local reviewer runs (address, model, key, certificate), that reviewer on only when the shared
    one is. What an approved snapshot says of them is never weighed: the administrator moves them for everyone."""
    return config.model_copy(
        update={
            "port": shared.port,
            "capture_dir": shared.capture_dir,
            "llm_review": pinned_reviewer(shared.llm_review, config.llm_review),
        }
    )


def _pii_of(config: Config) -> list[str]:
    """The personal-data types ``config`` detects."""
    from latet.engine.detectors.pii import effective_pii

    if config.profile == Profile.SECRETS:
        return []
    return effective_pii(config.pii, config.id_regions, every_region=config.profile == Profile.MAX)


def _project_lowers(globals_: tuple[Config, ...], was: Any, now: Any) -> bool:
    """Whether the entry ``now`` for a folder lowers what ``was`` gave it, under any of ``globals_``' global rules (the
    snapshot's and hers: the composition's lie between them), as ``latet.protection`` weighs a project."""
    from latet.projects import effective_config
    from latet.protection import lowered_in_project

    if (was is None or was.enabled) and now is not None and not now.enabled:
        return True
    for config in globals_:
        old_here, _ = effective_config(config, None, was)
        new_here, _ = effective_config(config, None, now)
        if lowered_in_project(old_here, new_here):
            return True
    return False


def _identity_of(key: str, item: Any) -> Any:
    """``_mark`` of an item of the settings as they are held (a term, a field rule, a destination; a recognizer's
    mapping; a text): what makes two of them the same one, read without a copy of them as JSON."""
    if isinstance(item, TermConfig):
        return item.value
    if isinstance(item, FieldRule | Destination):
        return item.name
    return _mark(key, item)


def _composed_key(key: str, found: Config, base: Config, now: Any, then: Any) -> Any:
    """``key``'s value in a composition, where ``found``'s (``now``) lowers ``base``'s (``then``): made of the values
    the two hold, each valid settings already, never of a copy of them as JSON (a shared list of tens of thousands of
    terms is composed at every re-application while a change waits)."""
    if key in _IN_CLEAR:
        marks = {_identity_of(key, item) for item in then}
        return [item for item in now if _identity_of(key, item) in marks]
    if key in _PROTECTIVE:
        known = {_identity_of(key, item) for item in then}
        merged = list(then)  # the snapshot's first: one of a name stays its
        seen = set(known)
        for item in now:
            if (mark := _identity_of(key, item)) not in seen:
                seen.add(mark)
                merged.append(item)
        if key == "recognizers":  # one of hers that leaves what it finds in clear waits
            return [item for item in merged if _identity_of(key, item) in known or not keeps_in_clear(item)]
        if key in ("terms", "fields"):  # as one of hers of a type let through, at the top or in a folder of hers
            through = lets_through(base) | lets_through(found) | folders_let_through([*base.projects, *found.projects])
            if key == "terms":
                return [item for item in merged if item.value in known or found_type(item) not in through]
            # and one whose values stay in clear in strict data mode
            return [
                item
                for item in merged
                if _identity_of(key, item) in known or (item.mask and found_type(item) not in through)
            ]
        return merged
    if key == "pii":
        types = list(dict.fromkeys([*_pii_of(base), *_pii_of(found)]))
        if then is None and loosened(base, base.model_copy(update={"pii": types})):
            # Written out, a list is used as it is in every folder: where the snapshot's "not set" lets a folder's own
            # stricter profile detect more (max: every country's ID numbers), it would detect fewer there. The
            # snapshot's then, and what hers adds at the top waits with the rest of her change.
            return then
        return types
    if key == "policies":
        out = dict(then)
        for etype, policy in now.items():
            if POLICY_STRICTNESS[Policy(policy)] > POLICY_STRICTNESS[Policy(out.get(etype, Policy.MASK))]:
                out[etype] = policy
        return out
    if key == "projects":
        from latet.projects import norm_path

        mine = {norm_path(e.path): e for e in now}
        kept = {norm_path(e.path): e for e in then}
        out_entries = []
        for path in dict.fromkeys([*kept, *mine]):
            chosen = mine.get(path)
            if chosen != kept.get(path) and _project_lowers((base, found), kept.get(path), chosen):
                chosen = kept.get(path)
            if chosen is not None:
                out_entries.append(chosen)
        return out_entries
    if key == "upstreams":  # each destination of the snapshot where it led; one of hers added leads nowhere new
        hers = {d.name: d for d in now}
        out_list = [hers[d.name] if d.name in hers and hers[d.name].url == d.url else d for d in then]
        names = {d.name for d in out_list}
        return [*out_list, *(d for d in now if d.name not in names)]
    return then


def composed(found: Config, approved: Config, shared: Config) -> Config:
    """What a person's space runs on a server, at its start and at every re-application: ``found``, her settings as
    PostgreSQL has them now, but where they lower protection against ``approved``, the settings she approved last,
    where those apply. A protective list holds both (an administrator's new shared term applies at once, the one he
    removed waits for her); a list that leaves values in clear holds what both leave; a policy is the stricter of the
    two; a project or a destination is the snapshot's where hers lowers it; any other setting is the snapshot's where
    hers lowers it, else hers. What is the server's comes from ``shared``, the shared settings of now (``repinned``).
    ``found`` itself when it lowers nothing.

    Never looser than the snapshot, in any folder either: where what one setting does with another lowers protection
    anyway, the snapshot (re-pinned) applies, said in the log. Each setting is composed of the values the two hold, so
    the composition is valid settings as they are."""
    base = repinned(approved, shared)
    if not loosened(base, found):
        return found
    update: dict[str, Any] = {}
    for key in Config.model_fields:
        if key in SERVER_KEYS:
            continue
        now, then = getattr(found, key), getattr(base, key)
        if now == then:
            continue
        if not loosened(base, base.model_copy(update={key: now})):
            continue  # hers adds protection, or changes nothing that protects
        update[key] = _composed_key(key, found, base, now, then)
    result = repinned(found.model_copy(update=update), shared)
    if loosened(base, result):
        if count := _UNCOMPOSED.due():
            log.warning(
                "the settings in use could not be composed without lowering protection: the approved ones apply%s",
                f" ({count} times since the line before)" if count > 1 else "",
            )
        return base
    return result


def _by_path(entries: list[Any]) -> dict[str, Any]:
    from latet.projects import norm_path

    return {norm_path(e.path): e for e in entries}


def _laid_back(key: str, mine: Any, theirs: Any, kept: Any, shared: Any = ()) -> Any:
    """``key``'s value in a save laid back on the settings kept (``OwnSettings._rebased``): ``mine`` is the save's,
    ``theirs`` the composition's in use, ``kept`` the settings kept's. A list that protects or lets values through gets
    the items she added, loses those she removed, and takes her version of an item of her own she changed under the
    same mark (a field rule's, a term's, a recognizer's or a destination's content): one whose mark ``shared``, the
    shared settings' list, does not hold, a shared item keeping the shared version. A project gets each entry she
    changed; a policy, each type she changed; any other setting she changed is hers whole."""
    if key in (*UNION_KEYS, "upstreams"):
        ours = {_identity_of(key, item) for item in theirs}
        hers = {_identity_of(key, item) for item in mine}
        common = {_identity_of(key, item) for item in shared}
        before = {_identity_of(key, item): item for item in theirs}
        changed: dict[Any, Any] = {}  # her own items, by mark, whose content she changed
        for item in mine:
            mark = _identity_of(key, item)
            if mark in before and mark not in common and item != before[mark]:
                changed[mark] = item
        out = []  # without what she removed, with what she changed
        for item in kept:
            mark = _identity_of(key, item)
            if mark not in ours - hers:
                out.append(changed.get(mark, item))
        skip = ours | {_identity_of(key, item) for item in out}
        return out + [item for item in mine if _identity_of(key, item) not in skip]  # with what she added
    if key == "projects":
        mine_at, theirs_at, kept_at = _by_path(mine), _by_path(theirs), _by_path(kept)
        entries = []
        for path in dict.fromkeys([*mine_at, *kept_at]):
            entry = kept_at.get(path) if mine_at.get(path) == theirs_at.get(path) else mine_at.get(path)
            if entry is not None:
                entries.append(entry)
        return entries
    if key == "policies":
        out_policies = dict(kept)
        for etype in {*mine, *theirs}:
            if mine.get(etype) != theirs.get(etype):
                if etype in mine:
                    out_policies[etype] = mine[etype]
                else:
                    out_policies.pop(etype, None)
        return out_policies
    return mine


def _carried(key: str, mine: Any, theirs: Any, kept: Any, approved: Any, shared: Any = ()) -> Any:
    """``key``'s value in the approved snapshot that a save made over a composition carries (``carried``):
    ``approved``'s, with what the save (``mine``) changed from the composition in use (``theirs``), read as
    ``_laid_back`` reads a save (by item, by folder, by type, or whole; ``shared``: the shared settings' list), but
    never what the composition holds out of the settings kept (``kept``), even where the save names it: an item it
    holds out, or the version of an item it holds back. That waits for the person, and is not the save's."""
    if key in _LISTS:
        kept_at = {_identity_of(key, item): item for item in kept}
        theirs_at = {_identity_of(key, item): item for item in theirs}
        named = []
        for item in mine:
            mark = _identity_of(key, item)
            held = kept_at.get(mark)
            if mark not in theirs_at:
                if held is not None:  # an item the composition holds out (a value let through for everyone)
                    continue
            elif held is not None and held != theirs_at[mark] and item == held:
                item = theirs_at[mark]  # the version of it the composition holds back, named: as it is in force
            named.append(item)
        return _laid_back(key, named, theirs, approved, shared)
    if key == "projects":
        from latet.projects import norm_path

        kept_at, theirs_at = _by_path(kept), _by_path(theirs)
        entries = []
        for entry in mine:
            path = norm_path(entry.path)
            if path in kept_at and kept_at[path] != theirs_at.get(path) and entry == kept_at[path]:
                entry = theirs_at.get(path)  # the entry the composition holds out: as it is in force
            if entry is not None:
                entries.append(entry)
        return _laid_back(key, entries, theirs, approved)
    if key == "policies":
        named = dict(mine)
        for etype in {*kept, *theirs}:
            if kept.get(etype) != theirs.get(etype) and mine.get(etype) == kept.get(etype):  # held out: as in force
                if etype in theirs:
                    named[etype] = theirs[etype]
                else:
                    named.pop(etype, None)
        return _laid_back(key, named, theirs, approved)
    return approved if mine == kept != theirs else mine


def carried(config: Config, in_force: Config, kept: Config, approved: Config, shared: Config) -> Config:
    """``approved``, the settings a person approved last, with what ``config`` changed from ``in_force``: ``config`` a
    save of hers made over that composition of ``kept``, the settings kept, with them, over the shared settings
    ``shared`` (``_carried``). What her save approves, and nothing the composition holds out. What is the server's is
    left to the composition (``repinned``)."""
    update: dict[str, Any] = {}
    for key in Config.model_fields:
        if key in SERVER_KEYS:
            continue
        mine, theirs = getattr(config, key), getattr(in_force, key)
        if mine == theirs:
            continue
        common = getattr(shared, key) if key in _LISTS else ()
        value = _carried(key, mine, theirs, getattr(kept, key), getattr(approved, key), common)
        if value != getattr(approved, key):
            update[key] = value
    return approved.model_copy(update=update) if update else approved


class OwnSettings:
    """An identity's settings on a server (E0a's ``SettingsStore``): the shared ones with its own part on top, its part
    written alone (and kept while it has the value it gave), a change that drops a shared item refused, and one that
    names more projects, or holds more rules of its own, than a server keeps. Where its part and its approved snapshot
    are kept is the subclass's: PostgreSQL (``space_settings.PgSettings``), memory in tests."""

    def __init__(self, shared: Callable[[], Config]) -> None:
        self._shared = shared
        # The shared settings that the last load or preparation laid the own part over: what the settings in use were
        # made of (a copy read later may be newer).
        self._laid_over: Config | None = None
        # The settings kept and the settings in use, when those are a composition stricter than these (``composed``):
        # a save of the settings in use is laid back on the settings kept (``_rebased``), a person's: an API key's save
        # is the administration's, which approves what it sets (``kind``, which the subclass sets).
        self._composition: tuple[Config, Config] | None = None
        self.kind = "person"

    def _own(self) -> dict[str, Any]:
        raise NotImplementedError

    def _keep_own(self, own: dict[str, Any]) -> None:
        raise NotImplementedError

    def reviewed(self) -> Config | None:
        raise NotImplementedError

    def save_reviewed(self, config: Config) -> None:
        raise NotImplementedError

    def shared(self) -> Config:
        """The settings the server shares, under this identity's: read only (on a server the instance's copy itself,
        which nobody changes)."""
        return self._shared()

    @property
    def composition(self) -> tuple[Config, Config] | None:
        """The settings kept and the settings in use, when those are a composition stricter than these (``composed``: a
        change waits for the person) over which a save is laid back on the settings kept: a person's. None otherwise,
        and for an API key, whose save is the administration's."""
        return self._composition if self.kind != "key" else None

    def load(self) -> Config:
        shared = self._shared()
        self._laid_over, self._composition = shared, None
        return layered(shared, self._own())

    def _made_over(self) -> Config:
        """The shared settings the settings in use were laid over (``_laid_over``), which a save of them is made over:
        never a copy read later, whose every value changed since would differ from what the save holds and become the
        identity's own for good. The shared settings of now before any load."""
        return self._laid_over if self._laid_over is not None else self._shared()

    def save(self, config: Config) -> None:
        """Keep ``config``'s own part, worked out against the shared settings it was made over (``_made_over``). When
        the settings in use are a composition (``composed``) and ``config`` is a save made over them, it is laid back on
        the settings kept first (``_rebased``): what the composition alone holds (a shared item removed, which waits for
        the person) never becomes her own, nor is what her own part holds (what waits for her approval) dropped, so that
        what waits for her stays hers to decide; and what waits is asked again over what is kept (``_laid_back``). The
        composition itself, saved as it is (her rejection of what waits), is kept whole, and so is an API key's save:
        the administration's, which approves what it sets."""
        kept = self._rebased(config)
        if len(kept.projects) > PROJECTS_LIMIT:
            raise TooManyProjects(len(kept.projects))
        check_rules(self._made_over(), kept)
        self._keep_own(own_part(self._made_over(), kept, before=self._own()))
        if kept is config:
            self._composition = None
            return
        self._composition = (kept, config)
        self._laid_back()

    def _rebased(self, config: Config) -> Config:
        """``config`` laid back on the settings kept, when the settings in use are a composition and ``config`` is not
        that composition itself: each setting she changed from the composition is hers (``_laid_back``: by item, an
        item of her own changed under its mark too, by folder, by type where a setting holds many), each she did not is
        the settings kept's. ``config`` itself otherwise (the composition itself, saved as it is, is kept whole)."""
        if self._composition is None or self.kind == "key":
            return config
        kept, in_force = self._composition
        if config == in_force:  # the composition saved as it is: what it holds is kept
            return config
        shared = self._made_over()
        update: dict[str, Any] = {}
        for key in Config.model_fields:
            mine, theirs, was = getattr(config, key), getattr(in_force, key), getattr(kept, key)
            common = getattr(shared, key) if key in _LISTS else ()
            value = was if mine == theirs else _laid_back(key, mine, theirs, was, common)
            if value != mine:
                update[key] = value
        return config.model_copy(update=update) if update else config

    def _laid_back(self) -> None:
        """A save was laid back on the settings kept: what waits for the person is to be asked again over them. Nothing
        here; on a server the space reads its settings again (``space_settings.PgSettings``)."""

    def prepare(self, config: Config, *, strict: bool = False) -> Config:
        """``config`` as it applies: its own part, worked out against the shared settings it was made over
        (``_made_over``), laid over those same ones. A change of the shared settings that the settings in use do not
        hold yet is not the identity's to keep or to drop: it reaches it when its space applies it, weighed against what
        it approved (``composed``), so that a shared item removed meanwhile waits for it there, and is never approved
        by a save of its own made before it saw the removal. With ``strict``, refused: a change that drops a shared item
        it was shown (over a composition, a shared item in force: one it holds out is not shown), too many projects or
        rules of its own (``check_rules``, on the save laid back on the settings kept, as ``save`` keeps it), a term too
        long. A person's save made over a composition (``composition``)
        is composed again (``_recomposed``): what waits for her stays out, whatever the save names."""
        shared = self._made_over()
        held = self.composition if strict else None
        shown = shared if held is None else shared_in_force(shared, held[1])
        if strict and (missing := removed_shared(shown, config)):
            raise SharedItemsError(missing)
        if strict and len(config.projects) > PROJECTS_LIMIT:
            raise TooManyProjects(len(config.projects))
        if strict:  # counted on what the save keeps (``_rebased``): what the composition alone holds is not hers
            check_rules(shared, config if held is None else self._rebased(config))
        if strict and (places := _terms_too_long(shared, config)):
            raise TermsTooLong(places)
        self._laid_over = shared
        if held is not None:
            return self._recomposed(config, held, shared)
        return layered(shared, own_part(shared, config, before=self._own()))

    def _recomposed(self, config: Config, held: tuple[Config, Config], shared: Config) -> Config:
        """``config``, a person's save made over the composition ``held`` (the settings kept, and those in use made of
        them), as it applies: the settings the save keeps (laid back on the settings kept, ``_rebased``, then over
        ``shared`` again), composed with the approved snapshot carrying what the save changed (``carried``), as a space
        composes its settings (``composed``). So what the composition holds out (a value an administrator let through
        for everyone, a shared term removed) stays out, approved neither by the save nor by what it names, and her own
        changes apply at once and are approved. The settings in use themselves when the save changes nothing of them
        (kept as they are, a save of them would be her rejection of what waits). Raises ChangeWaits when the approved
        snapshot cannot be read (none kept, kept by another version of Latet, or one that does not open), or a shared
        destination move remains held back: decide the current pending change before making this separate edit."""
        kept, in_force = held
        if config == in_force:
            return in_force
        shared_urls = {d.name: d.url for d in shared.upstreams}
        kept_urls = {d.name: d.url for d in kept.upstreams}
        if any(
            d.name in shared_urls and kept_urls.get(d.name) == shared_urls[d.name] != d.url for d in in_force.upstreams
        ):
            # A held shared route cannot become the person's own route while its move still awaits their decision.
            raise ChangeWaits()
        try:
            approved = self.reviewed()
        except ValueError:  # a snapshot that does not open: composed with nothing, the save would apply what waits
            approved = None
        if approved is None:
            raise ChangeWaits()
        found = layered(shared, own_part(shared, self._rebased(config), before=self._own()))
        return composed(found, carried(config, in_force, kept, approved, shared), shared)

    def composed(self, found: Config, approved: Config) -> Config:
        """``composed`` of ``found`` (the last load) and ``approved``, over the shared settings ``found`` was laid
        over; a save of it is laid back on ``found`` (``save``)."""
        result = composed(found, approved, self._laid_over or self._shared())
        self._composition = None if result is found else (found, result)
        return result


def _digest(config: Config) -> str:
    """The etag of ``config``: ``latet.runtime.digest``, with which the administration answers the settings it reads.
    Imported when it is first needed: latet.runtime loads the whole engine, which a computer that imports this module
    for its layering does not need."""
    from latet.runtime import digest

    return digest(config)


class SharedSettings:
    """The shared settings in PostgreSQL, sealed with the master key (an edition's rules for everyone hold terms, and so
    may a row an earlier version kept: a list of customers is sensitive in itself). Every change bumps their counter
    (``change_counters``, owner ``''``) in its own transaction, and once it has committed it is announced on `shared`
    with it: an instance that missed the announcement finds the
    change by the counter. An instance keeps a copy until it hears of a change (``forget``), with the count it was read
    at (``count``); a read that began before a ``forget`` answers what it read and keeps nothing of it, since the change
    told meanwhile has made it old, and none replaces a copy read at a later count. An announcement that Valkey lost
    leaves a copy behind PostgreSQL's count: a caller that read that count asks for the copy at least as recent
    (``counted``), and a space is built so (the beat's check forgets a copy behind the count it reads too).

    What every instance lays identities over (``get``, ``counted``, its copy) are the shared settings in force
    (``in_force``): the row's settings and the rules for everyone the edition gives (``rules``, the edition's
    ``shared_rules``: none in the Community Edition). The row as it is (``stored``), rules kept from an earlier version
    in it, is the administration's alone; the log says once per change that rules are kept, by their counts.

    ``get``, ``counted``, ``set`` and ``announce`` wait on PostgreSQL or Valkey: worker threads call them, not the event
    loop."""

    def __init__(
        self,
        db: Database,
        bus: Bus,
        master: MasterKey,
        *,
        clock: Callable[[], float] = time.time,
        rules: Callable[[Config], Mapping[str, Any]] | None = None,
    ) -> None:
        from latet.edition import Community

        self.db, self.bus, self.master, self._clock = db, bus, master, clock
        self.rules = rules if rules is not None else Community().shared_rules
        self._said = -1  # the change count whose kept rules the log has said
        self._lock = threading.Lock()
        self._copy: Config | None = None
        self._count = 0  # the shared settings' change count the copy was read at
        # Counted by every ``forget``. A read notes it before it reads and keeps what it read only if it has not moved.
        self._generation = 0

    def get(self, *, at_least: int = 0, copy: bool = True) -> Config:
        """The shared settings (``Config()`` when none were set), a copy of the caller's own: what one caller changes in
        it changes nobody else's. ``at_least``: their change count as PostgreSQL had it when the caller read it; a copy
        kept from before it is read again. ``copy=False``: the instance's copy itself, for a caller that only reads it
        (an identity's settings laid over it, weighed, composed: never changed), spared a copy of every term at each
        use; nobody changes it. Raises ValueError, in words that hold nothing of them, when what the database keeps
        does not open with the master key or is not valid settings."""
        return self.counted(at_least=at_least, copy=copy)[0]

    def counted(self, *, at_least: int = 0, copy: bool = True) -> tuple[Config, int]:
        """The shared settings, as ``get`` answers them, and the change count they were read at: every change up to it
        is in them. A copy kept from before ``at_least`` (its announcement lost) is forgotten and read again."""
        with self._lock:
            if self._copy is not None and self._count >= at_least:
                return (self._copy.model_copy(deep=True) if copy else self._copy), self._count
            if self._copy is not None:  # behind PostgreSQL: the announcement of a change was lost
                self._generation += 1
                self._copy = None
            generation = self._generation  # before anything is read
        stored, count = self._read()
        rules = self.rules(stored)
        config = in_force(stored, rules)
        if (kept := kept_rules(stored, rules)) and self._said != count:
            self._said = count
            log.warning(
                "the shared settings keep rules for everyone from an earlier version, in force for nobody (%s): "
                "`latet server rules export` writes them, `latet server rules forget` deletes them",
                kept_said(kept),
            )
        with self._lock:
            # Nothing was forgotten while it read, and no read that began after this one kept a later copy first.
            if self._generation == generation and (self._copy is None or self._count <= count):
                self._copy, self._count = config, count
        return (config.model_copy(deep=True) if copy else config), count

    def stored(self) -> Config:
        """The shared settings as PostgreSQL has them now, past this instance's copy, which it neither keeps nor
        forgets: what the administration answers, since an instance that missed an announcement keeps an older copy,
        and would answer its etag. The row as it is, rules kept from an earlier version in it: never what an identity is
        laid over (``get``). Raises UnreadableShared as ``get`` does."""
        return self._read()[0]

    def kept(self) -> dict[str, Any]:
        """The rules for everyone the row keeps from an earlier version and the edition puts in force nowhere."""
        stored = self.stored()
        return kept_rules(stored, self.rules(stored))

    @property
    def count(self) -> int | None:
        """The change count the copy kept was read at; None when none is kept."""
        with self._lock:
            return self._count if self._copy is not None else None

    def _read(self) -> tuple[Config, int]:
        """The shared settings as PostgreSQL has them now, and their change count."""
        with self.db.connection() as conn:
            row = conn.execute(_READ).fetchone()
        if row is None:
            return Config(), 0
        count = int(row[1]) if row[1] is not None else 0
        return (self._opened(row[0]) if row[0] is not None else Config()), count

    def _opened(self, sealed: bytes) -> Config:
        """The settings that the sealed value of the row holds. Raises UnreadableShared once the error of the opening,
        of the decoding or of the settings is handled: none is chained to it."""
        data: Any = None
        readable = False
        with contextlib.suppress(InvalidTag, MasterKeyError, ValueError):  # another key or place; not sealed; not JSON
            data, readable = json.loads(self.master.open(sealed, _AAD)), True
        if not readable:
            raise UnreadableShared(
                "the shared settings kept in the database cannot be read: they do not open with this server's master "
                "key, or what opens is not JSON"
            )
        wrong = ""
        try:
            return _settings(data, "the shared settings kept in the database")
        except ValueError as exc:  # its words name fields and kinds of error alone
            wrong = str(exc)
        raise UnreadableShared(wrong)

    def _readable(self, kept: Any) -> bool:
        """Whether the row read under the lock holds settings that open here (no row: the defaults, which do)."""
        if kept is None:
            return True
        try:
            self._opened(kept[0])
        except UnreadableShared:
            return False
        return True

    def set(
        self,
        config: Config,
        *,
        actor: str,
        etag: str | None = None,
        conn: Connection[Any] | None = None,
        unreadable: bool = False,
    ) -> int:
        """Keep ``config`` as the shared settings, stamped with ``actor`` and the time: their counter, which this change
        bumped. ``etag``: the digest (``latet.runtime.digest``) of the settings the caller read; when the ones kept are
        others now (another administrator changed them meanwhile), nothing is written and StaleSettings is raised.
        Two changes made at once wait for each other, so that two made from one reading never both pass.
        ``unreadable``: the caller found the row unreadable (``UnreadableShared``), and replaces it only if it still is,
        nothing of it read; StaleSettings when it opens now (another change landed: read them again).

        Without ``conn``, the change commits here and is announced. Given ``conn``, a connection in the middle of a
        transaction (not in autocommit mode), it is written there, in a savepoint, and kept or lost with that
        transaction (with the entry of the administration log that records it, say), and nothing is announced: once the
        transaction has committed, the caller calls ``announce`` with the counter. A change that is refused there leaves
        nothing of itself in the transaction, its count either. The counter's row stays locked until that transaction
        ends, and every other change of the shared settings waits for it.

        Raises ValueError, before anything is written, for what the shared settings may not hold
        (``shared_refusals``), and SharedTooLarge for settings past ``SETTINGS_LIMIT`` sealed."""
        if refusals := shared_refusals(config):
            raise ValueError(f"the shared settings cannot hold these: {'; '.join(refusals)}")
        sealed = self.master.seal(json.dumps(config.model_dump(mode="json")).encode(), _AAD)
        if len(sealed) > SETTINGS_LIMIT:
            raise SharedTooLarge(len(sealed))
        if conn is None:
            with self.db.transaction() as own:
                own.execute(_READ_COMMITTED)
                counter = self._write(own, sealed, actor, etag, unreadable)
            self.announce(counter)
            return counter
        if conn.autocommit:
            raise ValueError("the shared settings are changed in a transaction: this connection is in autocommit mode")
        conn.execute(_READ_COMMITTED)  # in the caller's transaction itself: a savepoint cannot change it
        with conn.transaction():  # a savepoint: a refusal rolls back to it, and the caller's transaction goes on
            return self._write(conn, sealed, actor, etag, unreadable)

    def _write(self, conn: Connection[Any], sealed: bytes, actor: str, etag: str | None, unreadable: bool) -> int:
        """The change in the transaction of ``conn``, which ``set`` has made READ COMMITTED (``_READ_COMMITTED``): the
        counter bumped (``_COUNT``), the settings kept compared with ``etag`` when one is given (or found unreadable
        still, when the caller found them so), then the new ones written."""
        row = conn.execute(_COUNT).fetchone()
        assert row is not None  # RETURNING gives the row it counted
        if unreadable:
            if self._readable(conn.execute(_LOCKED).fetchone()):
                raise StaleSettings("the shared settings can be read now: read them again")
        elif etag is not None:
            kept = conn.execute(_LOCKED).fetchone()
            current = self._opened(kept[0]) if kept is not None else Config()
            if not hmac.compare_digest(str(etag).encode("utf-8", "surrogatepass"), _digest(current).encode()):
                raise StaleSettings("the shared settings changed since they were read: read them again")
        conn.execute(_WRITE, (sealed, self._clock(), actor))
        return int(row[0])

    def announce(self, counter: int) -> None:
        """Have every instance, this one too, read the shared settings again: their change ``counter`` has committed.
        When it cannot be told (Valkey does not answer), the error is said in the log by its type and not raised: the
        change took effect, and the other instances find it by its counter."""
        self.forget()
        try:
            self.bus.publish("shared", {"changed": True, "counter": counter})
        except Exception as exc:
            log.warning(
                "a change of the shared settings could not be told to the other instances (%s): they find it by its "
                "counter",
                type(exc).__name__,
            )

    def forget(self) -> None:
        """They changed (here or on another instance): read them again at their next use. A read under way meanwhile
        keeps nothing of what it reads."""
        with self._lock:
            self._generation += 1
            self._copy = None
