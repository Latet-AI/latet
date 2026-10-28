"""Format-preserving surrogate generators.

Every generator is a pure function of (original, rng): the same seed always gives the same fake,
which keeps masked conversations byte-stable. Surrogates keep the shape a model expects (prefix,
length, character classes, valid checksums) but never contain quotes, backslashes or ``$``, so they
survive JSON and shell quoting unchanged. A secret shorter than ``SECRET_FAKE_SHORTEST`` characters
is the exception: its fake has that many letters and digits, of its kinds, its signs in place
(``gen_short_secret``), so that no id or number of a later text holds it by chance.

A fake never shows its original: a word drawn from a list (a name, a company, a town, a host's word)
is never one of the original's words, nor holds one, nor is a part of one, nor holds the whole
original (``_unlike``), so ``emily`` never gets ``emily`` and two digits; and a fake the vault is given
that holds its original all the same, whatever made it, is drawn again (``Vault.intern``).

A fake drawn from a list runs out where a vault holds more values of its form than the list has fakes (a first name
alone: about 80; a public IPv4 address: 762 in the blocks of the documentation). Such a generator has wider forms
(``wide``, ``WIDER``), tried in turn by the vault once the narrower ones are taken: the full lists of both languages,
then digits as people write them (`luca84@`, `luca.rossi84@`), more given names, more company words, towns made from
family names, and for IPv4 the benchmarking block too, then every address of the blocks in turn (``every_ip``). The
first form (``wide`` 0) is the one Latet always drew: a value that finds a free fake there gets the fake it always got.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import ipaddress
import re
import string
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from latet.engine.detectors import ids
from latet.engine.detectors.pii import IBAN_LENGTHS
from latet.engine.surrogates.data.names import (
    ADDRESS_WORDS,
    COMPANIES,
    ENGLISH,
    ENGLISH_TOWN_ENDINGS,
    ENGLISH_TOWN_PARTS,
    FIRST_NAMES,
    ITALIAN,
    ITALIAN_TOWN_PARTS,
    ITALIAN_TOWN_WORDS,
    LAST_NAMES,
    PUBLIC_MAIL_DOMAINS,
    STREET_PARTICLES,
    STREET_TYPES,
    Names,
)
from latet.engine.types import SECRET_FAKE_SHORTEST


class Drbg:
    """Deterministic random generator: HMAC-SHA256 in counter mode (stable across Python versions)."""

    def __init__(self, key: bytes, seed: bytes) -> None:
        self._key = key
        self._seed = seed
        self._counter = 0
        self._pool = b""

    def _bytes(self, n: int) -> bytes:
        while len(self._pool) < n:
            block = hmac.new(self._key, self._seed + self._counter.to_bytes(8, "big"), hashlib.sha256).digest()
            self._counter += 1
            self._pool += block
        out, self._pool = self._pool[:n], self._pool[n:]
        return out

    def below(self, n: int) -> int:
        """Uniform integer in [0, n) by rejection sampling."""
        if n <= 1:
            return 0
        nbytes = (n.bit_length() + 7) // 8
        limit = (256**nbytes // n) * n
        while True:
            value = int.from_bytes(self._bytes(nbytes), "big")
            if value < limit:
                return value % n

    def choice(self, seq: Sequence[str]) -> str:
        return seq[self.below(len(seq))]


_UPPER = string.ascii_uppercase
_LOWER = string.ascii_lowercase
_DIGITS = string.digits
_HEX_LOWER = "0123456789abcdef"
_HEX_UPPER = "0123456789ABCDEF"
# Characters a surrogate never carries: they would need escaping in JSON or a shell.
_UNSAFE = set("\"'\\`$")


def shape(original: str, rng: Drbg, keep: int = 0) -> str:
    """Same length and character classes; separators, whitespace and the first ``keep`` chars kept."""
    head, tail = original[:keep], original[keep:]
    hex_lower = bool(tail) and all(c in _HEX_LOWER for c in tail) and any(c.isalpha() for c in tail)
    hex_upper = bool(tail) and all(c in _HEX_UPPER for c in tail) and any(c.isalpha() for c in tail)
    out = []
    for c in tail:
        if hex_lower:
            out.append(rng.choice(_HEX_LOWER))
        elif hex_upper:
            out.append(rng.choice(_HEX_UPPER))
        elif c in _UPPER:
            out.append(rng.choice(_UPPER))
        elif c in _LOWER:
            out.append(rng.choice(_LOWER))
        elif c in _DIGITS:
            out.append(rng.choice(_DIGITS))
        elif c.isdecimal():  # a digit of another script (Arabic, Devanagari, full width): one of the same script
            out.append(chr(ord(c) - unicodedata.decimal(c) + rng.below(10)))
        elif c in _UNSAFE or (not c.isprintable() and c not in "\n\r\t"):
            out.append(rng.choice(_LOWER))
        elif c.isalpha():
            out.append(rng.choice(_LOWER if c.islower() else _UPPER))
        else:
            out.append(c)
    return head + "".join(out)


_FIXED_PREFIXES = ("AKIA", "ASIA", "AGPA", "AIDA", "AROA", "AIPA", "ANPA", "ANVA", "APKA", "ABIA", "ACCA", "AIza")
_SEPARATED_PREFIX = re.compile(r"^(?:[A-Za-z][A-Za-z0-9]{0,11}[-_.])+")


def secret_prefix_length(value: str) -> int:
    """How much of a token is a public, typed prefix (``sk-ant-api03-``, ``ghp_``, ``AKIA``...)."""
    for p in _FIXED_PREFIXES:
        if value.startswith(p):
            return len(p)
    m = _SEPARATED_PREFIX.match(value[:24])
    if m and len(value) - m.end() >= 8:
        return m.end()
    return 0


def gen_secret(original: str, rng: Drbg) -> str:
    return shape(original, rng, keep=secret_prefix_length(original))


_SHORT_DRAWS = 64  # fakes of a short secret drawn in turn until one does not hold the value (a one-digit `7`: a few)
# The digits of a short number's fake (`1234`): a few more than ``SECRET_FAKE_SHORTEST``, as ten kinds of characters
# make fewer strings than sixteen, and logs are full of long numbers (a time in milliseconds has thirteen digits, a
# snowflake id nineteen). Fifteen digits are still a number JSON and JavaScript read exactly (below 2^53).
SHORT_NUMBER_FAKE = 15


def _kept_sign(c: str) -> bool:
    """A character of a short secret its fake keeps in place: a sign or a space, as ``shape`` keeps one; never one a
    fake must not carry (``_UNSAFE``, a control character), which is drawn as a letter or a digit is."""
    return not c.isalnum() and c not in _UNSAFE and (c.isprintable() or c in "\n\r\t")


def _short_kinds(characters: str) -> tuple[str, bool]:
    """The characters a short secret's fake draws from, and whether they are digits, by its letters and digits:
    digits for digits, hexadecimal digits in its case for hexadecimal, else letters in its cases and digits."""
    if characters and all(c in _DIGITS for c in characters):
        return _DIGITS, True
    hexadecimal = any(c.isalpha() for c in characters)
    if hexadecimal and all(c in _HEX_LOWER for c in characters):
        return _HEX_LOWER, False
    if hexadecimal and all(c in _HEX_UPPER for c in characters):
        return _HEX_UPPER, False
    upper = any(c.isupper() for c in characters)
    lower = any(c.islower() for c in characters) or not upper
    return (_LOWER if lower else "") + (_UPPER if upper else "") + _DIGITS, False


def gen_short_secret(original: str, rng: Drbg) -> str:
    """The fake of a secret shorter than ``SECRET_FAKE_SHORTEST`` characters (0.10.2): its signs and spaces in place
    (``_kept_sign``), and between them runs drawn from the kinds of the value's letters and digits (``_short_kinds``),
    its longest run (the first of the longest) drawn longer, so that the fake has ``SECRET_FAKE_SHORTEST`` letters and
    digits, ``SHORT_NUMBER_FAKE`` for a number. A fake of the value's own shape, as 0.10.0 gave it, could be held by
    chance inside an id or a number of a later text (`5821` in `order_id=9958213`), and restored there; this one is
    not. Its runs stand as the value's do, so that a text that glues letters to it is read as one that glues them to
    the value: where a sign ends the value (`Summer2024!`, `abcdefg-h`), its longest run is found by its edge, as the
    value's is (``anchors.edges``), where a fake of one run glued on both sides would be found by no word. A number
    keeps its sign and its decimal point, no zero first (unless the value has one and more digits after it, `0042`, no
    JSON number) and none last after a point, so that a JSON number stays one, read and written again as it was
    (`-12`, `1.5`, `0`: fifteen digits, which a double holds exactly). One that holds the value is drawn again, from the
    same ``rng``: the same value gets the same fake."""
    kept = [_kept_sign(c) for c in original]
    runs: list[tuple[int, int]] = []  # each run of drawn characters, by where it starts and ends in the value
    for at, sign in enumerate(kept):
        if not sign:
            if runs and runs[-1][1] == at:
                runs[-1] = (runs[-1][0], at + 1)
            else:
                runs.append((at, at + 1))
    kinds, number = _short_kinds("".join(c for c in original if c.isalnum()))
    lengths = [end - start for start, end in runs] or [0]
    longest = lengths.index(max(lengths))
    lengths[longest] += max(0, (SHORT_NUMBER_FAKE if number else SECRET_FAKE_SHORTEST) - sum(lengths))
    head = original[runs[0][0] : runs[0][1]] if runs else ""
    zero_first = not number or (len(head) > 1 and head[0] == "0")
    zero_last = not number or len(runs) < 2 or original[runs[-1][0] - 1] != "."
    folded = original.casefold()
    fake = ""
    for _ in range(_SHORT_DRAWS):
        drawn = []
        for n, length in enumerate(lengths):
            run = [rng.choice(kinds) for _ in range(length)]
            if run and n == 0 and not zero_first:
                run[0] = rng.choice(_DIGITS[1:])
            if run and n == len(lengths) - 1 and not zero_last:
                run[-1] = rng.choice(_DIGITS[1:])
            drawn.append("".join(run))
        pieces, cursor = [], 0
        for (start, end), run in zip(runs, drawn, strict=False):
            pieces += [original[cursor:start], run]
            cursor = end
        fake = "".join(pieces) + original[cursor:] + ("" if runs else drawn[0])
        if folded not in fake.casefold():
            break
    return fake  # one that still holds it (no draw in 64 did) is refused by the vault, which draws its next counter


def gen_latet_token(original: str, rng: Drbg) -> str:
    """Only the public credential prefix stays (``latet_pat_``, ``latet_ck_``, ``latet_scim_``, ``latet_brg_``,
    ``latet_code_``: up to the underscore after its kind); every URL-safe character of its secret tail is drawn."""
    end = original.find("_", len("latet_")) + 1 if original.startswith("latet_") else 0
    return original[:end] + gen_latet_route_key(original[end:], rng)


def gen_latet_route_key(original: str, rng: Drbg) -> str:
    """A route key has no public prefix, even if it holds separators or consists of signs alone."""
    return "".join(rng.choice(_UPPER + _LOWER + _DIGITS + "_-") for _ in original)


def gen_jwt(original: str, rng: Drbg) -> str:
    parts = original.split(".")
    return ".".join(shape(p, rng, keep=3 if p.startswith("eyJ") else 0) for p in parts)


def _styled(word: str, like: str) -> str:
    if like.isupper():
        return word.upper()
    if like.islower():
        return word.lower()
    return word


_WORD = re.compile(r"[^\W\d_]+")
_ITALIAN_KNOWN = ITALIAN.known()
_ENGLISH_KNOWN = ENGLISH.known()
# A word of an original this long or longer is never held in a word drawn for its fake, and a drawn word this long or
# longer is never a part of one of its words: `Anna` for `Annabella`, `Miller` for `Mill`.
_PART_FROM = 3


def _unlike(pool: Sequence[str], original: str) -> tuple[str, ...]:
    """The words of ``pool`` a fake of ``original`` may show: none that is one of its words, holds one or is a part of
    one (``_PART_FROM`` letters or more), nor holds the whole original, whatever the case.

    So ``emily`` never gets ``emily`` and two digits, ``Annabella Rossi`` neither ``Anna`` nor ``Rossi``,
    ``a@gmail.com`` no name holding an ``a``, and ``acme.com`` no ``acme`` as the word of its domain. When every word of
    ``pool`` is out, the empty tuple: the caller's ``rng.choice`` raises ``IndexError``, and the vault makes a fake of
    the original's shape instead (``Vault._generate``).

    A list of the module's own (a tuple) keeps its last answer: the vault draws one value's fake many times over, from
    the same lists, as their wider forms come in turn (``Vault._draws``). One value per list, the last one asked."""
    if isinstance(pool, tuple):
        last = _UNLIKE_LAST.get(id(pool))
        if last is not None and last[0] == original:
            return last[1]
        found = _unlike_now(pool, original)
        _UNLIKE_LAST[id(pool)] = (original, found)
        return found
    return _unlike_now(pool, original)


_UNLIKE_LAST: dict[int, tuple[str, tuple[str, ...]]] = {}  # each list's last answer (``_unlike``), by the list


def _unlike_now(pool: Sequence[str], original: str) -> tuple[str, ...]:
    whole = original.casefold()
    words = {word.casefold() for word in _WORD.findall(original)}

    def shows(candidate: str) -> bool:
        drawn = candidate.casefold()
        if whole in drawn:
            return True
        return any(
            drawn == word or (len(word) >= _PART_FROM and word in drawn) or (len(drawn) >= _PART_FROM and drawn in word)
            for word in words
        )

    return tuple(candidate for candidate in pool if not shows(candidate))


def language(original: str, address: bool = False) -> Names:
    """The lists a fake of ``original`` comes from: Italian or English.

    An address says it with its street type ("via", "Lane"). Otherwise the last word decides, a family name
    more than a given one: a word of one list only is of that language; any other ends like an Italian word
    (a, i, o, u) or not. State and province codes ("RM", "IL") say nothing.
    """
    words = _WORD.findall(original)
    if address:
        for word in words:
            if word.lower() in ITALIAN.street_types:
                return ITALIAN
            if word.lower() in ENGLISH.street_types:
                return ENGLISH
    named = [w for w in words if len(w) > 2 or not w.isupper()]
    if not named:
        return ENGLISH
    last = named[-1].lower()
    italian, english = last in _ITALIAN_KNOWN, last in _ENGLISH_KNOWN
    if italian != english:
        return ITALIAN if italian else ENGLISH
    return ITALIAN if last.endswith(("a", "i", "o", "u")) else ENGLISH


def _lists(names: Names, wide: int) -> tuple[Sequence[str], Sequence[str]]:
    """The first and the last names a fake is drawn from: its language's (``wide`` 0, as Latet always drew them), or
    both languages' (a wider form: the full lists)."""
    return (names.first, names.last) if wide == 0 else (FIRST_NAMES, LAST_NAMES)


def _digits(rng: Drbg, wide: int) -> str:
    """The digits a name drawn from a list takes in its wider forms, as people write them (`luca84`): none in the
    first two, then two more in each."""
    return "".join(rng.choice(_DIGITS) for _ in range(2 * max(0, wide - 1)))


def gen_person(original: str, rng: Drbg, wide: int = 0) -> str:
    """A name of the original's language for each of its words. Wider forms (``wide``): drawn from both languages'
    lists, then with a given name more for each form past the first (`Marco Luca Ferri`; a name alone, a family name
    more: `Marco Ferri`)."""
    tokens = original.split()
    if not tokens:
        return shape(original, rng)
    first, last = _lists(language(original), wide)
    out = []
    for i, token in enumerate(tokens):
        pool = first if i == 0 and len(tokens) > 1 else last if len(tokens) > 1 else first
        out.append(_styled(rng.choice(_unlike(pool, original)), token))
        if i == 0 and wide > 1:
            more = first if len(tokens) > 1 else last
            out += [_styled(rng.choice(_unlike(more, original)), token) for _ in range(wide - 1)]
    return " ".join(out)


_STREET_KEEP = STREET_TYPES | STREET_PARTICLES | ADDRESS_WORDS


def _kept(part: str) -> bool:
    return not part or part.isspace() or part.lower().strip(".") in _STREET_KEEP


def _region(code: str, names: Names) -> tuple[str, ...] | None:
    """The codes a state or province code is drawn from (the address's country first), or None for no code."""
    for regions in (names.regions, ITALIAN.regions if names is ENGLISH else ENGLISH.regions):
        if code in regions:
            return tuple(c for c in regions if c != code)
    return None


def _street_name(rng: Drbg, names: Names, original: str, like: str, wide: int) -> str:
    """The name of a street: a family name of its language, or (``wide``) of both, then with given names before it
    (`via Marco Ferri`), none that shows ``original``."""
    first, last = _lists(names, wide)
    given = [rng.choice(_unlike(first, original)) for _ in range(max(0, wide - 1))]
    return _styled(" ".join([*given, rng.choice(_unlike(last, original))]), like)


def _street_word(part: str, rng: Drbg, names: Names, original: str, wide: int = 0) -> str:
    """One word of an address: kept, another code, a family name for a name (none that shows ``original``, the whole
    address), or the same shape."""
    if _kept(part):
        return part
    if (regions := _region(part, names)) is not None:
        return rng.choice(regions)
    if part.isalpha() and len(part) > 1:
        return _street_name(rng, names, original, part, wide)
    if part.isalpha():
        return rng.choice(_UPPER if part.isupper() else _LOWER)  # "scala C": a letter, not a hex digit
    return shape(part, rng)


def _place_name(part: str, names: Names) -> bool:
    return part.isalpha() and len(part) > 1 and not _kept(part) and _region(part, names) is None


def _town(rng: Drbg, names: Names, town: str, original: str, wide: int) -> str:
    """Another town than ``town`` for a place of ``original``: one of its language's list; in the wider forms
    (``wide``) one made from a family name (`Borgo Ferri`, `Walkerton`), then with the word that tells a part of a town
    (`Borgo Ferri Alta`, `North Walkerton`). None that shows ``original``."""
    if wide == 0:
        return rng.choice(_unlike([c for c in names.cities if c.lower() != town.lower()], original))
    family = rng.choice(_unlike(names.last, original))
    italian = names is ITALIAN
    made = f"{rng.choice(ITALIAN_TOWN_WORDS)} {family}" if italian else f"{family}{rng.choice(ENGLISH_TOWN_ENDINGS)}"
    if wide > 1:
        made = f"{made} {rng.choice(ITALIAN_TOWN_PARTS)}" if italian else f"{rng.choice(ENGLISH_TOWN_PARTS)} {made}"
    return made


def _place(piece: str, rng: Drbg, names: Names, original: str, wide: int = 0) -> str:
    """A town and what goes with it ("Bristol BS1 4DJ", "Roma RM"): the town's words become one other town, none
    that shows ``original`` (the whole address)."""
    parts = re.split(r"(\s+)", piece)
    out, i = [], 0
    while i < len(parts):
        if not _place_name(parts[i], names):
            out.append(_street_word(parts[i], rng, names, original, wide))
            i += 1
            continue
        j = i
        while j + 2 < len(parts) and _place_name(parts[j + 2], names):
            j += 2
        town = "".join(parts[i : j + 1])
        out.append(_styled(_town(rng, names, town, original, wide), parts[i]))
        i = j + 1
    return "".join(out)


def gen_street(original: str, rng: Drbg, wide: int = 0) -> str:
    """ "via Garibaldi 12" → "via Ferrari 48", "42 Mill Lane, Bristol BS1 4DJ" → "27 Young Lane, Leeds RA0 2NS".

    Names and numbers change, in the address's language; the street type, particles and details ("scala C")
    stay. After a comma comes the place: another town for the town, another state or province code for a code,
    a postcode of the same shape. A street with nothing that changes ("Corso", "Piazza della") gets a street of its
    language that holds none of it ("via Ferrari 48"): never itself, nor itself with a name added. Wider forms
    (``wide``): family names of both languages, then given names before them (`via Marco Ferri 48`), and towns made
    from family names (``_town``).
    """
    names = language(original, address=True)
    out = []
    for i, piece in enumerate(re.split(r"(,)", original)):
        if piece == ",":
            out.append(piece)
        elif i == 0 or any(w.lower() in STREET_TYPES for w in _WORD.findall(piece)):
            out.append("".join(_street_word(part, rng, names, original, wide) for part in re.split(r"(\s+)", piece)))
        else:
            out.append(_place(piece, rng, names, original, wide))
    fake = "".join(out)
    if fake == original:
        kind = rng.choice(_unlike(sorted(names.street_types), original))
        kind = kind.capitalize() if original[:1].isupper() or names is ENGLISH else kind
        name, number = _street_name(rng, names, original, "", wide), 1 + rng.below(99)
        fake = _styled(f"{kind} {name} {number}" if names is ITALIAN else f"{number} {name} {kind}", original)
    return fake


def gen_city(original: str, rng: Drbg, wide: int = 0) -> str:
    return _place(original, rng, language(original), original, wide)


def gen_username(original: str, rng: Drbg, wide: int = 0) -> str:
    """A first name of the original's language and two digits; wider forms (``wide``): of both languages, then with
    two digits more in each."""
    name = rng.choice(_unlike(_lists(language(original), wide)[0], original))
    return name.lower() + "".join(rng.choice(_DIGITS) for _ in range(2)) + _digits(rng, wide)


def gen_company(original: str, rng: Drbg, wide: int = 0) -> str:
    """A company's word for each of the original's words; wider forms (``wide``): one company word more in each, after
    the first (`Contoso Northwind`)."""
    words = original.split()
    companies = _unlike(COMPANIES, original)
    like = words[0] if words else original
    out = [_styled(rng.choice(companies), like)]
    out += [_styled(rng.choice(companies), like) for _ in range(wide)]
    if len(words) > 1:
        out += [shape(w, rng) if not w.isalpha() else _styled(rng.choice(companies), w) for w in words[1:]]
    return " ".join(out)


def gen_domain(domain: str, rng: Drbg) -> str:
    word = rng.choice(_unlike(COMPANIES, domain)).lower()
    return f"{word}-{''.join(rng.choice(_HEX_LOWER) for _ in range(4))}.example"


# An e-mail address written out so that no pattern takes it, as the local reviewer reports one: `@` as `at`, `(at)` or
# `[at]`, and `.` as `dot`, `(dot)` or `[dot]`.
_WRITTEN_AT = re.compile(r"\s*[(\[{]\s*at\s*[)\]}]\s*|\s+at\s+", re.IGNORECASE)
_WRITTEN_DOT = re.compile(r"\s*[(\[{]\s*dot\s*[)\]}]\s*|\s+dot\s+", re.IGNORECASE)
_ADDRESS = re.compile(r"[\w%+-]+(?:\.[\w%+-]+)*@[\w-]+(?:\.[\w-]+)+")


@dataclass(frozen=True, slots=True)
class Written:
    """An e-mail address written out (`daniel dot okafor at example dot org`): the address it stands for, and how it
    writes `@` and `.`."""

    address: str
    at: str
    dot: str

    def write(self, address: str) -> str:
        """``address`` written out the same way."""
        local, _, domain = address.rpartition("@")
        return local.replace(".", self.dot) + self.at + domain.replace(".", self.dot)


def written_email(value: str) -> Written | None:
    """The e-mail address ``value`` writes out, or None when it writes none: no `@`, one `at`, and the dots in words
    or as they are."""
    ats = list(_WRITTEN_AT.finditer(value))
    if "@" in value or len(ats) != 1:
        return None
    dots = _WRITTEN_DOT.findall(value)
    address = _WRITTEN_DOT.sub(".", _WRITTEN_AT.sub("@", value.strip()))
    if _ADDRESS.fullmatch(address) is None:
        return None
    return Written(address, ats[0].group(0), dots[0] if dots else ".")


def email_domain(value: str) -> str:
    """The domain of the e-mail address ``value`` is, or writes out: what follows its last `@`, or "" when it has
    none."""
    written = written_email(value)
    _, at, domain = (written.address if written is not None else value).rpartition("@")
    return domain if at else ""


def gen_email(original: str, rng: Drbg, domain_surrogate: Callable[[str], str] | None = None, wide: int = 0) -> str:
    """A name of the local part's language for each of its words (`first.last`, a first name alone), or its shape; the
    domain's fake, or a mailbox provider's domain as it is. Wider forms (``wide``): names of both languages, then
    digits after them as people write them (`luca84@`, `luca.rossi84@`), two more in each."""
    if "@" not in original:  # written out, as the local reviewer reports it; or no address at all
        written = written_email(original)
        if written is None:
            return shape(original, rng)
        return written.write(gen_email(written.address, rng, domain_surrogate, wide))
    local, _, domain = original.rpartition("@")
    parts = re.split(r"([._\-+])", local)
    alpha = [p for p in parts if p and p.isalpha()]
    given, family = _lists(language(local), wide)
    if len(alpha) == 2 and len(parts) == 3:
        first, last = _unlike(given, local), _unlike(family, local)
        fake_local = rng.choice(first).lower() + parts[1] + rng.choice(last).lower()
    elif local.isalpha():
        fake_local = rng.choice(_unlike(given, local)).lower()
    else:
        fake_local = shape(local, rng)
    fake_local += _digits(rng, wide)
    if domain.lower() in PUBLIC_MAIL_DOMAINS:
        fake_domain = domain
    elif domain_surrogate is not None:
        fake_domain = domain_surrogate(domain)
    else:
        fake_domain = gen_domain(domain, rng)
    return f"{fake_local}@{fake_domain}"


def gen_phone(original: str, rng: Drbg) -> str:
    """Keep ``+CC`` (or the leading digit) and the formatting; randomize the subscriber digits."""
    keep = 0
    digits_seen = 0
    plus = original.lstrip().startswith(("+", "00"))
    keep_digits = 3 if plus else 1
    for i, c in enumerate(original):
        if c.isdigit():
            digits_seen += 1
            if digits_seen == keep_digits:
                keep = i + 1
                break
    head, tail = original[:keep], original[keep:]
    return head + "".join(rng.choice(_DIGITS) if c.isdigit() else c for c in tail)


def gen_iban(original: str, rng: Drbg) -> str:
    compact = original.replace(" ", "").replace("-", "")
    country = compact[:2].upper()
    bban = "".join(
        rng.choice(_UPPER) if c.isalpha() else rng.choice(_DIGITS) if c.isdigit() else c for c in compact[4:]
    )
    numeric = "".join(str(int(c, 36)) for c in bban + country + "00")
    check = 98 - int(numeric) % 97
    new_compact = f"{country}{check:02d}{bban}"
    assert len(new_compact) == len(compact)
    assert IBAN_LENGTHS.get(country) in (None, len(compact))
    out, j = [], 0
    for c in original:
        if c in " -":
            out.append(c)
        else:
            out.append(new_compact[j])
            j += 1
    return "".join(out)


def _luhn_digit(payload: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(payload)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def gen_card(original: str, rng: Drbg) -> str:
    digits = [c for c in original if c.isdigit()]
    body = digits[0] + "".join(rng.choice(_DIGITS) for _ in range(len(digits) - 2))
    new_digits = body + _luhn_digit(body)
    out, j = [], 0
    for c in original:
        if c.isdigit():
            out.append(new_digits[j])
            j += 1
        else:
            out.append(c)
    return "".join(out)


# The blocks an IPv4 address's fake is drawn from, each as (its first /24, how many /24 it holds), a fake taking a host
# of a /24 (1 to 254): for a public address, blocks never routed on the internet, in tiers, each taken whole before the
# next: the documentation's three (RFC 5737: 762 fakes, the only ones before) and 198.19.0.0/16 (65,024 more), then
# 198.18.0.0/16 (65,024), the half of benchmarking's block (RFC 2544) that proxies in fake-ip mode (Clash, Mihomo,
# Surge) give the hosts they resolve by default, which Latet leaves unmasked as private; for a private address, its own
# private block (RFC 1918). An IPv6 address's fake is of the documentation's block (RFC 3849): 2^96 of them.
_DOCUMENTATION_V4 = ((0xC00002, 1), (0xC63364, 1), (0xCB0071, 1))
_FIRST_TIER_V4 = (*_DOCUMENTATION_V4, (0xC61300, 256))
_LAST_TIER_V4 = ((0xC61200, 256),)
_PRIVATE_V4 = {"10": ((0x0A0000, 65_536),), "192": ((0xC0A800, 256),), "172": ((0xAC1000, 4_096),)}
_HOSTS = 254


def _hosts(blocks: tuple[tuple[int, int], ...]) -> int:
    return sum(count for _, count in blocks) * _HOSTS


def _host(blocks: tuple[tuple[int, int], ...], index: int) -> str:
    """The ``index``-th host of ``blocks`` (``_FIRST_TIER_V4``'s kind), in order."""
    net, host = divmod(index, _HOSTS)
    for first, count in blocks:
        if net < count:
            return str(ipaddress.IPv4Address(((first + net) << 8) | (host + 1)))
        net -= count
    raise IndexError(index)


def _v4_tiers(addr: ipaddress.IPv4Address, original: str) -> tuple[tuple[tuple[int, int], ...], ...]:
    """The blocks the fakes of an IPv4 address are drawn from in its widest form (``every_ip``), tier by tier."""
    if not addr.is_private:
        return (_FIRST_TIER_V4, _LAST_TIER_V4)
    a = original.split(".")[0]
    return (_PRIVATE_V4.get(a, _PRIVATE_V4["172"]),)


def gen_ip(original: str, rng: Drbg, wide: int = 0) -> str:
    """A fake of the same family: a private IPv4 address of the same private block, a public one of a block never
    routed on the internet (the documentation's; wider, ``wide``: 198.19.0.0/16 too), an IPv6 one of the
    documentation's."""
    addr = ipaddress.ip_address(original)
    if isinstance(addr, ipaddress.IPv6Address):
        groups = [f"{rng.below(65536):x}" for _ in range(6)]
        return "2001:db8:" + ":".join(groups)
    if addr.is_private:
        a = original.split(".")[0]
        if a == "10":
            return f"10.{rng.below(256)}.{rng.below(256)}.{rng.below(254) + 1}"
        if a == "192":
            return f"192.168.{rng.below(256)}.{rng.below(254) + 1}"
        return f"172.{16 + rng.below(16)}.{rng.below(256)}.{rng.below(254) + 1}"
    if wide:
        return _host(_FIRST_TIER_V4, rng.below(_hosts(_FIRST_TIER_V4)))
    net = rng.choice(("192.0.2", "198.51.100", "203.0.113"))
    return f"{net}.{rng.below(254) + 1}"


def every_ip(original: str, rng: Drbg) -> Iterator[str]:
    """Every fake an IPv4 address may get (``gen_ip``'s widest form), each once, tier by tier, each from a place drawn
    by ``rng``: the vault takes the first one free once those drawn at random were taken, so that it runs out only when
    the blocks are full (130,810 public addresses), and takes one of 198.18.0.0/16 only once the documentation's blocks
    and 198.19.0.0/16 are (65,786). None for IPv6, whose fakes do not run out, nor for what is no address."""
    try:
        addr = ipaddress.ip_address(original)
    except ValueError:
        return
    if isinstance(addr, ipaddress.IPv6Address):
        return
    for blocks in _v4_tiers(addr, original):
        size = _hosts(blocks)
        start = rng.below(size)
        for k in range(size):
            yield _host(blocks, (start + k) % size)


# The word a fake host name begins with: the first that shows nothing of the original (a computer named `host` got
# `host-` and six digits, which the final check then found in every request that named it, and stopped).
_HOST_WORDS = ("host", "node", "box")


def gen_hostname(original: str, rng: Drbg) -> str:
    whole = original.casefold()
    words = [word for word in _unlike(_HOST_WORDS, original) if whole not in f"{word}-"]
    return f"{words[0]}-" + "".join(rng.choice(_HEX_LOWER) for _ in range(6))  # none left: IndexError, as _unlike's


_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:([T ])(\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$")
_PLAIN_NUMBER = re.compile(r"^-?[1-9]\d*(?:[.,]\d+)?$")
# The schemes a link keeps: the common ones, which say nothing of yours. An app's own (acmebank://) can name a company.
_SCHEME = re.compile(
    r"^(?:https?|ftps?|sftp|wss?|s3|gs|file|ssh|git(?:\+ssh|\+https)?|svn|smb|nfs|ldaps?|rediss?|postgres(?:ql)?|mysql|"
    r"mongodb(?:\+srv)?|amqps?)://",
    re.I,
)


def gen_data(original: str, rng: Drbg) -> str:
    """A value of a structure (strict data mode, field rules): its shape, and still a date or number.

    An ISO date stays a valid date (moved within two years) with a valid time; a number keeps its
    digits without gaining a leading zero.
    """
    m = _ISO_DATE.match(original)
    if m:
        try:
            day = datetime.date(int(m[1]), int(m[2]), int(m[3])) + datetime.timedelta(days=rng.below(1461) - 730)
        except (ValueError, OverflowError):
            day = None
        if day is not None:
            out = day.isoformat()
            if m[4]:
                out += f"{m[4]}{rng.below(24):02d}:{rng.below(60):02d}"
                if m[7]:
                    out += f":{rng.below(60):02d}" + shape(m[8] or "", rng)
            return out + (m[9] or "")
    scheme = _SCHEME.match(original)
    # A link stays a link to the model (https://), the rest masked. A scheme with nothing after it (what is left of a
    # link whose user or host is masked apart) is shaped whole: kept, its fake would be itself.
    if scheme and any(c.isalnum() for c in original[scheme.end() :]):
        return scheme.group() + shape(original[scheme.end() :], rng)
    out = shape(original, rng)
    if _PLAIN_NUMBER.match(original):
        lead = 1 if out.startswith("-") else 0
        if out[lead : lead + 1] == "0":
            out = out[:lead] + rng.choice("123456789") + out[lead + 1 :]
    return out


GENERATORS: dict[str, Callable[[str, Drbg], str]] = {
    "LATET_PERSONAL_TOKEN": gen_latet_token,
    "LATET_API_KEY": gen_latet_token,
    "LATET_ADMIN_TOKEN": gen_latet_token,
    "LATET_COMPUTER_KEY": gen_latet_token,
    "LATET_SCIM_TOKEN": gen_latet_token,
    "LATET_BRIDGE_TOKEN": gen_latet_token,
    "LATET_ONE_TIME_CODE": gen_latet_token,
    "LATET_ROUTE_KEY": gen_latet_route_key,
    "JWT": gen_jwt,
    "PERSON": gen_person,
    "USERNAME": gen_username,
    "HOSTNAME": gen_hostname,
    "DOMAIN": gen_domain,
    "PHONE": gen_phone,
    "IBAN": gen_iban,
    "CREDIT_CARD": gen_card,
    "IP_ADDRESS": gen_ip,
    "CLIENT": gen_company,
    "COMPANY": gen_company,
    "ORG": gen_company,
    "STREET_ADDRESS": gen_street,
    "CITY": gen_city,
}
# National IDs and VAT numbers: each country module has its own (valid check digits, same layout).
GENERATORS.update(ids.generators())

# The generators of words drawn from lists (names, companies, towns), each with how many wider forms it has past its
# first (``wide`` 1 to that many): the vault tries each in turn once the narrower ones are taken, then the value's own
# shape, as a value no generator can fake gets (``Vault._draws``). An e-mail address's is ``gen_email``.
WIDER: dict[str, int] = {
    "EMAIL": 4, "USERNAME": 4, "PERSON": 4, "CLIENT": 4, "COMPANY": 4, "ORG": 4, "STREET_ADDRESS": 3, "CITY": 2,
}  # fmt: skip
WIDE: dict[str, Callable[[str, Drbg, int], str]] = {
    "USERNAME": gen_username, "PERSON": gen_person, "CLIENT": gen_company, "COMPANY": gen_company, "ORG": gen_company,
    "STREET_ADDRESS": gen_street, "CITY": gen_city, "IP_ADDRESS": gen_ip,
}  # fmt: skip
