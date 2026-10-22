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
  uv tool install "latet[server] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
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

## HTTPS, proxies and capacity

Terminate TLS at the instance or the load balancer. Set `forwarded_allow_ips` to the
actual trusted proxies, never `*`; forwarded headers from other addresses are not trusted.
Keep clocks synchronized, within 60 seconds. Restrict direct provider access separately
if the server is the company's required control point.

The default request body limit is 32 MiB. `requests_in_flight` defaults to 16 per identity
on an instance, with separate allowances for destination, dashboard and bridge requests.
Long polls and streams have their own bounds. A capacity refusal is a reason to inspect
load and limits, not to disable protection. Shutdown gives an instance 30 seconds to
finish; long-running streams may be cut, so clients must handle disconnection.

OCR uses bounded readers, each holding model memory. `ocr_readers` defaults to 2 and
cannot exceed the processors available to the instance. `ocr_read_seconds` defaults to
30; `ocr_wait_seconds` defaults to 35 and cannot be shorter than the read budget. Raising
reader counts costs memory as well as CPU. An unread image is withheld or its checked
request refused according to the protected path; increasing timeouts does not improve
recognition accuracy.

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
  429 with `Retry-After`. A body is read up to `max_request_mb` (32 MiB by default); a JSON body near that limit
  holds the interpreter's lock for one to three seconds wherever it is parsed, so keep the limit as low as your
  agents allow and run several instances.
- **PostgreSQL:** each instance keeps up to 10 connections; PostgreSQL's `max_connections` must cover every
  instance's 10 and the administrators' commands.
- **Valkey:** each instance keeps up to `valkey_connections` connections (100 by default), and past them a call
  waits five seconds at most. Valkey's `maxclients` must cover every instance's connections. Connecting to
  Valkey, its TLS and its first answer share one budget of five seconds.
- **The providers' certificates:** an instance trusts the operating system's certificate store; on Linux,
  OpenSSL's default paths, which `SSL_CERT_FILE` and `SSL_CERT_DIR` change for the whole process. Change them
  only to add your company's authority.
- **`SSLKEYLOGFILE`** must not be set for an instance: the TLS connections that read it, Valkey's among them,
  write their session keys to that file, and whoever reads it reads that traffic. An instance started with it
  says so in its log.

## Upgrading and adding instances

The instances share one set of tables, and an instance starts only on tables of its own version. Before an
upgrade, ask the new version whether it changes them:

```sh
latet server migrate --check
```

- **The tables stay the same:** replace the instances one at a time behind the load balancer. A
  stopped instance finishes its requests within 30 seconds.
- **The tables change:** stop every instance, run `latet server migrate` once with the new version,
  then start the instances with it. An instance does not start on tables of another version.

In the Compose example, the same steps run in containers of the new image, when the tables change:

```sh
docker compose -p latet --profile server pull
docker compose -p latet --profile server run --rm latet-server-1 server migrate --check
docker compose -p latet --profile server stop latet-server-1 latet-server-2
docker compose -p latet --profile server run --rm latet-server-1 server migrate
docker compose -p latet --profile server up -d latet-server-1 latet-server-2
```

To add an instance, start another one with the same settings, the same master key and the same
PostgreSQL and Valkey, then add its address to the load balancer. Instances keep their state in
PostgreSQL and Valkey, so any of them can answer any request.

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

With the Windows service, the procedure for installing the models is not documented yet.

## Programs with an API key

A program reaches its routes with its API key and keeps its own provider key. Write the API key to
a private file, then print the variables that send the program through the server:

```sh
latet env --key-file program.key --server https://latet.acme.lan
```

The printed variables carry the route key: keep them as secret as the key, never in a repository or a shared
profile.

The program must trust the server's certificate: one from an authority it already trusts, or the
certificate added to its own trust settings.

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
