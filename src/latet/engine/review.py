"""Claude Code's auto mode reviewer: which identity detail each value found in a review is, and
which of them the user lets through to it (``auto_mode_review``)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from latet.engine.types import EntityClass, Span

DETAILS = ("user_name", "paths", "host_name", "git_author")
LABELS = {
    "user_name": "your user name",
    "paths": "your user folder in paths",
    "host_name": "your host name",
    "git_author": "your git author",
}

# A user name right after a home folder (C:\Users\, /home/, /Users/, JSON-escaped too) is a path.
_HOME = re.compile(r"(?i)(?:[\\/]|\\\\)(?:users|home)(?:[\\/]|\\\\)$")


def detail(text: str, span: Span) -> str | None:
    """The identity detail ``span`` is, or None (not this machine's identity)."""
    if span.rule == "identity:domain":
        return "git_author"  # the domain of your git author's email
    if span.eclass != EntityClass.IDENTITY:
        return None
    if span.etype == "USERNAME":
        in_path = span.rule == "identity:home-path" or _HOME.search(text, max(0, span.start - 12), span.start)
        return "paths" if in_path else "user_name"
    if span.etype == "HOSTNAME":
        return "host_name"
    if span.etype in ("PERSON", "EMAIL"):
        return "git_author"
    return None


@dataclass(frozen=True, slots=True)
class ReviewPolicy:
    allowed: frozenset[str] = frozenset()  # details the user lets through to the reviewer
    surrogate_user_name: bool = False  # Claude Code is given the user name's surrogate instead

    def details(self, text: str, spans: Sequence[Span]) -> list[str | None]:
        """The detail of each span. A value inside another one belongs to the outer one: the git
        author's email is found as personal data too, and it may contain the user name. A secret is
        never a detail, wherever it is."""
        own = [detail(text, span) for span in spans]
        identity = [(s, d) for s, d in zip(spans, own, strict=True) if d is not None]
        out: list[str | None] = []
        for span in spans:
            if span.eclass == EntityClass.SECRET:
                out.append(None)
                continue
            around = [(o, d) for o, d in identity if o.start <= span.start and span.end <= o.end]
            out.append(max(around, key=lambda pair: pair[0].end - pair[0].start)[1] if around else None)
        return out

    def let_through(self, details: Sequence[str | None]) -> list[bool]:
        """Which values may reach the reviewer: those of an allowed detail."""
        return [d in self.allowed for d in details]


def block_reason(policy: ReviewPolicy, blocking: Sequence[tuple[str | None, str]]) -> str:
    """Why a review was stopped. ``blocking`` holds (detail, preview) of each value that blocked it."""
    details = sorted({d for d, _ in blocking if d}, key=DETAILS.index)
    others = sorted({preview for d, preview in blocking if not d})
    what = [*others, *(LABELS[d] for d in details)]
    text = f"auto mode's reviewer would receive {_join(what)}, so auto mode cannot approve this action"
    if "user_name" in details and policy.surrogate_user_name:
        details.remove("user_name")
        text += (
            ". Claude Code still has your real user name, not its surrogate: run `latet agents apply claude`, "
            "then restart Claude Code"
        )
    if details:
        keys = ", ".join(f"{d}: allow" for d in details)
        # Next to a secret, name the details: only they can be let through.
        what = _join([LABELS[d] for d in details]) if others else "it" if len(details) == 1 else "them"
        text += f". To let {what} through to the reviewer, set auto_mode_review: {{{keys}}}"
        text += " (or use the dashboard's Rules)"
    return text


def _join(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
