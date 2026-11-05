"""One owned child, one held launch, and fenced recovery and retirement."""

from __future__ import annotations

import base64
import contextlib
import re
import subprocess
import threading
from collections.abc import Callable
from typing import Any

_ID = re.compile(r"[0-9a-f]{32}")
_KEY = re.compile(r"latet_lk_[A-Za-z0-9_-]{43}")
_MAX = (1 << 64) - 1
_BASE = "/_latet/engine/v1/launches"


class LaunchControlUnavailable(Exception):
    def __init__(self, code: str = "launch-unavailable") -> None:
        allowed = {
            "launch-unavailable",
            "launch-stale",
            "launch-capacity",
            "launch-busy",
            "launch-ended",
            "launch-mismatch",
            "launch-invalid",
            "launch-unregistered",
            "unauthorized",
        }
        self.code = code if code in allowed else "launch-unavailable"
        super().__init__(self.code)


def _uint(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX


def _fence(value: Any) -> tuple[str, int]:
    if (
        type(value) is not dict
        or set(value) != {"epoch", "retirement"}
        or type(value["epoch"]) is not str
        or _ID.fullmatch(value["epoch"]) is None
        or not _uint(value["retirement"])
    ):
        raise LaunchControlUnavailable()
    return value["epoch"], value["retirement"]


def _credential(key: Any) -> str:
    if type(key) is not str or _KEY.fullmatch(key) is None:
        raise LaunchControlUnavailable()
    raw = base64.urlsafe_b64decode(key[9:] + "=")
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != key[9:]:
        raise LaunchControlUnavailable()
    return key


def _binding(value: Any) -> dict[str, str]:
    if type(value) is not dict or set(value) != {"folder", "root", "flavor", "source", "project"}:
        raise LaunchControlUnavailable()
    if (
        type(value["flavor"]) is not str
        or value["flavor"] not in {"windows", "posix"}
        or type(value["source"]) is not str
        or value["source"] not in {"server", "client"}
        or type(value["project"]) is not str
        or re.fullmatch(r"[0-9a-f]{12}", value["project"]) is None
    ):
        raise LaunchControlUnavailable()
    for name in ("folder", "root"):
        text = value[name]
        if type(text) is not str or not 0 < len(text) <= 4096 or _KEY.search(text) is not None:
            raise LaunchControlUnavailable()
        try:
            if len(text.encode("utf-8")) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in text):
                raise LaunchControlUnavailable()
        except UnicodeError:
            raise LaunchControlUnavailable() from None
    return dict(value)


def claude_headers(existing: str, key: str | None) -> str:
    """Preserve ordinary header lines, as they were written; replace only Latet's reserved carrier. Lines are split
    at a newline alone, as Claude Code and Copilot CLI read them: a vertical tab, a form feed or a line separator inside
    a user's value, or a carriage return before its newline, stays as it was (M4)."""
    if type(existing) is not str:
        raise LaunchControlUnavailable()
    lines = [line for line in existing.split("\n") if line.partition(":")[0].strip().lower() != "x-latet-launch-key"]
    if key is not None:
        if lines and not lines[-1].strip():
            lines.pop()  # the value's final newline: the key's line follows the last header, not a blank line
        lines.append("x-latet-launch-key: " + _credential(key))
    return "\n".join(lines)


# What a launcher says when the agent it starts has no launch key (root's ruling I-5): why, in a sentence, and what it
# means for the session, never a code. The session is decided as claimed: its project's added rules apply, its
# loosenings do not (D7). P2-7 shows the same on the dashboard and in `latet doctor`.
_CLAIMED = "so this session's project settings only add protection: their loosenings do not apply"
_UNVERIFIED = {
    "claude": "Claude Code's custom headers are set here where Latet cannot add a launch key (managed or project "
    "settings, or the settings sources chosen on its command line)",
    "codex": "Codex's settings here (a profile, a -c override or its provider's own headers) leave Latet no verified "
    "way to give it a launch key",
    "opencode": "OpenCode's settings here leave Latet no verified way to give it a launch key",
    "gemini": "Gemini CLI as it is set up here leaves Latet no verified way to give it a launch key",
    "copilot": "Copilot CLI carries a launch key only to an OpenAI-compatible provider over Chat Completions, and "
    "it is set up otherwise here",
    "run": "a program that latet run starts has no way to carry a launch key",
}
_WHY = {
    "builtin-provider": "Codex's built-in providers (its OpenAI provider, ChatGPT sign-in included) have no verified "
    "way to carry a launch key",
    "older-codex": "Codex older than 0.115 has no way to carry a launch key",
    # The pins of latet.launch_carriers (P2-8a): OpenCode substitutes {env:...} in OPENCODE_CONFIG_CONTENT from the
    # version whose source was read, Gemini CLI reads its header variable from 0.19, and in ACP mode the editor that
    # starts it can choose another address and headers.
    "older-opencode": "OpenCode older than 1.19.5 has no verified way to carry a launch key",
    "unknown-opencode": "Latet could not read OpenCode's version, and only OpenCode 1.19.5 or later carries a launch "
    "key the way Latet verified",
    "older-gemini": "Gemini CLI older than 0.19 has no way to carry a launch key",
    "gemini-acp": "Gemini CLI in ACP mode takes its address and headers from the editor that starts it, so Latet "
    "gives it no launch key",
    "launch-capacity": "too many launched agents hold a launch key right now, and this one starts without one",
    "launch-unavailable": "Latet could not open a launch key for this session (it did not answer in time)",
}


def keyless_words(agent: str, reason: str | None) -> tuple[str, bool] | None:
    """The sentence a launcher says when its agent starts without a launch key, and whether it is a warning; None:
    nothing to say (on a local route Latet is not in front of the agent at all)."""
    if reason is None or reason == "local-route":
        return None
    if reason == "launch-unregistered":
        return "Latet: this folder is not one of your projects, so the agent needs no launch key here.", False
    if reason == "carrier-unverified":
        why = _UNVERIFIED.get(agent, _UNVERIFIED["run"])
    else:
        why = _WHY.get(reason, "Latet could not open a launch key for this session")
    sentence = why if why.startswith("Latet") else f"Latet: {why}"
    return f"{sentence}, {_CLAIMED}.", True


class LaunchSession:
    """One launch, one actual child, one held request, one recovery owner.

    The process runner owns the single non-daemon monitor thread; the kernel
    deliberately cannot start background work or queue another attempt.
    """

    __slots__ = (
        "_binding",
        "_call",
        "_child",
        "_disposed",
        "_held",
        "_hold",
        "_interrupt",
        "_key",
        "_lock",
        "_operation",
        "_registration",
        "_terminal",
        "generation",
        "launch_id",
    )

    def __init__(
        self, call: Callable, hold: Callable, registration: dict, opened: dict, interrupt: Callable = lambda: None
    ) -> None:
        self._call, self._hold, self._interrupt = call, hold, interrupt
        self._disposed = False
        self._lock = threading.Lock()
        self._operation: object | None = None
        self._terminal = False
        self._child = None
        self._held = None
        self._key = _credential(opened.get("key"))
        self.launch_id = opened.get("launch_id")
        self.generation = opened.get("generation", 0)
        if type(self.launch_id) is not str or _ID.fullmatch(self.launch_id) is None or not _uint(self.generation):
            raise LaunchControlUnavailable()
        self._registration = dict(registration)
        protected = set(registration) == {"folder", "root", "flavor"}
        expected = {"key", "launch_id", "generation", "binding"} if protected else {"key", "launch_id"}
        if set(opened) != expected or (not protected and set(registration) != {"folder"}):
            raise LaunchControlUnavailable()
        self._binding = _binding(opened["binding"]) if protected else None
        if self._binding is not None:
            # Use the exact authenticated server-selected binding for recovery.
            if self._binding["folder"] != registration["folder"] or self._binding["flavor"] != registration["flavor"]:
                raise LaunchControlUnavailable()
            self._registration = {
                "folder": self._binding["folder"],
                "root": self._binding["root"],
                "flavor": self._binding["flavor"],
            }

    @classmethod
    def open(
        cls,
        call: Callable,
        hold: Callable,
        registration: dict,
        interrupt: Callable = lambda: None,
    ) -> LaunchSession | None:
        answer = call("POST", _BASE, registration)  # never replay this OPEN
        if answer == {"key": None, "launch_id": None, "reason": "launch-capacity"}:
            return None
        known_id = answer.get("launch_id") if type(answer) is dict else None
        owner = None
        try:
            if type(answer) is not dict:
                raise LaunchControlUnavailable()
            if set(registration) == {"folder", "root", "flavor"}:
                _binding(answer.get("binding"))  # validate protected reply BEFORE constructor/disclosure
            owner = cls(call, hold, registration, answer, interrupt)
            owner._held = owner._new_hold(owner.generation)
            owner.generation = owner._held.generation
            return owner
        except BaseException:
            try:
                if owner is not None:
                    owner.mark_terminal()
                    with contextlib.suppress(Exception):
                        owner.close_hold()
            finally:
                if owner is not None:
                    owner._key = ""
                if type(known_id) is str and _ID.fullmatch(known_id) is not None:
                    # Exact known ID only, one best-effort END. Unknown
                    # disclosure stays negative; no re-OPEN or fabricated EXIT.
                    with contextlib.suppress(Exception):
                        call("POST", f"{_BASE}/{known_id}/end", {})
            raise

    @property
    def key(self) -> str:
        return self._key

    def bind_child(self, child: Any) -> None:
        # The runner supplies its actual Popen, never a PID/facts reported by child.
        with self._lock:
            if self._child is not None or self._terminal:
                raise LaunchControlUnavailable()
            self._child = child

    def _live(self) -> bool:
        return not self._terminal and self._child is not None and self._child.poll() is None

    def _new_hold(self, generation: int) -> Any:
        if not _uint(generation) or generation == _MAX:
            raise LaunchControlUnavailable()
        held = self._hold(self.launch_id, generation)
        if type(held.generation) is not int or held.generation != generation + 1:
            held.close()
            held.drain()
            raise LaunchControlUnavailable()
        return held

    def wait_hold(self) -> None:
        # Called only by the one monitor. close() must interrupt the actual read.
        with self._lock:
            held = self._held
        if held is not None:
            held.wait()

    def reconnect(self) -> bool:
        marker = object()
        with self._lock:
            if not self._live() or self._operation is not None:
                return False
            self._operation = marker
            old, self._held = self._held, None
        try:
            if old is not None:
                old.close()
                old.drain()
            epoch, retirement = _fence(self._call("GET", _BASE + "/prepare", None))
            if retirement == _MAX:
                raise LaunchControlUnavailable()
            # R124: fresh prepare, THEN actual-child check and operation ownership.
            with self._lock:
                if not self._live() or self._operation is not marker:
                    return False
            payload = {
                **self._registration,
                "key": self._key,
                "launch_id": self.launch_id,
                "epoch": epoch,
                "retirement": retirement,
            }
            try:
                answer = self._call("POST", _BASE + "/recover", payload)
            finally:
                payload.clear()
            if (
                type(answer) is not dict
                or set(answer)
                != (
                    {"launch_id", "generation", "binding"} if self._binding is not None else {"launch_id", "generation"}
                )
                or answer.get("launch_id") != self.launch_id
                or not _uint(answer.get("generation"))
                or (_binding(answer["binding"]) if self._binding is not None else None) != self._binding
            ):
                raise LaunchControlUnavailable()
            with self._lock:
                if not self._live():
                    return False
            held = self._new_hold(answer["generation"])
            with self._lock:
                keep = self._live()
                if keep:
                    self._held, self.generation = held, held.generation
            if not keep:
                held.close()
                held.drain()
            return keep
        finally:
            with self._lock:
                if self._operation is marker:
                    self._operation = None

    def mark_terminal(self) -> None:
        # This is always the FIRST shutdown action, including interruption.
        with self._lock:
            self._terminal = True

    def close_hold(self) -> None:
        with self._lock:
            if not self._terminal:
                raise LaunchControlUnavailable()
            held, self._held = self._held, None
        if held is not None:
            held.close()

    def finish(self) -> bool:
        """After terminal, interrupt/close and JOIN monitor outside the lock.

        False means a still-owned exchange: do not claim drain/retirement.
        A failed EXIT leaves the existing negative; no key is re-registered.
        """
        marker = object()
        with self._lock:
            if not self._terminal or self._operation is not None:
                return False
            self._operation = marker
            exited = self._child is not None and self._child.poll() is not None
        try:
            self.close_hold()
            if exited:
                # A terminal owner uses a new boot's E only for EXIT, never recover.
                epoch, _ = _fence(self._call("GET", _BASE + "/prepare", None))
                answer = self._call("POST", f"{_BASE}/{self.launch_id}/exit", {"epoch": epoch})
                return type(answer) is dict and answer.get("retired") is True
            self._call("POST", f"{_BASE}/{self.launch_id}/end", {})
            return False
        finally:
            with self._lock:
                self._key = ""
                if self._operation is marker:
                    self._operation = None

    def close(self) -> None:
        self.mark_terminal()
        with self._lock:
            if self._disposed:
                return
            self._disposed = True
        self._interrupt()
        self.close_hold()
        # The runner has already joined the sole monitor before entering here.
        # With no child (settings/spawn failure), finish conservatively uses END.
        with contextlib.suppress(Exception):
            self.finish()
        self._key = ""


def run_owned(owner: LaunchSession, argv: list[str], env: dict[str, str]) -> int:
    stop = threading.Event()
    monitor_done = threading.Event()
    child = worker = None

    def monitor() -> None:
        try:
            delay = 1.0
            while not stop.is_set():
                try:
                    owner.wait_hold()
                    if stop.is_set() or not owner.reconnect():
                        return
                    delay = 1.0
                except LaunchControlUnavailable as error:
                    if error.code in {"launch-ended", "launch-mismatch", "launch-invalid", "unauthorized"}:
                        return
                except Exception:
                    if stop.is_set():
                        return  # categorical unavailable; no transport/body/traceback logging
                if stop.wait(delay):
                    return
                delay = min(5.0, delay * 2)
        finally:
            monitor_done.set()

    try:
        with owner._lock:
            if owner._terminal or owner._child is not None or owner._operation is not None:
                raise LaunchControlUnavailable()
            child = subprocess.Popen(argv, env=env)  # noqa: S603 - the user's already-resolved command
            owner._child = child
        worker = threading.Thread(target=monitor, name="latet-launch-hold", daemon=False)
        worker.start()
        return child.wait()
    finally:
        owner.mark_terminal()  # BEFORE cancel, socket shutdown, child cleanup or join
        stop.set()
        try:
            owner._interrupt()
        finally:
            try:
                owner.close_hold()
            finally:
                try:
                    if child is not None and child.poll() is None:
                        child.kill()
                        child.wait()
                finally:
                    interrupted = False
                    try:
                        if worker is not None and worker.ident is not None:
                            while True:
                                try:
                                    # Neither interrupted join nor thread bookkeeping
                                    # alone proves the monitor's Python owner settled.
                                    worker.join()
                                    monitor_done.wait()
                                    break
                                except KeyboardInterrupt:
                                    interrupted = True
                    finally:
                        owner.close()
                    if interrupted:
                        raise KeyboardInterrupt
