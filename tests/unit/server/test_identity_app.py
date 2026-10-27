"""An identity's space on a server: its runtime takes nothing of the server's machine as the identity's, and its app
trusts the server and nothing else (the identity it names, its host name); what would reach the server's own files,
keys, programs or network is refused."""

import asyncio
import base64
import gzip
import io
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import zlib
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import brotli
import httpx
import pytest
import zstandard
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import InvalidStatus

from latet.config import Config, Destination, ProjectEntry, TermConfig
from latet.engine.detectors import recognizers as recognizers_module
from latet.engine.reviewer import Answer
from latet.engine.types import SlotKind
from latet.engine.vault import Vault
from latet.gateway import ws
from latet.gateway.app import GatewayConfig, OnServer, create_app
from latet.gateway.aws import profile_tag
from latet.gateway.routes import destination_upstream
from latet.projects import Projects
from latet.protocols.base import ImageRef
from latet.runtime import image_masking, make_runtime, personal_names
from latet.server import egress
from latet.server.queue import BatchRefused
from latet.server.shared import OWN_RULES
from latet.server.space_settings import SettingsStale, SettingsTooLarge
from latet.server.vault import VaultFull
from latet.state import LocalSettings, local_backends
from latet.storage.exchanges import ExchangeStore
from latet.transfer import export_rules
from latet.vision import models
from latet.vision import pool as pool_module
from latet.vision.masking import WITHHELD, ImageMasking
from latet.vision.ocr import (
    BATCH_COLUMNS,
    MAX_LINE_WIDTH,
    OcrBusy,
    OcrTooLong,
    shared_ocr,
)
from latet.vision.pool import (
    SPACE_MAX_PIXELS,
    SPACE_READERS,
    ServerReading,
    SpaceReads,
    shared_pool,
)

from ...conftest import ROOT_KEY
from ...fake_ocr import ModelSession, blank_image, lines_map, long_lines_map, nothing_read, stand_in_ocr
from .conftest import EMILY, SHARED, URL, MemorySettings, as_identity

API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}
GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
INSIDE = ipaddress.ip_address("10.0.3.9")
OUTSIDE = ipaddress.ip_address("1.2.3.4")  # an address on the internet (the documentation ranges are refused)
MINE = {"upstreams": [{"name": "llm", "url": "https://llm.acme.com"}, {"name": "gw", "url": "https://gw.acme.com"}]}
CHAT = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
BEARER = {"authorization": "Bearer sk-test"}
REDIRECTS = (301, 302, 303, 307, 308)
CANARY = "emily.carter@acme.com"
HOST = {"host": "latet.acme.lan"}  # a WebSocket's handshake through the test client names the server's host so


def _client(rt, identity_id=EMILY.id, base_url=URL):
    return TestClient(as_identity(create_app(rt.gateway), identity_id), base_url=base_url)


def _put(c, path, body, headers=UI):
    """A save of the dashboard's: the etag of the settings as the page read them (``GET /config``) sent with ``body``,
    as the dashboard sends it (on a server a save without one is refused, 409 ``settings_stale``)."""
    etag = c.get(f"{API}/config").json()["etag"]
    return c.put(f"{API}{path}", json={**body, "etag": etag}, headers=headers)


def inside(host, port):
    return [INSIDE]


def outside(host, port):
    return [OUTSIDE]


def _answered() -> httpx.Response:
    """A provider's answer, as a stream: the gateway relays it as it comes."""
    return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(b'{"choices":[]}'))


def _upstream(rt, answer=None):
    """The providers the space reaches, a stand-in that records every request it gets: never the network."""
    seen: list[httpx.Request] = []

    def model(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request) if answer is not None else _answered()

    rt.gateway.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(model))
    return seen


class _Fixed:
    """A server on 127.0.0.1 that answers every connection with ``answer``, keeping the head of what it was sent."""

    def __init__(self, answer: bytes = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n") -> None:
        self.answer, self.heads = answer, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                head = b""
                while b"\r\n\r\n" not in head and (chunk := conn.recv(4096)):
                    head += chunk
                self.heads.append(head.lower())
                conn.sendall(self.answer)

    def close(self) -> None:
        self.sock.close()


# --- the runtime: nothing of the server's machine ----------------------------------------------------------------------


def test_a_space_masks_no_identity_of_the_servers_machine_and_reads_no_project_file(space, tmp_path):
    rt, _ = space
    assert rt.identity == EMILY and not any(d.id == "identity" for d in rt.built.masker.detectors)
    (tmp_path / "repo" / "sub").mkdir(parents=True)
    (tmp_path / "repo" / ".git").mkdir()  # a repository: a computer would take its latet.yaml for the folder's
    (tmp_path / "repo" / "latet.yaml").write_text("terms: [{value: Orion}]\n", "utf-8")
    project = rt.projects.resolve(str(tmp_path / "repo" / "sub"))
    assert project.root == str(tmp_path / "repo" / "sub") and project.repo_file is None


def test_a_spaces_bridge_token_is_its_own_and_nobody_knows_it(space, tmp_path):
    rt, _ = space
    assert rt.gateway.bridge_token and not (tmp_path / "models" / "bridge.token").exists()
    assert re.fullmatch(r"latet_brg_[A-Za-z0-9_-]{43}", rt.gateway.bridge_token)  # masked by its shape if it leaks
    assert rt.gateway.dashboard is not None
    assert rt.gateway.pin is not None and rt.gateway.pin.keywords == {"holder": EMILY.id}  # her lookups counted for her


def test_a_space_looks_at_no_project_file_again_either(space, tmp_path, monkeypatch):
    rt, _ = space
    looked: list[Path] = []
    monkeypatch.setattr("latet.projects.find_project_file", lambda cwd: looked.append(cwd))
    monkeypatch.setattr("latet.projects._RECHECK", 0.0)  # a computer would look at once
    rt.projects.resolve(str(tmp_path))
    rt.projects.resolve(str(tmp_path))
    assert looked == []


def test_a_spaces_start_weighs_its_settings_against_the_ones_approved(tmp_path):
    """Emily's own part lowers protection against what she approved last (written while her space was closed, by a
    save never approved or by whoever writes the database): her space starts with what she approved, and the change
    waits for her, as a computer's start does with a config.yaml edited while the daemon was off."""
    settings = MemorySettings(SHARED)
    settings.approved = settings.load()
    settings.own = {"allow_values": ["emily.carter@acme.com"]}
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    rt = make_runtime(None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=EMILY)
    try:
        assert "emily.carter@acme.com" not in rt.config.allow_values
        [waiting] = rt.mediator.approvals.changes.values()
        assert "emily.carter@acme.com" in waiting.config.allow_values and waiting.source == "start"
    finally:
        rt.close()


# --- the shared text recognition ---------------------------------------------------------------------------------------


def test_one_text_recognition_serves_every_space(tmp_path):
    first = shared_ocr(tmp_path / "det", tmp_path / "rec")
    assert shared_ocr(tmp_path / "det", tmp_path / "rec") is first
    assert shared_ocr(tmp_path / "det-2", tmp_path / "rec") is not first


def test_a_read_waits_its_turn_for_a_bounded_time_and_reads_take_turns_in_order(tmp_path):
    """One identity's read holds the shared text recognition: another's waits its turn at most ``wait`` seconds, and is
    then not read (``OcrBusy``); reads that wait take their turns in the order they asked, so that no identity passes
    another's read."""
    go = threading.Event()
    ocr = stand_in_ocr(det=ModelSession(lines_map, go=go))
    first = threading.Thread(target=ocr.read, args=(blank_image(),))
    first.start()
    assert ocr._det.started.wait(5)
    started = time.monotonic()
    with pytest.raises(OcrBusy):
        ocr.read(blank_image(), wait=0.2)
    assert time.monotonic() - started < 2
    order: list[str] = []

    def read(name: str) -> None:
        ocr.read(blank_image(), wait=10)
        order.append(name)

    second = threading.Thread(target=read, args=("second",))
    second.start()
    time.sleep(0.2)  # the second asks before the third
    third = threading.Thread(target=read, args=("third",))
    third.start()
    time.sleep(0.2)
    go.set()
    for thread in (first, second, third):
        thread.join(10)
    assert order == ["second", "third"]


def test_a_read_that_asks_again_at_once_waits_behind_the_one_that_was_waiting():
    """The identity that held the shared text recognition asks for it again as soon as it gives it back (its next image):
    the read that was waiting has its turn first, so that one identity's images in a row never keep another's out."""
    turns, order = stand_in_ocr()._lock, []
    turns.acquire()

    def waiting() -> None:
        assert turns.acquire(5)
        order.append("waiting")
        turns.release()

    other = threading.Thread(target=waiting)
    other.start()
    time.sleep(0.2)  # it waits its turn
    turns.release()
    assert turns.acquire(5)  # at once, before the other thread has run
    order.append("again")
    turns.release()
    other.join(5)
    assert order == ["waiting", "again"]


def test_a_read_stops_once_it_has_run_its_budget_and_lets_the_next_one_read():
    ocr = stand_in_ocr(rec=ModelSession(nothing_read, seconds=0.05))
    started = time.monotonic()
    with pytest.raises(OcrTooLong):
        ocr.read(blank_image(height=2000), budget=0.1)  # 100 lines: 13 batches would read them all
    assert time.monotonic() - started < 2 and ocr._rec.runs < 6
    assert ocr.read(blank_image(height=40), wait=1, budget=5).text == ""  # the turn was given back


def _shapes(inputs, seen):
    """The recognizer's stand-in, keeping the shape of every batch it is given."""
    seen.append(inputs["x"].shape)
    return nothing_read(inputs)


def _ref(image) -> ImageRef:
    return ImageRef(
        {"data": base64.b64encode(_png(image)).decode()}, "data", "base64", "image/png", SlotKind.USER_TEXT, 0
    )


def test_a_line_too_long_for_its_height_is_not_read_and_its_image_withheld_at_once():
    """`probe_ocr_width.py`: an image within a server's ceiling but of an extreme shape (one line of text across 100,000
    pixels, 20 high) would have the recognizer read one tensor of hundreds of megabytes (25.7 GiB at 4,000,000 by 4),
    under the turn every space shares. It is not read: its image is withheld, and the turn is free again at once."""
    seen: list[tuple[int, ...]] = []
    shared = stand_in_ocr(rec=ModelSession(lambda inputs: _shapes(inputs, seen)))
    mine = ImageMasking(SpaceReads(shared, wait=0.2, budget=5), max_pixels=SPACE_MAX_PIXELS)
    started = time.monotonic()
    work = mine.work(_ref(blank_image(100_000, 20)))
    assert work.state == WITHHELD and "could not be read" in (work.reason or "") and seen == []
    assert time.monotonic() - started < 2 and shared._lock.acquire(0)  # nobody waits behind it
    shared._lock.release()


def test_a_batch_of_long_lines_holds_a_bounded_tensor_whatever_the_images_shape():
    seen: list[tuple[int, ...]] = []
    ocr = stand_in_ocr(det=ModelSession(long_lines_map), rec=ModelSession(lambda inputs: _shapes(inputs, seen)))
    ocr.read(
        blank_image(3_000, 1_200)
    )  # lines across 3,000 pixels (read whole), each read at about 10,000 wide: 8 together would be too wide
    assert seen and all(width <= MAX_LINE_WIDTH and batch * width <= BATCH_COLUMNS for batch, _, _, width in seen)
    assert max(width for *_, width in seen) * 8 > BATCH_COLUMNS  # the budget split the batches...
    assert len(seen) > 1 and max(batch for batch, *_ in seen) > 1  # ...which still hold several lines each


def test_an_image_whose_reading_runs_out_of_memory_is_withheld_never_an_error():
    def exhausted(inputs):
        raise MemoryError

    mine = ImageMasking(SpaceReads(stand_in_ocr(rec=ModelSession(exhausted)), wait=0.2, budget=5))
    work = mine.work(_ref(blank_image()))
    assert work.state == WITHHELD and "could not be read (MemoryError)" in (work.reason or "")


def test_an_image_that_runs_out_of_memory_as_it_is_decoded_is_withheld_never_an_error(monkeypatch):
    def exhausted(data, max_pixels, max_scans=None):
        raise MemoryError

    monkeypatch.setattr("latet.vision.masking.decode", exhausted)
    work = ImageMasking(SpaceReads(stand_in_ocr(), wait=0.2, budget=5)).work(_ref(blank_image()))
    assert work.state == WITHHELD and "could not be read (MemoryError)" in (work.reason or "")


def test_a_spaces_image_read_too_late_is_withheld_and_read_again_when_the_turn_comes():
    """A space's image whose read could not have the shared text recognition in time is withheld, never sent unread;
    sent again once the text recognition is free, it is read."""
    go = threading.Event()
    shared = stand_in_ocr(det=ModelSession(lines_map, go=go))
    holder = threading.Thread(target=shared.read, args=(blank_image(),))
    holder.start()
    assert shared._det.started.wait(5)
    mine = ImageMasking(SpaceReads(shared, wait=0.2, budget=5))
    data = base64.b64encode(_png(blank_image())).decode()
    work = mine.work(ImageRef({"data": data}, "data", "base64", "image/png", SlotKind.USER_TEXT, 0))
    assert work.state == WITHHELD and "could not be read" in (work.reason or "")
    go.set()
    holder.join(10)
    again = mine.work(ImageRef({"data": data}, "data", "base64", "image/png", SlotKind.USER_TEXT, 0))
    assert again.state != WITHHELD


def _png(image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def test_a_space_reads_through_the_shared_text_recognition_with_a_servers_ceilings(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "runtime_available", lambda: True)
    monkeypatch.setattr(models, "installed", lambda data: True)
    space, computer = image_masking(tmp_path, None, space=True), image_masking(tmp_path, None)
    assert space is not None and computer is not None
    assert isinstance(space.ocr, SpaceReads) and space.ocr.ocr is shared_pool(*models.folders(tmp_path), SPACE_READERS)
    assert space.max_pixels == SPACE_MAX_PIXELS and computer.max_pixels > SPACE_MAX_PIXELS
    assert computer.ocr is shared_ocr(*models.folders(tmp_path))
    small = ImageMasking(computer.ocr, max_pixels=100)
    data = base64.b64encode(_png(blank_image(20, 20))).decode()
    work = small.work(ImageRef({"data": data}, "data", "base64", "image/png", SlotKind.USER_TEXT, 0))
    assert work.state == WITHHELD and "megapixels" in (work.reason or "")


def test_a_space_whose_models_come_later_reads_through_its_servers_pool_and_bounds(tmp_path, monkeypatch):
    """T22.7: a space built before its instance's models were installed makes its images' reading when its settings
    are next applied, with its server's reading: the settings' pool, wait and budget, and no other pool beside it."""
    settings = MemorySettings(SHARED)
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    reading = ServerReading(readers=3, wait=50, budget=40)
    rt = make_runtime(
        None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=EMILY, reading=reading
    )
    try:
        assert rt.mediator.vision is None and rt.reading == reading  # no models yet
        monkeypatch.setattr(models, "runtime_available", lambda: True)
        monkeypatch.setattr(models, "installed", lambda data: True)
        pools = set(pool_module._POOLS)
        rt.apply_config(rt.config, save=False)
        vision = rt.mediator.vision
        assert vision is not None and vision.space and isinstance(vision.ocr, SpaceReads)
        assert vision.ocr.ocr is shared_pool(*models.folders(rt.data), 3) and (vision.ocr.wait, vision.ocr.budget) == (
            50,
            40,
        )
        assert set(pool_module._POOLS) - pools == {(*models.folders(rt.data), 3)}
    finally:
        rt.close()


# --- the bridge: the identity the server names ------------------------------------------------------------------------


def test_the_bridge_answers_the_identity_the_server_names_and_nobody_else(space):
    rt, _ = space
    with _client(rt) as c:
        assert c.get("/_latet/engine/v1/ping").json()["ok"] is True
    with _client(rt, "u_danielokaf") as c:
        assert c.get("/_latet/engine/v1/ping", headers={"x-latet-token": rt.gateway.bridge_token}).status_code == 401


def test_only_the_servers_host_name_is_answered(space):
    rt, _ = space
    for base in ("https://evil.example", "http://127.0.0.1:5283"):
        with _client(rt, base_url=base) as c:
            assert c.get("/_latet/engine/v1/ping").status_code == 403


def test_a_web_page_is_refused_on_a_proxy_route_and_nothing_shuts_the_space_down(space):
    rt, _ = space
    with _client(rt) as c:
        assert (
            c.post("/llm/v1/chat/completions", json={}, headers={"origin": "https://evil.example"}).status_code == 403
        )
        assert c.post("/_latet/admin/shutdown", headers={"x-latet-token": rt.gateway.bridge_token}).status_code == 404


def test_the_servers_aws_profiles_never_sign(space):
    rt, _ = space
    tag = profile_tag(rt.gateway.bridge_token or "", "default")
    with _client(rt) as c:
        assert c.post(f"/bedrock/us-east-1/profile/default/{tag}/model/m/invoke", json={}).status_code == 404


def test_no_claude_md_of_the_server_is_read(space, tmp_path):
    rt, _ = space
    (tmp_path / "CLAUDE.md").write_text(f"API_TOKEN={GH_PAT}\n", "utf-8")
    with _client(rt) as c:
        found = c.post("/_latet/engine/v1/instructions", json={"session": "s", "cwd": str(tmp_path)}).json()
    assert found == {"findings": []}


def _live(rt, c):
    """The Live Reviewer turned on in Emily's space."""
    assert _put(c, "/config", {"live_review": {"enabled": True}}).status_code == 200
    assert rt.config.live_review.enabled


def test_the_plugins_answer_is_given_again_when_the_queue_fails_to_take_it(space, monkeypatch):
    rt, _ = space
    approvals = rt.mediator.approvals
    give, failed = approvals.give, []

    def flaky(batch_id, answer):
        if not failed:
            failed.append(batch_id)
            raise ConnectionError("Valkey did not answer")
        give(batch_id, answer)

    monkeypatch.setattr(approvals, "give", flaky)
    items = [{"text": "write to emily.carter@acme.com", "kind": "user_text"}]
    with _client(rt) as c:
        _live(rt, c)
        pending = c.post("/_latet/engine/v1/mask", json={"session": "s", "items": items}).json()["pending"]
        assert c.post(f"{API}/approvals/{pending}", json={"action": "approve"}, headers=UI).status_code == 200
        answer = c.post("/_latet/engine/v1/approvals/wait", json={"id": pending})
    assert failed == [pending] and answer.status_code == 200
    assert "emily.carter@acme.com" not in answer.json()["items"][0]["text"]


def test_a_decision_that_cannot_be_heard_still_answers_the_plugin(space, monkeypatch):
    rt, _ = space

    async def deaf(batch):
        raise ConnectionError("Valkey did not answer")

    monkeypatch.setattr(rt.mediator.approvals, "decided", deaf)
    items = [{"text": "write to emily.carter@acme.com", "kind": "user_text"}]
    with _client(rt) as c:
        _live(rt, c)
        pending = c.post("/_latet/engine/v1/mask", json={"session": "s", "items": items}).json()["pending"]
        started = time.monotonic()
        answer = c.post("/_latet/engine/v1/approvals/wait", json={"id": pending})
    assert answer.status_code == 500 and answer.json() == {"error": "masking failed"}
    assert time.monotonic() - started < 10  # given at once, not after the plugin's poll ran out


# --- the dashboard: the identity the server signed in, at its origin --------------------------------------------------


def test_the_identity_the_server_names_is_signed_in_at_the_servers_origin(space):
    rt, _ = space
    with _client(rt) as c:
        me = c.get(f"{API}/me")
        assert me.status_code == 200 and me.json()["data_dir"] == ""  # nothing of the server's folders
        assert _put(c, "/config", {}, {**UI, "origin": URL}).status_code == 200
        assert _put(c, "/config", {}, {**UI, "origin": "http://127.0.0.1:5283"}).status_code == 403
    name = {"name": "emily"}  # one of her computer's two calls (E0c's Task 9: the CLI's others refuse on a server)
    with TestClient(create_app(rt.gateway), base_url=URL) as nobody:
        assert nobody.get(f"{API}/me").status_code == 401
        token = {"x-latet-token": rt.gateway.bridge_token}  # the space's own token opens nothing
        assert nobody.post("/_latet/admin/user-name", json=name, headers=token).status_code == 401
    with _client(rt) as c:  # her computer's calls, as the server hands them on, and never from a web page
        assert c.post("/_latet/admin/user-name", json=name).status_code == 200
        assert c.post("/_latet/admin/user-name", json=name, headers={"origin": URL}).status_code == 401


def test_a_save_that_drops_a_shared_item_is_refused_with_how_many(space):
    """A save that would remove an item set for everyone: refused (409), counted by list and never named, with a reason
    the dashboard words in her language (T11.5); nothing of it applies."""
    rt, settings = space
    with _client(rt) as c:
        terms = c.get(f"{API}/config").json()["terms"]
        mine = {"terms": [*terms, {"value": "Mercury Labs", "type": "CLIENT"}]}
        assert _put(c, "/config", mine).status_code == 200
        assert [t["value"] for t in settings.own["terms"]] == ["Mercury Labs"]
        dropped = _put(c, "/config", {"terms": mine["terms"][1:]})
    assert dropped.status_code == 409 and "1 term" in dropped.json()["error"] and "Falcon" not in dropped.text
    assert dropped.json()["reason"] == "settings_drops_shared" and dropped.json()["dropped"] == {"terms": 1}
    assert [t.value for t in rt.config.terms] == ["Project Falcon", "Mercury Labs"]


def test_a_save_that_drops_a_shared_destination_is_refused_with_how_many(space):
    """The shared settings' destinations stay in every edition: a save of hers without one is refused (409), counted and
    never named, and the destination still serves her space."""
    rt, settings = space
    with _client(rt) as c:
        upstreams = c.get(f"{API}/config").json()["upstreams"]
        assert [u["name"] for u in upstreams] == ["llm"]
        dropped = _put(c, "/config", {"upstreams": []})
    assert dropped.status_code == 409 and "1 destination" in dropped.json()["error"]
    assert "llm.acme.com" not in dropped.text
    assert dropped.json()["reason"] == "settings_drops_shared" and dropped.json()["dropped"] == {"upstreams": 1}
    assert [u.name for u in rt.config.upstreams] == ["llm"] and "upstreams" not in settings.own


def test_seventy_thousand_recognizers_saved_in_the_dashboard_are_refused_at_once_by_their_count(space, monkeypatch):
    """The save the review made: seventy thousand recognizers, which the dashboard compiled one by one to report those
    that are broken (a minute, holding her runtime's lock and the interpreter), then built her masker of. Refused at
    once by their count, before any is compiled; nothing of them applies or is kept. The refusal carries a reason the
    dashboard words in her language (RP6), as a save her settings' store refuses does."""
    rt, settings = space
    compiled = []
    compile_one = recognizers_module.compile_one

    def counted(*args, **kwargs):
        compiled.append(True)
        return compile_one(*args, **kwargs)

    monkeypatch.setattr(recognizers_module, "compile_one", counted)
    many = [{"name": f"r{i}", "pattern": f"ZQ{i:05d}-[0-9]{{6}}"} for i in range(70_000)]
    with _client(rt) as c:
        project = c.post(f"{API}/projects", json={"path": "/home/emily/billing-api"}, headers=UI).json()
        for path in ("/config", f"/projects/{project['id']}/personal"):  # her settings, and hers for a folder
            started = time.perf_counter()
            refused = _put(c, path, {"recognizers": many})
            took = time.perf_counter() - started
            assert refused.status_code == 409 and "70000 recognizers" in refused.json()["error"]
            assert refused.json()["reason"] == "settings_too_many_rules"
            assert took < 10.0, f"{took:.1f} s"
    assert compiled == [] and rt.config.recognizers == [] and rt.config.projects == [] and settings.own == {}


def test_an_import_past_the_rules_a_server_keeps_is_refused_with_its_reason(space):
    """An import that would carry her own field rules past what a server keeps for one identity: refused (409) with a
    reason the dashboard words in her language (RP6), nothing of it applied."""
    rt, _ = space
    mine = [{"name": f"column_{i}", "type": "NOTE"} for i in range(OWN_RULES["fields"] - 10)]
    more = "".join(f"  - {{name: imported_{i}, type: NOTE}}\n" for i in range(20))
    with _client(rt) as c:
        assert _put(c, "/config", {"fields": mine}).status_code == 200
        imported = c.post(f"{API}/rules/import", json={"file": f"fields:\n{more}", "apply": True}, headers=UI)
    assert imported.status_code == 409, imported.text
    assert imported.json()["reason"] == "settings_too_many_rules"
    assert len(rt.config.fields) == len(mine)


def test_a_waiting_change_holding_more_rules_than_a_server_keeps_is_refused_at_its_approval_and_none_of_it_runs(space):
    """A change that waits for her, holding more recognizers of her own than a server keeps (made before the caps, or
    written by hand): her approval applies it unprepared, and its save refuses it; what ran before runs again, and the
    answer says why (409), never a server's error."""
    rt, _ = space
    many = [{"name": f"r{i}", "pattern": f"ZQ{i:05d}-[0-9]{{6}}"} for i in range(OWN_RULES["recognizers"] + 1)]
    lowered = rt.config.model_copy(update={"allow_values": [*rt.config.allow_values, CANARY], "recognizers": many})
    change = rt.propose(lowered, "command")
    assert change is not None
    before = rt.config
    with _client(rt) as c:
        approved = c.post(f"{API}/changes/{change.id}", json={"action": "approve"}, headers=UI)
    assert approved.status_code == 409 and "501 recognizers" in approved.json()["error"]
    assert approved.json()["reason"] == "settings_too_many_rules"
    assert rt.config == before and CANARY not in rt.config.allow_values


REFUSED_SAVES = {
    "stale": (SettingsStale("the settings of u_emilycarte changed on another instance since they were read"), 409),
    "too-large": (SettingsTooLarge("the settings of u_emilycarte would weigh 5000000 bytes sealed"), 413),
    "unavailable": (
        OSError("the settings of u_emilycarte could not be read or kept in the database (PoolTimeout)"),
        503,
    ),
}


@pytest.mark.parametrize("why", list(REFUSED_SAVES))
def test_a_save_its_store_refuses_is_answered_as_the_administration_does_and_none_of_it_runs(space, monkeypatch, why):
    """A save in the dashboard that the store of her settings refuses: 409 when another instance wrote them since (read
    them again, then make the change), 413 when they weigh more than a server keeps, 503 when it does not answer, each
    with a reason the dashboard words in her language; and what ran before the save runs again, so that settings that
    were not kept never run (an address she let through, refused, stays masked). Her change of a waiting change and her
    import are answered alike."""
    rt, settings = space
    refusal, status = REFUSED_SAVES[why]
    let_through = rt.config.model_copy(update={"allow_values": [*rt.config.allow_values, CANARY]})
    change = rt.propose(let_through, "command")  # what lowers protection waits for her
    assert change is not None

    def refused(own):
        raise refusal

    monkeypatch.setattr(settings, "_keep_own", refused)
    before = rt.config
    with _client(rt) as c:
        answer = _put(c, "/config", {"allow_values": ["support@acme.com", CANARY]})
        imported = c.post(
            f"{API}/rules/import", json={"file": "terms:\n  - {value: Orion}\n", "apply": True}, headers=UI
        )
        approved = c.post(f"{API}/changes/{change.id}", json={"action": "approve"}, headers=UI)
    for refused_save in (answer, imported, approved):
        assert refused_save.status_code == status, refused_save.text
        assert refused_save.json()["reason"] == f"settings_{why.replace('-', '_')}"
        assert "u_emilycarte" not in refused_save.text and "PoolTimeout" not in refused_save.text
    assert rt.config == before and CANARY not in rt.config.allow_values and settings.approved == before
    session = rt.built.engine.session("cc:s1")
    assert CANARY not in rt.built.engine.mask_text(f"write to {CANARY}", SlotKind.TOOL_RESULT, session).text


def test_a_destination_of_ones_own_that_only_the_server_reaches_is_refused_and_a_shared_one_stays(space):
    rt, _ = space
    with _client(rt) as c:
        r = _put(c, "/config", {"upstreams": [{"name": "mine", "url": "https://127.0.0.1:8443"}]})
        assert r.status_code == 400 and "server itself" in str(r.json()["details"])
        named = _put(c, "/config", {"upstreams": [{"name": "me", "url": f"{URL}/v1"}]})
        assert named.status_code == 400 and "server itself" in str(named.json()["details"])  # its own name
        assert c.delete(f"{API}/destinations/llm", headers=UI).status_code == 409
    # Each refused with a reason the dashboard words in her language (T11.5), and its address, hers, said back.
    for refused, url in ((r, "https://127.0.0.1:8443"), (named, f"{URL}/v1")):
        [detail] = refused.json()["details"]
        assert detail["reason"] == "destination_refused" and detail["url"] == url and detail["loc"] == ["upstreams", 0]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [("PUT", "/reviewer/key", {"key": "sk-x"}), ("DELETE", "/reviewer/key", None),
     ("POST", "/reviewer/certificate", {"url": "https://gpu.acme.lan/v1"})],
)  # fmt: skip
def test_the_reviewers_key_and_certificate_are_the_servers(space, method, path, body):
    rt, _ = space
    with _client(rt) as c:
        r = c.request(method, f"{API}{path}", json=body, headers=UI)
    assert r.status_code == 409 and "Latet server" in r.json()["error"]


def test_where_the_reviewers_key_comes_from_is_the_servers_own_business(space, monkeypatch):
    rt, _ = space
    monkeypatch.setenv("LATET_LLM_API_KEY", "sk-x")
    with _client(rt) as c:
        found = c.get(f"{API}/reviewer/key").json()
    assert found == {"source": "server", "variable": None, "stored": None}


@contextmanager
def _space_over(tmp_path, shared):
    """A space as the ``space`` fixture makes it, over these shared settings; and the store of its settings."""
    settings = MemorySettings(shared)
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    rt = make_runtime(None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=EMILY)
    rt.gateway.on_server = OnServer(URL, EMILY.id)
    try:
        yield rt, settings
    finally:
        rt.close()


def _probed(monkeypatch) -> list:
    """The settings the reviewer's Try it probes with, each time it probes (nothing is reached)."""
    tried = []

    async def probe(settings, view, text, client=None):
        if client is not None:
            await client.aclose()
        tried.append(settings)
        empty = {"models": [], "model": None, "slots": None, "n_ctx": None, "vision": None, "seconds": 0}
        return SimpleNamespace(ok=False, error="no", answer=None, **empty)

    monkeypatch.setattr("latet.engine.reviewer.probe.probe", probe)
    return tried


def test_the_reviewers_try_it_keeps_the_shared_server(tmp_path, monkeypatch):
    reviewed = SHARED.model_copy(update={"llm_review": SHARED.llm_review.model_copy(update={"enabled": True})})
    tried = _probed(monkeypatch)
    given = {"url": "https://evil.example/v1", "remote": True, "api_key_env": "LATET_MASTER_KEY"}
    with _space_over(tmp_path, reviewed) as (rt, _), _client(rt) as c:
        c.post(f"{API}/reviewer/test", json={"llm_review": given}, headers=UI)
    assert (tried[0].url, tried[0].api_key_env) == (SHARED.llm_review.url, SHARED.llm_review.api_key_env)


def test_the_reviewers_try_it_reaches_nothing_where_the_administrator_set_no_reviewer(space, monkeypatch):
    """The shared reviewer is off, its server the default one on the server's own port: Try it would show an identity
    what answers there. Refused, as drafting a recognizer and the columns' opinions are."""
    rt, _ = space
    tried = _probed(monkeypatch)
    with _client(rt) as c:
        refused = c.post(f"{API}/reviewer/test", json={"llm_review": {"enabled": True}}, headers=UI)
    assert refused.status_code == 409 and "administrator" in refused.json()["error"] and tried == []


def _on_a_loop() -> bool:
    """Whether an event loop runs in this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.mark.parametrize("enabled", [True, False])
def test_the_reviewers_try_it_reads_the_shared_settings_off_the_event_loop(tmp_path, monkeypatch, enabled):
    """On a server the shared settings, once a shared change is published, are a round trip to PostgreSQL and the
    opening of up to 4 MiB before a space's thread has read them again: Try it reads them once, in a worker thread,
    the event loop every identity shares serving the others meanwhile (S16, the final review's area 2, M-7)."""
    reviewer = SHARED.llm_review.model_copy(update={"enabled": enabled})
    tried, reads = _probed(monkeypatch), []
    with _space_over(tmp_path, SHARED.model_copy(update={"llm_review": reviewer})) as (rt, settings), _client(rt) as c:
        shared = settings._shared

        def read():
            reads.append(_on_a_loop())
            return shared()

        monkeypatch.setattr(settings, "_shared", read)
        answer = c.post(f"{API}/reviewer/test", json={"llm_review": {"enabled": True}}, headers=UI)
    assert answer.status_code == (200 if enabled else 409) and reads == [False]
    assert [settings.url for settings in tried] == ([SHARED.llm_review.url] if enabled else [])


@pytest.mark.parametrize("named", [{"api_key_file": "/etc/latet/master.key"}, {"api_key_env": "LATET_MASTER_KEY"}])
def test_no_save_names_a_file_or_a_variable_of_the_servers_for_the_reviewers_key(space, named):
    rt, _ = space
    with _client(rt) as c:
        reviewer = c.get(f"{API}/config").json()["llm_review"]
        assert _put(c, "/config", {"llm_review": reviewer}).status_code == 200  # as it is
        refused = _put(c, "/config", {"llm_review": {**reviewer, **named}})
    [name] = named
    assert refused.status_code == 400 and [d["loc"] for d in refused.json()["details"]] == [["llm_review", name]]
    assert [d["reason"] for d in refused.json()["details"]] == ["reviewer_key_server"]  # worded by the dashboard
    assert "master" not in refused.text.lower()  # named by the setting, never by what it names
    assert rt.config.llm_review.api_key_file is None and rt.config.llm_review.api_key_env == "LATET_LLM_API_KEY"


def test_a_projects_files_are_neither_read_nor_written_on_a_server(space, tmp_path):
    rt, _ = space
    with _client(rt) as c:
        project = c.post(f"{API}/projects", json={"path": str(tmp_path)}, headers=UI).json()
        assert project["repo"]["writable"] is False
        assert c.post(f"{API}/projects/{project['id']}/suggest", headers=UI).status_code == 409
        repo = {"settings": {}, "tag": ""}
        assert c.put(f"{API}/projects/{project['id']}/repo", json=repo, headers=UI).status_code == 409


def test_a_rules_file_is_imported_into_no_project_on_a_server_and_the_global_rules_take_it(
    space, tmp_path, monkeypatch
):
    """Importing into a project would read the latet.yaml of a folder of the server: refused, and no folder looked at;
    the global import reads no file and stays."""
    rt, _ = space
    (tmp_path / "srv" / ".git").mkdir(parents=True)
    (tmp_path / "srv" / "latet.yaml").write_text("terms: [{value: Orion Payroll}]\n", "utf-8")
    looked: list[Path] = []
    monkeypatch.setattr("latet.projects.find_project_file", lambda cwd: looked.append(cwd))
    rules, _ = export_rules(Config(terms=[TermConfig(value="Mercury Labs", type="CLIENT")]))
    with _client(rt) as c:
        project = c.post(f"{API}/projects", json={"path": str(tmp_path / "srv")}, headers=UI).json()
        into = c.post(f"{API}/rules/import", json={"file": rules, "project": project["id"], "apply": True}, headers=UI)
        assert into.status_code == 409 and "Orion" not in into.text and looked == []
        globally = c.post(f"{API}/rules/import", json={"file": rules, "apply": True}, headers=UI)
    assert globally.status_code == 200 and globally.json()["applied"] is True
    assert "Mercury Labs" in [t.value for t in rt.config.terms]


def test_integrations_run_no_program_of_the_server(space, monkeypatch):
    rt, _ = space
    monkeypatch.setattr("latet.integrations.agents.ALL", {})
    with _client(rt) as c:
        view = c.get(f"{API}/integrations").json()
    assert view["agents"] == [] and view["checks"][0]["title"] == "Server"
    assert [(d["name"], d["shared"]) for d in view["destinations"]] == [("llm", True)]


def test_the_servers_os_user_name_is_never_interned_and_the_computers_is_when_the_space_wants_a_surrogate(
    space, monkeypatch
):
    """The server's own OS user name is nobody's: never read, never interned. The user name a computer sends is the one
    interned, and only when the space's settings want a surrogate (E0c's Task 9, T9.3)."""
    rt, _ = space
    read: list[bool] = []
    monkeypatch.setattr("latet.config.os_user_name", lambda: read.append(True) or "latet-admin")
    with _client(rt) as c:
        kept = c.post("/_latet/admin/user-name", json={"name": "emily"})
        assert kept.json() == {"surrogate": None} and rt.built.vault.by_original("emily") is None  # the space keeps it
        review = rt.config.auto_mode_review.model_copy(update={"user_name": "surrogate"})
        rt.update_config(lambda config: config.model_copy(update={"auto_mode_review": review}))
        fake = c.post("/_latet/admin/user-name", json={"name": "emily"}).json()["surrogate"]
        assert c.post("/_latet/admin/user-name").status_code == 400  # no name: nothing of the server's is taken
    assert rt.built.vault.by_original("emily").surrogate == fake
    assert rt.built.vault.by_original("latet-admin") is None and read == []


# --- a destination of one's own: where its name leads, at save and at connect -----------------------------------------


def test_a_destination_of_ones_own_is_judged_by_where_its_name_leads_at_save_and_at_connect(space, monkeypatch, caplog):
    rt, _ = space
    monkeypatch.setattr(egress, "resolve", inside)
    with _client(rt) as c:
        refused = _put(c, "/config", MINE)
        assert refused.status_code == 400 and "own network" in str(refused.json()["details"])
        monkeypatch.setattr(egress, "resolve", outside)
        assert _put(c, "/config", MINE).status_code == 200
        assert rt.gateway.personal_names == {"gw"}  # the shared `llm` is not the identity's own: never checked
        monkeypatch.setattr(egress, "resolve", inside)  # the name points into the server's network since it was saved
        with caplog.at_level(logging.INFO):
            stopped = c.post("/gw/v1/chat/completions", json=CHAT, headers=BEARER)
        assert stopped.status_code == 400 and "own network" in stopped.text and "Nothing was sent" in stopped.text
    assert "gw.acme.com" not in caplog.text and "10.0.3.9" not in caplog.text  # no address in a log line


def test_a_destination_is_the_identitys_own_unless_the_shared_settings_set_it_as_it_is(space, tmp_path):
    rt, settings = space
    moved = Config(upstreams=[{"name": "llm", "url": "https://other.acme.com"}, {"name": "gw", "url": "https://gw.co"}])
    assert personal_names(settings, rt.config) == set()  # the shared `llm`, as it is
    assert personal_names(settings, moved) == {"llm", "gw"}  # a shared name at another address is the identity's own
    assert personal_names(LocalSettings(tmp_path / "config.yaml", tmp_path), moved) == set()  # a computer has none


def test_a_destination_of_ones_own_is_judged_when_the_server_connects_whatever_names_are_known(space, monkeypatch):
    """The names of the identity's own destinations change with its settings while a request is routed: the route a
    request took says that its destination is the identity's own, and that is what has it judged."""
    rt, _ = space
    monkeypatch.setattr(egress, "resolve", outside)
    with _client(rt) as c:
        assert _put(c, "/config", MINE).status_code == 200
        assert [u.own for u in rt.gateway.destinations] == [False, True]
        rt.gateway.personal_names = set()  # as if the names were read before the settings that added `gw`
        monkeypatch.setattr(egress, "resolve", inside)
        stopped = c.post("/gw/v1/chat/completions", json=CHAT, headers=BEARER)
    assert stopped.status_code == 400 and "own network" in stopped.text


def test_a_destination_named_as_the_identitys_own_is_judged_where_its_route_does_not_say_so():
    """Routes made as a computer makes them, with the names of the identity's own destinations given beside them: those
    are judged when the server connects all the same."""
    asked: list[str] = []

    async def pin(url):
        asked.append(url)
        raise egress.EgressRefused("gw.acme.com: refused")

    gw = destination_upstream(Destination(name="gw", url="https://gw.acme.com"))
    seen: list[httpx.Request] = []
    model = httpx.MockTransport(lambda request: seen.append(request) or _answered())
    config = GatewayConfig(
        upstreams=[],
        destinations=[gw],
        personal_names={"gw"},
        pin=pin,
        client=lambda: httpx.AsyncClient(transport=model),
    )
    with TestClient(create_app(config), base_url="http://127.0.0.1:5283") as c:
        stopped = c.post("/gw/v1/chat/completions", json=CHAT, headers=BEARER)
    assert stopped.status_code == 400 and asked == ["https://gw.acme.com/v1/chat/completions"] and seen == []


async def test_what_the_local_reviewer_found_that_a_full_vault_cannot_mask_stops_the_request(space, monkeypatch):
    rt, _ = space
    mediator = rt.mediator

    async def answered(config, items):
        return {0: Answer()}

    def full(*args):
        raise VaultFull("this identity's vault holds 200000 values, the most this server allows (vault_limit)")

    monkeypatch.setattr(mediator, "review_config", lambda session: rt.config.llm_review)
    monkeypatch.setattr(mediator, "review_inline", answered)
    monkeypatch.setattr(mediator, "_apply_review", full)
    upstream = destination_upstream(Destination(name="llm", url="https://llm.acme.com"))
    prepared = mediator.prepare(upstream, "POST", "/llm/v1/chat/completions", BEARER, json.dumps(CHAT).encode())
    prepared.reviews = [SimpleNamespace(index=0, answer=None)]
    reviewed = await mediator.review_prepared(prepared)
    assert "vault_limit" in (reviewed.block_reason or "")


def test_a_destination_of_ones_own_at_the_servers_own_name_is_refused_when_the_server_connects(space):
    """Kept by whoever writes the identity's settings, past the dashboard's check: the server refuses to reach itself."""
    rt, settings = space
    settings.own = {"upstreams": [{"name": "me", "url": f"{URL}/v1"}]}
    rt.apply_config(settings.load(), save=False)
    assert "me" in rt.gateway.personal_names
    seen = _upstream(rt)
    with _client(rt) as c:
        stopped = c.post("/me/chat/completions", json=CHAT, headers=BEARER)
        with pytest.raises(WebSocketDisconnect) as closed, c.websocket_connect("/me/responses", headers=HOST):
            pass
    assert stopped.status_code == 400 and "server itself" in stopped.text and "Nothing was sent" in stopped.text
    assert closed.value.code == 4400 and "nothing was sent" in closed.value.reason and seen == []


def test_a_websocket_to_a_destination_of_ones_own_is_judged_where_its_name_leads_when_the_server_connects(
    space, monkeypatch
):
    rt, _ = space
    monkeypatch.setattr(egress, "resolve", outside)
    with _client(rt) as c:
        assert _put(c, "/config", MINE).status_code == 200
        monkeypatch.setattr(egress, "resolve", inside)
        with pytest.raises(WebSocketDisconnect) as closed, c.websocket_connect("/gw/v1/responses", headers=HOST):
            pass
    assert closed.value.code == 4400 and "nothing was sent" in closed.value.reason


def test_a_request_to_a_destination_of_ones_own_takes_no_proxy_and_follows_no_redirect(space, monkeypatch):
    """The address checked answers that the request is to be made elsewhere, inside the server's network: the agent is
    answered with a clear error naming no address, and the server goes nowhere else; nor through a proxy its
    environment names."""
    rt, _ = space
    elsewhere = _Fixed()
    moved = _Fixed(
        f"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{elsewhere.port}/v1/chat/completions\r\n"
        "Content-Length: 0\r\n\r\n".encode()
    )

    async def pin(url):  # the address checked: the server that answers 302
        return f"http://127.0.0.1:{moved.port}/v1/chat/completions", "gw.acme.com", {}

    monkeypatch.setattr(egress, "resolve", outside)
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{elsewhere.port}")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    rt.gateway.pin = pin
    try:
        with _client(rt) as c:
            assert _put(c, "/config", MINE).status_code == 200
            answer = c.post("/gw/v1/chat/completions", json=CHAT, headers=BEARER, follow_redirects=False)
    finally:
        moved.close()
        elsewhere.close()
    assert answer.status_code == 502 and "location" not in answer.headers and str(elsewhere.port) not in answer.text
    assert "answered with a redirect (302)" in answer.json()["error"]["message"]
    assert len(moved.heads) == 1 and b"\r\nhost: gw.acme.com\r\n" in moved.heads[0] and elsewhere.heads == []


@pytest.mark.parametrize("where", ["https://llm.acme.com/v1/elsewhere", "/v1/elsewhere", "http://10.0.3.9/admin"])
@pytest.mark.parametrize("status", REDIRECTS)
def test_a_shared_destinations_redirect_reaches_the_agent_as_a_clear_error_on_a_server(space, status, where):
    """An agent that followed it would send its request, unmasked, to the address it names, past the server: on the
    same host, or inside the server's network. The agent is answered 502, handed no address; the server went nowhere
    else."""
    rt, _ = space
    moved = {"location": where, "content-type": "text/html"}
    seen = _upstream(rt, lambda request: httpx.Response(status, headers=moved, stream=httpx.ByteStream(b"Moved")))
    with _client(rt) as c:
        answer = c.post("/llm/v1/chat/completions", json=CHAT, headers=BEARER, follow_redirects=False)
    assert answer.status_code == 502 and "location" not in answer.headers and where not in answer.text
    assert f"answered with a redirect ({status})" in answer.json()["error"]["message"] and "Moved" not in answer.text
    assert [str(request.url) for request in seen] == ["https://llm.acme.com/v1/chat/completions"]


@pytest.mark.parametrize("where", ["the same host", "another origin"])
async def test_a_websocket_to_a_destination_of_ones_own_takes_no_proxy_and_follows_no_redirect(monkeypatch, where):
    """Reached at the address checked, through no proxy of the server's environment, its redirect is followed nowhere,
    to another origin (inside the server's network) or to the same one (a path the identity chooses could make an open
    redirect of it send the server on), as a shared destination's is."""
    elsewhere = _Fixed()
    location = "/v2/responses" if where == "the same host" else f"ws://127.0.0.1:{elsewhere.port}/v1/responses"
    moved = _Fixed(f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode())
    monkeypatch.setenv("WS_PROXY", f"http://127.0.0.1:{elsewhere.port}")  # a proxy the server's environment names
    try:
        with pytest.raises(InvalidStatus) as refused:  # raised as the handshake's refusal: the relay answers it 502
            await ws._upstream("ws://gw.acme.com/v1/responses", [], ("127.0.0.1", moved.port))
    finally:
        moved.close()
        elsewhere.close()
    assert refused.value.response.status_code == 302 and len(moved.heads) == 1 and elsewhere.heads == []
    assert b"\r\nhost: gw.acme.com\r\n" in moved.heads[0] and b" /v1/responses " in moved.heads[0]


@pytest.mark.parametrize("where", ["the same host", "another origin"])
@pytest.mark.parametrize("status", REDIRECTS)
def test_a_websocket_to_a_shared_destination_is_answered_a_clear_error_for_a_redirect_on_a_server(
    tmp_path, status, where
):
    """A destination the administrator set may lead inside, and a path or a query the identity chooses could make an
    open redirect of it send the server on: its redirect is followed nowhere, to another origin or to the same one, as
    the HTTP requests follow none. The client is answered 502, handed no address to go to by itself."""
    moved, elsewhere = _Fixed(), _Fixed()
    location = "/v2/responses" if where == "the same host" else f"ws://127.0.0.1:{elsewhere.port}/v1/responses"
    moved.answer = f"HTTP/1.1 {status} Moved\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode()
    relay = Destination(name="relay", url=f"http://127.0.0.1:{moved.port}")
    settings = MemorySettings(SHARED.model_copy(update={"upstreams": [relay]}))
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    rt = make_runtime(None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=EMILY)
    rt.gateway.on_server = OnServer(URL, EMILY.id)
    try:
        denied = pytest.raises(WebSocketDenialResponse)
        with _client(rt) as c, denied as refused, c.websocket_connect("/relay/v1/responses", headers=HOST):
            pass
    finally:
        rt.close()
        moved.close()
        elsewhere.close()
    answer = refused.value
    assert answer.status_code == 502 and "location" not in answer.headers and location not in answer.text
    assert f"answered with a redirect ({status})" in answer.json()["error"]["message"]
    assert rt.gateway.personal_names == set() and len(moved.heads) == 1 and b"/v1/responses" in moved.heads[0]
    assert elsewhere.heads == []


def test_a_name_that_does_not_resolve_in_time_holds_a_save_no_longer_than_the_bound(space, monkeypatch):
    """The save resolves the names of the identity's own destinations in the egress threads, its lookups counted for
    the identity, and waits for them a bounded time: a name that has not answered by then is judged when the server
    connects."""
    rt, _ = space
    release, threads = threading.Event(), []

    def stuck(host, port):
        threads.append(threading.current_thread().name)
        release.wait(10)
        return [INSIDE]

    monkeypatch.setattr(egress, "resolve", stuck)
    monkeypatch.setattr(egress, "RESOLVE_SECONDS", 0.3)
    try:
        with _client(rt) as c:
            started = time.monotonic()
            saved = _put(c, "/config", MINE)
            assert time.monotonic() - started < 3 and saved.status_code == 200
            assert threads and all(name.startswith("latet-egress") for name in threads)
            stopped = c.post("/gw/v1/chat/completions", json=CHAT, headers=BEARER)
            assert stopped.status_code == 400 and "did not resolve in time" in stopped.text
    finally:
        release.set()


def test_a_vault_at_its_limit_stops_the_request_with_a_clear_error(space, monkeypatch, caplog):
    rt, _ = space

    def full(*args, **kwargs):
        raise VaultFull("this identity's vault holds 200000 values, the most this server allows (vault_limit)")

    monkeypatch.setattr(rt.built.vault, "intern", full)
    seen = _upstream(rt)
    with _client(rt) as c, caplog.at_level(logging.INFO):
        body = {"model": "m", "messages": [{"role": "user", "content": "write to emily.carter@acme.com"}]}
        stopped = c.post("/llm/v1/chat/completions", json=body, headers=BEARER)
        assert stopped.status_code == 400 and "vault_limit" in stopped.text and "Nothing was sent" in stopped.text
        items = [{"text": "write to emily.carter@acme.com", "kind": "user_text"}]
        held = c.post("/_latet/engine/v1/mask", json={"session": "s", "items": items})
        assert held.status_code == 500 and "vault_limit" in held.json()["error"]
    frame = json.dumps({"type": "response.create", "model": "m", "input": "write to emily.carter@acme.com"})
    sent, turn = rt.mediator.mask_ws_frame(frame, rt.built.engine.session("ws:1"))  # a turn over a WebSocket alike
    assert sent == frame and turn is not None and "vault_limit" in (turn.block_reason or "") and seen == []
    assert "emily.carter" not in caplog.text


def test_a_compressed_request_that_would_decode_past_the_limit_is_stopped_and_nothing_is_sent(space, caplog):
    """On a server a request's body is bounded once decoded too (the pre-read's C1): a few KiB of gzip, brotli, zstd or
    deflate that would inflate past ``max_request_mb`` stop the request (413), its rest never decoded."""
    rt, _ = space
    rt.mediator.body_limit = 1024 * 1024  # what Spaces gives a space: the server's max_request_mb
    seen = _upstream(rt)
    big = json.dumps({"model": "m", "messages": [{"role": "user", "content": "x" * (8 * 1024 * 1024)}]}).encode()
    packed = {
        "gzip": gzip.compress(big),
        "br": brotli.compress(big),
        "zstd": zstandard.ZstdCompressor().compress(big),
        "deflate": zlib.compress(big),
    }
    with _client(rt) as c, caplog.at_level(logging.INFO):
        for coding, body in packed.items():
            headers = {**BEARER, "content-encoding": coding, "content-type": "application/json"}
            stopped = c.post("/llm/v1/chat/completions", content=body, headers=headers)
            assert stopped.status_code == 413, coding
            assert "max_request_mb" in stopped.text and "Nothing was sent" in stopped.text
        fine = c.post(
            "/llm/v1/chat/completions",
            content=gzip.compress(json.dumps(CHAT).encode()),
            headers={**BEARER, "content-encoding": "gzip", "content-type": "application/json"},
        )
        assert fine.status_code == 200
    assert len(seen) == 1  # the one under the limit, and none of the others
    assert "DecodedTooLarge" in caplog.text


def test_a_request_the_live_reviewers_queue_does_not_keep_is_stopped_with_its_reason(space, monkeypatch):
    rt, _ = space
    seen = _upstream(rt)

    def heavy(batch):
        raise BatchRefused(
            "the Live Reviewer of a server keeps 4194304 bytes of a request's new texts at most: send less"
        )

    monkeypatch.setattr(rt.mediator.approvals.backend, "put", heavy)
    with _client(rt) as c:
        _live(rt, c)
        stopped = c.post("/llm/v1/chat/completions", json=CHAT, headers=BEARER)
    assert stopped.status_code == 400 and "send less" in stopped.text and "Nothing was sent" in stopped.text
    assert seen == []


def test_what_a_request_carried_is_recorded_as_left_once_it_has_left_and_never_before(space):
    """A text sent again in its conversation leaves as it left only when that very text is known to have left there:
    the gateway records what a request it rewrote carried once its provider has answered, and nothing of one that never
    reached it."""
    rt, _ = space
    answers = iter([httpx.ConnectError("refused"), None])

    def maybe(request):
        failed = next(answers)
        if failed is not None:
            raise failed
        return _answered()

    seen = _upstream(rt, maybe)
    body = {"model": "m", "messages": [{"role": "user", "content": "the status page is up"}]}
    headers = {**BEARER, "session-id": "s-1"}
    with _client(rt) as c:
        assert c.post("/llm/v1/chat/completions", json=body, headers=headers).status_code == 502
        assert not rt.mediator.verifier.sent("session-id:s-1", "the status page is up")
        assert c.post("/llm/v1/chat/completions", json=body, headers=headers).status_code == 200
    rt.mediator.executor.submit(lambda: None).result(5)  # what was handed to the engine's thread is done
    assert len(seen) == 2 and rt.mediator.verifier.sent("session-id:s-1", "the status page is up")


def test_a_history_that_counts_as_empty_is_recorded_and_read(tmp_path):
    class Uncounted(ExchangeStore):
        def __len__(self) -> int:  # as a store that counts its rows elsewhere may answer
            return 0

    settings = MemorySettings(SHARED)
    history = Uncounted(tmp_path / "data" / "exchanges.db", ROOT_KEY)
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY, record=False), settings=settings)
    rt = make_runtime(
        None, data=tmp_path / "models", root_key=ROOT_KEY, backends=replace(backends, history=history), identity=EMILY
    )
    rt.gateway.on_server = OnServer(URL, EMILY.id)
    _upstream(rt)
    try:
        with _client(rt) as c:
            assert c.post("/llm/v1/chat/completions", json=CHAT, headers=BEARER).status_code == 200
            assert rt.recorder is not None and rt.recorder.drain(timeout=5)
            assert c.get(f"{API}/stats").json()["by_agent"] != {}
    finally:
        rt.close()


def test_the_projects_a_space_remembers_are_bounded(monkeypatch):
    """Every folder a client reports is a project of its own on a server: the space remembers at most so many, the
    least recently seen forgotten, and one of the person's own settings is found again when asked for."""
    monkeypatch.setattr("latet.projects.PROJECTS_KEPT", 50)
    config = Config(projects=[ProjectEntry(path="/home/emily/billing-api", profile="max")])
    projects = Projects(config, Vault(ROOT_KEY), files=False)
    for n in range(200):
        projects.resolve(f"/tmp/folder-{n}")
    assert len(projects._projects) <= 50
    assert "/home/emily/billing-api" in [p.root for p in projects.known()]
    src, docs = projects.resolve("/home/emily/billing-api/src"), projects.resolve("/home/emily/billing-api/docs")
    assert src.config.profile == "max"  # the nearest settings of hers
    # Each folder the same settings cover shares one copy of the rules, which no folder named at will multiplies.
    assert src.config is docs.config and projects.resolve("/tmp/a").config is projects.resolve("/tmp/b").config
    projects.use(config, None)
    assert projects.resolve("/home/emily/billing-api/src").config is not src.config  # new rules: worked out again
