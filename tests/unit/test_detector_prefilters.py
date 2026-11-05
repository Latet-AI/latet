"""The generic detectors made faster read the same texts to the same spans (the server performance plan, Task 2): one
RE2 Set over the personal-data patterns says which scans have anything to read, and gitleaks' rules are found by the
keywords present. Each is checked against the detector with its switch off (``prefilter=False``, ``by_keyword=False``:
0.10.0's way), on the bench corpora and the fuzz texts of ``scripts/spans_golden.py``."""

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import re2

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.append(str(ROOT / "scripts"))

import spans_golden  # noqa: E402

import latet.engine.detectors.gitleaks as gitleaks  # noqa: E402
import latet.engine.detectors.pii as pii  # noqa: E402
from latet.engine.detectors import ids  # noqa: E402
from latet.engine.detectors.base import DetectContext, quiet_options  # noqa: E402
from latet.engine.types import SlotKind  # noqa: E402

CODE = DetectContext(SlotKind.TOOL_RESULT)
PROSE = DetectContext(SlotKind.USER_TEXT)


@pytest.fixture(scope="module")
def corpora():
    return spans_golden.corpora()


# What the corpora hold none of, or little: a VAT number, Bitcoin addresses, a Spanish and a Dutch ID with their words.
EXTRA = (
    "VAT DE811907980 and IT00743110157, invoice 2026-10",
    "wallet 1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2 or bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
    "DNI 12345678Z, BSN 111222333, nothing else",
    "no personal data here, only words and a version 1.2.3",
    "",
)


@pytest.fixture(scope="module")
def texts(corpora):
    return [*(text for corpus in corpora for text in corpus.texts), *EXTRA]


def own_answer(detector: pii.PiiDetector, text: str) -> frozenset[str]:
    """The scans whose pattern matches somewhere in ``text``, each pattern asked on its own."""
    scans = {
        "email": [pii._EMAIL],
        "phone": [pii._PHONE_CANDIDATE],
        "iban": [pii._IBAN],
        "card": [pii._CARD],
        "ip": [pii._IPV4, pii._IPV6],
        "crypto": [pii._BTC_BASE58, pii._BTC_BECH32],
        "vat": [regex for _, regex in ids.vat._COMPILED],
    }
    for k, matcher in enumerate(detector._ids):
        if matcher.regex is not None:  # a matcher with a finder of its own has no pattern to ask
            scans[f"id{k}"] = [matcher.regex]
    return frozenset(name for name, regexes in scans.items() if any(regex.search(text) for regex in regexes))


def test_the_set_answers_as_each_pattern_does(texts):
    detector = pii.PiiDetector()
    answered = set()
    for text in texts:
        got = detector.matched(text)
        assert got == own_answer(detector, text), text[:100]
        answered |= got
    # the corpora hold something for the scans the Set decides, and texts where it says nothing at all
    assert {"email", "phone", "iban", "card", "ip", "crypto", "vat"} <= answered
    assert any(name.startswith("id") for name in answered)
    assert any(not detector.matched(text) for text in texts)


def test_the_set_leaves_out_the_scans_that_are_off_and_the_matchers_with_a_finder_of_their_own():
    only_email = pii.PiiDetector(enabled=["EMAIL"])
    assert only_email.matched("write to ada@lovelace.org or call +39 347 123 4567") == {"email"}
    # the Italian fiscal code and partita IVA are found by finders of their own, not by a pattern: they always run
    text = "CF: RSSMRA85T10A562S, partita IVA 00743110157"
    for prefilter in (True, False):
        detector = pii.PiiDetector(prefilter=prefilter)
        kinds = {s.etype for s in detector.detect(text, CODE)}
        assert {"IT_FISCAL_CODE", "IT_VAT_CODE"} <= kinds, prefilter
    names = {f"id{k}" for k, matcher in enumerate(pii.PiiDetector()._ids) if matcher.regex is None}
    assert names and not names & pii.PiiDetector().matched(text)


def test_a_pattern_compiled_with_options_other_than_the_defaults_stays_outside_the_set(monkeypatch):
    """The Set reads every pattern with RE2's defaults: a scan with a pattern compiled otherwise (here without regard
    to case) is not asked of it, and reads every text."""
    odd = quiet_options(case_sensitive=False)
    readings = []
    monkeypatch.setattr(ids.vat, "_COMPILED", [(ids.vat.VATS[0], re2.compile(r"zz\d+", odd))])
    monkeypatch.setattr(ids.vat, "find", lambda text, skip=frozenset(): readings.append(text) or iter(()))
    detector = pii.PiiDetector(enabled=["EMAIL", "VAT_NUMBER"])
    assert detector.matched("nothing to find in these words") == frozenset()  # the Set holds the email pattern alone
    assert list(detector.detect("nothing to find in these words", CODE)) == []
    assert readings == ["nothing to find in these words"]  # the VAT scan read it all the same
    # beside it, a scan whose patterns are RE2's own is skipped where its pattern matches nowhere
    monkeypatch.undo()
    skipped = []
    monkeypatch.setattr(ids.vat, "find", lambda text, skip=frozenset(): skipped.append(text) or iter(()))
    list(pii.PiiDetector(enabled=["EMAIL", "VAT_NUMBER"]).detect("nothing to find in these words", CODE))
    assert skipped == []


def test_the_personal_data_found_is_the_same_with_and_without_the_set(corpora):
    with_set, without = pii.PiiDetector(prefilter=True), pii.PiiDetector(prefilter=False)
    found = 0
    for name, texts in [*((corpus.name, corpus.texts) for corpus in corpora), ("extra", EXTRA)]:
        for ctx in (CODE, PROSE):
            for text in texts:
                spans = list(with_set.detect(text, ctx))
                assert spans == list(without.detect(text, ctx)), (name, text[:100])
                found += len(spans)
    assert found > 1000


def test_detectors_with_the_same_scans_share_one_set():
    """A process holds many maskers: the Set, whose DFA cache is what a detector costs, is made once for all of them."""
    assert pii.PiiDetector()._set is pii.PiiDetector()._set is not None
    assert pii.PiiDetector(enabled=["EMAIL"])._set is not pii.PiiDetector()._set


def test_threads_reading_through_the_shared_set_get_each_texts_own_answer(texts):
    detector = pii.PiiDetector()
    sample = texts[::7][:600]
    alone = [detector.matched(text) for text in sample]
    with ThreadPoolExecutor(8) as pool:
        together = list(pool.map(detector.matched, sample * 3))
    assert together == alone * 3


def test_the_scans_all_run_where_the_set_cannot_be_made(monkeypatch, texts):
    monkeypatch.setattr(pii, "_SET_MEMORY", 1 << 12)  # too little for RE2 to compile the Set
    detector = pii.PiiDetector()
    assert detector._set is None
    assert detector.matched("nothing here") == detector._names
    plain = pii.PiiDetector(prefilter=False)
    for text in texts[:200]:
        assert list(detector.detect(text, CODE)) == list(plain.detect(text, CODE))


# The test's own Stripe-shaped key, written in two pieces so that a host's push protection does not take it for one
STRIPE_KEY = "sk_" + "live_4eC39HqLyjWDarjtT1zdp7dcX9aB"
# three rules whose secrets this text holds, the stripe key first: the ruleset reads aws, then github, then stripe
THREE_RULES = f"STRIPE={STRIPE_KEY}\naws_key = AKIAJ3Q7T5W2XP4MZR6B\ntoken ghp_aB3dE5gH7jK9mN1pQ4sT6vW8yZ0cF2hJ4kLm\n"


def test_gitleaks_reads_the_rules_its_keywords_name_in_the_rulesets_order(texts):
    keyed, every = gitleaks.GitleaksDetector(by_keyword=True), gitleaks.GitleaksDetector(by_keyword=False)
    found_by = {}
    for text in [*texts, THREE_RULES]:
        got = list(keyed.detect(text, CODE))
        assert got == list(every.detect(text, CODE)), text[:100]
        found_by[text] = got
    three = found_by[THREE_RULES]
    order = [rule.id for rule in gitleaks.load_ruleset()[0]]
    rules = [s.rule.removeprefix("gitleaks:") for s in three]
    assert {"aws-access-token", "github-pat", "stripe-access-token"} <= set(rules)
    assert rules == sorted(rules, key=order.index)  # the ruleset's order, not the text's (the stripe key is first)
    assert min(s.start for s in three if s.rule.endswith("stripe-access-token")) < min(
        s.start for s in three if s.rule.endswith("github-pat")
    )
    assert sum(bool(spans) for spans in found_by.values()) > 10  # the corpora hold secrets for the rules to find


def test_gitleaks_reads_the_rules_by_the_path_of_the_text_as_before():
    path = DetectContext(SlotKind.TOOL_RESULT, path="node_modules/pkg/index.js")
    keyed, every = gitleaks.GitleaksDetector(by_keyword=True), gitleaks.GitleaksDetector(by_keyword=False)
    assert list(keyed.detect(THREE_RULES, path)) == list(every.detect(THREE_RULES, path))
    disabled = ("github-pat", "aws-access-token")
    keyed, every = (gitleaks.GitleaksDetector(disabled, by_keyword=k) for k in (True, False))
    got = list(keyed.detect(THREE_RULES, CODE))
    assert got == list(every.detect(THREE_RULES, CODE))
    assert not {"gitleaks:github-pat", "gitleaks:aws-access-token"} & {s.rule for s in got}


def test_a_rule_without_keywords_is_read_for_every_text(monkeypatch):
    rules, allowlists = gitleaks.load_ruleset()
    bare = gitleaks.Rule(
        id="bare-test-secret",
        regex=re2.compile(r"zzbare-[a-z0-9]{12}"),
        keywords=(),
        entropy=0.0,
        secret_group=0,
        path=None,
        allowlists=(),
    )
    monkeypatch.setattr(gitleaks, "load_ruleset", lambda: ((*rules, bare), allowlists))
    text = "plain words, then zzbare-q7w3e9r5t1y2 and nothing else"
    for by_keyword in (True, False):
        detector = gitleaks.GitleaksDetector(by_keyword=by_keyword)
        assert not detector._keywords.Match(text.lower())  # no keyword of any rule is in the text
        found = list(detector.detect(text, CODE))
        assert [text[s.start : s.end] for s in found] == ["zzbare-q7w3e9r5t1y2"], by_keyword
        assert found[0].rule == "gitleaks:bare-test-secret"
