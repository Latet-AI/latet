"""T6.20 consumer preparation: real callers, synthetic Windows metadata, no native ACL claim.

Only owner/grant metadata is doubled. Connection parsing, credential dispatch, pin selection, wrapper/apply and
session consumers remain real. These tests are unexecuted preparation until reviewed computer dependencies and the
coordinator's exclusive runtime slot. Darwin's selected-mode case is explicitly a dispatch simulation.
"""

import hashlib
import json
import os
import ssl
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest

from latet import wrappers
from latet.config import Config, data_dir
from latet.engine.reviewer import certificate as certificates
from latet.integrations import agents, base
from latet.server import client, connection
from latet.storage import folders

from . import test_agents_connected as existing
from .test_agents_connected import PROXY_AUTHORITY, SPACE, TOKEN

# Reuse test-owned homes, real connection/pin setup and the fake installed-plugin fixture.
_agents_here = existing._agents_here
claude_home = existing.claude_home
connected = existing.connected

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows metadata dispatch simulation")


@pytest.fixture
def namespace_metadata(monkeypatch):
    """A trusted chain except the explicitly named component: no host ACL is changed or inspected."""
    unsafe: set[Path] = set()

    def grants(path):
        allowed = [folders.Grant("S-1-1-0", 0x100, False)] if Path(path) in unsafe else []
        return "task6-current-user", allowed

    monkeypatch.setattr(folders, "my_sid", lambda: "task6-current-user")
    monkeypatch.setattr(folders, "folder_grants", grants)
    return unsafe


def _forbid_content(monkeypatch, *paths: Path) -> None:
    """Trap current Path reads and fd-based bounded-reader replacements at the same content boundary."""
    selected = set(paths)
    path_open, fd_open = Path.open, os.open

    def opened(path, *args, **kwargs):
        if Path(path) in selected:
            pytest.fail("content was opened before namespace admission")
        return path_open(path, *args, **kwargs)

    def opened_fd(path: str | os.PathLike[str], *args, **kwargs):
        if Path(path) in selected:
            pytest.fail("content was opened before namespace admission")
        return fd_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(os, "open", opened_fd)


def _forbid_creation(monkeypatch, root: Path) -> None:
    mkdir = Path.mkdir

    def made(path, *args, **kwargs):
        if path == root or root in path.parents:
            pytest.fail("a read or refused writer attempted directory creation")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", made)


def _no_sensitive_work(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("sensitive consumer ran before namespace admission")


def _selected_elsewhere(computer: Path, monkeypatch) -> Path:
    path = computer / "selected-parent" / "private" / "connection.yaml"
    path.parent.mkdir(parents=True)
    path.write_bytes(connection.connection_path().read_bytes())
    monkeypatch.setenv("LATET_CONNECTION", str(path))
    return path


def test_t620_n01_missing_connection_read_is_noncreating(computer, namespace_metadata, monkeypatch):
    missing = data_dir()
    assert not missing.exists()
    _forbid_creation(monkeypatch, missing)
    assert connection.connection_folder_problem() is None
    assert connection.load_connection() is None
    assert not missing.exists()


def test_t620_n02_selected_connection_ancestor_precedes_yaml_content(
    connected, computer, namespace_metadata, monkeypatch
):
    selected = _selected_elsewhere(computer, monkeypatch)
    namespace_metadata.add(selected.parent.parent)
    _forbid_content(monkeypatch, selected)
    with pytest.raises(connection.NotConnected):
        connection.load_connection()


def test_t620_n03_safe_selected_missing_connection_writer_creates_and_loads(
    connected, computer, namespace_metadata, monkeypatch
):
    checked = connection.load_connection()
    assert checked is not None
    selected = computer / "safe-new" / "private" / "connection.yaml"
    monkeypatch.setenv("LATET_CONNECTION", str(selected))
    assert connection.save_connection(checked) == selected
    assert connection.load_connection() == checked


def test_t620_n04_unsafe_selected_missing_writer_creates_nothing(connected, computer, namespace_metadata, monkeypatch):
    checked = connection.load_connection()
    assert checked is not None
    parent = computer / "unsafe-writer"
    parent.mkdir()
    namespace_metadata.add(parent)
    selected = parent / "new-private" / "connection.yaml"
    monkeypatch.setenv("LATET_CONNECTION", str(selected))
    _forbid_creation(monkeypatch, selected.parent)
    with pytest.raises(connection.NotConnected):
        connection.save_connection(checked)
    assert not selected.parent.exists()


@pytest.mark.parametrize("keyring", ["on", "off"], ids=["keyring", "token-file"])
def test_t620_n05_token_admission_precedes_either_credential_source(
    connected, namespace_metadata, monkeypatch, keyring
):
    checked = connection.load_connection()
    assert checked is not None
    namespace_metadata.add(data_dir().parent)
    monkeypatch.setenv("LATET_KEYRING", keyring)
    monkeypatch.setattr(connection, "_keyring", _no_sensitive_work)
    monkeypatch.setattr(connection, "_read_private", _no_sensitive_work)
    with pytest.raises(connection.NotConnected):
        connection.load_server_token(data_dir(), checked)


def test_t620_n06_token_writer_precedes_keyring_mutation(connected, namespace_metadata, monkeypatch):
    checked = connection.load_connection()
    assert checked is not None
    namespace_metadata.add(data_dir().parent)
    monkeypatch.setenv("LATET_KEYRING", "on")
    monkeypatch.setattr(connection, "_keyring", _no_sensitive_work)
    monkeypatch.setattr(connection, "write_atomic", _no_sensitive_work)
    with pytest.raises(connection.NotConnected):
        connection.save_server_token(data_dir(), checked, TOKEN)


def test_t620_n07_wrapper_checks_selected_parent_before_token_and_ca(
    connected, computer, namespace_metadata, monkeypatch
):
    selected = _selected_elsewhere(computer, monkeypatch)
    namespace_metadata.add(selected.parent.parent)
    monkeypatch.setattr(connection, "load_server_token", _no_sensitive_work)
    monkeypatch.setattr(wrappers, "_their_certificates", _no_sensitive_work)
    with pytest.raises(connection.NotConnected):
        wrappers.claude_env()


@pytest.mark.parametrize("consumer", ["certificate-file", "agent-pin", "wrapper-tls"])
def test_t620_n08_selected_certificate_child_is_admitted_before_content(
    connected, namespace_metadata, monkeypatch, consumer
):
    checked = connection.load_connection()
    assert checked is not None and checked.fingerprint is not None
    folder = data_dir() / "certificates"
    namespace_metadata.add(folder)
    _forbid_content(monkeypatch, folder / f"{checked.fingerprint.removeprefix('sha256:')}.pem")
    try:
        if consumer == "certificate-file":
            assert connection.server_certificate_file(checked) is None
        elif consumer == "agent-pin":
            assert connection.pin_for_agents(checked) == (None, "missing")
        else:
            # Existing TLS refusal semantics permit an empty trust context for an unavailable requested pin.
            accepted: list[str | None] = []
            context = certificates.context

            def recorded(pem):
                accepted.append(pem)
                return context(pem)

            monkeypatch.setattr(certificates, "context", recorded)
            wrappers.tls()
            assert accepted == [None]
    except (connection.NotConnected, client.ServerError):
        pass  # Controlled refusal is also valid; the content trap must never fire.


def test_t620_n09_bundle_output_admission_precedes_settings_and_ca_reads(
    connected, claude_home, namespace_metadata, monkeypatch
):
    output = connection.connection_dir()
    output.mkdir()
    namespace_metadata.add(output)
    monkeypatch.setattr(wrappers, "_claude_settings_certificates", _no_sensitive_work)
    monkeypatch.setattr(connection, "_read_theirs", _no_sensitive_work)
    monkeypatch.setattr(connection, "write_atomic", _no_sensitive_work)
    with pytest.raises(connection.NotConnected):
        wrappers.extra_ca_env("claude")


def test_t620_n10_apply_checks_selected_parent_before_route_backup_and_plugin(
    connected, claude_home, computer, namespace_metadata, monkeypatch
):
    selected = _selected_elsewhere(computer, monkeypatch)
    namespace_metadata.add(selected.parent.parent)
    monkeypatch.setattr(agents.ClaudeCode, "route", _no_sensitive_work)
    monkeypatch.setattr(agents.ClaudeCode, "prepare_marketplace", _no_sensitive_work)
    monkeypatch.setattr(agents, "backup", _no_sensitive_work)
    monkeypatch.setattr(agents, "write_atomic", _no_sensitive_work)
    with pytest.raises(connection.NotConnected):
        agents.ClaudeCode().apply(config=Config())
    assert not (claude_home / "settings.json").exists()


def test_t620_n11_session_output_admission_precedes_supplied_settings_read(
    connected, computer, namespace_metadata, monkeypatch
):
    output = data_dir() / "claude-settings"
    output.mkdir()
    namespace_metadata.add(output)
    supplied = computer / "session-settings.json"
    supplied.write_text('{"permissions": {"allow": []}}', encoding="utf-8")
    _forbid_content(monkeypatch, supplied)
    monkeypatch.setattr(wrappers, "write_atomic", _no_sensitive_work)
    with pytest.raises(connection.NotConnected):
        wrappers.claude_settings_file({"LATET_BRIDGE_TOKEN": TOKEN}, ["--settings", str(supplied)])
    assert list(output.iterdir()) == []


def test_t620_n12_safe_session_keeps_settings_and_private_route(connected, computer, namespace_metadata):
    path, args = wrappers.claude_settings_file(
        {"LATET_BRIDGE_TOKEN": TOKEN, "LATET_BRIDGE_URL": SPACE},
        ['--settings={"permissions":{"allow":["Read"]}}', "hello"],
    )
    saved = json.loads(path.read_text("utf-8"))
    assert saved["permissions"] == {"allow": ["Read"]}
    assert saved["env"] == {"LATET_BRIDGE_TOKEN": TOKEN, "LATET_BRIDGE_URL": SPACE}
    assert args == ["hello"] and path.parent == data_dir() / "claude-settings"


def test_t620_n13_followed_corporate_ca_keeps_public_input_semantics(
    connected, computer, namespace_metadata, monkeypatch
):
    public = computer / "public-ca"
    public.mkdir()
    authority = public / "authority.pem"
    authority.write_text(PROXY_AUTHORITY, encoding="utf-8")
    linked = computer / "corporate-ca.pem"
    try:
        linked.symlink_to(authority)
    except OSError:
        pytest.skip("the owned fixture cannot create a symbolic link")
    # An accepted operator-selected input is not a Latet-owned private record directory.
    namespace_metadata.add(public)
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(linked))
    bundle = Path(wrappers.extra_ca_env("opencode")["NODE_EXTRA_CA_CERTS"])
    assert bundle.read_text("utf-8") == PROXY_AUTHORITY + connected


@pytest.mark.parametrize(
    ("mode", "admitted"),
    [(0o700, True), (0o755, True), (0o775, False)],
    ids=["private-positive", "read-only-tightened", "group-writable-refused"],
)
def test_t620_n14_darwin_selected_mode_connection_consumer_simulation(
    connected, namespace_metadata, monkeypatch, mode, admitted
):
    """Selected-mode consumer simulation with no ALLOW entries; no native Darwin ACL/ABI claim. R2: a data folder
    nobody else could write into (0755, as an older Latet made it) is tightened to 0700 through its bound descriptor,
    then admitted; one its group may write into is refused, never repaired."""
    selected = data_dir()
    original_stat, original_lstat = os.stat, os.lstat
    original_metadata = base._metadata
    current = {"mode": mode}
    descriptors: dict[int, Path] = {}
    chmods: list[tuple[Path, int]] = []

    def observed(path, found):
        if found is None or not stat.S_ISDIR(found.st_mode):
            return found
        fields = {name: getattr(found, name) for name in dir(found) if name.startswith("st_")}
        fields.update(st_mode=stat.S_IFDIR | (current["mode"] if Path(path) == selected else 0o700), st_uid=0)
        return SimpleNamespace(**fields)

    def opened(path, flags, *args, **kwargs):
        descriptors[60000 + len(descriptors)] = Path(path)
        return 60000 + len(descriptors) - 1

    def changed(descriptor, wanted):
        chmods.append((descriptors[descriptor], wanted))
        if descriptors[descriptor] == selected:
            current["mode"] = wanted

    simulated_os = SimpleNamespace(**vars(os))
    simulated_os.getuid = lambda: 0
    simulated_os.stat = lambda path, *a, **kw: observed(path, original_stat(path, *a, **kw))
    simulated_os.lstat = lambda path, *a, **kw: observed(path, original_lstat(path, *a, **kw))
    simulated_os.open = opened
    simulated_os.fstat = lambda descriptor: observed(descriptors[descriptor], original_stat(descriptors[descriptor]))
    simulated_os.fchmod = changed
    simulated_os.close = lambda descriptor: descriptors[descriptor] and None
    monkeypatch.setattr(folders, "os", simulated_os)
    monkeypatch.setattr(folders, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(folders, "_darwin_allows", lambda folder, found: [])
    monkeypatch.setattr(base, "_metadata", lambda path: observed(path, original_metadata(path)))
    if admitted:
        assert connection.load_connection() is not None
        assert chmods == ([] if mode == 0o700 else [(selected, 0o700)])
    else:
        _forbid_content(monkeypatch, connection.connection_path())
        with pytest.raises(connection.NotConnected, match="chmod 700"):
            connection.load_connection()
        assert chmods == []


def test_t620_n15_reselection_cannot_admit_one_connection_through_another_parent(
    connected, computer, namespace_metadata, monkeypatch
):
    trusted = connection.connection_path()
    selected = _selected_elsewhere(computer, monkeypatch)
    namespace_metadata.add(selected.parent.parent)
    # Change the real selector input during metadata inspection, without changing any metadata or trust result.
    original_stat, original_lstat = Path.stat, Path.lstat

    def metadata(reader, path, *args, **kwargs):
        found = reader(path, *args, **kwargs)
        if path in (selected, selected.parent.parent):
            monkeypatch.setenv("LATET_CONNECTION", str(trusted))
        return found

    monkeypatch.setattr(Path, "stat", lambda path, *a, **kw: metadata(original_stat, path, *a, **kw))
    monkeypatch.setattr(Path, "lstat", lambda path, *a, **kw: metadata(original_lstat, path, *a, **kw))
    _forbid_content(monkeypatch, selected)
    with pytest.raises(connection.NotConnected):
        connection.load_connection()
    assert os.environ["LATET_CONNECTION"] == str(trusted)


def test_t620_n16_unavailable_connection_metadata_is_not_a_missing_connection(
    connected, computer, namespace_metadata, monkeypatch
):
    """R1: an absent connection file is no connection, whatever its folders are (nothing of them is admitted); a name
    whose metadata cannot be read is not taken for an absent one, and the admission says why."""
    parent = computer / "unavailable-metadata"
    parent.mkdir()
    selected = parent / "connection.yaml"
    monkeypatch.setenv("LATET_CONNECTION", str(selected))
    _forbid_creation(monkeypatch, parent)
    assert connection.load_connection() is None  # True absence beneath an observable, trusted chain.
    original_lstat = Path.lstat

    def metadata(path, *args, **kwargs):
        if path in (parent, selected):
            raise PermissionError("test-owned metadata is unavailable")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", metadata)
    _forbid_content(monkeypatch, selected)
    with pytest.raises(connection.NotConnected):
        connection.load_connection()


def test_t620_n17_keep_rejects_before_cleanup_and_preserves_existing_credentials(
    connected, computer, namespace_metadata, keyring_on, monkeypatch
):
    """Postimplementation regression witness: composite keep rejection, not an earlier observed RED."""
    data, original = data_dir(), connection.connection_path()
    checked = connection.load_connection()
    assert checked is not None
    token_file = data / "server.token"
    token_before, yaml_before = token_file.read_bytes(), original.read_bytes()
    keyring_on.set_password("latet-server", "server-token", token_before.decode("utf-8"))
    credentials_before = dict(keyring_on.kept)
    unsafe = computer / "keep-unsafe"
    unsafe.mkdir()
    selected = unsafe / "new-private" / "connection.yaml"
    namespace_metadata.add(unsafe)
    monkeypatch.setenv("LATET_CONNECTION", str(selected))
    credential_calls, cleanup_calls = [], []
    keyring, remove = connection._keyring, connection.remove_server_token

    def credential_source():
        credential_calls.append(True)
        return keyring()

    def cleanup(folder):
        cleanup_calls.append(folder)
        return remove(folder)

    monkeypatch.setattr(connection, "_keyring", credential_source)
    monkeypatch.setattr(connection, "remove_server_token", cleanup)
    _forbid_creation(monkeypatch, selected.parent)
    with pytest.raises(connection.NotConnected):
        connection.keep_connection(checked, "latet_pat_" + "n" * 43)
    assert cleanup_calls == [] and credential_calls == []
    assert keyring_on.kept == credentials_before and token_file.read_bytes() == token_before
    assert original.read_bytes() == yaml_before and not selected.parent.exists()


def test_t620_n18_keep_retains_one_snapshot_after_token_persistence_changes_selection(
    connected, computer, namespace_metadata, monkeypatch
):
    """Postimplementation regression witness: actual token write precedes selector change, with no helper mock."""
    data_a, path_a = data_dir(), connection.connection_path()
    checked = connection.load_connection()
    assert checked is not None
    data_b = computer / "other-connection-state"
    path_b = data_b / "connection.yaml"
    monkeypatch.setenv("LATET_DATA_DIR", str(data_b))
    monkeypatch.setenv("LATET_CONNECTION", str(path_b))
    other = checked.model_copy(update={"url": "https://other.latet.lan", "user_name": "Other selected state"})
    assert connection.save_server_token(data_b, other, "latet_pat_" + "b" * 43) == "file"
    connection.save_connection(other)
    b_token, b_yaml = (data_b / "server.token").read_bytes(), path_b.read_bytes()
    monkeypatch.setenv("LATET_DATA_DIR", str(data_a))
    monkeypatch.setenv("LATET_CONNECTION", str(path_a))
    replacement = checked.model_copy(update={"user_name": "Captured connection state A"})
    token = "latet_pat_" + "a" * 43
    write, switched = connection.write_atomic, []

    def persisted(path, *args, **kwargs):
        result = write(path, *args, **kwargs)
        if path == data_a / "server.token":
            switched.append(path)
            monkeypatch.setenv("LATET_DATA_DIR", str(data_b))
            monkeypatch.setenv("LATET_CONNECTION", str(path_b))
        return result

    monkeypatch.setattr(connection, "write_atomic", persisted)
    assert connection.keep_connection(replacement, token) == "file"
    assert switched == [data_a / "server.token"]
    assert os.environ["LATET_DATA_DIR"] == str(data_b) and os.environ["LATET_CONNECTION"] == str(path_b)
    assert (data_b / "server.token").read_bytes() == b_token and path_b.read_bytes() == b_yaml
    with monkeypatch.context() as selected_a:
        selected_a.setenv("LATET_DATA_DIR", str(data_a))
        selected_a.setenv("LATET_CONNECTION", str(path_a))
        assert connection.load_connection() == replacement
        assert connection.load_server_token(data_a, replacement) == token


def test_t620_n19_successfully_checked_pin_is_not_reopened_by_tls(connected, computer, namespace_metadata, monkeypatch):
    """A real accepted pin survives pathname replacement as checked bytes, or is refused before TLS use."""
    from .test_connection import _handshake

    checked = connection.load_connection()
    assert checked is not None and checked.fingerprint is not None
    host = urlsplit(checked.url).hostname
    assert host is not None
    crt, key = computer / "tls" / "server.crt", computer / "tls" / "server.key"
    ordinary = wrappers.tls()
    assert ordinary is not None
    _handshake(ordinary, crt, key, host)  # An actual usable positive before the replacement/refusal observation.
    pin = data_dir() / "certificates" / f"{checked.fingerprint.removeprefix('sha256:')}.pem"
    pinned, load_trust = client._pinned, ssl.SSLContext.load_verify_locations
    accepted, used = [], []

    def checked_then_replaced(fingerprint, **kwargs):
        pem, problem = pinned(fingerprint, **kwargs)  # Real saved-file read, fingerprint check and authority check.
        assert (pem, problem) == (connected, None)
        accepted.append(fingerprint)
        pin.write_text(PROXY_AUTHORITY, encoding="utf-8")
        _forbid_content(monkeypatch, pin)
        return pem, problem

    def loaded(context, cafile=None, capath=None, cadata=None):
        # Observe the actual OpenSSL loading boundary, including native cafile opens invisible to Path.open traps.
        assert cafile is None and capath is None, "TLS reopened a pathname after accepting checked PEM"
        assert cadata == connected, "TLS consumed bytes other than the successfully checked pin"
        used.append(cadata)
        return load_trust(context, cafile=cafile, capath=capath, cadata=cadata)

    monkeypatch.setattr(client, "_pinned", checked_then_replaced)
    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", loaded)
    try:
        context = wrappers.tls()
    except (connection.NotConnected, client.ServerError):
        assert accepted == [checked.fingerprint] and used == []
        return  # Refusal is valid only after the real successful check and before TLS trust is loaded.
    assert accepted == [checked.fingerprint]
    assert context is not None
    if not used:
        with pytest.raises(ssl.SSLCertVerificationError):
            _handshake(context, crt, key, host)  # Preserve the requested-pin empty-trust refusal convention.
    else:
        assert used == [connected]
        _handshake(context, crt, key, host)  # Real OpenSSL verification accepts the original server/key pair.


def test_t620_n20_earlier_pin_filter_refuses_unavailable_store_after_current_pin_read(
    connected, computer, namespace_metadata, monkeypatch
):
    """Postimplementation regression: a failed earlier-pin inspection is never an absent pin."""
    data = data_dir()
    checked = connection.load_connection()
    assert checked is not None and checked.fingerprint is not None
    prior, _, _ = existing.make_certificate("https://prior.latet.lan", computer / "prior-tls")
    prior_pem = prior.read_text("utf-8")
    certificates.save(data, prior_pem)
    supplied = computer / "earlier-pin.pem"
    supplied.write_bytes(prior_pem.encode("utf-8"))
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(supplied))
    output = Path(wrappers.extra_ca_env("opencode")["NODE_EXTRA_CA_CERTS"])
    before = output.read_bytes()
    assert before.decode("utf-8") == connected  # The actual known earlier pin is excluded in an ordinary usable call.
    read_pin, read_input, write = connection._server_pem, connection._read_theirs, connection.write_atomic
    accepted, published = [], []

    def pin(data, fingerprint, **kwargs):
        pem = read_pin(data, fingerprint, **kwargs)
        if fingerprint == checked.fingerprint:
            assert pem == connected
            accepted.append(fingerprint)
        return pem

    def supplied_bytes(path):
        raw = read_input(path)
        assert path == supplied and raw.decode("utf-8") == prior_pem
        assert accepted == [checked.fingerprint]
        namespace_metadata.add(data / "certificates")  # Actual later namespace metadata changes, no predicate bypass.
        return raw

    def publish(path, *args, **kwargs):
        published.append(path)
        return write(path, *args, **kwargs)

    monkeypatch.setattr(connection, "_server_pem", pin)
    monkeypatch.setattr(connection, "_read_theirs", supplied_bytes)
    monkeypatch.setattr(connection, "write_atomic", publish)
    with pytest.raises(connection.NotConnected):
        wrappers.extra_ca_env("opencode")
    assert accepted == [checked.fingerprint] and published == [] and output.read_bytes() == before


def test_t620_n21_claude_env_retains_one_snapshot_after_actual_token_read_changes_selection(
    connected, computer, claude_home, namespace_metadata, monkeypatch
):
    data_a, path_a = data_dir(), connection.connection_path()
    checked = connection.load_connection()
    assert checked is not None and checked.fingerprint is not None
    ordinary_a = wrappers.claude_env()
    bundle_a = Path(ordinary_a["NODE_EXTRA_CA_CERTS"])
    assert ordinary_a["LATET_BRIDGE_URL"] == SPACE and ordinary_a["LATET_BRIDGE_TOKEN"] == TOKEN
    assert bundle_a.parent == data_a / "connection" and bundle_a.read_text("utf-8") == connected

    data_b = computer / "wrapper-selected-state-b"
    path_b = data_b / "connection.yaml"
    url_b, token_b = "https://other.latet.lan", "latet_pat_" + "b" * 43
    crt_b, _, fingerprint_b = existing.make_certificate(url_b, computer / "wrapper-tls-b")
    pem_b = crt_b.read_text("utf-8")
    other = checked.model_copy(update={"url": url_b, "fingerprint": fingerprint_b, "user_name": "State B"})
    monkeypatch.setenv("LATET_DATA_DIR", str(data_b))
    monkeypatch.setenv("LATET_CONNECTION", str(path_b))
    assert connection.save_server_token(data_b, other, token_b) == "file"
    connection.save_connection(other)
    assert certificates.save(data_b, pem_b) == fingerprint_b
    ordinary_b = wrappers.claude_env()
    bundle_b = Path(ordinary_b["NODE_EXTRA_CA_CERTS"])
    assert ordinary_b["LATET_BRIDGE_TOKEN"] == token_b and ordinary_b["LATET_BRIDGE_URL"] != SPACE
    assert bundle_b.parent == data_b / "connection" and bundle_b.read_text("utf-8") == pem_b
    preserved = {
        path: path.read_bytes() for path in (path_a, data_a / "server.token", path_b, data_b / "server.token", bundle_b)
    }
    monkeypatch.setenv("LATET_DATA_DIR", str(data_a))
    monkeypatch.setenv("LATET_CONNECTION", str(path_a))
    load_token, read_pin, write = connection.load_server_token, connection._server_pem, connection.write_atomic
    tokens, pins, published = [], [], []

    def token_read(data, selected, **kwargs):
        token = load_token(data, selected, **kwargs)
        tokens.append((data, selected, token))
        if len(tokens) == 1:
            assert (data, selected, token) == (data_a, checked, TOKEN)
            monkeypatch.setenv("LATET_DATA_DIR", str(data_b))
            monkeypatch.setenv("LATET_CONNECTION", str(path_b))
        return token

    def checked_pin(data, fingerprint, **kwargs):
        pem = read_pin(data, fingerprint, **kwargs)
        pins.append((data, fingerprint, pem))
        return pem

    def publish(path, *args, **kwargs):
        published.append(path)
        return write(path, *args, **kwargs)

    monkeypatch.setattr(connection, "load_server_token", token_read)
    monkeypatch.setattr(connection, "_server_pem", checked_pin)
    monkeypatch.setattr(connection, "write_atomic", publish)
    try:
        env = wrappers.claude_env()
    except connection.NotConnected:
        assert published == []  # A controlled refusal must precede publication under either selection.
    else:
        assert env["LATET_BRIDGE_URL"] == SPACE and env["ANTHROPIC_BASE_URL"] == f"{SPACE}/anthropic"
        assert env["LATET_BRIDGE_TOKEN"] == TOKEN
        assert Path(env["NODE_EXTRA_CA_CERTS"]) == bundle_a and bundle_a.read_text("utf-8") == connected
        assert pins == [(data_a, checked.fingerprint, connected)] and published == [bundle_a]
    assert tokens and tokens[0] == (data_a, checked, TOKEN)
    assert os.environ["LATET_DATA_DIR"] == str(data_b) and os.environ["LATET_CONNECTION"] == str(path_b)
    assert all(path.read_bytes() == raw for path, raw in preserved.items())


def test_t620_n27_global_install_keeps_initial_checked_connection_through_publication(
    quiet, connected, computer, claude_home, namespace_metadata, monkeypatch
):
    """Public global-install must not discard A's checked record and then install B under A's announcement."""
    from latet import claude_bridge
    from latet.service import autostart, daemon

    from .conftest import run_cli

    data_a, path_a = data_dir(), connection.connection_path()
    checked = connection.load_connection()
    assert checked is not None and checked.fingerprint is not None
    data_b = computer / "global-install-selected-b"
    path_b = data_b / "connection.yaml"
    url_b, token_b = "https://global-other.latet.lan", "latet_pat_" + "b" * 43
    crt_b, _, fingerprint_b = existing.make_certificate(url_b, computer / "global-install-tls-b")
    pem_b = crt_b.read_text("utf-8")
    other = checked.model_copy(update={"url": url_b, "fingerprint": fingerprint_b, "user_name": "State B"})
    monkeypatch.setenv("LATET_DATA_DIR", str(data_b))
    monkeypatch.setenv("LATET_CONNECTION", str(path_b))
    assert connection.save_server_token(data_b, other, token_b) == "file"
    connection.save_connection(other)
    assert certificates.save(data_b, pem_b) == fingerprint_b
    for agent in agents.ALL.values():
        installed = agent.id == "claude"
        info = agents.AgentInfo(agent.id, agent.name, installed, "2.1" if installed else None, "")
        monkeypatch.setattr(type(agent), "detect", lambda self, info=info: info)
    monkeypatch.setattr(daemon, "start", _no_sensitive_work)
    monkeypatch.setattr(autostart, "install", _no_sensitive_work)
    settings = claude_home / "settings.json"
    for data, path, bound, token, pem in (
        (data_a, path_a, checked, TOKEN, connected),
        (data_b, path_b, other, token_b, pem_b),
    ):
        monkeypatch.setenv("LATET_DATA_DIR", str(data))
        monkeypatch.setenv("LATET_CONNECTION", str(path))
        ordinary = run_cli("global-install", "--yes", "--agents", "claude")
        assert ordinary.exit_code == 0, ordinary.output
        env = existing._settings_env(claude_home)
        assert env["LATET_BRIDGE_TOKEN"] == token and env["LATET_BRIDGE_URL"].startswith(bound.url + "/u/")
        assert Path(env["NODE_EXTRA_CA_CERTS"]).read_text("utf-8") == pem
        _published_helper(data, path, bound, env)
        agents.ClaudeCode().revert()  # Return the same real agent settings to their prior state for the next control.
    preserved = {
        path: path.read_bytes()
        for path in (
            path_a,
            data_a / "server.token",
            path_b,
            data_b / "server.token",
            base.manifest_path(),
            data_b / "certificates" / f"{fingerprint_b.removeprefix('sha256:')}.pem",
        )
    }
    settings_before = settings.read_bytes() if settings.exists() else None
    monkeypatch.setenv("LATET_DATA_DIR", str(data_a))
    monkeypatch.setenv("LATET_CONNECTION", str(path_a))
    read, reads, effects = connection._load_connection, [], []

    def checked_then_switched(selected, *, unchecked=False):
        found = read(selected, unchecked=unchecked)
        reads.append((selected.data, selected.path, found, unchecked))
        if len(reads) == 1:
            assert reads[0] == (data_a, path_a, checked, False)
            monkeypatch.setenv("LATET_DATA_DIR", str(data_b))
            monkeypatch.setenv("LATET_CONNECTION", str(path_b))
        return found

    def observe(function, label):
        def forwarding(*args, **kwargs):
            effects.append(label)
            return function(*args, **kwargs)

        return forwarding

    monkeypatch.setattr(connection, "_load_connection", checked_then_switched)
    for name in ("backup", "write_atomic", "save_records"):
        monkeypatch.setattr(agents, name, observe(getattr(agents, name), name))
    for name in ("_claude", "prepare_marketplace"):
        monkeypatch.setattr(agents.ClaudeCode, name, observe(getattr(agents.ClaudeCode, name), name))
    monkeypatch.setattr(claude_bridge, "_publish_binding", observe(claude_bridge._publish_binding, "publish"))
    done = run_cli("global-install", "--yes", "--agents", "claude")
    if done.exit_code == 2:
        assert effects == [] and (settings.read_bytes() if settings.exists() else None) == settings_before
    else:
        assert done.exit_code == 0, done.output
        env = existing._settings_env(claude_home)
        assert env["LATET_BRIDGE_URL"] == SPACE and env["LATET_BRIDGE_TOKEN"] == TOKEN
        assert Path(env["NODE_EXTRA_CA_CERTS"]).parent == data_a / "connection"
        _published_helper(data_a, path_a, checked, env)
    assert reads and reads[0] == (data_a, path_a, checked, False)
    assert all(path.read_bytes() == raw for path, raw in preserved.items())
    assert not claude_bridge._binding_path(data_b).exists()


def _published_helper(data: Path, selected: Path, checked, env, *, session: bool = False):
    """Observe the adopted on-disk contract, without importing a future helper implementation."""
    root = data / "connection" / "claude-helper"
    paths = list(root.glob("session-*.json")) if session else [root / "permanent.json"]
    assert len(paths) == 1 and paths[0].is_file(), "trusted public setup did not publish its helper descriptor"
    path = paths[0]
    raw = path.read_bytes()
    assert len(raw) <= 16 * 1024 and TOKEN.encode() not in raw and env["LATET_BRIDGE_TOKEN"].encode() not in raw
    assert env["LATET_BRIDGE_URL"].encode() not in raw
    assert env["LATET_BRIDGE_URL"].rsplit("/", 1)[-1].encode() not in raw
    descriptor = json.loads(raw)
    assert set(descriptor) == {
        "v",
        "kind",
        "data",
        "connection",
        "origin",
        "identity",
        "fingerprint",
        "space_sha256",
        "install",
        "launch",
    }
    assert descriptor["v"] == 1 and descriptor["kind"] == "claude-bridge"
    assert Path(descriptor["data"]) == data and Path(descriptor["connection"]) == selected
    assert (descriptor["origin"], descriptor["identity"], descriptor["fingerprint"]) == (
        checked.url,
        checked.user,
        checked.fingerprint,
    )
    assert descriptor["space_sha256"] == hashlib.sha256(env["LATET_BRIDGE_URL"].encode("utf-8")).hexdigest()
    binding = json.loads(env["LATET_BRIDGE_HELPER"])
    assert set(binding) == {"v", "python", "cwd", "descriptor", "env"} and binding["v"] == 1
    assert Path(binding["descriptor"]) == path and Path(binding["cwd"]) == root / "cwd"
    assert Path(binding["python"]) == Path(sys.executable).resolve()
    assert descriptor["launch"] == binding and isinstance(binding["env"], dict) and binding["env"]
    assert set(descriptor["install"]) == {"python", "prefix", "base_prefix", "latet_root", "latet_version"}
    assert descriptor["install"]["python"] == binding["python"]
    return path, descriptor


def test_t620_n24_public_apply_publishes_last_reapply_refreshes_and_revert_cleans_binding(
    connected, computer, claude_home, namespace_metadata, monkeypatch
):
    data, selected = data_dir(), connection.connection_path()
    checked = connection.load_connection()
    assert checked is not None
    descriptor = data / "connection" / "claude-helper" / "permanent.json"
    settings = claude_home / "settings.json"
    settings.write_text(json.dumps({"env": {"TASK6_PERSONAL_SETTING": "keep"}}), "utf-8")
    plugin, save = agents.ClaudeCode._claude, agents.save_records
    steps = []

    def plugin_step(self, *args):
        assert not descriptor.exists(), "binding was usable before the required plugin step finished"
        steps.append(args)
        return plugin(self, *args)

    def saved(records):
        assert not descriptor.exists(), "binding was published before the integration record"
        result = save(records)
        assert not descriptor.exists(), "record persistence must complete before binding publication"
        steps.append("record")
        return result

    monkeypatch.setattr(agents.ClaudeCode, "_claude", plugin_step)
    monkeypatch.setattr(agents, "save_records", saved)
    claude = agents.ClaudeCode()
    record = claude.apply()
    env = existing._settings_env(claude_home)
    assert env["LATET_BRIDGE_URL"] == SPACE and env["LATET_BRIDGE_TOKEN"] == TOKEN
    path, first = _published_helper(data, selected, checked, env)
    assert path == descriptor and steps[-1] == "record"
    assert record.data["written"]["LATET_BRIDGE_HELPER"] == env["LATET_BRIDGE_HELPER"]
    prior_descriptor, prior_settings = descriptor.read_bytes(), settings.read_bytes()
    monkeypatch.setenv("CLAUDE_CODE_USE_MANTLE", "1")
    with pytest.raises(agents.RouteProblem):
        claude.apply()
    assert descriptor.read_bytes() == prior_descriptor and settings.read_bytes() == prior_settings
    monkeypatch.delenv("CLAUDE_CODE_USE_MANTLE")
    replacement = "latet_pat_" + "y" * 43
    connection.save_server_token(data, checked, replacement)
    claude.apply()
    env = existing._settings_env(claude_home)
    assert env["LATET_BRIDGE_TOKEN"] == replacement and env["LATET_BRIDGE_URL"] != SPACE
    _, refreshed = _published_helper(data, selected, checked, env)
    assert refreshed["space_sha256"] != first["space_sha256"]
    claude.revert()
    assert not descriptor.exists() and "claude" not in base.load_records()
    assert existing._settings_env(claude_home) == {"TASK6_PERSONAL_SETTING": "keep"}


def test_t620_n25_required_plugin_failure_invalidates_existing_public_apply_binding(
    connected, claude_home, namespace_metadata, monkeypatch
):
    data, selected = data_dir(), connection.connection_path()
    checked = connection.load_connection()
    assert checked is not None
    claude = agents.ClaudeCode()
    claude.apply()
    env = existing._settings_env(claude_home)
    assert env["LATET_BRIDGE_URL"] == SPACE and env["LATET_BRIDGE_TOKEN"] == TOKEN
    descriptor, _ = _published_helper(data, selected, checked, env)
    token_before = (data / "server.token").read_bytes()
    plugin = agents.ClaudeCode._claude
    failed = []

    def required_failure(self, *args):
        if args[:2] == ("plugin", "install"):
            assert not descriptor.exists(), "old binding remained usable at the required failing step"
            failed.append(args)
            return {"outcome": "error", "message": "owned required install failure"}
        return plugin(self, *args)

    monkeypatch.setattr(agents.ClaudeCode, "_claude", required_failure)
    with pytest.raises(RuntimeError, match="plugin install failed"):
        claude.apply()
    assert len(failed) == 1 and not descriptor.exists()
    assert (data / "server.token").read_bytes() == token_before


def test_t620_n26_public_session_publishes_after_settings_and_cleans_on_exit_or_setup_failure(
    connected, claude_home, namespace_metadata, monkeypatch
):
    import httpx

    from latet.cli import main

    from .conftest import run_cli

    data, selected = data_dir(), connection.connection_path()
    checked = connection.load_connection()
    assert checked is not None
    root = data / "connection" / "claude-helper"
    settings_written, launches = [], []
    write = wrappers.write_atomic

    def settings_write(path, *args, **kwargs):
        assert path.parent == data / "claude-settings" and list(root.glob("session-*.json")) == []
        result = write(path, *args, **kwargs)
        assert list(root.glob("session-*.json")) == []
        settings_written.append(path)
        return result

    def launch(argv, env):
        settings = Path(argv[argv.index("--settings") + 1])
        assert settings_written == [settings] and settings.is_file()
        assert json.loads(settings.read_text("utf-8"))["env"] == env
        assert env["LATET_BRIDGE_URL"] == SPACE and env["LATET_BRIDGE_TOKEN"] == TOKEN
        descriptor, _ = _published_helper(data, selected, checked, env, session=True)
        launches.append((settings, descriptor))
        return 0

    monkeypatch.setattr(main, "_require", lambda command: None)
    monkeypatch.setattr(client.ServerClient, "request", lambda self, *args, **kwargs: httpx.Response(200, json={}))
    monkeypatch.setattr(wrappers, "write_atomic", settings_write)
    monkeypatch.setattr(wrappers, "run", launch)
    done = run_cli("claude", "-p", "hi")
    assert done.exit_code == 0, (done.output, done.exception)
    [owned] = launches
    assert all(not path.exists() for path in owned)
    assert not (root / "permanent.json").exists()
    launches.clear()

    def cannot_write(path, *args, **kwargs):
        assert path.parent == data / "claude-settings" and list(root.glob("session-*.json")) == []
        raise OSError("owned session settings write failure")

    monkeypatch.setattr(wrappers, "write_atomic", cannot_write)
    done = run_cli("claude", "-p", "hi")
    assert done.exit_code == 2 and "Cannot read your --settings" in done.output
    assert launches == [] and list(root.glob("session-*.json")) == []


def test_t620_n23_public_claude_session_keeps_env_and_settings_in_one_selected_state(
    connected, computer, namespace_metadata, monkeypatch
):
    """A real token read cannot change the selected state for the rest of `latet claude`."""
    import httpx

    from latet.cli import main
    from latet.service import daemon

    from .conftest import run_cli

    data_a, path_a = data_dir(), connection.connection_path()
    checked = connection.load_connection()
    assert checked is not None and checked.fingerprint is not None
    data_b = computer / "cli-selected-state-b"
    path_b = data_b / "connection.yaml"
    url_b, token_b = "https://cli-other.latet.lan", "latet_pat_" + "b" * 43
    crt_b, _, fingerprint_b = existing.make_certificate(url_b, computer / "cli-tls-b")
    pem_b = crt_b.read_text("utf-8")
    other = checked.model_copy(update={"url": url_b, "fingerprint": fingerprint_b, "user_name": "State B"})
    monkeypatch.setenv("LATET_DATA_DIR", str(data_b))
    monkeypatch.setenv("LATET_CONNECTION", str(path_b))
    assert connection.save_server_token(data_b, other, token_b) == "file"
    connection.save_connection(other)
    assert certificates.save(data_b, pem_b) == fingerprint_b
    calls, launches = [], []

    def answer(self, method, path, **kwargs):
        calls.append((self.url, self.token, self.fingerprint, method, path))
        assert (method, path) == ("GET", "whoami") or (method == "POST" and path.endswith("/_latet/admin/user-name"))
        return httpx.Response(200, json={"surrogate": None})

    def launch(argv, env):
        settings = Path(argv[argv.index("--settings") + 1])
        launches.append((dict(env), settings, json.loads(settings.read_text("utf-8"))))
        return 0

    monkeypatch.setattr(main, "_require", lambda command: None)
    monkeypatch.setattr(client.ServerClient, "request", answer)
    monkeypatch.setattr(wrappers, "run", launch)  # Existing owned launch observation; never starts Claude Code.
    monkeypatch.setattr(daemon, "start", _no_sensitive_work)
    ordinary = {}
    for data, path, token, pem in ((data_a, path_a, TOKEN, connected), (data_b, path_b, token_b, pem_b)):
        monkeypatch.setenv("LATET_DATA_DIR", str(data))
        monkeypatch.setenv("LATET_CONNECTION", str(path))
        done = run_cli("claude", "-p", "hi")
        assert done.exit_code == 0, done.output
        env, settings, content = launches[-1]
        assert env == content["env"] and env["LATET_BRIDGE_TOKEN"] == token
        assert settings.parent == data / "claude-settings" and not settings.exists()
        assert Path(env["NODE_EXTRA_CA_CERTS"]).read_text("utf-8") == pem
        ordinary[data] = env
    assert ordinary[data_a]["LATET_BRIDGE_URL"] == SPACE
    assert ordinary[data_b]["LATET_BRIDGE_URL"] != SPACE
    preserved = {path: path.read_bytes() for path in (path_a, data_a / "server.token", path_b, data_b / "server.token")}
    monkeypatch.setenv("LATET_DATA_DIR", str(data_a))
    monkeypatch.setenv("LATET_CONNECTION", str(path_a))
    calls.clear()
    launches.clear()
    load_token = connection.load_server_token
    tokens, published = [], []

    def token_read(data, selected, **kwargs):
        token = load_token(data, selected, **kwargs)
        tokens.append((data, selected, token))
        if len(tokens) == 1:
            assert (data, selected, token) == (data_a, checked, TOKEN)
            monkeypatch.setenv("LATET_DATA_DIR", str(data_b))
            monkeypatch.setenv("LATET_CONNECTION", str(path_b))
        return token

    def publication(writer):
        def write(path, *args, **kwargs):
            published.append(path)
            return writer(path, *args, **kwargs)

        return write

    monkeypatch.setattr(connection, "load_server_token", token_read)
    monkeypatch.setattr(connection, "write_atomic", publication(connection.write_atomic))
    monkeypatch.setattr(wrappers, "write_atomic", publication(wrappers.write_atomic))
    done = run_cli("claude", "-p", "hi")
    if done.exit_code == 0:
        [observed] = launches
        env, settings, content = observed
        assert env == content["env"] == ordinary[data_a]
        assert settings.parent == data_a / "claude-settings" and not settings.exists()
        assert all(path == data_a / "connection" / "claude.pem" or path.parent == settings.parent for path in published)
    else:
        assert done.exit_code == 2 and "connection" in done.output.lower() and "Traceback" not in done.output
        assert launches == [] and published == []
    assert tokens and tokens[0] == (data_a, checked, TOKEN)
    assert all(call[:3] == (checked.url, TOKEN, checked.fingerprint) for call in calls)
    assert os.environ["LATET_DATA_DIR"] == str(data_b) and os.environ["LATET_CONNECTION"] == str(path_b)
    assert all(path.read_bytes() == raw for path, raw in preserved.items())


def test_t620_n22_wrapper_tls_keeps_checked_connection_and_pin_in_the_same_selected_root(
    connected, computer, namespace_metadata, monkeypatch
):
    """A valid public A pin in B must not conceal selection drift after the actual checked A read."""
    from .test_connection import _handshake

    data_a, path_a = data_dir(), connection.connection_path()
    checked = connection.load_connection()
    assert checked is not None and checked.fingerprint is not None
    host = urlsplit(checked.url).hostname
    assert host is not None
    crt_a, key_a = computer / "tls" / "server.crt", computer / "tls" / "server.key"
    ordinary_a = wrappers.tls()
    assert ordinary_a is not None
    _handshake(ordinary_a, crt_a, key_a, host)

    data_b = computer / "tls-selected-state-b"
    path_b = data_b / "connection.yaml"
    crt_b, key_b, fingerprint_b = existing.make_certificate(checked.url, computer / "tls-selected-pair-b")
    pem_b = crt_b.read_text("utf-8")
    assert fingerprint_b != checked.fingerprint
    other = checked.model_copy(update={"fingerprint": fingerprint_b, "user_name": "TLS state B"})
    monkeypatch.setenv("LATET_DATA_DIR", str(data_b))
    monkeypatch.setenv("LATET_CONNECTION", str(path_b))
    connection.save_connection(other)  # TLS requires no B credential copy.
    assert certificates.save(data_b, pem_b) == fingerprint_b
    assert certificates.save(data_b, connected) == checked.fingerprint  # Public A pin present under B's correct name.
    ordinary_b = wrappers.tls()
    assert ordinary_b is not None
    _handshake(ordinary_b, crt_b, key_b, host)
    pin_a = data_a / "certificates" / f"{checked.fingerprint.removeprefix('sha256:')}.pem"
    b_own = data_b / "certificates" / f"{fingerprint_b.removeprefix('sha256:')}.pem"
    b_copy = data_b / "certificates" / pin_a.name
    preserved = {path: path.read_bytes() for path in (path_a, path_b, pin_a, b_own, b_copy)}
    monkeypatch.setenv("LATET_DATA_DIR", str(data_a))
    monkeypatch.setenv("LATET_CONNECTION", str(path_a))
    load, read_pin = connection._load_connection, client._server_pem
    reads, pin_reads, pins = [], [], []

    def checked_then_switched(selected, *, unchecked=False):
        found = load(selected, unchecked=unchecked)
        reads.append((selected.data, selected.path, found, unchecked))
        if len(reads) == 1:
            assert (selected.data, selected.path, found, unchecked) == (data_a, path_a, checked, False)
            monkeypatch.setenv("LATET_DATA_DIR", str(data_b))
            monkeypatch.setenv("LATET_CONNECTION", str(path_b))
        return found

    def actual_pin(data, fingerprint, **kwargs):
        pin_reads.append((data, fingerprint))
        pem = read_pin(data, fingerprint, **kwargs)
        pins.append((data, fingerprint, pem))
        return pem

    monkeypatch.setattr(connection, "_load_connection", checked_then_switched)
    monkeypatch.setattr(client, "_server_pem", actual_pin)
    try:
        context = wrappers.tls()
    except (connection.NotConnected, client.ServerError):
        assert all(data == data_a for data, _ in pin_reads)  # Refusal cannot follow a foreign-root pin read.
    else:
        assert context is not None
        _handshake(context, crt_a, key_a, host)
        with pytest.raises(ssl.SSLCertVerificationError):
            _handshake(context, crt_b, key_b, host)
        assert pins == [(data_a, checked.fingerprint, connected)]
    assert all(data == data_a for data, _ in pin_reads)
    assert reads == [(data_a, path_a, checked, False)]
    assert os.environ["LATET_DATA_DIR"] == str(data_b) and os.environ["LATET_CONNECTION"] == str(path_b)
    assert all(path.read_bytes() == raw for path, raw in preserved.items())
