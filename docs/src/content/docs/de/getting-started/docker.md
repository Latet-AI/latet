---
title: Docker
description: Führen Sie den Daemon von Latet in einem Container aus, und richten Sie Ihre Agenten darauf aus.
---

Latet läuft auch als Container: kein Python auf Ihrem Rechner, und der Daemon getrennt vom Rest des
Rechners. Das Image enthält den Daemon, das Dashboard und die Laufzeitumgebung des optionalen
[Namensmodells](../installation/) und der optionalen [Texterkennung](../../concepts/images/); ihre
Modelle werden nur heruntergeladen, wenn Sie es verlangen. Ihre Agenten bleiben auf Ihrem Rechner und
sprechen mit dem Container auf `127.0.0.1`.

Der Container kann weder die Einstellungen Ihrer Agenten ändern noch sehen, wer Sie sind, daher
werden zwei Schritte, die `latet global-install` selbst erledigt, hier von Hand ausgeführt: die
Agenten auf ihn ausrichten und ihm Ihre Identität mitteilen. Beides geht per Kopieren und Einfügen.

## Starten

Speichern Sie [`compose.yaml`](https://github.com/Latet-AI/latet/blob/main/compose.yaml) aus dem
Repository, dann:

```sh
docker compose up -d
```

Ohne Compose dasselbe in einem einzigen Befehl:

```sh
docker run -d --name latet --restart unless-stopped -p 127.0.0.1:5283:5283 \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true --init \
  -v latet-data:/data -v latet-config:/config ghcr.io/latet-ai/latet:latest
```

`docker exec latet latet status` zeigt, ob der Container läuft. Er läuft als Benutzer ohne
Privilegien, auf einem schreibgeschützten Dateisystem, und hält alles in zwei Volumes: `latet-data`
(der Tresor, sein Schlüssel und der Verlauf des Dashboards) und `latet-config` (`config.yaml`).

## Ihre Agenten darauf ausrichten

```sh
docker exec latet latet agents print
```

Der Befehl gibt für jeden Agenten aus, was zu ändern ist, und einen Befehl, der es erledigt. Unter
Windows fügen Sie `--shell powershell` hinzu. Wenn die Einstellungsdatei eines Agenten noch nicht
existiert, legen Sie sie zuerst leer an, etwa mit `mkdir -p ~/.codex && touch ~/.codex/config.toml`.
Kurz gesagt:

- **Claude Code** braucht das Plugin und einige Variablen in `~/.claude/settings.json`:

  ```sh
  claude plugin marketplace add Latet-AI/latet
  claude plugin install latet@latet
  docker exec -i latet latet agents merge claude < ~/.claude/settings.json > ~/.claude/settings.json.new \
    && mv ~/.claude/settings.json.new ~/.claude/settings.json
  ```

  `agents merge` liest die Datei, fügt die Variablen hinzu und schreibt sie zurück, sodass das
  Token, mit dem sich das Plugin beim Daemon ausweist, in die Datei gelangt, ohne ausgegeben zu
  werden. Eine der Variablen, `LATET_AUTOSTART=0`, weist das Plugin an, keinen eigenen Daemon zu
  starten: Läuft der Container nicht, meldet Claude Code das und hält Anfragen zurück, bis er wieder
  läuft.
- **Codex** braucht `openai_base_url` in `~/.codex/config.toml`, was dieser Befehl schreibt
  (`--codex-login api-key`, wenn sich Codex mit einem API-Schlüssel statt mit ChatGPT anmeldet):

  ```sh
  docker exec -i latet latet agents merge codex < ~/.codex/config.toml > ~/.codex/config.toml.new \
    && mv ~/.codex/config.toml.new ~/.codex/config.toml
  ```

- **OpenCode** braucht auf dieselbe Weise eine `baseURL` für jeden Anbieter in `opencode.json`.

PowerShell kennt keine Umleitung mit `<`, daher kopiert `agents print --shell powershell` die Datei
mit `docker cp` in das Volume `latet-data` des Containers, führt sie dort zusammen und kopiert sie
zurück.

Starten Sie die Agenten danach neu. Für diese Routen sind die [Modi](../../concepts/how-it-works/)
und der Schutz dieselben wie mit dem auf Ihrem Rechner installierten Daemon: Anthropic für Claude
Code, OpenAI oder ChatGPT für Codex und die Anbieter von OpenCode. Claude Code mit Bedrock, Vertex AI
oder Microsoft Foundry, ein eigener Anbieter von Codex und das Gateway eines Unternehmens werden von
`latet claude`, `latet codex` und `latet global-install` eingerichtet, die auf Ihrem Rechner
laufen: Verwenden Sie dafür das auf Ihrem Rechner installierte Latet.

## Latet mitteilen, wer Sie sind

Latet maskiert Ihren Benutzernamen in Pfaden, Ihren Hostnamen und Ihren Git-Autor. Im Container kann
es sie nicht sehen, teilen Sie sie ihm daher einmal mit, von Ihrem Rechner aus (das steht auch in
`agents print`):

```sh
docker exec latet latet identity set --user-name "$(id -un)" --user-name "$(basename "$HOME")" \
  --host-name "$(hostname)" --full-name "$(git config --global user.name)" \
  --email "$(git config --global user.email)"
```

`docker exec latet latet identity show` gibt aus, was es maskiert.

## Im Alltag

| Was | Befehl |
| --- | --- |
| Das Dashboard öffnen | `docker exec latet latet dashboard --print-url`, dann den Link auf Ihrem Rechner öffnen |
| Alles prüfen | `docker exec latet latet doctor` |
| Das Namensmodell herunterladen (etwa 1,6 GB, einmalig) | `docker exec latet latet ner install --yes` |
| Die Modelle für die Texterkennung herunterladen (etwa 31 MB, einmalig) | `docker exec latet latet ocr install --yes` |
| Ihre eigenen Erkenner | `docker exec -i latet latet rules add - < rules.yaml` |
| Aktualisieren | `docker compose pull && docker compose up -d` |
| Ein anderer Port | `LATET_PORT=6655 docker compose up -d`, dann erneut `agents merge` |
| Eine Sicherung der Einstellungen und des Tresors | `docker exec -it latet latet backup /data/latet.backup --vault`, dann `docker cp latet:/data/latet.backup .` ([Wiederherstellung](../../concepts/export-import/#in-docker)) |

Die Einstellungen liegen in `config.yaml` im Volume `latet-config`; die Seite **Regeln** des
Dashboards ändert sie, und `docker exec latet latet reload` übernimmt eine Datei, die Sie bearbeitet
haben.

## Projekte und der lokale Prüfer

- **[Projekte](../../concepts/projects/):** Ihre eigenen Einstellungen für ein Projekt richten sich
  nach dem Pfad seines Ordners. Die `latet.yaml` eines Projekts wird nur gelesen, wenn der Ordner
  unter demselben Pfad in den Container eingebunden ist.
- **Der [lokale Prüfer](../../concepts/llm-review/)** läuft auf Ihrem Rechner, außerhalb des
  Containers, und im Container ist `127.0.0.1` der Container selbst. Setzen Sie seine Adresse auf
  `http://host.docker.internal:8080/v1`, die Latet nur in seinem Container akzeptiert. Unter Linux
  fügen Sie dem Dienst in `compose.yaml` `extra_hosts: ["host.docker.internal:host-gateway"]` hinzu
  und lassen den Modellserver auf einer Adresse lauschen, die der Container erreichen kann.

## Sicherheit

- **Veröffentlichen Sie den Port nur auf `127.0.0.1`.** Unter Linux umgehen die von Docker
  veröffentlichten Ports die Firewall: `-p 5283:5283` würde Latet und die Anfragen, die Ihre Agenten
  darüber senden, für Ihr Netzwerk öffnen. `compose.yaml` und der Befehl oben veröffentlichen auf
  `127.0.0.1`.
- Andere Container im selben Docker-Netzwerk können den Port erreichen. Die Bridge, das Dashboard
  und die Admin-Routen verlangen trotzdem das Token oder eine Dashboard-Sitzung, und jede Anfrage
  muss `127.0.0.1` oder `localhost` als Host nennen; die Proxy-Routen leiten die eigenen Zugangsdaten
  des Aufrufers weiter.
- Wer das Volume `latet-data` lesen kann, kann den Tresor lesen: Es enthält den Schlüssel, mit dem
  der Tresor verschlüsselt ist. Um den Schlüssel woanders aufzubewahren, übergeben Sie ihn als
  Docker-Secret und setzen `LATET_ROOT_KEY_FILE` auf seinen Pfad.

## Hinter einem Proxy

Wenn die Anbieter über einen Proxy Ihres Netzwerks erreicht werden, setzen Sie `HTTPS_PROXY` (und
`NO_PROXY`) vor `docker compose up`; `compose.yaml` reicht sie weiter. Wenn der Proxy TLS mit einem
eigenen Zertifikat inspiziert, binden Sie dieses Zertifikat in den Container ein und setzen
`SSL_CERT_FILE` darauf.

## Rückgängig machen

Dieser Befehl nimmt die Einstellungen von Claude Code wieder heraus (ebenso für `codex` und
`opencode`, mit ihren Dateien):

```sh
docker exec -i latet latet agents merge claude --remove < ~/.claude/settings.json > ~/.claude/settings.json.new \
  && mv ~/.claude/settings.json.new ~/.claude/settings.json
```

`claude plugin uninstall latet@latet` entfernt das Plugin. Dann `docker compose down`;
`docker compose down -v` löscht auch die Volumes und mit ihnen den Tresor.
