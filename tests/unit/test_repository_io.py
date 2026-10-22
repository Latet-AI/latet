"""EOP-26: the repositories a computer remembers are read by one helper process at a time, within deadlines.

The helper's real process (``repository_child``, started by ``repository_io._launch``) reads each remembered folder's
latet.yaml; the daemon waits for it no longer than its deadlines, ends it, and keeps its place while the system has not
let it go. Fake helpers here (a process that never answers, frames of another shape, a reader that stalls) exercise the
place, the deadlines and the frames; ``real_repository_helper`` the real process.
"""

import io
import json
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from latet import repository_io
from latet.config import Config, IdentityConfig, save_config
from latet.project_file import PROJECT_FILE, Remembered
from latet.repository_child import FRAME_MOST, encode
from latet.repository_io import RepositoryReader
from latet.runtime import make_runtime
from latet.state.local import LocalRepositories, LocalSettings

from ..conftest import ROOT_KEY
from ..repository_children import InProcessChild

SRC = Path(repository_io.__file__).resolve().parents[1]


def _repo(tmp_path, name, raw=b"profile: max\n"):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    if raw is not None:
        (root / PROJECT_FILE).write_bytes(raw)
    return root


def _link(target, link):
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _unlink(link):
    os.rmdir(link) if os.name == "nt" else link.unlink()  # the test's own link alone, never what it names


# --- the real helper ---------------------------------------------------------------------------------------------


def test_the_real_helper_answers_each_file_in_order_within_its_deadlines(tmp_path, real_repository_helper):
    strict = _repo(tmp_path, "strict")
    linked = tmp_path / "linked"
    _link(_repo(tmp_path, "elsewhere", b"profile: max\npolicies: {EMAIL: redact}\n"), linked)
    a_folder = _repo(tmp_path, "a-folder", None)
    (a_folder / PROJECT_FILE).mkdir()
    folders = [
        str(strict),
        str(tmp_path / "away"),  # a drive not there now
        str(_repo(tmp_path, "broken", b"profile: [max\n")),
        str(a_folder),
        str(_repo(tmp_path, "large", b"#" * (256 * 1024 + 1))),
        str(linked),
    ]
    try:
        timings = []
        for _ in range(2):  # a cold start of the helper, then a warm one
            started = time.monotonic()
            outcome = repository_io.scan_folders(folders)
            timings.append(time.monotonic() - started)
            assert outcome.complete and outcome.failure is None
            # R23: capped where the system enforces it; macOS (no RLIMIT_AS there) says it has no cap, never a false one
            assert outcome.capped is (sys.platform != "darwin")
            kinds = [read.kind for read in outcome.results if read is not None]
            assert kinds == ["settings", "missing", "invalid", "nonregular", "nonregular", "settings"]
            assert outcome.results[0].settings.profile == "max"
            assert outcome.results[5].settings.policies == {"EMAIL": "redact"}  # read through the link
        assert all(seconds < repository_io.TOTAL_SECONDS for seconds in timings), timings
        print(f"real helper: cold {timings[0]:.2f}s, warm {timings[1]:.2f}s")  # recorded with -s
    finally:
        _unlink(linked)


def test_the_real_helper_starts_isolated_and_its_memory_is_capped():
    """The helper's own cap, set before it reads anything: past 1 GiB its allocations fail. On Windows, its job lets it
    start no process of its own. macOS enforces no RLIMIT_AS: there the helper claims no cap (R23), and is bounded by
    its deadlines alone."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); from latet.repository_child import limit_memory\n"
        "capped = limit_memory()\n"
        "if sys.platform == 'darwin':\n"
        "    print('no cap claimed' if capped is None else 'a cap claimed')\n"
        "    raise SystemExit(0)\n"
        "assert capped is True\n"
        "try:\n"
        "    block = bytearray(1536 * 1024 * 1024)\n"
        "except MemoryError:\n"
        "    print('capped')\n"
        "else:\n"
        "    print('not capped')\n"
        "if sys.platform == 'win32':\n"
        "    import subprocess\n"
        "    try:\n"
        "        subprocess.run([sys.executable, '-c', 'pass'], check=True, timeout=20)\n"
        "    except OSError:\n"
        "        print('no process')\n"
        "    else:\n"
        "        print('a process started')\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(SRC)], capture_output=True, text=True, timeout=60, check=False
    )
    lines = done.stdout.splitlines()
    assert done.returncode == 0, done.stderr[-400:]
    if sys.platform == "darwin":
        assert lines == ["no cap claimed"]
        return
    assert "capped" in lines
    assert sys.platform != "win32" or "no process" in lines


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS claims no cap: nothing to fail")
def test_a_cap_that_cannot_be_set_ends_the_helper_before_it_reads_and_says_memory_cap_failed(
    tmp_path, monkeypatch, real_repository_helper
):
    """Linux and Windows keep the cap: one that cannot be set ends the helper before it is ready (no unlimited
    fallback), and the check is incomplete in the words "memory cap failed"."""
    helper = tmp_path / "latet" / "repository_child.py"
    helper.parent.mkdir()
    source = Path(repository_io.__file__).with_name("repository_child.py").read_text("utf-8")
    failing = "def limit_memory() -> bool | None:\n    return False\n\n\ndef _limit_memory_unused"
    helper.write_text(source.replace("def limit_memory", failing, 1), "utf-8")
    monkeypatch.setattr(
        repository_io,
        "launch",
        lambda: subprocess.Popen(  # the helper's own code, its cap made to fail, started as the daemon starts it
            [sys.executable, "-I", "-B", str(helper)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=repository_io._child_environment(),
        ),
    )
    outcome = _reader().scan([str(tmp_path)])
    assert not outcome.complete and outcome.failure == "memory cap failed"


def test_the_helper_reads_nothing_when_another_latet_comes_first(tmp_path):
    """Started from a copy of its file whose own package is not the Latet that imports first, the helper stops before
    it says it is ready: it never runs another Latet's code on a repository's files."""
    helper = tmp_path / "latet" / "repository_child.py"
    helper.parent.mkdir()
    helper.write_bytes(Path(repository_io.__file__).with_name("repository_child.py").read_bytes())
    done = subprocess.run(
        [sys.executable, "-I", "-B", str(helper)],
        input=b"",
        capture_output=True,
        timeout=60,
        check=False,
        env=repository_io._child_environment(),
    )
    assert done.returncode == 2 and done.stdout == b""


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_the_helper_says_whether_it_is_capped_in_its_ready_frame(monkeypatch, platform):
    from latet import repository_child

    written = io.BytesIO()
    monkeypatch.setattr(repository_child, "limit_memory", lambda: None if platform == "darwin" else True)
    code = repository_child.run(io.BytesIO(), written, lambda: lambda folder: Remembered("missing"))
    assert code == 2  # no check sent: nothing read
    assert repository_child.read_frame(io.BytesIO(written.getvalue())) == {"ready": 1, "cap": platform != "darwin"}


def test_a_failed_cap_is_said_by_the_helper_exit_code(monkeypatch):
    from latet import repository_child

    written = io.BytesIO()
    monkeypatch.setattr(repository_child, "limit_memory", lambda: False)
    assert repository_child.run(io.BytesIO(), written, lambda: lambda folder: Remembered("missing")) == 3
    assert written.getvalue() == b""  # never ready, nothing read


# --- R20: what a repository's file holds never makes its check incomplete -----------------------------------------

RICH = (
    "profile: max\n"
    "pii: [IBAN]\n"
    "id_regions: [DE]\n"
    "phone_regions: [it]\n"
    "include_private_ips: true\n"
    "ner: auto\n"
    "terms:\n"
    "  - {value: Fonderia Brambilla \N{EM DASH} \N{CJK UNIFIED IDEOGRAPH-6771}\N{CJK UNIFIED IDEOGRAPH-4EAC}, type: CLIENT}\n"
    "recognizers:\n"
    "  - name: badge\n"
    "    pattern: 'B-[0-9]{6}'\n"
    "    since: 2020-01-01\n"
    "    examples: [B-123456]\n"
    "    nested: {flag: true, none: null, list: [1, 2.5, x]}\n"
    "policies: {EMAIL: redact}\n"
    "data: strict\n"
    "fields: [{name: 'note_*', type: NOTE}]\n"
    "dump_guard: 25\n"
    "images: mask\n"
    "ocr: auto\n"
    "llm_review: inline\n"
)


def test_settings_travel_through_the_real_helper_as_a_conversation_reads_them(tmp_path, real_repository_helper):
    from latet.projects import read_file

    root = _repo(tmp_path, "rich", RICH.encode("utf-8"))
    outcome = repository_io.scan_folders([str(root)])
    assert outcome.complete and outcome.results[0].kind == "settings"
    read = read_file(root / PROJECT_FILE)
    assert read.notices == [] and outcome.results[0].settings == read.repo
    assert read.repo.recognizers[0]["since"] == "2020-01-01"  # as JSON holds it, on both paths


@pytest.mark.parametrize(
    ("value", "left_out"),
    [
        pytest.param('!!binary "/w=="', True, id="bytes-that-are-not-text"),
        pytest.param("0x" + "f" * 4_000, True, id="an-integer-past-json"),
        pytest.param(".inf", False, id="infinity"),  # the model writes it as JSON's null: kept so, on both paths
    ],
)
def test_a_value_json_cannot_hold_is_left_out_on_both_paths_never_unavailable(
    tmp_path, real_repository_helper, value, left_out
):
    from latet.projects import read_file

    root = _repo(tmp_path, "odd", f"profile: max\nrecognizers:\n  - {{name: odd, extra: {value}}}\n".encode())
    outcome = repository_io.scan_folders([str(root)])
    assert outcome.complete and outcome.results[0].kind == "settings"
    read = read_file(root / PROJECT_FILE)
    assert read.repo is not None and read.repo.profile == "max"
    if left_out:
        assert [notice.key for notice in read.notices if notice.level == "error"] == ["recognizers"]
        assert read.repo.recognizers == []
    else:
        assert read.notices == [] and read.repo.recognizers == [{"name": "odd", "extra": None}]
    assert outcome.results[0].settings == read.repo


def test_a_file_of_more_yaml_than_a_project_file_holds_is_invalid_and_quick(tmp_path, real_repository_helper):
    """A valid 256 KiB file of thousands of small mappings would take seconds to parse: past its bound of YAML events
    it is not valid, on both paths, well within the helper's deadline (never a check incomplete by its content)."""
    from latet.projects import read_project_file

    lines = [f"k{index}: {{a: 1}}" for index in range(20_000)]
    raw = ("recognizers_note:\n" + "\n".join(f"  {line}" for line in lines) + "\n").encode()[: 256 * 1024]
    raw = raw[: raw.rfind(b"\n") + 1]
    root = _repo(tmp_path, "many", raw)
    started = time.monotonic()
    outcome = repository_io.scan_folders([str(root)])
    assert outcome.complete and outcome.results[0].kind == "invalid"
    assert time.monotonic() - started < repository_io.ITEM_SECONDS + 1.0  # the helper's start included
    settings, notices = read_project_file(root / PROJECT_FILE)
    assert settings is None and [notice.message for notice in notices] == [
        "not valid YAML: more than 20,000 YAML events, more than a project file holds: nothing in it applies"
    ]


def test_the_real_helper_is_this_interpreter_isolated_with_nothing_of_the_users_environment(monkeypatch):
    started = []

    class Recorded:
        def __init__(self, argv, **kwargs):
            started.append((argv, kwargs))
            raise OSError("not started in this test")

    monkeypatch.setattr(subprocess, "Popen", Recorded)
    monkeypatch.setenv("PYTHONPATH", "elsewhere")
    monkeypatch.setenv("LATET_SECRET_MARKER", "private")
    with pytest.raises(OSError, match="not started"):
        repository_io._launch()
    [(argv, kwargs)] = started
    assert argv[:3] == [sys.executable, "-I", "-B"]
    assert Path(argv[3]) == Path(repository_io.__file__).with_name("repository_child.py")
    assert kwargs["stderr"] is subprocess.DEVNULL and kwargs["cwd"] == str(Path(argv[3]).parent)
    assert set(kwargs["env"]) <= {"SystemRoot", "WINDIR"}
    assert "shell" not in kwargs or kwargs["shell"] is False


def test_a_start_and_a_reload_weigh_a_remembered_repository_through_the_real_helper(tmp_path, real_repository_helper):
    root = _repo(tmp_path, "strict")
    start = {"data": tmp_path / "data", "config_file": tmp_path / "config.yaml", "root_key": ROOT_KEY}
    approved = Config(identity=IdentityConfig(enabled=False))
    LocalSettings(start["config_file"], start["data"]).save_reviewed(approved)
    LocalRepositories(start["data"]).save([str(root)])
    save_config(approved.model_copy(update={"include_private_ips": False}), start["config_file"])
    rt = make_runtime(**start)
    try:
        [change] = rt.mediator.approvals.pending_changes()
        assert change["source"] == "start" and any("strict" in reason for reason in change["loosened"])
        assert rt.config == approved
    finally:
        rt.close()


# --- fakes: the place, the deadlines, the frames -----------------------------------------------------------------


class _Scripted:
    """A helper whose answers are these bytes, then its end; ``code``: how it exits."""

    def __init__(self, data: bytes, code: int = 0) -> None:
        self.stdout = io.BytesIO(data)
        self.stdin = io.BytesIO()
        self.code = code
        self.killed = False

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.killed = True


def _frames(*frames) -> bytes:
    return b"".join(encode(frame) for frame in frames)


READY = {"ready": 1, "cap": True}
GOOD = {"i": 0, "kind": "settings", "settings": {"profile": "max"}}


def _reader(**limits) -> RepositoryReader:
    return RepositoryReader(**{"ready": 2.0, "item": 1.0, "total": 4.0, "cleanup": 1.0, **limits})


def test_a_well_formed_answer_is_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(repository_io, "launch", lambda: _Scripted(_frames(READY, GOOD, {"end": 1})))
    outcome = _reader().scan([str(tmp_path)])
    assert outcome.complete and outcome.results[0].settings.profile == "max"


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(struct.pack(">I", FRAME_MOST + 1), id="a-frame-past-its-bound"),
        pytest.param(struct.pack(">I", 40) + b"{", id="a-frame-cut-short"),
        pytest.param(_frames(READY, {"i": 1, "kind": "missing"}, {"end": 1}), id="out-of-order"),
        pytest.param(_frames(READY, GOOD, GOOD, {"end": 1}), id="twice"),
        pytest.param(_frames(READY, {"i": 0, "kind": "fine"}, {"end": 1}), id="an-unknown-kind"),
        pytest.param(_frames(READY, {**GOOD, "path": "x"}, {"end": 1}), id="a-key-of-another-shape"),
        pytest.param(
            _frames(READY, {"i": 0, "kind": "settings", "settings": {"allow_values": 1}}, {"end": 1}),
            id="settings-not-valid",
        ),
        pytest.param(_frames(READY, {"i": 0, "kind": "missing", "settings": {}}, {"end": 1}), id="settings-of-nothing"),
        pytest.param(_frames(READY, GOOD, {"end": 2}), id="an-end-of-another-count"),
        pytest.param(_frames(READY, GOOD), id="no-end"),
        pytest.param(_frames({"ready": 2, "cap": True}, GOOD, {"end": 1}), id="another-protocol"),
        pytest.param(_frames({"ready": 1}, GOOD, {"end": 1}), id="ready-without-its-cap"),
        pytest.param(_frames({"ready": 1, "cap": "yes"}, GOOD, {"end": 1}), id="a-cap-of-another-shape"),
        pytest.param(_frames(READY, GOOD, {"end": 1})[:-1], id="its-end-cut-short"),
    ],
)
def test_frames_of_another_shape_leave_the_check_incomplete(tmp_path, monkeypatch, answer):
    monkeypatch.setattr(repository_io, "launch", lambda: _Scripted(answer))
    outcome = _reader().scan([str(tmp_path)])
    assert not outcome.complete and outcome.failure == "helper"


def test_a_frame_longer_than_its_bound_is_refused_before_it_is_read(tmp_path, monkeypatch):
    """A well-formed answer of more than 2 MiB (settings holding many terms) is refused by its length alone."""
    terms = [{"value": f"Term {index:07d}", "type": "PROJECT"} for index in range(60_000)]
    body = json.dumps({"i": 0, "kind": "settings", "settings": {"terms": terms}}).encode()
    assert len(body) > FRAME_MOST
    answer = _frames(READY) + struct.pack(">I", len(body)) + body + _frames({"end": 1})
    monkeypatch.setattr(repository_io, "launch", lambda: _Scripted(answer))
    outcome = _reader().scan([str(tmp_path)])
    assert not outcome.complete and outcome.failure == "helper"


def test_a_helper_that_fails_after_its_end_leaves_the_check_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(repository_io, "launch", lambda: _Scripted(_frames(READY, GOOD, {"end": 1}), code=1))
    outcome = _reader().scan([str(tmp_path)])
    assert not outcome.complete and outcome.results[0] is not None  # its answer kept, the check incomplete


def test_answers_past_their_budget_leave_the_check_incomplete(tmp_path, monkeypatch):
    answer = _frames(READY, GOOD, {"i": 1, "kind": "settings", "settings": {"profile": "max"}}, {"end": 2})
    monkeypatch.setattr(repository_io, "launch", lambda: _Scripted(answer))
    outcome = _reader(results_most=60).scan([str(tmp_path), str(tmp_path)])
    assert not outcome.complete and outcome.failure == "helper"


def test_a_folder_no_check_may_name_starts_no_helper(monkeypatch):
    started = []
    monkeypatch.setattr(repository_io, "launch", lambda: started.append(1))
    for folders in (["relative"], ["/x\x00y"], ["/" + "x" * 5000], ["/x"] * 2_001):
        outcome = _reader().scan(folders)
        assert not outcome.complete and started == []


def test_a_helper_that_never_answers_is_ended_at_its_ready_deadline(tmp_path, monkeypatch):
    children = []

    def silent():
        child = subprocess.Popen(  # a process that never answers
            [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        children.append(child)
        return child

    monkeypatch.setattr(repository_io, "launch", silent)
    reader = _reader(ready=0.5, cleanup=2.0)
    started = time.monotonic()
    outcome = reader.scan([str(tmp_path)])
    elapsed = time.monotonic() - started
    try:
        assert not outcome.complete and outcome.failure == "timeout"
        assert elapsed < 0.5 + 1.5, elapsed  # killed at its deadline, not after the cleanup allowance
        assert children[0].poll() is not None  # killed and reaped
        monkeypatch.setattr(repository_io, "launch", lambda: _Scripted(_frames(READY, GOOD, {"end": 1})))
        assert reader.scan([str(tmp_path)]).complete  # its place free again
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(5)


STALLS_AFTER_READY = (
    "import json, struct, sys, time\n"
    "body = json.dumps({'ready': 1, 'cap': True}).encode()\n"
    "sys.stdout.buffer.write(struct.pack('>I', len(body)) + body); sys.stdout.buffer.flush()\n"
    "size = struct.unpack('>I', sys.stdin.buffer.read(4))[0]; sys.stdin.buffer.read(size)\n"
    "time.sleep(60)\n"
)


def test_a_real_helper_stalled_on_a_folder_is_killed_at_the_folders_deadline(tmp_path, monkeypatch):
    children = []

    def stalls():
        child = subprocess.Popen(  # ready, takes the check, then never answers: a read stuck on a share
            [sys.executable, "-I", "-c", STALLS_AFTER_READY],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        children.append(child)
        return child

    monkeypatch.setattr(repository_io, "launch", stalls)
    started = time.monotonic()
    outcome = _reader(item=0.5, cleanup=2.0).scan([str(tmp_path)])
    try:
        assert not outcome.complete and outcome.failure == "timeout"
        assert time.monotonic() - started < 2.0, (
            "not killed at the folder's deadline"
        )  # never after the cleanup allowance
        assert children[0].poll() is not None  # killed and reaped at the folder's deadline
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(5)


def test_a_frame_length_is_checked_before_any_of_its_bytes_are_read():
    from latet.repository_child import ProtocolError, read_frame

    class Guarded(io.BytesIO):
        def read(self, size=-1):
            assert size is not None and 0 <= size <= FRAME_MOST, "a frame read past its bound"
            return super().read(size)

    with pytest.raises(ProtocolError):
        read_frame(Guarded(struct.pack(">I", FRAME_MOST + 1) + b"x" * 64))


def test_a_folder_past_its_deadline_ends_the_check_and_its_helper_keeps_the_place_until_it_ends(tmp_path, monkeypatch):
    """The second folder stalls (a share that does not answer): the check stops at its deadline, incomplete, the first
    folder's answer kept. A helper not ended yet keeps the one place: every check meanwhile is answered at once, with no
    helper started; once it ends, the place is free."""
    stalled, release = threading.Event(), threading.Event()
    launched = []

    def read_one(folder):
        if folder.endswith("stalls"):
            stalled.set()
            release.wait(30)
        return Remembered("missing", why="FileNotFoundError")

    def launch():
        launched.append(1)
        return InProcessChild(read_one)

    monkeypatch.setattr(repository_io, "launch", launch)
    reader = _reader(item=0.3, cleanup=0.3)
    started = time.monotonic()
    outcome = reader.scan([str(tmp_path / "first"), str(tmp_path / "stalls"), str(tmp_path / "third")])
    try:
        assert time.monotonic() - started < 0.3 + 0.3 + 2.0
        assert stalled.is_set() and not outcome.complete and outcome.failure == "timeout"
        assert outcome.results[0] is not None and outcome.results[1:] == (None, None)
        quick = time.monotonic()
        for _ in range(100):
            assert reader.scan([str(tmp_path / "first")]).failure == "busy"
        assert time.monotonic() - quick < 1.0 and launched == [1]  # none started meanwhile, none waited for
    finally:
        release.set()
    deadline = time.monotonic() + 10
    while reader.scan([str(tmp_path / "first")]).failure == "busy" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert reader.scan([str(tmp_path / "first")]).complete and len(launched) >= 2


def test_the_whole_check_ends_at_its_deadline_whatever_each_folder_takes(tmp_path, monkeypatch):
    def read_one(folder):
        time.sleep(0.2)
        return Remembered("missing", why="FileNotFoundError")

    monkeypatch.setattr(repository_io, "launch", lambda: InProcessChild(read_one))
    reader = _reader(item=0.5, total=1.0, cleanup=0.5)
    started = time.monotonic()
    outcome = reader.scan([str(tmp_path / f"f{index}") for index in range(20)])
    assert not outcome.complete and outcome.failure == "timeout"
    assert time.monotonic() - started < 1.0 + 0.5 + 1.0
    assert 0 < sum(read is not None for read in outcome.results) < 20


def test_a_second_check_while_one_runs_is_answered_at_once(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def read_one(folder):
        entered.set()
        release.wait(10)
        return Remembered("missing", why="FileNotFoundError")

    monkeypatch.setattr(repository_io, "launch", lambda: InProcessChild(read_one))
    reader = _reader(item=5.0, total=8.0)
    first = []
    runner = threading.Thread(target=lambda: first.append(reader.scan([str(tmp_path)])))
    runner.start()
    try:
        assert entered.wait(5)
        quick = time.monotonic()
        assert reader.scan([str(tmp_path)]).failure == "busy"
        assert time.monotonic() - quick < 0.5
    finally:
        release.set()
        runner.join(10)
    assert first and first[0].complete


def test_frames_on_the_wire_hold_no_folder(tmp_path):
    """The helper answers by place: no path travels back, nor in a log line it writes (it writes none)."""
    from latet.repository_child import item

    secret = tmp_path / "payroll-private"
    frame = item(0, Remembered("missing", why="FileNotFoundError"))
    assert "payroll" not in json.dumps(frame) and set(frame) == {"i", "kind", "why"}
    assert str(secret) not in json.dumps(item(0, Remembered("unavailable", why="PermissionError")))
