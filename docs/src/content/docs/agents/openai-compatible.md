---
title: OpenAI-compatible & OpenRouter
description: Use Latet with any client that lets you change the OpenAI or Anthropic base URL, and with OpenRouter.
---

Any client that lets you change its OpenAI or Anthropic base URL can use **gateway mode**: your own
scripts, the official SDKs, and the tools built on them.

## Base URLs

| Provider | Instead of | Use |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `http://127.0.0.1:5283/openai/v1` |
| Anthropic | `https://api.anthropic.com` | `http://127.0.0.1:5283/anthropic` |
| OpenRouter | `https://openrouter.ai/api/v1` | `http://127.0.0.1:5283/openrouter/api/v1` |
| Google Gemini | `https://generativelanguage.googleapis.com` | `http://127.0.0.1:5283/gemini` |

Keep your API key in the client as usual. The gateway forwards it unchanged and never writes its
value to logs or to the dashboard.

## For one command: `latet run`

`latet run` starts the daemon if needed and runs any command with the base URLs set:

```sh
latet run python welcome.py
```

The command sees `OPENAI_BASE_URL` and `OPENAI_API_BASE` set to `http://127.0.0.1:5283/openai/v1`,
`ANTHROPIC_BASE_URL` set to `http://127.0.0.1:5283/anthropic`, and `GOOGLE_GEMINI_BASE_URL` and
`GOOGLE_VERTEX_BASE_URL` set to `http://127.0.0.1:5283/gemini` and `http://127.0.0.1:5283/vertex`.
The official OpenAI, Anthropic and Google GenAI SDKs read these variables: see
[Gemini CLI and Google GenAI](../gemini/) for Google's.

## For a whole shell: `latet env`

`latet env` prints the same variables in your shell's syntax. To apply them to the current
session:

```sh
eval "$(latet env)"              # macOS and Linux
```

```powershell
latet env | Invoke-Expression    # Windows PowerShell
```

`--shell bash`, `--shell powershell` or `--shell cmd` picks the syntax; the default follows your
system. To keep the variables, add the printed lines to your shell profile. Claude Code started with
`claude` in such a shell then reaches Latet without its plugin: its requests are only checked, and each
one that carries a sensitive value in clear is stopped. Start it with `latet claude`, or set it up with
`latet global-install`, which give it the plugin too.

## Example

A script that asks a model to draft an email:

```python title="welcome.py"
from openai import OpenAI

client = OpenAI()  # reads OPENAI_API_KEY and OPENAI_BASE_URL
reply = client.chat.completions.create(
    model="your-model",
    messages=[{"role": "user", "content": "Write a short welcome email to emily.carter@northwind-legal.com"}],
)
print(reply.choices[0].message.content)
```

Run it with `latet run python welcome.py`:

1. The daemon replaces the address before the request leaves. The provider reads *"Write a short
   welcome email to laura.bennett@contoso-4f1a.example"*.
2. The model writes an email to `laura.bennett@contoso-4f1a.example`.
3. On the way back the daemon puts the original in, so the script prints an email addressed to
   `emily.carter@northwind-legal.com`.

Only the surrogate itself is restored. If the model also wrote *Dear Laura*, a name it guessed from
the fake address, that word stays as the model wrote it.

Streamed answers work the same way, chunk by chunk.

## Agent frameworks

LangChain, LangGraph, AutoGen and CrewAI reach the model through the official SDKs or LiteLLM, which
read the variables `latet run` sets: start your program with `latet run`, as any script. Each is
tested for real through Latet, set up as below:

| Framework | Set up | Tested |
| --- | --- | --- |
| LangChain and LangGraph | `ChatOpenAI`, with no base URL (`ChatAnthropic` reads `ANTHROPIC_BASE_URL` the same way) | A chat model, and a graph on it |
| AutoGen | `OpenAIChatCompletionClient`, with no base URL | An `AssistantAgent` |
| CrewAI | `LLM(model="openai/gpt-5.1")`, with no base URL | Its `LLM`, and a crew with one task |

To set the address in your code instead, give it as `base_url`: `http://127.0.0.1:5283/openai/v1` for
OpenAI's clients, `http://127.0.0.1:5283/anthropic` for Anthropic's.

Aider reaches the model through LiteLLM, which reads `OPENAI_API_BASE`: start it with
`latet run aider --model openai/gpt-5.1`, or keep `openai-api-base: http://127.0.0.1:5283/openai/v1` in
its `.aider.conf.yml`. It is tested for real the same way.

**What Latet does not see.** Latet sits between your program and the model, and nowhere else. A tool
your agent calls itself, such as a web search API, a database or a vector store in the cloud, gets
the real values: the model asks for it with fakes, and Latet puts the originals back in its answer
before your program runs the tool. Keep such tools on your machine, or count what they receive as
sent in clear.

## What the gateway reads

- **Chat Completions** (`/chat/completions`, and the older `/completions`), the **Responses API**
  (`/responses`) and **Anthropic Messages** (`/v1/messages`, token counting too): the system
  prompt, every message, tool calls and tool results are masked, and the answers are restored,
  streamed or not.
- **Embeddings** (`/embeddings`): the input is masked.
- **Images** sent as `data:` URLs are read and masked, with the optional
  [text recognition](../../concepts/images/). Images by their address, audio and files are sent as
  they are, and flagged *Not inspected* in the dashboard.
- **Any other request** to OpenAI, Anthropic or OpenRouter, such as listing models, passes through
  unchanged. On Google's routes, a request with a body Latet does not read is refused instead: what
  Latet reads there is listed in [Gemini CLI and Google GenAI](../gemini/#what-the-gateway-reads).

A request to Anthropic that comes from Claude Code, or that carries a Claude subscription token, is
never rewritten: it is only checked, and blocked if a sensitive value is in it. See
[Claude Code](../claude-code/).

## Good to know

- The gateway listens on `127.0.0.1` only and refuses requests that come from a web page.
- A path outside the routes above gets a `404` error: `no upstream for this path`.
- **Another OpenAI-compatible provider** (DeepSeek, Mistral, a model server on your network): add it as
  a destination, then use its route as the agent's base URL, `http://127.0.0.1:5283/deepseek/v1` here:

  ```sh
  latet config add upstreams "{name: deepseek, url: https://api.deepseek.com}"
  ```

  See [`upstreams`](../../reference/configuration/#upstreams-items).
- Autonomous agents such as Hermes Agent and OpenClaw take these base URLs in their own settings:
  see [Hermes Agent, OpenClaw and other agents](../autonomous-agents/).
