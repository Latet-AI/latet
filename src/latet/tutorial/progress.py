"""The chapters of the tutorial you finished, so that the menu shows them from one run to the next.

Kept in `tutorial.json` in your own data folder (the one file the tutorial writes outside its sandbox): chapter ids
only, `{"done": [...]}`, written whole or not at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from latet.config import write_atomic
from latet.tutorial.chapters import ORDER


class Progress:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> list[str]:
        """The ids in the file, as they are; none when it is missing or cannot be read."""
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        done = data.get("done") if isinstance(data, dict) else None
        return [cid for cid in done if isinstance(cid, str)] if isinstance(done, list) else []

    def done(self) -> list[str]:
        """The chapters finished, in the menu's order."""
        done = set(self._read())
        return [cid for cid in ORDER if cid in done]

    def mark(self, chapter_id: str) -> None:
        """``chapter_id`` finished. A chapter another version of Latet knows, and this one does not, stays."""
        kept = self._read()
        if chapter_id in kept:
            return
        done = {*kept, chapter_id}
        ids = [cid for cid in ORDER if cid in done] + [cid for cid in dict.fromkeys(kept) if cid not in ORDER]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self.path, json.dumps({"done": ids}))
