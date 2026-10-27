---
title: Security & threat model
description: What Latet defends against, what it does not, and how it protects itself.
---

:::note[One layer of defence]
Latet masks what its detectors recognize, and some agents and surfaces are covered by design
rather than tested live (each agent's page says which). Do not make it your only safeguard for data
that must never leave your machine.
:::

## What Latet protects

Recognizable secrets and personal data in your workspace and conversations: API keys, tokens,
passwords, private keys, credentials in connection strings, emails, phone numbers, IBANs, card
numbers, national ID, tax and VAT numbers, IP addresses, the terms you add, and this machine's
identity. The [detection page](../concepts/detection/) says exactly what is recognized.

## Trust boundaries

For a connected deployment, the [server trust boundary](../concepts/server/) also applies:
originals can reach the server and authenticated dashboard views. Administrators own the
server configuration and the settings it shares, such as its destinations and its reviewer;
connection pins, credentials and agent bindings remain on each computer. Enforce provider egress
restrictions separately if the server must be the company's control point. A server does not take
a request's markers as proof that Claude Code's plugin ran: it checks the tools' descriptions,
schemas and examples too.

- **Your machine** is trusted: the agent, its tools, the Latet plugin and daemon, and your files.
- **The model provider** is not trusted with those values. Latet assumes nothing about how
  requests are logged, retained or used once they arrive, so it keeps the originals away from them.
- **The network** in between is protected by TLS as usual.

## In scope

- Accidental disclosure of recognizable values to the provider through prompts, files, tool output
  and conversation history.
- Requests that would leave with a sensitive value in clear in client mode, for example through a
  path the plugin does not cover: the plugin's final check and the daemon block them.
- Web pages trying to use the local daemon (DNS rebinding, cross-site requests).

## Out of scope

- **Values the detectors miss.** See [What is and isn't protected](../concepts/protection-scope/).
- **A compromised machine.** Malware, other users of your account, or a malicious plugin or agent
  can read your files directly. Latet is not a sandbox.
- **An agent determined to turn Latet down, or misled into it.** A change to the settings that
  lowers protection waits for your approval, and commands that turn Latet off refuse to run in an
  agent: a guardrail against mistakes, not a lock. An agent runs as you, so one set on getting around
  it, or misled by a prompt injection, can read what Latet reads and approve its own change through
  the daemon's local interface. A real boundary needs Latet running as another user, or on another
  machine ([Latet on a server](../concepts/server/)). See
  [Agents and your settings](../concepts/agents-and-settings/).
- **Exfiltration through tools.** Originals are restored in tool-call arguments so the agent's work
  is correct on disk. A prompt-injected instruction could make the agent pass a surrogate to a
  command that sends data over the network, such as `curl`; the restored original would then leave
  through that command. Latet keeps the surrogates in web tools and claude.ai connectors, but a
  shell command or a local MCP server gets the real value. Keep your agent's permission prompts on
  for network commands.
- **Inference from context.** Surrogates keep the shape of the original, and surrounding text can
  reveal what a value is used for.
- **Surfaces Latet cannot see**: cloud sessions, Claude Desktop Chat and Cowork, Cursor, and the
  results of claude.ai connectors, which come back through Anthropic's servers.

## How Latet protects itself

**The daemon and the dashboard**

- They listen on `127.0.0.1` only, so nothing on your network can reach them.
- Requests whose `Host` header is not a local address are rejected, which defeats DNS rebinding.
- Requests that come from a web page (with an `Origin` header, or a cross-site `Sec-Fetch-Site`)
  are refused on the proxy routes.
- The dashboard needs a one-time login link from `latet dashboard`, then an `HttpOnly`,
  `SameSite=Strict` session cookie, and every change needs a header other sites cannot send. See
  the [dashboard guide](../dashboard/#privacy-of-the-dashboard-itself).
- The Claude Code plugin talks to the daemon with a random token that starts with `latet_brg_`,
  kept in `bridge.token` in the data folder (a token made by an earlier version has no prefix and
  works as before). To replace it with one that has the prefix, run `latet stop`, delete
  `bridge.token` in the [data folder](../getting-started/uninstall/), run `latet start` (it makes a
  new one) and `latet agents apply`, which writes the new token into your agents' settings.
  Without the token, the daemon refuses to mask, restore or check anything.
- **Your agents reach the daemon over plain http on `127.0.0.1`, and that is on purpose.** That
  traffic never reaches a network card: no other machine, not even on your Wi-Fi, can see it. A
  program on your computer that could read it (with administrator rights, or as you) could read the
  vault and your files directly, so encrypting it would not stop it. It would cost a certificate
  authority of your own installed in the operating system, whose key, if stolen, would let someone
  pass for any website on your computer. What leaves your computer is encrypted: the requests to
  your provider, as the agent sends them, and to the local reviewer on another machine (below).

**The tutorial**

- `latet tutorial` runs its sandbox inside the command, on `127.0.0.1` and a free port, guarded
  as the dashboard is: a one-time link, then a cookie of its own, which never takes the place of
  your dashboard's sign-in.
- The sandbox has its own data, settings and vault, in a temporary folder, and none of your keys
  or tokens. Its one route leads to a pretend model in the same process, so no request reaches a
  provider; what would reach outside it, such as a destination or a local reviewer's server, is
  refused there. See [Tutorial](../getting-started/tutorial/#a-sandbox).

**The local reviewer on another machine**

- It is sent the original texts, so it is reached over `https://`. Outside your local network
  nothing else is accepted.
- A server of yours with a self-signed certificate is trusted by its fingerprint, which you compare
  once with the server's (`latet llm trust`, or *It's the same: trust it* in the dashboard). From then on only
  that certificate is accepted, checked before any text is sent, and no authority is added to the
  operating system.
- On your local network `http://` is still accepted, for servers set up before: the dashboard,
  `latet doctor` and the daemon's log warn that the texts cross your network unencrypted.

**A project's file**

- A `latet.yaml` in a repository can only add protection: a stricter profile, more terms and
  recognizers. What would turn protection down (values never to mask, rules turned off, Latet off)
  is ignored, so a repository you clone cannot weaken your settings. See
  [Projects](../concepts/projects/#the-project-file).
- Its recognizers are regular expressions run by RE2, in time linear in the text, and the file is
  capped in size: a hostile file cannot make masking hang.

**Your values**

- The vault encrypts every original on disk with AES-GCM. The key is derived from a per-install
  root key kept in your operating system's credential store (Windows Credential Manager, macOS
  Keychain, the desktop keyring on Linux), or in a private file, `root.key`, when there is none.
- Surrogates are derived with HMAC from that per-install key: without the key, nobody can work
  back from a surrogate to the original.
- A surrogate is restored only in the conversation that was shown it, so another session cannot
  use the vault as an oracle.
- The dashboard history keeps requests as they left your machine, already masked (blocked ones as
  they would have left), encrypted under the same key, for 14 days by default. Set
  `history: metadata` (counts and timings only) or `history: off` in the
  [configuration](../reference/configuration/) to keep less.

**Backups**

`latet backup` writes [your installation](../concepts/export-import/#your-installation-for-another-machine)
to one file:
- **The key comes from your passphrase.** It is derived with Argon2id (128 MiB of memory, 3
  passes) and a random salt.
- **The body is sealed in chunks.** It is a tar stream cut in chunks of 1 MiB, each sealed with
  AES-256-GCM. A chunk's nonce is its number and whether it is the last, and the file's header (its
  format, and the key's parameters and salt) is authenticated with every chunk.
- **What is refused, before anything in the file is used:**
  - another passphrase;
  - a chunk changed, moved, removed or added;
  - a file cut short, or a header changed;
  - key parameters beyond what Latet accepts (a file cannot make it use all the memory);
  - anything inside but the expected files.
- **Nothing is in clear on disk.** The databases are copied as they are (their values sealed), the
  root key exists only inside the encrypted stream, and the file is written whole, readable by you
  alone.
- **With `--vault` the file holds your real values and the key to them:** keep it as you would keep
  the values.

`latet upgrade` copies, before it installs a release, the vault, the history and the settings into
`backups/before-<version>-<time>` in Latet's data folder
([Upgrade](../getting-started/installation/#upgrade)):
- **Sealed as the vault is.** The databases are copied as they are, their values sealed with this
  computer's key, which is not copied; the folder is readable by you alone.
- **They follow what you remove**, while Latet 0.9.10 or a later release runs.
  *Forget* takes a value out of every copy, clearing the history
  clears theirs, and their history is kept no longer than yours. A copy that cannot be cleaned is
  deleted, or set aside and never offered back.
- **Three at most, and going back is yours.** Putting a copy back is offered only at a terminal of
  your own, and an agent cannot run `latet upgrade --to` an older release.

**The local reviewer** (optional, [off until you turn it on](../concepts/llm-review/))

- It runs on your own server: on this machine, or on another machine of yours once you confirm it
  (`remote: true`), over https outside your local network. It reads the original text, so give it
  only a server you control.
- It can add to what is masked, never take anything away; what it finds is checked against the
  text (a value it invents, or one inside a placeholder, is dropped).
- A tool output with instructions for it can at worst make it find less; the detectors still apply,
  and an empty answer to a text that speaks to the reviewer is not trusted (the request is stopped).
- Its server's API key is kept in the operating system's credential store (or comes from a variable
  or a file), is sent only to that server, and is never written to logs, to the dashboard or to
  config.yaml. The dashboard can save or remove it, never show it.
- What it found is remembered sealed in the vault's database, like the vault's originals.
- It is shown images only when you turn that on (`llm_review.images`): showing them to a server on
  another machine waits for your approval when an agent asks for it.

**Images** (with the optional [text recognition](../concepts/images/))

- Images are read on your machine; the models are downloaded once, at a pinned revision, each file
  checked against a pinned SHA-256, and then run offline.
- The images as they were sent are kept in the dashboard history, sealed, only with `history: full`
  (at most 10 MB each), and go with their exchange.
- What you decide on an image in the Live Reviewer, and an image the local reviewer withheld, are
  remembered sealed in the vault's database, by a keyed hash of the image.
- An image that cannot be read is withheld, never sent as it was.

**Traffic**

- Credential headers (`Authorization`, API keys, cookies and similar) are forwarded unchanged but
  never written to logs or to the dashboard: only their length is recorded. Latet's own headers
  (`x-latet-*`, one of which names a folder of yours) never leave your computer.
- One exception, when you use Amazon Bedrock with your AWS login: Claude Code sends no credentials,
  and Latet signs each request for Amazon (SigV4) with your AWS profile. It reads the profile as the
  AWS CLI does, keeps the credentials in memory only, never writes them anywhere, and signs only
  requests to Amazon Bedrock. Credentials found only in your shell (aws-vault, exported keys) are
  handed over by `latet claude` for its session alone: Latet forgets them when that session ends,
  even if `latet claude` is killed, and at their own expiry. A route that signs carries a key made
  from Latet's own token, so another user of your computer, or a program that cannot read your
  files, cannot have requests signed in your name; a program running as you can read that token, as
  it can your AWS files. The key never reaches a log. See
  [Company endpoints](../agents/company-endpoints/#amazon-bedrock).
- A repository cannot decide where your requests go: Latet reads an agent's route from your own
  settings, never a project's, and the Claude Code plugin stops prompts when a project's settings set
  where they go, or a proxy for them, even through Latet. An address written so that two programs
  could read it differently (a backslash, a space, a user name in it) is refused, never guessed. When
  the plugin cannot check a session's route, its prompts are stopped.
- Requests from Claude Code, or carrying a Claude subscription token, are never rewritten, only
  checked.
- When masking fails, the request is refused instead of sent: a body Latet cannot decode, a
  required detector that errors, or a value it cannot replace all end in an error that says
  nothing was sent. In Claude Code, when the daemon cannot be reached, the plugin holds content back.
- Upstream TLS is verified against your operating system's certificate store, so a company proxy
  whose root certificate is installed there keeps working.
- Raw capture of exchanges for debugging is off by default. When you turn it on
  (`latet serve --capture <folder>`), credential headers are redacted, but the captures still
  hold conversation content: keep them on your machine.

The daemon makes no network connections other than forwarding your agent's requests to the
provider (or the company endpoint) your agent already uses, and, when you turn it on, asking the local
reviewer, on this machine or on another of yours. When it signs for Amazon Bedrock with an SSO profile, the AWS CLI it runs may
reach AWS's sign-in service to renew your login, as it does when you use it yourself. The commands
reach the network only for what you ask: `latet ner install` and `latet ocr install` download
their models from huggingface.co, and `latet upgrade` looks up the latest release on GitHub and
downloads it. There is no telemetry and no analytics, in the daemon, in the
dashboard or in this documentation site.

**On a Latet server** ([Latet on a server](../concepts/server/))

- The administration answers over HTTPS only, to a program: a request from a web page is refused, and
  an administration token is checked only after its address passes a limit of refused attempts.
- The server keeps no secret it issues: only a keyed hash of each token, key and code. Each space's
  key is sealed with the server's master key, kept apart from the database's backups.
- Every administration call is recorded in a chained log, reads and refusals too; `latet server log
  verify` checks the chain.
- The token a computer's agents hold opens the plugin's bridge and the space's proxy, never the
  dashboard: that also needs the computer key, which no agent is given.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting on the
[Latet repository](https://github.com/Latet-AI/latet/security).
