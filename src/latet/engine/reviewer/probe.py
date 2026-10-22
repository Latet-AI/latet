"""Trying the local reviewer: does its server answer, with which model, how fast, and what it finds."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from latet.config import LlmReview
from latet.engine.masker import Masker
from latet.engine.reviewer.client import ReviewClient, ReviewError
from latet.engine.reviewer.reviewer import Answer, Reviewer
from latet.engine.reviewer.view import View, view_of
from latet.engine.types import SlotKind

# Invented values the detectors leave in clear and a reviewer should find: a name, a relative, an
# illness, a street address and a password written in prose.
SAMPLE = (
    "Ciao, sono Paolo Neri: giovedì porto mia figlia Giulia dal cardiologo, in via Garibaldi 12 a Brescia. "
    "Per il NAS di casa la password è tramonto-77."
)


@dataclass
class Probe:
    ok: bool
    models: list[str] = field(default_factory=list)
    model: str | None = None
    slots: int | None = None
    n_ctx: int | None = None
    seconds: float | None = None
    answer: Answer | None = None
    error: str | None = None
    vision: bool | None = None  # its model sees images (None: the server does not say)


def sees_images(props: dict[str, Any] | None) -> bool | None:
    """Whether the server's model sees images, as llama.cpp says in ``/props`` (with ``--mmproj``);
    None when the server does not say (not llama.cpp, or an older one)."""
    modalities = (props or {}).get("modalities")
    if not isinstance(modalities, dict) or "vision" not in modalities:
        return None
    return bool(modalities["vision"])


def reviewed_view(masker: Masker, text: str, kind: SlotKind = SlotKind.USER_TEXT) -> View:
    """``text`` as the reviewer reads it in a request: masked by the detectors, shown as placeholders."""
    result = masker.mask(text, kind)
    covered = []
    shift = 0
    for r in sorted(result.replacements, key=lambda r: r.start):
        start = r.start + shift
        covered.append((start, start + len(r.replacement), r.etype))
        shift += len(r.replacement) - (r.end - r.start)
    return view_of(result.text, covered)


async def probe(config: LlmReview, view: View, original: str, client: ReviewClient) -> Probe:
    """Ask the server what it offers, then review ``view`` once (nothing is remembered)."""
    try:
        models = await client.models()
        props = await client.props() or {}
        if config.model and config.model not in models:
            return Probe(False, models, error=f"the server has no model {config.model}")
        model = config.model or (models[0] if models else None)
        if model is None:
            return Probe(False, models, error="the server lists no model")
        slots = props.get("total_slots")
        n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
        reviewer = Reviewer(config, os.urandom(32), client=client)
        started = time.perf_counter()
        answer = await reviewer.review(view, original, guidance=config.guidance)
        seconds = round(time.perf_counter() - started, 2)
        return Probe(
            answer.failed is None,
            models,
            model,
            slots if isinstance(slots, int) else None,
            n_ctx if isinstance(n_ctx, int) else None,
            seconds,
            answer,
            None if answer.failed is None else f"the review failed ({answer.failed})",
            sees_images(props),
        )
    except ReviewError as exc:
        return Probe(False, error=str(exc))
    finally:
        await client.aclose()
