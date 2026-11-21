"""What central approvals add to an instance's metrics (Latet Enterprise): how many changes and texts wait for someone
else, read from the record, and how many ended through this instance, by kind and how; never a value or a name."""

import time

from latet.ee.approvals import WAITING_EVERY, ApprovalMetrics, CentralApprovals, MemoryRecords, Row
from latet.ee.settings import ApprovalsSettings
from latet.server.metrics import Metrics
from latet.state import IdentityRef

from .stand_ins import FakeDirectory, Spaces

MARK = IdentityRef("u_markellisa", "Mark Ellis", "person")
EMILY = IdentityRef("u_emilycarte", "Emily Carter", "person")
REASONS = {"source": "command", "agent": None, "loosened": ["the profile: lowered"], "keys": ["profile"]}


class Ticks:
    """The registry's clock: moved past WAITING_EVERY, the record is read again at the next render."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _sample(text: str, name: str, **labels: str) -> float | None:
    for line in text.splitlines():
        if line.startswith(name + "{") and all(f'{key}="{value}"' in line for key, value in labels.items()):
            return float(line.rsplit(" ", 1)[1])
    return None


def _until(registry: Metrics, ticks: Ticks, name: str, value: float, **labels: str) -> str:
    """A render once the gauge read the record (in the metrics' own threads, never waited for by a scrape)."""
    for _ in range(300):
        text = registry.render()
        if _sample(text, name, **labels) == value:
            return text
        ticks.now += WAITING_EVERY + 1
        time.sleep(0.01)
    raise AssertionError(f"{name} {labels} never read {value}")


def test_what_waits_and_what_ended_are_counted_by_kind_route_and_state():
    records, ticks = MemoryRecords(), Ticks()
    registry = Metrics(clock=ticks)
    central = CentralApprovals(
        records, FakeDirectory(), settings=ApprovalsSettings, licensed=lambda: True, space=Spaces(),
        waiting_batches=lambda items: {item_id for _identity_id, item_id in items}, ended_changes=lambda items: {},
    )  # fmt: skip
    metrics = ApprovalMetrics.of(registry)
    assert ApprovalMetrics.of(registry) is metrics  # once a registry: an edition made again binds the same
    metrics.bind(central)
    try:
        records.add(Row("c1", "u_emilycarte", "Emily Carter", "person", "change", "security", REASONS, 1.0))
        records.add(Row("k1", "u_billingcia", "Billing CI", "key", "change", "controllers", REASONS, 2.0))
        _until(registry, ticks, "latet_approvals_company_waiting", 1.0, kind="change", route="controllers")
        central.ended(EMILY, "c1", "change", "approved", MARK)
        central.ended(EMILY, "c1", "change", "approved", MARK)  # ended already: counted once
        text = _until(registry, ticks, "latet_approvals_company_waiting", 0.0, kind="change", route="security")
        assert _sample(text, "latet_approvals_company_decided_total", kind="change", state="approved") == 1
        ours = "\n".join(line for line in text.splitlines() if "latet_approvals_company" in line)  # E5's own (R56)
        assert ours and "Emily Carter" not in ours and "the profile: lowered" not in ours and "u_" not in ours
    finally:
        metrics.unbind(central)
        registry.close()


def _central(records: MemoryRecords) -> CentralApprovals:
    return CentralApprovals(
        records, FakeDirectory(), settings=ApprovalsSettings, licensed=lambda: True, space=Spaces(),
        waiting_batches=lambda items: {item_id for _identity_id, item_id in items}, ended_changes=lambda items: {},
    )  # fmt: skip


def test_an_edition_closed_leaves_the_one_made_after_it_bound_and_its_own_counts_nothing():
    """As E4's ``ServerMetrics`` (I10): an edition made again on a server binds before the one it replaces closes; the
    closing one's unbinding leaves the newer bound, and the closed one's ends count nothing; once none is bound, the
    gauge is left out of the scrape."""
    old_records, new_records, ticks = MemoryRecords(), MemoryRecords(), Ticks()
    registry = Metrics(clock=ticks)
    old, new = _central(old_records), _central(new_records)
    metrics = ApprovalMetrics.of(registry)
    try:
        metrics.bind(old)
        metrics.bind(new)
        metrics.unbind(old)
        new_records.add(Row("b1", "u_emilycarte", "Emily Carter", "person", "batch", "security", {}, 1.0))
        old_records.add(Row("c1", "u_emilycarte", "Emily Carter", "person", "change", "security", REASONS, 1.0))
        _until(registry, ticks, "latet_approvals_company_waiting", 1.0, kind="batch", route="security")
        old.ended(EMILY, "c1", "change", "approved", MARK)
        decided = _sample(registry.render(), "latet_approvals_company_decided_total", kind="change", state="approved")
        assert decided is None
        metrics.unbind(new)
        for _ in range(300):
            if "latet_approvals_company_waiting{" not in registry.render():
                break
            ticks.now += WAITING_EVERY + 1
            time.sleep(0.01)
        else:
            raise AssertionError("the gauge of an unbound record was still read")
    finally:
        metrics.unbind(new)
        registry.close()
