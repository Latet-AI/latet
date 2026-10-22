"""False-positive budget: public standard-library code holds no secrets and almost no personal data.

Author emails in module headers are real personal data and are expected to be found; everything
else that fires here is a false positive. The budget only goes down.
"""

import collections

import pytest

from latet.config import Config, Profile
from latet.engine.build import detectors_for
from latet.engine.corpus import stdlib_files
from latet.engine.detectors.base import DetectContext
from latet.engine.types import SlotKind

BUDGET = {Profile.BALANCED: 6, Profile.MAX: 10, Profile.SECRETS: 3}


def _stdlib_files():
    return stdlib_files()


@pytest.mark.parametrize("profile", list(Profile))
def test_false_positives_on_stdlib_within_budget(profile):
    # Strict data mode aside (max turns it on): it masks every value of a table or JSON on purpose, and leaves
    # the output of the tools that read files alone.
    detectors = detectors_for(Config(profile=profile, data="normal"), identity=None)
    ctx = DetectContext(SlotKind.TOOL_RESULT)
    hits = collections.Counter()
    examples = []
    files = _stdlib_files()
    assert len(files) >= 25
    for path in files:
        text = path.read_text("utf-8", errors="replace")
        for det in detectors:
            for span in det.detect(text, ctx):
                if span.etype == "EMAIL":
                    continue
                hits[span.rule] += 1
                examples.append(f"{span.rule}: {text[span.start : span.end][:40]!r} in {path.name}")
    total = sum(hits.values())
    assert total <= BUDGET[profile], "\n".join(examples)


def test_no_national_id_or_vat_rule_fires_on_stdlib():
    from latet.engine.detectors import ids

    rules = {spec.rule_id for spec in ids.SPECS.values()}
    [pii] = [d for d in detectors_for(Config(profile=Profile.MAX), identity=None) if d.id == "pii"]
    for kind in (SlotKind.TOOL_RESULT, SlotKind.USER_TEXT):
        ctx = DetectContext(kind)
        for path in _stdlib_files():
            text = path.read_text("utf-8", errors="replace")
            hits = [s for s in pii.detect(text, ctx) if s.rule in rules or s.rule.startswith("pii:vat-")]
            assert not hits, [(s.rule, text[s.start : s.end], path.name) for s in hits]
