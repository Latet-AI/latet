---
title: Docker
description: Ejecuta el daemon de Latet en un contenedor y conecta tus agentes a él.
---

Latet también se ejecuta como contenedor: sin Python en tu equipo, y con el daemon separado del
resto del equipo. La imagen contiene el daemon, el panel y el entorno de ejecución del
[modelo de nombres](../installation/) y del [reconocimiento de texto](../../concepts/images/), ambos
opcionales; sus modelos solo se descargan si lo pides. Tus agentes se quedan en tu equipo y se
comunican con el contenedor en `127.0.0.1`.

El contenedor no puede cambiar los ajustes de tus agentes ni ver quién eres, así que dos pasos que
`latet global-install` hace por sí solo aquí se hacen a mano: conectar los agentes al contenedor, y
comunicarle tu identidad. Los dos consisten en copiar y pegar.

## Inícialo

Guarda [`compose.yaml`](https://github.com/Latet-AI/latet/blob/main/compose.yaml), del repositorio,
y después:

```sh
docker compose up -d
```

Sin Compose, lo mismo en un solo comando:

```sh
docker run -d --name latet --restart unless-stopped -p 127.0.0.1:5283:5283 \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true --init \
  -v latet-data:/data -v latet-config:/config ghcr.io/latet-ai/latet:latest
```

`docker exec latet latet status` indica si está en marcha. El contenedor se ejecuta como un usuario
sin privilegios, en un sistema de archivos de solo lectura, y lo guarda todo en dos volúmenes:
`latet-data` (la caja fuerte, su clave y el historial del panel) y `latet-config` (`config.yaml`).

## Conecta tus agentes

```sh
docker exec latet latet agents print
```

Muestra, para cada agente, qué cambiar y un comando que lo hace. En Windows, añade
`--shell powershell`. Si el archivo de ajustes de un agente todavía no existe, créalo antes vacío, por
ejemplo `mkdir -p ~/.codex && touch ~/.codex/config.toml`. En resumen:

- **Claude Code** necesita el plugin y algunas variables en `~/.claude/settings.json`:

  ```sh
  claude plugin marketplace add Latet-AI/latet
  claude plugin install latet@latet
  docker exec -i latet latet agents merge claude < ~/.claude/settings.json > ~/.claude/settings.json.new \
    && mv ~/.claude/settings.json.new ~/.claude/settings.json
  ```

  `agents merge` lee el archivo, añade las variables y lo vuelve a escribir, así que el token que el
  plugin muestra al daemon entra en el archivo sin aparecer en pantalla. Una de las variables,
  `LATET_AUTOSTART=0`, le dice al plugin que no inicie un daemon propio: si el contenedor no está en
  marcha, Claude Code lo indica y retiene las peticiones hasta que vuelva a estarlo.
- **Codex** necesita `openai_base_url` en `~/.codex/config.toml`, que esto escribe
  (`--codex-login api-key` si Codex inicia sesión con una clave de API en lugar de con ChatGPT):

  ```sh
  docker exec -i latet latet agents merge codex < ~/.codex/config.toml > ~/.codex/config.toml.new \
    && mv ~/.codex/config.toml.new ~/.codex/config.toml
  ```

- **OpenCode** necesita una `baseURL` para cada proveedor en `opencode.json`, de la misma forma.

PowerShell no tiene la redirección `<`, así que `agents print --shell powershell` copia el archivo al
volumen `latet-data` del contenedor con `docker cp`, lo fusiona allí y lo copia de vuelta.

Después reinicia los agentes. Para estas rutas, los [modos](../../concepts/how-it-works/) y la
protección son los mismos que con el daemon instalado en tu equipo: Anthropic para Claude Code, OpenAI
o ChatGPT para Codex, y los proveedores de OpenCode. Claude Code en Bedrock, Vertex AI o Microsoft
Foundry, un proveedor propio de Codex y la puerta de enlace de una empresa los configuran
`latet claude`, `latet codex` y `latet global-install`, que se ejecutan en tu equipo: para ellos usa
Latet instalado en tu equipo.

## Dile quién eres

Latet enmascara tu nombre de usuario en las rutas, tu nombre de host y tu autor de git. En el
contenedor no puede verlos, así que díselos una vez, desde tu equipo (esto también aparece en
`agents print`):

```sh
docker exec latet latet identity set --user-name "$(id -un)" --user-name "$(basename "$HOME")" \
  --host-name "$(hostname)" --full-name "$(git config --global user.name)" \
  --email "$(git config --global user.email)"
```

`docker exec latet latet identity show` muestra lo que enmascara.

## Uso diario

| Qué | Comando |
| --- | --- |
| Abrir el panel | `docker exec latet latet dashboard --print-url`, y después abre el enlace en tu equipo |
| Comprobarlo todo | `docker exec latet latet doctor` |
| Descargar el modelo de nombres (unos 1,6 GB, una vez) | `docker exec latet latet ner install --yes` |
| Descargar los modelos de reconocimiento de texto (unos 31 MB, una vez) | `docker exec latet latet ocr install --yes` |
| Tus propios reconocedores | `docker exec -i latet latet rules add - < rules.yaml` |
| Actualizar | `docker compose pull && docker compose up -d` |
| Otro puerto | `LATET_PORT=6655 docker compose up -d`, y después de nuevo `agents merge` |
| Una copia de seguridad de los ajustes y de la caja fuerte | `docker exec -it latet latet backup /data/latet.backup --vault`, y después `docker cp latet:/data/latet.backup .` ([cómo restaurarla](../../concepts/export-import/#en-docker)) |

Los ajustes están en `config.yaml`, en el volumen `latet-config`; la página **Reglas** del panel los
cambia, y `docker exec latet latet reload` aplica un archivo que hayas editado.

## Los proyectos y el revisor local

- **[Proyectos](../../concepts/projects/):** tus propios ajustes para un proyecto funcionan según la
  ruta de su carpeta. El `latet.yaml` de un proyecto solo se lee cuando la carpeta está montada en el
  contenedor en la misma ruta.
- **El [revisor local](../../concepts/llm-review/)** se ejecuta en tu equipo, fuera del contenedor,
  y dentro del contenedor `127.0.0.1` es el propio contenedor. Configura su dirección como
  `http://host.docker.internal:8080/v1`, que Latet solo acepta en su contenedor. En Linux, añade
  `extra_hosts: ["host.docker.internal:host-gateway"]` al servicio en `compose.yaml`, y haz que el
  servidor del modelo escuche en una dirección a la que el contenedor pueda llegar.

## Seguridad

- **Publica el puerto solo en `127.0.0.1`.** En Linux, los puertos que publica Docker se saltan el
  cortafuegos: `-p 5283:5283` abriría Latet a tu red, junto con las peticiones que tus agentes
  envían a través de él. `compose.yaml` y el comando de arriba publican en `127.0.0.1`.
- Otros contenedores de la misma red de Docker pueden llegar al puerto. El bridge, el panel y las
  rutas de administración siguen necesitando el token o una sesión del panel, y cada petición debe
  indicar `127.0.0.1` o `localhost` como host; las rutas del proxy reenvían las credenciales del
  propio llamante.
- Quien pueda leer el volumen `latet-data` puede leer la caja fuerte: contiene la clave con la que
  está cifrada. Para guardar la clave en otro sitio, pásala como secreto de Docker y pon su ruta en
  `LATET_ROOT_KEY_FILE`.

## Detrás de un proxy

Cuando a los proveedores se llega a través de un proxy de tu red, define `HTTPS_PROXY` (y
`NO_PROXY`) antes de `docker compose up`; `compose.yaml` los transmite. Si el proxy inspecciona el
TLS con un certificado propio, monta ese certificado en el contenedor y pon su ruta en
`SSL_CERT_FILE`.

## Deshacer

Esto quita los ajustes de Claude Code (lo mismo para `codex` y `opencode`, con sus archivos):

```sh
docker exec -i latet latet agents merge claude --remove < ~/.claude/settings.json > ~/.claude/settings.json.new \
  && mv ~/.claude/settings.json.new ~/.claude/settings.json
```

`claude plugin uninstall latet@latet` quita el plugin. Después, `docker compose down`;
`docker compose down -v` también borra los volúmenes, y con ellos la caja fuerte.
