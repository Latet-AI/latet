"""Your own recognizers (``recognizers:`` in config.yaml): a pattern, check digits, keywords.

Each entry is checked on its own. A broken one is turned off and reported, and stays in the file as
it was written; the others keep working. A rule that would fire all over ordinary code (the
standard-library sample in ``engine.corpus``) is refused, since it would mask everything.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import re2
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from latet.engine.detectors import checksums
from latet.engine.detectors.base import DetectContext, quiet_options
from latet.engine.detectors.context import ContextWords
from latet.engine.detectors.terms import has_letter_or_digit
from latet.engine.types import EntityClass, Policy, Span

log = logging.getLogger(__name__)

NAME = r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"
TYPE = r"^[A-Z][A-Z0-9_]{1,63}$"
MAX_PATTERN = 2000
MAX_CORPUS_MATCHES = 50


class WeightedChecksum(BaseModel):
    """Each digit times its weight (weights repeat from the left), summed, modulo ``modulus``."""

    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["weighted"]
    weights: list[int] = Field(min_length=1, description="One weight per digit, repeated from the left.")
    modulus: int = Field(ge=2, description="The sum is taken modulo this.")
    remainder: int = Field(default=0, ge=0, description="What the sum modulo `modulus` must be.")


class ContextSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    words: list[str] = Field(default_factory=list, description="Words that must be near the value (any case).")
    acronyms: list[str] = Field(default_factory=list, description="Words that count in this exact case only.")
    window: int = Field(default=48, ge=1, le=500, description="How many characters before the value to look.")
    header: bool = Field(default=True, description="Also look in the column header of a CSV, TSV or Markdown table.")


class Examples(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match: list[str] = Field(default_factory=list, description="Invented texts the rule must find a value in.")
    no_match: list[str] = Field(default_factory=list, description="Invented texts the rule must leave alone.")


class KeepSurrogate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keep: int = Field(ge=0, le=32, description="Characters kept from the start of the value (a prefix).")


class RecognizerSpec(BaseModel):
    """One recognizer, as written under ``recognizers:``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(
        json_schema_extra={"pattern": NAME}, description="Lower case letters, digits and hyphens; unique."
    )
    type: str | None = Field(
        default=None,
        json_schema_extra={"pattern": TYPE},
        description="The type shown for it, in capitals; the name in capitals when not set.",
    )
    description: str = Field(default="", description="What it finds, in words.")
    pattern: str = Field(
        min_length=1,
        max_length=MAX_PATTERN,
        description="An RE2 regular expression. A group named `value` picks the part to mask.",
    )
    checksum: str | WeightedChecksum | None = Field(
        default=None,
        description=f"Check digits the value must pass: {', '.join(checksums.CHECKSUMS)}, or a weighted sum.",
    )
    context: ContextSpec | None = Field(default=None, description="Keywords that must be near the value.")
    examples: Examples = Field(default_factory=Examples, description="Invented examples, checked on every load.")
    class_: Literal["custom", "secret"] = Field(
        default="custom",
        alias="class",
        description="`custom`, or `secret` to treat it like a password (never let through, blocks a request).",
    )
    policy: Policy | None = Field(default=None, description="`mask`, `redact` or `allow`; `policies` wins.")
    surrogate: Literal["shape"] | KeepSurrogate = Field(
        default="shape", description="`shape` (same length and kinds of characters), or `{keep: N}`."
    )
    min_length: int = Field(default=1, ge=1, description="Shorter values are skipped.")
    enabled: bool = Field(default=True, description="`false` keeps the rule in the file, turned off.")

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not re2.fullmatch(NAME, value):
            raise ValueError("lower case letters, digits and hyphens, such as employee-id")
        return value

    @field_validator("type")
    @classmethod
    def _type(cls, value: str | None) -> str | None:
        if value is not None and not re2.fullmatch(TYPE, value):
            raise ValueError("capital letters, digits and underscores, such as EMPLOYEE_ID")
        return value

    @field_validator("checksum")
    @classmethod
    def _known_checksum(cls, value: str | WeightedChecksum | None) -> str | WeightedChecksum | None:
        if isinstance(value, str) and value not in checksums.CHECKSUMS:
            raise ValueError(f"unknown checksum; use one of {', '.join(checksums.CHECKSUMS)}, or a weighted sum")
        return value

    @property
    def etype(self) -> str:
        return self.type or self.name.upper().replace("-", "_")


@dataclass
class Status:
    """How a recognizer stands after loading: active, off (``enabled: false``) or in error."""

    name: str
    state: Literal["active", "off", "error"]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "errors": self.errors, "warnings": self.warnings}


class Recognizer:
    def __init__(self, spec: RecognizerSpec, regex: re2._Regexp) -> None:
        self.spec = spec
        self.regex = regex
        self.group = regex.groupindex.get("value", 0)
        if isinstance(spec.checksum, WeightedChecksum):
            w = spec.checksum
            self.checksum: Callable[[str], bool] | None = lambda d: checksums.weighted(
                d, w.weights, w.modulus, w.remainder
            )
            self.reads: Literal["digits", "alnum"] = "digits"
        elif spec.checksum is not None:
            known = checksums.CHECKSUMS[spec.checksum]
            self.checksum, self.reads = known.validate, known.reads
        else:
            self.checksum, self.reads = None, "alnum"
        context = spec.context
        self.keywords = ContextWords(context.words, context.acronyms) if context else None
        if self.keywords is not None and not self.keywords:
            self.keywords = None  # a context block without words asks for nothing

    @property
    def etype(self) -> str:
        return self.spec.etype

    @property
    def eclass(self) -> EntityClass:
        return EntityClass.SECRET if self.spec.class_ == "secret" else EntityClass.CUSTOM

    @property
    def rule(self) -> str:
        return f"recognizer:{self.spec.name}"

    def find(self, text: str) -> Iterator[Span]:
        for start, end, reason in self._candidates(text):
            if reason is None:
                yield Span(start, end, self.etype, self.eclass, self.rule)

    def _candidates(self, text: str) -> Iterator[tuple[int, int, str | None]]:
        """Every match of the pattern, and why it is not a value (None: it is)."""
        keywords = self.keywords
        if keywords is not None and not keywords.anywhere(text):
            for m in self.regex.finditer(text):
                if m.end(self.group) > m.start(self.group) >= 0:
                    yield m.start(self.group), m.end(self.group), self._no_keyword()
            return
        for m in self.regex.finditer(text):
            start, end = m.span(self.group)
            if start < 0 or end <= start:
                continue
            value = text[start:end]
            if len(value) < self.spec.min_length:
                yield start, end, f"shorter than min_length ({self.spec.min_length})"
                continue
            if not has_letter_or_digit(value):  # a fake keeps the signs of a value: "---" would be its own
                yield start, end, "it has no letter or digit: signs alone are never masked"
                continue
            if self.checksum is not None and not self.checksum(checksums.compact(value, self.reads)):
                yield start, end, f"the {self._checksum_name()} check fails"
                continue
            context = self.spec.context
            if keywords is not None and context is not None:
                window = context.window
                if not keywords.near(text, start, end, before=window, after=window // 2, header=context.header):
                    yield start, end, self._no_keyword()
                    continue
            yield start, end, None

    def generate(self, original: str, rng: Any) -> str:
        """A fake of the same shape (``keep`` leading characters as they are) that passes the checksum."""
        from latet.engine.surrogates.generators import shape

        keep = self.spec.surrogate.keep if isinstance(self.spec.surrogate, KeepSurrogate) else 0
        for _ in range(5000):
            candidate = shape(original, rng, keep=keep)
            if candidate == original:
                continue
            if self.checksum is None or self.checksum(checksums.compact(candidate, self.reads)):
                return candidate
        raise ValueError("no fake passes the checksum")

    def _checksum_name(self) -> str:
        return "weighted" if isinstance(self.spec.checksum, WeightedChecksum) else str(self.spec.checksum)

    def _no_keyword(self) -> str:
        context = self.spec.context
        assert context is not None
        words = ", ".join([*context.words, *context.acronyms])
        return f"no keyword ({words}) within {context.window} characters"

    def explain(self, text: str) -> tuple[list[str], str | None]:
        """For ``rules test``: the values found in ``text``, and why the pattern's other matches are not."""
        found, reasons = [], []
        for start, end, reason in self._candidates(text):
            if reason is None:
                found.append(text[start:end])
            else:
                reasons.append(f"matched {text[start:end]!r}, but {reason}")
        if not found and not reasons:
            return [], "the pattern does not match"
        return found, "; ".join(reasons) or None


class RecognizerDetector:
    """Your own recognizers from config.yaml: a pattern, check digits and keywords for each."""

    id = "recognizers"
    mandatory = True

    def __init__(self, recognizers: Iterable[Recognizer]) -> None:
        self.recognizers = list(recognizers)

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        for recognizer in self.recognizers:
            yield from recognizer.find(text)


def _messages(exc: ValidationError) -> list[str]:
    out = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        where = ".".join(str(p) for p in error["loc"] if not str(p).startswith(("function-", "union[")))
        message = error["msg"].removeprefix("Value error, ")
        if error["type"] == "extra_forbidden":
            message = "not a key of a recognizer"
        elif error["type"] == "missing":
            message = "required"
        elif error["type"] == "model_type" and not where:
            message = "each recognizer is a mapping (name: ..., pattern: ...)"
        out.append(f"{where}: {message}" if where else message)
    return list(dict.fromkeys(out))


def compile_one(
    raw: Any, reserved: Iterable[str] = (), corpus: str | None = None, include_off: bool = False
) -> tuple[Recognizer | None, Status]:
    """Check one entry: (the recognizer, or None when it is broken, or off unless ``include_off``;
    its status)."""
    given = raw.get("name") if isinstance(raw, dict) else None
    name = given if isinstance(given, str) and given else "(no name)"
    try:
        spec = RecognizerSpec.model_validate(raw)
    except ValidationError as exc:
        return None, Status(name, "error", _messages(exc))
    if spec.etype in set(reserved):
        return None, Status(spec.name, "error", [f"type: {spec.etype} is a type Latet already has; choose another"])
    try:
        regex = re2.compile(spec.pattern, quiet_options())  # the error is reported, not printed
    except re2.error as exc:
        detail = exc.args[0].decode() if exc.args and isinstance(exc.args[0], bytes) else str(exc)
        return None, Status(spec.name, "error", [f"pattern: not a valid RE2 expression ({detail})"])
    match = regex.search("")
    if match is not None:
        return None, Status(spec.name, "error", ["pattern: it matches an empty text, so it would match everywhere"])
    recognizer = Recognizer(spec, regex)
    status = Status(spec.name, "active" if spec.enabled else "off")
    if corpus is not None:
        hits = sum(1 for _ in recognizer.find(corpus))
        if hits > MAX_CORPUS_MATCHES:
            message = f"too broad: {hits} matches in ordinary code (the standard-library sample); at most "
            status.errors.append(message + f"{MAX_CORPUS_MATCHES}. Add keywords, a checksum or a stricter pattern")
            status.state = "error"
            return None, status
    for text in spec.examples.match:
        found, reason = recognizer.explain(text)
        if not found:
            status.warnings.append(f"examples.match {text!r}: nothing found ({reason})")
            continue
        if problem := _fake_problem(recognizer, text, found[0]):
            status.warnings.append(problem)
    for text in spec.examples.no_match:
        found, _ = recognizer.explain(text)
        if found:
            status.warnings.append(f"examples.no_match {text!r}: found {', '.join(map(repr, found))}")
    return (recognizer if spec.enabled or include_off else None), status


def _fake_problem(recognizer: Recognizer, text: str, value: str) -> str | None:
    """A fake in place of the example's value should be found again (the model reads it as one)."""
    from latet.engine.surrogates.generators import Drbg

    try:
        fake = recognizer.generate(value, Drbg(b"\x00" * 32, b"rules-test"))
    except ValueError:
        return f"examples.match {text!r}: no fake of {value!r} passes the checksum"
    found, _ = recognizer.explain(text.replace(value, fake, 1))
    if fake not in found:
        return f"examples.match {text!r}: its fake ({fake!r}) would not match the pattern"
    return None


def compile_all(
    raw: Sequence[Any], reserved: Iterable[str] = (), corpus: str | None = None
) -> tuple[list[Recognizer], list[Status]]:
    """Every entry of ``recognizers:``. Names and types must be unique; the first one wins."""
    reserved = set(reserved)
    recognizers: list[Recognizer] = []
    statuses: list[Status] = []
    names: set[str] = set()
    types: set[str] = set()
    for place, entry in enumerate(raw, 1):
        recognizer, status = compile_one(entry, reserved, corpus)
        if status.name in names:
            status = Status(status.name, "error", [f"name: another recognizer is already called {status.name}"])
            recognizer = None
        elif recognizer is not None and recognizer.etype in types:
            status = Status(status.name, "error", [f"type: another recognizer already uses {recognizer.etype}"])
            recognizer = None
        names.add(status.name)
        if recognizer is not None:
            types.add(recognizer.etype)
            recognizers.append(recognizer)
        statuses.append(status)
        # By its name when it is one; anything else a person wrote there (an address, a client's name) by its place.
        called = status.name if re2.fullmatch(NAME, status.name) else f"number {place}"
        for problem in status.errors:
            log.warning("recognizer %s turned off: a problem with its %s", called, _about(problem))
    return recognizers, statuses


# The keys of a recognizer, which its problems start with (``pattern: ...``).
_KEYS = frozenset(field.alias or name for name, field in RecognizerSpec.model_fields.items())


def _about(problem: str) -> str:
    """The key of a recognizer that a problem is about (``pattern``, ``type``, ``name``...), as the log says it: never
    the problem's words, which can quote the pattern (re2's do), and so the terms it is made of."""
    key = problem.partition(":")[0].partition(".")[0]
    if key == "too broad":
        return "pattern"
    return key if key in _KEYS else "definition"
