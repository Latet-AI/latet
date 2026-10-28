"""A short secret's fake is never found by chance inside an unrelated word (0.10.1), in a person's vault (a computer's,
a person's space on a server): the gateway's restore of a model's answer, the unmask of the whole vault, mask's echo.

A secret shorter than twelve characters got a fake of its own shape in 0.10.0 (`password=1234`: four digits), which a
later text could hold by chance inside an id or a number (`order_id=9958213`): an unmask of the whole vault restored it
there, and mask's echo masked it there as a value of its own. Such a secret now gets a longer fake of its kinds, its
signs in place (twelve letters and digits, fifteen digits for a number). A short fake given before (eight characters
or fewer), kept for ever (R7), is restored by a conversation as 0.10.0 restored it, inside a word too, and by an unmask
of the whole vault only where it stands as a whole word. An API key's vault is ``test_stored_vault.py``'s, and the
server's applications API ``tests/server/test_apps_api.py``'s."""

import json
import re
import string

import pytest

from latet.config import Config
from latet.engine.build import build, build_masker
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.terms import KnownValuesDetector
from latet.engine.masker import MaskReport
from latet.engine.pipeline import Engine, Session
from latet.engine.rehydrate import restore_text, restores_for
from latet.engine.surrogates.generators import Drbg, shape
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Entity, Vault, derive_keys, fake_reading
from latet.gateway.mediate import Mediator

from ..conftest import ROOT_KEY

PAIR = "INFO login ok password=1234 user=ada"  # a password of four digits (PASSWORD)
TOKEN = "INFO auth ok token=ab12f3 user=ada"  # a token of six hexadecimal digits (ASSIGNED_SECRET)
OLD = (("PASSWORD", "1234", "assignment:password"), ("ASSIGNED_SECRET", "ab12f3", "assignment:token"))
# The review's probe of 0.10.1: short secrets whose values a later text holds inside a longer word or number.
INSIDE = (("PASSWORD", "postgres", None), ("PASSWORD", "1234", None), ("PASSWORD", "admin1", None))
INSIDE_TEXT = (
    "connect to postgresql://app@db.internal/shop; data in /var/lib/postgres/data; "
    "GET /orders 200 order_id=9912343 took 12 ms; user admin12 logged in"
)
DIGITS, HEX_LOWER, HEX_UPPER = string.digits, string.digits + "abcdef", string.digits + "ABCDEF"


def fake_of_0_10_0(etype: str, value: str, root: bytes = ROOT_KEY) -> str:
    """The fake 0.10.0 gave the secret ``value`` at its first draw: its shape (digits for digits, hexadecimal digits for
    hexadecimal), as long as it is."""
    return shape(value, Drbg(derive_keys(root)["surrogate"], f"{etype}\x00{value}\x000".encode()))


def ids_holding(digits: str, hexadecimal: str) -> list[str]:
    """Log lines whose ids hold ``digits`` or ``hexadecimal`` inside them, by chance: none of them ever held a value."""
    return [
        f"GET /orders 200 order_id=99{digits}13 took 12 ms",
        f"GET /orders 200 request_id=347bf{hexadecimal}20a2290 took 9 ms",
        f"GET /orders 200 request_id=347bf{digits}a2290 trace={hexadecimal}9 ok",
    ]


def given_before(vault: Vault, secrets=OLD) -> list[Entity]:
    """``secrets`` (type, value, rule) as 0.10.0 kept them: each with a fake of its shape."""
    return [
        Entity(
            vault.entity_id(etype, value), etype, EntityClass.SECRET, value, fake_of_0_10_0(etype, value), 0, 0.0, rule
        )
        for etype, value, rule in secrets
    ]


def inside_unmasked(masked: str, old: list[Entity]) -> str:
    """``INSIDE_TEXT`` masked (``masked``) as an unmask of the whole vault gives it back: the value standing as a whole
    word restored (`/var/lib/postgres/data`), those masked inside a longer word or number left as their short fakes, the
    cost of 0.10.1's rule (`postgresql`, `order_id=9912343`, `admin12`)."""
    fakes = {e.original: e.surrogate for e in old}
    expected = (
        INSIDE_TEXT.replace("postgresql", fakes["postgres"] + "ql")
        .replace("9912343", f"99{fakes['1234']}3")
        .replace("admin12", fakes["admin1"] + "2")
    )
    assert masked == expected.replace("/postgres/", f"/{fakes['postgres']}/")  # as 0.10.0 masked it
    return expected


def _engine(vault: Vault) -> Engine:
    masker, _ = build_masker(Config(profile="balanced", ner="off"), vault)
    return Engine(vault, masker)


def _mask(engine: Engine, text: str, session: Session | None = None):
    return engine.mask_text(text, SlotKind.TOOL_RESULT, session or Session("t", kept=False))


def _back(engine: Engine, session: Session, masked: str) -> str:
    """``masked``, a model's answer in ``session``, as the gateway restores it: plain, and as a tool call's JSON."""
    plain = restore_text(masked, engine.restores(session, json_mode=False))[0]
    call = json.dumps({"command": masked}, ensure_ascii=False)
    assert json.loads(restore_text(call, engine.restores(session, json_mode=True))[0])["command"] == plain
    return plain


def test_a_short_secret_found_in_a_text_gets_a_long_fake_of_its_kinds():
    """Fifteen digits for a number: ten kinds of characters make fewer strings than sixteen, and logs are full of
    long numbers."""
    engine = _engine(Vault(ROOT_KEY))
    [digits] = [r.replacement for r in _mask(engine, PAIR).replacements]
    [hexadecimal] = [r.replacement for r in _mask(engine, TOKEN).replacements]
    assert len(digits) == 15 and set(digits) <= set(DIGITS) and digits[0] != "0"  # a number stays one
    assert len(hexadecimal) == 12 and set(hexadecimal) <= set(HEX_LOWER)
    assert "1234" not in digits and "ab12f3" not in hexadecimal


LOWER_DIGITS = string.ascii_lowercase + DIGITS
EVERY_KIND = string.ascii_letters + DIGITS
_RUN = re.compile(r"[A-Za-z0-9]+")


@pytest.mark.parametrize(
    ("etype", "value", "kinds", "count"),
    [
        ("PASSWORD", "1234", DIGITS, 15),
        ("PASSWORD", "0042", DIGITS, 15),
        ("PASSWORD", "7", DIGITS, 15),
        ("PASSWORD", "-12", DIGITS, 15),
        ("PASSWORD", "1.5", DIGITS, 15),
        ("PASSWORD", "ab12", HEX_LOWER, 12),
        ("PASSWORD", "AB12", HEX_UPPER, 12),
        ("PASSWORD", "x9q", LOWER_DIGITS, 12),
        ("PASSWORD", "hunter2", LOWER_DIGITS, 12),
        ("PASSWORD", "pw5Kz", EVERY_KIND, 12),
        ("PASSWORD", "WINTER", string.ascii_uppercase + DIGITS, 12),
        ("PASSWORD", "Summer2024!", EVERY_KIND, 12),  # a sign at its end: kept there
        ("PASSWORD", "abcdefg-h", LOWER_DIGITS, 12),  # a sign inside: kept there, its longest run longer
        ("PASSWORD", "p@ss w0rd!", LOWER_DIGITS, 12),
        ("API_KEY", "tok_ab12", LOWER_DIGITS, 12),
        ("API_KEY", "Zt8kQ2pW", EVERY_KIND, 12),
        ("ASSIGNED_SECRET", "Zq81vT5mWx7", EVERY_KIND, 12),  # eleven characters
        ("URL_PASSWORD", "s3cr3t", LOWER_DIGITS, 12),
    ],
)
def test_every_short_secret_gets_a_long_fake_of_its_kinds_with_its_signs_in_place(etype, value, kinds, count):
    """Its signs and spaces where the value has them, its runs of letters and digits between them as many, drawn from
    the kinds of the value's, the longest of them longer: as many letters and digits as ``count``. Never one that
    holds the value; drawn from the vault's key, the same again."""
    fakes = [Vault(bytes([n]) * 32).intern(etype, EntityClass.SECRET, value).surrogate for n in range(40)]
    for fake in fakes:
        assert _RUN.sub("~", fake) == _RUN.sub("~", value), fake  # the signs in place, the runs as many
        drawn = "".join(_RUN.findall(fake))
        assert len(drawn) == count and set(drawn) <= set(kinds), fake
        assert value.casefold() not in fake.casefold(), fake
    assert len(set(fakes)) == len(fakes)
    assert Vault(bytes([3]) * 32).intern(etype, EntityClass.SECRET, value).surrogate == fakes[3]


@pytest.mark.parametrize("number", [-12, 1.5, 0.25, -0.5, 1234])
def test_a_secret_json_number_stays_a_number_of_its_kind_and_comes_back(number):
    """The review's M4: a secret that is a JSON number (a field rule's, read where strict data mode reads a document's
    numbers) keeps its sign and its point, and gets a fake that JSON reads and writes again as it is: masked into a
    number of its kind, not a string, and given back as the number it was (0.10.0 kept a number; 0.10.1's first fake
    of letters made one of `-12` a string)."""
    built = build(Config(data="strict"), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    session = built.engine.session("s")
    doc = json.dumps({"password": number})
    out = mediator.mask_value(doc, SlotKind.TOOL_RESULT, session, MaskReport(), tool="Bash")
    [replacement] = out.replacements
    assert replacement.eclass == EntityClass.SECRET and replacement.original == json.dumps(number)
    masked = json.loads(out.text)["password"]
    assert type(masked) is type(number) and masked != number and (masked < 0) == (number < 0)
    assert json.dumps(masked) == replacement.replacement  # read and written again as it was drawn
    assert json.loads(restore_text(out.text, built.engine.restores(session, json_mode=False))[0]) == {
        "password": number
    }


@pytest.mark.parametrize("value", ["0", "7", "-0", "0.5", "10.05", "-12", "1.5"])
def test_a_short_numbers_fake_is_a_json_number_written_as_json_writes_it(value):
    """No zero first, none last after a point, at most fifteen digits: JSON reads the fake as a number of the value's
    kind and writes it again as it is, under every key of a vault."""
    for n in range(40):
        fake = Vault(bytes([n]) * 32).intern("PASSWORD", EntityClass.SECRET, value).surrogate
        read = json.loads(fake)
        assert type(read) is type(json.loads(value)) and json.dumps(read) == fake, fake


def test_a_long_secret_personal_data_and_a_recognizers_secret_keep_their_shape():
    """Only a secret shorter than twelve characters that Latet fakes itself changes: one of twelve or more keeps its
    shape, personal data keeps its shape whatever its length (a phone number of ten digits, a term of yours), and a
    recognizer of yours keeps the fake its rule says (`surrogate: shape`)."""
    vault = Vault(ROOT_KEY)
    long = vault.intern("PASSWORD", EntityClass.SECRET, "Winter2024xy").surrogate
    assert len(long) == 12 and long[:1].isupper() and long[1:6].islower() and long[6:10].isdigit()
    phone = vault.intern("PHONE", EntityClass.PII, "3331234567").surrogate
    assert len(phone) == 10 and phone.isdigit() and phone != "3331234567"
    custom = vault.intern("CUSTOM", EntityClass.CUSTOM, "Q7-1").surrogate
    assert len(custom) == 4 and custom[0].isupper() and custom[1].isdigit() and custom[2] == "-"
    badge = vault.intern("BADGE", EntityClass.SECRET, "AB-1234", generate=shape).surrogate
    assert len(badge) == 7 and badge[2] == "-" and badge[:2].isupper() and badge[3:].isdigit()


def test_no_id_that_never_held_a_short_secret_is_unmasked_or_masked_by_chance():
    """The finding of 0.11.0's Task 1: lines whose ids hold the digits or the hexadecimal digits a short secret's fake
    had in 0.10.0 come back as they were from the unmask of the whole vault and from the gateway's restore of a model's
    answer, and mask keeps no value of them (the echo of a fake found inside a word was one, then one of its fake)."""
    vault = Vault(ROOT_KEY)
    engine = _engine(vault)
    session = Session("conversation", kept=False)
    _mask(engine, PAIR, session)
    _mask(engine, TOKEN, session)
    lines = ids_holding(fake_of_0_10_0("PASSWORD", "1234"), fake_of_0_10_0("ASSIGNED_SECRET", "ab12f3"))
    count = vault.count
    for line in lines:
        assert engine.unmask(line) == (line, 0)
        assert restore_text(line, engine.restores(session, json_mode=False)) == (line, [])
        assert _mask(engine, line).text == line
    assert vault.count == count


def test_a_conversation_restores_a_short_fake_given_before_as_0_10_0_did():
    """The review of 0.10.1 (C1): a value of a short secret of 0.10.0 inside a longer word or number (`postgres` in
    `postgresql://`, `1234` in `order_id=9912343`, `admin1` in `admin12`) is masked there with its short fake, as
    always; the model's answer, which a conversation restores from the fakes it was shown alone, comes back whole, the
    short fakes put back inside the words too, as 0.10.0 put them."""
    old = given_before(Vault(ROOT_KEY), INSIDE)
    engine = _engine(Vault(ROOT_KEY, old))
    session = Session("conversation", kept=False)
    masked = _mask(engine, INSIDE_TEXT, session).text
    inside_unmasked(masked, old)  # masked as 0.10.0 masked it
    assert _back(engine, session, masked) == INSIDE_TEXT


def test_a_conversation_masks_a_short_fake_given_before_inside_a_word_and_its_answer_comes_back_as_sent():
    """A conversation restores a short fake of 0.10.0 inside a word, so its masking finds one there too, as 0.10.0
    did: an id a client sends that holds one by chance is masked there as a value of its own (mask's echo), and the
    model's answer that names the id comes back as the client sent it, not with the secret inside."""
    old = given_before(Vault(ROOT_KEY))
    engine = _engine(Vault(ROOT_KEY, old))
    session = Session("conversation", kept=False)
    digits, hexadecimal = (e.surrogate for e in old)
    for line in ids_holding(digits, hexadecimal):
        masked = _mask(engine, line, session).text
        assert masked != line  # the short fake inside the id: masked as a value of its own
        assert _back(engine, session, masked) == line
    answer = f'password={digits} and the token "{hexadecimal}".'
    assert _back(engine, session, answer) == answer  # the conversation was shown neither of them
    engine.show(session, [e.id for e in old])
    assert _back(engine, session, answer) == 'password=1234 and the token "ab12f3".'


def test_an_unmask_of_the_whole_vault_restores_a_short_fake_given_before_only_as_a_whole_word():
    """``Engine.unmask``, which reads every fake of the vault for every text (the applications API's unmask), puts a
    short fake of 0.10.0 back where it stands as a whole word, never inside an id or a number that holds it by chance.
    The cost: a value of such a secret masked inside a longer word stays its fake there (the review's C1, documented)."""
    old = given_before(Vault(ROOT_KEY))
    engine = _engine(Vault(ROOT_KEY, old))
    digits, hexadecimal = (e.surrogate for e in old)
    assert len(digits) == 4 and len(hexadecimal) == 6
    answer = f'password={digits} and the token "{hexadecimal}".'
    assert engine.unmask(answer) == ('password=1234 and the token "ab12f3".', 2)
    for line in ids_holding(digits, hexadecimal):
        assert engine.unmask(line) == (line, 0)
    inside = given_before(Vault(ROOT_KEY), INSIDE)
    engine = _engine(Vault(ROOT_KEY, inside))
    masked = _mask(engine, INSIDE_TEXT).text
    assert engine.unmask(masked) == (inside_unmasked(masked, inside), 1)


def test_a_fake_of_nine_to_eleven_characters_given_before_is_restored_inside_a_word_everywhere():
    """A secret of 9 to 11 characters got a fake of as many in 0.10.0: as rare by chance in a text as a longer one, it
    is restored inside a word as 0.10.0 restored it, by a conversation and by an unmask of the whole vault alike."""
    old = given_before(Vault(ROOT_KEY), (("PASSWORD", "Winter2024", None), ("PASSWORD", "s3cr3tpass1", None)))
    assert [len(e.surrogate) for e in old] == [10, 11]
    engine = _engine(Vault(ROOT_KEY, old))
    session = Session("conversation", kept=False)
    text = "ref xWinter2024y and zzs3cr3tpass1qq end"
    masked = _mask(engine, text, session).text
    assert masked == text.replace("Winter2024", old[0].surrogate).replace("s3cr3tpass1", old[1].surrogate)
    assert _back(engine, session, masked) == text
    assert engine.unmask(masked) == (text, 2)


@pytest.mark.parametrize(
    "text",
    [
        "xhunter2y",
        "ab12y",
        "tok_ab12Rossi",
        "request_id=347bfab12a2290",
        "pw5Kzpw5Kz and q7pw5Kz9",
        "ref xSummer2024!y and xabcdefg-hy end",
    ],
)
def test_a_short_secret_glued_to_letters_or_digits_is_masked_and_restored_whole(text):
    """A secret is found inside a word (`xhunter2y`), and its fake, now of twelve letters and digits and its signs, is
    restored there: the text comes back whole, from the unmask of the whole vault and from the gateway's restore, plain
    and in JSON."""
    vault = Vault(ROOT_KEY)
    values = ("hunter2", "ab12", "pw5Kz", "Summer2024!", "abcdefg-h")
    for value in values:
        vault.intern("PASSWORD", EntityClass.SECRET, value)
    engine = _engine(vault)
    session = Session("conversation", kept=False)
    masked = _mask(engine, text, session)
    assert masked.replacements and not any(value in masked.text for value in values if value in text)
    assert all(len("".join(_RUN.findall(r.replacement))) == 12 for r in masked.replacements)
    assert engine.unmask(masked.text)[0] == text
    assert _back(engine, session, masked.text) == text


def test_one_decision_for_each_entity_restores_and_finds_its_fake():
    """The review's M3: whether a fake is short is the entity's (``vault.short_fake``: its fake as written, eight
    characters or fewer), for its restores and for the detectors of its fake alike, its reading included: a fake
    written in escapes whose reading is short (`k%40qz%21`, read `k@qz!`) is no short fake, found and restored inside a
    word; a short one (`5821`) is found inside a word only by a conversation's detector (``echo_inside``)."""
    short = Entity("e1", "PASSWORD", EntityClass.SECRET, "1234", "5821", 0, 0.0, None)
    escaped = Entity("e2", "PASSWORD", EntityClass.SECRET, "p%40ss%21", "k%40qz%21", 0, 0.0, None, decoded=True)
    assert fake_reading(escaped) == "k@qz!"
    vault = Vault(ROOT_KEY, [short, escaped])
    held = vault.matchers.current()
    text = "order 99582113 and xk@qz!y, pin 5821"

    def found(detector) -> list[str]:
        return [text[s.start : s.end] for s in sorted(detector.detect(text, DetectContext()), key=lambda s: s.start)]

    assert found(held.echo) == ["k@qz!", "5821"]  # the short fake as a whole word alone
    assert found(held.echo_inside) == ["5821", "5821"]  # a conversation's: inside a word too
    restores = restores_for([short, escaped], whole_vault=True)
    assert {"5821", "k%40qz%21", "k@qz!"} <= {r.pattern for r in restores}
    assert all(r.word == (r.entity_id == "e1") for r in restores)
    assert not any(r.word for r in restores_for([short, escaped]))  # a conversation's: inside a word too
    assert _engine(vault).unmask(text) == ("order 99582113 and xp@ss!y, pin 1234", 2)
    strings = [("5821", "PASSWORD", EntityClass.SECRET), ("582194736201", "PASSWORD", EntityClass.SECRET)]
    spans = KnownValuesDetector(strings, rule="echo", whole={"5821"}).detect(
        "x5821 5821 x582194736201y", DetectContext()
    )
    assert [(s.start, s.end) for s in sorted(spans, key=lambda s: s.start)] == [(6, 10), (12, 24)]
