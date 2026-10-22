"""EC5/EC6 record contracts; prepared before dependency integration, never an empty-store fallback."""

import importlib
import json
from pathlib import Path

import pytest

from latet.config import data_dir
from latet.integrations import base

MARKER = "planted_record_content_9741"
BYTE_LIMIT = 16 * 1024 * 1024


def _row(**changes):
    row = {"integration": "claude", "applied_at": 123.0, "files": [], "data": {}}
    row.update(changes)
    return {"claude": row}


def _manifest(raw):
    path = base.manifest_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def _refused_without_rewriting(raw):
    path = _manifest(raw)
    # A malformed store must raise a controlled error, never return {} or drop a bad row.
    with pytest.raises(Exception, match=r"(?i)record|integration|unavailable") as caught:
        base.load_records()
    assert not isinstance(caught.value, (json.JSONDecodeError, UnicodeError, TypeError, RecursionError))
    words = str(caught.value).lower()
    assert any(word in words for word in ("record", "integration", "unavailable"))
    assert MARKER not in str(caught.value)
    assert path.read_bytes() == raw


def test_missing_record_store_is_absent_without_creating_a_folder(computer):
    assert not data_dir().exists()
    assert base.load_records() == {}
    assert not data_dir().exists()


@pytest.mark.parametrize("extra", [0, 1], ids=["exact-16MiB", "over-16MiB"])
def test_record_byte_budget_includes_trailing_whitespace(computer, extra):
    raw = b"{}" + b" " * (BYTE_LIMIT - 2 + extra)
    if extra:
        _refused_without_rewriting(raw)
    else:
        _manifest(raw)
        assert base.load_records() == {}


@pytest.mark.parametrize("nodes", [100_000, 100_001])
def test_record_node_budget_counts_containers_and_scalar_values(computer, nodes):
    # Seven non-item nodes: root, record, integration, timestamp, files, data, items.
    raw = json.dumps(_row(data={"items": [0] * (nodes - 7)})).encode()
    if nodes > 100_000:
        _refused_without_rewriting(raw)
    else:
        _manifest(raw)
        assert len(base.load_records()["claude"].data["items"]) == 99_993


@pytest.mark.parametrize("depth", [32, 33])
def test_record_container_nesting_budget(computer, depth):
    # Root, record and data are the first three containers; object keys are not nodes.
    nested = 0
    for _ in range(depth - 3):
        nested = [nested]
    raw = json.dumps(_row(data={"nested": nested})).encode()
    if depth > 32:
        _refused_without_rewriting(raw)
    else:
        _manifest(raw)
        assert base.load_records()["claude"].integration == "claude"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"claude":{"integration":"claude","applied_at":1},"claude":{"integration":"claude","applied_at":2}}',
        b'{"claude":{"integration":"claude","applied_at":1,"data":{"x":1,"x":2}}}',
        b"[]",
        b'{"claude":null}',
        b'{"claude":{"integration":"codex","applied_at":1}}',
        json.dumps(_row(applied_at=float("nan"))).encode(),
        json.dumps(_row(applied_at=float("inf"))).encode(),
        json.dumps(_row(applied_at=True)).encode(),
        json.dumps(_row(files={})).encode(),
        json.dumps(_row(files=[None])).encode(),
        json.dumps(_row(files=[{"path": 3, "backup": None, "sha_before": None, "sha_after": None}])).encode(),
        json.dumps(_row(data=[])).encode(),
        ('{"claude":{"integration":"' + MARKER + '","applied_at":1}}').encode(),
        b"\xff",
        b'{"claude":',
    ],
    ids=[
        "duplicate-record",
        "duplicate-nested-key",
        "top-array",
        "null-row",
        "id-mismatch",
        "nan-time",
        "infinite-time",
        "boolean-time",
        "files-object",
        "null-edit",
        "nonstring-path",
        "data-array",
        "content-marker",
        "invalid-utf8",
        "truncated-json",
    ],
)
def test_malformed_record_store_is_controlled_and_preserved(computer, raw):
    _refused_without_rewriting(raw)


def test_legacy_record_without_previous_still_reconstructs_prior_environment(computer):
    from latet.integrations.agents import ClaudeCode

    backup = computer / "prior-settings.json"
    backup.write_text('{"env":{"ANTHROPIC_BASE_URL":"https://prior.example.test"}}', "utf-8")
    raw = json.dumps(
        _row(
            files=[
                {
                    "path": str(ClaudeCode().settings_path()),
                    "backup": str(backup),
                    "sha_before": None,
                    "sha_after": None,
                }
            ]
        )
    ).encode()
    _manifest(raw)
    assert ClaudeCode().original_env() == {"ANTHROPIC_BASE_URL": "https://prior.example.test"}


def test_directory_in_place_of_manifest_is_a_controlled_refusal(computer):
    base.manifest_path().mkdir(mode=0o700, parents=True)
    with pytest.raises(Exception, match=r"(?i)record|integration|unavailable") as caught:
        base.load_records()
    assert not isinstance(caught.value, OSError)
    assert base.manifest_path().is_dir()


@pytest.mark.parametrize(
    "reader", ["load", "claude-prior", "codex-prior", "claude-revert", "codex-revert", "opencode-revert"]
)
def test_disconnected_unsafe_store_is_refused_before_content_access(computer, monkeypatch, reader):
    from latet.integrations.agents import ClaudeCode, Codex, OpenCode
    from latet.storage import folders

    path = _manifest(json.dumps(_row()).encode())
    # This tests propagation of the existing platform helper's refusal, not platform ACL correctness.
    monkeypatch.setattr(folders, "private_folder_problem", lambda folder: "folder open to another account")
    if hasattr(base, "private_folder_problem"):
        monkeypatch.setattr(base, "private_folder_problem", folders.private_folder_problem)
    original = Path.open

    def checked_open(self, *args, **kwargs):
        assert self != path, "untrusted manifest content was opened"
        return original(self, *args, **kwargs)

    descriptor_open = base.os.open

    def checked_descriptor_open(name, *args, **kwargs):
        candidate = None
        if isinstance(name, (Path, str, bytes)):
            try:
                candidate = Path(base.os.fsdecode(name))
            except (TypeError, ValueError):
                return descriptor_open(name, *args, **kwargs)
        assert candidate != path, "untrusted manifest record content was opened by descriptor"
        return descriptor_open(name, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    monkeypatch.setattr(base.os, "open", checked_descriptor_open)
    methods = {
        "load": base.load_records,
        "claude-prior": ClaudeCode().original_env,
        "codex-prior": Codex().original_texts,
        "claude-revert": ClaudeCode().revert,
        "codex-revert": Codex().revert,
        "opencode-revert": OpenCode().revert,
    }
    with pytest.raises(Exception, match=r"(?i)folder|record") as caught:
        methods[reader]()
    assert not isinstance(caught.value, AssertionError), "the guard ran after content access"
    assert "folder" in str(caught.value).lower() or "record" in str(caught.value).lower()
    assert not (data_dir() / "connection.yaml").exists()


def test_recreated_record_roots_after_quarantine_are_reported_from_metadata_only(computer, monkeypatch):
    # These direct-reader functions join only with reviewed Task 5; execute after its dependency gate.
    command = importlib.import_module("latet.cli.connect")
    manifest = _manifest(json.dumps(_row()).encode())
    backups = data_dir() / "backups"
    backups.mkdir()
    (backups / "old.json").write_text(MARKER, "utf-8")
    command._quarantine_records()
    manifest.write_text(MARKER, "utf-8")
    backups.mkdir()
    (backups / "recreated.json").write_text(MARKER, "utf-8")
    protected = [
        manifest,
        backups,
        manifest.with_name("integrations.json.untrusted"),
        backups.with_name("backups.untrusted"),
    ]
    read_regular, rglob = command._read_regular, Path.rglob

    def checked_read(path):
        assert not any(path == root or path.is_relative_to(root) for root in protected)
        return read_regular(path)

    def checked_rglob(self, *args, **kwargs):
        assert not any(self == root or self.is_relative_to(root) for root in protected)
        return rglob(self, *args, **kwargs)

    monkeypatch.setattr(command, "_read_regular", checked_read)
    monkeypatch.setattr(Path, "rglob", checked_rglob)
    left = command._still_holding([], [], [], unsafe=True)
    assert {manifest, backups} <= {path for path, _ in left}
    assert all(MARKER not in reason for _, reason in left)


def test_existing_quarantine_destination_preserves_both_versions(computer, capsys):
    command = importlib.import_module("latet.cli.connect")
    manifest = _manifest(b"original untrusted content")
    destination = manifest.with_name("integrations.json.untrusted")
    destination.write_bytes(b"older quarantined content")
    # A controlled retained result may be returned or raised; an OS error cannot escape raw.
    failure = None
    result = None
    try:
        result = command._quarantine_records()
    except Exception as exc:
        failure = exc
    assert not isinstance(failure, OSError)
    output = capsys.readouterr()
    words = str(failure).lower() if failure is not None else (output.out + output.err).lower()
    assert result is not None or any(word in words for word in ("record", "quarantin", "retain"))
    assert manifest.read_bytes() == b"original untrusted content"
    assert destination.read_bytes() == b"older quarantined content"
