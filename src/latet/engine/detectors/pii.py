"""PII detectors tuned for code: each pattern is backed by a validator (checksum, registry, TLD).

Patterns and validators for email, IBAN, credit cards, Italian fiscal code and VAT number follow
Microsoft Presidio's recognizers (MIT); phone numbers use ``phonenumbers`` with a prefilter and
stricter rules outside natural language, where digit runs are usually not phone numbers. National
ID and VAT numbers live in ``detectors.ids``, one module per country.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Iterable, Iterator
from functools import cache
from importlib import resources

import phonenumbers
import re2

from latet.engine.detectors import checksums, ids
from latet.engine.detectors.base import DetectContext, group
from latet.engine.detectors.ids.regions.it import (  # noqa: F401 - their old home
    fiscal_code_check_char,
    fiscal_code_valid,
    vat_code_valid,
)
from latet.engine.types import EntityClass, Span

# On whatever the countries (id_regions): VAT numbers carry their country prefix.
BASE_PII = ("EMAIL", "PHONE", "IBAN", "CREDIT_CARD", "IP_ADDRESS", "CRYPTO_WALLET", "VAT_NUMBER")
ALL_PII = (
    "EMAIL",
    "PHONE",
    "IBAN",
    "CREDIT_CARD",
    "IT_FISCAL_CODE",
    "IT_VAT_CODE",
    "IP_ADDRESS",
    "CRYPTO_WALLET",
    "VAT_NUMBER",
    *(etype for etype in ids.ID_TYPES if not etype.startswith("IT_")),
)

# How each type is told apart from look-alikes (shown in the detector reference).
VALIDATION = {
    "EMAIL": "Top-level domain checked against the IANA list (shipped offline).",
    "PHONE": "Parsed and validated with libphonenumber; numbers without a prefix only for `phone_regions`. "
    "Decimals, dates, versions and digit lists are skipped.",
    "IBAN": "Country length and ISO 13616 mod-97 checksum.",
    "CREDIT_CARD": "Issuer prefix (IIN) and Luhn checksum; repeated digits are skipped.",
    "IT_FISCAL_CODE": "Italian codice fiscale structure and check character.",
    "IT_VAT_CODE": "Italian partita IVA checksum, with an `IT` prefix or a VAT word on the same line.",
    "IP_ADDRESS": "Public IPv4 and IPv6 addresses (private ones too with `include_private_ips`); "
    "version-like numbers are skipped.",
    "CRYPTO_WALLET": "Bitcoin addresses with a valid Base58Check or bech32/bech32m checksum.",
    "VAT_NUMBER": ids.vat.VALIDATION,
    **{etype: spec.checked for etype, spec in ids.SPECS.items() if not etype.startswith("IT_")},
}


def effective_pii(pii: Iterable[str] | None, id_regions: Iterable[str], every_region: bool = False) -> list[str]:
    """The personal-data types in use: an explicit list as it is; otherwise the base types and the
    ID numbers of ``id_regions`` (of every country with ``every_region``, the max profile)."""
    if pii is not None:
        return list(pii)
    regions = list(ids.REGIONS) if every_region else list(id_regions)
    return [*BASE_PII, *ids.types_for(regions)]


@cache
def _tlds() -> frozenset[str]:
    text = resources.files("latet.engine.detectors").joinpath("data/tlds.txt").read_text("utf-8")
    return frozenset(line.strip().lower() for line in text.splitlines() if line and not line.startswith("#"))


# --- email ---------------------------------------------------------------------------------------

_EMAIL = re2.compile(
    r"\b[A-Za-z0-9][A-Za-z0-9._%+\-]{0,63}@((?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+([A-Za-z]{2,63}|xn--[A-Za-z0-9\-]{1,59}))\b"
)
_RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
_RESERVED_TLDS = ("example", "test", "invalid", "localhost")
_SYSTEM_LOCALS = ("git", "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon")


def _emails(text: str) -> Iterator[Span]:
    if "@" not in text:
        return
    tlds = _tlds()
    for m in _EMAIL.finditer(text):
        domain = group(m, 1).lower()
        tld = group(m, 2).lower()
        local = text[m.start() : m.start(1) - 1].lower()
        if tld in _RESERVED_TLDS or domain in _RESERVED_DOMAINS or tld not in tlds:
            continue
        if local in _SYSTEM_LOCALS:
            continue
        yield Span(m.start(), m.end(), "EMAIL", EntityClass.PII, "pii:email")


# --- phone ---------------------------------------------------------------------------------------

_PHONE_CANDIDATE = re2.compile(r"(?:\+|\b00)?\(?\d[\d ()./\-]{6,20}\d\b")
# What ``phone_end`` reads of a line: a candidate takes 25 characters at most, and its last ``\b`` the one after it.
_PHONE_WINDOW = 48
# Dates, times and dotted versions look like formatted numbers.
_NOT_PHONE = re2.compile(r"^(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d+(?:\.\d+){2,})")
_DECIMAL = re2.compile(r"^\d+\.\d+$")
_PHONE_SEPARATORS = re.compile(r"[ ()./\-]+")
_CODE_PHONE = re2.compile(r"^(?:\+|00)|[ ()]")
DEFAULT_PHONE_REGIONS = ("IT", "US", "GB")


def _phone_shaped(candidate: str) -> bool:
    """Whether a candidate has the digits and groups of a phone number, not those of a date, a version, a decimal or a
    list of single digits."""
    digits = sum(c.isdigit() for c in candidate)
    if digits < 8 or digits > 15:
        return False
    if _NOT_PHONE.match(candidate) or _DECIMAL.match(candidate):
        return False
    groups = [g for g in _PHONE_SEPARATORS.split(candidate) if g]
    return sum(1 for g in groups if len(g) == 1) <= 2  # "0 1 2 3 4 5": lists of digits


def _phone_valid(candidate: str, regions: tuple[str, ...]) -> bool:
    for region in (None,) if candidate.startswith("+") else regions:
        try:
            number = phonenumbers.parse(candidate, region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(number):
            return True
    return False


# What a key that names a phone is made of: its words, apart by a sign or a capital (``phone``, ``mobile_phone``,
# ``phoneNumber``, ``TEL``), never a part of a longer word (``telemetry``, ``cellar``). A key names a phone when its
# last word is one of these, or "number" and the like after one (``phone_number``, ``tel_no``): ``cell_id``,
# ``mobile_version``, ``fax_sent`` and ``phone_count`` name what is about a phone, which is not one.
_PHONE_KEY_WORDS = frozenset(
    {
        *("phone", "phones", "tel", "telephone", "mobile", "cell", "cellphone", "fax", "gsm"),
        *("msisdn", "whatsapp", "telefono", "telefon", "cellulare", "handy", "portable", "movil", "celular"),
    }
)
_NUMBER_WORDS = frozenset({"number", "numbers", "num", "no", "nr"})
_KEY_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
_KEY_MOST = 41  # characters of a key read before an ``=``


def _names_a_phone(text: str, equals: int) -> bool:
    """Whether the key that ends right before the ``=`` at ``equals`` names a phone."""
    start = equals
    while start > 0 and equals - start < _KEY_MOST and (text[start - 1].isalnum() or text[start - 1] in "_.-"):
        start -= 1
    words = [word.lower() for word in _KEY_WORD.findall(text[start:equals])]
    if words and words[-1] in _NUMBER_WORDS:
        words.pop()
        return bool(words) and words[-1] in _PHONE_KEY_WORDS
    return bool(words) and words[-1] in _PHONE_KEY_WORDS


def _glued_phone(text: str, start: int, candidate: str) -> bool:
    """Whether a number that follows one of ``._/#=`` right away may be a phone. A ``+`` makes it one wherever it
    stands. After an ``=`` it is also one, with ``00`` (``mobile=0039...``) or none (``phone=347 123 4567``), when the
    key names a phone: any other number there (``count=3471234567``, ``id=0012345678901``) is a value, as it was."""
    if candidate.startswith("+"):
        return True
    return text[start - 1] == "=" and _names_a_phone(text, start - 1)


def _phones(text: str, natural: bool, regions: Iterable[str]) -> Iterator[Span]:
    regions = tuple(regions)
    for m in _PHONE_CANDIDATE.finditer(text):
        candidate = text[m.start() : m.end()]
        if not _phone_shaped(candidate):
            continue
        if not natural and (not _CODE_PHONE.search(candidate) or "." in candidate or "/" in candidate):
            # In code a bare digit run, digits joined by "-", or anything with "." or "/" is an id, a
            # build number, a date or arithmetic far more often than a phone: require "+", "00", a
            # space or parentheses, and no dots or slashes.
            continue
        prev = text[m.start() - 1 : m.start()]
        if prev and (prev.isalnum() or (prev in "._/#=" and not _glued_phone(text, m.start(), candidate))):
            continue
        if _phone_valid(candidate, regions):
            yield Span(m.start(), m.end(), "PHONE", EntityClass.PII, "pii:phone")


def phone_end(
    text: str, start: int, regions: Iterable[str] = DEFAULT_PHONE_REGIONS, limit: int | None = None
) -> int | None:
    """Where a valid phone number that begins exactly at ``start`` ends, else None. For a value that begins there
    (an unquoted ``key=value``): nothing is asked of what is before it, nor of prose or code. ``limit``: where the
    value ends as it is read without it; the shape of a phone number that does not go past it is no use (None, and
    ``phonenumbers`` is not asked, which costs a parse for each region of every number of a numeric log)."""
    if text[start : start + 1] not in ("+", "(", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
        return None
    if limit is not None:
        # A value that a space, a parenthesis and a digit or another one follows may go on as a phone number: a letter
        # after the space (the next pair's key), a delimiter or the end of the line, never.
        after = text[limit : limit + 2]
        if len(after) < 2 or after[0] not in " ()" or after[1] not in "0123456789 (":
            return None
    # Only the few characters a candidate can take, never the line: RE2's wrapper encodes the whole text it is given at
    # each call, and a call for each pair of a long line would cost the square of its length. A value begins after an
    # ``=``, no character of a word, so the ``\b`` of ``\b00`` reads the same at the window's start.
    window = text[start : start + _PHONE_WINDOW]
    m = _PHONE_CANDIDATE.match(window)
    if m is None or (limit is not None and start + m.end() <= limit):
        return None
    candidate = window[: m.end()]
    return start + m.end() if _phone_shaped(candidate) and _phone_valid(candidate, tuple(regions)) else None


# --- IBAN ----------------------------------------------------------------------------------------

_IBAN = re2.compile(r"\b[A-Z]{2}\d{2}(?:[ \-]?[A-Z0-9]){11,30}\b")
IBAN_LENGTHS = {
    "AD": 24,
    "AE": 23,
    "AL": 28,
    "AT": 20,
    "AZ": 28,
    "BA": 20,
    "BE": 16,
    "BG": 22,
    "BH": 22,
    "BI": 27,
    "BR": 29,
    "BY": 28,
    "CH": 21,
    "CR": 22,
    "CY": 28,
    "CZ": 24,
    "DE": 22,
    "DJ": 27,
    "DK": 18,
    "DO": 28,
    "EE": 20,
    "EG": 29,
    "ES": 24,
    "FI": 18,
    "FK": 18,
    "FO": 18,
    "FR": 27,
    "GB": 22,
    "GE": 22,
    "GI": 23,
    "GL": 18,
    "GR": 27,
    "GT": 28,
    "HN": 28,
    "HR": 21,
    "HU": 28,
    "IE": 22,
    "IL": 23,
    "IQ": 23,
    "IS": 26,
    "IT": 27,
    "JO": 30,
    "KW": 30,
    "KZ": 20,
    "LB": 28,
    "LC": 32,
    "LI": 21,
    "LT": 20,
    "LU": 20,
    "LV": 21,
    "LY": 25,
    "MC": 27,
    "MD": 24,
    "ME": 22,
    "MK": 19,
    "MN": 20,
    "MR": 27,
    "MT": 31,
    "MU": 30,
    "NI": 28,
    "NL": 18,
    "NO": 15,
    "OM": 23,
    "PK": 24,
    "PL": 28,
    "PS": 29,
    "PT": 25,
    "QA": 29,
    "RO": 24,
    "RS": 22,
    "RU": 33,
    "SA": 24,
    "SC": 31,
    "SD": 18,
    "SE": 24,
    "SI": 19,
    "SK": 24,
    "SM": 27,
    "SO": 23,
    "ST": 25,
    "SV": 28,
    "TL": 23,
    "TN": 24,
    "TR": 26,
    "UA": 29,
    "VA": 22,
    "VG": 24,
    "XK": 20,
    "YE": 30,
}


def iban_valid(compact: str) -> bool:
    if not 15 <= len(compact) <= 34:
        return False
    expected = IBAN_LENGTHS.get(compact[:2])
    if expected is not None and len(compact) != expected:
        return False
    return checksums.iban(compact)


def _ibans(text: str) -> Iterator[Span]:
    for m in _IBAN.finditer(text):
        raw = text[m.start() : m.end()]
        compact = raw.replace(" ", "").replace("-", "")
        # The pattern may run past the IBAN into the next word: try the registered length first.
        expected = IBAN_LENGTHS.get(compact[:2])
        if expected and len(compact) > expected:
            compact = compact[:expected]
            raw_end = m.start()
            seen = 0
            while seen < expected:
                if text[raw_end] not in " -":
                    seen += 1
                raw_end += 1
            if iban_valid(compact):
                yield Span(m.start(), raw_end, "IBAN", EntityClass.PII, "pii:iban")
            continue
        if iban_valid(compact):
            yield Span(m.start(), m.end(), "IBAN", EntityClass.PII, "pii:iban")


# --- credit cards --------------------------------------------------------------------------------

_CARD = re2.compile(r"\b(?:\d[ \-]?){12,18}\d\b")
_CARD_IIN = re2.compile(
    r"^(?:4\d{12}(?:\d{3}){0,2}|5[1-5]\d{14}|2(?:2[2-9]\d|[3-6]\d\d|7[01]\d|720)\d{12}|3[47]\d{13}|"
    r"3(?:0[0-5]|[68]\d)\d{11}|6(?:011|5\d\d|4[4-9]\d)\d{12,15}|35(?:2[89]|[3-8]\d)\d{12,15}|"
    r"(?:5018|5020|5038|6304|6759|676[1-3])\d{8,15}|62\d{14,17})$"
)


luhn_valid = checksums.luhn  # the old name, kept for callers


def _cards(text: str) -> Iterator[Span]:
    for m in _CARD.finditer(text):
        digits = "".join(c for c in text[m.start() : m.end()] if c.isdigit())
        if len(set(digits)) < 2 or not _CARD_IIN.match(digits) or not luhn_valid(digits):
            continue
        yield Span(m.start(), m.end(), "CREDIT_CARD", EntityClass.PII, "pii:credit-card")


# --- IP addresses --------------------------------------------------------------------------------

_IPV4 = re2.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b")
_IPV6 = re2.compile(r"(?i)(?:^|[^0-9a-z:.])((?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4})")


def _ip_interesting(addr: ipaddress.IPv4Address | ipaddress.IPv6Address, include_private: bool) -> bool:
    if addr.is_loopback or addr.is_unspecified or addr.is_link_local or addr.is_multicast or addr.is_reserved:
        return False
    if isinstance(addr, ipaddress.IPv4Address):
        if any(addr in net for net in _DOC_V4):
            return False
    elif addr in _DOC_V6:
        return False
    if addr.is_private:
        return include_private
    return addr.is_global


_DOC_V4 = tuple(ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
_DOC_V6 = ipaddress.ip_network("2001:db8::/32")


def _ips(text: str, include_private: bool) -> Iterator[Span]:
    if "." in text:
        for m in _IPV4.finditer(text):
            before = text[max(0, m.start() - 10) : m.start()].lower()
            after = text[m.end() : m.end() + 2]
            if before.endswith(("v", "version ", "version=", "ver ")) or (after[:1] == "." and after[1:2].isdigit()):
                continue  # version numbers, not addresses
            raw = text[m.start() : m.end()]
            if all(len(octet) == 1 for octet in raw.split(".")):
                continue  # 1.2.3.4-like: versions in code (and well-known public resolvers)
            addr = ipaddress.ip_address(raw)
            if _ip_interesting(addr, include_private):
                yield Span(m.start(), m.end(), "IP_ADDRESS", EntityClass.PII, "pii:ipv4")
    if "::" in text or text.count(":") >= 3:
        for m in _IPV6.finditer(text):
            start, end = m.span(1)
            try:
                addr = ipaddress.IPv6Address(text[start:end])
            except ValueError:
                continue
            if _ip_interesting(addr, include_private):
                yield Span(start, end, "IP_ADDRESS", EntityClass.PII, "pii:ipv6")


# --- crypto wallets (Bitcoin) ----------------------------------------------------------------------

_BTC_BASE58 = re2.compile(r"\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b")
_BTC_BECH32 = re2.compile(r"(?i)\bbc1[ac-hj-np-z02-9]{11,71}\b")
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58check(s: str) -> bool:
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    raw = n.to_bytes(25, "big") if n.bit_length() <= 200 else b""
    if len(raw) != 25:
        return False
    return hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4] == raw[-4:]


def _bech32_valid(s: str) -> bool:
    charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    s = s.lower()
    hrp, data = s[:2], s[3:]
    values = [charset.index(c) for c in data]
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp] + values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk in (1, 0x2BC830A3)  # bech32 / bech32m


def _crypto(text: str) -> Iterator[Span]:
    for m in _BTC_BASE58.finditer(text):
        if _base58check(text[m.start() : m.end()]):
            yield Span(m.start(), m.end(), "CRYPTO_WALLET", EntityClass.PII, "pii:btc")
    if "bc1" in text.lower():
        for m in _BTC_BECH32.finditer(text):
            if _bech32_valid(text[m.start() : m.end()]):
                yield Span(m.start(), m.end(), "CRYPTO_WALLET", EntityClass.PII, "pii:btc-bech32")


class PiiDetector:
    """Structured personal data, each match validated (checksums, number plans, the IANA TLD list)."""

    id = "pii"
    mandatory = False

    def __init__(
        self,
        enabled: Iterable[str] = ALL_PII,
        phone_regions: Iterable[str] = DEFAULT_PHONE_REGIONS,
        include_private_ips: bool = False,
    ) -> None:
        self.enabled = frozenset(enabled)
        self.phone_regions = tuple(phone_regions)
        self.include_private_ips = include_private_ips
        self._ids = [ids.IdMatcher(spec) for etype, spec in ids.SPECS.items() if etype in self.enabled]
        # The Italian partita IVA keeps its own type, IT prefix or not, while that type is on.
        self._vat_skip = frozenset({"IT"}) if "IT_VAT_CODE" in self.enabled else frozenset()

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        on = self.enabled
        natural = ctx.kind.natural_language
        if "EMAIL" in on:
            yield from _emails(text)
        if "PHONE" in on:
            yield from _phones(text, natural, self.phone_regions)
        if "IBAN" in on:
            yield from _ibans(text)
        if "CREDIT_CARD" in on:
            yield from _cards(text)
        for matcher in self._ids:
            yield from matcher.find(text, natural)
        if "VAT_NUMBER" in on:
            yield from ids.vat.find(text, self._vat_skip)
        if "IP_ADDRESS" in on:
            yield from _ips(text, self.include_private_ips)
        if "CRYPTO_WALLET" in on or "CRYPTO" in on:  # CRYPTO: the name before 0.1
            yield from _crypto(text)
