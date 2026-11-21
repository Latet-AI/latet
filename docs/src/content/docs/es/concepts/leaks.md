---
title: Secretos en las sesiones guardadas
description: latet leak lee las sesiones que tus agentes guardaron en este equipo, enumera los secretos que contienen sin mostrar ningún valor y dice cuáles pueden haber llegado al proveedor del modelo.
---

Tus agentes de programación guardan cada conversación en el disco, entera: los prompts que
escribiste, los archivos que leyeron, la salida de los comandos que ejecutaron. Un secreto que
entró en una conversación antes de que instalaras Latet, o en una sesión que no pasó por él, está en
esos archivos. El [panel](../../dashboard/) no puede mostrarlo: solo ve las peticiones que pasaron
por Latet, y solo mientras su historial las conserva (14 días de forma predeterminada). `latet leak`
lee las sesiones guardadas con los detectores de Latet y enumera los secretos que contienen, para
que sepas qué claves cambiar y qué conversaciones no retomar.

```sh
latet leak
```

El comando lee y muestra, y nunca enseña un valor. No modifica ninguno de tus archivos, no necesita
red ni un daemon en marcha, y la caja fuerte de Latet no aprende nada de él: los valores que
encuentra se quedan en su memoria mientras lee, y no más. Como no muestra ningún valor, también
puede ejecutarlo un agente.

## Dónde guardan tus agentes sus sesiones

`~` es tu carpeta personal (`C:\Users\you` en Windows). Cuando la variable de un agente está
definida, sus sesiones están en la carpeta que indica, y `latet leak` las lee allí.

| Agente | Sus sesiones guardadas | La variable que las traslada |
| --- | --- | --- |
| [Claude Code](../../agents/claude-code/) | `~/.claude/projects/`: un archivo `.jsonl` por sesión y por subagente | `CLAUDE_CONFIG_DIR`, en lugar de `~/.claude` |
| [Codex](../../agents/codex/) | `~/.codex/sessions/` y `~/.codex/archived_sessions/` | `CODEX_HOME`, en lugar de `~/.codex` |
| [OpenCode](../../agents/opencode/) | `~/.local/share/opencode/opencode.db`, y la antigua carpeta `storage` a su lado | `XDG_DATA_HOME`, en lugar de `~/.local/share` |
| [Copilot CLI](../../agents/other-tools/#github-copilot-cli) | `~/.copilot/session-state/`: un archivo `events.jsonl` por sesión | `COPILOT_HOME`, en lugar de `~/.copilot` |
| [Gemini CLI](../../agents/gemini/) | `~/.gemini/tmp/`: la carpeta `chats` de cada proyecto | `GEMINI_CLI_HOME`, en lugar de `~` |
| [Qwen Code](../../agents/other-tools/#qwen-code) | `~/.qwen/projects/`: la carpeta `chats` de cada proyecto; y la antigua `~/.qwen/tmp/` | `QWEN_HOME`, en lugar de `~/.qwen` |
| [Hermes Agent](../../agents/autonomous-agents/#hermes-agent) | `state.db` en `~/.hermes` y, en Windows, en `%LOCALAPPDATA%\hermes` | `HERMES_HOME`, la carpeta que contiene `state.db` |

`--agent` lee solo los agentes que nombras, con estos nombres: `claude`, `codex`, `opencode`,
`copilot`, `gemini`, `qwen` y `hermes`; repítelo para leer más. `--path` lee además un archivo, o
los archivos `.json`, `.jsonl` y `.ndjson` de una carpeta y sus subcarpetas, de la misma manera: por
ejemplo las sesiones de un agente que esta tabla no incluye, o una carpeta de sesiones copiada de
otro equipo.

**Qué lee.** En cada registro, cada texto: tus prompts, las respuestas del modelo, las llamadas a
herramientas y sus resultados, también un texto que es a su vez JSON. No los identificadores, las
marcas de tiempo ni las firmas, que no contienen nada de la conversación. Cada texto lo leen tus
propios detectores con tus ajustes globales, como Latet lee la salida de una herramienta: las reglas
integradas, tus términos y tus reconocedores, cada tipo con su política. Un valor que tus ajustes
dejan pasar no aparece en la lista. No se usan ni el modelo de nombres local ni el revisor local, y
no se lee el `latet.yaml` de un proyecto.

De forma predeterminada solo busca secretos: claves, tokens, contraseñas, claves privadas,
credenciales en una dirección web. `--personal` cuenta además los datos personales, como direcciones
de correo y números de teléfono, por tipo: cuántas sesiones contienen cada tipo, nunca un valor.

## Qué muestra

Un ejemplo, con carpetas y cifras inventadas:

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

El mismo secreto pegado en varias sesiones ocupa una línea, con el número de sesiones. Lo mismo vale
para un secreto que los detectores encontraron con dos tipos, como un token de GitHub en la cabecera
de un comando `curl` y asignado a una variable: la línea muestra el tipo del proveedor. Un secreto
puede estar en dos listas, como el token de GitHub de arriba: pegado antes de que Latet estuviera, y
otra vez después.

### En claro

Nada indica que Latet los sustituyera: pueden haber llegado al proveedor del modelo (*no sign that
Latet replaced them: they may have reached the provider*). Una sesión cuenta aquí cuando contenía el
valor antes de que la caja fuerte de Latet lo guardara por primera vez, o cuando la caja fuerte no
conoce el valor en absoluto. Otros dos casos cuentan aquí: una sesión cuyos registros no tienen
hora, que no se puede situar ni antes ni después de esa fecha, y una sesión que, según el historial
de Latet, envió primero el valor en claro: el historial aún guarda las primeras peticiones de la
sesión, y solo muestra el valor enmascarado en peticiones posteriores. Estos son los secretos que
hay que cambiar. Debajo de la lista, el comando dice desde qué fecha puede saberlo Latet: la fecha
más antigua que su caja fuerte y su historial conservan en este equipo.

### Sustituidos por Latet

La caja fuerte ya conocía el valor cuando la sesión lo contenía: donde la sesión pasó por Latet, el
proveedor del modelo recibió por tanto el valor ficticio de Latet. El valor está igualmente en el
archivo de la sesión, porque el agente guarda lo que tenía antes de que Latet lo sustituyera al
salir. No hay nada que cambiar por el proveedor del modelo, aunque el valor está en un archivo de
este equipo.

Cada secreto dice cuántas de sus sesiones están *confirmed by Latet's history*, confirmadas por el
historial de Latet: el historial aún guarda una petición de la sesión en la que el valor se
enmascaró. Para eso hace falta un historial que se guarde entero (*Todo*, el ajuste predeterminado)
y que aún contenga la sesión (14 días de forma predeterminada), y un agente que nombre su sesión en
sus peticiones, como hacen Claude Code, Codex y Hermes Agent. Cualquier otra sesión cuenta *if the
session went through Latet*, solo si pasó por Latet: un agente que entonces no estaba configurado
para Latet envió el valor en claro, y solo el historial puede saberlo. Cuando un secreto tiene una
sesión así, el encabezado dice *Latet replaced, or may have*.

### Suprimidos

Los secretos que tus ajustes suprimen, o que Latet suprime porque son demasiado largos para
guardarlos (*your settings redact, or Latet redacts as too long to keep*): un secreto de un tipo que
tus [políticas](../profiles/#políticas) ponen en `redact`, o uno demasiado largo para la caja fuerte
(más de 16.384 caracteres). Latet sustituye ese valor por un marcador y no lo guarda en ningún
sitio, así que su caja fuerte no tiene fecha para él. Las sesiones desde la fecha más antigua que
Latet tiene en este equipo aparecen aquí, las anteriores en claro. Tus ajustes pueden haber sido
otros entonces, y puede que la sesión no pasara por Latet: por eso el encabezado dice que Latet
puede haber enviado un marcador en su lugar (*may have sent a marker*).

### Cada lista

Bajo su encabezado, una lista cuenta sus secretos por tipo: los seis tipos con más, y luego los
demás juntos. Después vienen los secretos, primero las claves de un proveedor (un token de GitHub,
una clave de Anthropic, una clave de AWS: las que conviene cambiar primero), luego los que están en
más sesiones. Una lista muestra los 20 primeros y dice cuántos más tiene; `--all` los enumera todos.

Un secreto muestra su tipo, una pista y, para las sesiones de esa lista, los agentes, el número de
sesiones, las carpetas de proyecto (tres como mucho, y luego cuántas más) y las fechas en que se vio
por primera y por última vez, según el reloj de este equipo, como `--since` lee una fecha. La
pista solo conserva el prefijo público que el proveedor pone delante de cada clave de su tipo
(`ghp_…`, `sk-ant-api03-…`, `AKIA…`), el mismo que conserva un valor ficticio, y la longitud; un
secreto sin ese prefijo muestra su tipo y su longitud (`Password (17 characters)`).

Al final vienen los datos personales con `--personal`, qué hacer, y lo que no se pudo leer, con el
motivo, nunca su contenido: un archivo que no se puede abrir, un archivo JSON de más de 32 MB, un
solo texto de más de 8 MB, una base de datos que cambió cada vez que se copió.

**Sin las respuestas de Latet.** En un equipo conectado a un [servidor de Latet](../server/), la
caja fuerte y el historial son los del servidor. El comando dice entonces que no pudo saber qué
secretos sustituyó Latet, y enumera cada secreto en claro, salvo los que enumera como suprimidos.
Hace lo mismo, y dice por qué, cuando no se pudo leer la caja fuerte de Latet en este equipo, o su
clave, o ninguna clave encontrada aquí la abre. En un equipo sin caja fuerte de Latet ni servidor de
Latet, Latet no sustituyó nada: el comando lo dice, y enumera cada secreto en claro, también los de
los tipos suprimidos.

## Opciones

| Opción | Qué hace |
| --- | --- |
| `--agent NAME` | Leer solo las sesiones de este agente: `claude`, `codex`, `opencode`, `copilot`, `gemini`, `qwen` o `hermes`. Repítela para leer más. |
| `--since DATE` | Leer los registros desde esta fecha (`2026-10-01`), según la hora de cada registro, o si no, la última modificación de su archivo. |
| `--personal` | Contar además los datos personales, por tipo: cuántas sesiones contienen cada tipo. |
| `--path PATH` | Leer además este archivo, o los archivos JSON y JSON Lines de esta carpeta. Repítela para leer más. |
| `--json` | Mostrar un documento JSON en lugar de texto, con cada secreto. |
| `--all` | Enumerar cada secreto de cada lista, no solo los 20 primeros. |

`--json` da un registro por secreto: su `type` (el que muestra el texto), sus `types` (cada tipo con
el que se encontró), su `class`, su `hint`, sus `agents` y sus `folders`, y sus sesiones `in_clear`,
`replaced` y `redacted`, cada una con cuántas son, la primera y la última fecha, cuántas están
confirmadas, y los `agents` y `folders` de esas sesiones. A su lado: las sesiones leídas por agente,
los datos personales por tipo, lo que no se pudo leer (`skipped`) y cuántas cosas son (`unread`),
`latet_answers` (si se pudo preguntar a la caja fuerte y al historial de Latet) y `latet_since` (la
fecha más antigua que conservan, que se da cuando hay un secreto en claro).

El código de salida es `0` cuando no encuentra nada, `1` cuando encuentra un secreto, en cualquier
lista, o datos personales con `--personal`, y `2` cuando no pudo leer ninguna sesión o una opción es
incorrecta. Un script puede ejecutarlo y actuar según eso.

## Cuánto tarda

`latet leak` lee cada texto de cada sesión guardada, y eso lleva tiempo cuando hay muchas: unos
11 minutos para 10 GB de sesiones en un PC de sobremesa. En una consola muestra en una línea el
agente que está leyendo y cuántas de sus sesiones ha leído ya, y luego cuánto tardó; pasado un
minuto, dice que `--agent` y `--since` leen menos:

```sh
latet leak --agent claude --since 2026-10-01
```

Una sesión se juzga por los registros leídos: con `--since`, un secreto que un registro anterior de
la misma sesión envió en claro puede aparecer como sustituido.

Esas líneas van a la salida de error estándar, y solo a una consola: a la salida estándar solo va el
informe, y con `--json` sigue siendo allí un único documento JSON.

## Lo que no puede saber

- **Antes de la fecha de Latet.** Latet conoce un valor desde el día en que su caja fuerte lo guardó
  por primera vez, y una sesión que contenía el valor antes de ese día aparece en claro. Una caja
  fuerte vaciada, un valor que quitas con *Olvidar* (la caja fuerte lo vuelve a conocer desde que
  reaparece) o un historial más allá de los días que conserva no guardan nada más antiguo: una
  sesión que aparece en claro y es anterior a la fecha que figura bajo la lista puede haber pasado
  por Latet de todos modos.
- **Una sesión que no pasó por Latet.** Un secreto que aparece como sustituido, en una sesión que el
  historial no confirma, puede haber salido sin pasar por Latet: solo el historial puede saberlo,
  durante los días que conserva.
- **Lo que Claude Code guarda para su propia pantalla.** Claude Code guarda una segunda copia del
  resultado de una herramienta para mostrarla él mismo (`toolUseResult`): un archivo que leyó, el
  archivo entero de una edición, la salida de un comando. Esa copia nunca llega al proveedor del
  modelo, y `latet leak` no la lee. Sí lee la copia que llevaba la petición.
- **Un secreto partido en dos.** Cada texto se lee por separado: un secreto repartido entre dos
  registros no se encuentra, y un valor que un texto permite reconocer por la palabra que lo precede
  (`password:`) no se encuentra donde otro texto lo escribe solo.
- **Lo que los detectores no reconocen,** igual que al enmascarar con Latet
  ([No protegido](../protection-scope/#no-protegido)). Tampoco se encuentra un valor que Latet solo
  conoce por su caja fuerte, porque lo encontró el revisor local o tú: el análisis parte de una caja
  fuerte propia, vacía.

Una base de datos en modo write-ahead, junto a la que una simple lectura añadiría archivos, se lee
desde una copia privada en una carpeta temporal, que se borra después; la copia solo se hace donde
queda al menos 1 GB libre en el disco tras ella. Esto vale para las bases de datos de OpenCode y
de Hermes Agent, y para la caja fuerte y el historial del propio Latet.

## Qué hacer con un hallazgo

- **Cambia cada secreto en claro en el proveedor de la clave,** primero las claves de un proveedor,
  en el orden de la lista: crea una clave nueva donde el proveedor las emite (GitHub, Anthropic,
  AWS...), úsala en lugar de la antigua y revoca la antigua; cambia una contraseña. Lo que llegó a un
  proveedor de modelos puede quedarse en sus registros, borres lo que borres en tu equipo. El tipo,
  el prefijo, las carpetas y las fechas te dicen qué clave es y dónde se usó.
- **No retomes esas conversaciones.** Una conversación retomada vuelve a enviar todos sus mensajes,
  y el secreto con ellos: empieza una nueva.
- **Los secretos que Latet sustituyó** no necesitan nada en el proveedor de la clave donde la sesión
  pasó por Latet: el proveedor del modelo recibió un valor ficticio. **Puede que los suprimidos no
  necesiten nada:** si la sesión pasó por Latet, el proveedor del modelo recibió un marcador en su
  lugar.
- **Los archivos del disco son de tu agente.** Latet no borra ni mueve ninguno. Para quitar un valor
  de este equipo, borra o mueve los archivos de sesión que lo contienen, con el agente cerrado: el
  agente, la carpeta del proyecto y las fechas de la lista te dicen dónde buscar. Una conversación
  cuyo archivo borras ya no se puede retomar.

Un secreto que ya cambiaste sigue en la lista mientras sus archivos de sesión estén ahí: la lista
dice lo que contienen los archivos, no si una clave sigue siendo válida.
