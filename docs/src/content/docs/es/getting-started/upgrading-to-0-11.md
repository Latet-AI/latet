---
title: Pasar a la 0.11
description: Lo que Latet 0.11 rechaza y la 0.10 enviaba, qué comprobar antes de actualizar y cómo volver atrás.
---

Latet 0.11.0 lee cada parte de una petición antes de enviarla: la ruta, los parámetros de consulta,
las cabeceras y el cuerpo. Una petición con una parte que Latet ni inspecciona ni deja pasar por una
regla o una excepción que hayas aprobado no se envía. Un cuerpo vacío ya no basta para dejar pasar
una petición. Las credenciales que necesita tu proveedor, y las partes que un proveedor firma, siguen
sus propias reglas. Vale igual en tu equipo y en un servidor de Latet.

Así que una llamada que la 0.10 enviaba sin leerla puede rechazarse ahora. Esta página explica qué
cambia, qué comprueba `latet doctor` por ti antes y después de actualizar, y qué hacer en cada caso.

## Lo que la 0.11 rechaza y la 0.10 enviaba

**Llamadas sin cuerpo.** La lista de modelos y la lectura de un modelo pasan en cada proveedor que
Latet conoce: Anthropic, OpenAI, OpenRouter, ChatGPT (la lista de modelos de Codex), Gemini y los
perfiles de inferencia de Bedrock. También se leen sus parámetros de consulta (`limit`, `after_id`,
`pageToken`, `maxResults`...). Cualquier otra llamada se rechaza: archivos y subidas, lotes, audio,
generación de imágenes, moderación, leer o borrar una respuesta guardada, leer una caché de Gemini,
los puntos de conexión de una cuenta. Al agente se le dice, y no se envía nada:

```text
Latet: request envelope refused (envelope-route): route /{destination}/{unsupported-route}; this version of Latet does not support this call, so change the agent or its settings so that it does not make it. Nothing was sent; run `latet dashboard` to see where it was
```

Una excepción nombra una cabecera, un parámetro de consulta o un miembro del cuerpo, nunca una ruta
entera: en la 0.11.0 ninguna excepción deja pasar una de estas llamadas. Cambia los ajustes del
agente para que no haga la llamada, o hazla sin Latet, a la dirección del proveedor, sabiendo que
Latet no ve lo que se envía.

**Parámetros de consulta.** Un parámetro de consulta que ningún perfil nombra se rechaza, y al agente
se le dice su nombre, nunca su valor:

```text
Latet: request envelope refused (envelope-query-unknown): query parameter drift. Nothing was sent; run `latet dashboard` to see where it was
```

**Cabeceras.** Cada cabecera se lee con una regla propia: una credencial se envía tal como llegó y
nunca se registra; una cabecera propia de un agente debe tener la forma que ese agente le da (un ID de
sesión debe ser un UUID, por ejemplo); una cabecera que puede contener texto libre, como `user-agent`,
pasa la comprobación final. Una cabecera que ningún perfil nombra se rechaza, y al agente se le dice
su nombre, nunca su valor:

```text
Latet: request envelope refused (envelope-header-unknown): header x-team. Nothing was sent; run `latet dashboard` to see where it was
```

En una petición que Latet solo comprueba (Claude Code, Bedrock, Vertex), las cabeceras que Claude
Code añade al ganar funciones nuevas pasan, como pide su guía de puertas de enlace: una cabecera
`anthropic-*` nueva, o un valor de `anthropic-beta` de forma nueva, pasa una vez leída por la
comprobación final, mostrada como *solo comprobada* en el panel, y una cabecera `x-claude-code-*`
nueva se quita. Pasan hasta ocho cabeceras `anthropic-*` de este tipo, cada una con un nombre de
letras minúsculas, cifras y guiones, 48 como mucho después de `anthropic-` y ninguna palabra de más
de 16 entre sus guiones, y un valor de 1 a 1024 caracteres imprimibles; un secreto o un valor
tuyo en una de ellas se sigue rechazando.
En una petición que Latet reescribe, como la de un SDK de Anthropic, se rechazan como cualquier otra
cabecera que ningún perfil nombra.

En cambio, algunas cabeceras se quitan: las de telemetría y de trazado de los SDK, y algunas
propias de los agentes que pueden contener texto libre.
[Las cabeceras que quita Latet](#las-cabeceras-que-quita-latet) las enumera.

**Miembros del cuerpo.** Cada miembro del cuerpo de una petición se lee según su perfil: enmascarado,
revisado por la comprobación final, o sujeto a la forma que le da el proveedor (el ID de un modelo,
el ID de una llamada a una herramienta, un ajuste). En la 0.11 un miembro que ningún perfil nombraba
se rechazaba, diciendo dónde está (`top-level`, `message content block`, `tool definition`...), nunca
su nombre ni su valor:

```text
Latet: request body refused (unknown member in top-level). Nothing was sent; run `latet dashboard` to see where it was
```

Desde la 0.15.0 un miembro así se lee entero y se comprueba, y pasa tal como llegó cuando nada en él
se enmascararía y no nombra nada sobre lo que actúe el proveedor: consulta
[Nuevos miembros de las peticiones](#nuevos-miembros-de-las-peticiones). Se sigue rechazando, con las
mismas palabras, cuando nombra un archivo, una dirección, una conversación guardada o un modelo,
contiene datos codificados o lleva algo más que tokens cortos dentro de un elemento propio del
proveedor; y con `unknown_members: refuse` se rechaza como en la 0.11.

Una [excepción exacta](#aprobar-una-excepción-exacta) puede nombrar un miembro rechazado por su puntero
JSON, salvo donde ninguna puede llegar: dentro de un elemento propio del proveedor (`provider item`:
thinking, reasoning), en la fuente de una imagen (`inline image`) y en cualquier trama WebSocket.
Al agente se le dicen las mismas palabras; el panel registra el rechazo como
`body-unknown-member-no-exception` en lugar de `body-unknown-member`, y dice *Su cuerpo llevaba un
miembro que ningún perfil de petición prevé, donde ninguna excepción puede llegar (dentro de un
elemento del proveedor, la fuente de una imagen o un turno WebSocket).*

Una petición que Latet solo comprueba (Claude Code, Bedrock, Vertex) nunca se reescribe, así que allí
vale una regla más: un miembro nuevo cuyo valor es un texto o una lista de textos pasa una vez que la
comprobación final ha leído su nombre y su texto, y el panel lo muestra como *solo comprobada*. Un
miembro nuevo anidado, o de otro tipo, se lee como dice
[Nuevos miembros de las peticiones](#nuevos-miembros-de-las-peticiones). `client_metadata` de Codex,
que repite la ruta y los remotos del repositorio, se quita de la petición antes de enmascararla.

**Lo que devolvió el proveedor.** Las firmas del razonamiento del modelo, el razonamiento cifrado, las
compactaciones y los resultados de búsquedas web vuelven en cada turno. Pasan porque Latet los vio
llegar del proveedor en esa conversación. Donde no los vio (el daemon de un equipo se reinició, por
ejemplo), una regla integrada deja pasar los campos cifrados del proveedor, solo en los turnos del
modelo y solo hacia la dirección del propio proveedor; el panel los muestra como *permitida por una
regla*. Una conversación a través de una
[pasarela de empresa](#conversaciones-a-través-de-una-pasarela-de-empresa) no tiene esa regla.

**Imágenes y documentos.** Una imagen solo se envía cuando Latet ha leído su texto. Sin el
reconocimiento de texto, una petición que lleva una imagen se rechaza, con palabras que dicen que
ejecutes `latet ocr install` y empieces una conversación nueva, porque la imagen se queda en su
historial. `images: allow` ya no envía una imagen que Latet no haya leído. Los PDF y otros documentos,
el audio y las imágenes indicadas por su dirección se rechazan, diga lo que diga `images`. En una
petición que Latet solo comprueba (Claude Code, Bedrock, Vertex), cada byte de una imagen tiene que
estar justificado, y una foto del móvil con la ubicación, las notas del fabricante de la cámara, una
miniatura o una segunda imagen dentro (un mapa de profundidad, el vídeo de una foto en movimiento) se
rechaza: consulta [Las fotos y sus metadatos](../../concepts/images/#las-fotos-y-sus-metadatos). Allí
también se rechaza una imagen WebP original: expórtala como PNG. Las peticiones que Latet reescribe
(Codex y los demás) envían las imágenes reescritas, sin sus metadatos, y no ven esos rechazos.

**Un proyecto en el que Latet está desactivado** no envía nada: consulta
[más abajo](#un-proyecto-en-el-que-latet-está-desactivado). **`latet serve --passthrough`** ya no
existe: la opción sigue oculta durante toda la 0.11 y termina enseguida con un mensaje, para que un
servicio que todavía la nombre se detenga donde lo veas. Quítala del comando del servicio, o del
script que arranca Latet.

**Una carpeta que nombra una petición solo añade protección:** consulta
[Las carpetas que nombra una petición](#las-carpetas-que-nombra-una-petición).

## Las cabeceras que quita Latet

Latet quita estas cabeceras de una petición antes de enviarla, sea cual sea el agente que las envió:

| Dónde | Cabeceras |
| --- | --- |
| Cada petición: telemetría y trazado de los SDK | `x-stainless-*`, `x-datadog-*`, `x-b3-*`, `traceparent`, `tracestate`, `baggage` |
| Claude Code, en el formato de Anthropic (Anthropic, Bedrock, Vertex, una pasarela de empresa) | `x-claude-code-agent-id` y `x-claude-code-parent-agent-id`, los ID de un subagente, que pueden contener su nombre; `x-claude-remote-session-id`, `x-claude-remote-container-id`, `x-claude-remote-session-origin` y `x-client-app`, que Claude Code copia de su entorno; `x-claude-code-prev-tool-durations`, una indicación para las pasarelas que nombra las herramientas ejecutadas (entre ellas los nombres propios de un servidor MCP); cualquier otra cabecera `x-claude-code-*`, que Latet todavía no nombra |
| Bedrock: el SDK de AWS | `amz-sdk-*`, `x-amz-user-agent` |
| Gemini: la biblioteca cliente de Google | `x-goog-api-client` |
| Codex, en el formato de OpenAI y en el de ChatGPT | `x-codex-turn-metadata`, que contiene la ruta, los remotos y el commit de tu repositorio; `x-openai-subagent`, el tipo o una etiqueta libre de un subagente |

Las cabeceras `x-latet-*` propias de Latet tampoco llegan nunca a un proveedor. Una credencial nunca
se quita. En una petición que Latet solo comprueba (Claude Code, Bedrock, Vertex), el cuerpo sale byte
a byte: estas cabeceras son el único cambio. `client_metadata` de Codex, la copia de
`x-codex-turn-metadata` en el cuerpo, se quita del cuerpo de una petición que Latet reescribe.

Cada intercambio del panel cuenta las cabeceras y los miembros del cuerpo que se le quitaron. Cuando
el proveedor responde `401` o `403` a una petición a la que se le quitaron algunas, se le dice al
agente, y el intercambio dice que esa cabecera puede ser una que el proveedor necesita:

```text
Latet: the provider answered 401 after Latet removed 2 SDK/tracing headers
```

## Nuevos miembros de las peticiones

Una nueva versión de un agente añade a menudo un miembro a sus peticiones: un modo, un indicador, un
ajuste propio. Antes de la 0.15.0, Latet rechazaba una petición con un miembro que ningún perfil de
petición nombra, salvo que, en una petición que solo comprueba, el miembro contuviera solo texto: el
agente se detenía hasta que una versión de Latet nombrara ese miembro. Desde la 0.15.0, Latet lee un
miembro así entero, cada nombre y cada valor que contiene, lo comprueba con el mismo rigor que un
texto que enmascara, y lo envía tal como llegó cuando nada en él se enmascararía y no nombra nada
sobre lo que actúe el proveedor. Es `unknown_members: check`, el valor predeterminado, en cada
petición: en las que Latet solo comprueba (Claude Code, Bedrock, Vertex) y en las que reescribe
(Codex, OpenCode, Gemini CLI, Hermes Agent, los SDK). Un miembro así nunca se enmascara: sale tal como
llegó, o la petición se detiene.

**Lo que pasa:** un miembro hecho de valores de control, como
`{"mode": "compact", "level": 2, "on": true}`: un modo, un indicador, un número, un token corto, o un
objeto o una lista de ellos.

**Lo que sigue deteniendo la petición,** con las palabras y los códigos de la 0.11, esté donde esté
dentro del miembro:

- una dirección web o cualquier otro URI, incluido uno `data:`, o el ID de un archivo, de una
  conversación, de una respuesta guardada o de otro recurso que el proveedor conserva (`file-…`,
  `conv_…`, `resp_…`, `gs://…`);
- un nombre que dice que se trata de algo sobre lo que el proveedor actuaría, como un archivo, un
  adjunto, una imagen, un documento, una dirección web, un servidor, un servidor MCP, un contenedor,
  una conversación, una caché o un modelo;
- datos codificados: una secuencia de 64 caracteres base64 o más que mezcla dígitos, mayúsculas y
  minúsculas, o de 48 dígitos hexadecimales;
- un número que no es finito, o más de lo que Latet lee de una petición;
- más de lo que Latet lee de los miembros nuevos de una petición: un nombre de más de 256
  caracteres, más de 4.096 textos distintos en total (cada nombre y cada valor, y un valor una vez
  más por cada campo en el que está), más de 256 KiB de esos textos, o miembros que el modelo local
  de nombres no puede leer en los 5 segundos que tiene para un documento;
- dentro de un elemento propio del proveedor (thinking, reasoning, los resultados de una búsqueda
  web, la fuente de una imagen), cualquier cosa que no sea un token corto: texto libre, una clave o
  un valor de más de 64 letras, dígitos, `_`, `.`, `:` o `-`, o más de 32 valores o 2.048 bytes en
  total;
- un valor sensible: cualquier cosa que tus reglas enmascararían o suprimirían en un texto (un
  secreto, un nombre, una dirección de correo, un número de teléfono, un valor que nombra una de tus
  reglas de campo, por su clave o su ruta), o un valor que guarda tu caja fuerte. La petición se
  detiene como lo haría por un valor así en cualquier otro texto, y el panel indica el tipo del
  valor, *Nuevo miembro de la petición*, nunca el nombre del miembro.

Un texto de un miembro así que ya salió pasa de nuevo en la misma conversación, como el texto ya
enviado, incluso después de que Latet aprenda un valor que contiene: una conversación nunca se queda
atascada por ello. En el panel, un intercambio que dejó pasar uno incluye *un campo que ningún perfil
de petición prevé todavía, leído y comprobado* entre lo que lo dejó pasar. El nombre de un miembro
nunca se registra, se anota en los registros ni se muestra.

### Volver a rechazarlos

`unknown_members: refuse` rechaza un miembro así como hacía Latet antes de la 0.15.0: una petición que
lleva uno se rechaza, salvo que, en una petición que Latet solo comprueba, el miembro contenga solo
texto. En `config.yaml`:

```yaml
unknown_members: refuse
```

o en el panel, en *Nuevos miembros de las peticiones* dentro de los datos de producción de
**Reglas**. El cambio a `refuse` se aplica en el acto. La vuelta a `check` reduce la protección:
hecha en `config.yaml`, o por un agente, espera tu aprobación en **Aprobaciones**; hecha en el
panel, guardar es tu aprobación. En un servidor de Latet cuyas [aprobaciones
centrales](../../enterprise/approvals/) (Latet Enterprise) envían estos cambios al equipo de
seguridad de tu empresa, espera en cambio su decisión, incluso si se guarda en el panel. Donde las
directrices de tu empresa bloquean `unknown_members: refuse`, `refuse` sigue en vigor digan lo que
digan tus ajustes, y el panel solo ofrece ese valor. Una petición rechazada con `refuse` dice en el
panel que `unknown_members: refuse`, en tus ajustes, en los del proyecto o en las directrices de tu
empresa, rechaza miembros así, y lo que haría `check`, con un enlace a esta sección; donde una
excepción exacta puede llegar al miembro, sigue diciendo cómo aprobar una, porque una excepción se
lee antes que este ajuste.

- **Un proyecto solo puede rechazarlos.** `unknown_members: refuse` en tus ajustes para un proyecto
  (su entrada en `projects` de `config.yaml`) o en el `latet.yaml` de su repositorio los rechaza solo
  en ese proyecto. `check` corresponde solo a tus ajustes: la entrada de un proyecto en `config.yaml`
  solo admite `refuse`, y un `latet.yaml` que dice `check` aparece en *Lo que no se aplica*.
- **El `refuse` de un repositorio vincula a un proyecto de confianza o declarado.** Como toda regla
  de un proyecto que añade protección, se aplica a las peticiones que Latet sitúa en ese proyecto,
  tanto si lo considera de confianza como si una petición lo declara: un cliente que nombra otra
  carpeta no queda vinculado, salvo que su clave de lanzamiento lo vincule al proyecto (consulta
  [Las carpetas que nombra una petición](#las-carpetas-que-nombra-una-petición)).
- **En un servidor de Latet**, cada persona y cada clave de API tiene su propio valor, `check` salvo
  que ponga `refuse`, y `latet server status` dice el valor en vigor (`new request members: …`). Con
  Latet Enterprise, las directrices de la empresa pueden bloquear `unknown_members: refuse` para cada
  persona y cada clave de API ([Reglas bloqueadas](../../enterprise/locked-rules/)); como toda regla
  bloqueada, sigue en vigor cuando la licencia termina.
- **`latet doctor`** dice el valor en vigor, en *New request members*, y, mientras `config.yaml` no
  tenga `unknown_members`, que `check` es el valor predeterminado y cómo volver a rechazarlos.

### Lo que no cubre

- **El revisor local**, el modelo de lenguaje que lee los textos por categorías, no lee estos
  miembros: lo hacen los detectores de Latet y tu caja fuerte.
- **El modo de datos estricto** no lee estos miembros: cubre las salidas de herramientas, los
  archivos y los hosts que nombra, y un miembro nuevo no es ninguno de ellos. Tus reglas de campo
  los leen, por la clave o la ruta de un valor, y también los detectores de Latet.
- **Los datos codificados no se descodifican:** un secreto en base64 más corto que las secuencias de
  arriba, o escrito de una forma propia, se lee como el texto que es, como en cualquier texto que
  Latet comprueba.
- **En una petición que Latet reescribe,** un valor sensible en un miembro así detiene la petición en
  lugar de salir como un valor ficticio, cada vez que se envía de nuevo, salvo que ya saliera en esa
  conversación. Un cliente que copia en un miembro nuevo los valores que Latet restauró se detiene
  hasta que una versión de Latet nombre ese miembro.
- **En una petición que Latet solo comprueba,** un miembro nuevo cuyo valor es un texto o una lista
  de textos, fuera de un elemento del proveedor, mantiene la regla de la 0.11 (*solo comprobada*): la
  comprobación final detiene en él los secretos, tus términos, tu identidad y los datos personales,
  pero no busca nombres ni aplica allí tus reglas de campo ni el modo de datos estricto, para que nada
  de lo que salía antes de la 0.15.0 se rechace ahora.
- **El `refuse` de un repositorio** solo vincula las peticiones que Latet sitúa en su proyecto, como
  arriba.
- **La actualización pasó cada instalación a `check`**, servidores incluidos, sin aprobación: si
  contabas con el rechazo, pon `unknown_members: refuse`.

## Lo que comprueba `latet doctor`

Ejecuta `latet doctor` antes de actualizar, y otra vez después. Enumera, por tipo y por número, nunca
un valor:

- los proyectos en los que Latet está desactivado (las peticiones de cada uno se rechazarán);
- el reconocimiento de texto sin instalar, o desactivado con `ocr: off` (cada petición con una
  imagen se rechazará);
- `--passthrough` en una entrada que inicia Latet al arrancar la sesión;
- las llamadas vistas en tu historial que ningún perfil cubre, por su método y su plantilla de ruta,
  nunca su ruta concreta;
- tus excepciones permanentes;
- los agentes que Latet lanzó cuyas peticiones llegaron sin su clave de lanzamiento, con el agente y
  su versión.

El daemon dice lo mismo en su registro la primera vez que arranca con la 0.11.

## Instalar el reconocimiento de texto

```sh
latet ocr install   # unos 31 MB, una sola vez
```

`latet upgrade` añade lo que los modelos necesitan para ejecutarse a un Latet instalado sin ello. En
Docker: `docker exec latet latet ocr install --yes`. En un servidor de Latet, los administradores
instalan los modelos en cada instancia
([Modelos de reconocimiento en un servidor](../../concepts/server-operations/#modelos-de-reconocimiento-en-un-servidor)).
Consulta [Capturas de pantalla e imágenes](../../concepts/images/).

## Las cabeceras que defines para tus agentes

Un agente configurado con cabeceras tuyas las envía con cada petición, y la 0.11 las rechaza hasta que
las dejes pasar: `ANTHROPIC_CUSTOM_HEADERS` de Claude Code, `http_headers` y `env_http_headers` de
Codex, `COPILOT_PROVIDER_HEADERS` de Copilot CLI, `options.headers` de OpenCode,
`GEMINI_CLI_CUSTOM_HEADERS` de Gemini CLI. El rechazo nombra la cabecera. Deja pasar cada una con una
excepción exacta, como abajo, nombrando el destino al que va; la clave de una pasarela de empresa en
una cabecera se nombra como credencial, que Latet envía tal como llegó y nunca lee. La clave de
lanzamiento de Latet, que `latet claude` y los demás añaden a estos ajustes, no tienes que nombrarla:
Latet la quita antes de reenviar nada.

Una cabecera que defines para Claude Code cuyo nombre empieza por `anthropic-` no necesita ninguna
excepción: pasa una vez leída por la comprobación final, como una cabecera nueva del propio Claude
Code ([Cabeceras](#lo-que-la-011-rechaza-y-la-010-enviaba)); una cuyo nombre empieza por
`x-claude-code-` se quita. Una credencial de un destino en una cabecera de este tipo se nombra de
todos modos con una excepción, para que Latet la envíe tal como llegó y nunca la lea. Una excepción
que nombra una cabecera de este tipo decide sobre ella incluso donde no se aplica: en una petición
de otro proyecto, o cuando la excepción ha caducado, la cabecera se rechaza (nombrándola, o
nombrando la excepción y el día en que caducó), y nunca se lee para enviarse después en lugar de la
excepción. Quita la excepción si la cabecera debe pasar en todas partes tras comprobarse.

## Aprobar una excepción exacta

Una excepción nombra exactamente lo que deja pasar, en un destino y un perfil de petición: una
cabecera, un parámetro de consulta o un miembro del cuerpo JSON. Por ejemplo, para que la cabecera
`x-team` de Claude Code llegue a Anthropic:

```sh
latet config add request_exceptions '{destination: anthropic, profile: anthropic.messages.1, components: [{kind: header, name: x-team}], reason: custom-header}'
```

Lo mismo en `config.yaml`:

```yaml
request_exceptions:
  - destination: anthropic           # la ruta de un proveedor, o uno de tus destinos
    profile: anthropic.messages.1    # un método, una ruta y un transporte
    components:
      - {kind: header, name: x-team}                  # comprobada: su valor pasa la comprobación final
    reason: custom-header
  - destination: llm-acme-com
    profile: openai.chat.1
    components:
      - {kind: header, name: x-acme-key, role: credential}   # enviada tal como llegó, nunca leída
    reason: destination-credential
  - destination: openai
    profile: openai.responses.1
    components:
      - {kind: body, name: /metadata_x/team, type: string}  # un puntero JSON exacto y el tipo de su valor
    reason: other
```

- **Espera tu aprobación.** Una excepción reduce la protección, así que solo se aplica cuando la
  apruebas, en la página **Aprobaciones** del panel, en *Ajustes*. Latet le da entonces un ID
  (`x1a2b3c4d5e`) y la hora de su aprobación. Quitar una, o hacer que nombre menos, se aplica en el
  acto.
- **Dura 30 días** desde su aprobación, de forma predeterminada, y 90 como máximo (`days`). El panel
  avisa siete días antes de que acabe, y una petición rechazada después nombra la excepción y el día en
  que terminó. Renovarla es una aprobación nueva.
- **Permanente, a propósito.** `permanent: true` hace una que nunca caduca. Se aprueba como cualquier
  excepción, siempre aparece marcada como *permanente* en el panel, y la enumeran `latet doctor` y, en
  un servidor, `latet server status`; aprobar una en un servidor escribe una entrada en el registro de
  administración, solo con su ID y el tipo de su alcance.
- **De quién son las peticiones.** Una excepción sin `project` se aplica a todas tus peticiones, sea cual
  sea el agente que las envíe, y el panel lo dice antes de que la apruebes. Con
  `project: C:\dev\acme`, solo se aplica a las peticiones cuyo proyecto Latet considera de confianza
  allí: un agente que lanzó con su clave, o Claude Code con el plugin (consulta
  [Proyectos](../../concepts/projects/#en-qué-proyecto-está-una-sesión)).
- **Nunca en un repositorio.** El `latet.yaml` de un proyecto no puede contener excepciones. Como
  máximo 128, 32 para un proyecto, cada una con hasta 64 componentes; `description` es solo para ti,
  nunca aparece en un registro.
- **No en un WebSocket.** En la 0.11.0 una excepción no se aplica a un WebSocket: Codex con la sesión
  iniciada con ChatGPT, que usa uno, no puede enviar una cabecera tuya. El panel no ofrece ninguna para un
  rechazo ahí, y dice *En esta versión de Latet, un WebSocket no admite excepciones de petición.*
- **Después de una actualización.** Una versión posterior que cambia una parte de un perfil que
  nombra una excepción la suspende hasta que vuelvas a aprobarla; el panel la muestra como *Hay que
  aprobarla de nuevo: su perfil se revisó*, y `latet doctor` dice que hay que aprobarla de nuevo,
  también las permanentes; en un servidor, `latet server status` la enumera en cuanto el espacio de la
  identidad ha arrancado con la nueva versión. Una actualización nunca amplía lo que deja pasar una
  excepción.

El perfil es el tipo de llamada. Una excepción nombra un solo perfil, así que una cabecera que un
agente envía en cada llamada necesita una excepción por cada tipo de llamada que hace:

| Llamadas | Perfil |
| --- | --- |
| Anthropic Messages (Claude Code, el SDK de Anthropic), recuento de tokens, la lista de modelos, un modelo y la llamada que abre la conexión de Claude Code al arrancar (un HEAD sin cuerpo) | `anthropic.messages.1`, `anthropic.count_tokens.1`, `anthropic.models.1`, `anthropic.model.1`, `anthropic.hello.1` |
| El formato de OpenAI (OpenAI, OpenRouter, una pasarela de empresa): Chat Completions, los antiguos completions, Responses por HTTP, embeddings, la lista de modelos y un modelo | `openai.chat.1`, `openai.completions.1`, `openai.responses.1`, `openai.embeddings.1`, `openai.models.1`, `openai.model.1` |
| Codex con la sesión iniciada con ChatGPT, por HTTP: Responses y la lista de modelos | `chatgpt.codex.responses.1`, `chatgpt.codex.models.1` |
| Gemini, por la API de Gemini o por Vertex AI: generación, streaming, recuento, embeddings (uno o por lotes), la lista de modelos y un modelo, crear o cambiar una caché, el chat y los embeddings compatibles con OpenAI | `gemini.<version>.generateContent.1`, `gemini.<version>.streamGenerateContent.1`, `gemini.<version>.countTokens.1`, `gemini.<version>.embedContent.1`, `gemini.<version>.batchEmbedContents.1`, `gemini.<version>.models.1`, `gemini.<version>.model.1`, `gemini.<version>.cache-create.1`, `gemini.<version>.cache-patch.1`, `gemini.<version>.openai.completions.1`, `gemini.<version>.openai.embeddings.1` |
| Bedrock: invocación, streaming, recuento de tokens, los perfiles de inferencia y uno de ellos | `bedrock.invoke.1`, `bedrock.invoke-with-response-stream.1`, `bedrock.count-tokens.1`, `bedrock.inference-profiles.1`, `bedrock.inference-profile.1` |
| Claude en Vertex AI | `vertex.anthropic.rawPredict.1`, `vertex.anthropic.streamRawPredict.1` |
| Una pasarela de empresa delante de Bedrock o de Claude en Vertex AI | `company.bedrock.invoke.1`, `company.bedrock.invoke-with-response-stream.1`, `company.bedrock.count-tokens.1`, `company.vertex.rawPredict.1`, `company.vertex.streamRawPredict.1` |
| Destinos de Azure OpenAI | `azure-openai.chat.1`, `azure-openai.completions.1`, `azure-openai.responses.1`, `azure-openai.embeddings.1` |
| Destinos de Microsoft Foundry | `foundry.chat.1`, `foundry.embeddings.1` |

En un ID de Gemini, `<version>` es la que nombra la ruta de la petición: `v1` o `v1beta` en la API
de Gemini, `v1` o `v1beta1` en Vertex AI (`gemini.v1beta.generateContent.1`).

En un servidor de Latet, las excepciones forman parte de los ajustes propios de cada persona, que las
aprueba en el panel; las de una clave de API se definen desde la administración.

## Bedrock y Vertex: qué hay que enumerar

Un ID de modelo escrito en la forma que documenta un proveedor pasa por sí solo, en una ruta igual que
en un cuerpo, y lo mismo un perfil de inferencia de Bedrock definido por Amazon
(`us.anthropic.claude-…`, `eu.`, `apac.`, `global.`). Lo que pertenece a tu cuenta o a tu proyecto en
la nube debe figurar en `request_resources` antes de que se envíe una petición que lo nombre: cualquier
ARN en una ruta de Bedrock (incluido el ARN de un perfil de inferencia de aplicación) y el proyecto de
Vertex. Cada fila nombra el destino, su dirección, el papel del valor y el valor:

```yaml
request_resources:
  - destination: bedrock
    url: https://bedrock-runtime.us-east-1.amazonaws.com    # llamadas al modelo; las lecturas van a https://bedrock.us-east-1.amazonaws.com
    role: model                                             # inference-profile, para leer uno
    value: arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/a1b2c3d4e5f6
  - destination: vertex
    url: https://us-east5-aiplatform.googleapis.com
    role: project
    value: acme-ml-prod
```

`latet config add request_resources '{...}'` añade una fila. Una fila deja pasar exactamente ese valor hacia
ese destino y nada más, y se aplica en el acto. Mientras falte, una petición así se rechaza con
`envelope-resource`. Un ID de modelo fuera de las formas documentadas (los nombres de modelos propios
de una pasarela de empresa) se enumera igual, con el papel `model`.

En OpenRouter un modelo se nombra con su fabricante. Los de Anthropic, OpenAI y Google pasan por sí
solos cuando la parte que sigue al fabricante está en la forma documentada de ese proveedor, con o sin
una variante como `:free` o `:thinking` (`anthropic/claude-sonnet-4.5`, `openai/gpt-4o`,
`google/gemini-2.5-pro`), y también `openrouter/auto`. El modelo de cualquier otro fabricante
(`meta-llama/…`, `deepseek/…`) se enumera con el destino `openrouter`, la dirección
`https://openrouter.ai` y el papel `model`.

## Un proyecto en el que Latet está desactivado

En la 0.10, un proyecto en el que Latet estaba desactivado enviaba sus peticiones sin enmascarar. En
la 0.11 Latet no envía ninguna, por HTTP ni por un WebSocket, venga el proyecto del lanzamiento del
agente, del plugin o de una carpeta que nombre la petición. Al agente se le dice:

```text
Latet is off in this project: Latet sends no request from there; turn it on for the project (`latet project on` in its folder, or the project's switch in the dashboard), or reach the provider directly for this folder by starting the agent without Latet's address. Nothing was sent; run `latet dashboard` to see where it was
```

Cuando el proyecto solo lo nombra la petición (declarado, más abajo), las palabras empiezan por
`request claimed a project where Latet is off:` y siguen igual.

`latet project off` y el interruptor del panel lo dicen antes de desactivar un proyecto. Para trabajar
en esa carpeta sin Latet, arranca allí el agente sin la dirección de Latet: un agente que arrancas con
`latet claude`, `latet codex` u otro comando `latet` arranca por sí solo (`claude`, `codex`); uno
cuyos ajustes apuntan a Latet (después de `latet global-install`) pasa por Latet arranque donde
arranque, así que dale para esa sesión la dirección de su proveedor, como dice su documentación, o
usa `latet agents revert`. Latet no ve entonces nada de lo que envía. Si habías desactivado Latet para
dejar pasar los datos personales, *Solo secretos* hace justo eso y sigue protegiendo los secretos
([Proyectos](../../concepts/projects/#solo-secretos-o-latet-desactivado-en-un-proyecto)).

## Las carpetas que nombra una petición

Una petición puede nombrar su carpeta: la cabecera `x-latet-project`, o la carpeta de trabajo que
Codex, OpenCode y Gemini CLI escriben en sus peticiones. En la 0.11 una carpeta así está *declarada*:
las reglas de su proyecto que añaden protección se aplican, sus relajaciones no (valores que nunca se
enmascaran, reglas de secretos desactivadas, un perfil más débil, `data_tools`, las indicaciones y las
imágenes del revisor local). Un proyecto declarado en el que Latet está desactivado se rechaza, como
arriba. Un proyecto es *de confianza* cuando Latet lo conoce por sí mismo: por la clave de lanzamiento
de un agente que lanzó, o por el plugin de Claude Code. El panel muestra para cada petición si su
proyecto era de confianza o declarado, y cómo se decidió su lanzamiento. Consulta
[Proyectos](../../concepts/projects/#en-qué-proyecto-está-una-sesión).

Lo que significa para cada agente:

- **Lanzado por Latet** (`latet claude`, `latet codex`, `latet opencode`, `latet gemini`,
  `latet copilot`): de confianza gracias a su clave de lanzamiento, donde el agente la lleva. Codex en
  sus proveedores integrados (con la sesión iniciada con ChatGPT, o el proveedor propio de OpenAI), Codex anterior a la
  0.115, OpenCode anterior a la 1.19.5, Gemini CLI en modo ACP, Copilot CLI fuera de Chat Completions
  y los programas que arranca `latet run` no llevan
  ninguna: el comando que los lanza lo dice en una frase, y sus proyectos son declarados.
- **Claude Code con el plugin**, configurado de forma permanente: de confianza gracias al plugin.
- **Cualquier otro agente configurado de forma permanente**: declarado. Tus propias reglas y cualquier
  excepción sin `project` se siguen aplicando.

## Conversaciones a través de una pasarela de empresa

Claude Code a través de LiteLLM u otra pasarela de empresa que reenvía las firmas del razonamiento
depende de que Latet haya visto volver esas firmas. La regla integrada para los campos cifrados del
proveedor solo vale para la dirección del propio proveedor, nunca para una ruta de empresa. Tras un
reinicio del daemon, o en un servidor cuando ya no están los datos de la conversación, una conversación
así se rechaza en su siguiente turno: empieza una nueva.

## Volver a la 0.10

```sh
latet upgrade --to 0.10.4
```

Volver a la versión con la que se hizo una copia de tus datos ofrece restaurar esa copia
([Actualizar](../installation/#actualizar)). Latet 0.10 ignora `request_exceptions` y
`request_resources`, y vuelve a enviar lo que la 0.11 rechaza: las partes no leídas de las peticiones,
las peticiones de los proyectos en los que Latet está desactivado y las imágenes que no sabe leer.
Vuelve atrás solo si lo tienes en cuenta.

Hay un ajuste que la 0.10 no conoce: `request_profile`, en un destino de `upstreams`. La 0.11 solo
lo escribe para un destino en el que elegiste un perfil, en `config.yaml` y en
`config.reviewed.json`, los ajustes que aprobaste por última vez, en la carpeta de datos de Latet. La
0.10 no arranca si está en `config.yaml`, y lee un `config.reviewed.json` que lo contiene como si no
hubiera ajustes aprobados: un cambio en `config.yaml` se aplicaría entonces sin esperar tu
aprobación. Si elegiste uno, restaura la copia que hizo la actualización a la 0.11, que contiene los
dos archivos tal como estaban antes: `latet upgrade --to` la ofrece al volver atrás (está en la
carpeta de datos, en `backups/before-<versión>-<hora>`, con el nombre de la versión instalada, por
ejemplo `before-0.15.1-…`). Sin esa copia (la 0.11 instalada con el
instalador, que no hace ninguna), quita la clave de los dos archivos antes de volver atrás: las líneas
`request_profile:` de `config.yaml`, y cada miembro `"request_profile": "…"` de
`config.reviewed.json`, dejándolo como JSON válido.

En un servidor de Latet, la 0.11 añade tablas a la base de datos, y una instancia rechaza una base de
datos más reciente que ella: volver atrás requiere la copia de seguridad de la base hecha antes de
actualizar, con todas las instancias paradas
([Actualizar y añadir instancias](../../concepts/server-operations/#actualizar-y-añadir-instancias)).
