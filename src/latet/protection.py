"""Which changes to the settings lower protection.

A change that adds protection applies at once, whoever makes it. One that lowers it waits for you:
asked at your own terminal, approved in the dashboard otherwise (an agent's command, a hand-edited
config.yaml). Each reason names what is lowered, never a value: they reach the agent that asked.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Collection, Hashable, Iterable, Mapping
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


def _narrowed(old: list[TermConfig], new: list[TermConfig]) -> int:
    """How many values of ``old``'s terms ``new`` still holds and matches in fewer forms: a way one of them was matched
    (in any case or its own, inside words or as whole words alone) that no entry of its value in ``new`` matches,
    ``case_sensitive`` or ``whole_word`` turned on. A value is matched as every entry of it says, together, and is
    weighed so, in time linear in the lists (a shared list of tens of thousands of terms is weighed at every
    re-application of every space); a value ``new`` no longer holds is weighed as removed."""
    ways: dict[str, set[tuple[bool, bool]]] = {}
    for term in new:
        ways.setdefault(term.value, set()).add((term.case_sensitive, term.whole_word))
    narrowed: set[str] = set()
    for term in old:
        now = ways.get(term.value)
        if now is not None and not any(
            (not case or term.case_sensitive) and (not whole or term.whole_word) for case, whole in now
        ):
            narrowed.add(term.value)
    return len(narrowed)


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


def _private_ips(config: Config) -> bool:
    return config.include_private_ips if config.include_private_ips is not None else config.profile == Profile.MAX


def _reviewer(old: LlmReview, new: LlmReview) -> list[str]:
    out: list[str] = []
    if old.enabled and not new.enabled:
        return ["the local reviewer: turned off"]
    if new.guidance != old.guidance:  # free text, which could tell it to report nothing: also while it is off
        out.append("the local reviewer: its guidance changed")
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


def _masking(old: Config, new: Config) -> list[str]:
    """What lowers the masking keys, the ones a project may set too."""
    out: list[str] = []
    if _PROFILES[new.profile] < _PROFILES[old.profile]:
        out.append(f"profile: from {old.profile.value} to {new.profile.value}")
    if n := len(_effective_pii(old) - _effective_pii(new)):
        out.append(f"personal data: {n} type{'s' if n != 1 else ''} no longer detected")
    if n := _removed(old.phone_regions, new.phone_regions):
        out.append(f"phone numbers: {n} countr{'ies' if n != 1 else 'y'} no longer recognised")
    if _private_ips(old) and not _private_ips(new):
        out.append("private IP addresses: no longer masked")
    if n := _added(old.disabled_secret_rules, new.disabled_secret_rules):
        out.append(f"secret rules: {n} turned off")
    recognizers_before = recognizers_in_force(old.recognizers)
    recognizers_after = recognizers_in_force(new.recognizers)
    policies_before, policies_after = _policies(old, recognizers_before), _policies(new, recognizers_after)
    # The types whose values the new settings leave in clear: a term or a field rule of one leaves what it finds so.
    through = {etype for etype, policy in policies_after.items() if policy == Policy.ALLOW}
    if n := _removed([t.value for t in old.terms], [t.value for t in new.terms]):
        out.append(f"terms: {_values(n)} no longer masked")
    if n := _narrowed(old.terms, new.terms):
        out.append(f"terms: {n} match{'es' if n == 1 else ''} fewer forms")
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
    elif strict_after and ((not old.data_tools and new.data_tools) or _removed(old.data_tools, new.data_tools)):
        out.append("strict data mode: covers fewer tools")
    # A file or a web address named is data with strict data mode on or off: one taken away covers less either way.
    if _removed(old.data_files, new.data_files):
        out.append("strict data mode: covers fewer files")
    if _removed(old.data_hosts, new.data_hosts):
        out.append("strict data mode: covers fewer web addresses")
    old_fields, new_fields = [f.model_dump() for f in old.fields], [f.model_dump() for f in new.fields]
    if _removed(old_fields, new_fields):
        out.append("field rules: changed or removed")
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
    and definition on both sides is left out first (a `policies` entry changed around it is said as that policy). In
    time linear in the lists."""
    same = {name for name, body in before.items() if after.get(name) == body}
    found_before = [_as_found(body, policies_before) for name, body in before.items() if name not in same]
    found_after = [_as_found(body, policies_after) for name, body in after.items() if name not in same]
    free: dict[Hashable, list[int]] = {}  # by definition, the policies in force after, the least strict first
    for core, rank in sorted((found for found in found_after if found is not None), key=lambda found: found[1]):
        free.setdefault(core, []).append(rank)
    changed = 0
    for core, rank in (found for found in found_before if found is not None):
        ranks = free.get(core, [])
        place = next((i for i, kept in enumerate(ranks) if kept >= rank), None)
        if place is None:
            changed += 1
        else:
            del ranks[place]  # matched: the least strict that is as strict, the stricter ones left for others
    in_clear = sum(1 for ranks in free.values() for rank in ranks if rank == _POLICIES[Policy.ALLOW])
    return changed, in_clear


def keeps_in_clear(recognizer: Any) -> bool:
    """A recognizer whose own policy lets what it finds through (``policies`` cannot see it)."""
    return isinstance(recognizer, dict) and str(recognizer.get("policy") or "").lower() == Policy.ALLOW.value


def recognizer_type(recognizer: Any) -> str | None:
    """The type a build gives ``recognizer`` when it keeps it in use (``_built``); None when it does not."""
    if not isinstance(recognizer, dict):
        return None
    return _built(json.dumps(recognizer, sort_keys=True, default=str))[1]


def _policies(config: Config, in_force: dict[str, dict[str, Any]]) -> dict[str, Policy]:
    """Each type's policy as a build lays them (``build_masker``): a recognizer's own is its type's default, an entry of
    ``policies`` wins; a type in neither is masked. ``in_force``: ``config``'s recognizers as a build keeps them
    (``_in_force``)."""
    out: dict[str, Policy] = {}
    for recognizer in in_force.values():
        own = str(recognizer.get("policy") or "").lower()
        if own in _POLICY_NAMES:
            etype = _built(json.dumps(recognizer, sort_keys=True, default=str))[1]
            if etype is not None:
                out[etype] = Policy(own)
    return out | dict(config.policies)


def policies_in_force(config: Config) -> dict[str, Policy]:
    """Each type's policy in ``config`` as a build lays them (``_policies``): a type in neither its policies nor its
    recognizers' own is masked."""
    return _policies(config, recognizers_in_force(config.recognizers))


def lets_through(config: Config) -> frozenset[str]:
    """The types whose values ``config`` leaves in clear (policy allow), as a build lays the policies: a term or a field
    rule of one leaves what it finds in clear."""
    return frozenset(etype for etype, policy in policies_in_force(config).items() if policy == Policy.ALLOW)


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
    rule new as a whole; and the values and types the top no longer holds (``gone``), whose entries in a folder's own
    settings come into force there (``effective_config`` adds a folder's term only where the top holds none of its value
    and type). The only ones of the top's lists that can leave values in clear in a folder whose own settings let their
    type through (``folders_let_through``) and not at the top, where what else a change of the lists lowers (a term
    removed, one matching fewer forms) is said already: weighed in each such folder alone (``in_clear``), without the
    tens of thousands of terms that did not change. Made in time linear in the lists, once a check."""

    def __init__(self, old: Config, new: Config) -> None:
        had = {(t.value, found_type(t), t.case_sensitive, t.whole_word) for t in old.terms}
        self.terms = [t for t in new.terms if (t.value, found_type(t), t.case_sensitive, t.whole_word) not in had]
        now = {_term_key(t) for t in new.terms}
        self.gone = {key for key in map(_term_key, old.terms) if key not in now}
        keys = {_term_key(t) for t in self.terms} | self.gone
        self.before: dict[tuple[str, str], list[TermConfig]] = {}  # the top's entries of those values and types before
        for term in old.terms:
            if (key := _term_key(term)) in keys:
                self.before.setdefault(key, []).append(term)
        self.fields: list[tuple[FieldRule, Hashable]] = []
        if old.fields != new.fields:
            rules = {_item(f.model_dump()) for f in old.fields}
            self.fields = [(f, item) for f in new.fields if (item := _item(f.model_dump())) not in rules]
        self.types = {found_type(t) for t in self.terms} | {etype for _, etype in self.gone}
        self.types |= {found_type(f) for f, _ in self.fields if f.mask}

    def in_clear(
        self, through: Collection[str], layers: list[ProjectSettings], strict: Collection[int] = ()
    ) -> list[str]:
        """What the changed terms and field rules leave in clear in a folder whose types let through are ``through``
        and whose own settings are ``layers`` (a repository's file, your settings for the folder), as ``_masking``
        weighs that folder's lists in full: a term whose value and type the folder had before (from the top, or else
        from every layer that holds them, as ``effective_config`` adds them) in no form it matches, the
        folder's own entries of a value and type the top no longer holds against the top's entries before, a field
        rule that masks and that none of its layers holds. ``strict``: the places in ``layers`` of the ones that may
        only add protection (a repository file), whose term or field rule of a type let through is set aside there,
        never in force (``projects._layer``)."""
        out: list[str] = []
        terms = [t for t in self.terms if found_type(t) in through]
        gone = [key for key in self.gone if key[1] in through]
        if terms or gone:
            own: dict[tuple[str, str], list[TermConfig]] = {}
            for i, layer in enumerate(layers):
                for term in layer.terms:
                    if i not in strict or found_type(term) not in through:
                        own.setdefault(_term_key(term), []).append(term)
            terms += [term for key in gone for term in own.get(key, [])]  # in force there now, the top's entry gone
            before = [e for key in {_term_key(t) for t in terms} for e in self.before.get(key, own.get(key, []))]
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
    its local reviewer and its Live Reviewer, which is all a project sets. One reason each, never a value."""
    return _masking(old, new) + _reviewer(old.llm_review, new.llm_review) + _live(old.live_review, new.live_review)


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


def weighs_repositories(old: Config, new: Config) -> bool:
    """Whether a change from ``old`` to ``new`` can lower anything in a repository's own latet.yaml beyond what it
    lowers at the top (``_repositories``): only where your settings for folders changed, or a setting that protects
    and that the file is laid over (``_context``, without ``_NOT_PROTECTING``). One that changes neither (a term added,
    the port, the history and its retention) is weighed the same whatever the repositories hold: none need be read for
    it, and one that cannot be read now holds it back from nothing."""
    if old.projects != new.projects:
        return True
    unweighed: dict[str, Any] = dict.fromkeys(_NOT_PROTECTING)
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
            out.append(f"destination {destination.name}: its address changed")
    return out


def destination_changes(old: Config, new: Config) -> list[dict[str, str | None]]:
    """The destinations moved or gone, with their address before and after (None: gone): for the
    dashboard, which shows where requests would go."""
    now = {d.name: d.url for d in new.upstreams}
    return [
        {"name": d.name, "before": d.url, "after": now.get(d.name)} for d in old.upstreams if now.get(d.name) != d.url
    ]


def loosened(old: Config, new: Config, repositories: Collection[tuple[str, ProjectSettings]] = ()) -> list[str]:
    """What ``new`` lowers compared with ``old``: one reason each, never a value. Empty: it only adds
    protection, or changes what does not protect (the port, the history). ``repositories``: the folders whose
    repository has a latet.yaml, with what it sets (``Projects.repositories``, a computer's), each weighed as it applies
    there too."""
    out = _masking(old, new)
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
    top = set(out)
    out += _projects(old, new, top)
    if repositories:
        out += [reason for reason in _repositories(old, new, repositories, top) if reason not in out]
    return out


def changed_keys(old: Config, new: Config) -> list[str]:
    """The top-level keys whose values differ."""
    a, b = old.model_dump(mode="json"), new.model_dump(mode="json")
    return [key for key in b if a.get(key) != b.get(key)]
