---
title: Agents and your settings
description: Let your agent look at and change Latet's settings. What adds protection applies; what lowers it waits for you.
---

:::caution[A guardrail, not a lock]
This lets an agent help you set up Latet and your projects without lowering protection by
mistake. It cannot hold against an agent set on getting around it, or one misled by a prompt
injection: the agent runs as you. A real boundary takes more, such as Latet running as another
user or on another machine: see [what this is worth](#how-latet-tells-and-what-that-is-worth).
:::

You can ask your agent to set Latet up for you: *"mask our client Falcon everywhere"*, *"turn on
the Live Reviewer in this project"*, *"why was this value masked?"*. Point it at
[`llms-config.md`](https://github.com/Latet-AI/latet/blob/main/llms-config.md) in the repository:
it is written for agents, and tells them which commands to use and what to ask you first.

## What an agent can do by itself

It looks with `latet config get`, `latet project show` and `latet status`, and changes with
`latet config set`, `add` and `remove`. Every value is checked against the settings' schema, and
the change applies to the running daemon at once, **as long as it adds protection**: a term, a
recognizer, a stricter profile, the local reviewer reading more, the Live Reviewer turned on.

```sh
latet config add terms '{value: Project Falcon, type: PROJECT}'
latet config set live_review true --project .
```

## What waits for you

A change that **lowers** protection does not apply until you approve it, whoever makes it:

- a `latet` command run by an agent, or by a script;
- `config.yaml` edited by hand, when the daemon reloads it;
- `config.yaml` found changed when the daemon starts: until you decide, it runs with the settings
  you approved last.

Lowering protection means, for example, a value never to mask, a lower profile, a term or
recognizer removed, a policy set to `allow`, the local reviewer or the Live Reviewer turned off or
reading less, Latet off in a project, the local reviewer's server moved to another machine, or
[its guidance](../llm-review/#your-guidance) changed: free text could tell it to report nothing, so
any change waits, and the dashboard shows the guidance before and after. The same goes for a
[destination](../../agents/company-endpoints/) moved to another address, or removed: an agent pointed
at it would send its requests, and its keys, somewhere else, so the dashboard shows the address before
and after.

The change waits on the **Approvals** page of the dashboard, under *Settings*, with what it lowers,
where it comes from (the agent that ran the command, when Latet can tell) and which settings it
touches. **Approve** applies it. **Undo it** puts `config.yaml` back as it was. Only one change
waits at a time; a newer one replaces it. At a terminal of your own, the command asks you instead.

The agent is told what the change lowers, never a value, and that it waits for you: the guide asks
it to tell you what it changed and why.

**A repository Latet cannot read now.** A change is weighed in each repository Latet has seen with a
`latet.yaml` of its own too. Their files are read again by a helper process, with 10 seconds for all
of them and 2 for each, at a reload or a start whose change could lower protection in a repository,
and at an import in the dashboard (see [The repositories Latet remembers](../projects/#the-repositories-latet-remembers)).
When one cannot be read in that time, or cannot be read now at all (a share that does not answer, a
refused permission), a change that could lower protection there neither applies nor waits for you:
`latet reload` says so and changes nothing, and so does an import in the dashboard, until the
repository can be read again. A change that cannot lower anything in a repository (a term added, the
port, the history) applies as usual. A start that finds `config.yaml` changed and cannot weigh it so
runs the settings you approved last. `latet repositories` shows which repository it is, and
`latet repositories forget` takes it out of the list, at your own terminal.

## What agents cannot do

`latet global-uninstall`, `latet agents revert` and `latet service uninstall` take protection away from
every agent at once: run in an agent, they refuse, and ask you to run them in a terminal of your
own. So do `latet repositories forget`, `latet backup` and `latet restore` (your vault and its key),
`latet connect` and `latet disconnect` (where your agents' requests are masked), `latet dashboard`
and `latet identity send` on a connected computer, `latet server` administration, and
`latet rules suggest --show-values`, which would print real values.

## How Latet tells, and what that is worth

An agent marks the shells it runs commands in with variables of its own, which Latet recognises:
Claude Code, Codex, OpenCode, Gemini CLI, Cursor, Hermes Agent, Goose, OpenClaw, and any agent
that sets `AGENT`. The daemon also names the agent in the change it shows you.

These are guards against mistakes, not locks:

- **The check that matters is the daemon's.** It compares every new configuration with the one you
  approved last, whoever wrote it. An agent that edits `config.yaml` instead of using the commands
  gets the same answer.
- **An agent runs as you.** One determined to, or misled by text it read (a prompt injection), can
  unset its variables, read the files Latet reads, and approve its own change through the daemon's
  local interface. Latet cannot stop what your own user account can do: that needs Latet
  running as another user, or on another machine
  ([Latet on a server](../server/)).

For an agent you supervise, this is enough: it cannot weaken Latet by mistake, and you see every
change that lowers protection before it applies. See [Security](../../security/) for the rest of
the threat model.

## On a connected computer

The shared settings and each identity's own settings and rules live on the server. Use
`latet dashboard` for a person's settings and `latet server` administration for the shared
settings or an API key's.
Connected, `config`, `rules`, `llm`, `project`, `identity`, `repositories` and `reload` refuse to
read settings as well as to change them; only `config schema`, `rules test` and `identity send` run.
`latet identity send` explicitly resends this computer's identity facts.

Connection metadata, the certificate pin, credentials and agent bindings remain on the
computer. `LATET_CONNECTION` selects metadata, not a personal-token file. Applying Claude
on Windows writes the token into user Claude settings as the documented installation exception (on
Linux and macOS no token is written, since Claude Code goes through a server from Windows alone);
disconnect removes that binding. Server reviewer endpoints, keys, certificates and OCR/NER
installation belong to administrators. A project in the dashboard does not let the server
scan the computer's repository. See [server setup](../../getting-started/server/) and
[shared settings and own rules](../server/).

## Commands

| Command | What it does |
| --- | --- |
| `latet config get [KEY]` | A setting (dots for what is inside: `llm_review.enabled`), or all of them |
| `latet config set KEY VALUE` | Changes a setting (the value in YAML); `--unset` puts back its default |
| `latet config add KEY VALUE` / `remove KEY VALUE` | Adds an item to a list, or takes one out (terms by value, recognizers by name) |
| `--project FOLDER` | Your own settings for the project in that folder, instead of the global ones |
| `--wait` | When the change waits for you, waits for your answer |
| `latet reload` | Applies a `config.yaml` edited by hand (what lowers protection waits for you) |

Exit codes: `0` applied, or saved while the daemon is not running (it applies when the daemon
starts, and what lowers protection waits for you then); `3` waits for you; `1` the daemon refused
the change (saved, not applied: `latet reload` says why), or the item to remove is not there; `2`
not valid (nothing saved).
