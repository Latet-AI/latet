---
title: Claude Code
description: Client mode for Claude Code, with Pro and Max subscriptions or API keys, and which Claude surfaces are covered.
---

Claude Code uses **client mode**: the Latet plugin masks content inside Claude Code, and the local
daemon runs verify-only. This works with Pro and Max subscriptions without an API key, and with API
keys as well.

## Connected Windows terminal

The connected-server helper is supported for Claude Code in a Windows terminal, with
trusted user settings and the private helper binding installed by Latet. This does not
extend connected support to Desktop, VS Code, JetBrains, Linux or macOS. On Linux and macOS,
`latet connect`, `latet claude`, `latet agents apply claude` and `latet global-install` say so
up front, no token is written to Claude Code's settings, and the plugin holds back every
prompt; `latet codex` and `latet opencode` go through the server. Follow
[the server connection guide](../../getting-started/server/) for `latet connect`,
`latet agents apply claude`, token storage and `latet disconnect`.

On a connected computer, `LATET_BRIDGE_TOKEN` in Claude Code's settings and environment holds this
computer's token, and every command Claude Code runs can read it. From anywhere the server is reached,
the token opens the plugin's bridge and your space's proxy: whoever holds it can have texts masked in
your space, have the fakes they hold restored, and send requests through it. It never opens your
dashboard, where every original can be revealed, never changes your identity and never lowers your
protection: the only change it can make to your settings is adding a destination for an address, which
lowers nothing. For the dashboard and the identity the server also asks for this computer's key, which
stays in the credential store (or the private token file) and is never given to an agent. The key stops a
token that leaked (from the environment, the settings, a log or one `curl`) from opening the dashboard; it
does not stop a command written to read both from the credential store. `latet dashboard` and
`latet identity send` refuse to run inside an agent on a connected computer. If the token may have
leaked, `latet disconnect` revokes it.

On a connected computer, print mode (`claude -p`) is not supported: the plugin withholds
its requests. Use the interactive terminal. When the plugin withholds a request it says why:
print mode or another surface; a helper binding that is missing or out of date
(`latet agents apply claude`); a server that did not answer (`latet status`); or a token the
server no longer accepts (`latet disconnect`, then `latet connect` with a new code from an
administrator). The plugin masks the descriptions of Claude Code's tools; on a server the final
check also reads their descriptions, parameter schemas and examples, and a value left in clear in
them stops the request ([protection scope](../../concepts/protection-scope/)). The coverage tables
and daemon instructions below describe **local Latet**; they do not widen this connected-helper
support.

## Coverage

| Surface | Plugin | Verify-only daemon | Notes |
| --- | --- | --- | --- |
| Claude Code CLI (terminal) | ✅ | ✅ | Full client mode |
| VS Code and JetBrains extensions | ✅ | ✅ | Same Claude Code, same plugins and settings |
| Claude Desktop's Code tab (local sessions), signed in to Claude | ✅ | ❌ | Plugin only: the app ignores `ANTHROPIC_BASE_URL`, so no daemon checks behind the plugin and nothing is recorded in the dashboard |
| Claude Desktop's Chat and Cowork, signed in to Claude | ❌ | ❌ | Not covered: they do not load Claude Code's plugins and do not pass through Latet |
| Claude Desktop on a third-party gateway: Chat, Cowork and Code | Code tab | ✅ | Every request passes through Latet and is checked; only the Code tab masks |
| Cloud sessions | ❌ | ❌ | Not covered: they run on Anthropic's infrastructure, not on your machine |

**Claude Desktop, two setups.** Signed in to Claude, with a Pro or Max subscription, the Code tab
loads Latet's plugin, which masks content and makes its own final check before each request. The
app ignores `ANTHROPIC_BASE_URL`, though, so no daemon checks the request behind the plugin, and
nothing is recorded in the dashboard. Chat and Cowork do not load Claude Code's plugins and do not
pass through Latet: they are not covered.

On a third-party gateway, Claude Desktop is not signed in to Claude, so there is no Pro or Max
subscription: requests are paid per use, with an Anthropic API key or through your company's
provider. With the gateway set to Latet, Chat, Cowork and Code send every request through it.
Claude Code's engine builds these requests, so Latet checks each one and stops one that would carry
a secret or personal data in clear, but does not mask it: Anthropic's
[gateway guide](https://code.claude.com/docs/en/llm-gateway-protocol) asks a gateway to inspect
Claude Code's requests, not to modify them. The Code tab gets both the plugin's masking and the
daemon's check. To set it up, see
[Claude Desktop on a gateway](../other-tools/#claude-desktop-on-a-gateway).

At work, Claude Code often goes through your company's gateway, Amazon Bedrock, Google Vertex or
Microsoft Foundry: Latet reads that route from your settings and goes in front of it, keys and all.
See [Company endpoints](../company-endpoints/).

Tested live: the CLI on a subscription. The IDE extensions and the Claude Desktop Code tab use the
same plugin and settings; if something does not work there, please
[open an issue](https://github.com/Latet-AI/latet/issues). Cursor is not covered: its model
requests are sent from Cursor's servers, not from your machine.

## Pro and Max subscriptions

Client mode works with Claude Code signed in with a Pro or Max subscription, with no API key, and
with an API key as well. Anthropic's [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)
page sets how a subscription may be used. Point by point:

| Anthropic's terms | What Latet does |
| --- | --- |
| Signing in with a subscription is meant for ordinary use of Claude Code and Anthropic's own apps. | Your subscription is used in Claude Code only. Codex, OpenCode, Hermes Agent and every other agent go through the gateway with their own sign-in: an API key, or Codex's ChatGPT sign-in. |
| The Claude Code binary must not be modified. | Claude Code runs as Anthropic publishes it. Latet adds a plugin, through Claude Code's own plugin commands, and a few environment settings in `settings.json`. |
| Sign-in must complete through Anthropic's own flow; developers may not collect, store or intermediate Claude.ai credentials. | You sign in with Claude Code's own login. Latet never asks for your credentials, never reads them for itself and never stores them. |
| The rules do not prevent you from signing in to the unmodified Claude Code binary with your own subscription. | That is the way Latet is used: your Claude Code, your login. |

**The way a request goes.** With `ANTHROPIC_BASE_URL` pointing at it, Claude Code sends its
requests through the local daemon, as it does through any gateway: Anthropic documents a
[gateway](https://code.claude.com/docs/en/llm-gateway) with a subscription login, and its
[gateway guide](https://code.claude.com/docs/en/llm-gateway-protocol) asks a gateway to forward
requests unchanged and to inspect them without modifying them. That is what the daemon does:
each request reaches Anthropic byte for byte, headers included, with your token in them as Claude
Code put it there. The token is never saved, logged or shown in the dashboard.

**Why nothing needs rewriting.** Claude Code adds to each request's system prompt an attribution
block with a fingerprint derived from the conversation. A proxy that rewrote requests would change
what Claude Code sent. The plugin masks inside Claude Code instead, before the request is built, so
the daemon only checks it: it can let a request through or block it, never change it. See
[How it works](../../concepts/how-it-works/) for a step-by-step example.

This describes how Latet works; it is not legal advice, and Anthropic's terms are the reference.

## What the plugin does

The plugin asks the local daemon to detect and mask; nothing is detected inside Claude Code itself.

- **Masks content where it is born**, before it joins the conversation: the prompts you type, the
  system prompt, `CLAUDE.md` and other context Claude Code adds, attachments, skill prompts, tool
  descriptions, and every tool result (file contents, command output, hook output).
- **Restores originals in tool-call arguments** before a tool runs, so Read, Edit, Write, Bash and
  your local MCP servers work on the real values and files on disk are correct.
- **Keeps the fakes where arguments leave your machine**: WebFetch, WebSearch and claude.ai
  connectors receive the surrogates the model wrote. Subagents get them too, and work on them as
  the model does.
- **Stops a tool outside your machine that would work on a fake**: a WebSearch, WebFetch or
  claude.ai connector call with a stand-in in it would search for someone else, open the wrong page
  or write to an address that does not exist. It does not run; the model is told why, and asks you
  whether the value may go out in clear. The dashboard shows the call, with *Not sensitive* for each
  value.
- **Restores what you see**: your messages, the model's answers, the commands and tool calls shown
  on screen (a folded group of them too, while it runs) and what they printed carry the real values.
  What the model reads does not change.
- **Checks every request before it is sent.** If a sensitive value is about to leave in clear, the
  request is not sent and you see a message instead:

  ```text
  Latet stopped this request before it left your machine: PASSWORD (tool result) would have been
  sent in clear. Nothing was sent.
  ```

  The dashboard shows the request, with the value that stopped it boxed in red: if it is not
  sensitive, click it and choose *Not sensitive*, then try the turn again.

  When the values in clear are in a conversation that began before Latet was on, the message
  says so, and asks you to start a new conversation with `/clear` (see
  [Troubleshooting](../../troubleshooting/#every-request-of-a-conversation-is-blocked)).

If the daemon is not running, the plugin starts it. If that fails, the plugin holds content back
rather than sending it unmasked: your prompt is not sent, tool results are replaced by a notice,
and tool calls are refused, until the daemon is back. `latet doctor` tells you what is wrong.

## What the daemon does

With `ANTHROPIC_BASE_URL` pointing at it, every request passes through the daemon, which:

- checks the outgoing request for sensitive values in clear, and blocks it if it finds one: Claude
  Code then shows an API error with the message `Latet: sensitive values would leave in clear`;
- otherwise forwards the bytes unchanged, headers included;
- records the exchange for the [dashboard](../../dashboard/), without its credentials.

It never rewrites a request from Claude Code, or one that carries a Claude subscription token,
whoever sends it.

## Every session, or one

There are two ways to run Claude Code through Latet:

| | `latet global-install`, `latet agents apply claude` | `latet claude` |
| --- | --- | --- |
| What it covers | Every Claude Code session: the terminal, VS Code, JetBrains, and the Desktop Code tab (plugin only) | That one session, in the terminal |
| What it changes | Installs the plugin and adds the settings below to `settings.json`, backed up first | Nothing: the plugin and the settings are given to that session only |
| Undo | `latet agents revert claude`, or `latet global-uninstall` | End the session |
| `latet doctor` | Checks the plugin and the settings | Reports Claude Code as not applied, which is expected |

`latet agents apply claude` routes Claude Code alone: run it when you install Claude Code after
`latet global-install`, or after changing the port. `latet claude` starts the daemon if needed and
passes every argument to `claude`:

```sh
latet claude
latet claude --continue
```

`claude` started on its own, with no `latet global-install`, does not go through Latet; in a shell where
`latet env` set `ANTHROPIC_BASE_URL`, it reaches Latet without the plugin, and its requests are only
checked. `latet run claude` is the
same as `latet claude`: Claude Code needs the plugin as well as the address, and `latet run`
alone would give it only the address.

## What global-install changes

`latet global-install`, or `latet agents apply claude`, does two things:

1. Installs the plugin through Claude Code's own plugin commands, from a local marketplace in the
   Latet data folder.
2. Adds an `env` block to `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`),
   after backing the file up:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:5283/anthropic",
    "LATET_BRIDGE_URL": "http://127.0.0.1:5283",
    "LATET_BRIDGE_TOKEN": "<a random token, created when first needed>",
    "ENABLE_TOOL_SEARCH": "true",
    "CLAUDE_CODE_AUTO_MODE_SERVER": "0"
  }
}
```

- `ANTHROPIC_BASE_URL` sends every request through the daemon. With your company's gateway, Amazon
  Bedrock, Google Vertex or Microsoft Foundry, Latet writes that route's variable in its place (see
  [Company endpoints](../company-endpoints/)); with an address on this computer, it leaves yours as it is.
- `LATET_BRIDGE_URL` and `LATET_BRIDGE_TOKEN` tell the plugin where the daemon is, and prove to
  the daemon that the plugin is yours.
- `ENABLE_TOOL_SEARCH` keeps MCP tool search on: Claude Code turns it off when the base URL is
  custom.
- `CLAUDE_CODE_AUTO_MODE_SERVER` moves auto mode's review into requests Latet can check.
  Without this line, the review travels as an extra field of every request, out of reach of both the plugin and the
  daemon. `latet agents apply claude --keep-classifier-context` leaves this line out.

Restart Claude Code afterwards. `latet agents status` shows whether it is applied, and
`latet agents revert claude` undoes both steps.

### Auto mode's reviewer

In auto mode, Claude Code asks a reviewer model whether each action is safe. The reviewer is sent
your CLAUDE.md, the action, your OS user name (as *User identity*) and the paths of your home and
working directories, in text the plugin cannot reach. It uses them to tell your things from other
people's: a push to a `yourname/...` branch is yours, one to `someone-else/...` is not. The daemon
checks these requests like any other, and `auto_mode_review` decides, detail by detail, what of your
identity may reach the reviewer:

| Detail | What it is |
| --- | --- |
| `user_name` | Your OS user name, the reviewer's *User identity*. |
| `paths` | Your user folder's name in the paths of your home and working directories. |
| `host_name` | This machine's host name, when your CLAUDE.md or the action contains it. |
| `git_author` | Your name and email as in your git author, and that email's domain when it is your company's, when your CLAUDE.md or the action contains them. |

Each detail is `block` (the default) or `allow`. A review that would carry a blocked detail is
stopped, so the detail stays local, and auto mode cannot approve the action. Claude Code then
refuses it, reporting its reviewer as temporarily unavailable, and does not ask you: run the action
yourself, or switch Claude Code to a permission mode that asks you. An allowed detail reaches the
reviewer, and only the reviewer. `auto_mode_review: block`
and `auto_mode_review: allow-identity` set every detail at once; one by one, it reads:

```yaml
auto_mode_review:
  user_name: allow
  paths: allow
  host_name: block
  git_author: block
```

Secrets and other people's data are never let through: a key or a client's email in the action
stops the review whatever you choose. Your own email counts as part of your git author.

**Keep secrets out of your CLAUDE.md.** Claude Code gives the reviewer your CLAUDE.md as it is on
disk, in a request it builds itself, so the plugin cannot mask it there, and the daemon never
changes Claude Code's requests. A password or a client's email in the file then stops every
review, and auto mode refuses every action that needs it. The model doing the work is not affected: it
gets your CLAUDE.md masked. Move those values to `.env` and name the variable in the file instead
(*the staging database is in `DATABASE_URL`*). The plugin says so when a session starts, and
`latet doctor` lists each value by line and type, never the value itself.

By default every detail stays local; `latet global-install --auto-mode-review user_name,paths`
lets them through one by one. The dashboard's **Rules** page and `config.yaml` change it later. When a
review is stopped, the dashboard shows the details it carried and the setting that lets them
through.

**A fake user name.** With `user_name: surrogate`, Latet gives Claude Code your user name's
surrogate in `USERNAME` and `USER`. Claude Code tells the reviewer that name, the same fake name the
model sees everywhere else, and a review that still carries your real user name is stopped. Run
`latet agents apply claude` after choosing it, then restart Claude Code. The commands Claude Code
runs see the fake name too, in `%USERNAME%`, `$USER` and Python's `getpass.getuser()`; `whoami`,
your home folder and git's author stay as they are. It helps when your user name and your user
folder differ, which is common on Windows, together with `paths: allow`: the reviewer then gets your
folder's name but never your user name. When they are the same, the paths carry the real name
anyway.

## Limits

- **Print mode shows the fakes.** In `claude -p` the printed answer contains surrogates, because
  only the interactive interface restores what is on screen. Files written by tools still get the
  real values.
- **Claude Desktop signed in to Claude**: the Code tab gets the plugin and its final check, but not
  the daemon behind it, and nothing is recorded in the dashboard; Chat and Cowork are not covered.
  On a third-party gateway, every request is checked and only the Code tab masks: see
  [Coverage](#coverage).
- **Remote Control is off** while Latet is in the path: Claude Code turns it off when the base URL
  is custom.
- **Web tools and claude.ai connectors cannot use a masked value.** Their arguments leave your
  machine, so they would get the fake one: such a call is stopped, and you choose whether the value
  may go out in clear (*Not sensitive*). Subagents stay on your machine and work on fakes as the
  model does. Connector results reach Claude Code through Anthropic's servers, so their content is
  there before Latet sees it. See
  [What is and isn't protected](../../concepts/protection-scope/).
- **Images**, with the optional [text recognition](../../concepts/images/): those that tools return
  are replaced by their masked copy; a screenshot you paste with values in it is taken out of the
  prompt, and Claude gets the path of a masked copy to open with Read. **PDFs** are not read and are
  sent as they are.
