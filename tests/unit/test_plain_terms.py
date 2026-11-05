"""The other occurrences of a value found in a text (its repeats) looked for with ``str.find``, where an RE2 detector
was compiled for each text: the same terms as ``KnownValuesDetector`` keeps, found as it finds them, and where two
found overlap or a value is not ASCII, the detector as before."""

import pytest

from latet.config import Config
from latet.engine.build import build_masker
from latet.engine.detectors import terms as terms_module
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.terms import KnownValuesDetector, NotPlain, PlainTerms
from latet.engine.masker import Masker
from latet.engine.types import EntityClass, SlotKind, Span
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY

NER, PII, SECRET = EntityClass.NER, EntityClass.PII, EntityClass.SECRET


def _known(values: list[tuple[str, str, EntityClass]], text: str) -> list[Span]:
    return list(KnownValuesDetector(values, rule="known-values").detect(text, DetectContext()))


def _found(spans: list[Span], text: str) -> list[tuple[str, str]]:
    return [(text[s.start : s.end], s.etype) for s in spans]


def test_a_name_inside_a_longer_word_is_not_found():
    values = [("Zoë", "PERSON", NER), ("Rossi", "PERSON", NER)]
    plain = PlainTerms(values)
    assert plain.spans("Zoëlla Rossini") == []
    assert plain.strict_spans("Zoëlla Rossini") == []
    text = "Zoëlla Rossini met Zoë Rossi."
    assert _found(plain.spans(text), text) == [("Zoë", "PERSON"), ("Rossi", "PERSON")]
    assert plain.spans(text) == _known(values, text)


def test_a_secret_is_found_inside_a_word():
    values = [("tok9fQ2xL7pZ", "API_KEY", SECRET), ("Mario Rossi", "PERSON", NER)]
    text = "xtok9fQ2xL7pZy and Mario Rossiz"
    plain = PlainTerms(values)
    assert _found(plain.strict_spans(text), text) == [("tok9fQ2xL7pZ", "API_KEY")]
    assert plain.strict_spans(text) == plain.spans(text) == _known(values, text)


def test_a_string_of_whole_is_found_as_a_whole_word_a_secrets_too():
    # A vault's short fakes (``vault.short_fake``) are given so (``known._Part``): the same strings, the same spans.
    values = [("ab12", "API_KEY", SECRET), ("tok9fQ2xL7pZ", "API_KEY", SECRET)]
    text = "order_id=9ab12x key=ab12 and xtok9fQ2xL7pZy"
    plain = PlainTerms(values, whole={"ab12"})
    assert _found(plain.strict_spans(text), text) == [("ab12", "API_KEY"), ("tok9fQ2xL7pZ", "API_KEY")]
    known = list(KnownValuesDetector(values, rule="known-values", whole={"ab12"}).detect(text, DetectContext()))
    assert plain.strict_spans(text) == plain.spans(text) == known
    assert text[known[0].start : known[0].end] == "ab12" and known[0].start == text.index("key=ab12") + 4


def test_a_persons_name_from_three_letters():
    values = [("Ugo", "PERSON", NER), ("abc", "DOMAIN", PII), ("Ada", "API_KEY", SECRET)]
    text = "Ugo wrote to abc and Ada"
    plain = PlainTerms(values)
    assert _found(plain.strict_spans(text), text) == [("Ugo", "PERSON")]
    assert plain.strict_spans(text) == _known(values, text)
    assert _found(PlainTerms(values, min_length=3).strict_spans(text), text) == [
        ("Ugo", "PERSON"),
        ("abc", "DOMAIN"),
        ("Ada", "API_KEY"),
    ]


def test_a_term_not_ascii_or_overlapping_is_not_plain():
    with pytest.raises(NotPlain):
        PlainTerms([("Niccolò", "PERSON", NER)]).strict_spans("Niccolò Ferri wrote")
    with pytest.raises(NotPlain):
        PlainTerms([("Anna Maria", "PERSON", NER), ("Maria Rossi", "PERSON", NER)]).strict_spans("Anna Maria Rossi")
    # Not found in the text, a value not ASCII changes nothing; a value found inside the one found where it starts
    # (an address's domain, inside the address) is left out by KnownValuesDetector too.
    values = [("Niccolò", "PERSON", NER), ("ops@acme-intra.it", "EMAIL", PII), ("acme-intra.it", "DOMAIN", PII)]
    text = "mail ops@acme-intra.it or see acme-intra.it"
    assert _found(PlainTerms(values).strict_spans(text), text) == [
        ("ops@acme-intra.it", "EMAIL"),
        ("acme-intra.it", "DOMAIN"),
    ]
    assert PlainTerms(values).strict_spans(text) == _known(values, text)


def test_overlaps_are_cut_as_known_values_cut_them():
    values = [("Anna Maria", "PERSON", NER), ("Maria Rossi", "PERSON", NER)]
    text = "Anna Maria Rossi"
    found = PlainTerms(values).spans(text)
    assert found == _known(values, text)
    assert {i for s in found for i in range(s.start, s.end)} == set(range(16))


def test_a_text_joined_of_lines_cuts_each_lines_overlaps_within_its_own_bound():
    """The review of Task 7: read whole, a line dense with overlaps (120 `Mario Rossi`, each overlapped by a name of
    twenty words) spent the bound on cutting overlaps of the next line too, and `Rossi Bianchi` across `Mario Rossi`
    there was left out. Read as a text joined of lines (``lines``), each line has its own, as the detector reads it and
    as each line alone: the empty set, of what the detector finds, that ``spans`` does not."""
    values = [
        ("Mario Rossi", "PERSON", NER),
        (" ".join(["Rossi", "Mario"] * 10), "PERSON", NER),
        ("Rossi Bianchi", "PERSON", NER),
    ]
    lines = [" ".join(["Mario Rossi"] * 120), "ticket closed by Mario Rossi Bianchi today"]
    text = "\n\n".join(lines)
    detector = KnownValuesDetector(values, rule="known-values")
    found = set(PlainTerms(values).spans(text, lines=True))
    assert set(detector.detect(text, DetectContext(joined=True))) <= found
    second = text.index(lines[1])
    alone = {(s.start + second, s.end + second) for s in PlainTerms(values).spans(lines[1])}
    assert alone <= {(s.start, s.end) for s in found} and any(text[s.start : s.end] == "Rossi Bianchi" for s in found)


def test_a_text_too_long_for_its_terms_is_left_to_the_detector():
    # str.find reads the text once for each term, RE2 once for all: past _PLAIN_READ characters read, RE2 costs less.
    values = [("Mario Rossi", "PERSON", NER), ("tok9fQ2xL7pZ", "API_KEY", SECRET)]
    text = "Mario Rossi " + "x" * (terms_module._PLAIN_READ // 2) + " Mario Rossi"
    with pytest.raises(NotPlain):
        PlainTerms(values).strict_spans(text)
    assert PlainTerms(values).spans(text) == _known(values, text)
    shorter = text[: terms_module._PLAIN_READ // 4]
    assert PlainTerms(values).strict_spans(shorter) == _known(values, shorter) != []


def test_a_later_value_of_the_same_string_names_its_type():
    values = [("acme-intra.it", "EMAIL", PII), ("acme-intra.it", "DOMAIN", PII), ("Ugo Bassi", "PERSON", NER)]
    text = "acme-intra.it, Ugo Bassi"
    assert _found(PlainTerms(values).strict_spans(text), text) == [("acme-intra.it", "DOMAIN"), ("Ugo Bassi", "PERSON")]
    assert PlainTerms(values).strict_spans(text) == _known(values, text)


def test_repeats_compile_no_detector_for_a_log_line(monkeypatch):
    line = (
        "2026-10-24T09:00:01.247Z WARN auth-service login failed for samuel.dorlanlan@veberlielmtrade.ch from "
        "60.221.55.221: invalid password; retry by samuel.dorlanlan@veberlielmtrade.ch from 60.221.55.221 at "
        "veberlielmtrade.ch"
    )
    masker, _ = build_masker(Config(profile="balanced", ner="off"), Vault(ROOT_KEY))
    made = []
    init = KnownValuesDetector.__init__

    def counting(self, *args, **kwargs):
        made.append(kwargs.get("rule"))
        init(self, *args, **kwargs)

    monkeypatch.setattr(terms_module.KnownValuesDetector, "__init__", counting)
    spans, _ = masker.spans(line, SlotKind.TOOL_RESULT)
    assert made == []
    assert [line[s.start : s.end] for s in spans if s.rule == "repeat"] == ["veberlielmtrade.ch"]
    monkeypatch.setattr(Masker, "plain_repeats", False)  # the bench's switch: an RE2 detector as before
    assert masker.spans(line, SlotKind.TOOL_RESULT)[0] == spans
    assert made == ["repeat"]
