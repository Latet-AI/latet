---
title: Server operations
description: Operate the storage, network, keys, limits and observability of a Latet server.
---

These requirements apply to every instance of a configured Latet server. Keep its public
HTTPS address, PostgreSQL database, Valkey and master key consistent across instances.
[Latet on a server](../server/) explains which data reaches the server and what remains
readable in its stores.

## Configuration and keys

`server.yaml` holds instance settings. `--config` selects the file for server commands;
`LATET_SERVER_CONFIG` selects it through the environment. The corresponding
`LATET_SERVER_*` variables override file values. Instance settings are separate from
the shared settings and each identity's own settings and rules, stored in PostgreSQL.
The generated [server settings table](../../reference/configuration/#server-settings) lists every
instance setting, its default and its purpose.

Supply the master key through `master_key_file` or the supported master-key environment
input. Prefer a protected secret file; do not put it in a repository or command line.
All instances need the same key. Back it up separately with restricted access: replacing
the file with a new random key does not rotate encrypted data and makes existing data unreadable.
A vault entry that does not open (sealed with another key, or changed in the database) is left
out: the log names its id, never its value, and masking goes on with the other entries.

Use `database_password_file`, `valkey_password_file`, `metrics_token_file` and
`reviewer_key_file` for their respective secrets. The reviewer key is read once at startup
and goes only to the shared reviewer chosen by the administrators. It must be that
reviewer's credential, never a master key or administration token. The server does not
look up a reviewer's key in a person's environment, keyring or saved local key.

## Host services

Install the server package in a trusted machine-wide location, outside personal home
folders. Prepare `server.yaml` with the URLs, secret-file paths and TLS configuration
described above; the service must be able to read those protected files. Precreate the
master key and run initialization in your private administrator terminal: its first
administration token is printed once. Do not capture that output in shared logs.

On Linux, a few things come first, as root. Install Latet with the `server` extra for the
machine: uv's folders for tools, commands and Python all outside the home folders. Create
the `latet` user and group that the service runs as, then the folder that holds the settings
and the secrets, with the master key and the database's password in it:

```sh
UV_TOOL_DIR=/opt/latet/tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/latet/python \
  uv tool install --python ">=3.12,<3.14" "latet[server] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
useradd --system --user-group --no-create-home --shell /usr/sbin/nologin latet
install -d -m 0750 -o root -g latet /etc/latet
(umask 077 && openssl rand -base64 32 > /etc/latet/master.key)
(umask 077 && openssl rand -hex 32 > /etc/latet/db.password)   # or the password your database administrator gives
```

Put Valkey's password in `/etc/latet/valkey.password`, and the TLS certificate and its key in
`/etc/latet/server.crt` and `/etc/latet/server.key`. Then write `/etc/latet/server.yaml`; a
minimal one for a host service:

```yaml
url: https://latet.acme.lan
database_url: postgresql://latet@db.acme.lan:5432/latet?sslmode=verify-full&sslrootcert=system
database_password_file: /etc/latet/db.password
valkey_url: rediss://valkey.acme.lan:6379/0
valkey_password_file: /etc/latet/valkey.password
master_key_file: /etc/latet/master.key
certificate: /etc/latet/server.crt
key: /etc/latet/server.key
```

Give `server.yaml` and each secret file it names, `key` included, to root and the `latet`
group, readable by them alone (`0640`, or `0440`):

```sh
cd /etc/latet
chown root:latet server.yaml master.key db.password valkey.password server.key
chmod 0640 server.yaml master.key db.password valkey.password server.key
```

Add `ner` and `ocr` to the extra for name detection and text recognition. The service
installation says which of these it finds missing: an installation under a home folder,
no `latet` user or group, or a file with another owner or mode.

The default configuration is `/etc/latet/server.yaml`, the unit is `latet-server`, and
its data directory is `/var/lib/latet`:

```sh
latet server init --config /etc/latet/server.yaml
latet server service install --config /etc/latet/server.yaml --print
latet server service install --config /etc/latet/server.yaml
systemctl start latet-server
```

On Windows, the default configuration is `C:/ProgramData/Latet/server.yaml`.
The service runs as `NT SERVICE\LatetServer`, with protected directory ACLs.
Its log is `C:/ProgramData/Latet/logs/server.log`, rotated across five 10 MiB files:

```powershell
latet server init --config C:/ProgramData/Latet/server.yaml
latet server service install --config C:/ProgramData/Latet/server.yaml --print
latet server service install --config C:/ProgramData/Latet/server.yaml
Start-Service LatetServer
```

`--print` shows the installation plan without installing it. Actual installation needs
Linux root or Windows administrator privileges and enables the service at boot; start
it explicitly when ready. Agent invocations are refused even with `--print` or
`--yes`. At your own terminal the installation asks before it changes anything, `--yes` or
not; `--yes` lets a script of yours, with no terminal, install without the question. Service
installation in a container is refused. These commands describe the installation procedure, not a preinstalled
service on your machine.

## The Compose server profile

Use the repository's `compose.yaml` and `deploy/Caddyfile`. Prepare a private host
directory `secrets/`: on POSIX it is administrator-owned with mode `0700`; the
individual bind-mounted files use `0444` so each container's user can read them.
On Windows restrict the directory and file ACLs to the administrators and the Docker
service account that needs access. Compose bind secrets do not remap file owners or
modes; changing every file to `0600` can make it unreadable inside a container.

Create `master.key` (32 random bytes in base64), `db-admin.password`, `db.password`,
`valkey.password` and `metrics.token` in that directory before initialization. Use strong
random values; `db.password` and `valkey.password` must be hexadecimal for the supplied
startup commands (`openssl rand -hex 32`). Never replace an existing master key as a
setup shortcut. The optional `reviewer.key` requires uncommenting its file setting,
secret mount and definition together. These secrets come from files only, without
environment fallback. Supply the TLS files in `certs/` and keep the private key protected.

The instances connect to PostgreSQL as the role `latet`, which is not a superuser. On a
new data volume, `deploy/postgres-init.sh` creates it with the password in `db.password`,
as the owner of the database `latet`. PostgreSQL's own superuser, `postgres`, has
`db-admin.password`, which no instance is given: a compromised instance cannot run a
program in the database's container (`COPY … PROGRAM` needs a superuser). The script runs
only while the volume is new and changes no existing database. A deployment whose volume was
made before it keeps `latet` as PostgreSQL's own superuser, an attribute PostgreSQL never lets
that role lose (`ALTER ROLE latet NOSUPERUSER` is refused): to move it over, dump the database
with `pg_dump --no-owner --no-privileges`, start PostgreSQL on a new volume, and restore the
dump as `latet`.

Write the server's address in a file named `.env` beside `compose.yaml`, which Compose reads
for every command, so that an upgrade run later from another terminal keeps it (it holds no
secret):

```sh
printf 'LATET_SERVER_URL=https://latet.acme.lan\nLATET_SERVER_HOST=latet.acme.lan\n' > .env
```

The following POSIX-shell example selects project `latet`, whose internal network is
`latet_latet-back`. Initialization uses Docker's `--log-driver none` because it prints
the administrator token; keep the attached terminal private and transfer that token
to your secret store. Check initialization succeeded before starting the instances:

```sh
# --wait returns only once PostgreSQL and Valkey are healthy (at most 120 s): go on only if it succeeds
docker compose -p latet --profile server up -d --wait --wait-timeout 120 postgres valkey
docker run --rm --log-driver none --network latet_latet-back \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  --mount "type=bind,source=$PWD/secrets/master.key,target=/run/secrets/latet-master-key,readonly" \
  --mount "type=bind,source=$PWD/secrets/db.password,target=/run/secrets/latet-db-password,readonly" \
  --mount "type=bind,source=$PWD/secrets/valkey.password,target=/run/secrets/latet-valkey-password,readonly" \
  --mount "type=bind,source=$PWD/secrets/metrics.token,target=/run/secrets/latet-metrics-token,readonly" \
  --env LATET_SERVER_URL=https://latet.acme.lan \
  --env LATET_SERVER_DATABASE_URL=postgresql://latet@postgres:5432/latet \
  --env LATET_SERVER_DATABASE_PASSWORD_FILE=/run/secrets/latet-db-password \
  --env LATET_SERVER_DATABASE_PRIVATE_NETWORK=1 \
  --env LATET_SERVER_VALKEY_URL=redis://valkey:6379/0 \
  --env LATET_SERVER_VALKEY_PASSWORD_FILE=/run/secrets/latet-valkey-password \
  --env LATET_SERVER_VALKEY_PRIVATE_NETWORK=1 \
  --env LATET_MASTER_KEY_FILE=/run/secrets/latet-master-key \
  --env LATET_SERVER_METRICS_TOKEN_FILE=/run/secrets/latet-metrics-token \
  ghcr.io/latet-ai/latet:latest server init
```

After initialization exits successfully, start the instances and Caddy:

```sh
docker compose -p latet --profile server up -d latet-server-1 latet-server-2 caddy
```

Within the server profile, only Caddy publishes a port: `443`. The separate default
computer service `latet` remains in the Compose file; the explicit service list above
does not start it. The front network is `172.28.0.0/24`, with automatic allocation from
`172.28.0.128/25`; fixed Caddy address `172.28.0.2` is outside that pool and is the
only trusted proxy. The back network is internal.

Caddy uses the supplied TLS files, a **32 MiB** request limit, immediate streaming flush
and WebSocket upgrades. Keep access logging disabled and retain the global
`log default { exclude http.log.error }` exclusion: proxy errors can otherwise log
request URIs and custom authentication headers. The supplied Caddy has no rate limiter;
per-client limits belong at an external proxy or WAF.

Server containers use bounded, nonblocking JSON-file logs. Instances run as the image's
own unprivileged user, uid and gid `10001`, never as root. They have read-only
roots, dropped capabilities and a temporary `/tmp`; there is no production instance
memory limit in this example. Budget for spaces, models and concurrent OCR work rather
than copying smoke-test limits. Valkey has a bounded memory allocation with `noeviction`.
The image includes its third-party notices.

## PostgreSQL and Valkey

For PostgreSQL off the instance's machine, use verified TLS in the connection URL, for
example `?sslmode=verify-full&sslrootcert=system`, or name the trusted certificate file.
`verify-full` without `sslrootcert` is insufficient here. Hostless URLs and service-name
connections are refused; `PG*` environment variables do not supply missing connection
settings. Percent-escape reserved password characters in URLs, or use the password file.
The connection timeout does not include DNS resolution.

Use Valkey 7.2 or later, or Redis 7, as one logical server; replicas are possible, Cluster
is not supported. Its scripts operate across several key prefixes. Set `maxmemory-policy`
to `noeviction`, provision memory for the expected spaces and monitor capacity. Losing
coordination state is not equivalent to evicting a harmless application cache.

Require authentication and verified TLS (`rediss://`) when Valkey is off the instance's
machine. The `database_private_network`, `valkey_private_network` and
`valkey_unauthenticated` exceptions are explicit deployment decisions for services that
only the intended instances can reach; do not use them to suppress a certificate or
authentication error on an ordinary shared network.

Before using this recipe, verify that the Valkey user `latet` does not exist. It creates
a fresh user; if that user already exists, stop and have the operator review its ACL,
passwords and selectors. Applying the command to an existing user adds to its configuration;
this recipe does not reset it or rotate its passwords.

Give the application its own Valkey user with this ACL. It uses the default `latet:`
namespace; replace the placeholder `password` through a protected administrative
connection. This is a Valkey command, not a shell command; keep the real secret out of
shell arguments, history and repositories.

```text
ACL SETUSER latet on >password ~latet:* resetchannels &latet:identity:* &latet:approvals:* &latet:feed:* &latet:shared &latet:identities &latet:signins &latet:beat +@all -@dangerous -monitor -config -keys -debug
```

Keep the subscription patterns literally as written: pattern subscriptions require
those exact grants. Lua scripting and `SCAN` remain available for coordination and
scoped purge; `MONITOR`, `CONFIG`, `KEYS` and `DEBUG` are denied. An isolated automated
test exercises this recipe with only its user, password and namespace substituted;
it does not inspect or configure your deployed service.

Run migrations as the schema owner. The first migration creates the schema and needs
`CREATE` on the database; later migrations do not need that creation privilege. The
migration lock waits at most 600 seconds. An application role without `DELETE` on
`audit` and `audit_anchor` cannot prune the administration log: grant those rights or
keep the owner's role for the instances. Keep a recoverable backup before an upgrade.
A connection pooler must preserve required PostgreSQL startup `options`.

### Sizing Valkey

The per-identity byte budgets are 32 MiB for texts as they left, 64 MiB for the approval
queue and 16 MiB for referenced feed events. Their sum, **112 MiB, is not a total memory
ceiling**. Other families are limited by entry count, so their record sizes also matter.
Conversation storage permits 1,000,000 fields and approved texts across at most 10,000
conversations. Checked and recorded block marks each permit 200,000 entries; reverse
texts and response mappings each permit 50,000, AWS sessions 200 and dashboard sessions
10 per person.
Reverse texts are model outputs remembered so they can be recognized when sent back
in a later request.

The following are planning examples, not measured usage. Conversation entries are assumed
to occupy 256 bytes each and block marks 192 bytes each. Other sizes are encoded plaintext:
the estimates add the 30-byte encryption frame, allocation rounding and 256 bytes of
estimated key/index overhead. The three byte budgets already count that overhead.

| Family | Moderate example | Count-saturated example, same record sizes |
| --- | --- | --- |
| Texts / queue / events | 8 / 8 / 1 MiB | 32 / 64 / 16 MiB |
| Conversation entries | 20,000 at 256 bytes | 1,000,000 at 256 bytes |
| Checked / recorded marks | 10,000 each at 192 bytes | 200,000 each at 192 bytes |
| Reverse texts | 5,000 at 2 KiB | 50,000 at 2 KiB |
| Response mappings | 5,000 at 64 bytes | 50,000 at 64 bytes |
| AWS sessions | 10 at 2 KiB | 200 at 2 KiB |
| Dashboard sessions | 2 at 128 bytes | 10 at 128 bytes |
| **Modeled subtotal per identity** | **40.76 MiB** | **581.75 MiB** |

With an illustrative 50% reserve these become 61.13 and 872.62 MiB per identity, or
about 5.97 and 85.22 GiB for 100 identities. Budget separately for Valkey's base memory,
connections, replication/persistence and any remaining structures or fragmentation that
exceed that reserve. Count identities whose data is still retained, not just those
making requests now. Application-process caches and OCR models need separate RAM.

The saturated example is **not an absolute worst case**: its small record sizes are
assumptions. The writer's 4 MiB waiting queue does not cap the persistent reverse family
at 4 MiB. A succession of nearly 4 MiB records can approach an adverse envelope of
195 GiB for 50,000 reverse records alone, before other families. This is a loose
source-derived estimate, not a measured workload or total quota. A long conversation
key or folder can also exceed the assumed entry size. Keep `noeviction`, monitor actual
record sizes and occupancy, and plan for the admitted workload; do not promise a compact
per-identity memory ceiling from the entry counts alone.

Latet 0.11 adds three kinds of keys, each bounded, none holding a text, a value or a fake:

- **What the provider gave a conversation** (thinking signatures, encrypted reasoning,
  compactions), as keyed hashes, so that the next turn passes on any instance: at most 20,000
  entries and 4 MiB per identity, and 256 MiB for the whole server, kept a week after their last use.
  A write Valkey refuses (a cap, or `noeviction` out of memory) records nothing: such an item is then
  let through by the built-in rule for the provider's encrypted fields where it applies, or refused,
  never answered `503`.
- **Launch keys** of agents started by Latet on connected computers: one sealed record of at most
  16 KiB per launch open at the moment, at most 64 at once per identity.
- **Request exceptions used while the history is off**: one row per instance and identity, holding
  ids and numbers alone, refreshed as the instance checks its spaces and gone 30 seconds after the
  instance stops. Valkey as the Compose profile ships it keeps nothing on disk (`save ""`,
  `appendonly no`); an operator who turns on its persistence writes these counts to disk.

## HTTPS, proxies and capacity

Terminate TLS at the instance or the load balancer. Set `forwarded_allow_ips` to the
actual trusted proxies, never `*`; forwarded headers from other addresses are not trusted.
Keep clocks synchronized, within 60 seconds. Restrict direct provider access separately
if the server is the company's required control point.

The default request body limit is 32 MiB. `requests_in_flight` defaults to 16 per identity
on an instance, with separate allowances for destination, dashboard, bridge and applications API
requests.
Long polls and streams have their own bounds. A capacity refusal is a reason to inspect
load and limits, not to disable protection. Shutdown gives an instance 30 seconds to
finish; long-running streams may be cut, so clients must handle disconnection.

An instance does most of its work on one processor core. A machine with more cores serves more
through several workers on Linux ([below](#several-instances-on-one-machine)), and a server serves
more through more instances behind the load balancer.
[How fast it is](../../applications/#how-fast-it-is) gives what one instance and several workers
masked through the applications API on the measuring computer.

Latet 0.11 reads every part of each request it forwards
([Upgrading to 0.11](../../getting-started/upgrading-to-0-11/)), which costs time on each. Measured
with one instance on Windows on that computer, at the median, against the same release without that
reading: a small chat request took 6.99 ms instead of 5.95, the first request of a new key 104 ms
instead of 97, a JSON body of 1 MiB 391 ms instead of 373, a request of 4,000 slots 1,088 ms instead
of 779, and a request of 500 texts masked before and one new 183 ms instead of 158.

OCR uses bounded readers, each holding model memory. `ocr_readers` defaults to 2 and
cannot exceed the processors available to the instance (with several workers, a worker's share of
them: the machine's processors divided by the workers). `ocr_read_seconds` defaults to
30; `ocr_wait_seconds` defaults to 35 and cannot be shorter than the read budget. Raising
reader counts costs memory as well as CPU. An unread image is withheld or its checked
request refused according to the protected path; increasing timeouts does not improve
recognition accuracy.

An API key's space holds in memory the values the key used last (`key_working_values`,
5,000 by default, about 10 MiB a key), the prints of its whole vault's words (about 15 MB
for a million values), and, for good, its newest short secrets that no word of a text finds
when other letters are glued to them (`key_held_secrets`, 1,000 by default, about 23 KB
each, most of it in the detector that finds them). Each instance and each worker that serves
the key holds all of it. While it builds the index of a vault's prints, an instance holds
more for a moment: 49 MB more for 1,000,000 values, for about 1.4 seconds, within the 64 MB
it sets aside first, and two builds at once at most. Raise `key_held_secrets` for a key that
sees many short secrets, such as a session token in each log line: every text the key masks
or unmasks is read for each one held, so its calls slow a little as well (unmask about a
tenth slower with 1,000 held, measured with Latet 0.10.0)
([a key's vault](../../applications/#a-keys-vault)).

A person's space built while the vault has 35,000 values or fewer holds it whole in memory
(about 1.6 KB a value on each instance that serves the person, counted in the space's
memory): the person's texts are masked without asking PostgreSQL for their values, about
twice as fast as looking them up. Each request still asks PostgreSQL first what the other
instances changed, so a value kept or forgotten on one instance is seen by the next request
on any other. Past 35,000 values, and in a space built past them until it is made again (even after values
are forgotten), a person's space works as an API key's: it holds in memory
the values the person used last (`key_working_values`) and, for good, every value that no
word of a text finds, whatever its age, and finds every other value in PostgreSQL as a text
needs it, so no instance holds a large vault whole. A vault kept by an earlier Latet is
served from memory, as before, until its words are made, once, in the background on one
instance; the space built after that works from PostgreSQL.

Measured with Latet 0.11 on the computer of [how fast it is](../../applications/#how-fast-it-is), on the engine
(the gateway's masking alone, not through HTTP), one line at a time, in turns of 500 lines with Latet 0.10.0's
vault in the same process, five each (medians of the turns): a person's vault of 20,000 values, held whole,
masked 851 lines a second against 558 for 0.10.0, 1.52 times where this release set out to reach twice; one of
5,000 values 885 against 1,094, 0.81 times, since the read of the change counter made before each line, about
half a millisecond there, is a large part of the time of one line; one of 200,000 values, on its store, 385
against 59. At 200,000 values a person's space took 24 MB more after its lines, where 0.10.0's vault took 495
MB.

A person's space holds every value that no word of a text finds when other letters are glued
to it, whatever `key_held_secrets` says (about 23 KB each on every instance). A request
whose texts go past the bounds of one lookup, such as a lockfile or a long log of hashes, is
never refused: a person's space that holds its vault whole reads it from memory; a larger
one reads its whole vault once for it, shares that reading with the requests that need it at
the same time, and counts it in the space's memory while it lasts (at 200,000 values, measured
with Latet 0.11, the first 500 lines past a bound took about 10 seconds with the reading among
them, and the reading held about 380 MB on the instance or worker that made it, about what
0.10.0's vault held); an API key's space looks its texts' words up as Latet 0.10.2 did.

### Connections, budgets and trust

- **Refused credentials** count against the address they come from: an IPv4 address, or for IPv6 its /64 and,
  together, its /48. A credential found valid within the last hour, and a sign-in link the server issued, skip
  that limit, so a NAT neighbour's failed attempts hold back only credentials not seen valid lately, for as long
  as the attempts go on; a proxy or firewall in front that limits each client avoids it. While the threads that
  check credentials are busy, enrollments and credentials new to every instance get 503 with `Retry-After`, and a
  dashboard sign-in link is sent to `?login=busy`.
- **A computer's token and key:** the token a computer's agents hold opens the plugin's bridge, the space's
  proxy and the computer's two calls at an agent's start (a destination added for an address, which lowers
  nothing, and the user name's fake), says whose it is and revokes itself; a sign-in link to the dashboard and a change of the person's
  identity also ask for the computer key (header `x-latet-computer-key`), which stays in that computer's
  credential store. A missing or wrong key is refused (403 `computer_key_required`) and counts against its
  address like a refused credential.
- **One identity** has at most `requests_in_flight` requests in flight on an instance (16 by default); past it,
  429 with `Retry-After`. A body is read up to `max_request_mb` (32 MiB by default), a call of the applications API up to
  `apps_request_mb` (8 MiB); a JSON body near that limit
  holds the interpreter's lock for one to three seconds wherever it is parsed, so keep the limit as low as your
  agents allow and run several instances.
- **PostgreSQL:** each instance keeps up to `database_pool` connections (10 by default), for every identity's
  work, and each worker is an instance; PostgreSQL's `max_connections` must cover every instance's pool and the
  administrators' commands. While it builds the index of a vault, an API key's or a person's, an instance also
  opens up to two connections of its own, closed when the build ends: count them in `max_connections` too. With
  none to spare, the build uses one of the pool's. An applications API call that keeps new values holds one of
  them until it ends, three tenths of the pool at most (three of ten); past them, calls keep each new value at
  once, which is slower. Raise `database_pool`, and `max_connections` with it, for an instance that masks for
  many new keys at once.
- **Valkey:** each instance, each worker too, keeps up to `valkey_connections` connections (100 by default), and
  past them a call waits five seconds at most. Valkey's `maxclients` must cover every instance's connections.
  Connecting to Valkey, its TLS and its first answer share one budget of five seconds.
- **Memory:** each instance, each worker too, gives the spaces it holds 2 GiB, weighed from the counts their parts
  keep rather than read from the process. One space takes at most half of it, and its detectors up to an eighth of
  that half more (128 MiB), which a busy space keeps while its requests run: several busy spaces can pass the 2 GiB
  by that much each. The process itself, the recognition models and the indexes being built come on top. A worker
  is started again when it ends or goes silent, never for its memory: plan the machine's memory for N workers
  ([several instances on one machine](#several-instances-on-one-machine)).
- **The providers' certificates:** an instance trusts the operating system's certificate store; on Linux,
  OpenSSL's default paths, which `SSL_CERT_FILE` and `SSL_CERT_DIR` change for the whole process. Change them
  only to add your company's authority.
- **`SSLKEYLOGFILE`** must not be set for an instance: the TLS connections that read it, Valkey's among them,
  write their session keys to that file, and whoever reads it reads that traffic. An instance started with it
  says so in its log.

## Upgrading and adding instances

From 0.10 to 0.11, read [Upgrading to 0.11](../../getting-started/upgrading-to-0-11/) first: 0.11
refuses the parts of requests it does not read, on a server as on a computer, and `latet doctor`
lists what will stop.

0.11 also changes the tables, so the server stops for its migration:

1. Take the copy of the database described below, and run `latet server migrate --check` with 0.11:
   it says that the tables change.
2. Stop every instance, every worker of every machine. A server still on 0.10 answers
   `latet server status` with the instance that answered alone, since 0.10 does not report its
   instances: the check that none is left is the next step's.
3. Wait 30 seconds, then run `latet server migrate` once, with 0.11: an instance of 0.10 leaves its
   key in Valkey as it stops, and counts as alive until the key ends, 30 seconds after its last beat.
   While an instance of 0.10 or 0.11 is alive, it migrates nothing, says how many are alive and of
   which version (`1 instance of the server is alive (1 of Latet 0.10 or older): nothing was
   migrated`) and exits 2: stop them and run it again. With a migration to apply, it and
   `latet server init` migrate nothing either while Valkey does not answer. The migration adds to each
   word row of every vault an empty place for its print and for the count of the change that wrote it
   last, with their indexes: on a database of large vaults it takes longer than earlier migrations
   did, and the prints themselves are made later, in the background (below). Once the migrations are
   applied, it asks Valkey again, and when an instance came alive meanwhile it says so after what it
   applied and exits 1 (`The tables are migrated, and 1 instance of the server is alive (1 of Latet
   0.10 or older): stop it, then start it with this version of Latet`).
4. Start every instance with 0.11. No call is answered between the stop and this start.

Then, in the background: the prints of each vault kept by 0.10 are made by the first instance that builds its
space, once, one instance at a time, and each instance builds its own index of them, for a person's vault as for
an API key's. Until then that identity's requests and calls are answered by looking their words up in
PostgreSQL, as 0.10 did, more slowly, and none is refused for it. An instance whose index cannot be built (the
prints not all made yet, PostgreSQL or Valkey not answering, a catch-up that failed) keeps that identity on
PostgreSQL's lookup and builds the index again: at once when another instance's fill of the prints ends,
otherwise 10, 20 and 30 seconds after each try and then once a minute, two builds at a time on each instance,
the next starting as one ends. The log says once when a space's index stops being used (`the vault index of <id>
is not used (<why>): its lookups go to PostgreSQL until it is built again`) and once when it is used again. A
vault past 16,777,216 prints, about eight million values, gets no index while its space lasts: its lookups go to
PostgreSQL, and a new space of it tries once again. A person's vault kept before Latet 0.10.0, whose values have
no words yet, is held whole in memory, as before, until its words and prints are made, once, on one instance;
the person's space built after that works from PostgreSQL. 0.11 does not make any key's words again ([upgrading
a server](../../applications/#upgrading-a-server)).

Latet reads each request by the profile of its route, which names every field, header and query
parameter it carries. At each Latet release, and at least every two weeks, the newest versions of the
agents Latet supports run through the requests these profiles describe. When an agent's update sends
something no profile names, the profile's new revision ships in a patch release; until you install it,
that request is refused, naming its category, and nothing of it is sent. Keep the instances on the
newest patch release of their version.

The instances share one set of tables, and an instance starts only on tables of its own version. The
database is yours to keep: Latet copies nothing of it. Before you pull a new image and run
`latet server migrate`, take a copy of it with `pg_dump`, which is consistent even while the
instances write:

```sh
pg_dump --format=custom --file=latet-before-upgrade.dump "postgresql://latet@db.acme.lan/latet"
```

To go back, restore that copy (`pg_restore --clean --if-exists`) and start the previous image: an
instance does not start on tables of a newer version. The values in the copy stay encrypted, as in the
database: only the server's master key opens them. Before an upgrade, ask the new version whether it
changes the tables:

```sh
latet server migrate --check
```

- **The tables stay the same:** replace the instances one at a time behind the load balancer. A
  stopped instance finishes its requests within 30 seconds.
- **The tables change:** stop every instance, run `latet server migrate` once with the new version,
  then start the instances with it. `latet server migrate` refuses while an instance is alive (one of
  0.10 for 30 seconds after it stops), and an instance does not start on tables of another version.
- **Programs use the applications API:** stop every older instance before the new ones start, even
  when the tables stay the same. While two versions run side by side, a value an older instance
  writes to a key's vault may not be found by a newer one until the key's space is built again
  ([upgrading a server](../../applications/#upgrading-a-server)).
- **A release that makes a key's words anew** (the changelog says so; 0.11 does not): the first
  instance that builds each API key's space makes the words of its vault again before it answers that
  key's first call, which took Latet 0.10.0 about 40 seconds for 200,000 values and four and a half
  minutes for 1,000,000. It is done once for each key, but an instance builds four spaces at once,
  people's and keys' alike, so several large keys built together after a restart hold back the first
  calls of every other space on that instance: send each large key one call, one key after the other,
  before the traffic comes back.

In the Compose example, the same steps run in containers of the new image, when the tables change
(the pause lets the keys of stopped instances of 0.10 end; an instance of 0.11 removes its own as it
stops):

```sh
docker compose -p latet --profile server pull
docker compose -p latet --profile server run --rm latet-server-1 server migrate --check
docker compose -p latet --profile server stop latet-server-1 latet-server-2
sleep 30
docker compose -p latet --profile server run --rm latet-server-1 server migrate
docker compose -p latet --profile server up -d latet-server-1 latet-server-2
```

To add an instance, start another one with the same settings, the same master key and the same
PostgreSQL and Valkey, then add its address to the load balancer. Instances keep their state in
PostgreSQL and Valkey, so any of them can answer any request.

### Several instances on one machine

An instance does most of its work on one processor core. To use more cores of a machine,
`latet server run --workers N` (or the `workers` setting, `LATET_SERVER_WORKERS`) starts N instances,
called workers, on the one port, each a process of its own: they share PostgreSQL and Valkey as
instances on N machines would, and the load balancer stays as it is. In the Compose example, set
`LATET_SERVER_WORKERS` in `.env`. Start with one worker for each core the machine gives the server.

- **How many:** `--workers` on the command line, else `LATET_SERVER_WORKERS`, else `workers` in
  `server.yaml`, else one; from 1 to 64. The settings refuse a count whose metrics ports (below) would
  go past 65535 or take the instance's own port. In the Compose example each server container starts
  its own workers: two containers with `LATET_SERVER_WORKERS=4` are eight instances, with eight pools
  of connections.
- **What they give:** measured on Linux through the applications API, eight workers masked 6.28 times
  what one masked and unmasked 5.21 times, where this release set out to reach 5 times
  ([how fast it is](../../applications/#how-fast-it-is)). Fewer free cores, or a few long-lived
  connections, give less: N workers are not N times one.
- **How connections reach them:** on Linux each worker listens on the port with a socket of its own,
  and the system spreads new connections across them by a hash of each connection's addresses and
  ports. Many connections spread evenly; a few long-lived ones, such as a load balancer's, can still
  land unevenly, and more connections spread better. On macOS, which would give every connection to
  just one of several sockets on a port, the workers share one socket instead, and connections reach
  them less evenly. For connections spread by the kernel, run the server on Linux or in the
  container image.
- **The server's user:** on Linux, the process that starts the workers holds the port for the whole
  run, so no other program takes it, even while no worker listens; right after a server with one
  instance stopped on the same port, it waits up to a minute for that server's closed connections to
  clear. A program can listen on the port beside the workers only when it runs as the same user as
  the server, and it could then take a share of the connections, or all of them. Run the server as a
  user that runs nothing else: the `latet` user of the host service, or the container's own.
- **Memory and connections:** each worker holds about as much memory as one instance: on Linux about
  110 MB resident at rest, and 185 to 227 MB with an API key's space of 200,000 values after it
  served 200,000 distinct log lines, as measured for each worker with no recognition models; more for
  further spaces, its caches and the recognition models when they are installed. A worker that serves
  a person whose vault is held whole holds all of it, about 1.6 KB a value (about 58 MB at 35,000
  values). Plan N times that, and up to about 70 MB more for the process that starts and watches
  them. Text recognition gives each worker its share of the machine's processors. Each worker also
  has its own connections, `database_pool` to PostgreSQL and `valkey_connections` to Valkey: make
  PostgreSQL's `max_connections` and Valkey's `maxclients` cover every worker of every machine.
- **The process that starts them:** it builds no space and opens no database, master key or
  certificate; each worker does that in its own process. It starts with the server's settings and
  environment, and hands them to the workers, so protect it as an instance.
- **Metrics:** worker 0 answers Prometheus on `metrics_port`, each other worker on the next port
  (7283, 7284 and so on): scrape each one. `latet_process_memory_bytes` is a worker's memory (its
  resident set on Linux, its private bytes on Windows), and `latet_vault_index_bytes` the part of it
  its vaults' indexes take.
- **Watching:** a worker that ends, or sends no beat for 30 seconds, is ended and started again, at
  most once every ten seconds. A worker that cannot start three times in a row (its settings, its
  database or its certificate refuse it) stops every worker and the server, as a single instance
  stops, so that the service manager decides. A worker that is not ready (PostgreSQL or Valkey away)
  stays: the load balancer's readiness check decides. `latet server status` lists every instance
  alive, each worker of each machine, with its worker number, whether it is ready, its memory and its
  version; a worker that died stays listed for up to 30 seconds after the one that replaces it
  starts. A request a worker was serving when it ended is not answered: its client gets an error and
  must send it again, and none is answered unmasked. On Linux, the connections still waiting for a worker
  that ended are cut, and their clients must connect again, and a worker that goes silent keeps its
  share of new connections waiting until it is ended.
- **Handing waiting connections over (optional):** on Linux 5.14 and later, with
  `net.ipv4.tcp_migrate_req` set to 1, the system hands the connections waiting for a worker that
  ends to the other workers instead of cutting them. In the Compose example, uncomment the `sysctls`
  entry of the server's services; for a host service, put `net.ipv4.tcp_migrate_req = 1` in a file
  under `/etc/sysctl.d/` and load it with `sysctl --system`. It is not set by default: on an older
  kernel, Docker refuses to start a container that sets it.
- **Stopping:** a stop signal reaches every worker, and each has 30 seconds to finish what is under
  way, as a single instance has.
- **Windows:** several workers need Linux or the container image. On Windows `latet server run`
  refuses more than one: a socket there serves one process, and the only event loop that could share
  it ends a worker past 512 connections. Run one instance on each Windows machine behind the load
  balancer; the Windows service runs one, whatever the setting says.

### Upgrading a Latet installed as a host service

A Latet installed as a host service, as above, is upgraded with the command it was installed with,
run again with the new release's wheel, the same `UV_*` folders and the same extras (`server`, with
`ner` and `ocr` if it has them):

```sh
UV_TOOL_DIR=/opt/latet/tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/latet/python \
  uv tool install --upgrade --python ">=3.12,<3.14" "latet[server] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
```

Then ask the new version whether it changes the tables, as above (`latet server migrate --check`),
and restart the service (`systemctl restart latet-server`). On Windows, which replaces no file of a
program that runs, stop the service before the install (`Stop-Service LatetServer`) and start it
after (`Start-Service LatetServer`). `latet upgrade` installs in uv's own folders: on a Latet
installed in folders of its own, it stops before anything changes and prints this command, with the
folders it found.

Up to 0.10.4, `latet upgrade` named only name detection and the text in images, and uv took the
other extras away: a server it upgraded lost `server` (psycopg and redis), and `latet server run`
then refused to start. A server on 0.10.4 or earlier therefore moves to a later release once with
the command above, naming `server`, or with the installer fetched again from GitHub and run with the
same `UV_*` variables. A server that has already lost `server` gets it back with the command above:
the installer cannot know of an extra that is gone.

## Recognition models on a server

Name detection and text recognition read their models from the server's data folder, not from the
administrator's computer. Install them there, as the account the instances run as:
`latet ner install` and `latet ocr install` with `LATET_DATA_DIR` set to that folder. In the Compose
example the folder is the `latet-server-data` volume, mounted at `/data` in every instance: install
them from a container of an instance, then restart the instances.

```sh
docker compose -p latet --profile server run --rm latet-server-1 ner install --yes
docker compose -p latet --profile server run --rm latet-server-1 ocr install --yes
docker compose -p latet --profile server restart latet-server-1 latet-server-2
```

With the Linux service, the instances run as the user `latet`, and their folder is
`/var/lib/latet`, which systemd creates when the service first starts. Once it has started,
install the models as that user, then restart the service:

```sh
sudo -u latet env LATET_DATA_DIR=/var/lib/latet latet ner install --yes
sudo -u latet env LATET_DATA_DIR=/var/lib/latet latet ocr install --yes
systemctl restart latet-server
```

For the Windows service, this page gives no procedure for installing the models.

## Programs with an API key

A program reaches its routes with its API key and keeps its own provider key. Write the API key to
a private file, then print the variables that send the program through the server:

```sh
latet env --key-file program.key --server https://latet.acme.lan
```

The printed variables carry the route key: keep them as secret as the key, never in a repository or a shared
profile. Only the key's first token has these addresses: a token made with `keys add-token` has none,
whatever its permissions, and calls the applications API alone. `latet env` asks the server which
kind the token is: for such a token it prints no variable and says so.

The program must trust the server's certificate: one from an authority it already trusts, or the
certificate added to its own trust settings.

A program that masks its own logs or documents calls the [applications API](../../applications/)
instead: no variables, a token of the key in its `Authorization` header, with only the permissions
it needs. Its calls share the key's space with the key's agents, so they share its vault and fakes.

Two kinds of its work queue for the whole server, not for each key. Each unmask's entry of the
administration log is written before its answer, one at a time on the log's chain, by two threads of
each instance for every key's unmasks; and forgets run two at a time on each instance, for every key.
A key that sends many unmasks at once, or forgets a long list, delays the unmasks or the forgets of the
other keys, never an enrolment, a revocation or what agents send. The administration log grows by one
row for each unmask call.

## Logs, metrics and recovery

Application logs go to standard output as `text` or `json`. Protect and rotate the
deployment's log files. The administration log is separate and records operations,
including code issuance and credential changes. Read it with `latet server log` and
check its chain with `latet server log verify`. Keep the returned head outside the
database: deleting the chain's tail can leave the remaining chain valid.

The status command's chain verdict is cached and may be unknown; it is not a new full
verification. Prometheus metrics use a separate listener, `127.0.0.1:7283` by default,
and require `metrics_token_file`.
Distinguish an unknown chain verdict from a broken one. The
[administration reference](../../reference/server-administration/) defines log pagination
and the precise meaning of `latet_ocr_unread_total{reason}`.

Every unmask through the applications API is an `apps.unmasked` entry of the administration log, with
the token and how many values it restored, and every forget an `apps.forgotten` entry, with the token
and how many values it took out of the key's vault; scan and mask are counted in the metrics only
(`latet_apps_texts_total` and `latet_apps_values_total`, by operation, and the requests as
`latet_http_requests_total{kind="apps"}`), with no label for the key
([what the server records](../../reference/apps-api/#what-the-server-records)).

In this Compose configuration, scrape `127.0.0.1:7283` from inside each instance with
its metrics token. There is no published metrics port, Caddy route or supplied monitoring
network. A different deployment must explicitly arrange a protected, authenticated
scrape path; the application HTTPS port does not serve these metrics. The metrics
listener answers `GET /_latet/metrics` alone, any other path with `404`: Prometheus asks
for `/metrics` unless its `metrics_path` says otherwise.

One instance an hour walks the administration log's chain and keeps the verdict; the status and the metrics
show that verdict for two hours, and say it is unknown when there is none (a new server, or Valkey started
afresh). Latet has no tool that repairs a broken chain: compare it with the head you kept outside the
database, find the entry it names, and restore the database from a backup if the log was altered.

A Prometheus beside an instance scrapes it like this, every 30 seconds; any interval up to two
minutes works. The alerts wait longer than one scrape, because the first scrape after a start has no
store readings yet:

```yaml
scrape_configs:
  - job_name: latet
    scrape_interval: 30s
    metrics_path: /_latet/metrics
    authorization:
      credentials_file: /etc/prometheus/latet-metrics.token
    static_configs:
      - targets: ["127.0.0.1:7283"]
```

```yaml
groups:
  - name: latet
    rules:
      - alert: LatetDatabaseDown
        expr: latet_database_up == 0
        for: 2m
      - alert: LatetValkeyDown
        expr: latet_valkey_up == 0
        for: 2m
      - alert: LatetAuditChainBroken
        expr: latet_audit_chain_known == 1 and latet_audit_chain_ok == 0
        for: 5m
      - alert: LatetLogsDropped
        expr: increase(latet_log_dropped_total[10m]) > 0
```

Back up PostgreSQL, the matching master key and deployment configuration with appropriate
access controls. Test restoration in an isolated environment before relying on the backup.
Restoring an old database can restore old authorization state too: reconcile credentials
and revocations before reopening service. History retention and clearing the dashboard
history do not delete backups or the vault. Monitor those stores separately.
