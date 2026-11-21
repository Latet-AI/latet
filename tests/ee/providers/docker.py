"""Containers for the tests against real providers: started from images already pulled (never pulled here), named
``latet-prov-<name>``, their ports published on 127.0.0.1 alone, and removed with their volumes when the test ends."""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

PREFIX = "latet-prov-"


def _docker(*args: str, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    found = shutil.which("docker")
    if found is None:
        pytest.skip("docker is not installed")
    done = subprocess.run([found, *args], capture_output=True, text=True, timeout=timeout, check=False)
    if check and done.returncode != 0:
        raise AssertionError(f"docker {args[0]} failed: {done.stderr.strip()[-2000:]}")
    return done


def require_image(image: str) -> None:
    """Skips the test when ``image`` is not on this computer: the tests never pull one (README.md)."""
    if _docker("image", "inspect", "--format", "{{.Id}}", image, check=False).returncode != 0:
        pytest.skip(f"{image} is not on this computer: pull it by hand first (tests/ee/providers/README.md)")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build(tag: str, context: Path) -> None:
    """An image built from ``context``'s Dockerfile, its base never pulled (``--pull=false``)."""
    _docker("build", "--pull=false", "--tag", tag, str(context), timeout=900)


@dataclass
class Container:
    """A container of this run: ``ports`` maps a port inside it to the one published on 127.0.0.1."""

    name: str
    ports: dict[int, int] = field(default_factory=dict)

    def logs(self) -> str:
        done = _docker("logs", self.name, check=False)
        return done.stdout + done.stderr

    def run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        """``argv`` run inside the container."""
        return _docker("exec", self.name, *argv, check=False)

    def signal(self, name: str) -> None:
        _docker("kill", "--signal", name, self.name)

    def remove(self) -> None:
        _docker("rm", "--force", "--volumes", self.name, check=False)


def start(
    name: str,
    image: str,
    *,
    ports: Sequence[int] = (),
    env: Mapping[str, str] | None = None,
    args: Sequence[str] = (),
    files: Mapping[str, Path] | None = None,
    options: Sequence[str] = (),
) -> Container:
    """A container of ``image`` named ``latet-prov-<name>``, never pulled, each of ``ports`` published on 127.0.0.1,
    ``files`` (a folder on this computer by the path it takes inside) copied in before it starts. One left by a run
    that was stopped short is removed first."""
    require_image(image)
    container = Container(PREFIX + name, {port: free_port() for port in ports})
    container.remove()
    argv = ["create", "--pull", "never", "--name", container.name]
    for inside, outside in container.ports.items():
        argv += ["--publish", f"127.0.0.1:{outside}:{inside}"]
    for key, value in (env or {}).items():
        argv += ["--env", f"{key}={value}"]
    _docker(*argv, *options, image, *args)
    try:
        for inside, folder in (files or {}).items():
            _docker("cp", f"{folder}{'/.' if folder.is_dir() else ''}", f"{container.name}:{inside}")
        _docker("start", container.name)
    except BaseException:
        container.remove()
        raise
    return container


def wait(done: Callable[[], bool], what: str, container: Container, seconds: float = 180.0) -> None:
    """Waits until ``done()``, ``seconds`` at most; the container's last lines in the failure."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            if done():
                return
        except (OSError, httpx.HTTPError):
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} did not happen within {seconds:g} s: {container.logs()[-3000:]}")
        time.sleep(1.0)
