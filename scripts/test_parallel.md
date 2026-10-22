# Complete parallel pytest gate (Windows)

The coordinator owns an exclusive test campaign slot, existing PostgreSQL/TLS/Valkey services and an owned namespace.
Use the existing environment's Python; no additional packages or downloads are needed.

```powershell
python -I -B scripts/test_parallel.py --root <checkout> --namespace-record <record.json> --config <config.json> --output <new-report-directory> --quiet-file scripts/test_parallel_quiet.txt --workers 4
```

The config is `{"environment": {...}}` with explicit `LATET_TEST_DATABASE_URL`, `LATET_TEST_TLS_DATABASE_URL`,
`LATET_TEST_VALKEY_URL`, `LATET_NER_MODEL_DIR` and `LATET_OCR_DATA_DIR` values for the existing services and models.
Optional `"quiet_database": "tls"` at the config's top level routes the whole quiet child through the existing
`LATET_TEST_TLS_DATABASE_URL`; collection and ordinary workers retain `LATET_TEST_DATABASE_URL`. Omitting it retains
the same database for all lanes. Other values, or a missing/blank/nonstring TLS URL, fail before any child starts.
Keep this local config private. It is not copied into reports. The namespace record contains its absolute `root`,
native `device`/`inode`, and `marker_sha256`; its plain directory holds `homes`, `temp`, and the existing 32-byte
`.latet-e0c-owned` marker. The runner verifies these before use and creates fresh children beneath them.

Each child traverses the complete `tests` tree under the same isolation and then selects its assigned cases. This
preserves canonical collection order and conftest fixture ancestry. Every child's full inventory and collection
skip ledger must match the initial complete collection, and its deselections must be exactly the unassigned cases.
Ordinary cases of each module stay
together, balanced by case count; `--durations <previous-summary.json>` uses recorded module time instead.
`--quiet-selector <module-or-function>` can be repeated, and function selectors include all parametrizations.
The audited manifest keeps native model modules together and places exact timing cases in the quiet phase.
`--quiet-file` is required so the quiet policy cannot be omitted; a synthetic suite can supply an empty file.
Every ordinary child must exit with zero descendants before the single quiet child starts. The coordinator must
also prevent unrelated campaigns from overlapping this phase.

Every child has seven private home variables, a separate TEMP/basetemp and log, offline settings, a null keyring,
loopback sockets and the requested checkout's source. Inherited LATET/PYTEST configuration and hazardous PostgreSQL
overrides are removed. The existing Hypothesis profile is preserved. Children join a common 16 GiB Windows Job and
an individual Job before importing pytest or application code; the common per-process cap is 4 GiB.
Runner children use the current base interpreter with CPython's `__PYVENV_LAUNCHER__` marker to preserve the virtual
environment while avoiding a separate trampoline Job for each child. Each child verifies its interpreter paths
and prefixes before pytest imports. See the [CPython path initialization](https://github.com/python/cpython/blob/v3.13.16/Modules/getpath.py)
and [uv trampoline](https://github.com/astral-sh/uv/blob/0.12.23/crates/uv-trampoline/src/bounce.rs) sources.

`--seconds` bounds the whole run (default 1200); `--child-seconds` bounds each child (default 1200). Timeout,
interruption, missing reports, nonzero exits, changed inventory, partial phase reports and surviving descendants
fail the gate. Cleanup terminates only owned processes and Jobs. Hard termination can leave the test's random
database schema/key prefix behind; the runner never resets shared services.

`summary.json` retains complete child records, phase outcomes, warnings, collection skips, module durations and
whole-run elapsed time; individual reports, specs and full logs also remain. A setup skip is a test result;
collection skips have their own attributed ledger. `success` means complete passing execution and cleanup;
`target_met` additionally requires less than 600 seconds, including collection and cleanup. A slower passing run
does not meet the performance target. There are no automatic retries or assertion/profile changes.
