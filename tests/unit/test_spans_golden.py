"""0.10.0's spans, frozen (the server performance plan's S9): the bench corpora and the fuzz texts, each text's spans as
the masker of 0.10.0 found them (``scripts/spans_golden.py make``, on 0.10.0 as released, into
``tests/data/spans-0.10.0.json.gz``). The engine made faster finds every one of them, and no other."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "data" / "spans-0.10.0.json.gz"
if str(ROOT / "scripts") not in sys.path:
    sys.path.append(str(ROOT / "scripts"))

import spans_golden  # noqa: E402


def test_the_engine_finds_the_spans_0_10_0_found():
    golden = spans_golden.load(GOLDEN)
    texts = spans_golden.corpora()
    assert spans_golden.digest(texts) == golden["sha256"], "the corpora are not the texts the file was made from"
    found = spans_golden.spans_of(texts)
    differences = spans_golden.differences(texts, golden["spans"], found)
    assert not differences, "\n".join(differences[:10])
