"""Secret detection with the gitleaks default ruleset (vendored, MIT), evaluated with RE2.

The semantics follow gitleaks v8 ``detect.go``: a keyword prefilter per rule, the secret is the
``secretGroup`` (or the first non-empty capture group), entropy must exceed the rule threshold,
and global/rule allowlists apply to the secret, the match or the line.
"""

from __future__ import annotations

import tomllib
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import cache
from importlib import resources

import re2

from latet.engine.detectors.base import DetectContext, line_bounds, line_pattern, quiet_options, shannon_entropy
from latet.engine.types import EntityClass, Span

RULESET_VERSION = "v8.30.1"
_ALLOW_SIGNATURE = "gitleaks:allow"


def _compiled(pattern: str) -> re2._Regexp:
    """One of the ruleset's patterns, compiled quietly as every pattern that is not written in the code."""
    return re2.compile(pattern, quiet_options())


# Each rule's pattern as it reads the lines of a text joined of lines (``line_regex``), by the pattern: made at its
# first use in the process (most rules are never read so), and ``line_pattern``'s answer for each, None where a pattern
# cannot be read by line.
_LINES: dict[str, re2._Regexp] = {}
_LINE_PATTERNS: dict[str, str | None] = {}


def _line_pattern(pattern: str) -> str | None:
    if pattern not in _LINE_PATTERNS:
        _LINE_PATTERNS[pattern] = line_pattern(pattern)
    return _LINE_PATTERNS[pattern]


def line_regex(rule: Rule) -> re2._Regexp:
    """``rule``'s pattern as it reads each line of a text joined of lines (``DetectContext.joined``), as it reads each
    line alone. As written, a match runs through a blank line (``[\\s'"]{0,3}`` after a secret's name), and one the
    rule then refuses takes the place of the next line's own (``Masker.joined``). Made once in the process."""
    pattern = rule.regex.pattern
    found = _LINES.get(pattern)
    if found is None:
        line = _line_pattern(pattern)
        if line is None:  # ``GitleaksDetector.joinable`` says so first: no text is read so with such a rule
            raise ValueError(f"the rule {rule.id} cannot be read by line")
        found = _LINES[pattern] = re2.compile(line, quiet_options(never_nl=True))
    return found


@dataclass(frozen=True, slots=True)
class Allowlist:
    regexes: tuple[re2._Regexp, ...]
    stopwords: tuple[str, ...]
    target: str  # "secret" | "match" | "line"
    condition_and: bool
    paths: tuple[re2._Regexp, ...]

    def allows(self, secret: str, match: str, line: str, path: str | None) -> bool:
        target = {"match": match, "line": line}.get(self.target, secret)
        regex_ok = any(r.search(target) for r in self.regexes)
        lowered = secret.lower()
        stop_ok = any(w in lowered for w in self.stopwords)
        if self.condition_and:
            checks: list[bool] = []
            if self.paths:
                checks.append(path is not None and any(p.search(path) for p in self.paths))
            if self.regexes:
                checks.append(regex_ok)
            if self.stopwords:
                checks.append(stop_ok)
            return bool(checks) and all(checks)
        return regex_ok or stop_ok

    def allows_path(self, path: str | None) -> bool:
        return not self.condition_and and path is not None and any(p.search(path) for p in self.paths)


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    regex: re2._Regexp
    keywords: tuple[str, ...]
    entropy: float
    secret_group: int
    path: re2._Regexp | None
    allowlists: tuple[Allowlist, ...]


def _allowlist(raw: dict) -> Allowlist:
    return Allowlist(
        regexes=tuple(_compiled(r) for r in raw.get("regexes", [])),
        stopwords=tuple(w.lower() for w in raw.get("stopwords", [])),
        target=raw.get("regexTarget", "secret"),
        condition_and=raw.get("condition", "OR").upper() == "AND",
        paths=tuple(_compiled(p) for p in raw.get("paths", [])),
    )


@cache
def load_ruleset() -> tuple[tuple[Rule, ...], tuple[Allowlist, ...]]:
    data = tomllib.loads(resources.files(__package__).joinpath("gitleaks.toml").read_text("utf-8"))
    rules = []
    for raw in data["rules"]:
        if "regex" not in raw:
            continue  # path-only rules (e.g. pkcs12-file) describe files, not text
        rules.append(
            Rule(
                id=raw["id"],
                regex=_compiled(raw["regex"]),
                keywords=tuple(k.lower() for k in raw.get("keywords", [])),
                entropy=float(raw.get("entropy", 0.0)),
                secret_group=int(raw.get("secretGroup", 0)),
                path=_compiled(raw["path"]) if raw.get("path") else None,
                allowlists=tuple(_allowlist(a) for a in raw.get("allowlists", [])),
            )
        )
    global_lists = data.get("allowlists") or ([data["allowlist"]] if "allowlist" in data else [])
    return tuple(rules), tuple(_allowlist(a) for a in global_lists)


def _secret_span(rule: Rule, m: re2._Match) -> tuple[int, int] | None:
    groups = m.re.groups
    if groups == 0:
        return m.span()
    if rule.secret_group:
        if rule.secret_group > groups:
            return None
        span = m.span(rule.secret_group)
        return span if span[0] >= 0 and span[1] > span[0] else None
    for g in range(1, groups + 1):
        start, end = m.span(g)
        if start >= 0 and end > start:
            return start, end
    return m.span()


class GitleaksDetector:
    """API keys and tokens of known services, with the gitleaks ruleset: patterns, keywords, entropy
    thresholds and allowlists as gitleaks applies them."""

    id = "gitleaks"
    mandatory = True

    def __init__(self, disabled_rules: Iterable[str] = (), *, by_keyword: bool = True) -> None:
        rules, self._global = load_ruleset()
        disabled = set(disabled_rules)
        self._rules = tuple(r for r in rules if r.id not in disabled)
        # One pass over the text finds every keyword present (gitleaks' prefilter), instead of one
        # substring search per rule.
        keywords = sorted({k for r in self._rules for k in r.keywords})
        self._keyword_index = {k: i for i, k in enumerate(keywords)}
        self._keywords = re2.Set.SearchSet(quiet_options())
        for k in keywords:
            self._keywords.Add(re2.escape(k))
        self._keywords.Compile()
        # With ``by_keyword`` the rules to read for a text are looked up by the keywords present (the rules each
        # keyword names, made once) and those without keywords, which every text is read with; without it every rule
        # is asked whether one of its keywords is present, as 0.10.0 did. The same rules are read either way.
        self._by_keyword = by_keyword
        named: dict[int, list[int]] = defaultdict(list)
        for number, rule in enumerate(self._rules):
            for k in rule.keywords:
                named[self._keyword_index[k]].append(number)
        self._named = {k: tuple(numbers) for k, numbers in named.items()}
        self._unnamed = tuple(number for number, rule in enumerate(self._rules) if not rule.keywords)
        self._joinable: bool | None = None

    @property
    def joinable(self) -> bool:
        """It reads a text joined of lines (``DetectContext.joined``) as it reads each line alone: each of its rules
        has a pattern that reads by line (``line_regex``). Worked out once, at the first call read joined."""
        if self._joinable is None:
            self._joinable = all(_line_pattern(rule.regex.pattern) is not None for rule in self._rules)
        return self._joinable

    def _to_read(self, present: Iterable[int]) -> Iterable[Rule]:
        """The rules to read a text with, in the ruleset's order, when the keywords numbered ``present`` are in it."""
        if not self._by_keyword:
            found = set(present)
            index = self._keyword_index
            return [r for r in self._rules if not r.keywords or any(index[k] in found for k in r.keywords)]
        chosen = set(self._unnamed)
        for k in present:
            chosen.update(self._named.get(k, ()))
        return [self._rules[number] for number in sorted(chosen)]

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        if not text:
            return
        present = self._keywords.Match(text.lower()) or ()
        path = ctx.path
        if path is not None and any(a.allows_path(path) for a in self._global):
            return
        for rule in self._to_read(present):
            if rule.path is not None and (path is None or not rule.path.search(path)):
                continue
            if path is not None and any(a.allows_path(path) for a in rule.allowlists):
                continue
            for m in (line_regex(rule) if ctx.joined else rule.regex).finditer(text):
                span = _secret_span(rule, m)
                if span is None:
                    continue
                start, end = span
                secret = text[start:end]
                if rule.entropy and shannon_entropy(secret) <= rule.entropy:
                    continue
                line_start, line_end = line_bounds(text, m.start(), m.end())
                line = text[line_start:line_end]
                if _ALLOW_SIGNATURE in line:
                    continue
                match = text[m.start() : m.end()]
                if any(a.allows(secret, match, line, path) for a in self._global):
                    continue
                if any(a.allows(secret, match, line, path) for a in rule.allowlists):
                    continue
                yield Span(start, end, _etype(rule.id), EntityClass.SECRET, f"gitleaks:{rule.id}")


def _etype(rule_id: str) -> str:
    """Entity type from a rule id: ``github-pat`` → ``GITHUB_PAT``."""
    return rule_id.upper().replace("-", "_")


def rule_types() -> set[str]:
    """Every type the ruleset can report."""
    rules, _ = load_ruleset()
    return {_etype(rule.id) for rule in rules}
