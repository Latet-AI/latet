"""The Enterprise Edition's policy is a server's (R0): a computer without a server is the Community Edition, with no
floor whatever its folders hold, a license included. A server's edition is told when the floor changes, and
tells every runtime watching; the Policy page is for whoever has policy.read on a server."""

import time
from types import SimpleNamespace

import pytest

from latet.edition import Community, EditionContext, load_edition
from latet.ee import edition as edition_module
from latet.ee.policy import parse_policy
from latet.ee.policy_sources import policy_page, source_view

from .editions import memory_edition

POLICY = "latet_rules: 1\nprofile: max\n"


def test_a_computer_has_no_floor_even_with_a_policy_and_a_license_in_its_folders(tmp_path, licensed):
    data = tmp_path / "data"
    data.mkdir()
    (data / "license.json").write_bytes(licensed.make(expires="2099-12-31"))  # read by nothing
    (data / "policy.yaml").write_text(POLICY, "utf-8")
    context = EditionContext(data=tmp_path / "data", config_dir=tmp_path / "config", server=None, clock=time.time)
    edition = load_edition(context)
    try:
        assert isinstance(edition, Community) and edition.floor(None) is None  # the Enterprise Edition is a server's
        assert "policy" not in edition.me(None) and edition.me(None)["pages"] == []
        told = []
        stop = edition.watch(lambda: told.append("runtime"))
        stop()
        assert told == []
    finally:
        edition.close()


def test_the_policy_page_is_for_policy_read_on_a_server():
    emily = SimpleNamespace(id="u_7f3a2b9c1d", name="Emily Carter", kind="person")
    in_force = {"policy": {"sources": [], "problems": []}, "actions": []}
    assert not policy_page(emily, in_force, licensed=True)  # a server, before E3 gives policy.read
    assert policy_page(emily, {**in_force, "actions": ["policy.read"]}, licensed=True)
    reader = {**in_force, "actions": ["policy.read"]}
    assert not policy_page(None, reader, licensed=True)  # without an identity it is never listed
    assert not policy_page(emily, {"policy": None, "actions": ["policy.read"]}, licensed=False)  # nothing to show


@pytest.fixture
def sourced(tmp_path, monkeypatch, licensed):
    """A server's Enterprise Edition (its license in memory) whose policy is the test's ``Source``, and how the edition
    made it."""
    edition, _, source = memory_edition(monkeypatch, tmp_path)
    yield edition, source, source.made
    edition.close()


def test_the_edition_answers_the_floor_its_policy_holds_for_every_identity(sourced):
    edition, source, _ = sourced
    floor = parse_policy(POLICY, source="server")
    source.held = floor
    emily = SimpleNamespace(id="u_7f3a2b9c1d", name="Emily Carter", kind="person")
    assert edition.floor(None) is floor and edition.floor(emily) is floor


def test_the_policy_tells_every_runtime_watching_through_the_edition(sourced):
    edition, source, made = sourced
    told = []
    edition.watch(lambda: told.append("runtime"))
    assert "start" not in source.calls  # watch starts nothing: the policy runs from the edition's start (F7)
    made["changed"]()  # the policy's floor changed
    assert told == ["runtime"]
    assert made["licensed"]() is False  # weighed at each look: no license here
    assert "record" not in made  # R9: made without a recorder, E4's license job records one event a change


def test_a_refresh_of_the_edition_looks_at_its_policy_again_and_closing_closes_it(sourced):
    edition, source, _ = sourced
    edition.refresh()
    assert source.calls == ["refresh"]
    edition.close()
    assert source.calls == ["refresh", "close"]


def test_me_says_the_policy_and_lists_its_page_for_policy_read(sourced, monkeypatch):
    edition, source, _ = sourced
    said = {"sources": [source_view(parse_policy(POLICY, source="server", origin="https://latet.acme.lan"))]}
    source.said = {**said, "problems": []}
    assert edition.me(None)["policy"] == source.said and "policy" not in edition.me(None)["pages"]
    monkeypatch.setattr(edition_module, "policy_page", lambda identity, answer, licensed, reads=None: True)
    assert edition.me(None)["pages"][-1] == "policy"


def test_the_policy_page_is_listed_from_the_actions_the_identity_is_given(sourced, monkeypatch):
    """Whole-branch review, Minor 6: the page is weighed once the actions are there (E3 gives them at ``_access``),
    not from the empty list written before them."""
    edition, source, _ = sourced
    source.said = {"sources": [], "problems": [{"level": "fail", "text": "unreadable"}]}
    emily = SimpleNamespace(id="u_7f3a2b9c1d", name="Emily Carter", kind="person")
    assert "policy" not in edition.me(emily)["pages"] and edition.me(emily)["actions"] == []
    monkeypatch.setattr(edition, "_access", lambda identity: (["security"], ["policy.read"]))
    me = edition.me(emily)
    assert me["pages"][-1] == "policy" and (me["roles"], me["actions"]) == (["security"], ["policy.read"])


class Unanswered:
    """A license store that never answers (a server's database down since the instance started)."""

    def read(self, *, conn=None):
        raise ConnectionError("connection to 10.0.0.5 refused")


class Nothing:
    """A license store with no license."""

    def read(self, *, conn=None):
        return None


def test_the_policy_weighs_the_license_as_unknown_until_this_instance_ever_read_it(sourced):
    """L4: a license never read says neither yes nor no, so that a stored policy builds no space meanwhile."""
    from latet.ee.license import Licensing

    edition, _, made = sourced
    edition.licensing = Licensing(Unanswered())
    assert made["licensed"]() is None and not edition.licensing.knows()
    edition.licensing = Licensing(Nothing())
    assert made["licensed"]() is False and edition.licensing.knows()


def test_a_floors_source_is_said_without_its_notices_words():
    floor = parse_policy("latet_rules: 1\nexceptions: everyone\nprofile: max\n", source="server", received=1.0)
    assert floor.notices  # a notice of the file's, whose words stay with the administrators (M6)
    assert source_view(floor) == {"source": "server", "origin": "", "received": 1.0, "notices": 1}
