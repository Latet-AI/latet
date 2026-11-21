"""A server's policy: checked, stored once for every instance, and announced once the change commits. Each instance
reads it again when told or at its next check, weighs the license only then, keeps the floor it read last when it
cannot read a new one, and builds no space below a policy it never read."""

import logging
import threading

import pytest

from latet.ee import policy_server as policy_server_module
from latet.ee.policy import PolicyError
from latet.ee.policy_server import CHANNEL, PolicyUnavailable, ServerPolicy, StoredPolicy
from latet.floor import FLOOR_UNAVAILABLE, FloorUnavailable

POLICY = "latet_rules: 1\nexceptions: company\nprofile: max\nterms: [{value: Project Falcon, type: PROJECT}]\n"
NOW = 1_791_191_520.0


class Store:
    """ee_policy in memory: one row (the sealing and the transaction are the real store's, Task 6)."""

    def __init__(self):
        self.kept = None
        self.conns = []

    def stamp(self):
        return None if self.kept is None else (self.kept.etag, self.kept.set_at)

    def load(self):
        return self.kept

    def save(self, policy, *, conn=None):
        self.conns.append(conn)
        self.kept = policy

    def delete(self, *, conn=None):
        self.conns.append(conn)
        had, self.kept = self.kept is not None, None
        return had


def _instance(store, licensed=None, changed=None, every=None, record=None):
    """One instance's view of the server's policy, not started: a refresh stands for a message on ee:policy."""
    licensed = licensed or [True]
    published = []
    policy = ServerPolicy(
        store, publish=lambda channel, message: published.append((channel, dict(message))),
        origin="https://latet.acme.lan", licensed=lambda: licensed[0], changed=changed or (lambda: None),
        record=record or (lambda event: None), clock=lambda: NOW, every=every,
    )  # fmt: skip
    return policy, published


def _set(policy, text=POLICY, conn="the route's transaction"):
    floor = policy.set(text, actor="admin:t_ab12", conn=conn)
    policy.committed()
    return floor


def test_a_policy_set_is_written_in_the_callers_transaction_and_announced_once_committed():
    store = Store()
    policy, published = _instance(store)
    floor = policy.set(POLICY, actor="admin:t_ab12", conn="the route's transaction")
    assert floor.rules.profile == "max" and floor.exceptions == "company" and floor.source == "server"
    assert (store.kept.text, store.kept.etag, store.kept.set_by) == (POLICY, floor.etag, "admin:t_ab12")
    assert store.conns == ["the route's transaction"]
    assert published == [] and policy.floor() is None  # not committed yet: nobody told, this instance's floor unchanged
    policy.committed()
    assert published == [(CHANNEL, {})] and policy.floor().etag == floor.etag
    assert "Project Falcon" not in repr(published)


def test_once_committed_this_instance_reads_it_before_the_others_are_told_and_says_whether_they_were():
    store, seen = Store(), []
    told = [True]

    def publish(channel, message):
        seen.append(policy.floor())  # what this instance holds when the others are told
        return told[0]

    policy = ServerPolicy(
        store, publish=publish, origin="https://latet.acme.lan", licensed=lambda: True, clock=lambda: NOW
    )
    floor = policy.set(POLICY, actor="admin:t_ab12")
    assert policy.committed() is True and seen[0] is not None and seen[0].etag == floor.etag
    told[0] = False  # Valkey did not take the message
    policy.set(POLICY + "ocr: auto\n", actor="admin:t_ab12")
    assert policy.committed() is False


def test_a_policy_is_saved_only_with_the_floor_read_from_its_own_text():
    store = Store()
    policy, _ = _instance(store)
    floor = policy.read(POLICY)
    assert store.kept is None  # read: nothing written
    with pytest.raises(ValueError, match="not read from this text"):
        policy.save(POLICY + "ocr: off\n", floor, actor="admin:t_ab12")
    assert store.kept is None
    policy.save(POLICY, floor, actor="admin:t_ab12")
    assert (store.kept.text, store.kept.etag, store.kept.set_at) == (POLICY, floor.etag, floor.received)


def test_a_policy_that_cannot_be_read_changes_nothing():
    store = Store()
    policy, published = _instance(store)
    with pytest.raises(PolicyError, match="not valid YAML"):
        policy.set("profile: [max", actor="admin:t_ab12")
    assert store.kept is None and store.conns == [] and published == [] and policy.floor() is None


def test_another_instance_reads_the_new_policy_when_told_and_tells_its_spaces():
    store, told = Store(), []
    first, _ = _instance(store)
    second, _ = _instance(store, changed=lambda: told.append("spaces"))
    assert second.floor() is None
    _set(first)
    assert second.floor() is None  # not told yet
    assert second.refresh() is True and told == ["spaces"]  # the message on ee:policy, or its next check
    assert second.floor().rules.profile == "max"
    assert second.refresh() is False and told == ["spaces"]


def test_an_instance_that_cannot_read_a_new_policy_keeps_the_last_and_one_that_never_read_one_builds_no_space():
    store = Store()
    first, _ = _instance(store)
    _set(first)
    store.kept = StoredPolicy("latet_rules: 99\nprofile: max\n", "9f0e1d2c3b4a5968", NOW + 60, "admin:t_cd34")
    assert first.refresh() is False  # a newer Latet's policy: the floor read last stays in force
    assert first.floor().rules.profile == "max"
    [problem] = first.summary()["problems"]
    assert problem["level"] == "fail" and "newer Latet" in problem["text"]
    started, _ = _instance(store)  # an instance that starts now
    with pytest.raises(PolicyUnavailable, match="newer Latet"):
        started.floor()


def test_removing_the_policy_frees_every_space_once_committed():
    store, told = Store(), []
    policy, published = _instance(store, changed=lambda: told.append("spaces"))
    _set(policy)
    assert policy.remove(conn="the route's transaction") is True
    policy.committed()
    assert policy.floor() is None and told == ["spaces", "spaces"] and published[-1] == (CHANNEL, {})
    assert policy.remove() is False


def test_a_license_that_comes_or_goes_brings_the_stored_policy_in_or_out_at_the_next_look():
    store, licensed = Store(), [True]
    policy, _ = _instance(store, licensed=licensed)
    _set(policy)
    licensed[0] = False  # the license removed (or one without locked rules installed): taken at the next look
    assert policy.floor() is not None  # never weighed when asked (R4)
    assert policy.refresh() is True and policy.floor() is None
    assert "not in force" in policy.summary()["problems"][0]["text"]
    view = policy.admin_view()
    assert view["policy"]["in_force"] is False and view["problems"] == [
        "not in force: no license switches locked rules on"
    ]
    licensed[0] = True
    assert policy.refresh() is True and policy.floor().rules.profile == "max"


def test_the_floor_never_weighs_the_license_when_asked():
    store, weighs = Store(), []
    policy, _ = _instance(store)
    _set(policy)
    policy._licensed = lambda: weighs.append(1) or True
    for _ in range(100):
        assert policy.floor().rules.profile == "max"
    assert weighs == []


def test_the_administrators_view_says_when_and_by_whom_and_holds_the_text():
    store = Store()
    policy, _ = _instance(store)
    assert policy.admin_view() == {"policy": None, "problems": []}
    floor = _set(policy)
    view = policy.admin_view()["policy"]
    assert (view["etag"], view["set_at"], view["set_by"]) == (floor.etag, NOW, "admin:t_ab12")
    assert view["keys"] == ["profile", "terms"] and view["exceptions"] == "company" and view["in_force"] is True
    assert view["brought"] == ["pii", "id_regions", "include_private_ips", "data", "images"]  # keys stays as stored
    assert view["text"] == POLICY and view["notices"] == []
    assert policy.summary() == {
        "sources": [{"source": "server", "origin": "https://latet.acme.lan", "received": NOW, "notices": 0}],
        "problems": [],
    }


def test_once_started_the_check_catches_a_message_that_never_came_until_closed():
    store = Store()
    first, _ = _instance(store)
    changed = threading.Event()
    second, _ = _instance(store, changed=changed.set, every=0.05)
    second.start()
    assert second._thread.name == "latet-policy"
    first.set(POLICY, actor="admin:t_ab12")  # written, never announced to the second instance
    assert changed.wait(5)
    second.close()
    assert not second._thread.is_alive()


# --- what the rulings add (M4, L2, L3, L4, L6, F19) ---------------------------------------------------------------


def test_the_floor_is_the_same_object_while_the_policy_is_unchanged():
    """A space's start compares the floor it was built on with the edition's by identity (M4): a policy that did not
    change answers the very floor it answered, or every space would be built twice."""
    store = Store()
    policy, _ = _instance(store)
    _set(policy)
    floor = policy.floor()
    assert policy.floor() is floor
    assert policy.refresh() is False and policy.floor() is floor


def test_the_administrators_view_names_the_keys_in_a_rules_files_order():
    store = Store()
    policy, _ = _instance(store)
    _set(policy, "latet_rules: 1\nallow_values: [acme.com]\nterms: [{value: Project Falcon}]\nprofile: max\n")
    assert policy.admin_view()["policy"]["keys"] == ["profile", "terms", "allow_values"]


def test_an_administrator_reads_why_a_stored_policy_cannot_be_read():
    store = Store()
    policy, _ = _instance(store)
    store.kept = StoredPolicy("latet_rules: 99\nprofile: max\n", "9f0e1d2c3b4a5968", NOW, "admin:t_cd34")
    [problem] = policy.admin_view()["problems"]
    assert policy.admin_view()["policy"] is None and "newer Latet" in problem


def test_nothing_stored_and_nothing_wrong_is_said_as_nothing():
    policy, _ = _instance(Store())
    assert policy.summary() is None and policy.floor() is None


def test_a_policy_set_again_is_read_again_by_when_it_was_set_too():
    """L3: the etag and when it was set say whether the policy changed, so a row written again is read again."""
    store = Store()
    policy, _ = _instance(store)
    _set(policy)
    store.kept = StoredPolicy(POLICY, store.kept.etag, NOW + 60, "admin:t_cd34")  # the same text, set again
    assert policy.refresh() is False  # the floor is the same: no space is raised again
    assert policy.summary()["sources"][0]["received"] == NOW + 60


def test_a_stored_text_that_fails_otherwise_is_a_problem_said_and_the_instance_starts(monkeypatch):
    """L2: only the database not answering stops an instance; anything a stored text does is said, and no space is
    built below it, so the administration API that removes it is up."""
    store = Store()
    first, _ = _instance(store)
    _set(first)

    def broken(*args, **kwargs):
        raise RuntimeError("a parser of an older release failed on 'Project Falcon'")

    monkeypatch.setattr(policy_server_module, "parse_policy", broken)
    started, _ = _instance(store)
    with pytest.raises(PolicyUnavailable) as raised:
        started.floor()
    assert "RuntimeError" in str(raised.value) and "Project Falcon" not in str(raised.value)
    [problem] = started.summary()["problems"]
    assert problem["level"] == "fail" and "Project Falcon" not in problem["text"]


class Down(Store):
    """A store whose database does not answer."""

    def __init__(self):
        super().__init__()
        self.down = False

    def stamp(self):
        if self.down:
            raise ConnectionError("connection to 10.0.0.5 refused")
        return super().stamp()


def test_a_database_that_does_not_answer_stops_the_start_and_later_keeps_the_floor(caplog):
    store = Down()
    store.down = True
    with pytest.raises(ConnectionError):
        _instance(store)  # the instance does not start
    store.down = False
    policy, _ = _instance(store)
    _set(policy)
    floor = policy.floor()
    store.down = True
    with caplog.at_level(logging.WARNING, logger="latet.ee.policy_server"):
        assert policy.refresh() is False
    assert policy.floor() is floor
    assert "ConnectionError" in caplog.text and "10.0.0.5" not in caplog.text


def test_before_the_license_was_ever_read_here_a_stored_policy_builds_no_space():
    """L4: a license this instance never read says nothing of whether the policy is in force."""
    store, licensed = Store(), [True]
    first, _ = _instance(store)
    _set(first)
    licensed[0] = None  # the license's store has not answered since this instance started
    started, _ = _instance(store, licensed=licensed)
    with pytest.raises(PolicyUnavailable, match="license"):
        started.floor()
    assert started.summary()["problems"][0]["level"] == "fail"
    licensed[0] = True
    assert started.refresh() is True and started.floor().rules.profile == "max"
    nothing, _ = _instance(Store(), licensed=[None])
    assert nothing.floor() is None  # nothing stored: nothing to build below


def test_policy_unavailable_is_the_free_codes_floor_unavailable():
    """The dashboard and the administration answer it with their fixed words (503), never its own."""
    assert issubclass(PolicyUnavailable, FloorUnavailable) and "try again" in FLOOR_UNAVAILABLE


def test_an_older_look_never_replaces_a_newer_one():
    """L6: the store is read outside the lock; a look that read before another one, and ends after it, is dropped."""
    store = Store()
    policy, _ = _instance(store)
    _set(policy)
    stricter = POLICY.replace("profile: max", "profile: max\npii: [EMAIL]")

    class Slow(Store):
        def __init__(self, kept):
            super().__init__()
            self.kept, self.reading, self.go = kept, threading.Event(), threading.Event()

        def load(self):
            found = self.kept
            self.reading.set()
            assert self.go.wait(5)
            return found

    first = StoredPolicy(POLICY.replace("max", "secrets"), "a" * 64, NOW + 1, "admin:t_ab12")
    slow = Slow(first)
    policy._store = slow
    older = threading.Thread(target=policy.refresh)
    older.start()
    assert slow.reading.wait(5)  # the older look has read the row, and waits
    floor_while_reading = policy.floor()  # never waits for a look under way
    slow.reading.clear()
    second = policy.set(stricter, actor="admin:t_ab12")
    slow.go.set()
    policy.refresh()  # the newer look
    older.join(5)
    assert floor_while_reading.rules.profile == "max"
    assert policy.floor().etag == second.etag


def test_a_change_is_recorded_as_an_event_without_a_value():
    store, events = Store(), []
    policy, _ = _instance(store, record=events.append)
    _set(policy)
    [event] = events
    assert (event["type"], event["reason"]) == ("policy", "changed") and "Project Falcon" not in repr(event)


def test_what_is_stored_is_never_shown_by_its_repr():
    stored = StoredPolicy(POLICY, "e" * 64, NOW, "admin:t_ab12")
    assert "Project Falcon" not in repr(stored)


def test_the_check_reads_its_period_when_the_policy_is_made(monkeypatch):
    monkeypatch.setattr(policy_server_module, "CHECK", 0.05)
    changed = threading.Event()
    store = Store()
    first, _ = _instance(store)
    second, _ = _instance(store, changed=changed.set)
    second.start()
    try:
        first.set(POLICY, actor="admin:t_ab12")
        assert changed.wait(5)
    finally:
        second.close()


def test_a_look_that_fails_is_said_and_the_checking_goes_on(caplog):
    store = Down()
    looked = threading.Event()
    second, _ = _instance(store, changed=looked.set, every=0.05)

    def failing():
        raise RuntimeError("Project Falcon")

    refresh = second.refresh
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            failing()
        return refresh()

    second.refresh = flaky
    with caplog.at_level(logging.WARNING, logger="latet.ee.policy_server"):
        second.start()
        try:
            first, _ = _instance(store)
            first.set(POLICY, actor="admin:t_ab12")
            assert looked.wait(5)
        finally:
            second.close()
    assert "RuntimeError" in caplog.text and "Project Falcon" not in caplog.text
