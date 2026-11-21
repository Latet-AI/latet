"""Which changes to the settings lower protection.

A change that adds protection applies at once, whoever makes it. One that lowers it waits for you:
asked at your own terminal, approved in the dashboard otherwise (an agent's command, a hand-edited
config.yaml). Each reason names what is lowered, never a value: they reach the agent that asked.
"""

from __future__ import annotations

import functools
import json
import secrets
import unicodedata
from collections.abc import Collection, Hashable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from latet.config import (
    POLICY_STRICTNESS,
    PROFILE_STRICTNESS,
    REVIEW_MODES,
    Config,
    FieldRule,
    IdentityConfig,
    LiveReview,
    LlmReview,
    Policy,
    Profile,
    ProjectSettings,
    TermConfig,
    UnknownMembers,
)
from latet.vision.policy import IMAGES_RANK, image_policy

_PROFILES = PROFILE_STRICTNESS
_POLICIES = POLICY_STRICTNESS
_POLICY_NAMES = frozenset(policy.value for policy in Policy)


def _item(item: Any) -> Hashable:
    """An item of a list as two lists are compared: itself, or for a mapping (a field rule as dumped) its pairs in
    order, or its canonical JSON when they hold a list, so that a list is compared with another by hashing, in time
    linear in their length (a shared list of tens of thousands of terms is weighed at every re-application of every
    space)."""
    try:
        hash(item)
    except TypeError:
        pass
    else:
        return item
    if isinstance(item, dict):
        pairs = tuple(sorted(item.items()))
        try:
            hash(pairs)
        except TypeError:
            pass
        else:
            return pairs
    return json.dumps(item, sort_keys=True, default=str)


def _added(old: Iterable[Any], new: Iterable[Any]) -> int:
    old, new = list(old), list(new)
    try:
        before = set(old)
        return sum(1 for x in new if x not in before)
    except TypeError:  # a mapping among them (field rules, as dumped): each by its canonical JSON
        keyed = {_item(x) for x in old}
        return sum(1 for x in new if _item(x) not in keyed)


def _removed(old: Iterable[Any], new: Iterable[Any]) -> int:
    return _added(new, old)


def _hosts(identity: IdentityConfig) -> list[str]:
    """Every host name an identity masks, wherever it is written (``hostname``, ``hostnames``), each once: one moved
    from a field to the other is no detail dropped."""
    return list(dict.fromkeys(h for h in (identity.hostname, *identity.hostnames) if h))


def _user_names(identity: IdentityConfig) -> list[str]:
    """Every user name an identity masks, wherever it is written (the older ``username``, ``usernames``), each once:
    one moved from a field to the other is no detail dropped, and one dropped from either is."""
    return list(dict.fromkeys(n for n in (identity.username, *identity.usernames) if n))


def _values(n: int) -> str:
    return f"{n} value{'s' if n != 1 else ''}"


def _composed(value: str) -> str:
    """A term's value as the terms detector matches it in its own case: composed and decomposed alike (``_forms``). An
    ASCII value is its own composed form, read as it is."""
    return value if value.isascii() else unicodedata.normalize("NFC", value)


def _terms_lowered(old: list[TermConfig], new: list[TermConfig]) -> tuple[int, int]:
    """How many of ``old``'s terms ``new`` no longer matches at all, and how many values of them it still matches in
    fewer forms: a way one of them was matched (in any case or its own, inside words or as whole words alone) that no
    entry of ``new`` matching its value matches, ``case_sensitive`` or ``whole_word`` turned on. An entry of ``new``
    matches a value when it holds it, or holds it in another case and matches any case: in lower case as RE2 folds case,
    one character for another (a sharp s is not ``ss`` there). A value is matched as every entry of it says, together,
    and is weighed so, in time linear in the lists (a shared list of tens of thousands of terms is weighed at every
    re-application of every space). An old entry whose very entry ``new`` holds (its value composed, both flags) is
    matched in every form it was, with no lowering of its value: a folder's list the same as before (each folder is
    weighed so at the projects' cap) costs one look-up an entry, and the entries in any case are gathered only for an
    old entry that has no such twin."""
    ways: dict[str, set[tuple[bool, bool]]] = {}
    keys = [_composed(term.value) for term in new]
    for key, term in zip(keys, new, strict=True):
        ways.setdefault(key, set()).add((term.case_sensitive, term.whole_word))
    # The entries that match any case, by their value in lower case: whole words or not (gathered at the first need).
    folded: dict[str, set[bool]] | None = None
    removed = 0
    narrowed: set[str] = set()
    for term in old:
        key = _composed(term.value)
        now = ways.get(key, set())
        if (term.case_sensitive, term.whole_word) in now:
            continue  # its very entry: matched as before in every form
        if folded is None:
            folded = {}
            for twin, entry in zip(keys, new, strict=True):
                if not entry.case_sensitive:
                    folded.setdefault(twin.lower(), set()).add(entry.whole_word)
        any_case = folded.get(key.lower(), set())
        if not now and not any_case:
            removed += 1
        elif not any(
            (not case or term.case_sensitive) and (not whole or term.whole_word) for case, whole in now
        ) and not any(not whole or term.whole_word for whole in any_case):
            narrowed.add(term.value)
    return removed, len(narrowed)


def found_type(item: TermConfig | FieldRule) -> str:
    """The type the engine finds the values of a term or a field rule as, which their policy is looked up by: a term's
    type upper-cased (``TermsDetector``), a field rule's too, ``FIELD`` when it has none (``FieldDetector``)."""
    if isinstance(item, TermConfig):
        return item.type.upper()
    return (item.type or "FIELD").upper()


def _term_key(term: TermConfig) -> tuple[str, str]:
    """A term by its value and the type it is found as: two entries of one key are one term, matched as both say."""
    return term.value, found_type(term)


def _terms_in_clear(old: list[TermConfig], new: list[TermConfig], through: Collection[str]) -> int:
    """How many terms of ``new``, by value and type, leave what they find in clear (their type in ``through``, let
    through) where ``old`` did not: a term added or given such a type, or matching more forms than every entry of
    ``old`` with its value and its type (its case no longer its own, inside longer words too). A term's span outranks
    the detectors' (an IBAN found as personal data), which it leaves in clear with itself. One ``old`` holds already as
    it is lowers nothing of its own: in clear there, or masked by a policy that ``new`` sets to allow, which is said as
    that policy. In time linear in the lists."""
    if not through:
        return 0
    ways: dict[tuple[str, str], set[tuple[bool, bool]]] = {}
    for term in old:
        ways.setdefault(_term_key(term), set()).add((term.case_sensitive, term.whole_word))
    found: set[tuple[str, str]] = set()
    for term in new:
        key = _term_key(term)
        if key[1] not in through or key in found:
            continue
        if not any(
            (not case or term.case_sensitive) and (not whole or term.whole_word) for case, whole in ways.get(key, ())
        ):
            found.add(key)
    return len(found)


def _effective_pii(config: Config) -> set[str]:
    from latet.engine.detectors.pii import effective_pii

    if config.profile == Profile.SECRETS:
        return set()
    return set(effective_pii(config.pii, config.id_regions, every_region=config.profile == Profile.MAX))


def private_ips(config: Config) -> bool:
    """Whether ``config`` masks private IP addresses: as it says, else as its profile does (max)."""
    return config.include_private_ips if config.include_private_ips is not None else config.profile == Profile.MAX


def _first_fields(rules: list[FieldRule]) -> dict[str, FieldRule]:
    """Each field name's first rule, the one in use for it (the first rule that matches a field wins; names are matched
    without case)."""
    out: dict[str, FieldRule] = {}
    for rule in rules:
        out.setdefault(rule.name.lower(), rule)
    return out


def _masks_as_much(
    rule: FieldRule, now: FieldRule | None, before: Mapping[str, Policy], after: Mapping[str, Policy]
) -> bool:
    """Whether ``now``, the rule in use for ``rule``'s field after a change, masks it as much as ``rule`` did before:
    masked, a secret if it was one, of its type or of one whose policy in force (``before``, ``after``: as ``_policies``
    lays them) is as strict. Its type's own policy loosened is said as that policy. One typed EMAIL is kept only by one
    typed EMAIL, whatever their policies: the address brings its domain along, masked wherever else it appears
    (``Masker._repeats``), which no other type does."""
    if now is None or not now.mask or (rule.secret and not now.secret):
        return False
    if found_type(now) == found_type(rule):
        return True
    if found_type(rule) == "EMAIL":
        return False
    return _POLICIES[after.get(found_type(now), Policy.MASK)] >= _POLICIES[before.get(found_type(rule), Policy.MASK)]


GUIDANCE_CHANGED = "the local reviewer: its guidance changed"


def _reviewer(old: LlmReview, new: LlmReview) -> list[str]:
    out: list[str] = []
    if old.enabled and not new.enabled:
        return ["the local reviewer: turned off"]
    if new.guidance != old.guidance:  # free text, which could tell it to report nothing: also while it is off
        out.append(GUIDANCE_CHANGED)
    if not new.enabled:
        return out
    if REVIEW_MODES.index(new.mode()) < REVIEW_MODES.index(old.mode()):
        out.append(f"the local reviewer: reads less (from {old.mode()} to {new.mode()})")
    elif n := _removed(old.inline, new.inline):
        out.append(f"the local reviewer: {n} kind{'s' if n != 1 else ''} of text no longer read before it leaves")
    if old.on_failure == "stop" and new.on_failure == "send":
        out.append("the local reviewer: a request it cannot review is sent anyway")
    if old.background and not new.background and not new.inline:
        out.append("the local reviewer: no longer reads after sending")
    if new.budget_ms is not None and (old.budget_ms is None or new.budget_ms < old.budget_ms):
        out.append("the local reviewer: less time to review")
    if old.block and not new.block:
        out.append("the local reviewer: can no longer stop a request")
    if n := _removed(old.auto_mask, new.auto_mask):
        out.append(f"the local reviewer: {n} categor{'ies' if n != 1 else 'y'} no longer masked by itself")
    if old.images and not new.images:
        out.append("the local reviewer: no longer sees the images")
    if new.remote and (not old.remote or new.url != old.url):
        out.append("the local reviewer: the original texts go to a server on another machine")
    elif new.remote and new.images and not old.images:
        out.append("the local reviewer: the images go to a server on another machine")
    if new.in_clear() and not old.in_clear():
        out.append("the local reviewer: the original texts cross your network unencrypted (http)")
    if new.certificate and new.certificate != old.certificate:
        out.append("the local reviewer: a new certificate is trusted for its server")
    return out


def _live(old: LiveReview, new: LiveReview) -> list[str]:
    if old.enabled and not new.enabled:
        return ["the Live Reviewer: turned off"]
    if not new.enabled:
        return []
    out = []
    if n := _removed(old.kinds, new.kinds):
        out.append(f"the Live Reviewer: {n} kind{'s' if n != 1 else ''} of text no longer wait for you")
    if new.auto_approve_clean and not old.auto_approve_clean:
        out.append("the Live Reviewer: texts with nothing found leave without asking you")
    if new.timeout_s is not None and (old.timeout_s is None or new.timeout_s < old.timeout_s):
        out.append("the Live Reviewer: a shorter wait for your answer")
    return out


NEW_MEMBERS_CHECKED = "new request members: passed after a check instead of refused"


def _new_members(old: Config, new: Config) -> list[str]:
    """A request member no request profile names, refused before and read and checked now (``unknown_members``): a
    project may set it too. Settings that do not say it are `check`: an installation upgraded to 0.15.0 moves there with
    no approval (the owner's choice), and only a `refuse` written and then taken away lowers protection."""
    return [NEW_MEMBERS_CHECKED] if old.unknown_members == "refuse" and new.unknown_members == "check" else []


def effective_unknown_members(config: Config, project: ProjectSettings | None) -> UnknownMembers:
    """How a request member no request profile names is read: `refuse` when the settings in use say so (a company's
    floor raises them to it: ``floor.raised_to``, which stays after its license ends) or the project's own settings do
    (a repository's latet.yaml, your settings for the folder: they may only tighten it), `check` otherwise. Never a
    request's word: no header, no launch claim."""
    if config.unknown_members == "refuse" or (project is not None and project.unknown_members == "refuse"):
        return "refuse"
    return "check"


def new_members_checked(old: Config, new: Config) -> dict[str, Any] | None:
    """What an approval from ``old`` to ``new`` moves of ``unknown_members`` from `refuse` to `check`, as a server's
    administration log says it (design §3), never a folder: ``scope`` ``identity`` when the settings themselves move,
    else ``project``; and ``projects``, how many of their projects stop refusing (an entry's `refuse` taken away, the
    settings at `check`) when any does. None when it moves nothing so (a move to `refuse` among them)."""
    from latet.projects import norm_path

    top = old.unknown_members == "refuse" and new.unknown_members == "check"
    after = {norm_path(entry.path): entry for entry in new.projects}
    projects = sum(
        1
        for entry in old.projects
        if entry.unknown_members == "refuse"
        and effective_unknown_members(new, after.get(norm_path(entry.path))) == "check"
    )
    if not top and not projects:
        return None
    scope = "identity" if top else "project"
    details: dict[str, Any] = {"setting": "unknown_members", "value": "check", "scope": scope}
    if projects:
        details["projects"] = projects
    return details


def _masking(old: Config, new: Config) -> list[str]:
    """What lowers the masking keys, the ones a project may set too."""
    out: list[str] = []
    if _PROFILES[new.profile] < _PROFILES[old.profile]:
        out.append(f"profile: from {old.profile.value} to {new.profile.value}")
    if n := len(_effective_pii(old) - _effective_pii(new)):
        out.append(f"personal data: {n} type{'s' if n != 1 else ''} no longer detected")
    if n := _removed(old.phone_regions, new.phone_regions):
        out.append(f"phone numbers: {n} countr{'ies' if n != 1 else 'y'} no longer recognised")
    if private_ips(old) and not private_ips(new):
        out.append("private IP addresses: no longer masked")
    if n := _added(old.disabled_secret_rules, new.disabled_secret_rules):
        out.append(f"secret rules: {n} turned off")
    recognizers_before = recognizers_in_force(old.recognizers)
    recognizers_after = recognizers_in_force(new.recognizers)
    policies_before = _policies(old.policies, recognizers_before)
    policies_after = _policies(new.policies, recognizers_after)
    # The types whose values the new settings leave in clear: a term or a field rule of one leaves what it finds so.
    through = {etype for etype, policy in policies_after.items() if policy == Policy.ALLOW}
    # Of a type let through, a term still masks something (an EMAIL one brings its domain along): taken out, or
    # matching fewer forms, it is said as any term.
    removed, narrowed = _terms_lowered(old.terms, new.terms)
    if removed:
        out.append(f"terms: {_values(removed)} no longer masked")
    if narrowed:
        out.append(f"terms: {narrowed} match{'es' if narrowed == 1 else ''} fewer forms")
    if n := _terms_in_clear(old.terms, new.terms, through):
        out.append(f"terms: {n} that leave what they find in clear (policy allow)")
    # A recognizer is free text too: one changed may find less, or none (turned off); one whose type's policy in force
    # lets its finds through leaves them in clear. Matched by what it finds and does, not by its name or its type.
    changed, in_clear = _recognizers_lowered(recognizers_before, recognizers_after, policies_before, policies_after)
    changed_at = len(out)  # where a type's policy loosened that nothing names is said as a recognizer changed, below
    if changed:
        out.append(f"recognizers: {changed} changed or removed")
    if in_clear:
        out.append(f"recognizers: {in_clear} that leave what they find in clear (policy allow)")
    if n := _added(old.allow_values, new.allow_values):
        out.append(f"values never to mask: {n} added")
    if n := _added(old.allow_patterns, new.allow_patterns):
        out.append(f"patterns of values never to mask: {n} added")
    # A type weighed as the policy it ends up with, said by its name when something else finds values of it: one
    # `policies` names on either side (an entry taken away leaves the type to its recognizer's own, `allow` perhaps),
    # one a term or a field rule of either side uses, and one Latet's own detectors give (strict data mode's DATA, the
    # name detector's, the local reviewer's: ``detected_types``), whose values all follow a recognizer's own policy as
    # their type's default (one with `policy: allow` retyped to a term's type, or to DATA, sends those values in
    # clear). Any other type a change loosens is said as a recognizer changed, without a type named, as before 0.9.8:
    # the vault finds remembered values again under the type they were found as (Ruling 098-R4).
    lowered = sorted(
        etype
        for etype in {*policies_before, *policies_after}
        if _POLICIES[policies_after.get(etype, Policy.MASK)] < _POLICIES[policies_before.get(etype, Policy.MASK)]
    )
    if lowered:
        from latet.engine.build import detected_types

        weighed = {*old.policies, *new.policies, *detected_types()}
        weighed |= {found_type(term) for terms in (old.terms, new.terms) for term in terms}
        weighed |= {found_type(rule) for fields in (old.fields, new.fields) for rule in fields}
        unnamed = 0
        for etype in lowered:
            if etype in weighed:
                before, after = policies_before.get(etype, Policy.MASK), policies_after.get(etype, Policy.MASK)
                out.append(f"policy for {etype}: from {before.value} to {after.value}")
            else:
                unnamed += 1
        if unnamed and not changed:
            out.insert(changed_at, f"recognizers: {unnamed} changed or removed")
    if old.ner == "auto" and new.ner == "off":
        out.append("local name detection: turned off")
    # As it applies: the profile's when `data` is not set, so leaving max turns it off too.
    strict_before, strict_after = old.data_mode() == "strict", new.data_mode() == "strict"
    if strict_before and not strict_after:
        out.append("strict data mode: turned off")
    elif strict_after and new.data_tools and (not old.data_tools or _removed(old.data_tools, new.data_tools)):
        out.append("strict data mode: covers fewer tools")
    # A file or a web address named is data with strict data mode on or off: one taken away covers less either way.
    if _removed(old.data_files, new.data_files):
        out.append("strict data mode: covers fewer files")
    if _removed(old.data_hosts, new.data_hosts):
        out.append("strict data mode: covers fewer web addresses")
    # The first rule of a field is the one in use: one that masked, taken out, put out of use or masking less, lowers
    # (of a type let through too: it still masks something, as a term); one that kept the field's values in clear,
    # taken out, masks them again.
    fields_after = _first_fields(new.fields)
    if any(
        rule.mask and not _masks_as_much(rule, fields_after.get(name), policies_before, policies_after)
        for name, rule in _first_fields(old.fields).items()
    ):
        out.append("field rules: changed or removed")
    old_fields, new_fields = [f.model_dump() for f in old.fields], [f.model_dump() for f in new.fields]
    kept_before = {_item(f) for f in old_fields}
    if any(not f.mask and _item(dumped) not in kept_before for f, dumped in zip(new.fields, new_fields, strict=True)):
        out.append("field rules: values kept in clear in strict data mode")
    # One that masks, of a type let through: its values, found by their field, outrank what else finds them (a secret's
    # name) and are left in clear. One held already as it is lowers nothing of its own, as a term (``_terms_in_clear``).
    in_clear = {
        _item(dumped)
        for rule, dumped in zip(new.fields, new_fields, strict=True)
        if rule.mask and found_type(rule) in through
    }
    if n := len(in_clear - kept_before):
        out.append(f"field rules: {n} that leave what they find in clear (policy allow)")
    before = old.dump_guard or None
    after = new.dump_guard or None
    if before is not None and (after is None or after > before):
        out.append("dump guard: raised or turned off")
    # What is sent in the end: images: mask with the max profile sends images that max alone would not.
    images_before, images_after = image_policy(old).send, image_policy(new).send
    if new.images != old.images and IMAGES_RANK[images_after] < IMAGES_RANK[images_before]:
        out.append(f"images: from {images_before} to {images_after}")
    if old.ocr == "auto" and new.ocr == "off":
        out.append("the text in images: no longer read")
    return out


@functools.cache
def _sample() -> str:
    """What a build tries a recognizer on (``compile_all``'s corpus): read once."""
    from latet.engine.corpus import benign_sample

    return benign_sample()


# The recognizers kept compiled by their JSON (``_built``): at least both sides of a weighing of a server's identity's
# settings at their caps, its own recognizers with as many an edition may share (``server.shared.OWN_RULES``), so that
# no call compiles them all again; past the cache's size each call compiled every one again (seconds a call).
RECOGNIZERS_COMPILED = 4096


@functools.lru_cache(maxsize=RECOGNIZERS_COMPILED)
def _built(recognizer: str) -> tuple[str, str | None]:
    """A recognizer (its canonical JSON) as a build compiles it (``compile_one``, with the types Latet has and the
    corpus ``compile_all`` is given): its name, and its type when the build keeps it in use (valid, enabled, not too
    broad); None when it does not. Kept by its JSON: a re-application weighs the same recognizers again and again."""
    from latet.engine.build import reserved_types
    from latet.engine.detectors.recognizers import compile_one

    found, status = compile_one(json.loads(recognizer), reserved_types(), _sample())
    return status.name, (found.etype if found is not None else None)


def recognizers_in_force(recognizers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The recognizers a build of ``recognizers`` keeps, by name, as ``compile_all`` does: the first of each name, a
    broken one's too, and none whose type one before it in use has; a recognizer that does not compile (or is off, or
    too broad) takes no type, so that it never puts one of its type out of use where the build keeps that one. In a
    project the global ones come first: one added there with the name or the type of the project's own puts that one
    out of use, which the project's scope must see (a repository file's is laid apart instead:
    ``projects.effective_config``)."""
    out: dict[str, dict[str, Any]] = {}
    names: set[str] = set()
    types: set[str] = set()
    for recognizer in recognizers:
        if not isinstance(recognizer, dict):
            continue
        name, etype = _built(json.dumps(recognizer, sort_keys=True, default=str))
        if name in names:
            continue
        names.add(name)
        if etype is not None:
            if etype in types:
                continue
            types.add(etype)
        out[name] = recognizer
    return out


def _as_found(recognizer: dict[str, Any], policies: Mapping[str, Policy]) -> tuple[Hashable, int] | None:
    """What a recognizer in force finds and does: its definition but for its name, its type and its own policy
    (``_item``), and how strict the policy in force for its type is (``policies``, as ``_policies`` lays them). None for
    one a build does not keep (off, broken, too broad): it finds nothing."""
    etype = recognizer_type(recognizer)
    if etype is None:
        return None
    core = _item({key: value for key, value in recognizer.items() if key not in ("name", "type", "policy")})
    return core, _POLICIES[policies.get(etype, Policy.MASK)]


def _recognizers_lowered(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    policies_before: Mapping[str, Policy],
    policies_after: Mapping[str, Policy],
) -> tuple[int, int]:
    """How many recognizers in force ``before`` (by name, ``recognizers_in_force``) are changed or removed in
    ``after``, and how many of ``after`` leave what they find in clear (policy allow) where none did before. A
    recognizer is matched by what it finds and does (``_as_found``), not by its name or its type: renamed, laid under a
    name and a type of its own in a project (``projects.effective_config``), or given a stricter policy, it is the same
    recognizer; one that finds other values, or whose type's policy in force got looser, is not. One the same by name
    and definition on both sides is left out first (a `policies` entry changed around it is said as that policy). One
    that left what it found in clear masked nothing: taken out, or changed into one of its name that masks what it
    finds, it masks no less (changed into one that leaves other values in clear, it is said). In time linear in the
    lists."""
    allow = _POLICIES[Policy.ALLOW]
    same = {name for name, body in before.items() if after.get(name) == body}
    found_before = [(name, _as_found(body, policies_before)) for name, body in before.items() if name not in same]
    found_after = [_as_found(body, policies_after) for name, body in after.items() if name not in same]
    free: dict[Hashable, list[int]] = {}  # by definition, the policies in force after, the least strict first
    for core, rank in sorted((found for found in found_after if found is not None), key=lambda found: found[1]):
        free.setdefault(core, []).append(rank)
    changed = 0
    for name, found in found_before:
        if found is None:
            continue
        core, rank = found
        ranks = free.get(core, [])
        place = next((i for i, kept in enumerate(ranks) if kept >= rank), None)
        if place is not None:
            del ranks[place]  # matched: the least strict that is as strict, the stricter ones left for others
        elif rank != allow or ((now := after.get(name)) is not None and _leaves_in_clear(now, policies_after)):
            changed += 1
    in_clear = sum(1 for ranks in free.values() for rank in ranks if rank == allow)
    return changed, in_clear


def _leaves_in_clear(recognizer: dict[str, Any], policies: Mapping[str, Policy]) -> bool:
    """Whether ``recognizer``, in force, leaves what it finds in clear: its type's policy in force (``policies``, as
    ``_policies`` lays them) is allow."""
    etype = recognizer_type(recognizer)
    return etype is not None and policies.get(etype, Policy.MASK) == Policy.ALLOW


def keeps_in_clear(recognizer: Any) -> bool:
    """A recognizer whose own policy lets what it finds through (``policies`` cannot see it)."""
    return isinstance(recognizer, dict) and str(recognizer.get("policy") or "").lower() == Policy.ALLOW.value


def recognizer_type(recognizer: Any) -> str | None:
    """The type a build gives ``recognizer`` when it keeps it in use (``_built``); None when it does not."""
    if not isinstance(recognizer, dict):
        return None
    return _built(json.dumps(recognizer, sort_keys=True, default=str))[1]


def _policies(policies: Mapping[str, Policy], in_force: dict[str, dict[str, Any]]) -> dict[str, Policy]:
    """Each type's policy as a build lays them (``build_masker``): a recognizer's own is its type's default, an entry of
    ``policies`` wins; a type in neither is masked. ``in_force``: the recognizers as a build keeps them
    (``recognizers_in_force``)."""
    out: dict[str, Policy] = {}
    for recognizer in in_force.values():
        own = str(recognizer.get("policy") or "").lower()
        if own in _POLICY_NAMES:
            etype = _built(json.dumps(recognizer, sort_keys=True, default=str))[1]
            if etype is not None:
                out[etype] = Policy(own)
    return out | dict(policies)


def policies_in_force(config: Config) -> dict[str, Policy]:
    """Each type's policy in ``config`` as a build lays them (``_policies``): a type in neither its policies nor its
    recognizers' own is masked."""
    return _policies(config.policies, recognizers_in_force(config.recognizers))


def lets_through(config: Config) -> frozenset[str]:
    """The types whose values ``config`` leaves in clear (policy allow), as a build lays the policies: a term or a field
    rule of one leaves what it finds in clear."""
    return let_through_by(config.policies, config.recognizers)


def let_through_by(policies: Mapping[str, Policy], recognizers: list[Any]) -> frozenset[str]:
    """The types ``policies`` and ``recognizers`` leave in clear together (policy allow), as a build lays them: in a
    project, the global ones and the project's own laid over them (``lets_through`` for settings as a whole)."""
    in_force = recognizers_in_force(recognizers)
    return frozenset(etype for etype, policy in _policies(policies, in_force).items() if policy == Policy.ALLOW)


def recognizers_let_through(recognizers: Iterable[Any]) -> frozenset[str]:
    """The types of ``recognizers`` whose own policy lets what they find through, each as a build types it."""
    out: set[str] = set()
    for recognizer in recognizers:
        if isinstance(recognizer, dict) and keeps_in_clear(recognizer):
            etype = _built(json.dumps(recognizer, sort_keys=True, default=str))[1]
            if etype is not None:
                out.add(etype)
    return frozenset(out)


def folders_let_through(folders: Iterable[ProjectSettings]) -> frozenset[str]:
    """The types the own settings of ``folders`` (your settings for a project, a repository's latet.yaml) may let
    through there: their own policies set to allow, and the types of their own recognizers that let what they find
    through. A term or a field rule of one of them, added at the top, leaves what it finds in clear in such a folder."""
    out: set[str] = set()
    for folder in folders:
        out.update(etype for etype, policy in folder.policies.items() if policy == Policy.ALLOW)
        out.update(recognizers_let_through(folder.recognizers))
    return frozenset(out)


class _Changed:
    """The terms and field rules a change of the top adds or changes: a term new by its value, type and options, a field
    rule new as a whole. The only ones of the top's lists that can leave values in clear in a folder whose own settings
    let their type through (``folders_let_through``) and not at the top, where what else a change of the lists lowers (a
    term removed, one matching fewer forms) is said already: weighed in each such folder alone (``in_clear``), without
    the tens of thousands of terms that did not change. A folder's own term is in force there whatever the top holds but
    the very same entry (``effective_config``): one the top no longer holds brings into force no form that was not.
    Made in time linear in the lists, once a check."""

    def __init__(self, old: Config, new: Config) -> None:
        had = {(t.value, found_type(t), t.case_sensitive, t.whole_word) for t in old.terms}
        self.terms = [t for t in new.terms if (t.value, found_type(t), t.case_sensitive, t.whole_word) not in had]
        keys = {_term_key(t) for t in self.terms}
        self.before: dict[tuple[str, str], list[TermConfig]] = {}  # the top's entries of those values and types before
        for term in old.terms:
            if (key := _term_key(term)) in keys:
                self.before.setdefault(key, []).append(term)
        self.fields: list[tuple[FieldRule, Hashable]] = []
        if old.fields != new.fields:
            rules = {_item(f.model_dump()) for f in old.fields}
            self.fields = [(f, item) for f in new.fields if (item := _item(f.model_dump())) not in rules]
        self.types = {found_type(t) for t in self.terms}
        self.types |= {found_type(f) for f, _ in self.fields if f.mask}

    def in_clear(
        self, through: Collection[str], layers: list[ProjectSettings], strict: Collection[int] = ()
    ) -> list[str]:
        """What the changed terms and field rules leave in clear in a folder whose types let through are ``through``
        and whose own settings are ``layers`` (a repository's file, your settings for the folder), as ``_masking``
        weighs that folder's lists in full: a term whose value and type the folder had before (from the top and from
        every layer that holds them, as ``effective_config`` adds them) in no form it matches, a field rule that masks
        and that none of its layers holds. ``strict``: the places in ``layers`` of the ones that may only add protection
        (a repository file), whose term or field rule of a type let through is set aside there, never in force
        (``projects._layer``)."""
        out: list[str] = []
        if terms := [t for t in self.terms if found_type(t) in through]:
            keys = {_term_key(t) for t in terms}
            before = [e for key in keys for e in self.before.get(key, [])]
            for i, layer in enumerate(layers):
                before += [
                    term
                    for term in layer.terms
                    if _term_key(term) in keys and (i not in strict or found_type(term) not in through)
                ]
            if n := _terms_in_clear(before, terms, through):
                out.append(f"terms: {n} that leave what they find in clear (policy allow)")
        rules = {item for rule, item in self.fields if rule.mask and found_type(rule) in through}
        held = {
            _item(f.model_dump())
            for i, layer in enumerate(layers)
            for f in layer.fields
            if i not in strict or found_type(f) not in through
        }
        if rules and (n := len(rules - held)):
            out.append(f"field rules: {n} that leave what they find in clear (policy allow)")
        return out


def lowered_in_project(old: Config, new: Config) -> list[str]:
    """What the rules of a project, ``new``, lower against ``old`` (the same global settings around both): its masking,
    how it reads new request members, its local reviewer and its Live Reviewer, which is all a project sets. One reason
    each, never a value."""
    return (
        _masking(old, new)
        + _new_members(old, new)
        + _reviewer(old.llm_review, new.llm_review)
        + _live(old.live_review, new.live_review)
    )


# The lists a project's own settings only add to: inside a project each holds the global items and the project's (its
# field rules first), so that what a change of the global ones lowers there, it lowers at the top as much, where it is
# said. A folder whose entry is the same on both sides is weighed without them (``_context``).
_ADDED_TO = ("terms", "allow_values", "allow_patterns", "disabled_secret_rules", "phone_regions", "fields")


def _context(config: Config) -> Config:
    """What of ``config`` a project's entry is laid over and a change of it can lower there beyond what it lowers at
    the top: ``config`` without the lists an entry only adds to (``_ADDED_TO``), its projects and its destinations. A
    setting the global settings write as their own profile has it changes nothing at the top, and overrides there what
    a project's stricter profile gives where it is not set (private addresses, strict data mode and its tools, the
    types of personal data, the images); the Live Reviewer a project turns on takes the global settings' kinds and
    waits; a global recognizer of the name or the type of a project's own puts that one out of use."""
    return config.model_copy(update={key: [] for key in (*_ADDED_TO, "projects", "upstreams")})


def _by_folders(said: list[tuple[str, str]]) -> list[str]:
    """``(folder, reason)`` pairs as they are said: a reason of one folder as ``project <folder>: <reason>``, one of
    several once, its folders named up to three and then counted (``projects a, b and 198 more: <reason>``), in the
    order each reason first came. Two hundred folders with two settings lowered make two reasons, never four hundred."""
    folders: dict[str, list[str]] = {}
    for name, reason in said:
        names = folders.setdefault(reason, [])
        if name not in names:
            names.append(name)
    out = []
    for reason, names in folders.items():
        if len(names) == 1:
            out.append(f"project {names[0]}: {reason}")
        elif len(names) <= 3:
            out.append(f"projects {', '.join(names[:-1])} and {names[-1]}: {reason}")
        else:
            out.append(f"projects {', '.join(names[:2])} and {len(names) - 2} more: {reason}")
    return out


def _own_lowering(new: Config, repo: Any, was: Any, now: Any) -> list[str]:
    """What an entry's own change lowers, weighed under the new settings alone: said for its folder even when the top
    lowers the same, in the same words (the folder's own lowering is hers to approve too)."""
    from latet.projects import effective_config

    before, _ = effective_config(new, repo, was)
    after, _ = effective_config(new, repo, now)
    return lowered_in_project(before, after)


def _shape(entry: Any) -> str:
    """What an entry sets that a change of its context can lower: all of it but its folder and the lists it only adds
    to. Entries of one shape are weighed once."""
    return json.dumps(entry.model_dump(mode="json", exclude={"path", *_ADDED_TO}), sort_keys=True)


def _projects(old: Config, new: Config, top: Collection[str] = ()) -> list[str]:
    """What the change lowers in each project of your settings, weighed as it applies there: the project's rules under
    the old settings against its rules under the new ones, each laid over its own side's global settings, so that a
    change of the global settings that lowers protection only in a project whose own settings are stricter (a stricter
    profile, the Live Reviewer turned on there) is weighed where it lands. A folder whose entry is the same on both
    sides is weighed only when its context changed (``_context``), once for every entry of one shape, without the lists
    it only adds to; and, where its own settings let a type through (``folders_let_through``), for the terms and field
    rules the top changed of such a type alone (``_Changed``), which leave what they find in clear there. A reason
    ``top`` gives already, the global settings' own, is not said again for each folder, but one the folder's own entry
    gives too (``_own_lowering``). Folders that share a reason say it once (``_by_folders``)."""
    from latet.projects import effective_config, norm_path

    said: list[tuple[str, str]] = []
    before = {norm_path(e.path): e for e in old.projects}
    after = {norm_path(e.path): e for e in new.projects}
    contexts: tuple[Config, Config] | None = None
    shapes: dict[str, list[str]] = {}  # what a change of the context lowers in an entry of a shape
    allowed: dict[str, frozenset[str]] = {}  # the types let through in an entry of a shape, under the new settings
    changed: _Changed | None = None  # what the top changed of its terms and field rules, made once if needed
    for path in sorted({*before, *after}):
        was, now = before.get(path), after.get(path)
        name = Path(path.rstrip("/\\")).name or path
        if (was is None or was.enabled) and now is not None and not now.enabled:
            said.append((name, "Latet turned off"))
            continue
        own: Collection[str] = ()
        if was == now and now is not None:
            if contexts is None:
                contexts = (_context(old), _context(new))
            reasons: list[str] = []
            shape: str | None = None
            if contexts[0] != contexts[1]:
                shape = _shape(now)
                if shape not in shapes:
                    old_here, _ = effective_config(contexts[0], None, now)
                    new_here, _ = effective_config(contexts[1], None, now)
                    shapes[shape] = lowered_in_project(old_here, new_here)
                reasons = list(shapes[shape])
            if lets := folders_let_through([now]):
                changed = changed if changed is not None else _Changed(old, new)
                if lets & changed.types:
                    shape = shape if shape is not None else _shape(now)
                    if shape not in allowed:
                        allowed[shape] = lets_through(effective_config(contexts[1], None, now)[0])
                    reasons += changed.in_clear(allowed[shape], [now])
        else:
            old_here, _ = effective_config(old, None, was)
            new_here, _ = effective_config(new, None, now)
            reasons = lowered_in_project(old_here, new_here)
            if top and any(reason in top for reason in reasons):
                own = set(_own_lowering(new, None, was, now))
        said += [(name, reason) for reason in reasons if reason not in top or reason in own]
    return _by_folders(said)


def _repositories(
    old: Config, new: Config, repositories: Collection[tuple[str, ProjectSettings]], top: Collection[str]
) -> list[str]:
    """What the change lowers in each project whose repository has a latet.yaml of its own (a computer's: a server
    reads none), weighed as a project of your settings is: its rules under the old settings against its rules under
    the new ones, the file and your settings for its folder laid over each side's own. A repository's stricter profile
    is defeated so by a line written at the top as the global profile has it (``include_private_ips: false``). Weighed
    only where your settings for the folder or what the file is laid over changed; and, where the file or your settings
    for the folder let a type through (``folders_let_through``), for the terms and field rules the top changed of such
    a type alone (``_Changed``)."""
    from latet.projects import effective_config, personal_for

    said: list[tuple[str, str]] = []
    contexts: tuple[Config, Config] | None = None
    changed: _Changed | None = None  # what the top changed of its terms and field rules, made once if needed
    for root, repo in repositories:
        was, now = personal_for(old.projects, root), personal_for(new.projects, root)
        name = Path(root.replace("\\", "/").rstrip("/")).name or root
        if was == now:
            if contexts is None:
                contexts = (_context(old), _context(new))
            if contexts[0] == contexts[1]:  # the same rules there on both sides, but for the top's lists
                layers = [repo] if now is None else [repo, now]
                if lets := folders_let_through(layers):
                    changed = changed if changed is not None else _Changed(old, new)
                    if lets & changed.types:
                        through = lets_through(effective_config(contexts[1], repo, now)[0])
                        said += [(name, r) for r in changed.in_clear(through, layers, strict=(0,)) if r not in top]
                continue
        old_here, _ = effective_config(old, repo, was)
        new_here, _ = effective_config(new, repo, now)
        reasons = lowered_in_project(old_here, new_here)
        own: Collection[str] = ()
        if was != now and top and any(reason in top for reason in reasons):
            own = set(_own_lowering(new, repo, was, now))
        said += [(name, r) for r in reasons if r not in top or r in own]
    return _by_folders(said)


# The settings that protect nothing in a project: the port, what the history keeps and for how long. A change of them
# alone lowers nothing in a repository's latet.yaml, and no repository is read for it (R21).
_NOT_PROTECTING = ("port", "history", "retention_days")
# What no repository's latet.yaml can hold, nor is laid over in one (``project_file.REPO_KEYS``): your request
# exceptions are weighed at the top alone, whatever the repositories hold.
_NOT_IN_REPOSITORIES = ("request_exceptions",)


def weighs_repositories(old: Config, new: Config) -> bool:
    """Whether a change from ``old`` to ``new`` can lower anything in a repository's own latet.yaml beyond what it
    lowers at the top (``_repositories``): only where your settings for folders changed, or a setting that protects
    and that the file is laid over (``_context``, without ``_NOT_PROTECTING``). One that changes neither (a term added,
    the port, the history and its retention) is weighed the same whatever the repositories hold: none need be read for
    it, and one that cannot be read now holds it back from nothing."""
    if old.projects != new.projects:
        return True
    unweighed: dict[str, Any] = dict.fromkeys((*_NOT_PROTECTING, *_NOT_IN_REPOSITORIES))
    return _context(old).model_copy(update=unweighed) != _context(new).model_copy(update=unweighed)


def guidance_changes(old: Config, new: Config) -> list[dict[str, str | None]]:
    """Where the local reviewer's guidance changes (``where``: None for the global one, else the
    project's name), with the text before and after: for the dashboard, never for the reasons."""
    from latet.projects import norm_path

    out: list[dict[str, str | None]] = []
    if old.llm_review.guidance != new.llm_review.guidance:
        out.append({"where": None, "before": old.llm_review.guidance, "after": new.llm_review.guidance})
    before = {norm_path(e.path): e.review_guidance for e in old.projects}
    after = {norm_path(e.path): e.review_guidance for e in new.projects}
    for path in sorted({*before, *after}):
        if before.get(path) != after.get(path):
            name = Path(path.rstrip("/\\")).name or path
            out.append({"where": name, "before": before.get(path), "after": after.get(path)})
    return out


def _destinations(old: Config, new: Config) -> list[str]:
    """A destination moved or gone: an agent pointed at it would send its requests, and its keys,
    elsewhere. Taking one away first and adding it back with another address would do the same."""
    now = {d.name: d.url for d in new.upstreams}
    out = []
    for destination in old.upstreams:
        if destination.name not in now:
            out.append(f"destination {destination.name}: removed")
        elif now[destination.name] != destination.url:
            # An exception names its destination: it follows it to the new address (Minor 9), and the move says so.
            bound = sum(1 for row in new.request_exceptions if row.destination == destination.name and row.approved)
            follows = (
                ""
                if not bound
                else ", and the request exception that names it goes with it"
                if bound == 1
                else f", and the {bound} request exceptions that name it go with it"
            )
            out.append(f"destination {destination.name}: its address changed{follows}")
    return out


def destination_changes(old: Config, new: Config) -> list[dict[str, str | None]]:
    """The destinations moved or gone, with their address before and after (None: gone): for the
    dashboard, which shows where requests would go."""
    now = {d.name: d.url for d in new.upstreams}
    return [
        {"name": d.name, "before": d.url, "after": now.get(d.name)} for d in old.upstreams if now.get(d.name) != d.url
    ]


def _exceptions(old: Config, new: Config) -> list[str]:
    """The request exceptions ``new`` adds, widens, renews or makes permanent (D3, R-108): each a loosening, said by
    its destination alone, never by what it names or its description. One taken away, narrowed, shortened, scoped to a
    project, made no longer permanent or only described applies at once. One whose id, approval time or revision is
    not the one approved is new: Latet's stamps are never caller input."""
    before = {row.id: row for row in old.request_exceptions if row.id is not None}
    return [
        f"a request exception for {row.destination}: {_approved_as(row, how)}"
        for row in new.request_exceptions
        if (how := exception_loosened(before.get(row.id) if row.id is not None else None, row)) is not None
    ]


def _approved_as(row: Any, how: str) -> str:
    """What approving ``row`` approves, as its approval says it (I-2, D3): how it lowers protection, how long it then
    lasts (``permanent``, chosen on purpose, or its days), and that it lets a credential through unread when one of
    its components is a destination's credential (D4); by category alone, never what it names or its description."""
    if how == "made permanent":
        words = how
    elif row.permanent:
        words = f"{how}, permanent"
    else:
        words = f"{how} for {row.days} days"
    if any(component.role == "credential" for component in row.components):
        words += ", lets a credential through unread"
    return words


def exception_view(row: Any) -> dict[str, Any]:
    """An exception as the dashboard shows it to its owner (P2-7): what it names (each component's kind, exact name,
    role and a body member's type), where (its destination and request profile), for whom (``identity``, or ``project``
    with the project folder's name alone), why by category, how long and the owner's own words. Never a request's
    value."""
    from pathlib import PureWindowsPath

    project = PureWindowsPath(row.project).name or row.project if row.project is not None else None
    return {
        "id": row.id,
        "destination": row.destination,
        "profile": row.profile,
        "scope": "project" if row.project is not None else "identity",
        "project": project,
        "reason": row.reason,
        "components": [{"kind": c.kind, "name": c.name, "role": c.role, "type": c.type} for c in row.components],
        "days": row.days,
        "permanent": row.permanent,
        "description": row.description,
        # Who approved it, by its category (owner, person, administration, security, controllers): None while it waits;
        # with security or controllers (Latet Enterprise), the name of the person who did.
        "issuer": row.issuer,
        "approver": row.approver,
    }


def exception_changes(old: Config, new: Config) -> list[dict[str, Any]]:
    """The request exceptions ``new`` adds, widens, renews or makes permanent against ``old`` (the settings approved),
    each as the dashboard shows it (``exception_view``) with how it lowers protection (``how``): what an approval would
    let through, read before it is approved (R-75: one for the whole identity says so)."""
    before = {row.id: row for row in old.request_exceptions if row.id is not None}
    out = []
    for row in new.request_exceptions:
        how = exception_loosened(before.get(row.id) if row.id is not None else None, row)
        if how is not None:
            out.append({**exception_view(row), "how": how})
    return out


def exception_loosened(was: Any, row: Any) -> str | None:
    """How ``row`` lowers protection against ``was``, the approved exception of the same id (None: there is none):
    ``added``, ``widened``, ``made permanent`` or ``renewed``; None when it does not."""
    from latet.projects import norm_path

    if was is None or (_stamps(was) != _stamps(row) and not _earlier(was, row)):
        return "added"
    project_was = norm_path(was.project) if was.project is not None else None
    project_now = norm_path(row.project) if row.project is not None else None
    # A body member's type is part of what it names: the same pointer with another type lets another value through.
    components_was = {(c.kind, c.name, c.type): c.role for c in was.components}
    if (
        (row.destination, row.profile) != (was.destination, was.profile)
        or (project_was is not None and project_now != project_was)
        or any(components_was.get((c.kind, c.name, c.type)) not in (c.role, "credential") for c in row.components)
    ):
        return "widened"
    if row.permanent and not was.permanent:
        return "made permanent"
    if not row.permanent and not was.permanent and (row.expires() or row.granted) > (was.expires() or was.granted):
        return "renewed"
    return None


def _stamps(row: Any) -> tuple[Any, ...]:
    """Latet's stamps on an exception, who approved it among them: one whose stamps are not the ones approved is new."""
    return (row.granted, row.revision, row.body_revision, row.issuer, row.approver, row.approver_id)


def stamp_exceptions(
    base: Config,
    approved: Config,
    now: datetime,
    revision: str,
    issuer: str | None = None,
    approver: tuple[str, str] | None = None,
) -> Config:
    """``approved`` as you approved it: each exception it adds, widens, renews or makes permanent against ``base`` (the
    settings approved before) stamped by Latet, a new id, ``now`` as its approval time (its lifetime runs from then),
    the request profiles' ``revision`` (with body components, its body protocol's revision too, R-100) and who
    approved it (``issuer``, and with a decider of the company's ``approver``, their name and id), whatever stamps it
    carried; every other one as it is."""
    from latet.gateway.body_selectors import revision as body_revision
    from latet.gateway.request_policy import body_protocol

    before = {row.id: row for row in base.request_exceptions if row.id is not None}
    rows = []
    for row in approved.request_exceptions:
        if exception_loosened(before.get(row.id) if row.id is not None else None, row) is not None:
            protocol = body_protocol(row.profile) if any(c.kind == "body" for c in row.components) else None
            row = row.model_copy(
                update={
                    "id": "x" + secrets.token_hex(5),
                    "granted": now,
                    "revision": revision,
                    "body_revision": body_revision(protocol) if protocol is not None else None,
                    "issuer": issuer,
                    "approver": approver[0] if approver is not None else None,
                    "approver_id": approver[1] if approver is not None else None,
                }
            )
        rows.append(row)
    if rows == list(approved.request_exceptions):
        return approved
    return approved.model_copy(update={"request_exceptions": rows})


def _earlier(was: Any, row: Any) -> bool:
    """``row`` the same approval as ``was``, its time moved earlier and nothing else of Latet's changed, who approved it
    included (minors after 0.11, item 8: a relabelled approval waits again): it ends sooner, which applies at once."""
    return (
        was.revision == row.revision
        and was.body_revision == row.body_revision
        and (was.issuer, was.approver, was.approver_id) == (row.issuer, row.approver, row.approver_id)
        and was.granted is not None
        and row.granted is not None
        and (row.granted < was.granted)
    )


def loosened(old: Config, new: Config, repositories: Collection[tuple[str, ProjectSettings]] = ()) -> list[str]:
    """What ``new`` lowers compared with ``old``: one reason each, never a value. Empty: it only adds
    protection, or changes what does not protect (the port, the history). ``repositories``: the folders whose
    repository has a latet.yaml, with what it sets (``Projects.repositories``, a computer's), each weighed as it applies
    there too."""
    out = _masking(old, new) + _new_members(old, new)
    identity_before, identity_after = old.identity, new.identity
    if identity_before.enabled and not identity_after.enabled:
        out.append("this machine's identity: no longer masked")
    elif identity_after.enabled:
        names = (_user_names(identity_before), _user_names(identity_after))
        emails = (identity_before.emails, identity_after.emails)
        dropped = _removed(*names) + _removed(*emails) + _removed(_hosts(identity_before), _hosts(identity_after))
        dropped += bool(identity_before.full_name and identity_before.full_name != identity_after.full_name)
        if dropped:
            out.append(f"this machine's identity: {dropped} detail{'s' if dropped != 1 else ''} no longer masked")
    review_before, review_after = old.auto_mode_review, new.auto_mode_review
    for detail in ("user_name", "paths", "host_name", "git_author"):
        if getattr(review_before, detail) != "allow" and getattr(review_after, detail) == "allow":
            out.append(f"Claude Code's auto mode reviewer: may be sent your {detail.replace('_', ' ')}")
    if new.capture_dir is not None and new.capture_dir != old.capture_dir:
        out.append("raw exchanges: written to a folder (capture_dir)")
    out += _reviewer(old.llm_review, new.llm_review)
    out += _live(old.live_review, new.live_review)
    out += _destinations(old, new)
    out += _exceptions(old, new)
    top = set(out)
    out += _projects(old, new, top)
    if repositories:
        out += [reason for reason in _repositories(old, new, repositories, top) if reason not in out]
    return out


def changed_keys(old: Config, new: Config) -> list[str]:
    """The top-level keys whose values differ."""
    a, b = old.model_dump(mode="json"), new.model_dump(mode="json")
    return [key for key in b if a.get(key) != b.get(key)]
