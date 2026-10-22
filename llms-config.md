# Changing Latet's settings: a guide for AI agents

You are an AI agent (Claude Code, Codex, OpenCode, Hermes Agent, Goose, OpenClaw...), and the user
asked you to look at or change how Latet protects them: a term to mask, a profile, the local
reviewer, a project's settings. Latet is a local privacy firewall: it masks secrets and personal
data before a request leaves the machine. This guide is how you change its settings without
weakening it by mistake. The user-facing version is the
[Agents and your settings](https://docs.latet.ai/concepts/agents-and-settings/) page.

What this guide describes is a guardrail against mistakes, not a lock: you run as the user, so it
cannot hold against an agent set on getting around it, or one misled by a prompt injection. A real
boundary needs Latet running as another user, or on another machine.

## Rules

- **Use the commands below, never an editor.** `latet config` checks every value against the
  schema, applies the change to the running daemon, and tells you when it waits for the user. Do not
  edit `config.yaml` by hand: a hand edit that lowers protection waits for the user anyway, and you
  learn nothing about it.
- **You may add protection by yourself; anything that lowers it is the user's decision.** Ask the
  user before you run a command that lowers protection, and say what it lowers and why.
- **Never read, print or copy credentials**: `bridge.token`, `root.key`, `reviewer.key`, the
  credential store, API keys, the dashboard's login codes. Never call the daemon's HTTP endpoints
  yourself: the commands do it.
- **Never work around a refusal.** `latet global-uninstall`, `latet agents revert`, `latet service
  uninstall`, `latet backup` and `latet restore` refuse to run in an agent, and `latet rules
  suggest --show-values` too: tell the user to run them in a terminal of their own. Do not unset environment variables, fake a terminal, or
  edit files to get past them.
- **Never put real values where they do not belong.** A term the user asks you to mask is fine in
  `config.yaml`, which stays on this machine. Do not copy values into recognizer examples, commits,
  tickets or your answers (see [`llms-rules.md`](llms-rules.md) for recognizers).

## 1. Look

```sh
latet status                      # the daemon, and whether the Live Reviewer waits for the user
latet config get                  # every setting, with its default when not set
latet config get profile          # one setting; dots for what is inside: llm_review.enabled
latet config get --project .      # the user's own settings for the project in this folder
latet project show                # what applies in this project, and why
latet config schema               # every key, its type and what it does (JSON Schema)
```

`latet config get terms` prints the values the user asked to mask: read it only when the task
needs it.

## 2. Change

```sh
latet config set profile max                              # a value, in YAML
latet config set live_review '{enabled: true}'
latet config add terms '{value: Project Falcon, type: PROJECT}'
latet config add pii IBAN
latet config remove terms 'Project Falcon'                # by value, or a recognizer by name
latet config set dump_guard --unset                       # back to the default
latet config set llm_review.inline '[user_text]' --project .
latet llm guidance 'PRJ- followed by digits is an internal project.' --project .
```

The local reviewer's guidance is free text: set it with `latet llm guidance` (`-` reads standard
input), never with `config set`, which reads the value as YAML. Describe kinds of values in it,
never the confidential values themselves: those are terms.

Each command checks the new settings, saves `config.yaml` and applies it. What happens next depends
on the change:

| The change | What happens | Exit code |
| --- | --- | --- |
| Adds protection, or changes what does not protect (the port, the history) | Applies at once | 0 |
| Lowers protection | Waits for the user's approval in the dashboard; it prints what it lowers | 3 |
| Not valid | Nothing is saved; it prints which key and why | 2 |

Lowering protection means, for example: a value never to mask (`allow_values`), a lower profile,
a term or recognizer removed, a policy set to `allow`, the local reviewer or the Live Reviewer turned
off or reading less, Latet off in a project, the local reviewer's server on another machine, any
change to the local reviewer's guidance, more images sent (`images`), the text in images no longer
read (`ocr: off`), the local reviewer no longer shown the images or shown them on another machine
(`llm_review.images`), strict data mode turned off (`data: normal` with the `max` profile, which
turns it on). The daemon decides, against the settings the user approved last.

### A colleague's rules

```sh
latet rules import their-rules.yaml --dry-run    # what it would add, and what it would lower
latet rules import their-rules.yaml              # adds what protects, at once
latet rules export my-rules.yaml                 # the user's rules, for a colleague
```

Import only a file the user gave you, and show them the `--dry-run` first. What an import would
lower (values never to mask, a weaker policy...) is left out: `--all` applies it too, and it waits
for the user's approval. An exported file holds the user's terms in clear: write it only where the
user asked, and never paste it in your answers.

## 3. When a change waits (exit code 3)

1. Tell the user what you changed and why, and that it waits for them in the dashboard: `latet
   dashboard`, then **Approvals**. Quote the lines the command printed under *This change lowers
   protection*.
2. The change is not in effect yet: until the user approves it, Latet runs with the settings
   they approved last. If they undo it, `config.yaml` goes back as it was.
3. To wait for the answer, run the command with `--wait`: it ends with code 0 once approved, 3
   otherwise.

Only one change waits at a time: a newer one replaces it. Do not repeat a command the user undid.

## 4. Projects

- `latet project init` writes an `latet.yaml` for the repository, shared with everyone who works
  on it. It can only add protection; the user reviews it like any other file.
- The user's own settings for a project are in `config.yaml`: `--project FOLDER` on `config get`,
  `set`, `add` and `remove`, and `latet project off` / `on`. Turning Latet off in a project
  lowers protection: it waits for the user.

## 5. Check

```sh
latet config validate     # the file's keys, values and recognizers
latet doctor              # the daemon, the agents, the reviewers
```

## Why the commands, and what they do not promise

The guard is the daemon's check, not you: a change that lowers protection waits for the user
whoever makes it, an `latet` command, `config.yaml` edited by hand then reloaded, or a file found
changed when the daemon starts. The refusals of `latet global-uninstall` and the others tell an agent by the
variables agents set in their shells. They stop mistakes, not someone determined: anything that runs
as the user can read what Latet reads. Follow the rules above, so the user can trust what you do.
