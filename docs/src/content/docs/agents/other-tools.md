---
title: GitHub Copilot, JetBrains, Zed and other tools
description: Set up GitHub Copilot, JetBrains Junie and AI Assistant, Zed, Cline, Kilo Code, Continue, Qwen Code, Crush, Prime Agent, Android Studio and Claude Desktop to go through Latet's gateway, and see which tools Latet cannot protect.
sidebar:
  label: Copilot, JetBrains and more
---

Many AI tools let you choose the address of the model they call. Set that address to Latet's
**gateway** and every request the tool sends is masked before it leaves your machine, and the
answer is restored on it. Nothing is installed in the tool: a setting is all it takes.

You keep your provider's API key in the tool, as usual. Latet forwards it unchanged to the provider
and never writes it to its logs or to the dashboard.

| Provider | Address to give the tool |
| --- | --- |
| OpenAI | `http://127.0.0.1:5283/openai/v1` |
| Anthropic | `http://127.0.0.1:5283/anthropic`, or `http://127.0.0.1:5283/anthropic/v1` for a tool that adds only `/messages` to it |
| OpenRouter | `http://127.0.0.1:5283/openrouter/api/v1` |

Each section below gives the exact address the tool needs. A tool whose built-in OpenRouter provider
does not let you change its address reaches OpenRouter through an OpenAI-compatible provider with
the OpenRouter address above.

Junie CLI, Kilo Code, Continue and Crush also read settings from the project you open, and a project's
own settings can point them at another address: check them before you work in a repository you did
not write.

## At a glance

Tested means that the tool itself was run through Latet to a test provider on each API it speaks:
the provider received fakes, and the tool showed the real values.

| Tool | What goes through Latet | What does not | Tested |
| --- | --- | --- | --- |
| GitHub Copilot in VS Code | Chat and agents, with a Custom Endpoint model | Inline suggestions and the other features that use GitHub's own service | Yes |
| GitHub Copilot CLI | Every request, with a provider of your own | Nothing, offline | Yes |
| GitHub Copilot in JetBrains IDEs and Visual Studio | Chat with a custom OpenAI-compatible endpoint | Inline suggestions | No |
| JetBrains Junie CLI | Every request, with a custom model | Nothing | Yes |
| JetBrains AI Assistant | AI Chat and the features you assign the model to | Code completion and the agents in AI Chat | No |
| Zed | The agent panel and the inline assistant | Edit predictions | No |
| Cline | Every request | Nothing | The CLI |
| Kilo Code | Every request, with a custom provider | Nothing | The CLI |
| Continue | Every request of a model whose `apiBase` is Latet | Nothing | The CLI |
| Qwen Code | Every request | An email address typed in the prompt | Yes |
| Crush | Every request | Nothing | Yes |
| Prime Agent | Every request to Anthropic, OpenAI and OpenRouter, with an API key | Prime Inference and a ChatGPT plan; with a Claude plan, requests are checked, not masked | Yes |
| Android Studio | Chat and agent mode, with a third-party remote provider | Gemini's own features | No |
| Claude Desktop on a gateway | Every request is checked, and stopped if it would carry a sensitive value | Masking, outside the Code tab | No |

## GitHub Copilot in VS Code

VS Code's **Custom Endpoint** sends Copilot Chat to a model at an address you choose. It works
signed in to GitHub or not, with or without a Copilot plan; on Copilot Business and Enterprise, an
administrator can turn it off.

1. Run **Chat: Manage Language Models**, then **Add Models** → **Custom Endpoint**.
2. Enter a name and your provider's API key. VS Code keeps the key in its secret storage.
3. Describe each model in the file VS Code opens, `chatLanguageModels.json`, next to your
   `settings.json`. Each model's `url` is the full address of the call, and `apiType` is the API it
   speaks:

```json title="chatLanguageModels.json"
[
  {
    "name": "Latet",
    "vendor": "customendpoint",
    "models": [
      {
        "id": "gpt-5.1",
        "name": "GPT-5.1 through Latet",
        "url": "http://127.0.0.1:5283/openai/v1/responses",
        "apiType": "responses",
        "toolCalling": true,
        "vision": true,
        "maxInputTokens": 272000,
        "maxOutputTokens": 128000
      },
      {
        "id": "claude-sonnet-4-5",
        "name": "Claude Sonnet 4.5 through Latet",
        "url": "http://127.0.0.1:5283/anthropic/v1/messages",
        "apiType": "messages",
        "toolCalling": true,
        "vision": true,
        "maxInputTokens": 200000,
        "maxOutputTokens": 64000
      }
    ]
  }
]
```

For the Chat Completions API, use `http://127.0.0.1:5283/openai/v1/chat/completions` with
`"apiType": "chat-completions"`. Choose the model in the chat's model picker; a model needs
`"toolCalling": true` to work in agent mode.

**What does not go through Latet.** Inline suggestions (code completions and next edit
suggestions), code search and the other features that rely on GitHub's embeddings use GitHub's own
service, not your model: when you are signed in, they leave without Latet. Signed in, Copilot also
gives short tasks, such as chat titles and commit messages, to GitHub's models; to keep them on your
model, set
`"chat.byokUtilityModelDefault": "mainAgent"` in your settings, or choose models for them with
`chat.utilityModel` and `chat.utilitySmallModel`.

## GitHub Copilot CLI

The Copilot CLI goes through Latet with a provider of your own: an OpenAI, Anthropic or OpenRouter
key, or another address, given in environment variables. Signed in with GitHub, on the models of a
Copilot plan, it sends to GitHub's Copilot service, which Latet cannot go in front of: `latet copilot`
says so and starts nothing.

```sh
export COPILOT_PROVIDER_TYPE=openai          # Chat Completions
export COPILOT_PROVIDER_API_KEY=...          # your OpenAI key
export COPILOT_MODEL=gpt-5.1
export COPILOT_OFFLINE=true                  # no GitHub sign-in, no telemetry
latet copilot
```

In PowerShell:

```powershell
$env:COPILOT_PROVIDER_TYPE = "openai"
$env:COPILOT_PROVIDER_API_KEY = "..."        # your OpenAI key
$env:COPILOT_MODEL = "gpt-5.1"
$env:COPILOT_OFFLINE = "true"
latet copilot
```

`latet copilot` points the provider's address at Latet for that session alone: OpenAI's, Anthropic's
and OpenRouter's go to Latet's own routes, another address becomes one of your destinations, and one
on this computer is left as it is. With OpenRouter, set
`COPILOT_PROVIDER_BASE_URL=https://openrouter.ai/api/v1` and your OpenRouter key; for Anthropic,
`COPILOT_PROVIDER_TYPE=anthropic` and your Anthropic key. To start `copilot` yourself instead, set
`COPILOT_PROVIDER_BASE_URL` to Latet's address for it: `http://127.0.0.1:5283/openai/v1`,
`/openrouter/api/v1` or `/anthropic`.

`COPILOT_PROVIDER_WIRE_API=responses` uses the Responses API instead. The model must support tool
calling and streaming. Without `COPILOT_OFFLINE=true`, the CLI still sends telemetry to GitHub, and
features such as `/delegate` need a GitHub sign-in; your prompts go only to your provider.

## GitHub Copilot in JetBrains IDEs and Visual Studio

Copilot's JetBrains plugin takes a custom OpenAI-compatible endpoint: open **Manage Models** from the
chat's model picker, add the endpoint `http://127.0.0.1:5283/openai/v1` with your API key, and choose
its model in the chat. Visual Studio 2026 does the same in agent mode, from its own model settings.
Inline suggestions keep using GitHub's service in both.

## JetBrains Junie CLI

Junie CLI takes a custom model from a profile file in `~/.junie/models/`, or in `.junie/models/` in a
project. Its `baseUrl` is the full address of the call: Junie adds nothing to it.

```json title="~/.junie/models/latet.json"
{
  "id": "gpt-5.1",
  "baseUrl": "http://127.0.0.1:5283/openai/v1/responses",
  "apiType": "OpenAIResponses",
  "apiKey": "${OPENAI_API_KEY}"
}
```

Start it with `junie --model custom:latet`, or choose the model under **Custom** in `/model`. Other
values of `apiType`:

| `apiType` | `baseUrl` |
| --- | --- |
| `OpenAICompletion` (Chat Completions) | `http://127.0.0.1:5283/openai/v1/chat/completions` |
| `Anthropic` | `http://127.0.0.1:5283/anthropic/v1/messages` |

A custom model needs no JetBrains account. `JUNIE_SHARE_ANONYMOUS_STATISTICS=false` turns off the
anonymous usage statistics. Junie inside the IDE takes OpenAI and Anthropic keys only, not a custom
address.

## JetBrains AI Assistant

In **Settings | Tools | AI Assistant | Providers & API keys**, under **Third-party AI providers**,
add an OpenAI-compatible provider with the URL `http://127.0.0.1:5283/openai/v1` and your API key,
and turn on **Tool calling**. Its models appear in the AI Chat model picker; in **Models
Assignment**, you can give them the other features too, such as commit messages.

Code completion does not use a model of yours, and the agents in AI Chat, such as Junie, Claude Agent
and Codex, take their providers' own keys, not a custom address: they do not go through Latet.

## Zed

Add a provider in Zed's `settings.json`, then choose its model in the agent panel:

```json title="settings.json"
{
  "language_models": {
    "openai_compatible": {
      "Latet": {
        "api_url": "http://127.0.0.1:5283/openai/v1",
        "available_models": [{ "name": "gpt-5.1", "max_tokens": 272000 }]
      }
    },
    "anthropic": { "api_url": "http://127.0.0.1:5283/anthropic" }
  }
}
```

Zed reads the key of the `Latet` provider from the agent settings, or from the variable
`LATET_API_KEY`; the Anthropic provider keeps its own key. Zed's built-in OpenAI and OpenRouter
providers take an `api_url` too.

**Edit predictions** come from Zed's own service by default and do not go through Latet: set
`"edit_predictions": { "provider": "none" }` to turn them off.

## Cline

In Cline's settings, choose the **OpenAI Compatible** provider with the base URL
`http://127.0.0.1:5283/openai/v1`, your API key and a model ID. For Anthropic, choose the
**Anthropic** provider, tick **Use custom base URL** and enter `http://127.0.0.1:5283/anthropic/v1`:
Cline adds `/messages` to it.

The Cline CLI takes the OpenAI-compatible provider from the command line:

```sh
cline auth -p openai-compatible -k "$OPENAI_API_KEY" -m gpt-5.1 -b http://127.0.0.1:5283/openai/v1
```

For its Anthropic provider, set `baseUrl` in `~/.cline/data/settings/providers.json`: `cline auth`
takes a base URL for OpenAI-compatible providers only.

## Kilo Code

In the Kilo Code extension, add a **custom provider** in its settings, with the API it speaks
(OpenAI Compatible, OpenAI Responses or Anthropic Messages) and the base URL below. The `kilo` CLI
reads the same provider from `kilo.json`, in `~/.config/kilo/` or in a project:

```json title="kilo.json"
{
  "provider": {
    "latet": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Latet",
      "options": { "baseURL": "http://127.0.0.1:5283/openai/v1", "apiKey": "{env:OPENAI_API_KEY}" },
      "models": { "gpt-5.1": { "name": "GPT-5.1 through Latet" } }
    }
  },
  "model": "latet/gpt-5.1",
  "disabled_providers": ["kilo"]
}
```

`disabled_providers` turns off Kilo's own provider, whose requests go to Kilo's service without Latet.
For Anthropic, use `"npm": "@ai-sdk/anthropic"` with the base URL
`http://127.0.0.1:5283/anthropic/v1`; for the Responses API, `"npm": "@ai-sdk/openai"` with
`http://127.0.0.1:5283/openai/v1`.

## Continue

Continue's extensions for VS Code and JetBrains and its `cn` CLI read the same
`~/.continue/config.yaml`. Give each model Latet's address in `apiBase`:

```yaml title="~/.continue/config.yaml"
name: Latet
version: 1.0.0
schema: v1
models:
  - name: GPT-5.1 through Latet
    provider: openai
    model: gpt-5.1
    apiBase: http://127.0.0.1:5283/openai/v1
    apiKey: ${{ secrets.OPENAI_API_KEY }}
  - name: Claude through Latet
    provider: anthropic
    model: claude-sonnet-4-5
    apiBase: http://127.0.0.1:5283/anthropic/v1
    apiKey: ${{ secrets.ANTHROPIC_API_KEY }}
```

The Anthropic `apiBase` ends in `/v1`: Continue adds `/messages` to it.

## Qwen Code

Qwen Code reads its address from the same variables as the official SDKs, so `latet run` routes it:

```sh
latet run qwen
```

It uses `OPENAI_BASE_URL` with its OpenAI-compatible provider and `ANTHROPIC_BASE_URL` with its
Anthropic one; choose the provider in `~/.qwen/settings.json`
(`"security": { "auth": { "selectedType": "openai" } }`, or `"anthropic"`), and set the key and the
model in `OPENAI_API_KEY` and `OPENAI_MODEL`, or `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`. Without
`latet run`, set the base URLs yourself: `http://127.0.0.1:5283/openai/v1` and
`http://127.0.0.1:5283/anthropic`.

**An email address you type.** Qwen Code reads every `@` in a prompt you type as a file to attach,
and sends `mario.rossi@acme.it` as `mario.rossi @acme.it`. Latet does not recognize that as an
address, so it leaves in clear. Addresses in the files, command output and pages Qwen Code reads are
not changed, and Latet masks them.

## Crush

Add a provider to Crush's `crush.json`, in a project or in your Crush settings folder:

```json title="crush.json"
{
  "providers": {
    "latet": {
      "type": "openai-compat",
      "base_url": "http://127.0.0.1:5283/openai/v1",
      "api_key": "$OPENAI_API_KEY",
      "models": [{ "id": "gpt-5.1", "name": "GPT-5.1 through Latet", "context_window": 272000, "default_max_tokens": 16000 }]
    }
  }
}
```

Choose that provider for both of Crush's models, so that no request, the short ones that name a
session included, goes to a provider Crush set up by itself from your environment:

```json title="crush.json"
"models": {
  "large": { "model": "gpt-5.1", "provider": "latet" },
  "small": { "model": "gpt-5.1", "provider": "latet" }
}
```

For Anthropic, use `"type": "anthropic"` with `http://127.0.0.1:5283/anthropic`. Crush's `openai`
type speaks the Responses API, and its `openrouter` type does not take a base URL: reach OpenRouter
with `openai-compat` and `http://127.0.0.1:5283/openrouter/api/v1`.

## Prime Agent

Prime Agent reads the address of each provider from `models.json`, in `~/.prime/agent`
(`%USERPROFILE%\.prime\agent` on Windows). Give its built-in providers Latet's address:

```json title="~/.prime/agent/models.json"
{
  "providers": {
    "anthropic": { "baseUrl": "http://127.0.0.1:5283/anthropic" },
    "openai": { "baseUrl": "http://127.0.0.1:5283/openai/v1" },
    "openrouter": { "baseUrl": "http://127.0.0.1:5283/openrouter/api/v1" }
  }
}
```

Every model of these providers then goes through Latet, with the API key you gave Prime Agent with
`/login` or in `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `OPENROUTER_API_KEY`.

**With a plan instead of a key.** Signed in with a Claude plan, Prime Agent presents itself to
Anthropic as Claude Code. Its requests still go through Latet, which checks requests signed with a
Claude plan and never rewrites them: one that would carry a secret or personal data in clear is
stopped, with a message instead of an answer. Use an API key to have them masked. Requests signed in
with a ChatGPT plan, and those to Prime Inference, Prime Intellect's own service, do not go through
Latet.

## Android Studio

In **Settings → Tools → AI → Model Providers**, add a **Third-Party Remote Provider** with Latet's
address in place of the provider's, and your API key, then refresh the model list and choose the
models: `http://127.0.0.1:5283/openai/v1` for OpenAI, `http://127.0.0.1:5283/anthropic` for
Anthropic, `http://127.0.0.1:5283/openrouter/api/v1` for OpenRouter. Chat and agent mode use the
model you choose; Gemini's own features keep using Google's service.

## Claude Desktop on a gateway

Claude Desktop can run on an inference gateway instead of Anthropic's service, with no Claude
sign-in: an organization uses this to send it to its own provider. There is then no Pro or Max
subscription: requests are paid per use, with an Anthropic API key or through your company's
provider. On one computer, without signing in, turn on **Help → Troubleshooting → Enable Developer Mode**, then
open **Developer → Configure Third-Party Inference**; an organization sets the same keys from a device
management profile. Set the gateway to Latet:

| Key | Value |
| --- | --- |
| `inferenceProvider` | `gateway` |
| `inferenceGatewayBaseUrl` | `http://127.0.0.1:5283/anthropic` |
| `inferenceGatewayApiKey` | Your Anthropic API key |
| `inferenceGatewayAuthScheme` | `x-api-key` |

Claude Desktop accepts an `http://` address on this computer. Chat, Cowork and Code then send every
request through Latet, and the model list comes from Anthropic through it.

**What Latet does with them.** Claude Code's engine builds these requests, and Latet never rewrites
a request from Claude Code: it checks each one, and stops one that would carry a secret or personal
data in clear, with a message instead of an answer. It does not mask them, as it does for the other
tools on this page: Anthropic's [gateway guide](https://code.claude.com/docs/en/llm-gateway-protocol)
asks a gateway to inspect Claude Code's requests, not to modify them. In the Code tab, Latet's
plugin masks content before the request is built, so the Code tab gets both the masking and the
check.

Signed in to Claude, Claude Desktop does not use a gateway: Chat and Cowork are not covered, and the
plugin masks in the Code tab with no check behind it. See [Claude Code](../claude-code/#coverage).

## What Latet cannot protect

Latet runs on your computer, between the tool and the model. These tools send their requests from
the vendor's own servers, or call a service that no setting can change, so their requests never pass
through Latet:

| Tool | Why |
| --- | --- |
| Cursor and its CLI | Cursor builds and sends each model request from its own servers, even with your own API key. |
| Windsurf and Devin Desktop | Requests go to the vendor's servers, which call the model. |
| Google Antigravity (the IDE) | Requests go to Google's service, which calls the model. |
| Jules | It runs in Google's cloud, not on your computer. |
| Kiro and Amazon Q Developer | Requests go to Amazon's service, which calls the model. |
| Warp | Requests go to Warp's servers, which call the model. |
| The ChatGPT desktop app | It talks to OpenAI's service only; it has no setting for another address. |
| Claude Desktop signed in to Claude, and cloud sessions | Chat and Cowork call Anthropic's service directly, and cloud sessions run on Anthropic's infrastructure. |

If a tool you use is not on this page, look for a setting named base URL, API base, endpoint or
custom provider: with one, it goes through Latet as the tools above do. If it does not work, please
[open an issue](https://github.com/Latet-AI/latet/issues).
