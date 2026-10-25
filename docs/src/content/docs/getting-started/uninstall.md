---
title: Uninstall
description: Remove Latet cleanly, in the right order, so your agents keep working.
---

Order matters: undo the agent configuration **before** you remove Latet. Otherwise your agents
keep sending their traffic to `127.0.0.1:5283` and fail with connection errors once the daemon is
gone.

## 1. Turn Latet off

```sh
latet global-uninstall
```

This one command:

- reverts every agent Latet configured. For Claude Code it uninstalls the plugin and puts back
  `settings.json` as it was before `latet global-install`. For Codex it removes the
  `openai_base_url` line, for OpenCode the `baseURL` overrides. If you edited one of these files
  since, Latet removes only what it added and keeps your edits;
- removes the start at login;
- stops the daemon.

It does not change the tools you pointed at Latet yourself, in their own settings or in your shell:
GitHub Copilot, Zed, Cline, Kilo Code, Continue, Crush, Prime Agent, Junie, Android Studio, Hermes
Agent, OpenClaw, Goose, OpenHands, Aider, and a `latet env` line in your shell profile. Put back
their own address in each, or they fail with connection errors once the daemon is stopped.

It prints what it did for each part, for example:

```text
Claude Code: restored
Codex: restored
OpenCode: not applied
autostart: removed
daemon: stopped
```

Restart your agents afterwards. To revert a single agent instead, run
`latet agents revert claude` (or `codex`, `opencode`).

## 2. Remove the tool

```sh
uv tool uninstall latet
```

If you ran Latet from a source checkout, there is nothing to uninstall: run `uv run latet global-uninstall`
in step 1, then delete the checkout. If you installed it with `py -m pip`, run
`py -m latet global-uninstall` in step 1, then `py -m pip uninstall latet`.

## 3. Delete local data (optional)

To keep your settings and the vault, for later or for another machine, save them first:
`latet backup latet.backup --vault` (see
[Export and import](../../concepts/export-import/#your-installation-for-another-machine)).

Latet keeps its data on your machine only, in two folders:

| | Windows | macOS | Linux |
| --- | --- | --- | --- |
| Data | `%LOCALAPPDATA%\latet` | `~/Library/Application Support/latet` | `~/.local/share/latet` |
| Configuration (`config.yaml`) | `%APPDATA%\latet` | `~/Library/Application Support/latet` | `~/.config/latet` |

If you set `LATET_DATA_DIR` or `LATET_CONFIG`, look there instead. The data folder holds:

- `latet.db`: the vault, which maps each original to its surrogate. The originals are encrypted.
- `exchanges.db`: the dashboard history.
- `backups/`: copies of your agents' configuration files from before `latet global-install`
  changed them.
  `latet global-uninstall` needs them, so delete them only after step 1. In
  `backups/before-<version>-<time>/` are the copies of the vault, the history and the settings
  that `latet upgrade` takes before each upgrade (the three newest).
- `models/`: the local name model and the text recognition models (about 1.6 GB and 31 MB), which
  `latet ner remove` and `latet ocr remove` delete.
- `logs/`, `bridge.token`, `integrations.json` (the record of what `latet global-install` changed),
  `claude-marketplace/` (the copy of the Claude Code plugin), `tutorial.json` (the chapters of the
  [tutorial](../tutorial/) you finished), `repositories.json` (the folders of the repositories with a
  `latet.yaml` that Latet has seen), and `root.key` if your system has no credential store.

The key that encrypts the vault lives in your operating system's credential store (Windows
Credential Manager, macOS Keychain, or the desktop keyring on Linux), under the service name
`latet` and the account `root-key`. Delete that entry too.

## If the `latet` command is already gone

Undo the changes by hand:

- **Claude Code**: run `claude plugin uninstall latet@latet` and
  `claude plugin marketplace remove latet`. Then remove these keys from the `env` block of
  `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`): `ANTHROPIC_BASE_URL`,
  `LATET_BRIDGE_URL`, `LATET_BRIDGE_TOKEN`, `ENABLE_TOOL_SEARCH` and
  `CLAUDE_CODE_AUTO_MODE_SERVER`. Keep any of them you had set yourself before. Remove also
  `LATET_BRIDGE_HELPER`, `NODE_EXTRA_CA_CERTS`, `USER` and `USERNAME` if Latet set them, and put
  back any address that points at `127.0.0.1` (`ANTHROPIC_BEDROCK_BASE_URL`,
  `ANTHROPIC_VERTEX_BASE_URL`, `ANTHROPIC_FOUNDRY_BASE_URL`) as it is in the copy of `settings.json`
  in `backups/`.
- **Codex**: remove the `openai_base_url` line from `~/.codex/config.toml` (or
  `$CODEX_HOME/config.toml`), and, for a provider of your own, put back the `base_url` of its
  `[model_providers.<id>]` section, in `config.toml` or in the profile's `<name>.config.toml`, from
  the copy in `backups/`.
- **OpenCode**: remove the `baseURL` entries that point at `127.0.0.1:5283` from
  `~/.config/opencode/opencode.jsonc` (or `opencode.json`).
- **Other clients**: unset `OPENAI_BASE_URL`, `OPENAI_API_BASE`, `ANTHROPIC_BASE_URL`,
  `GOOGLE_GEMINI_BASE_URL` and `GOOGLE_VERTEX_BASE_URL`, or put back the base URL you used before.
  In the tools you pointed at Latet in their own settings, put back their own address.
- **Start at login**: on Windows, delete the `Latet` value under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. On macOS, run
  `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/dev.latet.daemon.plist`, then delete
  that file. On Linux, run `systemctl --user disable latet.service`, then delete
  `~/.config/systemd/user/latet.service`.

Copies of your configuration files from before `latet global-install` are in the `backups/`
folder of the data folder.
