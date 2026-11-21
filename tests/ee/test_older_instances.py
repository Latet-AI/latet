"""What Latet Enterprise says while instances alive run another release or edition than this one's (M4 of the merge
review of 0.11.0): that they do not apply the policy (``_not_applying``), route no approval (``not_routing``) or mask no
synced term (``not_syncing``), each counted from what the instances say of themselves (``instances_said``). A listing
cut at its bound may leave an instance alive out of it, so the count is then a lower bound, said "at least"."""

from types import SimpleNamespace

import pytest

from latet import __version__
from latet.ee.approvals_api import not_routing
from latet.ee.policy_routes import _not_applying
from latet.ee.sync.api import not_syncing
from latet.server import app as app_module

SERVER = SimpleNamespace(edition=SimpleNamespace(name="enterprise"))
THIS = f"{__version__} enterprise"
# A release before instances said their release ("1"), and this release without the Enterprise Edition.
OLDER = ("1", f"{__version__} community")
WARNINGS = [
    (_not_applying, "not apply the policy (a release of Latet before locked rules"),
    (not_routing, "not run this release of Latet with the Enterprise Edition: one of a release before central"),
    (not_syncing, "not run this release of Latet with the Enterprise Edition: it masks no synced term"),
]


def _said(monkeypatch, rows, whole):
    monkeypatch.setattr(app_module, "instances_said", lambda server: (list(rows), whole))


@pytest.mark.parametrize(("warning", "words"), WARNINGS)
def test_a_listing_cut_at_its_bound_counts_at_least_the_instances_it_lists(warning, words, monkeypatch):
    _said(monkeypatch, (THIS, *OLDER), False)
    [problem] = warning(SERVER)
    assert problem.startswith(f"at least 2 instances do {words}"), problem
    _said(monkeypatch, (THIS, OLDER[0]), False)
    [problem] = warning(SERVER)
    assert problem.startswith(f"at least 1 instance does {words}"), problem


@pytest.mark.parametrize(("warning", "words"), WARNINGS)
def test_a_listing_read_whole_counts_the_instances_it_lists(warning, words, monkeypatch):
    _said(monkeypatch, (THIS, *OLDER), True)
    [problem] = warning(SERVER)
    assert problem.startswith(f"2 instances do {words}"), problem
    _said(monkeypatch, (THIS, OLDER[1]), True)
    [problem] = warning(SERVER)
    assert problem.startswith(f"1 instance does {words}"), problem
    _said(monkeypatch, (THIS,), True)
    assert warning(SERVER) == []  # read whole, and none differs: nothing to say


@pytest.mark.parametrize(("warning", "words"), WARNINGS)
def test_a_cut_listing_whose_instances_all_run_this_release_still_says_those_it_does_not_show_may_not(
    warning, words, monkeypatch
):
    """Fix round 1 of the release minors: a listing cut at its bound where every instance listed runs this release with
    the Enterprise Edition still says that an instance it does not show may not, since one alive and not listed may be
    of another release or edition."""
    _said(monkeypatch, (THIS,), False)
    [problem] = warning(SERVER)
    assert problem.startswith(f"an instance the list of instances does not show (it was cut at its bound) may {words}")
    _said(monkeypatch, (), False)
    [problem] = warning(SERVER)
    assert problem.startswith("an instance the list of instances does not show"), problem
