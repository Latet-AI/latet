"""The tutorial's local reviewer: the Tutorial Reviewer, a pretend one, as the Tutorial Model is a pretend model.

It answers what Latet's reviewer asks an OpenAI-compatible server (`GET /v1/models`, `GET /props`, `POST
/v1/chat/completions`, its answer streamed as llama.cpp streams one), through a client that reaches it in-process: no
request leaves the computer. What it finds is written in the chapter (`reviewer`): in each text it is asked about, the
chapter's values that are in it, each with its category; a value found with guidance, only when its instructions hold
the user's guidance. A real model follows what the guidance says; this one only sees that there is some.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from latet.config import LlmReview
from latet.engine.reviewer.client import ReviewClient
from latet.engine.reviewer.prompt import GUIDANCE_INTRO
from latet.tutorial.chapters import Find

NAME: Final = "Tutorial Reviewer"
MODEL_ID: Final = "tutorial-reviewer"
URL: Final = "http://127.0.0.1:9/v1"  # its address in the settings: never dialled, its client reaches it in-process
# What the sandbox gives the local reviewer in a chapter with the Tutorial Reviewer, over the chapter's own settings.
SETTINGS: Final[dict[str, Any]] = {"enabled": True, "url": URL, "model": MODEL_ID, "slots": 1}
PIECE: Final = 16  # characters of its answer per streamed event


@dataclass(frozen=True, slots=True)
class Asked:
    """A text it was asked about: whether its instructions held guidance, and the values it reported."""

    text: str
    guided: bool
    found: tuple[str, ...]


class TutorialReviewer:
    def __init__(self) -> None:
        self.asked: list[Asked] = []
        self._finds: list[Find] = []
        self.app = Starlette(routes=[Route("/{path:path}", self._answer, methods=["GET", "POST"])])

    def expect(self, finds: Sequence[Find]) -> None:
        """What it finds from now on: a chapter's `reviewer` (none: nothing). What it was asked before is forgotten."""
        self._finds = list(finds)
        self.asked.clear()

    def client_for(self, config: LlmReview) -> ReviewClient:
        """The client Latet's local reviewer asks it with: in-process, whatever the settings' address."""
        return ReviewClient(config.url, config.model, transport=httpx.ASGITransport(app=self.app))

    def answer(self, system: str, text: str) -> list[Find]:
        """What it reports of ``text``: the chapter's values in it, in the order they come, each once; one found with
        guidance only when ``system`` holds the user's guidance."""
        guided = GUIDANCE_INTRO in system
        found = sorted(
            (f for f in self._finds if f.value in text and (guided or not f.guidance)),
            key=lambda f: text.index(f.value),
        )
        once = list({f.value: f for f in found}.values())
        self.asked.append(Asked(text, guided, tuple(f.value for f in once)))
        return once

    async def _answer(self, request: Request) -> Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/models":
            return JSONResponse({"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]})
        if request.method == "GET" and path == "/props":
            return JSONResponse({"total_slots": 1})
        if request.method != "POST" or path != "/v1/chat/completions":
            return JSONResponse(
                {"error": {"message": f"{NAME} answers a local reviewer's routes only"}}, status_code=404
            )
        try:
            body = await request.json()
            system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            return JSONResponse({"error": {"message": "not a review"}}, status_code=400)
        text = user if isinstance(user, str) else " ".join(str(p.get("text", "")) for p in user if isinstance(p, dict))
        found = self.answer(str(system), text)
        content = json.dumps({"findings": [{"text": f.value, "category": f.category} for f in found], "block": False})
        return StreamingResponse(_stream(content), media_type="text/event-stream")


async def _stream(content: str) -> AsyncIterator[bytes]:
    """``content`` as llama.cpp streams an answer: deltas of PIECE characters, then `[DONE]`."""
    for at in range(0, len(content), PIECE):
        chunk = {"choices": [{"index": 0, "delta": {"content": content[at : at + PIECE]}}]}
        yield f"data: {json.dumps(chunk)}\n\n".encode()
    yield b"data: [DONE]\n\n"
