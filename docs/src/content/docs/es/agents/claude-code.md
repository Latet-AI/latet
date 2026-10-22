---
title: Claude Code
description: El modo cliente para Claude Code, con suscripciones Pro y Max o con claves de API, y qué superficies de Claude están cubiertas.
---

Claude Code usa el **modo cliente**: el plugin de Latet enmascara el contenido dentro de Claude Code, y el
daemon local solo verifica. Funciona con las suscripciones Pro y Max sin clave de API, y también con
claves de API.

## Terminal Windows conectado

El proceso auxiliar conectado al servidor admite Claude Code en un terminal Windows,
con ajustes de usuario de confianza y la vinculación privada instalada por Latet.
Esto no amplía el uso conectado a Desktop, VS Code, JetBrains, Linux ni macOS. En Linux y
macOS, `latet connect`, `latet claude`, `latet agents apply claude` y `latet global-install`
lo dicen de entrada, los ajustes de Claude Code no reciben el token y el plugin retiene cada
prompt; `latet codex` y `latet opencode` pasan por el servidor. Sigue la
[guía de conexión](../../getting-started/server/) para `latet connect`,
`latet agents apply claude`, almacenamiento del token y `latet disconnect`.

En un equipo conectado, `LATET_BRIDGE_TOKEN`, en los ajustes y el entorno de Claude Code, contiene
el token de este equipo, y cada comando que Claude Code ejecuta puede leerlo. Desde cualquier lugar
donde se alcance el servidor, el token abre el bridge del plugin y el proxy de tu espacio: quien lo
tenga puede hacer enmascarar textos en tu espacio, hacer restaurar los valores ficticios que tenga y
enviar peticiones a través de él. Nunca abre tu panel, donde se puede mostrar cada original, nunca
cambia tu identidad y nunca reduce tu protección: el único cambio que puede hacer en tus ajustes es
añadir un destino para una dirección, lo que no reduce nada. Para el panel y la identidad el servidor
pide también la clave del equipo, que se queda en el almacén de credenciales (o en el archivo privado
del token) y nunca se da a un agente. La clave impide que un token filtrado (desde el entorno, los
ajustes, un registro o con un solo `curl`) abra el panel; no detiene un comando escrito para leer los
dos del almacén de credenciales. En un equipo conectado, `latet dashboard` y `latet identity send` se
niegan a ejecutarse dentro de un agente. Si el token puede haberse filtrado, `latet disconnect` lo
revoca.

En un equipo conectado, el modo de impresión (`claude -p`) no se admite: el plugin retiene
sus peticiones. Usa el terminal interactivo. Cuando el plugin retiene una petición dice por
qué: el modo de impresión u otra superficie; una vinculación del proceso auxiliar ausente o
desactualizada (`latet agents apply claude`); un servidor que no respondió (`latet status`);
o un token que el servidor ya no acepta (`latet disconnect` y luego `latet connect` con un
nuevo código de un administrador).
El plugin enmascara las descripciones de las herramientas de Claude Code; en un servidor, la
comprobación final también lee sus descripciones, esquemas de parámetros y ejemplos, y un valor
que quede en claro en ellos detiene la petición ([alcance de la protección](../../concepts/protection-scope/)). Las tablas de cobertura y las
instrucciones del daemon que siguen describen **Latet local**; no amplían este uso
del proceso auxiliar conectado.

## Cobertura

| Superficie | Plugin | Daemon de solo verificación | Notas |
| --- | --- | --- | --- |
| Claude Code CLI (terminal) | ✅ | ✅ | Modo cliente completo |
| Extensiones para VS Code y JetBrains | ✅ | ✅ | El mismo Claude Code, los mismos plugins y ajustes |
| Pestaña Code de Claude Desktop (sesiones locales), con el inicio de sesión de Claude | ✅ | ❌ | Solo el plugin: la aplicación ignora `ANTHROPIC_BASE_URL`, así que ningún daemon comprueba detrás del plugin y no se registra nada en el panel |
| Chat y Cowork de Claude Desktop, con el inicio de sesión de Claude | ❌ | ❌ | No cubiertos: no cargan los plugins de Claude Code y no pasan por Latet |
| Claude Desktop en una puerta de enlace de terceros: Chat, Cowork y Code | Pestaña Code | ✅ | Cada petición pasa por Latet y se comprueba; solo la pestaña Code enmascara |
| Sesiones en la nube | ❌ | ❌ | No cubiertas: se ejecutan en la infraestructura de Anthropic, no en tu equipo |

**Claude Desktop, dos configuraciones.** Con el inicio de sesión de Claude, con una suscripción Pro o
Max, la pestaña Code carga el plugin de Latet, que enmascara el contenido y hace su propia
comprobación final antes de cada petición. Sin embargo, la aplicación ignora `ANTHROPIC_BASE_URL`,
así que ningún daemon comprueba la petición detrás del plugin, y no se registra nada en el panel.
Chat y Cowork no cargan los plugins de Claude Code y no pasan por Latet: no están cubiertos.

En una puerta de enlace de terceros, Claude Desktop funciona sin iniciar sesión en Claude, así que no
hay suscripción Pro ni Max: las peticiones se pagan por uso, con una clave de API de Anthropic o a
través del proveedor de tu empresa. Con Latet como puerta de enlace, Chat, Cowork y Code envían cada
petición a través de él. El motor de Claude Code construye estas peticiones, así que Latet comprueba
cada una y detiene la que llevaría un secreto o datos personales en claro, pero no la enmascara: la
[guía de puertas de enlace](https://code.claude.com/docs/en/llm-gateway-protocol) de Anthropic pide
a una puerta de enlace que inspeccione las peticiones de Claude Code, no que las modifique. La
pestaña Code recibe tanto el enmascaramiento del plugin como la comprobación del daemon. Para
configurarlo, consulta
[Claude Desktop en una puerta de enlace](../other-tools/#claude-desktop-en-una-puerta-de-enlace).

En el trabajo, Claude Code pasa a menudo por la puerta de enlace de tu empresa, por Amazon Bedrock, por
Google Vertex o por Microsoft Foundry: Latet lee esa ruta de tus ajustes y se pone delante, claves
incluidas. Consulta [Puntos de conexión de empresa](../company-endpoints/).

Probado en condiciones reales: la CLI con una suscripción. Las extensiones para IDE y la pestaña Code de
Claude Desktop usan el mismo plugin y los mismos ajustes; si algo no funciona ahí, por favor,
[abre una incidencia](https://github.com/Latet-AI/latet/issues). Cursor no está cubierto: sus peticiones
al modelo salen de los servidores de Cursor, no de tu equipo.

## Suscripciones Pro y Max

El modo cliente funciona con Claude Code cuando inicias sesión con una suscripción Pro o Max, sin clave
de API, y también con una clave de API. La página
[Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance) de Anthropic establece cómo
se puede usar una suscripción. Punto por punto:

| Las condiciones de Anthropic | Qué hace Latet |
| --- | --- |
| El inicio de sesión con una suscripción está pensado para el uso ordinario de Claude Code y de las aplicaciones de la propia Anthropic. | Tu suscripción solo se usa en Claude Code. Codex, OpenCode, Hermes Agent y cualquier otro agente pasan por la puerta de enlace con su propio inicio de sesión: una clave de API, o el inicio de sesión con ChatGPT de Codex. |
| El binario de Claude Code no debe modificarse. | Claude Code se ejecuta tal como lo publica Anthropic. Latet añade un plugin, mediante los propios comandos de plugins de Claude Code, y algunas variables de entorno en `settings.json`. |
| El inicio de sesión debe completarse mediante el flujo de la propia Anthropic; los desarrolladores no pueden recopilar ni almacenar las credenciales de Claude.ai, ni actuar como intermediarios de ellas. | Inicias sesión desde el propio Claude Code. Latet nunca te pide tus credenciales, nunca las lee para sí y nunca las guarda. |
| Las normas no te impiden iniciar sesión en el binario sin modificar de Claude Code con tu propia suscripción. | Así es como se usa Latet: tu Claude Code, tu inicio de sesión. |

**El camino de una petición.** Con `ANTHROPIC_BASE_URL` apuntando al daemon local, Claude Code envía sus
peticiones a través de él, como lo hace a través de cualquier puerta de enlace: Anthropic documenta una
[puerta de enlace](https://code.claude.com/docs/en/llm-gateway) con el inicio de sesión de una
suscripción, y su [guía de puertas de enlace](https://code.claude.com/docs/en/llm-gateway-protocol) pide
a una puerta de enlace que reenvíe las peticiones sin cambios y que las inspeccione sin modificarlas.
Eso es lo que hace el daemon: cada petición llega a Anthropic byte a byte, cabeceras incluidas, con tu
token dentro tal como lo puso Claude Code. El token nunca se guarda, ni se escribe en los registros, ni
se muestra en el panel.

**Por qué no hace falta reescribir nada.** Claude Code añade al prompt de sistema de cada petición un
bloque de atribución con una huella digital derivada de la conversación. Un proxy que reescribiera las
peticiones cambiaría lo que Claude Code envió. En cambio, el plugin enmascara dentro de Claude Code,
antes de que se construya la petición, así que el daemon solo la comprueba: puede dejar pasar una
petición o bloquearla, nunca cambiarla. Consulta [Cómo funciona](../../concepts/how-it-works/) para ver
un ejemplo paso a paso.

Esto describe cómo funciona Latet; no es asesoramiento jurídico, y la referencia son las condiciones de
Anthropic.

## Qué hace el plugin

El plugin pide al daemon local que detecte y enmascare; dentro del propio Claude Code no se detecta
nada.

- **Enmascara el contenido donde nace**, antes de que entre en la conversación: los prompts que
  escribes, el prompt de sistema, `CLAUDE.md` y el resto del contexto que añade Claude Code, los
  adjuntos, los prompts de las skills, las descripciones de las herramientas y cada resultado de una
  herramienta (el contenido de los archivos, la salida de los comandos, la salida de los hooks).
- **Restaura los originales en los argumentos de las llamadas a herramientas** antes de que se ejecute
  una herramienta, para que Read, Edit, Write, Bash y tus servidores MCP locales trabajen con los
  valores reales y los archivos en disco sean correctos.
- **Mantiene los valores ficticios donde los argumentos salen de tu equipo**: WebFetch, WebSearch y los
  conectores de claude.ai reciben los valores ficticios que escribió el modelo. Los subagentes también
  los reciben, y trabajan con ellos como el modelo.
- **Detiene una herramienta de fuera de tu equipo que trabajaría con un valor ficticio**: una llamada a
  WebSearch, a WebFetch o a un conector de claude.ai con un valor ficticio dentro buscaría a otra
  persona, abriría la página equivocada o escribiría a una dirección que no existe. La llamada no se
  ejecuta; el modelo recibe el motivo y te pregunta si el valor puede salir en claro. El panel muestra
  la llamada, con *No sensible* para cada valor.
- **Restaura lo que ves**: tus mensajes, las respuestas del modelo, los comandos y las llamadas a
  herramientas que se muestran en pantalla (también un grupo plegado de ellos, mientras se ejecuta) y
  lo que imprimieron llevan los valores reales. Lo que lee el modelo no cambia.
- **Comprueba cada petición antes de enviarla.** Si un valor sensible está a punto de salir en claro,
  la petición no se envía y en su lugar ves un mensaje:

  ```text
  Latet stopped this request before it left your machine: PASSWORD (tool result) would have been
  sent in clear. Nothing was sent.
  ```

  El panel muestra la petición, con el valor que la detuvo recuadrado en rojo: si no es sensible, haz
  clic en él y elige *No sensible*, y después vuelve a intentar el turno.

  Cuando los valores en claro están en una conversación que empezó antes de que Latet estuviera
  activo, el mensaje lo dice y te pide que empieces una conversación nueva con `/clear` (consulta
  [Solución de problemas](../../troubleshooting/#se-bloquean-todas-las-peticiones-de-una-conversación)).

Si el daemon no está en marcha, el plugin lo inicia. Si no lo consigue, el plugin retiene el contenido
en lugar de enviarlo sin enmascarar: tu prompt no se envía, los resultados de las herramientas se
sustituyen por un aviso y las llamadas a herramientas se rechazan, hasta que el daemon vuelva.
`latet doctor` te dice qué falla.

## Qué hace el daemon

Con `ANTHROPIC_BASE_URL` apuntando a él, cada petición pasa por el daemon, que:

- comprueba si la petición saliente contiene valores sensibles en claro, y la bloquea si encuentra
  alguno: Claude Code muestra entonces un error de la API con el mensaje
  `Latet: sensitive values would leave in clear`;
- si no, reenvía los bytes sin cambios, cabeceras incluidas;
- registra el intercambio para el [panel](../../dashboard/), sin sus credenciales.

Nunca reescribe una petición de Claude Code, ni una que lleve el token de una suscripción de Claude, la
envíe quien la envíe.

## Todas las sesiones, o una

Hay dos formas de pasar Claude Code por Latet:

| | `latet global-install`, `latet agents apply claude` | `latet claude` |
| --- | --- | --- |
| Qué cubre | Todas las sesiones de Claude Code: el terminal, VS Code, JetBrains y la pestaña Code de Desktop (solo el plugin) | Esa sesión, en el terminal |
| Qué cambia | Instala el plugin y añade a `settings.json` los ajustes de abajo, tras hacer una copia de seguridad | Nada: el plugin y los ajustes se dan solo a esa sesión |
| Deshacer | `latet agents revert claude`, o `latet global-uninstall` | Termina la sesión |
| `latet doctor` | Comprueba el plugin y los ajustes | Indica que Claude Code no está aplicado, lo cual es normal |

`latet agents apply claude` pasa por Latet solo Claude Code: ejecútalo cuando instales Claude Code
después de `latet global-install`, o después de cambiar el puerto. `latet claude` inicia el daemon si
hace falta y pasa todos los argumentos a `claude`:

```sh
latet claude
latet claude --continue
```

`claude` iniciado por su cuenta, sin `latet global-install`, no pasa por Latet; en una shell donde
`latet env` ha definido `ANTHROPIC_BASE_URL`, llega a Latet sin el plugin, y sus peticiones solo se
comprueban. `latet run claude`
equivale a `latet claude`: Claude Code necesita el plugin además de la dirección, y `latet run` por sí
solo le daría solo la dirección.

## Qué cambia global-install

`latet global-install`, o `latet agents apply claude`, hace dos cosas:

1. Instala el plugin mediante los propios comandos de plugins de Claude Code, desde un marketplace
   local en la carpeta de datos de Latet.
2. Añade un bloque `env` a `~/.claude/settings.json` (o a `$CLAUDE_CONFIG_DIR/settings.json`),
   después de hacer una copia de seguridad del archivo:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:5283/anthropic",
    "LATET_BRIDGE_URL": "http://127.0.0.1:5283",
    "LATET_BRIDGE_TOKEN": "<a random token, created when first needed>",
    "ENABLE_TOOL_SEARCH": "true",
    "CLAUDE_CODE_AUTO_MODE_SERVER": "0"
  }
}
```

- `ANTHROPIC_BASE_URL` envía cada petición a través del daemon. Con la puerta de enlace de tu empresa,
  Amazon Bedrock, Google Vertex o Microsoft Foundry, Latet escribe en su lugar la variable de esa ruta
  (consulta [Puntos de conexión de empresa](../company-endpoints/)); con una dirección de este
  ordenador, deja la tuya como está.
- `LATET_BRIDGE_URL` y `LATET_BRIDGE_TOKEN` le dicen al plugin dónde está el daemon, y le demuestran al
  daemon que el plugin es el tuyo.
- `ENABLE_TOOL_SEARCH` mantiene activada la búsqueda de herramientas MCP: Claude Code la desactiva
  cuando la URL base es personalizada.
- `CLAUDE_CODE_AUTO_MODE_SERVER` traslada la revisión del modo automático a peticiones que Latet puede
  comprobar. Sin esta línea, la revisión viaja como un campo adicional de cada petición, fuera del
  alcance tanto del plugin como del daemon. `latet agents apply claude --keep-classifier-context` deja
  fuera esta línea.

Después, reinicia Claude Code. `latet agents status` muestra si está aplicado, y
`latet agents revert claude` deshace los dos pasos.

### El revisor del modo automático

En el modo automático, Claude Code pregunta a un modelo revisor si cada acción es segura. El revisor
recibe tu CLAUDE.md, la acción, tu nombre de usuario del sistema operativo (como *User identity*) y las
rutas de tu directorio personal y de tu directorio de trabajo, en un texto al que el plugin no puede
llegar. Los usa para distinguir tus cosas de las de otras personas: un push a una rama `yourname/...`
es tuyo, uno a `someone-else/...` no. El daemon comprueba estas peticiones como cualquier otra, y
`auto_mode_review` decide, dato por dato, qué parte de tu identidad puede llegar al revisor:

| Dato | Qué es |
| --- | --- |
| `user_name` | Tu nombre de usuario del sistema operativo, la *User identity* del revisor. |
| `paths` | El nombre de tu carpeta de usuario en las rutas de tu directorio personal y de tu directorio de trabajo. |
| `host_name` | El nombre de host de este equipo, cuando tu CLAUDE.md o la acción lo contienen. |
| `git_author` | Tu nombre y tu correo electrónico tal como figuran en tu autor de git, y el dominio de ese correo cuando es el de tu empresa, cuando tu CLAUDE.md o la acción los contienen. |

Cada dato es `block` (el predeterminado) o `allow`. Una revisión que llevaría un dato bloqueado se
detiene, así que el dato se queda en local, y el modo automático no puede aprobar la acción. Claude
Code entonces la rechaza, indicando que su revisor no está disponible temporalmente, y no te pregunta:
ejecuta tú la acción o cambia Claude Code a un modo de permisos que te pregunte. Un dato permitido
llega al revisor, y solo al revisor. `auto_mode_review: block` y `auto_mode_review: allow-identity`
establecen todos los datos a la vez; uno por uno, se escribe así:

```yaml
auto_mode_review:
  user_name: allow
  paths: allow
  host_name: block
  git_author: block
```

Los secretos y los datos de otras personas nunca se dejan pasar: una clave o el correo electrónico de
un cliente en la acción detienen la revisión, elijas lo que elijas. Tu propio correo electrónico cuenta
como parte de tu autor de git.

**Mantén los secretos fuera de tu CLAUDE.md.** Claude Code le da al revisor tu CLAUDE.md tal como está
en disco, en una petición que construye él mismo, así que el plugin no puede enmascararlo ahí, y el
daemon nunca cambia las peticiones de Claude Code. Una contraseña o el correo electrónico de un
cliente en el archivo detienen entonces todas las revisiones, y el modo automático rechaza todas las
acciones que necesitan una. Al modelo que hace el trabajo no le afecta: recibe tu CLAUDE.md
enmascarado. Mueve esos valores a `.env` y, en su lugar, nombra la variable en el archivo (*la base de
datos de staging está en `DATABASE_URL`*). El plugin lo avisa cuando empieza una sesión, y
`latet doctor` enumera cada valor por línea y tipo, nunca el valor en sí.

Por defecto, todos los datos se quedan en local;
`latet global-install --auto-mode-review user_name,paths` los deja pasar uno por uno. Más adelante
puedes cambiarlo en la página **Reglas** del panel y en `config.yaml`. Cuando se detiene una revisión,
el panel muestra los datos que llevaba y el ajuste que los deja pasar.

**Un nombre de usuario ficticio.** Con `user_name: surrogate`, Latet le da a Claude Code el valor
ficticio de tu nombre de usuario en `USERNAME` y `USER`. Claude Code le comunica ese nombre al revisor,
el mismo nombre ficticio que el modelo ve en todos los demás sitios, y una revisión que aún lleve tu
nombre de usuario real se detiene. Después de elegirlo, ejecuta `latet agents apply claude` y reinicia
Claude Code. Los comandos que ejecuta Claude Code también ven el nombre ficticio, en `%USERNAME%`,
`$USER` y en `getpass.getuser()` de Python; `whoami`, tu directorio personal y el autor de git se
quedan como están. Resulta útil cuando tu nombre de usuario y tu carpeta de usuario son distintos, algo
habitual en Windows, junto con `paths: allow`: el revisor recibe entonces el nombre de tu carpeta, pero
nunca tu nombre de usuario. Cuando son iguales, las rutas llevan de todos modos el nombre real.

## Límites

- **El modo print muestra los valores ficticios.** Con `claude -p`, la respuesta impresa contiene
  valores ficticios, porque solo la interfaz interactiva restaura lo que hay en pantalla. Los archivos
  que escriben las herramientas siguen recibiendo los valores reales.
- **Claude Desktop con el inicio de sesión de Claude**: la pestaña Code tiene el plugin y su
  comprobación final, pero no el daemon que hay detrás, y no se registra nada en el panel; Chat y
  Cowork no están cubiertos. En una puerta de enlace de terceros, cada petición se comprueba y solo la
  pestaña Code enmascara: consulta [Cobertura](#cobertura).
- **Remote Control está desactivado** mientras Latet está en medio: Claude Code lo desactiva cuando la
  URL base es personalizada.
- **Las herramientas web y los conectores de claude.ai no pueden usar un valor enmascarado.** Sus
  argumentos salen de tu equipo, así que recibirían el ficticio: una llamada así se detiene, y tú
  eliges si el valor puede salir en claro (*No sensible*). Los subagentes se quedan en tu equipo y
  trabajan con valores ficticios, como el modelo. Los resultados de los conectores llegan a Claude Code
  a través de los servidores de Anthropic, así que su contenido ya está ahí antes de que Latet lo vea.
  Consulta [Qué está protegido y qué no](../../concepts/protection-scope/).
- **Imágenes**, con el [reconocimiento de texto](../../concepts/images/) opcional: las que devuelven las
  herramientas se sustituyen por su copia enmascarada; una captura de pantalla que pegas con valores
  dentro se saca del prompt, y Claude recibe la ruta de una copia enmascarada para abrirla con Read.
  Los **PDF** no se leen y se envían tal cual.
