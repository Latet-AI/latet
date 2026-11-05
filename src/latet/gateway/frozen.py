"""Worker-frozen text with identity-only checks for mutable JSON slot subtrees.

Only the preparation arena owns these mutable-container checks. Decisions and record snapshots receive their
immutable strings, never the checks or the containers. No check serializes or compares payload text by content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from latet.protocols.base import JsonRef, SlotRef


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class FrozenText:
    slot: SlotRef
    text: str
    root: object = None
    nodes: tuple[tuple[Any, tuple[Any, ...]], ...] = field(default=())

    def unchanged(self, slot: SlotRef) -> bool:
        if slot is not self.slot:
            return False
        if not isinstance(slot, JsonRef):
            return slot.get() is self.text
        if slot.container[slot.key] is not self.root:
            return False
        for holder, children in self.nodes:
            if len(holder) != len(children):
                return False
            if isinstance(holder, dict):
                if any(
                    key is not was_key or value is not was_value
                    for (key, value), (was_key, was_value) in zip(holder.items(), children, strict=True)
                ):
                    return False
            elif any(value is not was for value, was in zip(holder, children, strict=True)):
                return False
        return True


def freeze_text(slot: SlotRef, previous: FrozenText | None = None) -> FrozenText:
    """Prepare a real JsonRef's compact string once, reusing it while every selected identity is unchanged."""
    if previous is not None and previous.unchanged(slot):
        return previous
    text = slot.get()
    text = text if isinstance(text, str) else ""
    if not isinstance(slot, JsonRef):
        return FrozenText(slot, text)
    root = slot.container[slot.key]
    nodes: list[tuple[Any, tuple[Any, ...]]] = []
    seen: set[int] = set()

    def remember(value: Any) -> None:
        if not isinstance(value, dict | list) or id(value) in seen:
            return
        seen.add(id(value))
        children = tuple(value.items()) if isinstance(value, dict) else tuple(value)
        nodes.append((value, children))
        for child in value.values() if isinstance(value, dict) else value:
            remember(child)

    remember(root)
    return FrozenText(slot, text, root, tuple(nodes))
