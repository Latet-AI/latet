"""Nothing a dashboard asks holds the event loop that every request of the process shares (the fix wave's S2, S3, S16
and F-S2): what waits on the runtime's lock or on a store, and what reads, parses, aggregates, decodes or renders much,
is done in a worker thread, the loop serving every other request meanwhile."""

import asyncio
import base64
import contextlib
import json
import threading
import time
import types
from typing import ClassVar

import httpx
import pytest
from starlette.responses import JSONResponse

from latet.api import rest
from latet.bridge import api as bridge_api
from latet.config import Config, IdentityConfig
from latet.engine.types import EntityClass
from latet.gateway.app import create_app
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY

API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}
# The longest the event loop may go without a turn while a dashboard's request waits on the runtime's lock or works
# through much: a few of its own steps, never the wait or the work.
TURN = 0.25


@pytest.fixture
def runtime(tmp_path):
    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)
    yield rt
    rt.close()


@contextlib.asynccontextmanager
async def _dashboard(rt):
    """A client of ``rt``'s app, on this test's event loop, signed in to its dashboard."""
    app = create_app(rt.gateway)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{rt.gateway.port}") as client:
            code = rt.gateway.dashboard.issue_code()
            assert (await client.get(f"/_latet/ui/login?code={code}", follow_redirects=False)).status_code == 303
            yield client


@contextlib.asynccontextmanager
async def _turns():
    """The longest the event loop went without a turn while it lasts (a list of one, filled at its end)."""
    longest, stop, started = [0.0], asyncio.Event(), asyncio.Event()

    async def tick():
        last = time.monotonic()
        started.set()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            longest[0], last = max(longest[0], now - last), now

    ticking = asyncio.create_task(tick())
    await started.wait()  # ticking before what it measures begins
    try:
        yield longest
    finally:
        stop.set()
        await ticking


def _held(lock, seconds):
    """``lock`` held ``seconds`` by another thread, as a save or a catch-up of the vault holds the runtime's: the
    thread, once it holds it."""
    held = threading.Event()

    def hold():
        with lock:
            held.set()
            time.sleep(seconds)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(5)
    return holder


async def test_forgetting_a_value_waits_for_the_runtimes_lock_off_the_event_loop(runtime):
    """S2 (the final review's area 3, I1): the dashboard's ``forget`` takes the runtime's lock, which a save holds for
    seconds, in a worker thread as its twins do: the loop that serves every request takes its turns meanwhile."""
    entity = runtime.built.vault.intern("EMAIL", EntityClass.PII, "emily.carter@acme.com")
    async with _dashboard(runtime) as client:
        holder = _held(runtime._lock, 1.0)
        try:
            async with _turns() as longest:
                answer = await client.delete(f"{API}/vault/{entity.id}", headers=UI)
        finally:
            holder.join()
    assert answer.json() == {"ok": True} and runtime.built.vault.get(entity.id) is None
    assert longest[0] < TURN, longest


class _Read(list):
    """Summaries that note the thread they are read in, and wait for ``gate`` (when given) before they are."""

    def __init__(self, items, threads, gate=None):
        super().__init__(items)
        self.threads, self.gate = threads, gate

    def __iter__(self):
        self.threads.append(threading.current_thread())
        if self.gate is not None:
            assert self.gate.wait(10)
        return super().__iter__()


def _summaries(count, agent="claude-code"):
    return [{"ts": time.time(), "new_masked": 1, "flags": [], "agent": agent} for _ in range(count)]


async def test_the_stats_are_aggregated_off_the_event_loop_one_call_at_a_time(runtime, monkeypatch):
    """S3 (area 3, I3): the stats are read, aggregated and rendered in a worker thread, and a dashboard's calls of them
    take their turn: one holds a thread at a time, the others wait on the loop, holding none."""
    threads, gate, reading = [], threading.Event(), []

    def summaries_parts(_since, _most):
        reading.append(threading.current_thread())
        yield _Read(_summaries(3), threads, gate if len(reading) == 1 else None)

    monkeypatch.setattr(runtime.exchanges, "summaries_parts", summaries_parts)
    async with _dashboard(runtime) as client:
        calls = [asyncio.create_task(client.get(f"{API}/stats")) for _ in range(3)]
        await asyncio.sleep(0.3)
        assert len(reading) == 1  # the others wait for its turn to end
        gate.set()
        answers = [(await call).json() for call in calls]
    assert [sum(day["exchanges"] for day in a["by_day"]) for a in answers] == [3, 3, 3]
    loop = threading.current_thread()
    assert len(threads) == 3 and all(thread is not loop for thread in threads)


async def test_a_history_read_a_part_at_a_time_gives_the_stats_its_latest_and_no_more(runtime, monkeypatch):
    """The history gives the stats its summaries a part at a time (``HistoryStore.summaries_parts``, a computer's and a
    server's), ``STATS_MOST`` at most, each part read and aggregated before the next, never the whole history at once."""
    asked = []

    def summaries_parts(since, most):
        asked.append(most)
        yield _summaries(2, "codex")
        yield _summaries(1, "codex")

    monkeypatch.setattr(runtime.exchanges, "summaries_parts", summaries_parts)
    monkeypatch.setattr(runtime.exchanges, "summaries_since", lambda _since: pytest.fail("read whole"))
    async with _dashboard(runtime) as client:
        answer = (await client.get(f"{API}/stats?days=90")).json()
    assert asked == [rest.STATS_MOST] and answer["by_agent"] == {"codex": 3}


async def test_an_image_of_the_live_reviewer_is_decoded_off_the_event_loop(runtime, monkeypatch):
    """S3 (area 3, M6): a batch's image, 16 MiB of base64 at most, is decoded in a worker thread."""
    picture, threads = b"\x89PNG\r\n" + bytes(range(256)) * 64, []

    async def image(batch_id, item, sent):
        return base64.b64encode(picture).decode(), "image/png"

    def decoded(data):
        threads.append(threading.current_thread())
        return base64.b64decode(data)

    monkeypatch.setattr(runtime.mediator.approvals, "image", image)
    monkeypatch.setattr(rest, "base64", types.SimpleNamespace(b64decode=decoded))
    async with _dashboard(runtime) as client:
        answer = await client.get(f"{API}/approvals/b_0123456789ab/images/0")
    assert answer.status_code == 200 and answer.content == picture
    assert threads and all(thread is not threading.current_thread() for thread in threads)


async def test_the_rules_page_reads_the_settings_made_and_rendered_off_the_event_loop(runtime, monkeypatch):
    """S3 (area 3, M6) and S16 (area 2, Q2): ``GET /config`` makes and renders the settings in use in a worker thread:
    on a server they hold every shared term and recognizer."""
    dashboard, threads = runtime.gateway.dashboard, []
    made = dashboard._config_view

    def view(config):  # the settings in use, read once (the view, its etag and the shared items from one reading)
        threads.append(threading.current_thread())
        return made(config)

    monkeypatch.setattr(dashboard, "_config_view", view)
    async with _dashboard(runtime) as client:
        answer = await client.get(f"{API}/config")
    assert answer.status_code == 200 and answer.json()["profile"] == runtime.config.profile.value
    assert threads and all(thread is not threading.current_thread() for thread in threads)


async def test_a_body_past_a_few_kib_is_parsed_in_a_worker_thread_and_one_nested_too_deep_is_none(runtime, monkeypatch):
    """F-S2: a dashboard's body, 8 MB at most (a rules file, the settings with every term), is parsed in a worker thread
    past ``PARSED_ON_LOOP`` bytes, for every handler that reads one (``Dashboard._json``), as the bridge and the
    administration parse theirs (A-04); a small one where it is read, which costs less than a thread's hop. A body
    nested deeper than Python reads is none, as one that is not JSON is: refused, not a 500."""
    parsed, parse = [], rest._parsed

    def noted(body):
        parsed.append((len(body), threading.current_thread()))
        return parse(body)

    monkeypatch.setattr(rest, "_parsed", noted)
    deep, as_json = b"[" * 100_000 + b"]" * 100_000, {**UI, "content-type": "application/json"}
    async with _dashboard(runtime) as client:
        small = await client.post(f"{API}/playground", json={"text": "hi"}, headers=UI)
        large = await client.post(f"{API}/playground", json={"text": "write to emily " * 1_000}, headers=UI)
        nested = await client.post(f"{API}/playground", content=deep, headers=as_json)
    assert (small.status_code, large.status_code, nested.status_code) == (200, 200, 400)
    loop = threading.current_thread()
    assert [thread is loop for size, thread in parsed] == [True, False, False]
    assert [size > rest.PARSED_ON_LOOP for size, _ in parsed] == [False, True, True]


class _Rendered(JSONResponse):
    """A JSON answer of the bridge's, noting what it rendered and in which thread."""

    seen: ClassVar[list[tuple[object, threading.Thread]]] = []

    def render(self, content):
        self.seen.append((content, threading.current_thread()))
        return super().render(content)


async def test_the_bridges_answers_are_rendered_in_its_thread(runtime, monkeypatch):
    """S3 (area 3, M6): the plugin's masked and restored texts, tens of MiB at most, are rendered in the bridge's
    thread, as their bodies were parsed there."""
    _Rendered.seen = []
    monkeypatch.setattr(bridge_api, "JSONResponse", _Rendered)
    token = {"x-latet-token": runtime.gateway.bridge_token}
    async with _dashboard(runtime) as client:
        items = [{"text": "write to emily.carter@acme.com", "kind": "user_text"}]
        masked = await client.post("/_latet/engine/v1/mask", json={"session": "s1", "items": items}, headers=token)
        [item] = masked.json()["items"]
        back = {"session": "s1", "items": [item["text"]]}
        restored = await client.post("/_latet/engine/v1/rehydrate", json=back, headers=token)
    assert restored.json()["items"] == ["write to emily.carter@acme.com"]
    answers = [(content, thread) for content, thread in _Rendered.seen if "items" in json.dumps(content)]
    assert len(answers) == 2 and all(thread.name.startswith("latet-bridge") for _, thread in answers)
