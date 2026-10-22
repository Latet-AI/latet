"""EOP-24/25/26/27/33 consumer regressions, prepared without executing the application.

These exercise current entry points. The future supervisor/bootstrap protocol has a separate capability gate;
missing future APIs are never used as RED evidence here. Every repository/configuration belongs to tmp_path.
"""

import asyncio
import hashlib
import json
import os
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from typer.testing import CliRunner

from latet import projects as projects_module
from latet.api import rest
from latet.bridge import api as bridge_module
from latet.config import Config, IdentityConfig, load_config, save_config
from latet.projects import MAX_FILE_BYTES, PROJECT_FILE, file_tag, read_project_file
from latet.runtime import REVIEWED_FILE, make_runtime
from latet.state import local as local_module
from latet.state.local import LocalRepositories, LocalSettings

from ..conftest import ROOT_KEY


def _repo(tmp_path, name="strict", raw=b"profile: max\n"):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    (root / PROJECT_FILE).write_bytes(raw)
    return root


def _start(tmp_path):
    return {"data": tmp_path / "data", "config_file": tmp_path / "config.yaml", "root_key": ROOT_KEY}


def _approved(tmp_path, roots):
    config = Config(identity=IdentityConfig(enabled=False))
    start = _start(tmp_path)
    LocalSettings(start["config_file"], start["data"]).save_reviewed(config)
    LocalRepositories(start["data"]).save([str(root) for root in roots])
    save_config(config, start["config_file"])
    return config, start


def _request(rt, data=None, pid=None, authorized=True):
    body = json.dumps(data or {}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "query_string": b"",
            "headers": [(b"x-latet-token", (rt.gateway.bridge_token if authorized else "wrong").encode())],
            "path_params": {"id": pid} if pid else {},
        },
        receive,
    )


async def _reload(rt):
    response = await rest.Dashboard(rt).reload(_request(rt))
    return response, json.loads(bytes(response.body))


def _other_thread_can_take(lock):
    result = []

    def attempt():
        acquired = lock.acquire(blocking=False)
        result.append(acquired)
        if acquired:
            lock.release()

    thread = threading.Thread(target=attempt, name="repository-lock-witness")
    thread.start()
    thread.join(1)
    assert not thread.is_alive(), "finite lock witness did not finish"
    return result == [True]


@pytest.mark.parametrize("authorized", [False, True], ids=["unauthorized", "authorized"])
async def test_failed_route_check_remembers_the_project_off_loop(tmp_path, monkeypatch, authorized):
    root = _repo(tmp_path)
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), **_start(tmp_path))
    loop_thread = threading.get_ident()
    calls = []
    loop, progressed, release = asyncio.get_running_loop(), [], threading.Event()
    try:
        assert rt.recorder is not None
        real_root, real_load, real_save = projects_module.project_root, LocalRepositories.load, LocalRepositories.save

        def discovery(path):
            assert threading.get_ident() != loop_thread, "repository discovery ran on the event loop"
            calls.append("root")
            loop.call_soon_threadsafe(loop_progress)
            assert release.wait(1), "event loop failed to progress during bounded repository discovery"
            return real_root(path)

        def loop_progress():
            progressed.append(threading.get_ident())
            release.set()

        def loading(store):
            assert threading.get_ident() != loop_thread, "repository list read ran on the event loop"
            calls.append("load")
            return real_load(store)

        def saving(store, folders):
            assert threading.get_ident() != loop_thread, "repository list write ran on the event loop"
            calls.append("save")
            return real_save(store, folders)

        monkeypatch.setattr(projects_module, "project_root", discovery)
        monkeypatch.setattr(LocalRepositories, "load", loading)
        monkeypatch.setattr(LocalRepositories, "save", saving)
        monkeypatch.setattr(
            bridge_module, "check_claude_route", lambda data: SimpleNamespace(ok=False, reason="test", toast="test")
        )
        bridge = bridge_module.Bridge(rt.mediator, rt.gateway.bridge_token, recorder=rt.recorder)
        response = await bridge.route_check(_request(rt, {"session": "owned", "cwd": str(root)}, authorized=authorized))
        assert response.status_code == (200 if authorized else 401)
        if authorized:
            assert {"root", "load", "save"} <= set(calls)
            assert progressed == [loop_thread]
            assert real_load(LocalRepositories(tmp_path / "data")) == [str(root)]
        else:
            assert calls == []
    finally:
        release.set()
        rt.close()


@pytest.mark.parametrize("changed_after_read", [False, True], ids=["count", "immutable"])
async def test_startup_uses_one_snapshot_and_reload_reads_a_new_membership(tmp_path, monkeypatch, changed_after_read):
    roots = [_repo(tmp_path, name) for name in ("first", "second")]
    approved, start = _approved(tmp_path, roots)
    save_config(approved.model_copy(update={"include_private_ips": False}), start["config_file"])
    reads, lists = Counter(), []
    original, original_load = projects_module._read_remembered, LocalRepositories.load

    def reading(folder):
        reads[folder] += 1
        result = original(folder)
        if changed_after_read:
            (Path(folder) / PROJECT_FILE).write_bytes(b"")
        return result

    def loading(store):
        lists.append(store.path)
        return original_load(store)

    monkeypatch.setattr(projects_module, "_read_remembered", reading)
    monkeypatch.setattr(LocalRepositories, "load", loading)
    try:
        rt = make_runtime(**start)
    except (OSError, ValueError, RuntimeError):
        if not changed_after_read:
            raise
        # A source-stamp revalidation may explicitly refuse this externally changed snapshot.
        assert reads == Counter({str(root): 1 for root in roots})
        assert len(lists) == 1
        assert LocalSettings(start["config_file"], start["data"]).reviewed() == approved
        assert LocalRepositories(start["data"]).load() == list(map(str, roots))
        return
    try:
        pending = rt.mediator.approvals.pending_changes()
        assert len(pending) == 1 and pending[0]["source"] == "start"
        # Folders that share a reason say it once (``protection._by_folders``): one reason naming both.
        [reason] = pending[0]["loosened"]
        assert "first" in reason and "second" in reason, "initial proposal lost the startup byte snapshot"
        assert reads == Counter({str(root): 1 for root in roots})
        assert len(lists) == 1, "startup reread the remembered list"
        third = _repo(tmp_path, "newly-remembered")
        for root in roots:
            (root / PROJECT_FILE).write_bytes(b"profile: max\n")
        LocalRepositories(start["data"]).save([str(third), *map(str, roots)])
        # Other settings than the start's change: the same ones would find that change kept as it is (a kept proposal
        # is never announced again, Task 23A), its reasons the start's; these open a change weighed by this reload.
        save_config(
            approved.model_copy(update={"include_private_ips": False, "retention_days": 7}), start["config_file"]
        )
        reads.clear()
        lists.clear()
        _, answer = await _reload(rt)
        assert not answer.get("applied", False)
        assert reads == Counter({str(root): 1 for root in [*roots, third]})
        assert len(lists) == 1
        assert any("newly-remembered" in reason for reason in answer["pending"]["loosened"])
    finally:
        rt.close()


async def test_reload_filesystem_work_holds_neither_runtime_nor_projects_lock(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    approved, start = _approved(tmp_path, [root])
    rt = make_runtime(**start)
    real = projects_module._read_remembered
    witnessed = []
    try:

        def reading(folder):
            # Recorded, not asserted here: the read runs in the helper, whose failure would only make the check
            # incomplete (a refusal this test must not mistake for the locks being free).
            witnessed.append((folder, _other_thread_can_take(rt._lock), _other_thread_can_take(rt.projects._lock)))
            return real(folder)

        monkeypatch.setattr(projects_module, "_read_remembered", reading)
        save_config(approved.model_copy(update={"include_private_ips": False}), start["config_file"])
        response, answer = await _reload(rt)
        assert witnessed == [(str(root), True, True)], "repository I/O held the runtime or the projects lock"
        assert response.status_code == 200 and answer["pending"] and not answer["applied"]
    finally:
        rt.close()


def test_project_discovery_releases_the_projects_lock_before_filesystem_work(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), **_start(tmp_path))
    original = projects_module.project_root
    witnessed = []
    try:

        def discovering(path):
            assert _other_thread_can_take(rt.projects._lock), "root discovery holds the projects lock"
            assert _other_thread_can_take(rt._lock), "root discovery holds the runtime lock"
            witnessed.append(path)
            return original(path)

        monkeypatch.setattr(projects_module, "project_root", discovering)
        project = rt.projects.resolve(str(root))
        assert project.root == str(root) and witnessed == [str(root)]
    finally:
        rt.close()


@pytest.mark.parametrize("failure", [PermissionError, TimeoutError, OSError])
@pytest.mark.parametrize("already_waiting", [False, True], ids=["new", "pending"])
async def test_indeterminate_repository_reload_keeps_runtime_reviewed_membership_and_pending(
    tmp_path, monkeypatch, failure, already_waiting
):
    root = _repo(tmp_path)
    approved, start = _approved(tmp_path, [root])
    rt = make_runtime(**start)
    try:
        candidate = approved.model_copy(update={"include_private_ips": False})
        save_config(candidate, start["config_file"])
        if already_waiting:
            _, first = await _reload(rt)
            assert not first["applied"]
        pending = set(rt.mediator.approvals.changes)
        reviewed = (start["data"] / REVIEWED_FILE).read_bytes()
        original = projects_module.own_file
        applied, approved_writes, settlements = [], [], []
        real_apply, real_reviewed = rt.apply_config, LocalSettings.save_reviewed
        approvals = rt.mediator.approvals
        real_settle = approvals.settle_change

        def apply(config, *args, **kwargs):
            applied.append(config)
            return real_apply(config, *args, **kwargs)

        def save_reviewed(store, config):
            approved_writes.append(config)
            return real_reviewed(store, config)

        def settle(*args, **kwargs):
            settlements.append(args)
            return real_settle(*args, **kwargs)

        def unavailable(path, most, **kwargs):
            if Path(path) == root / PROJECT_FILE:
                raise failure("private repository marker")
            return original(path, most, **kwargs)

        monkeypatch.setattr(projects_module, "own_file", unavailable)
        monkeypatch.setattr(rt, "apply_config", apply)
        monkeypatch.setattr(LocalSettings, "save_reviewed", save_reviewed)
        monkeypatch.setattr(approvals, "settle_change", settle)
        response, answer = await _reload(rt)
        assert response.status_code == 503 and answer["reason"] == "repositories_unchecked"
        assert applied == approved_writes == settlements == [], "an incomplete scan committed or settled protection"
        assert rt.config == approved, "an incomplete scan applied the candidate"
        assert (start["data"] / REVIEWED_FILE).read_bytes() == reviewed
        assert set(rt.mediator.approvals.changes) == pending, "uncertainty settled/replaced a waiting change"
        assert LocalRepositories(start["data"]).load() == [str(root)]
        assert "private repository marker" not in bytes(response.body).decode()
    finally:
        rt.close()


@pytest.mark.parametrize("has_approved", [False, True], ids=["first-start", "safe-start"])
def test_indeterminate_startup_requires_an_existing_safe_approved_snapshot(tmp_path, monkeypatch, has_approved):
    root = _repo(tmp_path)
    approved, start = _approved(tmp_path, [root])
    candidate = approved.model_copy(update={"include_private_ips": False})
    save_config(candidate, start["config_file"])
    reviewed_path = start["data"] / REVIEWED_FILE
    reviewed = reviewed_path.read_bytes()
    if not has_approved:
        reviewed_path.unlink()
    original = projects_module.own_file

    def unavailable(path, most, **kwargs):
        if Path(path) == root / PROJECT_FILE:
            raise PermissionError("private startup marker")
        return original(path, most, **kwargs)

    monkeypatch.setattr(projects_module, "own_file", unavailable)
    if not has_approved:
        # R24 M2: with no settings approved there is nothing to weigh against, so a scan, complete or not, protects
        # nothing: the start does not refuse, and reads no repository for it.
        reads = []
        monkeypatch.setattr(projects_module, "_read_remembered", lambda folder: reads.append(folder))
        rt = make_runtime(**start)
        try:
            assert rt.config == candidate and reads == []
        finally:
            rt.close()
    else:
        rt = make_runtime(**start)
        try:
            assert rt.config == approved, "startup applied a candidate from an incomplete scan"
            assert rt.projects.before == approved
            assert reviewed_path.read_bytes() == reviewed
        finally:
            rt.close()
    assert LocalRepositories(start["data"]).load() == [str(root)]


@pytest.mark.parametrize("delta", [0, 1], ids=["exact-cap", "over-cap"])
def test_project_reader_and_tag_never_use_an_unbounded_path_read(tmp_path, monkeypatch, delta):
    raw = b"#" * (MAX_FILE_BYTES + delta)
    path = _repo(tmp_path, raw=raw) / PROJECT_FILE
    real = Path.read_bytes

    def unbounded_read(file):
        assert file != path, "project consumer attempted unrestricted Path.read_bytes"
        return real(file)

    monkeypatch.setattr(Path, "read_bytes", unbounded_read)
    settings, notices = read_project_file(path)
    if delta:
        assert settings is None and notices
        try:
            tag = file_tag(path)
        except (OSError, ValueError):
            pass
        else:
            assert tag is None, "oversized data produced a usable editor tag"
    else:
        assert settings is not None and notices == []
        assert file_tag(path) == hashlib.sha256(raw).hexdigest()[:16]


@pytest.mark.parametrize("kind", ["directory", "unreadable", "oversized"])
async def test_unknown_editor_tag_cannot_authorize_empty_tag_write(tmp_path, monkeypatch, kind):
    root = _repo(tmp_path)
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), **_start(tmp_path))
    try:
        project = rt.projects.resolve(str(root))
        path = root / PROJECT_FILE
        if kind == "directory":
            path.unlink()
            path.mkdir()
        elif kind == "oversized":
            path.write_bytes(b"#" * (MAX_FILE_BYTES + 1))
        else:
            real = Path.read_bytes
            real_open = os.open

            def denied(file):
                if file == path:
                    raise PermissionError("private editor marker")
                return real(file)

            monkeypatch.setattr(Path, "read_bytes", denied)

            def denied_open(file, flags, *args, **kwargs):
                if Path(file) == path:
                    raise PermissionError("private editor marker")
                return real_open(file, flags, *args, **kwargs)

            monkeypatch.setattr(os, "open", denied_open)
        writes = []
        monkeypatch.setattr(rest, "in_container", lambda: False)
        monkeypatch.setattr(rest, "write_project_file", lambda *args: writes.append(args))
        response = await rest.Dashboard(rt).put_repo(
            _request(rt, {"tag": "", "settings": {"profile": "max"}}, project.id)
        )
        assert response.status_code == {"directory": 409, "unreadable": 503, "oversized": 409}[kind]
        assert writes == [], "an unknown tag authorized a repository write"
        assert "private editor marker" not in bytes(response.body).decode()
    finally:
        rt.close()


def test_no_follow_without_atomic_platform_support_refuses_before_open(tmp_path, monkeypatch):
    path = _repo(tmp_path) / PROJECT_FILE
    opened = []
    real = os.open

    def opening(file, flags, *args, **kwargs):
        opened.append(file)
        return real(file, flags, *args, **kwargs)

    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    # Windows' own atomic way taken away too (``fileio._windows_no_follow``): a platform with neither.
    from latet import fileio

    monkeypatch.setattr(fileio, "_windows_no_follow", None)
    monkeypatch.setattr(os, "open", opening)
    try:
        result = local_module.own_file(path, MAX_FILE_BYTES, follow=False)
    except OSError:
        pass
    else:
        assert result is None, "unsupported no-follow mode returned readable bytes"
    assert opened == [], "unsupported no-follow mode attempted an unsafe open"


def test_project_read_and_tag_follow_a_fixture_owned_parent_link(tmp_path):
    target = _repo(tmp_path)
    link = tmp_path / "linked"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        settings, notices = read_project_file(link / PROJECT_FILE)
        assert settings is not None and settings.profile == "max" and notices == []
        assert file_tag(link / PROJECT_FILE) == hashlib.sha256(b"profile: max\n").hexdigest()[:16]
    finally:
        # Only the fixture-owned link: never recursively traverse its target during cleanup.
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()


@pytest.mark.skipif(os.name == "nt", reason="file symlinks need a Windows privilege")
def test_project_read_and_tag_follow_a_fixture_owned_file_link(tmp_path):
    root = _repo(tmp_path)
    target = root / "settings.yaml"
    (root / PROJECT_FILE).rename(target)
    (root / PROJECT_FILE).symlink_to(target)
    settings, notices = read_project_file(root / PROJECT_FILE)
    assert settings is not None and settings.profile == "max" and notices == []
    assert file_tag(root / PROJECT_FILE) == hashlib.sha256(target.read_bytes()).hexdigest()[:16]


@pytest.mark.parametrize("operation", ["detail", "save"])
async def test_project_detail_and_save_filesystem_work_run_off_loop(tmp_path, monkeypatch, operation):
    root = _repo(tmp_path)
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), **_start(tmp_path))
    try:
        project = rt.projects.resolve(str(root))
        loop_thread = threading.get_ident()
        real_dir, real_tag = Path.is_dir, rest.file_tag
        checked = []

        def directory(path):
            if path == root:
                assert threading.get_ident() != loop_thread, "detail directory lookup ran on loop"
                checked.append("directory")
            return real_dir(path)

        def tag(path):
            assert threading.get_ident() != loop_thread, "detail tag read ran on loop"
            checked.append("tag")
            return real_tag(path)

        monkeypatch.setattr(Path, "is_dir", directory)
        monkeypatch.setattr(rest, "file_tag", tag)
        monkeypatch.setattr(rest, "in_container", lambda: False)
        dashboard = rest.Dashboard(rt)
        if operation == "detail":
            response = await dashboard.project(_request(rt, pid=project.id))
        else:
            expected = hashlib.sha256(b"profile: max\n").hexdigest()[:16]
            response = await dashboard.put_repo(
                _request(rt, {"tag": expected, "settings": {"profile": "max"}}, project.id)
            )
        assert response.status_code == 200 and {"directory", "tag"} <= set(checked)
    finally:
        rt.close()


def _import(computer, monkeypatch, answer, *, wait=False, dry=False, state="approved"):
    from latet.cli import admin, rules
    from latet.cli.main import app

    save_config(Config(identity=IdentityConfig(enabled=False)))
    saved, reloaded, waited = [], [], []
    real_save = save_config

    def saving(config, *args, **kwargs):
        saved.append(config.model_copy(deep=True))
        return real_save(config, *args, **kwargs)

    def reload_answer():
        reloaded.append(True)
        return answer

    def wait_for_change(change_id):
        waited.append(change_id)
        return state

    monkeypatch.setattr("latet.config.save_config", saving)
    monkeypatch.setattr(admin, "_reload_answer", reload_answer)
    monkeypatch.setattr(admin, "wait_for_change", wait_for_change)
    monkeypatch.setattr(rules, "_known_in_vault", lambda values: None)
    arguments = ["rules", "import", "-", "--all"]
    arguments += ["--wait"] if wait else []
    arguments += ["--dry-run"] if dry else []
    result = CliRunner().invoke(
        app, arguments, input="latet_rules: 1\nterms: [{value: Project Falcon}]\nallow_values: [owned-dev]\n"
    )
    return result, saved, reloaded, waited


@pytest.mark.parametrize("dry", [False, True], ids=["pending", "dry-run"])
def test_import_all_plans_one_final_candidate_and_saves_reloads_once(computer, monkeypatch, dry):
    result, saved, reloaded, waited = _import(
        computer, monkeypatch, {"applied": False, "pending": {"id": "owned"}}, dry=dry
    )
    assert result.exit_code == (0 if dry else 3), result.output
    assert "Added:" not in result.output and ("Would add" in result.output or "Planned additions" in result.output)
    assert len(saved) == len(reloaded) == (0 if dry else 1)
    assert waited == []
    if not dry:
        assert saved[0].allow_values == ["owned-dev"]
        assert [term.value for term in saved[0].terms] == ["Project Falcon"]
        assert load_config().allow_values == ["owned-dev"]


@pytest.mark.parametrize("state", ["approved", "rejected", "stale", "superseded", None])
def test_import_wait_reports_settlement_without_duplicate_save(computer, monkeypatch, state):
    result, saved, reloaded, waited = _import(
        computer, monkeypatch, {"applied": False, "pending": {"id": "owned"}}, wait=True, state=state
    )
    assert result.exit_code == (0 if state == "approved" else 3), result.output
    assert len(saved) == len(reloaded) == 1 and waited == ["owned"]
    assert saved[0].allow_values == ["owned-dev"]
    if state != "approved":
        assert "Rules reloaded." not in result.output and "Added:" not in result.output


@pytest.mark.parametrize("answer", [None, {}, {"error": "runtime refused"}, {"applied": True}])
def test_import_completion_never_infers_application_from_a_saved_candidate(computer, monkeypatch, answer):
    result, saved, reloaded, _ = _import(computer, monkeypatch, answer)
    assert len(saved) == len(reloaded) == 1
    assert saved[0].allow_values == ["owned-dev"]
    if answer is None or answer == {}:
        assert result.exit_code == 1, result.output
        assert "unconfirmed" in result.output.lower() or "unknown" in result.output.lower()
        assert "daemon is not running" not in result.output.lower() and "Rules reloaded." not in result.output
    elif "error" in answer:
        assert result.exit_code == 1 and "Added:" not in result.output
    else:
        assert result.exit_code == 0 and "Rules reloaded." in result.output


@pytest.mark.parametrize("answer", [{"ok": True}, {"applied": "yes"}, {"recognizers": []}])
def test_an_answer_that_does_not_say_it_applied_is_never_taken_for_applied(computer, monkeypatch, answer):
    result, saved, reloaded, _ = _import(computer, monkeypatch, answer)
    assert len(saved) == len(reloaded) == 1
    assert result.exit_code == 1 and "unconfirmed" in result.output, result.output
    assert "Rules reloaded." not in result.output


async def test_an_incomplete_reload_is_a_fixed_retryable_refusal_and_the_cli_says_why(tmp_path, monkeypatch):
    """The reload's answer when a remembered repository cannot be read now: 503 with its reason and Retry-After, its
    own fixed words; the CLI says why in words of its own, never the answer's."""
    from latet.cli.admin import _refusal

    root = _repo(tmp_path)
    approved, start = _approved(tmp_path, [root])
    rt = make_runtime(**start)
    try:
        save_config(approved.model_copy(update={"include_private_ips": False}), start["config_file"])
        original = projects_module.own_file

        def unavailable(path, most, **kwargs):
            if Path(path) == root / PROJECT_FILE:
                raise PermissionError("private repository marker")
            return original(path, most, **kwargs)

        monkeypatch.setattr(projects_module, "own_file", unavailable)
        response, answer = await _reload(rt)
        assert response.status_code == 503 and response.headers["retry-after"] == "1"
        assert answer["reason"] == "repositories_unchecked" and "strict" not in answer["error"]
        assert rt.config == approved and rt.mediator.approvals.pending_changes() == []
    finally:
        rt.close()
    said = _refusal({"status": 503, "error": "words of another program", "reason": "repositories_unchecked"})
    assert "could not all be read" in said and "another program" not in said
    assert _refusal({"status": 503, "error": "x"}).startswith("it answered 503")


async def test_a_change_that_cannot_lower_a_repository_applies_while_one_cannot_be_read(tmp_path, monkeypatch):
    """Only a change that could lower protection in a repository waits on its being read: a term added, which adds
    protection everywhere and is laid over no repository's own settings, applies as before."""
    from latet.config import TermConfig

    root = _repo(tmp_path)
    approved, start = _approved(tmp_path, [root])
    rt = make_runtime(**start)
    try:
        term = TermConfig(value="Project Falcon", type="PROJECT")
        save_config(approved.model_copy(update={"terms": [term]}), start["config_file"])
        original = projects_module.own_file

        def unavailable(path, most, **kwargs):
            if Path(path) == root / PROJECT_FILE:
                raise PermissionError("private repository marker")
            return original(path, most, **kwargs)

        monkeypatch.setattr(projects_module, "own_file", unavailable)
        response, answer = await _reload(rt)
        assert response.status_code == 200 and answer["applied"] is True
        assert [t.value for t in rt.config.terms] == ["Project Falcon"]
    finally:
        rt.close()


def _unreadable(monkeypatch, root):
    """``root``'s latet.yaml refused to every read from now on, as a share or a permission refuses it."""
    original = projects_module.own_file

    def unavailable(path, most, **kwargs):
        if Path(path) == root / PROJECT_FILE:
            raise PermissionError("private repository marker")
        return original(path, most, **kwargs)

    monkeypatch.setattr(projects_module, "own_file", unavailable)


@pytest.mark.parametrize("apply", [False, True], ids=["preview", "apply"])
async def test_a_dashboard_import_is_refused_while_a_repository_cannot_be_read(tmp_path, monkeypatch, apply):
    root = _repo(tmp_path)
    approved, start = _approved(tmp_path, [root])
    rt = make_runtime(**start)
    try:
        _unreadable(monkeypatch, root)
        rules = "latet_rules: 1\nterms: [{value: Project Falcon}]\nallow_values: [owned-dev]\n"
        response = await rest.Dashboard(rt).rules_import(_request(rt, {"file": rules, "apply": apply, "all": True}))
        answer = json.loads(bytes(response.body))
        assert response.status_code == 503 and answer["reason"] == "repositories_unchecked"
        assert "private repository marker" not in answer["error"]
        assert rt.config == approved and load_config(start["config_file"]) == approved
    finally:
        rt.close()


def test_a_repository_reached_after_the_snapshot_is_weighed_with_it(tmp_path):
    """The snapshot is read before the settings' lock; a repository a conversation reaches meanwhile is weighed as the
    projects of the process hold it when the change is weighed (``Projects.with_active``)."""
    strict = _repo(tmp_path)
    approved, start = _approved(tmp_path, [])
    rt = make_runtime(**start)
    try:
        assert rt.projects is not None
        scan = rt.repositories_for()
        assert scan is not None and scan.complete and len(scan) == 0
        rt.projects.resolve(str(strict))  # reached after the snapshot was read
        change = rt.propose(approved.model_copy(update={"include_private_ips": False}), "file", repositories=scan)
        assert change is not None and any("strict" in reason for reason in change.loosened)
    finally:
        rt.close()


def test_a_destination_added_while_a_change_waits_keeps_it_as_it_is_when_a_repository_cannot_be_read(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    approved, start = _approved(tmp_path, [root])
    save_config(approved.model_copy(update={"include_private_ips": False, "allow_values": ["owned-dev"]}),
                start["config_file"])  # fmt: skip
    rt = make_runtime(**start)
    try:
        [waiting] = rt.mediator.approvals.pending_changes()
        _unreadable(monkeypatch, root)
        names = rt.add_destinations([("https://llm.example.com", "claude")])
        assert names and [change["id"] for change in rt.mediator.approvals.pending_changes()] == [waiting["id"]]
        assert load_config(start["config_file"]).allow_values == ["owned-dev"]  # what waits, kept as it was
    finally:
        rt.close()


async def test_a_project_whose_latet_yaml_cannot_be_read_is_shown_unwritable(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), **_start(tmp_path))
    try:
        project = rt.projects.resolve(str(root))
        path, real_open = root / PROJECT_FILE, os.open

        def denied(file, flags, *args, **kwargs):
            if Path(file) == path:
                raise PermissionError("private editor marker")
            return real_open(file, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", denied)
        response = await rest.Dashboard(rt).project(_request(rt, pid=project.id))
        repo = json.loads(bytes(response.body))["repo"]
        assert repo["readable"] is False and repo["writable"] is False and repo["tag"] == ""
    finally:
        rt.close()


def test_closing_the_runtime_ends_a_helper_still_at_work(tmp_path, monkeypatch):
    """R24 M7: the daemon closing kills a helper still reading (ER.4's shutdown), never waiting for it."""
    from latet import repository_io

    from ..repository_children import InProcessChild

    entered, release, children = threading.Event(), threading.Event(), []

    def read_one(folder):
        entered.set()
        release.wait(10)
        return projects_module._read_remembered(folder)

    def launch():
        children.append(InProcessChild(read_one))
        return children[-1]

    monkeypatch.setattr(repository_io, "launch", launch)
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), **_start(tmp_path))
    scanning = threading.Thread(target=lambda: repository_io.READER.scan([str(tmp_path)]))
    scanning.start()
    try:
        assert entered.wait(5)
        rt.close()
        assert children and children[0].killed
    finally:
        release.set()
        scanning.join(10)


def test_a_start_that_cannot_build_its_runtime_says_why_in_fixed_words_and_exits_without_a_traceback(
    monkeypatch, capsys
):
    """R24 M2: what stops make_runtime at the daemon's start ends with fixed words and exit code 1, never a traceback
    (a service manager logs one line)."""
    from latet import runtime
    from latet.cli.main import run_server
    from latet.config import ConfigError
    from latet.service import ports

    monkeypatch.setattr(ports, "probe", lambda port: SimpleNamespace(usable=True, reason=None))
    for failure, said in (
        (ConfigError("config.yaml: terms.0: not valid"), "config.yaml: terms.0: not valid"),
        (PermissionError(13, "denied", "C:/private/latet.db"), "PermissionError"),
    ):

        def refusing(*args, failure=failure, **kwargs):
            raise failure

        monkeypatch.setattr(runtime, "make_runtime", refusing)
        with pytest.raises(SystemExit) as stopped:
            run_server(port=6698)
        err = capsys.readouterr().err
        assert stopped.value.code == 1 and "Not started" in err and said in err
        assert "Traceback" not in err and "private" not in err


async def test_a_reload_that_cannot_lower_a_repository_reads_none(tmp_path, monkeypatch):
    """R24 M4: the candidate is read before the scan, and a change that cannot weigh (the port, say) starts no helper."""
    root = _repo(tmp_path)
    approved, start = _approved(tmp_path, [root])
    rt = make_runtime(**start)
    try:
        reads = []
        real = projects_module._read_remembered

        def reading(folder):
            reads.append(folder)
            return real(folder)

        monkeypatch.setattr(projects_module, "_read_remembered", reading)
        save_config(approved.model_copy(update={"port": 5299}), start["config_file"])
        response, answer = await _reload(rt)
        assert response.status_code == 200 and answer["applied"] is True and reads == []
        save_config(approved.model_copy(update={"include_private_ips": False}), start["config_file"])
        _, answer = await _reload(rt)
        assert reads == [str(root)] and not answer["applied"]  # one that can weigh reads them
    finally:
        rt.close()


def test_a_held_back_repository_is_weighed_in_the_starts_snapshot_never_read_again_on_the_conversation(
    tmp_path, monkeypatch
):
    """R24 M4: the conversation that reaches a held-back repository starts no helper on its own thread: the change it
    opens is weighed in the start's snapshot of the repositories remembered, with this run's projects as they are now
    (the repository just reached among them)."""
    from latet import repository_io

    remembered = _repo(tmp_path, "remembered", b"pii: [IBAN]\n")  # lowered by nothing at the start
    approved, start = _approved(tmp_path, [remembered])
    save_config(approved.model_copy(update={"include_private_ips": False}), start["config_file"])
    rt = make_runtime(**start)
    try:
        assert rt.projects.before == approved and rt.config != approved
        launched = []
        monkeypatch.setattr(repository_io, "launch", lambda: launched.append(1))
        rt.projects.resolve(str(_repo(tmp_path, "strict")))
        assert launched == [], "the conversation's thread started a helper"
        assert rt.config == approved  # what you approved runs at once
        [change] = rt.mediator.approvals.pending_changes()
        assert change["source"] == "start" and any("strict" in reason for reason in change["loosened"])
    finally:
        rt.close()


async def test_two_saves_of_a_project_file_compare_and_write_one_after_the_other(tmp_path, monkeypatch):
    """ER.7: the tag compared and the file written by one save at a time (M6): a second save waits for the first."""
    root = _repo(tmp_path)
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), **_start(tmp_path))
    try:
        project = rt.projects.resolve(str(root))
        dashboard = rest.Dashboard(rt)
        real_tag = rest.file_tag

        def slow_tag(path):
            tag = real_tag(path)
            time.sleep(0.3)  # read, then a while before the write: another save in between would read the same
            return tag

        monkeypatch.setattr(rest, "file_tag", slow_tag)
        monkeypatch.setattr(rest, "in_container", lambda: False)
        expected = hashlib.sha256(b"profile: max\n").hexdigest()[:16]
        body = {"tag": expected, "settings": {"profile": "max", "pii": ["IBAN"]}}
        answers = await asyncio.gather(
            dashboard.put_repo(_request(rt, body, project.id)), dashboard.put_repo(_request(rt, body, project.id))
        )
        # One after the other: the second compares the file the first wrote, and is refused (two at once would both
        # match the bytes read before either wrote, and the second would write over the first unseen).
        assert sorted(answer.status_code for answer in answers) == [200, 409]
    finally:
        rt.close()
