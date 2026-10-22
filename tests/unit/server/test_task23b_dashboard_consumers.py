"""Remaining positive dashboard branches use their real parsers, stores and reviewer clients."""

import base64
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.routing import Router

from latet import rules_report, transfer
from latet.api import rest
from latet.engine.detectors import recognizers
from latet.engine.reviewer import columns as review_columns
from latet.engine.reviewer import draft as review_draft
from latet.engine.reviewer import probe, reviewer
from latet.engine.reviewer.client import ReviewClient
from latet.gateway.approvals import Batch
from latet.server.machines import in_work
from latet.storage.exchanges import BlockRecord, ExchangeRecord, ImageRecord

from .conftest import EMILY, URL, as_identity
from .test_reviewer_key import _space

UI = {"x-latet-ui": "1"}
RULE = {
    "name": "badge",
    "type": "BADGE",
    "pattern": r"\bBD-[0-9]{6}\b",
    "examples": {"match": ["badge BD-381904"], "no_match": ["BD-42"]},
}


@pytest.fixture
async def dashboard_client(tmp_path, monkeypatch):
    runtime = _space(tmp_path)
    runtime.gateway.work = in_work
    dashboard = runtime.gateway.dashboard

    async def forbidden(*args, **kwargs):
        pytest.fail("server dashboard used the default executor")

    monkeypatch.setattr(rest.asyncio, "to_thread", forbidden)
    app = as_identity(Router(routes=dashboard.routes()), EMILY.id)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, trust_env=False) as client:
            yield runtime, dashboard, client
    finally:
        runtime.close()


def _observe(monkeypatch, owner, name, seen, label=None):
    original = getattr(owner, name)

    def observed(*args, **kwargs):
        seen.append((label or name, threading.current_thread().name))
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, observed)


def _dashboard_work(seen, required):
    assert required <= {name for name, _ in seen}, seen
    assert all(thread.startswith("latet-dashboard") for _, thread in seen), seen


def _exchange(runtime):
    text = json.dumps([{"name": "Orion", "status": "ready"}, {"name": "Vega", "status": "ready"}])
    return runtime.exchanges.record(
        ExchangeRecord(
            {"ts": time.time(), "session": "cc:consumers", "agent": "claude-code"},
            blocks=[BlockRecord("block-owned", "tool", "tool_result", False, True, text)],
            images=[
                ImageRecord("picture", "clean", "im-owned", "image/png", base64.b64encode(b"owned-picture").decode())
            ],
        )
    )


async def test_exchange_restore_columns_and_real_approval_image_use_dashboard_workers(dashboard_client, monkeypatch):
    runtime, dashboard, client = dashboard_client
    exchange = _exchange(runtime)
    batch = Batch("cc:picture", None, "claude-code", [])
    # Real Approvals.image reads the same image protocol supplied by an image masking result.
    batch.images = {0: SimpleNamespace(original=(base64.b64encode(b"owned-original").decode(), "image/png"))}
    await runtime.mediator.approvals.backend.store(batch, lambda: None)
    seen = []
    for owner, name in (
        (runtime.exchanges, "detail"),
        (runtime.exchanges, "image"),
        (rest, "restore_detail"),
        (dashboard, "_output_columns"),
    ):
        _observe(monkeypatch, owner, name, seen)
    decode = base64.b64decode

    def decoded(value):
        seen.append(("decode", threading.current_thread().name))
        return decode(value)

    monkeypatch.setattr(rest, "base64", SimpleNamespace(b64decode=decoded))
    detail = await client.get(f"{rest.API}/exchanges/{exchange}")
    columns = await client.get(f"{rest.API}/exchanges/{exchange}/columns?slot=0")
    image = await client.get(f"{rest.API}/images/im-owned")
    approval = await client.get(f"{rest.API}/approvals/{batch.id}/images/0")
    assert detail.status_code == columns.status_code == image.status_code == approval.status_code == 200
    assert "Orion" in detail.json()["request"]["slots"][0]["original"]
    assert {column["name"] for column in columns.json()["columns"]} == {"name", "status"}
    assert image.content == b"owned-picture" and approval.content == b"owned-original"
    _dashboard_work(seen, {"detail", "image", "restore_detail", "_output_columns", "decode"})


async def test_projects_rules_playground_and_recognizer_reach_their_positive_workers(dashboard_client, monkeypatch):
    runtime, dashboard, client = dashboard_client
    seen = []
    for owner, name in (
        (runtime.projects, "resolve"),
        (dashboard, "_find_project"),
        (dashboard, "_project_view"),
        (dashboard, "_playground_masker"),
        (transfer, "export_rules"),
        (transfer, "read_rules"),
        (rules_report, "report"),
    ):
        _observe(monkeypatch, owner, name, seen)
    added = await client.post(f"{rest.API}/projects", json={"path": "/owned/consumer-project"}, headers=UI)
    assert added.status_code == 200
    project = added.json()["id"]
    assert (await client.get(f"{rest.API}/projects")).status_code == 200
    assert (await client.get(f"{rest.API}/projects/{project}")).json()["id"] == project
    exported = await client.get(f"{rest.API}/rules/export?project={project}")
    assert exported.status_code == 200 and "latet_rules:" in exported.json()["file"]
    _observe(monkeypatch, transfer, "merge_rules", seen)
    imported = await client.post(f"{rest.API}/rules/import", json={"file": "terms:\n  - {value: Orion}\n"}, headers=UI)
    assert imported.status_code == 200 and imported.json()["applied"] is False
    playground = await client.post(f"{rest.API}/playground", json={"text": "plain owned text"}, headers=UI)
    assert playground.status_code == 200 and playground.json()["masked"] == "plain owned text"
    tested = await client.post(
        f"{rest.API}/recognizers/test", json={"recognizer": RULE, "text": "badge BD-381904"}, headers=UI
    )
    assert tested.status_code == 200 and tested.json()["try"]["spans"]
    _dashboard_work(
        seen,
        {
            "resolve",
            "_find_project",
            "_project_view",
            "export_rules",
            "read_rules",
            "merge_rules",
            "_playground_masker",
            "report",
        },
    )


async def test_personal_rules_compile_and_save_then_delete_with_real_settings(dashboard_client, monkeypatch):
    runtime, dashboard, client = dashboard_client
    project = (await client.post(f"{rest.API}/projects", json={"path": "/owned/personal-project"}, headers=UI)).json()[
        "id"
    ]
    etag = (await client.get(f"{rest.API}/config")).json()["etag"]
    seen = []
    _observe(monkeypatch, dashboard, "_find_project", seen)
    _observe(monkeypatch, rest, "check_rules", seen)
    # Runtime rebuilding also compiles rules in its save thread. Observe the handler's real compile separately.
    compile_all = recognizers.compile_all
    own_pool = dashboard._turn("pool")

    def compiled(*args, **kwargs):
        if own_pool.locked():
            seen.append(("compile_all", threading.current_thread().name))
        return compile_all(*args, **kwargs)

    monkeypatch.setattr(recognizers, "compile_all", compiled)
    answer = await client.put(
        f"{rest.API}/projects/{project}/personal", json={"etag": etag, "recognizers": [RULE]}, headers=UI
    )
    assert answer.status_code == 200 and answer.json()["ok"] is True
    assert runtime.config.projects and runtime.config.projects[-1].recognizers
    removed = await client.delete(f"{rest.API}/projects/{project}/personal", headers=UI)
    assert removed.status_code == 200 and removed.json()["ok"] is True
    _dashboard_work(seen, {"_find_project", "check_rules", "compile_all"})


async def test_reviewer_probe_draft_report_and_column_opinions_use_real_clients(dashboard_client, monkeypatch):
    runtime, dashboard, client = dashboard_client
    exchange = _exchange(runtime)
    seen, requests = [], []
    reviewer_turn, pool_turn = dashboard._turn("reviewer"), dashboard._turn("pool")

    def transport(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen"}]})
        if request.url.path.endswith("/props"):
            return httpx.Response(200, json={"total_slots": 1})
        data = json.loads(request.content)
        system = data["messages"][0]["content"]
        if system == review_draft.SYSTEM:
            answer = {**RULE, "description": "An owned badge"}
        elif system == review_columns.SYSTEM:
            answer = {"columns": [{"name": "status", "keep": True, "why": "owned finite states"}]}
        else:
            answer = {"findings": [], "block": False}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

    def review_client(*args, **kwargs):
        assert reviewer_turn.locked() and pool_turn.locked()
        seen.append(("client", threading.current_thread().name))
        return ReviewClient(*args, **kwargs, transport=httpx.MockTransport(transport))

    monkeypatch.setattr(reviewer, "ReviewClient", review_client)
    for owner, name in (
        (dashboard, "_shared"),
        (dashboard, "_review_settings"),
        (probe, "reviewed_view"),
        (dashboard, "_output_columns"),
        (rules_report, "report"),
    ):
        _observe(monkeypatch, owner, name, seen)
    checked = await client.post(f"{rest.API}/reviewer/test", json={"text": "plain owned text"}, headers=UI)
    assert checked.status_code == 200 and checked.json()["ok"] is True
    drafted = await client.post(
        f"{rest.API}/recognizers/draft", json={"description": "owned badge numbers"}, headers=UI
    )
    assert drafted.status_code == 200 and drafted.json()["draft"]["pattern"] == RULE["pattern"]
    assert drafted.json()["report"]["state"] != "error"
    columns = await client.post(f"{rest.API}/exchanges/{exchange}/columns/review?slot=0", headers=UI)
    assert columns.status_code == 200 and columns.json()["opinions"]["status"]["keep"] is True
    assert len([path for path in requests if path.endswith("/chat/completions")]) == 3
    _dashboard_work(seen, {"_shared", "_review_settings", "reviewed_view", "client", "report", "_output_columns"})
    assert not reviewer_turn.locked() and not pool_turn.locked()


async def test_local_only_file_and_key_operations_still_refuse_before_worker_submission(dashboard_client, monkeypatch):
    _, dashboard, client = dashboard_client

    async def forbidden(*args, **kwargs):
        pytest.fail("a server-only refusal reached blocking work")

    monkeypatch.setattr(dashboard, "_in_pool", forbidden)
    for method, path, body in (
        ("PUT", "/reviewer/key", {"key": "owned"}),
        ("POST", "/reviewer/certificate", {"url": "https://owned.invalid"}),
        ("PUT", "/projects/owned/repo", {}),
        ("POST", "/rules/import", {"file": "terms: []", "project": "owned"}),
    ):
        assert (await client.request(method, f"{rest.API}{path}", json=body, headers=UI)).status_code == 409
