"""What a call answers, one result for each text sent. ``scan`` says where the values are, ``mask`` the text with fakes
in their place (and where they are in it), ``unmask`` the text with the real values back; ``forget`` how many values it
took out of the vault, and how many the vault did not hold. Positions are Unicode code points, which is what a Python
string indexes by."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["Finding", "ForgetResult", "MaskResult", "ScanResult", "UnmaskResult"]


@dataclass(frozen=True, slots=True)
class Finding:
    """A value found in a text, or a fake put in its place: its ``type`` (``EMAIL``, ``IBAN``...), its
    ``entity_class`` (``pii``, ``secret``... the JSON's ``class``) and where it is, ``text[start:end]``."""

    type: str
    entity_class: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Where the server's rules find values in a text: nothing is kept, no fake is made. ``counts`` is how many of each
    type."""

    findings: list[Finding]
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class MaskResult:
    """A text masked: ``text`` has the fakes of the API key's vault in place of its values, ``replacements`` says where
    (in ``text``), ``counts`` how many of each type. When the local reviewer or the server's guard stopped the text,
    ``text`` is ``None`` and ``blocked`` says why. ``degraded`` names what ran weaker on the call (a detector that did
    not read a text, a review that did not end), the same on every result of one call; empty when nothing did."""

    text: str | None
    replacements: list[Finding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    blocked: str | None = None
    degraded: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UnmaskResult:
    """A text with every fake of the API key's vault in it replaced by its value; ``restored`` is how many."""

    text: str
    restored: int


@dataclass(frozen=True, slots=True)
class ForgetResult:
    """Values taken out of the API key's vault: ``forgotten`` is how many it held (gone, with their fakes), ``unknown``
    how many it did not hold, each value counted once."""

    forgotten: int
    unknown: int


class Malformed(ValueError):
    """An answer that is not the applications API's (a proxy's page, another service). The client never shows it: it
    raises a ``LatetError`` in words of its own."""


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Malformed
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise Malformed
    return value


def _number(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise Malformed
    return value


def _counts(value: Any) -> dict[str, int]:
    return {_text(name): _number(count) for name, count in _object(value).items()}


def _places(value: Any) -> list[Finding]:
    if not isinstance(value, list):
        raise Malformed
    places = []
    for item in value:
        found = _object(item)
        place = (
            _text(found.get("type")),
            _text(found.get("class")),
            _number(found.get("start")),
            _number(found.get("end")),
        )
        places.append(Finding(*place))
    return places


def _items(body: Any, count: int) -> list[dict[str, Any]]:
    items = _object(body).get("results")
    if not isinstance(items, list) or len(items) != count:
        raise Malformed
    return [_object(item) for item in items]


def scan_results(body: Any, count: int) -> list[ScanResult]:
    """The ``count`` results of a ``scan`` answer; ``Malformed`` when it is not one."""
    return [ScanResult(_places(item.get("findings")), _counts(item.get("counts"))) for item in _items(body, count)]


def mask_results(body: Any, count: int) -> list[MaskResult]:
    """The ``count`` results of a ``mask`` answer, each with the call's ``degraded``; ``Malformed`` if it is not one."""
    items = _items(body, count)
    degraded = _object(body).get("degraded", [])
    if not isinstance(degraded, list):
        raise Malformed
    weaker = tuple(_text(name) for name in degraded)
    results = []
    for item in items:
        if "blocked" in item:
            results.append(MaskResult(None, blocked=_text(item["blocked"]), degraded=weaker))
        else:
            text, places, counts = (
                _text(item.get("text")),
                _places(item.get("replacements")),
                _counts(item.get("counts")),
            )
            results.append(MaskResult(text, places, counts, degraded=weaker))
    return results


def unmask_results(body: Any, count: int) -> list[UnmaskResult]:
    """The ``count`` results of an ``unmask`` answer; ``Malformed`` when it is not one."""
    return [UnmaskResult(_text(item.get("text")), _number(item.get("restored"))) for item in _items(body, count)]


def forget_results(body: Any, count: int) -> list[ForgetResult]:
    """The one result of a ``forget`` answer, whatever how many values it carried (``count``); ``Malformed`` when it
    is not one."""
    found = _object(body)
    forgotten, unknown = _number(found.get("forgotten")), _number(found.get("unknown"))
    if forgotten < 0 or unknown < 0:
        raise Malformed
    return [ForgetResult(forgotten, unknown)]
