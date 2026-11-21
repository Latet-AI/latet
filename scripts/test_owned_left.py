"""List what still exists under the names a test run's journals hold, read only: nothing is removed.

Each child of scripts/test_parallel.py writes `<label>.owned.jsonl` beside its reports (tests/owned.py): every schema,
key prefix, role, database and Valkey user its tests made, named before it was made. After a whole run, or a run
stopped by force, this lists what still exists under those names and writes it to `owned-left.json` in the same
directory, beside `summary.json`:

    python -I -B scripts/test_owned_left.py --output <report-directory> --config <config.json>

The config is the runner's: its `LATET_TEST_DATABASE_URL`, `LATET_TEST_TLS_DATABASE_URL` and `LATET_TEST_VALKEY_URL`
say where each record's place is. An object belongs to a record only by its exact name, or that name followed by `_`
(PostgreSQL), or a key that begins with the record's prefix (Valkey): compared as strings, never through LIKE or a
glob, so that another campaign's names are never listed. A record of a shape tests/owned.py does not make, an
`unreadable` place or a place the config does not name is set apart and matched against nothing: resolve it by hand
against the private config, or leave it. Keys are counted, never written; no address is written. The exit status is 0
only when there were journals, every place was read in full, nothing is left and nothing was set apart.
"""

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("latet_tests_owned", ROOT / "tests" / "owned.py")
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("tests/owned.py of this checkout cannot be loaded")
_OWNED = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_OWNED)

SERVICES = {
    "LATET_TEST_DATABASE_URL": "database",
    "LATET_TEST_TLS_DATABASE_URL": "database",
    "LATET_TEST_VALKEY_URL": "valkey",
}
SCAN_SECONDS = 60.0


def journals(output: Path) -> list[dict]:
    """Every record of every journal in ``output``; a line that is not a record is kept as ``{journal, line}``."""
    records = []
    for path in sorted(output.glob("*.owned.jsonl")):
        for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            if not isinstance(record, dict):
                record = {"line": number}
            records.append({**record, "journal": path.name})
    return records


def sort_out(records: list[dict], places: dict[str, str]) -> dict:
    """The records by place (``places`` maps a place to ``database`` or ``valkey``), and those set apart: ``refused``
    (a shape tests/owned.py does not make, a kind for the other service, a record cut short), ``unreadable`` and
    ``unmatched`` (a place the config does not name)."""
    out: dict = {"by_place": {}, "refused": [], "unreadable": [], "unmatched": []}
    for record in records:
        name, kind, at = record.get("name"), record.get("kind"), record.get("at")
        if not _OWNED.shaped(name, kind) or not isinstance(at, str):
            out["refused"].append(record)
        elif at == "unreadable":
            out["unreadable"].append(record)
        elif at not in places:
            out["unmatched"].append(record)
        elif (places[at] == "database") != (kind in _OWNED.POSTGRES_KINDS):
            out["refused"].append(record)
        else:
            out["by_place"].setdefault(at, []).append(record)
    return out


def under(name: str, record: str) -> bool:
    """Whether a PostgreSQL object named ``name`` is under the record ``record``: its exact name, or it followed by
    ``_``. Plain string comparison: ``_`` is no wildcard here."""
    return name == record or name.startswith(record + "_")


def postgres_left(records: list[dict], *, schemas: list[str], roles: list[str], databases: list[str]) -> list[dict]:
    """The schemas, roles and databases under a record, whatever the record's kind."""
    left = []
    for record in records:
        for kind, names in (("schema", schemas), ("role", roles), ("database", databases)):
            left += [_item(kind, name, record) for name in names if under(name, record["name"])]
    return left


def valkey_left(records: list[dict], *, keys: list[bytes], users: list[str]) -> list[dict]:
    """The keys under each prefix record, counted, and under each Valkey user's ``<user>:``; the users of that name."""
    left = []
    for record in records:
        prefix = record["name"] if record["kind"] == "prefix" else record["name"] + ":"
        count = sum(1 for key in keys if key.startswith(prefix.encode()))
        if count:
            left.append({**_item("keys", prefix, record), "count": count})
        if record["kind"] == "valkey-user" and record["name"] in users:
            left.append(_item("valkey-user", record["name"], record))
    return left


def _item(kind: str, name: str, record: dict) -> dict:
    return {
        "object": kind,
        "name": name,
        "record": record["name"],
        "kind": record["kind"],
        "at": record["at"],
        "test": record.get("test", ""),
        "journal": record.get("journal", ""),
    }


def _postgres(url: str) -> tuple[list[str], list[str], list[str]]:
    import psycopg

    options = "-c default_transaction_read_only=on -c statement_timeout=10000"
    with psycopg.connect(url, connect_timeout=5, options=options) as conn:
        schemas = [row[0] for row in conn.execute("SELECT nspname FROM pg_namespace")]
        roles = [row[0] for row in conn.execute("SELECT rolname FROM pg_roles")]
        databases = [row[0] for row in conn.execute("SELECT datname FROM pg_database")]
    return schemas, roles, databases


def _valkey(url: str) -> tuple[list[bytes], list[str], bool]:
    import redis

    client = redis.Redis.from_url(url, socket_connect_timeout=5, socket_timeout=10)
    try:
        keys, deadline = [], time.monotonic() + SCAN_SECONDS
        for key in client.scan_iter(count=1000):
            keys.append(key)
            if time.monotonic() > deadline:
                return keys, [], False
        users = [user.decode() if isinstance(user, bytes) else user for user in client.acl_users()]
        return keys, users, True
    finally:
        client.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True, help="the run's report directory")
    parser.add_argument("--config", type=Path, required=True, help="the runner's private config")
    options = parser.parse_args(argv)
    environment = json.loads(options.config.read_text("utf-8")).get("environment", {})
    urls = {
        name: environment[name] for name in SERVICES if isinstance(environment.get(name), str) and environment[name]
    }
    places: dict[str, str] = {}
    by_place_url: dict[str, str] = {}
    for name, url in urls.items():
        at = _OWNED.place(url)
        if at != "unreadable":
            places[at], by_place_url[at] = SERVICES[name], url
    records = journals(options.output)
    found = sort_out(records, places)
    result = {
        "journals": sorted({record["journal"] for record in records}),
        "records": len(records),
        "left": [],
        "unreachable": [],
        **{key: found[key] for key in ("refused", "unreadable", "unmatched")},
    }
    for at, grouped in sorted(found["by_place"].items()):
        try:
            if places[at] == "database":
                schemas, roles, databases = _postgres(by_place_url[at])
                result["left"] += postgres_left(grouped, schemas=schemas, roles=roles, databases=databases)
            else:
                keys, users, whole = _valkey(by_place_url[at])
                if not whole:
                    result["unreachable"].append({"at": at, "why": f"the scan took more than {SCAN_SECONDS:.0f} s"})
                    continue
                result["left"] += valkey_left(grouped, keys=keys, users=users)
        except Exception as error:  # its words can hold an address: only its type is kept
            result["unreachable"].append({"at": at, "why": type(error).__name__})
    # No journal at all is a wrong directory or a run whose children never started, never a run that left nothing.
    result["complete"] = bool(result["journals"]) and not result["unreachable"]
    (options.output / "owned-left.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", "utf-8")
    counts = {key: len(result[key]) for key in ("left", "refused", "unreadable", "unmatched", "unreachable")}
    print(json.dumps({"records": result["records"], **counts}), flush=True)
    clean = result["complete"] and not any(counts[key] for key in ("left", "refused", "unreadable", "unmatched"))
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
