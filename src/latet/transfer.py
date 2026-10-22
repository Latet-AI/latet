"""Your rules in one file, for a colleague: what ``latet rules export`` writes, and how ``latet
rules import`` and the dashboard read and merge it.

The file holds what protects (terms, recognizers, policies, the personal-data types and countries,
field rules...), the exceptions (values and patterns never to mask, secret rules turned off) and the
local reviewer's guidance; never this machine's identity, port, paths or servers. A project's shared
rules travel in its own latet.yaml; your own settings for a project make a file of their own.

Imported, it adds protection at once: new terms, recognizers and field rules, stricter values. What
would lower protection (the exceptions, a weaker value, the guidance, a value stricter at the top
that lowers protection in a folder of yours whose own settings are stricter still) is held back, and
applied only when asked for: then it waits for approval, like any change that lowers protection.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import re2
import yaml
from pydantic import ValidationError

from latet.config import (
    MAX_GUIDANCE,
    POLICY_STRICTNESS,
    PROFILE_STRICTNESS,
    TERM_WITHOUT_LETTERS,
    Config,
    Policy,
    Profile,
    ProjectEntry,
    ProjectSettings,
    join_guidance,
    load_yaml,
)
from latet.engine.detectors.base import quiet_options
from latet.schema import RULES_SCHEMA_URL
from latet.vision.policy import IMAGES_RANK, image_policy

FORMAT = 1  # the version a file says it is in (``latet_rules``): a newer one is refused
VERSION_KEY = "latet_rules"
MAX_BYTES = 2 * 1024 * 1024
LIMITS = {"terms": 5000, "recognizers": 500, "fields": 500, "allow_values": 5000, "allow_patterns": 500}
# What a rules file holds, in the order it is written.
PROTECTING = (
    "profile", "pii", "id_regions", "phone_regions", "include_private_ips", "ner", "terms", "recognizers",
    "policies", "fields", "data", "data_files", "data_hosts", "dump_guard", "images", "ocr",
)  # fmt: skip
EXCEPTIONS = ("allow_values", "allow_patterns", "disabled_secret_rules", "data_tools")
KEYS = (*PROTECTING, *EXCEPTIONS, "guidance")
HEADER = (
    f"# yaml-language-server: $schema={RULES_SCHEMA_URL}\n"
    "# Latet rules: `latet rules import FILE`, or Import rules in the dashboard.\n"
    "# The terms are values in clear: send this file the way you would send them.\n"
)


class RulesError(ValueError):
    """A file that cannot be imported at all: not YAML, too large, made by a newer Latet."""


# --- export -------------------------------------------------------------------------------------------


def _own_entry(config: Config, root: str) -> ProjectEntry | None:
    """Your settings for exactly ``root`` (not those of a folder around it)."""
    from latet.projects import norm_path

    return next((e for e in config.projects if norm_path(e.path) == norm_path(root)), None)


def export_rules(
    config: Config, root: str | None = None, known: Callable[[list[str]], list[bool] | None] | None = None
) -> tuple[str, list[str]]:
    """The rules file: your rules, or your own settings for the project in ``root``; and warnings.

    ``known`` says which recognizer examples are values Latet has seen for real: such a recognizer
    is left out (its examples should be invented, and the file goes to someone else)."""
    data: dict[str, Any] = {}
    if root is None:
        defaults = Config()
        for key in (*PROTECTING, *EXCEPTIONS):
            value = getattr(config, key)
            if value != getattr(defaults, key):
                data[key] = value
        guidance = config.llm_review.guidance
    else:
        entry = _own_entry(config, root)
        settings = entry.model_dump(exclude_defaults=True) if entry is not None else {}
        data = {key: getattr(entry, key) for key in (*PROTECTING, *EXCEPTIONS) if key in settings}
        guidance = entry.review_guidance if entry is not None else None
    warnings: list[str] = []
    if data.get("recognizers"):
        data["recognizers"], dropped = _invented_examples(data["recognizers"], known)
        warnings += dropped
        if not data["recognizers"]:
            del data["recognizers"]
    if guidance:
        data["guidance"] = guidance
    plain = ProjectSettings.model_validate({k if k != "guidance" else "review_guidance": v for k, v in data.items()})
    dumped = plain.model_dump(mode="json", exclude_defaults=True)
    body: dict[str, Any] = {VERSION_KEY: FORMAT}
    for key in KEYS:
        name = "review_guidance" if key == "guidance" else key
        if name in dumped:
            body[key] = dumped[name]
    if data.get("terms"):
        n = len(data["terms"])
        warnings.append(
            f"{n} term{'s are' if n != 1 else ' is'} in the file in clear: send it the way you would send them"
        )
    return HEADER + yaml.safe_dump(body, sort_keys=False, allow_unicode=True), warnings


def _invented_examples(
    recognizers: list[dict[str, Any]], known: Callable[[list[str]], list[bool] | None] | None
) -> tuple[list[dict[str, Any]], list[str]]:
    if known is None:
        return recognizers, []
    examples = [
        [str(x) for x in (r.get("examples") or {}).get(side) or []] if isinstance(r, dict) else []
        for r in recognizers
        for side in ("match", "no_match")
    ]
    values = [v for group in examples for v in group]
    if not values:
        return recognizers, []
    answer = known(values)
    if answer is None or len(answer) != len(values):
        return recognizers, ["the recognizers' examples could not be checked against the vault (the daemon is off)"]
    real = {v for v, seen in zip(values, answer, strict=True) if seen}
    kept, dropped = [], []
    for recognizer in recognizers:
        sides = (recognizer.get("examples") or {}) if isinstance(recognizer, dict) else {}
        mine = {str(x) for side in ("match", "no_match") for x in sides.get(side) or []}
        if mine & real:
            dropped.append(
                f"recognizer {recognizer.get('name')}: left out, its examples hold values Latet has seen for real"
            )
        else:
            kept.append(recognizer)
    return kept, dropped


# --- reading ------------------------------------------------------------------------------------------


def _problem(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    return "; ".join(str(e["msg"]) for e in errors[:3])


def read_rules(text: str) -> tuple[ProjectSettings, list[str]]:
    """A rules file's settings, and what in it was not read. A key with a mistake is left out (and
    said); a file that cannot be read at all raises ``RulesError``. A config.yaml or a latet.yaml
    is read too: what is not a rule in it is left out."""
    from latet.engine.detectors.pii import ALL_PII
    from latet.engine.detectors.terms import TERM_LONGEST, has_letter_or_digit
    from latet.rules_report import report

    if len(text.encode("utf-8")) > MAX_BYTES:
        raise RulesError(f"larger than {MAX_BYTES // (1024 * 1024)} MB")
    try:
        data = load_yaml(text)
    except yaml.YAMLError as exc:
        raise RulesError(f"not valid YAML: {str(exc).splitlines()[0]}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise RulesError("not a mapping of rules")
    data = {str(k): v for k, v in data.items()}
    version = data.pop(VERSION_KEY, FORMAT)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise RulesError(f"{VERSION_KEY}: not a version of the format")
    if version > FORMAT:
        raise RulesError("made by a newer Latet: update Latet to import it")
    review = data.pop("llm_review", None)  # config.yaml: only the reviewer's guidance is a rule
    if isinstance(review, dict) and review.get("guidance") and "guidance" not in data:
        data["guidance"] = review["guidance"]
    notices: list[str] = []
    kept: dict[str, Any] = {}
    for key, value in data.items():
        if key in KEYS:
            name = "review_guidance" if key == "guidance" else key
            try:
                ProjectSettings.model_validate({name: value})
            except ValidationError as exc:
                notices.append(f"{key}: {_problem(exc)}")
                continue
            kept[name] = value
        elif key in Config.model_fields or key in ProjectEntry.model_fields:
            notices.append(f"{key}: not a rule, it stays on each machine")
        else:
            notices.append(f"{key}: not a setting Latet knows")
    for key, limit in LIMITS.items():
        if isinstance(kept.get(key), list) and len(kept[key]) > limit:
            notices.append(f"{key}: more than {limit}, only the first {limit} are read")
            kept[key] = kept[key][:limit]
    settings = ProjectSettings.model_validate(kept)
    unknown = [t for t in settings.pii if t not in ALL_PII]
    if unknown:
        notices.append(f"pii: not types Latet knows: {', '.join(unknown)}")
        settings.pii = [t for t in settings.pii if t in ALL_PII]
    patterns = []
    for place, pattern in enumerate(settings.allow_patterns, 1):
        try:
            re2.compile(pattern, quiet_options())
        except re2.error:  # by its place: a pattern is made of the values it lets through
            notices.append(f"allow_patterns: entry {place} is not a valid RE2 pattern, left out")
            continue
        patterns.append(pattern)
    settings.allow_patterns = patterns
    usable = []
    for place, term in enumerate(settings.terms, 1):
        if not has_letter_or_digit(term.value):
            notices.append(f"term {term.value!r}: {TERM_WITHOUT_LETTERS}")
        elif len(term.value) > TERM_LONGEST:  # by its place, never quoted: it could leave no rule built (S-03)
            notices.append(f"terms: entry {place} holds more than {TERM_LONGEST} characters, left out")
        else:
            usable.append(term)
    settings.terms = usable
    if settings.recognizers:
        recognizers = []
        for item, entry in zip(report(settings.recognizers), settings.recognizers, strict=True):
            if item["state"] == "error":
                notices.append(f"recognizer {item.get('name') or '?'}: {'; '.join(item['errors'])}")
            else:
                recognizers.append(entry)
        settings.recognizers = recognizers
    return settings, notices


# --- merging ------------------------------------------------------------------------------------------


@dataclass
class Report:
    """What an import does: added (adds protection), skipped (yours already, or in conflict with
    yours), held (would lower protection: applied only when asked for), notices (not read)."""

    added: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {"added": self.added, "skipped": self.skipped, "held": self.held, "notices": self.notices}


@dataclass
class Merged:
    protective: Config  # yours, with what adds protection
    full: Config  # and with what was held back
    report: Report


Change = Callable[[Any], None]  # applied to the settings written: config.yaml's, or a project entry


def _some(values: list[str], shown: int = 5) -> str:
    more = len(values) - shown
    return ", ".join(values[:shown]) + (f" and {more} more" if more > 0 else "")


def _rtype(recognizer: dict[str, Any]) -> str:
    return str(recognizer.get("type") or str(recognizer.get("name") or "").upper().replace("-", "_")).upper()


def _applies(base: Config, root: str | None) -> Config:
    """The rules in use: the global ones, or a project's (its file and your settings for it)."""
    if root is None:
        return base
    from latet.projects import effective_config, find_project_file, read_project_file

    found = find_project_file(Path(root))
    repo = read_project_file(found)[0] if found is not None else None
    return effective_config(base, repo, _own_entry(base, root))[0]


def _target(config: Config, root: str | None) -> Any:
    if root is None:
        return config
    entry = _own_entry(config, root)
    if entry is None:
        entry = ProjectEntry(path=root)
        config.projects.append(entry)
    return entry


def merge_rules(
    base: Config,
    imported: ProjectSettings,
    root: str | None = None,
    repositories: Collection[tuple[str, ProjectSettings]] = (),
) -> Merged:
    """``base`` with the rules of a file, into the global rules or into your own settings for the
    project in ``root``. What adds protection is measured against what applies there now, and weighed
    in every folder of yours and in ``repositories``, the folders seen whose repository has a latet.yaml
    of its own (``Projects.repositories``), as a change of config.yaml is (``Runtime.propose``)."""
    from latet.engine.detectors.pii import effective_pii
    from latet.protection import loosened

    current = _applies(base, root)
    report = Report()
    adds: list[Change] = []
    holds: list[Change] = []
    # Whether each addition can lower protection in a folder of yours: what changes the settings a folder's own are laid
    # over (a value, a policy, the personal-data types and their countries, a recognizer named or typed as one of a
    # folder's). Terms, phone countries and field rules that mask only add, there as at the top.
    reach: list[bool] = []

    def add(sentence: str, change: Change, *, folders: bool = True) -> None:
        report.added.append(sentence)
        adds.append(change)
        reach.append(folders)

    def hold(sentence: str, change: Change) -> None:
        report.held.append(sentence)
        holds.append(change)

    _scalars(current, imported, add, hold)
    # Lists: what is new is added; nothing of yours is removed.
    for key, label in (
        ("id_regions", "countries for ID numbers"),
        ("phone_regions", "countries for phone numbers"),
        ("data_files", "the files strict data mode covers"),
        ("data_hosts", "the web addresses strict data mode covers"),
    ):
        new = [r for r in getattr(imported, key) if r not in getattr(current, key)]
        if new:
            change = lambda t, key=key, new=new: setattr(t, key, [*getattr(t, key), *new])  # noqa: E731
            add(f"{label}: {_some(new)}", change, folders=key == "id_regions")  # a folder's phone countries add
    in_use = set(effective_pii(current.pii, current.id_regions, every_region=current.profile == Profile.MAX))
    new_pii = [p for p in imported.pii if p not in in_use]
    if new_pii:
        if root is None:  # a list in config.yaml is used as it is: write the types in use, and the new ones
            regions = list(dict.fromkeys([*current.id_regions, *imported.id_regions]))
            listed = effective_pii(current.pii, regions, every_region=current.profile == Profile.MAX)
            add(f"personal-data types: {_some(new_pii)}", lambda t: setattr(t, "pii", [*listed, *new_pii]))
        else:  # a project's list adds to the global one
            add(f"personal-data types: {_some(new_pii)}", lambda t: setattr(t, "pii", [*t.pii, *new_pii]))
    have = {(t.value, t.type.upper()) for t in current.terms}
    terms = [t for t in imported.terms if (t.value, t.type.upper()) not in have]
    if terms:
        change = lambda t: t.terms.extend(x.model_copy() for x in terms)  # noqa: E731
        add(f"terms: {_some([t.value for t in terms])}", change, folders=False)
    # A recognizer added at the top comes before a folder's own there, a repository's too: one of its name or type puts
    # that one out of use.
    folders = (
        [r for e in [*base.projects, *(repo for _, repo in repositories)] for r in e.recognizers if isinstance(r, dict)]
        if root is None
        else []
    )
    theirs = {str(r.get("name")) for r in folders} | {_rtype(r) for r in folders}
    _recognizers(current, imported, report, add, hold, theirs)
    _fields(current, imported, report, add, hold)
    _exceptions(current, imported, hold)
    guidance = imported.review_guidance
    if guidance and guidance not in (current.llm_review.guidance or ""):
        entry = _own_entry(base, root) if root is not None else None
        own = base.llm_review.guidance if root is None else (entry.review_guidance if entry is not None else None)
        if len(join_guidance(own, guidance) or "") > MAX_GUIDANCE:
            report.skipped.append(f"the local reviewer's guidance: with yours it would pass {MAX_GUIDANCE} characters")
        else:
            preview = guidance if len(guidance) <= 80 else guidance[:77] + "..."
            hold(f"the local reviewer's guidance: {preview}", lambda t: _add_guidance(t, guidance))

    protective = _applied(base, root, adds)
    if loosened(base, protective, repositories):
        # Stricter where it is measured, an addition can lower protection in a folder of yours, or a repository's seen,
        # whose own settings are stricter still: a list of personal-data types written out at the top is used as it is
        # there too, where a max profile detects every country's ID numbers; `images: mask` sends what a max profile
        # blocks. Weighed as it lands in each folder (``loosened``), such an addition is held back, as what lowers
        # protection is. Only those that can reach a folder are weighed alone (``reach``), each on a copy sharing what
        # it does not change: each of a thousand additions weighed on a deep copy of the whole settings, checked again,
        # took a minute.
        for i in reversed(range(len(adds))):
            if reach[i] and loosened(base, _sketched(base, root, adds[i]), repositories):
                report.held.insert(0, report.added.pop(i))
                holds.insert(0, adds.pop(i))
                reach.pop(i)
        protective = _applied(base, root, adds)
    full = _applied(base, root, [*adds, *holds])
    if reasons := loosened(base, protective, repositories):  # never: what is added only protects
        raise RulesError(f"the import would lower protection ({'; '.join(reasons)}): nothing was imported")
    if not report.added and not report.held:
        report.skipped.insert(0, "nothing new: these rules are yours already")
    return Merged(protective, full, report)


def _valid(config: Config) -> Config:
    """Checked again as a whole, as config.yaml would be read."""
    return Config.model_validate(config.model_dump(mode="json"))


def _applied(base: Config, root: str | None, changes: list[Change]) -> Config:
    """``base`` with ``changes`` made, into the global rules or into your own settings for the project in ``root``,
    checked again as a whole."""
    config = base.model_copy(deep=True)
    for change in changes:
        change(_target(config, root))
    return _valid(config)


# What a change may change in place (``t.terms.extend``, ``_appended``, a policy set): copied for a sketch; anything
# else a change sets anew.
_IN_PLACE = ("terms", "recognizers", "fields", "policies")


def _own_containers(settings: Any) -> dict[str, Any]:
    """The lists and the mapping of ``settings`` (the global rules or a project entry) a change may change in place,
    each a copy holding the same items."""
    return {key: type(getattr(settings, key))(getattr(settings, key)) for key in _IN_PLACE}


def _sketched(base: Config, root: str | None, change: Change) -> Config:
    """``base`` with ``change`` made, on a copy sharing what it does not change, unchecked: only weighed
    (``loosened`` reads it), at the cost of a few lists of references rather than of a copy of every term."""
    config = base.model_copy(update={**_own_containers(base), "projects": list(base.projects)})
    if root is not None:  # the project entry a change lands in: its own containers copied too
        config.projects = [e.model_copy(update=_own_containers(e)) for e in base.projects]
    change(_target(config, root))
    return config


def _add_guidance(target: Any, guidance: str) -> None:
    if isinstance(target, Config):
        joined = join_guidance(target.llm_review.guidance, guidance)
        target.llm_review = target.llm_review.model_copy(update={"guidance": joined})
    else:
        target.review_guidance = join_guidance(target.review_guidance, guidance)


def _scalars(current: Config, imported: ProjectSettings, add: Any, hold: Any) -> None:
    """One value each: taken when stricter than the one in use, held back when weaker."""
    from latet.protection import _private_ips

    def take(key: str, value: Any, stricter: bool, sentence: str) -> None:
        (add if stricter else hold)(sentence, lambda t: setattr(t, key, value))

    if imported.profile is not None and imported.profile != current.profile:
        up = PROFILE_STRICTNESS[imported.profile] > PROFILE_STRICTNESS[current.profile]
        take("profile", imported.profile, up, f"profile: {imported.profile.value} (yours: {current.profile.value})")
    if imported.include_private_ips is not None and imported.include_private_ips != _private_ips(current):
        on = imported.include_private_ips
        take("include_private_ips", on, on, f"private IP addresses: {'masked' if on else 'left in clear'}")
    if imported.ner is not None and imported.ner != current.ner:
        take("ner", imported.ner, imported.ner == "auto", f"local name detection: {imported.ner}")
    if imported.data is not None and imported.data != current.data_mode():
        take("data", imported.data, imported.data == "strict", f"strict data mode: {imported.data}")
    if imported.images is not None and imported.images != current.images:
        stricter = IMAGES_RANK[imported.images] > IMAGES_RANK[image_policy(current).send]  # max: block already
        take("images", imported.images, stricter, f"images: {imported.images} (yours: {current.images})")
    if imported.ocr is not None and imported.ocr != current.ocr:
        take("ocr", imported.ocr, imported.ocr == "auto", f"the text in images: {imported.ocr}")
    if imported.dump_guard is not None:  # 0: never, also in a project (where not set means the global one)
        theirs, yours = imported.dump_guard or None, current.dump_guard or None
        if theirs != yours:
            lower = theirs is not None and (yours is None or theirs < yours)
            sentence = f"dump guard: {theirs or 'off'} (yours: {yours or 'off'})"
            take("dump_guard", imported.dump_guard, lower, sentence)
    for etype, policy in imported.policies.items():
        mine = current.policies.get(etype, Policy.MASK)
        if policy != mine:
            up = POLICY_STRICTNESS[policy] > POLICY_STRICTNESS[mine]
            change = add if up else hold
            change(
                f"policy for {etype}: {policy.value} (yours: {mine.value})",
                lambda t, etype=etype, policy=policy: t.policies.__setitem__(etype, policy),
            )


def _recognizers(
    current: Config, imported: ProjectSettings, report: Report, add: Any, hold: Any, theirs: set[str]
) -> None:
    """``theirs``: the names and types of the recognizers of your folders' own settings, which a recognizer added at the
    top of one of them puts out of use there."""
    from latet.protection import _keeps_in_clear

    names = {str(r.get("name")): r for r in current.recognizers if isinstance(r, dict)}
    types = {_rtype(r) for r in current.recognizers if isinstance(r, dict)}
    for recognizer in imported.recognizers:
        name, etype = str(recognizer.get("name")), _rtype(recognizer)
        if name in names:
            if names[name] != recognizer:
                report.skipped.append(f"recognizer {name}: yours, with the same name, is kept")
            continue
        if etype in types:
            report.skipped.append(f"recognizer {name}: the type {etype} is one of yours already")
            continue
        names[name] = recognizer
        types.add(etype)
        if _keeps_in_clear(recognizer):
            hold(
                f"recognizer {name}: leaves what it finds in clear (policy allow)", _appended("recognizers", recognizer)
            )
        else:
            add(f"recognizer {name}", _appended("recognizers", recognizer), folders=bool({name, etype} & theirs))


def _fields(current: Config, imported: ProjectSettings, report: Report, add: Any, hold: Any) -> None:
    names = {f.name.casefold(): f for f in current.fields}
    for rule in imported.fields:
        if rule.name.casefold() in names:
            if names[rule.name.casefold()] != rule:
                report.skipped.append(f"field rule {rule.name}: yours, for the same field, is kept")
            continue
        names[rule.name.casefold()] = rule
        if rule.mask:  # a folder's field rules come first, and this one adds a field: it only adds there too
            add(f"field rule {rule.name}", _appended("fields", rule.model_copy()), folders=False)
        else:
            hold(f"field rule {rule.name}: its values kept in clear in strict data mode", _appended("fields", rule))


def _exceptions(current: Config, imported: ProjectSettings, hold: Any) -> None:
    labels = {
        "allow_values": "values never to mask",
        "allow_patterns": "patterns of values never to mask",
        "disabled_secret_rules": "secret rules turned off",
        "data_tools": "the tools strict data mode covers",
    }
    for key, label in labels.items():
        new = [v for v in getattr(imported, key) if v not in getattr(current, key)]
        if new:
            hold(f"{label}: {_some(new)}", lambda t, key=key, new=new: setattr(t, key, [*getattr(t, key), *new]))


def _appended(key: str, item: Any) -> Change:
    return lambda target: getattr(target, key).append(item)
