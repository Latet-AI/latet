"""The Tutorial Reviewer: what Latet's local reviewer asks a server, answered as the chapter writes it, in-process; and
the runtime's local reviewer asking it."""

import json

from latet.config import Config, LlmReview
from latet.engine.reviewer import Reviewer, view_of
from latet.engine.reviewer.prompt import SCHEMA, system_for
from latet.runtime import make_runtime
from latet.tutorial.chapters import Find
from latet.tutorial.reviewer import MODEL_ID, SETTINGS, TutorialReviewer

TEXT = "Daniel Okafor, a patient on Ward 7B, has type 2 diabetes."
FINDS = [
    Find(value="Daniel Okafor", category="person"),
    Find(value="type 2 diabetes", category="health"),
    Find(value="Ward 7B", category="confidential", guidance=True),
    Find(value="Ward 3A", category="confidential", guidance=True),
]


def _reviewer() -> tuple[TutorialReviewer, LlmReview]:
    reviewer = TutorialReviewer()
    reviewer.expect(FINDS)
    return reviewer, LlmReview.model_validate(SETTINGS)


async def test_it_reports_the_values_in_the_text_in_order_and_the_guided_ones_only_with_guidance():
    reviewer, config = _reviewer()
    client = reviewer.client_for(config)
    plain = json.loads(await client.ask(system_for(None), TEXT, SCHEMA))
    guided = json.loads(await client.ask(system_for("Ward names are confidential."), TEXT, SCHEMA))
    await client.aclose()
    assert plain == {
        "findings": [
            {"text": "Daniel Okafor", "category": "person"},
            {"text": "type 2 diabetes", "category": "health"},
        ],
        "block": False,
    }
    assert [f["text"] for f in guided["findings"]] == ["Daniel Okafor", "Ward 7B", "type 2 diabetes"]
    assert [(a.guided, a.found) for a in reviewer.asked] == [
        (False, ("Daniel Okafor", "type 2 diabetes")),
        (True, ("Daniel Okafor", "Ward 7B", "type 2 diabetes")),
    ]


async def test_it_answers_what_latet_s_client_asks_first():
    reviewer, config = _reviewer()
    client = reviewer.client_for(config.model_copy(update={"model": None}))
    assert await client.models() == [MODEL_ID]
    assert await client.props() == {"total_slots": 1}
    await client.aclose()


async def test_latet_s_local_reviewer_masks_what_it_reports():
    reviewer, config = _reviewer()
    review = Reviewer(config, b"k" * 32, reviewer.client_for(config))
    answer = await review.review(view_of(TEXT, []), TEXT)
    await review.aclose()
    assert [(f.value, f.category) for f in answer.findings] == [
        ("Daniel Okafor", "person"),
        ("type 2 diabetes", "health"),
    ]


async def test_the_runtime_s_local_reviewer_asks_the_client_it_is_given(tmp_path):
    tutorial, review = _reviewer()
    base = Config.model_validate({"identity": {"enabled": False}, "ner": "off"})
    rt = make_runtime(base, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=b"\x01" * 32)
    try:
        rt.review_client = tutorial.client_for
        rt.apply_config(base.model_copy(update={"llm_review": review}))
        reviewer = rt.mediator.reviewer
        assert reviewer is not None
        answer = await reviewer.review(view_of(TEXT, []), TEXT)
        assert [f.value for f in answer.findings] == ["Daniel Okafor", "type 2 diabetes"]
        assert len(tutorial.asked) == 1
    finally:
        rt.close()
