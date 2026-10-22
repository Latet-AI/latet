"""Format-preserving surrogate generators.

Every generator is a pure function of (original, rng): the same seed always gives the same fake,
which keeps masked conversations byte-stable. Surrogates keep the shape a model expects (prefix,
length, character classes, valid checksums) but never contain quotes, backslashes or ``$``, so they
survive JSON and shell quoting unchanged.

A fake never shows its original: a word drawn from a list (a name, a company, a town, a host's word)
is never one of the original's words, nor holds one, nor is a part of one, nor holds the whole
original (``_unlike``), so ``emily`` never gets ``emily`` and two digits; and a fake the vault is given
that holds its original all the same, whatever made it, is drawn again (``Vault.intern``).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import re
import string
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from latet.engine.detectors import ids
from latet.engine.detectors.pii import IBAN_LENGTHS
from latet.engine.surrogates.data.names import (
    ADDRESS_WORDS,
    COMPANIES,
    ENGLISH,
    ITALIAN,
    PUBLIC_MAIL_DOMAINS,
    STREET_PARTICLES,
    STREET_TYPES,
    Names,
)


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


def gen_latet_token(original: str, rng: Drbg) -> str:
    """Only the public credential prefix stays (``latet_pat_``, ``latet_ck_``: up to the underscore after its kind);
    every URL-safe character of its secret tail is drawn."""
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
    the original's shape instead (``Vault._generate``)."""
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


def gen_person(original: str, rng: Drbg) -> str:
    tokens = original.split()
    if not tokens:
        return shape(original, rng)
    names = language(original)
    out = []
    for i, token in enumerate(tokens):
        pool = names.first if i == 0 and len(tokens) > 1 else names.last if len(tokens) > 1 else names.first
        out.append(_styled(rng.choice(_unlike(pool, original)), token))
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


def _street_word(part: str, rng: Drbg, names: Names, original: str) -> str:
    """One word of an address: kept, another code, a family name for a name (none that shows ``original``, the whole
    address), or the same shape."""
    if _kept(part):
        return part
    if (regions := _region(part, names)) is not None:
        return rng.choice(regions)
    if part.isalpha() and len(part) > 1:
        return _styled(rng.choice(_unlike(names.last, original)), part)
    if part.isalpha():
        return rng.choice(_UPPER if part.isupper() else _LOWER)  # "scala C": a letter, not a hex digit
    return shape(part, rng)


def _place_name(part: str, names: Names) -> bool:
    return part.isalpha() and len(part) > 1 and not _kept(part) and _region(part, names) is None


def _place(piece: str, rng: Drbg, names: Names, original: str) -> str:
    """A town and what goes with it ("Bristol BS1 4DJ", "Roma RM"): the town's words become one other town, none
    that shows ``original`` (the whole address)."""
    parts = re.split(r"(\s+)", piece)
    out, i = [], 0
    while i < len(parts):
        if not _place_name(parts[i], names):
            out.append(_street_word(parts[i], rng, names, original))
            i += 1
            continue
        j = i
        while j + 2 < len(parts) and _place_name(parts[j + 2], names):
            j += 2
        town = "".join(parts[i : j + 1])
        towns = _unlike([c for c in names.cities if c.lower() != town.lower()], original)
        out.append(_styled(rng.choice(towns), parts[i]))
        i = j + 1
    return "".join(out)


def gen_street(original: str, rng: Drbg) -> str:
    """ "via Garibaldi 12" → "via Ferrari 48", "42 Mill Lane, Bristol BS1 4DJ" → "27 Young Lane, Leeds RA0 2NS".

    Names and numbers change, in the address's language; the street type, particles and details ("scala C")
    stay. After a comma comes the place: another town for the town, another state or province code for a code,
    a postcode of the same shape. A street with nothing that changes ("Corso", "Piazza della") gets a street of its
    language that holds none of it ("via Ferrari 48"): never itself, nor itself with a name added.
    """
    names = language(original, address=True)
    out = []
    for i, piece in enumerate(re.split(r"(,)", original)):
        if piece == ",":
            out.append(piece)
        elif i == 0 or any(w.lower() in STREET_TYPES for w in _WORD.findall(piece)):
            out.append("".join(_street_word(part, rng, names, original) for part in re.split(r"(\s+)", piece)))
        else:
            out.append(_place(piece, rng, names, original))
    fake = "".join(out)
    if fake == original:
        kind = rng.choice(_unlike(sorted(names.street_types), original))
        kind = kind.capitalize() if original[:1].isupper() or names is ENGLISH else kind
        name, number = rng.choice(_unlike(names.last, original)), 1 + rng.below(99)
        fake = _styled(f"{kind} {name} {number}" if names is ITALIAN else f"{number} {name} {kind}", original)
    return fake


def gen_city(original: str, rng: Drbg) -> str:
    return _place(original, rng, language(original), original)


def gen_username(original: str, rng: Drbg) -> str:
    name = rng.choice(_unlike(language(original).first, original))
    return name.lower() + "".join(rng.choice(_DIGITS) for _ in range(2))


def gen_company(original: str, rng: Drbg) -> str:
    words = original.split()
    companies = _unlike(COMPANIES, original)
    out = [_styled(rng.choice(companies), words[0] if words else original)]
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


def gen_email(original: str, rng: Drbg, domain_surrogate: Callable[[str], str] | None = None) -> str:
    if "@" not in original:  # written out, as the local reviewer reports it; or no address at all
        written = written_email(original)
        return written.write(gen_email(written.address, rng, domain_surrogate)) if written else shape(original, rng)
    local, _, domain = original.rpartition("@")
    parts = re.split(r"([._\-+])", local)
    alpha = [p for p in parts if p and p.isalpha()]
    names = language(local)
    if len(alpha) == 2 and len(parts) == 3:
        first, last = _unlike(names.first, local), _unlike(names.last, local)
        fake_local = rng.choice(first).lower() + parts[1] + rng.choice(last).lower()
    elif local.isalpha():
        fake_local = rng.choice(_unlike(names.first, local)).lower()
    else:
        fake_local = shape(local, rng)
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


def gen_ip(original: str, rng: Drbg) -> str:
    import ipaddress

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
    net = rng.choice(("192.0.2", "198.51.100", "203.0.113"))
    return f"{net}.{rng.below(254) + 1}"


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
