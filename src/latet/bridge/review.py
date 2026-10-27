"""The local reviewer on the texts of a call to a space's engine, inline, before they are handed back: the plugin's
(``Bridge.mask``) and an application's (``Apps``, on the applications API). What the reviewer finds is masked in the
text, which is withheld when it must not leave or when its review failed and its settings say to stop."""

from __future__ import annotations

from typing import Any

from latet.engine.masker import MaskReport
from latet.engine.pipeline import Session
from latet.engine.reviewer import Answer
from latet.gateway.mediate import Mediator, ReviewItem, still_in_clear
from latet.vision.masking import ImageWork

__all__ = ["MASKED", "inline_review"]

# The key of an item of ``out`` that holds its text's ``Masked`` (an application's): it gets the text masked again.
MASKED = "_masked"
# Who gives the review its own time (``budget_s``), when ``llm_review.budget_ms`` is not set: the applications API.
_WAITS = "an application's call waits for it, which llm_review.budget_ms changes"


async def inline_review(
    mediator: Mediator,
    session: Session,
    reviews: list[ReviewItem],
    out: list[dict[str, Any]],
    degraded: list[str],
    *,
    budget_s: float | None,
    background: bool,
) -> list[str]:
    """The local reviewer on a call's items, inline ones before they are handed back (on the event loop, the masking in
    the bridge's thread): each item of ``out`` (``{text, count}``, by its index) gets its text masked again with what
    the reviewer found, or ``blocked`` and a notice in its place. The degradations of the call, with the reviewer's.

    The review waits ``llm_review.budget_ms`` when it is set, else ``budget_s`` (None: as long as it takes), and a
    review that did not end in time says which of the two it was.
    ``background``: the plugin's call, which the reviewer may read again later and whose texts keep the fakes they hold:
    what it did not read in time goes to the background reviewer, and a review that failed goes to you when the Live
    Reviewer is on there. An application's call (False) has neither: its texts are masked as values, whole, and its
    review is over when it answers, a failed one following ``llm_review.on_failure``."""
    await mediator.close_retired()
    config = mediator.review_config(session)
    if config is None:
        return degraded
    answers = {i.index: i.answer for i in reviews if i.answer is not None}
    answers |= await mediator.review_inline(config, reviews, budget_s)
    for item in reviews:
        item.answer = answers.get(item.index, item.answer)
    # The time the review was given, as a review that did not end within it names it: the setting, or the call's own.
    limit = None if config.budget_ms or budget_s is None else f"the {budget_s:g} seconds {_WAITS}"
    if answers:
        degraded = await mediator.run_bridge(
            _apply_review, mediator, session, config, reviews, answers, out, degraded, background, limit
        )
    if background:
        later = [
            i
            for i in reviews
            if i.answer is None and (not i.inline or answers.get(i.index, Answer()).failed == "timeout")
        ]
        mediator.submit_background(session, config, later)
    return degraded


def _apply_review(
    mediator: Mediator,
    session: Session,
    config: Any,
    reviews: list[ReviewItem],
    answers: dict[int, Answer],
    out: list[dict[str, Any]],
    degraded: list[str],
    background: bool,
    limit: str | None,
) -> list[str]:
    """In the bridge's thread: what the reviewer answered, applied to the items of ``out`` (``limit``: the time the
    review was given, in words, when it was not ``llm_review.budget_ms``)."""
    masker = mediator._masker(session)
    report = MaskReport()
    live = background and mediator.live_config(session) is not None  # a failed review goes to you then
    for item in reviews:
        answer = answers.get(item.index)
        if answer is None:
            continue
        entry = out[item.index]
        found = mediator.kept(config, masker, answer)
        reason = None
        if found:
            done = mediator.remask(session, item, found, report, keep=background, as_text=not background)
            entry["text"], entry["count"] = done.text, len(done.replacements)
            if MASKED in entry:
                entry[MASKED] = done
            item.sent = done.text
            if isinstance(work := entry.get("_work"), ImageWork):
                work.masked("text", done.text, done.replacements)
            if still_in_clear(found, item.original, done.text, masker):  # a value masking could not place
                reason = "a value the local reviewer found would have left in clear"
        mediator.withhold_unseen(config, masker, item, answer)
        if answer.failed and not live:
            reason = reason or mediator.failure(config, [item.kind.value], answer.failed, limit)
        if answer.failed:
            report.degraded.append("llm-review")
        if answer.block and config.block:
            what = item.kind.value.replace("_", " ")
            reason = reason or f"the local reviewer stopped this {what}: it must not leave even masked"
        if reason:
            entry["blocked"] = reason
            entry["text"] = f"[withheld by Latet: {reason}]"
        mediator._record(session, item, answer, found, inline=True)
    return sorted({*degraded, *report.degraded})
