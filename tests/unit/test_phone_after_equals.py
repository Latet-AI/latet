"""A phone number written right after ``=`` (``phone=+39 347 123 4567``), or after another sign glued to it, is found; and
an unquoted ``key=value`` value of a field rule or of strict data mode takes in the spaced groups of a phone number that
begins it, so that ``+39`` is not masked alone with ``347 123 4567`` left in clear."""

import time

import phonenumbers
import pytest

from latet.config import Config, FieldRule
from latet.engine.build import build
from latet.engine.detectors import fields, pii
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.pii import PiiDetector
from latet.engine.types import SlotKind
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY

DATA = DetectContext(SlotKind.TOOL_RESULT)
PROSE = DetectContext(SlotKind.USER_TEXT)
CONTEXTS = pytest.mark.parametrize("ctx", [DATA, PROSE], ids=["data", "prose"])
WITH_PLUS = ["+39 347 123 4567", "+393471234567", "+49 151 23456789"]
WITH_ZEROS = ["0039 347 123 4567", "0039347 1234567"]
INTERNATIONAL = [*WITH_PLUS, WITH_ZEROS[0]]
# A key names a phone when its last word is a phone word, or "number" and the like after one.
PHONE_KEYS = [
    *("phone", "tel", "mobile", "cell", "fax", "TEL", "Phone", "gsm", "msisdn", "whatsapp", "telefono", "cellulare"),
    *("phone_number", "phoneNumber", "PhoneNumber", "mobile_phone", "contact-phone", "home.phone", "phone_no"),
    *("tel_num", "cell.nr", "mobile-number", "work_phone", "customerPhone", "user.mobile", "phone2"),
]
# A phone word as one of the words of a key is not enough: these hold ids, counts, versions and flags.
OTHER_KEYS = [
    *("qty", "count", "ts", "id", "x", "contact", "telemetry", "cellar", "hotel", "telnet_port", "order", "invoice"),
    *("cell_id", "cell_count", "cellId", "mobile_app", "mobile_version", "fax_sent", "gsm_band", "handy_id"),
    *("portable_mode", "phone_count", "phone_number_id", "phone_verified", "tel_type", "mobile_os", "number"),
]


def _found(text, ctx):
    return [(text[s.start : s.end], s.etype) for s in PiiDetector(enabled=["PHONE"]).detect(text, ctx)]


@CONTEXTS
@pytest.mark.parametrize("number", WITH_PLUS)
@pytest.mark.parametrize("key", ["phone", "tel", "mobile", "cell", "contact", "id", "x", *OTHER_KEYS[:8]])
def test_a_phone_with_a_plus_right_after_an_equal_sign_is_found_under_any_key(key, number, ctx):
    assert _found(f"2026-10-26 12:00:01 INFO user {key}={number} plan=gold", ctx) == [(number, "PHONE")]


@CONTEXTS
@pytest.mark.parametrize("number", WITH_ZEROS)
@pytest.mark.parametrize("key", PHONE_KEYS)
def test_a_phone_with_00_right_after_an_equal_sign_is_found_under_a_key_that_names_a_phone(key, number, ctx):
    assert _found(f"2026-10-26 12:00:01 INFO user {key}={number} plan=gold", ctx) == [(number, "PHONE")]


@CONTEXTS
@pytest.mark.parametrize("glue", ["=", ":", ",", ";", "|", "/", "#", "_", ".", "@"])
def test_a_phone_with_a_plus_is_found_after_any_sign_glued_to_it(glue, ctx):
    assert _found(f"call{glue}+39 347 123 4567 now", ctx) == [("+39 347 123 4567", "PHONE")]


@CONTEXTS
@pytest.mark.parametrize("key", PHONE_KEYS)
def test_a_number_without_a_prefix_is_found_after_a_key_that_names_a_phone(key, ctx):
    assert _found(f"user {key}=333 123 4567 plan=gold", ctx) == [("333 123 4567", "PHONE")]


@pytest.mark.parametrize("key", OTHER_KEYS)
def test_a_number_without_a_prefix_after_any_other_key_is_not_taken_for_a_phone(key):
    assert _found(f"user {key}=333 123 4567 plan=gold", DATA) == []
    assert _found(f"user {key}=3331234567 plan=gold", PROSE) == []
    assert _found(f"user {key}=3471234567 plan=gold", PROSE) == []


@CONTEXTS
@pytest.mark.parametrize("number", ["0012345678901", "0044 1234 567890", "0044123456789012", "0039347123456"])
@pytest.mark.parametrize("key", OTHER_KEYS)
def test_a_number_with_00_after_any_other_key_is_an_id_and_not_a_phone(key, number, ctx):
    assert _found(f"user {key}={number} plan=gold", ctx) == []


@CONTEXTS
@pytest.mark.parametrize(
    "text",
    [
        "ref=A+393471234567",  # glued to a letter or a digit: arithmetic or an id
        "sum=1+393471234567",
        "id_0039 347 123 4567",  # "00" is a prefix only right after the "=" of a key that names a phone
        "path/0039 347 123 4567",
        "v1.0039 347 123 4567",
        "id=0039 347 123 4567",
    ],
)
def test_a_phone_glued_to_a_letter_or_a_digit_stays_unfound(text, ctx):
    assert _found(text, ctx) == []


@CONTEXTS
@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("key", ["phone", "x"])
def test_a_quoted_phone_is_still_found(key, quote, ctx):
    number = "+39 347 123 4567"
    assert _found(f"{key}={quote}{number}{quote} plan=gold", ctx) == [(number, "PHONE")]


def _clear(text, config, slot):
    built = build(Config(ner="off", **config), Vault(ROOT_KEY))
    masked = built.masker.mask(text, slot).text
    return [group for group in ("347", "123", "4567", "151", "23456789") if group in masked], masked


MODES = {
    "balanced": {"profile": "balanced"},
    "max": {"profile": "max"},
    "a field rule": {"profile": "secrets", "fields": [FieldRule(name="phone")]},
    "a field rule and balanced": {"profile": "balanced", "fields": [FieldRule(name="phone")]},
    "strict data mode": {"profile": "secrets", "data": "strict"},
    "strict data mode and balanced": {"profile": "balanced", "data": "strict"},
}
LOGS = [
    "2026-10-26 12:00:01 INFO signup phone={number} plan=gold",
    "2026-10-26 12:00:01 INFO user=emanuele phone={number} plan=gold source=web",
    "2026-10-26 12:00:01 INFO signup plan=gold phone={number}",
]


@pytest.mark.parametrize("slot", [SlotKind.USER_TEXT, SlotKind.TOOL_RESULT], ids=["prose", "data"])
@pytest.mark.parametrize("mode", [m for m in MODES if "field" not in m and "strict" not in m])
@pytest.mark.parametrize("log", LOGS)
@pytest.mark.parametrize("number", INTERNATIONAL)
def test_a_phone_after_an_equal_sign_in_a_log_line_leaves_masked_whole(number, log, mode, slot):
    left, masked = _clear(log.format(number=number), MODES[mode], slot)
    assert left == [], masked


# Strict data mode reads the data of tool results and files, never what a person writes.
FIELD_MODES = [
    (mode, slot)
    for mode in MODES
    if "field" in mode or "strict" in mode
    for slot in (SlotKind.USER_TEXT, SlotKind.TOOL_RESULT)
    if not ("strict" in mode and slot == SlotKind.USER_TEXT)
]


@pytest.mark.parametrize(("mode", "slot"), FIELD_MODES, ids=[f"{m}-{s.value}" for m, s in FIELD_MODES])
@pytest.mark.parametrize("log", LOGS)
@pytest.mark.parametrize("number", INTERNATIONAL)
def test_a_phone_in_an_unquoted_value_of_a_field_rule_or_strict_data_mode_is_masked_whole(number, log, mode, slot):
    left, masked = _clear(log.format(number=number), MODES[mode], slot)
    assert left == [], masked


@pytest.mark.parametrize("slot", [SlotKind.USER_TEXT, SlotKind.TOOL_RESULT], ids=["prose", "data"])
def test_a_number_without_a_prefix_in_a_field_rules_unquoted_value_is_masked_whole_too(slot):
    left, masked = _clear("signup phone=333 123 4567 plan=gold", MODES["a field rule"], slot)
    assert left == [] and "333" not in masked and " plan=" in masked, masked


@pytest.mark.parametrize("config", ["a field rule", "strict data mode"])
def test_what_follows_a_phone_in_an_unquoted_value_is_still_read_as_its_own_pair(config):
    built = build(Config(ner="off", **MODES[config]), Vault(ROOT_KEY))
    masked = built.masker.mask("signup phone=+39 347 123 4567 plan=gold password=Winter2024x", SlotKind.TOOL_RESULT)
    assert "347" not in masked.text and "4567" not in masked.text
    assert "Winter2024x" not in masked.text  # the pair after it is found as ever
    assert masked.text.startswith("signup phone=") and " plan=" in masked.text and " password=" in masked.text


@pytest.mark.parametrize("config", ["a field rule", "strict data mode", "balanced"])
@pytest.mark.parametrize(
    "line",
    [
        'signup phone="+39 347 123 4567" plan=gold',
        '{"phone": "+39 347 123 4567", "plan": "gold"}',
        "signup phone=+39 plan=gold",
    ],
)
def test_the_forms_that_were_found_before_are_still_found(config, line):
    left, masked = _clear(line, MODES[config], SlotKind.TOOL_RESULT)
    assert left == [] and "+39 347" not in masked


def _numeric_log(lines):
    """Logfmt lines of numeric pairs of 8 to 15 digits, each followed by the next pair's key: no value can be taken in
    for a phone number, and each is a candidate for the shape of one."""
    return "\n".join(
        f"ts={1_761_480_000 + i} user_id={10_000_000 + i * 7} order={2_026_102_600 + i} bytes={48_151_623 + i} "
        f"latency_us={1_234_567 + i} trace={900_000_000_000 + i} size={12_345_678 + i} total={98_765_432_101 + i}"
        for i in range(lines)
    )


def test_a_value_that_ends_before_a_letter_costs_no_phone_parse(monkeypatch):
    parsed = []
    real = phonenumbers.parse
    monkeypatch.setattr(phonenumbers, "parse", lambda *args, **kwargs: parsed.append(args) or real(*args, **kwargs))
    found = fields.cells(_numeric_log(300))
    assert len(found) == 300 * 8 and parsed == []
    # one that goes past its value (a phone number's spaced groups) is parsed, and taken in whole
    text = "signup phone=+39 347 123 4567 plan=gold"
    [phone, plan] = fields.cells(text)
    assert text[phone.start : phone.end] == "+39 347 123 4567" and text[plan.start : plan.end] == "gold" and parsed


def test_a_value_that_goes_past_its_end_without_the_shape_of_a_phone_is_not_parsed(monkeypatch):
    parsed = []
    real = phonenumbers.parse
    monkeypatch.setattr(phonenumbers, "parse", lambda *args, **kwargs: parsed.append(args) or real(*args, **kwargs))
    assert pii.phone_end("a=48151623 b=1", 2, pii.DEFAULT_PHONE_REGIONS, limit=10) is None and parsed == []
    assert pii.phone_end("a=48151623 12345678 b=1", 2, pii.DEFAULT_PHONE_REGIONS, limit=10) is None  # parsed, not valid


@pytest.mark.timing
@pytest.mark.parametrize("config", ["a field rule", "strict data mode", "strict data mode and balanced"])
def test_a_numeric_log_is_masked_in_about_the_time_it_took_before_phones_were_taken_in(config, monkeypatch):
    """4,000 lines of 8 numeric pairs: 0.14 to 0.4 s without the extension, 4 s when every value was parsed, under 0.7 s
    with the check of the value's shape first."""
    built = build(Config(ner="off", **MODES[config]), Vault(ROOT_KEY))
    text = _numeric_log(1_500)

    def fastest():
        took = float("inf")
        for _ in range(3):  # the fastest of three: a shared runner may pause any one of them
            started = time.perf_counter()
            built.masker.detect(text, DetectContext(SlotKind.TOOL_RESULT))
            took = min(took, time.perf_counter() - started)
        return took

    with monkeypatch.context() as before:
        before.setattr(fields, "phone_end", lambda *args, **kwargs: None)  # a pair is taken as it was
        without = fastest()
    now = fastest()
    assert now < 3 * without + 0.15, f"{now:.2f} s against {without:.2f} s without the extension"


# One line of pairs as long as a text can be (``fields._MAX_TEXT``), of the shapes that once cost the square of its
# length: a value that a space, a parenthesis and a digit follow (where a phone number may go on, ``phone_end``), a run
# of pairs, a run of quoted pairs (each asked whether it stands in a tag), and phone numbers after keys. Each with the
# seconds that reading its 2 MB may take, its structures alone and with every detector: about a third of that on this
# computer (1.3 and 2.1 s; 5 and 11 s for the phone numbers, 87,000 of them, each parsed). Before, the first shape
# took 7.6 s at 200 KB and twelve minutes at 2 MB, the next two about two minutes.
LONG_LINE = fields._MAX_TEXT
LONG_SHAPES = {
    "may go on as a phone": ("a=1 (2 ", 5.0, 10.0),
    "a run of pairs": ("a=1 ", 5.0, 10.0),
    "a run of quoted pairs": ('a="1" ', 5.0, 10.0),
    "phone numbers": ("phone=+39 347 123 4567 ", 20.0, 40.0),
}
LONG = pytest.mark.parametrize(("unit", "cells_at_most", "detect_at_most"), LONG_SHAPES.values(), ids=list(LONG_SHAPES))


def _long_line(unit, size):
    """A line of at most ``size`` characters (a longer text is read for no structure at all)."""
    head = "password=hunter2x "
    return head + unit * ((size - len(head)) // len(unit))


@pytest.mark.timing
def test_where_a_phone_ends_costs_the_same_on_a_line_of_any_length():
    """``phone_end`` reads the few characters a phone number can take, not the line: RE2's wrapper encodes the whole
    text it is given at each call (0.35 ms a call on a line of 2 MB), which made a line of pairs cost the square of its
    length."""
    line = _long_line("a=1 (2 ", LONG_LINE)
    started = time.perf_counter()
    for at in range(20, 20 + 7 * 10_000, 7):  # each the value of a pair that a space, a parenthesis and a digit follow
        assert pii.phone_end(line, at, pii.DEFAULT_PHONE_REGIONS, limit=at + 1) is None
    took = time.perf_counter() - started
    assert took < 0.5, f"10,000 calls on a line of 2 MB: {took:.2f} s"


@pytest.mark.timing
@LONG
def test_a_line_of_pairs_is_read_in_a_time_that_grows_with_its_length(unit, cells_at_most, detect_at_most):
    """A tenth of the line first, so that a reading that grows with the square of the length fails in seconds."""
    for size in (LONG_LINE // 10, LONG_LINE):
        text = _long_line(unit, size)
        started = time.perf_counter()
        fields.cells(text)
        took = time.perf_counter() - started
        assert took < cells_at_most * size / LONG_LINE + 0.3, f"{len(text):,} characters: {took:.2f} s"


@pytest.mark.timing
@LONG
def test_a_scan_of_a_line_of_pairs_ends_in_seconds(unit, cells_at_most, detect_at_most):
    """What ``scan`` and ``mask`` run on a text: every detector, a secret's name in it so that the field detector reads
    its pairs."""
    built = build(Config(), Vault(ROOT_KEY))
    for size in (LONG_LINE // 10, LONG_LINE):
        text = _long_line(unit, size)
        started = time.perf_counter()
        built.masker.detect(text, DetectContext(SlotKind.TOOL_RESULT))
        took = time.perf_counter() - started
        assert took < detect_at_most * size / LONG_LINE + 0.5, f"{len(text):,} characters: {took:.2f} s"
