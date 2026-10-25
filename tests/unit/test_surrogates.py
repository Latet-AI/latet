"""Fakes for people and places: a name in the language of the original, an address that keeps its shape, and never
the original itself, nor a word that names it."""

import base64
import hashlib
import ipaddress
import os
import random
import re
import string
import time

import pytest

from latet.config import Config
from latet.engine import masker as masker_module
from latet.engine import vault as vault_module
from latet.engine.build import build
from latet.engine.masker import ExtraValue
from latet.engine.rehydrate import restore_text, restores_for
from latet.engine.surrogates.data import names
from latet.engine.surrogates.generators import (
    GENERATORS,
    Drbg,
    email_domain,
    every_ip,
    gen_city,
    gen_company,
    gen_domain,
    gen_email,
    gen_hostname,
    gen_ip,
    gen_person,
    gen_street,
    gen_username,
    shape,
    written_email,
)
from latet.engine.types import EntityClass, Policy, SlotKind
from latet.engine.vault import Entity, MaskingError, Vault

KEY = b"t" * 32
EN_FIRST, EN_LAST, EN_CITIES = names.ENGLISH_FIRST_NAMES, names.ENGLISH_LAST_NAMES, names.ENGLISH_CITIES
IT_FIRST, IT_LAST, IT_CITIES = names.ITALIAN_FIRST_NAMES, names.ITALIAN_LAST_NAMES, names.ITALIAN_CITIES


def _fakes(generate, original: str, n: int = 40) -> list[str]:
    """The fakes of ``original`` under ``n`` seeds: what a vault draws from, value by value."""
    return [generate(original, Drbg(KEY, f"{original}-{i}".encode())) for i in range(n)]


def _lower(words) -> set[str]:
    return {w.lower() for w in words}


@pytest.mark.parametrize(
    ("original", "first", "last"),
    [
        ("Emily Carter", EN_FIRST, EN_LAST),
        ("Sarah Mitchell", EN_FIRST, EN_LAST),  # in no list: an English ending
        ("Giulia Bianchi", IT_FIRST, IT_LAST),
        ("Lucio Fumagalli", IT_FIRST, IT_LAST),  # in no list: an Italian ending
    ],
)
def test_a_name_gets_a_fake_in_its_own_language(original, first, last):
    for fake in _fakes(gen_person, original):
        given, family = fake.split()
        assert given in first and family in last, fake


def test_one_name_alone_keeps_its_language_and_its_case():
    assert all(fake in EN_FIRST for fake in _fakes(gen_person, "Emily"))
    assert all(fake in IT_FIRST for fake in _fakes(gen_person, "Paolo"))
    assert all(fake.isupper() for fake in _fakes(gen_person, "EMILY CARTER"))


def test_an_email_address_gets_a_name_in_the_language_of_its_own():
    for original, first, last in (
        ("emily.carter@northwind-legal.com", EN_FIRST, EN_LAST),
        ("giulia.bianchi@acme.it", IT_FIRST, IT_LAST),
    ):
        for fake in _fakes(gen_email, original):
            given, family = fake.split("@")[0].split(".")
            assert given in _lower(first) and family in _lower(last), fake


@pytest.mark.parametrize(
    ("original", "address"),
    [
        ("daniel dot okafor at stmarys-health dot org", "daniel.okafor@stmarys-health.org"),
        ("Daniel DOT Okafor AT stmarys-health DOT org", "Daniel.Okafor@stmarys-health.org"),
        ("daniel.okafor (at) stmarys-health.org", "daniel.okafor@stmarys-health.org"),
        ("daniel[dot]okafor[at]stmarys-health[dot]org", "daniel.okafor@stmarys-health.org"),
    ],
)
def test_an_address_written_out_is_read_as_the_address_it_stands_for(original, address):
    written = written_email(original)
    assert written is not None and written.address == address


@pytest.mark.parametrize("text", ["Daniel Okafor", "meet at noon", "at the desk", "a@b.org", "x at y at z.org"])
def test_words_that_write_no_address_are_none(text):
    assert written_email(text) is None


@pytest.mark.parametrize(
    ("value", "domain"),
    [
        ("ops@acme-intra.it", "acme-intra.it"),
        ("daniel dot okafor at stmarys-health dot org", "stmarys-health.org"),
        ("Daniel DOT Okafor AT stmarys-health DOT org", "stmarys-health.org"),
        ("daniel.okafor (at) stmarys-health.org", "stmarys-health.org"),
        ("meet at noon", ""),
        ("the ward clerk", ""),
    ],
)
def test_an_address_has_the_domain_it_names_whether_written_or_written_out(value, domain):
    assert email_domain(value) == domain


def test_a_written_out_address_gets_a_fake_written_out_the_same_way():
    for fake in _fakes(gen_email, "daniel dot okafor at stmarys-health dot org"):
        assert re.fullmatch(r"[a-z]+ dot [a-z]+ at [a-z0-9-]+ dot example", fake), fake
    for fake in _fakes(gen_email, "daniel.okafor (at) stmarys-health.org"):
        assert re.fullmatch(r"[a-z]+\.[a-z]+ \(at\) [a-z0-9-]+\.example", fake), fake


def test_what_writes_no_address_gets_a_fake_of_its_own_shape():
    for fake in _fakes(gen_email, "the ward clerk"):
        assert "@" not in fake and len(fake) == len("the ward clerk") and fake.count(" ") == 2, fake


def test_a_user_name_gets_a_name_in_its_language():
    for original, first in (("ecarter", EN_FIRST), ("nicola", IT_FIRST)):
        for fake in _fakes(gen_username, original):
            m = re.fullmatch(r"([a-z]+)\d\d", fake)
            assert m is not None and m[1] in _lower(first), fake


def test_a_us_address_keeps_a_city_and_a_state_code():
    # The model's span stops before the ZIP code, which becomes a postcode of its own.
    for fake in _fakes(gen_street, "42 Maple Avenue, Springfield, IL"):
        m = re.fullmatch(r"\d\d (\w+) Avenue, (\w+), ([A-Z]{2})", fake)
        assert m is not None, fake
        assert m[1] in EN_LAST and m[2] in EN_CITIES and m[3] in names.US_STATES and m[3] != "IL", fake


def test_a_uk_address_keeps_a_city_and_the_shape_of_its_postcode():
    for fake in _fakes(gen_street, "42 Mill Lane, Bristol BS1 4DJ"):
        m = re.fullmatch(r"\d\d (\w+) Lane, (\w+) [A-Z]{2}\d \d[A-Z]{2}", fake)
        assert m is not None and m[1] in EN_LAST and m[2] in EN_CITIES and m[2] != "Bristol", fake


def test_an_italian_address_keeps_its_language_and_its_details():
    for fake in _fakes(gen_street, "via Garibaldi 12"):
        m = re.fullmatch(r"via (\w+) \d\d", fake)
        assert m is not None and m[1] in IT_LAST, fake
    for fake in _fakes(gen_street, "viale Monza 87/B, scala C"):
        m = re.fullmatch(r"viale (\w+) \d\d/[A-Z], scala [A-Z]", fake)
        assert m is not None and m[1] in IT_LAST, fake


def test_a_city_gets_a_city_of_its_language():
    assert all(fake in IT_CITIES for fake in _fakes(gen_city, "Milano"))
    assert all(fake in EN_CITIES and fake != "Bristol" for fake in _fakes(gen_city, "Bristol"))
    assert all(fake in EN_CITIES for fake in _fakes(gen_city, "Mountain View"))  # one city for a two-word name
    for fake in _fakes(gen_city, "Roma RM"):
        city, _, code = fake.rpartition(" ")
        assert city in IT_CITIES and code in names.IT_PROVINCES and code != "RM", fake


# A fake never shows its original: it never holds it, whatever the case, nor a word that names it or a part of one.
# The values weighed for each generator that draws words (names, companies, towns, a domain's word, a host's word).
_DRAWN = [
    *((gen_username, v) for v in ("emily", "nicola", "luca", "daniel", "sara", "mia", "anna1", "ecarter", "a", "ab")),
    (gen_username, "emily.carter"),
    *((gen_person, v) for v in ("Emily Carter", "Emily", "Giulia Bianchi", "Anna Smith", "Annabella Rossi", "A")),
    *((gen_email, v) for v in ("emily.carter@acme.com", "emily@gmail.com", "a@gmail.com", "luca.bianchi@contoso.it")),
    *((gen_company, v) for v in ("Contoso", "Northwind Traders", "Liberty Mutual", "Co")),
    *((gen_domain, v) for v in ("contoso.com", "northwind.io", "coho.net")),
    *((gen_street, v) for v in ("via Rossi 3", "12 Smith Street", "via Garibaldi 12", "Corso", "Piazza della", "VIA")),
    (gen_street, "42 Mill Lane, Bristol BS1 4DJ"),
    *((gen_city, v) for v in ("Lucca", "Bristol", "Roma RM", "Reading", "Lee")),
    *((gen_hostname, v) for v in ("host", "ho", "myhost", "t-", "emily-laptop", "node1")),
]
_LETTERS = re.compile(r"[^\W\d_]+")
_KEPT = names.STREET_TYPES | names.STREET_PARTICLES | names.ADDRESS_WORDS


def _naming(generate, original: str) -> set[str]:
    """The words of ``original`` that name it, of three letters or more: not those an address keeps (``via``,
    ``Lane``), a top-level domain, nor a mailbox provider's domain, which name nobody and stay as they are."""
    if generate in (gen_email, gen_domain):
        local, at, domain = original.rpartition("@")
        domain = domain if at else original
        domain = "" if domain.lower() in names.PUBLIC_MAIL_DOMAINS else domain.rpartition(".")[0]
        original = f"{local} {domain}"
    words = {w.casefold() for w in _LETTERS.findall(original)}
    return {w for w in words if len(w) >= 3 and w not in _KEPT}


def _shown(fake: str, original: str, naming: set[str]) -> bool:
    """Whether ``fake`` shows ``original``: it holds it, or a word of it holds a word that names it or is a part of
    one (of three letters or more), whatever the case."""
    theirs = {w.casefold() for w in _LETTERS.findall(fake)}
    return original.casefold() in fake.casefold() or any(
        word in their or (len(their) >= 3 and their in word) for word in naming for their in theirs
    )


@pytest.mark.parametrize(("generate", "original"), _DRAWN)
def test_no_fake_shows_its_original_nor_a_word_that_names_it(generate, original):
    # emily got emily59 once in 35 seeds, Emily Carter Emily Thompson, a@gmail.com roberta@gmail.com, Contoso
    # contoso-655c.example, Corso Corso DeLuca 3, a computer named host host-1a2b3c (which the final check then found
    # in the request, and stopped it).
    naming = _naming(generate, original)
    shown = [fake for fake in _fakes(generate, original, 400) if _shown(fake, original, naming)]
    assert shown == [], shown[:5]


def test_each_generator_of_the_module_is_weighed_here():
    # The others draw every character at random, the length mostly kept (a fake of the same length holds its original
    # only when it is it): whatever they draw, the vault refuses a fake that holds its original (below).
    at_random = {
        "JWT",
        "PHONE",
        "IBAN",
        "CREDIT_CARD",
        "IP_ADDRESS",
        "LATET_PERSONAL_TOKEN",
        "LATET_API_KEY",
        "LATET_ADMIN_TOKEN",
        "LATET_COMPUTER_KEY",
        "LATET_ROUTE_KEY",
    }
    drawn = {generate for generate, _ in _DRAWN}
    module = {etype: g for etype, g in GENERATORS.items() if g.__module__ == gen_person.__module__}
    assert {etype for etype, g in module.items() if g not in drawn} == at_random


def _keys(n: int) -> list[bytes]:
    return [hashlib.sha256(f"install {i}".encode()).digest() for i in range(n)]


@pytest.mark.parametrize(
    ("value", "etype"), [("emily", "USERNAME"), ("12", "USERNAME"), ("a", "USERNAME"), ("host", "HOSTNAME")]
)
def test_the_vault_never_keeps_a_fake_that_holds_its_original(value, etype):
    # A user name in a home path, of any length, and a computer's name: the fake of `emily` was `emily` and two digits
    # for one install in 35, of `12` a name and `12` once in a hundred, of `host` always `host-` and six digits.
    held = []
    for key in _keys(500):
        fake = Vault(key).intern(etype, EntityClass.IDENTITY, value).surrogate
        if value in fake.lower():
            held.append(fake)
    assert held == [], held[:5]


def test_a_fake_that_holds_its_original_is_drawn_again_whatever_makes_it():
    # A recognizer of yours, a country's numbers: whatever makes a fake, one that holds its original is drawn again,
    # and a maker that can do nothing else fails the masking, never lets the original through.
    def appended(original: str, rng: Drbg) -> str:
        return original + "9" if rng.below(2) else shape(original, rng)

    fakes = [Vault(key).intern("CUSTOM", EntityClass.CUSTOM, "Q7-1", generate=appended).surrogate for key in _keys(50)]
    assert [fake for fake in fakes if "q7-1" in fake.lower()] == []
    with pytest.raises(MaskingError):
        Vault(_keys(1)[0]).intern("CUSTOM", EntityClass.CUSTOM, "Q7-2", generate=lambda o, rng: f"x{o.lower()}")


def test_a_long_value_gets_its_fake_in_time_linear_in_its_length_and_a_shorter_one_keeps_its_fake():
    """A fake is drawn from a seed of its value: a seed of the whole of a long value was hashed again for every 32
    bytes the fake took, so that a password of a million characters in a tool's JSON took minutes to mask, holding a
    thread of the server. Past ``SEED_LONGEST`` characters the seed holds a digest of the value instead; a value up to
    that length keeps the fake it always had (the tutorial's pins, every vault's next draws), exactly."""
    vault = Vault(KEY)
    value = "x7" * (vault_module.SEED_LONGEST // 2)  # SEED_LONGEST characters: the seed holds it whole, as always
    seed = f"PASSWORD\x00{value}\x000".encode()
    assert vault._generate("PASSWORD", EntityClass.SECRET, value, 0, None) == shape(value, Drbg(vault._k_sur, seed))
    password = os.urandom(500_001).hex()[:1_000_000]
    started = time.perf_counter()
    entity = vault.intern("PASSWORD", EntityClass.SECRET, password)  # drawn by the vault itself, past the masker's cap
    took = time.perf_counter() - started
    assert len(entity.surrogate) == len(password) and entity.surrogate != password
    assert took < 10.0, f"{took:.1f} s"
    built = build(Config(), Vault(KEY), None)
    session = built.engine.session("cc:s1")
    started = time.perf_counter()
    masked = built.engine.mask_text(password, SlotKind.TOOL_RESULT, session, tool="Bash", field="password")
    took = time.perf_counter() - started
    assert password not in masked.text and len(masked.replacements) == 1  # in a tool's JSON: redacted, past the cap
    assert took < 10.0, f"{took:.1f} s"


def _pem(size: int) -> str:
    """A private key as a PEM block of ``size`` bytes, made here: a fake whose body is counted bytes."""
    body = base64.encodebytes(bytes((i * 7 + 3) % 256 for i in range(size))).decode()
    return f"-----BEGIN PRIVATE KEY-----\n{body}-----END PRIVATE KEY-----"


def test_a_value_longer_than_a_vault_keeps_is_redacted_never_kept_nor_restored():
    """A value of a masked type past ``VALUE_LONGEST`` characters is redacted as ``redact`` does: never kept in the
    vault (it would weigh its length in every space that knows it, and be kept in PostgreSQL), so never restored, and
    the dashboard's activity shows it redacted, linked to no entry. A PEM key of RSA 4096 bits, a certificate or a
    token stays under it, and keeps a fake of its own."""
    vault = Vault(KEY)
    built = build(Config(), vault, None)
    session = built.engine.session("cc:s1")
    password = os.urandom(10_000).hex()  # 20,000 characters
    assert len(password) > masker_module.VALUE_LONGEST
    masked = built.engine.mask_text(password, SlotKind.TOOL_RESULT, session, tool="Bash", field="password")
    assert masked.text == "[REDACTED:PASSWORD]" and vault.count == 0 and vault.by_original(password) is None
    [replacement] = masked.replacements
    assert replacement.policy == Policy.REDACT and replacement.entity_id is None
    key = _pem(2_375)  # the size of an RSA 4096 bits private key's PKCS#8 body: about 3,270 characters of PEM
    assert len(key) < masker_module.VALUE_LONGEST
    kept = built.engine.mask_text(key, SlotKind.TOOL_RESULT, session, tool="Bash", field="password")
    assert key not in kept.text and "REDACTED" not in kept.text
    assert kept.replacements and all(r.policy == Policy.MASK and r.entity_id for r in kept.replacements)
    assert vault.count >= 1 and all(vault.get(r.entity_id) is not None for r in kept.replacements)
    count, found = vault.count, os.urandom(9_000).hex()  # 18,000 characters the local reviewer reports
    taught = built.masker.mask(
        f"the key is {found}", SlotKind.TOOL_RESULT, extra=[ExtraValue(found, "SECRET", EntityClass.LLM, "llm:secret")]
    )
    assert taught.text == "the key is [REDACTED:SECRET]" and vault.count == count  # its own too: never kept


def _words(n: int, seed: int, make) -> list[str]:
    """``n`` distinct values made by ``make`` from a generator of random words of 4 to 9 letters (seeded)."""
    rng = random.Random(seed)

    def word() -> str:
        return "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(4, 9)))

    found: dict[str, None] = {}
    while len(found) < n:
        found[make(word, rng)] = None
    return list(found)


def _public_ipv4(n: int) -> list[str]:
    rng, found = random.Random(7), {}
    while len(found) < n:
        address = ipaddress.IPv4Address(rng.getrandbits(32))
        if address.is_global and not address.is_multicast:
            found[str(address)] = None
    return list(found)


def _interned(etype: str, eclass: EntityClass, values: list[str], bound: float) -> list[str]:
    """Each of ``values`` interned in one vault, in ``bound`` seconds at most: their fakes, one each."""
    vault = Vault(KEY)
    started = time.perf_counter()
    fakes = [vault.intern(etype, eclass, value).surrogate for value in values]  # a MaskingError fails the test
    took = time.perf_counter() - started
    assert took < bound, f"{took:.1f} s"
    assert len(set(fakes)) == len(values) and not set(fakes) & set(values)
    return fakes


# The blocks of IPv4 never routed on the internet that public addresses get their fakes from: the three of the
# documentation (RFC 5737) and benchmarking's (RFC 2544).
_NOT_ROUTED = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "198.18.0.0/15")]


def test_two_thousand_public_ipv4_addresses_each_get_a_fake_of_their_own_in_blocks_never_routed():
    # The three blocks of the documentation hold 762 fakes: the vault failed at the 600th address or so, and a log of
    # a service's clients stopped every request that carried it.
    fakes = _interned("IP_ADDRESS", EntityClass.PII, _public_ipv4(2_000), bound=10.0)
    assert all(any(ipaddress.ip_address(fake) in block for block in _NOT_ROUTED) for fake in fakes)
    assert not any(ipaddress.ip_address(fake).is_global for fake in fakes)


def test_a_thousand_mailbox_addresses_of_one_word_each_get_a_fake_of_their_own():
    # A first name alone: about 80 of them, and the vault failed within the first hundred addresses.
    values = _words(1_000, 1, lambda word, _: f"{word()}@gmail.com")
    fakes = _interned("EMAIL", EntityClass.PII, values, bound=10.0)
    assert all(re.fullmatch(r"[a-z]+\d*@gmail\.com", fake) for fake in fakes), fakes[-5:]


def test_five_thousand_first_last_mailbox_addresses_each_get_a_fake_of_their_own():
    # First and last name of one language: about 3,170 pairs, and the vault failed around the 1,700th address.
    values = _words(5_000, 2, lambda word, _: f"{word()}.{word()}@gmail.com")
    fakes = _interned("EMAIL", EntityClass.PII, values, bound=20.0)
    assert all(re.fullmatch(r"[a-z]+\.[a-z]+\d*@gmail\.com", fake) for fake in fakes), fakes[-5:]


@pytest.mark.parametrize(
    ("etype", "eclass", "make"),
    [
        ("EMAIL", EntityClass.PII, lambda word, _: f"{word()}@acme-corp.it"),  # a company's people, by first name
        ("EMAIL", EntityClass.PII, lambda word, _: f"{word()}.{word()}@acme-corp.it"),
        ("PERSON", EntityClass.NER, lambda word, _: word().capitalize()),  # a first name alone: 80 of them
        ("PERSON", EntityClass.NER, lambda word, _: f"{word().capitalize()} {word().capitalize()}"),
        ("CLIENT", EntityClass.CUSTOM, lambda word, _: word().capitalize()),  # a term of yours: 31 company words
        ("CLIENT", EntityClass.CUSTOM, lambda word, _: f"{word().capitalize()} {word().capitalize()}"),
        ("ORG", EntityClass.LLM, lambda word, _: word().capitalize()),
        ("PROJECT", EntityClass.CUSTOM, lambda word, _: word().capitalize()),
        ("CITY", EntityClass.NER, lambda word, _: word().capitalize()),  # 12 towns of each language
        ("STREET_ADDRESS", EntityClass.NER, lambda word, _: f"via {word().capitalize()}"),  # 44 family names
        ("USERNAME", EntityClass.IDENTITY, lambda word, _: word()),
    ],
)
def test_two_thousand_values_drawn_from_a_list_of_words_each_get_a_fake_of_their_own(etype, eclass, make):
    _interned(etype, eclass, _words(2_000, 3, make), bound=20.0)


def test_once_its_list_is_used_a_mailbox_name_gets_digits_as_people_write_them():
    vault = Vault(KEY)
    for word in _words(400, 4, lambda word, _: word()):
        vault.intern("EMAIL", EntityClass.PII, f"{word}@gmail.com")
    one = vault.intern("EMAIL", EntityClass.PII, "qwzkvy@gmail.com").surrogate
    assert re.fullmatch(r"[a-z]+\d{2,}@gmail\.com", one), one


def _wide(generate, wide: int):
    return lambda original, rng: generate(original, rng, wide=wide)


_ALL_FIRST, _ALL_LAST = _lower(names.FIRST_NAMES), _lower(names.LAST_NAMES)


def test_the_wider_forms_of_a_fake_keep_the_form_of_its_value():
    # Wider forms come in turn, each once the narrower ones are taken: they stay what the value is, a name, an address,
    # a company, a town, a street, and never show the value.
    for fake in _fakes(_wide(gen_email, 1), "mario.rossi@gmail.com"):
        given, family = fake.removesuffix("@gmail.com").split(".")
        assert given in _ALL_FIRST and family in _ALL_LAST, fake  # both languages' lists
    for fake in _fakes(_wide(gen_email, 2), "mario.rossi@acme.it"):
        assert re.fullmatch(r"[a-z]+\.[a-z]+\d\d@[a-z]+-[0-9a-f]{4}\.example", fake), fake
    for fake in _fakes(_wide(gen_email, 3), "mario@gmail.com"):
        assert re.fullmatch(r"[a-z]+\d{4}@gmail\.com", fake) and fake[:-14] in _ALL_FIRST, fake
    for fake in _fakes(_wide(gen_username, 3), "ecarter"):
        assert re.fullmatch(r"[a-z]+\d{6}", fake), fake
    for fake in _fakes(_wide(gen_person, 2), "Mario Rossi"):
        given, more, family = fake.split()
        assert {given.lower(), more.lower()} <= _ALL_FIRST and family.lower() in _ALL_LAST, fake
    for fake in _fakes(_wide(gen_person, 2), "Emily"):
        given, family = fake.split()
        assert given.lower() in _ALL_FIRST and family.lower() in _ALL_LAST, fake
    for fake in _fakes(_wide(gen_company, 1), "Globex"):
        assert all(word in names.COMPANIES for word in fake.split()) and len(fake.split()) == 2, fake
    for fake in _fakes(_wide(gen_city, 1), "Milano"):
        word, family = fake.split()
        assert word in names.ITALIAN_TOWN_WORDS and family in IT_LAST, fake
    for fake in _fakes(_wide(gen_city, 2), "Bristol"):
        part, made = fake.split(" ", 1)
        assert part in names.ENGLISH_TOWN_PARTS and any(made.startswith(f) for f in EN_LAST), fake
    for fake in _fakes(_wide(gen_street, 2), "via Garibaldi 12"):
        m = re.fullmatch(r"via (\w+) (\w+) \d\d", fake)
        assert m is not None and m[1].lower() in _ALL_FIRST and m[2].lower() in _ALL_LAST, fake
    for fake in _fakes(_wide(gen_ip, 1), "8.8.4.4"):
        assert any(ipaddress.ip_address(fake) in block for block in _NOT_ROUTED), fake
    for generate, original in _DRAWN:
        if generate in (gen_email, gen_username, gen_person, gen_company, gen_city, gen_street):
            naming = _naming(generate, original)
            for wide in (1, 2, 3):
                try:
                    shown = [f for f in _fakes(_wide(generate, wide), original, 50) if _shown(f, original, naming)]
                except IndexError:  # no word left that does not show it: the vault gives it its shape
                    continue
                assert shown == [], (wide, shown[:5])


@pytest.mark.parametrize(
    ("etype", "eclass", "value"),
    [
        ("IP_ADDRESS", EntityClass.PII, "8.8.4.4"),  # every address of its blocks, past the random ones
        ("EMAIL", EntityClass.PII, "luca@gmail.com"),
        ("PERSON", EntityClass.NER, "Emily Carter"),
    ],
)
def test_a_store_that_refuses_every_fake_is_asked_no_more_often_than_before(etype, eclass, value):
    # Each fake a store refuses is a round trip to PostgreSQL on a server: the wider forms never make a value ask it
    # more often than a value of a structure always could (80), whatever is left to try.
    asked: list[str] = []

    def refuse(entity):
        asked.append(entity.surrogate)
        raise vault_module.FakeTaken(f"the fake made for a {entity.etype} is another value's")

    with pytest.raises(MaskingError, match=f"no collision-free surrogate for a {etype}"):
        Vault(KEY, on_insert=refuse).intern(etype, eclass, value)
    assert 16 <= len(asked) <= 80


def test_a_fake_person_with_a_name_more_gives_each_name_back_for_its_own():
    # `Marco Luca Ferri` for `Mario Rossi`: the model's `Marco` is Mario, its `Ferri` is Rossi, its `Luca` is no one;
    # `Marco Ferri` for `Mario` alone: `Marco` is Mario.
    entities = [
        Entity("p1", "PERSON", EntityClass.NER, "Mario Rossi", "Marco Luca Ferri", 32, 0.0),
        Entity("p2", "PERSON", EntityClass.NER, "Giulia", "Sara Galli", 33, 0.0),
    ]
    restores = restores_for(entities)
    text = "Marco Luca Ferri wrote. Marco said Ferri and Luca agree. Sara Galli too, and Sara, not Galli."
    restored, _ = restore_text(text, restores)
    assert restored == "Mario Rossi wrote. Mario said Rossi and Luca agree. Giulia too, and Giulia, not Galli."


def test_a_public_address_takes_198_19_before_198_18_which_proxies_give_their_hosts():
    # Clash, Mihomo and Surge in fake-ip mode give hosts addresses of 198.18.0.0/16 by default, which Latet leaves
    # unmasked (private): a fake there could be such a host's. The wider form draws from 198.19.0.0/16, and every
    # address of the documentation's blocks and of 198.19 is taken before one of 198.18.
    first = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "198.19.0.0/16")]
    last = ipaddress.ip_network("198.18.0.0/16")
    for fake in _fakes(_wide(gen_ip, 1), "8.8.4.4", 400):
        assert any(ipaddress.ip_address(fake) in block for block in first), fake
    every = list(every_ip("8.8.4.4", Drbg(KEY, b"seed")))
    tier = (3 + 256) * 254
    assert all(any(ipaddress.ip_address(fake) in block for block in first) for fake in every[:tier])
    assert all(ipaddress.ip_address(fake) in last for fake in every[tier:]) and len(every) - tier == 256 * 254


def test_every_fake_of_an_ipv4_address_is_tried_once_its_blocks_fill_up():
    every = list(every_ip("8.8.4.4", Drbg(KEY, b"seed")))
    assert len(every) == len(set(every)) == 515 * 254  # three /24 of the documentation and the /15 of benchmarking
    assert all(any(ipaddress.ip_address(fake) in block for block in _NOT_ROUTED) for fake in every[::997])
    private = list(every_ip("192.168.1.20", Drbg(KEY, b"seed")))
    assert len(set(private)) == 256 * 254 and all(fake.startswith("192.168.") for fake in private)
    assert list(every_ip("2a00::1", Drbg(KEY, b"seed"))) == [] and list(every_ip("not one", Drbg(KEY, b"s"))) == []
    vault = Vault(KEY)  # the vault takes the first free one once the blocks hold all but a few
    taken = set(every[:-3])
    vault._taken = lambda candidate: candidate in taken  # type: ignore[method-assign]
    assert vault.intern("IP_ADDRESS", EntityClass.PII, "8.8.4.4").surrogate in every[-3:]


def test_a_new_value_gets_the_fake_it_got_before_wherever_one_was_free():
    # Only a value that found no free fake gets a wider one: the others' fakes are those Latet always gave, so a vault
    # shared by two versions agrees, value by value.
    vault = Vault(KEY)
    fakes = {
        value: vault.intern(etype, eclass, value).surrogate
        for etype, eclass, value in (
            ("EMAIL", EntityClass.PII, "mario.rossi@gmail.com"),
            ("EMAIL", EntityClass.PII, "giulia@acme.it"),
            ("PERSON", EntityClass.NER, "Mario Rossi"),
            ("CLIENT", EntityClass.CUSTOM, "Globex"),
            ("CITY", EntityClass.NER, "Milano"),
            ("IP_ADDRESS", EntityClass.PII, "8.8.4.4"),
            ("USERNAME", EntityClass.IDENTITY, "ecarter"),
        )
    }
    assert fakes == {
        "mario.rossi@gmail.com": "stefano.leone@gmail.com",
        "giulia@acme.it": "antonio@margie-87a2.example",
        "Mario Rossi": "Alessandro Vitale",
        "Globex": "Consolidated",
        "Milano": "Rovigo",
        "8.8.4.4": "203.0.113.5",
        "ecarter": "noah44",
    }


def test_a_value_every_name_would_show_gets_a_fake_of_its_shape():
    # An address whose name holds every first name of its language: no name is left to draw, so its fake keeps its
    # shape, as a value no generator can fake does, never a name that shows it.
    local = "".join(name.lower() for name in IT_FIRST)
    entity = Vault(_keys(1)[0]).intern("EMAIL", EntityClass.PII, f"{local}@gmail.com")
    assert len(entity.surrogate) == len(local) + len("@gmail.com") and local not in entity.surrogate
