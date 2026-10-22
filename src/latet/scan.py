"""The text files of a folder, for ``latet rules test --corpus`` and ``latet rules suggest``.

In a git repository, the files git knows (tracked or new, ignored ones left out); elsewhere a walk
that skips version control, dependencies and build output. Binary and large files are skipped, and
the walk stops at a few thousand files.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "dist", "build", "target", "__pycache__", ".tox"}
    | {".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", ".nuxt", ".gradle", ".idea", ".vscode"}
)
MAX_FILE_BYTES = 2_000_000
MAX_FILES = 5_000
MAX_TOTAL_BYTES = 50_000_000


def _git_files(root: Path) -> list[Path] | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [git, "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=root,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [root / name for name in out.stdout.decode("utf-8", errors="replace").split("\0") if name]


def _walk(root: Path) -> Iterator[Path]:
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            yield Path(folder) / name


def text_files(root: Path) -> Iterator[tuple[Path, str]]:
    """(path, text) of each text file under ``root`` (or ``root`` itself, when it is a file)."""
    paths = [root] if root.is_file() else (_git_files(root) or _walk(root))
    total = 0
    for count, path in enumerate(paths):
        if count >= MAX_FILES or total >= MAX_TOTAL_BYTES:
            return
        try:
            if not path.is_file() or not 0 < path.stat().st_size <= MAX_FILE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            continue  # binary
        total += len(data)
        yield path, data.decode("utf-8", errors="replace")


def shape(value: str) -> str:
    """A value's form without the value: ``AB-123456`` → ``AA-999999``."""
    return "".join(("A" if c.isupper() else "a") if c.isalpha() else "9" if c.isdigit() else c for c in value)
