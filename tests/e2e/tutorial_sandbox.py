"""The tutorial's sandbox in the tests: made as `latet tutorial` makes it (its own folder, the environment
switched to it, the tests' fake reader in place of your text recognition models), its app started, a dashboard
signed in through its API; and a chapter played the way a user plays it in the page: each `do` step's action done in
the real dashboard API, `next`, the state polled while the agent works. Everything in-process: nothing listens on a
port, nothing leaves the computer."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from starlette.applications import Starlette

from latet.gateway.app import create_app
from latet.tutorial import chapters
from latet.tutorial.chapters import Chapter, Step
from latet.tutorial.checks import Context, evaluate
from latet.tutorial.progress import Progress
from latet.tutorial.sandbox import Finder, Sandbox, environment

from ..fake_ocr import payment_reader

PORT = 6691  # names the host the gateway accepts; nothing listens on it
API = "/_latet/api/v1"
TUTORIAL = f"{API}/tutorial"
UI = {"x-latet-ui": "1"}
CATEGORY = "person"  # what an `approve` step's value is masked as by hand: a customer's name
PATIENCE = 20.0  # seconds a step may take, in-process, before a test gives up on it


@dataclass
class Opened:
    sandbox: Sandbox
    app: Starlette
    client: httpx.AsyncClient  # a dashboard, signed in
    progress: Path  # tutorial.json, in the user's own data folder (outside the sandbox)


def switch(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """The environment `latet tutorial` runs the sandbox in."""
    for name, value in environment(root, PORT).items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def written(folder: Path, monkeypatch: pytest.MonkeyPatch, *made: dict[str, Any]) -> None:
    """The tutorial's chapters are these: chapters made for a test, written as files like the package's."""
    folder.mkdir(parents=True, exist_ok=True)
    for chapter in made:
        text = yaml.safe_dump(chapter, sort_keys=False, allow_unicode=True)
        (folder / f"{chapter['id']}.yaml").write_bytes(text.encode())
    monkeypatch.setattr(chapters, "_folder", lambda: folder)


@contextlib.asynccontextmanager
async def opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requested: str | None = None, reader: Finder | None = None
) -> AsyncIterator[Opened]:
    """A sandbox in ``tmp_path``, its app started, a dashboard signed in; what the agent waits on let go at the end,
    and the runtime closed. ``reader``: where the sandbox finds its text recognition models, by default the tests'
    fake reader, which is there from the start."""
    root = tmp_path / "latet-tutorial-test"
    switch(monkeypatch, root)
    progress = tmp_path / "user" / "tutorial.json"
    found = payment_reader()  # the tests' fake reader: it reads the tutorial's screenshot as the models do
    sandbox = Sandbox.create(
        root, PORT, progress=Progress(progress), requested=requested, reader=reader or (lambda: found)
    )
    app = create_app(sandbox.runtime.gateway)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{PORT}") as client:
                await login(client, sandbox)
                try:
                    yield Opened(sandbox, app, client, progress)
                finally:
                    await sandbox.session.end()
    finally:
        sandbox.close()


async def login(client: httpx.AsyncClient, sandbox: Sandbox) -> httpx.Response:
    """As `latet tutorial` signs the browser in: a one-time code, then the sign-in page sets the cookie."""
    token = sandbox.runtime.gateway.bridge_token or ""
    code = (await client.post("/_latet/admin/login-code", headers={"x-latet-token": token})).json()["code"]
    signed = await client.get(f"/_latet/ui/login?code={code}", follow_redirects=False)
    assert signed.status_code == 303
    return signed


async def state(client: httpx.AsyncClient, cid: str) -> dict[str, Any]:
    answer = await client.get(f"{TUTORIAL}/chapters/{cid}/state")
    assert answer.status_code == 200, answer.text
    return answer.json()["state"]


async def act(client: httpx.AsyncClient, step: Step) -> list[str]:
    """The user's action a `do` step waits for, done before the agent tries again; what the page saw (`seen`)."""
    wait = step.wait
    if wait is None or wait.kind == "approve":  # an `approve` is done while the step's own request waits
        return []
    if wait.kind == "pin":
        return ["pin"]  # the page saw the value opened
    if wait.kind == "term":
        answer = await client.post(f"{API}/terms", json={"value": wait.target}, headers=UI)
    elif wait.kind == "allow":
        answer = await client.post(f"{API}/allow", json={"value": wait.target}, headers=UI)
    elif wait.kind == "change":  # the explanation's advice: undo it
        changes = (await client.get(f"{API}/changes")).json()["changes"]
        [change] = [c for c in changes if wait.target in c["keys"]]
        answer = await client.post(f"{API}/changes/{change['id']}", json={"action": "reject"}, headers=UI)
    elif wait.kind == "guidance":  # the guidance written in for the reader, saved as it is
        review = (await client.get(f"{API}/config")).json()["llm_review"]
        answer = await client.put(f"{API}/config", json={"llm_review": {**review, "guidance": wait.target}}, headers=UI)
    else:  # fields: "Choose the columns to keep in clear" saves them first, as the strict columns panel does
        fields = (await client.get(f"{API}/config")).json()["fields"]
        kept = [{"name": name, "mask": False} for name in wait.target]
        answer = await client.put(f"{API}/config", json={"fields": [*kept, *fields]}, headers=UI)
    assert answer.status_code == 200, answer.text
    return []


async def approve(client: httpx.AsyncClient, value: str) -> bool:
    """In the Live Reviewer: the batch holding ``value``, approved with it masked by hand. False: none waits yet."""
    for batch in (await client.get(f"{API}/approvals")).json()["batches"]:
        for index, item in enumerate(batch["items"]):
            at = item["original"].find(value)
            if at < 0:
                continue
            mask = {"item": index, "start": at, "end": at + len(value), "category": CATEGORY}
            body = {"action": "approve", "masks": [mask]}
            answer = await client.post(f"{API}/approvals/{batch['id']}", json=body, headers=UI)
            assert answer.status_code == 200, answer.text
            return True
    return False


async def held(client: httpx.AsyncClient, cid: str) -> dict[str, Any]:
    """Chapter ``cid`` started and its first step sent: the batch its request waits in, in the Live Reviewer, as the
    page shows it (the user decides next)."""
    started = await client.post(f"{TUTORIAL}/chapters/{cid}/start", headers=UI)
    assert started.status_code == 200, started.text
    sent = await client.post(f"{TUTORIAL}/chapters/{cid}/next", json={"step": 0}, headers=UI)
    assert sent.status_code == 200, sent.text
    deadline = time.monotonic() + PATIENCE
    while not (batches := (await client.get(f"{API}/approvals")).json()["batches"]):
        assert time.monotonic() < deadline, "nothing came to wait in the Live Reviewer"
        await asyncio.sleep(0.01)
    [batch] = batches
    return batch


async def settled(client: httpx.AsyncClient, cid: str) -> dict[str, Any]:
    """The state once the agent is done with the step at work."""
    deadline = time.monotonic() + PATIENCE
    now = await state(client, cid)
    while now["busy"]:
        assert time.monotonic() < deadline, "the step never ended"
        await asyncio.sleep(0.01)
        now = await state(client, cid)
    return now


async def run(opened: Opened, cid: str, index: int, step: Step, seen: list[str]) -> dict:
    """`next`, then the state polled until the agent is done (the page polls each second); a request the Live
    Reviewer holds is approved meanwhile, as the user does it."""
    client = opened.client
    answer = await client.post(f"{TUTORIAL}/chapters/{cid}/next", json={"step": index, "seen": seen}, headers=UI)
    assert answer.status_code == 200, answer.text
    now = answer.json()["state"]
    approving = step.wait is not None and step.wait.kind == "approve"
    deadline = time.monotonic() + PATIENCE
    while now["busy"]:
        assert time.monotonic() < deadline, f"step {index} never ended"
        if approving and await approve(client, step.wait.target):  # type: ignore[union-attr, arg-type]
            approving = False
        await asyncio.sleep(0.01)
        now = await state(client, cid)
    return now


def context(opened: Opened, chapter: Chapter, index: int) -> Context:
    """What step ``index``'s checks read, from what the sandbox kept of it."""
    sandbox, rt = opened.sandbox, opened.sandbox.runtime
    assert rt.exchanges is not None
    ran = sandbox.session.ran[index]
    exchanges = {turn: rt.exchanges.detail(xid) for turn, xid in sandbox.session.turns.items()}
    if ran.exchanges:
        exchanges["last"] = rt.exchanges.detail(ran.exchanges[-1])
    return Context(
        captured=sandbox.model.captured,
        turns=sandbox.model.served,
        received=ran.received,
        received_run=[r.text for r in sandbox.model.received],
        workspace=sandbox.agent.workspace,
        got=ran.run.got,
        error=ran.run.error,
        exchanges={k: v for k, v in exchanges.items() if v is not None},
        show=chapter.show_of(index),
        vault=rt.built.vault,
        masker=rt.built.masker,
        config=rt.config,
        config_file=rt.config_file,
    )


async def play(
    opened: Opened,
    cid: str,
    *,
    checked: bool = True,
    before: list[dict[str, Any]] | None = None,
    lang: str | None = None,
) -> dict[str, Any]:
    """Chapter ``cid`` from its start to its end, each step's checks holding and every scripted turn used. Its
    last state; ``before``: where to keep the state the page has before each step runs; ``lang``: the language the
    page starts it in (none said: English)."""
    client, sandbox = opened.client, opened.sandbox
    body = {"lang": lang} if lang is not None else None
    started = await client.post(f"{TUTORIAL}/chapters/{cid}/start", json=body, headers=UI)
    assert started.status_code == 200, started.text
    chapter = sandbox.session.chapter  # the run's: in the language it started in
    assert chapter is not None and chapter.id == cid
    now = started.json()["state"]
    for index, step in enumerate(chapter.steps):
        if before is not None:
            before.append(now)
        seen = await act(client, step)
        now = await run(opened, cid, index, step, seen)
        assert now["error"] is None, f"step {index + 1} ({step.title}): {now['error']}"
        assert now["steps"][index]["ran"] and now["next"] == index + 1
        assert sandbox.model.leftover() == [], f"step {index + 1} ({step.title}): turns not used"
        if not checked:
            continue
        ran = sandbox.session.ran[index]
        if step.agent.expect == "answer" and not any(check.kind == "agent_error" for check in step.checks):
            assert ran.run.error is None, f"step {index + 1} ({step.title}): {ran.run.error}"
        failed = evaluate(step.checks, context(opened, chapter, index))
        assert failed == [], f"step {index + 1} ({step.title}): {failed}"
    assert now["finished"] and now["end"] is not None
    return now
