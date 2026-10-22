---
title: Hermes Agent, OpenClaw and other agents
description: Autonomous agents through Latet's gateway, Hermes Agent, OpenClaw, Goose and OpenHands among them, and any agent that takes an OpenAI-compatible base URL.
sidebar:
  label: Autonomous agents
---

Autonomous agents work on their own, for hours: they read mailboxes, browse, query databases, run
commands and call APIs, and no one reads each step. Every page, file and result they read goes to
the model with the next request. Pointed at Latet, each of those requests is masked before it
leaves your machine, and the answers are restored on it.

They go through Latet's **gateway**, like any OpenAI-compatible client: you set the agent's base
URL to Latet and keep your provider's API key in the agent, as usual. `latet global-install` does not
change these agents' files: the settings below are all it takes.

| Provider | Base URL to set | Key |
| --- | --- | --- |
| OpenAI | `http://127.0.0.1:5283/openai/v1` | Your OpenAI API key |
| OpenRouter (Claude, Gemini and hundreds of other models) | `http://127.0.0.1:5283/openrouter/api/v1` | Your OpenRouter API key |
| Anthropic, for an agent that speaks Anthropic's format | `http://127.0.0.1:5283/anthropic` | Your Anthropic API key |

A Claude Pro or Max subscription works in Claude Code only, as Anthropic's terms say, so these
agents use an API key: they reach Claude models through OpenRouter, or with an Anthropic API key.
The key passes through Latet to the provider and is never logged or shown in the dashboard. Hermes
Agent signed in with ChatGPT needs no key: it goes through Latet with that sign-in, as Codex does
([below](#signed-in-with-chatgpt)).

## Hermes Agent

Hermes reads its model and endpoint from `config.yaml` in its folder: `~/.hermes` on macOS and
Linux, `%LOCALAPPDATA%\hermes` on Windows (`HERMES_HOME` sets another). With an API key, point a
custom provider at Latet:

```yaml title="config.yaml"
model:
  provider: custom
  base_url: http://127.0.0.1:5283/openrouter/api/v1   # or http://127.0.0.1:5283/openai/v1
  key_env: OPENROUTER_API_KEY                          # the variable that holds your key
  default: anthropic/claude-sonnet-4.5                 # a model your provider lists
```

`hermes model` sets the same interactively. `OPENAI_BASE_URL` in the environment is not enough:
Hermes honours it only for its `openai-api` provider, so `latet run hermes` would not route a
custom provider.

### Signed in with ChatGPT

Hermes signed in with your ChatGPT account (its `openai-codex` provider) goes through Latet at the
address Codex uses. Add one line to `.env` in Hermes's folder:

```sh title="~/.hermes/.env"
HERMES_CODEX_BASE_URL=http://127.0.0.1:5283/backend-api/codex
```

Hermes reads the file again by itself: its next message goes through Latet. The variable moves
every request Hermes sends with that sign-in: the conversation, and the work it hands the same
model on the side, such as a session's title, the summary of a long context, or the reading of an
image or a web page. It comes before `model.base_url` in `config.yaml`, which you can leave as it
is.

- **A Latet 0.9.3 or earlier** gives Hermes back the surrogates in place of the real values: upgrade
  Latet first (`latet upgrade`).
- **A fallback provider with a `base_url` of its own** keeps that address: in `fallback_providers`,
  set Latet's address there too, or remove the line. Otherwise a request that falls back to it goes
  to ChatGPT with nothing masked.
- If Latet is not running, Hermes cannot reach the model. That is deliberate: nothing goes out
  unmasked.
- In the dashboard, Hermes's requests are listed under Codex or under another agent: Hermes names
  itself only to ChatGPT's own address.

Tested live: Hermes Agent signed in with ChatGPT.

## OpenClaw

OpenClaw reads `~/.openclaw/openclaw.json` (JSON5) and applies changes to it by itself. Add a
provider that points at Latet, and make one of its models the default:

```json5 title="~/.openclaw/openclaw.json"
{
  agents: {
    defaults: { model: { primary: "latet/gpt-5.1" } },
  },
  models: {
    providers: {
      latet: {
        baseUrl: "http://127.0.0.1:5283/openai/v1",
        apiKey: "${OPENAI_API_KEY}",
        api: "openai-completions",
        models: [{ id: "gpt-5.1", name: "GPT-5.1 through Latet" }],
      },
    },
  },
}
```

For OpenRouter, use `http://127.0.0.1:5283/openrouter/api/v1`, your OpenRouter key and its model
names.

## Goose

Goose's OpenAI provider takes a host and a path, which it joins:

```sh
export OPENAI_HOST=http://127.0.0.1:5283
export OPENAI_BASE_PATH=openai/v1/chat/completions
export OPENAI_API_KEY=...        # your OpenAI key, as usual
export GOOSE_PROVIDER=openai
export GOOSE_MODEL=gpt-5.1
goose session
```

`GOOSE_PROVIDER` and `GOOSE_MODEL` choose the provider and the model: without them, Goose asks you
to run `goose configure` first, which stores the same values. For OpenRouter, add a custom
OpenAI-compatible provider with `goose configure`, with the base URL
`http://127.0.0.1:5283/openrouter/api/v1` and your OpenRouter key; this setup is not among those
tested.

## OpenHands

In OpenHands' LLM settings, under **Advanced**, choose a custom model with its provider prefix
(`openai/gpt-5.1`), set the base URL to `http://127.0.0.1:5283/openai/v1` and enter your API key.
With the command line:

```sh
LLM_MODEL=openai/gpt-5.1 LLM_BASE_URL=http://127.0.0.1:5283/openai/v1 LLM_API_KEY=... \
  openhands --override-with-envs
```

## Any other agent

An agent that takes an OpenAI-compatible base URL goes through Latet with the addresses in the
table above. One that reads `OPENAI_BASE_URL` or `ANTHROPIC_BASE_URL` from the environment can be
started with [`latet run`](../openai-compatible/#for-one-command-latet-run), which sets them for
it alone.

**An agent in a container** does not reach Latet at `127.0.0.1`: there, `127.0.0.1` is the
container itself, and Latet answers only requests addressed to `127.0.0.1` or `localhost`. Run
the agent on your machine; on Linux, a container started with `--network host` shares your
machine's `127.0.0.1` too.

## For an agent no one watches

- **Turn on the [local reviewer](../../concepts/llm-review/)** if you have a local model server. It
  reads what the agent fetches, the part no one types, and by default a request waits for its review
  and is stopped when the reviewer cannot answer.
- **Use [strict data mode](../../concepts/production-data/)** for an agent that reads databases or
  logs: every value of a table or a JSON document is masked, and joins still work.
- **Watch it in the dashboard:** `latet dashboard` shows each request the agent sent, what was
  masked in it, and what was blocked.
- **Let it manage Latet, safely:** point it at
  [`llms-config.md`](https://github.com/Latet-AI/latet/blob/main/llms-config.md). What adds
  protection applies; what lowers it waits for your approval in the dashboard. See
  [Agents and your settings](../../concepts/agents-and-settings/).

Each of these four agents is tested for real, set up as above: it sends a task through Latet to a
test provider that answers as OpenAI does, the provider receives fakes, and the agent gets the
real values back. If an agent does not work through Latet, please
[open an issue](https://github.com/Latet-AI/latet/issues).
