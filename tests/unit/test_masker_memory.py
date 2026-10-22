"""What a masker holds as it works, where a process holds many (an identity's space on a server builds one for each of
its projects): the vault's values and fakes matched by one pair of detectors whatever the number of maskers, and a
project's terms matched under an RE2 budget sized to them, a bound its space is weighed by, never at the cost of a term."""

import random
import string

import re2

from latet.config import Config, TermConfig
from latet.engine.build import build_masker
from latet.engine.detectors import terms as terms_module
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.terms import RE2_MEMORY, Term, TermsDetector
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Vault

CANARY = "emily.carter@acme.com"


def _terms(count: int, seed: int = 5) -> list[str]:
    rng = random.Random(seed)

    def word() -> str:
        return "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(5, 12))).capitalize()

    return list(dict.fromkeys(f"{word()} {word()} {word()} {word()}" for _ in range(count)))


def test_the_maskers_of_one_vault_match_its_values_with_one_pair_of_detectors():
    vault = Vault(b"k" * 32)
    everyone = build_masker(Config(), vault)[0]
    project = build_masker(Config(terms=[TermConfig(value="Project Falcon")]), vault)[0]
    vault.intern("EMAIL", EntityClass.PII, CANARY)
    for masker in (everyone, project):  # each finds the value the vault knows, and its fake
        assert masker.known_in(f"write to {CANARY}")
        fake = vault.by_original(CANARY)
        assert fake is not None and masker.surrogates_in(f"from {fake.surrogate}")
    assert everyone._refresh()[0] is project._refresh()[0]  # one pair for the vault, however many maskers
    assert everyone._refresh()[1] is project._refresh()[1]
    vault.intern("EMAIL", EntityClass.PII, "daniel.okafor@acme.com")  # the vault moves: made again, once, for both
    assert project.known_in("cc daniel.okafor@acme.com") and everyone._refresh()[0] is project._refresh()[0]
    masked = project.mask(f"write to {CANARY}", SlotKind.TOOL_RESULT).text
    assert CANARY not in masked


def test_a_bounded_terms_matcher_takes_a_budget_sized_to_its_terms_and_finds_every_one():
    few, many = _terms(50), _terms(2000)
    small = TermsDetector([Term(v, "ORG") for v in few], bounded=True)
    large = TermsDetector([Term(v, "ORG") for v in many], bounded=True)
    assert small.memory < large.memory <= RE2_MEMORY // 4  # sized to what it matches: far below RE2_MEMORY
    assert TermsDetector([Term(v, "ORG") for v in few]).memory == RE2_MEMORY  # unbounded: as before
    for detector, values in ((small, few), (large, many)):
        text = " and ".join(values)
        found = {text[s.start : s.end] for s in detector.detect(text, DetectContext())}
        assert found == set(values)


def test_a_term_list_too_large_for_its_sized_budget_is_compiled_with_more_never_left_out(monkeypatch):
    values = _terms(500)
    monkeypatch.setattr(terms_module, "BOUNDED_PER_BYTE", 1)  # a budget far too small for the pattern
    monkeypatch.setattr(terms_module, "BOUNDED_SMALLEST", 64 * 1024)
    attempts = []
    compile_pattern = re2._Regexp

    def counted(pattern, options):
        attempts.append(options.max_mem)
        return compile_pattern(pattern, options)

    monkeypatch.setattr(re2, "_Regexp", counted)
    detector = TermsDetector([Term(v, "ORG") for v in values], bounded=True)
    assert len(attempts) > 1 and attempts == sorted(attempts)  # refused, then given more, until it compiles
    assert detector.memory == attempts[-1]
    text = " ; ".join(values)
    assert {text[s.start : s.end] for s in detector.detect(text, DetectContext())} == set(values)
