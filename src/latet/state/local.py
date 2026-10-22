"""latet.state's implementations on a computer: this process's memory, SQLite and files, as a Latet kept them before
the protocols. One process holds them, so what it keeps is the same for every request."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Iterator, MutableMapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from latet.config import TERM_TOO_LONG, Config, left_out, load_config, save_config, write_private_where_allowed
from latet.engine.detectors.terms import TERM_LONGEST
from latet.engine.pipeline import LRU, Session
from latet.fileio import own_file  # the list below is read by it; ``projects`` reads a project's file by it
from latet.logline import described
from latet.state.protocols import Left, StateBackends, Timing
from latet.storage.folders import make_private_folder

if TYPE_CHECKING:
    from latet.gateway.approvals import Batch, Change, Decision
    from latet.gateway.sigv4 import AwsCredentials

log = logging.getLogger(__name__)


class LocalJobLock:
    """A job runs in one thread at a time: the whole Latet is this process. ``seconds`` is for a server's lock, which
    ends by itself when its instance stops; here the lock ends with the job."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    @contextlib.contextmanager
    def hold(self, name: str, seconds: float) -> Iterator[bool]:
        with self._guard:
            lock = self._locks.setdefault(name, threading.Lock())
        mine = lock.acquire(blocking=False)
        try:
            yield mine
        finally:
            if mine:
                lock.release()


class LocalConversations:
    """The conversations of this process, as the engine kept them: the last 10,000, each the one object its requests
    change; what the model wrote, by its restored form; the conversation of each Responses API id."""

    def __init__(self, reverse: int = 50_000) -> None:
        self._sessions: LRU[str, Session] = LRU(10_000)
        self._lock = threading.Lock()  # one thread at a time looks a conversation up and makes it when it is new
        self._reverse: LRU[bytes, str] = LRU(reverse)
        self._responses: LRU[str, str] = LRU(50_000)

    def session(self, key: str) -> Session:
        # In one step: two threads asking for the same new conversation at once (the engine's and the bridge's, at
        # a Claude Code session's first request) get the one object, so what one shows it the other restores.
        with self._lock:
            found = self._sessions.get(key)
            if found is None:
                found = Session(key)
                self._sessions.put(key, found)
            return found

    def update(self, session: Session, shown: Iterable[str] = ()) -> None:
        """Nothing to write: the object every request of the conversation changes is the one kept."""

    def sessions(self) -> list[Session]:
        return self._sessions.values()

    def remember(self, key: bytes, masked: str) -> None:
        self._reverse.put(key, masked)

    def recall(self, key: bytes) -> str | None:
        return self._reverse.get(key)

    def name(self, response_id: str, key: str) -> None:
        self._responses.put(response_id, key)

    def named(self, response_id: str) -> str | None:
        return self._responses.get(response_id)


class LocalSentBlocks:
    """Which blocks each conversation recorded already, in this process: the last 200,000."""

    def __init__(self) -> None:
        self._sent: LRU[tuple[str, str], bool] = LRU(200_000)
        self._lock = threading.Lock()  # asking and counting the block as recorded are one step

    def first(self, session: str, block: str) -> bool:
        key = (session, block)
        with self._lock:
            new = self._sent.get(key) is None
            self._sent.put(key, True)
        return new


class LocalGiven:
    """A conversation's texts as they left, and what the final check saw leave, in this process's memory: the LRUs the
    mediator and the verifier kept before the protocol, with their sizes."""

    def __init__(self, texts: int = 50_000, blocks: int = 200_000) -> None:
        self._masked: LRU[bytes, Left] = LRU(texts)
        self._checked: LRU[tuple[str, bytes], bool] = LRU(blocks)

    def masked(self, key: bytes) -> Left | None:
        return self._masked.get(key)

    def keep(self, key: bytes, left: Left) -> None:
        self._masked.put(key, left)

    def checked(self, session: str, key: bytes) -> bool:
        return self._checked.get((session, key)) is not None

    def keep_checked(self, session: str, keys: Iterable[bytes]) -> None:
        for key in keys:
            self._checked.put((session, key), True)


class Latest(MutableMapping[str, str]):
    """How the latest changes to your settings ended, by id: the last ``keep`` written, the oldest forgotten as one
    more is written. One thread writes at a time (the runtime's lock); the event loop only reads."""

    def __init__(self, keep: int) -> None:
        self._keep = keep
        self._ended: dict[str, str] = {}  # in the order they were written

    def __getitem__(self, key: str) -> str:
        return self._ended[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._ended.setdefault(key, value)
        while len(self._ended) > self._keep:
            del self._ended[next(iter(self._ended))]

    def __delitem__(self, key: str) -> None:
        del self._ended[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._ended)

    def __len__(self) -> int:
        return len(self._ended)


# Seconds an answer nobody asks for is kept, by default: the plugin polls every few seconds while it waits, so past this
# it asks no more (as long as the gateway gives a batch nobody asks about: ``approvals.ABANDON_AFTER``).
ABANDON_AFTER = 60.0


class LocalApprovals:
    """What waits for you, in this process's memory: the batches with the future each request waits on, the
    plugin's answers, the changes to your settings and how they ended, the dashboards watching. ``abandon_after``: the
    seconds an answer nobody asks for is kept, given to it as a server's backend would be given it."""

    def __init__(self, abandon_after: float = ABANDON_AFTER) -> None:
        self.abandon_after = abandon_after
        self.batches: dict[str, Batch] = {}
        self.changes: MutableMapping[str, Change] = {}  # at most one: a newer change replaces the one waiting
        self.settled: MutableMapping[str, str] = Latest(100)  # how the latest changes ended, for a command that waits
        self._answers: dict[str, tuple[asyncio.Future[dict[str, Any]], Batch]] = {}  # the plugin's, by batch
        self._viewers = 0

    def open(self, change: Change) -> tuple[Change, list[str]]:
        """One settings proposal, under the runtime's lock; a kept proposal is never announced again."""
        from latet.runtime import digest

        for waiting in self.changes.values():
            if waiting.base == change.base and digest(waiting.config) == digest(change.config):
                return replace(waiting), []
        superseded = list(self.changes)
        for change_id in superseded:
            self.end(change_id, "superseded")
        self.changes[change.id] = change
        return change, superseded

    def end(self, change_id: str, state: str) -> bool:
        """The runtime lock covers the waiting check and removal; the first state is kept before removal."""
        if change_id not in self.changes:
            return False
        self.settled[change_id] = state
        del self.changes[change_id]
        return True

    def put(self, batch: Batch) -> None:
        """``batch`` waits, on the event loop (what ``store`` does here; not the backends' protocol's)."""
        loop = asyncio.get_running_loop()
        batch.future = loop.create_future()
        self.batches[batch.id] = batch
        self._forget_answered()
        if batch.polled:  # the plugin will ask for its answer
            self._answers[batch.id] = (loop.create_future(), batch)

    async def store(self, batch: Batch, announce: Callable[[], None] | None = None) -> None:
        """``put``, then ``announce``, both on the event loop: this process's memory costs it nothing to wait for."""
        self.put(batch)
        if announce is not None:
            announce()

    async def replace(self, batch_id: str, items: list[dict[str, Any]]) -> Batch | None:
        batch = self.batches.get(batch_id)
        if batch is None or batch.future.done():
            return None
        batch.items = items
        return batch

    def get(self, batch_id: str) -> Batch | None:
        return self.batches.get(batch_id)

    async def fetch(self, batch_id: str) -> Batch | None:
        return self.get(batch_id)  # this process's memory: nothing to read elsewhere

    def waiting(self) -> list[Batch]:
        return sorted(self.batches.values(), key=lambda b: b.created)

    async def listed(self) -> list[Batch]:
        return self.waiting()  # this process's memory: nothing to read elsewhere

    def timings(self) -> list[Timing]:
        return [Timing.of(batch) for batch in self.batches.values()]  # this process holds every batch waiting

    def settle(self, batch_id: str, decision: Decision) -> bool:
        batch = self.batches.pop(batch_id, None)
        if batch is None:
            return False
        if not batch.future.done():
            batch.future.set_result(decision)
        if batch_id in self._answers:
            self._forget_later()  # the plugin may never ask for its answer
        return True

    async def settle_all(self, decisions: list[tuple[str, Decision]], told: Callable[[str, Decision], None]) -> int:
        """``settle`` for each, on the event loop: this process's memory costs it nothing to wait for."""
        settled = 0
        for batch_id, decision in decisions:
            if self.settle(batch_id, decision):
                told(batch_id, decision)
                settled += 1
        return settled

    async def decision(self, batch: Batch, hold: float | None) -> Decision | None:
        try:
            return await asyncio.wait_for(asyncio.shield(batch.future), hold)
        except TimeoutError:
            return None

    def polled(self, batch: Batch, delta: int) -> None:
        batch.waiting += delta
        batch.seen = time.monotonic()

    def image(self, batch_id: str, item: int, sent: bool) -> tuple[str, str] | None:
        batch = self.batches.get(batch_id)
        work = batch.images.get(item) if batch is not None else None
        if work is None:
            return None
        return (work.ref.data(), work.ref.media_type) if sent else work.original

    async def fetch_image(self, batch_id: str, item: int, sent: bool) -> tuple[str, str] | None:
        return self.image(batch_id, item, sent)  # this process's memory: nothing to read elsewhere

    def give(self, batch_id: str, answer: dict[str, Any]) -> None:
        if batch_id in self.batches:  # the answer is what leaves, and only your decision lets it: keep nothing before
            raise ValueError("a batch that waits for you has no answer to give yet")
        slot = self._answers.get(batch_id)
        if slot is not None and not slot[0].done():
            slot[0].set_result(answer)

    async def answer(self, batch_id: str, hold: float) -> dict[str, Any] | None:
        self._forget_answered()
        slot = self._answers.get(batch_id)
        if slot is None:
            raise KeyError(batch_id)
        future, batch = slot
        self.polled(batch, 1)
        try:
            answer = await asyncio.wait_for(asyncio.shield(future), hold)
        except TimeoutError:
            return None
        finally:
            self.polled(batch, -1)
            self._forget_later()  # this poll may be the plugin's last
        # Handed over once: in the step that returns it the call is forgotten, with its batch and its original texts,
        # and a poll that comes later for the id finds nothing (the bridge answers 404).
        self._answers.pop(batch_id, None)
        return answer

    def viewers(self, delta: int = 0) -> int:
        self._viewers = max(self._viewers + delta, 0)
        return self._viewers

    def _forget_answered(self) -> None:
        """Answers nobody asks for any more: those of a batch that no longer waits, given or not (whoever should give
        one may never do it), once no poll is open on it and none began during ``abandon_after`` seconds."""
        now = time.monotonic()
        for batch_id, (_, batch) in list(self._answers.items()):
            if batch_id not in self.batches and not batch.waiting and now - batch.seen > self.abandon_after:
                del self._answers[batch_id]

    def _forget_later(self) -> None:
        """Sweep again once a slot left by this call can be past its time: a second after ``abandon_after``. A slot
        holds its batch's original texts, its images and your decision, and after a session's last call no other call
        comes to sweep it."""
        with contextlib.suppress(RuntimeError):  # no loop runs here (the daemon stops): the next call sweeps, if any
            asyncio.get_running_loop().call_later(self.abandon_after + 1, self._forget_answered)


CODE_TTL = 120.0  # a one-time code of `latet dashboard`
# A dashboard's session. A computer's store counts it from the session's last use (it slides at each request), a
# server's from its opening; the browser's cookie is set once, at sign-in, with this as its Max-Age, so in a browser a
# session ends twelve hours after sign-in either way.
SESSION_TTL = 12 * 3600.0


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class LocalSignIns:
    """The dashboard's sign-ins in this process's memory: one-time codes and sessions, each by the hash of its secret
    and for its subject ("" on a computer: you)."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._codes: dict[str, tuple[str, float]] = {}  # sha256(code) → subject, until when
        self._sessions: dict[str, tuple[str, float]] = {}  # sha256(session id) → subject, until when

    def issue_code(self, subject: str) -> str:
        now = self._clock()
        self._codes = {k: v for k, v in self._codes.items() if v[1] > now}
        code = secrets.token_urlsafe(24)
        self._codes[_hash(code)] = (subject, now + CODE_TTL)
        return code

    def redeem(self, code: str) -> str | None:
        found = self._codes.pop(_hash(code), None) if code else None
        return found[0] if found is not None and found[1] >= self._clock() else None

    def open(self, subject: str) -> str:
        now = self._clock()
        # Those past their end that nobody presented again (`subject_of` drops the ones presented) go here.
        self._sessions = {k: v for k, v in self._sessions.items() if v[1] >= now}
        sid = secrets.token_urlsafe(32)
        self._sessions[_hash(sid)] = (subject, now + SESSION_TTL)
        return sid

    def subject_of(self, sid: str) -> str | None:
        if not sid:
            return None
        key, now = _hash(sid), self._clock()
        found = self._sessions.get(key)
        if found is None or found[1] < now:
            self._sessions.pop(key, None)
            return None
        self._sessions[key] = (found[0], now + SESSION_TTL)  # sliding
        return found[0]

    def end(self, sid: str | None = None, subject: str | None = None) -> None:
        if sid:
            self._sessions.pop(_hash(sid), None)
        if subject is not None:
            self._sessions = {k: v for k, v in self._sessions.items() if v[0] != subject}
            self._codes = {k: v for k, v in self._codes.items() if v[0] != subject}


# The last settings you approved: a change that lowers protection against them waits for you.
REVIEWED_FILE = "config.reviewed.json"


class LocalSettings:
    """config.yaml (``config_file``; None: LATET_CONFIG, or the configuration folder's) and the settings you approved
    last, config.reviewed.json in ``data``."""

    def __init__(self, config_file: Path | None, data: Path) -> None:
        self.config_file = config_file
        self.data = data
        # What the last load left out of config.yaml (an allow pattern RE2 cannot compile), each by its place: a reload
        # says them in its answer.
        self.notices: list[str] = []

    def load(self) -> Config:
        notices: list[str] = []
        config = load_config(self.config_file, notices)
        self.notices = notices
        return config

    def save(self, config: Config) -> None:
        save_config(config, self.config_file)

    def reviewed(self) -> Config | None:
        path = self.data / REVIEWED_FILE
        try:
            data = json.loads(path.read_text("utf-8"))
            # Read as config.yaml is: an allow pattern that does not compile and a term too long (ones an older Latet
            # approved) are left out and said, so that the settings you approved still count.
            for notice in left_out(data):
                log.warning("%s: %s", path.name, notice)
            return Config.model_validate(data)
        except FileNotFoundError:
            return None  # none approved yet
        except (OSError, ValueError) as exc:
            # There, and not readable (cut short, edited by hand, a disk error): it counts as none all the same, and
            # the daemon starts with config.yaml. Said, because a config.yaml edited meanwhile then applies without
            # waiting for you: by the file and the error's type, never the error's words, which can quote the settings.
            unread = "the settings you approved last cannot be read (%s: %s): they count as none"
            log.warning(unread, path, described(exc))
            return None

    def save_reviewed(self, config: Config) -> None:
        make_private_folder(self.data)
        # For you alone, as the key beside it: it holds your terms, your identity and the values you let through. Where
        # the system refuses that it is written all the same: with no copy at all, a config.yaml edited while the
        # daemon is off would apply without waiting for you.
        text = json.dumps(config.model_dump(mode="json"), indent=1)
        write_private_where_allowed(self.data / REVIEWED_FILE, text)

    def prepare(self, config: Config, *, strict: bool = False) -> Config:
        """On a computer the settings apply as they are. Saved (``strict``), they hold no term longer than a term holds
        (``TERM_LONGEST``), which could leave no rule built at all: refused by its places, never quoted, as a change a
        server cannot keep is (``SettingsRefused``), whatever saves it (the dashboard, an import, a command)."""
        if strict:
            places = [f"terms.{i}" for i, term in enumerate(config.terms) if len(term.value) > TERM_LONGEST]
            for index, entry in enumerate(config.projects):
                places += [
                    f"projects.{index}.terms.{i}" for i, t in enumerate(entry.terms) if len(t.value) > TERM_LONGEST
                ]
            if places:
                from latet.server.shared import TermsTooLong  # here: the server's modules sit above this one

                raise TermsTooLong(places, TERM_TOO_LONG)
        return config


# The folders of the repositories this computer has seen with a latet.yaml of their own, the most recent first: a change
# of the settings is weighed in each of them, also once the daemon has started again (``projects.SeenRepositories``).
REPOSITORIES_FILE = "repositories.json"
REPOSITORIES_KEPT = 2_000  # the most recent kept
# What the file holds at most, in bytes: 2,000 folders of a usual length, fewer when they are longer. A larger one is
# not read.
REPOSITORIES_BYTES = 1024 * 1024
_FOLDER_LONGEST = 4096  # a working directory as a client reports one (``projects._a_folder``)


def rememberable(folder: Any) -> bool:
    """Whether ``folder`` is one a list of repositories may hold: a folder as a client reports one (a text, not blank,
    ``_FOLDER_LONGEST`` characters at most), absolute (read from no folder the daemon happens to run in), with no NUL
    (which no folder holds)."""
    return (
        isinstance(folder, str)
        and bool(folder.strip())
        and len(folder) <= _FOLDER_LONGEST
        and "\x00" not in folder
        and os.path.isabs(folder)
    )


class LocalRepositories:
    """The folders of the repositories this computer has seen with a latet.yaml of their own, in REPOSITORIES_FILE of
    ``data``: what ``projects.SeenRepositories`` remembers across the daemon's starts, so that a change of config.yaml
    that lowers protection only where a repository's own file is stricter waits for you though the daemon has not seen
    that repository since it started (R3-4). A security input: bounded, written for you alone, read without trusting
    it."""

    def __init__(self, data: Path) -> None:
        self.path = data / REPOSITORIES_FILE

    def load(self) -> list[str] | None:
        """The folders remembered, the most recent first: ``REPOSITORIES_KEPT`` at most, each one a list may hold
        (``rememberable``; any other entry is left out). Empty when the file is not there; empty too when it was read
        and is damaged (not such a list), which counts as none, as the settings you approved do
        (``LocalSettings.reviewed``), and which the next list written replaces. None when it is there and is not read:
        a sharing violation, a refused permission, not a file of its own (a link, a folder), larger than
        ``REPOSITORIES_BYTES``: unavailable, never empty, and nothing may be written over it then (R24 M3). Either is
        said in the log by the file and why, never by what it holds, which names your projects' folders."""
        unread = "the repositories Latet has seen cannot be read from %s (%s): none counts"
        left = "the repositories Latet has seen cannot be read from %s (%s) now, and the file is left as it is"
        try:
            raw = own_file(self.path, REPOSITORIES_BYTES)
        except FileNotFoundError:
            return []  # none seen yet
        except OSError as exc:
            log.warning(left, self.path, described(exc))
            return None
        if raw is None:
            log.warning(left, self.path, "not a file of its own, or larger than such a list can be")
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):  # not text, not JSON, nested past what Python reads: never its words
            log.warning(unread, self.path, "not a list of folders")
            return []
        folders = data.get("repositories") if isinstance(data, dict) else None
        if not isinstance(folders, list):
            log.warning(unread, self.path, "not a list of folders")
            return []
        return [folder for folder in folders if rememberable(folder)][:REPOSITORIES_KEPT]

    def save(self, folders: Iterable[str]) -> None:
        """Remember ``folders``, the most recent first: the first ``REPOSITORIES_KEPT`` that fit in
        ``REPOSITORIES_BYTES``, written whole or not at all, for you alone where the system allows it (they name your
        projects' folders). As the settings you approved are: worth more kept than kept private. Raises ``OSError`` when
        it cannot write them. Only over a list just read, one that is not there or one that is damaged
        (``projects.SeenRepositories.saw``)."""
        kept: list[str] = []
        size = 32  # the object around the list, its key and its brackets
        for folder in folders:
            line = len(json.dumps(folder)) + 4  # an entry: its indent, its quotes, a comma and its end of line
            if len(kept) == REPOSITORIES_KEPT or size + line > REPOSITORIES_BYTES:
                break
            kept.append(folder)
            size += line
        make_private_folder(self.path.parent)
        write_private_where_allowed(self.path, json.dumps({"repositories": kept}, indent=1))


class LocalAwsSessions:
    """The AWS credentials `latet claude` handed over for its sessions, in this process's memory: never written."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._sessions: dict[str, AwsCredentials] = {}

    def add_session(self, credentials: AwsCredentials) -> str:
        session = secrets.token_hex(16)
        self._sessions[session] = credentials
        return session

    def session(self, session: str) -> AwsCredentials:
        from latet.gateway.aws import AwsError  # here: latet.gateway.aws imports this module

        credentials = self._sessions.get(session)
        if credentials is not None and credentials.expires is not None and credentials.expires <= self._clock():
            del self._sessions[session]
            credentials = None
        if credentials is None:
            raise AwsError(
                "aws-session-gone",
                "the AWS credentials of this session are gone (they ended, or Latet restarted): start "
                "`latet claude` again",
            )
        return credentials

    def has_session(self, session: str) -> bool:
        """Whether a session's credentials are still here: not dropped, not past their end (those past their end
        leave this memory when asked about, as when asked for)."""
        from latet.gateway.aws import AwsError

        try:
            self.session(session)
        except AwsError:
            return False
        return True

    def drop_session(self, session: str) -> bool:
        return self._sessions.pop(session, None) is not None


def local_backends(data: Path, config_file: Path | None, root_key: bytes, *, record: bool = True) -> StateBackends:
    """This computer's: the vault (latet.db) and the history (exchanges.db; none without ``record``) in ``data``,
    sealed with ``root_key``; config.yaml (``config_file``) and config.reviewed.json; the rest in this process's
    memory, as before E0a."""
    from latet.gateway.recorder import Broadcaster
    from latet.storage.db import Store
    from latet.storage.exchanges import ExchangeStore

    vault = Store(data / "latet.db", root_key)
    try:
        history = ExchangeStore(data / "exchanges.db", root_key) if record else None
    except BaseException:  # the history cannot be opened: the vault opened a moment ago is not left open
        vault.close()
        raise
    return StateBackends(
        vault=vault,
        history=history,
        conversations=LocalConversations(),
        sent=LocalSentBlocks(),
        given=LocalGiven(),
        approvals=LocalApprovals(),
        feed=Broadcaster(),
        approvals_feed=Broadcaster(),
        sign_ins=LocalSignIns(),
        settings=LocalSettings(config_file, data),
        jobs=LocalJobLock(),
        aws=LocalAwsSessions(),
    )
