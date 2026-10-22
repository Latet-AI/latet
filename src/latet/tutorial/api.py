"""The tutorial's HTTP API, under /_latet/api/v1/tutorial/: in the sandbox's dashboard only (a `Dashboard` given a
`TutorialApi`), guarded like the rest of it. Errors are `{"error": ...}`; a call that does not fit where the chapter
is answers 409 with the state to go on from (`{"error", "state"}`). The page gives its language (`?lang=`, and `lang`
when it starts a chapter). The contract: docs/dashboard-api.md, Tutorial.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from latet.api.rest import API as DASHBOARD
from latet.api.rest import Handler
from latet.tutorial.agent import AGENT, UA
from latet.tutorial.chapters import ENGLISH
from latet.tutorial.model import NAME
from latet.tutorial.session import AGENT_ID, Conflict, TutorialSession

API = f"{DASHBOARD}/tutorial"
_MAX_BODY = 64_000  # a step's number and what the page saw: read no further


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _conflict(conflict: Conflict) -> JSONResponse:
    return JSONResponse({"error": conflict.error, "state": conflict.state}, status_code=409)


def _missing(cid: str) -> JSONResponse:
    return _error(f"there is no chapter {cid}", 404)


async def _body(request: Request) -> dict[str, Any] | None:
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > _MAX_BODY:
            return None
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _lang(request: Request) -> str:
    """The page's language, as it asks (`?lang=it`); English when it does not say."""
    return request.query_params.get("lang", ENGLISH)


class TutorialApi:
    def __init__(self, session: TutorialSession) -> None:
        self.session = session

    def routes(self, guarded: Callable[[Handler], Handler]) -> list[BaseRoute]:
        """The routes, each behind ``guarded``: the dashboard's sign-in and its checks against other sites."""
        return [
            Route(f"{API}/chapters", guarded(self.chapters), methods=["GET"]),
            Route(f"{API}/chapters/{{id}}", guarded(self.chapter), methods=["GET"]),
            Route(f"{API}/chapters/{{id}}/start", guarded(self.start), methods=["POST"]),
            Route(f"{API}/chapters/{{id}}/next", guarded(self.next), methods=["POST"]),
            Route(f"{API}/chapters/{{id}}/state", guarded(self.state), methods=["GET"]),
            Route(f"{API}/chapters/{{id}}/pictures/{{path:path}}", guarded(self.picture), methods=["GET"]),
            Route(f"{API}/end", guarded(self.end), methods=["POST"]),
        ]

    async def chapters(self, request: Request) -> Response:
        return JSONResponse(self.session.menu(_lang(request)))

    async def chapter(self, request: Request) -> Response:
        cid = request.path_params["id"]
        view = self.session.chapter_view(cid, _lang(request))
        return JSONResponse(view) if view is not None else _missing(cid)

    async def start(self, request: Request) -> Response:
        """``{lang?}``: the page's language, which the chapter keeps until the next start."""
        cid = request.path_params["id"]
        if cid not in self.session.chapters:
            return _missing(cid)
        lang = (await _body(request) or {}).get("lang", ENGLISH)
        if not isinstance(lang, str):
            return _error("lang: the code of the page's language (en, it...)", 400)
        try:
            return JSONResponse(await self.session.start(cid, lang))
        except Conflict as conflict:
            return _conflict(conflict)

    async def next(self, request: Request) -> Response:
        """``{step, seen?}``: run that step (the one the chapter is at). ``seen``: what the page saw (`pin`)."""
        cid = request.path_params["id"]
        if cid not in self.session.chapters:
            return _missing(cid)
        data = await _body(request) or {}
        step, seen = data.get("step"), data.get("seen", [])
        if not isinstance(step, int) or isinstance(step, bool):
            return _error("step: the index of the step to run", 400)
        if not isinstance(seen, list) or not all(isinstance(what, str) for what in seen):
            return _error('seen: what the page saw the user do (["pin"])', 400)
        try:
            return JSONResponse({"state": await self.session.next(request.app, cid, step, seen)})
        except Conflict as conflict:
            return _conflict(conflict)

    async def state(self, request: Request) -> Response:
        cid = request.path_params["id"]
        if cid not in self.session.chapters:
            return _missing(cid)
        try:
            return JSONResponse({"state": self.session.state(cid)})
        except Conflict as conflict:
            return _conflict(conflict)

    async def picture(self, request: Request) -> Response:
        """A picture of a chapter's project, as it is on this computer."""
        found = self.session.picture(request.path_params["id"], request.path_params["path"])
        if found is None:
            return _error("there is no such picture in this chapter", 404)
        data, media = found
        headers = {"x-content-type-options": "nosniff", "cache-control": "private, max-age=3600"}
        return Response(data, media_type=media, headers=headers)

    async def end(self, request: Request) -> Response:
        """The tutorial ends: what the agent waits on is let go, and the server stops (`latet tutorial` then
        deletes the sandbox, as on Ctrl+C)."""
        await self.session.end()
        server = getattr(request.app.state, "server", None)
        if server is not None:
            server.should_exit = True
        return JSONResponse({"ending": True})

    def integrations(self, port: int) -> dict[str, Any]:
        """`GET /integrations` in the sandbox: its own view. The agents of this computer are neither run (`<agent>
        --version`) nor read (their settings), and the sandbox has no destination."""
        return {
            "checks": [
                {
                    "status": "ok",
                    "title": "Daemon",
                    "detail": f"the tutorial's sandbox, on 127.0.0.1:{port}: nothing leaves this computer",
                }
            ],
            "agents": [
                {
                    "id": AGENT_ID,
                    "name": AGENT,
                    "installed": True,
                    "version": UA.split("/", 1)[1].split(" ", 1)[0],
                    "status": "applied",
                    "notes": ["The tutorial's pretend agent: it works in the sandbox only."],
                    "route": {"summary": f"Latet, then {NAME}, the pretend model", "problem": None, "notes": []},
                }
            ],
            "destinations": [],
        }
