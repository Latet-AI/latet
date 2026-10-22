# Installing Latet with an AI agent

This file is for an AI agent (Claude Code, Codex, OpenCode, Hermes Agent...) that a user asked to
install Latet. Follow it in order. Every command below runs without prompts.

Latet is a local privacy firewall for AI agents: it masks secrets and personal data before a
request leaves the machine and restores them locally, so the agent keeps working normally.

## Rules

- **Install, explain, then wait.** Installing the package changes nothing on its own: no agent goes
  through Latet, nothing starts at login. Route no agent and change no tool's settings until the user
  has chosen how to go on (step 3). The choices are theirs: do not pick them yourself.
- **Never edit agent configuration by hand** (`~/.claude/settings.json`, `~/.codex/config.toml`,
  `opencode.json`). `latet global-install` and `latet agents apply` change them with backups,
  and `latet global-uninstall` restores them byte for byte. For the tools these commands do not set
  up, see [Other agents and tools](#other-agents-and-tools).
- **Never read, print or copy credentials**: `auth.json`, `.credentials.json`, API keys, the OS
  keyring, `bridge.token`, `root.key`.
- **Stop on errors.** If a command fails, show the user its output and ask how to go on. Don't retry
  with different options on your own.
- If you are Claude Code: `latet global-install` changes Claude Code's settings for **new**
  sessions. This session keeps working as it is; tell the user to restart Claude Code at the end.

A **Latet server** for a team, or connecting this computer to one, is not installed this way: read
[`llms-server.md`](llms-server.md) instead.

## 0. On the computer, or in Docker?

The usual install is on the computer (steps 1 to 6). If the user asks for Docker, or has Docker but
no wish to install Python tools, follow [Docker](#docker) at the end instead.

## 1. Install the package

Run Latet's installer. It installs uv, the Python tool manager, when it is missing; stops an
Latet that is running (Windows cannot replace files in use); and installs the latest release
from GitHub, with what local name detection and text in images run on. Their models are downloaded
later, only if the user said yes.

- Windows: `powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Latet-AI/latet/main/scripts/install.ps1 | iex"`
- macOS and Linux: `curl -LsSf https://raw.githubusercontent.com/Latet-AI/latet/main/scripts/install.sh | sh`

The installer is the supported way; the same release is also on PyPI as `latet`. Never install it
from the repository's address (`git+https://...`, which has no dashboard).

> **From a checkout** of the repository (to try changes that are not in a release yet), build the
> dashboard first (it needs Node.js 22.12 or newer), then install from the folder:
>
> ```sh
> cd dashboard && npm ci && npm run build && cd ..
> uv tool install --force ".[ner,ocr]"      # or ".[ner]", ".[ocr]", or "."
> ```

If `latet` is not found afterwards, run `uv tool update-shell` and open a new shell, or call it by
its full path. The folder is printed by `uv tool dir --bin`.

## 2. See what is installed

```sh
latet version
claude --version
codex --version
opencode --version
gemini --version
```

A command that is not found is an agent that is not installed. Note the others: step 3 names them.

## 3. Explain the options, and wait

Tell the user, in one message, that Latet is installed and changes nothing yet, and how they can go
on. Name the agents step 2 found.

- **See it at work first:** `latet tutorial`, in a terminal of their own (it opens a browser, and
  runs until they end it): about 30 minutes, with a pretend agent, a pretend model and invented data.
  Nothing leaves the computer. Do not run it yourself.
- **Try it, one session at a time:** `latet claude`, `latet codex`, `latet opencode` or
  `latet gemini` starts that agent through Latet for that session only, with none of its settings
  changed. See [Try it first](#try-it-first).
- **Every session:** `latet global-install` routes Claude Code, Codex and OpenCode for good, with
  backups, and can start Latet at login; `latet global-uninstall` takes it all away. If they choose
  it, ask the questions of step 4.
- **Other agents and tools** they use (Gemini CLI, GitHub Copilot, Zed, Cline, Continue, Goose,
  Hermes Agent, their own scripts and frameworks...): each goes through Latet in its own way, see
  [Other agents and tools](#other-agents-and-tools).
- **The local models**, recommended from day one: name detection finds names and street addresses
  (about 1.6 GB), text in images reads screenshots and masks the values in them (about 31 MB). They
  run on the CPU, are downloaded once from huggingface.co, and nothing leaves the computer with
  either. Ask whether to download them.

Then stop, and wait for the answer. Go on only with what they chose: [Try it first](#try-it-first),
step 4, or the setting of another tool.

## 4. Every session: global-install

Only when the user chose every session. Ask in one message, showing the defaults:

1. **Profile**: `balanced` (default: secrets and personal data), `max` (also private IP addresses,
   names everywhere, every value of the tables a tool prints, and images or PDFs are not sent), or
   `secrets` (secrets and their own terms only).
2. **Countries** whose national ID and tax numbers to recognise, among BE, CA, CH, DE, ES, FI, FR,
   GB, IT, LV, NL, PL, PT, SE, US. Default: the user's own country, US and GB (left out,
   `latet global-install` guesses the country from the system). VAT numbers of every European country are recognised
   anyway.
3. **Mask this machine's identity** (OS user names in paths, host name, git author)? Default: yes.
4. **Which agents** to route through Latet. Default: every installed one.
5. **Claude Code only: auto mode's reviewer.** It is sent the OS user name and the paths of the
   home and working directories, which no plugin can mask. `block` (default) keeps them local, but
   auto mode then cannot approve actions by itself. `allow-identity` lets only those details through
   to the reviewer, so auto mode keeps working. The user can also pick details one by one: the user
   name, the user folder in paths, the host name, the git author. Secrets and personal data are never
   let through.
6. **Local models**, if step 3 did not settle it: run on the CPU and downloaded once from huggingface.co: *name detection* finds
   names and street addresses in prompts (about 1.6 GB); *text in images* reads screenshots and
   masks the values in them (about 31 MB). Nothing leaves the computer with either. They are the
   recommended setup from day one. Default: yes to both.
7. **Start Latet at login?** Default: yes.
8. **A local reviewer**, optional: a language model on a server of the user's (llama.cpp, Ollama,
   LM Studio) that reads every text before it leaves and masks what the detectors miss. Ask only if
   the user runs such a server; then ask its address and model. Default: no.

Then run `latet global-install` with their answers, mapped onto options. Example: balanced,
Italian, US and UK numbers, identity masked, Claude Code and Codex, reviewer blocked, both local
models, start at login:

```sh
latet global-install --yes --profile balanced --id-regions IT,US,GB --identity --agents claude,codex --auto-mode-review block --ner --ocr --autostart
```

| Answer | Option |
| --- | --- |
| Profile | `--profile balanced`, `--profile max` or `--profile secrets` |
| Countries | `--id-regions IT,US,GB` (any of BE, CA, CH, DE, ES, FI, FR, GB, IT, LV, NL, PL, PT, SE, US) |
| Identity | `--identity` or `--no-identity` |
| Agents | `--agents claude,codex,opencode` (any subset), or `--agents none` |
| Auto mode's reviewer | `--auto-mode-review block`, `--auto-mode-review allow-identity`, or the details to let through: `--auto-mode-review user_name,paths` (also `host_name`, `git_author`) |
| Name detection | `--ner` or `--no-ner` |
| Text in images | `--ocr` or `--no-ocr` |
| Start at login | `--autostart` or `--no-autostart` |

It prints everything it changes. `--yes` applies it without asking, as the user agreed in step 4;
without it, and without a terminal, it changes nothing. Anything you leave out takes its default.

If an agent reaches its model through the user's company (its own gateway, Amazon Bedrock, Google
Vertex, Microsoft Foundry, Azure OpenAI), `global-install` reads it from the agent's settings and keeps
it: the summary says so ("your company's gateway llm.acme.com: Latet goes in front of it"). When a
line says "left as it is", tell the user the reason it gives: Latet could not go in front of that
agent, and changed nothing for it.

If the user wants name detection, or text in images, and it was not downloaded by
`latet global-install`:

```sh
latet ner install --yes
latet ocr install --yes
```

If the user wants the local reviewer, add to their config.yaml (`latet config path` prints where
it is), with their server's address and model, then run `latet reload`:

```yaml
llm_review:
  enabled: true
  url: http://127.0.0.1:8080/v1
  model: the-model-id
```

A server with an API key: ask the user to save it themselves, with `latet llm key` in their own
terminal or under *API key* in the dashboard's reviewer settings. Never read, write or pass the key
yourself. A server on another machine needs `remote: true` in `llm_review`, and the user's
confirmation that the server is theirs: the reviewer reads the original texts.

## 5. Verify

```sh
latet doctor
latet agents status
```

With the local reviewer, `latet llm status` checks its server with a sample text.

`latet doctor` exits with 0 when nothing failed. Every line is `OK`, `WARN` or `FAIL`, most with
the command that fixes it. Report any `FAIL` to the user with its line.

## 6. Tell the user

- **Restart the agents** that were routed (Claude Code, Codex, OpenCode): the change applies to new
  sessions.
- **Other agents and tools** they chose, set up as [Other agents and tools](#other-agents-and-tools)
  says: Gemini CLI started with `latet gemini`, a tool's own base URL, `latet run` for scripts.
- **New to Latet?** `latet tutorial` shows it at work in about 30 minutes, in the browser, with
  a pretend agent, a pretend model and invented data: nothing leaves the computer, and none of their
  settings change. They run it in a terminal of their own: it opens a browser, and runs until they
  end it. Do not run it yourself.
- `latet dashboard` opens the local dashboard. It shows what was sent to the model and how each
  value was masked.
- `latet global-uninstall` undoes everything (agent settings restored, start at login removed, daemon
  stopped); `uv tool uninstall latet` then removes the program.
- To hide formats of their own (badge numbers, contract codes), you can write recognizers with them:
  follow [`llms-rules.md`](llms-rules.md).
- To change Latet's settings later, you can do it for them: follow [`llms-config.md`](llms-config.md).
  What lowers protection waits for their approval in the dashboard.
- Coming from another machine with Latet: `latet backup FILE --vault` there and `latet
  restore FILE` here move their settings and vault, so the fakes stay the same. They run these
  themselves, in a terminal of their own: both refuse to run in an agent.
- Documentation: <https://docs.latet.ai/> (also in `docs/` in the repository).

## Other agents and tools

`latet global-install` sets up Claude Code, Codex and OpenCode only. The others:

- **Gemini CLI** is not available in global mode: the user starts it with `latet gemini` (or
  `latet run gemini`) each time, with a Gemini API key or Vertex AI chosen in Gemini CLI's `/auth`.
  Signed in with a Google account, its requests do not go through Latet, and `latet gemini` refuses to
  start it. Do not write `GOOGLE_GEMINI_BASE_URL` into a `.env` file or the user's profile: a
  project's own `.env` would hide it without a word.
- **Tools that take a base URL in their own settings**: GitHub Copilot in VS Code and JetBrains IDEs,
  Junie CLI, JetBrains AI Assistant, Zed, Cline, Kilo Code, Continue, Crush, Android Studio, Hermes
  Agent, OpenClaw, Goose, OpenHands, Aider. Give the user the setting for each tool they use, from
  <https://docs.latet.ai/agents/other-tools/> and <https://docs.latet.ai/agents/autonomous-agents/>.
  Change a tool's settings for them only if they ask, and back the file up first. Keep their API key
  where it is: Latet forwards it.
- **Their own scripts, SDKs and agent frameworks** (the OpenAI, Anthropic and Google GenAI SDKs,
  LangChain, LangGraph, AutoGen, CrewAI), **Qwen Code and the GitHub Copilot CLI** take Latet's address
  from the environment: `latet run <command>` for one command, `latet env` for a shell. See
  <https://docs.latet.ai/agents/openai-compatible/>. A tool such an agent calls itself (a web search
  API, a database) still gets the real values: tell the user.
- **Not covered**: Cursor, Windsurf, Antigravity, Jules, Kiro, Warp and the ChatGPT app send their
  requests from the vendor's servers, and Claude Desktop's Chat and Cowork signed in to Claude do not
  go through Latet. Tell the user plainly; do not try to route them.

## Docker

The image runs the daemon; the agents stay on the computer. The container cannot edit the agents'
files or see the user's identity, so `latet global-install` is not used: the steps below replace
it. Ask
the user step 4's questions 1, 2 and 5 first (profile, countries, auto mode's reviewer) and set them
afterwards in the dashboard, or skip them to keep the defaults.

1. Start it. Save `compose.yaml` from the repository (<https://github.com/Latet-AI/latet>), then:

   ```sh
   docker compose up -d
   docker exec latet latet status
   ```

   Never change the published port to anything but `127.0.0.1:...`: on Linux it would open
   Latet to the network.
2. Point the agents at it. `docker exec latet latet agents print` (add `--shell powershell` on
   Windows) prints the commands for each installed agent. Run the ones for the agents the user
   chose. For Claude Code that is `claude plugin marketplace add Latet-AI/latet`,
   `claude plugin install latet@latet`, then the `agents merge claude` command, which writes the
   bridge token into the settings file without printing it. Do not copy the token by hand, and do
   not print the settings file.
3. Tell it the user's identity: run the `identity set` command `agents print` shows, on the
   computer (it reads the user name, host name and git author there).
4. Check: `docker exec latet latet doctor`.
5. Tell the user to restart the agents, and that `docker exec latet latet dashboard
   --print-url` gives a link to the dashboard (it works once, for two minutes).

## Try it first

When the user chose to try it first, do not run `latet global-install`. After step 1:

1. Check the install with `latet version`. `latet doctor` works too: its line for each agent
   says `not applied`, which is expected while trying.
2. Download the local models the user said yes to (step 3): `latet ner install --yes` and
   `latet ocr install --yes`.
3. Tell the user, in one message:
   - **To see it at work first:** `latet tutorial`, in a terminal of their own (it opens a
     browser, and runs until they end it): about 30 minutes, with a pretend agent, a pretend model
     and invented data. Nothing leaves the computer. Do not run it yourself.
   - **To try it:** in a terminal, in a scratch folder, start the agent through Latet for that
     session only: `latet claude`, `latet codex`, `latet opencode` or `latet gemini`. Every other
     argument passes through (`latet claude --resume`). None of the agent's settings change.
   - **A harmless check:** put a made-up key in a `.env` file (`DEMO_API_KEY=demo_7Hq2LmX9pR4tV8nB3cK6`),
     ask the agent to copy it into `config.local.json`, then run `latet dashboard`. The request
     shows a fake key; the file on disk holds the real one.
   - **For every session,** when they are ready: `latet global-install`, or ask you again and
     follow step 4.
   - `uv tool uninstall latet` removes it; there is nothing to revert.
4. If you are Claude Code: this session does not go through Latet. The user starts
   `latet claude` in a new terminal to try it.
