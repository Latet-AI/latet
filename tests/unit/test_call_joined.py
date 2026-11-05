"""An applications call's texts read as one text (``Masker.joined``, the server performance plan's Task 4): the texts
that hold no line break, joined by a blank line in parts of at most ``JOINED_CHARS`` characters, where no name model
reads their kind; a value found across two texts has those texts read alone. A call masks in each text what that text
masked read alone, never less (Review Focus 4); the bench corpora neither less nor more."""

import contextlib
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from latet.bridge.apps import Apps
from latet.bridge.review import MASKED
from latet.config import Config, FieldRule, IdentityConfig, TermConfig
from latet.engine import masker as masker_module
from latet.engine.build import build, build_masker
from latet.engine.detectors.base import DetectContext, DetectorSkipped, line_pattern
from latet.engine.detectors.gitleaks import GitleaksDetector, line_regex, load_ruleset
from latet.engine.detectors.pii import PiiDetector
from latet.engine.masker import JOIN, JOINED_CHARS, MaskReport
from latet.engine.pipeline import Session
from latet.engine.types import EntityClass, Policy, SlotKind, Span
from latet.engine.vault import Vault
from latet.gateway.app import OnServer
from latet.gateway.mediate import Mediator

from ..conftest import ROOT_KEY

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.append(str(ROOT / "scripts"))

import spans_golden  # noqa: E402
from vault_fuzz import PIECES_0_10_1, fuzz_texts  # noqa: E402

DATA, PROSE = SlotKind.TOOL_RESULT, SlotKind.USER_TEXT
CALL = 500  # texts in a call of the corpora


def _mediator(config: Config | None = None) -> Mediator:
    return _mediator_over(Vault(ROOT_KEY), config)


def _mediator_over(vault: Vault, config: Config | None = None) -> Mediator:
    built = build(config or Config(profile="balanced", ner="off"), vault)
    return Mediator(built.engine, built.verifier)


def _call(mediator: Mediator, texts: list[str], kind: SlotKind, *, joined: bool = True) -> list[set[int]]:
    """Where each text of an applications call is masked, the call masked as ``Apps._mask`` masks it (its texts each
    masked as one text, then those a later one made stale masked again): read joined (``Masker.joined``), or each
    alone as 0.10.0 read them."""
    session = Session("apps", kept=False)
    report = MaskReport()
    vault = mediator.engine.vault
    masker = mediator._masker(session)
    with vault.holding(scoped=True):
        vault.prefetch(texts)
        call = mediator.call()
        with masker.joined(texts, kind, report) if joined else contextlib.nullcontext():
            done = [
                mediator.mask_value(text, kind, session, report, keep_surrogates=False, call=call, as_text=True)
                for text in texts
            ]
        mediator.settle(call)
    return [{i for r in d.replacements if r.policy != Policy.ALLOW for i in range(r.start, r.end)} for d in done]


def _differ(
    calls: list[list[str]], kind: SlotKind, config: Config | None = None, stubs: Callable[[], list] | None = None
) -> tuple[list, list]:
    """The positions each call masks read alone and not joined (``less``), and joined and not alone (``more``), on two
    fresh vaults of one key: a list of (text, positions) for each. ``stubs``: detectors added to each masker."""
    less, more = [], []
    alone, joined = _mediator(config), _mediator(config)
    for mediator in (alone, joined) if stubs is not None else ():
        mediator.engine.masker.detectors.extend(stubs())
    for texts in calls:
        for text, a, j in zip(texts, _call(alone, texts, kind, joined=False), _call(joined, texts, kind), strict=True):
            if a - j:
                less.append((text[:120], sorted(a - j)))
            if j - a:
                more.append((text[:120], sorted(j - a)))
    return less, more


def _calls(texts: list[str], size: int = CALL) -> list[list[str]]:
    return [texts[i : i + size] for i in range(0, len(texts), size)]


def test_joined_masks_as_each_alone_on_the_bench_corpora():
    corpora = {corpus.name: corpus for corpus in spans_golden.corpora()}
    lines, escaped = corpora["log lines"], corpora["JSON lines, escaped"]
    assert len(lines.texts) == 4 * CALL and len(escaped.texts) == CALL
    for corpus in (lines, escaped):
        less, more = _differ(_calls(corpus.texts), corpus.kind)
        assert less == [] and more == [], (corpus.name, less[:3], more[:3])


def test_joined_masks_as_each_alone_on_the_fuzz_texts():
    """Neither less nor more. (Before parts ended at the texts read alone, 13 of the 5,000 masked more: the fuzz's
    addresses glued to an `@` written in escapes, `acme.it&#64;`, read as values of their own.) The texts of 0.10.1's
    pieces: those the integration review added put secrets after `password=` and `api_key: ` in many texts, each found
    read joined in the texts of its part before the one that names it too, which masks more by design."""
    less, more = _differ(_calls(fuzz_texts(10 * CALL, PIECES_0_10_1)), DATA)
    assert less == [] and more == [], (less[:3], more[:3])


def test_joined_masks_no_less_than_each_alone_on_the_fuzz_texts_with_assignments():
    """With the integration review's pieces too (`password=`, `api_key: `), never less. (Before ``settle`` read the
    original text too, one text did: `password=0542811101`, which a later part taught, around the secret its own
    part had masked.)"""
    less, _ = _differ(_calls(fuzz_texts(10 * CALL)), DATA)
    assert less == [], less[:3]


@pytest.mark.parametrize("kind", [DATA, PROSE])
def test_an_address_found_in_its_escapes_is_masked_as_written_in_a_later_text_of_its_part(kind):
    """A value one text's escapes read (`Marialuca.neri%40acme.it`, its `%40` an `@` where the text is a URL's quote)
    and a later text of its part holds as written, where that text's escapes read otherwise (a log's bytes): read
    alone, the later text finds it in the vault, which keeps it as written; read joined, as a repeat of it, the same
    value with the same fake. Before, the repeat was looked for as it reads alone, and a shorter address the vault
    knew (`neri%40acme.it`, from a call before) was masked inside it, which left `Marialuca.` in clear and no value
    in clear for ``settle`` to find (the fuzz's texts with 0.10.1's pieces)."""
    texts = ["D%27Angelo&#242;Marialuca.neri%40acme.it'%C3%B2q", "1234&#64;Marialuca.neri%40acme.it" + "\\" * 4 + "xb2"]
    masked = []
    for joined in (False, True):
        mediator = _mediator()
        mediator.engine.vault.intern("EMAIL", EntityClass.PII, "neri%40acme.it")
        mediator.engine.masker.joins = joined
        out = Apps(mediator, OnServer("https://latet.invalid", "u_key", kind="key"))._mask(texts, kind)[1]
        masked.append([entry[MASKED].text for entry in out])
    alone, joined = masked
    assert "Marialuca" not in joined[1] and "neri%40acme.it" not in joined[1], joined
    assert joined == alone


@pytest.mark.parametrize("kind", [DATA, PROSE])
def test_a_value_masked_in_a_group_with_a_secret_is_masked_as_a_secret_in_a_later_text_of_its_part(kind):
    """An IBAN that holds a short secret the vault knew from 0.10.0 (`1234`) is masked as one group, named by the
    secret, and the vault keeps it as a secret, found inside a word: read alone, a later text that glues the IBAN to
    other letters (`Corp12347IT60X…`) finds it there; read joined, as a repeat of the group, the same value with the
    same fake. Before, the repeat was looked for as an IBAN, a whole word, and only the secret was masked inside it,
    the rest of the IBAN left in clear (the fuzz's texts with 0.10.1's pieces)."""
    iban = "IT60X0542811101000000123456"
    texts = [f"admin1abcdefg-hRossiRossi%20  {iban} Ferri", f"Acme Corp12347{iban} %20c3"]
    masked = []
    for joined in (False, True):
        mediator = _mediator()
        mediator.engine.vault.intern("PASSWORD", EntityClass.SECRET, "1234")
        mediator.engine.masker.joins = joined
        out = Apps(mediator, OnServer("https://latet.invalid", "u_key", kind="key"))._mask(texts, kind)[1]
        masked.append([entry[MASKED].text for entry in out])
    alone, joined = masked
    assert all(iban[:12] not in text for text in joined), joined
    assert joined == alone


@pytest.mark.parametrize("glued", ["u.", " u."], ids=["glued on both sides", "glued on one side"])
@pytest.mark.parametrize("kind", [DATA, PROSE])
def test_a_group_a_repeat_forms_is_masked_as_the_vault_keeps_it_in_a_later_text_of_its_part(kind, glued):
    """A secret one text's detector finds (`password=0542811101`) sits inside an IBAN of the next text, where its
    repeat and the IBAN are masked as one group named by the secret: read alone, the vault keeps the IBAN as a secret,
    and a third text that glues it to other letters finds it there, whole. Read joined, that group exists only once the
    repeats are found: it is looked for then too (the integration review). Before, only the secret was masked inside
    the third text's IBAN, `IT60X` and `000000123456` left in clear, which neither ``settle`` nor the cache could see."""
    iban = "IT60X0542811101000000123456"
    texts = ["password=0542811101", f"x {iban} y", f"Acme Corp{iban}{glued}"]
    less, _ = _differ([texts], kind)
    assert less == [], less
    masked = []
    for joined in (False, True):
        mediator = _mediator()
        mediator.engine.masker.joins = joined
        out = Apps(mediator, OnServer("https://latet.invalid", "u_key", kind="key"))._mask(texts, kind)[1]
        masked.append([entry[MASKED].text for entry in out])
    alone, joined_texts = masked
    assert all("IT60X" not in text and "000000123456" not in text for text in joined_texts), joined_texts
    assert joined_texts == alone


_IBAN = "IT60X0542811101000000123456"
# The masked text first, its secret and the IBAN's group taught by later texts (one read alone between: a later part).
_TAUGHT_AROUND = {
    "the glued IBAN first": [f"Mario{_IBAN}Ferri", "x password=0542811101 y", "%3D\nu", f"{_IBAN} Acme"],
    "the secret first": ["x password=0542811101 y", f"Mario{_IBAN}Ferri", "%3D\nu", f"{_IBAN} Acme"],
}


@pytest.mark.parametrize("joined", [False, True], ids=["alone", "joined"])
@pytest.mark.parametrize("order", list(_TAUGHT_AROUND))
def test_a_value_a_later_text_teaches_around_values_masked_already_is_masked_whole(order, joined):
    """A text whose secret is masked already (`0542811101`, inside a glued IBAN) when a later text of the call makes
    the IBAN a secret's group: ``settle`` masks it again, whole. Before, the masked text held no value in clear, so the
    cache took it as fresh and the IBAN's other characters stayed in clear: read alone with the secret first (0.10.0
    and 0.10.1 so too), and read joined in either order, its part finding the secret in the IBAN at once."""
    texts = _TAUGHT_AROUND[order]
    mediator = _mediator()
    mediator.engine.masker.joins = joined
    out = Apps(mediator, OnServer("https://latet.invalid", "u_key", kind="key"))._mask(texts, DATA)[1]
    masked = out[texts.index(f"Mario{_IBAN}Ferri")][MASKED].text
    assert "IT60X" not in masked and "000000" not in masked, masked


def test_a_conversation_masks_whole_a_value_a_later_text_teaches_around_values_masked_already():
    """The same in a conversation's request: its texts masked one by one, then those a later one made stale."""
    texts = _TAUGHT_AROUND["the secret first"]
    mediator = _mediator()
    session = Session("conversation")
    report = MaskReport()
    call = mediator.call()
    done = [mediator.mask_value(t, DATA, session, report, call=call, as_text=True) for t in texts]
    mediator.settle(call)
    masked = done[texts.index(f"Mario{_IBAN}Ferri")].text
    assert "IT60X" not in masked and "000000" not in masked, masked


# Review Focus 4: what a structure carries from one text to the next. Read alone, each text masks what it masks; read
# joined, never less (more may be: a value a later text names, found where an earlier one names it too).
CARRIED = {
    "a CSV header, then two rows": [
        "name,email,password",
        "Mario,mario.rossi@acme-intra.it,Hunter2!xQ",
        "Ada,ada.byron@acme-intra.it,S3cret#yZ",
    ],
    "a pipe table split by line": ["| name | password |", "| --- | --- |", "| Mario | Hunter2!xQ |"],
    "a PEM key's begin, body and end": [
        "-----BEGIN RSA PRIVATE KEY-----",
        "MIIEowIBAAKCAQEA7x2y3z4w5v6u7t8s9r0qp1o2n3m4l5k6j7i8h9g0f",
        "-----END RSA PRIVATE KEY-----",
    ],
    "password= then its value": ["password=", "Zx8vQ2mN5pL0wR7t"],
    "<tr>, a cell, </tr>": ["<tr>", "<td>mario.rossi@acme-intra.it</td>", "</tr>"],
    "+39 333 then 1234567": ["+39 333", "1234567"],
    "a fiscal code's keyword, then the code": ["codice fiscale:", "RSSMRA85T10A562S"],
    # A secret rule's pattern reads through the blank line (``[\s'"]{0,3}`` after a name): a match it would refuse there
    # took the place of the pair the next text holds alone.
    "a secret's name, then a pair a secret rule reads": ["token:", "account_api=Zx8vQ2mN5pL0wR7t"],
    # An HTML row left open takes the next text's header into it: its cells lose the column that says secret.
    "a row left open, then a header and its row": [
        "<tr><td>a</td>",
        "<tr><th>password</th></tr><tr><td>Hunter2xyzQ</td></tr>",
    ],
    # A JSON line whose escapes read as a table (`\n` a line break, as soon as one escape writes a letter): the number's
    # column is named in the first line of its own reading, never in the first text of the call.
    "a text, then a JSON line that reads as a table with a header": [
        '{"note": "first"}',
        '{"rows": "SSN;' + "remarks about the person in the second column" * 2 + '\\n536-90-4399;Ad\\u00e8"}',
    ],
}


@pytest.mark.parametrize("kind", [DATA, PROSE])
def test_texts_joined_never_mask_less_than_each_alone(kind):
    found = {name: _differ([texts], kind) for name, texts in CARRIED.items()}
    less = {name: lost for name, (lost, _) in found.items() if lost}
    more = {name: extra for name, (_, extra) in found.items() if extra}
    print("more, joined:", more)  # listed, not failed
    assert less == {}


# Your own rules read a structure across texts too: a field kept in clear in strict data mode, named by a header in
# another text (an HTML table's, an accessibility snapshot's), and a recognizer that asks for a whole text (``^...$``).
YOURS = [
    (
        Config(profile="balanced", ner="off", data="strict", fields=[FieldRule(name="note", mask=False)]),
        ["<tr><th>note</th></tr>", "<tr><td>Mario Rossi</td></tr>", "<tr><td>Ada Byron</td></tr>"],
    ),
    (
        Config(profile="balanced", ner="off", data="strict", fields=[FieldRule(name="note", mask=False)]),
        ['columnheader "note"', 'cell "Mario Rossi"', 'cell "Ada Byron"'],
    ),
    (
        Config(
            profile="balanced",
            ner="off",
            recognizers=[{"name": "employee", "pattern": r"^EMP-\d{6}$"}],
        ),
        ["EMP-123456", "EMP-654321", "an EMP-111111 in a sentence"],
    ),
    (  # read across the blank line, `1111\n\n4242` fails its check digits, and takes the place of `4242 4242`
        Config(
            profile="balanced",
            ner="off",
            recognizers=[{"name": "badge", "pattern": r"\b\d{4}\s*\d{4}\b", "checksum": "luhn"}],
        ),
        ["room 1111", "4242 4242 at the gate"],
    ),
]


@pytest.mark.parametrize(("config", "texts"), YOURS)
def test_texts_joined_never_mask_less_than_each_alone_under_rules_of_yours(config, texts):
    less, _ = _differ([texts], DATA, config)
    assert less == []


class _Across:
    """Finds ``left``, the blank line and ``right`` as one value: across two texts, in a text joined of them."""

    id = "across"
    mandatory = True

    def __init__(self) -> None:
        self.read: list[str] = []

    def detect(self, text, ctx):
        self.read.append(text)
        at = text.find(f"left{JOIN}right")
        if at >= 0:
            yield Span(at, at + len(f"left{JOIN}right"), "CUSTOM", EntityClass.CUSTOM, "across")


def test_a_span_across_two_texts_has_them_read_alone():
    texts = ["to mario.rossi@acme-intra.it left", "right then", "from ada.byron@acme-intra.it", "and the end"]
    alone, joined = _mediator(), _mediator()
    stubs = []
    for mediator in (alone, joined):
        stub = _Across()
        mediator.engine.masker.detectors.append(stub)
        stubs.append(stub)
    assert _call(joined, texts, DATA) == _call(alone, texts, DATA, joined=False)
    read = stubs[1].read
    assert read[0] == JOIN.join(texts)  # read joined first
    assert read[1] == texts[0]  # then the first alone: the value across it and the next is none of theirs
    # Read alone, it may have taught the vault a value: the texts after it read again, joined, the second no longer
    # reached by a span across.
    assert read[2] == JOIN.join(texts[1:])
    assert not {texts[1], texts[2], texts[3]} & set(read)


# A text read alone in the middle of a call teaches the vault a value that a later text holds bare: alone, the later text
# finds it in the vault; joined, it must not be answered by a reading made before that value was learnt.
LONG = "word " * (JOINED_CHARS // 5 + 10)
TAUGHT_BETWEEN = {
    "a text with a line break, a password": [
        "2026-10-27 12:00:00 INFO job queued",
        "2026-10-27 12:00:01 INFO job started",
        "user,password\nmario,Zx8vQ2mN5pL0wR7tQ",
        "2026-10-27 12:00:02 WARN retrying with Zx8vQ2mN5pL0wR7tQ",
        "2026-10-27 12:00:03 WARN retrying with Zx8vQ2mN5pL0wR7tQ again",
    ],
    "a text with a line break, an address's domain": [
        "hello there",
        "all fine",
        "contact:\nops@acme-intra.it",
        "see acme-intra.it for the terms",
        "and acme-intra.it again",
    ],
    "an HTML cell, an address's domain": [
        "hello there",
        "all fine",
        "<td>ops@acme-intra.it</td>",
        "see acme-intra.it for the terms",
        "and acme-intra.it again",
    ],
    "a text longer than a part, an address's domain": [
        "hello there",
        "all fine",
        f"{LONG}ops@acme-intra.it",
        "see acme-intra.it for the terms",
        "and acme-intra.it again",
    ],
}


@pytest.mark.parametrize("name", list(TAUGHT_BETWEEN))
@pytest.mark.parametrize("kind", [DATA, PROSE])
def test_a_value_a_text_read_alone_teaches_is_masked_in_the_texts_after_it(name, kind):
    less, _ = _differ([TAUGHT_BETWEEN[name]], kind)
    assert less == []


class _Teach:
    """Finds ``Zx8vQ2mN5pL0wR7tQ`` after ``teach:`` in a text without a blank line: a value only a text read alone
    finds, which the vault then holds."""

    id = "teach"
    mandatory = True

    def detect(self, text, ctx):
        at = text.find("teach: Zx8vQ2mN5pL0wR7tQ")
        if at >= 0 and JOIN not in text:
            yield Span(at + 7, at + 24, "CUSTOM", EntityClass.CUSTOM, "teach")


def test_a_value_a_text_split_by_a_span_across_teaches_is_masked_in_the_texts_after_it():
    """Two texts a span across splits are read alone after their part: what they teach the vault, the part's later
    texts hold bare, and they are read again (once; then alone)."""
    texts = [
        "hello there",
        "teach: Zx8vQ2mN5pL0wR7tQ left",
        "right then",
        "the code Zx8vQ2mN5pL0wR7tQ again",
        "and Zx8vQ2mN5pL0wR7tQ once more",
        "teach: Zx8vQ2mN5pL0wR7tQ left",
        "right and then",
        "Zx8vQ2mN5pL0wR7tQ at the end",
    ]
    less, _ = _differ([texts], DATA, stubs=lambda: [_Across(), _Teach()])
    assert less == []


def test_the_applications_api_masks_a_value_a_text_read_alone_teaches_in_a_later_text():
    texts = [
        "2026-10-27 12:00:01 INFO job started",
        "user,password\nmario,Zx8vQ2mN5pL0wR7tQ",
        "2026-10-27 12:00:02 WARN retrying with Zx8vQ2mN5pL0wR7tQ",
    ]
    apps = Apps(_mediator(), OnServer("https://latet.acme.lan", "u_billingcix", kind="key"))
    _, out, _, _ = apps._mask(texts, DATA)
    assert all("Zx8vQ2mN5pL0wR7tQ" not in o["text"] for o in out)


def _call_beside(mediator: Mediator, texts: list[str], joined: bool, other: list[str], after: int) -> list[str]:
    """The masked texts of a call masked as ``Apps._mask`` masks it, while another thread (the gateway's, or another
    call's, on the same space: one mediator, one vault) masks ``other`` as an applications call right after the
    ``after``-th text, to its end, before the next text is masked."""
    session = Session("apps", kept=False)
    report = MaskReport()
    vault = mediator.engine.vault
    beside = Apps(mediator, OnServer("https://latet.acme.lan", "u_billingcix", kind="key"))
    done = []
    with vault.holding(scoped=True):
        vault.prefetch(texts)
        call = mediator.call()
        with mediator._masker(session).joined(texts, DATA, report) if joined else contextlib.nullcontext():
            for k, text in enumerate(texts):
                done.append(mediator.mask_value(text, DATA, session, report, call=call, as_text=True))
                if k == after:
                    thread = threading.Thread(target=beside._mask, args=(list(other), DATA))
                    thread.start()
                    thread.join()
        mediator.settle(call)
    return [d.text for d in done]


SECRET = "Zx8vQ2mN5pL0wR7tQ"
BESIDE = {
    "a secret taught beside, held bare by the last text": (
        ["2026-10-27 12:00:01 INFO job started", "2026-10-27 12:00:02 INFO step one", f"WARN retrying with {SECRET}"],
        [f"user,password\nmario,{SECRET}"],
        SECRET,
    ),
    "a domain taught beside, held bare by a text in the middle": (
        ["hello there", "see acme-intra.it for the terms", "and a phone +39 347 123 4567 at the end"],
        ["contact:\nops@acme-intra.it"],
        "acme-intra.it",
    ),
}


@pytest.mark.parametrize("name", list(BESIDE))
@pytest.mark.parametrize("after", [0, 2])
def test_a_value_another_thread_teaches_while_a_part_is_answered_is_masked(name, after):
    """A value another thread teaches the vault after a call's part was read, before a text of it is masked: alone,
    that text finds it in the vault, or ``settle`` masks it again; joined, never less. (``after`` 2: past the last
    text, which ``settle`` caught already.)"""
    texts, other, bare = BESIDE[name]
    alone = _call_beside(_mediator(), texts, False, other, after)
    joined = _call_beside(_mediator(), texts, True, other, after)
    assert [bare in t for t in joined] == [bare in t for t in alone] == [False] * len(texts), (alone, joined)


def _spy(masker, cls=PiiDetector) -> list[str]:
    """The texts the masker's detector of ``cls`` reads, in order."""
    [detector] = [d for d in masker.detectors if isinstance(d, cls)]
    real = detector.detect
    read: list[str] = []

    def detect(text, ctx):
        read.append(text)
        return real(text, ctx)

    detector.detect = detect
    return read


def test_texts_with_a_line_break_are_read_alone():
    broken = ["one\ntwo", "one\rtwo", "one\N{LINE SEPARATOR}two", "one\x85two", "one\x0btwo", "ends with a break\n"]
    before = ["to mario.rossi@acme-intra.it", "and to ops@acme-intra.it"]
    after = ["from ada.byron@acme-intra.it", "and the end"]
    texts = [*before, *broken, *after]
    mediator = _mediator()
    read = _spy(mediator.engine.masker)
    positions = _call(mediator, texts, DATA)
    # Each read alone, in the call's order, between the part before them and the part after them (which they end).
    assert read == [JOIN.join(before), *broken, JOIN.join(after)]
    assert positions[0] and positions[-2]


def test_a_call_is_joined_in_parts_of_at_most_joined_chars():
    texts = [f"text {k} to mario.rossi@acme-intra.it " + "word " * 12_000 for k in range(10)]
    assert all(len(t) < JOINED_CHARS // 4 for t in texts)
    mediator = _mediator()
    read = _spy(mediator.engine.masker)
    positions = _call(mediator, texts, DATA)
    assert all(len(t) <= JOINED_CHARS for t in read)
    assert [t.count(JOIN) + 1 for t in read] == [4, 4, 2]  # as many as a part holds, in order
    assert all(len(p) == len("mario.rossi@acme-intra.it") for p in positions)


class _NameModel:
    """A detector that reads texts of ``PROSE`` ahead, as the local name model does."""

    id = "ner"
    mandatory = False
    kinds = frozenset({PROSE})

    def read_ahead(self, texts, kind, deadline):
        return True

    def detect(self, text, ctx):
        return iter(())


def test_nothing_is_joined_where_the_name_model_reads_the_kind():
    texts = ["to mario.rossi@acme-intra.it", "from ada.byron@acme-intra.it", "and the end"]
    for kind, joined in ((PROSE, False), (DATA, True)):
        mediator = _mediator()
        mediator.engine.masker.detectors.append(_NameModel())
        read = _spy(mediator.engine.masker)
        _call(mediator, texts, kind)
        assert read == ([JOIN.join(texts)] if joined else texts), kind


def test_a_calls_lines_are_read_once():
    lines = spans_golden.corpora()[0].texts[:50]
    mediator = _mediator()
    read = _spy(mediator.engine.masker)
    _call(mediator, lines, DATA)
    assert 0 < len(read) < len(lines)


def test_a_joined_reading_that_ran_weaker_answers_none_of_its_texts():
    """An optional detector that did not read the joined text would leave each text without it: each is read alone,
    and says what ran weaker on its own."""
    mediator = _mediator()
    [pii] = [d for d in mediator.engine.masker.detectors if isinstance(d, PiiDetector)]
    real = pii.detect

    def skipping(text, ctx):
        if "skip me" in text:
            raise DetectorSkipped("told to skip it")
        return real(text, ctx)

    pii.detect = skipping
    texts = ["skip me: +39 347 123 4567", "to mario.rossi@acme-intra.it"]
    session = Session("apps", kept=False)
    report = MaskReport()
    with mediator._masker(session).joined(texts, DATA, report):
        done = [mediator.mask_value(t, DATA, session, report, as_text=True) for t in texts]
    assert report.degraded == ["pii"]  # the first text's own, read alone
    assert "+39 347 123 4567" in done[0].text and "mario.rossi@acme-intra.it" not in done[1].text


def test_the_applications_api_reads_a_calls_texts_joined():
    lines = spans_golden.corpora()[0].texts[:40]
    mediator = _mediator()
    apps = Apps(mediator, OnServer("https://latet.acme.lan", "u_billingcix", kind="key"))
    read = _spy(mediator.engine.masker)
    _, out, _, _ = apps._mask(lines, DATA)
    masked = len(read)
    apps._scan(lines, DATA)
    assert 0 < masked < len(lines) and 0 < len(read) - masked < len(lines)
    alone = _call(_mediator(), lines, DATA, joined=False)
    assert [{i for r in o[MASKED].replacements for i in range(r.start, r.end)} for o in out] == alone


# --- secret rules and recognizers read a joined text line by line ------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "line"),
    [
        (r"abc", "(?m)abc"),
        (r"\Aabc\z", "(?m)^abc$"),
        (r"\\Aabc", r"(?m)\\Aabc"),  # a backslash, then an A
        (r"[\A-Z]x", None),  # \A in a class: not an anchor, nothing RE2 compiles either
        (r"\Qa.b\E", None),  # quoted: \A in it would be letters
        (r"(?-m)^abc$", None),  # multiline turned off: ^ and $ the text's ends
        (r"(?i:x)(?s)y", "(?m)(?i:x)(?s)y"),
    ],
)
def test_a_pattern_as_it_reads_a_line(pattern, line):
    assert line_pattern(pattern) == line


def test_every_secret_rule_reads_a_joined_text_as_each_line_alone():
    rules, _ = load_ruleset()
    corpora = spans_golden.corpora()
    texts = [t for t in corpora[0].texts[:300] + corpora[3].texts[:700] if "\n" not in t and "\r" not in t]
    texts += ["token:", "account_api=Zx8vQ2mN5pL0wR7t", "key = 'Zx8vQ2mN5pL0wR7tZx8v'", "openshift sha256~" + "a" * 43]
    joined = JOIN.join(texts)
    starts, at = [], 0
    for text in texts:
        starts.append(at)
        at += len(text) + len(JOIN)
    for rule in rules:
        line = line_regex(rule)
        each = [
            (starts[k] + m.start(), starts[k] + m.end()) for k, t in enumerate(texts) for m in rule.regex.finditer(t)
        ]
        assert [(m.start(), m.end()) for m in line.finditer(joined)] == each, rule.id
    assert GitleaksDetector().joinable


OVERLAPPING = ("Anna Maria", "Maria Rossi", "ab ab", "ab ab ab ab ab ab ab ab")
DENSE = [
    " ".join(["ab"] * 3000),
    "met Anna Maria Rossi today",
    " ".join(["Anna Maria Rossi"] * 400),
    "and Maria Rossi with Anna Maria Rossi, ab ab ab",
    "Mario Ferretti wrote to marco.f@studio-ferretti.it from /home/mferretti/src",
]


def _rich_masker(many: bool):
    """The balanced profile with terms that overlap (and 2,100 more with ``many``), an identity, and a vault holding
    those values too (and 2,100 more)."""
    names = [f"Client{k} Holding" for k in range(2100)] if many else []
    config = Config(
        profile="balanced",
        ner="off",
        terms=[TermConfig(value=v, type="CLIENT") for v in (*OVERLAPPING, *names)],
    )
    identity = IdentityConfig(full_name="Mario Ferretti", emails=["marco.f@studio-ferretti.it"], username="mferretti")
    vault = Vault(ROOT_KEY)
    for value in (*OVERLAPPING, "Rossi Anna", *(f"Supplier{k} Group" for k in range(2100 if many else 0))):
        vault.intern("PERSON", EntityClass.NER, value)
    masker, _ = build_masker(config, vault, identity)
    return masker


def test_a_dense_text_leaves_the_next_text_its_own_overlap_budget():
    config = Config(profile="balanced", ner="off", terms=[TermConfig(value=v, type="CLIENT") for v in OVERLAPPING])
    less, _ = _differ([[" ".join(["ab"] * 3000), "met Anna Maria Rossi today"]], DATA, config)
    assert less == []


# Names that overlap, found by their words in an API key's stored vault (``StoredVault.read_for``: ``TextValues``, with
# ``str.find``): a text of 120 `Mario Rossi`, a name of twenty words that overlaps each of them, then a text holding
# `Rossi Bianchi` across `Mario Rossi` (the review of Task 7).
OVERLAPPING_NAMES = ("Mario Rossi", " ".join(["Rossi", "Mario"] * 10), "Rossi Bianchi")
DENSE_NAMES = [" ".join(["Mario Rossi"] * 120), "ticket closed by Mario Rossi Bianchi today"]


def _stored_vault(values: tuple[str, ...], working: int = 0):
    """An API key's stored vault over a store of its own holding ``values`` as people's names, its memory ``working``."""
    from latet.engine.stored import StoredVault

    from .test_stored_vault import _Store

    store = _Store()
    plain = Vault(ROOT_KEY)  # its fakes drawn as a vault draws them, kept in the store at once
    for value in values:
        entity = plain.intern("PERSON", EntityClass.NER, value, rule="ner")
        store.rows[entity.id] = entity
    return StoredVault(ROOT_KEY, store, working=working, on_insert=store.save_entity, on_rule=store.save_rule)


@pytest.mark.parametrize("working", [0, 1_000], ids=["in the store", "in memory"])
def test_a_dense_text_leaves_the_next_text_its_own_overlap_budget_in_a_stored_vault(working):
    """The review of Task 7: a key's stored vault reads a part's texts for the values their words find with
    ``str.find`` (``TextValues``), each line of the part within its own bound on overlaps, as the detector of known
    values reads them and as each text alone: the dense first text leaves `Rossi Bianchi` of the second masked whole."""
    joined = _mediator_over(_stored_vault(OVERLAPPING_NAMES, working))
    alone = _mediator_over(_stored_vault(OVERLAPPING_NAMES, working))
    masked = {}
    for name, mediator, together in (("joined", joined, True), ("alone", alone, False)):
        masked[name] = _call(mediator, DENSE_NAMES, DATA, joined=together)
    assert masked["joined"][1] == masked["alone"][1]
    text = DENSE_NAMES[1]
    assert set(range(text.index("Bianchi"), len(text) - len(" today"))) <= masked["joined"][1]


@pytest.mark.parametrize("kind", [DATA, PROSE])
@pytest.mark.parametrize("rules", ["balanced", "terms, identity, vault", "many terms and values"])
def test_each_detector_reads_a_joined_part_as_each_text_alone(kind, rules):
    """Each detector finds in a part read joined what it finds in each of its texts alone, at the same places: the
    corpora's log lines and JSON lines, the fuzz's texts, and texts dense with overlapping terms, as ``Masker.joined``
    parts them. Of the balanced profile; with terms that overlap, an identity and a vault holding values (each matched
    by one alternation); and with more terms and values than an alternation takes (``WORD_INDEX_FROM``: a word index).
    A dense text spends its own overlaps' budget, never the next text's."""
    corpora = spans_golden.corpora()
    texts = list(dict.fromkeys(corpora[0].texts[:500] + corpora[1].texts[:200] + fuzz_texts(3000)))
    if rules == "balanced":
        masker = spans_golden.masker()
    else:
        masker = _rich_masker(many=rules == "many terms and values")
        texts = [*DENSE, *texts[:1500], *DENSE]
    parts = masker._parts(texts, kind)
    assert sum(len(part) for part in parts) > 1000
    for detector in [*masker.detectors, masker._known_values()]:
        for part in parts:
            starts = [0]
            for text in part[:-1]:
                starts.append(starts[-1] + len(text) + len(JOIN))
            joined = detector.detect(JOIN.join(part), DetectContext(kind, joined=True))
            alone = [
                (at + s.start, at + s.end, s.etype, s.rule)
                for at, text in zip(starts, part, strict=True)
                for s in detector.detect(text, DetectContext(kind))
            ]
            assert sorted((s.start, s.end, s.etype, s.rule) for s in joined) == sorted(alone), detector.id
    if rules == "balanced":
        return
    # An API key's stored vault holding the same values: a part is read for its texts' words (``for_text`` with the
    # texts it is joined of), its values and its fakes each line within its own bounds, as each text alone: the same
    # spans (a value its detectors hold, `ab ab`, is found twice where another text's words find its fake too, which
    # the merge of the spans takes as one).
    values = (
        *OVERLAPPING,
        "Rossi Anna",
        *(f"Supplier{k} Group" for k in range(2100 if rules.startswith("many") else 0)),
    )
    vault = _stored_vault((*values, *OVERLAPPING_NAMES))
    texts = [*DENSE_NAMES, *texts]
    with vault.holding(scoped=True):
        vault.prefetch(texts)
        for part in masker._parts(texts, kind):
            starts = [0]
            for text in part[:-1]:
                starts.append(starts[-1] + len(text) + len(JOIN))
            reading = vault.matchers.for_text(JOIN.join(part), pieces=part)
            for name in ("known", "echo"):
                joined = getattr(reading, name).detect(JOIN.join(part), DetectContext(kind, joined=True))
                alone = [
                    (at + s.start, at + s.end, s.etype, s.rule)
                    for at, text in zip(starts, part, strict=True)
                    for s in getattr(vault.matchers.for_text(text), name).detect(text, DetectContext(kind))
                ]
                assert {(s.start, s.end, s.etype, s.rule) for s in joined} == set(alone), name


def test_a_recognizer_whose_pattern_cannot_be_read_by_line_joins_nothing():
    config = Config(profile="balanced", ner="off", recognizers=[{"name": "quoted", "pattern": r"\QEMP-\E\d{6}"}])
    mediator = _mediator(config)
    read = _spy(mediator.engine.masker)
    texts = ["EMP-123456", "to mario.rossi@acme-intra.it"]
    _call(mediator, texts, DATA)
    assert read == texts


def test_a_text_a_structure_reads_with_the_lines_around_it_is_read_alone():
    texts = [
        "to ops@acme-intra.it",
        "to mario.rossi@acme-intra.it",
        "<TR><td>x</td>",
        'cell "x"',
        "from ada.byron@acme-intra.it",
    ]
    mediator = _mediator()
    read = _spy(mediator.engine.masker)
    _call(mediator, texts, DATA)
    assert read == [JOIN.join(texts[:2]), texts[2], texts[3], texts[4]]  # the last, a part of one, alone too


def test_the_switch_reads_each_text_alone(monkeypatch):
    monkeypatch.setattr(masker_module.Masker, "joins", False)
    texts = ["to mario.rossi@acme-intra.it", "from ada.byron@acme-intra.it"]
    mediator = _mediator()
    read = _spy(mediator.engine.masker)
    _call(mediator, texts, DATA)
    assert read == texts


def test_a_joined_text_is_read_with_its_context_flag():
    seen: list[DetectContext] = []

    class Context:
        id = "context"
        mandatory = True

        def detect(self, text, ctx):
            seen.append(ctx)
            return iter(())

    mediator = _mediator()
    mediator.engine.masker.detectors.append(Context())
    _call(mediator, ["one", "two"], DATA)
    assert [c.joined for c in seen] == [True]
