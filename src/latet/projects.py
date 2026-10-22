"""Settings for one project: an ``latet.yaml`` in its repository, and your own in config.yaml.

The repository file is shared, and a cloned repository must not be able to turn anything off: it can
only add protection, and what it says beyond that is ignored and reported. Your own settings for a
project stay in your config.yaml: they may also loosen it, or turn Latet off there.

A session belongs to the project of its working directory: the Claude Code plugin tells the daemon,
and Codex and OpenCode write it in their requests.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeGuard

import re2
import yaml

from latet import repository_io
from latet.config import (
    POLICY_STRICTNESS,
    PROFILE_STRICTNESS,
    REVIEW_MODES,
    TERM_WITHOUT_LETTERS,
    Config,
    IdentityConfig,
    Profile,
    ProjectEntry,
    ProjectSettings,
    join_guidance,
    write_atomic,
)
from latet.engine.build import build_masker
from latet.engine.detectors.base import DetectContext, quiet_options
from latet.engine.detectors.ids import types_for
from latet.engine.detectors.pii import ALL_PII, effective_pii
from latet.engine.detectors.recognizers import Status
from latet.engine.detectors.terms import TERM_LONGEST, TermsDetector, has_letter_or_digit
from latet.engine.masker import Masker
from latet.engine.pipeline import LRU, Scope, Session
from latet.engine.types import Policy, Span
from latet.engine.vault import Vault
from latet.fileio import FileReadError, ReadFailure, read_regular_bytes, read_regular_snapshot
from latet.logline import described
from latet.project_file import (  # noqa: F401 - _problem, Remembered: re-exported, as they were defined here
    _LOOSENING,
    MAX_FILE_BYTES,
    MAX_RECOGNIZERS,
    MAX_TERMS,
    PROJECT_FILE,
    REPO_KEYS,
    Notice,
    Remembered,
    _problem,
    _project_settings,
    read_remembered,
    repo_settings,
)
from latet.protection import lowered_in_project
from latet.schema import PROJECT_SCHEMA_URL
from latet.state.local import REPOSITORIES_KEPT, LocalRepositories, own_file, rememberable
from latet.vision.policy import IMAGES_RANK, image_policy

log = logging.getLogger(__name__)

PROJECT_DOCS_URL = "https://docs.latet.ai/concepts/projects/"
_RECHECK = 2.0  # seconds between two looks at a project's file
# The projects remembered at most: on a server every folder a client names is a project of its own. The least recently
# seen go, with the maskers and copies of the settings no other one uses, and are worked out again when used
# (``Projects.scope_for`` resolves a session's folder again).
PROJECTS_KEPT = 2_000
# What the projects are weighed at (``Projects.weight``: a server's budget of spaces counts it), in bytes, from their
# rules, as measured: a masker (its detectors' tables, each term, each recognizer, each pattern, the RE2 programs Python
# does not see included, each value), a copy of the settings (each object of its lists, each text it shares with the
# settings it was copied from), and each project's own record. Its terms' matcher is weighed besides at its RE2 budget
# (``TermsDetector.memory``), the bound of what its automaton grows to as it works; the vault's values are matched by
# one pair of detectors for the whole space (``Vault.matchers``), never a project's own.
_MASKER_BASE = 20 * 1024
_MASKER_PER_TERM = 256
_MASKER_PER_RECOGNIZER = 64 * 1024
_MASKER_PER_PATTERN = 4 * 1024
_MASKER_PER_VALUE = 160
_COPY_BASE = 6 * 1024
_COPY_PER_OBJECT = 512
_COPY_PER_TEXT = 16
_PROJECT_BASE = 2 * 1024
# The lists of the rules a project in the dashboard's list is worked out without (its summary counts none of them).
_LISTED_WITHOUT = ("terms", "recognizers", "fields", "allow_values", "allow_patterns", "upstreams", "projects")

_STRICTNESS = PROFILE_STRICTNESS
_POLICY_STRICTNESS = POLICY_STRICTNESS

FILE_HEADER = (
    f"# yaml-language-server: $schema={PROJECT_SCHEMA_URL}\n"
    "# Latet settings for this project, shared with everyone who works on it. They can only add\n"
    f"# protection to each person's own: {PROJECT_DOCS_URL}\n"
)

TEMPLATE = (
    FILE_HEADER
    + """#
# profile: max                # stricter than your own: secrets < balanced < max
# id_regions: [DE, FR]        # countries whose ID and tax numbers to recognise, besides your own
# pii: [IBAN]                 # personal-data types to detect, besides your own
# terms:                      # values to always mask (this file is shared: they end up in git)
#   - {value: Project Falcon, type: PROJECT}
# recognizers: []             # formats of this project's own codes: `latet rules suggest .`
# policies: {EMAIL: redact}   # only stricter than your own (allow < mask < redact)
# data: strict                # mask every value of tool outputs that are tables, JSON or key=value
# fields:                     # rules for single fields: a column, a JSON key or path, a key=value key
#   - {name: "note_*", type: NOTE}
# dump_guard: 25              # withhold a tool output with more personal values than this
# images: mask                # send images only with their text masked (block: send none)
# llm_review: inline          # the local reviewer, where one is on: only more (off < background < inline)
"""
)


UNBUILT = "its rules could not be built: its texts are refused until they can be"
# The reasons of a project's notices the dashboard words in its reader's language: its space, past its share of a Latet
# server's memory, built no masker for it; its rules could not be built (``UNBUILT``).
PAST_SHARE_REASON = "space_past_share"
UNBUILT_REASON = "project_unbuilt"


class _Unbuilt:
    """The detector of a project whose rules could not be built: it fails at every text, so that the masking stops the
    request there (a mandatory detector that fails is a ``MaskingError``) and the final check refuses alike: nothing
    leaves on rules that mask less than the project's."""

    id = "project-rules"
    mandatory = True

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        raise RuntimeError("this project's rules could not be built")


class _NotBuilt(Exception):
    """A project's masker its space does not build now, or does not keep once built: past its share of a Latet server's
    memory (``words``). ``weight``: what the masker weighs, or would by its rules."""

    def __init__(self, words: str, weight: int) -> None:
        super().__init__(words)
        self.words, self.weight = words, weight


class _PastShare:
    """The detector of a project whose masker its space does not build, past its share of a Latet server's memory: it
    refuses every text with the share's words (``runtime.SpaceFull``), and the final check refuses alike."""

    id = "project-rules"
    mandatory = True

    def __init__(self, words: str) -> None:
        self.words = words

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        from latet.runtime import SpaceFull  # here: the runtime imports this module

        raise SpaceFull(self.words)


def _unbuilt(config: Config, enabled: bool, vault: Vault, refusing: _PastShare | None = None) -> Scope:
    """The scope of a project whose rules could not be built (S-03), or were not (``refusing``, ``_PastShare``): every
    text there refused (``_Unbuilt``), the rest as its settings say; Latet off there stays off, nothing masked and so
    nothing refused. Kept by no cache of maskers: the project's next working out (new settings, its file changed, its
    space under its share again) tries to build its rules again."""
    key = f"unbuilt:{secrets.token_hex(8)}"  # its own: no text masked or checked under it is kept as it left
    return Scope(
        key, key, Masker(vault, [refusing or _Unbuilt()]), enabled, image_policy(config), config.dump_guard,
        config.llm_review, config.live_review, ocr=config.ocr == "auto",
    )  # fmt: skip


# --- folders ------------------------------------------------------------------------------------------


def norm_path(path: str) -> str:
    """A folder as compared: forward slashes, no trailing one, and no case on Windows."""
    text = path.strip().replace("\\", "/")
    while len(text) > 1 and text.endswith("/") and not text.endswith(":/"):
        text = text[:-1]
    windows = os.name == "nt" or (len(text) >= 2 and text[1] == ":") or text.startswith("//")
    return text.casefold() if windows else text


def within(path: str, folder: str) -> bool:
    """``path`` is ``folder`` or inside it."""
    p, f = norm_path(path), norm_path(folder)
    return p == f or p.startswith(f.rstrip("/") + "/")


def project_id(root: str) -> str:
    return hashlib.sha256(norm_path(root).encode()).hexdigest()[:12]


def git_root(start: Path) -> Path | None:
    for folder in (start, *start.parents):
        if (folder / ".git").exists():
            return folder
    return None


def find_project_file(cwd: Path) -> Path | None:
    """The nearest latet.yaml from ``cwd`` up to the root of its repository (only ``cwd`` outside one)."""
    root = git_root(cwd)
    for folder in (cwd, *cwd.parents):
        candidate = folder / PROJECT_FILE
        if candidate.is_file():
            return candidate
        if root is None or folder == root:
            return None
    return None


def not_applied_here(cwd: Path) -> str | None:
    """On a computer connected to a Latet server, why the working folder's project file is not applied (R9): the
    server reads no repository file, so what a team shares in its latet.yaml (a stricter profile, its terms and
    recognizers) does not reach this person's requests. None when the folder's project has no latet.yaml. Said once by
    `latet doctor` and `latet claude`."""
    try:
        found = find_project_file(cwd)
    except OSError:
        return None
    if found is None:
        return None
    return (
        f"{found} is not applied: this computer is connected to a Latet server, which applies your own settings, "
        "never a repository's file. Set what it sets in your dashboard (`latet dashboard`), or import it there as a "
        "rules file"
    )


def project_root(cwd: str) -> tuple[str, Path | None]:
    """The project of a working directory and its file: the folder of its latet.yaml, else the root of
    its repository, else the folder itself (also when it is not on this machine, as in a container)."""
    path = Path(cwd)
    try:
        if path.is_dir():
            found = find_project_file(path)
            if found is not None:
                return str(found.parent), found
            return str(git_root(path) or path), None
    except OSError:
        pass
    return cwd, None


# --- reading -----------------------------------------------------------------------------------------


# Why a project's latet.yaml was not read, as its notice says it: fixed words, never the system's (they can name the
# file) nor the path.
_UNREAD: dict[ReadFailure, str] = {
    "missing": "cannot read it: it is not there",
    "nonregular": "cannot read it: it is not a regular file",
    "too_large": f"larger than {MAX_FILE_BYTES // 1024} KB: not read",
    "unavailable": "cannot read it now",
}


@dataclass(frozen=True, slots=True)
class FileRead:
    """A project's latet.yaml read once (``read_file``): what it sets that applies and its notices; the tag of the very
    bytes read (what the dashboard's editor saves over, ``file_tag``) and their stamp, from the file as it was opened;
    or why it was not read (``failure``, with no tag)."""

    repo: ProjectSettings | None
    notices: list[Notice]
    tag: str | None = None
    stamp: tuple[int, int] | None = None
    failure: ReadFailure | None = None


def _tag(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def read_file(path: Path) -> FileRead:
    """A repository file read once, never raising, and never more of it than a project file may hold: through any link
    (as T24.3 has it), a regular file only, opened without waiting on a pipe and checked again once open
    (``fileio.read_regular_snapshot``). What is wrong with it comes back as notices."""
    try:
        raw, opened = read_regular_snapshot(path, limit=MAX_FILE_BYTES, follow=True)
    except FileReadError as failure:
        return FileRead(None, [Notice("repo", "", "error", _UNREAD[failure.reason])], failure=failure.reason)
    settings, notices = _project_settings(raw)
    return FileRead(settings, notices, _tag(raw), (opened.st_mtime_ns, opened.st_size))


def read_project_file(path: Path) -> tuple[ProjectSettings | None, list[Notice]]:
    """A repository file, never raising: what is wrong with it comes back as notices (``read_file``)."""
    read = read_file(path)
    return read.repo, read.notices


def _stamp(path: Path | None) -> tuple[int, int] | None:
    if path is None:
        return None
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _read_remembered(folder: str) -> Remembered:
    """What the latet.yaml of a repository remembered from before (``SeenRepositories``) comes to, read again now
    (``project_file.read_remembered``, by this module's ``own_file`` and ``_project_settings``): what the helper that
    checks them reads (``repository_child``), and a test's own helper in this process."""
    return read_remembered(folder, own_file, _project_settings)


# --- merging ------------------------------------------------------------------------------------------


def _layer(
    config: Config,
    s: ProjectSettings,
    source: Literal["repo", "personal"],
    extra_pii: list[str],
    *,
    adding: bool = False,
) -> list[Notice]:
    """Apply one layer onto ``config``. The repository's may only make it stricter. ``adding``: your own entry taken
    as a repository file's is, only where it adds protection (``effective_config(only_adding=True)``), and its Live
    Reviewer turned on with it, which only adds protection too."""
    strict = source == "repo"
    out: list[Notice] = []

    def ignore(key: str, message: str) -> None:
        out.append(Notice(source, key, "ignored", message))

    if s.profile is not None and s.profile != config.profile:
        if strict and _STRICTNESS[s.profile] < _STRICTNESS[config.profile]:
            ignore("profile", f"a repository file can only make the profile stricter than {config.profile.value}")
        else:
            config.profile = s.profile
    if s.id_regions:
        new = [r for r in s.id_regions if r not in config.id_regions]
        config.id_regions = [*config.id_regions, *new]
        if config.pii is not None:  # an explicit list leaves the regions out: add their numbers to it
            config.pii = list(dict.fromkeys([*config.pii, *types_for(new)]))
    if s.pii:
        unknown = [t for t in s.pii if t not in ALL_PII]
        if unknown:
            out.append(Notice(source, "pii", "error", f"unknown types: {', '.join(unknown)}"))
        extra_pii += [t for t in s.pii if t in ALL_PII]
    if s.phone_regions:
        config.phone_regions = list(dict.fromkeys([*config.phone_regions, *(r.upper() for r in s.phone_regions)]))
    if s.include_private_ips is not None:
        current = (
            config.include_private_ips if config.include_private_ips is not None else config.profile == Profile.MAX
        )
        if strict and current and not s.include_private_ips:
            ignore("include_private_ips", "a repository file can only turn the masking of private IPs on")
        else:
            config.include_private_ips = s.include_private_ips
    if s.ner is not None and s.ner != config.ner:
        if strict and s.ner == "off":
            ignore("ner", "a repository file can only turn name detection on")
        else:
            config.ner = s.ner
    if s.terms:
        have = {(t.value, t.type.upper()) for t in config.terms}
        usable = []
        for place, t in enumerate(s.terms, 1):
            if not has_letter_or_digit(t.value):
                out.append(Notice(source, "terms", "error", f"{t.value!r} left out: {TERM_WITHOUT_LETTERS}"))
            elif len(t.value) > TERM_LONGEST:  # by its place: compiled, it could leave the project no rule (S-03)
                out.append(Notice(source, "terms", "error", f"entry {place} holds more than {TERM_LONGEST} characters: "
                                  "left out"))  # fmt: skip
            else:
                usable.append(t)
        new = [t for t in usable if (t.value, t.type.upper()) not in have]
        config.terms = [*config.terms, *new]
    if s.recognizers:
        config.recognizers = [*config.recognizers, *s.recognizers]  # a name already in use: that entry is off
    for etype, policy in s.policies.items():
        current = config.policies.get(etype, Policy.MASK)
        if strict and _POLICY_STRICTNESS[policy] < _POLICY_STRICTNESS[current]:
            ignore(f"policies.{etype}", f"a repository file can only make {etype} stricter than {current.value}")
        else:
            config.policies[etype] = policy
    if s.data is not None:
        if not strict or s.data == "strict":
            config.data = s.data  # set even when the profile gives it already: a later layer's profile keeps it
        elif config.data_mode() == "strict":
            ignore("data", "a repository file can only turn strict data mode on")
    if s.fields:
        kept = []
        for rule in s.fields:
            if strict and not rule.mask:
                ignore(
                    f"fields.{rule.name}", "a repository file cannot keep a field in clear: put it in your own settings"
                )
            else:
                kept.append(rule)
        config.fields = [*kept, *config.fields]  # the nearer layer first: the first rule that matches wins
    if s.dump_guard is not None:
        if not strict:
            config.dump_guard = s.dump_guard or None
        elif config.dump_guard is None or 0 < s.dump_guard < config.dump_guard:
            config.dump_guard = s.dump_guard or config.dump_guard
        elif s.dump_guard == 0 or s.dump_guard > config.dump_guard:
            ignore("dump_guard", f"a repository file can only lower the dump guard, now {config.dump_guard}")
    if s.images is not None and s.images != config.images:
        current = image_policy(config).send  # with the max profile, `block` whatever `images` says
        if strict and IMAGES_RANK[s.images] < IMAGES_RANK[current]:
            ignore("images", f"a repository file can only send fewer images, now `{current}`")
        else:
            config.images = s.images
    if s.ocr is not None and s.ocr != config.ocr:
        if strict and s.ocr == "off":
            ignore("ocr", "a repository file can only turn reading the text in images on")
        else:
            config.ocr = s.ocr
    if s.llm_review is not None:
        current = config.llm_review.mode() if config.llm_review.enabled else "off"
        if strict and REVIEW_MODES.index(s.llm_review) < REVIEW_MODES.index(current):
            ignore("llm_review", f"a repository file can only make the local reviewer read more than {current}")
        elif config.llm_review.enabled:
            config.llm_review = config.llm_review.with_mode(s.llm_review)
    # Yours alone: a repository file never makes you approve. Yours taken as only adding: turned on, never off.
    if s.live_review is not None and (not strict or (adding and s.live_review)):
        config.live_review = config.live_review.model_copy(update={"enabled": s.live_review})
    if s.review_images is not None and not strict:  # yours alone: images may go to a reviewer on another machine
        config.llm_review = config.llm_review.model_copy(update={"images": s.review_images})
    if s.review_guidance and not strict:  # yours alone: a repository file never tells your reviewer what to skip
        guidance = join_guidance(config.llm_review.guidance, s.review_guidance)
        config.llm_review = config.llm_review.model_copy(update={"guidance": guidance})
    # The files and the web addresses strict data mode covers: adding one protects, so a repository file adds too.
    config.data_files = list(dict.fromkeys([*config.data_files, *s.data_files]))
    config.data_hosts = list(dict.fromkeys([*config.data_hosts, *s.data_hosts]))
    if not strict:
        config.data_tools = list(dict.fromkeys([*config.data_tools, *s.data_tools]))
        config.allow_values = list(dict.fromkeys([*config.allow_values, *s.allow_values]))
        config.disabled_secret_rules = list(dict.fromkeys([*config.disabled_secret_rules, *s.disabled_secret_rules]))
        for place, pattern in enumerate(s.allow_patterns, 1):
            try:
                re2.compile(pattern, quiet_options())
            except re2.error:  # by its place: a pattern is made of the values it lets through
                why = f"entry {place} is not a valid RE2 pattern: left out"
                out.append(Notice(source, "allow_patterns", "error", why))
                continue
            if pattern not in config.allow_patterns:
                config.allow_patterns = [*config.allow_patterns, pattern]
    return out


def effective_config(
    base: Config,
    repo: ProjectSettings | None,
    personal: ProjectEntry | None,
    *,
    only_adding: bool = False,
) -> tuple[Config, list[Notice]]:
    """The rules in a project: the global ones, then the repository file's (only stricter), then yours. With
    ``only_adding`` yours too are taken as a repository file's are, only where they add protection, your Live Reviewer
    turned on there among them (what a Latet server applies while it cannot tell that you are in that folder:
    ``Projects.scope_for``).

    Laid over a shallow copy of ``base``: each list a layer adds to is replaced, never changed in place, and the one
    mapping it changes in place is copied; the items themselves are the global settings' (nobody changes an item of
    settings). Not a deep copy of every term and rule, which a check of every folder would pay for each, and which each
    folder's rules would hold for as long as they are kept (a server's every collection of memory visits them)."""
    # Your settings for every folder are no folder's rules: left out, as the copy's policies are its own.
    config = base.model_copy(update={"projects": [], "policies": dict(base.policies)})
    notices: list[Notice] = []
    extra_pii: list[str] = []
    if repo is not None:
        notices += _layer(config, repo, "repo", extra_pii)
    if personal is not None:
        notices += _layer(config, personal, "repo" if only_adding else "personal", extra_pii, adding=only_adding)
    if extra_pii:
        current = effective_pii(config.pii, config.id_regions, every_region=config.profile == Profile.MAX)
        config.pii = list(dict.fromkeys([*current, *extra_pii]))
    return config, notices


def _a_folder(cwd: Any) -> TypeGuard[str]:
    """Whether ``cwd`` is a working directory as a client reports one: a text, not blank, 4,096 characters at most."""
    return isinstance(cwd, str) and bool(cwd.strip()) and len(cwd) <= 4096


def personal_for(entries: list[ProjectEntry], root: str) -> ProjectEntry | None:
    """Your own settings that cover ``root``: the entry of the nearest folder."""
    matching = [e for e in entries if within(root, e.path)]
    return max(matching, key=lambda e: len(norm_path(e.path)), default=None)


def masker_weight(config: Config) -> int:
    """What a masker built from ``config`` holds, roughly (``_MASKER_BASE``): a project's, and a space's own on a
    server (``spaces.space_size``)."""
    return (
        _MASKER_BASE
        + _MASKER_PER_TERM * len(config.terms)
        + _MASKER_PER_RECOGNIZER * len(config.recognizers)
        + _MASKER_PER_PATTERN * (len(config.allow_patterns) + len(config.fields))
        + _MASKER_PER_VALUE * len(config.allow_values)
    )


def _copy_weight(config: Config) -> int:
    """What a copy of the settings ``config`` holds, roughly: its objects are its own, its texts shared."""
    objects = len(config.terms) + len(config.recognizers) + len(config.fields) + len(config.upstreams)
    texts = len(config.allow_values) + len(config.allow_patterns) + len(config.disabled_secret_rules)
    texts += len(config.data_tools) + len(config.data_files) + len(config.data_hosts)
    return _COPY_BASE + _COPY_PER_OBJECT * objects + _COPY_PER_TEXT * texts


# --- the registry -------------------------------------------------------------------------------------


@dataclass
class Project:
    id: str
    name: str
    root: str
    repo_file: Path | None
    repo: ProjectSettings | None  # what the repository file sets that applies
    personal: ProjectEntry | None
    notices: list[Notice]
    config: Config  # the rules in effect there
    scope: Scope | None  # None: the global rules apply as they are
    statuses: list[Status]  # recognizers as loaded (of the global rules, when scope is None)
    stamp: tuple[int, int] | None
    generation: int
    checked: float = 0.0
    last_seen: float = field(default_factory=time.time)
    # Your own settings for it lower protection against the global rules (Latet off there, say), as the runtime weighs
    # a change; and what applies then to a conversation whose folder no client reported to this process (on a server,
    # its project came from Valkey alone): the global rules and what the project only adds (None: the global rules).
    lowers: bool = False
    safe: Scope | None = None
    # What its masker weighs, not built or not kept, its space past its share (``Projects.past_share``): worked out
    # again once that fits. 0: none refused.
    unbuilt: int = 0
    # The tag of the bytes of its latet.yaml it was worked out from (``read_file``): what the dashboard's editor saves
    # over. "" with no file; None when the file there could not be read (nothing may be saved over it).
    tag: str | None = ""

    @property
    def enabled(self) -> bool:
        return self.personal.enabled if self.personal is not None else True

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "root": self.root,
            "repo_file": str(self.repo_file) if self.repo_file else None,
            "has_personal": self.personal is not None,
            "enabled": self.enabled,
            "profile": self.config.profile.value,
            "notice_count": len(self.notices),
            "last_seen": self.last_seen,
        }


# A change of the settings weighed on a snapshot of the repositories that is not complete (``RepositoryScan``): it
# neither applies nor waits for you, and says so in these words, and its reason for the dashboard.
UNCHECKED = (
    "the repositories Latet has seen could not all be read now (a file not readable, or not in time): a change of the "
    "settings that could lower their protection neither applies nor waits until they can; try again"
)
UNCHECKED_REASON = "repositories_unchecked"


class RepositoriesUnchecked(RuntimeError):
    """A change of the settings that could lower protection in a repository, weighed on an incomplete snapshot of them
    (``RepositoryScan.complete``): nothing applied, nothing opened for you, nothing kept as approved. Fixed words."""

    def __init__(self, words: str = UNCHECKED) -> None:
        super().__init__(words)


class RepositoryScan(tuple[tuple[str, ProjectSettings], ...]):
    """The repositories a change of the settings is weighed in, each folder with what its latet.yaml sets that applies,
    as read once (``Projects.repositories``): a snapshot, never changed. ``complete``: every repository remembered was
    read, or is known to have nothing to weigh now (not there, not a regular file, not valid). An incomplete one (a
    file not readable now, a check past its deadline, the list of them not readable) never counts as one with nothing
    in it: what a change would lower in a repository not read is not known, and the change neither applies nor waits on
    it (``Runtime.propose``)."""

    complete: bool

    def __new__(cls, items: Iterable[tuple[str, ProjectSettings]] = (), *, complete: bool = True) -> RepositoryScan:
        scan = super().__new__(cls, items)
        scan.complete = complete
        return scan


class SeenRepositories:
    """The repositories a computer has seen with a latet.yaml of their own, remembered across the daemon's starts in its
    data folder (``store``), the most recent ``REPOSITORIES_KEPT``: what a change of the global settings is weighed in
    besides the projects of this process (``Projects.repositories``), so that one that lowers protection only where a
    repository's own file is stricter waits for you though the daemon has not seen that repository since it started
    (R3-4). The list is read again at every start and reload, and each folder's latet.yaml with it. Only its bounds let
    an entry go: one with nothing to weigh now (a drive away, a folder deleted, a branch without its latet.yaml checked
    out) stays, and is weighed again whenever it is back. Thread-safe: its lock is never held while another lock is
    waited for, nor while the files of the repositories are read (``weighed``)."""

    def __init__(self, store: LocalRepositories) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._folders: dict[str, str] = {}  # by ``norm_path``, the least recent first
        self._touched: set[str] = set()  # made the most recent in this process already
        found = store.load()
        self._loaded = found is not None  # the list read at the start (None: there, and not readable then)
        self._add_older(found or [])

    def entries(self) -> tuple[list[str], bool]:
        """The folders remembered, the most recent first, the list read again first and merged as ``weighed`` merges
        it; and whether the list could be read now (``latet repositories``). Reads no repository."""
        with self._lock:  # read under the list's lock, as ``saw`` and ``forget`` read it (R25 N5)
            found = self._store.load()
            self._add_older(found or [])
            return [folder for _, folder in reversed(self._folders.items())], found is not None

    def forget(self, folder: str) -> Literal["forgotten", "unknown", "unreadable"]:
        """``folder`` taken out of the list and of this process's memory (``latet repositories forget``), under the
        list's lock: the list read again and merged with memory before it is written, as ``saw`` writes it, and never
        written over when it cannot be read now (``unreadable``). ``unknown``: not one the list or memory holds. The
        repository is remembered again when a conversation reaches it: until then no change is weighed in it."""
        key = norm_path(folder)
        with self._lock:
            found = self._store.load()
            if found is None:
                return "unreadable"
            if key not in self._folders and all(norm_path(other) != key for other in found):
                return "unknown"
            merged: dict[str, str] = {}  # the most recent first: the list's, then this process's
            for other in found:
                merged.setdefault(norm_path(other), other)
            for other, kept in reversed(self._folders.items()):
                merged.setdefault(other, kept)
            merged.pop(key, None)
            self._folders = dict(reversed(merged.items()))
            self._touched.discard(key)
            self._cut()
            return "forgotten" if self._save() else "unreadable"

    def saw(self, root: str) -> None:
        """A repository with a latet.yaml worked out in this process: the most recent, and written at once the first
        time in this process (its next workings out change nothing), merged first with the list read again (what
        another start wrote, or what could not be read before). A list that is there and cannot be read now is never
        written over: the folder stays in this process's memory, and is written with the next one seen once the list
        can be read. A folder no list may hold is not remembered."""
        if not rememberable(root):
            return
        key = norm_path(root)
        with self._lock:
            if key in self._touched:
                return
            self._touched.add(key)
            found = self._store.load()
            merged = {key: root}  # the most recent first: this folder, the list's, then this process's
            for folder in found or []:
                merged.setdefault(norm_path(folder), folder)
            for other, folder in reversed(self._folders.items()):
                merged.setdefault(other, folder)
            self._folders = dict(reversed(merged.items()))
            self._cut()
            if found is not None:
                self._save()

    def weighed(self, known: dict[str, ProjectSettings] | None = None, *, again: bool = True) -> RepositoryScan:
        """The repositories remembered, the most recent first, each with what its latet.yaml sets that applies, read
        again now by the process's one helper within its deadlines (``repository_io``), never under a lock; ``again``:
        the list read again first, and what it holds that this process does not know added to it (at a start, the
        list it read a moment ago is the one). One with nothing to weigh now stays, the others weighed all the same;
        how many there are is said in the log by why, never by folder. One whose file could not be read now, or not in
        time, makes the snapshot incomplete, as a list that could not be read does (``RepositoryScan.complete``).
        ``known``: what the projects of this process set, by ``norm_path`` of their folders: a repository read again
        as it is known there is left out, being weighed already. Never on the event loop."""
        with self._lock:  # the list read under its lock: a ``forget`` meanwhile is never merged back (R25 N5)
            found = self._store.load() if again else []
            self._add_older(found or [])
            folders = list(reversed(self._folders.items()))
            loaded = found is not None and (again or self._loaded)
        known = known or {}
        checked = repository_io.scan_folders([folder for _, folder in folders])
        out: list[tuple[str, ProjectSettings]] = []
        unweighed: Counter[str] = Counter()
        unknown: Counter[str] = Counter()
        for (key, folder), read in zip(folders, checked.results, strict=True):
            if read is None:
                unknown[checked.failure or "not checked"] += 1
            elif read.kind == "unavailable":
                unknown[read.why or "not readable now"] += 1
            elif read.kind != "settings" or read.settings is None:
                unweighed[read.why or read.kind] += 1
            elif known.get(key) != read.settings:
                out.append((folder, read.settings))
        if unweighed:
            why = ", ".join(f"{reason}: {count}" for reason, count in sorted(unweighed.items()))
            log.info(
                "%d of the %d repositories Latet has seen have nothing to weigh now (%s)",
                unweighed.total(), len(folders), why,
            )  # fmt: skip
        if unknown:
            why = ", ".join(f"{reason}: {count}" for reason, count in sorted(unknown.items()))
            log.warning(
                "%d of the %d repositories Latet has seen could not be read now (%s): a change of the settings neither "
                "applies nor waits until they can",
                unknown.total(), len(folders), why,
            )  # fmt: skip
        if not loaded:
            log.warning(
                "the list of the repositories Latet has seen could not be read now: a change of the settings neither "
                "applies nor waits until it can"
            )
        return RepositoryScan(out, complete=loaded and checked.complete and not unknown)

    def _add_older(self, folders: list[str]) -> None:
        """``folders``, the most recent first, that this process does not know: kept as less recent than every folder
        it knows (under the lock, or before the list is shared)."""
        new: dict[str, str] = {}
        for folder in folders:
            key = norm_path(folder)
            if key not in self._folders and key not in new:
                new[key] = folder
        if new:
            self._folders = {**dict(reversed(new.items())), **self._folders}
            self._cut()

    def _cut(self) -> None:
        """The most recent ``REPOSITORIES_KEPT`` kept (under the lock); one let go is remembered again when worked out
        again."""
        if len(self._folders) > REPOSITORIES_KEPT:
            items = list(self._folders.items())
            self._touched.difference_update(key for key, _ in items[:-REPOSITORIES_KEPT])
            self._folders = dict(items[-REPOSITORIES_KEPT:])

    def _save(self) -> bool:
        """The list written, the most recent first (under the lock): whether it was. One that cannot be written is said
        in the log by the file and the error's type, never by its folders: it stays in this process's memory, and is
        written whole at its next change."""
        try:
            self._store.save(list(reversed(self._folders.values())))
        except OSError as exc:
            log.warning("could not remember the repositories seen in %s: %s", self._store.path, described(exc))
            return False
        return True


# What a remembered repository's state is, as ``latet repositories`` says it: a state, and why in a few fixed words
# (a category, never an error's words or what the file holds).
_STATES: dict[str, tuple[str, str]] = {
    "settings": ("read", ""),
    "missing": ("nothing to weigh", "not there now"),
    "nonregular": ("nothing to weigh", "not a regular file, or too large"),
    "invalid": ("nothing to weigh", "not valid"),
    "unavailable": ("unavailable", "not readable now"),
}
_UNCHECKED_WHY = {"timeout": "not read in time", "busy": "another check under way"}


def repository_states(seen: SeenRepositories) -> dict[str, Any]:
    """Each repository remembered, the most recent first, with its state as the helper reads it now (within its
    deadlines, ``repository_io``) and why, in fixed categories; and whether the list itself could be read
    (``list``). What ``latet repositories`` shows: never what a file holds."""
    folders, readable = seen.entries()
    checked = repository_io.scan_folders(folders)
    out: list[dict[str, str]] = []
    for folder, read in zip(folders, checked.results, strict=True):
        if read is None:
            failure = checked.failure or "not checked"
            state, why = "unavailable", _UNCHECKED_WHY.get(failure, failure)
        else:
            state, why = _STATES[read.kind]
        out.append({"folder": folder, "state": state, "why": why})
    return {"repositories": out, "list": "readable" if readable else "unreadable"}


class Projects:
    """The projects sessions belong to, each with the rules in effect there (thread-safe)."""

    def __init__(
        self,
        config: Config,
        vault: Vault,
        identity: IdentityConfig | None = None,
        models: Path | None = None,
        files: bool = True,
        seen: SeenRepositories | None = None,
    ) -> None:
        self._config = config
        self._vault = vault
        self._identity = identity
        self._models = models
        self._files = files  # a project's own files are read: never on a server, which has none of them
        # A computer's: the repositories with a latet.yaml seen before the daemon started too, weighed with a change of
        # the settings (``repositories``); a repository worked out here joins them. None: none remembered (a server).
        self._seen = seen
        self._generation = 0
        self._lock = threading.RLock()
        self._by_cwd: LRU[str, str] = LRU(2_000)  # working directory → project id
        self._projects: dict[str, Project] = {}
        self._scopes: dict[str, tuple[Scope, list[Status]]] = {}  # identical rules share a masker
        # The rules of the projects with no file of their own, by the settings of yours that cover them: worked out once
        # and shared (on a server every folder a client names is a project, and each would keep a copy of the settings).
        self._shared: dict[str | None, tuple[Config, list[Notice]]] = {}
        self._masker_weights: dict[str, int] = {}  # each masker's weight, by its scope's key, while one holds it
        self._entry_ids: dict[str, str] | None = None  # your settings' folders by their projects' ids
        self._listed: dict[str, str] = {}  # the folders of the projects ``known`` listed last, by their ids
        self._weighed = 0  # what ``weight`` found last
        self._dirty = False  # something changed since
        # A computer's: the global settings you approved before the daemon started, when it started on others that
        # lower nothing it could weigh then (it knew no repository). A repository seen since is weighed against them at
        # its build: where its own latet.yaml is lowered, it is worked out under them, and ``on_held_back`` is told
        # (outside the lock), the runtime then running what you approved and opening the change. None: nothing kept.
        self.before: Config | None = None
        self.on_held_back: Callable[[], None] | None = None
        self._held_back = False  # a build used ``before`` since the runtime was last told
        # On a Latet server: the words of why its space, its projects weighing so much (with the masker they would
        # build), would be past its share of the instance's memory (``server.spaces``); None when it fits, as on a
        # computer always. Asked under the lock before a masker is built (``_scope``), so that builds at once are
        # weighed one after another. A folder whose masker is not built has its texts refused with these words, and is
        # worked out again once its masker may fit.
        self.past_share: Callable[[int], str | None] = lambda _weight: None

    def use(self, config: Config, identity: IdentityConfig | None) -> None:
        """New global rules: every project's rules are worked out again when next used."""
        with self._lock:
            self._config = config
            self._identity = identity
            self._generation += 1
            self._scopes.clear()
            self._shared.clear()
            self._entry_ids = None
            self._prune()  # what each project still holds until its next use stays weighed, and nothing else

    def attach(self, session: Session, cwd: Any) -> Project | None:
        """Give a session its project, from the working directory a client reported (to this process: what that
        project lowers applies to the session from now on, ``scope_for``)."""
        if not _a_folder(cwd):
            return None
        session.reported = cwd
        if session.project_id is not None and session.cwd == cwd:
            project = self.get(session.project_id)
            if project is not None:
                return project
        # A new folder; or one this process never resolved, the session's project having come with it from another
        # instance of a Latet server: resolved here.
        project = self.resolve(cwd)
        session.project_id, session.cwd = project.id, cwd
        return project

    def resolve(self, cwd: str) -> Project:
        """The project of a working directory (known from now on to the dashboard). Its folder looked for and its file
        read outside the lock (a share can make them wait), then worked out under it, without reading anything."""
        key = norm_path(cwd)
        with self._lock:
            pid = self._by_cwd.get(key)
            project = self._projects.get(pid) if pid else None
        if project is None:
            root, repo_file = self._root(cwd)
            read = read_file(repo_file) if repo_file is not None else None
            with self._lock:
                pid = project_id(root)
                self._by_cwd.put(key, pid)
                project = self._projects.get(pid)
                if project is None:  # not worked out meanwhile by another thread
                    project = self._load(root, repo_file, read)
                    self._projects[pid] = project
                    self._dirty = True
                    self._forget_oldest()
        project.last_seen = time.time()
        project = self._fresh(project)
        self._remember(project)
        self._tell_held_back()
        return project

    def get(self, pid: str) -> Project | None:
        """A project remembered, or one the dashboard lists (``known``) that no conversation has used, worked out now
        as it opens it: listed here, or one of your settings (on a server the list may come from another instance);
        None for any other id."""
        with self._lock:
            project = self._projects.get(pid)
            folder = None
            if project is None:
                folder = self._listed.get(pid)
                if folder is None:
                    if self._entry_ids is None:
                        self._entry_ids = {project_id(entry.path): entry.path for entry in self._config.projects}
                    folder = self._entry_ids.get(pid)
        if project is not None:
            project = self._fresh(project)
            self._remember(project)
        self._tell_held_back()
        if project is not None:
            return project
        if folder is None:
            return None
        project = self.resolve(folder)
        return project if project.id == pid else None  # on a computer its project may be the repository's

    def _remember(self, project: Project) -> None:
        """A project with a latet.yaml of its own remembered (``SeenRepositories.saw``): weighed with a change of the
        settings at the daemon's next starts too. Outside the lock (the list is read and written), and once a process:
        it is weighed meanwhile as a project of this process (``repositories``)."""
        if project.repo_file is not None and self._seen is not None:
            self._seen.saw(project.root)

    def _tell_held_back(self) -> None:
        """The runtime told, outside the lock, that a repository was worked out under the settings approved before the
        start (``before``): it then takes the lock of the settings, whose changes take this one."""
        if self._held_back:
            with self._lock:
                held, self._held_back = self._held_back, False
            if held and self.on_held_back is not None:
                self.on_held_back()

    def touch(self, pid: str) -> None:
        """Look at the project's file again at its next use (it was just written)."""
        with self._lock:
            project = self._projects.get(pid)
            if project is not None:
                project.checked = 0.0

    @property
    def seen(self) -> SeenRepositories | None:
        """The repositories this computer remembers (None on a server, which reads none)."""
        return self._seen

    @property
    def count(self) -> int:
        """How many projects it remembers (``PROJECTS_KEPT`` at most)."""
        return len(self._projects)

    @property
    def weight(self) -> int:
        """What its projects hold in memory, in bytes, roughly (a server weighs every space by it): each masker and each
        copy of the settings they keep, counted once, by its rules, and each project's own record. Never waits (the
        event loop asks, and a value kept is weighed by it): while another thread works out a project, what they
        weighed last, each masker kept since counted (``_scope``)."""
        if self._dirty and self._lock.acquire(blocking=False):
            try:
                self._dirty = False
                self._weighed = self._weigh()
            finally:
                self._lock.release()
        return self._weighed

    def peek(self, pid: str) -> Project | None:
        """A project as last worked out, without looking at its file again (cheap: for the event loop)."""
        return self._projects.get(pid)

    def repositories(self, *, again: bool = True) -> RepositoryScan:
        """The folders of the projects it remembers whose repository has a latet.yaml, with what the file sets that
        applies, as last read; and of the repositories seen before, the daemon's earlier runs included
        (``SeenRepositories``), each file read again now (never on a server, which reads none): what a change of the
        global settings is weighed in too (``protection.loosened``), so that a line of config.yaml that lowers
        protection only where a repository's own profile is stricter waits for you, as one that lowers it in a folder
        of your settings does, though the daemon has not seen that repository since it started. One snapshot, read
        outside every lock (``RepositoryScan``; ``again``: the list of those seen read again first, as at a reload);
        never on the event loop."""
        found = self._active()
        if self._seen is None:
            return RepositoryScan(found)
        remembered = self._seen.weighed({norm_path(root): repo for root, repo in found}, again=again)
        return RepositoryScan([*found, *remembered], complete=remembered.complete)

    def with_active(self, scan: RepositoryScan) -> RepositoryScan:
        """``scan`` with what the projects of this process set now, where it does not hold that already: a repository
        worked out since the snapshot was read (a conversation reached it meanwhile) is weighed as it is now, never left
        out. Reads nothing: for a change weighed under the settings' lock."""
        held = {(norm_path(root), repo.model_dump_json()) for root, repo in scan}
        extra = [(root, repo) for root, repo in self._active() if (norm_path(root), repo.model_dump_json()) not in held]
        return RepositoryScan([*scan, *extra], complete=scan.complete) if extra else scan

    def _active(self) -> list[tuple[str, ProjectSettings]]:
        """The folders of the projects of this process whose repository has a latet.yaml, with what it sets, as last
        read (under the lock: nothing read)."""
        with self._lock:
            return [(project.root, project.repo) for project in self._projects.values() if project.repo is not None]

    def scope_for(self, session: Session) -> Scope | None:
        """The rules of the session's project. One this process never resolved (on a Latet server, a conversation's
        project and folder come from another instance, through Valkey) is resolved by its folder. Valkey alone never
        lowers protection: what the project lowers applies only once a client has reported its folder to this process
        (``attach``), and until then the global rules apply with what the project only adds."""
        if session.project_id is None:
            return None
        project = self.get(session.project_id)
        if project is None and _a_folder(session.cwd):
            project = self.resolve(session.cwd)
        if project is None:
            return None
        if project.lowers and session.reported != session.cwd:
            return project.safe
        return project.scope

    def known(self) -> list[Project]:
        """Every project seen, and every one you have settings for. One whose rules changed since it was worked out,
        and one of your settings no conversation has used, is listed as it applies now, worked out without a masker or
        the lists of its rules (the list counts none of them) and kept by nobody: listing every folder of yours builds
        nothing and holds the masking path's lock for no time. ``get`` works one out whole."""
        with self._lock:
            config, generation = self._config, self._generation
            remembered = sorted(self._projects.values(), key=lambda p: -p.last_seen)
        slim = config.model_copy(update={key: [] for key in _LISTED_WITHOUT})
        listed: list[Project] = []
        for project in remembered:
            root, repo_file = project.root, project.repo_file
            if project.generation == generation and not self._changed_file(project):
                listed.append(project)
                continue
            if self._files:
                root, repo_file = self._root(project.root)
                if norm_path(root) != norm_path(project.root):  # its latet.yaml is gone: the folder stays the project
                    root, repo_file = project.root, None
            listed.append(self._view(config, slim, root, repo_file, project.last_seen))
        seen = {project.id for project in listed}
        for entry in config.projects:
            root, repo_file = self._root(entry.path)
            if project_id(root) not in seen:
                seen.add(project_id(root))
                listed.append(self._view(config, slim, root, repo_file, 0.0))
        with self._lock:
            self._listed = {project.id: project.root for project in listed}
        return listed

    # --- internals ---------------------------------------------------------------------------------

    def _root(self, cwd: str) -> tuple[str, Path | None]:
        """The project of a folder and its latet.yaml; on a server the folder as given, with no file read."""
        return project_root(cwd) if self._files else (cwd, None)

    def _changed_file(self, project: Project) -> bool:
        """Whether the project's latet.yaml is not the one it was worked out with any more (never on a server)."""
        if not self._files:
            return False
        found = find_project_file(Path(project.root)) if project.repo_file is None else project.repo_file
        return found != project.repo_file or _stamp(found) != project.stamp

    def _view(self, config: Config, slim: Config, root: str, repo_file: Path | None, last_seen: float) -> Project:
        """The project of ``root`` as the dashboard lists it, worked out from ``config`` without a masker, its rules
        those of ``slim`` (``config`` without its lists) with yours for the folder: kept by nobody."""
        read = read_file(repo_file) if repo_file is not None else FileRead(None, [], "")
        personal = personal_for(config.projects, root)
        effective, more = effective_config(slim, read.repo, personal)
        name = Path(root.replace("\\", "/").rstrip("/")).name or root
        return Project(
            project_id(root), name, root, repo_file, read.repo, personal, [*read.notices, *more], effective, None, [],
            read.stamp, self._generation, last_seen=last_seen, tag=read.tag,
        )  # fmt: skip

    def _forget_oldest(self) -> None:
        """At most ``PROJECTS_KEPT`` projects remembered: the least recently seen go, and with them the maskers and
        copies of the settings no other one uses (under the lock)."""
        over = len(self._projects) - PROJECTS_KEPT
        if over > 0:
            for pid in sorted(self._projects, key=lambda p: self._projects[p].last_seen)[:over]:
                del self._projects[pid]
            self._prune()

    def _prune(self) -> None:
        """The maskers and the copies of the settings kept for no project remembered, gone; the global rules' copy
        stays (under the lock)."""
        scopes = {s.key for p in self._projects.values() for s in (p.scope, p.safe) if s is not None}
        configs = {id(p.config) for p in self._projects.values()}
        for key in [key for key in self._scopes if key not in scopes]:
            del self._scopes[key]
        for folder in [f for f, (c, _) in self._shared.items() if f is not None and id(c) not in configs]:
            del self._shared[folder]
        self._masker_weights = {key: weight for key, weight in self._masker_weights.items() if key in scopes}
        self._dirty = True

    def _weigh(self) -> int:
        """``weight``, worked out (under the lock): what the caches keep and what the projects remembered hold, each
        masker and each copy of the settings once."""
        maskers = {scope.key for scope, _ in self._scopes.values()}
        copies = {id(config): config for config, _ in self._shared.values()}
        for project in self._projects.values():
            maskers.update(scope.key for scope in (project.scope, project.safe) if scope is not None)
            copies[id(project.config)] = project.config
        weights = self._masker_weights
        self._masker_weights = {key: weights.get(key, _MASKER_BASE) for key in maskers}  # those held, and no others
        held = sum(self._masker_weights.values())
        return held + sum(map(_copy_weight, copies.values())) + _PROJECT_BASE * len(self._projects)

    def _fresh(self, project: Project) -> Project:
        """The project as it applies now: worked out again when the settings changed since, its masker fits again, or
        its latet.yaml changed (looked at every ``_RECHECK`` seconds). Called outside the lock: the folder is looked at
        and the file read outside it, the project worked out again under it."""
        now = time.monotonic()
        with self._lock:
            project = self._projects.get(project.id, project)  # another thread may have worked it out again
            stale = project.generation != self._generation or (
                project.unbuilt > 0 and self.past_share(self._weigh() + project.unbuilt) is None  # its masker fits now
            )
            look = not stale and self._files and now - project.checked >= _RECHECK
            if look:
                project.checked = now
        if look:
            stale = self._changed_file(project)
        if not stale:
            return project
        root, repo_file = self._root(project.root)
        if norm_path(root) != norm_path(project.root):  # its latet.yaml is gone: the folder stays the project
            root, repo_file = project.root, None
        read = read_file(repo_file) if repo_file is not None else None
        with self._lock:
            current = self._projects.get(project.id)
            if current is not None and current is not project:  # worked out again meanwhile by another thread
                return current
            fresh = self._load(root, repo_file, read)
            fresh.last_seen = project.last_seen
            self._projects[project.id] = fresh
            self._dirty = True
            return fresh

    def _effective(self, repo: ProjectSettings | None, personal: ProjectEntry | None) -> tuple[Config, list[Notice]]:
        """``effective_config`` of the global rules; for a project with no file of its own, worked out once for every
        project your same settings cover, which share it (none changes it: it is read). Under the lock."""
        if repo is not None:
            return effective_config(self._config, repo, personal)
        key = norm_path(personal.path) if personal is not None else None
        found = self._shared.get(key)
        if found is None:
            found = self._shared[key] = effective_config(self._config, None, personal)
            self._dirty = True
        return found

    def _load(self, root: str, repo_file: Path | None, read: FileRead | None) -> Project:
        """The project of ``root`` worked out under the lock, from its latet.yaml read before it (``read``: None when it
        has none): nothing is read here."""
        if repo_file is None or read is None:
            read = FileRead(None, [], "")
        repo, notices = read.repo, read.notices
        personal = personal_for(self._config.projects, root)
        config, more = self._effective(repo, personal)
        if repo is not None and self.before is not None:
            # Settings the daemon started on, weighed here as a change of them is (``protection._repositories``): where
            # they lower the repository's own latet.yaml against those you approved before, those apply here, and the
            # runtime is told.
            approved, _ = effective_config(self.before, repo, personal_for(self.before.projects, root))
            if lowered_in_project(approved, config):
                config, self._held_back = approved, True
        notices = [*notices, *more]
        enabled = personal.enabled if personal is not None else True
        scope: Scope | None = None
        safe: Scope | None = None
        statuses: list[Status] = []
        changes = bool(repo is not None and repo.model_dump(exclude_defaults=True)) or bool(
            personal is not None and personal.model_dump(exclude_defaults=True, exclude={"path"})
        )
        # A repository file only adds protection: what lowers it is your own settings, weighed as a change to them is.
        base, _ = self._effective(None, None)
        lowers = personal is not None and (not enabled or bool(lowered_in_project(base, config)))
        unbuilt = 0
        if changes:
            try:
                scope, statuses = self._scope(config, enabled)
                if lowers:
                    adding, _ = effective_config(self._config, repo, personal, only_adding=True)
                    if adding.model_dump(mode="json") != base.model_dump(mode="json"):
                        safe, _ = self._scope(adding, True)
            except _NotBuilt as refused:
                # Its space is past its share: never the global rules in its place either; its texts are refused with
                # the share's words, until its masker fits (``_fresh``).
                notices.append(Notice("personal", "", "error", refused.words, PAST_SHARE_REASON))
                scope, statuses = _unbuilt(config, enabled, self._vault, _PastShare(refused.words)), []
                safe = _unbuilt(config, True, self._vault, _PastShare(refused.words)) if lowers else None
                unbuilt = refused.weight
            except Exception as exc:
                # Never the global rules in its place, which would leave its own terms in clear: its texts are refused
                # until its rules can be built (S-03), and the dashboard says why. By its type, and naming no folder: a
                # client reports it, and a project's id is a hash of it that a list of usual folders gives back.
                log.error("could not build the rules of a project (%s): its texts are refused", type(exc).__name__)
                notices.append(Notice("personal", "", "error", UNBUILT, UNBUILT_REASON))
                scope, statuses = _unbuilt(config, enabled, self._vault), []
                safe = _unbuilt(config, True, self._vault) if lowers else None
        name = Path(root.replace("\\", "/").rstrip("/")).name or root
        return Project(
            project_id(root), name, root, repo_file, repo, personal, notices, config, scope, statuses,
            read.stamp, self._generation, time.monotonic(), lowers=lowers, safe=safe, unbuilt=unbuilt, tag=read.tag,
        )  # fmt: skip

    def _scope(self, config: Config, enabled: bool) -> tuple[Scope, list[Status]]:
        dumped = json.dumps(config.model_dump(mode="json"), sort_keys=True, default=str)
        key = hashlib.sha256(f"{self._generation}|{enabled}|{dumped}".encode()).hexdigest()[:16]
        found = self._scopes.get(key)
        if found is None:
            # Weighed here, under the lock, every masker built before it counted (builds at once are weighed one after
            # another): the projects as they weigh now and this masker, by its rules before it is built, then whole,
            # its terms' matcher too, before it is kept. Past its space's share none is built, or kept.
            held, rules = self._weigh(), masker_weight(config)
            if (words := self.past_share(held + rules)) is not None:
                raise _NotBuilt(words, rules)
            # On a server (no files of its own: one of many spaces in its process) the terms within an RE2 budget sized
            # to them, weighed whole: what their automaton grows to as it works, which Python's count does not see.
            masker, statuses = build_masker(config, self._vault, self._identity, self._models, bounded=not self._files)
            weight = rules + sum(d.memory for d in masker.detectors if isinstance(d, TermsDetector))
            if (words := self.past_share(held + weight)) is not None:
                raise _NotBuilt(words, weight)  # built, and let go
            images = image_policy(config)
            found = (
                Scope(
                    key,
                    hashlib.sha256(f"{enabled}|{dumped}".encode()).hexdigest(),  # with no count: every instance's
                    masker,
                    enabled,
                    images,
                    config.dump_guard,
                    config.llm_review,
                    config.live_review,
                    ocr=config.ocr == "auto",
                ),
                statuses,
            )
            self._scopes[key] = found
            self._masker_weights[key] = weight
            # Counted at once in what ``weight`` answers while the lock is busy, as it stays through a burst of builds:
            # a value kept meanwhile is weighed against this masker too. Weighed whole again at the next read it frees.
            self._weighed, self._dirty = held + weight, True
        return found


# --- writing your own settings and the repository file ----------------------------------------------


def with_personal(config: Config, root: str, entry: ProjectEntry | None) -> Config:
    """``config`` with your settings for ``root`` replaced (``None`` removes them)."""
    out = config.model_copy(deep=True)
    out.projects = [e for e in out.projects if norm_path(e.path) != norm_path(root)]
    if entry is not None:
        out.projects.append(entry.model_copy(update={"path": root}))
    return out


def render_project_file(settings: ProjectSettings) -> str:
    data = {k: v for k, v in settings.model_dump(mode="json", exclude_defaults=True).items() if k in REPO_KEYS}
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True) if data else ""
    return FILE_HEADER + body


class ProjectFileUnreadable(OSError):
    """A project's latet.yaml that is there and was not read (``reason``: ``nonregular``, ``too_large``,
    ``unavailable``): no tag of it is known, and nothing may be saved over it. Fixed words, never the path."""

    def __init__(self, reason: ReadFailure) -> None:
        super().__init__(errno.EIO, _UNREAD[reason])
        self.reason = reason


def file_tag(path: Path) -> str:
    """What the dashboard read, to refuse a save over a file changed since: the tag of its bytes, read whole and bounded
    as a project's file is (``read_file``); "" when there is no file there (a save creates it). One there that cannot
    be read as a project's file raises ``ProjectFileUnreadable``: never a tag a save could match, "" least of all."""
    try:
        raw = read_regular_bytes(path, limit=MAX_FILE_BYTES, follow=True)
    except FileReadError as failure:
        if failure.reason == "missing":
            return ""
        raise ProjectFileUnreadable(failure.reason) from None
    return _tag(raw)


def write_project_file(path: Path, text: str) -> None:
    """Write latet.yaml at once: a reader never sees half of it."""
    write_atomic(path, text)
