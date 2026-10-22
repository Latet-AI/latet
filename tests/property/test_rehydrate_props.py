import json

from hypothesis import given
from hypothesis import strategies as st

from latet.engine.detectors.pii import PiiDetector
from latet.engine.detectors.structural import AssignmentDetector
from latet.engine.masker import Masker
from latet.engine.rehydrate import Annotator, StreamRehydrator, restore_text, restores_for
from latet.engine.types import EntityClass
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY

filler = st.text(alphabet=st.characters(codec="utf-8", exclude_categories=["Cs"]), max_size=40)
token = st.text(alphabet="ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789", min_size=10, max_size=30)


def _entities(values):
    vault = Vault(ROOT_KEY)
    kinds = ["ASSIGNED_SECRET", "PASSWORD", "PERSON", "EMAIL"]
    out = []
    for i, v in enumerate(values):
        etype = kinds[i % len(kinds)]
        if etype == "EMAIL":
            v = f"{v[:8].lower()}@acme.it"
        if etype == "PERSON":
            v = f"{v[:5].title()} {v[5:10].title()}"
        eclass = EntityClass.SECRET if etype in ("ASSIGNED_SECRET", "PASSWORD") else EntityClass.PII
        out.append(vault.intern(etype, eclass, v))
    return out


@st.composite
def stream_case(draw):
    values = draw(st.lists(token, min_size=1, max_size=5, unique=True))
    entities = _entities(values)
    parts = []
    for _ in range(draw(st.integers(1, 8))):
        parts.append(draw(filler))
        parts.append(draw(st.sampled_from(entities)).surrogate)
    parts.append(draw(filler))
    text = "".join(parts)
    cuts = sorted(draw(st.lists(st.integers(0, len(text)), max_size=12)))
    return entities, text, cuts


def _chunks(text, cuts):
    out, prev = [], 0
    for c in [*cuts, len(text)]:
        out.append(text[prev:c])
        prev = c
    return out


@given(stream_case(), st.booleans())
def test_streaming_equals_static_for_any_split(case, json_mode):
    entities, text, cuts = case
    if json_mode:
        text = json.dumps(text)[1:-1]
    restores = restores_for(entities, json_mode=json_mode)
    static, _ = restore_text(text, restores)
    stream = StreamRehydrator(restores)
    streamed = "".join(stream.feed(c) for c in _chunks(text, cuts)) + stream.flush()
    assert streamed == static
    if json_mode:
        json.loads(f'"{streamed}"')  # restored JSON string content stays valid


@given(stream_case(), st.booleans())
def test_annotator_matches_static_restore_and_locates_every_surrogate(case, json_mode):
    entities, text, _ = case
    if json_mode:
        text = json.dumps(text)[1:-1]
    restores = restores_for(entities, json_mode=json_mode)
    static, ids = restore_text(text, restores)
    restored, found = Annotator(restores).annotate(text)
    assert restored == static
    assert [f.restore.entity_id for f in found] == ids
    for f in found:
        assert text[f.start : f.end] == f.restore.pattern
        assert restored[f.restored_start : f.restored_end] == f.restore.replacement


@st.composite
def env_file(draw):
    keys = draw(
        st.lists(
            st.sampled_from(["API_KEY", "DB_PASSWORD", "SECRET_TOKEN", "CLIENT_SECRET"]),
            min_size=1,
            max_size=4,
            unique=True,
        )
    )
    lines = [f"{k}={draw(token)}" for k in keys]
    if draw(st.booleans()):
        lines.append(f"OWNER={draw(token)[:6].lower()}.x@acme.it")
    lines.insert(draw(st.integers(0, len(lines))), "# comment " + draw(filler).replace("\n", " "))
    return "\n".join(lines) + "\n"


@given(env_file())
def test_mask_then_restore_round_trips_and_hides_values(text):
    vault = Vault(ROOT_KEY)
    masker = Masker(vault, [AssignmentDetector(), PiiDetector()])
    masked = masker.mask(text)
    for r in masked.replacements:
        assert r.original not in masked.text or r.original in r.replacement
    restored, _ = restore_text(masked.text, restores_for(vault.entities()))
    assert restored == text
    assert masker.mask(text).text == masked.text  # deterministic: byte-stable across turns
