---
title: Desinstalación
description: Quita Latet de forma limpia, en el orden correcto, para que tus agentes sigan funcionando.
---

El orden importa: deshaz la configuración de los agentes **antes** de quitar Latet. Si no, tus
agentes siguen enviando su tráfico a `127.0.0.1:5283` y fallan con errores de conexión cuando el
daemon ya no está.

## 1. Desactiva Latet

```sh
latet global-uninstall
```

Este único comando:

- restaura cada agente que configuró Latet. En Claude Code desinstala el plugin y deja `settings.json`
  como estaba antes de `latet global-install`. En Codex quita la línea `openai_base_url`; en
  OpenCode, las sobrescrituras de `baseURL`. Si has editado alguno de estos archivos desde entonces,
  Latet solo quita lo que añadió y conserva tus cambios;
- quita el inicio al arrancar la sesión;
- detiene el daemon.

No cambia las herramientas que apuntaste a Latet tú mismo, en sus propios ajustes o en tu shell:
GitHub Copilot, Zed, Cline, Kilo Code, Continue, Crush, Prime Agent, Junie, Android Studio, Hermes
Agent, OpenClaw, Goose, OpenHands, Aider, y una línea de `latet env` en el perfil de tu shell.
Vuelve a poner en cada una su propia dirección, o fallarán con errores de conexión cuando el daemon
esté detenido.

Muestra lo que ha hecho con cada parte, por ejemplo:

```text
Claude Code: restored
Codex: restored
OpenCode: not applied
autostart: removed
daemon: stopped
```

Después reinicia tus agentes. Para restaurar en cambio un solo agente, ejecuta
`latet agents revert claude` (o `codex`, `opencode`).

## 2. Quita la herramienta

```sh
uv tool uninstall latet
```

Si ejecutabas Latet desde un clon del repositorio, no hay nada que desinstalar: ejecuta
`uv run latet global-uninstall` en el paso 1 y después borra el clon. Si lo instalaste con
`py -m pip`, ejecuta `py -m latet global-uninstall` en el paso 1 y después `py -m pip uninstall latet`.

## 3. Borra los datos locales (opcional)

Para conservar tus ajustes y la caja fuerte, para más adelante o para otro equipo, guárdalos antes:
`latet backup latet.backup --vault` (consulta
[Exportar e importar](../../concepts/export-import/#tu-instalación-para-otro-equipo)).

Latet guarda sus datos solo en tu equipo, en dos carpetas:

| | Windows | macOS | Linux |
| --- | --- | --- | --- |
| Datos | `%LOCALAPPDATA%\latet` | `~/Library/Application Support/latet` | `~/.local/share/latet` |
| Configuración (`config.yaml`) | `%APPDATA%\latet` | `~/Library/Application Support/latet` | `~/.config/latet` |

Si has definido `LATET_DATA_DIR` o `LATET_CONFIG`, mira allí en su lugar. La carpeta de datos
contiene:

- `latet.db`: la caja fuerte, que asocia cada original a su valor ficticio. Los originales están
  cifrados.
- `exchanges.db`: el historial del panel.
- `backups/`: copias de los archivos de configuración de tus agentes de antes de que
  `latet global-install` los cambiara. `latet global-uninstall` las necesita, así que bórralas solo
  después del paso 1. En `backups/before-<versión>-<hora>/` están las copias de la caja fuerte, del
  historial y de la configuración que `latet upgrade` hace antes de cada actualización (las tres más
  recientes).
- `models/`: el modelo local de nombres y los modelos de reconocimiento de texto (unos 1,6 GB y 31 MB),
  que `latet ner remove` y `latet ocr remove` borran.
- `logs/`, `bridge.token`, `integrations.json` (el registro de lo que cambió `latet global-install`),
  `claude-marketplace/` (la copia del plugin de Claude Code), `tutorial.json` (los capítulos del
  [tutorial](../tutorial/) que has terminado), `repositories.json` (las carpetas de los repositorios
  con un `latet.yaml` que Latet ha visto), y `root.key` si tu sistema no tiene almacén de
  credenciales.

La clave que cifra la caja fuerte está en el almacén de credenciales de tu sistema operativo (el
Administrador de credenciales de Windows, el Llavero de macOS o el llavero del escritorio en Linux),
con el nombre de servicio `latet` y la cuenta `root-key`. Borra también esa entrada.

## Si el comando `latet` ya no está

Deshaz los cambios a mano:

- **Claude Code**: ejecuta `claude plugin uninstall latet@latet` y
  `claude plugin marketplace remove latet`. Después quita estas claves del bloque `env` de
  `~/.claude/settings.json` (o `$CLAUDE_CONFIG_DIR/settings.json`): `ANTHROPIC_BASE_URL`,
  `LATET_BRIDGE_URL`, `LATET_BRIDGE_TOKEN`, `ENABLE_TOOL_SEARCH` y
  `CLAUDE_CODE_AUTO_MODE_SERVER`. Conserva las que ya hubieras definido tú antes. Quita también
  `LATET_BRIDGE_HELPER`, `NODE_EXTRA_CA_CERTS`, `USER` y `USERNAME` si los definió Latet, y vuelve a
  poner como estaba en la copia de `settings.json` de `backups/` cualquier dirección que apunte a
  `127.0.0.1` (`ANTHROPIC_BEDROCK_BASE_URL`, `ANTHROPIC_VERTEX_BASE_URL`,
  `ANTHROPIC_FOUNDRY_BASE_URL`).
- **Codex**: quita la línea `openai_base_url` de `~/.codex/config.toml` (o
  `$CODEX_HOME/config.toml`) y, para un proveedor propio, vuelve a poner el `base_url` de su sección
  `[model_providers.<id>]`, en `config.toml` o en el `<name>.config.toml` del perfil, tomándolo de la
  copia de `backups/`.
- **OpenCode**: quita de `~/.config/opencode/opencode.jsonc` (o `opencode.json`) las entradas
  `baseURL` que apuntan a `127.0.0.1:5283`.
- **Otros clientes**: elimina las variables `OPENAI_BASE_URL`, `OPENAI_API_BASE`, `ANTHROPIC_BASE_URL`,
  `GOOGLE_GEMINI_BASE_URL` y `GOOGLE_VERTEX_BASE_URL`, o vuelve a poner la URL base que usabas antes.
  En las herramientas que apuntaste a Latet en sus propios ajustes, vuelve a poner su propia dirección.
- **Inicio al arrancar la sesión**: en Windows, borra el valor `Latet` de
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. En macOS, ejecuta
  `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/dev.latet.daemon.plist` y después borra
  ese archivo. En Linux, ejecuta `systemctl --user disable latet.service` y después borra
  `~/.config/systemd/user/latet.service`.

Las copias de tus archivos de configuración de antes de `latet global-install` están en la carpeta
`backups/` de la carpeta de datos.
