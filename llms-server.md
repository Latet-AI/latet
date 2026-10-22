# A Latet server, with an AI agent

This file is for an AI agent that a user asked to set up a Latet server (one Latet for a team, with
PostgreSQL and Valkey), to administer one, or to connect a computer to one. Read it before you run
anything.

## What you cannot do, by design

A Latet server's administration refuses to run inside an agent, and so does connecting a computer.
These commands create or use the server's master key, its administration token and each person's
one-time code, which must never pass through an agent:

- `latet server init` and `latet server service install`, even with `--print` or `--yes`;
- every `latet server` command that changes the administration (people, API keys, tokens, shared
  settings, rules), and `config get` or rules exported to the terminal, which can print values;
- `latet connect`, and on a connected computer `latet dashboard` and `latet identity send`.

Do not try to get around these refusals: no other shell, script, scheduled task or variable that hides
that an agent runs them. Never ask the user to paste an administration token, a one-time code, the
master key or a private key into the conversation.

## What you can do

1. **Explain the two ways to run it**, and what each needs, from
   <https://docs.latet.ai/concepts/server-operations/>:
   - a **host service** on a trusted machine (Linux with systemd, or a Windows service), with
     PostgreSQL and Valkey the user runs;
   - the **Compose server profile** of the repository's `compose.yaml`, which brings PostgreSQL,
     Valkey, two Latet instances and Caddy.

   Both need a public HTTPS address for the server and a certificate the computers trust.
2. **Check what is there**, read-only: Docker and Compose (`docker --version`,
   `docker compose version`), the machine's operating system, whether the address resolves.
3. **Give the user the steps in order**, to run in their own terminal, from
   <https://docs.latet.ai/getting-started/server/>: prepare the certificate and the secret files,
   initialize, check with `latet server status`, give each person their own space (each gets a
   one-time code), create API keys for programs, set the shared settings. Say what to keep private:
   the master key, the administration token, the certificate's private key, each one-time code.
4. **Explain how each person connects their computer**: they install Latet as usual
   ([`llms-install.md`](llms-install.md) can do that part), then run
   `latet connect https://their-server` in their own terminal and enter the code their administrator
   gave them, with `--fingerprint` for a self-signed certificate.
5. **Answer questions** about how a server works: what it shares with every space (its
   destinations, the local reviewer, text recognition, history and how long it is kept), that each
   person's and each API key's rules are their own, and that rules set for everyone, that nobody can
   lower, are planned for the Enterprise Edition. See <https://docs.latet.ai/concepts/server/>.

Tell the user plainly at the start that these steps are theirs to run, and why: an agent that ran
them would hold the keys to everyone's space.
