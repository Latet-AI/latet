"""Per-worker provenance mechanics; no real fuzz command, database or child process."""

import hashlib
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_task8_benchmark_measurements import helper, vault_fuzz


def test_worker_rejects_foreign_product_module_and_hashes_exact_local_files(tmp_path):
    adapter = helper("task8-fuzz-child.py")
    source = tmp_path / "src" / "latet"
    source.mkdir(parents=True)
    package = source / "__init__.py"
    package.write_text("synthetic package")
    outside = tmp_path / "foreign.py"
    outside.write_text("foreign product source")
    modules = {"latet": SimpleNamespace(__file__=str(package)), "unrelated": SimpleNamespace(__file__=str(outside))}
    origins = adapter.product_origins(tmp_path, modules)
    assert origins == {
        "latet": {"file": str(package.resolve()), "sha256": hashlib.sha256(package.read_bytes()).hexdigest()}
    }
    modules["latet.foreign"] = SimpleNamespace(__file__=str(outside))
    with pytest.raises(ValueError, match=r"outside ROOT/src: latet\.foreign"):
        adapter.product_origins(tmp_path, modules)


@pytest.mark.parametrize("fails", [False, True])
def test_worker_writes_before_and_finally_after_and_delegates_exact_options(tmp_path, monkeypatch, fails):
    adapter = helper("task8-fuzz-child.py")
    source = tmp_path / "src" / "latet"
    source.mkdir(parents=True)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    harness = scripts / "vault_fuzz.py"
    harness.write_text("synthetic harness identity; not executed")
    package = source / "__init__.py"
    package.write_text("synthetic package")
    later = source / "later.py"
    later.write_text("synthetic later import")
    modules = {"latet": SimpleNamespace(__file__=str(package))}
    monkeypatch.setattr(adapter, "ROOT", tmp_path)
    monkeypatch.setattr(adapter, "sys", SimpleNamespace(modules=modules, argv=[]))
    output = tmp_path / "apps-m1000-email-allow-domain-term.json"
    config = "email-allow-domain-term"
    prefix = output.stem + "." + config
    before = output.with_name(prefix + ".origins-before.json")
    after = output.with_name(prefix + ".origins-after.json")
    given = [
        "one",
        str(output),
        "5000",
        config,
        "--path",
        "apps",
        "--memory",
        "1000",
        "--index",
        "on",
        "--pg",
        "--schema",
        "latet_t_keyfuzz_pi0102_0123456789ab_7",
    ]
    calls = []

    def delegated(argv):
        calls.append(argv)
        assert before.exists() and not after.exists()
        assert set(json.loads(before.read_text())["product_modules"]) == {"latet"}
        modules["latet.later"] = SimpleNamespace(__file__=str(later))
        if fails:
            raise LookupError("synthetic delegated failure")
        return 7

    fuzz = SimpleNamespace(parser=vault_fuzz.parser, main=delegated)
    monkeypatch.setattr(adapter, "load_fuzz", lambda: fuzz)
    if fails:
        with pytest.raises(LookupError, match="synthetic delegated failure"):
            adapter.main(given)
    else:
        assert adapter.main(given) == 7
    assert calls == [given]
    first, last = json.loads(before.read_text()), json.loads(after.read_text())
    assert first["phase"] == "before" and last["phase"] == "after"
    assert first["source_root"] == last["source_root"] == str(tmp_path)
    assert last["product_modules"]["latet.later"] == {
        "file": str(later.resolve()),
        "sha256": hashlib.sha256(later.read_bytes()).hexdigest(),
    }
    assert first["harness"] == last["harness"]
    assert last["harness"][str(harness.resolve())] == hashlib.sha256(harness.read_bytes()).hexdigest()
    assert len(last["harness"]) == 2


def test_worker_loads_exact_harness_with_registered_module_and_src_first(tmp_path, monkeypatch):
    adapter = helper("task8-fuzz-child.py")
    registry = {}
    monkeypatch.setattr(adapter, "ROOT", tmp_path)
    monkeypatch.setattr(adapter, "sys", SimpleNamespace(modules=registry, path=["foreign-first"]))
    loaded = SimpleNamespace()
    calls = []

    def execute(module):
        assert registry["vault_fuzz"] is module
        calls.append("loaded")

    def specification(name, path):
        assert name == "vault_fuzz"
        assert path == tmp_path / "scripts" / "vault_fuzz.py"
        return SimpleNamespace(name=name, loader=SimpleNamespace(exec_module=execute))

    monkeypatch.setattr(adapter.importlib.util, "spec_from_file_location", specification)
    monkeypatch.setattr(adapter.importlib.util, "module_from_spec", lambda spec: loaded)
    assert adapter.load_fuzz() is loaded
    assert calls == ["loaded"]
    assert adapter.sys.path[:2] == [str(tmp_path / "src"), str(tmp_path / "scripts")]


def test_worker_records_real_data_namespace_from_loaded_registry():
    adapter = helper("task8-fuzz-child.py")
    package = importlib.import_module("latet.engine.surrogates.data")
    assert package.__file__ is None
    origins = adapter.product_origins(adapter.ROOT, sys.modules)
    assert origins[package.__name__] == {
        "namespace_paths": [str(Path(location).resolve()) for location in package.__path__]
    }
    assert origins["latet"]["sha256"] == hashlib.sha256(Path(sys.modules["latet"].__file__).read_bytes()).hexdigest()


@pytest.mark.parametrize("fault", ["foreign", "missing", "empty", "mismatch", "missing-directory"])
def test_worker_rejects_foreign_or_missing_namespace_paths(tmp_path, fault):
    adapter = helper("task8-fuzz-child.py")
    source = tmp_path / "src" / "latet"
    source.mkdir(parents=True)
    package = source / "__init__.py"
    package.write_text("synthetic local package")
    local = source / "namespace"
    local.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    locations = [str(local)]
    declared = locations
    expected = "no valid search paths"
    if fault == "foreign":
        locations = declared = [str(local), str(foreign)]
        expected = "not a directory within ROOT/src"
    elif fault == "missing":
        locations = None
    elif fault == "empty":
        locations = declared = []
    elif fault == "mismatch":
        declared = [str(source)]
        expected = "search paths disagree"
    else:
        locations = declared = [str(source / "absent")]
        expected = "not a directory within ROOT/src"
    namespace = SimpleNamespace(
        __file__=None, __path__=locations, __spec__=SimpleNamespace(submodule_search_locations=declared)
    )
    modules = {"latet": SimpleNamespace(__file__=str(package)), "latet.namespace": namespace}
    with pytest.raises(ValueError, match=expected):
        adapter.product_origins(tmp_path, modules)
