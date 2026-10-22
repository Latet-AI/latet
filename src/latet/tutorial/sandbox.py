"""The tutorial's sandbox: Latet as it runs on your computer, with a folder, settings, a vault and a history of its
own, where the only model is the pretend one.

Tutorial mode exists only through these objects, never through a variable: the runtime is given the sandbox's fixed
root key (the same fakes for everyone and at every run: the chapters' texts and the screenshots rely on them), its
gateway the pretend model as its one route and the client that reaches it in-process, its local reviewer (in a
chapter that has one) the Tutorial Reviewer, reached in-process too, its text recognition (in a chapter that needs
it) a reader of your own models, and its dashboard the tutorial's routes. The sandbox is made in a folder of its
own only, never in, around or as your own data or settings folder; `latet tutorial` makes a temporary one and runs it
in `environment` (the code that reads its data folder or settings from the environment, not from the runtime, then
finds the sandbox's). Your models are not in it: the command makes a finder of them in your data folder first
(`user_reader`), and the sandbox calls it again each time it is asked what a chapter lacks (`missing`), as
`latet ocr install` may be run while the tutorial runs.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import platformdirs

from latet.api.rest import Dashboard
from latet.config import APP, Config, config_dir, config_path, data_dir, save_config
from latet.logline import described
from latet.runtime import Runtime, make_runtime
from latet.tutorial.agent import TutorialAgent
from latet.tutorial.api import TutorialApi
from latet.tutorial.chapters import Chapter, asset
from latet.tutorial.model import PretendModel, PretendUpstream
from latet.tutorial.progress import Progress
from latet.tutorial.reviewer import SETTINGS as REVIEWER
from latet.tutorial.reviewer import TutorialReviewer
from latet.tutorial.session import TutorialSession
from latet.vision.masking import ImageMasking

if TYPE_CHECKING:
    from latet.vision.ocr import Ocr

log = logging.getLogger(__name__)

ROOT_KEY: Final = bytes.fromhex("117df8ea0f13060b282aeebc06b7be5920a6358649f26527a79f2a43813213c3")
# The sandbox's own settings, under each chapter's: nobody's identity (yours would be found, and kept in its vault),
# no slow detector, the history kept whole (the page shows it), no local reviewer (a chapter with the Tutorial
# Reviewer turns it on), no Live Reviewer (a chapter turns it on).
BASE: Final[dict[str, Any]] = {
    "identity": {"enabled": False},
    "ner": "off",
    "ocr": "off",
    "history": "full",
    "llm_review": {"enabled": False},
    "live_review": {"enabled": False},
}
# Its folder: the runtime's data, its settings, the agent's project (a folder per chapter).
DATA: Final = "data"
CONFIG: Final = "config.yaml"
WORKSPACE: Final = "workspace"
Finder = Callable[[], "Ocr | None"]  # a text recognition reader, once its models are there (None: not yet)


class SandboxError(ValueError):
    """A folder the sandbox is not made in, and why."""


def environment(root: Path, port: int) -> dict[str, str | None]:
    """The environment the sandbox in ``root`` runs in (None: unset): its own data folder and settings, port and
    no keyring; none of your keys, tokens, addresses or reviewer's key."""
    return {
        "LATET_DATA_DIR": str(root / DATA),
        "LATET_CONFIG": str(root / CONFIG),
        "LATET_PORT": str(port),
        "LATET_KEYRING": "off",
        "LATET_ROOT_KEY": None,
        "LATET_ROOT_KEY_FILE": None,
        "LATET_BRIDGE_TOKEN": None,
        "LATET_HOST": None,
        "LATET_LLM_API_KEY": None,
    }


def _own_folders() -> list[Path]:
    """Your own Latet folders, where they are when no variable moves them."""
    return [Path(platformdirs.user_data_dir(APP, appauthor=False)), config_dir()]


def _check(root: Path) -> None:
    """Refuse a root in, around or as one of your own folders (the sandbox empties its vault and history, and its
    folder is deleted at the end), a folder of the sandbox that leads outside it, and an environment that names
    other folders than the sandbox's."""
    for own in _own_folders():
        own = own.resolve()
        if root == own or root.is_relative_to(own) or own.is_relative_to(root):
            raise SandboxError(f"{root} is in, around or as your own Latet folder ({own}): not a sandbox")
    for name in (DATA, CONFIG, WORKSPACE):
        if not (root / name).resolve().is_relative_to(root):
            raise SandboxError(f"{root / name} leads outside the sandbox's folder")
    if data_dir().resolve() != (root / DATA).resolve() or config_path().resolve() != (root / CONFIG).resolve():
        raise SandboxError(
            f"the environment names another data folder or settings than the sandbox's ({root / DATA}, "
            f"{root / CONFIG}): run it in `environment(root, port)`"
        )


def settings_for(chapter: Chapter, port: int) -> dict[str, Any]:
    """The sandbox's settings for ``chapter``: its own, the chapter's over them, and in a chapter with the Tutorial
    Reviewer the local reviewer on, the Tutorial Reviewer its server (what it masks by itself is the chapter's)."""
    settings = {**BASE, **chapter.settings, "port": port}
    if chapter.reviewer:
        settings["llm_review"] = {**chapter.settings.get("llm_review", {}), **REVIEWER}
    return settings


def user_reader(data: Path) -> Finder:
    """Your text recognition models, in your own data folder ``data`` (found before the sandbox's folders replace it):
    a reader once they are there, which `latet ocr install` may make so while the tutorial runs."""

    def find() -> Ocr | None:
        from latet.vision import models

        if not models.runtime_available() or not models.installed(data):
            return None
        from latet.vision.ocr import OnnxOcr

        return OnnxOcr(*models.folders(data))

    return find


class Sandbox:
    """The sandbox's folder, its runtime, the pretend model and agent, the Tutorial Reviewer, the finder of your text
    recognition models, and the tutorial's session."""

    def __init__(
        self,
        root: Path,
        port: int,
        runtime: Runtime,
        model: PretendModel,
        agent: TutorialAgent,
        reviewer: TutorialReviewer,
        reader: Finder | None = None,
    ) -> None:
        self.root = root
        self.port = port
        self.runtime = runtime
        self.model = model
        self.agent = agent
        self.reviewer = reviewer
        self._reader = reader
        self.session: TutorialSession

    @classmethod
    def create(
        cls, root: Path, port: int, *, progress: Progress, requested: str | None = None, reader: Finder | None = None
    ) -> Sandbox:
        """A sandbox in ``root`` for a server on ``port``: its settings written first (so that nobody's identity is
        found while its runtime is made), its runtime made with the fixed key, the pretend model its only route, and
        the Tutorial Reviewer the server of a local reviewer (given once the runtime is made: until a chapter that
        has the Tutorial Reviewer turns it on, the settings keep the local reviewer off).
        ``requested``: the chapter named on the command line; ``progress``: where the finished chapters are kept;
        ``reader``: finds a text recognition reader once your models are there (`user_reader`; none: no chapter that
        needs text recognition can start)."""
        root = root.resolve()
        _check(root)
        root.mkdir(parents=True, exist_ok=True)
        config = Config.model_validate({**BASE, "port": port})
        save_config(config, root / CONFIG)
        runtime = make_runtime(config, data=root / DATA, config_file=root / CONFIG, root_key=ROOT_KEY)
        try:
            model = PretendModel()
            gateway = runtime.gateway
            gateway.client = model.client
            gateway.fixed_routes = [PretendUpstream()]
            gateway.tutorial = True
            reviewer = TutorialReviewer()
            runtime.review_client = reviewer.client_for  # a chapter's local reviewer asks it, in-process
            sandbox = cls(root, port, runtime, model, TutorialAgent(root / WORKSPACE, port), reviewer, reader)
            sandbox.session = TutorialSession(sandbox, progress, requested)
            sandbox.agent.commands["latet config"] = sandbox.session.config_command
            gateway.dashboard = Dashboard(runtime, tutorial=TutorialApi(sandbox.session))
        except BaseException:
            runtime.close()  # its files can then be deleted
            raise
        return sandbox

    def missing(self, needs: Sequence[str]) -> list[str]:
        """What of ``needs`` (a chapter's) the sandbox lacks now: `ocr` until it has a reader that works. A reader is
        looked for again at each call while there is none (`latet ocr install` may come while the tutorial runs), and
        the one found is installed in the mediator (`mediator.vision`) for good: asking what is missing can give the
        sandbox its reader."""
        return [need for need in needs if need == "ocr" and not self._reads_images()]

    def _reads_images(self) -> bool:
        """Whether the sandbox reads images now: the mediator holds a reader whose models did not fail to load. While
        it holds none, the finder is asked for one and a reader found is put in the mediator (it is kept, and the
        models load once). A finder that raises, as a broken `ocr` extra makes it, is said in the log and finds none:
        the chapter then needs its models, and the menu does not fail."""
        mediator = self.runtime.mediator
        if mediator.vision is None and self._reader is not None:
            try:
                reader = self._reader()
            except Exception as exc:
                log.warning("your text recognition models could not be looked for: %s", described(exc))
                reader = None
            if reader is not None:
                mediator.vision = ImageMasking(reader, self.runtime.store)
        vision = mediator.vision
        return vision is not None and vision.available

    async def reset(self, chapter: Chapter) -> None:
        """Back to ``chapter``'s beginning, in this order: what waits for the user let go (a batch not sent, a
        change undone), every value forgotten, and what the local reviewer read (a run's answers, given with its
        guidance, are not the next run's), the history emptied, the agent's project made again with the chapter's
        files and pictures, the sandbox's settings with the chapter's over them (`settings_for`: the local reviewer
        on, in a chapter with the Tutorial Reviewer), the reader of a chapter that needs text recognition found again
        (a settings save racing the start may have written it away), what the Tutorial Reviewer finds in it, a new
        conversation, the pretend model as new."""
        rt = self.runtime
        approvals = rt.mediator.approvals
        for batch_id in list(approvals.batches):
            await approvals.decide(batch_id, False)
        for change_id in list(approvals.changes):
            # As Undo it: under the settings' lock, so an answer the user gives meanwhile is the only one, or none.
            await asyncio.to_thread(rt.decide_change, change_id, False)
        await asyncio.to_thread(self._forget)
        await asyncio.to_thread(rt.forget_reviews)
        await asyncio.to_thread(self._clear_history)
        self.agent.workspace = await asyncio.to_thread(self._workspace, chapter)
        config = Config.model_validate(settings_for(chapter, self.port))
        await asyncio.to_thread(rt.apply_config, config, True)
        if chapter.needs and rt.mediator.vision is None and not self.missing(chapter.needs):
            # A settings save racing this start wrote the reader back to None (`apply_config` keeps the one it read,
            # and `start` found it before): it is found again, and the models load now, as the chapter's `ocr: auto`
            # asks (the warm-up in `apply_config` saw none).
            await asyncio.to_thread(rt.warm_up)
        self.reviewer.expect(chapter.reviewer)
        self.agent.new_conversation(chapter.id)
        self.model.reset()

    def _forget(self) -> None:
        for entity in self.runtime.built.vault.entities():
            self.runtime.forget(entity.id)

    def _clear_history(self) -> None:
        rt = self.runtime
        if rt.recorder is not None:
            rt.recorder.drain(10)  # what the step before still had to record goes too
        if rt.exchanges is not None:
            rt.exchanges.clear()

    def _workspace(self, chapter: Chapter) -> Path:
        """The agent's project for ``chapter``, with only its files and pictures, written as they are (no newline
        translated)."""
        folder = self.root / WORKSPACE
        if folder.exists():
            shutil.rmtree(folder)
        project = folder / chapter.id
        project.mkdir(parents=True)
        for path, text in chapter.files.items():
            (project / path).parent.mkdir(parents=True, exist_ok=True)
            (project / path).write_bytes(text.encode("utf-8"))
        for path, name in chapter.pictures.items():
            (project / path).parent.mkdir(parents=True, exist_ok=True)
            (project / path).write_bytes(asset(name))
        return project

    def close(self) -> None:
        """The runtime closed: the sandbox's files can then be deleted (Windows refuses a file still open)."""
        self.runtime.close()
