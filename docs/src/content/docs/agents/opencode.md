---
title: OpenCode
description: Route OpenCode's API-key providers through the Latet gateway.
---

OpenCode uses **gateway mode**: the local daemon masks requests and restores responses. It covers
the providers you use with an API key: Anthropic, OpenAI and OpenRouter.

## Route it through Latet

`latet global-install`, or `latet agents apply opencode`, points those three providers at the daemon in
your global OpenCode configuration, `~/.config/opencode/opencode.jsonc` (or `opencode.json` if
that is the file you have). The file is backed up first, and its comments are kept. It adds:

```json title="~/.config/opencode/opencode.jsonc"
{
  "provider": {
    "anthropic": { "options": { "baseURL": "http://127.0.0.1:5283/anthropic/v1" } },
    "openai": { "options": { "baseURL": "http://127.0.0.1:5283/openai/v1" } },
    "openrouter": { "options": { "baseURL": "http://127.0.0.1:5283/openrouter/api/v1" } }
  }
}
```

Your API keys stay in OpenCode as before: the gateway forwards them unchanged and never writes their
values to logs or to the dashboard. Restart OpenCode afterwards.

A project's own `opencode.json` is read after your global file, and an address it sets for one of
these providers takes the place of Latet's: in such a project, start OpenCode with `latet opencode`,
whose addresses OpenCode reads last.

**Without touching your configuration**, run one session through Latet with the wrapper. It
passes the same overrides to OpenCode for that run only (through `OPENCODE_CONFIG_CONTENT`), starts
the daemon if needed, and passes every argument to `opencode`:

```sh
latet opencode
latet opencode run "add the payments key from .env to config/local.toml"
```

`latet opencode` also gives OpenCode a launch key, in each routed provider's `options.headers`, so
that Latet trusts the session's project and applies its settings in full. OpenCode set up for good
carries none: the project it names in its requests is claimed, and only its rules that add
protection apply ([Projects](../../concepts/projects/#trusted-and-claimed)). Headers of your own in a
provider's `options.headers` are refused, naming each one, until an
[exact exception](../../getting-started/upgrading-to-0-11/#approving-an-exact-exception) lets it
through.

`latet opencode` passes that key only to OpenCode 1.19.5 or later, the version Latet has verified: an
older OpenCode could send the key's reference as it is written, so below 1.19.5, or when Latet cannot
read OpenCode's version, it passes none. It says so as it starts, the session's project is claimed,
and `latet doctor` names an OpenCode older than 1.19.5.

**OpenCode Desktop** reads the same global configuration: `latet agents apply opencode` (or
`latet global-install`) routes it once you restart the app, while `latet opencode` starts the
command-line OpenCode only. `latet agents revert opencode` takes Latet's addresses away again and
leaves the rest of the file as it is.

## Limits

- **Only these three providers are routed.** A model from any other provider (Amazon Bedrock or
  Azure in OpenCode, for example) talks to its provider directly, without Latet, and nothing warns you:
  choose an Anthropic, OpenAI or OpenRouter model.
  Claude Code and Codex go through Latet with those clouds too: see
  [Company endpoints](../company-endpoints/).
- **OpenRouter's models by Anthropic, OpenAI and Google pass on their own.** Any other vendor's
  (`meta-llama/…`, `deepseek/…`) is refused until it is listed in `request_resources`:
  [Bedrock and Vertex: what to list](../../getting-started/upgrading-to-0-11/#bedrock-and-vertex-what-to-list).
- **A console organisation can move a provider.** When OpenCode is signed in to an OpenCode console
  organisation, the organisation's settings can change a provider's address, and that provider's
  requests then do not go through Latet. Latet cannot see this: run `latet doctor`, and check the
  organisation's settings.
- **ChatGPT sign-in is not covered.** OpenCode's ChatGPT plugin has the provider's address built
  in, so a base URL cannot redirect it. OpenCode signed in with ChatGPT is on the
  [roadmap](../../roadmap/).
- **No Claude Pro or Max sign-in.** Anthropic allows a subscription in Claude Code and its own apps,
  and Latet never rewrites a request that carries a Claude subscription token: it only checks it,
  and blocks it if a sensitive value is in it. Without a plugin masking the content first, such
  requests would be blocked instead of masked. Use an Anthropic API key.

To stop using Latet, run `latet agents revert opencode`, or `latet global-uninstall` for every agent.
