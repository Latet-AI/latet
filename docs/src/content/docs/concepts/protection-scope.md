---
title: What is and isn't protected
description: The limits of Latet. Read this before you rely on it.
---

Latet reduces what your agent discloses to the model provider. It is not a guarantee
that nothing sensitive ever leaves your machine. This page lists what is covered and what is not.

## Protected

When a [detector](../detection/) recognizes a value, Latet replaces it with a surrogate before
the protected request reaches the model provider. With a server, original data can reach
that server first. That covers:

- **In Claude Code (client mode):** the prompts you type, the system prompt, `CLAUDE.md` files and
  the other context Claude Code adds, attachments, skill prompts, tool descriptions, and every tool
  result: file contents, command output, hook output. A final check then reads the texts of each
  request before it is sent. What the model wrote stops nothing when it comes back exactly as the
  model wrote it, which Latet knows from the answers it passed on and from the tool calls the plugin
  had restored; any other text in the model's place is read as yours. On a computer the daemon keeps
  this in its memory: after a restart, a text the model wrote earlier in the conversation is read as
  yours, and one that holds a value Latet masks stops the request, so start a new conversation then.
  A Latet server keeps it for every instance, for an hour after its last use.
- **In gateway mode:** the system prompt or instructions, and every message of the conversation:
  your text, the model's earlier answers, tool calls and tool results. Embeddings input too. A final
  check then reads those texts before the request is sent, as in client mode.

Gateway responses, streams and tool-call arguments are restored where Latet runs: on
the computer for local use, or on the server for a connected gateway. Claude Code's plugin
restores tool arguments and interactive on-screen text on the computer; `claude -p`
stdout still contains surrogates. OCR/NER processing runs where the applicable Latet
engine runs; a configured reviewer can receive original text.

The keys of a JSON document, such as a tool's output, are masked as its values are: an email
address, a phone number, a secret, a name or one of your terms written as a key is replaced with the
fake it has everywhere, and a key the model writes with a fake in it is restored for the agent. A
field rule and strict data mode still find a value by its key as it came, and a key that only names
a field, such as `id` or `email`, is left as it is.

With the [local reviewer](../llm-review/) on, a language model on your computer also reads those
texts before they leave, and masks the names, health details and other values no detector finds,
in the keys of a JSON document too. A key written like a field's name, in lowercase letters, digits,
`_` and `-` (`exit_code`, but also `mario_rossi`), is not shown to it: the detectors read it as they
read every key.

## Not protected

**Values the detectors do not recognize.** Latet masks only what it detects. A secret in an
unusual format, split across lines, or encoded (for example in base64 under an ordinary name) can
pass through. Names and street addresses in free text are found only with the optional
[local name model](../../getting-started/installation/) or the [local reviewer](../llm-review/),
both best effort; without them, only your own git author name, your terms and values already in the
vault are masked. Dates of birth are not detected. Detection always has false negatives.

**Images.** With the optional [text recognition](../images/), the text in a screenshot is read on
your machine and each value found is covered with its fake. It is best effort, like names: text it
does not read leaves as it is. Without it, images are sent as they are and flagged *Not inspected*.

**PDFs and other documents, images by their address, audio clips.** Their content is not read and is
sent as is, by a Latet server too; the dashboard marks those requests as *Not inspected*. Do not
attach documents that contain sensitive data, or set `images: block` (the `max` profile does the
same): a request that carries one is then not sent. A browser's pages, read as text, are masked like
any tool output ([production data](../production-data/)); its screenshots are images.

**Web tools, subagents and claude.ai connectors keep the fakes (Claude Code).** WebFetch,
WebSearch, subagents and claude.ai connectors (Gmail, Calendar...) receive the surrogates the model
wrote, not the originals, because their arguments leave your machine or start another model
conversation. A web search for a masked name would look for someone else, and a connector asked to
email `emily.carter@northwind-legal.com` would get the fake address, so a web tool or connector call with
a stand-in in it is stopped before it runs: the model asks you, and *Not sensitive* in the dashboard
lets the value go out in clear. The results of claude.ai connectors come back through Anthropic's
servers, where the connector runs: whatever they contain is already there before Latet can mask
it.

**Tools the provider runs (gateway agents).** A web search or a remote MCP server the provider runs
itself, as Codex can, works on what the model wrote: the stand-in. Latet sees such a call only
after it ran, in the next request's history: the dashboard flags it (*Stand-in to a tool outside*),
with *Not sensitive* for each value, so the next time the model uses the real one.

**Surfaces Latet cannot see:**

- Claude Desktop Chat and Cowork, signed in to Claude;
- cloud sessions, which run on the provider's infrastructure rather than on your machine;
- Cursor, Windsurf and the other tools whose model requests are sent from the vendor's servers
  ([the list](../../agents/other-tools/#what-latet-cannot-protect));
- GitHub Copilot's inline suggestions and Zed's edit predictions, which come from their vendors'
  own services;
- OpenCode signed in with ChatGPT (on the [roadmap](../../roadmap/));
- what an agent's own tools send when they run, such as a web search API, a database or a vector
  store in the cloud: they get the real values (see
  [Agent frameworks](../../agents/openai-compatible/#agent-frameworks));
- any agent or surface you have not configured to use Latet.

Signed in to Claude, the Claude Desktop Code tab masks content with the plugin, but the app ignores
`ANTHROPIC_BASE_URL`: there is no daemon check behind the plugin, and no record in the dashboard. On
a third-party gateway, with no Claude sign-in, Chat, Cowork and Code send every request through
Latet: Latet checks each one and stops one that would carry a sensitive value in clear, but masks
only in the Code tab, with the plugin. See [Claude Code](../../agents/claude-code/#coverage) and
[Claude Desktop on a gateway](../../agents/other-tools/#claude-desktop-on-a-gateway).

**Context and metadata.** File names, paths, code structure, comments and business logic are sent
as they are, apart from the values Latet detects in them, such as your user name inside a home
path. Surrogates keep the shape of the original (length, format, prefix; a secret shorter than
twelve characters gets a longer surrogate), and the surrounding text can reveal what a value is
("the production database password").

**What a JSON document holds where masking does not reach.** A card number written as a JSON
number, which the `balanced` profile does not mask, is found by the final check, which then stops
the request: the agent is told why, and the dashboard shows where the value is. While such an output
is in the conversation, each of its requests stops: start a new conversation, and ask the agent for
the data in another form. A fake drawn for a key is none of the other keys of its object; a tool call
whose input holds a value and its fake as two keys, which would be one once masked, stops the
request, since an object holds a key once, and two such keys are left as the model wrote them when
Latet restores its answer for the agent. A JSON object that gives a key twice is read with its last
copy, and leaves written anew with that copy alone, so that the copy Latet did not read never leaves;
a request that Latet only checks, such as Claude Code's, is stopped when its own body gives a key
twice.

**Colliding tool history.** Retaining both keys in a restored call preserves its data, but masking
that call again can make the keys identical. This is a documented limitation: the next request and
every later request retaining that call are refused. Start a new conversation, or remove or
reformulate the colliding call through your client's supported history controls. Retrying the
unchanged request does not recover the conversation.

**Streamed tool arguments.** Latet holds JSON tool arguments until the call completes, then restores
the complete document without merging distinct keys. Ordinary text continues streaming. Held JSON
channels share one bounded response budget, including their metadata and reconstruction and
serialization growth. Exceeding it, or receiving incomplete or invalid arguments, ends the stream
with an error. An Anthropic call with nonempty start input cannot also supply a second nonempty
JSON argument source. A call interrupted before completion is not recorded as complete model output.

**Accepted model-output records.** After validating a response batch, Latet accepts its model-output
records and queues their recording before returning that batch's output. Recording runs in the
background: acceptance proves neither delivery to the client nor that the records are already
stored. Cancellation before acceptance publishes no records; after acceptance recording may finish
even if delivery is cancelled.

Within one instance, each identity's gateway admits at most 32 MiB of these records and 256 batches
across its sessions, including queued and running work. Masked and restored text, response identifiers
and record overhead count toward admission. This bounds the new retained work, not total process memory (RSS); delayed
cleartext retention and interpreter overhead still consume memory. Full or closed admission, or a
failed submission, refuses that batch's output. Earlier streamed text cannot be withdrawn.

The records become visible later. Requests queued afterwards on the same gateway worker follow its
recording attempt. On a Latet server this guarantees neither that Valkey has written them nor that
another instance sees them, nor their order with the Claude Code plugin's work. Shutdown can discard queued records. A store failure after delivery
gets a paced diagnostic containing only a fixed category and the exception type; it cannot undo
delivery or provide transactional rollback. Missing records still use the ordinary session lookup,
masking and refusal rules.

**Request fields outside the conversation.** In gateway mode, the tools' descriptions, the schemas
of their parameters and the examples of their input are masked like the conversation, and so are the
schema of a structured output, a predicted output, and what a request says of whom it is for (`user`,
`safety_identifier`, `metadata`). The tools' names, what lets the provider run a tool of its own (a
remote MCP server's address and headers, a web search's location) and the other request settings
are forwarded as they are. In Claude Code, the plugin masks the tools' descriptions and the final
check reads `metadata` and the schema of a structured output; from a computer, the schemas of the
tools' parameters are sent as they are, and a Latet server checks them (see below). A client other
than Claude Code on a route Latet only checks (a subscription's sign-in, Bedrock, Vertex) has no
plugin: the final check reads its tools' descriptions, parameters and examples too, and stops a
request that would let a value out in clear in them.

**Claude Code's tools on a server.** The plugin masks the tools' descriptions, including those of
`tool_addition`. A Latet server does not take the request's markers as proof that the plugin ran:
its final check reads each tool's description, the schema of its parameters and the examples of its
input, as it does for a client with no plugin, and stops a request that would let a value out in
clear in them. An MCP tool whose schema names a customer's address therefore stops Claude Code's
requests on a server until the value is removed from the schema or allowed in your settings. A tool
definition in a form Latet does not read is refused, as for every client. The metadata,
structured-output and fallback-schema checks described here still apply.

For Anthropic, structured-output scanning also covers descriptions and schema keys and values in
each `fallbacks[].output_config.format`. Rewriting masks sensitive schemas; on checked requests,
sensitive schemas are refused and safe bytes stay unchanged. Model-only fallbacks remain supported.
An enabled server refuses malformed fallback containers and output schemas.

**What Latet does not read, on a Latet server.** A computer sends as it came what Latet does not
read; a Latet server refuses it, with an error that says why. It refuses a request whose body Latet
does not read: one to an endpoint other than a model's call, such as files or batches, or with a
method other than POST. It refuses content in a form Latet does not read: a completions prompt given
as tokens; a prompt variable, a tool definition or an answer's format of a kind it does not know; a
part of content, a block of a message or an input item of a kind it does not know, in chat
completions, Anthropic's messages and the Responses API alike, or one that is not an object; a field
it reads given in another form, such as a tool call's input that is not an object or a system prompt
that is neither text nor blocks; and, in the Responses API, the audio of a live conversation
(`audio`, `output_audio`) and what was heard in an audio clip. It refuses a WebSocket frame other
than a turn (`response.create`; `response.cancel` passes) and, on a request it only checks (Claude
Code, Bedrock, Vertex), an image whose text it could not read, whatever `images` says. What the
provider made and is given back as it made it passes: the model's reasoning, a compaction, the calls
of the provider's own tools and their results. So does what holds no content, such as a reference
to an earlier item, and a request with no body, such as the list of models.

An enabled server also refuses chat messages carrying `name`, Anthropic `search_result` blocks,
and document blocks carrying `title` or `context`, including inside tool results and with
`images: allow`. These optional forms are unsupported on the server even when their values are
empty or null. A document without a title or context follows the `images` setting, as everywhere.
A computer does not refuse these forms, and does not mask them either: their text leaves as it
came. A search result and a document are flagged *Not inspected*; a chat message's `name` is
neither masked nor flagged, so keep the values you protect out of it.

**Claude Code's auto mode review.** Auto mode's reviewer is sent your OS user name and the paths of
your home and working directories, plus any host name or git author in your CLAUDE.md or the action,
and no plugin can mask them. By default the daemon stops those reviews, so the details stay local
and auto mode cannot approve actions by itself. `auto_mode_review` lets each detail through to the
reviewer, and only to it, or gives the reviewer a fake user name; see
[Claude Code](../../agents/claude-code/).

**Your own machine.** Latet is not a sandbox: it does not defend against malware, other users of
your account or a compromised agent. The tools the agent runs on your machine, local MCP servers
included, receive the real values, so that files and commands work: a command that sends data over
the network sends the real value. Run the agent in a sandbox when it needs one.

**Rules added mid-conversation (Claude Code).** Say you add `Project Falcon` as a custom term
halfway through a conversation. New content is masked from then on, but the earlier turns that
already reached Anthropic with the name in clear are sent again with every request. Latet flags
them in the dashboard instead of blocking them: they were sent already, and blocking would stop the
conversation. Start a new conversation to leave them behind.

**Data sent earlier.** Anything sent before Latet was installed, or while it was off, is already
with the provider.

See the [security and threat model](../../security/) for the reasoning behind these limits.
