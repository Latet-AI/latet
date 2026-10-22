---
title: Latet on a server
description: Spaces, storage, shared settings and the trust boundary of a Latet server.
---

A Latet server receives protected requests from connected agents. In gateway mode it
masks requests before forwarding them to the model provider and restores the protected
return path; recognized Claude Code requests use the plugin and verify-only boundary
below. Original data can reach the Latet server. Using a server changes who must be trusted;
it does not make detection infallible or protect traffic that never reaches Latet.

## Connected clients

The computer must use the server route and keep its connection and credential bindings
private. Server-side masking covers gateway clients. Claude Code also needs its plugin:
on the supported connected Windows terminal, the private helper binding and trusted
settings select the server bridge. The helper's personal token travels through stdin;
the metadata binding is not a token store. An unsafe or missing binding stops that path.

For recognized Claude requests, the plugin masks tool descriptions, and the server's final
check reads the descriptions, parameter schemas and examples of the tools too: a value left in
clear in them stops the request. Request markers do not prove the plugin ran.
Read the [protection scope](../protection-scope/) for this narrow exception and the
separate output-schema checks. [Connect a computer](../../getting-started/server/)
covers apply, diagnostics, identity resend and disconnection.

If Latet is turned off in a folder, a request an instance receives for it before the plugin has told the server
so is masked as if Latet were on: it fails safe.

### A computer's token and key

When a computer connects, the server gives it two secrets, each shown once. The **token** goes to the
agents: Claude Code holds it in its settings and environment (`LATET_BRIDGE_TOKEN`), where every command
it runs can read it. From anywhere the server is reached, the token opens the plugin's bridge and the
space's proxy (whoever holds it can have texts masked in the space and the fakes they hold restored), says
whose it is and revokes itself. The **computer key** stays in the computer's
credential store, or in the private token file, and no agent is given it: the server asks for it as well
before it gives a sign-in link to the dashboard or adds to the person's identity, so the token alone never
opens the dashboard, where every original can be revealed, and never lowers the person's protection: the
only change it can make to their settings is adding a destination for an address, which lowers nothing.
The key stops a token that leaked (from the environment, the settings, a log or one `curl`) from opening
the dashboard; it does not stop a command written to read both from the credential store.
`latet dashboard` and `latet identity send` refuse to run inside an agent on a connected computer, and
`latet disconnect` forgets both.

### What stays on the computer

- **A repository's `latet.yaml` is not applied.** The server reads none of the computer's files: what a
  team shares in its project file (a stricter profile, its terms and recognizers) does not reach the
  requests of a connected person. In such a project `latet doctor` and `latet claude` say so. Set it in
  your dashboard: import the project's `latet.yaml` on **Rules** with **Import rules**.
- **The check of CLAUDE.md files**, which Claude Code gives its auto mode reviewer, runs on the
  computer, in `latet doctor`.
- **Claude Code through a server needs a Windows terminal** in this version. On Linux and macOS its
  plugin holds back every prompt, and Latet says so up front where it would apply it. Print mode
  (`claude -p`) is held back on every connected computer.

### Certificates and the agents' own connections

Each agent trusts the server's certificate through its own variable, which Latet sets: `NODE_EXTRA_CA_CERTS`
for Claude Code and OpenCode, `CODEX_CA_CERTIFICATE` for Codex. It adds to what the agent already trusts, and
the copy Latet writes keeps the certificates the person had named there, a company's authority for example.
A self-signed certificate reaches the agents only in the form `latet server certificate --make` gives it,
limited to the server's own name or address; a certificate from the company's authority works as it is.

When they start, the agents also call their own vendors, outside Latet: Claude Code `api.anthropic.com`, with its
API key, unless its non-essential traffic is turned off in its settings; Codex `chatgpt.com` and `api.github.com`
for its plugins; OpenCode the npm registry and GitHub. None of these calls carries the space's address, its route
key or the token. Where the server must be the only way to the models, block the model providers' hosts,
`api.anthropic.com` and `chatgpt.com` among them, and turn off Claude Code's non-essential traffic; Codex's
plugin listing then fails. For example:

```text
allow  latet.acme.lan:443                        # the Latet server
allow  api.github.com:443, github.com:443        # Codex's plugins and OpenCode's start-up, if wanted
allow  registry.npmjs.org:443                    # OpenCode's start-up, if wanted
deny   api.anthropic.com:443, chatgpt.com:443    # the models only through the server
deny   every other host of the AI providers
```

## Spaces and instances

Each person and each API key has a separate space, with its own vault, history and settings.
The computers of one person share that space. An API key represents a program, has no
personal dashboard and gets its settings from the administrator.

An instance is one running Latet server process. Several instances can serve the same
public HTTPS address behind a load balancer, using the same PostgreSQL database, Valkey
and master key. PostgreSQL holds durable state; Valkey coordinates short-lived state.
Requests do not need sticky sessions. Adding instances does not create separate copies
of a person's space. See [Server operations](../server-operations/) for service requirements.

## What the server stores

The values and surrogates in a vault, recorded text and images, and a space's settings
are encrypted with its identity key. That key is encrypted with the server's master key;
shared settings are encrypted with the master key. Keep the master key separate from
database backups. Without the matching key, encrypted data cannot be recovered.

Some metadata remains readable in PostgreSQL: identity names and email addresses,
external identifiers, token labels and times, vault rule names, counters and times, and
administration log actors, actions, targets, identifiers, counts and client addresses.
History project and conversation identifiers use keyed hashes. Valkey uses keyed names,
encrypted sensitive payloads and signed coordination messages.

Encryption at rest does not hide originals from the running server or its operator.
Authorized dashboard views receive originals in the browser. An administrator can issue
a person's enrollment code and use it to access that person's dashboard; this operation
is recorded. An administrator can also configure a reviewer service that receives original
text. Trust the server administrators, the deployment operator and that reviewer accordingly.
Encryption and signatures do not prevent a database writer from restoring an older valid state.

Clearing recorded history removes this space's recorded exchanges, not its vault. It does
not erase backups, agent transcripts, provider records or copies already received by a browser.

## Shared settings and own rules

The shared settings are what the server needs to run, the same for every space: the
destinations requests may go to, the shared local reviewer, text recognition, the history and
its retention. Administrators set them. Protection rules (terms, recognizers, the profile,
policies and the rest) are each identity's own: people set theirs in their dashboard, and an
administrator sets an API key's with `latet server config --key` and `latet server rules --key`.

A team shares its rules by hand: one person exports them (**Export rules** on the dashboard's
**Rules** page, or `latet rules export` on a computer not connected to a server) and each person
imports the file in their own dashboard. [Export and import](../export-import/) describes the
file. Rules set for everyone, which nobody can lower, are planned for the Enterprise Edition.

A shared change that reduces protection, such as a destination moved or the reviewer turned
off, waits for the person's approval. Until then, their previously approved, stricter settings
remain in use. Saving personal edits does not approve the waiting shared change, even if the
save contains the same item. A personal save cannot silently remove a destination set for
everyone.

An API key has nobody to approve such a change: it reaches the key's space at once, a change that
lowers protection too, so check what a change to the shared settings does to your programs before
you make it.

Distinguish these save outcomes:

| Result | What it means |
| --- | --- |
| `409 settings_stale` | The settings changed since the page read them; nothing from this save was kept. Reload before editing again. |
| `409 settings_change_waits` | The waiting change cannot be combined with the approved settings now; nothing from this save was kept. |
| `409 settings_saved_change_pending`, `saved: true` | Your edits were kept, but refreshing the waiting proposal failed. Reload and inspect the pending change; do not blindly repeat the save. |
| `409 settings_terms_too_long` | A term exceeds the accepted length; nothing from this save was kept. |

Other limits and storage failures can also refuse a save. An HTTP error alone is not
proof that nothing was written; keep the returned reason and any partial-save indication.

### A person's own destinations

A destination a person sets in their own settings never leads into the server's own network. An address that is
private, unique-local, carrier-grade NAT, loopback, link-local, multicast or reserved is refused, written as an
address or as a name that resolves to one; so are the IPv6 forms that carry an IPv4 address or lead to a gateway
(6to4, Teredo, NAT64 prefixes). On a network with NAT64 the server learns its prefixes as RFC 7050 describes, and
until it has, it reaches no IPv6 address a NAT64 could translate: a name that also has an IPv4 address goes there.
A name is resolved again when the server connects, and the request goes to the address that was checked. No proxy
is used for these destinations. Only an administrator, in the shared settings, sets a destination inside the
network.

An IPv6-only server behind DNS64 on the well-known prefix therefore reaches no IPv4-only destination of a
person's own.

## A space's limits

- **Rules:** a person's own settings hold at most 500 recognizers, 500 field rules and 500 patterns of values
  never to mask, counted over the settings and all their projects. A save past them is refused.
- **Projects:** at most 200 for each person.
- **Values:** a value longer than 16,384 characters is redacted rather than replaced with a fake, and is never
  kept nor restored; a term holds 8,192 characters at most.
- **Memory:** an instance gives its spaces a fixed 2 GiB, and one space uses at most half of it. A space at its
  share learns no new value: a request that brings one is stopped with words that say so, until values are
  forgotten in the dashboard, rules or projects are fewer, or the server has more. No other person's space is
  closed to make room.
- **Statistics** count the latest 100,000 exchanges, and a person keeps their ten newest dashboard sessions.

## The protection boundary

Latet can inspect only the supported requests routed through it. If company policy requires
all model traffic to pass through the server, the network must prevent computers from
reaching model providers directly. Configure and maintain that restriction at the network
boundary; Latet does not install or enforce it. Arbitrary tool network traffic is outside
this guarantee.

[What is and isn't protected](../protection-scope/) describes detection, image and protocol
limits. Unsupported content may be refused or withheld on a protected server path; a
historical flag or an available provider endpoint does not establish support. A server
cannot read a connected computer's repository merely because a project appears in its
dashboard. The project information it has is information supplied to it.

For administration commands, start with [Administer a Latet server](../../getting-started/server/).
