---
title: Changelog
description: Notable changes to Latet, newest first.
---

Latet follows [semantic versioning](https://semver.org). Changes not in a release yet are listed
under **Unreleased**; each release lists its changes newest first.

## Unreleased

What comes next is on the [roadmap](../roadmap/): a plain refusal from auto mode, a signed installer
for Windows, your own name models, more agents, and the documentation in more languages.

## 0.9.1 (2026-10-22)

Fixes found by the first runs of CI on GitHub's Linux, macOS and Windows runners.

- Fixed: the installers and `latet upgrade` took the newest Python uv found or could download, and on
  one newer than Latet's dependencies have wheels for (3.15 on GitHub's macOS runner) google-re2 had
  to be built, and could not be: the install stopped. They ask for Python 3.12 or 3.13, the versions
  Latet is tested on, and uv fetches one where there is none.
- Fixed: on Windows, whose clock moves in steps of about 16 ms, a document or an image given no time
  left to be read was read all the same: a deadline is now reached at its own instant.
- Fixed: on a Latet server, a request that came just as another had waited in vain for a name to
  resolve could wait for that same lookup, and be refused too; a lookup a request has waited out is
  now joined by no later one, and the next request asks the name server anew.
- On a Latet server, the administration log reads an entry's details 64 levels deep at most on every
  computer, as its responses encode them: deeper, the entry is one that does not hold, wherever the
  server runs.

## 0.9.0 (2026-10-22)

The first release, its changes listed by milestone, newest first.

### Found in real use

- Fixed: a name between quotes, as Node's `console.table` prints a string, kept one quote in its
  fake (`Marta Galli'`). A name the local model finds never begins or ends with a quote or an
  apostrophe.
- Fixed: the output of a command you run with `!` in Claude Code was read as one of your prompts. It
  is masked as the Bash tool's output, its tables in strict data mode included.
- Latet's own name is never masked: whatever finds it (a term, a value in the vault, the local
  reviewer) leaves it in clear.
- Strict data mode reads a table drawn with box characters (Node's `console.table`, psql's unicode
  style) as a table, each value under its column, and reads its cells for secret names as any line.
- Start at login on macOS no longer starts the daemon again every 10 seconds.

### More agents and tools

- **More tools, each set up with a setting of its own.** GitHub Copilot in VS Code and the Copilot
  CLI, JetBrains' Junie CLI, Qwen Code, Crush, and the CLIs of Cline, Kilo Code and Continue go
  through Latet's gateway, each tested for real on every API it speaks. The guide covers JetBrains AI
  Assistant, Zed, Android Studio and Claude Desktop on a gateway too, and lists the tools Latet
  cannot protect. See [GitHub Copilot, JetBrains, Zed and other tools](../agents/other-tools/).
- **Hermes Agent, OpenClaw, Goose and OpenHands are tested for real**, each set up as its guide
  says. The guide now gives Goose its provider and model (`GOOSE_PROVIDER`, `GOOSE_MODEL`), without
  which it asks for `goose configure`, and Hermes its folder on Windows, `%LOCALAPPDATA%\hermes`.
  See [Hermes Agent, OpenClaw and other agents](../agents/autonomous-agents/).
- **LangGraph, AutoGen and CrewAI are tested for real** through Latet, as LangChain is, each set up as
  [Scripts and SDKs](../agents/openai-compatible/) says; the page says too that a
  tool an agent calls itself, such as a web search API, gets the real values.
- **Which tools stay protected, and how**: the quickstart says which tools `latet global-install`
  sets up, which keep Latet's address in their own settings, and which take it from the environment
  for each command; Gemini CLI is not available in global mode.
- **Qwen Code's Anthropic provider is masked.** It sends Claude Code's user agent, and Latet took
  its requests for Claude Code's: it only checked them, and stopped every one that carried a secret.
  Latet now knows Claude Code by its session id and its attribution block.

### Google's Gemini API

- Gemini CLI, with an API key or Vertex AI, goes through Latet with `latet gemini` (or `latet run
  gemini`): its requests are masked and its answers restored, streamed or not. Signed in with
  Google, it goes to an endpoint Latet does not read, and `latet gemini` does not start it. See
  [Gemini CLI and Google GenAI](../agents/gemini/).
- The gateway reads Google's Gemini API on `/gemini` and Gemini on Vertex AI on `/vertex`, with a
  project or in express mode: generating, counting tokens, embeddings and creating a cache. Any
  other request with a body on these routes (the Interactions API, the Batch API, file uploads,
  image generation) is refused rather than sent unmasked, and a WebSocket of the Live API is closed
  before anything goes through. The API key reaches Google as it was sent
  and is never recorded, a debug capture included.
- `latet run` and `latet env` also set `GOOGLE_GEMINI_BASE_URL` and `GOOGLE_VERTEX_BASE_URL`, which
  Google's GenAI SDKs read. The dashboard names Gemini CLI and shows Gemini's answers; `latet
  doctor` and **Protected agents** know Gemini CLI.
- `gemini` is one of Latet's own routes: a destination cannot take that name.

### Strict data mode for files and web addresses

- `data_files` names the files strict data mode covers when the agent reads them (`*.json`,
  `exports/**`, `.env*`), and `data_hosts` the web addresses (`*.salesforce.com`, which covers
  `salesforce.com` too), globally, in your settings for a project, and in a project's `latet.yaml`,
  which may add them. A file's values are found by its format (JSON, tables, `.env` and properties,
  YAML; a markdown file a pattern names, its tables only) and replaced where they are, so the file
  keeps its lines and its keys; what an edit or a write of that file shows back is masked as the
  file is. See [Production data](../concepts/production-data/).
- The dashboard sets them under **Rules** → **Protection** → **Production data**, beside the tools
  and MCP servers that count as data. An exchange keeps the file each tool output came from, masked
  as the request carried it, and a web address by its scheme and host alone, so forgetting a value
  removes it there too.

### The dashboard

- **The documentation from every part of the dashboard.** Each page has *Documentation* at its top
  right, each section of **Rules** and each rule's panel a *?*, and each notice that says what
  happened a *Why*: each opens the section that explains it, in the dashboard's language, in a new
  tab. What opens is the documentation of the version installed, which Latet serves itself at
  `/_latet/docs/`, so it works offline and asks nothing of another host; the wheel and the container
  image carry it (about 13 MB more). An install without it opens docs.latet.ai. See the
  [Dashboard guide](../dashboard/).
- **The rules as tables.** Every list under **Rules** has a tab and a table of its own (terms,
  recognizers, the values and patterns never masked, policies), with search, filters, pages and a
  selection turned on, off or deleted at once; the search at the top of the page finds rows in every
  tab. A row opens in a side panel, a full-screen window on a phone, where it is edited, tried and
  saved by itself, and leaving with an edit not saved asks first. The terms and the values never
  masked are imported and exported as CSV, the recognizers as a rules file. In a project each table
  shows every rule that applies there and where it comes from (the global rules, your settings for
  the project, the project's `latet.yaml`), and *Save to* says where a new one goes.
- **The Playground masks when you ask**, with *Mask text* or Ctrl+Enter, never as you type. It offers
  the four kinds of text that mask differently (a prompt, an attachment, a tool's output, the agent's
  instructions) and tries a text as another profile would mask it, your settings left as they are.
- **The texts waiting in the Live Reviewer follow your rules.** A value marked *Not sensitive*,
  masked in another text, or a setting changed shows in the texts still waiting at once, in every
  dashboard open and on every instance of a server; a text you approve leaves masked with the rules
  of now.
- The vault comes in pages of fifty values, newest first, with page links under the table; the page
  is in the address, and a new search or filter starts from the first.
- The Integrations page is **Protected agents**, and a line above its table says what Yes and No mean
  under *Through Latet*.

### Latet on a server

- One server over HTTPS for people and API keys, with separate spaces, each with its own settings
  and rules, and the settings the server shares with every space (its destinations, the shared
  local reviewer, text recognition, history and retention), backed by PostgreSQL and Valkey across
  multiple instances. See [server setup and connection](../getting-started/server/).
- Administration from a person's own terminal with `latet server`: people, API keys and their
  rules, the shared settings, administration tokens and the log. Host commands initialize, migrate
  and run the server.
- Rules set for everyone, which nobody can lower, are planned for the Enterprise Edition: without
  `--key`, `latet server config` refuses a protection rule. A team shares its rules by hand, with a
  rules file exported from one place (`latet rules export`, or **Export rules** in the dashboard)
  and imported in each (`latet rules import`, or **Import rules**); an API key's with
  `latet server rules --key KEY import FILE`; on computers not connected to a server, a project's
  `latet.yaml`. The dashboard's **Rules** page shows a person's own rules, and **Protected agents**
  marks the destinations set for everyone.
- A Compose server profile with two instances and Caddy, plus systemd and Windows service
  installation plans. Authenticated metrics use a separate listener; the Compose setup
  scrapes inside each instance. See [Server operations](../concepts/server-operations/).
- Connected `status`, `doctor` and `dashboard`, explicit `identity send`, and refusal
  of local settings mutations while connected. The connected Claude helper supports Windows
  terminals; a server checks the descriptions, schemas and examples of Claude Code's tools.
  This does not extend compatibility to every agent surface.
- Each computer gets a computer key with its token. The token Claude Code holds opens the plugin's
  bridge and the space's proxy, never the dashboard, and never lowers the person's protection; the key
  stays in the credential store, and `latet dashboard` and `latet identity send` refuse to run inside an
  agent on a connected computer. See
  [Latet on a server](../concepts/server/).

### The tutorial

- `latet tutorial` shows Latet at work in your browser, in about 30 minutes, before you point a
  real agent at it: a pretend agent sends its requests to a pretend model through the real Latet,
  with invented data. Nothing leaves your computer. See the [Tutorial](../getting-started/tutorial/).
- Nine chapters, each shown in the dashboard's own views: a key and a person's details masked, your
  own terms, a value masked for nothing, a request stopped and how to go on, the Live Reviewer, an
  agent lowering your rules, production data, the local reviewer, and screenshots.
  `latet tutorial terms` opens one, and `--list` prints them with the ones you finished.
- In the local reviewer's chapter, the reviewer is the Tutorial Reviewer, simulated: no model of
  yours is needed. The screenshots chapter reads its screenshot with your own text recognition
  models, and until they are installed (`latet ocr install`) the menu says so in place of Start.
- It runs in a sandbox: its own settings, vault and history in a temporary folder, deleted when you
  end it (*End the tutorial*, or Ctrl+C), and its own server on `127.0.0.1`. Your daemon, settings,
  vault and dashboard sign-in are not touched; `tutorial.json`, in your data folder, keeps the
  chapters you finished.
- The installers end by suggesting it, before your agent, and so do the README and the Quickstart.

### Strict data mode with the max profile

- The `max` profile turns strict data mode on: every value of the tables, JSON and `key=value`
  pairs a tool prints is masked. `data: normal` turns it off there, `data: strict` turns it on with
  the other profiles, and left out, `data` follows the profile, in a project too. Turning it off
  where it is on, by `data: normal` or by leaving `max`, waits for your approval like any change
  that lowers protection.
- A `config.yaml` saved by an earlier Latet says `data: normal` even if you never chose it, and a
  value you set wins over the profile: `latet config set data --unset`, or *As the profile* on
  the dashboard's **Rules** page, lets the profile decide.

### Installed from GitHub

- The installers install the latest release from GitHub, with local name detection and text in
  images (without them where they cannot be installed, and saying so at the end); their models are
  still downloaded only when you ask. `LATET_SOURCE` installs what it names instead. The same
  release is also on PyPI, as `latet`.
- `latet upgrade` installs the latest release the same way. A Latet installed without name
  detection or text in images gets them with it, and the commands that need them say so. One that
  has them never loses them: where the new release cannot have them, it is left as it was.
- Where GitHub's API does not answer (it takes 60 requests an hour from one address), both read the
  page of the latest release instead. A lookup that fails says why (what GitHub answered, or that it
  could not be reached), where the releases are, and how to install one by hand with
  `LATET_SOURCE`: see [Troubleshooting](../troubleshooting/).
- Names and images are the recommended setup from day one: the installers end by naming
  `latet ner install` and `latet ocr install`, and the installation pages say why.
- Fixed: in Windows PowerShell 5.1, the Windows installer stopped halfway when a command it runs
  quietly wrote to its error output, such as `uv tool update-shell` when its folder is already on
  `PATH`.

### Strict data mode, guided

- In an exchange, under a tool's output strict data mode masked, the dashboard proposes the columns
  that could go in clear (ids, dates of events, states, counts) and the ones to keep masked, each
  with its reason; one click keeps the chosen ones in clear, in the project or everywhere. A column is
  proposed in clear only when its name says what it holds and its values hold no personal data. See
  [Choosing the columns to keep in clear](../concepts/production-data/).
- The local reviewer gives its opinion on each column when you ask. Nothing applies by itself.
- While strict data mode is off for an exchange, a line says when one of the ten most recent tool
  outputs of its request holds a table, JSON or `key=value` pairs and comes from a tool strict data
  mode covers, with a link to where the mode is set for it: your settings for its project when they
  set `data`, else the global rules. The columns of a tool's output say whether strict data mode
  covers its tool (`applies`), and an exchange keeps the tool of each tool output it records.
- Field rules take a name as written, but for `*`: `?` and brackets are no longer wildcards, so a
  JSON path with two `[]`, such as `orders[].items[].sku`, now matches. A rule that used `?` or
  `[...]` as a wildcard needs a `*` instead.
- A column kept in clear from the dashboard goes first among your field rules, so a broader rule
  such as `note_*` no longer masks it again.

### Company endpoints

- Claude Code and Codex through your company's gateway, Amazon Bedrock, Google Vertex, Microsoft
  Foundry or Azure OpenAI, with Latet in front: `latet claude`, `latet codex` and
  `latet global-install` read the route from the agent's own settings, keep the company's address
  as a destination (`upstreams`) and go in front of it. See
  [Company endpoints](../agents/company-endpoints/).
- Amazon Bedrock with your AWS login: Latet signs each request for Amazon with your AWS profile,
  credentials in memory only. With a Bedrock API key, the key passes as it is.
- Bedrock's public address written out in your settings is signed by Latet too. A VPC endpoint or a
  FIPS address is refused, with its reason: use a Bedrock API key or your company's gateway.
- The AWS credentials of a `latet claude` session end with the session, even when it is killed,
  and at their own expiry.
- The AWS CLI is found in `~/.local/bin` and `/snap/bin` too; when there is none, `latet doctor`
  says where it looked.
- Google Vertex: each request goes to the address of the location it names.
- `latet codex` routes Codex's own providers (Azure OpenAI, a company gateway) too, the profile's
  you name included, and `global-install` changes each in the file that writes it: before, only
  OpenAI's address moved, and a provider of your own went straight to its address.
- A Codex older than 0.115 reads only `OPENAI_BASE_URL`: `latet codex` hands it Latet's address
  there, and does not start it when it has providers of its own. `agents apply codex` and
  `global-install` leave such a Codex as it is, and say why.
- Another OpenAI-compatible provider (DeepSeek, Mistral...) goes through Latet as a destination.
- `latet claude` passes its settings with Claude Code's `--settings`, which beat your settings and
  the project's: before, an address in your settings won over Latet's.
- `global-uninstall` gives back a company address it replaced, even if you edited the file since.
- An agent's company address is saved in `config.yaml` behind the daemon's back only when no daemon
  answers. A re-apply that is refused, by the daemon or because Latet can no longer go in front of
  the route, leaves the agent as it was, and `latet upgrade` goes on with the other agents.
- `global-install` with the daemon running keeps the settings you chose when it adds a destination.
- The Claude Code plugin stops prompts when a session's requests would go around Latet, and names
  the settings file that sends them elsewhere. A repository's settings never set where requests go,
  nor a proxy, even through Latet; when the check cannot be made, prompts stop too.
- Requests to Bedrock and Vertex are checked, never rewritten; one that is not Claude's messages is
  not sent.
- Moving or removing a destination outside the dashboard waits for your approval. A change waiting
  for you when an agent adds a destination is shown again, with the reasons it now has.
- The dashboard lists your destinations, and shows on each exchange where it went and whether Latet
  signed it. Removing one there takes away that one alone; one still in use stays. `latet doctor`
  and `latet agents status` show each agent's route.
- Fixed: the `x-latet-project` header, which names a folder of yours, reached the provider.
- Requests are forwarded with their path exactly as sent (an encoded `/` in a model ARN stays encoded).

### Screenshots and images

- The text in images is read on your machine and masked like any other text, and each value is
  covered on the image with its fake (a bar for a type set to `redact`); the answer gets the real
  values back. Optional: `latet ocr install` downloads PaddleOCR's PP-OCRv6 small models (about
  31 MB, Apache-2.0) once, at a pinned revision, checked by SHA-256; `latet global-install --ocr`
  too. See [Screenshots and images](../concepts/images/).
- Every agent: Claude's images, `data:` URLs in OpenAI Chat and Responses, Codex over HTTP and over
  its WebSocket, OpenAI's computer use screenshots.
- Claude Code: the images tools return (Read, MCP servers) are replaced by their masked copy; a
  screenshot you paste with values in it is taken out of the prompt, and Claude gets the path of a
  masked copy to open with Read. The final check covers images too.
- The same image sent again leaves the same, byte for byte, also after a restart, so the provider's
  prompt cache keeps working. An image that cannot be read is withheld, never sent as it was.
- `images: mask` sends only the images Latet read and masked; `ocr: off` stops reading them. Both
  in a project too. The max profile still sends no images; a project of yours can send them masked.
- The Live Reviewer shows an image with the boxes where its values are covered: drag a rectangle to
  cover more of it, or do not send it. Your decision is kept for that image.
- The local reviewer can be shown the images, only when you turn that on (`llm_review.images`, or a
  project's `review_images`), to find what text recognition missed: a value it sees and Latet
  cannot place withholds the image. `latet doctor` and *Try it* say when its model does not see
  images.
- The fakes on images are written in IBM Plex Sans, shipped with Latet (OFL-1.1): it has the
  accented letters, and its `O` and `0` differ.
- An IBAN or an Italian fiscal code read with a look-alike character (`O` for `0`) is put right by
  its check digits.
- The dashboard shows an exchange's images as they were sent (with `history: full`), whether text
  recognition is installed, and the three choices for images in **Rules**.
- `latet doctor` checks text recognition: the runtime, the models, and a sample read.
- Fixed: Codex signed in with ChatGPT, which talks over a WebSocket, sent images and documents even
  with `images: block` or the max profile.
- A request stopped by `images: block` or `images: mask` says so, in the agent's error and in the
  dashboard: before, it blamed the max profile.
- Fixed: the screenshot of a whole page, much taller than a screen (1280 by 12,288 pixels, say), or
  of several screens side by side, was read scaled down to 2,048 pixels on its long side. Its text
  shrank below what text recognition finds, and the image was sent as it came, with every value in
  it. Such an image is now read in overlapping parts, each at its own size, and its values are
  covered as on any screenshot; one so long for its width that it would take more than 16 parts is
  withheld. A 4K or 5K screen's screenshot, read at half its size or less, misread the small text of
  a screen at 100%: it is read in parts too, at two thirds of its size at least, as is any image
  larger than that.

### Milestone M4: hardening and release (done)

- A value the [local reviewer](../concepts/llm-review/) finds, or you mask in the
  [Live Reviewer](../concepts/live-review/), under a type set to `redact` is now kept: sealed in
  the vault, with a fake of its own that is never sent, so that Latet finds it again wherever it
  appears and redacts it there too. It is listed in the vault like any value Latet keeps, and if
  you set its type back to `mask`, that fake is the one sent. A value the detectors find under
  `redact`, your terms and recognizers included, is still not kept. See
  [Policies](../concepts/profiles/).
- Latvia joins the countries `id_regions` takes (`LV`; the `max` profile takes it too): a personal
  code (*personas kods*) is masked as it is usually written, `DDMMYY-NNNNN`, or `32NNNN-NNNNN` for
  the codes issued since July 2017, which hold no birth date. A code is confirmed by its check digit
  and, unless it starts with 32, by its birth date. Written as eleven digits, without the hyphen, it
  needs a keyword such as *personas kods* beside it. Its fake is a valid code of the same kind and
  layout. A person's VAT number, `LV` and the same eleven digits, was already found.
- Fakes follow the language of the original: an English name, user name or email address gets an
  English one, an Italian one an Italian one. An address keeps the shape of its country: after the
  street, its town becomes another town and a state or province code another code (`Springfield, IL`
  becomes `Exeter, AR`, where a family name took both places before).
- A request stopped because a value would leave in clear has that value marked in it, on its exchange
  page, as a masked value is: click it, and *Not sensitive* lets it through the next time. Values of one
  stop that overlap, such as a key found whole and in part, are one mark. The card on top says first to
  take the value out: a new conversation, with the agent pointed at the file or variable that holds
  it. *Not sensitive* is for a value that is no secret, such as a test card.
- An agent's error for a request Latet stopped names Latet once, and says that nothing was sent
  and where to see it: Codex over its WebSocket gets the same as an agent over HTTP.
- Fixed: a turn Latet stopped on Codex's WebSocket was not on the dashboard. It is now, as a
  request stopped over HTTP is: why it was stopped, and what it carried.
- Fixed: in JSON text, as an agent reads it in a file or a command's output, a value right after an
  escape (`\nGB82…`) was not found: an IBAN, a card number, a fiscal code, your terms and your user
  name left in clear, and an address was masked together with the escape's letter. They are masked
  now, the escape left as it was, and the final check finds them too.
- Fixed: a fake name, company or term at the start of a line of a file the model writes, with an
  agent that goes through Latet's gateway (Codex, OpenCode…), stayed in the file: it is restored now.
- Fixed: with strict data mode, a tool's output with hundreds of three-digit numbers, such as a
  query's ids, failed to mask and the request was stopped. A fake that has no free value of its shape
  left is a character longer.
- Fixed: with strict data mode, a line break or a tab written as an escape inside a value (JSON text
  in a command's output) was masked into letters. It stays as written: the model reads valid JSON.
- Fixed: a term of yours, your own name, or a value already masked that begins or ends with an
  accented letter (`Nicolò`, `Émile`, `Zoë`) was not found, and left in clear; and `Zoë` was found
  inside `Zoëlla`. Word edges are now read in every alphabet, so such values are masked as whole
  words.
- Fixed: in a tool's JSON output, a value Latet learnt from one key stayed in clear in the keys
  before it, such as an email address's domain or a password written again before its `password`
  key. An agent going through Latet's gateway sent it in clear, and in Claude Code the final check
  stopped the request every time the output was sent. It is masked in every key now, whatever
  their order.
- Fixed: a term with no letter or digit (`--`, `###`, `+`) stopped every request that held it, as
  its fake would have been itself. The dashboard, `latet config` and an import refuse such a term
  and say why; one written by hand in `config.yaml` or a `latet.yaml` is left out, and the **Rules**
  page and `latet config validate` point it out. A git author name of signs alone no longer masks
  those signs everywhere, the Live Reviewer refuses a selection of signs such as `__`, and a value
  of digits of another script (Arabic, full width) gets a fake of the same script.
- Fixed: any other value of signs alone failed the masking the same way. One the local reviewer
  reported, such as a `---` it took for a secret, stopped the request, or in a background review
  could keep what else it had found in that text from being masked; a recognizer that matched one,
  such as `-{3,}` (which passes the check against ordinary code), stopped every request with a
  Markdown rule line; and so did a `Bearer` credential of dashes or a JSON password `!!!!!!!!`. Such
  a value is now left as it is, whatever finds it, and `latet rules test` and *Try it* say why a
  recognizer leaves it.
- Fixed: half of an emoji, as an agent written in JavaScript can send when it cuts a tool's output
  at a fixed length, kept the request from being sent, and every later one of the conversation,
  which sends it again. Latet now reads such a half as `�` (U+FFFD), the character for one that
  cannot be read, and masks the text around it. Through Latet's gateway the provider is sent `�` in
  its place (Anthropic's API refuses the half as invalid JSON); Claude Code's own requests still
  leave as they are, and the plugin puts `�` in the texts it masks.
- Fixed: a text that left with a value in clear before Latet learnt that value, such as a domain
  named before any address at it, kept the value in clear wherever it came again: in a new tool
  output with the same value, and in the same output or text in another conversation. In Claude
  Code the final check stopped those requests, and an agent going through Latet's gateway sent
  the value in clear. It is masked there now; the conversation the text first left in sends it
  again as it left, so the provider's prompt cache still holds it.
- Fixed: when one request brought two new tool outputs and the second taught Latet a value, such
  as an address whose domain the first one named, or a password found by its key, the first
  output kept that value in clear. In Claude Code the final check stopped the request, and an
  agent going through Latet's gateway sent the value in clear. The new texts of a request are
  now masked again when a later one of them teaches Latet a value; a text the conversation sent
  before still leaves as it left.
- Fixed: the name of the field a value sat in, which can be a value itself (a JSON key that is an
  address, a column named after a person), was kept in clear beside the value's encrypted original:
  in the vault's record of the rule that found it, with strict data mode or a field named like a
  secret, and in the history's record of what stopped a request. Both keep the field's kind alone
  now (`strict`, `field-secret:password`), and what an exchange shows of each value still names its
  field. A vault and a history kept by an earlier Latet are corrected when Latet starts.
- Fixed: a fake could hold the value it stood for. A user name in a home path got a first name and
  two digits, so `emily` could become `emily59` and stay readable in the text the model gets; a
  computer named `host` got `host-` and six digits, which the final check found in every request
  that named it, and stopped. A fake drawn for a name, a company, a town, a domain or a host name is
  now never one of the original's words, never holds one and is never a part of one; a street made
  only of kept words gets another street; and the vault draws again any fake that holds its value,
  whatever made it. A fake made before that holds its value (`emily` and `emily59`) is drawn again
  when Latet starts, and kept in the vault in its place: the value is masked with the new fake from
  then on. Start a new conversation afterwards, since the one you have already carries the old
  fake.
- Fixed: with the [local reviewer](../concepts/llm-review/) on, a request stopped before the reviewer
  read a text (its server was off or slow, or its answer could not be trusted) sent that text on the
  agent's next try masked by the detectors alone, because the text was no longer new to Latet. The
  reviewer now reads it when the agent sends it again, and a text it already answered is not asked
  about again.
- Fixed: a change to `config.yaml` that lowered protection only in a project with stricter rules of
  its own applied without waiting for your approval. For example, a repository's `latet.yaml` asks
  for the `max` profile and an agent adds `include_private_ips: false` to `config.yaml`: that is the
  value the balanced profile has anyway, so nothing changed in your own rules and nothing was
  weighed, but private addresses were no longer masked in that repository. Latet now weighs such a
  change against every project it has seen, at every reload and every start, also when no
  conversation has reached the project since the daemon started: it keeps the folders of the last
  2,000 repositories it has seen with a `latet.yaml` of their own in `repositories.json`, in its
  data folder, and reads each `latet.yaml` again then, through a link too. A repository on a drive
  that is not connected stays in the list and is weighed again once the drive is back. A project it
  has never seen is weighed the first time it sees it, after a start that finds `config.yaml`
  changed. A rules import holds back what would lower protection in one. What lowers it waits on **Approvals** like any change that
  lowers protection: see [Agents and your settings](../concepts/agents-and-settings/).
- Fixed: a repository Latet had seen on a share that no longer answered, or on a drive gone
  without the system saying so, held every start and reload for as long as the system waited, with
  the daemon's settings locked meanwhile. The files are now read by a helper process that has 10
  seconds for all of them (2 for each) and its memory capped (on Windows and Linux; macOS enforces no
  such cap, and there the deadlines alone bound it), and is ended past its time; only when the change
  could lower protection in a repository, never for the port or the history. A repository that
  cannot be read in time, or not at all for now, is never taken for one with nothing in it: a change
  that could lower its protection neither applies nor waits until it can be read (the reload says
  so), and a start that cannot weigh `config.yaml` runs the settings you approved last. What a file
  holds never makes it unreadable: a value JSON cannot hold is left out with a notice, and a file of
  more YAML than a project file holds is not valid: nothing in it applies. `latet repositories`
  lists the repositories remembered with their state, and `latet repositories forget <folder>`, at
  your own terminal and once you confirm, takes one out of the list. A start reads each file once,
  where it read them twice.
- Fixed: a conversation in a repository read its `latet.yaml` whole, however large, and could wait on
  a pipe in its place. It is read as the repositories Latet remembers are: a regular file of 256 KB
  at most, never waiting on a pipe, through a link too. The dashboard's editor never saves over a
  `latet.yaml` it could not read (one too large, not a regular file, or refused): it says so.
- Fixed: `latet rules import` said *Added* for what then waited for your approval. It now says what
  it *would add*, then what the daemon's reload did: applied, waiting for you, or not known when the
  daemon did not answer (exit code `1`). With `--all` the import is one change, which waits whole.
- Fixed: a provider's answer that sends the request elsewhere (301, 302, 303, 307, 308, or any other
  3xx naming a `Location`) was passed to the agent, which could follow it and send its request,
  unmasked, straight to the address it names, past Latet; and the WebSocket Latet opens to a
  provider followed such a redirect itself. Latet follows none now, over HTTP, in a stream and at a
  WebSocket's handshake, and answers the agent `502` with an error that says the provider answered
  with a redirect, with no address in it. A provider that has moved needs its new address in your
  [destinations](../agents/company-endpoints/).
- Fixed: a term of a million letters could not be compiled, so the daemon did not start and a
  reload failed. A term holds at most 8,192 characters now. The dashboard, `latet config` and an
  import refuse a longer one, naming where it is and never the term; one written by hand in
  `config.yaml`, or in a project's `latet.yaml`, is left out with a notice (`latet reload`, the
  daemon's log and `latet config validate` say which entry, and the project's page says so), and the
  rest applies. See [Your own terms](../concepts/detection/).
- Fixed: a `config.yaml` with an allow pattern that RE2 cannot compile (a look-ahead, a
  back-reference, a missing parenthesis) kept the daemon from starting and a reload from applying.
  The pattern is left out now, so that more is masked, and a notice names its place in the log and
  in the answer of `latet reload`; the rest of the file applies. A save still refuses such a
  pattern. A setting Latet refuses is named by its place (`retention_days`, `upstreams.1.url`), and
  the message no longer quotes the value it was given, which can be a term or an address of yours.
  `latet reload` also says when the daemon refused the file, where it said that no daemon was
  running.
- Fixed: a JSON document that gave a key twice, in a tool's output or in a request, such as
  `{"token": "ghp_…", "token": "redacted"}`, was read with its last copy, and when nothing in it was
  masked it was sent as it came, with the first copy, which Latet never read. It is now sent written
  anew from what Latet read; a request that Latet only checks, such as Claude Code's, is stopped
  when its own body gives a key twice. See [What is protected](../concepts/protection-scope/).
- Fixed: a request Latet's gateway rewrote, for Codex, OpenCode or a client with an API key, left
  without the final check that reads Claude Code's requests. Masking reads the values of a JSON
  document and not its keys, so an email address, a phone number or a secret that was the key of a
  tool's output left in clear. The final check now reads the texts of a rewritten request or a
  WebSocket turn before it leaves, and stops one that would let such a value out, saying why; a text
  the conversation sent before still leaves as it left.
- Fixed: the keys of a JSON document, such as a tool's output, were left as they came: an email
  address, a phone number, a secret, a name or one of your terms written as a key left in clear, or,
  with the final check, stopped the request and every later one of its conversation. Keys are now
  masked as values are, by the same detectors and with the same fake, through Latet's gateway as in
  Claude Code's tool outputs, and a key the model writes with a fake in it is restored for the agent,
  in its tool calls too. A field rule and strict data mode still find a value by its key as it came,
  and the dashboard names a field under a masked key by the key itself, so that keeping it in clear
  makes a rule that matches it. A fake drawn for a key is none of the other keys of its object, and
  two keys that would be one once restored for the agent are left as the model wrote them. See
  [What is protected](../concepts/protection-scope/).
- Fixed: in Claude Code, the final check stopped a request for values that leave in clear on
  purpose: one of a type whose policy is `allow`, your term or user name inside a fake drawn for
  someone else (`Daniel Jordan` beside the term `Jordan`), and a password the model wrote, such as
  `TEST_PASSWORD = "hunter2"` in a test, which stopped every later request of the conversation too,
  all the more once Claude Code's answer to the edit showed it again and Latet learnt it. None of
  them stops it now. What the model wrote passes when it is exactly what it wrote, which Latet knows,
  until the daemon restarts, from the answers it passed on and from the tool calls the plugin had
  restored; a value of yours in the same place still stops the request. On a route Latet only checks, a client other than Claude
  Code (a subscription's sign-in, Bedrock, Vertex) has every text it sends checked as its own, the
  assistant turns it writes too, and its tools' descriptions, parameters and examples.
- Through Latet's gateway, the tools' descriptions, the schemas of their parameters and the examples
  of their input are masked like the conversation, where they were forwarded as they were; so are a
  completions prompt given as a list and its suffix, the variables of a Responses prompt, a
  WebSocket turn whose request is under `response`, what each command of a shell call printed, the
  reason given for an approval, tools given as an input item or added to an Anthropic conversation
  as it goes, a predicted output, the schema of a structured output (Anthropic's too), a refusal the
  model wrote, the input of a custom tool's call in chat completions, and what a request says of
  whom it is for (`user`, `safety_identifier`, `metadata`). A tool's name is sent as it is: the model
  calls the tool by it. Where Latet only checks a request, as Claude Code's, the final check reads
  `metadata` and the schema of a structured output too.
- In the `max` profile, the local name model reads the texts and keys of a JSON tool output at once,
  each as it reads it alone: a thousand new values take seconds, where they took minutes and ran past
  the time Claude Code gives the plugin. An output it cannot read within five seconds is withheld,
  with a note telling the model to ask for fewer rows or fields. See
  [Production data](../concepts/production-data/).
- Fixed: a value the [local reviewer](../concepts/llm-review/) found that masking could not place,
  such as an amount also written as a JSON number, stopped the request the first time and left in
  clear when the agent sent it again, since the reviewer's answer was then not applied. It is applied
  again to a text that has not left, so the request stops each time; in Claude Code, such a tool
  output is withheld.
- Fixed: a term made to match fewer forms, with `case_sensitive` or `whole_word` turned on, applied
  without waiting for your approval. An agent could rewrite `Acme Corp` in `config.yaml` to match
  its own case alone and run `latet reload`, and `ACME CORP` then left in clear. Such a change now
  waits on **Approvals** like any change that lowers protection, and says how many terms match fewer
  forms.
- Fixed: a user name written the older way in `config.yaml` (`identity: {username: emily}`) was
  dropped by any save of the dashboard's **Rules** page, so that, with your full name set,
  `/home/emily/` left in clear; and a change that dropped it applied without waiting for you. A save
  now keeps it, moved into `usernames`, and a change that drops it waits for your approval.
- Fixed: the dashboard's *Import rules* applied at once what it counted as protection added, even
  where it lowered the rules of a repository with a stricter `latet.yaml` of its own: a list of
  personal-data types written out at the top is used as it is in a repository at the `max` profile,
  which detects every country's ID numbers, and detects fewer there. It now weighs what it adds in
  every repository Latet has seen, as a reload does, and holds back what would lower one.
- The **Rules** page refuses a term longer than 8,192 characters as you add it, as it refuses one
  of signs alone, and says why in your language, where the refusal came back from the daemon in
  English when you saved.
- A value longer than 16,384 characters, such as a whole JSON string with strict data mode or a very
  long secret, is now redacted whatever its type's policy (unless that type is let through), as a
  type set to `redact` is: it is not kept in the vault, so the answer does not get it back. Kept, it
  made the vault and its detectors grow by tens of bytes for each of its characters. A private key
  of RSA 16,384 bits, a certificate or a token is shorter, and keeps its fake.
- The [local reviewer](../concepts/llm-review/) reads a long text in pieces of 16,384 characters at
  most (`llm_review.chunk_chars`, which took up to 60,000): a value it finds in a piece is then never
  longer than a value Latet keeps, so it is always kept and found again. A larger setting is read as
  16,384.
- The installers only install: your agents' settings change only when you run `latet
  global-install`, which shows everything it would change and asks once (`--yes` for an agent or a
  script, once you agreed). `latet global-uninstall` takes it all away. This machine's identity
  is detected each time Latet starts, so a new git email is masked with no step of yours.
- [Export and import](../concepts/export-import/): `latet rules export` and `import` (and the
  dashboard's *Export rules* and *Import rules*) give a colleague your rules in a YAML file; an
  import adds protection at once, and what would lower it only when asked for, and approved.
  `latet backup` and `latet restore` move your installation to another machine in one file
  encrypted with a passphrase, the vault and its key with `--vault`: the same fakes there.
- What lowers protection is caught in more places: a recognizer changed under the same name, one
  that lets what it finds through (`policy: allow`), a field rule kept in clear. Settings files with
  YAML aliases are refused, and config.yaml is written whole or not at all.
- [Your guidance](../concepts/llm-review/) for the local reviewer: a text of yours
  after Latet's own instructions, globally and for each project, in the dashboard or with
  `latet llm guidance`. The dashboard shows Latet's instructions read-only, exactly as the daemon
  sends them (`latet llm prompt` prints them), and *Try it* reads a text of yours. A repository
  file cannot set it, and a change an agent makes waits for your approval, shown before and after.
- Strict data mode leaves web search results and pages alone (`WebSearch`, `WebFetch`), as it
  leaves files: they are public content, and every title and address masked left the model nothing
  to read or cite. The other detectors still apply to them; `data_tools` can name them to cover them.
- A tool outside your machine never works on a stand-in. In Claude Code, a WebSearch, WebFetch or
  claude.ai connector call with a masked value in it is stopped before it runs (it would search for
  someone else, or write to an address that does not exist), and the model asks you; *Not sensitive*
  lets the value go out. For gateway agents, a tool the provider ran with a stand-in is flagged in
  the dashboard. A blocked request now says why in plain words, auto mode's check included: Claude
  Code refuses the action when Latet keeps the review local, it does not ask you.
- [Agents and your settings](../concepts/agents-and-settings/): `latet config get`, `set`, `add`
  and `remove`, and `llms-config.md` for agents. A change that lowers protection waits for your
  approval in the dashboard, whoever makes it: an agent's command, `config.yaml` edited by hand, or
  a file found changed at start (the daemon runs with what you approved until then). `latet global-uninstall`,
  `agents revert` and `service uninstall` refuse to run in an agent. Hermes Agent, Goose and OpenClaw
  are recognised as agents.
- **Approvals** shows one project's queue at a time.
- The local reviewer on another machine over https with a self-signed certificate, trusted by its
  fingerprint (`latet llm trust`, or *Show its certificate* in the dashboard): only that
  certificate is accepted. Over http on your local network, the dashboard, `latet doctor` and the
  log warn that the texts cross it unencrypted. The [security page](../security/) says why your
  agents reach the daemon over plain http on 127.0.0.1.
- `latet start` waits for a slow first start after an install, and says why one failed.
- The [Live Reviewer](../concepts/live-review/): every new text waits for your approval in the
  dashboard before it leaves. You approve it, mask more in it, or stop it; you can only add
  masking. In Claude Code it happens in the plugin's masking step, for the other agents in the
  daemon. `latet status` and `latet doctor` say whether a dashboard is open to approve.
- Smaller things.
  - *Always mask* and *Not sensitive* apply to an exchange's project or everywhere; in a project the
    project comes first.
  - **Live**'s notice of values found after sending links to the session's latest exchange.
  - A person's name of three letters (*Ugo*) found once is masked again wherever it appears, as
    written.
  - Your git author's company domain is masked wherever it appears, not only next to an address.
    It counts as part of your git author for Claude Code's auto mode reviewer.
  - `latet run claude`, `codex` and `opencode` are `latet claude`, `codex` and `opencode`. A
    missing agent is named, and the daemon is not started for it.
- The local reviewer on another machine of yours, once you confirm it (`remote: true`), over https
  outside your local network; its API key saved from the dashboard or with `latet llm key`, in
  the operating system's credential store.
- The dashboard: the sections of **Rules** in tabs, with a dot on a tab that has unsaved changes and
  a search across every tab; **Approvals** in two tabs, *Texts* and *Settings*, each with the number
  waiting, one text at a time in a window narrower than 1100 px, and keyboard shortcuts, off until
  you turn them on (Enter no longer approves); a text decided only once it has shown for 0.4
  seconds, so that a double click on *Approve* does not approve the next one unseen, and the
  keyboard focus on the next text after a decision; and Latet's own mark.
- A conversation that began before Latet was on, and so holds values in clear in its history, is
  named as such: Claude Code's message asks for a new conversation (`/clear`), and the dashboard
  flags its requests *From the history*.
- In Claude Code, what the screen shows of tool calls carries the real values everywhere: a folded
  group of calls while it runs, and what a call printed, besides the call itself.
- A CLAUDE.md that holds secrets or other people's data is named when a Claude Code session starts,
  and in `latet doctor`, by file, line and type: Claude Code's auto mode reviewer gets that file
  unmasked, so Latet stops its reviews.
- A connection string keeps all but its credentials: only `Password=`, `AccountKey=` and the like,
  or the password of its URL, are masked, and a placeholder there is left alone
  (`Password={{password}};`). The whole string was a secret before.
- The daemon runs in a folder of its own. Started from a project, it held that folder, and Windows
  would not let you rename or delete the folder while the daemon ran.
- The local reviewer in the dashboard and on the command line.
  - **Rules** → **Reviewers** → **Local reviewer**: its server and model, what it reads before a
    text leaves, how long a request may wait, what happens when it cannot answer, what it masks by
    itself, and *Try it* (the server reads a sample with invented values, and you see what it found
    and how fast).
  - A project's rules set how much it reads there.
  - An exchange lists what the reviewer said of each block: what it masked, and what it only
    suggested, with *Always mask* and *Not sensitive*. **Live** says when it found values in a text
    already sent.
  - *Draft from a description* writes a recognizer with the reviewer's model, added only when its
    examples pass.
  - `latet llm status` and `latet llm test [FILE]` (categories and positions, never values);
    `latet config path` prints where config.yaml is.
- The local reviewer (`llm_review`, off until you turn it on). A language model on your own
  OpenAI-compatible server (llama.cpp, Ollama, LM Studio), on this machine only, reads everything the
  model did not write before it leaves: your prompts and attachments, its instructions, and what its
  tools fetch. It sees Latet's replacements as placeholders, and masks the people, addresses,
  contacts, health and money details and secrets it finds; other categories are reported. It can add
  to what is masked, never take anything away.
  - A text reviewed once is not read again, also after a restart: its answer is kept sealed in the
    vault.
  - By default a request waits for its review as long as it takes, and is stopped when the reviewer
    cannot answer. `on_failure: send` sends it masked by the detectors alone instead, and
    `budget_ms` caps the wait. In Claude Code, a prompt it stops is not sent and a tool output it
    could not read is withheld.
  - `block: true` lets it stop a text that must not leave even masked.
  - Per project, `llm_review: off`, `background` or `inline` (a repository file can only raise it).
    `latet doctor` checks its server.
- The domain of an email address (not a mail provider's) is masked wherever it appears in the same
  text, as it already was in every later one, so a text is masked the same way from its first
  sending on. The docs explain why surrogates stay the same over time.
- Production data. Query results, API answers and logs an agent reads are read by their structure:
  psql, CSV, TSV, markdown and HTML tables, a browser's accessibility snapshot, `key=value` pairs
  and JSON value by value with its path.
  - Columns and keys named like a secret (`password_hash`, `api_key`, `reset_token`) are always
    masked, even with the name only in a table's header.
  - **Strict data mode** (`data: strict`, globally or in a project) masks every value of a tool's
    output, column and key names aside, with consistent fakes that keep dates and numbers valid; a
    query written with a fake runs with the real value. Such a value is masked in its structure
    only, and the tools that read files are left alone; `data_tools` limits it to some tools.
  - **Field rules** (`fields:`) mask a column or a JSON path as a type of your own, or keep it in
    clear in strict data mode. In the dashboard, a value strict data mode masked offers *Don't mask*
    its column *in this project*, with a hint for ids, dates, states and counts.
  - **The dump guard** (`dump_guard: N`) withholds a tool output with more than N personal values,
    and tells the model to ask for fewer rows; the exchange is marked *Withheld*.
  - `images: block` stops requests carrying images or PDFs, in a project too, without the max
    profile.

  Every request now says which tool each output comes from (the Claude Code plugin names it too).
- Projects in the dashboard. The **Rules** page has a selector at the top: everything, or one
  project. For a project it shows:
  - its protection (on, secrets only, off);
  - what applies there, and what its files say but cannot apply;
  - an editor for its `latet.yaml` (only what adds protection, with *Suggest recognizers* from
    the project's files);
  - one for your own settings for it.

  **Live** filters by project, and an exchange links to its project and says which of the project's
  settings a rule comes from.
- Settings for one project. A `latet.yaml` in a repository adds the project's own terms,
  recognizers, countries and stricter profile and policies, shared with everyone who works on it; it
  can never turn protection down, and what it says beyond that is ignored and reported. Your own
  settings for a project, in config.yaml under `projects:`, may also loosen it, or turn Latet off
  there (`latet project off`). A session belongs to the project of its folder: the Claude Code
  plugin names it, and Codex and OpenCode name it in their requests. New commands: `latet project
  init`, `show`, `check`, `off` and `on`; `latet rules add|list|remove` take `--repo` and
  `--project`; `latet config schema --project`. The name model is loaded once for every project.
- Every masked value in the dashboard shows the part of Latet that found it (secrets, personal
  data, ID numbers, your identity, your terms, your recognizers, the name model), the rule, and how
  it was found in that text: by a detector, as a value already in the vault, masked by the Claude
  Code plugin, or restored in the answer. The vault keeps the rule of each value's first sighting,
  so Claude Code requests show it too, where they said only *plugin* before. Each exchange counts
  its values by module.
- A password named only `pass` (`SMTP_PASS`, `adminPass`), or followed by a comment on the same
  line, is masked.
- A container image, `ghcr.io/latet-ai/latet` (amd64 and arm64), with the daemon, the dashboard
  and the runtime of the name model, and a `compose.yaml` that publishes it on `127.0.0.1` only,
  read-only and without privileges. `latet agents print` shows how to point each agent at it,
  `latet agents merge` writes an agent's settings file without printing the token, and
  `latet identity set` gives it your identity. The Claude Code plugin does not start a daemon of
  its own with `LATET_AUTOSTART=0`, and warns when the plugin and the daemon versions differ.
- `latet rules suggest <folder>` finds the code-like values no rule covers, on this machine, and
  prints their shapes (`AA-999999`), counts, files, likely check digits and labels, never a value;
  `--yaml` drafts recognizers with examples invented from the shapes.
- The dashboard's **Rules** page edits custom recognizers: a form for each, with *Try it* on a
  text of your own, the result of every example and the matches in ordinary code. Values found by a
  recognizer are named *Custom recognizer <name>*, with a link to it.
- Custom recognizers (`recognizers:` in config.yaml): your own formats, each a pattern with
  optional check digits and keywords, tested on every load against invented examples and against
  ordinary code (a rule that fires all over it is refused). A broken one is turned off and reported,
  and kept in the file as written. `latet rules test`, `add`, `list` and `remove`;
  `latet config schema` and `validate`, with the JSON Schema published for editors; a guide for
  AI agents, `llms-rules.md`.
- National ID and tax numbers of 14 countries: Belgium, Canada, Finland, France, Germany, Italy,
  the Netherlands, Poland, Portugal, Spain, Sweden, Switzerland, the United Kingdom and the United
  States. Each is confirmed by its check digits, and by a keyword nearby where the number alone
  could be anything; fakes get valid check digits. `id_regions` chooses the countries (`IT`, `US`,
  `GB` by default, so existing settings keep the codice fiscale); `latet global-install` suggests
  your own, and the
  `max` profile takes every country. The dashboard's **Rules** page groups the types by country.
- VAT numbers of 30 European countries (the EU, Northern Ireland, the United Kingdom, Switzerland
  and Norway), with their country prefix and check digits, in every profile that masks personal
  data. The Italian partita IVA keeps its own type.
- `auto_mode_review` chooses detail by detail what Claude Code's auto mode reviewer may receive:
  your user name, your user folder in paths, your host name, your git author. `block` and
  `allow-identity` still work, and stay as they are in `config.yaml`. With
  `user_name: surrogate`, Claude Code is given your user name's surrogate, so the reviewer sees the
  same fake name as the model. The dashboard's **Rules** page has a switch for each detail, and a
  stopped review says which details stopped it and the setting that lets them through.
- A short user name in the reviewer's *User identity* line is now caught, and your git author's
  email no longer stops a review that lets the git author through.
- The OS user name is read from the operating system, not from `USERNAME` or `USER`, which a
  process can change.
- Port 5283 with a fallback: when another program or Windows holds it, `latet start` says so at
  once, and `latet global-install` moves Latet to the first free port from 5284 to 6699 and points the
  agents at it (`--port` picks one). `latet doctor` checks the port on every system.
- The Claude Code plugin no longer holds a session back while it starts the daemon (it used to be
  able to outlast Claude Code's 10-second hook budget).
- Optional local detection of names and street addresses: `latet[ner]`, then
  `latet ner install` downloads a multilingual model (Italian included) that runs on your CPU.
  Pinned revision, checked hashes, and no network access besides that download.
- The `max` profile no longer sends requests that carry images or PDFs.
- `latet reload` applies `config.yaml` edits to the running daemon.
- Claude Code's auto mode reviewer is recognized. Its requests carry your OS user name and paths,
  which no plugin can mask: `auto_mode_review` stops them (the default) or lets only those details
  through, so auto mode keeps working. Secrets and personal data are never let through.
- Masking and checks run on their own threads, so a long block never stalls other agents' streams;
  the Claude Code plugin has a thread of its own.
- Requests stopped by the Claude Code plugin's final check appear in the dashboard too.
- A request blocked by the daemon keeps blocking when the conversation sends it again (it used to
  pass on the second try).
- One-line installers for macOS and Linux (`install.sh`) and Windows (`install.ps1`): they install
  uv if it is missing, then Latet with `uv tool install`, and change nothing else. CI runs them on
  all three systems.
- A release workflow: the wheel ships the built dashboard, and a GitHub release carries it, with
  build provenance attestations and a CycloneDX SBOM; PyPI gets it too, by trusted publishing.
- The CLI, configuration and detectors reference pages are generated from the code, and CI checks
  that they are up to date.

### Milestone M3: dashboard (done)

- `latet dashboard`: a local web page, opened with a one-time login link. It shows every exchange
  live, each request block by block (the original next to what was sent), sessions, the vault, the
  rules, a playground, the agent integrations and usage stats.
- Rules changed in the dashboard apply at once, without a restart.
- One-click actions on a masked value: allow it, or always mask it.
- The history is encrypted at rest and kept 14 days by default; `history: metadata` or `off` keeps
  less.

### Milestone M2: agents and operations (done)

- The Claude Code plugin: masking inside Claude Code before requests are signed, restoring for local
  tools and on screen, and a final check of every request.
- The local engine bridge the plugin talks to, protected by a token.
- `latet global-install`, `doctor`, `agents apply`, `agents revert`, `agents status`, `service install`,
  `service uninstall`, `off` and `upgrade`.
- Wrappers that leave the agents' configuration alone: `latet claude`, `latet codex`,
  `latet opencode` and `latet run`, plus `latet env`.
- Agent integrations that back up every file they touch and can put it back byte for byte.
- Start at login on Windows, macOS and Linux, without administrator rights.
- Tested live with Claude Code on a subscription and with the Codex CLI signed in with ChatGPT.

### Milestone M1: engine and gateway mode (done)

- Detection: the gitleaks ruleset on RE2; structural secret detectors (sensitive assignments,
  private keys, credentials in URLs, JWTs, bearer tokens); validated personal-data recognizers
  (email, phone, IBAN, card, codice fiscale, partita IVA, IP address, Bitcoin address); custom
  terms; this machine's identity; known values.
- Deterministic, format-preserving surrogates and an encrypted local vault.
- Gateway mode: masking requests and restoring responses for Anthropic Messages, Chat Completions
  and the Responses API, over HTTP, SSE and WebSocket, compressed bodies included. Masked history
  stays byte-stable across turns.
- Verify-only handling of Claude Code traffic: forwarded unchanged, or blocked when a sensitive
  value would leave in clear.
- Leak-canary end-to-end tests, property tests and a false-positive budget on public code, in CI.

### Milestone M0: spike (done)

- Client-mode spike in the Claude Code CLI: a plugin swaps a fixed canary value for a surrogate
  before the request is built and signed, and restores it in tool-call arguments and in the
  terminal.
- Local gateway skeleton: HTTP, streaming and WebSocket forwarding to Anthropic, OpenAI, ChatGPT
  (Codex) and OpenRouter, listening on `127.0.0.1:5283` only.
- Gateway hardening: `Host` allowlist against DNS rebinding, refusal of browser-originated
  requests, credential headers never logged, upstream TLS verified against the operating system's
  certificate store.
- Optional raw capture of exchanges to a local directory, for debugging.
