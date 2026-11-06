"""Post-split EOP-39 import/ownership checks; missing pool.py is never feature RED evidence."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "src"
# What the child's own end decides is the test's verdict: its interpreter is the base one (a venv's launcher can take
# seconds to start on a loaded Windows), the venv's packages added by the child itself, and the only bound is a hang's.
# -S keeps the base interpreter's own site-packages out of its path, so those it holds never come before the venv's.
# A child importing the application under a loaded suite took past the 20 s this bound was, and failed a test whose
# checks it passed (minors after 0.11, item 19).
HUNG = 600
_PACKAGES = [folder for folder in sys.path if folder.endswith("site-packages")]
_WITH_PACKAGES = f"import site\nfor _folder in {_PACKAGES!r}:\n    site.addsitedir(_folder)\n"
# Which packages the child ran (the fix round's Q-M4): latet from this checkout's src, and every module of a
# site-packages folder from the venv's, never one the base interpreter holds (anyio, click, httpx... on this computer).
_WHICH = f"""
import sys as _sys
import latet as _latet
assert _latet.__file__.startswith({str(SOURCE)!r}), _latet.__file__
_foreign = sorted(
    _name for _name, _module in list(_sys.modules.items())
    if "site-packages" in (getattr(_module, "__file__", None) or "")
    and not any(_module.__file__.startswith(_folder) for _folder in {_PACKAGES!r})
)
assert not _foreign, ("modules not from the venv", _foreign[:8])
"""


def _child(script, tmp_path):
    # The coordinator's runner supplies isolated homes, external TEMP, offline
    # dependencies and an inherited Windows process/job memory limit.
    base = getattr(sys, "_base_executable", sys.executable)
    answer = subprocess.run(
        [base, "-I", "-S", "-B", "-c", _WITH_PACKAGES + script + _WHICH, str(SOURCE)],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=HUNG,
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
for name in ('latet.server.metrics', 'latet.runtime', 'latet.cli.main'):
    importlib.import_module(name)
assert not any(name in sys.modules for name in blocked)
# The server's extra holds numpy (its index of prints, latet.server.index) beside psycopg and redis: its app needs no
# more of the OCR extra than that.
blocked.discard('numpy')
importlib.import_module('latet.server.app')
assert not any(name in sys.modules for name in blocked)
assert not any(thread.name.startswith('latet-ocr') for thread in threading.enumerate())
assert {thread.ident for thread in threading.enumerate()} == before
""",
        tmp_path,
    )
