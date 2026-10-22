"""Post-split EOP-39 import/ownership checks; missing pool.py is never feature RED evidence."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "src"


def _child(script, tmp_path):
    # The coordinator's runner supplies isolated homes, external TEMP, offline
    # dependencies and an inherited Windows process/job memory limit.
    answer = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(SOURCE)],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert answer.returncode == 0, answer.stdout + answer.stderr


@pytest.mark.parametrize("first", ["ocr", "pool"])
def test_both_import_orders_keep_one_canonical_pool_and_inference_primitives(first, tmp_path):
    script = f"""
import importlib
import sys
import threading
sys.path.insert(0, sys.argv[1])
before = {{thread.ident for thread in threading.enumerate()}}
importlib.import_module('latet.vision.{first}')
ocr = importlib.import_module('latet.vision.ocr')
pool = importlib.import_module('latet.vision.pool')
for name in ('OcrPool', 'SpaceReads', 'ServerReading', 'shared_pool'):
    assert getattr(pool, name).__module__ == 'latet.vision.pool', name
for name in ('OnnxOcr', 'OcrError', '_Turns', '_Deadline', '_Deadlines', 'shared_ocr'):
    assert getattr(ocr, name).__module__ == 'latet.vision.ocr', name
assert pool._Turns is ocr._Turns
assert '_DEADLINES' not in vars(pool)
assert '_POOLS' not in vars(ocr)
assert '_SHARED' not in vars(pool)
assert {{thread.ident for thread in threading.enumerate()}} == before
"""
    _child(script, tmp_path)


def test_normal_application_imports_need_no_ocr_extra_and_start_no_ocr_threads(tmp_path):
    _child(
        """
import importlib
import importlib.abc
import sys
import threading
sys.path.insert(0, sys.argv[1])
before = {thread.ident for thread in threading.enumerate()}
blocked = {'numpy', 'onnxruntime', 'PIL', 'latet.vision.pool'}
class WithoutOcr(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise ModuleNotFoundError('OCR dependencies deliberately absent', name=fullname)
sys.meta_path.insert(0, WithoutOcr())
for name in ('latet.server.metrics', 'latet.runtime', 'latet.server.app', 'latet.cli.main'):
    importlib.import_module(name)
assert not any(name in sys.modules for name in blocked)
assert not any(thread.name.startswith('latet-ocr') for thread in threading.enumerate())
assert {thread.ident for thread in threading.enumerate()} == before
""",
        tmp_path,
    )
