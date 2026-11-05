"""Opt-in D2 retained-native observer; importing this module performs no qualification."""

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

PROFILE = "valkey-9.1.2-64bit-io1-retained-r80"
PREFIX = "tests/server/test_request_audit_privacy.py::"
FUNCTIONS = (
    "test_d2_actual_atomic_numeric_row_and_owned_four_families",
    "test_d2_native_prefix128_large_small_large",
    "test_d2_native_capacity_boundaries",
    "test_d2_native_publication_prefix_failure",
    "test_d2_native_prune_prefix_conserves_until_zero",
    "test_d2_native_prune_batch_limit",
    "test_d2_native_actual_31s_expiry_then_zero_refund",
)
NODES = frozenset(PREFIX + name for name in FUNCTIONS)
IDENTITY = ("run_id", "process_id", "valkey_version", "redis_version", "redis_build_id", "arch_bits")
MANIFEST_FIELDS = {
    "schema",
    "profile",
    "lua_sha256",
    "helper_sha256",
    "output_root",
    "config_reads",
    "nodes",
    "expected",
}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _hex(value, length):
    return type(value) is str and re.fullmatch("[0-9a-f]{" + str(length) + "}", value) is not None


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def validate_manifest(manifest, *, lua: str, output_root: Path, admitted: bool = False):
    """Validate controller inputs without touching the client or writing evidence."""
    _require(admitted is True, "native qualification was not admitted")
    _require(type(manifest) is dict and set(manifest) == MANIFEST_FIELDS, "invalid qualification manifest fields")
    _require(type(manifest["schema"]) is int and manifest["schema"] == 1, "unsupported manifest schema")
    _require(manifest["profile"] == PROFILE, "unadmitted native profile")
    _require(manifest["config_reads"] == ["io-threads"], "unadmitted CONFIG reads")
    _require(_hex(manifest["helper_sha256"], 64), "invalid helper pin")
    _require(type(lua) is str and _hex(manifest["lua_sha256"], 64), "invalid Lua binding")
    _require(hashlib.sha256(lua.encode("utf-8")).hexdigest() == manifest["lua_sha256"], "Lua binding mismatch")
    nodes = manifest["nodes"]
    _require(type(nodes) is list and 1 <= len(nodes) <= len(NODES), "invalid selected nodes")
    _require(all(type(node) is str and node in NODES for node in nodes), "unadmitted selected node")
    _require(len(nodes) == len(set(nodes)), "duplicate selected node")
    expiry = PREFIX + FUNCTIONS[-1]
    _require(expiry not in nodes or nodes == [expiry], "expiry witness requires a separate job")
    root = manifest["output_root"]
    _require(type(root) is str and 1 <= len(root) <= 1024, "invalid output root")
    if root == "runner-temp":
        root = os.environ.get("TEMP", "")
        _require(bool(root), "runner TEMP is missing")
    declared = Path(root)
    _require(declared.is_absolute() and output_root.is_absolute(), "output root must be absolute")
    _require(declared.resolve() == output_root.resolve(), "output root mismatch")
    expected = manifest["expected"]
    _require(
        type(expected) is dict and set(expected) == {*IDENTITY, "endpoint_sha256", "io_threads"},
        "invalid expected context",
    )
    _require(_hex(expected["run_id"], 40) and _hex(expected["endpoint_sha256"], 64), "invalid run or endpoint")
    _require(_integer(expected["process_id"], 1, (1 << 31) - 1), "invalid process id")
    for field in ("valkey_version", "redis_version", "redis_build_id"):
        value = expected[field]
        _require(
            type(value) is str and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value) is not None,
            "invalid version or build",
        )
    _require(
        expected["valkey_version"] == "9.1.2" and _integer(expected["arch_bits"], 64, 64),
        "unadmitted build profile",
    )
    _require(_integer(expected["io_threads"], 1, 1), "unadmitted IO profile")
    return json.loads(json.dumps(manifest))


class NativeObserver:
    """Observe an explicitly admitted case; measurements remain in the portable test."""

    def __init__(self, bus, *, lua: str, manifest: dict, output_root: Path, case_id: str, admitted: bool = False):
        self.manifest = validate_manifest(manifest, lua=lua, output_root=output_root, admitted=admitted)
        _require(type(case_id) is str and len(case_id) <= 512, "invalid qualification case")
        _require(
            any(
                case_id == node
                or (case_id.startswith(node + "[") and case_id.endswith("]") and len(case_id) > len(node) + 2)
                for node in self.manifest["nodes"]
            ),
            "case was not selected",
        )
        self.root = output_root.resolve()
        _require(self.root.is_dir(), "owned output root is missing")
        self.bus = bus
        self.case_id = case_id
        self.case_tag = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
        self.samples = 0
        self.closed = False
        self.context = self._context()

    def _context(self):
        client = self.bus.client
        kwargs = client.connection_pool.connection_kwargs
        endpoint = {key: kwargs.get(key) for key in ("host", "port", "db")}
        _require(endpoint["host"] in ("127.0.0.1", "::1"), "unadmitted endpoint")
        _require(
            _integer(endpoint["port"], 1, 65535) and _integer(endpoint["db"], 0, 15),
            "invalid endpoint coordinates",
        )
        digest = hashlib.sha256(json.dumps(endpoint, sort_keys=True).encode("ascii")).hexdigest()
        expected = self.manifest["expected"]
        _require(digest == expected["endpoint_sha256"], "endpoint binding mismatch")

        def checked_info():
            info = client.info(section="server")
            _require(type(info) is dict, "invalid server context")
            _require(
                all(type(info.get(key)) is type(expected[key]) and info.get(key) == expected[key] for key in IDENTITY),
                "run or build binding mismatch",
            )
            _require(_integer(info.get("uptime_in_seconds"), 0, (1 << 63) - 1), "invalid uptime")
            return {key: info[key] for key in (*IDENTITY, "uptime_in_seconds")}

        before = checked_info()  # No CONFIG until all declared identity/profile checks have passed.
        config = client.config_get("io-threads")
        _require(
            type(config) is dict and set(config) in ({"io-threads"}, {b"io-threads"}),
            "invalid exact CONFIG response",
        )
        _require(config.get("io-threads", config.get(b"io-threads")) in ("1", b"1"), "IO profile mismatch")
        after = checked_info()
        return {
            **{key: before[key] for key in IDENTITY},
            "endpoint_sha256": digest,
            "io_threads": 1,
            "uptime_seconds": {"before": before["uptime_in_seconds"], "after": after["uptime_in_seconds"]},
            "prior_disposal_queue_qualified": False,
            "source_to_endpoint_qualified": False,
        }

    def _write(self, suffix, record):
        encoded = json.dumps(record, sort_keys=True)
        _require(len(encoded.encode("utf-8")) <= 512 * 1024, "qualification record exceeds bound")
        path = self.root / f"p25-native-{self.case_tag}-{suffix}.json"
        with path.open("x", encoding="utf-8") as output:
            output.write(encoded)

    def sample(self, label: str, report: dict):
        _require(not self.closed and self.samples < 32, "qualification sample owner is closed or full")
        _require(type(label) is str and re.fullmatch(r"[a-z0-9-]{1,64}", label) is not None, "invalid sample label")
        if self.case_id == PREFIX + FUNCTIONS[0] and label == "atomic-published":
            _require(
                type(report) is dict and set(report) == {"reservations"} and _integer(report["reservations"], 1, 1),
                "invalid atomic observation",
            )
            self.samples += 1
            self._write(
                f"sample-{self.samples:02d}-{label}",
                {
                    "case_id": self.case_id,
                    "label": label,
                    "reservations": 1,
                    "context": self.context,
                    "lua_sha256": self.manifest["lua_sha256"],
                    "profile": PROFILE,
                    "qualification_scope": "atomic context observation only; no per-pair native qualification",
                },
            )
            return
        fields = {"label", "reservations", "reported_string_bytes", "max_pair_with_R80_slot_charge", "keys"}
        _require(type(report) is dict and set(report) == fields, "invalid measured report fields")
        _require(report["label"] == label, "sample label mismatch")
        _require(_integer(report["reservations"], 0, 1024), "invalid reservation count")
        _require(_integer(report["reported_string_bytes"], 0, 67108864), "invalid measured string bytes")
        _require(_integer(report["max_pair_with_R80_slot_charge"], 0, 65536), "invalid measured pair bytes")
        keys = report["keys"]
        _require(type(keys) is dict and len(keys) <= 1044, "invalid measured keys")
        for key, value in keys.items():
            _require(
                type(key) is str and key.startswith(self.bus.prefix) and len(key) <= 256,
                "unowned measured key",
            )
            if value is None:
                continue
            _require(
                type(value) is dict and set(value) == {"type", "length", "sha256", "native", "ttl"},
                "invalid key metadata",
            )
            _require(value["type"] == "string" and _integer(value["length"], 0, 32768), "invalid string metadata")
            _require(_hex(value["sha256"], 64) and _integer(value["native"], 1, 67108864), "invalid native metadata")
            _require(_integer(value["ttl"], -2, 30000), "invalid TTL metadata")
        self.samples += 1
        self._write(
            f"sample-{self.samples:02d}-{label}",
            {
                **report,
                "context": self.context,
                "case_id": self.case_id,
                "lua_sha256": self.manifest["lua_sha256"],
                "profile": PROFILE,
                "conditional_current_per_pair_bound": 18230,
                "shared_backing_and_old_versions_qualified": False,
            },
        )

    def close(self):
        if self.closed:
            return
        self.closed = True
        ended = self._context()
        self._write(
            "context",
            {
                "case_id": self.case_id,
                "profile": PROFILE,
                "lua_sha256": self.manifest["lua_sha256"],
                "opening": self.context,
                "closing": ended,
                "samples": self.samples,
                "qualification_scope": "retained observations only; startup/image/old-queue provenance is external",
            },
        )


class _QualificationPlugin:
    def __init__(self, manifest, root):
        self.manifest = manifest
        self.root = root

    @pytest.fixture
    def p25_native_observer(self, request):
        from latet.server.live import _REQUEST_STATUS

        observer = NativeObserver(
            request.getfixturevalue("bus"),
            lua=_REQUEST_STATUS,
            manifest=self.manifest,
            output_root=self.root,
            case_id=request.node.nodeid,
            admitted=True,
        )
        try:
            yield observer
        finally:
            observer.close()


def pytest_configure(config):
    """Only pytest's explicitly selected plugin path invokes this hook."""
    _require(os.environ.get("P25_NATIVE_CONFIG_READ_ADMITTED") == "1", "qualification plugin was not admitted")
    path = Path(os.environ.get("P25_NATIVE_MANIFEST", ""))
    pin = os.environ.get("P25_NATIVE_MANIFEST_SHA256", "")
    _require(path.is_absolute() and _hex(pin, 64), "missing controller manifest binding")
    _require(path.stat().st_size <= 16384, "controller manifest exceeds bound")
    raw = path.read_bytes()
    _require(len(raw) <= 16384 and hashlib.sha256(raw).hexdigest() == pin, "controller manifest changed")
    manifest = json.loads(raw)
    _require(type(manifest) is dict, "invalid controller manifest")
    _require(
        hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == manifest.get("helper_sha256"),
        "qualification helper changed",
    )
    root = Path(os.environ.get("P25_NATIVE_OUTPUT_ROOT", ""))
    from latet.server.live import _REQUEST_STATUS

    manifest = validate_manifest(manifest, lua=_REQUEST_STATUS, output_root=root, admitted=True)
    config.pluginmanager.register(_QualificationPlugin(manifest, root), name="p25_native_qualification")
