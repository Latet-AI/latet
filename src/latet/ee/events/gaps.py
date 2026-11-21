"""Which SIEM gap markers are Latet's (R3-1, R4-1). Each marker Latet makes (the bound's collapse, a pause or no output,
a skip) is written in one transaction with a chained entry of the administration log naming its range, `siem.gap` or
`siem.skipped` `{output, from, to, count}`; without the master key no one writes such an entry whose keyed hash holds
(R4-4). Every event whose `outcome` is `siem.gap` needs an account, whatever its form: it is the log's own entry when
its `reason` is `server`, the one actor that records a gap; otherwise it is a marker, Latet's only when its range reads
as Latet writes one (`reason` `audit:<first>-<last>`, `stopped` an int) and such an entry names it. A marker whose range
ends before the first entry the log still keeps cannot be checked (R4-2): said apart, never a failure.

``check`` is what `latet server log verify` asks on a server (Task 11's `EventService.gaps`): the markers waiting in
every queue first, then the log's entries, then where the log starts."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from typing import Any, Final, Protocol

from latet.ee.events.admin import PAGE, AuditReader
from latet.ee.events.store import GAP

RECORDS: Final = ("siem.gap", "siem.skipped")
# The `reason` of the log's own `siem.gap` entry as it reaches the SIEM: `gap_noted`'s actor.
ITS_OWN: Final = "server"
_RANGE: Final = re.compile(r"audit:([0-9]+)-([0-9]+)")  # ASCII digits alone: the form Latet writes (R4-1)
_SAID: Final = 80  # characters of a marker's reason, id or output said at most
_UNSAID: Final = re.compile(r"[^ -~]+")  # what is not printable ASCII: never said (a terminal acts on some of it)


class Markers(Protocol):
    def markers(self) -> list[tuple[str, dict[str, Any]]]: ...


def recorded(audit: AuditReader) -> set[tuple[int, int, int]]:
    """The ranges named by the log's `siem.gap` and `siem.skipped` entries whose keyed hash holds (R4-4: a row written
    by hand names nothing), read by id a page at a time: (first id, last id, how many), each an int as Latet writes it
    (a skip that made no marker names none: its `from` is null)."""
    found: set[tuple[int, int, int]] = set()
    for action in RECORDS:
        last = 0
        while page := list(audit.after(last, PAGE, action=action)):
            for entry in page:
                details = entry.details if isinstance(entry.details, Mapping) else {}
                first, to, count = (details.get(name) for name in ("from", "to", "count"))
                if type(first) is int and type(to) is int and type(count) is int and audit.holds(entry):
                    found.add((first, to, count))
            last = page[-1].id
    return found


def marker_range(event: Mapping[str, Any]) -> tuple[int, int, int] | None:
    """A marker's range as Latet writes one: (first id, last id, how many) when its `reason` is `audit:<first>-<last>`
    and its `stopped` an int; None for any other form (R4-1)."""
    reason, count = event.get("reason"), event.get("stopped")
    found = _RANGE.fullmatch(reason) if isinstance(reason, str) else None
    if found is None or type(count) is not int:
        return None
    return int(found[1]), int(found[2]), count


def _said(value: object) -> str:
    """What a marker says, as the check says it: printable ASCII alone, ``_SAID`` characters at most (a row written by
    hand may hold anything)."""
    return _UNSAID.sub("", str(value))[:_SAID]


def accounted(
    events: Iterable[tuple[str | None, Mapping[str, Any]]], ranges: Collection[tuple[int, int, int]], first_kept: int
) -> dict[str, list[dict[str, Any]]]:
    """Every event of ``events`` whose `outcome` is `siem.gap`, accounted for (R4-1): ``unrecorded``, the markers that
    are not Latet's (a range that cannot be read, or one no entry names); ``unchecked``, those no kept entry names
    whose range ends before ``first_kept``, the first id the log keeps (R4-2): a marker whose own entry, written after
    its range, is still kept is checked against it. The log's own entries pass. Each said with where it was (an
    output's queue, None for a SIEM's copy), its reason and id (printable ASCII, 80 characters at most), how many
    entries it says, and whether its range reads."""
    out: dict[str, list[dict[str, Any]]] = {"unrecorded": [], "unchecked": []}
    for where, event in events:
        if event.get("outcome") != GAP or event.get("reason") == ITS_OWN:
            continue
        span = marker_range(event)
        said = {
            "output": None if where is None else _said(where), "reason": _said(event.get("reason")),
            "id": _said(event.get("id")), "count": span[2] if span is not None else None, "readable": span is not None,
        }  # fmt: skip
        if span is not None and span in ranges:
            continue  # Latet's: a kept entry names it, though its range ends before the log's first entry
        if span is not None and span[1] < first_kept:
            out["unchecked"].append(said)
        else:
            out["unrecorded"].append(said)
    return out


def check(audit: AuditReader, queue: Markers) -> dict[str, Any]:
    """`latet server log verify`'s account of the gap markers waiting in this server's queues (R3-1, R4-1, R4-2): the
    ranges the log's entries name, the first id the log keeps, and the markers that are not Latet's or too old to check.

    The markers are read first, then the log, then where it starts. A marker and its entry of the log commit together,
    so read in this order every marker read has its entry in the log read after; read the other way, a collapse that
    lands between the two reads shows its marker with no entry. And a prune between them leaves a marker whose entry it
    took ending before the first id read last: too old to check, never not Latet's. Whatever the queues' read raises (a
    row that is not JSON, a statement cancelled, a recursion too deep) is raised: a check that could not run is never
    a pass (the route says it by its type)."""
    markers = queue.markers()
    ranges = recorded(audit)
    first = audit.first_kept()
    return {"recorded": sorted(ranges), "first_kept": first} | accounted(markers, ranges, first)
