"""User configuration (``config.yaml`` in the user config directory) and protection profiles."""

from __future__ import annotations

import contextlib
import functools
import ipaddress
import logging
import os
import re
import socket
import subprocess
import sys
from collections.abc import Collection, Iterable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import platformdirs
import re2
import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)
from yaml.composer import ComposerError
from yaml.constructor import ConstructorError
from yaml.reader import ReaderError

from latet.engine.detectors.base import quiet_options
from latet.engine.detectors.terms import TERM_LONGEST, VALUE_LONGEST
from latet.engine.types import Policy
from latet.logline import described
from latet.schema import SCHEMA_URL

log = logging.getLogger(__name__)

APP = "latet"


class Profile(StrEnum):
    BALANCED = "balanced"
    MAX = "max"
    SECRETS = "secrets"


# How much each protects, the least first: a repository file, an import and the approval of a
# change compare them.
PROFILE_STRICTNESS = {Profile.SECRETS: 0, Profile.BALANCED: 1, Profile.MAX: 2}
POLICY_STRICTNESS = {Policy.ALLOW: 0, Policy.MASK: 1, Policy.REDACT: 2}


class _NoAliases(yaml.SafeLoader):
    """``yaml.safe_load`` without aliases: a few hundred bytes of nested aliases expand to gigabytes,
    and no settings file needs them."""

    def compose_node(self, parent: Any, index: Any) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise ComposerError(None, None, "aliases (*name) are not allowed", event.start_mark)
        return super().compose_node(parent, index)

    def construct_object(self, node: Any, deep: bool = False) -> Any:
        """PyYAML builds a date, a time or a number with Python's own types, outside its errors: one they cannot hold
        (2024-13-45, an offset of a whole day, 5,000 digits) is a YAML error at its place, in words of its own."""
        try:
            return super().construct_object(node, deep)
        except (ValueError, OverflowError):
            raise ConstructorError(None, None, "a date, a time or a number out of range", node.start_mark) from None


def load_yaml(text: str) -> Any:
    """A settings file or value, as data: safe types only, and no aliases."""
    return yaml.load(text, Loader=_NoAliases)  # noqa: S506 - a SafeLoader


# The words YAML 1.1 reads as true or false, which settings take as words (`ocr: off`, `history: off`): a setting
# that is true or false reads them as such all the same (pydantic), so given alone they are kept as they were typed.
_WORDS = frozenset({"on", "off", "yes", "no", "y", "n"})


def load_value(text: str) -> Any:
    """A setting's value given on the command line, in YAML (``load_yaml``), but for a bare on, off, yes or no, kept
    as the word: `latet config set ocr off` sets off, never false."""
    if text.strip().lower() in _WORDS:
        return text.strip()
    return load_yaml(text)


def _yaml_error_at(exc: yaml.YAMLError, text: str) -> str:
    """`` (line 3, column 7)``: where PyYAML found the mistake in ``text``, and never the line it quotes; nothing when
    it does not say."""
    if isinstance(exc, yaml.MarkedYAMLError):
        mark = exc.problem_mark or exc.context_mark
        return f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
    if isinstance(exc, ReaderError):  # a character it refuses: it says how far in, not on which line
        before = text[: exc.position]
        line, start = before.count("\n") + 1, before.rfind("\n")
        return f" (line {line}, column {len(before) - start})"
    return ""


def write_atomic(path: Path, text: str, private: bool = False) -> None:
    """Write ``path`` whole or not at all: a reader, a crash or a full disk never leaves half of it.
    ``private``: readable by you alone, from the moment the file exists; the write fails where the system refuses that
    (``chmod`` on a mount that shows files as another owner's). On Windows a file's access follows its folder's, and
    ``private`` only makes the file new (CREATE_NEW: no file someone else made at its name, with their access, is
    written into)."""
    _write_atomic(path, text, private, tolerate_chmod=False)


def write_private_where_allowed(path: Path, text: str) -> None:
    """``write_atomic(path, text, private=True)`` for a file that is worth more kept than kept private: where the
    system refuses ``chmod`` it is written all the same, with the permissions the system gives it, and the log says so,
    once for each file and naming it."""
    _write_atomic(path, text, True, tolerate_chmod=True)


# The files said to be written without being made private: said once each, not at every start and change.
_NOT_MADE_PRIVATE: set[Path] = set()


def _say_not_made_private(path: Path, exc: OSError) -> None:
    if path in _NOT_MADE_PRIVATE:
        return
    _NOT_MADE_PRIVATE.add(path)
    log.warning(
        "%s is written with the permissions the file system gives it: chmod is refused there (%s)", path, described(exc)
    )


def _write_atomic(path: Path, text: str, private: bool, tolerate_chmod: bool) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        if private:
            # Created for its owner alone, not made so after: at the umask's mode a key would be readable by others
            # until the chmod below. Exclusive (on Windows too, CREATE_NEW), so that nothing already at this name (a
            # link, a file someone else made with their access) is written through; what a run that died left there is
            # taken away first.
            tmp.unlink(missing_ok=True)
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
                out.write(text.encode("utf-8"))
        else:
            tmp.write_bytes(text.encode("utf-8"))
        if private:
            try:
                tmp.chmod(0o600)
            except OSError as exc:
                if not tolerate_chmod:
                    raise
                _say_not_made_private(path, exc)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# Every model of the settings: a refusal printed anywhere (a command's error, a traceback) shows no input, which may be
# a customer's name, a term or your identity. Each check of this module says what it refuses, never what it was given.
_QUIET_ERRORS = ConfigDict(hide_input_in_errors=True)


class TermConfig(BaseModel):
    model_config = _QUIET_ERRORS

    value: str = Field(
        description="The text to mask wherever it appears: a client, a project code name, a domain. It needs a "
        "letter or a digit."
    )
    type: str = Field(default="CUSTOM", description="The label shown for it, such as CLIENT or PROJECT.")
    case_sensitive: bool = Field(default=False, description="Match the exact case only.")
    whole_word: bool = Field(default=True, description="Match whole words only (not inside longer words).")


TERM_WITHOUT_LETTERS = (
    "a term needs a letter or a digit: a fake keeps the signs of a value, so signs alone, such as -- or ###, "
    "cannot be masked"
)
# A longer term is matched on its own, outside the parts of its list (``latet.engine.detectors.terms``), and one of a
# million letters with more than one case (``k``: k, K and the Kelvin sign) compiles within no budget: the rules could
# not be built at all, so the daemon did not start and a reload failed (S-03 on a computer).
TERM_TOO_LONG = (
    f"a term holds at most {TERM_LONGEST} characters: a longer one is matched on its own, and one of about a million "
    "letters cannot be matched at all"
)


def _unmaskable(value: str) -> str | None:
    """Why Latet cannot mask a term, or None: no letter or digit, or more characters than a term holds."""
    from latet.engine.detectors.terms import has_letter_or_digit

    if not has_letter_or_digit(value):
        return TERM_WITHOUT_LETTERS
    return TERM_TOO_LONG if len(value) > TERM_LONGEST else None


def term_errors(
    values: Iterable[str], where: Sequence[str | int] = ("terms",), kept: Collection[str] = ()
) -> list[dict[str, Any]]:
    """The terms Latet cannot mask, as validation errors: those with no letter or digit, and those longer than a term
    holds (``TERM_LONGEST``), each by its place and never quoted. A file written by hand may hold one: one of signs
    alone is then never looked for, one too long is left out where the file is read (``left_out``); whatever adds terms
    refuses it, and says why. Those in ``kept`` were there already: a change that takes one out, or changes something
    else, does not fail on them."""
    kept = frozenset(kept)
    return [
        {"loc": [*where, i, "value"], "msg": why, "type": "value_error"}
        for i, value in enumerate(values)
        if value not in kept and (why := _unmaskable(value)) is not None
    ]


class IdentityConfig(BaseModel):
    model_config = _QUIET_ERRORS

    enabled: bool = Field(
        default=True,
        description="Mask this machine's identity (detected each time Latet starts, when the fields below are empty).",
    )
    username: str | None = Field(default=None, description="Older single-name form of `usernames`.")
    usernames: list[str] = Field(default_factory=list, description="OS user names, masked inside home paths.")
    hostname: str | None = Field(default=None, description="This machine's host name.")
    hostnames: list[str] = Field(
        default_factory=list,
        description="Other host names of yours, masked as `hostname` is (on a Latet server, each of your computers').",
    )
    full_name: str | None = Field(default=None, description="Your name, as in your git author.")
    emails: list[str] = Field(default_factory=list, description="Your email addresses, as in your git author.")


# Countries with national ID and tax numbers Latet knows (engine.detectors.ids.REGIONS).
ID_REGIONS = ("BE", "CA", "CH", "DE", "ES", "FI", "FR", "GB", "IT", "LV", "NL", "PL", "PT", "SE", "US")
DEFAULT_ID_REGIONS = ("IT", "US", "GB")


# What Claude Code's auto mode reviewer is sent of this machine's identity, in the order it is shown.
REVIEW_DETAILS = ("user_name", "paths", "host_name", "git_author")
ReviewChoice = Literal["block", "allow"]


class AutoModeReview(BaseModel):
    """Which identity details may reach Claude Code's auto mode reviewer.

    `block` and `allow-identity` stand for every detail blocked or allowed, and are written back that
    way, so files written before the per-detail form keep their meaning and their look.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    user_name: Literal["block", "allow", "surrogate"] = Field(
        default="block",
        description="Your OS user name, which the reviewer is told as its *User identity*. `surrogate` gives "
        "Claude Code your user name's surrogate instead (in `USERNAME` and `USER`), so the reviewer sees the "
        "same fake name as the model.",
    )
    paths: ReviewChoice = Field(
        default="block", description="Your user folder's name in the paths of your home and working directories."
    )
    host_name: ReviewChoice = Field(default="block", description="This machine's host name.")
    git_author: ReviewChoice = Field(default="block", description="Your name and email, as in your git author.")

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, data: Any) -> Any:
        if data == "block":
            return {}
        if data == "allow-identity":
            return dict.fromkeys(REVIEW_DETAILS, "allow")
        if isinstance(data, str):
            raise ValueError(f"`block`, `allow-identity`, or a choice for each of {', '.join(REVIEW_DETAILS)}")
        return data

    @model_serializer(mode="wrap")
    def _compact(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        choices = {data.get(d) for d in REVIEW_DETAILS}
        if choices == {"block"}:
            return "block"
        if choices == {"allow"}:
            return "allow-identity"
        return data

    def allowed(self) -> frozenset[str]:
        """The details let through to the reviewer (a surrogate user name is not one: the real one blocks)."""
        return frozenset(d for d in REVIEW_DETAILS if getattr(self, d) == "allow")


# What the local reviewer reports, and the categories it may mask by itself by default.
REVIEW_CATEGORIES = (
    "person", "address", "phone", "email", "id", "health", "sensitive", "financial", "secret", "org", "confidential",
)  # fmt: skip
ReviewCategory = Literal[
    "person", "address", "phone", "email", "id", "health", "sensitive", "financial", "secret", "org", "confidential"
]
ReviewKind = Literal["user_text", "attachment", "system", "tool_result", "tool_input", "tool_description", "image_text"]
# Everything that brings text the model did not write: your prompts and files, the instructions it is
# given, what its tools fetch, and the text in images.
REVIEW_EVERYTHING: tuple[ReviewKind, ...] = ("user_text", "attachment", "system", "tool_result", "image_text")
ImagesSetting = Literal["allow", "mask", "block"]
ReviewMode = Literal["off", "background", "inline"]
REVIEW_MODES: tuple[ReviewMode, ...] = ("off", "background", "inline")  # in order of protection
MAX_GUIDANCE = 2000  # characters of your guidance for the reviewer: the global one, and a project's
# Where the reviewer runs without asking: this machine (in the container, the host it runs on too).
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
# Names of a local network, where a server of yours may answer over plain http.
_LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")


def _local_network(host: str) -> bool:
    """A private address (192.168.x, 10.x, fd00::...), or a name of a local network."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host or host.endswith(_LOCAL_SUFFIXES)
    return ip.is_private or ip.is_link_local


def clean_guidance(text: str | None) -> str | None:
    """Your guidance as the reviewer reads it: plain line ends, nothing invisible; empty is none."""
    if text is None:
        return None
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    if any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in text):
        raise ValueError("text only: no control characters")
    if len(text) > MAX_GUIDANCE:
        raise ValueError(f"at most {MAX_GUIDANCE} characters, this is {len(text)}")
    return text


def join_guidance(*parts: str | None) -> str | None:
    """Your guidance, then a project's, with a blank line between."""
    return "\n\n".join(p for p in parts if p) or None


class LlmReview(BaseModel):
    """The local reviewer: a language model on your own server reads what the detectors left in clear."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = Field(default=False, description="Ask the local reviewer.")
    url: str = Field(
        default="http://127.0.0.1:8080/v1",
        description="Its OpenAI-compatible server (llama.cpp, Ollama, LM Studio): on this machine, or on another "
        "machine of yours with `remote: true`.",
    )
    remote: bool = Field(
        default=False,
        description="The server is yours, on another machine: Latet sends it the original texts. Outside your "
        "local network its address must be https.",
    )
    model: str | None = Field(default=None, description="The model's id on the server (not set: the first it lists).")
    api_key_env: str = Field(
        default="LATET_LLM_API_KEY",
        description="The environment variable holding the server's API key, if any. Without it, the key saved from "
        "the dashboard or with `latet llm key` is used (kept in the operating system's credential store).",
    )
    api_key_file: str | None = Field(
        default=None, description="Or a file holding the key (the key itself never goes in this file)."
    )
    inline: list[ReviewKind] = Field(
        default_factory=lambda: list(REVIEW_EVERYTHING),
        description="What is reviewed before it leaves: by default everything the model did not write (your prompts "
        "and attachments, its instructions, what its tools fetch).",
    )
    budget_ms: int | None = Field(
        default=None,
        ge=500,
        description="How long a request may wait for its review (not set: as long as it takes).",
    )
    on_failure: Literal["stop", "send"] = Field(
        default="stop",
        description="When a review cannot be done (the server is off, too slow for `budget_ms`, or its answer is not "
        "to be trusted): `stop` the request, or `send` it masked as the detectors did.",
    )
    background: bool = Field(
        default=True,
        description="Review after it is sent what is not reviewed before: what it finds is masked from the next "
        "request on.",
    )
    slots: int | None = Field(
        default=None,
        ge=1,
        le=4,
        description="Reviews the server runs side by side (not set: what llama.cpp says, or 1).",
    )
    block: bool = Field(
        default=False,
        description="Let the reviewer stop a request whose text must not leave even masked (a whole medical record).",
    )
    auto_mask: list[ReviewCategory] = Field(
        default_factory=lambda: ["person", "address", "email", "phone", "secret", "health", "financial"],
        description="Categories it masks by itself; the others are shown in the dashboard as suggestions.",
    )
    chunk_chars: int = Field(
        default=7000,
        ge=1000,
        le=VALUE_LONGEST,
        description=f"Long texts are reviewed in pieces of about this many characters, {VALUE_LONGEST:,} at most: the "
        "longest value Latet keeps, so that what the reviewer finds in a piece is always kept. A larger setting is "
        f"read as {VALUE_LONGEST:,}.",
    )
    certificate: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="The SHA-256 fingerprint of the server's certificate you trust, for a self-signed one on "
        "another machine of yours: `latet llm trust`, or *Trust it* in the dashboard. Only that certificate "
        "is accepted.",
    )
    images: bool = Field(
        default=False,
        description="Show the reviewer the images too, with the text read in them (its model must see images: "
        "llama.cpp with `--mmproj`). It finds what text recognition missed; a value it sees but Latet cannot place "
        "in the image withholds the image.",
    )
    guidance: str | None = Field(
        default=None,
        description="Your guidance, added after Latet's own instructions to the reviewer: what else is confidential "
        f"in your work, or what to leave alone (at most {MAX_GUIDANCE} characters). It applies to the texts reviewed "
        "from then on. It is sent to the reviewer's server with every review: describe kinds of values, and keep "
        "the confidential ones themselves in `terms`.",
    )

    @field_validator("guidance")
    @classmethod
    def _guidance(cls, text: str | None) -> str | None:
        return clean_guidance(text)

    @field_validator("chunk_chars", mode="before")
    @classmethod
    def _piece(cls, chars: Any) -> Any:
        """A piece larger than a value Latet keeps, as settings saved before took (up to 60,000), read as the largest it
        keeps: they still load, and what the reviewer finds in a piece is kept."""
        return VALUE_LONGEST if type(chars) is int and chars > VALUE_LONGEST else chars

    @field_validator("url")
    @classmethod
    def _address(cls, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("an http:// address, such as http://127.0.0.1:8080/v1")
        # On a server the address is shown whole in every person's dashboard: a credential in it would reach them all.
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(
                "no user or password in the address: the reviewer's key goes in api_key_file or api_key_env, or is "
                "saved with `latet llm key`"
            )
        return url.rstrip("/")

    @model_validator(mode="after")
    def _where(self) -> LlmReview:
        parsed = urlparse(self.url)
        host = (parsed.hostname or "").lower()
        if host in _LOOPBACK or (host == "host.docker.internal" and in_container()):
            return self
        if not self.remote:
            raise ValueError(
                "the reviewer is shown the original text: for a server on another machine, yours, set remote: true "
                "(the dashboard asks you to confirm it)"
            )
        if parsed.scheme != "https" and not _local_network(host):
            raise ValueError("a server outside your local network must be reached over https")
        if self.certificate and parsed.scheme != "https":
            raise ValueError("a certificate is trusted for an https:// server only")
        return self

    def in_clear(self) -> bool:
        """Whether the original texts cross your network unencrypted: http to another machine."""
        return urlparse(self.url).scheme == "http" and not self.on_this_machine()

    def on_this_machine(self) -> bool:
        """Whether the server is on this machine (or the container's host), not another one."""
        host = (urlparse(self.url).hostname or "").lower()
        return host in _LOOPBACK or host == "host.docker.internal"

    def mode(self) -> ReviewMode:
        """``inline``, ``background`` or ``off``: what this configuration reviews."""
        if not self.enabled:
            return "off"
        if self.inline:
            return "inline"
        return "background" if self.background else "off"

    def with_mode(self, mode: ReviewMode) -> LlmReview:
        """The same server, reviewing as much as ``mode`` says (a project's setting)."""
        if mode == "off":
            return self.model_copy(update={"inline": [], "background": False})
        if mode == "background":
            return self.model_copy(update={"inline": [], "background": True})
        return self.model_copy(update={"inline": self.inline or list(REVIEW_EVERYTHING), "background": True})


class LiveReview(BaseModel):
    """The Live Reviewer: you approve, in the dashboard, every new text before it leaves."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = Field(
        default=False,
        description="Every new text waits for your approval in the dashboard: you confirm it, or mask more first.",
    )
    kinds: list[ReviewKind] = Field(
        default_factory=lambda: list(REVIEW_EVERYTHING),
        description="What waits for you: by default everything the model did not write (your prompts and "
        "attachments, its instructions, what its tools fetch).",
    )
    auto_approve_clean: bool = Field(
        default=False,
        description="Send without asking a text in which neither Latet nor the local reviewer found anything.",
    )
    timeout_s: int | None = Field(
        default=None,
        ge=10,
        description="How long a text waits for you; when the time is up it is not sent (not set: until you answer).",
    )


class FieldRule(BaseModel):
    """A rule for the values of one field: a column of a table, a JSON key or path, the key of `key=value`."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str = Field(
        min_length=1,
        max_length=200,
        description="The field: a column name, a JSON key or path (`items[].sku`), or the key of `key=value`; "
        "`*` matches any characters, and case does not matter.",
    )
    type: str | None = Field(default=None, description="The type its values are masked as (`FIELD` when not set).")
    mask: bool = Field(
        default=True,
        description="`false` keeps its values in clear in strict data mode (only in your own settings).",
    )
    secret: bool = Field(default=False, description="Its values are secrets: one in clear blocks the request.")


def _entries(places: list[int]) -> str:
    """``entry 2``, ``entries 2 and 5``: places in a list, from 1, as a refusal names them in place of what is there."""
    if len(places) == 1:
        return f"entry {places[0]}"
    return f"entries {', '.join(str(p) for p in places[:-1])} and {places[-1]}"


def _known_regions(regions: list[str]) -> list[str]:
    unknown = [n for n, r in enumerate(regions, 1) if r.strip() and r.strip().upper() not in ID_REGIONS]
    if unknown:
        raise ValueError(f"{_entries(unknown)}: not a country Latet knows (the countries are {', '.join(ID_REGIONS)})")
    return list(dict.fromkeys(r.strip().upper() for r in regions if r.strip()))


def _compiles(pattern: str) -> str:
    """An allow pattern RE2 compiles. One that does not is refused here, where settings are read, in words that name no
    pattern (its place in the list says which: an allow list's pattern is made of the values it lets through), and
    never written to the standard error (``quiet_options``); where settings holding one anyway are applied, it is left
    out (``AllowList``, ``projects``)."""
    try:
        re2.compile(pattern, quiet_options())
    except re2.error:
        raise ValueError("not a valid RE2 pattern") from None
    return pattern


AllowPattern = Annotated[str, AfterValidator(_compiles)]

DATA_PATTERNS = 200  # the rules of `data_files` or of `data_hosts`, at most
DATA_PATTERN_LONGEST = 200  # the characters of one, at most


def _data_patterns(patterns: list[str]) -> list[str]:
    """The rules of `data_files` or `data_hosts`: at most ``DATA_PATTERNS``, each of at most ``DATA_PATTERN_LONGEST``
    characters, without the blanks around it, each once."""
    if len(patterns) > DATA_PATTERNS:
        raise ValueError(f"at most {DATA_PATTERNS} rules, these are {len(patterns)}")
    long = [n for n, p in enumerate(patterns, 1) if len(p.strip()) > DATA_PATTERN_LONGEST]
    if long:
        raise ValueError(f"{_entries(long)}: longer than {DATA_PATTERN_LONGEST} characters")
    return list(dict.fromkeys(p.strip() for p in patterns if p.strip()))


DataPatterns = Annotated[list[str], AfterValidator(_data_patterns)]


class ProjectSettings(BaseModel):
    """What one project may change: the masking keys of config.yaml, applied on top of them.

    A value replaces the global one (in the repository's `latet.yaml`, only when it is stricter); a
    list is added to the global list.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    profile: Profile | None = Field(default=None, description="The profile in this project.")
    pii: list[str] = Field(default_factory=list, description="Personal-data types to detect, besides the global ones.")
    id_regions: list[str] = Field(
        default_factory=list,
        description="Countries whose national ID and tax numbers to recognise, besides the global ones.",
    )
    phone_regions: list[str] = Field(
        default_factory=list,
        description="Countries whose phone numbers to recognise without a prefix, besides the global ones.",
    )
    include_private_ips: bool | None = Field(default=None, description="Also mask private IP addresses.")
    ner: Literal["auto", "off"] | None = Field(default=None, description="Local name and address detection.")
    terms: list[TermConfig] = Field(default_factory=list, description="Values to always mask in this project.")
    recognizers: list[dict[str, Any]] = Field(
        default_factory=list, description="Recognizers for this project (a name already in use turns the entry off)."
    )
    allow_values: list[str] = Field(default_factory=list, description="Values never to mask in this project.")
    allow_patterns: list[str] = Field(
        default_factory=list, description="Regular expressions (RE2) of values never to mask in this project."
    )
    policies: dict[str, Policy] = Field(default_factory=dict, description="Per type: `mask`, `redact` or `allow`.")
    disabled_secret_rules: list[str] = Field(
        default_factory=list, description="gitleaks rule ids to turn off in this project."
    )
    data: Literal["normal", "strict"] | None = Field(
        default=None, description="`strict` masks every value of tool outputs that are tables, JSON or `key=value`."
    )
    data_tools: list[str] = Field(
        default_factory=list, description="The tools strict data mode covers (`*` patterns), besides the global ones."
    )
    data_files: DataPatterns = Field(
        default_factory=list,
        description="The files strict data mode covers when a tool reads them (path patterns such as `*.json`), "
        "besides the global ones.",
    )
    data_hosts: DataPatterns = Field(
        default_factory=list,
        description="The web addresses strict data mode covers (host patterns such as `*.salesforce.com`), besides "
        "the global ones.",
    )
    fields: list[FieldRule] = Field(default_factory=list, description="Rules for the values of single fields.")
    dump_guard: int | None = Field(
        default=None, ge=0, description="Withhold a tool output with more personal values than this (0: never)."
    )
    images: ImagesSetting | None = Field(
        default=None,
        description="`mask` sends images only with their text read and masked, `block` sends no images or documents.",
    )
    ocr: Literal["auto", "off"] | None = Field(default=None, description="Reading the text in images.")
    llm_review: ReviewMode | None = Field(
        default=None,
        description="How much the local reviewer reads here, when it is on: `off`, `background` or `inline`.",
    )
    live_review: bool | None = Field(
        default=None, description="Whether new texts wait for your approval in the dashboard in this project."
    )
    review_images: bool | None = Field(
        default=None,
        description="Whether the local reviewer is shown the images in this project. Yours alone: a repository file "
        "cannot set it.",
    )
    review_guidance: str | None = Field(
        default=None,
        description="Your guidance to the local reviewer for this project, added after your global "
        f"`llm_review.guidance` (at most {MAX_GUIDANCE} characters). Yours alone: a repository file cannot set it.",
    )

    @field_validator("id_regions")
    @classmethod
    def _regions(cls, regions: list[str]) -> list[str]:
        return _known_regions(regions)

    @field_validator("review_guidance")
    @classmethod
    def _guidance(cls, text: str | None) -> str | None:
        return clean_guidance(text)


class ProjectEntry(ProjectSettings):
    """Your own settings for one project: they stay in your config.yaml, out of the repository."""

    # An allow pattern that does not compile is refused here, as in the global settings; a repository's file, or a file
    # of rules being imported, has one left out with a notice instead.
    allow_patterns: list[AllowPattern] = Field(
        default_factory=list, description="Regular expressions (RE2) of values never to mask in this project."
    )
    path: str = Field(description="The project's folder; its subfolders are part of it.")
    enabled: bool = Field(
        default=True,
        description="`false` turns Latet off in this project: nothing is masked or blocked, and surrogates already "
        "in a conversation are still restored.",
    )


# Routes Latet keeps for itself: the providers it knows, the clouds' fixed routes, its own paths.
RESERVED_DESTINATIONS = frozenset(
    {"anthropic", "openai", "chatgpt", "openrouter", "gemini", "bedrock", "vertex", "backend-api", "_latet"}
)


class Destination(BaseModel):
    """Where an agent's requests go after Latet, besides the providers it knows: your company's gateway,
    Azure OpenAI, Microsoft Foundry. Requests to `http://127.0.0.1:5283/<name>/...` go to `url`."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]{0,62}$",
        description="Its route: lowercase letters, digits and dashes (`llm-acme-com`), and none of Latet's own "
        "(`anthropic`, `openai`, `chatgpt`, `openrouter`, `gemini`, `bedrock`, `vertex`, `backend-api`).",
    )
    url: str = Field(
        description="The address requests go to, such as `https://llm.acme.com`: https outside your local network, "
        "no user, password or query in it."
    )
    agent: Literal["claude", "codex"] | None = Field(
        default=None, description="The agent whose settings Latet read it from; not set: written by you."
    )

    @field_validator("name")
    @classmethod
    def _name(cls, name: str) -> str:
        if name in RESERVED_DESTINATIONS:
            raise ValueError(f"{name} is one of Latet's own routes: choose another name")
        return name

    @field_validator("url")
    @classmethod
    def _address(cls, url: str) -> str:
        if re.search(r"[\x00-\x20\x7f\\]", url):  # what readers of an address take apart differently
            raise ValueError("no spaces, control characters or backslashes in the address")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("an https:// address, such as https://llm.acme.com")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("no user or password in the address: the agent sends its own key")
        if parsed.query or parsed.fragment:
            raise ValueError("no ?query or #fragment in the address")
        host = parsed.hostname.lower()
        if parsed.scheme != "https" and host not in _LOOPBACK and not _local_network(host):
            raise ValueError("a server outside your local network must be reached over https")
        return url.rstrip("/")


class Config(BaseModel):
    model_config = _QUIET_ERRORS

    profile: Profile = Field(
        default=Profile.BALANCED, description="Protection profile: `balanced`, `max` or `secrets`."
    )
    port: int = Field(default=5283, description="Local port of the daemon (it listens on 127.0.0.1 only).")
    phone_regions: list[str] = Field(
        default_factory=lambda: ["IT", "US", "GB"],
        description="Countries whose phone numbers are recognised without an international prefix.",
    )
    id_regions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ID_REGIONS),
        description="Countries whose national ID and tax numbers are recognised (the `max` profile: all of "
        f"them): {', '.join(f'`{c}`' for c in ID_REGIONS)}. VAT numbers of 30 European countries are "
        "recognised whatever this says.",
    )
    pii: list[str] | None = Field(
        default=None,
        description="Personal-data types to detect; not set means the base types and the ID numbers of "
        "`id_regions`, and a list is used as it is.",
    )
    include_private_ips: bool | None = Field(
        default=None, description="Also mask private IP addresses (10.x, 192.168.x...); on in `max` by default."
    )
    disabled_secret_rules: list[str] = Field(
        default_factory=list, description="gitleaks rule ids to turn off, such as `generic-api-key`."
    )
    terms: list[TermConfig] = Field(default_factory=list, description="Your own values to always mask.")
    recognizers: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Your own recognizers: a pattern, check digits and keywords for each (see below). An "
        "entry with a mistake is turned off and reported, and kept as it is.",
    )
    allow_values: list[str] = Field(default_factory=list, description="Values never to mask (not sensitive).")
    allow_patterns: list[AllowPattern] = Field(
        default_factory=list, description="Regular expressions (RE2) of values never to mask."
    )
    policies: dict[str, Policy] = Field(
        default_factory=dict,
        description="Per type: `mask` (reversible surrogate), `redact` (one way, never restored) or `allow`.",
    )
    identity: IdentityConfig = Field(default_factory=IdentityConfig, description="This machine's identity.")
    capture_dir: Path | None = Field(
        default=None, description="Debugging only: write raw exchanges (credentials redacted) to this directory."
    )
    history: Literal["full", "metadata", "off"] = Field(
        default="full",
        description="What the dashboard keeps of each exchange: `full` (masked text and answers), "
        "`metadata` (counts, timings, flags; no text) or `off`.",
    )
    retention_days: int = Field(default=14, ge=1, le=365, description="Days the dashboard history is kept.")
    auto_mode_review: AutoModeReview = Field(
        default_factory=AutoModeReview,
        description="Claude Code's auto mode reviewer is sent your OS user name and the paths of your home and "
        "working directories, plus any host name or git author in your CLAUDE.md or in the action. A review "
        "that would carry a blocked detail is stopped, and auto mode cannot approve that action: Claude Code "
        "refuses it as if its reviewer were unavailable. `block` blocks every "
        "detail, `allow-identity` lets every detail through, and the keys below choose one by one. Secrets "
        "and personal data never reach the reviewer.",
    )
    ner: Literal["auto", "off"] = Field(
        default="auto",
        description="Local name and address detection: `auto` uses it once installed (`latet ner install`), "
        "`off` never does.",
    )
    ocr: Literal["auto", "off"] = Field(
        default="auto",
        description="The text in images: `auto` reads it once the models are installed (`latet ocr install`) "
        "and masks what it finds, `off` never does (images are then sent as they are, and flagged).",
    )
    data: Literal["normal", "strict"] | None = Field(
        default=None,
        description="`strict` masks every value of tool outputs that are tables (psql, CSV, TSV, markdown, HTML, "
        "a browser's snapshot), JSON or `key=value`, column and key names aside; fields set `mask: false` stay in "
        "clear. Outputs of tools that read files are left alone unless `data_files` names the file, and so are web "
        "search results and pages (public content, not your data) unless `data_tools` names their tool or "
        "`data_hosts` their address. Not set: the profile decides, `strict` with `max` and `normal` with the others.",
    )
    data_tools: list[str] = Field(
        default_factory=list,
        description="The tools and MCP servers strict data mode covers (`*` patterns, such as `mcp__postgres__*`); "
        "empty: every tool but those that read files.",
    )
    data_files: DataPatterns = Field(
        default_factory=list,
        description="The files strict data mode covers when a tool reads them: path patterns such as `*.json`, "
        "`**/*.csv`, `exports/**` or `.env*` (`*` for any characters, in any case; a pattern without a slash names a "
        "file by its name). Each value is found by the file's format: JSON, tables, `.env` and properties, YAML. A "
        "markdown file only when a pattern names markdown (`*.md`), and then only its tables. A file named is data "
        "whether strict data mode is on or off.",
    )
    data_hosts: DataPatterns = Field(
        default_factory=list,
        description="The web addresses strict data mode covers when a web tool fetches them: host patterns such as "
        "`api.internal.example.com` or `*.salesforce.com`. An address named is data whether strict data mode is on or "
        "off.",
    )
    fields: list[FieldRule] = Field(
        default_factory=list, description="Rules for the values of single fields of tables, JSON and `key=value`."
    )
    dump_guard: int | None = Field(
        default=None,
        ge=0,
        description="Withhold a tool output that holds more personal values (e-mails, phone numbers, names, ID "
        "numbers...) than this: the model is told why instead. Not set or 0: never.",
    )
    images: ImagesSetting = Field(
        default="allow",
        description="`allow` sends images, with their text masked once the text recognition models are installed "
        "(`latet ocr install`); `mask` sends only images whose text was read and masked, and no documents; "
        "`block` sends no images or documents. The max profile sends none either, unless this says `mask`.",
    )
    llm_review: LlmReview = Field(
        default_factory=LlmReview,
        description="The local reviewer: a language model on your own server that reads what the detectors left in "
        "clear (see below).",
    )
    live_review: LiveReview = Field(
        default_factory=LiveReview,
        description="The Live Reviewer: every new text waits for your approval in the dashboard (see below).",
    )
    projects: list[ProjectEntry] = Field(
        default_factory=list,
        description="Your own settings for single projects, on top of these and of the project's `latet.yaml` "
        "(see below).",
    )
    upstreams: list[Destination] = Field(
        default_factory=list,
        description="Where agents' requests go after Latet, besides Anthropic, OpenAI and OpenRouter: your "
        "company's gateway, Azure OpenAI, Microsoft Foundry. `latet claude`, `latet codex` and `global-install` "
        "add the ones they read from your agents' settings (see below).",
    )

    @field_validator("id_regions")
    @classmethod
    def _regions(cls, regions: list[str]) -> list[str]:
        return _known_regions(regions)

    @field_validator("upstreams")
    @classmethod
    def _one_name_each(cls, destinations: list[Destination]) -> list[Destination]:
        names = [d.name for d in destinations]
        twice = [n for n, name in enumerate(names, 1) if names.count(name) > 1]
        if twice:
            raise ValueError(f"{_entries(twice)} have the same name: each destination needs its own")
        return destinations

    def data_mode(self) -> Literal["normal", "strict"]:
        """Strict data mode as it applies: `data` when set, else the profile's (`strict` with `max`)."""
        if self.data is not None:
            return self.data
        return "strict" if self.profile == Profile.MAX else "normal"


def config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP, appauthor=False, roaming=True))


def data_dir() -> Path:
    return Path(os.environ.get("LATET_DATA_DIR") or platformdirs.user_data_dir(APP, appauthor=False))


def config_path() -> Path:
    return Path(os.environ.get("LATET_CONFIG") or (config_dir() / "config.yaml"))


def in_container() -> bool:
    """Running in the Latet container image (``LATET_CONTAINER=1``)."""
    return os.environ.get("LATET_CONTAINER") == "1"


class ConfigError(ValueError):
    """Settings read from a file that are not valid YAML or not valid settings. Its words say where, never what is
    written there (a term, your identity, a value you let through): a log, a reload's answer or a traceback carries
    them."""


_PROBLEMS_SHOWN = 10  # what is wrong with settings that a refusal names at most


@functools.cache
def _field_names() -> frozenset[str]:
    """The name of every field of the settings, at any depth: what a place shows. Any other name in a place is a key
    the settings were given (a mapping's, or one no field has), which may be anything: it is shown as ``*``."""
    schema = Config.model_json_schema()
    found = set(schema.get("properties", {}))
    for model in schema.get("$defs", {}).values():
        found |= set(model.get("properties", {}))
    return frozenset(found)


def place_of(loc: Sequence[int | str]) -> str:
    """Where in the settings an error is: ``retention_days``, ``upstreams.1.url``, ``policies.*``."""
    return ".".join(str(p) if isinstance(p, int) or p in _field_names() else "*" for p in loc) or "(the settings)"


def problems_of(exc: ValidationError) -> list[str]:
    """What is wrong with settings, each by its place and pydantic's words for it, which quote nothing they were given
    (``_QUIET_ERRORS``): ``_PROBLEMS_SHOWN`` at most, and how many more."""
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    said = [f"{place_of(error['loc'])}: {error['msg'].removeprefix('Value error, ')}" for error in errors]
    more = [f"and {len(said) - _PROBLEMS_SHOWN} more"] if len(said) > _PROBLEMS_SHOWN else []
    return said[:_PROBLEMS_SHOWN] + more


def _compiled_or_left_out(holder: dict[str, Any]) -> list[int]:
    """The allow patterns of ``holder`` RE2 cannot compile, taken out of it (where it stands, in place), by their places
    from 1. One that is not text is left for pydantic to refuse."""
    patterns = holder.get("allow_patterns")
    if not isinstance(patterns, list):
        return []
    kept: list[Any] = []
    broken: list[int] = []
    for place, pattern in enumerate(patterns, 1):
        try:
            if isinstance(pattern, str):
                _compiles(pattern)
        except ValueError:
            broken.append(place)
            continue
        kept.append(pattern)
    if broken:
        holder["allow_patterns"] = kept
    return broken


def _too_long_left_out(holder: dict[str, Any]) -> list[int]:
    """The terms of ``holder`` longer than a term holds (``TERM_LONGEST``), taken out of it (where it stands, in place),
    by their places from 1. One that is not a term is left for pydantic to refuse."""
    terms = holder.get("terms")
    if not isinstance(terms, list):
        return []
    kept: list[Any] = []
    long: list[int] = []
    for place, term in enumerate(terms, 1):
        value = term.get("value") if isinstance(term, dict) else term
        if isinstance(value, str) and len(value) > TERM_LONGEST:
            long.append(place)
            continue
        kept.append(term)
    if long:
        holder["terms"] = kept
    return long


def left_out(data: Any) -> list[str]:
    """What settings read from a file (config.yaml, the settings you approved) hold that cannot apply, taken out of
    ``data`` before it is read as settings, each said by its place: an allow pattern RE2 cannot compile (the safe side:
    more is masked), ``allow_patterns entry 2 is not a valid RE2 pattern and is left out``; a term longer than a term
    holds, which could leave no rule built at all (said, as less is masked: never silently), ``projects entry 1, terms
    entry 1 holds more than 8192 characters and is left out``. A file edited by hand still loads; whatever saves
    settings refuses both (``AllowPattern``, ``term_errors``)."""
    if not isinstance(data, dict):
        return []

    def said(holder: dict[str, Any], where: str) -> list[str]:
        out = [f"{where}allow_patterns entry {n} is not a valid RE2 pattern and is left out"
               for n in _compiled_or_left_out(holder)]  # fmt: skip
        out += [f"{where}terms entry {n} holds more than {TERM_LONGEST} characters and is left out"
                for n in _too_long_left_out(holder)]  # fmt: skip
        return out

    notices = said(data, "")
    projects = data.get("projects")
    for index, entry in enumerate(projects if isinstance(projects, list) else [], 1):
        if isinstance(entry, dict):
            notices += said(entry, f"projects entry {index}, ")
    return notices


def settings_from(data: Any, what: str) -> Config:
    """``data`` as settings, read from ``what`` (a file's path). Raises ``ConfigError`` naming each place that is wrong,
    raised once pydantic's error is handled, so that no traceback shows that error with it."""
    problems: list[str] = []
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        problems = problems_of(exc)
    raise ConfigError(f"{what} is not valid settings: {'; '.join(problems)}")


def load_config(path: Path | None = None, notices: list[str] | None = None) -> Config:
    """The settings in ``path`` (None: config.yaml). An allow pattern RE2 cannot compile and a term longer than a term
    holds are left out, each with a notice that names its place, in the log and in ``notices`` when it is given
    (``left_out``). Raises ``ConfigError`` (a ``ValueError``) for a file that is not valid YAML or not valid settings,
    in words that say where and never what is written there, and ``OSError`` for one that cannot be read."""
    path = path or config_path()
    data = None
    if path.exists():
        text = path.read_text("utf-8")
        try:
            data = load_yaml(text)
        except yaml.YAMLError as exc:
            # Where, never what: PyYAML's message quotes the line, which can be a term, your identity or a value you
            # let through, and a log, a reload's answer or a traceback would carry it. `from None`: not chained either.
            raise ConfigError(f"{path} is not valid YAML{_yaml_error_at(exc, text)}") from None
    for notice in left_out(data):
        log.warning("%s: %s", path.name, notice)
        if notices is not None:
            notices.append(notice)
    config = settings_from(data or {}, str(path))
    # The container is given its port from outside, so that it and the published port agree.
    if port := os.environ.get("LATET_PORT"):
        config.port = int(port)
    return config


def save_config(config: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="json", exclude_none=True)
    # Your settings for each project: only what they set, so the file reads like what you wrote.
    data["projects"] = [
        {"path": e.path, **e.model_dump(mode="json", exclude_defaults=True, exclude={"path"})} for e in config.projects
    ]
    if not data["projects"]:
        del data["projects"]
    # Editors that read this line (VS Code's YAML extension, for one) complete and check the file.
    header = f"# yaml-language-server: $schema={SCHEMA_URL}\n"
    write_atomic(path, header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return path


def _git(key: str) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "config", "--global", "--get", key],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value or None


def os_user_name() -> str | None:
    """The login name, from the operating system.

    Never from `USERNAME` / `USER`: a process can set those, and Claude Code's sessions carry the
    user name's surrogate there with ``auto_mode_review.user_name: surrogate``.
    """
    if sys.platform == "win32":
        with contextlib.suppress(OSError):
            return os.getlogin()  # GetUserNameW
        return None
    else:
        import pwd

        with contextlib.suppress(KeyError, OSError):
            return pwd.getpwuid(os.getuid()).pw_name
        return None


def _country_of(locale_name: str) -> str | None:
    """``it_IT.UTF-8``, ``de-DE``, ``en_IT@currency=EUR`` → the country code."""
    m = re.match(r"^[A-Za-z]{2,3}[_-]([A-Za-z]{2})(?![A-Za-z])", locale_name.strip())
    return m.group(1).upper() if m else None


def system_country() -> str | None:
    """The country this machine is set to: the Windows location, the macOS region, or the locale."""
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            kernel32 = ctypes.windll.kernel32
            buffer = ctypes.create_unicode_buffer(85)
            geo = kernel32.GetUserGeoID(16)  # GEOCLASS_NATION
            if geo != -1 and kernel32.GetGeoInfoW(geo, 4, buffer, len(buffer), 0):  # GEO_ISO2
                return buffer.value.upper() or None
            if kernel32.GetUserDefaultLocaleName(buffer, len(buffer)):
                return _country_of(buffer.value)
    elif sys.platform == "darwin":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleLocale"],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if country := _country_of(out.stdout):
                return country
    for name in ("LC_ALL", "LC_ADDRESS", "LANG"):
        if (value := os.environ.get(name)) and (country := _country_of(value)):
            return country
    return None


def _home() -> Path:
    if sys.platform != "win32":
        import pwd

        with contextlib.suppress(KeyError, OSError):
            return Path(pwd.getpwuid(os.getuid()).pw_dir)
    return Path.home()


def detect_identity() -> IdentityConfig:
    """This machine's identity: OS user, host name and git author (masked by default)."""
    # The login name and the user folder can differ (Windows: user "jdoe", folder C:\Users\john).
    candidates = [os_user_name(), _home().name]
    usernames = list(dict.fromkeys(c for c in candidates if c and len(c) >= 2))
    emails = [e for e in [_git("user.email")] if e]
    return IdentityConfig(
        usernames=usernames,
        hostname=socket.gethostname() or None,
        full_name=_git("user.name"),
        emails=emails,
    )
