"""The tutorial's chapters: one YAML file each, shipped in the package and checked when it is loaded.

A chapter holds its invented values, the files of the agent's project, the settings it starts from, and its
steps: what the agent sends, what the pretend model answers (its turns), what the dashboard shows, what a "do"
step waits for, and the checks the tests run after each step. `{{value:NAME}}` is one of the chapter's values,
put in place when the chapter is loaded; `{{received:NAME}}` is what the model received where a pattern of
`received` matched, filled in only once the model has received it. A chapter with the Tutorial Reviewer (the local
reviewer, simulated) also says what it finds (`reviewer`): values of the texts it reads, each with its category.
The agent's project can hold pictures as well as files (`pictures`: a path of the project → an image the package ships
in `latet/tutorial/assets/`, by the name it has there and of the type the path says), and a chapter says what it needs
of this computer (`needs`: text recognition).

A chapter's file is English, and the chapter itself: its steps, values, files and checks. A language adds its texts
only, in `chapters/<lang>/<chapter>.yaml` (by step, and by the model's turns), each with the placeholders of its
English text; whatever it leaves out stays English, and so does a chapter it has no file for.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator, Mapping
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath
from typing import Any, ClassVar, Final, Literal, NoReturn, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from latet.config import Config, ReviewCategory, load_yaml

# The chapters, in the menu's order.
ORDER: Final = ("basics", "terms", "false-positives", "stopped", "live", "settings", "data", "llm", "images")
NEEDS: Final = ("ocr",)  # what a chapter may need of this computer: text recognition's models
# The pictures a chapter's project can hold, and the type each is sent as (Read sends the image itself).
IMAGE_TYPES: Final = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
}  # fmt: skip
LAST: Final = "last"  # in `show.exchange` and in checks: the step's last exchange
WAITS: Final = ("pin", "term", "allow", "approve", "change", "fields", "guidance")
CHECKS: Final = (
    "model_received",
    "received",
    "file",
    "exchange",
    "agent_got",
    "agent_error",
    "rules",
    "shown",
    "setting",
)
# What the sandbox keeps for itself: its port and only route, no folder outside it, nobody's identity, no reviewer of
# its own choosing (a chapter with the Tutorial Reviewer sets two things of `llm_review`: REVIEWER_SETTINGS).
SANDBOX_OWNS: Final = ("port", "upstreams", "capture_dir", "projects", "identity", "llm_review")
# What a chapter with the Tutorial Reviewer sets of the local reviewer: the sandbox gives its server.
REVIEWER_SETTINGS: Final = ("auto_mask", "inline")
_TOOL_INPUTS: Final = {"Read": ("file_path",), "Write": ("file_path", "content"), "Bash": ("command",)}
_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}")
_NAMED = re.compile(r"(value|received):([A-Za-z_][A-Za-z0-9_]*)")
_RECEIVED_ONLY = "the model knows only what it received: use {{received:NAME}}"
# The names Windows keeps for its devices, whatever follows a dot (`nul.txt` is NUL): reading CON waits for the
# console, writing NUL writes nowhere. COM0 and LPT0 too, as Windows' own list has them.
_DEVICES: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{port}{digit}" for port in ("COM", "LPT") for digit in "0123456789¹²³"}
)
_NOT_IN_A_NAME: Final = frozenset('<>:"|?*\\/') | {chr(code) for code in range(32)}
ENGLISH: Final = "en"
# What a language's file holds: a chapter's texts, by where they are in it; the rest of the chapter is English.
_LANGUAGE_TEXTS: Final = ("title", "learn", "steps", "end")
_STEP_TEXTS: Final = (
    "title", "explain", "prompt", "button", "says", "history", "hint", "otherwise", "turns", "guidance",
)  # fmt: skip
_OTHERWISE_TEXTS: Final = ("in_clear", "part_in_clear", "not_sent", "approved")
_TURN_TEXTS: Final = ("text", "approved")
_END_TEXTS: Final = ("learned", "where")


class ChapterError(ValueError):
    """A chapter that cannot be used, and why."""


class NotReceived(LookupError):
    """A `{{received:NAME}}` the model has not received (yet)."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def relative(path: str) -> bool:
    """A path inside the project's folder: relative, `/` between folders, no `..` or `.`, no drive or `\\`; and a
    name each part may have on every computer: no Windows device (`CON`, `aux.txt`), no trailing dot or space
    (Windows drops them: `.. ` is `..` there), nothing a file name cannot hold."""
    if not path or path != path.strip() or path.startswith("/"):
        return False
    return all(_name(part) for part in path.split("/"))


def _name(part: str) -> bool:
    if not part or part[-1] in ". " or not _NOT_IN_A_NAME.isdisjoint(part):
        return False
    return part.partition(".")[0].rstrip(" ").upper() not in _DEVICES


def render(text: str, received: Mapping[str, str]) -> str:
    """``text`` with what the model received in place of its `{{received:NAME}}` placeholders."""

    def one(found: re.Match[str]) -> str:
        named = _NAMED.fullmatch(found.group(1))
        if named is None or named.group(1) != "received":
            return found.group(0)
        if not received.get(named.group(2)):  # nothing found is not received: never an empty text in its place
            raise NotReceived(named.group(2))
        return received[named.group(2)]

    return _PLACEHOLDER.sub(one, text)


def render_json(value: Any, received: Mapping[str, str]) -> Any:
    """`render` on every text of a JSON value (a tool call's input)."""
    if isinstance(value, str):
        return render(value, received)
    if isinstance(value, list):
        return [render_json(item, received) for item in value]
    if isinstance(value, dict):
        return {key: render_json(item, received) for key, item in value.items()}
    return value


def _fill(value: Any, values: Mapping[str, str]) -> Any:
    """``value`` with the chapter's values in place of their `{{value:NAME}}` placeholders."""
    if isinstance(value, str):

        def one(found: re.Match[str]) -> str:
            named = _NAMED.fullmatch(found.group(1))
            return values[named.group(2)] if named is not None and named.group(1) == "value" else found.group(0)

        return _PLACEHOLDER.sub(one, value)
    if isinstance(value, list):
        return [_fill(item, values) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, values) for key, item in value.items()}
    return value


def _inside(path: str) -> str:
    if not relative(path):
        raise ValueError(f"`{path}` is not a path inside the project (relative, `/` between folders, no `..`)")
    return path


def _compiles(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"the pattern `{pattern}` does not compile: {exc}") from None


def _assets() -> Traversable:
    return resources.files("latet.tutorial") / "assets"


def _asset_names() -> set[str]:
    """The files of the assets folder, named as the package ships them. A name is looked up here and never opened to
    see whether it exists: a computer that ignores case (Windows, macOS) would open `PAYMENT.PNG` as `payment.png`."""
    return {entry.name for entry in _assets().iterdir() if entry.is_file()}


def asset(name: str) -> bytes:
    """A picture shipped with the tutorial (`latet/tutorial/assets/`), as it is. Only the folder's own files are read,
    each by the name the package ships it under: a path, `..` or the same name in another case is refused."""
    if name not in _asset_names():
        raise ValueError(f"no file `{name}` in the tutorial's assets (latet/tutorial/assets/)")
    return (_assets() / name).read_bytes()


def _suffix(path: str) -> str:
    """A file's type as its name says it: the suffix in lower case (`.png`)."""
    return PurePosixPath(path).suffix.lower()


class _Part(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_name=True, validate_by_alias=True)


class Capture(_Part):
    """Where the pretend model finds a value in what it received: the pattern's one group, in the latest text
    where it matches with something in that group (of what the user typed, of what the tools returned, or of
    any). Or, for what the user typed, a `value` of the chapter: found by the words around it in the text that holds
    it, in the chapter's language (the pattern is made of them when the chapter is loaded)."""

    pattern: str | None = None
    value: str | None = None
    where: Literal["any", "user", "tool_result"] = Field(default="any", alias="in")

    @field_validator("pattern")
    @classmethod
    def _one_group(cls, pattern: str | None) -> str | None:
        if pattern is None:
            return None
        groups = _compiles(pattern).groups
        if groups != 1:
            raise ValueError(f"the pattern `{pattern}` needs exactly one group (it has {groups})")
        return pattern

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.pattern is None) == (self.value is None):
            raise ValueError("a capture has exactly one of pattern, value")
        if self.value is not None and self.where != "user":
            raise ValueError("a capture by `value` finds it where the user typed it: `in: user`")
        return self


class ToolCall(_Part):
    """A tool the pretend model asks the agent to run, with its input."""

    name: Literal["Read", "Write", "Bash"]
    input: dict[str, Any]

    @model_validator(mode="after")
    def _arguments(self) -> Self:
        needed = _TOOL_INPUTS[self.name]
        if any(not isinstance(self.input.get(key), str) for key in needed):
            raise ValueError(f"{self.name} needs {', '.join(needed)} (a text each)")
        unknown = sorted(set(self.input) - set(needed))
        if unknown:
            raise ValueError(f"{self.name} takes {', '.join(needed)}, not {', '.join(unknown)}")
        if "file_path" in needed:
            _inside(self.input["file_path"])
        return self


class Find(_Part):
    """A value the Tutorial Reviewer finds, in any text it reads that holds it, and its category. With `guidance`, only
    when its instructions hold the user's guidance (a real model follows what the guidance says; this one only sees
    that there is some)."""

    value: str = Field(min_length=2)
    category: ReviewCategory
    guidance: bool = False


class Turn(_Part):
    """One answer of the pretend model: a text, a tool call, or both (the text first)."""

    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    text: str | None = None
    # The text instead, when the change to the settings the chapter waits for was approved (Undo it is the advice).
    approved: str | None = None
    tool: ToolCall | None = None

    @field_validator("id")
    @classmethod
    def _not_last(cls, value: str) -> str:
        if value == LAST:
            raise ValueError(f"`{LAST}` names a step's last exchange: give the turn another id")
        return value

    @model_validator(mode="after")
    def _something(self) -> Self:
        if self.text is None and self.tool is None:
            raise ValueError(f"turn `{self.id}`: a turn has a text, a tool call, or both")
        if self.approved is not None and self.text is None:
            raise ValueError(f"turn `{self.id}`: `approved` is said instead of its text: give it a text")
        return self


class Said(_Part):
    """A turn of a conversation from before Latet was on: what the user typed, or what the model answered."""

    user: str | None = None
    model: str | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.user is None) == (self.model is None):
            raise ValueError(
                "a turn of the history is what the user typed (`user`) or what the model answered (`model`)"
            )
        return self


class AgentSetup(_Part):
    """What the agent does in a step besides sending its prompt."""

    says: list[str] = Field(default_factory=list)  # its own lines
    files: list[str] = Field(default_factory=list)  # the files its Read sends in this step
    auth: Literal["api-key", "subscription"] = "api-key"
    clear: bool = False  # a new conversation first, as `/clear` starts one
    expect: Literal["answer", "blocked"] = "answer"
    bash: dict[str, str] = Field(default_factory=dict)  # prepared outputs, by command
    # A conversation from before Latet was on, resumed: what was said in it, sent before the step's prompt as it
    # was said (values in clear included). The user's turn first, the model's last.
    history: list[Said] = Field(default_factory=list)

    @field_validator("history")
    @classmethod
    def _turns(cls, history: list[Said]) -> list[Said]:
        whose = ["user" if said.user is not None else "model" for said in history]
        if whose != ["user" if i % 2 == 0 else "model" for i in range(len(whose))] or whose[-1:] == ["user"]:
            raise ValueError("history: the user's turn first, then the model's, and so on: the model's last")
        return history

    @model_validator(mode="after")
    def _resumed(self) -> Self:
        if self.history and self.clear:
            raise ValueError("a conversation resumed (`history`) is not a new one (`clear`)")
        return self

    @field_validator("files")
    @classmethod
    def _files(cls, files: list[str]) -> list[str]:
        for path in files:
            _inside(path)
        return files

    @field_validator("bash")
    @classmethod
    def _commands(cls, bash: dict[str, str]) -> dict[str, str]:
        if any(not command.strip() for command in bash):
            raise ValueError("a prepared command is not empty")
        return bash


class Show(_Part):
    """What Latet's pane shows after a step, and the original values in it to point at. Besides an exchange
    (`why`: why it was stopped), the dashboard's own pieces: Rules → Terms, a command, the Live Reviewer's queue
    (`approvals`), the changes to the settings waiting (`changes`), the columns of an exchange's tool output
    (`columns`), the exchange with what the local reviewer found in it (`review`), the reviewer's guidance
    (`guidance`), an exchange's images, as they were and as they were sent, and the text read in them (`images`)."""

    component: Literal[
        "exchange", "why", "terms", "command", "approvals", "changes", "columns", "review", "guidance", "images", "none"
    ] = "exchange"
    exchange: str | None = None  # a turn of this step or an earlier one, or `last`
    part: Literal["request", "response"] | None = None
    highlight: list[str] = Field(default_factory=list)
    focus: Literal["values", "block", "why", "terms"] | None = None
    command: str | None = None
    snippet: str | None = None
    rules: str | None = None
    # `images`: a picture of the chapter's project (`pictures`), shown as it is on this computer above the image as it
    # was sent.
    original: str | None = None

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.component in ("exchange", "why", "columns", "review", "images") and self.exchange is None:
            raise ValueError(f"a `{self.component}` view names its exchange: a turn, or `{LAST}`")
        if self.component == "exchange" and self.part is None:
            raise ValueError("an exchange shows a request or a response (`part`)")
        if self.component == "command" and self.command is None:
            raise ValueError("a `command` view names its command")
        return self


class Wait(_Part):
    """What a `do` step waits for, done by the user in the real dashboard: exactly one of these. The sandbox
    checks each one as it is (the page sees a `pin`)."""

    pin: str | None = Field(default=None, min_length=1)  # this value opened (clicked)
    term: str | None = Field(default=None, min_length=1)  # a term of the rules finds this value
    allow: str | None = Field(default=None, min_length=1)  # this value marked Not sensitive
    # The Live Reviewer's batch that holds this value decided (the user is asked to mask it by hand, then approve):
    # the step's own request waits in it, so this one is met while the step runs.
    approve: str | None = Field(default=None, min_length=1)
    change: str | None = None  # the change to this setting (a key of config.yaml) the agent asked for, decided
    fields: list[str] | None = Field(default=None, min_length=1)  # these columns kept in clear (strict data mode)
    # The guidance saved: the page's editor holds this sentence, written in for the reader; any guidance meets it.
    guidance: str | None = Field(default=None, min_length=1)
    hint: str | None = None

    @field_validator("change")
    @classmethod
    def _setting(cls, key: str | None) -> str | None:
        if key is not None and key not in Config.model_fields:
            raise ValueError(f"wait.change: no setting `{key}`")
        return key

    @field_validator("fields")
    @classmethod
    def _columns(cls, names: list[str] | None) -> list[str] | None:
        for name in names or []:
            if not name.strip() or "*" in name:
                raise ValueError(f"wait.fields: `{name}` is not one column (a name, no `*`)")
        return names

    @model_validator(mode="after")
    def _one(self) -> Self:
        given = [kind for kind in WAITS if getattr(self, kind) is not None]
        if len(given) != 1:
            raise ValueError(f"a wait has exactly one of {', '.join(WAITS)} (here: {', '.join(given) or 'none'})")
        return self

    @property
    def kind(self) -> str:
        return next(kind for kind in WAITS if getattr(self, kind) is not None)

    @property
    def target(self) -> str | list[str]:
        return getattr(self, self.kind)


class Otherwise(_Part):
    """What a step says instead of its explanation, when it did not end as the chapter asks. A step whose request
    waits in the Live Reviewer: approved with the value to mask by hand still in clear (Approve sends it as it is),
    approved with a word of it in clear (part of it masked), or not sent (Don't send). A step at or after one that
    waits for a change to the settings: the change approved, where the chapter advises Undo it (`approved`)."""

    in_clear: str | None = None
    part_in_clear: str | None = None
    not_sent: str | None = None
    approved: str | None = None

    @model_validator(mode="after")
    def _something(self) -> Self:
        if not self.live and self.approved is None:
            raise ValueError(
                "`otherwise` says how the step ended: in_clear, part_in_clear, not_sent, approved, or more"
            )
        return self

    @property
    def live(self) -> bool:
        """It says how a wait in the Live Reviewer ended."""
        return self.in_clear is not None or self.part_in_clear is not None or self.not_sent is not None


class _Check(_Part):
    asks: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="after")
    def _says_something(self) -> Self:
        if all(getattr(self, name) in (None, []) for name in self.asks):
            raise ValueError(f"this check says nothing to check: give it {' or '.join(self.asks)}")
        return self


class ModelReceivedCheck(_Check):
    asks: ClassVar[tuple[str, ...]] = ("contains", "not_contains", "requests")
    contains: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)
    requests: int | None = Field(default=None, ge=0)
    scope: Literal["step", "run"] = Field(default="step", alias="in")


class ReceivedCheck(_Check):
    asks: ClassVar[tuple[str, ...]] = ("equals", "differs", "matches")
    name: str
    equals: str | None = None
    differs: str | None = Field(default=None, alias="not")
    matches: str | None = None

    @field_validator("matches")
    @classmethod
    def _pattern(cls, pattern: str | None) -> str | None:
        if pattern is not None:
            _compiles(pattern)
        return pattern


class FileCheck(_Check):
    asks: ClassVar[tuple[str, ...]] = ("contains", "not_contains", "exists")
    path: str
    contains: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)
    exists: bool | None = None

    @field_validator("path")
    @classmethod
    def _path(cls, path: str) -> str:
        return _inside(path)


class ExchangeCheck(_Check):
    asks: ClassVar[tuple[str, ...]] = (
        "mode",
        "blocked",
        "reason_contains",
        "masked_classes",
        "masked_types",
        "masked_rules",
        "flags",
        "no_project",
    )
    ref: str  # a turn of this step or an earlier one, or `last`
    mode: Literal["rewrite", "verify", "passthrough"] | None = None
    blocked: bool | None = None
    reason_contains: str | None = None
    masked_classes: list[str] = Field(default_factory=list)
    masked_types: list[str] = Field(default_factory=list)
    masked_rules: list[str] = Field(default_factory=list)  # the request's values were masked by these (`human:person`)
    flags: list[str] = Field(default_factory=list)
    no_project: bool | None = None


class AgentGotCheck(_Check):
    """What the agent got: a turn of the step's model as the model sent it, every fake in it restored (what the agent
    shows, in any language), and texts it holds or not (values, which are the same in every language)."""

    asks: ClassVar[tuple[str, ...]] = ("turn", "contains", "not_contains")
    turn: str | None = None
    contains: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)


class AgentErrorCheck(_Check):
    asks: ClassVar[tuple[str, ...]] = ("contains",)
    contains: str


class RulesCheck(_Check):
    asks: ClassVar[tuple[str, ...]] = ("term", "allowed", "masks")
    term: str | None = None  # a term of the rules finds it
    allowed: str | None = None  # it is marked Not sensitive
    masks: str | None = None  # the rules mask it


class ShownCheck(_Check):
    asks: ClassVar[tuple[str, ...]] = ("marked", "clear")
    marked: list[str] = Field(default_factory=list)
    clear: list[str] = Field(default_factory=list)


class SettingCheck(_Check):
    """A setting as the sandbox runs it, and as its config.yaml holds it: a change that lowers protection is written
    there at once, and runs only once it is approved."""

    asks: ClassVar[tuple[str, ...]] = ("running", "saved")
    key: str  # dotted, as `latet config get` takes it: `profile`, `live_review.enabled`
    running: Any = None
    saved: Any = None

    @field_validator("key")
    @classmethod
    def _setting(cls, key: str) -> str:
        if (top := key.split(".", 1)[0]) not in Config.model_fields:
            raise ValueError(f"setting: no setting `{top}`")
        return key


class Check(_Part):
    """What must hold after a step: exactly one of the kinds of `CHECKS`. Run by the tests, never by the tutorial."""

    model_received: ModelReceivedCheck | None = None
    received: ReceivedCheck | None = None
    file: FileCheck | None = None
    exchange: ExchangeCheck | None = None
    agent_got: AgentGotCheck | None = None
    agent_error: AgentErrorCheck | None = None
    rules: RulesCheck | None = None
    shown: ShownCheck | None = None
    setting: SettingCheck | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        given = [kind for kind in CHECKS if getattr(self, kind) is not None]
        if len(given) != 1:
            raise ValueError(f"a check is exactly one of {', '.join(CHECKS)} (here: {', '.join(given) or 'none'})")
        return self

    @property
    def kind(self) -> str:
        return next(kind for kind in CHECKS if getattr(self, kind) is not None)

    @property
    def spec(self) -> Any:
        return getattr(self, self.kind)


class Step(_Part):
    """One step: `watch` (the agent sends; the effect is shown), `do` (the user acts in the dashboard, and the
    agent may try again) or `read` (nothing runs). A `do` step's `look` is what it shows while it waits for the
    user (what to act on); `show` is what it shows once it ran."""

    kind: Literal["watch", "do", "read"]
    title: str
    explain: str
    otherwise: Otherwise | None = None
    prompt: str | None = None
    button: str | None = None
    agent: AgentSetup = Field(default_factory=AgentSetup)
    model: list[Turn] = Field(default_factory=list)
    show: Show | Literal["previous"] | None = None
    look: Show | Literal["previous"] | None = None
    wait: Wait | None = None
    checks: list[Check] = Field(default_factory=list)

    @property
    def sends(self) -> bool:
        """The agent sends a request in this step: the model answers it, or Latet stops it."""
        return bool(self.model) or self.agent.expect == "blocked"


class Where(_Part):
    label: str
    to: str  # a page of the dashboard (`/live`), or `exchange:<turn>`


class End(_Part):
    learned: list[str] = Field(min_length=3, max_length=3)
    where: list[Where] = Field(default_factory=list)
    next: str | None = None


class Chapter(_Part):
    id: str
    title: str
    minutes: int = Field(gt=0)
    phase: Literal[1, 2]
    learn: list[str] = Field(min_length=3, max_length=3)
    values: dict[str, str] = Field(default_factory=dict)  # invented, every one
    settings: dict[str, Any] = Field(default_factory=dict)  # over the sandbox's own
    files: dict[str, str] = Field(default_factory=dict)  # the agent's project: path → text
    # The agent's project: path → a picture of the assets (named as the package ships it, of the type its path says).
    pictures: dict[str, str] = Field(default_factory=dict)
    needs: list[Literal["ocr"]] = Field(default_factory=list)  # what this computer must have: text recognition
    received: dict[str, Capture] = Field(default_factory=dict)
    reviewer: list[Find] = Field(default_factory=list)  # what the Tutorial Reviewer finds: a chapter that has it
    steps: list[Step] = Field(min_length=1)
    end: End

    @field_validator("id")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in ORDER:
            raise ValueError(f"`{value}` is not one of the tutorial's chapters ({', '.join(ORDER)})")
        return value

    @field_validator("values")
    @classmethod
    def _values(cls, values: dict[str, str]) -> dict[str, str]:
        for name, value in values.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ValueError(f"`{name}`: a value's name is lowercase letters, digits and `_`")
            if not value or "{{" in value:
                raise ValueError(f"`{name}`: a value holds no placeholder, and is not empty")
        return values

    @field_validator("received")
    @classmethod
    def _names(cls, received: dict[str, Capture]) -> dict[str, Capture]:
        for name in received:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"`{name}`: a pattern's name is letters, digits and `_`")
        return received

    @field_validator("files")
    @classmethod
    def _paths(cls, files: dict[str, str]) -> dict[str, str]:
        for path in files:
            _inside(path)
        return files

    @field_validator("pictures")
    @classmethod
    def _pictures(cls, pictures: dict[str, str]) -> dict[str, str]:
        for path, name in pictures.items():
            _inside(path)
            suffix = _suffix(path)
            if suffix not in IMAGE_TYPES:
                raise ValueError(f"pictures: `{path}` is not a picture ({', '.join(IMAGE_TYPES)})")
            if name not in _asset_names():
                raise ValueError(f"pictures: no picture `{name}` in the tutorial's assets (latet/tutorial/assets/)")
            own = _suffix(name)
            if own != suffix:  # it is sent as the type its path says: the picture's own
                raise ValueError(
                    f"pictures: `{path}` is a {suffix}, its picture `{name}` a {own or 'file without a suffix'}: "
                    "the same type"
                )
        return pictures

    @field_validator("settings")
    @classmethod
    def _settings(cls, settings: dict[str, Any]) -> dict[str, Any]:
        for key in settings:
            # The sandbox's own; but for `llm_review`, which `_coherent` allows in a chapter with the Tutorial Reviewer.
            if key in SANDBOX_OWNS and key != "llm_review":
                raise ValueError(f"settings: `{key}` is the sandbox's own")
            if key not in Config.model_fields:
                raise ValueError(f"settings: no setting `{key}`")
        try:
            Config.model_validate(settings)
        except ValidationError as exc:
            raise ValueError(f"settings: {exc}") from None
        return settings

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        self._placeholders()
        if both := sorted(self.files.keys() & self.pictures.keys()):
            raise ValueError(f"`{both[0]}` is a file and a picture: one or the other")
        review = self.settings.get("llm_review")
        if review is not None:
            if not self.reviewer:
                raise ValueError(
                    "settings: `llm_review` is the sandbox's own, but in a chapter with the Tutorial Reviewer "
                    "(`reviewer`)"
                )
            if not isinstance(review, dict) or not set(review) <= set(REVIEWER_SETTINGS):
                raise ValueError(
                    f"settings.llm_review: only {', '.join(REVIEWER_SETTINGS)}: the sandbox gives the Tutorial "
                    "Reviewer's server"
                )
        read = [*self.files.values(), *(s.prompt or "" for s in self.steps)]
        read += [said.user or "" for s in self.steps for said in s.agent.history]
        for number, find in enumerate(self.reviewer, 1):
            if not any(find.value in text for text in read):
                raise ValueError(
                    f"reviewer {number}: `{find.value}` is in no file or prompt of the chapter: the reviewer would "
                    "find it nowhere"
                )
        for name, capture in self.received.items():
            if capture.value is not None and self._typed(capture.value) is None:
                raise ValueError(
                    f"received.{name}: no prompt holds {{{{value:{capture.value}}}}}: a capture by value finds it by "
                    "the words around it where the user typed it"
                )
        turns: set[str] = set()
        for step in self.steps:
            for turn in step.model:
                if turn.id in turns:
                    raise ValueError(f"two turns named `{turn.id}`: each turn of a chapter has its own")
                turns.add(turn.id)
        self._steps()
        nxt = self.end.next
        if nxt is not None and nxt not in ORDER:
            raise ValueError(f"end.next: no chapter `{nxt}`")
        if nxt == self.id:
            raise ValueError("end.next: the chapter itself")
        for where in self.end.where:
            if where.to.startswith("exchange:"):
                if where.to.removeprefix("exchange:") not in turns:
                    raise ValueError(f"end.where: no turn named `{where.to.removeprefix('exchange:')}`")
            elif not where.to.startswith("/"):
                raise ValueError(f"end.where: `{where.to}` is a page (`/live`) or `exchange:<turn>`")
        return self

    def _placeholders(self) -> None:
        """Each placeholder names something the chapter defines, where it can be known."""
        for path, text, key in _strings(self.model_dump(by_alias=True), ()):
            where = _where(path)
            if "{{" in _PLACEHOLDER.sub("", text):
                raise ValueError(f"{where}: a `{{{{` that is not a placeholder")
            for found in _PLACEHOLDER.finditer(text):
                named = _NAMED.fullmatch(found.group(1))
                if named is None:
                    raise ValueError(
                        f"{where}: `{found.group(0)}` is not a placeholder (`{{{{value:NAME}}}}` or "
                        "`{{received:NAME}}`)"
                    )
                if key:
                    raise ValueError(f"{where}: no placeholder in a key")
                kind, name = named.groups()
                allowed = _allowed(path)
                if kind not in allowed:
                    if not allowed:
                        reason = "no placeholder here"
                    elif kind == "received" and path[2:3] == ("checks",):
                        reason = "what the model received fills only the texts a check compares (" + _COMPARED + ")"
                    elif kind == "received" and path[2:3] == ("otherwise",):
                        reason = "what a step says when it did not end as written names nothing the model receives"
                    elif kind == "received":
                        reason = "only the model's turns, the explanations and the checks know what the model received"
                    else:
                        reason = _RECEIVED_ONLY
                    raise ValueError(f"{where}: {found.group(0)}: {reason}")
                if kind == "value" and name not in self.values:
                    raise ValueError(f"{where}: {found.group(0)}: no value named {name}")
                if kind == "received" and name not in self.received:
                    raise ValueError(f"{where}: {found.group(0)}: no `received` pattern named {name}")

    def _steps(self) -> None:
        known: set[str] = set()  # the turns of the steps so far, this one's too
        pending: ToolCall | None = None  # the tool call the last step that sent left unanswered
        changed = False  # a step so far, this one included, waits for a change to the settings
        for number, step in enumerate(self.steps, 1):
            name = f"step {number} ({step.title})"
            earlier = set(known)  # what a `do` step can show before it runs: what the steps before sent
            known |= {turn.id for turn in step.model}
            if step.kind == "read" and (
                step.prompt is not None or step.model or step.wait or step.checks or step.agent != AgentSetup()
            ):
                raise ValueError(f"{name}: a `read` step runs nothing: no prompt, agent, turns, wait or checks")
            if step.kind == "do" and step.wait is None:
                raise ValueError(f"{name}: a `do` step waits for something (`wait`)")
            if step.kind != "do" and step.wait is not None:
                raise ValueError(f"{name}: only a `do` step waits")
            if step.kind != "do" and step.look is not None:
                raise ValueError(f"{name}: only a `do` step has a `look` (what it shows while it waits)")
            if step.kind == "do" and "{{received:" in step.explain:
                raise ValueError(
                    f"{name}: a `do` step's explanation is shown before it runs too: it names nothing the model "
                    "receives ({{received:...}})"
                )
            live = step.otherwise is not None and step.otherwise.live
            if live and (step.wait is None or step.wait.kind != "approve"):
                raise ValueError(
                    f"{name}: only a step whose request waits in the Live Reviewer (`approve`) says how it ended there "
                    "(`otherwise`: in_clear, part_in_clear, not_sent)"
                )
            changed = changed or (step.wait is not None and step.wait.kind == "change")  # a change waited for, so far
            if not changed and (
                (step.otherwise is not None and step.otherwise.approved is not None)
                or any(turn.approved is not None for turn in step.model)
            ):
                raise ValueError(
                    f"{name}: `approved` says how a change to the settings ended: only at or after a step that waits "
                    "for one (`change`)"
                )
            if (
                step.wait is not None
                and step.wait.kind == "approve"
                and not (step.prompt is not None and step.model and step.button is not None)
            ):
                raise ValueError(
                    f"{name}: an `approve` wait is met while the step's own request waits: give it a prompt, "
                    "`model` turns and a button"
                )
            if step.look == "previous" and number == 1:
                raise ValueError(f"{name}: `look: previous` on the first step")
            if isinstance(step.look, Show) and step.look.exchange is not None and step.look.exchange not in earlier:
                raise ValueError(
                    f"{name}: a look names `{step.look.exchange}`, not a turn of an earlier step (it is shown "
                    "before the step runs)"
                )
            if step.kind == "watch" and not step.sends:
                raise ValueError(
                    f"{name}: a `watch` step sends a request: give it `model` turns (or `expect: blocked`)"
                )
            if step.agent.history and step.prompt is None:
                raise ValueError(f"{name}: a conversation resumed (`history`) goes on with the step's prompt")
            if step.agent.expect == "blocked" and step.model:
                raise ValueError(f"{name}: a request expected to be stopped gets no answer: no `model` turns")
            if step.prompt is not None and not step.sends:
                raise ValueError(f"{name}: a prompt is sent: give the step `model` turns (or `expect: blocked`)")
            for turn in step.model[:-1]:
                if turn.tool is None:
                    raise ValueError(
                        f"{name}: `{turn.id}` has no tool call, so the step ends there: only its last turn may "
                        "have none"
                    )
            if step.agent.clear or step.agent.history:  # another conversation: the call left before is not run
                pending = None
            if step.sends:
                if step.prompt is None and pending is None:
                    raise ValueError(f"{name}: nothing to send: no prompt, and no tool call left by the step before")
                # The tools run in this step: the one left by the step before, then each turn's but the last's.
                runs = [pending] if pending is not None else []
                runs += [turn.tool for turn in step.model[:-1] if turn.tool is not None]
                for call in runs:
                    if call.name == "Read" and call.input["file_path"] not in step.agent.files:
                        raise ValueError(f"{name}: reads `{call.input['file_path']}`, not one of its agent.files")
                pending = step.model[-1].tool if step.model else None
            if step.show == "previous":
                if number == 1:
                    raise ValueError(f"{name}: `show: previous` on the first step")
            elif step.show is not None and step.show.exchange is not None:
                _exchange(name, "show", step.show.exchange, known, step)
            for view in (step.show, step.look):  # a picture shown as it is on this computer is one of the chapter's
                if isinstance(view, Show) and view.original is not None and view.original not in self.pictures:
                    raise ValueError(f"{name}: shows `{view.original}`, not a picture of the chapter")
            own = {turn.id: turn for turn in step.model}
            for check in step.checks:
                if check.exchange is not None:
                    _exchange(name, "an exchange check", check.exchange.ref, known, step)
                if check.agent_got is not None and (said := check.agent_got.turn) is not None:
                    if said not in own:
                        raise ValueError(
                            f"{name}: an agent_got check names turn `{said}`, not one of this step's "
                            f"({', '.join(own) or 'none'})"
                        )
                    if own[said].text is None:
                        raise ValueError(
                            f"{name}: an agent_got check names turn `{said}`, which says nothing (a tool call only)"
                        )
                if check.received is not None and check.received.name not in self.received:
                    missing = check.received.name
                    raise ValueError(f"{name}: a check names {missing}: no `received` pattern named {missing}")

    def show_of(self, index: int) -> Show | None:
        """What step ``index`` shows: `previous` is what the step before it showed."""
        show = self.steps[index].show
        while show == "previous" and index > 0:
            index -= 1
            show = self.steps[index].show
        return show if isinstance(show, Show) else None

    def look_of(self, index: int) -> Show | None:
        """What step ``index`` shows before it runs (a `do` step, while it waits): its `look` (`previous`: what the
        step before showed); without one, what it shows once it ran."""
        look = self.steps[index].look
        if look is None:
            return self.show_of(index)
        if look == "previous":
            return self.show_of(index - 1) if index > 0 else None
        return look

    def _typed(self, value: str) -> str | None:
        """The pattern that finds the value ``value`` where the user typed it: the words between its placeholder and
        the placeholders beside it (or the text's ends), as they are, around its group, in the first text the user
        typed that holds it (a step's prompt, then what the user said in the conversation it resumes, step by step).
        None when no such text holds it."""
        mark = f"{{{{value:{value}}}}}"
        for step in self.steps:
            for typed in (step.prompt, *(said.user for said in step.agent.history)):
                if typed is None or mark not in typed:
                    continue
                at = typed.index(mark)
                before = [found.end() for found in _PLACEHOLDER.finditer(typed, 0, at)]
                after = _PLACEHOLDER.search(typed, at + len(mark))
                head = typed[before[-1] if before else 0 : at]
                tail = typed[at + len(mark) : after.start() if after else len(typed)]
                return re.escape(head) + ("(.+?)" if tail else "(.+)") + re.escape(tail)
        return None

    def rendered(self) -> Chapter:
        """The chapter with its values in place of their placeholders, and a capture by value made the pattern of the
        words around it (in the texts as they are, before the values are put in); what the model received stays."""
        data = self.model_dump(by_alias=True)
        for name, capture in self.received.items():
            if capture.value is not None:
                data["received"][name] = {"pattern": self._typed(capture.value), "in": capture.where}
        return Chapter.model_validate(_fill(data, self.values))


def _exchange(name: str, what: str, ref: str, known: set[str], step: Step) -> None:
    if ref == LAST:
        if not step.sends:
            raise ValueError(f"{name}: {what} names `{LAST}`, but this step sends nothing")
    elif ref not in known:
        raise ValueError(f"{name}: {what} names `{ref}`, not a turn of this step or an earlier one")


Location = tuple[str | int, ...]  # where a text is in a chapter: keys and indices


def _strings(value: Any, path: Location) -> Iterator[tuple[Location, str, bool]]:
    """Every text of a chapter, keys included (``True``), with where it is."""
    if isinstance(value, str):
        yield path, value, False
    elif isinstance(value, dict):
        for key, item in value.items():
            yield (*path, key), str(key), True
            yield from _strings(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, (*path, index))


def _where(path: Location) -> str:
    return "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in path).lstrip(".")


# The texts of the checks that `checks.evaluate` fills with what the model received (kind, key as written): the
# others (an exchange's reason, the rules, a pattern) it reads as they are.
_FILLED: Final = frozenset(
    {
        ("model_received", "contains"),
        ("model_received", "not_contains"),
        ("received", "equals"),
        ("received", "not"),
        ("file", "contains"),
        ("file", "not_contains"),
        ("agent_got", "contains"),
        ("agent_got", "not_contains"),
        ("agent_error", "contains"),
        ("shown", "marked"),
        ("shown", "clear"),
    }
)
_COMPARED: Final = ", ".join(sorted({key for _, key in _FILLED}))


def _allowed(path: Location) -> tuple[str, ...]:
    """The placeholders a text can hold, by where it is: the model's turns know only what it received, the
    explanations and the texts the checks compare also the values; what the user and the agent write, and what
    the other checks read, only the values."""
    if path[:1] == ("received",):
        return ()
    if len(path) > 2 and path[0] == "steps":
        if path[2] == "model":
            return ("received",)
        if path[2] == "explain" or (path[2] == "checks" and tuple(path[4:6]) in _FILLED):
            return ("value", "received")
    return ("value",)


def _folder() -> Traversable:
    return resources.files("latet.tutorial") / "chapters"


def languages() -> tuple[str, ...]:
    """English, then each language whose folder (`chapters/<lang>/`) holds a chapter's texts, sorted."""
    found = (
        entry.name
        for entry in _folder().iterdir()
        if entry.is_dir() and entry.name != ENGLISH and any(f.name.endswith(".yaml") for f in entry.iterdir())
    )
    return (ENGLISH, *sorted(found))


def _read(text: str) -> dict[str, Any]:
    try:
        data = load_yaml(text)
    except yaml.YAMLError as exc:
        raise ChapterError(f"not a chapter's YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ChapterError("a chapter is a mapping (id, title, steps...)")
    return data


def _checked(data: dict[str, Any]) -> Chapter:
    """A chapter from its data, checked, its values in place."""
    try:
        return Chapter.model_validate(data).rendered()
    except ValidationError as exc:
        raise ChapterError(str(exc)) from None


def parse(text: str) -> Chapter:
    """A chapter from its YAML, checked, its values in place."""
    return _checked(_read(text))


def _count(number: int, what: str) -> str:
    return f"{number} {what}" if number == 1 else f"{number} {what}s"


def _marks(text: str) -> set[str]:
    """The placeholders of a text, as written."""
    return {found.group(0) for found in _PLACEHOLDER.finditer(text)}


def _overlay(data: dict[str, Any], texts: Any, where: str) -> dict[str, Any]:
    """The English chapter's ``data`` with a language's ``texts`` in their places (a text left out stays English):
    each one where English has one, as many lines as English has, and the placeholders of its English text. ``where``:
    the language's file, which a refusal names, then the place in it."""
    merged = copy.deepcopy(data)
    texts = {} if texts is None else texts  # an empty file: nothing translated yet

    def refuse(why: str) -> NoReturn:
        raise ChapterError(f"{where}: {why}")

    def keys(given: Any, known: tuple[str, ...], place: str) -> dict[Any, Any]:
        if not isinstance(given, dict):
            refuse(f"{place}: a mapping of its texts ({', '.join(known)})")
        for key in given:
            if key not in known:
                refuse(f"{place}: `{key}` is not one of its texts ({', '.join(known)})")
        return given

    def missing(owner: str, what: str) -> NoReturn:
        refuse(f"{owner} has no {what} in English")

    def text(place: str, english: str, given: Any) -> str:
        if not isinstance(given, str):
            refuse(f"{place}: a text")
        if (differ := _marks(given) ^ _marks(english)) != set():
            refuse(f"{place}: {', '.join(sorted(differ))}: a text has the placeholders of its English one, as they are")
        return given

    def lines(place: str, english: list[str], given: Any, what: str = "line") -> list[str]:
        if not isinstance(given, list):
            refuse(f"{place}: a list, as in English")
        if len(given) != len(english):
            refuse(f"{place}: {_count(len(given), what)}, English has {len(english)}")
        return [text(f"{place} {n}", line, said) for n, (line, said) in enumerate(zip(english, given, strict=True), 1)]

    def step(owner: str, english: dict[str, Any], given: Any) -> None:
        given = keys(given, _STEP_TEXTS, owner)
        for key in ("title", "explain", "prompt", "button"):
            if key in given:
                if english[key] is None:
                    missing(owner, key)
                english[key] = text(f"{owner} {key}", english[key], given[key])
        agent = english["agent"]
        if "says" in given:
            agent["says"] = lines(f"{owner} says", agent["says"], given["says"])
        if "history" in given:
            history = given["history"]
            if not isinstance(history, list):
                refuse(f"{owner} history: a list, as in English")
            if len(history) != len(agent["history"]):
                refuse(f"{owner} history: {_count(len(history), 'turn')}, English has {len(agent['history'])}")
            for n, (said, line) in enumerate(zip(agent["history"], history, strict=True), 1):
                kind = "user" if said["user"] is not None else "model"
                if not isinstance(line, dict) or list(line) != [kind]:
                    refuse(f"{owner} history {n}: `{kind}`, as in English")
                said[kind] = text(f"{owner} history {n}", said[kind], line[kind])
        if "hint" in given:
            if english["wait"] is None or english["wait"]["hint"] is None:
                missing(owner, "hint")
            english["wait"]["hint"] = text(f"{owner} hint", english["wait"]["hint"], given["hint"])
        if "guidance" in given:
            if english["wait"] is None or english["wait"]["guidance"] is None:
                missing(owner, "guidance")
            english["wait"]["guidance"] = text(f"{owner} guidance", english["wait"]["guidance"], given["guidance"])
        if "otherwise" in given:
            for key, said in keys(given["otherwise"], _OTHERWISE_TEXTS, f"{owner} otherwise").items():
                if english["otherwise"] is None or english["otherwise"][key] is None:
                    missing(owner, f"otherwise {key}")
                english["otherwise"][key] = text(f"{owner} otherwise {key}", english["otherwise"][key], said)
        if "turns" in given:
            turns = {turn["id"]: turn for turn in english["model"]}
            if not isinstance(given["turns"], dict):
                refuse(f"{owner} turns: a mapping of the model's turns, by id")
            for tid, said in given["turns"].items():
                if tid not in turns:
                    refuse(f"{owner}: no turn `{tid}` (its turns: {', '.join(turns) or 'none'})")
                turn = turns[tid]
                for key, line in keys(said, _TURN_TEXTS, f"{owner} turn `{tid}`").items():
                    if turn[key] is None:
                        missing(f"{owner} turn `{tid}`", key)
                    turn[key] = text(f"{owner} turn `{tid}` {key}", turn[key], line)

    if not isinstance(texts, dict):
        refuse(f"a language's file is a mapping of texts ({', '.join(_LANGUAGE_TEXTS)})")
    for key in texts:
        if key not in _LANGUAGE_TEXTS:
            refuse(f"`{key}` is not one of a language's texts ({', '.join(_LANGUAGE_TEXTS)})")
    if "title" in texts:
        merged["title"] = text("title", merged["title"], texts["title"])
    if "learn" in texts:
        merged["learn"] = lines("learn", merged["learn"], texts["learn"])
    if "steps" in texts:
        steps = texts["steps"]
        if not isinstance(steps, list):
            refuse("steps: a list of the steps' texts, in order")
        if len(steps) > len(merged["steps"]):
            refuse(f"{_count(len(steps), 'step')}, the chapter has {len(merged['steps'])}")
        for number, (english, given) in enumerate(zip(merged["steps"], steps, strict=False), 1):
            step(f"step {number}", english, given)
    if "end" in texts:
        end = keys(texts["end"], _END_TEXTS, "end")
        if "learned" in end:
            merged["end"]["learned"] = lines("end learned", merged["end"]["learned"], end["learned"])
        if "where" in end:
            places = merged["end"]["where"]
            labels = lines("end where", [place["label"] for place in places], end["where"], "label")
            for place, label in zip(places, labels, strict=True):
                place["label"] = label
    return merged


def _texts_of(chapter_id: str, lang: str) -> Traversable | None:
    """The file of ``lang``'s texts for chapter ``chapter_id``; None for English, an unknown language, or a language
    with no file for it."""
    if lang == ENGLISH or lang not in languages():
        return None
    file = _folder() / lang / f"{chapter_id}.yaml"
    return file if file.is_file() else None


def load(chapter_id: str, lang: str = ENGLISH) -> Chapter:
    """The chapter ``chapter_id`` of the package, in ``lang``: its texts from `chapters/<lang>/<chapter>.yaml`, the rest
    from the English chapter, then checked as an English chapter is. English for an unknown language, and for a chapter
    the language has no file for."""
    if chapter_id not in ORDER:
        raise ChapterError(f"`{chapter_id}` is not one of the tutorial's chapters ({', '.join(ORDER)})")
    file = _folder() / f"{chapter_id}.yaml"
    if not file.is_file():
        raise ChapterError(f"the chapter `{chapter_id}` is not written yet")
    name = f"chapters/{chapter_id}.yaml"
    try:
        data = _read(file.read_text("utf-8"))
        chapter = _checked(data)  # English first, as it is: its mistakes are the English file's
    except ChapterError as exc:
        raise ChapterError(f"{name}: {exc}") from None
    if chapter.id != chapter_id:
        raise ChapterError(f"{name}: its id is `{chapter.id}`")
    if (texts := _texts_of(chapter_id, lang)) is None:
        return chapter
    where = f"chapters/{lang}/{chapter_id}.yaml"
    try:
        given = load_yaml(texts.read_text("utf-8"))
    except yaml.YAMLError as exc:
        raise ChapterError(f"{where}: not a language's YAML: {exc}") from None
    merged = _overlay(Chapter.model_validate(data).model_dump(by_alias=True), given, where)
    try:
        return _checked(merged)
    except ChapterError as exc:
        raise ChapterError(f"{where}: {exc}") from None


def load_all(lang: str = ENGLISH) -> dict[str, Chapter]:
    """Every chapter written so far, in the menu's order, in ``lang`` (English where it has no texts). A file that is
    no chapter's, in English or in the language, is refused."""
    names = {entry.name for entry in _folder().iterdir() if entry.name.endswith(".yaml")}
    stray = sorted(name for name in names if name.removesuffix(".yaml") not in ORDER)
    if lang != ENGLISH and lang in languages():
        given = (entry.name for entry in (_folder() / lang).iterdir() if entry.name.endswith(".yaml"))
        stray += sorted(f"{lang}/{name}" for name in given if name.removesuffix(".yaml") not in ORDER)
    if stray:
        raise ChapterError(f"not chapters of the tutorial (ORDER): {', '.join(stray)}")
    return {cid: load(cid, lang) for cid in ORDER if f"{cid}.yaml" in names}
