"""Core data types shared by detectors, masker, vault and rehydrators."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum


class EntityClass(IntEnum):
    """Broad family of a finding. Higher value wins when spans overlap."""

    STRUCT = 5  # strict data mode: any value of a table, JSON or key=value (masked in its structure only)
    NER = 10
    LLM = 15  # found by the local reviewer (best effort, like NER: it never blocks a request)
    PII = 20
    IDENTITY = 30
    CUSTOM = 40
    SECRET = 50


class SlotKind(StrEnum):
    """Where a piece of text sits in a request; drives which detectors run."""

    SYSTEM = "system"
    USER_TEXT = "user_text"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_INPUT = "tool_input"
    TOOL_RESULT = "tool_result"
    ATTACHMENT = "attachment"
    TOOL_DESCRIPTION = "tool_description"
    OUTBOUND_TOOL = "outbound_tool"  # the input of a tool that runs outside this computer, as the model wrote it
    IMAGE_TEXT = "image_text"  # the text read in an image of the request
    OTHER = "other"

    @property
    def natural_language(self) -> bool:
        return self in (SlotKind.USER_TEXT, SlotKind.ASSISTANT_TEXT)


class Policy(StrEnum):
    MASK = "mask"  # reversible: surrogate out, original restored on the way back
    REDACT = "redact"  # one-way: a fixed marker out, never restored
    ALLOW = "allow"  # left as is (still reported)


@dataclass(frozen=True, slots=True)
class Span:
    """A detected sensitive range ``text[start:end]``."""

    start: int
    end: int
    etype: str  # e.g. "AWS_ACCESS_KEY", "EMAIL", "IT_FISCAL_CODE", "CUSTOM"
    eclass: EntityClass
    rule: str  # detector/rule id, for the dashboard
    score: float = 1.0

    def __post_init__(self) -> None:
        if not 0 <= self.start < self.end:
            raise ValueError(f"invalid span {self.start}:{self.end}")

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class Replacement:
    """One substitution made in a masked text."""

    start: int  # offsets in the ORIGINAL text
    end: int
    original: str
    replacement: str
    etype: str
    eclass: EntityClass
    rule: str
    policy: Policy
    entity_id: str | None  # vault id when reversible


@dataclass(slots=True)
class MaskedText:
    text: str
    replacements: list[Replacement] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.replacements)
