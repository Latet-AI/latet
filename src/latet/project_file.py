"""A repository's latet.yaml read and worked out into what it sets that applies: the settings, never more.

Light on purpose: the daemon's helper that reads the repositories remembered (``repository_child``) imports it, and
nothing of the engine, the vault or the agents with it. ``projects`` re-exports what it names.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from latet.config import Config, ProjectSettings, _NoAliases
from latet.logline import described

log = logging.getLogger(__name__)

PROJECT_FILE = "latet.yaml"
MAX_FILE_BYTES = 256 * 1024
MAX_TERMS = 500
MAX_RECOGNIZERS = 100
# The YAML a project file may hold, counted in the parser's events (a mapping's start and end, each key and value): a
# full file of 500 terms and 100 recognizers, the most the lists keep, holds about 4,500, a quarter of it. Past it the
# file is not valid, whatever it holds, and nothing in it applies; its parsing stops there: the pure-Python parser takes
# seconds over 256 KiB of small mappings, near the helper's 2 s for one folder (R20, R25 N3).
MAX_YAML_EVENTS = 20_000
_TOO_MUCH_YAML = f"more than {MAX_YAML_EVENTS:,} YAML events, more than a project file holds: nothing in it applies"
_NOT_KEPT = "a value Latet cannot keep as it is (not text, a number past what JSON holds, not finite): left out"


class _TooMuchYaml(yaml.YAMLError):
    """A project file past ``MAX_YAML_EVENTS``."""


class _ProjectLoader(_NoAliases):
    """``config``'s loader (safe types, no aliases) that stops a file past ``MAX_YAML_EVENTS``."""

    def get_event(self) -> Any:
        self.events = getattr(self, "events", 0) + 1
        if self.events > MAX_YAML_EVENTS:
            raise _TooMuchYaml(_TOO_MUCH_YAML)
        return super().get_event()


def _load_project_yaml(text: str) -> Any:
    return yaml.load(text, Loader=_ProjectLoader)  # noqa: S506 - a SafeLoader


def _kept(key: str, settings: ProjectSettings) -> Any:
    """``key``'s value as JSON holds it (what the helper sends and the daemon reads back: the very settings on both
    paths); ``ValueError`` when JSON cannot hold it."""
    try:
        value = settings.model_dump(mode="json")[key]
        return json.loads(json.dumps(value, allow_nan=False))
    except (PydanticSerializationError, TypeError, RecursionError) as exc:
        raise ValueError(_NOT_KEPT) from exc


# What a repository file may set: protection it adds. Anything else in it is ignored, and said so.
REPO_KEYS = (
    "profile", "pii", "id_regions", "phone_regions", "include_private_ips", "ner", "terms", "recognizers",
    "policies", "data", "data_files", "data_hosts", "fields", "dump_guard", "images", "ocr", "llm_review",
)  # fmt: skip
_LOOSENING = {
    "allow_values": "values never to mask",
    "allow_patterns": "patterns of values never to mask",
    "disabled_secret_rules": "secret rules to turn off",
    "data_tools": "which tools strict data mode covers",
    "review_guidance": "guidance for the local reviewer",
    "review_images": "whether the local reviewer sees the images",
    "enabled": "Latet off",
    "path": "a project folder",
}


@dataclass(frozen=True, slots=True)
class Notice:
    """Something in a project's settings that is not applied (``ignored``, ``error``), or not as written (``changed``:
    a repository file's recognizer laid under a name and a type of its own). ``reason``: why, for one the dashboard
    words in its reader's language (its message is the daemon's English); ``detail``: what those words name (the name
    and the type a recognizer is in use under), never a value."""

    source: Literal["repo", "personal"]
    key: str
    level: Literal["ignored", "error", "changed"]
    message: str
    reason: str | None = None
    detail: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"source": self.source, "key": self.key, "level": self.level, "message": self.message}
        if self.reason is not None:
            out["reason"] = self.reason
        if self.detail is not None:
            out["detail"] = dict(self.detail)
        return out


def _problem(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    return "; ".join(e["msg"] for e in errors[:3])


def repo_settings(data: dict[str, Any]) -> tuple[ProjectSettings, list[Notice]]:
    """The settings of a repository file that add protection; the rest, as notices. Each kept as JSON holds it, so that
    the helper that reads the repositories remembered sends the very settings a conversation reads (R20); one JSON
    cannot hold (bytes that are not text, an integer past JSON's digits, a value that is not finite) is left out with
    an error notice, never a reason for the file to count as unreadable."""
    notices: list[Notice] = []
    kept: dict[str, Any] = {}
    for raw_key, value in data.items():
        key = str(raw_key)
        if key in REPO_KEYS:
            try:
                kept[key] = _kept(key, ProjectSettings.model_validate({key: value}))
            except ValidationError as exc:
                notices.append(Notice("repo", key, "error", _problem(exc)))
            except ValueError:  # what JSON cannot hold (``_kept``), or what the model refused to write as JSON
                notices.append(Notice("repo", key, "error", _NOT_KEPT))
        elif key in _LOOSENING:
            notices.append(
                Notice(
                    "repo",
                    key,
                    "ignored",
                    f"a repository file cannot set {_LOOSENING[key]}: put it in your own settings for the project",
                    "repo_cannot_set",
                )
            )
        elif key in Config.model_fields:
            said = "a setting of the whole of Latet: only in your config.yaml"
            notices.append(Notice("repo", key, "ignored", said, "repo_whole_latet"))
        else:
            notices.append(Notice("repo", key, "ignored", "not a setting Latet knows", "repo_unknown"))
    for key, limit in (("terms", MAX_TERMS), ("recognizers", MAX_RECOGNIZERS)):
        if isinstance(kept.get(key), list) and len(kept[key]) > limit:
            notices.append(Notice("repo", key, "error", f"more than {limit}: only the first {limit} are used"))
            kept[key] = kept[key][:limit]
    return ProjectSettings.model_validate(kept), notices


def _project_settings(raw: bytes) -> tuple[ProjectSettings | None, list[Notice]]:
    """What a repository file of these bytes sets that applies, and what is wrong with it, as notices."""
    if len(raw) > MAX_FILE_BYTES:
        return None, [Notice("repo", "", "error", f"larger than {MAX_FILE_BYTES // 1024} KB: not read")]
    try:
        data = _load_project_yaml(raw.decode("utf-8"))
    except _TooMuchYaml:
        return None, [Notice("repo", "", "error", f"not valid YAML: {_TOO_MUCH_YAML}")]
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        return None, [Notice("repo", "", "error", f"not valid YAML: {str(exc).splitlines()[0]}")]
    except RecursionError:  # nested past what Python reads: not valid alike, never an error of the request
        return None, [Notice("repo", "", "error", "not valid YAML: nested too deep")]
    if data is None:
        return ProjectSettings(), []
    if not isinstance(data, dict):
        return None, [Notice("repo", "", "error", "not a mapping of settings")]
    return repo_settings(data)


# What reading a remembered repository's latet.yaml came to. ``settings``: read, and what it sets that applies. Nothing
# to weigh in it now, and known so: ``missing`` (not there: a drive away, a branch without it checked out),
# ``nonregular`` (not a regular file, or larger than a project file may be), ``invalid`` (not valid). ``unavailable``:
# not known now (a refused permission, a sharing violation, a share that does not answer): never weighed as nothing,
# its check incomplete.
RememberedKind = Literal["settings", "missing", "nonregular", "invalid", "unavailable"]
REMEMBERED_KINDS: tuple[RememberedKind, ...] = ("settings", "missing", "nonregular", "invalid", "unavailable")


@dataclass(frozen=True, slots=True)
class Remembered:
    """A remembered repository's latet.yaml as read now (``read_remembered``). ``why``: when there is nothing to weigh,
    why, by an error's type or in a few fixed words, never the folder."""

    kind: RememberedKind
    settings: ProjectSettings | None = None
    why: str = ""


def read_remembered(
    folder: str,
    own_file: Callable[..., bytes | None],
    parse: Callable[[bytes], tuple[ProjectSettings | None, list[Notice]]] = _project_settings,
) -> Remembered:
    """What the latet.yaml of a repository remembered from before sets that applies, read again now through any link,
    as a conversation reaching it is worked out (a link reaches nothing an entry could not name, and a repository file
    only adds protection), by ``own_file`` (``fileio.own_file``: bounded, a regular file only, never waiting on a pipe)
    and ``parse``. Raises nothing: one entry never stops the others being weighed. Not knowing now (``unavailable``) is
    never taken for nothing to weigh: whoever weighs it holds back what it could lower."""
    try:
        raw = own_file(Path(folder) / PROJECT_FILE, MAX_FILE_BYTES, follow=True)
    except (FileNotFoundError, NotADirectoryError) as exc:  # not there now: a drive away, a folder deleted
        return Remembered("missing", why=described(exc))
    except (OSError, ValueError) as exc:  # there, maybe, and not readable now
        return Remembered("unavailable", why=described(exc))
    if raw is None:
        return Remembered("nonregular", why="not a regular file, or too large")
    try:
        settings, _ = parse(raw)
    except MemoryError as exc:  # never what the file holds: what reading it took
        return Remembered("unavailable", why=described(exc))
    except Exception as exc:  # which it never raises: a hostile file must not stop a start, and is said
        log.warning("a repository Latet has seen could not be read (%s): nothing to weigh in it", described(exc))
        return Remembered("invalid", why=described(exc))
    return Remembered("settings", settings) if settings is not None else Remembered("invalid", why="not valid")
