"""Locked rules (Latet Enterprise): a floor the company sets, which nobody goes below.

The policy is a rules file (``latet_rules: 1``, what ``latet rules export`` writes) with one key of its own,
``exceptions``: whether each person's exceptions count (``personal``, the default) or only the company's
(``company``). What it says is locked at that value or stricter; what it leaves out stays each person's. Its profile
locks what the profile brings with it (the personal-data types, private IP addresses, strict data mode, images). The
settings in use are raised to it and never lowered: the company's own exceptions are allowed, never added, and nothing
of a person's own goes out of use (a recognizer of the company's joins theirs under a name and a type of its own where
theirs has its name or its type; a field rule of the company's is merged into theirs of its name). A company term of a
type the company does not allow (one of a type it allows is its exception: M-2) is masked as the company wrote it
whatever a person sets: the settings in use carry the company's terms to the build as
locked (``Config.locked_terms``), which the engine finds with a detector of their own, lets through by no exception,
and cuts out of what a person leaves in clear around one (``Masker.mask``); their entries of its value that it covers
are left out, and so are `allow` for its type and a value never to mask equal to it (ruling 4, which ``below`` says). A
change below it is refused with ``protection.loosened``'s reasons, which name a setting, never a value.

Where a policy comes from: latet.ee.policy_server (a server's database), and a file an administrator reads with
``latet policy check`` before setting it (``read_policy``).
"""

from __future__ import annotations

import functools
import hashlib
import json
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from latet.config import (
    NO_ALIASES,
    OUT_OF_RANGE,
    POLICY_STRICTNESS,
    PROFILE_STRICTNESS,
    Config,
    FieldRule,
    ImagesSetting,
    Policy,
    Profile,
    ProjectEntry,
    ProjectSettings,
    TermConfig,
    join_guidance,
)
from latet.ee.license import file_text
from latet.engine.detectors.ids import REGIONS as ID_COUNTRIES
from latet.engine.detectors.ids import types_for
from latet.engine.detectors.pii import effective_pii
from latet.fileio import FileReadError, read_regular_snapshot
from latet.protection import (
    GUIDANCE_CHANGED,
    _by_folders,
    found_type,
    keeps_in_clear,
    let_through_by,
    loosened,
    private_ips,
    recognizer_type,
    recognizers_in_force,
)
from latet.transfer import FORMAT, KEYS, MAX_BYTES, VERSION_KEY, RulesError, rules_data, rules_of
from latet.vision.policy import IMAGES_RANK, image_policy

Exceptions = Literal["personal", "company"]
Source = Literal["managed", "server"]

FEATURE = "locked-rules"
EXCEPTIONS_KEY = "exceptions"  # the policy file's own key: whose exceptions count
_ABSENT = object()  # a key the policy file does not write, which a key written with no value (null) is not
# The exceptions a person may add for themselves, unless the policy says `exceptions: company`: values and patterns
# never to mask, secret rules turned off, and what keeps values in clear in the keys that protect (a recognizer whose
# policy is allow, `allow` for a type in `policies`, a field rule with `mask: false`, and a term or a field rule of a
# type let through, which outranks what else is found where they meet: with `company`, `allow` stays only for a type the
# company allows, a term of yours of such a type stays in use, cut by the engine around what is masked without it, and
# a field rule of yours of such a type goes out of use where it is let through wherever it applies, its type masked
# where it is let through in some places only; a field rule the company's is merged into takes the company's type).
# Your terms are not locked by it: only those of a type let through are your exceptions.
PERSONAL_EXCEPTIONS = ("allow_values", "allow_patterns", "disabled_secret_rules", "recognizers", "policies", "fields")
# What a profile brings with it, locked with it.
_WITH_PROFILE = ("pii", "include_private_ips", "data", "images")


class PolicyError(ValueError):
    """A policy that cannot be read at all: no file, not YAML, made by a newer Latet. Its words never quote what the
    policy holds."""


def _composed(value: str) -> str:
    """A value as the terms detector compares it in its own case: composed and decomposed alike (an ASCII value is its
    own composed form, read as it is)."""
    return value if value.isascii() else unicodedata.normalize("NFC", value)


def _folded(value: str) -> str:
    """A value as an entry that matches any case matches it: composed, in lower case. RE2 folds case one character for
    another (simple folding), so a sharp s is not ``ss`` there, as it is for ``casefold``; and the allow list compares a
    value in lower case."""
    return _composed(value).lower()


@dataclass(frozen=True)
class Floor:
    rules: Config  # what the policy sets, over Latet's defaults: only `keys` are locked
    exceptions: Exceptions
    source: Source
    etag: str
    received: float  # when it was written (a file) or set (on a server)
    keys: frozenset[str] = frozenset()  # the rules-file keys the policy sets
    notices: tuple[str, ...] = ()  # what in it was not read
    origin: str = ""  # its file, or the server's address
    unread: frozenset[str] = frozenset()  # the keys of its file left out whole (a mistake in them, or not a rule)

    def below(self, config: Config) -> list[str]:
        return below_floor(self, config)

    def raise_to(self, config: Config) -> Config:
        return raise_to_floor(self, config)

    @property
    def locks_terms(self) -> bool:
        """Whether ``raise_to`` carries terms to the build as locked (``Config.locked_terms``): those of a type it does
        not allow (``_Company.terms``)."""
        return bool(self.company.terms)

    @property
    def carves_terms(self) -> bool:
        """Whether ``raise_to`` has the engine cut every span of a person's own left in clear (their terms' and their
        identity's) around what is masked without them (``Config.carves_terms``): with `exceptions: company`, so that
        a term of a person's, or their identity, of a type let through leaves in clear nothing masked without it."""
        return self.exceptions == "company"

    @property
    def decides_exceptions(self) -> bool:
        """Whether the company decides on request exceptions (Latet 0.11's): with `exceptions: company` one is approved
        by its security team alone, through central approvals (or by the administration that keeps an API key's
        settings); with `personal` they are each person's, as in the Community Edition."""
        return self.exceptions == "company"

    @property
    def brought(self) -> frozenset[str]:
        """The keys its profile locks without the file naming them (what ``raise_to`` raises to it): the personal-data
        types with `balanced` or `max`, and with `max` the ID numbers of every country, private IP addresses, strict
        data mode and its rule for images."""
        if "profile" not in self.keys or self.rules.profile == Profile.SECRETS:
            return frozenset()
        more = ("id_regions", "include_private_ips", "data", "images") if self.rules.profile == Profile.MAX else ()
        return frozenset(("pii", *more)) - self.keys

    @property
    def locked(self) -> frozenset[str]:
        """Every key it locks: the ones it sets and the ones its profile brings."""
        return self.keys | self.brought

    @functools.cached_property
    def company(self) -> _Company:
        """What raising any settings to it works out of the policy alone: once for this floor (a server keeps one floor
        object while its policy is unchanged), not at each raise of each space and each request."""
        return _Company(self)


def unread_words(keys: Iterable[str]) -> str:
    """The keys of a policy's file left out whole, as a refusal names them: a key Latet knows by its name, the others
    counted (a key Latet does not know is the file's own text)."""
    keys = set(keys)
    named = [key for key in KEYS if key in keys]
    others = len(keys - set(KEYS))
    if others:
        named.append(f"{others} key{'s' if others != 1 else ''} that {'are' if others != 1 else 'is'} not a rule")
    return ", ".join(named)


def floor_from(
    settings: ProjectSettings,
    notices: Iterable[str] = (),
    *,
    exceptions: Exceptions = "personal",
    source: Source = "managed",
    etag: str = "",
    received: float = 0.0,
    origin: str = "",
    unread: Iterable[str] = (),
) -> Floor:
    """The floor of a rules file's settings, as ``transfer.rules_of`` reads them (``unread``: the keys it left out
    whole)."""
    data = settings.model_dump(mode="json", exclude_defaults=True)
    keys = frozenset("guidance" if key == "review_guidance" else key for key in data) & frozenset(KEYS)
    rules = Config.model_validate({key: value for key, value in data.items() if key in KEYS})
    if guidance := data.get("review_guidance"):
        rules.llm_review = rules.llm_review.model_copy(update={"guidance": guidance})
    return Floor(rules, exceptions, source, etag, received, keys, tuple(notices), origin, frozenset(unread))


def _unreadable(where: str, exc: RulesError) -> PolicyError:
    """A rules file's refusal as a policy's: a YAML mistake by its place, in Latet's own words only (PyYAML's can quote
    the file: a tag, a character)."""
    if exc.yaml_mistake is None:
        return PolicyError(f"{where}: {exc}")
    problem, at = exc.yaml_mistake
    words = f": {problem}" if problem in (NO_ALIASES, OUT_OF_RANGE) else ""
    return PolicyError(f"{where}: not valid YAML{words}{at}")


def parse_policy(
    text: str,
    *,
    exceptions: Exceptions = "personal",
    source: Source = "managed",
    received: float = 0.0,
    origin: str = "",
) -> Floor:
    """The floor in a policy's text; ``exceptions`` when the text says none (with no value, it says one Latet cannot
    read: `company`). ``PolicyError`` when it cannot be read at all, whatever stops it (a YAML mistake, nesting too deep
    to follow, half of a character, settings that are not valid), in words that never quote it; a key with a mistake,
    or written with no value, is left out whole (``unread``), and said in ``notices``."""
    where = origin or "the policy"
    try:
        data = rules_data(text)
        notices: list[str] = []
        said: Any = data.pop(EXCEPTIONS_KEY, _ABSENT)
        chosen: Exceptions = exceptions
        if said == "personal" or said == "company":
            chosen = said
        elif said is not _ABSENT:  # with no value too (YAML's null): a value forgotten, never the default
            chosen = "company"
            notices.append(f"{EXCEPTIONS_KEY}: personal or company; read as company, the stricter")
        # A key written with no value (`ocr:` alone) is a value forgotten too: it locks nothing, so it is left out
        # whole, as a key with a mistake in it is, and said, never set unseen (read as not set, it would go unsaid).
        empty = [key for key in KEYS if key in data and data[key] is None]
        unread: set[str] = set(empty)
        for key in empty:
            del data[key]
            notices.append(f"{key}: written with no value, so it is not locked")
        if "guidance" in empty:  # a config.yaml's reviewer guidance is read only where the file has none of its own
            data.pop("llm_review", None)
        settings, more = rules_of(data, unread)
        etag = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return floor_from(
            settings,
            [*notices, *more],
            exceptions=chosen,
            source=source,
            etag=etag,
            received=float(received),  # a clock may answer an int: the sealing names the time as a float
            origin=origin,
            unread=unread,
        )
    except RulesError as exc:
        raise _unreadable(where, exc) from None
    except UnicodeError:
        raise PolicyError(f"{where}: it holds half of a character (a lone surrogate)") from None
    except RecursionError:
        raise PolicyError(f"{where}: not valid YAML: nested too deeply") from None
    except (ValueError, TypeError):  # settings a rules file holds that a policy cannot be made of
        raise PolicyError(f"{where}: its rules are not valid settings") from None


def read_policy(path: Path, *, exceptions: Exceptions = "personal", source: Source = "managed") -> tuple[str, Floor]:
    """A policy file's text and the floor in it (UTF-8, or UTF-16 or UTF-32 by the byte order mark Notepad writes:
    E1's ``file_text``), read up to one byte more than a rules file may hold; ``exceptions`` when the file says none.
    The one reading of `latet policy check` and `latet server policy set`, which sends the text. ``PolicyError`` when
    it cannot be read at all, in words that never quote what it holds (a decoding error's would quote a byte)."""
    try:
        data, status = read_regular_snapshot(path, limit=MAX_BYTES + 1, follow=True)
    except FileReadError as failure:
        if failure.reason == "too_large":
            raise PolicyError(f"{path}: larger than {MAX_BYTES // (1024 * 1024)} MB") from None
        why = " (it is not a regular file)" if failure.reason == "nonregular" else ""
        raise PolicyError(f"{path}: cannot read it{why}") from None
    if len(data) > MAX_BYTES:
        raise PolicyError(f"{path}: larger than {MAX_BYTES // (1024 * 1024)} MB")
    try:
        text = file_text(data)
    except ValueError:
        raise PolicyError(f"{path} is not text in UTF-8, or in UTF-16 or UTF-32 with its byte order mark") from None
    floor = parse_policy(text, exceptions=exceptions, source=source, received=status.st_mtime, origin=str(path))
    return text, floor


def policy_json(floor: Floor) -> dict[str, Any]:
    """The policy as a rules file in JSON, only what it sets: what an administrator is shown."""
    dumped = floor.rules.model_dump(mode="json")
    out: dict[str, Any] = {VERSION_KEY: FORMAT}
    for key in KEYS:
        if key in floor.keys:
            out[key] = floor.rules.llm_review.guidance if key == "guidance" else dumped[key]
    return out


def _locked(floor: Floor) -> dict[str, Any]:
    """What the policy locks, without a value: its terms (those it keeps masked: one of a type it allows is its
    exception), values and patterns never to mask, and the files, web addresses and tools strict data mode covers
    counted; its recognizers and field rules by name; whether it has guidance; the rest as it sets it."""
    out = policy_json(floor)
    # A host can name a client, and so can a tool's pattern (an MCP server's name, `mcp__northwind_crm__*`).
    for key in ("terms", "allow_values", "allow_patterns", "data_files", "data_hosts", "data_tools"):
        if key in out:
            out[key] = len(floor.company.terms) if key == "terms" else len(out[key])
    if "recognizers" in out:
        out["recognizers"] = [str(r.get("name")) for r in out["recognizers"] if isinstance(r, dict)]
    if "fields" in out:  # by name, the ones that mask; the ones that keep a field in clear (exceptions) only counted
        clear = [rule for rule in out["fields"] if rule.get("mask") is False]
        out["fields"] = [rule["name"] for rule in out["fields"] if rule.get("mask") is not False]
        if clear:
            out["fields_clear"] = len(clear)
    if "guidance" in out:
        out["guidance"] = True
    return out


def floor_view(floor: Floor) -> dict[str, Any]:
    """A floor as a person's dashboard shows it: its rules name what is locked, never a value (``_locked``), and its
    notices are counted, never quoted; no etag (a person has no use for it). A term, a client's name, a pattern made
    of values stay with the administrators, whose view is the administration API's (``ServerPolicy.admin_view``)."""
    return {
        "source": floor.source,
        "origin": floor.origin,
        "received": floor.received,
        "exceptions": floor.exceptions,
        "rules": _locked(floor),
        "brought": [key for key in KEYS if key in floor.brought],
        "notices": len(floor.notices),
    }


def raise_to_floor(floor: Floor, config: Config) -> Config:
    """The settings in use: ``config`` with what the policy locks raised to it. What is at it or above stays as it is,
    nothing is lowered, nothing of yours goes out of use, and the company's own exceptions are allowed, never added.
    Each of your projects too: Latet stays on there, and what you set for it is raised the same way. The company's terms
    go to the build as locked (``Config.locked_terms``): no exception of yours lets one through, and nothing you leave
    in clear around one leaves it in clear (``Masker.mask``)."""
    return _raised(floor, config).c


def _raised(floor: Floor, config: Config) -> _Raise:
    """``config`` raised to ``floor`` (``raise_to_floor``), with what the raise says it took out of use."""
    raising = _Raise(floor, config.model_copy(deep=True))
    raising.run()
    raising.c.lock_terms(t.model_copy() for t in floor.company.terms)
    if floor.carves_terms:
        raising.c.carve_terms()
    return raising


def below_floor(floor: Floor, config: Config) -> list[str]:
    """Why ``config`` goes below the floor, one reason each in ``protection.loosened``'s words (never a value), and what
    of yours the raise takes out of use as an exception that only the company's count (``_Raise.said``). Empty: it does
    not."""
    raising = _raised(floor, config)
    # A request exception is never weighed here: the raise leaves them as they are, and one not yet approved grants
    # nothing (``protection`` names every such one added, even against itself). What the floor judges is the approval
    # (``decides_exceptions``, weighed by the runtime as one is approved).
    reasons = loosened(_no_exceptions(raising.c), _no_exceptions(config))
    if (config.llm_review.guidance or "") in (raising.c.llm_review.guidance or ""):
        # The company's guidance joined to yours, which is all there: nothing of it is lowered (said for every person,
        # for good, it would say nothing).
        reasons = [reason for reason in reasons if reason != GUIDANCE_CHANGED]
    # ``loosened`` names a term or a field rule of a type let through as leaving what it finds in clear (0.9.8): what
    # the raise took out so is said once.
    return [*reasons, *(reason for reason in raising.said() if reason not in reasons)]


def _no_exceptions(config: Config) -> Config:
    """``config`` without its request exceptions, as the floor weighs it."""
    return config.model_copy(update={"request_exceptions": []}) if config.request_exceptions else config


# --- field rules: which may name the same field -----------------------------------------------------------------------


def _glob_matches(pattern: str, text: str) -> bool:
    """Whether ``text`` matches ``pattern``, where ``*`` matches any characters: its parts in order, the first at the
    start and the last at the end (leftmost is enough for stars alone)."""
    parts = pattern.split("*")
    head, tail = parts[0], parts[-1]
    if len(text) < len(head) + len(tail) or not text.startswith(head) or not text.endswith(tail):
        return False
    at, end = len(head), len(text) - len(tail)
    for part in parts[1:-1]:
        found = text.find(part, at, end)
        if found < 0:
            return False
        at = found + len(part)
    return True


def _globs_meet(a: str, b: str) -> bool:
    """Whether a text matches both ``a`` and ``b``, where ``*`` matches any characters and everything else itself. Two
    with a star each meet when the text before their first star and after their last agree: their longer beginning,
    every part of each between, and their longer end make a text of both."""
    if "*" not in a and "*" not in b:
        return a == b
    if "*" in a and "*" in b:
        head_a, head_b = a.split("*", 1)[0], b.split("*", 1)[0]
        tail_a, tail_b = a.rsplit("*", 1)[-1], b.rsplit("*", 1)[-1]
        heads = head_a.startswith(head_b) or head_b.startswith(head_a)
        return heads and (tail_a.endswith(tail_b) or tail_b.endswith(tail_a))
    return _glob_matches(a, b) if "*" in a else _glob_matches(b, a)


def _may_share_a_field(a: str, b: str) -> bool:
    """Whether a field may be named by both rules ``a`` and ``b``: a rule names a field by its whole name or by its last
    part (``FieldDetector._rule``: after the last dot, without ``[]``), without case. Never false where they may share
    one: so each is weighed against the other's names of a last part, ``_globs_meet`` of each with ``y``, ``*.y``,
    ``y[]``, ``*.y[]``, ``*.y[]*`` and ``y[]*`` of the other. One with a star meets ``*.y[]*`` of any other (their texts
    before the first star and after the last agree with nothing); two without one meet as ``_part_of`` says."""
    a, b = a.lower(), b.lower()
    if "*" in a or "*" in b:
        return True
    return _part_of(a, b) or _part_of(b, a)


def _part_of(a: str, b: str) -> bool:
    """Whether a field name ``a`` holds ``b`` as ``_may_share_a_field`` weighs it (both without a star, in lower case):
    ``a`` is ``b``, or ``b`` is ``a`` up to a ``[]`` of it, or ``a`` after a dot of it, to its end or to a ``[]``."""
    return a == b or a.startswith(f"{b}[]") or a.endswith(f".{b}") or f".{b}[]" in a


_MANY_PARTS = 256  # a name with more parts than this is weighed against each name instead of looked up (200 characters)


def _parts(a: str) -> set[str] | None:
    """Every ``b`` that ``a`` holds (``_part_of``), at most ``_MANY_PARTS`` of them (None when there are more)."""
    brackets = [at for at in range(len(a) - 1) if a.startswith("[]", at)]
    dots = [at for at, character in enumerate(a) if character == "."]
    if len(dots) * (len(brackets) + 1) + len(brackets) > _MANY_PARTS:
        return None
    out = {a, *(a[:at] for at in brackets), *(a[dot + 1 :] for dot in dots)}
    out.update(a[dot + 1 : at] for dot in dots for at in brackets if at > dot)
    return out


class _FieldIndex:
    """The company's field rules found by the names they may share a field with (``_may_share_a_field``), in their
    order: a rule with a star meets every name; two without one are found through what each holds (``_parts``). A
    lookup costs the parts of the name looked up, not the rules: a policy's 500 rules against 500 of yours."""

    def __init__(self, rules: list[FieldRule]) -> None:
        self.rules = rules
        self.everywhere: list[int] = []  # with a star
        self.weighed: list[int] = []  # too many parts to be found by them: weighed against each name
        self.named: dict[str, list[int]] = {}  # the others by their name
        self.held: dict[str, list[int]] = {}  # and by what they hold
        for at, rule in enumerate(rules):
            name = rule.name.lower()
            parts = None if "*" in name else _parts(name)
            if "*" in name:
                self.everywhere.append(at)
            elif parts is None:
                self.weighed.append(at)
            else:
                self.named.setdefault(name, []).append(at)
                for part in parts:
                    self.held.setdefault(part, []).append(at)

    def met(self, name: str) -> list[FieldRule]:
        """The rules a field rule named ``name`` may share a field with, in their order."""
        name = name.lower()
        if "*" in name:
            return list(self.rules)
        found = {*self.everywhere, *self.held.get(name, ())}
        found.update(at for at in self.weighed if _may_share_a_field(name, self.rules[at].name))
        parts = _parts(name)
        if parts is None:
            found.update(at for other, ats in self.named.items() if _part_of(name, other) for at in ats)
        else:
            for part in parts:
                found.update(self.named.get(part, ()))
        return [self.rules[at] for at in sorted(found)]


def _field_type(rule: FieldRule) -> str:
    return found_type(rule)


def _typed_by_you(rule: FieldRule, theirs: FieldRule | None) -> bool:
    """Whether a field rule of yours (``theirs``: the company's of its name, merged into it) masks by a type of yours:
    one that masks, or one the company's is merged into whose type is yours, not the company's."""
    if theirs is None:
        return rule.mask
    return rule.type is not None and _field_type(rule) != _field_type(theirs)


def _yours_lets_through(final: FieldRule, theirs: FieldRule | None, gone: set[str]) -> bool:
    """Whether ``final``, a field rule of yours as it is in use, masks by a type of yours that ``gone`` lets through
    wherever it applies (``_Raise._no_personal_clear``)."""
    return final.mask and _field_type(final) in gone and (theirs is None or _field_type(theirs) != _field_type(final))


# --- recognizers ------------------------------------------------------------------------------------------------------


def _own_policy(recognizer: dict[str, Any]) -> Policy | None:
    try:
        return Policy(str(recognizer["policy"]).lower()) if recognizer.get("policy") else None
    except ValueError:
        return None


def _digest(recognizer: dict[str, Any]) -> str:
    """What a company recognizer placed under a name or a type of its own is suffixed with: a digest of it."""
    return hashlib.sha256(json.dumps(recognizer, sort_keys=True, default=str).encode()).hexdigest()[:8]


def _shape(recognizer: dict[str, Any]) -> str:
    """A recognizer but for its name, its type and its policy: what a company recognizer placed keeps of itself."""
    return json.dumps({k: v for k, v in recognizer.items() if k not in ("name", "type", "policy")}, sort_keys=True)


def _names_of(recognizer: dict[str, Any]) -> str:
    return f"{recognizer.get('name')} {str(recognizer.get('type') or '').lower()}"


def _renamed(name: str, suffix: str) -> str:
    """A recognizer's name with a suffix: lower case letters, digits and hyphens, 64 at most."""
    return f"{name[: 63 - len(suffix)].rstrip('-')}-{suffix}"


def _retyped(etype: str, suffix: str) -> str:
    """A recognizer's type with a suffix: capitals, digits and underscores, 64 at most."""
    return f"{etype[: 63 - len(suffix)]}_{suffix.upper().replace('-', '_')}"


# --- the raise -------------------------------------------------------------------------------------------------------


def _locks(floor: Floor) -> frozenset[str]:
    """What the policy locks: its keys, what its profile brings with it, and the exceptions with `company`."""
    keys = set(floor.keys)
    if "profile" in keys:
        keys.update(_WITH_PROFILE)
    if "id_regions" in keys:
        keys.add("pii")
    if floor.exceptions == "company":
        keys.update(PERSONAL_EXCEPTIONS)
    return frozenset(keys)


def _required_pii(floor: Floor) -> list[str]:
    """The personal-data types the policy wants detected."""
    rules, keys = floor.rules, floor.keys
    wanted: list[str] = []
    if "profile" in keys and rules.profile != Profile.SECRETS:
        wanted += effective_pii(None, [], every_region=rules.profile == Profile.MAX)
    if "pii" in keys:
        wanted += rules.pii or []
    if "id_regions" in keys:
        wanted += types_for(rules.id_regions)
    return list(dict.fromkeys(wanted))


def _pii_in_use(config: Config) -> list[str]:
    if config.profile == Profile.SECRETS:
        return []
    return effective_pii(config.pii, config.id_regions, every_region=config.profile == Profile.MAX)


def _raised_policies(mine: dict[str, Policy], theirs: dict[str, Policy]) -> dict[str, Policy]:
    out = dict(mine)
    for etype, policy in theirs.items():
        if POLICY_STRICTNESS[out.get(etype, Policy.MASK)] < POLICY_STRICTNESS[policy]:
            out[etype] = policy
    return out


def _raised_tools(mine: list[str], theirs: list[str]) -> list[str]:
    """The tools strict data mode covers, never fewer than the policy's: empty is every tool."""
    if not theirs or not mine:
        return []
    return list(dict.fromkeys([*mine, *theirs]))


def _sends(config: Config, entry: ProjectEntry) -> ImagesSetting:
    """What images a project of yours sends: its profile and its images over the global ones."""
    scope = config.model_copy(
        update={"profile": entry.profile or config.profile, "images": entry.images or config.images}
    )
    return image_policy(scope).send


def _stricter(a: Policy, b: Policy) -> Policy:
    return a if POLICY_STRICTNESS[a] >= POLICY_STRICTNESS[b] else b


def _folder(entry: ProjectEntry) -> str:
    """A project's folder as ``protection`` names it in a reason."""
    from latet.projects import norm_path

    path = norm_path(entry.path)
    return Path(path.rstrip("/\\")).name or path


def _require_in(required: dict[str, Policy], etype: str, policy: Policy) -> None:
    """``etype`` required at least as strict as ``policy`` (``_Raise.required``), unless the company allows it."""
    if policy != Policy.ALLOW:  # a type the company allows itself: its own exception
        required[etype] = _stricter(policy, required.get(etype, Policy.MASK))


class _Company:
    """What raising any settings to a floor works out of the policy alone, once for the floor (``Floor.company``): its
    terms, its recognizers in use (each with its type, its digest and its shape, computed once), its field rules and
    their index, and the policies its own terms and fields require."""

    def __init__(self, floor: Floor) -> None:
        self.rules, self.locks = floor.rules, _locks(floor)
        rules, locks = self.rules, self.locks
        # The company's recognizers in use, and the own policy of each type they give (its `policies` winning).
        in_use = [
            (r, t) for r in recognizers_in_force(rules.recognizers).values() if (t := recognizer_type(r)) is not None
        ]
        self.own: dict[str, Policy] = {}
        for recognizer, etype in in_use:
            if (policy := _own_policy(recognizer)) is not None:
                self.own[etype] = policy
        # The terms it locks: one of a type it allows is its exception, as a recognizer or a field rule of one is,
        # never added to yours nor locked, and never taking out an entry of yours of its value (M-2).
        terms = rules.terms if "terms" in locks else []
        self.terms: list[TermConfig] = [t for t in terms if self.policy(t.type.upper()) != Policy.ALLOW]
        self.term_values = {_folded(t.value) for t in self.terms}
        self.terms_by_value: dict[str, list[TermConfig]] = {}
        for term in self.terms:
            self.terms_by_value.setdefault(_folded(term.value), []).append(term)
        # Those that mask: one that leaves what it finds in clear is the company's exception, allowed, never added.
        self.recognizers = [(r, t) for r, t in in_use if self.policy(t) != Policy.ALLOW]
        self.digests = [_digest(r) for r, _ in self.recognizers]
        # The shapes of those placed under a name or a type of their own, and the digests each shape may be placed with.
        self.placed: dict[str, set[str]] = {}
        for (recognizer, _), digest in zip(self.recognizers, self.digests, strict=True):
            self.placed.setdefault(_shape(recognizer), set()).add(digest)
        # The company's field rules that mask (the first of each name, the one in use; one of a type it allows is its
        # exception, as one it keeps in clear), and the names of those it keeps in clear.
        first: dict[str, FieldRule] = {}
        for rule in rules.fields if "fields" in locks else []:
            first.setdefault(rule.name.lower(), rule)
        self.fields = [r for r in first.values() if r.mask and self.policy(_field_type(r)) != Policy.ALLOW]
        self.fields_by_name = {rule.name.lower(): rule for rule in self.fields}
        self.field_at = {id(rule): at for at, rule in enumerate(self.fields)}
        self.field_policies = [self.policy(_field_type(rule)) for rule in self.fields]
        self.field_ranks = [POLICY_STRICTNESS[policy] for policy in self.field_policies]
        self.index = _FieldIndex(self.fields)
        self.kept_in_clear = {name for name, rule in first.items() if not rule.mask}
        # With `exceptions: company`, `allow` for a type stays only for a type the company allows itself. (A company
        # term needs none of this: the build finds it as locked, and nothing you leave in clear leaves it so.)
        self.allowed = {etype for etype, policy in rules.policies.items() if policy == Policy.ALLOW}
        # The policies the types a company term (ruling 4: no `allow` for it) or field rule masks as must have at least.
        self.required: dict[str, Policy] = {}
        for term in self.terms:
            _require_in(self.required, term.type.upper(), self.policy(term.type.upper()))
        for rule, policy in zip(self.fields, self.field_policies, strict=True):
            _require_in(self.required, _field_type(rule), policy)

    def policy(self, etype: str) -> Policy:
        """What a value of ``etype`` is in the company's own settings: its `policies`, else the own policy of its
        recognizer of the type, else masked."""
        return self.rules.policies.get(etype) or self.own.get(etype) or Policy.MASK


class _Raise:
    """``config`` raised in place to ``floor``: its own settings and each project's, with what is worked out once for
    all of them (and, once for the floor, of the policy alone: ``_Company``)."""

    def __init__(self, floor: Floor, config: Config) -> None:
        self.floor, self.c = floor, config
        self.left_out: dict[tuple[str, str], int] = {}  # (folder, what): how many of yours out of use as exceptions
        self.excepted_fields: dict[str, list[int]] = {}  # by folder, where those field rules are among yours
        known = self.known = floor.company
        self.rules, self.locks = known.rules, known.locks
        self.company = floor.exceptions == "company"
        self.terms, self.term_values = known.terms, known.term_values
        self.own, self.fields, self.kept_in_clear, self.allowed = (
            known.own,
            known.fields,
            known.kept_in_clear,
            known.allowed,
        )
        self.no_allow = self.company
        self.required: dict[str, Policy] = dict(known.required)
        self._met: dict[str, list[int]] = {}  # the company's field rules a name of yours may share a field with
        # A rule of yours that may name a company field first masks it as its type (the company's, merged into your
        # rule of its name that has none): that type at least as strict as the company's field; and a company type your
        # rule takes at least as strict as your untyped fields (FIELD).
        # Weighed on the rules as they will be, in their order: each against the company rules after it (merged into a
        # later rule of yours, or last; in a project, the global ones all come after its own).
        untyped: list[tuple[str, Policy]] = []
        ranks, policies = known.field_ranks, known.field_policies
        for holder in [config, *config.projects]:
            top = holder is config
            scope = config.policies if top else {**config.policies, **holder.policies}
            placed: list[tuple[FieldRule, FieldRule | None]] = []  # each rule as it will be, and the company's in it
            for rule, theirs in self._rules_and_merges(holder.fields):
                final = self._final(rule, theirs)
                if final is not None:
                    placed.append((final, theirs))
                if final is not None and theirs is not None and rule.type is None and final.type is not None:
                    untyped.append((_field_type(final), scope.get("FIELD", Policy.MASK)))
            # Where each company rule is: merged into a rule of yours, last at the top, else after all of them.
            at = [len(placed)] * len(self.fields)
            for i, (_, theirs) in enumerate(placed):
                if theirs is not None:
                    at[known.field_at[id(theirs)]] = i
            if top:  # the company's that none of yours is named as: last, in its order
                merged = {id(theirs) for _, theirs in placed if theirs is not None}
                for k, rule in enumerate(self.fields):
                    if id(rule) not in merged:
                        at[k] = len(placed)
                        placed.append((rule, rule))
            for i, (final, theirs) in enumerate(placed):
                if not final.mask:
                    continue
                own = known.field_at.get(id(theirs), -1) if theirs is not None else -1
                best, strictest = -1, Policy.MASK
                for k in self._fields_met_at(final):
                    if k != own and at[k] > i and ranks[k] > best:
                        best = ranks[k]
                        strictest = policies[k]
                if best >= 0:
                    self._require(_field_type(final), strictest)
        for etype, policy in untyped:
            self._require(etype, _stricter(policy, self.required.get("FIELD", Policy.MASK)))

    def _company_policy(self, etype: str) -> Policy:
        return self.known.policy(etype)

    def _require(self, etype: str, policy: Policy) -> None:
        _require_in(self.required, etype, policy)

    def _fields_met_at(self, rule: FieldRule) -> list[int]:
        """The company's field rules ``rule`` may name a field of, by their place (``_FieldIndex``): once a name."""
        name = rule.name.lower()
        if (found := self._met.get(name)) is None:
            field_at = self.known.field_at
            found = self._met[name] = [field_at[id(theirs)] for theirs in self.known.index.met(name)]
        return found

    def _fields_met(self, rule: FieldRule) -> list[FieldRule]:
        """The company's field rules ``rule`` may name a field of."""
        return [self.fields[k] for k in self._fields_met_at(rule)]

    def _rules_and_merges(self, mine: list[FieldRule]) -> Iterable[tuple[FieldRule, FieldRule | None]]:
        """Each rule of ``mine``, with the company's rule of its name when it is the first of its name (the one in use
        for it: the company's is merged into it)."""
        theirs = self.known.fields_by_name
        seen: set[str] = set()
        for rule in mine:
            name = rule.name.lower()
            yield rule, (theirs.get(name) if name not in seen else None)
            seen.add(name)

    def run(self) -> None:
        c = self.c
        self._values(c)
        for entry in c.projects:
            self._entry_values(entry)
        c.policies = self._policies(c.policies, top=True)
        for entry in c.projects:
            entry.policies = self._policies(entry.policies, top=False)
        scopes = [c.policies, *({**c.policies, **entry.policies} for entry in c.projects)]
        kept: list[Any] = []
        for recognizer in c.recognizers:
            etype = recognizer_type(recognizer)  # once: a JSON of it each time
            clear = [self._in_clear(recognizer, etype, scope) for scope in scopes]
            if all(clear):  # out of use: what it found is no longer left in clear anywhere
                continue
            # It masks somewhere of yours: kept, its type masked where it left what it found in clear.
            self._masked_where(str(etype), clear)
            kept.append(recognizer)
        c.recognizers = kept
        for entry in c.projects:
            scope = {**c.policies, **entry.policies}
            entry.recognizers = [r for r in entry.recognizers if not self._in_clear(r, recognizer_type(r), scope)]
        if "recognizers" in self.locks:
            self._company_recognizers()
        gone: dict[int, set[str]] = self._no_personal_clear() if self.no_allow else {}
        c.fields = self._fields(c.fields, gone=gone.get(id(c), set()))
        for entry in c.projects:
            entry.fields = self._fields(entry.fields, gone=gone.get(id(entry), set()), folder=_folder(entry))
        for holder in [c, *c.projects]:
            holder.allow_values, holder.allow_patterns, holder.disabled_secret_rules = self._exceptions(holder)

    # What the policy sets, one value each, and the terms.

    def _values(self, c: Config) -> None:
        rules, locks = self.rules, self.locks
        if "profile" in locks and PROFILE_STRICTNESS[c.profile] < PROFILE_STRICTNESS[rules.profile]:
            c.profile = rules.profile
        if "id_regions" in locks:
            c.id_regions = list(dict.fromkeys([*c.id_regions, *rules.id_regions]))
        if "phone_regions" in locks:
            c.phone_regions = list(dict.fromkeys([*c.phone_regions, *rules.phone_regions]))
        if "pii" in locks and (wanted := _required_pii(self.floor)):
            if c.profile == Profile.SECRETS:  # it detects no personal data at all
                c.profile = Profile.BALANCED
            missing = [t for t in wanted if t not in _pii_in_use(c)]
            if missing and c.pii is None:
                # By their countries: a list written out is used as it is, also in a project of yours whose max profile
                # detects every country's ID numbers, which a list would lose there.
                countries = [country for country in ID_COUNTRIES if set(types_for([country])) & set(missing)]
                c.id_regions = list(dict.fromkeys([*c.id_regions, *countries]))
                missing = [t for t in wanted if t not in _pii_in_use(c)]
            if missing:
                c.pii = [*_pii_in_use(c), *missing]
        if "include_private_ips" in locks and private_ips(rules) and not private_ips(c):
            c.include_private_ips = True
        if "ner" in locks and rules.ner == "auto":
            c.ner = "auto"
        if "ocr" in locks and rules.ocr == "auto":
            c.ocr = "auto"
        # A project of yours may only refuse too: raised here, every project is. It stays once the license ends, as the
        # whole floor does (``Licensing.licensed`` weighs no days).
        if "unknown_members" in locks and rules.unknown_members == "refuse":
            c.unknown_members = "refuse"
        if self.terms:  # the company's first, and none of yours it covers: its value is typed as the company wrote it
            c.terms = [*(t.model_copy() for t in self.terms), *self._terms(c.terms)]
        for key in ("data_files", "data_hosts"):
            if key in locks:
                setattr(c, key, list(dict.fromkeys([*getattr(c, key), *getattr(rules, key)])))
        if "policies" in locks:
            c.policies = _raised_policies(c.policies, rules.policies)
        if "data" in locks and rules.data_mode() == "strict":
            if c.data_mode() != "strict":
                c.data = "strict"
            c.data_tools = _raised_tools(c.data_tools, rules.data_tools)
        limit = rules.dump_guard or None
        if "dump_guard" in locks and limit is not None and (not c.dump_guard or c.dump_guard > limit):
            c.dump_guard = limit
        if "images" in locks:
            send = image_policy(rules).send
            if IMAGES_RANK[image_policy(c).send] < IMAGES_RANK[send]:
                before: list[ImagesSetting] = [_sends(c, entry) for entry in c.projects]
                c.images = send
                for entry, sent in zip(c.projects, before, strict=True):  # a project whose max profile sent none
                    if IMAGES_RANK[_sends(c, entry)] < IMAGES_RANK[sent]:
                        entry.images = sent
        guidance = rules.llm_review.guidance
        if "guidance" in locks and guidance and guidance not in (c.llm_review.guidance or ""):
            c.llm_review = c.llm_review.model_copy(update={"guidance": join_guidance(guidance, c.llm_review.guidance)})

    def _entry_values(self, e: ProjectEntry) -> None:
        """Your settings for a project, raised: Latet stays on there, and what the entry sets in place of the global
        settings is never below the policy (the lists it adds to them may only add)."""
        rules, locks = self.rules, self.locks
        e.enabled = True
        if (
            "profile" in locks
            and e.profile is not None
            and PROFILE_STRICTNESS[e.profile] < PROFILE_STRICTNESS[rules.profile]
        ):
            e.profile = rules.profile
        if "pii" in locks and e.profile == Profile.SECRETS and _required_pii(self.floor):
            e.profile = Profile.BALANCED
        if "include_private_ips" in locks and e.include_private_ips is False and private_ips(rules):
            e.include_private_ips = True
        if "ner" in locks and e.ner == "off" and rules.ner == "auto":
            e.ner = "auto"
        if "ocr" in locks and e.ocr == "off" and rules.ocr == "auto":
            e.ocr = "auto"
        if self.terms:
            e.terms = self._terms(e.terms)
        for key in ("data_files", "data_hosts"):
            if key in locks and getattr(e, key):
                setattr(e, key, list(dict.fromkeys([*getattr(e, key), *getattr(rules, key)])))
        if "policies" in locks:
            e.policies = _raised_policies(e.policies, rules.policies)
        if "data" in locks and rules.data_mode() == "strict":
            if e.data == "normal":
                e.data = "strict"
            if e.data_tools:  # added to the global ones: they would narrow every tool to these
                e.data_tools = list(dict.fromkeys([*e.data_tools, *rules.data_tools])) if rules.data_tools else []
        limit = rules.dump_guard or None
        if "dump_guard" in locks and limit is not None and e.dump_guard is not None and not 0 < e.dump_guard <= limit:
            e.dump_guard = limit
        if "images" in locks and e.images is not None:  # as it is sent there: allow with a max profile sends none
            send = image_policy(rules).send
            if IMAGES_RANK[_sends(self.c, e)] < IMAGES_RANK[send]:
                e.images = send

    def _terms(self, mine: list[TermConfig]) -> list[TermConfig]:
        """``mine`` without the entries a company term covers: of its value in any case, matched in no form it is not.
        One that matches more forms (inside words, where the company's matches whole words) stays."""
        theirs = self.known.terms_by_value

        def covered(term: TermConfig) -> bool:
            for t in theirs.get(_folded(term.value), ()):
                cases = not t.case_sensitive or (term.case_sensitive and _composed(t.value) == _composed(term.value))
                if cases and (not t.whole_word or term.whole_word):
                    return True
            return False

        return [term for term in mine if not covered(term)]

    # What leaves values in clear.

    def _policies(self, policies: dict[str, Policy], *, top: bool) -> dict[str, Policy]:
        """``policies`` with no `allow` where it would leave what the company locks in clear (``no_allow``), and each
        type a company term, field or recognizer masks as at least as strict as the company has it: an entry of yours
        raised, and at the top a redaction written where you have none."""
        out = dict(policies)
        if self.no_allow:
            for etype, policy in policies.items():
                if policy == Policy.ALLOW and etype not in self.allowed:
                    out[etype] = Policy.MASK
        for etype, policy in self.required.items():
            now = out.get(etype)
            weaker = now is not None and POLICY_STRICTNESS[now] < POLICY_STRICTNESS[policy]
            if weaker or (now is None and top and policy == Policy.REDACT):
                out[etype] = policy
        return out

    def _in_clear(self, recognizer: Any, etype: str | None, policies: dict[str, Policy]) -> bool:
        """Whether a recognizer of yours (``etype``: its type in use, ``recognizer_type``), under ``policies``, leaves
        what it finds in clear (its type's policy, or its own where its type has none) where the policy wants nothing
        left so: under ``no_allow``, or when its type is one the company masks as. Such a recognizer leaves the settings
        in use."""
        if etype is None or not isinstance(recognizer, dict):
            return False
        clear = policies[etype] == Policy.ALLOW if etype in policies else keeps_in_clear(recognizer)
        return clear and (self.no_allow or etype in self.required)

    def _masked_where(self, etype: str, clear: list[bool]) -> None:
        """``etype`` masked where something of yours that applies everywhere leaves what it finds in clear: ``clear``
        says where, the top first, then each project of yours (one that takes the top's policy is masked with it)."""
        c = self.c
        if clear[0]:
            c.policies[etype] = Policy.MASK
        for entry, here in zip(c.projects, clear[1:], strict=True):
            if here and (etype in entry.policies or not clear[0]):
                entry.policies[etype] = Policy.MASK

    def _through(self, entry: ProjectEntry | None) -> set[str]:
        """The types the settings in use let through at the top (``entry`` None) or in ``entry``'s project: `allow` in
        their policies, and a recognizer in use whose own policy is allow where they have none of its type."""
        c = self.c
        policies = c.policies if entry is None else {**c.policies, **entry.policies}
        recognizers = c.recognizers if entry is None else [*c.recognizers, *entry.recognizers]
        return set(let_through_by(policies, recognizers))

    def _no_personal_clear(self) -> dict[int, set[str]]:
        """With `exceptions: company`, once the policies are as they will be: a field rule of yours whose type is let
        through is an exception too (it names the field the company's is merged into, and a field's first rule outranks
        strict data mode's, which nothing cuts it around). One of yours that applies everywhere goes out of use where
        its type is let through everywhere, and has its type masked where it is let through in some places only (as a
        recognizer of yours, ``run``); one of a project goes out of use there. A term of yours of such a type stays in
        use: what it leaves in clear is cut around what is masked without it (``Config.carves_terms``,
        ``Masker._carved``), so it leaves in clear nothing masked without it, and one typed EMAIL still brings its
        domain along. The
        types whose field rules go out of use, by holder (``_fields``: one the company's is merged into takes the
        company's type instead)."""
        c = self.c
        types = {_field_type(rule) for rule, theirs in self._rules_and_merges(c.fields) if _typed_by_you(rule, theirs)}
        holders: list[ProjectEntry | None] = [None, *c.projects]
        through = [self._through(holder) for holder in holders]
        gone: dict[int, set[str]] = {id(c): set()}
        for etype in sorted(types):
            clear = [etype in here for here in through]
            if all(clear):
                gone[id(c)].add(etype)
            elif any(clear):
                self._masked_where(etype, clear)
        for entry in c.projects:
            gone[id(entry)] = self._through(entry)
        return gone

    def _left_out(self, folder: str, what: str, count: int) -> None:
        if count:
            key = (folder, what)
            self.left_out[key] = self.left_out.get(key, 0) + count

    def said(self) -> list[str]:
        """What of yours the raise took out of use as an exception (``_no_personal_clear``), in ``loosened``'s words,
        which name it too where they weigh it in the same place (``below_floor`` says it once)."""
        words = "that leave what they find in clear (policy allow)"
        top = [f"{what}: {count} {words}" for (folder, what), count in self.left_out.items() if not folder]
        return top + _by_folders(
            [(folder, f"{what}: {count} {words}") for (folder, what), count in self.left_out.items() if folder]
        )

    def _company_recognizers(self) -> None:
        """The company's recognizers, first: each as written where none of yours, nor any of your projects', has its
        name or its type in use, else under a name and a type of its own (its policy then its own), so that none of
        yours goes out of use and it is in use itself."""
        c = self.c
        known = self.known
        # Yours, but the company's under a name or a type of its own that an earlier raise placed (these settings
        # raised again): they name no recognizer of yours, and the company's is found among them as placed. Each of
        # yours weighed once (its shape a JSON of it), against the digests the company's of its shape are placed with.
        mine = []
        for holder in [c, *c.projects]:
            for r in holder.recognizers:
                if not isinstance(r, dict):
                    continue
                digests = known.placed.get(_shape(r)) if known.placed else None
                if digests and any(digest in _names_of(r) for digest in digests):
                    continue
                mine.append(r)
        names = {str(r.get("name")) for r in mine}
        types = {etype for r in mine if (etype := recognizer_type(r)) is not None}
        using: dict[str, list[dict[str, Any]]] = {}  # yours in use, by name: a candidate is compared with its name's
        for r in recognizers_in_force(c.recognizers).values():
            using.setdefault(str(r.get("name")), []).append(r)
        added: list[dict[str, Any]] = []
        for (recognizer, etype), digest in zip(known.recognizers, known.digests, strict=True):
            placed, present, placed_type = self._placed(recognizer, etype, digest, names, types, using)
            if not present:
                added.append(placed)
                names.add(str(placed["name"]))
            if placed_type is not None:  # placed now or there already: its type as strict as the company has it
                types.add(placed_type)
                self._require(placed_type, self._company_policy(etype))
        c.recognizers = [*added, *c.recognizers]
        if known.recognizers:  # their types' policies, where an entry of yours has one
            c.policies = self._policies(c.policies, top=True)
            for entry in c.projects:
                entry.policies = self._policies(entry.policies, top=False)

    def _placed(
        self,
        recognizer: dict[str, Any],
        etype: str,
        digest: str,
        names: set[str],
        types: set[str],
        using: dict[str, list[dict[str, Any]]],
    ) -> tuple[dict[str, Any], bool, str | None]:
        """The company's ``recognizer`` (of ``etype``, its digest ``digest``) as it joins your settings, whether it is
        in use there already, and the type it is in use as."""
        name = str(recognizer["name"])
        policy = self._company_policy(etype)
        for n in range(len(names) + len(types) + 2):
            candidate = dict(recognizer)
            if policy != (_own_policy(recognizer) or Policy.MASK):  # the company's `policies` for its type: its own
                candidate["policy"] = policy.value
            if n:
                suffix = digest if n == 1 else f"{digest}-{n}"
                if name in names:
                    candidate["name"] = _renamed(name, suffix)
                if etype in types:
                    candidate["type"] = _retyped(etype, suffix)
                    candidate["policy"] = policy.value  # its type's in the company's settings, which no longer names it
            placed = recognizer_type(candidate)
            if candidate in using.get(str(candidate["name"]), ()):
                return candidate, True, placed
            if str(candidate["name"]) not in names and placed is not None and placed not in types:
                return candidate, False, placed
        return dict(recognizer), False, etype  # not reached: there are more candidates than names and types

    def _fields(self, mine: list[FieldRule], *, gone: set[str], folder: str = "") -> list[FieldRule]:
        """Your field rules with the company's: each merged into your first of its name (masked, a secret if either
        says so, typed as yours, or as the company's where yours has none, its policy as strict as FIELD's); one of
        yours that may name a company field first made a secret where the company's is one, and left out where it keeps
        its values in clear, as one the company does not keep in clear with `exceptions: company`, or one of a type in
        ``gone`` (``_no_personal_clear``: one the company's is merged into takes the company's type instead); at the top
        (``folder`` empty) the company's that none of yours is named as, last, where none of yours goes out of use."""
        top = not folder
        merged: set[str] = set()
        out: list[FieldRule] = []
        excepted: list[int] = []
        for at, (rule, theirs) in enumerate(self._rules_and_merges(mine)):
            if theirs is not None:
                merged.add(theirs.name.lower())
            final = self._final(rule, theirs)
            if final is not None and _yours_lets_through(final, theirs, gone):
                excepted.append(at)
                if theirs is None:
                    continue
                final = final.model_copy(update={"type": theirs.type})  # yours let it through: the company's type
            if final is not None:
                out.append(final)
        self._left_out(folder, "field rules", len(excepted))
        if excepted:
            self.excepted_fields[folder] = excepted
        if top:
            out += [rule.model_copy() for rule in self.fields if rule.name.lower() not in merged]
        return out

    def _final(self, rule: FieldRule, theirs: FieldRule | None) -> FieldRule | None:
        """A field rule of yours as it is in use (``_fields``; ``theirs``: the company's of its name merged into it);
        None when it is left out."""
        met = self._fields_met(rule)
        if theirs is not None:
            update = {"mask": True, "secret": rule.secret or theirs.secret, "type": rule.type or theirs.type}
            rule = rule.model_copy(update=update)
        elif not rule.mask:
            if met or (self.company and rule.name.lower() not in self.kept_in_clear):
                return None
            return rule
        elif any(t.secret for t in met) and not rule.secret:
            rule = rule.model_copy(update={"secret": True})
        return rule

    def _exceptions(self, holder: Config | ProjectEntry) -> tuple[list[str], list[str], list[str]]:
        """The values and patterns never to mask, and the secret rules turned off, that count: only the policy's with
        `company`; never a value that is a company term (ruling 4: said so by ``below``; what lets one through, a
        pattern too, is never applied to it, the build finding it as locked)."""
        rules = self.rules
        values, patterns, secret_rules = holder.allow_values, holder.allow_patterns, holder.disabled_secret_rules
        if self.company:
            values = [v for v in values if v in rules.allow_values]
            patterns = [p for p in patterns if p in rules.allow_patterns]
            secret_rules = [r for r in secret_rules if r in rules.disabled_secret_rules]
        if self.terms:
            values = [v for v in values if _folded(v) not in self.term_values]
        return list(values), list(patterns), list(secret_rules)
