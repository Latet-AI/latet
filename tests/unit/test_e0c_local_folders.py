"""The E0c final review's fixes for Latet's own folders (R1-R4, R18): local Latet is not a connection, Latet makes its
folders private whatever the umask and tightens an older one nobody else could write into, and a refusal names the
folder, why and the fix. Every unsafe case is synthetic (owner, access list, mode): nothing here depends on the
profile of the computer the tests run on."""

import logging
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from latet import wrappers
from latet.config import Config, data_dir
from latet.doctor import checks
from latet.engine.reviewer import certificate as certificates
from latet.integrations import base
from latet.server import connection
from latet.server.certificate import make_certificate
from latet.server.client import ServerClient, pin_certificate
from latet.storage import folders, keys

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows' access lists")
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
OTHER = "S-1-5-21-1111111111-2222222222-3333333333-1003"  # another local account, as remoteadm is


@pytest.fixture
def unsafe_ancestor(computer, monkeypatch):
    """The data folder's parent is one another account may change (synthetic: its verdict alone is doubled, every
    other folder's is the real one)."""
    ancestor = computer
    real_problem, real_refusal = folders.namespace_folder_problem, getattr(folders, "namespace_refusal", None)
    said = f"{ancestor} lets PC\\remoteadm ({OTHER}) change what is in it: remove that entry, or LATET_DATA_DIR"

    def problem(folder, found, **kwargs):
        if folder == ancestor:
            return "namespace can be changed by another account"
        return real_problem(folder, found, **kwargs)

    def refusal(folder, found, **_):
        return said if folder == ancestor else real_refusal(folder, found, **_) if real_refusal else None

    monkeypatch.setattr(folders, "namespace_folder_problem", problem)
    monkeypatch.setattr(folders, "namespace_refusal", refusal, raising=False)
    return ancestor


def test_local_latet_never_connected_is_not_refused_for_a_connection_it_never_made(computer, unsafe_ancestor):
    """C-C1: with no connection file, nothing of a connection is admitted: the agents get this computer's Latet."""
    data_dir().mkdir()
    assert connection.load_connection() is None
    assert wrappers.connected() is None
    assert wrappers.base_url(Config(port=5283)) == "http://127.0.0.1:5283"
    assert wrappers.daemon_token() == keys.load_bridge_token(data_dir())
    assert wrappers.tls() is None
    assert wrappers.extra_ca_env("opencode") == {}


def test_a_planted_connection_file_in_an_unsafe_folder_is_still_refused_naming_the_folder(computer, unsafe_ancestor):
    data_dir().mkdir()
    connection.connection_path().write_text("url: https://latet.acme.lan\nuser: u_emilycarte\nuser_name: E\n")
    with pytest.raises(connection.NotConnected) as refused:
        wrappers.connected()
    assert str(unsafe_ancestor) in refused.value.message and "remoteadm" in refused.value.message


def test_the_records_refusal_names_the_ancestor_the_account_and_the_fix_and_no_connection(computer, unsafe_ancestor):
    """R3 and R4: on a connected computer the integration records stay refused under such a folder, in words a person
    can act on (in local mode it is a warning of `latet doctor`, R28: tests/unit/test_folder_trust.py)."""
    data_dir().mkdir()
    connection.connection_path().write_text("url: https://latet.acme.lan\nuser: u_emilycarte\nuser_name: E\n")
    with pytest.raises(base.RecordsUnavailable) as refused:
        base.load_records()
    words = str(refused.value)
    assert str(unsafe_ancestor) in words and "remoteadm" in words and "LATET_DATA_DIR" in words
    assert "connect" not in words.replace("`latet agents apply`", "")
    assert refused.value.said is not None


def test_doctor_says_a_refused_data_folder_as_the_data_folder_not_a_connection(computer, monkeypatch):
    """A parent another account owns: refused in local mode too (R28), said as the data folder's."""
    ancestor, real_problem = computer, folders.namespace_folder_problem
    said = f"{ancestor} belongs to another account (PC\\remoteadm ({OTHER})): make it yours, or LATET_DATA_DIR"

    def problem(folder, found, **kwargs):
        return "namespace belongs to another account" if folder == ancestor else real_problem(folder, found, **kwargs)

    monkeypatch.setattr(folders, "namespace_folder_problem", problem)
    monkeypatch.setattr(folders, "namespace_refusal", lambda folder, found, **_: said if folder == ancestor else None)
    data_dir().mkdir()
    found = checks.run_all()
    assert [check.title for check in found] == ["Data folder"]
    assert found[0].status == checks.FAIL and "remoteadm" in found[0].detail


def test_doctor_on_a_computer_that_never_connected_admits_no_connection(computer, monkeypatch):
    def refused():
        pytest.fail("a connection's folder admission ran with no connection file")

    monkeypatch.setattr(connection, "connection_folder_problem", refused)
    assert checks._diagnostic_connection().connection is None


@WINDOWS
def test_on_windows_the_namespace_words_name_the_account_its_rights_and_icacls(computer, monkeypatch):
    monkeypatch.setattr(folders, "my_sid", lambda: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(
        folders, "folder_grants", lambda folder: (folders.SYSTEM, [folders.Grant(OTHER, 0x1F01FF, False)])
    )
    monkeypatch.setattr(folders, "_account", lambda sid: "PC\\remoteadm" if sid == OTHER else "")
    said = folders.namespace_refusal(computer, computer.lstat())
    assert said is not None
    assert str(computer) in said and f"PC\\remoteadm ({OTHER})" in said and "full control" in said
    assert f'icacls "{computer}"' in said and "LATET_DATA_DIR" in said
    assert folders.namespace_folder_problem(computer, computer.lstat()) == "namespace can be changed by another account"


@WINDOWS
def test_on_windows_a_private_folder_refusal_names_who_else_it_lets_in(computer, monkeypatch):
    monkeypatch.setattr(folders, "my_sid", lambda: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(
        folders, "folder_grants", lambda folder: ("S-1-5-21-1-2-3-1001", [folders.Grant(OTHER, 0x1200A9, False)])
    )
    monkeypatch.setattr(folders, "_account", lambda sid: "PC\\remoteadm")
    problem = folders.private_folder_problem(computer)
    assert problem is not None and f"PC\\remoteadm ({OTHER})" in problem and "icacls" in problem


@pytest.mark.parametrize(
    ("mode", "allows", "words"),
    [(0o755, [], "chmod 700"), (0o700, [0x2], "chmod -N"), (0o700, None, "cannot be read")],
    ids=["mode", "allow-entry", "unreadable"],
)
def test_on_macos_a_private_folder_refusal_names_the_folder_and_the_fix(computer, monkeypatch, mode, allows, words):
    """R3 on macOS (Minor 10): simulated, no native Darwin claim."""
    folder = computer / "data"
    folder.mkdir()
    found = SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=4242, st_dev=1, st_ino=1)
    simulated = SimpleNamespace(**vars(os))
    simulated.stat = lambda path, *a, **k: found
    simulated.getuid = lambda: 4242

    def acl(path, metadata):
        if allows is None:
            raise OSError("Darwin directory ACL metadata unavailable")
        return allows

    monkeypatch.setattr(folders, "os", simulated)
    monkeypatch.setattr(folders, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(folders, "_darwin_allows", acl)
    problem = folders.private_folder_problem(folder)
    assert problem is not None and str(folder) in problem and words in problem


@POSIX
def test_on_posix_the_namespace_words_name_the_mode_and_the_fix(computer):
    shared = computer / "shared"
    shared.mkdir()
    shared.chmod(0o775)
    said = folders.namespace_refusal(shared, shared.lstat())
    assert said is not None and str(shared) in said and "0775" in said and f"chmod go-w {shared}" in said


# --- R2: Latet's own folders, private whatever the umask ------------------------------------------------------------


@pytest.fixture(params=[0o022, 0o002], ids=["umask022", "umask002"])
def umask(request):
    if sys.platform == "win32":
        pytest.skip("POSIX modes")
    before = os.umask(request.param)
    yield request.param
    os.umask(before)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_latets_own_creators_make_a_data_folder_its_own_checks_admit(computer, umask):
    """C-C1 on macOS and umask-002 Linux: the daemon's logs, the bridge token, a kept certificate and the vault each
    make the data folder 0700, then the records and a connection's writer admit it."""
    from latet.service import daemon
    from latet.storage.db import Store

    data = data_dir()
    nested = data.parent / "made" / "here"
    keys.load_bridge_token(data)
    assert _mode(data) == 0o700
    handler_count = len(logging.getLogger().handlers)
    try:
        daemon.configure_file_logging()
    finally:
        for handler in logging.getLogger().handlers[handler_count:]:
            logging.getLogger().removeHandler(handler)
            handler.close()
    assert _mode(data / "logs") == 0o700
    crt, _, _ = make_certificate("https://latet.acme.lan", computer / "tls")
    certificates.save(data, crt.read_text())
    assert _mode(data / "certificates") == 0o700
    assert all(_mode(pem) == 0o600 for pem in (data / "certificates").iterdir())
    Store(nested / "vault.db", b"k" * 32).close()
    assert _mode(nested) == 0o700 and _mode(nested.parent) == 0o700
    assert base.load_records() == {}
    connection._admit_connection(connection._selected_connection(), create=True)


def test_an_older_latets_read_only_data_folder_is_tightened_once_and_said(computer, umask, caplog):
    data = data_dir()
    data.mkdir()
    data.chmod(0o755)
    with caplog.at_level(logging.WARNING, logger="latet.storage.folders"):
        assert base.load_records() == {}
        base.load_records()
    assert _mode(data) == 0o700
    said = [record.getMessage() for record in caplog.records if "chmod 700" in record.getMessage()]
    assert len(said) == 1 and "0755" in said[0]
    assert str(data) not in said[0] and str(data.parent) not in said[0]  # a folder is personal data: never logged


def test_a_folder_its_group_may_write_stays_as_it_is_through_latets_own_creators(computer, umask):
    """R2: a folder others may write into is never repaired, whatever reaches it first (Minor 4)."""
    folder = computer / "shared"
    folder.mkdir()
    folder.chmod(0o775)
    assert folders.make_private_folder(folder) == folder
    assert _mode(folder) == 0o775
    assert folders.tighten_folder(folder) is False
    assert _mode(folder) == 0o775


def test_latets_own_creator_accepts_the_persons_own_link_and_never_tightens_through_it(computer, umask):
    """R1: a data folder the person moved with a link of their own keeps working; nothing is changed through it."""
    real = computer / "elsewhere"
    real.mkdir()
    real.chmod(0o755)
    linked = computer / "data-link"
    linked.symlink_to(real, target_is_directory=True)
    assert folders.make_private_folder(linked) == linked
    assert _mode(real) == 0o755  # tighten_folder never goes through a link


def test_latets_own_creator_refuses_a_link_to_a_folder_of_another_account(computer, umask, monkeypatch):
    real = computer / "theirs"
    real.mkdir(mode=0o700)
    linked = computer / "data-link"
    linked.symlink_to(real, target_is_directory=True)
    someone_else = os.getuid() + 1
    found = os.stat(real)
    simulated = SimpleNamespace(**vars(os))
    simulated.stat = lambda path, *a, **k: (  # the folder it leads to is another account's; the link is the person's
        SimpleNamespace(st_mode=found.st_mode, st_uid=someone_else) if Path(path) == linked else os.stat(path, *a, **k)
    )
    monkeypatch.setattr(folders, "os", simulated)
    with pytest.raises(OSError, match="another account"):
        folders.make_private_folder(linked)


def test_on_windows_latets_own_creator_accepts_the_persons_own_junction(computer):
    if sys.platform != "win32":
        pytest.skip("Windows junctions")
    import _winapi

    real = computer / "elsewhere"
    real.mkdir()
    junction = computer / "data-junction"
    _winapi.CreateJunction(str(real), str(junction))
    assert folders.make_private_folder(junction) == junction
    assert folders.make_private_folder(junction / "logs") == junction / "logs" and (real / "logs").is_dir()


def test_latets_own_creator_refuses_a_folder_of_another_account(computer, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("POSIX owners")
    folder = computer / "theirs"
    folder.mkdir(mode=0o700)
    someone_else = os.getuid() + 1
    monkeypatch.setattr(folders.os, "getuid", lambda: someone_else)
    with pytest.raises(OSError, match="another account"):
        folders.make_private_folder(folder)


def test_a_data_folder_others_may_write_into_is_refused_never_repaired(computer, umask):
    data = data_dir()
    data.mkdir()
    data.chmod(0o775)
    with pytest.raises(base.RecordsUnavailable) as refused:
        base.load_records()
    assert _mode(data) == 0o775
    assert str(data) in str(refused.value) and "chmod" in str(refused.value)


def test_a_link_in_the_place_of_the_data_folder_is_never_tightened(computer, umask):
    real = computer / "real"
    real.mkdir()
    real.chmod(0o755)
    data_dir().symlink_to(real, target_is_directory=True)
    assert folders.tighten_folder(data_dir()) is False
    assert _mode(real) == 0o755


def test_a_folder_of_another_owner_is_never_tightened(computer, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("POSIX modes")
    folder = computer / "theirs"
    folder.mkdir()
    folder.chmod(0o755)
    someone_else = os.getuid() + 1
    monkeypatch.setattr(folders.os, "getuid", lambda: someone_else)
    assert folders.tighten_folder(folder) is False
    assert _mode(folder) == 0o755


def test_tightening_is_nothing_on_windows(computer):
    if sys.platform != "win32":
        pytest.skip("Windows")
    data_dir().mkdir()
    assert folders.tighten_folder(data_dir()) is False


# --- R18: the pin store, through a real pinned TLS connection ---------------------------------------------------------


def test_a_pin_kept_under_any_umask_is_read_back_and_used_for_a_pinned_connection(computer, latet_server):
    """X-I1: on POSIX with a umask of 002 the pin store was made group-writable, its pin read back as missing, and the
    pinned connection refused. Here: pinned, read back through the connection's own reader, and used."""
    before = os.umask(0o002) if sys.platform != "win32" else None
    try:
        pinned = pin_certificate(latet_server.url, latet_server.fingerprint)
    finally:
        if before is not None:
            os.umask(before)
    if sys.platform != "win32":
        assert _mode(data_dir()) == 0o700 and _mode(data_dir() / "certificates") == 0o700
    assert connection._server_pem(data_dir(), pinned, strict=True) is not None
    with ServerClient(latet_server.url, None, pinned) as client:
        answer = client.enrol(latet_server.code, "laptop")
    assert answer["identity"]["id"]
