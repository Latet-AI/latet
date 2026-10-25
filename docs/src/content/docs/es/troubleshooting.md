---
title: Solución de problemas y preguntas frecuentes
description: Problemas habituales y preguntas frecuentes.
---

## Empieza aquí

La mayoría de los problemas aparecen con un solo comando:

```sh
latet doctor
```

Comprueba el daemon, la conexión del plugin con él, el arranque al iniciar sesión, el puerto, una
autoprueba del enmascaramiento, y cada agente que encuentra. Cada línea `WARN` o `FAIL` indica el
comando que soluciona el problema.

Cuando se ejecuta en segundo plano, el daemon escribe su registro en `logs/daemon.log`, dentro de la
carpeta de datos: `%LOCALAPPDATA%\latet` en Windows, `~/Library/Application Support/latet` en
macOS, `~/.local/share/latet` en Linux. Si prefieres seguirlo en directo, detén el daemon y ejecuta
`latet serve`, que escribe su registro en el terminal.

## Solución de problemas

### Mi agente dice que no puede conectarse

El agente está configurado para usar Latet, pero el daemon no está en marcha. Compruébalo e
inícialo:

```sh
latet status
latet start
```

`latet status` imprime `running: Latet 0.9.10 on 127.0.0.1:5283 (masking), pid 12345` si está en
marcha, o `not running` si no lo está. Si lo que quieres es dejar de usar Latet, ejecuta antes
`latet global-uninstall`: devuelve tus agentes a su configuración anterior. Consulta
[Desinstalación](../getting-started/uninstall/).

### Claude Code dice que Latet ha retenido contenido

Mensajes como estos significan que el plugin no ha recibido respuesta del daemon, o que el daemon ha
fallado al procesar el contenido, así que el plugin lo ha retenido en lugar de enviarlo sin
enmascarar:

- `Latet could not mask this prompt: the local daemon did not answer, or failed on it.`
- `[withheld by Latet: the local daemon did not answer, or failed on it, so this content was not sent]`
- `Latet could not check this request (the local daemon did not answer, or failed on it), so it was not sent.`

Ejecuta `latet start` y después `latet doctor`. Si el daemon está en marcha, su registro
(`daemon.log`, en la carpeta `logs` de la carpeta de datos) dice qué ha fallado: por favor, abre una
incidencia y adjúntalo. El plugin inicia el daemon por su cuenta con el comando `latet`; cuando
ejecutas Latet desde una copia del código fuente, ese comando no está en tu `PATH`, así que inícialo
con `uv run latet start`.

### Latet rechaza su carpeta de datos

El mensaje nombra la carpeta, quién más puede cambiarla y qué hacer, como en:

```text
Integration records unavailable (unsafe folder namespace): C:\Users\you lets PC\other (S-1-5-21-…)
change what is in it (its access list grants full control, (F), mask 0x001f01ff; `icacls "C:\Users\you"`
lists it): remove that entry (`icacls "C:\Users\you" /remove:g *S-1-5-21-…`), trust that account
(LATET_TRUSTED_ACCOUNTS), or use a data folder whose every parent only you, SYSTEM, the Administrators
and the accounts in LATET_TRUSTED_ACCOUNTS can change (LATET_DATA_DIR); with a new data folder,
`latet agents apply` applies your agents again.
```

Latet guarda lo que aplicó a tus agentes, y en un equipo conectado a un servidor Latet su conexión,
en una carpeta de datos que ninguna otra cuenta del equipo puede cambiar: otra cuenta podría dejar
allí lo que Latet luego aplica o envía. La carpeta nombrada es la carpeta de datos o una de las que la
contienen.

- **La propia carpeta de datos** se rechaza siempre que otra cuenta pueda cambiarla, igual que una
  carpeta superior que pertenece a otra cuenta.
- **Una carpeta superior que otra cuenta puede cambiar** se rechaza en un equipo conectado a un
  servidor Latet, donde está en juego el token del servidor. En un equipo que no está conectado, Latet
  sigue adelante y `latet doctor` lo advierte, con las mismas palabras: una cuenta que puede cambiar tu
  perfil ya puede cambiar todo lo tuyo que hay dentro, incluidos los datos de Latet.

En Windows, `icacls "<carpeta>"` lista quién puede cambiar la carpeta: una entrada para otra cuenta
con `(F)`, `(M)` o `(W)` se lo permite. Hay tres salidas:

1. Quita esa entrada, si el equipo lo puedes cambiar tú. Una entrada marcada `(I)` viene de la carpeta
   de arriba, y se quita allí; una entrada que aparece solo como SID es una cuenta que Windows ya no
   encuentra, a menudo una eliminada.
2. Confía en esa cuenta: ajusta `LATET_TRUSTED_ACCOUNTS` a su nombre o a su SID para tu cuenta, como
   en `setx LATET_TRUSTED_ACCOUNTS "PC\other"`, o en la configuración del sistema, o para todo el
   equipo con una directiva de grupo, y luego abre un terminal nuevo. Latet la lee allí, en el
   registro, y nunca del entorno de un terminal: un valor puesto solo en una sesión de terminal
   (`$env:` o `set`) no cuenta, para que la configuración de un repositorio o la sesión de un agente no
   puedan cambiarla. Varias cuentas se separan con `;`, y un SID se puede escribir `*S-1-5-…`, como lo
   escribe icacls. Vale solo para las carpetas superiores, nunca para la propia carpeta de datos, que
   guarda la clave del almacén, ni para una carpeta dentro de ella.
3. Ajusta `LATET_DATA_DIR` a una carpeta cuyas carpetas superiores solo puedan cambiar tú, SYSTEM, los
   Administradores y las cuentas en las que confías, y luego ejecuta de nuevo `latet agents apply`.

Un miembro directo del grupo local Administradores, como un grupo de soporte que añadió tu departamento
de TI, recibe en las carpetas superiores la misma confianza que el grupo, sin ningún ajuste. La
confianza en estas cuentas y miembros vale para las carpetas superiores de cada carpeta que Latet
comprueba así: las de la carpeta de datos, las que hay por encima de la carpeta de datos que comprueba
el proceso auxiliar de Claude Code, las de su instalación y las de la carpeta de registros del servicio
de Windows de un servidor Latet. `latet doctor` lista las cuentas en las que confía
`LATET_TRUSTED_ACCOUNTS`, con el lugar donde está puesta la variable, y los miembros en los que confió.
Nombra cada entrada que no da confianza a nadie: un nombre que Windows no encuentra, o una cuenta que no
es de este equipo, de un dominio, de Entra ID ni de un servicio, como Todos, Usuarios o Autenticación
NTLM. También dice cuándo la variable está puesta solo en su terminal.

En Linux y macOS, `ls -ld <carpeta>` muestra su propietario y sus permisos. Una carpeta en la que otros
pueden escribir se rechaza: `chmod go-w <carpeta>`, o `chmod 700` para la propia carpeta de datos. Una
carpeta de datos que otros solo pueden leer (0755, como la crea `mkdir`) pasa a ser solo tuya (0700) la
primera vez que Latet la necesita, y Latet lo dice una vez: en el registro del daemon, o en el terminal
del comando que hizo el cambio. `LATET_TRUSTED_ACCOUNTS` no cambia nada allí: las comprobaciones se
basan en el propietario y los permisos.

Un equipo que nunca se conectó a un servidor Latet nunca se rechaza por una conexión.

### Claude Code en un equipo conectado retiene cada prompt

El plugin dice por qué, y cada causa tiene su solución:

- **El modo de impresión u otra superficie:** `claude -p`, y toda superficie salvo el terminal
  interactivo, se retienen en un equipo conectado a un servidor Latet. Usa el terminal interactivo.
- **Falta la vinculación del proceso auxiliar o está desactualizada**, tras una actualización o una
  nueva conexión: ejecuta `latet agents apply claude`, o inicia Claude Code con `latet claude`.
  `latet doctor` la comprueba.
- **El servidor Latet no respondió:** `latet status` dice si responde y si acepta el token de este
  equipo.
- **El servidor ya no acepta el token de este equipo:** se revocó, o se desactivó tu identidad.
  Ejecuta `latet disconnect` y luego `latet connect <url>` con un nuevo código de un administrador.
- **Linux y macOS:** Claude Code a través de un servidor necesita un terminal Windows en esta versión;
  `latet codex` y `latet opencode` pasan por el servidor.

`latet agents apply claude` y `latet claude` pueden negarse con `Claude Code's helper binding is
unavailable`. El mensaje nombra la parte de la instalación que falla (Python, su entorno `sys.prefix`
o su instalación `sys.base_prefix`, el paquete de Latet, o la carpeta de Windows), la carpeta y el
motivo: su propietario, o una cuenta a la que su lista de acceso permite cambiarla. El proceso auxiliar
ejecuta ese código con el token de este equipo, así que solo lo ejecuta desde carpetas que solo pueden
cambiar tú, SYSTEM y los Administradores, y cuyas carpetas superiores solo pueden cambiar ellos, los
miembros directos de los Administradores y las cuentas de `LATET_TRUSTED_ACCOUNTS`. Comprueba la carpeta nombrada con `icacls "<carpeta>"` y
quita la entrada de la otra cuenta; o instala Python y Latet en carpetas tuyas, que eligen
`UV_PYTHON_INSTALL_DIR` y `UV_TOOL_DIR` de uv.

### Se ha bloqueado una petición

En Claude Code, la comprobación final del plugin responde en la conversación:

```text
Latet stopped this request before it left your machine: PASSWORD (tool result) would have been
sent in clear. Nothing was sent.
```

El mensaje indica el tipo de valor que habría salido en claro y la parte de la conversación en la que
estaba; no se ha enviado nada. Un bloqueo así significa que algún contenido ha llegado a la petición
sin enmascarar. Ejecuta `latet doctor` y comprueba con `latet agents status` que Claude Code muestra
`applied`; si no es así, ejecuta `latet agents apply claude` y reinicia Claude Code.

Cuando es el daemon el que bloquea una petición, el agente muestra un error con el mensaje
`Latet: sensitive values would leave in clear` (unos valores sensibles saldrían en claro), seguido
del tipo y la longitud de cada valor, como `PASSWORD[13]`. Ejecuta `latet dashboard`: la petición
bloqueada está ahí, tal como habría salido, con los valores que la han detenido recuadrados en rojo.
Para continuar, saca el valor: empieza una conversación nueva (en Claude Code, `/clear`) e indica al
agente el archivo o la variable que lo contiene, en lugar de pegarlo. Solo si un valor no es un
secreto en absoluto, como un valor de prueba o de ejemplo, haz clic en él y elige
**No sensible: permitir siempre**, y después vuelve a intentar el turno.

En el modo puerta de enlace, un error con `Latet: masking failed` o `Latet: unreadable request body`
significa que el daemon no ha podido enmascarar la petición, así que la ha rechazado.
El registro del daemon dice por qué.

### El modo automático dice que su revisor no está disponible temporalmente

En el modo automático, Claude Code pregunta a un modelo revisor si cada acción es segura. Cuando
Latet detiene esa revisión, el modo automático no puede aprobar la acción: Claude Code la rechaza,
diciendo que el revisor no está disponible temporalmente, y no te pregunta. Ejecuta tú la acción,
cambia Claude Code a un modo de permisos que te pregunte, o deja pasar los detalles como se explica
abajo. En el panel, estas peticiones reciben una respuesta `400` y llevan el indicador
*Revisión del modo automático*; el motivo dice qué llevaba la revisión. Hay dos cosas que la
detienen:

- **Tu identidad:** tu nombre de usuario del sistema operativo y tu carpeta de usuario en las rutas,
  de forma predeterminada. Para dejarlos pasar al revisor, y solo a él, pon
  `auto_mode_review: {user_name: allow, paths: allow}` o usa *Revisiones del modo automático de
  Claude Code* en **Reglas** → **Avanzado** → **Identidad**, en el panel. Consulta
  [Claude Code](../agents/claude-code/#el-revisor-del-modo-automático).
- **Un secreto o los datos de otra persona,** que nunca van al revisor. A menudo están en tu
  CLAUDE.md: Claude Code le pasa ese archivo al revisor sin enmascarar. Mueve esos valores a `.env`
  y escribe en el archivo el nombre de la variable. Cuando empieza una sesión, el plugin indica el
  archivo y las líneas, y `latet doctor` los enumera.

  **Si son marcadores de posición,** escríbelos de una [forma que Latet conoce](../concepts/detection/#marcadores-de-posición)
  y que deja tal cual: `postgres://app:<password>@host/db`, `${DB_PASSWORD}`, `YOUR_PASSWORD`,
  `changeme` o `***`. Un marcador de posición de otra forma va entre los valores del proyecto que no
  se enmascaran nunca (**Reglas**, elige el proyecto, *Valores permitidos*), que
  `latet doctor` también tiene en cuenta si lo ejecutas en la carpeta del proyecto.

Mantén las solicitudes de permiso en lugar de pasar al modo bypass permissions: en los comandos que
ejecuta, el agente trabaja con los valores reales.

### Se bloquean todas las peticiones de una conversación

En Claude Code, un mensaje como este detiene todas las peticiones de una conversación:

```text
Latet stopped this request before it left your machine: this conversation already held values in
clear before Latet checked it (PASSWORD (user text), EMAIL (tool result)). It began before Latet
was on, or was resumed from then, and every request sends that history again. Start a new
conversation with /clear: from there everything is masked as it comes in. Nothing was sent.
```

La conversación empezó antes de que Latet estuviera activo, o has retomado una de entonces
(`claude --resume`): sus primeros mensajes, tu CLAUDE.md y la salida de sus herramientas entraron en
el historial en claro. Claude Code envía todo el historial con cada petición, así que cada una vuelve
a llevar esos valores, y Latet no puede saber que ya habían salido. En el panel, estas peticiones
llevan el indicador *Del historial*.

Empieza una conversación nueva con `/clear`, o una sesión nueva, con Latet activo. A partir de ahí,
todo se enmascara según entra.

### El revisor local ha detenido una petición

Con el [revisor local](../concepts/llm-review/) activado, una petición espera su revisión, y no se
envía cuando el revisor no puede hacerla. El agente, o Claude Code, dice por qué:

- `the local reviewer could not read the user text (its server did not answer: start it, or run
  latet doctor), so it was not sent`: su servidor no ha respondido; inicia el servidor del modelo y
  compruébalo después con `latet llm status`. Un servidor que responde `HTTP 401` pide una clave de
  API: guárdala en *Clave de API*, en el panel, o con `latet llm key`;
- `... (it took longer than llm_review.budget_ms)`: la revisión ha tardado más de lo permitido;
  aumenta `budget_ms`, o quítalo para esperar lo que tarde la revisión;
- `the local reviewer could not trust its review of the tool result: the text gives it
  instructions`: el texto le da instrucciones al revisor, y este no ha encontrado nada en él, así
  que no puede fiarse de su revisión. Míralo en el panel antes de volver a enviarlo;
- `the local reviewer stopped this tool result: it must not leave even masked`: con `block: true`,
  el revisor ha juzgado que todo el texto es demasiado sensible para enviarlo, ni siquiera
  enmascarado, como un historial médico.

En Claude Code, un prompt detenido no se envía, y la salida de una herramienta detenida llega al
modelo como `[withheld by Latet: ...]`. Para enviar lo que han enmascarado los detectores cuando el
revisor no puede responder, pon `on_failure: send`.

### Se ha retenido la salida de una herramienta

`[withheld by Latet: this tool output holds 214 personal values ...]` en la conversación, y el
indicador *Retenido* en el panel, vienen de la
[protección contra volcados](../concepts/production-data/#la-protección-contra-volcados): la salida contenía más
valores sobre personas de los que `dump_guard` permite en una sola salida, así que no se ha enviado
nada de ella. Pide al agente menos filas, o recuentos y agregados; aumenta `dump_guard`, o ponlo a
`0` para desactivar la protección.

### Mi latet.yaml no tiene efecto

- `latet project show` imprime lo que se aplica en el proyecto, y cada clave del archivo que se
  ignora, con el motivo: un archivo de proyecto solo puede añadir protección, así que ahí se ignoran
  los valores que no se enmascaran nunca, las reglas desactivadas y Latet desactivado. Su sitio está
  en tus propios ajustes para el proyecto.
- `latet project check` comprueba el archivo, y termina con el código 1 cuando algo se ignora o un
  ejemplo falla.
- Solo se aplica el `latet.yaml` más cercano, desde la carpeta del agente hasta la raíz del
  repositorio. Consulta [Proyectos](../concepts/projects/).

### No se enmascara nada en un proyecto

Puede que Latet esté desactivado ahí: el panel marca esos intercambios con *Latet desactivado*.
`latet project show` lo indica, y `latet project on` lo vuelve a activar. Un proyecto configurado
con *Solo secretos* enmascara los secretos y tus términos, y deja pasar los datos personales.

### Se enmascara un valor que no debería

Es un falso positivo. En el panel, abre la petición, haz clic en el valor y elige
**No sensible: permitir siempre**, o añade el valor a `allow_values` en `config.yaml`. Para
desactivar una regla de secretos entera, añade su id a `disabled_secret_rules`: para un valor que el
panel muestra como encontrado por `gitleaks:generic-api-key`, el id es `generic-api-key`. Consulta
[Listas de permitidos](../concepts/profiles/#listas-de-permitidos).

### No se enmascara un valor

- Comprueba el [perfil](../concepts/profiles/): `secrets` no enmascara los datos personales.
- Los nombres en texto libre necesitan el
  [modelo de nombres local](../getting-started/installation/#detección-de-nombres) opcional o el
  [revisor local](../concepts/llm-review/), y ninguno de los dos da garantías: añade los nombres que
  importan como [términos personalizados](../concepts/detection/#tus-propios-términos).
- Pega el texto en el **Banco de pruebas** del panel para ver qué encuentran las reglas actuales.

### La respuesta de `claude -p` muestra valores ficticios

Es lo esperado: en el modo print, Claude Code no restaura lo que imprime. Los archivos que escriben
las herramientas contienen los valores reales, y la interfaz interactiva también los muestra.
Consulta [Claude Code](../agents/claude-code/#límites). En un equipo conectado a un servidor Latet, el modo
de impresión se retiene: consulta
[Claude Code en un equipo conectado retiene cada prompt](#claude-code-en-un-equipo-conectado-retiene-cada-prompt).

### Los comandos van lentos en Windows

Cada comando `latet` tarda segundos en arrancar, también `latet version`, mientras tus agentes
trabajan tan rápido como siempre. Es tu antivirus. Comprueba los programas que no conoce cada vez que
se ejecutan, y el pequeño lanzador que un instalador crea para el comando `latet` (tanto el de uv
como el de pip) es uno de ellos: sin firmar, y distinto en cada equipo. Con Windows Defender cuesta
alrededor de medio segundo; algunos antivirus, Avira por ejemplo, tardan varios segundos con cada
lanzador. Latet en sí arranca en una fracción de segundo, y tus agentes hablan con el daemon, que ya
está en marcha, así que la espera solo afecta al arranque de un comando.

Dos soluciones:

- **Añade Latet a las exclusiones de tu antivirus.** Con una instalación hecha con uv, las carpetas son
  `%APPDATA%\uv\tools\latet` y `%USERPROFILE%\.local\bin` (en esta última, basta con `latet.exe` y
  `latet-daemon.exe`).
- **O ejecútalo a través del propio Python,** que está firmado y arranca enseguida: instala el wheel
  de la [última versión](https://github.com/Latet-AI/latet/releases/latest) con
  `py -m pip install "latet[ner,ocr] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"`
  (con su versión en lugar de `X.Y.Z`) y ejecuta cada comando como `py -m latet`, por ejemplo
  `py -m latet claude`. El `latet.exe` que crea pip es tan lento como los demás, así que déjalo de
  lado. Latet comparte entonces ese Python con tus otros programas en lugar de tener el suyo propio.
  Para actualizarlo, ejecuta el mismo `py -m pip install` con el wheel de la nueva versión:
  `latet upgrade` instala con uv, como una segunda copia. Para quitarlo, ejecuta
  `py -m latet global-uninstall` y después `py -m pip uninstall latet`.

Con un antivirus lento, puede que el plugin de Claude Code no consiga iniciar el daemon por su cuenta
a tiempo, y mientras tanto retiene el contenido. Deja que el daemon arranque al iniciar sesión
(`latet global-install` lo configura), o ejecuta `latet start` antes de abrir Claude Code.

### El puerto 5283 ya está en uso

Latet escucha en `127.0.0.1:5283`, un puerto que no usa ningún software habitual. Si otro programa lo
ha ocupado, o Windows lo ha reservado (Hyper-V, WSL y Docker reservan bloques de puertos que pueden
cambiar en cada arranque), `latet start` lo dice enseguida:

```text
Latet cannot listen on 127.0.0.1:5283: in use by another program.
Run `latet global-install` to move it to a free port (your agents are updated too).
```

`latet global-install` elige el primer puerto libre entre 5284 y 5335, lo guarda en `config.yaml` y
hace que cada agente que ha configurado apunte al nuevo puerto. Si ninguno está libre, elige uno tú:
`latet global-install --port 6655`. `latet serve --port <port>` solo cambia el puerto de una única
ejecución en primer plano.

### Latet se ejecuta en Docker y los agentes no llegan a él

Ejecuta `docker exec latet latet status` y comprueba después que el puerto está publicado en
`127.0.0.1` y que es el mismo dentro y fuera (`LATET_PORT` fija los dos en `compose.yaml`). Después
de un cambio de puerto, vuelve a ejecutar los comandos `agents merge` que imprime
`docker exec latet latet agents print`. Claude Code con `LATET_AUTOSTART=0` nunca inicia un daemon
propio: espera al contenedor. Detrás de un proxy, consulta
[Docker](../getting-started/docker/#detrás-de-un-proxy).

### "browser requests are not accepted on proxy routes"

Has abierto una URL del proxy en un navegador web, o una página web ha intentado acceder a ella. Es
intencionado: el daemon rechaza las peticiones que vienen de los navegadores. A los agentes no les
afecta. Para ver el tráfico, usa `latet dashboard`.

### "host not allowed"

La petición ha usado un nombre de host distinto de `127.0.0.1`, `localhost` o `[::1]`, y el daemon no
lo permite. Usa uno de ellos en la URL base.

### Codex no pasa por Latet

- Un Codex anterior a la versión 0.115 ignora `openai_base_url`: actualízalo. `latet doctor` avisa
  de ello, y `latet agents apply codex` deja ese Codex tal como está. Hasta entonces, inícialo con
  `latet codex`, que le pasa la dirección en `OPENAI_BASE_URL`, la única que lee. Con un proveedor
  propio (Azure, la puerta de enlace de tu empresa), `latet codex` no lo inicia, y te dice que
  actualices Codex.
- Ejecuta `latet agents status`: Codex debería mostrar `applied`, y su ruta.
- Si has cambiado entre el inicio de sesión con ChatGPT y una clave de API, o has añadido un
  proveedor, vuelve a ejecutar `latet agents apply codex`.

### Claude Code dice que sus peticiones no pasan por Latet

El plugin ha visto que Claude Code está configurado para enviar sus peticiones a otro sitio, y ha
detenido sus prompts: nada comprobaría lo que sale. El mensaje indica el archivo que lo ha
configurado así.

- **El `.claude/settings.json` de este proyecto** (o `.claude/settings.local.json`) fija
  `ANTHROPIC_BASE_URL`, la dirección o el interruptor de una nube, o un proxy (`HTTPS_PROXY`): un
  repositorio nunca decide adónde van las peticiones, ni siquiera a través de Latet. Mueve la línea
  a tus propios ajustes y ejecuta `latet agents apply claude`, o inicia Claude Code con
  `latet claude`, cuyos ajustes prevalecen sobre la dirección del proyecto. Un proxy fijado por el
  proyecto detiene los prompts hasta que se quite de ahí.
- **Hay más de una nube activada** (`CLAUDE_CODE_USE_BEDROCK` y `CLAUDE_CODE_USE_VERTEX`, por
  ejemplo): deja solo una.
- **Ninguna dirección**, o una nube a la que se accede directamente (`CLAUDE_CODE_USE_BEDROCK` sin
  su dirección): inícialo con `latet claude`, o ejecuta `latet agents apply claude` una vez para
  todas las sesiones.
- **Los ajustes gestionados de tu organización** imponen una dirección: consulta
  [Puntos de conexión de empresa](../agents/company-endpoints/#dónde-latet-no-puede-ponerse-delante).

### Bedrock dice que el inicio de sesión de AWS ha caducado

Latet firma las peticiones a Bedrock con tu perfil de AWS, y el inicio de sesión en el que se basa ha
terminado. Ejecuta el comando que indica el mensaje (`aws sso login --profile work`) y vuelve a
enviar: no hay que reiniciar nada. Sin la AWS CLI, Latet solo lee las claves escritas en `~/.aws`;
un inicio de sesión SSO necesita la AWS CLI, y `latet doctor` lo indica. Latet busca `aws` en su
propio PATH, y después en `/usr/local/bin`, `/opt/homebrew/bin`, `/usr/bin`, `~/.local/bin` y
`/snap/bin` (en `C:\Program Files\Amazon\AWSCLIV2` en Windows). Si tu AWS CLI está en otro sitio,
inicia Latet desde un shell en el que `aws` funcione.

### No se puede acceder a un punto de conexión de empresa

La petición ha llegado a Latet, y el destino al que la reenvía no ha respondido. Comprueba la
dirección en Agentes protegidos → Adónde van las peticiones, en el panel, o con
`latet config get upstreams`; Claude Code muestra el host del destino en su error.

### Claude Desktop no pasa por el daemon

Es lo esperado: la aplicación de escritorio ignora `ANTHROPIC_BASE_URL`. Las sesiones locales de su
pestaña Code tienen el plugin y su comprobación final, pero no la comprobación del daemon, y no se
registran en el panel. Consulta [Claude Code](../agents/claude-code/).

### Remote Control ha desaparecido de Claude Code

Es lo esperado mientras Latet esté en medio: Claude Code desactiva Remote Control cuando su URL
base está personalizada. `latet agents revert claude` lo recupera, y hace que Claude Code deje de
pasar por Latet.

### El panel dice que sus archivos no forman parte de esta instalación

Estás ejecutando Latet desde una copia del código fuente. Compila el panel: `npm ci` y después
`npm run build`, en `dashboard/`. Las versiones publicadas lo incluyen. La página del tutorial
también lo necesita: hasta entonces, `latet tutorial` avisa de que su página no se mostrará.

### El tutorial no se abre, o pide que se vuelva a iniciar

- **No se ha abierto ningún navegador:** ejecuta `latet tutorial --print-url` y abre el enlace que
  imprime en el navegador que prefieras. El enlace funciona una sola vez, durante dos minutos.
- **La página pide que vuelvas a iniciar el tutorial:** su enlace ya se ha usado, o han pasado más
  de dos minutos. Pulsa Ctrl+C donde se ejecuta el tutorial y vuelve a iniciarlo: los capítulos que
  has terminado siguen completados.
- **Se ha detenido por un error:** ha impreso las últimas líneas de su registro, y conserva el
  registro completo en la carpeta del entorno aislado hasta que lo vuelvas a ejecutar. Por favor,
  abre una incidencia y adjúntalo.

### Una actualización falla en Windows porque hay archivos en uso

Usa `latet upgrade`: detiene el daemon, instala la nueva versión en una ventana propia cuando el
comando ha terminado (Windows no sustituye ningún archivo de un programa en ejecución) y vuelve a
iniciar Latet.

Un Latet 0.9.0 o 0.9.1 actualizado en Windows con su propio `latet upgrade` puede quedarse sin poder
arrancar, y `latet` responde `No module named 'latet'`: vuelve a ejecutar el instalador, que instala
la última versión entera.

### Volver a la versión que tenías

`latet upgrade --to 0.9.9` instala esa versión en lugar de la última. Cada `latet upgrade` copia
primero la caja fuerte, el historial y la configuración tal como están, en
`backups/before-<versión>-<hora>` en la carpeta de datos de Latet; conserva las tres copias más
recientes. Al volver a la versión con la que se hizo una copia, la actualización ofrece restaurarla:
dice cuántos valores ha aprendido la caja fuerte desde entonces, que se perderían, y deja tus datos
como están salvo que respondas que sí en tu terminal. Si la restauras, tus archivos tal como están
ahora se copian antes, en una copia propia.

Para restaurar una copia a mano: detén Latet (`latet stop`), copia `latet.db` y `exchanges.db` de la
copia en la carpeta de datos en lugar de los que hay, quitando sus archivos `-wal` y `-shm`, su
`config.yaml` en lugar del tuyo y su `config.reviewed.json` en la carpeta de datos, y luego inicia
Latet. Una copia solo se abre con la clave de este equipo, que se queda donde está.

### Una actualización se detiene antes de empezar: no se puede hacer la copia

`latet upgrade` copia la caja fuerte, el historial y la configuración antes de cambiar nada. Cuando no
se puede hacer esa copia, o el disco no tiene espacio para ella y para la instalación (dice cuánto
espacio necesita), dice por qué y se detiene: no se ha instalado
nada y el daemon no se ha detenido. Haz espacio, o borra copias más antiguas en `backups` en la
carpeta de datos de Latet, y vuelve a ejecutarlo.

### No se puede averiguar cuál es la última versión

Los instaladores y `latet upgrade` preguntan a la API de GitHub cuál es la última versión, e instalan
su wheel. Cuando no pueden, dicen por qué y no cambian nada:

- **La API de GitHub ha respondido 403** (o 429): acepta 60 peticiones por hora desde una misma
  dirección, y una oficina detrás de una sola dirección puede agotarlas. Entonces los dos leen la
  página de la última versión, que no se contabiliza; si eso también falla, espera una hora, o
  instala a mano (abajo).
- **No se ha podido acceder a GitHub:** no hay conexión, o hay un proxy por medio.
- **La última versión no tiene wheel,** o no hay ninguna: instala
  [desde el código fuente](../getting-started/installation/#ejecutar-desde-el-código-fuente).

Para instalar una versión a mano, toma la dirección de su wheel de la página de
[versiones](https://github.com/Latet-AI/latet/releases), y pásasela al instalador en `LATET_SOURCE`
(también actualiza un Latet ya instalado; después ejecuta `latet start`, y `latet agents apply claude`
si Claude Code pasa por Latet de forma permanente):

```sh
curl -LsSf https://raw.githubusercontent.com/Latet-AI/latet/main/scripts/install.sh | LATET_SOURCE="latet[ner,ocr] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl" sh
```

En Windows (PowerShell):

```powershell
$env:LATET_SOURCE = "latet[ner,ocr] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
irm https://raw.githubusercontent.com/Latet-AI/latet/main/scripts/install.ps1 | iex
```

También funciona un wheel que hayas descargado: su ruta en lugar de la dirección.

## Preguntas frecuentes

### ¿Funciona con una suscripción Claude Pro o Max?

Sí, sin clave de API: para eso está el modo cliente. Claude Code se ejecuta sin modificaciones,
inicias sesión desde el propio Claude Code, el plugin enmascara dentro de él, y el daemon reenvía
cada petición byte a byte. Consulta [Claude Code](../agents/claude-code/#suscripciones-pro-y-max).

### ¿Pueden Hermes Agent u OpenClaw usar mi suscripción de Claude a través de Latet?

No. Anthropic permite usar una suscripción en Claude Code y en sus propias aplicaciones, y Latet no
la presta a nada más. Los agentes autónomos pasan por la puerta de enlace con una clave de API: una
clave de OpenAI o de OpenRouter (OpenRouter también ofrece los modelos de Claude), o una clave de API
de Anthropic. Consulta [Hermes Agent, OpenClaw y otros agentes](../agents/autonomous-agents/).
Hermes Agent con la sesión iniciada con ChatGPT es otra cosa: pasa por Latet con esa sesión,
consulta [Con tu cuenta de ChatGPT](../agents/autonomous-agents/#con-tu-cuenta-de-chatgpt).

### ¿Por qué Latet no se limita a reescribir el tráfico de Claude Code?

Claude Code añade a cada petición un bloque de atribución con una huella digital de la conversación,
y la guía de Anthropic sobre puertas de enlace les pide que inspeccionen las peticiones sin
modificarlas. Por eso el enmascaramiento ocurre dentro de Claude Code, antes de que se construya la
petición, y el daemon solo la comprueba. Consulta
[Cómo funciona](../concepts/how-it-works/#modo-cliente-claude-code).

### ¿Puede el modelo seguir haciendo su trabajo con valores ficticios?

Normalmente, sí. Los valores ficticios conservan el formato del original, así que el código que
maneja una clave, un correo electrónico o un IBAN sigue teniendo sentido para el modelo. Solo que
nunca conoce el valor real. Eso sí, solo se restaura el propio valor ficticio: si el modelo escribe
*Dear Laura* porque la dirección ficticia empieza por `laura.`, ese saludo se queda.

### ¿El enmascaramiento rompe la caché de prompts del proveedor?

No. El mismo valor recibe siempre el mismo valor ficticio, y los turnos anteriores se vuelven a
enviar exactamente igual que antes, así que el proveedor puede seguir leyéndolos de su caché. La
página **Estadísticas** del panel muestra qué parte de cada prompt procede de la caché.

### ¿Dónde puedo ver lo que se ha enviado realmente?

Ejecuta `latet dashboard`. Consulta la [guía del panel](../dashboard/).

### ¿Cómo lo desactivo?

`latet global-uninstall` devuelve cada agente a su configuración anterior, quita el arranque al
iniciar sesión y detiene el daemon. Las herramientas que apuntaste a Latet en sus propios ajustes
conservan su dirección: vuelve a poner la suya. Si vuelves a ejecutar `latet global-install`, se
activa de nuevo.

### ¿Latet envía algo a algún sitio?

Solo el propio tráfico de tu agente, al proveedor (o al punto de conexión de empresa) que tu agente
ya usa. Todas las excepciones ocurren cuando tú lo pides: `latet ner install` y `latet ocr install`
descargan sus modelos de huggingface.co una vez; los instaladores y `latet upgrade` preguntan a
GitHub cuál es la última versión, y descargan su wheel de allí; y al revisor local se le pregunta en
tu propio servidor. No hay ningún servicio en la nube de Latet, ni telemetría, ni analíticas.

### ¿Mis datos están a salvo una vez instalado Latet?

Más a salvo, no a salvo del todo. Latet solo enmascara lo que reconocen sus detectores, y algunas
superficies y tipos de archivo quedan fuera de su alcance. Lee
[Qué está protegido y qué no](../concepts/protection-scope/).

### ¿Y las imágenes y los PDF?

Descarga los modelos de reconocimiento de texto con `latet ocr install` (un Latet instalado sin el
extra `ocr`, con el que funcionan, lo obtiene con `latet upgrade`): el texto de las capturas de
pantalla y las imágenes se lee entonces en tu equipo y se enmascara, y cada valor se cubre en la
imagen con su valor ficticio. Consulta [Capturas de pantalla e imágenes](../concepts/images/).

Los PDF y otros documentos no se leen: se envían tal cual, y el panel marca esas peticiones como
*No inspeccionado*. Evita adjuntar archivos que contengan datos sensibles, o pon `images: block` (el
perfil `max` hace lo mismo) para que no se envíe una petición que lleve uno.

### Se ha retenido una imagen

El modelo ha recibido *[image withheld by Latet: ...]* en lugar de una imagen, con el motivo:
- *the text recognition models were still loading*: los modelos de reconocimiento de texto todavía
  se estaban cargando, porque el daemon acababa de arrancar. Vuelve a enviarla.
- *it could not be read*: no se ha podido leer; es una imagen animada, una de más de 20 MB o
  40 megapíxeles, o no es una imagen válida.
- *its text could not be read (OcrTooManyTiles)*: la imagen es tan larga para su anchura, o tan
  ancha para su altura, que leerla llevaría más de 16 partes, como la captura de una página de más
  de unos 29.000 píxeles de largo. Haz varias capturas de la página, o recórtala a lo que necesita el
  agente.
- *its text could not be read (OcrTooWide)*: una línea de texto de la imagen es demasiado larga para
  su altura como para leerla, por ejemplo una sola línea que ocupa varias pantallas. Recorta la imagen
  a lo que necesita el agente.
- *its text could not be read (OcrTooLong)*: leerla ha llevado más tiempo del que puede llevar una
  lectura. Vuelve a enviarla, o envía una imagen más pequeña.
- *you withheld it in the Live Reviewer*: la has retenido tú en el Revisor en directo, al marcar
  *No enviar esta imagen*.
- *the local reviewer saw a value in it ...*: el [revisor local](../concepts/llm-review/#imágenes), al
  que se le ha mostrado la imagen, ha visto en ella un valor que Latet no ha encontrado en el texto
  leído, así que no ha podido cubrirlo. Enmascáralo a mano en el Revisor en directo, o recórtalo de
  la imagen.

### ¿Puedo ejecutar Latet en un entorno aislado con mi agente?

Sí. Latet no es un entorno aislado en sí, y funciona dentro de uno: instálalo donde se ejecuta el
agente, para que el agente llegue a él en `127.0.0.1`.

### ¿Mi empresa puede usar Latet gratis?

Sí, sea cual sea su tamaño, para su propio trabajo, en cualquier número de ordenadores y servidores.
El uso personal y el no comercial también son gratuitos. Para la Community Edition, solo hace falta una
licencia comercial para vender Latet, ofrecerlo a otros como servicio, integrarlo en un producto que
vendes o para un fork comercial: escribe a [info@latet.ai](mailto:info@latet.ai). La Enterprise
Edition, los archivos de los directorios `ee`, añade lo que una empresa necesita para gestionar Latet
de forma central, y necesita una licencia comercial en producción; desarrollar y probar con ella es
gratis. La Community Edition nunca comprueba una licencia: no hay claves. [LICENSING.md](https://github.com/Latet-AI/latet/blob/main/LICENSING.md)
tiene los detalles y ejemplos.

### ¿De dónde viene el nombre?

*Latet* significa "se esconde" en latín, como en *latet anguis in herba*: "una serpiente se esconde
en la hierba". Latet esconde tus secretos a la vista de todos: el modelo ve un valor en todo parecido
al real.
