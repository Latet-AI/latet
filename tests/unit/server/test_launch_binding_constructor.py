"""Direct ancestry compatibility for compact protected launch bindings."""

from typing import Literal

import pytest

from latet.server.launch_binding import Binding, _text
from latet.server.launch_records import LaunchUnavailable


@pytest.mark.parametrize(
    ("folder", "root", "flavor", "canonical_root", "accepted"),
    [
        pytest.param("/repo", "/repo", "posix", True, True, id="posix-equality"),
        pytest.param("/repo", "/", "posix", True, True, id="posix-anchor"),
        pytest.param("C:\\İ\\child", "C:\\i\u0307", "windows", True, True, id="windows-lower-expansion"),
        pytest.param("/repository", "/repo", "posix", True, False, id="posix-prefix-only"),
        pytest.param("/Repo/x", "/repo", "posix", True, False, id="posix-case-mismatch"),
        pytest.param(r"C:\repository", r"C:\repo", "windows", True, False, id="windows-prefix-only"),
        pytest.param(r"C:\Straße\x", r"C:\STRASSE", "windows", True, False, id="windows-casefold-only"),
        pytest.param(r"\\host\other\x", "\\\\host\\share\\", "windows", True, False, id="unc-other-share"),
        pytest.param("/repo/x", "/repo//", "posix", False, False, id="noncanonical-root"),
    ],
)
def test_binding_constructor_ancestry(
    folder: str, root: str, flavor: Literal["posix", "windows"], canonical_root: bool, accepted: bool
) -> None:
    assert str(_text(folder, flavor)) == folder
    if canonical_root:
        assert str(_text(root, flavor)) == root
    else:
        with pytest.raises(LaunchUnavailable):
            _text(root, flavor)

    project = "a" * 12
    if accepted:
        binding = Binding(folder, root, project, "client", flavor)
        assert binding.fields() == (folder.encode(), root.encode(), project.encode(), b"client", flavor.encode())
    else:
        with pytest.raises(LaunchUnavailable):
            Binding(folder, root, project, "client", flavor)
