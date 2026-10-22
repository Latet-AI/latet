"""What a log line may hold is a module of its own (``latet.logline``), below every part that logs: the settings,
storage, the engine, vision and the computer's state read it without importing the gateway, whose routes, forwarding
and app are built on them."""

import ast
import json
import subprocess
import sys
from pathlib import Path

import latet

# The parts below the gateway that log, each imported in turn in a fresh interpreter.
_BELOW_THE_GATEWAY = (
    "latet.config",
    "latet.storage.keys",
    "latet.engine.masker",
    "latet.engine.detectors.ner",
    "latet.engine.build",
    "latet.vision.ocr",
    "latet.state.local",
)
# Each part named, then the modules of the gateway it imported that none before it had.
_PULLED = """
import importlib, json, sys
seen, pulled = set(), {}
for name in sys.argv[1:]:
    importlib.import_module(name)
    found = {m for m in sys.modules if m.split(".")[:2] == ["latet", "gateway"]} - seen
    pulled[name], seen = sorted(found), seen | found
print(json.dumps(pulled))
"""


def test_the_parts_below_the_gateway_import_none_of_it():
    done = subprocess.run(
        [sys.executable, "-c", _PULLED, *_BELOW_THE_GATEWAY], capture_output=True, text=True, timeout=120, check=True
    )
    assert {name: found for name, found in json.loads(done.stdout).items() if found} == {}


def test_what_a_log_line_may_hold_imports_nothing_of_latet():
    """So that any part can read it, the bottom one included, with no cycle."""
    tree = ast.parse((Path(latet.__file__).parent / "logline.py").read_text("utf-8"))
    imported = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    imported += [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and not node.level]
    assert imported and not [name for name in imported if name.split(".")[0] == "latet"]
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level]  # none relative
