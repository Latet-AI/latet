"""A company's policy may force `unknown_members: refuse` (Latet Enterprise, locked rules): the settings in use are
raised to it, a person's own `check` is below it, and it stays in force once the license ends (what protects never
depends on a license's days: ``Licensing.licensed``), a trial's end included."""

from latet.config import Config, ProjectEntry
from latet.ee.license import Licensing
from latet.ee.policy import below_floor, parse_policy, policy_json, raise_to_floor
from latet.ee.policy_server import ServerPolicy, StoredPolicy
from latet.projects import effective_config
from latet.protection import effective_unknown_members

from .licenses import TRIAL, Clock, MemoryLicenses, at

LOWERED = "new request members: passed after a check instead of refused"
POLICY = "latet_rules: 1\nunknown_members: refuse\n"


def test_a_policy_that_says_refuse_raises_check_and_finds_check_below_it():
    floor = parse_policy(POLICY)
    assert floor.keys == {"unknown_members"} and floor.notices == ()
    assert policy_json(floor)["unknown_members"] == "refuse"
    raised = raise_to_floor(floor, Config())
    assert raised.unknown_members == "refuse" and effective_unknown_members(raised, None) == "refuse"
    assert below_floor(floor, Config()) == [LOWERED]
    assert below_floor(floor, Config(unknown_members="refuse")) == []
    assert below_floor(floor, raised) == []


def test_every_project_is_raised_with_it():
    floor = parse_policy(POLICY)
    raised = raise_to_floor(floor, Config(projects=[ProjectEntry(path="C:/work/shop", profile="max")]))
    assert effective_config(raised, None, raised.projects[0])[0].unknown_members == "refuse"


def test_a_policy_that_says_check_locks_nothing_and_is_said():
    floor = parse_policy("latet_rules: 1\nunknown_members: check\n")
    assert floor.keys == frozenset() and floor.unread == {"unknown_members"}
    assert [notice.split(":")[0] for notice in floor.notices] == ["unknown_members"]
    assert raise_to_floor(floor, Config()).unknown_members == "check"


class _Store:
    """ee_policy in memory: one row."""

    def __init__(self):
        self.kept = None

    def stamp(self, *, conn=None):
        return None if self.kept is None else (self.kept.etag, self.kept.set_at)

    def load(self):
        return self.kept

    def save(self, policy, *, conn=None):
        self.kept = policy

    def delete(self, *, conn=None):
        had, self.kept = self.kept is not None, None
        return had


def _policed(licensing, clock):
    """A server's policy forcing `refuse`, its floor weighed with ``licensing`` at each look."""
    store = _Store()
    floor = parse_policy(POLICY)
    store.kept = StoredPolicy(POLICY, floor.etag, clock(), "admin:t_ab12")
    return ServerPolicy(
        store, publish=lambda channel, message: True, origin="https://latet.acme.lan",
        licensed=lambda: licensing.licensed("locked-rules"), clock=clock,
    )  # fmt: skip


def test_the_forced_refuse_stays_once_a_trial_ends(signer):
    clock = Clock(at("2026-11-01"))
    licensing = Licensing(MemoryLicenses(), clock=clock, count_users=lambda: None, keys=signer.keys)
    licensing.install(signer.make(**TRIAL))
    policy = _policed(licensing, clock)
    assert effective_unknown_members(raise_to_floor(policy.floor(), Config()), None) == "refuse"
    clock.now = at("2026-12-03")  # the trial's last day was 2026-12-02: no grace
    assert not licensing.manageable() and licensing.licensed("locked-rules")
    policy.refresh()
    floor = policy.floor()
    assert floor is not None and effective_unknown_members(raise_to_floor(floor, Config()), None) == "refuse"


def test_the_forced_refuse_stays_once_a_paid_license_ends_and_its_grace_with_it(signer):
    clock = Clock(at("2027-03-01"))
    licensing = Licensing(MemoryLicenses(), clock=clock, count_users=lambda: None, keys=signer.keys)
    licensing.install(signer.make())  # its last day 2027-10-31, then 30 days of grace, then the pause
    policy = _policed(licensing, clock)
    clock.now = at("2028-01-15")
    policy.refresh()
    floor = policy.floor()
    assert floor is not None and below_floor(floor, Config()) == [LOWERED]
    assert effective_unknown_members(raise_to_floor(floor, Config()), None) == "refuse"
