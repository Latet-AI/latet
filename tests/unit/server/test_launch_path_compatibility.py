"""Finite canonical path and configured-root compatibility for protected OPEN."""

import pytest

from latet.config import ProjectEntry
from latet.gateway.launch_keys import LaunchRefused
from latet.projects import project_id
from latet.server import launch_open
from latet.server.launch_binding import _text
from latet.server.launch_records import LaunchUnavailable


@pytest.mark.parametrize(
    ("path", "flavor", "accepted"),
    [
        pytest.param("/", "posix", True, id="posix-root"),
        pytest.param(r"/a\b", "posix", True, id="posix-literal-backslash"),
        pytest.param("/a/", "posix", False, id="posix-trailing-slash"),
        pytest.param("/a//b", "posix", False, id="posix-repeated-slash"),
        pytest.param("/a/./b", "posix", False, id="posix-dot"),
        pytest.param("/a/../b", "posix", False, id="posix-dotdot"),
        pytest.param("//host/share", "posix", False, id="posix-double-anchor"),
        pytest.param("C:\\", "windows", True, id="windows-drive-anchor"),
        pytest.param("\\\\srv\\share\\", "windows", True, id="windows-unc-anchor"),
        pytest.param(r"c:\a", "windows", False, id="windows-lower-drive"),
        pytest.param("C:/a", "windows", False, id="windows-forward-slash"),
        pytest.param("C:\\a\\", "windows", False, id="windows-trailing-slash"),
        pytest.param(r"\\srv\share", "windows", False, id="windows-unc-missing-anchor-slash"),
        pytest.param(r"C:\NUL.txt", "windows", False, id="windows-device"),
        pytest.param(r"C:\a.", "windows", False, id="windows-trailing-dot"),
        pytest.param(r"C:\a ", "windows", False, id="windows-trailing-space"),
        pytest.param(r"C:\a:b", "windows", False, id="windows-segment-colon"),
        pytest.param(r"\\?\C:\a", "windows", False, id="windows-extended-namespace"),
    ],
)
def test_launch_canonical_path(path: str, flavor: str, accepted: bool) -> None:
    if accepted:
        assert str(_text(path, flavor)) == path
    else:
        with pytest.raises(LaunchUnavailable):
            _text(path, flavor)


@pytest.mark.parametrize(
    ("folder", "parent", "flavor", "accepted"),
    [
        pytest.param("/repo/child", "/repo", "posix", True, id="posix-descendant"),
        pytest.param("/repository/child", "/repo", "posix", False, id="posix-prefix-only"),
        pytest.param("/Repo/child", "/repo", "posix", False, id="posix-case-distinct"),
        pytest.param(r"C:\REPO\child", r"C:\Repo", "windows", True, id="windows-lowercase-comparison"),
        pytest.param(r"C:\Repository\child", r"C:\Repo", "windows", False, id="windows-prefix-only"),
        pytest.param("C:\\İ\\child", "C:\\i\u0307", "windows", True, id="windows-dotted-i-lower-expands"),
        pytest.param(r"C:\STRASSE\child", r"C:\Straße", "windows", False, id="windows-no-casefold"),
        pytest.param(r"\\SRV\SHARE\child", "\\\\srv\\share\\", "windows", True, id="unc-share-anchor"),
        pytest.param(r"\\SRV\OTHER\child", "\\\\srv\\share\\", "windows", False, id="unc-other-share"),
    ],
)
def test_launch_configured_ancestry(folder: str, parent: str, flavor: str, accepted: bool) -> None:
    data = {"folder": folder, "root": None, "flavor": flavor}
    entries = [ProjectEntry(path=parent)]
    if accepted:
        binding = launch_open._binding(data, entries)
        assert (binding.folder, binding.root, binding.flavor, binding.source) == (folder, parent, flavor, "server")
    else:
        with pytest.raises(LaunchRefused) as refused:
            launch_open._binding(data, entries)
        assert refused.value.code == "launch-unregistered"


@pytest.mark.parametrize(
    ("folder", "root", "flavor", "paths", "collide", "stored_label", "selected"),
    [
        pytest.param(
            "/repo/deep/child",
            None,
            "posix",
            ("/repo", "/repo/deep"),
            False,
            False,
            "/repo/deep",
            id="nearest-shallow-first",
        ),
        pytest.param(
            "/repo/deep/child",
            None,
            "posix",
            ("/repo/deep", "/repo"),
            False,
            False,
            "/repo/deep",
            id="nearest-deep-first",
        ),
        pytest.param(
            r"C:\Repo\child",
            None,
            "windows",
            (r"C:\Repo", r"C:\REPO"),
            False,
            False,
            None,
            id="windows-case-duplicate",
        ),
        pytest.param("/Repo/child", None, "posix", ("/repo", "/Repo"), False, False, "/Repo", id="posix-case-distinct"),
        pytest.param(
            "/repo/deep/child",
            None,
            "posix",
            ("/repo", "/repo/deep"),
            True,
            False,
            "/repo/deep",
            id="collision-distinct",
        ),
        pytest.param(
            r"C:\REPO\child",
            None,
            "windows",
            (r"C:\Other", r"C:\Repo"),
            True,
            False,
            r"C:\Repo",
            id="windows-collision-distinct",
        ),
        pytest.param(
            "/repo/child",
            None,
            "posix",
            ("/repo", "/other", "/repo"),
            True,
            False,
            None,
            id="collision-duplicate",
        ),
        pytest.param(
            "/repo/child",
            None,
            "posix",
            ("/repo", "/other", "/bad/./root"),
            True,
            False,
            "/repo",  # I-7: a folder not in a form a launch compares is left out, never failing the others
            id="collision-late-incomparable-left-out",
        ),
        pytest.param(
            r"C:\REPO\child",
            None,
            "windows",
            (r"C:\Other", r"C:\Repo"),
            False,
            True,
            r"C:\Repo",
            id="stored-label",
        ),
        pytest.param("/repo/child", "/other", "posix", ("/repo",), False, False, None, id="explicit-outside-root"),
    ],
)
def test_launch_configured_selection(
    monkeypatch: pytest.MonkeyPatch,
    folder: str,
    root: str | None,
    flavor: str,
    paths: tuple[str, ...],
    collide: bool,
    stored_label: bool,
    selected: str | None,
) -> None:
    data = {"folder": folder, "root": root, "flavor": flavor}
    entries = [ProjectEntry(path=path) for path in paths]
    with monkeypatch.context() as scoped:
        if collide:
            scoped.setattr(launch_open, "hash", lambda _value: 0, raising=False)
        if selected is None:
            with pytest.raises(LaunchRefused) as refused:
                launch_open._binding(data, entries, stored_label=stored_label)
            assert refused.value.code == "launch-invalid"
        else:
            binding = launch_open._binding(data, entries, stored_label=stored_label)
            assert (binding.folder, binding.root, binding.flavor, binding.source) == (
                folder,
                selected,
                flavor,
                "server",
            )
            assert binding.project == ("0" * 12 if stored_label else project_id(selected))


@pytest.mark.parametrize(
    ("written", "read"),
    [
        pytest.param("/home/u/other/", "/home/u/other", id="posix-trailing-slash"),
        pytest.param("/home/u/other//", "/home/u/other", id="posix-trailing-slashes"),
        pytest.param("/", "/", id="posix-root"),
        pytest.param("C:/Users/u/x", r"C:\Users\u\x", id="windows-forward-slashes"),
        pytest.param("c:\\Users\\u\\x\\", r"C:\Users\u\x", id="windows-lower-drive-trailing"),
        pytest.param("C:\\", "C:\\", id="windows-drive-anchor"),
        pytest.param(r"\\srv\share", "\\\\srv\\share\\", id="unc-share-anchor"),
        pytest.param("//srv/share/dir/", r"\\srv\share\dir", id="unc-forward-slashes"),
    ],
)
def test_project_entry_paths_are_canonical_where_settings_are_read(written: str, read: str) -> None:
    """I-7: a project's folder is read in the canonical form its machine compares (trailing separators, the drive
    letter's case, slashes), so that a launch's OPEN compares it as Latet's projects do (``norm_path``)."""
    assert ProjectEntry(path=written).path == read
    assert project_id(ProjectEntry(path=written).path) == project_id(written)  # the same project as before


@pytest.mark.parametrize("other", ["/home/u/other/", "C:/Users/u/x", "c:\\Users\\u\\y\\"])
def test_an_unrelated_entry_in_another_form_never_fails_the_open(other: str) -> None:
    data = {"folder": "/home/u/repo/src", "root": None, "flavor": "posix"}
    binding = launch_open._binding(data, [ProjectEntry(path="/home/u/repo"), ProjectEntry(path=other)])
    assert (binding.root, binding.source) == ("/home/u/repo", "server")


@pytest.mark.parametrize("bad", ["/home/u/./other", "relative/folder", "\\\\?\\C:\\x"])
def test_an_entry_that_cannot_be_compared_is_left_out_and_said(bad: str, caplog) -> None:
    data = {"folder": "/home/u/repo/src", "root": None, "flavor": "posix"}
    entries = [ProjectEntry(path=bad), ProjectEntry(path="/home/u/repo")]
    with caplog.at_level("WARNING", logger="latet.server.launch_open"):
        binding = launch_open._binding(data, entries)
    assert binding.root == "/home/u/repo"
    assert [record.getMessage() for record in caplog.records] == [
        "1 project folder of the settings is not in a form a launch compares: left out of this launch's projects"
    ]
    assert bad not in caplog.text
