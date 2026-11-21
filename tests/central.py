"""Stand-ins for a company that decides elsewhere (Latet Enterprise), as the free code sees one: an edition whose hooks
say where changes and texts go, and a record of what opened, never waited and how it ended."""

from __future__ import annotations

from typing import Any

from latet.edition import Community


class Queue:
    """The company's record (``Central``): who decides, what opened, how it ended."""

    def __init__(self, deciders: list[str] | None = None, *, broken: bool = False) -> None:
        self.names = ["Mark Ellis"] if deciders is None else list(deciders)
        self.broken = broken  # the record cannot be written: its database does not answer
        self.opens: list[Any] = []
        self.ends: list[tuple[str, str, str, Any]] = []  # (item id, kind, state, by)
        self.stoppers: dict[str, str] = {}  # item id → who stopped it in an API key's space, when named
        self.drops: list[str] = []
        self.withholds: list[str] = []  # texts a decider approved, withheld where they were sent
        self.askers: list[str | None] = []  # whom each item's names were asked without (``asked_by``)

    def deciders(self, identity: Any, decides: str, asked_by: str | None = None) -> list[str]:
        self.askers.append(asked_by)
        return list(self.names)

    def opened(self, identity: Any, item: Any) -> None:
        if self.broken:
            raise ConnectionError("the company's record does not answer")
        self.opens.append(item)

    def ended(self, identity: Any, item_id: str, kind: str, state: str, by: Any, stopped_by: str | None = None) -> None:
        self.ends.append((item_id, kind, state, by))
        if stopped_by is not None:
            self.stoppers[item_id] = stopped_by

    def dropped(self, identity: Any, item_id: str) -> None:
        self.drops.append(item_id)

    def withheld(self, identity: Any, item_id: str) -> None:
        self.withholds.append(item_id)


class Company(Community):
    """The edition of a company that decides elsewhere on ``changes`` ("self", "security" or "controllers") and on
    ``texts`` ("self" or "security"); every other hook as the Community Edition's."""

    def __init__(self, queue: Queue | None = None, changes: str = "security", texts: str = "self") -> None:
        super().__init__()
        self.queue: Any = queue if queue is not None else Queue()
        self.changes, self.texts = changes, texts
        self.events: list[dict[str, Any]] = []

    def changes_go_to(self, identity: Any) -> Any:
        return self.changes

    def live_review_goes_to(self, identity: Any) -> Any:
        return self.texts

    def central(self) -> Any:
        return self.queue

    def record(self, event: Any) -> None:
        self.events.append(dict(event))
