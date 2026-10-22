---
title: Troubleshooting & FAQ
description: Common problems and frequently asked questions.
---

## Start here

Most problems show up in one command:

```sh
latet doctor
```

It checks the daemon, the plugin's connection to it, start at login, the port, a masking self-test,
and every agent it finds. Each `WARN` or `FAIL` line names the command that
fixes it.

When it runs in the background, the daemon writes its log to `logs/daemon.log` in the data folder:
`%LOCALAPPDATA%\latet` on Windows, `~/Library/Application Support/latet` on macOS,
`~/.local/share/latet` on Linux. To watch it live instead, stop the daemon and run
`latet serve`, which logs to the terminal.

## Troubleshooting

### My agent says it cannot connect

The agent is configured to use Latet, but the daemon is not running. Check it, and start it:

```sh
latet status
latet start
```

`latet status` prints `running: Latet 0.9.4 on 127.0.0.1:5283 (masking), pid 12345`, or
`not running`. If you want to stop using Latet instead, run `latet global-uninstall` first: it reverts your
agents. See [Uninstall](../getting-started/uninstall/).

### Claude Code says Latet withheld content

Messages like these mean the plugin did not get an answer from the daemon, or the daemon failed on
it, so it held the content back instead of sending it unmasked:

- `Latet could not mask this prompt: the local daemon did not answer, or failed on it.`
- `[withheld by Latet: the local daemon did not answer, or failed on it, so this content was not sent]`
- `Latet could not check this request (the local daemon did not answer, or failed on it), so it was not sent.`

Run `latet start`, then `latet doctor`. If the daemon is running, its log (`daemon.log` in the
`logs` folder of the data folder) says what failed: please open an issue with it. The plugin starts the daemon by itself with the
`latet` command; when you run Latet from a source checkout that command is not on your `PATH`,
so start it with `uv run latet start`.

### Latet refuses its data folder

The message names the folder, who else may change it, and what to do, as in:

```text
Integration records unavailable (unsafe folder namespace): C:\Users\you lets PC\other (S-1-5-21-…)
change what is in it (its access list grants full control, (F), mask 0x001f01ff; `icacls "C:\Users\you"`
lists it): remove that entry (`icacls "C:\Users\you" /remove:g *S-1-5-21-…`), trust that account
(LATET_TRUSTED_ACCOUNTS), or use a data folder whose every parent only you, SYSTEM, the Administrators
and the accounts in LATET_TRUSTED_ACCOUNTS can change (LATET_DATA_DIR); with a new data folder,
`latet agents apply` applies your agents again.
```

Latet keeps what it applied to your agents, and on a computer connected to a Latet server its
connection, in a data folder that no other account of the computer can change: another account could
plant there what Latet then applies or sends. The folder named is the data folder or one of its
parents.

- **The data folder itself** is refused whenever another account can change it, and so is a parent
  folder that belongs to another account.
- **A parent folder another account can change** is refused on a computer connected to a Latet server,
  where the server's token is at stake. On a computer that is not connected, Latet goes on and
  `latet doctor` warns, with the same words: an account that can change your profile can already
  change everything of yours in it, Latet's data included.

On Windows, `icacls "<folder>"` lists who may change the folder: an entry for another account with
`(F)`, `(M)` or `(W)` lets it. There are three ways out:

1. Remove that entry, where the computer is yours to change. An entry marked `(I)` comes from the
   folder above, and is removed there; an entry shown as a SID alone is an account Windows no longer
   finds, often one that was deleted.
2. Trust that account: set `LATET_TRUSTED_ACCOUNTS` to its name or its SID for your account, as in
   `setx LATET_TRUSTED_ACCOUNTS "PC\other"`, or in the system settings, or for the whole computer by
   group policy, then open a new terminal. Latet reads it there, in the registry, and never from a
   terminal's own environment: a value set in one terminal session alone (`$env:` or `set`) counts for
   nothing, so that a repository's settings or an agent's session cannot change it. Several accounts
   are separated by `;`, and a SID may be written `*S-1-5-…`, as icacls writes it. It applies to the
   parent folders only, never to the data folder itself, which holds the vault's key, nor to a folder
   in it.
3. Set `LATET_DATA_DIR` to a folder whose parents only you, SYSTEM, the Administrators and the accounts
   you trust can change, then run `latet agents apply` again.

A direct member of the local Administrators group, such as a helpdesk group your IT department added,
is trusted on the parent folders as the group is, with no setting. The trust in these accounts and
members reaches the parents of each folder Latet checks this way: the data folder's, those above the
data folder that Claude Code's helper checks, the parents of the helper's installation, and those of a
Latet server's Windows service log folder. `latet doctor` lists the accounts `LATET_TRUSTED_ACCOUNTS`
trusts, with where it is set, and the members it trusted. It names each entry that trusts nothing: a
name Windows does not find, or an account that is not one of this computer, a domain, Entra ID or a
service, such as Everyone, Users or NTLM Authentication. It also says when the variable is set in its
terminal alone.

On Linux and macOS, `ls -ld <folder>` shows its owner and its mode. A folder others may write into is
refused: `chmod go-w <folder>`, or `chmod 700` for the data folder itself. A data folder that others may
only read (0755, as `mkdir` makes it) is made yours alone (0700) the first time Latet needs it, and
Latet says so once: in the daemon's log, or in the terminal of the command that made the change.
`LATET_TRUSTED_ACCOUNTS` changes nothing there: the checks are by owner and mode.

A computer that never connected to a Latet server is never refused for a connection's sake.

### Claude Code on a connected computer withholds every prompt

The plugin says why, and each cause has its own fix:

- **Print mode or another surface:** `claude -p`, and every surface but the interactive terminal, is
  held back on a computer connected to a Latet server. Use the interactive terminal.
- **The helper binding is missing or out of date**, after an upgrade or a new connection: run
  `latet agents apply claude`, or start Claude Code with `latet claude`. `latet doctor` checks it.
- **The Latet server did not answer:** `latet status` says whether it answers and accepts this
  computer's token.
- **The server no longer accepts this computer's token:** it was revoked, or your identity disabled.
  Run `latet disconnect`, then `latet connect <url>` with a new code from an administrator.
- **Linux and macOS:** Claude Code through a server needs a Windows terminal in this version;
  `latet codex` and `latet opencode` go through the server.

`latet agents apply claude` and `latet claude` may refuse with `Claude Code's helper binding is
unavailable`. The message names the part of the installation that failed (Python, its environment
`sys.prefix` or its installation `sys.base_prefix`, Latet's package, or the Windows folder), the folder
and why: its owner, or an account its access list lets change it. The helper runs that code with this
computer's token, so it runs it only from folders that only you, SYSTEM and the Administrators can
change, and whose parents only they, the Administrators' direct members and the accounts in
`LATET_TRUSTED_ACCOUNTS` can change. Check the folder named with `icacls "<folder>"`, and remove the entry for the other account;
or install Python and Latet in folders of your own, which uv's `UV_PYTHON_INSTALL_DIR` and
`UV_TOOL_DIR` choose.

### A request was blocked

In Claude Code, the plugin's final check answers in the conversation:

```text
Latet stopped this request before it left your machine: PASSWORD (tool result) would have been
sent in clear. Nothing was sent.
```

The message names the kind of value and the part of the conversation it was in. A block like this
means some content reached the request without being masked. Run `latet doctor`, and check with
`latet agents status` that Claude Code shows `applied`; if it does not, run
`latet agents apply claude` and restart Claude Code.

When the daemon blocks a request instead, the agent shows an error with the message
`Latet: sensitive values would leave in clear`, followed by the type and length of each value,
such as `PASSWORD[13]`. Run `latet dashboard`: the blocked request is there, as it would have
left, with the values that stopped it boxed in red. To go on, take the value out: start a new
conversation (in Claude Code, `/clear`) and point the agent at the file or variable that holds it,
instead of pasting it. Only if a value is not a secret at all, such as a test or example value,
click it and choose **Not sensitive: always allow**, then try the turn again.

In gateway mode, an error with `Latet: masking failed` or `Latet: unreadable request body`
means the daemon could not mask the request, so it refused it.
The daemon's log says why.

### Auto mode says its reviewer is temporarily unavailable

In auto mode, Claude Code asks a reviewer model whether each action is safe. When Latet stops
that review, auto mode cannot approve the action: Claude Code refuses it, saying the reviewer is
temporarily unavailable, and does not ask you. Run the action yourself, switch Claude Code to a
permission mode that asks you, or let the details through as below. In the
dashboard these requests get a `400` answer and are flagged *Auto mode review*; the reason says what the
review carried. Two things stop it:

- **Your identity:** your OS user name and your user folder in paths, by default. To let them
  through to the reviewer, and only to it, set `auto_mode_review: {user_name: allow, paths: allow}`
  or use *Claude Code auto mode reviews* under **Rules** → **Advanced** → **Identity** in the
  dashboard. See [Claude Code](../agents/claude-code/#auto-modes-reviewer).
- **A secret or someone else's data,** which never goes to the reviewer. It is often in your
  CLAUDE.md: Claude Code gives the reviewer that file unmasked. Move those values to `.env` and name
  the variable in the file. When a session starts, the plugin names the file and the lines, and
  `latet doctor` lists them.

  **If they are placeholders,** write them in a [form Latet knows](../concepts/detection/#placeholders)
  and leaves alone: `postgres://app:<password>@host/db`, `${DB_PASSWORD}`, `YOUR_PASSWORD`,
  `changeme` or `***`. A placeholder of another form goes among the project's values never to
  mask (**Rules**, pick the project, *Allowed values*), which `latet doctor`, run in the
  project's folder, follows too.

Keep the permission prompts rather than turning to bypass permissions: in the commands it runs, the
agent works with the real values.

### Every request of a conversation is blocked

In Claude Code, a message like this one stops every request of a conversation:

```text
Latet stopped this request before it left your machine: this conversation already held values in
clear before Latet checked it (PASSWORD (user text), EMAIL (tool result)). It began before Latet
was on, or was resumed from then, and every request sends that history again. Start a new
conversation with /clear: from there everything is masked as it comes in. Nothing was sent.
```

The conversation began before Latet was on, or you resumed one from then (`claude --resume`): its
first messages, your CLAUDE.md and the output of its tools went into the history in clear. Claude
Code sends the whole history with every request, so each one carries those values again, and
Latet cannot know they had left already. In the dashboard these requests are flagged *From the
history*.

Start a new conversation with `/clear`, or a new session, with Latet on. From then on everything
is masked as it comes in.

### The local reviewer stopped a request

With the [local reviewer](../concepts/llm-review/) on, a request waits for its review, and is not
sent when the reviewer cannot give one. The agent, or Claude Code, says why:

- `the local reviewer could not read the user text (its server did not answer: start it, or run
  latet doctor), so it was not sent`: start the model server, then check it with
  `latet llm status`. A server that answers `HTTP 401` wants an API key: save it under *API key*
  in the dashboard, or with `latet llm key`;
- `... (it took longer than llm_review.budget_ms)`: raise `budget_ms`, or remove it to wait as long
  as the review takes;
- `the local reviewer could not trust its review of the tool result: the text gives it
  instructions`: the text speaks to the reviewer, and it found nothing in it. Look at it in the
  dashboard before sending it again;
- `the local reviewer stopped this tool result: it must not leave even masked`: with `block: true`,
  the reviewer judged the whole text too sensitive to send, such as a medical record.

In Claude Code, a stopped prompt is not sent, and a stopped tool output reaches the model as
`[withheld by Latet: ...]`. To send what the detectors masked when the reviewer cannot answer,
set `on_failure: send`.

### A tool output was withheld

`[withheld by Latet: this tool output holds 214 personal values ...]` in the conversation, and the
*Withheld* flag in the dashboard, come from the
[dump guard](../concepts/production-data/#the-dump-guard): the output held more values about people
than `dump_guard` allows in one output, so none of it was sent. Ask the agent for fewer rows, or for
counts and aggregates; raise `dump_guard`, or set it to `0` to turn the guard off.

### My latet.yaml has no effect

- `latet project show` prints what applies in the project, and each key of the file that is
  ignored, with the reason: a project file can only add protection, so values never to mask,
  turned-off rules and Latet off are ignored there. They belong in your own settings for the
  project.
- `latet project check` checks the file, and exits with 1 when something is ignored or an example
  fails.
- Only the nearest `latet.yaml` applies, from the agent's folder up to the repository's root. See
  [Projects](../concepts/projects/).

### Nothing is masked in one project

Latet may be off there: the dashboard marks those exchanges *Latet off*. `latet project show`
says so, and `latet project on` turns it on again. A project set to *Secrets only* masks secrets
and your terms, and lets personal data through.

### A value is masked that should not be

That is a false positive. In the dashboard, open the request, click the value and choose
**Not sensitive: always allow**, or add the value to `allow_values` in `config.yaml`. To turn off a
whole secret rule, add its id to `disabled_secret_rules`: for a value the dashboard shows as found
by `gitleaks:generic-api-key`, the id is `generic-api-key`. See
[Allow lists](../concepts/profiles/#allow-lists).

### A value is not masked

- Check the [profile](../concepts/profiles/): `secrets` does not mask personal data.
- Names in free text need the optional
  [local name model](../getting-started/installation/#name-detection) or the
  [local reviewer](../concepts/llm-review/), and both are best effort: add the names that matter as
  [custom terms](../concepts/detection/#your-own-terms).
- Paste the text into the dashboard's **Playground** to see what the current rules find.

### The answer from `claude -p` shows fake values

Expected: in print mode Claude Code does not restore what it prints. Files written by tools contain
the real values, and the interactive interface shows them too. See
[Claude Code](../agents/claude-code/#limits). On a computer connected to a Latet server, print mode is held
back: see [Claude Code on a connected computer withholds every prompt](#claude-code-on-a-connected-computer-withholds-every-prompt).

### Commands are slow on Windows

Every `latet` command takes seconds to start, `latet version` too, while your agents work as
fast as usual. That is your antivirus. It checks the programs it does not know each time they run,
and the small launcher an installer makes for the `latet` command (uv's and pip's alike) is one
of them: unsigned, and different on every machine. With Windows Defender it costs about half a
second; some antiviruses, Avira for one, take several seconds for each launcher. Latet itself
starts in a fraction of a second, and your agents talk to the daemon that is already running, so
only the start of a command waits.

Two remedies:

- **Exclude Latet in your antivirus.** With an install made with uv, the folders are
  `%APPDATA%\uv\tools\latet` and `%USERPROFILE%\.local\bin` (in the second, `latet.exe` and
  `latet-daemon.exe` are enough).
- **Or run it through Python itself,** which is signed and starts at once: install the wheel of the
  [latest release](https://github.com/Latet-AI/latet/releases/latest) with
  `py -m pip install "latet[ner,ocr] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"`
  (its version in place of `X.Y.Z`) and run every command as `py -m latet`, such as
  `py -m latet claude`. The `latet.exe` that pip makes is as slow as the others, so leave it
  aside. Latet then shares that Python with your other programs instead of having its own. To
  upgrade it, run the same `py -m pip install` with the new release's wheel: `latet upgrade`
  installs with uv, as a second copy. To remove it, run `py -m latet global-uninstall`, then
  `py -m pip uninstall latet`.

With a slow antivirus, the Claude Code plugin may not manage to start the daemon by itself in time,
and holds content back meanwhile. Let the daemon start at login (`latet global-install` sets that up), or run
`latet start` before you open Claude Code.

### Port 5283 is already in use

Latet listens on `127.0.0.1:5283`, a port no common software uses. If another program took it,
or Windows reserved it (Hyper-V, WSL and Docker reserve blocks of ports that can change at every
boot), `latet start` says so at once:

```text
Latet cannot listen on 127.0.0.1:5283: in use by another program.
Run `latet global-install` to move it to a free port (your agents are updated too).
```

`latet global-install` picks the first free port from 5284 to 5335, saves it in `config.yaml` and points
every agent it set up at the new port. If none of them is free, choose one yourself:
`latet global-install --port 6655`.
`latet serve --port <port>` changes the port of a single foreground run only.

### Latet runs in Docker and the agents cannot reach it

Run `docker exec latet latet status`, then check that the port is published on `127.0.0.1` and
is the same inside and out (`LATET_PORT` sets both in `compose.yaml`). After a port change, run
the `agents merge` commands of `docker exec latet latet agents print` again. Claude Code with
`LATET_AUTOSTART=0` never starts a daemon of its own: it waits for the container. Behind a proxy,
see [Docker](../getting-started/docker/#behind-a-proxy).

### "browser requests are not accepted on proxy routes"

You opened a proxy URL in a web browser, or a web page tried to reach it. This is intended: the
daemon refuses requests that come from browsers. Agents are not affected. To look at the traffic,
use `latet dashboard`.

### "host not allowed"

The request used a host name other than `127.0.0.1`, `localhost` or `[::1]`. Use one of those in
the base URL.

### Codex does not go through Latet

- Codex older than 0.115 ignores `openai_base_url`: update it. `latet doctor` warns about this, and
  `latet agents apply codex` leaves such a Codex as it is. Until then, start it with `latet codex`,
  which hands it the address in `OPENAI_BASE_URL`, the only one it reads. With a provider of its own
  (Azure, your company's gateway), `latet codex` does not start it, and says to update Codex.
- Run `latet agents status`: Codex should show `applied`, and its route.
- If you switched between ChatGPT sign-in and an API key, or added a provider, run
  `latet agents apply codex` again.

### Claude Code says its requests go around Latet

The plugin found Claude Code set to send its requests elsewhere, and stopped its prompts: nothing would
check what leaves. The message names the file that set it.

- **This project's `.claude/settings.json`** (or `.claude/settings.local.json`) sets
  `ANTHROPIC_BASE_URL`, a cloud's address or switch, or a proxy (`HTTPS_PROXY`): a repository never
  decides where requests go, even through Latet. Move the line to your own settings and run
  `latet agents apply claude`, or start Claude Code with `latet claude`, whose settings beat the
  project's address. A proxy set by the project stops prompts until it is removed from there.
- **More than one cloud is on** (`CLAUDE_CODE_USE_BEDROCK` and `CLAUDE_CODE_USE_VERTEX`, say): keep one.
- **No address at all**, or a cloud reached directly (`CLAUDE_CODE_USE_BEDROCK` without its address):
  start it with `latet claude`, or run `latet agents apply claude` once for all sessions.
- **Your organization's managed settings** force an address: see
  [Company endpoints](../agents/company-endpoints/#where-latet-cannot-go-in-front).

### Bedrock says the AWS login has expired

Latet signs Bedrock's requests with your AWS profile, and the login behind it has ended. Run the
command in the message (`aws sso login --profile work`), then send again: nothing restarts. Without the
AWS CLI, Latet reads only keys written in `~/.aws`; an SSO login needs the AWS CLI, and
`latet doctor` says so. Latet looks for `aws` on its own PATH, then in `/usr/local/bin`,
`/opt/homebrew/bin`, `/usr/bin`, `~/.local/bin` and `/snap/bin` (in `C:\Program Files\Amazon\AWSCLIV2` on
Windows). If yours is somewhere else, start Latet from a shell where `aws` runs.

### A company endpoint cannot be reached

The request reached Latet, and the destination it forwards to did not answer. Check the address under
Protected agents → Where requests go in the dashboard, or with `latet config get upstreams`; Claude Code
shows the destination's host in its error.

### Claude Desktop does not go through the daemon

Expected: the desktop app ignores `ANTHROPIC_BASE_URL`. Local sessions in its Code tab get the
plugin and its final check, but not the daemon's check, and they are not recorded in the dashboard.
See [Claude Code](../agents/claude-code/).

### Remote Control is gone in Claude Code

Expected while Latet is in the path: Claude Code turns Remote Control off when its base URL is
custom. `latet agents revert claude` brings it back, and takes Claude Code out of Latet.

### The dashboard says its files are not part of this install

You are running Latet from a source checkout. Build the dashboard: `npm ci`, then
`npm run build`, in `dashboard/`. Releases include it. The tutorial's page needs it too: until
then, `latet tutorial` warns that its page will not show.

### The tutorial does not open, or asks to be started again

- **No browser opened:** run `latet tutorial --print-url`, and open the link it prints in the
  browser you choose. The link works once, for two minutes.
- **The page asks to start the tutorial again:** its link was used already, or more than two
  minutes went by. Press Ctrl+C where the tutorial runs, and start it again: the chapters you
  finished stay done.
- **It stopped on an error:** it printed the last lines of its log, and keeps the whole log in the
  sandbox's folder until you run it again. Please open an issue with it.

### An upgrade fails on Windows because files are in use

Use `latet upgrade`: it stops the daemon, installs the new release in a window of its own once the
command has ended (Windows replaces no file of a program that runs), and starts Latet again.

A Latet 0.9.0 or 0.9.1 upgraded with its own `latet upgrade` on Windows can be left unable to start,
`latet` then answering `No module named 'latet'`: run the installer again, which installs the latest
release whole.

### The latest release cannot be looked up

The installers and `latet upgrade` ask GitHub's API which release is the latest, and install its
wheel. When they cannot, they say why and change nothing:

- **GitHub's API answered 403** (or 429): it takes 60 requests an hour from one address, and an
  office behind one address can use them up. Both then read the page of the latest release, which
  is not counted; if that fails too, wait an hour, or install by hand (below).
- **GitHub could not be reached:** there is no connection, or a proxy is in the way.
- **The latest release has no wheel,** or there is none: install
  [from source](../getting-started/installation/#run-from-source).

To install a release by hand, take its wheel's address from the
[releases](https://github.com/Latet-AI/latet/releases) page, and give it to the installer in
`LATET_SOURCE` (it upgrades a Latet that is installed, too; then run `latet start`, and
`latet agents apply claude` if Claude Code goes through Latet for good):

```sh
curl -LsSf https://raw.githubusercontent.com/Latet-AI/latet/main/scripts/install.sh | LATET_SOURCE="latet[ner,ocr] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl" sh
```

On Windows (PowerShell):

```powershell
$env:LATET_SOURCE = "latet[ner,ocr] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
irm https://raw.githubusercontent.com/Latet-AI/latet/main/scripts/install.ps1 | iex
```

A wheel you downloaded works as well: its path in place of the address.

## FAQ

### Does it work with a Claude Pro or Max subscription?

Yes, with no API key: that is what client mode is for. Claude Code runs unmodified, you sign in with
its own login, the plugin masks inside it, and the daemon forwards each request byte for byte. See
[Claude Code](../agents/claude-code/#pro-and-max-subscriptions).

### Can Hermes Agent or OpenClaw use my Claude subscription through Latet?

No. Anthropic allows a subscription in Claude Code and its own apps, and Latet does not lend it to
anything else. Autonomous agents go through the gateway with an API key: an OpenAI or OpenRouter key
(OpenRouter serves Claude models too), or an Anthropic API key. See
[Hermes Agent, OpenClaw and other agents](../agents/autonomous-agents/). Hermes Agent signed in with
ChatGPT is another matter: it goes through Latet with that sign-in, see
[Signed in with ChatGPT](../agents/autonomous-agents/#signed-in-with-chatgpt).

### Why doesn't Latet just rewrite Claude Code's traffic?

Claude Code adds an attribution block with a fingerprint of the conversation to each request, and
Anthropic's gateway guide asks a gateway to inspect requests without modifying them. So the masking
happens inside Claude Code, before the request is built, and the daemon only checks it. See
[How it works](../concepts/how-it-works/#client-mode-claude-code).

### Can the model still do its job with fake values?

Usually, yes. Surrogates keep the shape of the original, so code that handles a key, an email or an
IBAN still makes sense to the model. It just never learns the real value. Only the surrogate itself
is put back, though: if the model writes *Dear Laura* because the fake address starts with
`laura.`, that greeting stays.

### Does masking break the provider's prompt cache?

No. The same value always gets the same surrogate, and earlier turns are sent again exactly as
before, so the provider can still read them from its cache. The dashboard's **Stats** page shows
how much of each prompt came from the cache.

### Where can I see what was actually sent?

Run `latet dashboard`. See the [dashboard guide](../dashboard/).

### How do I turn it off?

`latet global-uninstall` reverts every agent, removes the start at login and stops the daemon. Tools you
pointed at Latet in their own settings keep its address: put theirs back. Running
`latet global-install` again turns it back on.

### Does Latet send anything anywhere?

Only your agent's own traffic, to the provider (or the company endpoint) your agent already uses. The
exceptions are all when you ask: `latet ner install` and `latet ocr install` download their models
from huggingface.co once; the installers and `latet upgrade` ask GitHub which release is the latest,
and download its wheel from there; and the local reviewer is asked on your own server. There is no
Latet cloud service, no telemetry and no analytics.

### Is my data safe once Latet is installed?

Safer, not safe. Latet masks only what its detectors recognize, and some surfaces and file types
are out of its reach. Read [What is and isn't protected](../concepts/protection-scope/).

### What about images and PDFs?

Download the text recognition models with `latet ocr install` (a Latet installed without the
`ocr` extra they run on gets it with `latet upgrade`): the text in screenshots and images is then
read on your machine and masked, and each value is covered on the image with its fake. See
[Screenshots and images](../concepts/images/).

PDFs and other documents are not read: they are sent as they are, and the dashboard marks those
requests as *Not inspected*. Avoid attaching files that contain sensitive data, or set
`images: block` (the `max` profile does the same) so that a request carrying one is not sent.

### An image was withheld

The model got *[image withheld by Latet: ...]* in place of an image, with the reason:
- *the text recognition models were still loading*: the daemon had just started. Send it again.
- *it could not be read*: an animated image, one larger than 20 MB or 40 megapixels, or not a valid
  image.
- *its text could not be read (OcrTooManyTiles)*: the image is so long for its width, or so wide for
  its height, that reading it would take more than 16 parts, such as the screenshot of a page more
  than about 29,000 pixels long. Take the page in several screenshots, or crop it to what the agent
  needs.
- *its text could not be read (OcrTooWide)*: a line of text in it is too long for its height to be
  read, such as one line across several screens. Crop the image to what the agent needs.
- *its text could not be read (OcrTooLong)*: reading it took longer than a read may take. Send it
  again, or a smaller image.
- *you withheld it in the Live Reviewer*: you ticked *Don't send this image* for it.
- *the local reviewer saw a value in it ...*: the [local reviewer](../concepts/llm-review/#images),
  shown the image, saw a value Latet could not find in the text read, so it could not cover it.
  Mask it in the Live Reviewer by hand, or crop it out of the image.

### Can I run Latet in a sandbox with my agent?

Yes. Latet is not a sandbox itself, and it works inside one: install it where the agent runs, so
the agent reaches it at `127.0.0.1`.

### Can my company use Latet for free?

Yes, whatever its size, for its own work, on any number of computers and servers. Personal and
non-commercial use are free too. For the Community Edition, a commercial license is needed only to
sell Latet, to offer it to others as a service, to build it into a product you sell, or for a commercial fork: write to
[info@latet.ai](mailto:info@latet.ai). The Enterprise Edition, the files in `ee` directories, adds
what a company needs to manage Latet centrally, and needs a commercial license in production;
developing and testing with it is free. The Community Edition never checks a license: there are no
keys. [LICENSING.md](https://github.com/Latet-AI/latet/blob/main/LICENSING.md) has the details and
examples.

### Where does the name come from?

*Latet* is Latin for "it lies hidden", as in *latet anguis in herba*: "a snake lies hidden in the
grass". Latet hides your secrets in plain sight: the model sees a value just like the real one.
