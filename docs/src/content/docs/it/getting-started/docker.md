---
title: Docker
description: Esegui il daemon di Latet in un container, e collegaci i tuoi agenti.
---

Latet gira anche come container: niente Python sul tuo computer, e il daemon tenuto separato dal
resto del computer. L'immagine contiene il daemon, la dashboard e il runtime del
[modello dei nomi](../installation/) e del [riconoscimento del testo](../../concepts/images/),
entrambi facoltativi; i loro modelli vengono scaricati solo se lo chiedi. I tuoi agenti restano sul
tuo computer e comunicano con il container su `127.0.0.1`.

Il container non può cambiare le impostazioni dei tuoi agenti né vedere chi sei, quindi due passaggi che
`latet global-install` fa da solo qui si fanno a mano: collegare gli agenti al container, e comunicargli la
tua identità. Entrambi sono un copia e incolla.

## Avvialo

Salva [`compose.yaml`](https://github.com/Latet-AI/latet/blob/main/compose.yaml) dal repository,
poi:

```sh
docker compose up -d
```

Senza Compose, la stessa cosa in un solo comando:

```sh
docker run -d --name latet --restart unless-stopped -p 127.0.0.1:5283:5283 \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true --init \
  -v latet-data:/data -v latet-config:/config ghcr.io/latet-ai/latet:latest
```

`docker exec latet latet status` dice se è attivo. Il container gira come utente senza privilegi, su
un file system di sola lettura, e tiene tutto in due volumi: `latet-data` (la cassaforte, la sua
chiave e la cronologia della dashboard) e `latet-config` (`config.yaml`).

## Collega i tuoi agenti

```sh
docker exec latet latet agents print
```

Stampa, per ogni agente, cosa cambiare e un comando che lo fa. Su Windows, aggiungi
`--shell powershell`. Se il file delle impostazioni di un agente non esiste ancora, crealo prima
vuoto, per esempio con `mkdir -p ~/.codex && touch ~/.codex/config.toml`. In breve:

- **Claude Code** ha bisogno del plugin e di alcune variabili in `~/.claude/settings.json`:

  ```sh
  claude plugin marketplace add Latet-AI/latet
  claude plugin install latet@latet
  docker exec -i latet latet agents merge claude < ~/.claude/settings.json > ~/.claude/settings.json.new \
    && mv ~/.claude/settings.json.new ~/.claude/settings.json
  ```

  `agents merge` legge il file, aggiunge le variabili e lo riscrive, così il token che il plugin
  mostra al daemon finisce nel file senza essere stampato. Una delle variabili,
  `LATET_AUTOSTART=0`, dice al plugin di non avviare un proprio daemon: se il container non è
  attivo, Claude Code lo segnala e trattiene le richieste finché non torna attivo.
- **Codex** ha bisogno di `openai_base_url` in `~/.codex/config.toml`, che questo comando scrive
  (`--codex-login api-key` se Codex accede con una chiave API invece che con ChatGPT):

  ```sh
  docker exec -i latet latet agents merge codex < ~/.codex/config.toml > ~/.codex/config.toml.new \
    && mv ~/.codex/config.toml.new ~/.codex/config.toml
  ```

- **OpenCode** ha bisogno di un `baseURL` per ogni provider in `opencode.json`, allo stesso modo.

PowerShell non ha la redirezione `<`, quindi `agents print --shell powershell` copia il file nel
volume `latet-data` del container con `docker cp`, lo unisce lì e lo copia indietro.

Poi riavvia gli agenti. Per questi percorsi le [modalità](../../concepts/how-it-works/) e la
protezione sono le stesse di quando il daemon è installato sul tuo computer: Anthropic per Claude
Code, OpenAI o ChatGPT per Codex, e i provider di OpenCode. Claude Code su Bedrock, Vertex AI o
Microsoft Foundry, un provider di Codex tuo e il gateway di un'azienda li configurano
`latet claude`, `latet codex` e `latet global-install`, che girano sul tuo computer: per quei casi
usa Latet installato sul tuo computer.

## Digli chi sei

Latet maschera il tuo nome utente nei percorsi, il tuo nome host e il tuo autore git. Nel container
non può vederli, quindi comunicaglieli una volta sola, dal tuo computer (lo trovi anche in
`agents print`):

```sh
docker exec latet latet identity set --user-name "$(id -un)" --user-name "$(basename "$HOME")" \
  --host-name "$(hostname)" --full-name "$(git config --global user.name)" \
  --email "$(git config --global user.email)"
```

`docker exec latet latet identity show` stampa ciò che maschera.

## Uso quotidiano

| Cosa | Comando |
| --- | --- |
| Apri la dashboard | `docker exec latet latet dashboard --print-url`, poi apri il link sul tuo computer |
| Controlla tutto | `docker exec latet latet doctor` |
| Scarica il modello dei nomi (circa 1,6 GB, una volta sola) | `docker exec latet latet ner install --yes` |
| Scarica i modelli di riconoscimento del testo (circa 31 MB, una volta sola) | `docker exec latet latet ocr install --yes` |
| I tuoi riconoscitori | `docker exec -i latet latet rules add - < rules.yaml` |
| Aggiorna | `docker compose pull && docker compose up -d` |
| Un'altra porta | `LATET_PORT=6655 docker compose up -d`, poi di nuovo `agents merge` |
| Un backup delle impostazioni e della cassaforte | `docker exec -it latet latet backup /data/latet.backup --vault`, poi `docker cp latet:/data/latet.backup .` ([per ripristinarlo](../../concepts/export-import/#in-docker)) |

Le impostazioni stanno in `config.yaml` nel volume `latet-config`; la pagina **Regole** della
dashboard le modifica, e `docker exec latet latet reload` applica un file che hai modificato.

## I progetti e il revisore locale

- **[Progetti](../../concepts/projects/):** le tue impostazioni per un progetto funzionano in base al
  percorso della sua cartella. Il `latet.yaml` di un progetto viene letto solo quando la cartella è
  montata nel container allo stesso percorso.
- **Il [revisore locale](../../concepts/llm-review/)** gira sul tuo computer, fuori dal container, e
  nel container `127.0.0.1` è il container stesso. Imposta il suo indirizzo su
  `http://host.docker.internal:8080/v1`, che Latet accetta solo nel suo container. Su Linux,
  aggiungi `extra_hosts: ["host.docker.internal:host-gateway"]` al servizio in `compose.yaml`, e fai
  in modo che il server del modello sia in ascolto su un indirizzo che il container può raggiungere.

## Sicurezza

- **Pubblica la porta solo su `127.0.0.1`.** Su Linux, le porte pubblicate da Docker aggirano il
  firewall: `-p 5283:5283` aprirebbe alla tua rete Latet e le richieste che i tuoi agenti inviano
  attraverso di esso. `compose.yaml` e il comando qui sopra pubblicano su `127.0.0.1`.
- Gli altri container sulla stessa rete Docker possono raggiungere la porta. Il bridge, la dashboard
  e i percorsi di amministrazione richiedono comunque il token o una sessione della dashboard, e ogni
  richiesta deve indicare `127.0.0.1` o `localhost` come host; i percorsi del proxy inoltrano le
  credenziali del chiamante stesso.
- Chi può leggere il volume `latet-data` può leggere la cassaforte: contiene la chiave con cui la
  cassaforte è cifrata. Per tenere la chiave altrove, passala come Docker secret e imposta
  `LATET_ROOT_KEY_FILE` sul suo percorso.

## Dietro un proxy

Quando i provider si raggiungono tramite un proxy della tua rete, imposta `HTTPS_PROXY` (e
`NO_PROXY`) prima di `docker compose up`; `compose.yaml` li passa al container. Se il proxy
ispeziona il TLS con un proprio certificato, monta quel certificato nel container e imposta
`SSL_CERT_FILE` su di esso.

## Annulla tutto

Questo comando toglie le impostazioni di Claude Code (lo stesso per `codex` e `opencode`, con i loro
file):

```sh
docker exec -i latet latet agents merge claude --remove < ~/.claude/settings.json > ~/.claude/settings.json.new \
  && mv ~/.claude/settings.json.new ~/.claude/settings.json
```

`claude plugin uninstall latet@latet` rimuove il plugin. Poi `docker compose down`;
`docker compose down -v` elimina anche i volumi, e con essi la cassaforte.
