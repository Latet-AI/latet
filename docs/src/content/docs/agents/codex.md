---
title: Codex (CLI and Desktop)
description: Route Codex CLI and Codex Desktop through the Latet gateway with openai_base_url.
sidebar:
  label: Codex
---

Codex uses **gateway mode**: the local daemon masks requests and restores responses. It works
whether you sign in with ChatGPT or use an OpenAI API key.

## Route it through Latet

Codex CLI and Codex Desktop read the same configuration file, `~/.codex/config.toml` (or
`$CODEX_HOME/config.toml` if you set `CODEX_HOME`). One line there routes Codex through Latet.

`latet global-install`, or `latet agents apply codex`, writes it for you. It asks Codex how you sign in
(`codex login status`) and picks the matching address:

```toml title="~/.codex/config.toml"
# Signed in with ChatGPT:
openai_base_url = "http://127.0.0.1:5283/backend-api/codex"

# With an OpenAI API key instead:
# openai_base_url = "http://127.0.0.1:5283/openai/v1"
```

The rest of the file, comments included, stays as it was, and a backup is taken first. Restart
Codex (or the Codex Desktop app) afterwards.

- Codex older than 0.115 ignores `openai_base_url`. `latet doctor` warns about it: update Codex
  (`npm i -g @openai/codex`). `latet global-install` and `latet agents apply codex` leave such a
  Codex as it is and say why, since that line would not route it. Until you update it, `latet codex` hands
  Codex the address in `OPENAI_BASE_URL`, the only variable that Codex reads, in place of the one your
  shell sets. `latet codex` does not start a Codex that has a provider of its own (see below): that
  Codex would go round Latet.
- If you switch between ChatGPT sign-in and an API key later, run `latet agents apply codex`
  again, so the address matches.

**Without touching `config.toml`**, run one session through Latet with the wrapper. It starts the
daemon if needed and passes every argument to `codex`:

```sh
latet codex
latet codex exec "add the payments key from .env to config/local.toml"
```

`latet run codex` is the same as `latet codex`, which picks the address for your sign-in.

On a computer connected to a Latet server, `latet codex` starts the interactive session and
`codex exec`, `resume` and `fork`; it refuses Codex's other subcommands, such as `review` or `mcp`,
which it cannot send through the server.

**A provider of your own** (Azure OpenAI, your company's gateway, Mistral...) goes through Latet
too. Only its `base_url` moves to Latet; its key and everything else stay as you wrote them.

- **For a session**, `latet codex` moves the providers of `config.toml` and of the profile you name
  (`--profile work`), with `-c model_providers.<id>.base_url=...`. Codex reads no other file, and it
  refuses to start on an option for a provider it does not load. A Codex older than 0.115 cannot take
  those options: with a provider of its own, `latet codex` does not start it and says to update Codex.
- **For good**, `latet global-install` changes each address in the file that writes it, every
  profile's file included, since a profile can switch Codex to any of those addresses.

A profile's own `openai_base_url` (a company's address in place of OpenAI's) goes through Latet the
same way. See [Company endpoints](../company-endpoints/).

## How it behaves

- With a ChatGPT sign-in, Codex talks to the provider over a WebSocket. The daemon masks every
  request Codex sends on it and restores the events that stream back. With an API key the same
  happens over HTTP.
- The provider sees surrogates. Codex sees originals, in the answer text and in tool-call
  arguments, so the commands it runs and the patches it applies use the real values.
  [How it works](../../concepts/how-it-works/) follows a key from `.env` through a Codex turn,
  step by step.
- Compressed request bodies are decoded, masked and compressed again. A body Latet cannot decode
  is refused, not sent unchecked.
- Your sign-in token or API key is forwarded unchanged; its value is never written to logs or to the
  dashboard.
- If the daemon is not running, Codex cannot reach the provider. That is deliberate: nothing goes
  out unmasked.
- Codex reads files with shell commands, so [strict data mode](../../concepts/production-data/#which-tools)
  covers the files it reads too. Name its database tool in `data_tools` to cover only that.

Tested live: the Codex CLI signed in with ChatGPT. Codex Desktop reads the same `config.toml`; if
something does not work there, please [open an issue](https://github.com/Latet-AI/latet/issues).

To stop using Latet, run `latet agents revert codex`, or `latet global-uninstall` for every agent. See
[Uninstall](../../getting-started/uninstall/).
