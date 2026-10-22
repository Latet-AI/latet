"""The optional local models: where each comes from, and where it lives once downloaded.

A model is downloaded on request only (``latet ner install``, ``latet ocr install``), from a
pinned revision whose files must match their SHA-256 (``latet.models``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelSpec:
    name: str
    repo: str  # Hugging Face repository the files are downloaded from
    revision: str  # pinned commit: the files below never change under the same revision
    files: dict[str, tuple[str, int]]  # path in the repository → (sha256, size in bytes)
    license: str
    homepage: str

    @property
    def size(self) -> int:
        return sum(size for _, size in self.files.values())


VERIFIED = ".verified"  # written once every file matched its hash


def model_dir(data: Path, spec: ModelSpec) -> Path:
    return data / "models" / spec.name / spec.revision[:12]


def is_installed(data: Path, spec: ModelSpec) -> bool:
    directory = model_dir(data, spec)
    if not (directory / VERIFIED).is_file():
        return False
    return all(
        (directory / path).is_file() and (directory / path).stat().st_size == size
        for path, (_, size) in spec.files.items()
    )
