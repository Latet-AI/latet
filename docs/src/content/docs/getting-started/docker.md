---
title: Docker
description: Run the Latet daemon in a container, and point your agents at it.
---

Latet also runs as a container: no Python on your computer, and the daemon kept apart from the
rest of it. The image holds the daemon, the dashboard and the runtime of the optional
[name model](../installation/) and [text recognition](../../concepts/images/); their models are
downloaded only if you ask. Your agents stay
on your computer and talk to the container on `127.0.0.1`.

The container cannot change your agents' settings or see who you are, so two steps that
`latet global-install` does by itself are done by hand here: pointing the agents at it, and telling it your
identity. Both are a copy and paste.

## Start it

Save [`compose.yaml`](https://github.com/Latet-AI/latet/blob/main/compose.yaml) from the
repository, then:

```sh
docker compose up -d
```

Without Compose, the same in one command:

```sh
docker run -d --name latet --restart unless-stopped -p 127.0.0.1:5283:5283 \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true --init \
  -v latet-data:/data -v latet-config:/config ghcr.io/latet-ai/latet:latest
```

`docker exec latet latet status` says whether it is up. The container runs as a user without
privileges, on a read-only file system, and keeps everything in two volumes: `latet-data` (the
vault, its key and the dashboard history) and `latet-config` (`config.yaml`).

## Point your agents at it

```sh
docker exec latet latet agents print
```

It prints, for each agent, what to change and a command that does it. On Windows, add
`--shell powershell`. If an agent's settings file does not exist yet, create it empty first, such
as `mkdir -p ~/.codex && touch ~/.codex/config.toml`. In short:

- **Claude Code** needs the plugin and a few variables in `~/.claude/settings.json`:

  ```sh
  claude plugin marketplace add Latet-AI/latet
  claude plugin install latet@latet
  docker exec -i latet latet agents merge claude < ~/.claude/settings.json > ~/.claude/settings.json.new \
    && mv ~/.claude/settings.json.new ~/.claude/settings.json
  ```

  `agents merge` reads the file, adds the variables and writes it back, so the token the plugin
  shows the daemon goes into the file without being printed. One of the variables,
  `LATET_AUTOSTART=0`, tells the plugin not to start a daemon of its own: if the container is
  down, Claude Code says so and withholds requests until it is back.
- **Codex** needs `openai_base_url` in `~/.codex/config.toml`, which this writes
  (`--codex-login api-key` if Codex signs in with an API key instead of ChatGPT):

  ```sh
  docker exec -i latet latet agents merge codex < ~/.codex/config.toml > ~/.codex/config.toml.new \
    && mv ~/.codex/config.toml.new ~/.codex/config.toml
  ```

- **OpenCode** needs a `baseURL` for each provider in `opencode.json`, the same way.

PowerShell has no `<` redirection, so `agents print --shell powershell` copies the file into the
container's `latet-data` volume with `docker cp`, merges it there, and copies it back.

Restart the agents afterwards. For these routes the [modes](../../concepts/how-it-works/) and the
protection are the same as with the daemon installed on your computer: Anthropic for Claude Code,
OpenAI or ChatGPT for Codex, and OpenCode's providers. Claude Code on Bedrock, Vertex AI or
Microsoft Foundry, a Codex provider of its own and a company's gateway are set up by
`latet claude`, `latet codex` and `latet global-install`, which run on your computer: use Latet
installed on your computer for them.

## Tell it who you are

Latet masks your user name in paths, your host name and your git author. In the container it
cannot see them, so tell it once, from your computer (this is also in `agents print`):

```sh
docker exec latet latet identity set --user-name "$(id -un)" --user-name "$(basename "$HOME")" \
  --host-name "$(hostname)" --full-name "$(git config --global user.name)" \
  --email "$(git config --global user.email)"
```

`docker exec latet latet identity show` prints what it masks.

## Everyday use

| What | Command |
| --- | --- |
| Open the dashboard | `docker exec latet latet dashboard --print-url`, then open the link on your computer |
| Check everything | `docker exec latet latet doctor` |
| Download the name model (about 1.6 GB, once) | `docker exec latet latet ner install --yes` |
| Download the text recognition models (about 31 MB, once) | `docker exec latet latet ocr install --yes` |
| Your own recognizers | `docker exec -i latet latet rules add - < rules.yaml` |
| Update | `docker compose pull && docker compose up -d` |
| Another port | `LATET_PORT=6655 docker compose up -d`, then `agents merge` again |
| A backup of the settings and the vault | `docker exec -it latet latet backup /data/latet.backup --vault`, then `docker cp latet:/data/latet.backup .` ([restoring it](../../concepts/export-import/#in-docker)) |

The settings live in `config.yaml` in the `latet-config` volume; the dashboard's **Rules** page
changes them, and `docker exec latet latet reload` applies a file you edited.

## Projects and the local reviewer

- **[Projects](../../concepts/projects/):** your own settings for a project work by its folder's
  path. A project's `latet.yaml` is read only when the folder is mounted in the container at the
  same path.
- **The [local reviewer](../../concepts/llm-review/)** runs on your computer, outside the container,
  and in the container `127.0.0.1` is the container itself. Set its address to
  `http://host.docker.internal:8080/v1`, which Latet accepts in its container only. On Linux, add
  `extra_hosts: ["host.docker.internal:host-gateway"]` to the service in `compose.yaml`, and have
  the model server listen on an address the container can reach.

## Security

- **Publish the port on `127.0.0.1` only.** On Linux, Docker's published ports go around the
  firewall: `-p 5283:5283` would open Latet, and the requests your agents send through it, to
  your network. `compose.yaml` and the command above publish on `127.0.0.1`.
- Other containers on the same Docker network can reach the port. The bridge, the dashboard and
  the admin routes still need the token or a dashboard session, and every request must name
  `127.0.0.1` or `localhost` as its host; the proxy routes forward the caller's own credentials.
- Whoever can read the `latet-data` volume can read the vault: it holds the key the vault is
  encrypted with. To keep the key elsewhere, give it as a Docker secret and set
  `LATET_ROOT_KEY_FILE` to its path.

## Behind a proxy

When the providers are reached through a proxy of your network, set `HTTPS_PROXY` (and `NO_PROXY`)
before `docker compose up`; `compose.yaml` passes them on. If the proxy inspects TLS with a
certificate of its own, mount that certificate in the container and set `SSL_CERT_FILE` to it.

## Undo

This takes Claude Code's settings out (the same for `codex` and `opencode`, with their files):

```sh
docker exec -i latet latet agents merge claude --remove < ~/.claude/settings.json > ~/.claude/settings.json.new \
  && mv ~/.claude/settings.json.new ~/.claude/settings.json
```

`claude plugin uninstall latet@latet` removes the plugin. Then `docker compose down`;
`docker compose down -v` also deletes the volumes, and with them the vault.
