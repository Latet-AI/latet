"""Three readings issues 17, 20 and 21 found where a value of a secret meets the text around it (reviewed and written
anew here: none of the proposed code taken as it was).

- A pair whose unquoted value ends where a serialized document escapes a quote (``token=VALUE\\"`` in a JSON string):
  the backslashes write that quote, not the value. Read as part of it, the vault learnt ``VALUE\\`` and its fake took
  the backslash's place, so the masked document no longer parsed.
- A name in a Python call's boolean argument (``f(access_token=fallback or "")``) is a reference, as a name without a
  digit before ``,`` or ``)`` already is; one with a digit, an unquoted literal, stays a secret, as there.
- A bearer token followed by dots, the way a text shows one cut short (``Bearer abc...``) or ends a sentence with it:
  the dots are no part of it. Read as part of it, ``abc.`` was a second secret with a fake of its own, and a fake of
  the token followed by ``...`` read as a new token, which the final check refused.
"""

from __future__ import annotations

import gc
import json
import time

import pytest

from latet.config import Config
from latet.engine.build import build
from latet.engine.detectors import fields
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.structural import AssignmentDetector, CredentialDetector
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Vault


def _secrets(vault: Vault) -> list[str]:
    return sorted(e.original for e in vault.entities() if e.eclass == EntityClass.SECRET)


# --- a pair before an escaped quote ---------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["Synthet1c-Real-Secret-939", "canary"])
@pytest.mark.parametrize(
    "inner",
    [
        'check("region", ["us; token={v}", "bad"])',  # the value ends at a quote the document escapes once
        'echo "token={v}\\" done"',  # at a quote the text escapes itself, escaped again by the document
        "print('token={v}')",  # at a quote no escape writes
    ],
)
def test_a_pair_before_an_escaped_quote_is_masked_and_its_document_still_parses(value, inner):
    built = build(Config(ner="off"), Vault(b"8" * 32))
    original = inner.format(v=value)
    masked = built.masker.mask(json.dumps({"output": original}), SlotKind.TOOL_RESULT).text
    assert value not in masked
    shown = json.loads(masked)["output"]  # still a JSON document
    head, tail = original.split(value)
    assert shown.startswith(head) and shown.endswith(tail) and shown != original
    assert _secrets(built.vault) == [value]
    assert not built.verifier.verify([(masked, SlotKind.TOOL_RESULT, False)], "s", rewritten=True).blocking


@pytest.mark.parametrize("slashes", [1, 2, 3])
@pytest.mark.parametrize("before", ["log ", "a\n"])  # in a line, or beginning one (the review of 125a2bcf, L5)
def test_the_value_before_escaped_quotes_is_found_in_every_reading(before, slashes):
    text = before + "token=Synthet1c-Real-Secret-939" + "\\" * slashes + '"' + ("\nb" if before == "a\n" else " end")
    spans = list(AssignmentDetector().detect(text, DetectContext()))
    assert [text[s.start : s.end] for s in spans] == ["Synthet1c-Real-Secret-939"]


@pytest.mark.parametrize("text", ['password="aB7"', 'password="Synthet1c-Real-Secret-939"', "token=Synthet1c\\x41b"])
def test_a_secret_not_before_a_quote_is_read_as_before(text):
    built = build(Config(ner="off"), Vault(b"9" * 32))
    assert built.verifier.verify([(text, SlotKind.TOOL_RESULT, False)], "s").blocking


# --- a name in a call's boolean argument ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        'context_length(model, access_token=fallback or "", base_url=url)',
        "context_length(model, access_token=fallback and refresh)",
        '    return client(api_key=configured or "")',
    ],
)
def test_a_name_in_a_call_s_boolean_argument_is_a_reference(text):
    assert not list(AssignmentDetector().detect(text, DetectContext()))


@pytest.mark.parametrize(
    "text",
    [
        'context_length(model, access_token=token2 or "")',  # a digit: an unquoted literal, as before `,` or `)`
        'context_length(model, access_token=Winter2024x or "")',
        'context_length(model, ACCESS_TOKEN=fallback or "")',  # an environment's name
        'context_length(model, access_token="fallback" or "")',  # quoted: a literal
        'context_length(model, access_token=Actual-Secret-123 or "")',
        "log access_token=fallback or retry",  # a log's line, no call
        "log(foo) access_token=fallback or retry",
        "log(foo); access_token=fallback or retry",
        "log(foo)\naccess_token=fallback or retry",
        '"context_length(model, access_token=fallback or retry)"',  # inside a text's quotes
        "context_length(model, note='x', access_token=fallback or retry)",
        # The review of 125a2bcf (L4): what only looks like a call keeps its value a secret.
        "exists(select 1 from users where password=letmein and name=bob)",  # SQL: words no comma follows
        "if(password=dragonfly and user=admin)",  # a keyword is no call
        "while(password=dragonfly and user=admin)",
        "f(password=Pa$$word or x)",  # `$` is no Python name's
        "f(\rpassword=sunshine or x)",  # a carriage return begins a line
        "f(\N{LINE SEPARATOR}password=sunshine or x)",
        "f(" + "a, " * 100 + "password=sunshine or x)",  # its line begins further back than a call's head is read
        # The re-review of 8ef280cf (2): a name in capitals is SQL's more often than a call's.
        "WHERE(password=sunshine and user=admin)",
        "SELECT(password=letmein or 1)",
    ],
)
def test_a_literal_or_a_name_outside_a_call_s_argument_is_still_a_secret(text):
    assert list(AssignmentDetector().detect(text, DetectContext()))


@pytest.mark.timing
@pytest.mark.parametrize("unit", ["token=abcdefg or ", "a=b, token=abcdefg or ", "f(token=abcdefg or "])
def test_a_line_of_pairs_in_conditions_is_read_in_a_time_that_grows_with_its_length(unit):
    """Each pair's call is read in at most ``_CALL_REACH`` characters, never from its line's beginning: a line of a
    tenth of a text's longest, then one as long, the whole in less than 30 times its tenth (the review of 125a2bcf, H1:
    0.08 s at 17 KB, 2.15 s at 136 KB, about 9 minutes at 2 MB, read from the beginning)."""
    detector = AssignmentDetector()
    longest = fields._MAX_TEXT

    def line(size: int) -> str:
        return ("f(" + unit * size)[:size]

    list(detector.detect(line(longest // 100), DetectContext()))
    took = []
    collecting = gc.isenabled()
    gc.disable()
    try:
        for size in (longest // 10, longest):
            text = line(size)
            started = time.perf_counter()
            list(detector.detect(text, DetectContext()))
            took.append(time.perf_counter() - started)
    finally:
        if collecting:
            gc.enable()
    tenth, whole = took
    assert whole < 30 * tenth, f"the whole line {whole:.2f} s, {whole / tenth:.0f} times its tenth ({tenth:.2f} s)"
    # 7.7 s here for the first unit (its 117,000 secrets, not the rule), 9 minutes read from the beginning: a shared
    # runner about seven times slower stays under (the re-review of 8ef280cf, 3).
    assert whole < 80, f"{longest:,} characters: {whole:.2f} s"


# --- a bearer token followed by dots --------------------------------------------------------------------------------

TOKEN = "Abcdefghij0123456789KLMNopq"


@pytest.mark.parametrize("ending", ["", ".", "...", "...."])
@pytest.mark.parametrize("before", ["Use Bearer ", "Authorization: Bearer ", "authorization: token "])
def test_the_dots_after_a_bearer_token_are_no_part_of_it(before, ending):
    text = before + TOKEN + ending
    spans = [s for s in CredentialDetector().detect(text, DetectContext()) if s.etype == "BEARER_TOKEN"]
    assert spans and {text[s.start : s.end] for s in spans} == {TOKEN}


def test_a_token_s_inner_dots_stay_in_it():
    text = "Use Bearer abcdefghij.klmnopqrst.uvwxyz0123..."
    spans = [s for s in CredentialDetector().detect(text, DetectContext()) if s.etype == "BEARER_TOKEN"]
    assert [text[s.start : s.end] for s in spans] == ["abcdefghij.klmnopqrst.uvwxyz0123"]


@pytest.mark.parametrize("token", ["abcdefghijklmn", "Abcd1234efgh5678"])
@pytest.mark.parametrize("ending", [".", "......"])
def test_a_token_both_patterns_find_is_cut_once_and_keeps_its_one_fake(token, ending):
    """A token of 12 to 19 characters in an Authorization header, followed by dots up to 20: the header's pattern cuts
    it, the Bearer one, which asks for 20, reads it whole; the longer reading won, a second secret with the dots (the
    re-review of 8ef280cf, 1). One decision for both: the header's."""
    text = "Authorization: Bearer " + token + ending
    spans = [s for s in CredentialDetector().detect(text, DetectContext()) if s.etype == "BEARER_TOKEN"]
    assert spans and {text[s.start : s.end] for s in spans} == {token}
    vault = Vault(b"6" * 32)
    built = build(Config(ner="off"), vault)
    first = built.masker.mask(text, SlotKind.TOOL_RESULT).text
    assert _secrets(vault) == [token]
    fake = first.split("Bearer ")[1][: len(token)]
    for later in ("Authorization: Bearer " + fake + "...", "Authorization: Bearer " + fake):
        assert not built.verifier.verify([(later, SlotKind.TOOL_RESULT, False)], "s", rewritten=True).blocking
    assert token not in built.masker.mask("Send it as Bearer " + token + "...", SlotKind.TOOL_RESULT).text


_NO_TOKEN_WITHOUT_DOTS = [
    "Use Bearer abcdefghijklmnopq...",  # 17 characters and the dots: a token of 20 only with them
    "Use Bearer token...............",
    "Authorization: Bearer user........",
    "Authorization: Bearer YOUR_API_KEY...",  # a placeholder
    "Authorization: Bearer your-token-here.....",
]


@pytest.mark.parametrize("text", _NO_TOKEN_WITHOUT_DOTS)
def test_what_is_no_token_without_its_dots_is_read_whole_as_before(text):
    """The review of 125a2bcf (L3): cut, these would teach the vault a word (`token`, `user`) or a placeholder as a
    secret, and every text of it would have that word masked."""
    spans = [s for s in CredentialDetector().detect(text, DetectContext()) if s.etype == "BEARER_TOKEN"]
    assert spans and all(text[s.start : s.end].endswith(".") for s in spans)


# `Bearer token` and its fifteen dots finds no fake as before (0.12.1 too): the request is refused, nothing leaves.
@pytest.mark.parametrize("text", [t for t in _NO_TOKEN_WITHOUT_DOTS if not t.startswith("Use Bearer token")])
def test_what_is_no_token_without_its_dots_teaches_the_vault_no_word(text):
    built = build(Config(ner="off"), Vault(b"4" * 32))
    built.masker.mask(text, SlotKind.TOOL_RESULT)
    assert all(value.endswith(".") for value in _secrets(built.vault))
    assert built.masker.mask("def refresh(token): return user.token", SlotKind.TOOL_RESULT).text == (
        "def refresh(token): return user.token"
    )


@pytest.mark.parametrize("ending", [".", "..."])
def test_a_known_token_followed_by_dots_keeps_its_one_fake(ending):
    vault = Vault(b"5" * 32)
    built = build(Config(ner="off"), vault)
    first = built.masker.mask("Authorization: Bearer " + TOKEN, SlotKind.TOOL_RESULT).text
    fake = first.rsplit(" ", 1)[1]
    later = built.masker.mask("Send it as Bearer " + TOKEN + ending, SlotKind.TOOL_RESULT).text
    assert later == "Send it as Bearer " + fake + ending
    assert _secrets(vault) == [TOKEN]


@pytest.mark.parametrize(("ending", "refused"), [("", False), (".", False), ("...", False), ("...abc", True),
                                                 (".x", True), ("x...", True)])  # fmt: skip
def test_a_fake_followed_by_dots_passes_the_final_check_and_nothing_more_does(ending, refused):
    vault = Vault(b"5" * 32)
    built = build(Config(ner="off"), vault)
    fake = built.masker.mask("Authorization: Bearer " + TOKEN, SlotKind.TOOL_RESULT).text.rsplit(" ", 1)[1]
    result = built.verifier.verify([("Use Bearer " + fake + ending, SlotKind.TOOL_RESULT, False)], "s", rewritten=True)
    assert bool(result.blocking) is refused
