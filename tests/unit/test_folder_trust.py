"""After E0c (R26-R30): the accounts the person trusts in LATET_TRUSTED_ACCOUNTS, read on Windows from the registry's
computer and user environments alone, and the direct members of the local Administrators group may change a parent
folder of Latet's data as the group may; in local mode a folder above the data folder that another account may change
is a warning of `latet doctor`, not a refusal; the data folder itself, what is inside it, a parent another account
owns and any other folder stay refused. Every case is synthetic (owner, access list, members, names, the registry's
values): nothing here depends on the profile of the computer the tests run on, except the read-only calls of Windows'
own API at the end. The real registry is never written."""

import importlib
import re
import sys
import unicodedata
from pathlib import Path

import pytest

from latet import claude_bridge
from latet.config import data_dir
from latet.doctor import checks
from latet.integrations import base
from latet.server import connection
from latet.storage import folders

from .conftest import run_cli

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows' access lists")
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
ME = "S-1-5-21-1-2-3-1001"
OTHER = "S-1-5-21-1111111111-2222222222-3333333333-1010"  # another local account, as remoteadm is
HELPDESK = "S-1-5-21-1444444444-1555555555-1666666666-1105"  # a domain group an IT department adds to every profile
DOMAIN_USERS = "S-1-5-21-1444444444-1555555555-1666666666-513"
ENTRA = "S-1-12-1-1111111111-1222222222-1333333333-1444444444"
SERVICE = "S-1-5-80-1111111111-1222222222-1333333333-1444444444-1555555555"
NTLM = "S-1-5-64-10"
NAMES = {ME: "PC\\emily", OTHER: "PC\\remoteadm", HELPDESK: "ACME\\Desktop Admins", DOMAIN_USERS: "ACME\\Domain Users"}
FOUND = {"pc\\remoteadm": OTHER, "remoteadm": OTHER, "acme\\desktop admins": HELPDESK, "everyone": "S-1-1-0"}
FOUND |= {"acme\\domain users": DOMAIN_USERS, "nt authority\\ntlm authentication": NTLM}
FULL = 0x1F01FF
CHANGEABLE = "namespace can be changed by another account"


def _trust(monkeypatch, user: str | None = None, machine: str | None = None) -> None:
    """LATET_TRUSTED_ACCOUNTS as the registry holds it (R30), doubled: ``user`` in HKCU\\Environment, ``machine`` in
    the computer's environment; the real registry is never read nor written here."""
    values = {"user": user, "machine": machine}
    monkeypatch.setattr(folders, "environment_value", lambda scope: values[scope])


@pytest.fixture
def windows(computer, monkeypatch):
    """Windows as the folder checks read it, every answer synthetic: this account is ME; each folder is ME's, open to
    ME and SYSTEM alone, unless the test gives it an owner and entries of its own (the returned dict, by folder);
    Windows finds the names in FOUND; the Administrators group has no direct member; the registry holds no
    LATET_TRUSTED_ACCOUNTS."""
    if sys.platform != "win32":
        pytest.skip("Windows' access lists")
    special: dict[Path, tuple[str, list[folders.Grant]]] = {}

    def grants(folder):
        return special.get(
            Path(folder), (ME, [folders.Grant(ME, FULL, False), folders.Grant(folders.SYSTEM, FULL, False)])
        )

    def lookup(name):
        if name.casefold() not in FOUND:
            raise OSError(None, "No mapping between account names and security IDs was done", None, 1332)
        return FOUND[name.casefold()]

    monkeypatch.setattr(folders, "my_sid", lambda: ME)
    monkeypatch.setattr(folders, "folder_grants", grants)
    monkeypatch.setattr(folders, "_account", lambda sid: NAMES.get(sid, ""))
    monkeypatch.setattr(folders, "account_sid", lookup)
    monkeypatch.setattr(folders, "administrators_members", lambda: ())
    monkeypatch.delenv("LATET_TRUSTED_ACCOUNTS", raising=False)
    _trust(monkeypatch)
    return special


@pytest.fixture
def helper():
    """Claude Code's helper module, imported for its private-chain check and forgotten after: another test runs it as
    ``__main__`` (tests/unit/test_task6_claude_helper.py), which an imported copy would shadow."""
    present = "latet.claude_bridge_helper" in sys.modules
    yield importlib.import_module("latet.claude_bridge_helper")
    if not present:
        sys.modules.pop("latet.claude_bridge_helper", None)


def _open_to(special, folder: Path, sid: str, owner: str = folders.SYSTEM, *, inherited: bool = False) -> None:
    special[folder] = (owner, [folders.Grant(folders.SYSTEM, FULL, False), folders.Grant(sid, FULL, False, inherited)])


def _connected() -> None:
    """A connection file in the data folder: this computer is connected to a Latet server (R1)."""
    data_dir().mkdir(exist_ok=True)
    connection.connection_path().write_text("url: https://latet.acme.lan\nuser: u_emilycarte\nuser_name: E\n")


def _refused_words() -> str:
    with pytest.raises(base.RecordsUnavailable) as refused:
        base.load_records()
    return " ".join(str(refused.value).split())


def _local_groups_quiet(monkeypatch) -> None:
    """`latet doctor`'s other local checks, none of which is this file's concern, say nothing."""
    for name in ("daemon", "port", "engine", "recognizers", "ocr", "reviewer", "live_review", "agents", "instructions"):
        monkeypatch.setattr(checks, f"check_{name}", lambda: [])


def _titled(title: str) -> list[checks.Check]:
    return [check for check in checks.check_folders() if check.title == title]


# --- R26: LATET_TRUSTED_ACCOUNTS ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    [
        "PC\\remoteadm",
        "remoteadm",
        OTHER,
        OTHER.lower(),
        f"*{OTHER}",
        " ACME\\Desktop Admins ; pc\\REMOTEADM ,",
        f"x;{OTHER},",
    ],
    ids=["computer-name", "name", "sid", "sid-lower-case", "icacls-star", "list-semicolons", "list-commas"],
)
def test_a_grant_to_an_account_the_person_trusts_on_a_parent_passes_even_connected(windows, computer, monkeypatch,
                                                                                    written):  # fmt: skip
    _open_to(windows, computer, OTHER)
    _trust(monkeypatch, user=written)
    assert folders.namespace_folder_problem(computer, computer.lstat()) is None
    _connected()
    assert base.load_records() == {}
    assert connection.connection_folder_problem() is None


def test_without_it_the_same_grant_is_refused_connected_naming_latet_trusted_accounts(windows, computer):
    _open_to(windows, computer, OTHER)
    _connected()
    words = _refused_words()
    assert f"{computer} lets PC\\remoteadm ({OTHER}) change what is in it" in words and "full control" in words
    assert f'`icacls "{computer}" /remove:g *{OTHER}`' in words and "LATET_TRUSTED_ACCOUNTS" in words
    assert "LATET_DATA_DIR" in words
    assert "LATET_TRUSTED_ACCOUNTS" in (connection.connection_folder_problem() or "")


def test_without_it_the_same_grant_is_only_warned_in_local_mode_with_the_three_ways_out(windows, computer,
                                                                                         monkeypatch):  # fmt: skip
    _open_to(windows, computer, OTHER)
    data_dir().mkdir()
    assert base.load_records() == {}
    _local_groups_quiet(monkeypatch)
    found = checks.run_all()
    warned = [check for check in found if check.title == "Data folder"]
    assert [check.status for check in warned] == [checks.WARN], found
    words = " ".join(warned[0].detail.split())
    assert str(computer) in words and f"PC\\remoteadm ({OTHER})" in words and "full control, (F)" in words
    assert f'icacls "{computer}" /remove:g *{OTHER}' in words  # remove the entry
    assert "LATET_TRUSTED_ACCOUNTS" in words and "LATET_DATA_DIR" in words  # trust the account, or another folder
    assert not any(check.status == checks.FAIL for check in found)


def test_an_inherited_entry_is_to_be_removed_where_it_comes_from(windows, computer):
    """icacls /remove:g does nothing on a folder whose entry is inherited (I): the words say where to remove it."""
    _open_to(windows, computer, OTHER, inherited=True)
    words = " ".join((folders.namespace_warning(computer, computer.lstat()) or "").split())
    assert "/remove:g" not in words and "inherited" in words and "(I)" in words and "folder above" in words


def test_a_deleted_accounts_entry_is_said_so_and_its_sid_may_be_trusted(windows, computer, monkeypatch):
    """An account deleted since its entry was made leaves its SID in the access list, with no name: the words say
    Windows does not find it, and the SID, which nobody can hold again, may still be trusted by SID."""
    gone = "S-1-5-21-1111111111-2222222222-3333333333-1011"
    _open_to(windows, computer, gone)
    said = folders.namespace_warning(computer, computer.lstat()) or ""
    assert f"{computer} lets {gone}, an account Windows does not find" in said
    _trust(monkeypatch, user=gone)
    assert folders.namespace_folder_problem(computer, computer.lstat()) is None


def test_the_data_folder_itself_is_refused_with_the_account_trusted(windows, computer, monkeypatch):
    """R26: never the data folder itself: it holds the vault's key (``private_folder_problem`` stays strict)."""
    _trust(monkeypatch, user="PC\\remoteadm")
    data = data_dir()
    data.mkdir()
    _open_to(windows, data, OTHER, owner=ME)
    assert folders.private_folder_problem(data) is not None
    words = _refused_words()
    assert str(data) in words and "remoteadm" in words
    _local_groups_quiet(monkeypatch)
    assert [(check.status, check.title) for check in checks.run_all()] == [(checks.FAIL, "Data folder")]


def test_the_data_folder_itself_open_to_another_account_is_refused_in_local_mode(windows, computer):
    """R28: the data folder's own check stays a refusal in both modes."""
    data = data_dir()
    data.mkdir()
    _open_to(windows, data, OTHER, owner=ME)
    words = _refused_words()
    assert str(data) in words and "remoteadm" in words


def test_the_data_folder_as_a_parent_of_a_folder_in_it_is_checked_without_trust(windows, computer, monkeypatch, helper):
    """M3: the data folder, and every folder in it, are checked with the strict set also as a parent (the backups,
    the pin store, the helper's binding), whoever the person trusts."""
    _trust(monkeypatch, user="PC\\remoteadm")
    data = data_dir()
    (data / "backups").mkdir(parents=True)
    (data / "connection" / "claude-helper" / "cwd").mkdir(parents=True)
    _open_to(windows, data, OTHER, owner=ME)
    with pytest.raises(base.RecordsUnavailable, match="remoteadm"):
        base._trusted_folder(data / "backups")
    with pytest.raises(helper._Rejected):
        helper._private_chain(data / "connection" / "claude-helper" / "cwd")
    windows.pop(data)
    _open_to(windows, data / "connection", OTHER, owner=ME)
    with pytest.raises(helper._Rejected):
        helper._private_chain(data / "connection" / "claude-helper")


def test_the_helpers_private_chain_trusts_the_accounts_above_the_data_folder(windows, computer, monkeypatch, helper):
    """M6: the trust reaches the helper's private chain above the data folder, read from the registry there too."""
    data = data_dir()
    (data / "connection" / "claude-helper").mkdir(parents=True)
    _open_to(windows, computer, OTHER)
    with pytest.raises(helper._Rejected):
        helper._private_chain(data / "connection" / "claude-helper")
    _trust(monkeypatch, user="PC\\remoteadm")
    helper._private_chain(data / "connection" / "claude-helper")


def test_the_data_folder_spelled_another_way_is_known_by_its_identity(windows, computer, monkeypatch):
    """M3: LATET_DATA_DIR in the \\\\?\\ form names the same folder as the chain's ordinary spelling, which no path
    comparison sees: its identity does, and the data folder as a parent stays checked with no trust."""
    data = data_dir()
    (data / "backups").mkdir(parents=True)
    monkeypatch.setenv("LATET_DATA_DIR", "\\\\?\\" + str(data))
    assert base._normal(data_dir()) != base._normal(data)  # no comparison of paths finds it
    _trust(monkeypatch, user="PC\\remoteadm")
    _open_to(windows, data, OTHER, owner=ME)
    with pytest.raises(base.RecordsUnavailable, match="remoteadm"):
        base._trusted_folder(data / "backups")


@pytest.mark.parametrize("opened", ["data", "connection"])
def test_the_helpers_binding_is_read_with_the_data_folder_it_is_in_never_trusted(windows, computer, monkeypatch,
                                                                                  opened):  # fmt: skip
    """M3: the helper reads its binding while LATET_DATA_DIR names its own empty cwd (no inherited selection): the
    binding's own data folder, and its connection folder, are still checked with no trust as parents."""
    data = data_dir()
    root = data / "connection" / "claude-helper"
    (root / "cwd").mkdir(parents=True)
    descriptor = root / "permanent.json"
    descriptor.write_text("{}")
    monkeypatch.setenv("LATET_DATA_DIR", str(root / "cwd"))  # as claude_bridge_helper sets it before the read
    _trust(monkeypatch, user="PC\\remoteadm")
    _open_to(windows, data if opened == "data" else data / "connection", OTHER, owner=ME)
    with pytest.raises(base.RecordsUnavailable, match="remoteadm"):
        claude_bridge._read_binding(descriptor, lambda raw, most: {}, {})


def test_an_unresolvable_name_trusts_nothing_and_is_said(windows, computer, monkeypatch):
    _trust(monkeypatch, user="PC\\remotadm")  # a typo
    _open_to(windows, computer, OTHER)
    named, unresolved = folders.trusted_accounts()
    assert named == () and len(unresolved) == 1
    assert "PC\\remotadm" in unresolved[0] and "trusts nothing" in unresolved[0] and "1332" in unresolved[0]
    _connected()
    words = _refused_words()
    assert "LATET_TRUSTED_ACCOUNTS in your user environment names PC\\remotadm, which Windows does not find" in words
    trusted = _titled("Trusted accounts")
    assert [check.status for check in trusted] == [checks.WARN] and "PC\\remotadm" in trusted[0].detail


@pytest.mark.parametrize(
    "written",
    [
        "S-1-5-21-4444444444-1-2-1010",
        "S-1-5-21-1-2-3-" + "-".join(["1"] * 13),
        "S-1-281474976710656-1",
        "S-1-5-" + "9" * 200,
    ],
    ids=["sub-authority-over-32-bits", "more-than-15-sub-authorities", "authority-over-48-bits", "200-digits"],
)
def test_an_entry_shaped_as_a_sid_that_is_none_trusts_nothing(windows, monkeypatch, written):
    """Windows reads 4444444444 as 4294967295: another SID than the one written, which is never trusted."""
    _trust(monkeypatch, user=written)
    named, unresolved = folders.trusted_accounts()
    assert named == () and len(unresolved) == 1 and "trusts nothing" in unresolved[0]


@pytest.mark.parametrize(
    "written",
    [
        "PC\\remote\x1b[2Jadm",
        "PC\\remote\u202eadm",
        "PC\\remote\u2066adm",
        "PC\\remote\u2028adm",
        "PC\\remote\u200badm",
    ],
    ids=["escape", "right-to-left-override", "isolate", "line-separator", "zero-width-space"],
)
def test_an_entry_with_a_control_or_format_character_trusts_nothing_and_is_not_written_back(windows, monkeypatch,
                                                                                             written):  # fmt: skip
    _trust(monkeypatch, user=f"{written};PC\\remoteadm")
    named, unresolved = folders.trusted_accounts()
    assert [(item.entry, item.sid) for item in named] == [("PC\\remoteadm", OTHER)] and len(unresolved) == 1
    assert "character" in unresolved[0] and written not in unresolved[0]
    assert not any(unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp") for char in unresolved[0])


def test_entries_are_capped_in_number_and_length(windows, monkeypatch):
    asked = []
    monkeypatch.setattr(folders, "account_sid", lambda name: asked.append(name) or OTHER)
    _trust(monkeypatch, user=";".join(f"PC\\user{index}" for index in range(100)))
    named, unresolved = folders.trusted_accounts()
    assert len(named) == len(asked) == 64 and len(unresolved) == 1 and "64" in unresolved[0]
    asked.clear()
    _trust(monkeypatch, user="PC\\" + "a" * 300)
    named, unresolved = folders.trusted_accounts()
    assert named == () and asked == [] and "a" * 300 not in unresolved[0] and "256" in unresolved[0]


@pytest.mark.parametrize(
    "written",
    [
        "Everyone",
        "S-1-5-32-545",
        "ACME\\Domain Users",
        "S-1-3-0",
        "S-1-18-1",
        "NT AUTHORITY\\NTLM Authentication",
        "S-1-5-80-0",
        "S-1-5-19",
        "S-1-5-32-555",
        "S-1-5-21-0-0-0-496",
        "S-1-5-21-0-0-0-497",
    ],
)
def test_only_an_account_or_group_of_a_computer_a_domain_entra_id_or_a_service_is_trusted(windows, computer,
                                                                                          monkeypatch, written):  # fmt: skip
    """M1: an allowlist of SID forms, not a list of the groups that stand for every account."""
    _trust(monkeypatch, user=written)
    named, unresolved = folders.trusted_accounts()
    assert named == () and "trusts nothing" in unresolved[0]
    sid = FOUND.get(written.casefold(), written)
    _open_to(windows, computer, sid)
    _connected()
    words = _refused_words()
    assert "LATET_DATA_DIR" in words and "trust that account" not in words  # never offered for such a group


@pytest.mark.parametrize("sid", [ENTRA, SERVICE, HELPDESK], ids=["entra-id", "service", "domain-group"])
def test_the_allowed_forms_are_trusted(windows, computer, monkeypatch, sid):
    _trust(monkeypatch, user=sid)
    _open_to(windows, computer, sid)
    assert folders.namespace_folder_problem(computer, computer.lstat()) is None


def test_doctor_lists_the_accounts_latet_trusted_accounts_trusts_and_where_it_is_set(windows, computer, monkeypatch):
    """R29 and R30: a setting the person did not make is seen, with where it is set."""
    _trust(monkeypatch, user="PC\\remoteadm", machine=HELPDESK)
    trusted = _titled("Trusted accounts")
    assert [check.status for check in trusted] == [checks.OK, checks.OK]
    details = " ".join(check.detail for check in trusted)
    assert f"PC\\remoteadm ({OTHER})" in details and "your user environment" in details
    assert f"ACME\\Desktop Admins ({HELPDESK})" in details and "the computer's environment" in details


# --- R30: read from the registry, never from the process's environment ----------------------------------------------


def test_a_value_in_the_process_environment_alone_changes_no_verdict(windows, computer, monkeypatch):
    """A repository's settings or an agent's session can set a process's variables, never the registry's: the value
    there trusts nothing, and `latet doctor` says it is not read."""
    monkeypatch.setenv("LATET_TRUSTED_ACCOUNTS", "PC\\remoteadm")
    _open_to(windows, computer, OTHER)
    assert folders.trusted_accounts() == ((), ())
    assert folders.namespace_folder_problem(computer, computer.lstat()) == CHANGEABLE
    said = _titled("Trusted accounts")
    assert [check.status for check in said] == [checks.WARN] and "not read" in said[0].detail
    assert "setx" in said[0].detail
    _connected()
    assert "remoteadm" in _refused_words()
    _trust(monkeypatch, user="PC\\remoteadm")
    assert base.load_records() == {}
    assert _titled("Trusted accounts")[0].status == checks.OK  # the same value in the registry: no warning


def test_the_helpers_launch_overlay_blanks_the_variable():
    assert "LATET_TRUSTED_ACCOUNTS" in claude_bridge._EMPTY_ENV
    env = claude_bridge._launch_env(Path("C:\\data\\connection\\claude-helper"), Path("C:\\Windows"))
    assert env["LATET_TRUSTED_ACCOUNTS"] == ""


def test_an_unreadable_registry_value_trusts_nothing_and_is_said(windows, computer, monkeypatch):
    def environment(scope):
        if scope == "user":
            raise OSError("it is not text")
        return "PC\\remoteadm"

    monkeypatch.setattr(folders, "environment_value", environment)
    named, unresolved = folders.trusted_accounts()
    assert [(item.sid, item.where) for item in named] == [(OTHER, "the computer's environment")]
    assert len(unresolved) == 1 and "your user environment" in unresolved[0] and "cannot be read" in unresolved[0]


# --- M5: lookups remembered for the process's life ----------------------------------------------------------------


@WINDOWS
def test_a_name_windows_does_not_find_is_looked_up_once(monkeypatch):
    asked = []

    def lookup(name):
        asked.append(name)
        raise OSError(None, "No mapping between account names and security IDs was done", None, 1332)

    monkeypatch.setattr(folders, "_lookup_account", lookup)
    monkeypatch.setattr(folders, "_looked_up", {})
    for _ in range(3):
        with pytest.raises(OSError, match="No mapping"):
            folders.account_sid("ACME\\offline")
    assert asked == ["ACME\\offline"]


# --- R27: the direct members of the local Administrators group ------------------------------------------------------


def test_a_direct_member_of_the_administrators_is_trusted_on_a_parent_like_the_group(windows, computer, monkeypatch):
    monkeypatch.setattr(folders, "administrators_members", lambda: (ME, HELPDESK))
    _open_to(windows, computer, HELPDESK)
    assert folders.namespace_folder_problem(computer, computer.lstat()) is None
    _connected()
    assert base.load_records() == {}
    members = _titled("Administrators' members")
    assert [check.status for check in members] == [checks.OK]
    assert f"ACME\\Desktop Admins ({HELPDESK})" in members[0].detail  # R29: the members it trusted, said
    assert "the parent folders" in members[0].detail


def test_a_member_is_never_trusted_on_the_data_folder_itself(windows, computer, monkeypatch):
    monkeypatch.setattr(folders, "administrators_members", lambda: (ME, HELPDESK))
    data = data_dir()
    data.mkdir()
    _open_to(windows, data, HELPDESK, owner=ME)
    assert "Desktop Admins" in _refused_words()


def test_members_that_cannot_be_read_trust_nothing_more_and_the_reason_is_said(windows, computer, monkeypatch):
    def unreadable():
        raise OSError(None, "The specified domain either does not exist or could not be contacted", None, 1355)

    monkeypatch.setattr(folders, "administrators_members", unreadable)
    _open_to(windows, computer, HELPDESK)
    assert folders.namespace_folder_problem(computer, computer.lstat()) == CHANGEABLE
    _connected()
    words = _refused_words()
    assert "the members of the Administrators group cannot be read" in words and "1355" in words


@pytest.mark.parametrize("member", ["S-1-5-11", "S-1-18-1", "S-1-5-4", DOMAIN_USERS, "S-1-5-21-0-0-0-497"])
def test_a_member_that_is_no_account_or_group_of_an_allowed_form_is_not_trusted(windows, computer, monkeypatch,
                                                                                member):  # fmt: skip
    monkeypatch.setattr(folders, "administrators_members", lambda: (ME, member))
    _open_to(windows, computer, member)
    assert folders.namespace_folder_problem(computer, computer.lstat()) == CHANGEABLE


# --- R28: local mode warns for the data folder's parents alone; the rest stays refused ------------------------------


@pytest.mark.parametrize("trusted", [False, True], ids=["untrusted", "named-but-owner"])
def test_a_parent_another_account_owns_stays_refused_in_both_modes(windows, computer, monkeypatch, trusted):
    if trusted:
        _trust(monkeypatch, user="PC\\remoteadm")  # a grant's trust, never an owner's
    windows[computer] = (OTHER, [folders.Grant(folders.SYSTEM, FULL, False)])
    data_dir().mkdir()
    local = _refused_words()
    assert f"{computer} belongs to another account (PC\\remoteadm ({OTHER}))" in local
    _connected()
    assert "belongs to another account" in _refused_words()


def test_an_entry_latet_cannot_read_stays_refused_in_local_mode(windows, computer):
    windows[computer] = (folders.SYSTEM, [folders.Grant(OTHER, FULL, False), folders.Grant("", 0, False)])
    data_dir().mkdir()
    assert "an access-list entry Latet cannot read" in _refused_words()


@pytest.fixture
def changeable(computer, monkeypatch):
    """Folders another account may change (synthetic, on every platform: their verdict alone is doubled, every other
    folder's is the real one); by default the data folder's parent, ``computer``. Returns the set to add to."""
    marked = {computer}
    real_problem, real_refusal = folders.namespace_folder_problem, folders.namespace_refusal

    def problem(folder, found, **kwargs):
        return CHANGEABLE if folder in marked else real_problem(folder, found, **kwargs)

    def refusal(folder, found, **kwargs):
        if folder in marked:
            return f"{folder} lets PC\\remoteadm ({OTHER}) change what is in it: remove that entry, or LATET_DATA_DIR"
        return real_refusal(folder, found, **kwargs)

    monkeypatch.setattr(folders, "namespace_folder_problem", problem)
    monkeypatch.setattr(folders, "namespace_refusal", refusal)
    return marked


def test_local_agents_apply_and_revert_go_on_past_a_parent_others_may_change(changeable, quiet):
    applied = run_cli("agents", "apply", "opencode")
    assert applied.exit_code == 0, applied.output
    assert "OpenCode: applied" in applied.output
    assert "opencode" in base.load_records()
    reverted = run_cli("agents", "revert", "opencode")
    assert reverted.exit_code == 0, reverted.output
    assert "OpenCode: removed" in reverted.output and base.load_records() == {}


def test_connected_agents_apply_is_refused_under_such_a_parent(changeable, quiet):
    _connected()
    refused = run_cli("agents", "apply", "opencode")
    assert refused.exit_code == 1 and "Integration records unavailable" in refused.output
    assert "remoteadm" in refused.output


def test_what_a_connection_is_kept_in_is_refused_in_local_mode_too(changeable):
    """`latet connect` admits its folders before a connection file exists: the server's token would be kept there,
    and the server's pinned certificate read from there, and the certificate an agent is given written there. Each
    refuses such a parent, connection file or not."""
    data_dir().mkdir()
    assert "remoteadm" in (connection.connection_folder_problem() or "")
    with pytest.raises(connection.NotConnected, match="remoteadm"):
        connection._server_pem(data_dir(), "sha256:" + "0" * 64, strict=True)
    with pytest.raises(connection.NotConnected, match="remoteadm"):
        connection._prepare_output(data_dir() / "connection", "the certificate Codex reads is not written")


def test_the_helpers_binding_is_read_and_written_as_on_a_connected_computer(changeable, monkeypatch):
    """M2: the helper's binding guards a connection: read or written, such a parent is refused in local mode too."""
    root = data_dir() / "connection" / "claude-helper"
    (root / "cwd").mkdir(parents=True)
    descriptor = root / "permanent.json"
    descriptor.write_text("{}")
    with pytest.raises(base.RecordsUnavailable, match="remoteadm"):
        claude_bridge._read_binding(descriptor, lambda raw, most: {}, {})
    descriptor.unlink()
    monkeypatch.setattr(connection, "_prepare_output", lambda *args, **kwargs: None)  # the write's own check alone
    prepared = connection._PreparedConnection(connection._selected_connection(), None)
    with pytest.raises(base.RecordsUnavailable, match="remoteadm"):
        claude_bridge._publish_binding(claude_bridge._BindingPlan(prepared, descriptor, "{}", "{}"))
    assert not descriptor.exists()


@WINDOWS
def test_the_helpers_installation_is_admitted_as_on_a_connected_computer(changeable, computer):
    """M2: the folders the helper runs code from are refused under such a parent in local mode too."""
    install = computer / "install"
    install.mkdir()
    with pytest.raises(connection.NotConnected, match="remoteadm") as refused:
        claude_bridge._install_path(install, directory=True)
    assert "LATET_TRUSTED_ACCOUNTS" in refused.value.message  # M4: whom its parents may trust, said
    assert "direct members" in refused.value.message


def test_a_folder_inside_the_data_folder_is_never_gone_past(computer, changeable):
    """I2 and M2: only the folders above the data folder: one inside it (the backups, the pin store) is refused in
    local mode, as a parent and as the folder checked."""
    data = data_dir()
    (data / "backups" / "20261019-120000").mkdir(parents=True)
    changeable.clear()
    changeable.add(data / "backups")
    with pytest.raises(base.RecordsUnavailable):
        base._trusted_folder(data / "backups" / "20261019-120000")
    changeable.clear()
    changeable.add(data)
    with pytest.raises(base.RecordsUnavailable):
        base._trusted_folder(data / "backups")


def test_a_folder_outside_the_data_folders_chain_is_never_gone_past(computer, changeable):
    """I2: a folder another account may change that is no parent of the data folder (an agent's settings elsewhere)
    is refused in local mode."""
    elsewhere = computer / "elsewhere" / "settings"
    elsewhere.mkdir(parents=True)
    data_dir().mkdir()
    changeable.clear()
    changeable.add(computer / "elsewhere")
    with pytest.raises(base.RecordsUnavailable):
        base._namespace(elsewhere)


def test_a_parent_of_the_data_folder_checked_as_the_folder_itself_is_refused(computer, changeable):
    """M2: the folder asked for is never gone past, even when it is above the data folder."""
    data_dir().mkdir()
    with pytest.raises(base.RecordsUnavailable):
        base._namespace(computer)


@POSIX
def test_on_posix_a_parent_its_group_may_write_is_warned_in_local_mode_and_refused_connected(computer, monkeypatch):
    monkeypatch.setattr(folders, "_group_is_mine_alone", lambda gid: False)  # a group other accounts are in
    shared = computer / "shared"
    shared.mkdir()
    shared.chmod(0o775)
    monkeypatch.setenv("LATET_DATA_DIR", str(shared / "data"))
    data_dir().mkdir(mode=0o700)
    assert base.load_records() == {}
    warned = _titled("Data folder")
    assert len(warned) == 1 and str(shared) in warned[0].detail and "0775" in warned[0].detail
    assert "LATET_TRUSTED_ACCOUNTS" not in warned[0].detail  # it changes nothing here
    _connected()
    assert str(shared) in _refused_words()


@POSIX
def test_on_posix_latet_trusted_accounts_is_said_to_change_nothing(computer, monkeypatch):
    monkeypatch.setenv("LATET_TRUSTED_ACCOUNTS", "remoteadm")
    said = _titled("Trusted accounts")
    assert len(said) == 1 and said[0].status == checks.WARN and "changes nothing" in said[0].detail


# --- Windows' own API, read only --------------------------------------------------------------------------------------


@WINDOWS
def test_the_administrators_members_are_read_by_the_groups_sid(monkeypatch):
    members = folders.administrators_members.__wrapped__()
    assert isinstance(members, tuple) and all(re.fullmatch(r"S-1-[0-9-]+", sid) for sid in members)
    # The group's name is the one Windows gives its SID here (localised), never one written in Latet.
    monkeypatch.setattr(folders, "_account", lambda sid: "BUILTIN\\Latet-no-such-group-7f3a")
    with pytest.raises(OSError, match="NetLocalGroupGetMembers refused, status 2220"):  # NERR_GroupNotFound
        folders.administrators_members.__wrapped__()


@WINDOWS
def test_an_account_name_is_looked_up_to_its_sid():
    me = folders.my_sid()
    assert folders._lookup_account(folders._account(me)) == me
    with pytest.raises(OSError, match="1332"):  # ERROR_NONE_MAPPED
        folders._lookup_account("Latet-no-such-account-7f3a")
    assert folders.canonical_sid("s-1-5-32-544") == folders.ADMINISTRATORS


@WINDOWS
def test_the_registrys_environments_are_read_only_as_text():
    """Read only: whatever the person set, a text or nothing; the registry is never written."""
    for scope in ("machine", "user"):
        value = folders._read_environment(scope)
        assert value is None or isinstance(value, str)
