"""A computer's ended launches do not stay for ever, and a recovered launch is claimed until its hold is back (the review's
Minors 1 and 2 on P2-6a): a tombstone a launcher killed without its last step left (a closed terminal) is forgotten
after a day, or, oldest first, when the registry is full, so OPEN never stays at launch-capacity until the daemon
restarts; a key whose record was forgotten is then decided as claimed, which only tightens. A request between a
recovery's commit and its hold's acknowledgment is decided as claimed, never refused for the key alone (R-107)."""

from __future__ import annotations

import pytest

from latet.gateway import launch_keys
from latet.gateway.launch_keys import Binding, LocalLaunchKeys
from latet.gateway.local_launch import take_local_launch


def _binding() -> Binding:
    return Binding(b"/registered", b"/registered", "a" * 12)


def _ended(owner: LocalLaunchKeys) -> str:
    """A launch opened, held, and ended without its child's exit confirmed: the launcher was killed."""
    opened = owner.commit_open(owner.begin_open("computer"), _binding())
    hold = owner.reserve_hold(opened["launch_id"], "computer", 0)
    hold.acknowledge()
    assert owner.end(opened["launch_id"], "computer")
    hold.close()
    assert owner.verify(opened["key"], "computer").code == "ended"
    return opened["key"]


def test_a_tombstone_is_forgotten_after_a_day_and_its_key_then_claimed():
    now = [1000.0]
    owner = LocalLaunchKeys(b"\x21" * 32, clock=lambda: now[0])
    try:
        key = _ended(owner)
        now[0] += launch_keys.TOMBSTONE_SECONDS - 1
        assert owner.verify(key, "computer").code == "ended", "within a day it is refused"
        now[0] += 2
        assert owner.verify(key, "computer").code == "unknown", "past a day its record is gone: claimed"
        assert owner.counts["entries"] == 0
    finally:
        owner.close()


def test_a_full_registry_forgets_its_oldest_tombstone_for_a_new_launch(monkeypatch):
    monkeypatch.setattr(launch_keys, "ENTRIES", 3)
    now = [1000.0]
    owner = LocalLaunchKeys(b"\x22" * 32, clock=lambda: now[0])
    try:
        keys = []
        for _ in range(3):
            keys.append(_ended(owner))
            now[0] += 1
        assert owner.counts["entries"] == 3
        opened = owner.commit_open(owner.begin_open("computer"), _binding())  # never launch-capacity for tombstones
        assert opened["key"] not in keys
        assert owner.verify(keys[0], "computer").code == "unknown", "the oldest tombstone gave its place"
        assert [owner.verify(key, "computer").code for key in keys[1:]] == ["ended", "ended"]
    finally:
        owner.close()


def test_live_launches_still_fill_the_registry(monkeypatch):
    monkeypatch.setattr(launch_keys, "ENTRIES", 2)
    owner = LocalLaunchKeys(b"\x23" * 32)
    try:
        for _ in range(2):
            owner.commit_open(owner.begin_open("computer"), _binding())
        with pytest.raises(launch_keys.LaunchRefused, match="launch-capacity"):
            owner.begin_open("computer")
    finally:
        owner.close()


def test_a_request_between_a_recovery_and_its_hold_is_claimed_not_refused():
    owner = LocalLaunchKeys(b"\x24" * 32)
    restarted = LocalLaunchKeys(b"\x24" * 32)
    try:
        opened = owner.commit_open(owner.begin_open("computer"), _binding())
        fence = restarted.prepare()
        ticket = restarted.begin_recovery(
            opened["key"], opened["launch_id"], "computer", fence["epoch"], fence["retirement"]
        )
        restarted.commit_recovery(ticket, _binding())
        assert restarted.verify(opened["key"], "computer").code == "recovering"
        scope = {
            "type": "http",
            "path": "/v1/x",
            "raw_path": b"/v1/x",
            "headers": [(b"x-latet-launch-key", opened["key"].encode())],
        }
        assert take_local_launch(scope, restarted, computer=True).status == "claimed"
    finally:
        owner.close()
        restarted.close()


def test_a_new_launchs_key_before_its_hold_is_still_refused():
    """Its agent has not started before the hold's acknowledgment: a request with its key is not the launch's."""
    owner = LocalLaunchKeys(b"\x25" * 32)
    try:
        opened = owner.commit_open(owner.begin_open("computer"), _binding())
        assert owner.verify(opened["key"], "computer").code == "pending"
        scope = {
            "type": "http",
            "path": "/v1/x",
            "raw_path": b"/v1/x",
            "headers": [(b"x-latet-launch-key", opened["key"].encode())],
        }
        assert take_local_launch(scope, owner, computer=True).status == "pending"
    finally:
        owner.close()


def test_a_request_while_its_launch_is_registered_again_is_claimed_not_refused():
    """P2-6a's re-review, R-6: between a restarted daemon's recovery and its commit (the folder staged meanwhile), the
    agent runs and its requests are claims, never refused for the key alone (R-107)."""
    owner = LocalLaunchKeys(b"\x28" * 32)
    restarted = LocalLaunchKeys(b"\x28" * 32)
    try:
        opened = owner.commit_open(owner.begin_open("computer"), _binding())
        fence = restarted.prepare()
        ticket = restarted.begin_recovery(
            opened["key"], opened["launch_id"], "computer", fence["epoch"], fence["retirement"]
        )
        try:
            assert restarted.verify(opened["key"], "computer").code == "recovering"
            scope = {
                "type": "http",
                "path": "/v1/x",
                "raw_path": b"/v1/x",
                "headers": [(b"x-latet-launch-key", opened["key"].encode())],
            }
            assert take_local_launch(scope, restarted, computer=True).status == "claimed"
        finally:
            restarted.abort(ticket)
    finally:
        owner.close()
        restarted.close()
