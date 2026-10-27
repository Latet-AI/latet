---
title: Set up and administer a Latet server
description: Deploy a server, connect a computer and manage people, API keys and shared settings.
---

Run administration commands in your own terminal on a computer with Latet installed.
Read [how spaces and trust work](../../concepts/server/) before giving anyone access.
An administrator prepares the deployment; each person connects their own computer.
An AI agent can explain the steps and check the machine, but the commands that change the
administration, `config get`, `rules export`, `latet server init`, `latet server service install`
and `latet connect` refuse to run inside one: point it at [`llms-server.md`](https://github.com/Latet-AI/latet/blob/main/llms-server.md).
Never give an agent an administration token: with one, the commands that only read (the status, the
lists of people and keys, the log) run for it.

## Prepare the deployment

Choose the [host service or Compose deployment](../../concepts/server-operations/).
A host service needs a trusted machine-wide installation and protected configuration;
the Compose server profile supplies PostgreSQL, Valkey, two instances and Caddy.
Set the public HTTPS address and prepare the master key and secret files first.
Initialization prints an administration token: keep it in a private terminal and secret
store. Follow the operations guide's initialization with Docker logging disabled for a
container; an ordinary logged initialization can retain that token.

Use a certificate trusted by your computers. For a first trial, create a self-signed
server certificate and inspect its fingerprint:

```sh
latet server certificate --make --url https://latet.acme.lan --out certs
latet server certificate certs/server.crt
```

Keep `server.key` private. Compose mounts `certs/server.crt` and `certs/server.key` in
Caddy; a host deployment configures TLS at the instance or its trusted reverse proxy.
Give people the verified fingerprint through a separate trusted channel. A fingerprint
printed from an unverified network connection alone does not authenticate the server.

## Check the server

```sh
# a self-signed certificate: its fingerprint, verified as above, for every `latet server` command in this shell
export LATET_SERVER_FINGERPRINT="<fingerprint>"
latet server --server https://latet.acme.lan status
```

Give the administration token at the hidden prompt. The answer reports the database,
Valkey, identities and the instance that answered. It does not check every agent or prove
that all of a computer's traffic passes through Latet.

For repeated administration, `--admin-token-file FILE` reads a private file holding the
token, or a record bound to this server. Keep it outside repositories and agent workspaces.
Follow the permissions and origin checks in [Server administration](../../reference/server-administration/).
Do not export an administration token in the shell environment of an account where agents run.

In the answer, `database` is always true, since an instance that cannot reach PostgreSQL gives no status at
all, and `valkey` is usually true for the same reason: read their health in the metrics (`latet_database_up`,
`latet_valkey_up`). `audit` gives the chain's latest verdict (`ok`, `broken` or `unknown`),
and `vault` lists the identities at 90% or more of their vault's limit.

## Give a person their own space

```sh
latet server --server https://latet.acme.lan people add "Emily Carter" --email emily.carter@acme.com
latet server --server https://latet.acme.lan people list
```

The first command prints a one-time code. Give it privately to that person: whoever uses
it can connect as them and open their dashboard. Issuing a code is recorded in the
administration log. The code works once, within 24 hours. `people code "Emily Carter"` issues
another when it has expired or was lost, and the one before stops working; never put a code in a
ticket, shared channel or agent conversation.

A person's computers share one space: their vault, history and settings. To inspect the
computer tokens, use:

```sh
latet server --server https://latet.acme.lan people tokens "Emily Carter"
```

Revoke a lost computer's token by its listed id with `--revoke TOKEN_ID`.
`people disable "Emily Carter"` stops every token of hers within seconds and keeps her space
(`people enable` gives it back); `people remove "Emily Carter"` deletes her live server space,
including its vault, history and settings, once you confirm. `keys disable`, `enable` and `remove`
do the same for an API key. Neither operation erases copies in backups, browsers or model
providers. Read the confirmation before a removal.

On a removal the person's records in PostgreSQL go with them, Valkey's records of them are purged once by one
instance (again at the next check if that cannot finish), anything left there ends by its own lifetime, eight days
at most, and their dashboard and AWS sessions end at once.

## Connect a person's computer

After the server is ready and the administrator has issued the person's code, run:

```sh
latet connect https://latet.acme.lan
```

Enter the one-time code at the prompt. With a self-signed certificate, add
`--fingerprint FINGERPRINT`, using the fingerprint verified with the administrator.
Do not drop a pin to work around a certificate error. The connection record, including
a file selected by `LATET_CONNECTION`, holds metadata and the pin; the personal token
and the computer key are kept elsewhere, together, in the credential store or a private file.

For Claude Code, the connected helper is supported in a **Windows terminal**. Apply
its integration and restart Claude Code, or launch a single session:

```sh
latet agents apply claude
latet claude
```

`latet agents apply claude` and `latet global-install` write the personal token to the
user's Claude `settings.json` as `LATET_BRIDGE_TOKEN`; the command reports the location.
This is the installation exception to keeping the token out of agent files. The private
helper binding holds metadata, and the helper receives the existing token through stdin.
Software able to observe that process input can read it. Connected Claude Desktop,
VS Code, JetBrains, Linux and macOS helper use are not supported by this contract: on
Linux and macOS these commands say so up front and write no token, and the plugin holds back
every prompt.

That token opens the plugin's bridge and the space's proxy from anywhere the server is reached; it never
opens the dashboard, never changes the person's identity and never lowers their protection: the only
change it can make to their settings is adding a destination for an address, which lowers nothing. The
dashboard and the identity also need the computer key, which `latet connect` keeps with the token in the
credential store (or the private file) and which only `latet dashboard` and `latet identity send` read,
each refusing to run inside an agent; no agent is given it.

Use these commands on the connected computer:

```sh
latet status
latet doctor
latet dashboard
latet identity send
latet disconnect
```

`status` and `doctor` provide bounded diagnostics, not proof of every agent path.
`dashboard` opens the person's server dashboard; an agent cannot run it on a connected computer.
It prints the private sign-in link only with `--print-url`; keep that link private. `config`, `rules`, `llm`,
`project`, `identity` and `reload` refuse while connected, to read settings as well as to
change them; only `config schema`, `rules test` and `identity send` run. Use the server
dashboard or administration commands. `latet identity send` is the explicit way to resend
this computer's identity facts. `latet start` refuses too: nothing uses this computer's daemon
while it is connected.

`latet disconnect` revokes this computer's token and removes the connection's agent
bindings, including the saved Claude token. Revoking the token also ends the person's
dashboard sessions on their other computers. If revocation cannot be confirmed, read
the command's outcome and have an administrator inspect token ids before retrying.
On a connected computer, print mode (`claude -p`) is not supported: the plugin withholds
its requests and says so. Use the interactive terminal. The
[Claude Code guide](../../agents/claude-code/) distinguishes connected support from local surfaces.

Removing this computer's copies is not revoking the token. With `LATET_KEYRING=off` Latet saves no
credential in the operating system's keyring, but an older copy may still be there: `latet connect`
refuses while it is, and `latet disconnect` reads it, revokes it at its server and removes it; a locked
keychain may ask to be unlocked. If a connection or a disconnection is interrupted, a leftover temporary
token file is deleted unread, so the computer cannot revoke the token it held. Then, or when
`latet disconnect` says a copy remains, an administrator revokes it on the server: list the person's
tokens as above, find the right one by its id (the host-name label is only a hint: two computers can
share a name) and revoke it with `--revoke TOKEN_ID`. If the right token cannot be told apart,
`--revoke-all` revokes every token of that person, and each of their computers must then connect again.

## Give a program an API key

An API key has its own space and no person's dashboard or computer enrollment. Its
administrator manages its settings and rules. Create the secret in a new private file:

```sh
latet server --server https://latet.acme.lan keys add "Billing CI" --out billing.key
latet server --server https://latet.acme.lan keys list
```

Use a protected directory outside the repository for that file. The command prints the
identity and file path without printing the secret. Store the key in the program's secret
store. `keys rotate "Billing CI" --out replacement.key` replaces its credential;
update the program's key and route addresses together. A route key in an address is sensitive too.

A key can have several tokens, each with its own permissions among `scan`, `mask`, `unmask` and
`forget`, for programs that call the [applications API](../../applications/): an application that ships
its logs masked needs `mask` alone, the support tool that reads them `unmask` alone.

```sh
latet server --server https://latet.acme.lan keys add-token "Billing CI" shipper --mask --out shipper.token
latet server --server https://latet.acme.lan keys tokens "Billing CI"
```

The key's first token may scan, mask and unmask; `forget`, which takes values out of the key's vault, is
given to a token of its own ([forgetting values](../../applications/forget/)). Only the key's first
token has an address for models: a token made with `keys add-token`, whatever its permissions, is
refused there and calls the applications API alone, where each unmask is in the administration log.
Once a key has several tokens, `keys rotate` needs `--token` with the id `keys tokens` lists, and
`keys tokens "Billing CI" --revoke TOKEN_ID` stops one. `keys limit "Billing CI" 1000000` gives the
key's vault a limit of its own, above or below the server's `vault_limit`.

## Set the shared settings and API keys' rules

The shared settings hold what the server needs to run, the same for every space: the
destinations requests may go to (`upstreams`), the shared local reviewer (`llm_review`), text
recognition (`ocr`), the history and how long it is kept (`retention_days`).

```sh
latet server --server https://latet.acme.lan config get
latet server --server https://latet.acme.lan config add upstreams "{name: llm-acme-com, url: https://llm.acme.com}"
```

Without `--key`, `config get`, `set`, `add` and `remove` work on these settings only. A protection
rule, such as a term or the profile, is refused, with words that say how to share it by hand.
With `--key`, the same commands read and change an API key's own settings and rules, and
`latet server rules --key` manages its recognizers and imports a rules file into its rules:

```sh
latet server --server https://latet.acme.lan config --key "Billing CI" set profile max
latet server --server https://latet.acme.lan rules --key "Billing CI" import team-rules.yaml
```

`config get` and `rules export` can print values in clear (an API key's terms, the reviewer's
guidance), so keep their output private. A person's own rules belong in their dashboard, where
they set them.

To give a team the same rules, share them by hand: export them from one place
(`latet rules export FILE` on a computer not connected to a server, or **Export rules** on the
**Rules** page of a dashboard) and import the file in each (`latet rules import FILE`, or
**Import rules** in the dashboard); an API key's with `latet server rules --key KEY import FILE`.
On computers not connected to a server, a project's `latet.yaml` carries them in its repository.
Rules set for everyone, which nobody can lower, are planned for the Enterprise Edition.

A change to the shared settings that reduces a person's protection, such as a destination moved
to another address or the reviewer turned off, waits for their approval. Saving their own edits
does not approve a waiting shared change. A save can keep those edits while failing to
refresh the waiting proposal; read the result and reload before making another change.
An API key has nobody to approve such a change: it reaches the key's space at once, a change
that lowers protection too, so check what a change to the shared settings does to your programs
before you make it.
[Shared settings and own rules](../../concepts/server/) explains this distinction.

Use administration tokens with an end date for routine work. The
[administration reference](../../reference/server-administration/) covers issuing and
revoking them, private output files, recovery and reading the log.
