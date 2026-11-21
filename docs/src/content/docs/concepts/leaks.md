---
title: Secrets in saved sessions
description: latet leak reads the sessions your agents saved on this computer, lists the secrets in them without showing a value, and says which may have reached the provider.
---

Your coding agents keep each conversation on disk, whole: the prompts you typed, the files they
read, the output of the commands they ran. A secret that came into a conversation before you
installed Latet, or in a session that did not go through it, is in those files. The
[dashboard](../../dashboard/) cannot show it: it sees only the requests that went through Latet,
for as long as its history keeps them (14 days by default). `latet leak` reads the saved sessions
with Latet's own detectors and lists the secrets in them, so that you know which keys to rotate and
which conversations not to resume.

```sh
latet leak
```

It reads and prints, and never shows a value. It changes none of your files, needs no network and
no running daemon, and Latet's vault learns nothing from it: the values it finds stay in its memory
while it reads, and no longer. Since it prints no value, an agent can run it too.

## Where your agents keep their sessions

`~` is your home folder (`C:\Users\you` on Windows). Where an agent's variable is set, its sessions
are in the folder it names, and `latet leak` reads them there.

| Agent | Its saved sessions | The variable that moves them |
| --- | --- | --- |
| [Claude Code](../../agents/claude-code/) | `~/.claude/projects/`: a `.jsonl` file for each session, and for each of its subagents | `CLAUDE_CONFIG_DIR`, in place of `~/.claude` |
| [Codex](../../agents/codex/) | `~/.codex/sessions/` and `~/.codex/archived_sessions/` | `CODEX_HOME`, in place of `~/.codex` |
| [OpenCode](../../agents/opencode/) | `~/.local/share/opencode/opencode.db`, and the older `storage` folder beside it | `XDG_DATA_HOME`, in place of `~/.local/share` |
| [Copilot CLI](../../agents/other-tools/#github-copilot-cli) | `~/.copilot/session-state/`: an `events.jsonl` file for each session | `COPILOT_HOME`, in place of `~/.copilot` |
| [Gemini CLI](../../agents/gemini/) | `~/.gemini/tmp/`: each project's `chats` folder | `GEMINI_CLI_HOME`, in place of `~` |
| [Qwen Code](../../agents/other-tools/#qwen-code) | `~/.qwen/projects/`: each project's `chats` folder; and the older `~/.qwen/tmp/` | `QWEN_HOME`, in place of `~/.qwen` |
| [Hermes Agent](../../agents/autonomous-agents/#hermes-agent) | `state.db` in `~/.hermes`, and on Windows in `%LOCALAPPDATA%\hermes` | `HERMES_HOME`, the folder that holds `state.db` |

`--agent` reads the agents you name alone, by these names: `claude`, `codex`, `opencode`, `copilot`,
`gemini`, `qwen` and `hermes`; repeat it for more. `--path` also reads a file, or the `.json`,
`.jsonl` and `.ndjson` files of a folder and its subfolders, in the same way: the sessions of an
agent this table does not list, say, or a folder of sessions copied from another computer.

**What it reads.** In each record, every text: your prompts, the model's answers, the tool calls and
their results, and a text that is itself JSON. Not the ids, the times and the signatures, which
hold no conversation. Each text is read by your own detectors with your global settings, as Latet
reads a tool's output: the built-in rules, your terms and your recognizers, each type with its
policy. A value your settings let through is not listed. The local name model and the local reviewer
are not used, and a project's `latet.yaml` is not read.

By default it looks for secrets alone: keys, tokens, passwords, private keys, credentials in a web
address. `--personal` also counts personal data, such as e-mail addresses and phone numbers, by
type: how many sessions hold each type, never a value.

## What it says

An example, with made-up folders and counts:

```text
$ latet leak
Latet read 242 Claude Code sessions, 38 Codex sessions and 5 Copilot CLI sessions.

2 secrets in clear (no sign that Latet replaced them: they may have reached the provider):
  By type: 1 Github pat and 1 Password.
  Github pat  ghp_… (40 characters)  Claude Code, 4 sessions in C:\Users\you\dev\shop
      2026-09-02 to 2026-09-09
  Password (17 characters)  Codex, 1 session in C:\Users\you\dev\billing
      2026-09-20
Latet can tell only from 2026-10-10 on, the earliest date its vault and history keep on this computer: an emptied vault, or a history past the days it keeps (14 by default), keeps nothing older. A session listed in clear from before then may have gone through Latet all the same.

2 secrets Latet replaced, or may have (its fake left instead where the session went through Latet):
  By type: 1 Anthropic api key and 1 Github pat.
  Anthropic api key  sk-ant-api03-… (108 characters)  Claude Code and Codex, 6 sessions in C:\Users\you\dev\billing, C:\Users\you\dev\shop
      2026-10-12 to 2026-11-14; 5 confirmed by Latet's history, 1 if the session went through Latet
  Github pat  ghp_… (40 characters)  Claude Code, 2 sessions in C:\Users\you\dev\shop
      2026-10-12 to 2026-11-14; 1 confirmed by Latet's history, 1 if the session went through Latet

Rotate each secret in clear at its vendor: what reached a model provider may stay in its logs. Do not resume those sessions (a resumed conversation sends them again): start new ones. The secrets Latet replaced need nothing where the session went through Latet.
```

The same secret pasted in several sessions is one line, with the number of sessions. So is a secret
the detectors found as two types, such as a GitHub token in a `curl` command's header and assigned
to a variable: the line shows the vendor's type. A secret can be in two lists, as the GitHub token
above: pasted before Latet was there, and again after.

### In clear

*No sign that Latet replaced them: they may have reached the provider.* A session counts here when
it held the value before Latet's vault first kept it, or when the vault does not know the value at
all. Two more cases count here: a session whose records have no time, which cannot be placed before
or after that date, and a session that Latet's history shows sending the value in clear first: the
history still keeps the session's first requests, and shows the value masked only in later ones.
These are the secrets to rotate. Below the list, the command says from which date Latet can tell:
the earliest date its vault and history keep on this computer.

### Replaced by Latet

The vault already knew the value when the session held it, so where the session went through Latet,
the provider received Latet's fake instead. The value is in the session file all the same, because
the agent saves what it had before Latet replaced it on the way out. There is nothing to rotate for
the provider's sake, though the value sits in a file on this computer.

Each secret says how many of its sessions are *confirmed by Latet's history*: the history still
holds one of the session's requests with the value masked in it. That takes a history kept in full
(*Everything*, the default) and still holding the session (14 days by default), and an agent that
names its session in its requests, as Claude Code, Codex and Hermes Agent do. Another session counts
*if the session went through Latet*: an agent that was not set up to use Latet then sent the value
in clear, and only the history can tell. Where a secret has such a session, the header says *Latet
replaced, or may have*.

### Redacted

*Your settings redact, or Latet redacts as too long to keep:* a secret of a type your
[policies](../profiles/#policies) set to `redact`, or one longer than the 16,384 characters the
vault keeps. Latet replaces such a value with a marker and keeps it nowhere, so its vault has no
date for it. The sessions from the earliest date Latet has on this computer are listed here, those
before it in clear. Your settings may have been different then, and the session may not have gone
through Latet: so the header says that Latet *may have sent a marker in its place*.

### Each list

Under its header, a list counts its secrets by type: the six types with the most, then the others in
one count. Then come the secrets, a vendor's keys first (a GitHub token, an Anthropic key, an AWS
key: the ones to rotate first), then those in the most sessions. A list shows the first 20 and says
how many more it has; `--all` lists every one.

A secret shows its type, a hint and, for that list's sessions, the agents, how many sessions, the
project folders (three at most, then how many others) and the dates it was first and last seen, on
this computer's clock, as `--since` reads a date. The hint keeps only the public prefix the vendor
puts before every key of its kind (`ghp_…`, `sk-ant-api03-…`, `AKIA…`), the same a fake keeps, and
the length; a secret without one shows its type and its length (`Password (17 characters)`).

At the end come the personal data with `--personal`, what to do, and what could not be read, with
why, never its content: a file it may not open, a JSON file of more than 32 MB, a single text of
more than 8 MB, a database that changed each time it was copied.

**Without Latet's answers.** On a computer connected to a [Latet server](../server/), the vault and
the history are the server's. The command then says that it could not tell which secrets Latet
replaced, and lists every secret in clear, but those it lists as redacted. It does the same, and
says why, where Latet's vault on this computer, or its key, could not be read, or no key found here
opens it. On a computer with no Latet vault and no Latet server, Latet replaced nothing: the command
says so, and lists every secret in clear, the redacted types too.

## Options

| Option | What it does |
| --- | --- |
| `--agent NAME` | Read this agent's sessions alone: `claude`, `codex`, `opencode`, `copilot`, `gemini`, `qwen` or `hermes`. Repeat it for more. |
| `--since DATE` | Read the records from this date on (`2026-10-01`), by each record's time, else by its file's last change. |
| `--personal` | Also count personal data, by type: how many sessions hold each type. |
| `--path PATH` | Also read this file, or the JSON and JSON lines files of this folder. Repeat it for more. |
| `--json` | Print one JSON document instead of text, with every secret. |
| `--all` | List every secret of each list, not the first 20. |

`--json` gives a record for each secret: its `type` (the one the text shows), `types` (every type it
was found as), `class`, `hint`, `agents` and `folders`, and its sessions `in_clear`, `replaced` and
`redacted`, each with how many, the first and last dates, how many are confirmed, and the `agents`
and `folders` of those sessions. Beside them: the sessions read by agent, the personal data by type,
what could not be read (`skipped`) and how many things that is (`unread`), `latet_answers` (whether
Latet's vault and history could be asked) and `latet_since` (the earliest date they keep, given when
a secret is in clear).

The exit code is `0` when nothing is found, `1` when a secret is found, in any list, or personal
data with `--personal`, and `2` when no session could be read, or an option is wrong. A script can
run it and act on that.

## How long it takes

`latet leak` reads every text of every saved session, which takes time where there are many: about
11 minutes for 10 GB of sessions on a desktop PC. On a console it shows the agent it is reading and
how many of its sessions it has read so far, on one line, then how long it took; past a minute, it
says that `--agent` and `--since` read less:

```sh
latet leak --agent claude --since 2026-10-01
```

A session is judged on the records read: with `--since`, a secret that an earlier record of the
same session sent in clear can be listed as replaced.

Those lines go to standard error, and only to a console: the report alone goes to standard output,
and with `--json` it stays one JSON document there.

## What it cannot tell

- **Before Latet's date.** Latet knows a value from the day its vault first kept it, and a session
  that held the value before that day is listed in clear. An emptied vault, a value you *Forget*
  (the vault knows it again from when it comes back) or a history past the days it keeps keeps
  nothing older: a session listed in clear from before the date under the list may have gone
  through Latet all the same.
- **A session outside Latet.** A secret listed as replaced, in a session the history does not
  confirm, may have gone around Latet: only the history can tell, for the days it keeps.
- **What Claude Code keeps for its own screen.** Claude Code saves a second copy of a tool's result
  for its own display (`toolUseResult`): a file it read, an edit's whole file, a command's output.
  That copy never reaches the provider, and `latet leak` does not read it. The copy the request
  carried is read.
- **A secret split in two.** Each text is read on its own: a secret split across two records is not
  found, and a value found in one text by the word before it (`password:`) is not found where
  another text writes it alone.
- **What the detectors do not recognize,** as with Latet's masking ([Not protected](../protection-scope/#not-protected)).
  A value Latet knows only from its vault, because the local reviewer or you found it, is not found
  either: the scan starts from an empty vault of its own.

A database in write-ahead mode, which a read would change by adding files beside it, is read from a
private copy in a temporary folder, deleted afterwards; the copy is made only where at least 1 GB of
the disk stays free after it. That holds for OpenCode's and Hermes Agent's databases, and for
Latet's own vault and history.

## What to do with a finding

- **Rotate each secret in clear at its vendor,** the vendor's keys first, as the list puts them:
  make a new key where the vendor issues them (GitHub, Anthropic, AWS...), use it in place of the
  old one, and revoke the old one; change a password. What reached a model provider may stay in its
  logs, whatever you delete on your computer. The type, the prefix, the folders and the dates tell
  you which key it is and where it was used.
- **Do not resume those conversations.** A resumed conversation sends all its messages again, the
  secret with them: start a new one.
- **The secrets Latet replaced** need nothing at their vendor where the session went through Latet:
  the provider received a fake. **The redacted ones may need nothing:** if the session went through
  Latet, the provider received a marker in their place.
- **The files on disk are your agent's.** Latet deletes and moves none of them. To take a value off
  this computer, delete or move the session files that hold it, with the agent closed: the agent,
  the project folder and the dates in the list tell you where to look. A conversation whose file you
  delete can no longer be resumed.

A secret you rotated is still listed while its session files are there: the list says what the files
hold, not whether a key still works.
