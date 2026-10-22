"""Download and verify the optional local models (``latet ner install``, ``latet ocr install``).

The only network access Latet makes besides forwarding your agents' traffic, and only when asked.
Files come from a pinned revision and must match their SHA-256 before they are used.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import ssl
from collections.abc import Callable
from pathlib import Path

import httpx
import truststore

from latet.engine.detectors.ner import MODEL
from latet.engine.modelspec import VERIFIED, ModelSpec, is_installed, model_dir

HUB = "https://huggingface.co"


class ModelError(Exception):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install(
    data: Path,
    spec: ModelSpec = MODEL,
    progress: Callable[[int, int], None] | None = None,
    hub: str = HUB,
    client: httpx.Client | None = None,
) -> Path:
    """Download ``spec`` into the data folder (once; an interrupted download resumes per file)."""
    final = model_dir(data, spec)
    if is_installed(data, spec):
        return final
    staging = final.with_name(final.name + ".partial")
    from latet.storage.folders import make_private_folder

    make_private_folder(data)  # the data folder, when this makes it, is the person's alone (whatever the umask)
    staging.mkdir(parents=True, exist_ok=True)
    own = client is None
    http = client or httpx.Client(
        verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        follow_redirects=True,
        timeout=httpx.Timeout(60.0, read=300.0),
        trust_env=True,
    )
    done = 0
    try:
        for path, (sha256, size) in spec.files.items():
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and target.stat().st_size == size and _sha256(target) == sha256:
                done += size
                if progress:
                    progress(done, spec.size)
                continue
            partial = target.with_name(target.name + ".part")
            digest = hashlib.sha256()
            with http.stream("GET", f"{hub}/{spec.repo}/resolve/{spec.revision}/{path}") as response:
                response.raise_for_status()
                with partial.open("wb") as out:
                    for chunk in response.iter_bytes(1 << 20):
                        out.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, spec.size)
            if digest.hexdigest() != sha256:
                partial.unlink(missing_ok=True)
                raise ModelError(f"{path}: the download does not match its pinned checksum")
            partial.replace(target)
    finally:
        if own:
            http.close()
    (staging / VERIFIED).write_text(
        json.dumps({"repo": spec.repo, "revision": spec.revision, "files": {p: h for p, (h, _) in spec.files.items()}}),
        "utf-8",
    )
    if final.exists():
        shutil.rmtree(final)
    staging.replace(final)
    return final


def remove(data: Path, spec: ModelSpec = MODEL) -> bool:
    folder = data / "models" / spec.name
    if not folder.exists():
        return False
    shutil.rmtree(folder)
    return True
