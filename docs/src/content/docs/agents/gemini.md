---
title: Gemini CLI and Google GenAI
description: Route Gemini CLI, the Google GenAI SDKs and Gemini on Vertex AI through the Latet gateway.
---

Gemini CLI and the programs built on Google's GenAI SDKs use **gateway mode**: the local daemon
masks requests and restores responses. It covers the Gemini API with an API key, and Gemini on
Vertex AI, with a Google Cloud project or in express mode with an API key.

## Base URLs

| Service | Instead of | Use | Variable |
| --- | --- | --- | --- |
| Gemini API | `https://generativelanguage.googleapis.com` | `http://127.0.0.1:5283/gemini` | `GOOGLE_GEMINI_BASE_URL` |
| Vertex AI | `https://<location>-aiplatform.googleapis.com` | `http://127.0.0.1:5283/vertex` | `GOOGLE_VERTEX_BASE_URL` |

Keep your API key or your Google Cloud login where it is. The gateway forwards it unchanged (a key
in the `x-goog-api-key` header or in the address's `key` parameter, a Vertex token in
`Authorization`) and never writes its value to logs, to the dashboard or to a capture. On Vertex,
each request goes to the address of the location it names.

## Gemini CLI

Run Gemini CLI through Latet with the wrapper. It starts the daemon if needed, sets both base URLs
for that run only, and passes every argument to `gemini`:

```sh
latet gemini
latet gemini -p "write the release notes for the payments module"
```

`latet gemini` also gives Gemini CLI a launch key in `GEMINI_CLI_CUSTOM_HEADERS`, added to the
headers you set there, so that Latet trusts the session's project; where it cannot verify that Gemini
CLI will send it, it passes none, says so, and the project Gemini CLI names is claimed
([Projects](../../concepts/projects/#trusted-and-claimed)). Headers of your own in
`GEMINI_CLI_CUSTOM_HEADERS` are refused, naming each one, until an
[exact exception](../../getting-started/upgrading-to-0-11/#approving-an-exact-exception) lets it
through.

Gemini CLI reads `GEMINI_CLI_CUSTOM_HEADERS` from version 0.19.0, and Latet has verified how it sends
them from 0.19.0 to 0.66.0. An older Gemini CLI ignores the variable: its session is claimed, and
`latet doctor` names its version. In ACP mode (`--acp` or `--experimental-acp`, as an editor starts
Gemini CLI), the editor can give Gemini CLI an address and headers of its own, so `latet gemini`
passes no launch key there and says so as it starts.

`latet run gemini` does the same. Gemini CLI takes its address only from the environment, so it is not
available in global mode: `latet global-install` does not set it up, and there is no
`latet agents apply gemini`. A change for good would not reach a project with an `.env` file of its
own, and Gemini CLI would then talk to Google directly without saying so.

### Choose an API key or Vertex AI

In Gemini CLI, `/auth` chooses how it reaches its model, and the choice is kept in
`~/.gemini/settings.json`:

```json title="~/.gemini/settings.json"
{ "security": { "auth": { "selectedType": "gemini-api-key" } } }
```

- **An API key** (`gemini-api-key`): `GEMINI_API_KEY` holds it.
- **Vertex AI** (`vertex-ai`): `GOOGLE_GENAI_USE_VERTEXAI=true`, with `GOOGLE_CLOUD_PROJECT` and
  `GOOGLE_CLOUD_LOCATION` and your Google Cloud login, or `GOOGLE_API_KEY` alone in express mode.

When nothing is chosen yet, `latet gemini` offers the API key in Gemini CLI's dialog, or Vertex AI
when `GOOGLE_GENAI_USE_VERTEXAI=true` is set. A run with
`-p` needs the choice made first: set `selectedType` as above.

**Signed in with Google**, whether your settings, the project's (`.gemini/settings.json` in the
folder you start it in) or the machine's choose it, Gemini CLI sends its requests to an endpoint
Latet does not read, so nothing would be masked. `latet gemini` does not start it then, and says
where the choice is made and what to choose instead.

### Usage statistics

Gemini CLI sends its usage statistics to Google directly, not through Latet. To turn them off:

```json title="~/.gemini/settings.json"
{ "privacy": { "usageStatisticsEnabled": false } }
```

## Scripts and the Google GenAI SDKs

`latet run` sets `GOOGLE_GEMINI_BASE_URL` and `GOOGLE_VERTEX_BASE_URL` beside the OpenAI and
Anthropic variables, and `latet env` prints them for a whole shell. Google's SDKs for Python
(`google-genai`) and JavaScript (`@google/genai`) read them:

```python title="welcome.py"
from google import genai

client = genai.Client()  # reads GEMINI_API_KEY and GOOGLE_GEMINI_BASE_URL
reply = client.models.generate_content(
    model="gemini-3-pro",
    contents="Write a short welcome email to emily.carter@northwind-legal.com",
)
print(reply.text)
```

Run it with `latet run python welcome.py`: Google reads a fake address of the same shape, and the
script prints the email with the real one. Streamed answers work the same way, chunk by chunk.

Google's OpenAI-compatible endpoint goes through Latet too: give an OpenAI client the base URL
`http://127.0.0.1:5283/gemini/v1beta/openai`.

## What the gateway reads

- **Generating**, streamed or not (`generateContent`, `streamGenerateContent`), **counting tokens**
  (`countTokens`), **embeddings** (`embedContent`, `batchEmbedContents`) and **creating a cache**
  (`cachedContents`): the system instruction, every message, function calls and their results, the
  functions' declarations and the schema of the answer are masked, and the answers are restored.
- **Images** sent in the request are read and masked with the optional
  [text recognition](../../concepts/images/); without it, a request with an image is not sent. Other
  files, and files given by their address, are refused.
- The model's **thinking** is left as the model wrote it. The code Google runs for it and what that
  code printed, and the inputs and outputs of Google's own tools (Google Search, URL context, Maps,
  File Search), are masked, as a function's call and its result are.
- **Any other request with a body** on these routes is refused, with an error that says so, and a
  WebSocket of the Live API is closed before anything goes through: the Interactions API, the Live
  API, the Batch API, file uploads and Vertex's `:predict` (its own embeddings and image models)
  carry a conversation in forms Latet does not read (they are on the [roadmap](../../roadmap/)).
  Listing the models and reading one pass; any other call without a body is refused too.
- Google's client library header `x-goog-api-client` is taken off; your API key, in the `key`
  parameter or `x-goog-api-key`, is sent as it came and never logged.

## Limits

- **Gemini CLI signed in with Google** is not covered (see above).
- **Antigravity CLI** takes a base URL with an API key (`GOOGLE_GEMINI_BASE_URL`), but its
  documentation does not say which of Google's protocols it then speaks, and it is not verified
  with Latet. If it uses the Interactions API, Latet refuses its requests rather than send them
  unmasked.
- An answer that uses an **explicit cache** gets back the originals of the values its own
  conversation carries.
