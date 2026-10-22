"""Secret detection with the gitleaks default ruleset (vendored, MIT), evaluated with RE2.

The semantics follow gitleaks v8 ``detect.go``: a keyword prefilter per rule, the secret is the
``secretGroup`` (or the first non-empty capture group), entropy must exceed the rule threshold,
and global/rule allowlists apply to the secret, the match or the line.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import cache
from importlib import resources

import re2

from latet.engine.detectors.base import DetectContext, line_bounds, quiet_options, shannon_entropy
from latet.engine.types import EntityClass, Span

RULESET_VERSION = "v8.30.1"
_ALLOW_SIGNATURE = "gitleaks:allow"


def _compiled(pattern: str) -> re2._Regexp:
    """One of the ruleset's patterns, compiled quietly as every pattern that is not written in the code."""
    return re2.compile(pattern, quiet_options())


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

    def __init__(self, disabled_rules: Iterable[str] = ()) -> None:
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

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        if not text:
            return
        present = set(self._keywords.Match(text.lower()) or ())
        path = ctx.path
        if path is not None and any(a.allows_path(path) for a in self._global):
            return
        index = self._keyword_index
        for rule in self._rules:
            if rule.keywords and not any(index[k] in present for k in rule.keywords):
                continue
            if rule.path is not None and (path is None or not rule.path.search(path)):
                continue
            if path is not None and any(a.allows_path(path) for a in rule.allowlists):
                continue
            for m in rule.regex.finditer(text):
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
