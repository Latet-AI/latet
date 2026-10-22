# Task 15 finite verification plan (preparation only)

Prepared against base `5956bb8`. No execution is authorized by this document while
the coordinator owns the test seat. Task 14 must be reviewed/merged and Task 21's
reviewed final interfaces rechecked before production edits. No source, Dockerfile,
Compose, Caddy, workflow or notices changes are part of this preparation commit.

## Evidence classes and entry gates

- S: static checks now: Ruff lint/format, own-source Pyright and Python AST parsing.
  These do not execute tests or import the application.
- F: future structural feature checks on the shipped files. Missing server services,
  extra, exclusions or Caddyfile are feature REDs, never caught security-guard REDs.
- R: actual Docker-rendered Compose JSON, generated only in the exclusive execution
  seat. `LATET_TEST_CONTAINER_COMPOSE_JSON` is this test-only evidence handoff, not
  a product setting. Its absence skips only the separately parametrized R cases.
- N: native container behavior, explicitly deferred. Source checks cannot prove
  isolation, secret bind readability, actual argv, logging, body limits or image contents.

The new test file has no subprocess, application imports, download or Docker calls.
Run it later with the existing enterprise interpreter, own-source PYTHONPATH and
all seven home/config paths plus TEMP/TMP in the owned scratch root. Use the existing
test-home containment instructions; no shared service lifecycle commands. Record
exact focused test results, then the coordinator schedules any required whole suite.
Do not collect tests now. The source configuration's missing feature assertions must
remain failures until implementation, never skip them to obtain a passing result.

## Finite execution sequence

1. Coordinator grants the exclusive seat after dependencies are reviewed. Make one
   new task-owned scratch directory outside system TEMP. Record branch/head, Compose
   and Docker versions, free disk, and hashes of only tracked inputs. No scanning of
   user folders. Never open `api-key.txt` or a user `.env`, including for diagnostics.
2. Create a fresh empty `compose-empty.env` and synthetic secrets/certificates in that
   directory. Linux/native-container secret evidence: administrator-owned secrets
   directory 0700, files 0444 inside it. Windows ACL and Linux bind UID behavior are
   different checks; a Windows chmod result is not proof of host privacy. Check
   effective container readability as UID 10001 and the actual database/Valkey users,
   with no output of file contents. Use pre-created, valid synthetic master-key
   format accepted by the reviewed CLI; never let init generate/print a master key.
3. Render default and server configurations with explicit absolute `--file`,
   `--project-directory`, `--env-file <owned>/compose-empty.env`, and unique
   `--project-name e0c-t15-<random>` for every invocation. Child environments are
   allowlisted: no inherited LATET credentials, COMPOSE_FILE, COMPOSE_PROFILES,
   COMPOSE_ENV_FILES or proxy settings; COMPOSE_DISABLE_ENV_FILE=1. Use explicit
   synthetic LATET_SERVER_URL/HOST and LATET_PORT. No command ever relies on cwd
   discovery or implicit `.env`. First `config --quiet`, then `--profile server
   config --format json` to a private JSON file. Bound each config call to 30 seconds.
   Use an owned override to point required bind files at owned synthetic files; it
   must change only those host file paths for this rendering. No environment fallback.
   Feed this actual render to R tests and retain hash + exact argv + exit code.
   Never fabricate a JSON fixture and describe it as Docker-rendered evidence.
4. Prepare a second owned smoke override: pin instances to the locally built image,
   remove the computer service from selected startup, bind Caddy's published 443 to
   an available loopback-only high host port, use noncolliding task-owned network
   subnets and matching Caddy/trusted-proxy address, and attach an owned fake-upstream
   service to the front network. Override ingress solely for the test, retain a
   separate R artifact proving the shipped 443/default contracts. No container_name,
   external network/volume or shared test-services resources. The fake upstream
   listens only in this isolated network; host control connects through loopback.
5. Build once, at most 20 minutes, under existing E.1 download authorization for
   Caddy/base images/build packages only. No model download or new package sync.
   A build failure stops N checks; report its bounded, redacted output. Never send
   operational files in a context: before the real build use a tiny owned synthetic
   context with the real .dockerignore and synthetic excluded names to prove the
   builder does not receive them, preserving required build inputs. This checks
   Docker ignore semantics; the unit test checks only explicit source entries.
6. Start only owned PostgreSQL and Valkey. Use a one-shot init container with
   `--log-driver none`, the same pre-created file secrets and owned networks. Capture
   its stdout directly into a private owned file/pipe; never console/CI output.
   Assert inspect LogConfig.Type=none before treating stdout as protected. Parse the
   first administration token privately using the reviewed final init output,
   never grep/echo it. No future harness interface is asserted before it exists.
   Retain redacted metadata only; dispose of the private capture with owned cleanup.
7. Bring up the two instances and Caddy with dependencies. At most 60 readiness
   attempts at 2-second intervals with a 2-second request timeout and a 180-second
   overall deadline. Every HTTP client uses trust_env=False, explicit certificate
   trust and no redirects; no request leaves task resources/loopback. Obtain an
   owned identity/API key via reviewed actual admin interfaces. Through Caddy send
   one synthetic known maskable canary to the fake upstream. Assert fake upstream
   received a mask instead of the original and the response restored as appropriate;
   prove both instances ready independently and cover requests hitting both. Keep
   payloads and credentials in memory/private scratch, never ordinary logs/output.
8. Native assertions, each bounded to 30 seconds: inspect actual published ports
   and network memberships, trusted proxy address, UID 10001, readonly rootfs,
   cap-drop ALL, no-new-privileges, tmpfs, Docker logging buffer/rotation, metrics
   listening only on each instance loopback:7283 and unreachable via Caddy/host,
   authenticated Valkey/Pg connectivity and unauthenticated Valkey refusal. Inspect
   process argv and ordinary logs privately and assert synthetic secrets absent;
   no dump of inspect Env, argv or raw logs into CI. Verify Valkey effective
   maxmemory/noeviction and unchanged pubsub buffer default, plus healthcheck auth
   variable supported by the installed image CLI. Assert init's credential absent
   from ordinary container logs and master-key output path never taken.
9. Caddy behavioral boundary: use an isolated byte-counting fake receiver and
   streaming synthetic body, not a huge allocation. Send boundary-valid JSON of
   exactly 33,554,432 bytes and +1 byte through the proxy. Confirm 413 above the
   cap and no over-cap forwarding; compare direct receiver behavior so an app's
   independent limit cannot masquerade as the proxy guard. Finite websocket and
   streamed response checks prove upgrade and prompt flushing; no artificial short
   read timeout. Bound each to 15 seconds. Inspect actual adapted Caddy JSON for
   body max_size=33554432 and absence of access log/metrics routes, then preserve
   private logs proving the synthetic route credential never appears.
10. Inspect built-image notices and installed wheel's bundled-license metadata:
    LGPL-3.0, accompanying GPL-3.0, psycopg replacement instructions, exact bundled
    libpq/OpenSSL/other licenses and tzdata Apache-2.0 when included. Run the existing
    gen_notices --check only with the installed dependencies required to make its
    inventory complete; a missing tool/input is a gap, never a reason to download
    now or silently omit a package. Compare actual image file contents/hash.
11. Cleanup in finally within 60 seconds: only the exact unique project and files
    recorded at step 1. `down -v --remove-orphans` with the same explicit files,
    empty env file and project name; verify project label ownership before removal.
    No scripts/server-services.py up/down, prune, shared resource teardown, broad
    glob deletion or deletion outside the resolved owned scratch root. On teardown
    failure report owned resource identifiers and stop, never widen cleanup.

All N assertions remain deferred until actually observed on supported container
runtime. CI parity requires production-workflow edits after the preparation gate;
this manifest alone does not add CI smoke coverage. Tasks 17/18 must document the
actual shipped secret permissions/init command, per-client WAF limits, memory budget
(spaces' 2 GiB plus models/OCR), optional reviewer file and loopback-only metrics.

## Finite mutation matrix (one change, focused witness, restoration each)

No mutations execute now. Use synthetic owned configuration copies/overrides; never
mutate live shared services or broaden host exposure. Record baseline input hash,
single diff, failing behavioral assertion and restored hash/pass. A parse/collection
failure or absent interface does not count. Stop after these 18 targets; a new
uncovered concern requires coordinator amendment, not an open-ended campaign.

| ID | Single guard removed | Required caught witness |
|---|---|---|
| M01 | Attach Caddy to back network | private network test on actual rendered copy |
| M02 | Unset internal back flag | private network test on actual rendered copy |
| M03 | Broaden trusted proxy to * | fixed Caddy trust test |
| M04 | Publish an instance port | only Caddy publishes test; no mutated deployment |
| M05 | Replace password-file setting with environment password | secrets-file test; synthetic value only |
| M06 | Replace noeviction with volatile-lru | Valkey memory test + effective-config witness |
| M07 | Put Valkey password in --requirepass / CLI -a argv | argv test + private native argv witness |
| M08 | Replace non-blocking logging with blocking | bounded logging test |
| M09 | Remove log buffer/rotation limit | bounded logging test, one selected option |
| M10 | Remove instance read_only | container restrictions test |
| M11 | Remove cap_drop / no-new-privileges | restrictions test, each subcase separately restored |
| M12 | Change Caddy 32MiB to 32MB or remove bound | Caddy source test + actual byte boundary witness |
| M13 | Enable Caddy access log | Caddy source test + private synthetic-route log witness |
| M14 | Publish/proxy metrics or broaden metrics host | metrics config test + native reachability witness |
| M15 | Remove one required ignore exclusion | explicit-entry test + synthetic actual-context exclusion witness |
| M16 | Remove init log-driver none | actual init command/inspect and synthetic credential log witness, once harness exists |
| M17 | Remove pre-created master-key selection | actual init private-output assertion, once harness exists |
| M18 | Remove runtime notices copy | image source test + actual image contents witness |

M11's two subcases and M12's two alternatives are explicit finite cases (20 total
mutant variants). M16/M17 cannot claim RED until the actual command/harness exists.
Reviewer-key opt-in, no server extra and unprivileged image user also have structural
tests; their absent implementation is F evidence, not fabricated security RED.
