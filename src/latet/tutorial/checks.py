"""What must hold after each step of a chapter: its `checks`, run by the tests (the tutorial never runs them).

Each check reads what the code that ran the step gathered (a `Context`): what the model received, captured and said
in each turn, the agent's files, answers and error, the recorded exchanges, the live rules, and what the step shows.
A value is looked for as it is and as JSON writes it (escaped once or twice: a file's text inside a tool call inside a
request). `evaluate` says, in plain words, each thing that did not hold.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from latet.config import Config, load_config
from latet.engine.detectors.base import DetectContext
from latet.engine.masker import Masker
from latet.engine.rehydrate import restore_text, restores_for
from latet.engine.types import SlotKind
from latet.engine.vault import Vault
from latet.gateway.recorder import original_range, reconstruct
from latet.tutorial.chapters import (
    AgentErrorCheck,
    AgentGotCheck,
    Check,
    ExchangeCheck,
    FileCheck,
    ModelReceivedCheck,
    NotReceived,
    ReceivedCheck,
    RulesCheck,
    SettingCheck,
    Show,
    ShownCheck,
    render,
)


@dataclass
class Context:
    """What one step's checks look at."""

    captured: Mapping[str, str] = field(default_factory=dict)  # what the model's patterns found
    turns: Mapping[str, str] = field(default_factory=dict)  # what the model said in each turn, as it sent it
    received: Sequence[str] = ()  # the bodies the model received in this step
    received_run: Sequence[str] = ()  # ... since the chapter started
    workspace: Path | None = None
    got: Sequence[str] = ()  # what the agent got back (restored)
    error: str | None = None  # the error a stopped request gave the agent
    exchanges: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)  # turn (or `last`) → stored detail
    show: Show | None = None  # what the step shows (`previous` resolved)
    vault: Vault | None = None  # restores what the step shows, as the dashboard does
    masker: Masker | None = None  # the rules in use
    config: Config | None = None  # the settings in use
    config_file: Path | None = None  # the sandbox's config.yaml


def evaluate(checks: Sequence[Check], ctx: Context) -> list[str]:
    """What did not hold, one line each; nothing when every check holds."""
    failures: list[str] = []
    for check in checks:
        try:
            failures += _KINDS[check.kind](check.spec, ctx)
        except NotReceived as missing:
            failures.append(f"{check.kind}: the model never received {missing.name}")
    return failures


def forms(value: str) -> set[str]:
    """``value`` as it may appear: as it is, and as JSON writes it, escaped once or twice."""
    written = {value}
    for _ in range(2):
        written |= {json.dumps(form, ensure_ascii=escaped)[1:-1] for form in written for escaped in (True, False)}
    return written


def _in(texts: Sequence[str], value: str) -> bool:
    written = forms(value)
    return any(form in text for text in texts for form in written)


def _model_received(spec: ModelReceivedCheck, ctx: Context) -> list[str]:
    texts = ctx.received if spec.scope == "step" else ctx.received_run
    where = "in this step" if spec.scope == "step" else "in this chapter"
    failures = [f"the model never received {v!r} {where}" for v in _filled(spec.contains, ctx) if not _in(texts, v)]
    failures += [f"the model received {v!r} {where}" for v in _filled(spec.not_contains, ctx) if _in(texts, v)]
    if spec.requests is not None and len(texts) != spec.requests:
        failures.append(f"the model received {len(texts)} requests {where}, not {spec.requests}")
    return failures


def _received(spec: ReceivedCheck, ctx: Context) -> list[str]:
    got = ctx.captured.get(spec.name)
    if got is None:
        return [f"the model never received {spec.name}"]
    failures = []
    if spec.equals is not None and got != (equals := render(spec.equals, ctx.captured)):
        failures.append(f"the model received {got!r} as {spec.name}, not {equals!r}")
    if spec.differs is not None and got == render(spec.differs, ctx.captured):
        failures.append(f"the model received {got!r} as {spec.name}")
    if spec.matches is not None and re.fullmatch(spec.matches, got) is None:
        failures.append(f"{spec.name} is {got!r}, which does not match {spec.matches}")
    return failures


def _file(spec: FileCheck, ctx: Context) -> list[str]:
    if ctx.workspace is None:
        return ["no workspace to look in"]
    path = ctx.workspace / spec.path
    if not path.is_file():
        if spec.exists is False:
            return []
        return [f"there is no {spec.path}"]
    if spec.exists is False:
        return [f"{spec.path} exists"]
    text = [path.read_bytes().decode("utf-8", "replace")]
    failures = [f"{spec.path} does not hold {v!r}" for v in _filled(spec.contains, ctx) if not _in(text, v)]
    failures += [f"{spec.path} holds {v!r}" for v in _filled(spec.not_contains, ctx) if _in(text, v)]
    return failures


def _exchange(spec: ExchangeCheck, ctx: Context) -> list[str]:
    detail = ctx.exchanges.get(spec.ref)
    if detail is None:
        return [f"no exchange {spec.ref}"]
    summary = detail["summary"]
    name = f"exchange {spec.ref}"
    failures = []
    if spec.mode is not None and summary.get("mode") != spec.mode:
        failures.append(f"{name}: mode {summary.get('mode')}, not {spec.mode}")
    stopped = "blocked" in summary.get("flags", [])
    if spec.blocked is not None and stopped != spec.blocked:
        failures.append(f"{name}: {'stopped' if stopped else 'not stopped'}")
    reason = summary.get("block_reason") or ""
    if spec.reason_contains is not None and spec.reason_contains not in reason:
        failures.append(f"{name}: the reason {reason!r} does not say {spec.reason_contains!r}")
    classes, types = summary.get("by_class") or {}, summary.get("by_type") or {}
    failures += [f"{name}: nothing of class {c} masked" for c in spec.masked_classes if not classes.get(c)]
    failures += [f"{name}: nothing masked as {t}" for t in spec.masked_types if not types.get(t)]
    slots = (detail.get("request") or {}).get("slots", [])
    rules = {replacement.get("rule") for slot in slots for replacement in slot["replacements"]}
    failures += [f"{name}: nothing masked by {r}" for r in spec.masked_rules if r not in rules]
    failures += [f"{name}: no flag {f}" for f in spec.flags if f not in summary.get("flags", [])]
    project = summary.get("project")
    if spec.no_project is not None and (project is None) != spec.no_project:
        failures.append(f"{name}: in project {project}" if project is not None else f"{name}: in no project")
    return failures


def _agent_got(spec: AgentGotCheck, ctx: Context) -> list[str]:
    failures = _turn_got(spec.turn, ctx) if spec.turn is not None else []
    failures += [f"the agent never got {v!r}" for v in _filled(spec.contains, ctx) if not _in(ctx.got, v)]
    failures += [f"the agent got {v!r}" for v in _filled(spec.not_contains, ctx) if _in(ctx.got, v)]
    return failures


def _turn_got(turn: str, ctx: Context) -> list[str]:
    """The agent got the model's turn ``turn`` as the model sent it, each fake in it restored to its original by the
    vault (as the agent shows it): in whatever language the chapter speaks."""
    said = ctx.turns.get(turn)
    if said is None:
        return [f"the model never sent turn `{turn}`"]
    if ctx.vault is None:
        return [f"no vault to restore turn `{turn}`"]
    restored, _ = restore_text(said, restores_for(ctx.vault.entities()))
    return [] if _in(ctx.got, restored) else [f"the agent never got turn `{turn}` restored"]


def _agent_error(spec: AgentErrorCheck, ctx: Context) -> list[str]:
    if ctx.error is None:
        return ["the agent got no error"]
    wanted = render(spec.contains, ctx.captured)
    return [] if wanted in ctx.error else [f"the agent's error {ctx.error!r} does not say {wanted!r}"]


def _rules(spec: RulesCheck, ctx: Context) -> list[str]:
    masker = ctx.masker
    if masker is None:
        return ["no rules to look at"]
    failures = []
    if spec.term is not None and not detected(masker, spec.term, "terms:"):
        failures.append(f"no term {spec.term} in the rules")
    if spec.allowed is not None and not masker.allow.allows(spec.allowed):
        failures.append(f"{spec.allowed!r} is not marked Not sensitive")
    if spec.masks is not None and (masker.allow.allows(spec.masks) or not detected(masker, spec.masks, "")):
        failures.append(f"the rules do not mask {spec.masks!r}")
    return failures


def _setting(spec: SettingCheck, ctx: Context) -> list[str]:
    if ctx.config is None or ctx.config_file is None:
        return ["no settings to look at"]
    sides: list[tuple[str, Any, Config]] = []
    if spec.running is not None:
        sides.append(("in use", spec.running, ctx.config))
    if spec.saved is not None:
        sides.append(("in config.yaml", spec.saved, load_config(ctx.config_file)))
    failures = []
    for where, wanted, config in sides:
        try:
            value = _at(config.model_dump(mode="json"), spec.key)
        except LookupError:
            return [f"no setting {spec.key}"]
        if value != wanted:
            failures.append(f"{spec.key} is {value!r} {where}, not {wanted!r}")
    return failures


def _at(data: Any, key: str) -> Any:
    """The value of a dotted ``key`` in the settings as JSON writes them; LookupError when there is none."""
    for part in key.split("."):
        if not isinstance(data, dict) or part not in data:
            raise LookupError(key)
        data = data[part]
    return data


def detected(masker: Masker, value: str, rule: str = "") -> bool:
    """A detector of ``masker`` (whose rule starts with ``rule``) finds ``value``, whole, typed as a prompt."""
    spans = masker.detect(value, DetectContext(SlotKind.USER_TEXT))
    return any(s.start == 0 and s.end == len(value) and s.rule.startswith(rule) for s in spans)


def _shown(spec: ShownCheck, ctx: Context) -> list[str]:
    show = ctx.show
    if show is None or show.exchange is None:
        return ["the step shows no exchange"]
    detail = ctx.exchanges.get(show.exchange)
    if detail is None:
        return [f"no exchange {show.exchange} to show"]
    if ctx.vault is None:
        return ["no vault to restore what the step shows"]
    part = show.part or "request"
    texts = _restored(detail, part, ctx.vault)
    failures = []
    for value in _filled(spec.marked, ctx):
        marked, clear = _occurrences(texts, value)
        if clear:
            failures.append(f"{value!r} is in clear in the {part} shown")
        elif not marked:
            failures.append(f"{value!r} is not in the {part} shown")
    for value in _filled(spec.clear, ctx):
        marked, clear = _occurrences(texts, value)
        if marked:
            failures.append(f"{value!r} is marked in the {part} shown")
        elif not clear:
            failures.append(f"{value!r} is not in the {part} shown")
    return failures


Spans = list[tuple[int, int]]


def _restored(detail: Mapping[str, Any], part: str, vault: Vault) -> list[tuple[str, Spans]]:
    """The texts of one side of an exchange as the dashboard shows them, originals restored from the vault, with
    where each value is marked: each masked value, and in a stopped request each value that stopped it."""
    if part == "request":
        stopped = _stopped(detail)
        texts = []
        for slot in detail["request"]["slots"]:
            original, spans = reconstruct(slot["masked"], slot["replacements"], vault)
            # A value that stopped the request is in clear, as the check read it: its place moves with the values
            # masked before it (`original_range`, as the dashboard's API gives it).
            boxes = [
                original_range(spans, leak["masked_start"], leak["masked_end"])
                for leak in stopped
                if leak["block"] == slot.get("block")
            ]
            texts.append((original, [(span["start"], span["end"]) for span in spans] + _joined(boxes)))
        return texts
    response = detail.get("response") or {}
    pairs = [(response.get("text", ""), response.get("text_spans", []))]
    pairs += [(call.get("args", ""), call.get("spans", [])) for call in response.get("tool_calls", [])]
    texts = []
    for masked, replacements in pairs:
        original, spans = reconstruct(masked, replacements, vault)
        texts.append((original, [(span["start"], span["end"]) for span in spans]))
    return texts


def _stopped(detail: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The values that stopped the request, the ones the dashboard boxes: in a block sent for the first time, not
    let through (to auto mode's reviewer), and placed in a block the record keeps."""
    if "blocked" not in (detail.get("summary") or {}).get("flags", []):
        return []
    leaks = detail.get("leaks") or []
    placed = [leak for leak in leaks if "block" in leak and "masked_start" in leak]
    return [leak for leak in placed if leak.get("new") and not leak.get("allowed") and not leak.get("detail")]


def _joined(boxes: Spans) -> Spans:
    """Values that overlap, one box over them all, as the dashboard draws them (values that only touch stay apart)."""
    joined: Spans = []
    for start, end in sorted(boxes):
        if joined and start < joined[-1][1]:
            joined[-1] = (joined[-1][0], max(end, joined[-1][1]))
        else:
            joined.append((start, end))
    return joined


def _occurrences(texts: list[tuple[str, Spans]], value: str) -> tuple[int, int]:
    """How often ``value`` appears inside a mark (a masked value, or one that stopped the request), and outside
    every mark (clear)."""
    marked = clear = 0
    for text, spans in texts:
        for form in forms(value):
            at = text.find(form)
            while at >= 0:
                if any(start <= at and at + len(form) <= end for start, end in spans):
                    marked += 1
                else:
                    clear += 1
                at = text.find(form, at + 1)
    return marked, clear


def _filled(values: Sequence[str], ctx: Context) -> list[str]:
    return [render(value, ctx.captured) for value in values]


_KINDS: dict[str, Callable[[Any, Context], list[str]]] = {
    "model_received": _model_received,
    "received": _received,
    "file": _file,
    "exchange": _exchange,
    "agent_got": _agent_got,
    "agent_error": _agent_error,
    "rules": _rules,
    "shown": _shown,
    "setting": _setting,
}
