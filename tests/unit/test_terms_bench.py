"""scripts/terms_bench.py: the terms and texts it measures with are the same at every run, and hold what it says."""

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "terms_bench", Path(__file__).resolve().parents[2] / "scripts" / "terms_bench.py"
)
assert _SPEC is not None and _SPEC.loader is not None
bench = importlib.util.module_from_spec(_SPEC)
sys.modules["terms_bench"] = bench  # its dataclass looks its module up there
_SPEC.loader.exec_module(bench)


def test_the_terms_are_distinct_and_the_same_at_every_run():
    terms = bench.make_terms(2000)
    assert len({t.value.casefold() for t in terms}) == 2000
    assert [t.value for t in terms] == [t.value for t in bench.make_terms(2000)]
    assert {t.etype for t in terms} == {"PERSON", "CLIENT", "EMAIL"}


def test_a_text_holds_the_terms_it_is_given_and_none_without():
    terms = bench.make_terms(500)
    hits = [t.value for t in terms[:5]]
    with_hits, without = bench.make_text(100_000, hits, seed=3), bench.make_text(100_000, [], seed=3)
    assert len(with_hits) == len(without) == 100_000
    assert sum(with_hits.count(h) for h in hits) >= 5
    assert not any(t.value in without for t in terms)
