---
title: Upgrading to 0.11
description: What Latet 0.11 refuses that 0.10 sent, what to check before you upgrade, and how to go back.
---

Latet 0.11.0 reads every part of a request before it sends it: the path, the query parameters, the
headers and the body. A request with a part that Latet neither inspects nor lets through by a rule or
an exception you approved is not sent. An empty body is no longer enough to let a request through.
The credentials your provider needs, and the parts a provider signs, keep their own rules. This is
the same on your computer and on a Latet server.

So a call that 0.10 sent without reading it may be refused now. This page lists what changes, what
`latet doctor` checks for you before and after the upgrade, and what to do for each case.

## What 0.11 refuses that 0.10 sent

**Calls without a body.** Listing the models and reading one model pass on every provider Latet
knows: Anthropic, OpenAI, OpenRouter, ChatGPT (Codex's model list), Gemini, and Bedrock's inference
profiles. Their query parameters are read too (`limit`, `after_id`, `pageToken`, `maxResults`...).
Any other call is refused: files and uploads, batches, audio, image generation, moderations,
reading or deleting a stored response, reading a Gemini cache, an account's endpoints. The agent is
told so, and nothing is sent:

```text
Latet: request envelope refused (envelope-route): route /{destination}/{unsupported-route}; this version of Latet does not support this call, so change the agent or its settings so that it does not make it. Nothing was sent; run `latet dashboard` to see where it was
```

An exception names a header, a query parameter or a member of the body, never a whole route: in
0.11.0 no exception lets one of these calls through. Change the agent's settings so that it does not
make the call, or make it without Latet, to the provider's own address, knowing that Latet does not
see what it sends.

**Query parameters.** A query parameter no profile names is refused, naming it to the agent, never
its value:

```text
Latet: request envelope refused (envelope-query-unknown): query parameter drift. Nothing was sent; run `latet dashboard` to see where it was
```

**Headers.** Each header is read by a rule of its own: a credential is sent as it came and never
logged; an agent's own header is held to the form its agent sends (a session id must be a UUID, for
example); a header that can hold free text, such as `user-agent`, passes the final check. A header
no profile names is refused, naming it to the agent, never its value:

```text
Latet: request envelope refused (envelope-header-unknown): header x-team. Nothing was sent; run `latet dashboard` to see where it was
```

Some headers are taken off instead: the SDKs' telemetry and tracing headers, and a few of the
agents' own that can carry free text. [Headers Latet takes off](#headers-latet-takes-off) lists
them.

**Members of the body.** Every member of a request's body is read under its profile: masked, checked
by the final check, or held to the form the provider gives it (a model's id, a tool call's id, a
setting). A member no profile names is refused, saying where it is (`top-level`, `message content
block`, `tool definition`...), never its name or value:

```text
Latet: request body refused (unknown member in top-level). Nothing was sent; run `latet dashboard` to see where it was
```

An [exact exception](#approving-an-exact-exception) can name such a member by its JSON pointer,
except where none can reach it: inside a provider's own item (`provider item`: thinking, reasoning),
in an image's source (`inline image`), and in any WebSocket frame. The agent is told the same words;
the dashboard records the refusal as `body-unknown-member-no-exception` instead of
`body-unknown-member`, and says *Its body held a member that no request profile names, where no
exception can reach it (inside a provider's item, an image's source, or a WebSocket turn).*

A request Latet only checks (Claude Code, Bedrock, Vertex) is never rewritten, so one more rule
applies there: a new member whose value is a text or a list of texts passes once the final check has
read its name and its text, and the dashboard shows it as *checked only*. A nested or other new
member is refused as above. Codex's `client_metadata`, which repeats the repository's path and
remotes, is taken out of the request before it is masked.

**What the provider gave back.** The model's thinking signatures, encrypted reasoning, compactions
and web search results come back in every turn. They pass because Latet saw them come from the
provider in that conversation. Where it did not (a computer's daemon restarted, say), a built-in rule
lets the provider's own encrypted fields through, only in the model's turns and only to the
provider's own address; the dashboard shows them as *allowed by rule*. A conversation through a
[company gateway](#conversations-through-a-company-gateway) has no such rule.

**Images and documents.** An image is sent only once Latet has read its text. Without text
recognition, a request that carries an image is refused, with words that say to run
`latet ocr install` and to start a new conversation, since the image stays in its history.
`images: allow` no longer sends an image Latet did not read. PDFs and other documents, audio, and
images given by their address are refused, whatever `images` says. On a request Latet only checks
(Claude Code, Bedrock, Vertex) every byte of an image is accounted for, and a phone photo with a
location, a camera's maker notes, a thumbnail or a second image inside it (a depth map, the video
of a motion photo) is refused: see [Photos and their metadata](../../concepts/images/#photos-and-their-metadata).
An original WebP image is refused there too: export it as PNG. Requests Latet rewrites (Codex and
the others) send images written anew, without their metadata, and see no such refusal.

**A project where Latet is off** sends nothing: see [below](#a-project-where-latet-is-off).
**`latet serve --passthrough`** is gone: the flag stays hidden through 0.11, and exits at once with
a message, so a service that still names it stops where you can see it. Take it out of the service's
command, or of the script that starts Latet.

**A folder named by a request only adds protection:** see
[Folders a request names](#folders-a-request-names).

## Headers Latet takes off

Latet takes these headers off a request before it sends it, whichever agent sent them:

| Where | Headers |
| --- | --- |
| Every request: the SDKs' telemetry and tracing | `x-stainless-*`, `x-datadog-*`, `x-b3-*`, `traceparent`, `tracestate`, `baggage` |
| Claude Code, in Anthropic's format (Anthropic, Bedrock, Vertex, a company gateway) | `x-claude-code-agent-id` and `x-claude-code-parent-agent-id`, a subagent's ids, which can carry its name; `x-claude-remote-session-id`, `x-claude-remote-container-id`, `x-claude-remote-session-origin` and `x-client-app`, which Claude Code copies from its environment |
| Bedrock: the AWS SDK | `amz-sdk-*`, `x-amz-user-agent` |
| Gemini: Google's client library | `x-goog-api-client` |
| Codex, in OpenAI's format and ChatGPT's | `x-codex-turn-metadata`, which holds your repository's path, remotes and commit; `x-openai-subagent`, a subagent's kind or free label |

Latet's own `x-latet-*` headers never reach a provider either. A credential is never taken off. On a request
Latet only checks (Claude Code, Bedrock, Vertex), the body leaves byte for byte: these headers are
the only change. Codex's `client_metadata`, the body's copy of `x-codex-turn-metadata`, is taken out
of the body of a request Latet rewrites.

Each exchange in the dashboard counts the headers and body members taken off it. When the provider
answers `401` or `403` to a request that had some taken off, the agent is told so, and the exchange
says the header may be one the provider needs:

```text
Latet: the provider answered 401 after Latet removed 2 SDK/tracing headers
```

## What `latet doctor` checks

Run `latet doctor` before you upgrade, and again after. It lists, by kind and by count, never a
value:

- the projects where Latet is off (each one's requests will be refused);
- text recognition not installed, or turned off with `ocr: off` (each request that carries an
  image will be refused);
- `--passthrough` in an entry that starts Latet at login;
- the calls seen in your history that no profile covers, by their method and route template,
  never their path;
- your permanent request exceptions;
- the agents Latet launched whose requests arrived without their launch key, with the agent and
  its version.

The daemon says the same in its log when it first starts on 0.11.

## Install text recognition

```sh
latet ocr install   # about 31 MB, once
```

`latet upgrade` adds what the models run on to a Latet installed without it. In Docker:
`docker exec latet latet ocr install --yes`. On a Latet server, the administrators install the
models on each instance ([Recognition models on a server](../../concepts/server-operations/#recognition-models-on-a-server)).
See [Screenshots and images](../../concepts/images/).

## Headers you set for your agents

An agent configured with headers of your own sends them with every request, and 0.11 refuses them
until you let them through: Claude Code's `ANTHROPIC_CUSTOM_HEADERS`, Codex's `http_headers` and
`env_http_headers`, Copilot CLI's `COPILOT_PROVIDER_HEADERS`, OpenCode's `options.headers`, Gemini
CLI's `GEMINI_CLI_CUSTOM_HEADERS`. The refusal names the header. Let each one through with an exact
exception, as below, naming the destination it goes to; a company gateway's own key in a header is
named as a credential, which Latet sends as it came and never reads. Latet's own launch key, which
`latet claude` and the others add to these settings, is not yours to name: Latet takes it off before
anything is sent on.

## Approving an exact exception

An exception names exactly what it lets through, on one destination and one request profile: a
header, a query parameter, or a member of the JSON body. For example, to let Claude Code's
`x-team` header reach Anthropic:

```sh
latet config add request_exceptions '{destination: anthropic, profile: anthropic.messages.1, components: [{kind: header, name: x-team}], reason: custom-header}'
```

The same in `config.yaml`:

```yaml
request_exceptions:
  - destination: anthropic           # a provider's route, or one of your destinations
    profile: anthropic.messages.1    # one method, route and transport
    components:
      - {kind: header, name: x-team}                  # checked: its value passes the final check
    reason: custom-header
  - destination: llm-acme-com
    profile: openai.chat.1
    components:
      - {kind: header, name: x-acme-key, role: credential}   # sent as it came, never read
    reason: destination-credential
  - destination: openai
    profile: openai.responses.1
    components:
      - {kind: body, name: /metadata_x/team, type: string}  # an exact JSON pointer and its value's type
    reason: other
```

- **It waits for your approval.** An exception lowers protection, so it applies only once you
  approve it, on **Approvals** in the dashboard, under *Settings*. Latet then gives it an id
  (`x1a2b3c4d5e`) and its approval time. Removing one, or naming less, applies at once.
- **It lasts 30 days** from its approval by default, and 90 at most (`days`). The dashboard warns
  seven days before its end, and a request refused after it names the exception and the day it
  ended. Renewing it is a new approval.
- **Permanent, on purpose.** `permanent: true` makes one that never expires. It is approved like
  any exception, always marked *permanent* in the dashboard, and listed by `latet doctor` and, on a
  server, by `latet server status`; approving one on a server writes an entry in the administration
  log, with its id and the kind of its scope alone.
- **Whose requests.** An exception without `project` applies to every request of yours, whichever
  agent sends it, and the dashboard says so before you approve it. With `project: C:\dev\acme`, it
  applies only to requests whose project Latet trusts there: an agent it launched with its key, or
  Claude Code with the plugin (see
  [Projects](../../concepts/projects/#which-project-a-session-is-in)).
- **Never in a repository.** A project's `latet.yaml` cannot hold exceptions. At most 128, 32 for
  one project, each naming up to 64 components; `description` is for you alone, never in a log.
- **Not on a WebSocket.** In 0.11.0 an exception does not apply to a WebSocket: Codex signed in
  with ChatGPT, which talks over one, cannot send a header of yours. The dashboard offers none for a
  refusal there, and says *A WebSocket takes no request exception in this version of Latet.*
- **After an upgrade.** A later version that changes a part of a profile an exception names stops
  that exception until you approve it again; the dashboard shows it as *Needs approval again: its
  profile was revised*, and `latet doctor` says it needs approval again, permanent ones too; on a
  server, `latet server status` lists it once the identity's space has started on the new version.
  An upgrade never widens what an exception lets through.

The profile is the kind of call. One exception names one profile, so a header an agent sends on
every call needs one exception for each kind of call it makes:

| Calls | Profile |
| --- | --- |
| Anthropic Messages (Claude Code, Anthropic's SDK), token counting, the model list and one model | `anthropic.messages.1`, `anthropic.count_tokens.1`, `anthropic.models.1`, `anthropic.model.1` |
| OpenAI's format (OpenAI, OpenRouter, a company gateway): Chat Completions, older completions, Responses over HTTP, embeddings, the model list and one model | `openai.chat.1`, `openai.completions.1`, `openai.responses.1`, `openai.embeddings.1`, `openai.models.1`, `openai.model.1` |
| Codex signed in with ChatGPT, over HTTP: Responses and the model list | `chatgpt.codex.responses.1`, `chatgpt.codex.models.1` |
| Gemini, on the Gemini API or on Vertex AI: generating, streaming, counting, embeddings (one or a batch), the model list and one model, creating or changing a cache, the OpenAI-compatible chat and embeddings | `gemini.<version>.generateContent.1`, `gemini.<version>.streamGenerateContent.1`, `gemini.<version>.countTokens.1`, `gemini.<version>.embedContent.1`, `gemini.<version>.batchEmbedContents.1`, `gemini.<version>.models.1`, `gemini.<version>.model.1`, `gemini.<version>.cache-create.1`, `gemini.<version>.cache-patch.1`, `gemini.<version>.openai.completions.1`, `gemini.<version>.openai.embeddings.1` |
| Bedrock: invoking, streaming, counting tokens, the inference profiles and one of them | `bedrock.invoke.1`, `bedrock.invoke-with-response-stream.1`, `bedrock.count-tokens.1`, `bedrock.inference-profiles.1`, `bedrock.inference-profile.1` |
| Claude on Vertex AI | `vertex.anthropic.rawPredict.1`, `vertex.anthropic.streamRawPredict.1` |
| A company gateway in front of Bedrock or of Claude on Vertex AI | `company.bedrock.invoke.1`, `company.bedrock.invoke-with-response-stream.1`, `company.bedrock.count-tokens.1`, `company.vertex.rawPredict.1`, `company.vertex.streamRawPredict.1` |
| Azure OpenAI destinations | `azure-openai.chat.1`, `azure-openai.completions.1`, `azure-openai.responses.1`, `azure-openai.embeddings.1` |
| Microsoft Foundry destinations | `foundry.chat.1`, `foundry.embeddings.1` |

In a Gemini id, `<version>` is the one the request's path names: `v1` or `v1beta` on the Gemini
API, `v1` or `v1beta1` on Vertex AI (`gemini.v1beta.generateContent.1`).

On a Latet server, exceptions are part of each person's own settings, approved by that person in
the dashboard; an API key's are set through the administration.

## Bedrock and Vertex: what to list

A model id written in a provider's documented form passes on its own, in a path as in a body, and
so does a Bedrock inference profile defined by Amazon (`us.anthropic.claude-…`, `eu.`, `apac.`,
`global.`). What belongs to your account or your cloud project must be listed, in
`request_resources`, before a request that names it is sent: any ARN in a Bedrock path (an
application inference profile's ARN included), and the Vertex project. Each row names the
destination, its address, the role of the value and the value:

```yaml
request_resources:
  - destination: bedrock
    url: https://bedrock-runtime.us-east-1.amazonaws.com    # model calls; reads go to https://bedrock.us-east-1.amazonaws.com
    role: model                                             # inference-profile, to read one
    value: arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/a1b2c3d4e5f6
  - destination: vertex
    url: https://us-east5-aiplatform.googleapis.com
    role: project
    value: acme-ml-prod
```

`latet config add request_resources '{...}'` adds one. A row lets through that exact value at that
destination and nothing else, and applies at once. Until it is there, such a request is refused
with `envelope-resource`. A model id outside the documented forms (a company gateway's own model
names) is listed the same way, with the role `model`.

On OpenRouter a model is named by its vendor. Anthropic's, OpenAI's and Google's pass on their own
when the part after the vendor is in that provider's documented form, with or without a variant
such as `:free` or `:thinking` (`anthropic/claude-sonnet-4.5`, `openai/gpt-4o`,
`google/gemini-2.5-pro`), and so does `openrouter/auto`. Any other vendor's model (`meta-llama/…`,
`deepseek/…`) is listed with the destination `openrouter`, the address `https://openrouter.ai` and
the role `model`.

## A project where Latet is off

In 0.10, a project where Latet was off sent its requests unmasked. In 0.11 Latet sends none of
them, over HTTP or a WebSocket, whether the project came from the agent's launch, the plugin or a
folder the request named. The agent is told:

```text
Latet is off in this project: Latet sends no request from there; turn it on for the project (`latet project on` in its folder, or the project's switch in the dashboard), or reach the provider directly for this folder by starting the agent without Latet's address. Nothing was sent; run `latet dashboard` to see where it was
```

When the project is only named by the request (claimed, below), the words begin
`request claimed a project where Latet is off:` and go on the same way.

`latet project off` and the dashboard's switch say so before they turn a project off. To work in
that folder without Latet, start the agent there without Latet's address: an agent you start with
`latet claude`, `latet codex` or another `latet` command starts on its own (`claude`, `codex`); one
whose own settings point at Latet (after `latet global-install`) goes through Latet wherever it
starts, so give it its provider's address for that session, as its own documentation says, or
`latet agents revert` it. Latet then sees nothing of what it sends. If you turned Latet off to let
personal data through, *Secrets only* does that and keeps protecting secrets
([Projects](../../concepts/projects/#secrets-only-or-latet-off-in-a-project)).

## Folders a request names

A request can name its folder: the header `x-latet-project`, or the working folder Codex, OpenCode
and Gemini CLI write into their requests. In 0.11 such a folder is *claimed*: its project's rules
that add protection apply, and its loosenings do not (values never to mask, secret rules turned off,
a weaker profile, `data_tools`, the local reviewer's guidance and images). A claimed project where
Latet is off is refused, as above. A project is *trusted* when Latet knows it for itself: from the
launch key of an agent it started, or from the Claude Code plugin. The dashboard shows each request's
project as trusted or claimed, and how its launch was decided. See
[Projects](../../concepts/projects/#which-project-a-session-is-in).

What this means for each agent:

- **Started by Latet** (`latet claude`, `latet codex`, `latet opencode`, `latet gemini`,
  `latet copilot`): trusted through its launch key, where the agent carries it. Codex on its
  built-in providers (signed in with ChatGPT, or OpenAI's own provider), Codex older than 0.115,
  OpenCode older than 1.19.5, Gemini CLI in ACP mode, Copilot CLI outside Chat Completions, and the
  programs `latet run` starts carry none: the launcher
  says so in a sentence, and their projects are claimed.
- **Claude Code with the plugin**, configured for good: trusted through the plugin.
- **Any other agent configured for good**: claimed. Your own rules and every exception without
  `project` still apply.

## Conversations through a company gateway

Claude Code through LiteLLM or another company gateway that relays thinking signatures relies on
Latet having seen those signatures come back. The built-in rule for the provider's encrypted fields
applies only to the provider's own address, never to a company route. After the daemon restarts, or
on a server once the conversation's records are gone, such a conversation is refused at its next
turn: start a new one.

## Going back to 0.10

```sh
latet upgrade --to 0.10.4
```

Going back to the release a copy of your data was taken under offers to put that copy back
([Upgrade](../installation/#upgrade)). Latet 0.10 ignores `request_exceptions` and
`request_resources`, and sends again what 0.11 refuses: unread parts of requests, the requests of
projects where Latet is off, and images it cannot read. Go back only knowing that.

One setting is one 0.10 does not know: `request_profile`, on a destination in `upstreams`. 0.11
writes it only for a destination where you chose a profile, in `config.yaml` and in
`config.reviewed.json`, the settings you approved last, in Latet's data folder. 0.10 does not start
with it in `config.yaml`, and reads a `config.reviewed.json` that holds it as no approved settings at
all: a change to `config.yaml` would then apply without waiting for your approval. If you chose one,
put back the copy the upgrade to 0.11 took, which holds both files as they were before it:
`latet upgrade --to` offers it as it goes back (it is in the data folder, in
`backups/before-0.11.0-<time>`). Without that copy (0.11 installed by the installer, which takes
none), take the key out of both files before you go back: the `request_profile:` lines in
`config.yaml`, and each `"request_profile": "…"` member in `config.reviewed.json`, keeping it valid
JSON.

On a Latet server, 0.11 adds tables to the database, and an instance refuses a database newer than
itself: going back means the database backup taken before the upgrade, with every instance stopped
([Upgrading and adding instances](../../concepts/server-operations/#upgrading-and-adding-instances)).
