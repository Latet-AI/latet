"""The optional text recognition models (``latet ocr install``): PaddleOCR's PP-OCRv6, as ONNX.

A text detector finds the lines of text in an image, and a recognizer reads each one, accents
included (Italian, French, German, Spanish...). Both run with ONNX Runtime on this machine: nothing
leaves it. How they were chosen: ``notes/spike-ocr.md``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from latet.engine.modelspec import ModelSpec, is_installed, model_dir

DETECTOR = ModelSpec(
    name="ppocr-v6-small-det",
    repo="PaddlePaddle/PP-OCRv6_small_det_onnx",
    revision="28fe5895c24fd108c19eb3e8479f4ab385fbfc62",
    files={
        "inference.onnx": ("d73e0058b7a8086bbd57f3d10b8bcd4ff95363f67e06e2762b5e814fe9c9410e", 9_880_512),
        "inference.yml": ("193f435274bf9f0b5f71a929bbfbcf148282df7e633b34e7c373e8f44741b516", 885),
    },
    license="Apache-2.0",
    homepage="https://huggingface.co/PaddlePaddle/PP-OCRv6_small_det_onnx",
)
RECOGNIZER = ModelSpec(
    name="ppocr-v6-small-rec",
    repo="PaddlePaddle/PP-OCRv6_small_rec_onnx",
    revision="b8f84f0b80c529de40b4fbb3544b84fa7233a513",
    files={
        "inference.onnx": ("5435fd747c9e0efe15a96d0b378d5bd157e9492ed8fd80edf08f30d02fa24634", 21_159_378),
        "inference.yml": ("ab078671bb49f06228eadccd34f1bb501e157f7a047095ffb943ba81512c77d1", 150_579),
    },
    license="Apache-2.0",
    homepage="https://huggingface.co/PaddlePaddle/PP-OCRv6_small_rec_onnx",
)
MODELS = (DETECTOR, RECOGNIZER)


def size() -> int:
    return sum(spec.size for spec in MODELS)


def installed(data: Path) -> bool:
    return all(is_installed(data, spec) for spec in MODELS)


def folders(data: Path) -> tuple[Path, Path]:
    return model_dir(data, DETECTOR), model_dir(data, RECOGNIZER)


def runtime_available() -> bool:
    """The ``ocr`` extra is installed (ONNX Runtime, NumPy, Pillow)."""
    return all(importlib.util.find_spec(m) is not None for m in ("onnxruntime", "numpy", "PIL"))
