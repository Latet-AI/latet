"""The first chapter, run end to end in the sandbox without the tutorial's session or API: the agent sends each step
through the real gateway (masking, restoring, recording) to the pretend model, in-process, and every step's checks
hold with no scripted turn left unused. The "do" step is the user's: here it is skipped."""

import asyncio

import pytest

from latet.gateway.app import create_app
from latet.tutorial.chapters import load, render
from latet.tutorial.checks import Context, evaluate
from latet.tutorial.progress import Progress
from latet.tutorial.sandbox import Sandbox

from .test_tutorial_roundtrip import FAKE, KEY
from .tutorial_sandbox import PORT, switch

pytestmark = pytest.mark.e2e

EMAIL = "james.wilson@acme.com"
PHONE = "+44 20 7946 0958"


async def _new_exchanges(sandbox: Sandbox, known: dict[str, dict]) -> list[dict]:
    """The exchanges of the agent's conversation not seen before, in the order they were sent: the store's, in which
    they were recorded (their start times can be equal on Windows)."""
    rt = sandbox.runtime
    assert rt.recorder is not None and rt.exchanges is not None
    assert await asyncio.to_thread(rt.recorder.drain, 10)
    seen = {detail["summary"]["id"] for detail in known.values()}
    ids = rt.exchanges.in_order(sandbox.agent.session_key)
    return [detail for xid in ids if xid not in seen and (detail := rt.exchanges.detail(xid)) is not None]


async def test_the_basics_hold_from_the_first_step_to_the_last(tmp_path, monkeypatch):
    chapter = load("basics")
    root = tmp_path / "sandbox"
    switch(monkeypatch, root)
    sandbox = Sandbox.create(root, PORT, progress=Progress(tmp_path / "tutorial.json"))
    model, agent, rt = sandbox.model, sandbox.agent, sandbox.runtime
    app = create_app(rt.gateway)
    exchanges: dict[str, dict] = {}  # turn id → the exchange it answered; "last": the step's last
    ran = []
    try:
        async with app.router.lifespan_context(app):
            await sandbox.reset(chapter)
            for index, step in enumerate(chapter.steps):
                if step.kind != "watch":
                    continue  # the user's turn: clicking a value changes nothing the checks read
                before = len(model.received)
                model.expect(step.model, chapter.received)
                run = await agent.run_step(app, step, model, index)
                assert run.error is None, step.title
                assert model.leftover() == [], step.title
                for sent, detail in zip(run.sent, await _new_exchanges(sandbox, exchanges), strict=True):
                    exchanges["last"] = detail
                    if sent.turn is not None:
                        exchanges[sent.turn] = detail
                ctx = Context(
                    captured=model.captured,
                    received=[r.text for r in model.received[before:]],
                    received_run=[r.text for r in model.received],
                    workspace=agent.workspace,
                    got=run.got,
                    error=run.error,
                    exchanges=exchanges,
                    show=chapter.show_of(index),
                    vault=rt.built.vault,
                    masker=rt.built.masker,
                )
                assert evaluate(step.checks, ctx) == [], step.title
                render(step.explain, model.captured)  # what the explanation names was received by then
                ran.append(run)
    finally:
        sandbox.close()

    assert [run.sent[-1].turn for run in ran] == ["write", "done", "email"]
    # The fakes are the sandbox's own, the same at every run: the chapter's texts and screenshots rely on them.
    assert model.captured == {"DEMO_API_KEY": FAKE[KEY], "EMAIL": FAKE[EMAIL], "PHONE": FAKE[PHONE]}
    # The agent shows the real values, restored; the fakes appear only on the right, in the dashboard.
    shown = [line.text or line.content or "" for run in ran for line in run.lines]
    assert any(KEY in text for text in shown) and not any(FAKE[KEY] in text for text in shown)
    assert shown[-1] == f"I can't send emails, but I noted {EMAIL} and {PHONE}."
