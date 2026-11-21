"""A secret's fake keeps the public prefix its vendor puts before every key of its type, and no other character of it.

Until 0.15.0 a secret's fake kept up to 24 characters of letters, digits and separators from the start of the value
(``secret_prefix_length``), which ran into the random part of a token whose characters include `-`, `_` or `.`
(base64url) and kept the words of a value assigned to a name: those characters were sent to the provider inside the
fake. Now the fake keeps the type's public prefix alone (``prefixes.public_prefix``): Latet's own `latet_<kind>_`, the
characters a Gitleaks rule's pattern writes before every key it finds (`ghp_`, `sk-ant-api03-`, `AKIA`: the engine's
table ``RULE_PREFIXES``, which a test here reads again from the rules' patterns), and nothing for any other type.
``latet leak``'s hint shows the same prefix. Each run of letters and digits between signs is drawn from its alphabet
(one case's hexadecimal digits, or the kinds of characters it holds), so that a fake does not show which places of the
value hold a digit, a capital or a small letter; ``shape`` draws every other value's fake as it did. A fake already in
a vault stays as it was kept, so that a conversation under way sees the same fake. Every value here is made up."""

from __future__ import annotations

import importlib
import itertools
import random
import re
import string
import unicodedata
import warnings
from typing import Any

import pytest

from latet.config import Config
from latet.engine.build import build_masker
from latet.engine.detectors.gitleaks import RULESET_VERSION, _etype, load_ruleset, rule_types
from latet.engine.detectors.latet import TOKEN_TYPES
from latet.engine.detectors.recognizers import compile_one
from latet.engine.pipeline import Engine, Session
from latet.engine.surrogates import prefixes
from latet.engine.surrogates.generators import GENERATORS, Drbg, gen_jwt, gen_secret, shape
from latet.engine.surrogates.prefixes import ANY_VALUE, RULE_PREFIXES, RULESET, public_prefix
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Entity, Vault
from latet.leak.scan import hint
from latet.storage.db import Store

from ..conftest import ROOT_KEY
from .test_stored_vault import _many, _Store, _vault

# Letters and digits in no order, cut to the length a rule asks for: a key's random part, made up.
_POOL = "Q7w2K9m3X8l4P6r1Zt5Yb0Nc4Vd8Hs2Jf6Gk1Lp3" * 6
KEYS = [bytes([n]) * 32 for n in range(1, 17)]


def _drawn(n: int) -> str:
    return _POOL[:n]


# The values `latet leak`'s fix round measured (made up), each with its type and what its fake keeps, and how much of
# it the fake kept before (``secret_prefix_length``). Latet's own tokens never reached that function: the vault gives
# them ``gen_latet_token``'s fake (``GENERATORS``), which kept and keeps `latet_<kind>_` alone; ``gen_secret`` keeps
# the same of one.
MEASURED = [
    ("LATET_ADMIN_TOKEN", "latet_adm_" + "Ab3dE-fGh_IjkLmnOpqRstUvwXyz0123456789ab", "latet_adm_", 10),
    ("ASSIGNED_SECRET", "my-company-" + "prod-Xy7abcdef12345", "", 16),
    ("GENERIC_API_KEY", "Kq9w-Ze7_" + "rT2.mB4xLpQ8sVn3HjY6cD1", "", 13),
    ("ANTHROPIC_API_KEY", "sk-ant-api03-" + "AbC_dEf-GhIjKlMnOpQrStUvWxYz012345", "sk-ant-api03-", 21),
    ("GITHUB_PAT", "ghp_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "ghp_", 4),
]

# Keys of the vendors whose prefixes a fake keeps, each found by its Gitleaks rule (made up). The OpenAI and GitLab
# ones begin their random part with a lower-case word and a separator (`abc_`, `abc-`), which a fake took for a
# vendor's group before.
VENDORS = [
    ("GITHUB_PAT", "ghp_" + _drawn(36), "ghp_"),
    ("GITHUB_FINE_GRAINED_PAT", "github_pat_" + _drawn(22) + "_" + _drawn(59), "github_pat_"),
    ("ANTHROPIC_API_KEY", "sk-ant-api03-" + _drawn(93) + "AA", "sk-ant-api03-"),
    ("OPENAI_API_KEY", "sk-proj-" + "abc_" + _drawn(70) + "T3Blbk" + "FJ" + _drawn(74), "sk-proj-"),
    ("AWS_ACCESS_TOKEN", "AKIA" + "Q7W2K3M4X6L5P7RZ", "AKIA"),
    ("SLACK_BOT_TOKEN", "xoxb-" + "1234567890-9876543210-" + _drawn(24), "xoxb-"),
    ("GITLAB_PAT", "glpat-" + "abc-" + _drawn(16), "glpat-"),
    # The rule's second way, whose secret is its second group (``_secret_span``: the first group that is not empty).
    ("ATLASSIAN_API_TOKEN", "ATATT3" + _drawn(186), "ATATT3"),
]


def _chance(fake: str, value: str, start: int) -> tuple[int, int]:
    """Of the letters and digits of ``value`` from ``start`` on, how many its fake has at the same place, and of how
    many."""
    places = [i for i in range(start, len(value)) if value[i].isalnum()]
    return sum(fake[i] == value[i] for i in places), len(places)


def _drawn_after(fakes: list[str], value: str, kept: str, before: int, signs: bool = True) -> None:
    """Each fake keeps ``kept`` and (``signs``) the signs of the value where they are, and draws its letters and
    digits: none holds what the fake kept before past ``kept`` (``before`` characters), and the others meet the value's
    only as often as chance gives. A fake of Latet's own tokens draws their signs too (``gen_latet_token``)."""
    same, total = 0, 0
    for fake in fakes:
        assert fake.startswith(kept) and len(fake) == len(value), fake
        if signs:
            assert [(i, c) for i, c in enumerate(fake) if not c.isalnum()] == [
                (i, c) for i, c in enumerate(value) if not c.isalnum()
            ], fake
        if before > len(kept):
            assert fake[len(kept) : before] != value[len(kept) : before], fake
        hits, places = _chance(fake, value, len(kept))
        same, total = same + hits, total + places
    assert same <= 0.15 * total, (same, total)  # a letter is the same one time in 26, a digit one in 10


@pytest.mark.parametrize(("etype", "value", "kept", "before"), MEASURED)
def test_a_secrets_fake_keeps_its_types_public_prefix_alone(etype, value, kept, before):
    assert public_prefix(etype, value) == kept
    fakes = [gen_secret(value, Drbg(key, b"seed"), etype) for key in KEYS]
    _drawn_after(fakes, value, kept, before)


@pytest.mark.parametrize(("etype", "value", "kept", "before"), MEASURED)
def test_the_vault_gives_a_secret_a_fake_of_its_public_prefix_alone(etype, value, kept, before):
    fakes = [Vault(key).intern(etype, EntityClass.SECRET, value).surrogate for key in KEYS]
    _drawn_after(fakes, value, kept, before, signs=etype not in TOKEN_TYPES.values())


@pytest.mark.parametrize(("etype", "value", "prefix"), VENDORS)
def test_a_vendors_prefix_is_still_kept(etype, value, prefix):
    assert etype in rule_types()
    masker, _ = build_masker(Config(ner="off"), Vault(ROOT_KEY))
    [found] = masker.mask(f"the key {value} here", SlotKind.TOOL_RESULT).replacements
    assert (found.etype, found.original) == (etype, value)  # its own rule finds it, under the type named here
    assert public_prefix(etype, value) == prefix
    assert hint(etype, value) == f"{prefix}… ({len(value)} characters)"
    # The OpenAI and GitLab keys' `abc_` and `abc-` are drawn: they are the key's, not the vendor's.
    _drawn_after([Vault(key).intern(etype, EntityClass.SECRET, value).surrogate for key in KEYS], value, prefix, 0)
    assert all(not fake.startswith(prefix + "abc") for fake in (gen_secret(value, Drbg(k, b"s"), etype) for k in KEYS))


@pytest.mark.parametrize("kind", sorted(TOKEN_TYPES))
def test_latets_own_prefix_is_kept_for_each_of_its_tokens(kind):
    etype = TOKEN_TYPES[kind]
    value = f"latet_{kind}_" + "Ab3dE-fGh_" + _drawn(33)
    assert public_prefix(etype, value) == f"latet_{kind}_"
    assert hint(etype, value) == f"latet_{kind}_… ({len(value)} characters)"
    assert public_prefix("GENERIC_API_KEY", value) == ""  # its prefix is Latet's for its own type alone


@pytest.mark.parametrize(
    ("etype", "value"),
    [
        ("ADAFRUIT_API_KEY", "abcd-efgh_" + _drawn(22)),  # found by its name before it: no prefix of the vendor's
        ("HEROKU_API_KEY", "my-company-prod-" + _drawn(20)),
        ("PASSWORD", "Summer2024-" + "Company-Xy7q9w2kLmNo"),
        ("HASHICORP_TF_PASSWORD", "terraform-" + "admin-pass-9xk2m7q4w"),
        ("JWT", "eyJ" + _drawn(30)),
        ("BEARER_TOKEN", "sk-ant-api03-" + _drawn(40)),  # a vendor's key found by what is around it: its type has none
        ("CARD_REF", "ref-team-a-" + "4k2m9x7q3w"),
    ],
)
def test_a_type_found_by_what_is_around_it_keeps_nothing(etype, value):
    assert public_prefix(etype, value) == ""
    hits, places = 0, 0
    for key in KEYS:
        fake = gen_secret(value, Drbg(key, b"seed"), etype)
        hit, total = _chance(fake, value, 0)
        hits, places = hits + hit, places + total
    assert hits <= 0.15 * places


def test_a_rules_prefix_is_what_its_pattern_writes_before_what_varies():
    assert RULE_PREFIXES["GITHUB_PAT"] == ("ghp_",)
    assert RULE_PREFIXES["OPENAI_API_KEY"] == ("sk-svcacct-", "sk-admin-", "sk-proj-", "sk-")  # the longest first
    assert set(RULE_PREFIXES["AWS_ACCESS_TOKEN"]) == {"A3T", "AKIA", "ASIA", "ABIA", "ACCA"}
    assert set(RULE_PREFIXES["GITHUB_APP_TOKEN"]) == {"ghu_", "ghs_"}
    stripe = {f"{kind}_{mode}_" for kind in ("sk", "rk") for mode in ("test", "live", "prod")}
    assert set(RULE_PREFIXES["STRIPE_ACCESS_TOKEN"]) == stripe
    assert RULE_PREFIXES["ATLASSIAN_API_TOKEN"] == ("ATATT3",)  # its second group's, the secret of its second way
    for etype in ("ADAFRUIT_API_KEY", "GENERIC_API_KEY", "ASSIGNED_SECRET", "CURL_AUTH_HEADER", "PLANETSCALE_PASSWORD"):
        assert etype not in RULE_PREFIXES
    starts = {start for etype_starts in RULE_PREFIXES.values() for start in etype_starts}
    assert starts and all(start.isascii() and all(c.isalnum() or c in "-_.=" for c in start) for start in starts)


def test_a_fake_keeps_what_the_engines_table_says(monkeypatch):
    """The prefixes a fake keeps are the table's, read as a fake is drawn: not the rules' patterns, which the engine
    does not read for them (no parser of Python's own, which a new Python may change)."""
    value = "ghp_" + _drawn(36)
    assert public_prefix("GITHUB_PAT", value) == "ghp_"
    monkeypatch.setitem(RULE_PREFIXES, "GITHUB_PAT", ("gh",))
    assert public_prefix("GITHUB_PAT", value) == "gh"
    assert not hasattr(prefixes, "importlib") and not hasattr(prefixes, "load_ruleset")


# ---- The table, read again from the rules' patterns ------------------------------------------------------------------
#
# What the engine ships in ``RULE_PREFIXES`` is read here from each rule's pattern, as the rules Latet ships write it,
# by Python's own parser of patterns (``re._parser``, private: a new Python may change it, and this test then fails
# rather than a fake keeping other characters). A rule's pattern is written for Go's RE2: what Python reads otherwise is
# written first as Python reads it (``_python``). The secret is the group Gitleaks takes (``gitleaks._secret_span``):
# the rule's ``secretGroup``, else the first group that is not empty, else the whole match.

_PREFIX_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.=")
_STARTS_MOST = 64  # the prefixes a pattern may write, past which it is taken to write none of them
_SET_MOST = 4  # the characters of a set (`fm1[ar]_`, `gh[su]_` as Python writes `(?:ghu|ghs)_`) read as so many
_TEXT_END = re.compile(r"(?<!\\)\\z")
_POSIX_CLASS = re.compile(r"\[:([a-z]+):\]")
_POSIX = {
    "alnum": "a-zA-Z0-9", "alpha": "a-zA-Z", "digit": "0-9", "lower": "a-z", "upper": "A-Z", "xdigit": "0-9a-fA-F",
    "word": r"\w", "space": r"\s",
}  # fmt: skip


class _Ops:
    """The codes of Python's parsed patterns the reading below takes."""

    def __init__(self) -> None:
        constants: Any = importlib.import_module("re._constants")
        self.literal, self.at, self.inside = constants.LITERAL, constants.AT, constants.IN
        self.group, self.branch, self.assert_ = constants.SUBPATTERN, constants.BRANCH, constants.ASSERT
        repeats = ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT")  # the last one since Python 3.11
        self.repeats = tuple(getattr(constants, name) for name in repeats if hasattr(constants, name))
        self.asserts = (constants.ASSERT, constants.ASSERT_NOT)


def _python(pattern: str) -> str | None:
    """``pattern``, a Go one, as Python's parser reads it: no flags inside it (a start is compared without its case),
    `\\z` as `\\Z`, POSIX's classes as ranges; None where it holds a class of POSIX this does not know."""
    pattern = _TEXT_END.sub(r"\\Z", pattern.replace("(?i)", ""))
    if any(name not in _POSIX for name in _POSIX_CLASS.findall(pattern)):
        return None
    return _POSIX_CLASS.sub(lambda m: _POSIX[m[1]], pattern)


def _group(items: Any, number: int, ops: _Ops) -> Any | None:
    """The parsed items of group ``number`` among ``items``, wherever it stands; None where there is none."""
    for op, av in items:
        inner: list[Any] = []
        if op is ops.group:
            if av[0] == number:
                return av[-1]
            inner = [av[-1]]
        elif op is ops.branch:
            inner = list(av[1])
        elif op in ops.repeats:
            inner = [av[2]]
        elif op in ops.asserts:
            inner = [av[1]]
        for sub in inner:
            found = _group(sub, number, ops)
            if found is not None:
                return found
    return None


def _always(items: Any, ops: _Ops, number: int | None = None) -> bool:
    """Whether every match of ``items`` holds group ``number`` (None: any group) and it is not empty: it is not in one
    way of a branch alone, nor in a repeat that may be left out, nor in a look-ahead that refuses."""
    for op, av in items:
        if op is ops.group:
            if av[0] is not None and (number is None or av[0] == number) and av[-1].getwidth()[0] > 0:
                return True
            if _always(av[-1], ops, number):
                return True
        elif op in ops.repeats:
            if av[0] >= 1 and _always(av[2], ops, number):
                return True
        elif op is ops.branch:
            if all(_always(way, ops, number) for way in av[1]):
                return True
        elif op is ops.assert_ and _always(av[1], ops, number):
            return True
    return False


def _secrets(parsed: Any, secret_group: int, ops: _Ops) -> list[Any]:
    """The parsed items a rule's secret may be, as ``gitleaks._secret_span`` takes it: the whole match where the
    pattern has no group; its ``secretGroup``; else each group that may be the first one not empty, and the whole
    match where a match may leave every group empty."""
    groups = parsed.state.groups - 1
    if not groups:
        return [parsed]
    if secret_group:
        found = _group(parsed, secret_group, ops)
        return [] if found is None else [found]
    secrets = []
    for number in range(1, groups + 1):
        secrets.append(_group(parsed, number, ops))
        if _always(parsed, ops, number):
            return secrets
    return secrets if _always(parsed, ops) else [*secrets, parsed]


def _set(av: Any, ops: _Ops) -> list[str] | None:
    """The characters of a set made of a few characters alone (`[ar]`), or None: a range, a class, a negation."""
    if len(av) > _SET_MOST or any(op is not ops.literal for op, _ in av):
        return None
    return [chr(code) for _, code in av]


def _starts(items: list[Any], ops: _Ops) -> tuple[set[str], bool]:
    """What the parsed ``items`` write first, as the strings a text they match begins with (one of them), and whether
    that is the whole of them (so that what follows them is read too). Of a branch, each way's (`sk-` then `proj-`,
    `svcacct-`, `admin-`, or nothing for OpenAI's older keys)."""
    starts = {""}
    for op, av in items:
        if op is ops.at:  # `\b`, `^`: no character
            continue
        if op is ops.literal:
            ways, whole = [chr(av)], True
        elif op is ops.inside and (chars := _set(av, ops)) is not None:
            ways, whole = chars, True
        elif op is ops.group:
            inner, whole = _starts(list(av[-1]), ops)
            ways = sorted(inner)
        elif op is ops.branch:
            ways, whole = [], True
            for branch in av[1]:
                inner, complete = _starts(list(branch), ops)
                ways += sorted(inner)
                whole = whole and complete
        else:
            return starts, False
        grown = {start + way for start in starts for way in ways}
        if len(grown) > _STARTS_MOST:
            return starts, False
        starts = grown
        if not whole:
            return starts, False
    return starts, True


def _pattern_starts(pattern: str, secret_group: int) -> set[str]:
    """What ``pattern`` writes first in its secret, each start cut before the first character no prefix holds, the
    empty one left out."""
    written = _python(pattern)
    assert written is not None, pattern
    parser: Any = importlib.import_module("re._parser")
    with warnings.catch_warnings():  # Go's sets written as Python warns of (`[[`): a test, one thread
        warnings.simplefilter("ignore")
        parsed = parser.parse(written)
    ops = _Ops()
    found = set()
    for secret in _secrets(parsed, secret_group, ops):
        for start in _starts(list(secret), ops)[0]:
            end = next((i for i, c in enumerate(start) if c not in _PREFIX_CHARACTERS), len(start))
            if end:
                found.add(start[:end])
    return found


def _derived() -> dict[str, tuple[str, ...]]:
    """The table, read from the shipped rules: each type a fake keeps a prefix of (no type any value can have, no
    password's), its prefixes the longest first."""
    rules, _ = load_ruleset()
    found: dict[str, set[str]] = {}
    for rule in rules:
        found.setdefault(_etype(rule.id), set()).update(_pattern_starts(rule.regex.pattern, rule.secret_group))
    return {
        etype: tuple(sorted(starts, key=lambda start: (-len(start), start)))
        for etype, starts in sorted(found.items())
        if starts and etype not in ANY_VALUE and "PASSWORD" not in etype
    }


def test_the_engines_table_is_what_the_shipped_rules_write():
    """A new ruleset, or a Python whose parser reads a pattern otherwise, fails here: the table is written again from
    what this reads (the prefixes are the vendors' text, read by eye), and ``RULESET`` set to the ruleset's version."""
    assert RULESET == RULESET_VERSION
    derived = _derived()
    pairs = {etype: (RULE_PREFIXES.get(etype), derived.get(etype)) for etype in {*derived, *RULE_PREFIXES}}
    assert {etype: pair for etype, pair in pairs.items() if pair[0] != pair[1]} == {}  # (the table's, the rules')
    assert list(RULE_PREFIXES) == sorted(RULE_PREFIXES) and set(RULE_PREFIXES) <= rule_types()


# ---- A secret's fake: each run drawn from its alphabet ---------------------------------------------------------------

_DIGITS, _SMALL, _CAPITALS = string.digits, string.ascii_lowercase, string.ascii_uppercase
_KINDS = (_DIGITS, _SMALL, _CAPITALS)


def _runs(value: str) -> list[tuple[int, str]]:
    """Each run of letters and digits of ``value`` (no sign in it), with where it starts."""
    runs, at = [], 0
    for alnum, chars in itertools.groupby(value, key=str.isalnum):
        run = "".join(chars)
        if alnum:
            runs.append((at, run))
        at += len(run)
    return runs


def _alphabet(run: str) -> str:
    """What a run of a secret's fake is drawn from at every place: one case's hexadecimal digits where the run holds
    nothing else (`9f2c4e7a`, `0A6C`, `deadbeef`); else every kind of character it holds (`1234`: the digits; `0x9f2c`:
    the digits and the small letters; `Summer2024`: all 62)."""
    if any(c in "abcdefABCDEF" for c in run):
        for digits in ("0123456789abcdef", "0123456789ABCDEF"):
            if all(c in digits for c in run):
                return digits
    return "".join(kind for kind in _KINDS if any(c in kind for c in run))


def _kind(c: str) -> str:
    """The kind of a letter or digit: a digit, a small letter, a capital."""
    return next(kind for kind in _KINDS if c in kind)


def _drawn_from_alphabets(fakes: list[str], value: str, kept: str) -> None:
    """Each fake keeps ``kept``, the value's length and its signs where they are, and draws each run of letters and
    digits after ``kept`` from the run's alphabet (``_alphabet``) at every place: the kind of a fake's character (a
    digit, a small letter, a capital) is the value's at that place no more often than chance gives (by class, it was at
    every place), and no place has one kind in every fake where its run's alphabet has more than one."""
    places = [(len(kept) + at, run) for at, run in _runs(value[len(kept) :])]
    same, total = 0, 0
    for fake in fakes:
        assert fake.startswith(kept) and len(fake) == len(value), fake
        assert [(i, c) for i, c in enumerate(fake) if not c.isalnum()] == [
            (i, c) for i, c in enumerate(value) if not c.isalnum()
        ], fake
        for at, run in places:
            drawn, alphabet = fake[at : at + len(run)], _alphabet(run)
            assert all(c in alphabet for c in drawn), (fake, run, alphabet)
            same += sum(_kind(c) == _kind(r) for c, r in zip(drawn, run, strict=True))
            total += len(run)
    assert total and same <= 0.75 * total, (same, total)
    for at, run in places:
        if len({_kind(c) for c in _alphabet(run)}) > 1:
            assert all(len({_kind(fake[i]) for fake in fakes}) > 1 for i in range(at, at + len(run))), (value, run)


_HEX = "9f2c4e7a1b3d5f8e0a6c4d2b8e1f3a5c"  # 32 hexadecimal digits, made up

# Secrets whose fake was drawn place by place by the class of each (a digit for a digit, a capital for a capital), so
# that it showed which places of the value hold a digit, a capital or a small letter: each with the type it is found
# as and what its fake keeps. Made up.
SECRETS = [
    ("GENERIC_API_KEY", "pk_" + _HEX[:20], ""),  # a word before it, found by Gitleaks' generic rule
    ("GENERIC_API_KEY", "0x" + _HEX * 2, ""),  # an EVM private key as a `.env` writes it: 66 of 66 places shown
    ("ASSIGNED_SECRET", "key" + _HEX, ""),  # glued to a word: 35 of 35
    ("ASSIGNED_SECRET", "prod-" + "9f2c4e7a-1b3d5f8e-0a6c4d2b", ""),  # runs between signs
    ("ASSIGNED_SECRET", "key_" + _HEX[:20].upper(), ""),  # upper case
    ("SOURCEGRAPH_ACCESS_TOKEN", "sgp_" + _HEX[:20] * 2, "sgp_"),  # a vendor's prefix kept before it
    ("LOB_API_KEY", "live_" + _HEX + "7d9", "live_"),
    ("GITHUB_PAT", "ghp_" + _drawn(36), "ghp_"),  # a token: about 53 of its 214 bits shown
    ("ASSIGNED_SECRET", "Xy7q9w2kLmNoPq3RsT5uVw", ""),  # a run of every kind
    ("PASSWORD", "Summer2024!" + "Autumn7", ""),  # a password, whose pattern helps a guesser most
    ("URL_PASSWORD", "S3cr3t-" + "Pa55w0rd", ""),
]


@pytest.mark.parametrize(("etype", "value", "kept"), SECRETS)
def test_a_secrets_fake_draws_each_run_from_its_alphabet(etype, value, kept):
    """A secret's fake drew each place by its class: a hexadecimal key glued to `0x` showed 61 of its 256 bits, a
    `ghp_` token about 53 of its 214. Each run between signs is drawn from its alphabet now, by ``gen_secret`` and by
    the vault (a password's fake too, ``_SHAPE_SECRETS``)."""
    fakes = [gen_secret(value, Drbg(key, b"seed"), etype) for key in KEYS]
    fakes += [Vault(key).intern(etype, EntityClass.SECRET, value).surrogate for key in KEYS]
    _drawn_from_alphabets(fakes, value, kept)


def test_a_run_of_one_kind_is_drawn_from_its_kind():
    """A run of one kind of character is drawn from that kind, which says nothing more of it: a word (`prod`) from the
    small letters, a number (`1234`) from the digits, a word of hexadecimal letters (`cafe`) from the hexadecimal
    digits, as a hexadecimal run is."""
    value = "prod-" + "cafe-" + "1234-" + "9f2c4e7a1b3d"
    fakes = [gen_secret(value, Drbg(key, b"seed"), "ASSIGNED_SECRET") for key in KEYS]
    for fake in fakes:
        assert fake[4] == fake[9] == fake[14] == "-", fake
        assert all(c in _SMALL for c in fake[:4]), fake
        assert all(c in "0123456789abcdef" for c in fake[5:9] + fake[15:]), fake
        assert all(c in _DIGITS for c in fake[10:14]), fake
    assert any(c in _DIGITS for fake in fakes for c in fake[5:9])  # `cafe` is drawn from the hexadecimal digits


def test_a_jwts_fake_keeps_eyj_and_its_dots_and_draws_the_rest_from_each_runs_alphabet():
    value = "eyJ" + _drawn(33) + "." + "eyJ" + _drawn(20) + "-" + _drawn(25) + "." + _drawn(20) + "_" + _drawn(22)
    fakes = [gen_jwt(value, Drbg(key, b"seed")) for key in KEYS]
    fakes += [Vault(key).intern("JWT", EntityClass.SECRET, value).surrogate for key in KEYS]
    assert all(fake.count(".") == 2 for fake in fakes), fakes
    for n, part in enumerate(value.split(".")):
        _drawn_from_alphabets([fake.split(".")[n] for fake in fakes], part, "eyJ" if part.startswith("eyJ") else "")


def _none(original: str, rng: Drbg) -> str:
    raise ValueError("no fake of its kind")


def test_a_secret_whose_generator_gives_no_fake_gets_one_drawn_from_its_alphabets(monkeypatch):
    """The vault's fallback where a value's own generator gives no fake (``Vault._generate``): a secret's is drawn as a
    secret's fake is; any other value's is its ``shape``, a letter for a letter and a digit for a digit."""
    value = "Xy7q9w2kLmNoPq3RsT5uVw"
    _drawn_from_alphabets(
        [Vault(key).intern("BADGE", EntityClass.SECRET, value, generate=_none).surrogate for key in KEYS], value, ""
    )
    for key in KEYS:
        fake = Vault(key).intern("BADGE", EntityClass.CUSTOM, value, generate=_none).surrogate
        assert [_kind(c) for c in fake] == [_kind(c) for c in value], fake
    monkeypatch.setitem(GENERATORS, "JWT", _none)
    jwt = "eyJ" + _drawn(33) + "." + "eyJ" + _drawn(40) + "." + _drawn(43)
    _drawn_from_alphabets([Vault(key).intern("JWT", EntityClass.SECRET, jwt).surrogate for key in KEYS], jwt, "")


# ---- ``shape`` for every other value: eed4c39d's ---------------------------------------------------------------------


def _shape_of_eed4c39d(original: str, rng: Drbg, keep: int = 0) -> str:
    """eed4c39d's ``shape``, copied: a value's fake of the same length and kinds of characters, the hexadecimal digits
    drawn from where all it draws is hexadecimal."""
    head, tail = original[:keep], original[keep:]
    hex_lower = bool(tail) and all(c in "0123456789abcdef" for c in tail) and any(c.isalpha() for c in tail)
    hex_upper = bool(tail) and all(c in "0123456789ABCDEF" for c in tail) and any(c.isalpha() for c in tail)
    out = []
    for c in tail:
        if hex_lower:
            out.append(rng.choice("0123456789abcdef"))
        elif hex_upper:
            out.append(rng.choice("0123456789ABCDEF"))
        elif c in _CAPITALS:
            out.append(rng.choice(_CAPITALS))
        elif c in _SMALL:
            out.append(rng.choice(_SMALL))
        elif c in _DIGITS:
            out.append(rng.choice(_DIGITS))
        elif c.isdecimal():
            out.append(chr(ord(c) - unicodedata.decimal(c) + rng.below(10)))
        elif c in _UNSAFE or (not c.isprintable() and c not in "\n\r\t"):
            out.append(rng.choice(_SMALL))
        elif c.isalpha():
            out.append(rng.choice(_SMALL if c.islower() else _CAPITALS))
        else:
            out.append(c)
    return head + "".join(out)


_UNSAFE = '"' + "'" + chr(92) + "`$"  # a quote, a backslash, a backtick, a dollar sign
_OTHER = (
    "\N{LATIN SMALL LETTER N WITH TILDE}\N{LATIN CAPITAL LETTER E WITH ACUTE}\N{LATIN SMALL LETTER SHARP S}"
    "\N{ARABIC-INDIC DIGIT THREE}\N{SUPERSCRIPT TWO}\N{CJK UNIFIED IDEOGRAPH-4E2D}"
)
_WRITTEN = (
    string.ascii_letters + string.digits + "-_.:/@+= " + _UNSAFE + string.whitespace + chr(0) + chr(0x7F) + _OTHER
)


def test_shape_is_eed4c39ds_for_every_value_but_a_secrets_fake():
    """Round 1 drew a run of hexadecimal digits from them in every caller of ``shape``: a recognizer of yours
    (`surrogate: shape`, the same length and kinds of characters), an e-mail address's local part, a link, a word of a
    company, a value of a structure. ``shape`` is eed4c39d's again, for random values (made up) and ``keep``."""
    chosen = random.Random(20261116)
    pools = (_WRITTEN, "0123456789abcdef", "0123456789ABCDEF", "0123456789abcdef-_.", _DIGITS, _SMALL + "-")
    for n in range(4000):
        pool = chosen.choice(pools)
        value = "".join(chosen.choice(pool) for _ in range(chosen.randrange(0, 40)))
        keep = chosen.randrange(0, len(value) + 2)
        key, seed = bytes([n % 256]) * 32, f"seed-{n}".encode()
        assert shape(value, Drbg(key, seed), keep) == _shape_of_eed4c39d(value, Drbg(key, seed), keep), (value, keep)


@pytest.mark.parametrize(
    ("pattern", "example"),
    [
        (r"\b[a-z]{3}-[a-z][0-9][a-z][0-9][a-z][0-9]\b", "host srv-a1b2c3 is up"),
        (r"\b[A-Z]{3}-[0-9]{4}[A-Z]{2}\b", "badge EMP-1234AB"),
    ],
)
def test_a_recognizers_fake_matches_its_pattern_again(pattern, example):
    """A recognizer's fake (`surrogate: shape`) keeps the kind of each place, so that its own pattern finds it: round 1's
    `a1b2c3` and `1234AB` were drawn from the hexadecimal digits, and the check of its examples warned."""
    recognizer, status = compile_one(
        {"name": "an-id", "type": "AN_ID", "pattern": pattern, "examples": {"match": [example]}}
    )
    assert recognizer is not None and (status.state, status.warnings) == ("active", [])


@pytest.mark.parametrize(
    ("etype", "value"),
    [
        ("GITLAB_PAT", "glpat-" + "." * 20),
        ("SLACK_BOT_TOKEN", "xoxb-" + "-" * 20),
        ("LATET_ADMIN_TOKEN", "latet_adm_" + "-" * 30),
        ("GITHUB_PAT", "ghp_" + "\N{SUPERSCRIPT TWO}" * 36),
    ],
)
def test_a_prefix_is_kept_only_before_what_a_fake_draws(etype, value):
    """57de9a6d's guard: a prefix is one only before eight letters or decimal digits, which a fake draws; before signs
    alone, its fake would be the value itself."""
    assert public_prefix(etype, value) == ""
    assert Vault(ROOT_KEY).intern(etype, EntityClass.SECRET, value).surrogate != value


# A value kept by a Latet before 0.15.0, with the fake it gave it then (the words of the value kept, the rest drawn).
OLD_VALUE = "my-company-" + "prod-Xy7abcdef12345"
OLD_FAKE = "my-company-" + "prod-Qd1vertmw83521"
OLD_TEXT = f"token = {OLD_VALUE}"


def _old(vault: Vault) -> Entity:
    return Entity(
        vault.entity_id("ASSIGNED_SECRET", OLD_VALUE), "ASSIGNED_SECRET", EntityClass.SECRET, OLD_VALUE, OLD_FAKE, 0,
        0.0, "assignment:token",
    )  # fmt: skip


def _masked_with(vault: Vault) -> str:
    masker, _ = build_masker(Config(ner="off"), vault)
    masked = Engine(vault, masker).mask_text(OLD_TEXT, SlotKind.TOOL_RESULT, Session("t", kept=False))
    [replacement] = masked.replacements
    assert replacement.original == OLD_VALUE and OLD_VALUE not in masked.text
    return replacement.replacement


def test_a_fake_a_computers_vault_kept_before_is_the_values_fake_still(tmp_path):
    """A computer's vault, read back from its database: the value is masked with the fake it kept, as the
    conversations under way were shown it; and so is a value another process kept meanwhile, which the store answers
    with as the vault keeps it (``Store.save_entity``)."""
    path = tmp_path / "latet.db"
    store = Store(path, ROOT_KEY)
    assert store.save_entity(_old(Vault(ROOT_KEY))) is None
    read = Vault(ROOT_KEY, Store(path, ROOT_KEY).load_entities(), on_insert=store.save_entity)
    assert _masked_with(read) == OLD_FAKE
    elsewhere = Vault(ROOT_KEY, on_insert=Store(path, ROOT_KEY).save_entity)  # loaded before the value was kept
    assert _masked_with(elsewhere) == OLD_FAKE
    assert [e.surrogate for e in Store(path, ROOT_KEY).load_entities()] == [OLD_FAKE]


def test_a_fake_a_servers_vault_kept_before_is_the_values_fake_still():
    """A space's vault on a server, the value out of its memory: the store's entry, with its fake, is the value's."""
    store = _Store()
    vault = _vault(store)
    store.save_entity(_old(vault))
    _many(vault, 200)
    assert _old(vault).id not in {e.id for e in vault.entities()}
    assert _masked_with(vault) == OLD_FAKE
    assert [e.surrogate for e in store.rows.values() if e.original == OLD_VALUE] == [OLD_FAKE]
