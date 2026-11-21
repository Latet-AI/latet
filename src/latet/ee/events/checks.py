"""The lines E4 adds to the server's status (E1's `checks()`, which `latet server status` shows): the event log, and
each SIEM output: when it last delivered, since when it fails and why, what waits, what was dropped, a secret not
set. Queue state and removed-output counts are read here; the caller gives settings and the license's standing,
and calls this function in the administration status worker."""

from __future__ import annotations

import time
from collections.abc import Callable

from latet.doctor.checks import FAIL, OK, WARN, Check
from latet.ee.events.api import REPORTS, SIEM, pause_of
from latet.ee.events.store import ADMIN_KEPT, QueueState, SiemQueue
from latet.ee.license import Standing
from latet.ee.settings import EnterpriseSettings, SiemOutput


def _when(seconds: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds)) if seconds else "never"


def said(state: QueueState) -> tuple[str, str]:
    """A queue in words: its status, and what to say of it (the receiver's failure in Task 5's fixed words). FAIL for a
    queue held at an entry of the log (I5), and for one whose oldest entries of the log the bound collapsed into a gap
    marker (round 3: until it delivers again; `siem.gap` in the log says it for good); WARN from a tenth of the
    bound."""
    counts = f"{state.queued:,} waiting" + (f", {state.dropped:,} dropped" if state.dropped else "")
    if state.blocked or state.held_to is not None:
        what = "an entry of the administration log" if state.blocked else "a run of events"
        held = f"held since {_when(state.failing_since)} at {what} it refuses"
        return FAIL, f"{held} ({state.last_error}); {counts}"
    if state.collapsed:  # the bound: these never reach it, their gap marker in their place
        entries = "entry" if state.collapsed == 1 else "entries"
        lost = f"{state.collapsed:,} {entries} of the administration log collapsed into a gap marker"
        return FAIL, f"{lost} past {ADMIN_KEPT:,} waiting: they never reach it; {counts}"
    if state.failing_since is not None:
        return WARN, f"failing since {_when(state.failing_since)} ({state.last_error}); {counts}"
    if state.admin >= ADMIN_KEPT // 10:  # the log's entries pile up towards the bound
        return WARN, f"{state.admin:,} entries of the administration log waiting, {ADMIN_KEPT:,} at most; {counts}"
    if state.last_ok is None:
        return OK, f"nothing sent yet; {counts}"
    return (WARN if state.dropped else OK), f"last sent {_when(state.last_ok)}; {counts}"


def check_events(
    *,
    queue: SiemQueue,
    settings: EnterpriseSettings,
    standing: Standing,
    secret_state: Callable[[SiemOutput], tuple[bool, bool]],
) -> list[Check]:
    """The event log's line and one for each SIEM output; none when the license lists neither reports nor the SIEM.
    ``secret_state``: whether an output's secret is kept for where it sends, and whether one set for another address
    is (I1)."""
    lic = standing.license
    if lic is None or not ({REPORTS, SIEM} & set(lic.features)):
        return []
    pause = pause_of(standing, REPORTS) if REPORTS in lic.features else None
    detail = f"in PostgreSQL, people {settings.reports.people}, kept {settings.reports.retention_days} days"
    lost = queue.admin_dropped()  # I5: a gap Latet left in the SIEM's copy of the log, said apart
    if lost:
        entries = "entry" if lost == 1 else "entries"
        detail += f"; {lost:,} {entries} of the administration log never reached an output removed"
    checks = [Check(WARN if pause or lost else OK, "Event log", detail + (f"; {pause['error']}" if pause else ""))]
    siem_pause = pause_of(standing, SIEM)
    for output in settings.siem.outputs:
        title = f"SIEM {output.name}"
        kept, elsewhere = secret_state(output) if output.secret else (True, False)
        if siem_pause is not None:
            checks.append(Check(WARN, title, siem_pause["error"]))
        elif elsewhere:
            words = f"its secret was set for another address: set it again: `latet server siem secret {output.name}`"
            checks.append(Check(FAIL, title, words))
        elif not kept:
            checks.append(Check(FAIL, title, f"its secret is not set: `latet server siem secret {output.name}`"))
        else:
            state = queue.state(output.name)
            status, words = said(state)
            if state.blocked or state.held_to is not None:
                words += f"; `latet server siem skip {output.name}` sends it no more"
            checks.append(Check(status, title, f"{output.kind}, {words}"))
    return checks
