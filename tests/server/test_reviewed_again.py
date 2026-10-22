"""The local reviewer in an identity's space on a Latet server: a text whose request was stopped before the reviewer read
it is read when the agent sends it again, though it comes from the space's cache, and what the reviewer answered is
kept (in PostgreSQL), so that, once the text left so, it is not asked about again."""

import pytest

from latet.config import LlmReview

from ..unit.test_reviewer import HEADERS, UPSTREAM, FakeServer, request, reviewer
from .support import listening

pytestmark = pytest.mark.server


async def test_a_text_stopped_before_the_reviewer_read_it_is_read_when_it_is_sent_again_on_a_server(server):
    emily, _ = server.identities.add_person("Emily Carter")
    async with listening(server):
        runtime = await server.spaces.runtime(emily)
        mediator, fake = runtime.mediator, FakeServer(status=500)  # its server answers nothing usable: stopped
        mediator.llm_review = LlmReview(enabled=True, model="fake-model")
        mediator.reviewer = reviewer(fake, mediator.llm_review, memory=runtime.store)
        records: list = []
        mediator.on_review = records.append  # the dashboard's record of each review

        async def send():
            raw = request()
            prepared = await mediator.run(mediator.prepare, UPSTREAM, "POST", "/anthropic/v1/messages", HEADERS, raw)
            if prepared.reviews and not prepared.block_reason:
                prepared = await mediator.review_prepared(prepared)
            return prepared

        stopped = await send()
        assert stopped.block_reason and "could not read" in stopped.block_reason
        fake.status, fake.texts, records[:] = 200, [], []
        again = await send()  # the agent sends it again: the text comes from the space's cache
        assert any("chemio" in text for text in fake.texts) and not again.block_reason and len(records) == 1
        assert "Maria" not in again.body.decode() and "chemio" not in again.body.decode()
        await mediator.run(mediator.record_left, again)  # it reached its provider (the forwarder records it so)
        fake.texts.clear()
        third = await send()
        assert fake.texts == [] and not third.block_reason  # answered once, kept: not asked again
        assert len(records) == 1  # nor recorded again
