---
title: La API para aplicaciones
description: Analiza, enmascara y restaura texto desde tus propios programas, con una clave de API de un servidor de Latet.
sidebar:
  label: Descripción general
---

Latet protege lo que un agente envía a un modelo. Pero los programas también envían texto sensible a
otros sitios: sus registros a Datadog, Splunk o Elastic, sus documentos al índice de una aplicación
RAG. La API para aplicaciones permite a un programa pedir a un servidor de Latet, con el token de una
clave de API, que:

- **analice** un texto (`scan`): qué contiene de sensible, y dónde;
- lo **enmascare** (`mask`): el mismo texto con valores ficticios realistas en lugar de sus valores;
- lo **restaure** (`unmask`): los valores reales de nuevo en lugar de los ficticios;
- **olvide** valores (`forget`): los quite de la caja fuerte de la clave, cuando una persona pide que
  sus datos desaparezcan ([olvidar valores](forget/)).

Es una función del servidor de Latet: las claves de API y sus tokens solo existen allí, y un equipo
sin servidor no tiene nada de ella. Para probarla en tu propio equipo, arranca en él un servidor
Community con el perfil de servidor de `compose.yaml` ([operación del servidor](../concepts/server-operations/)).

Lo que aporta frente a una regla de ocultación:

- **El mismo valor ficticio cada vez.** Un valor recibe el mismo valor ficticio en cada llamada, en
  cada instancia del servidor, mientras la caja fuerte de la clave lo conserve. En un servicio de
  registros, los gráficos y las búsquedas siguen funcionando, y se puede seguir a un cliente durante
  días de registros sin que nadie vea quién es. En un índice RAG, una persona sigue siendo la misma
  persona de un documento a otro.
- **Una sola caja fuerte para todo el flujo.** Los documentos y las preguntas enmascarados con la
  misma clave comparten sus valores ficticios, así que un modelo razona sobre valores ficticios
  coherentes, y `unmask` vuelve a poner los valores reales en su respuesta.
- **Reversible, bajo control.** Restaurar es un permiso propio, solo alcanza los valores ficticios de
  la clave, y cada restauración a través de la API para aplicaciones queda escrita en el registro de
  administración del servidor.

Dos páginas recorren los dos usos para los que se hizo: los [registros](logs/) y el [RAG](rag/). La
[referencia HTTP](../reference/apps-api/) da el contrato para programas en cualquier lenguaje, y la
[descripción OpenAPI](../../apps-api.openapi.json) de la API permite generar un cliente a partir de
ella. Los programas en Python pueden usar el paquete
[`latet-client`](https://pypi.org/project/latet-client/), que funciona con LangChain, y los flujos de
n8n los [nodos de Latet para n8n](n8n/).

## Una primera llamada

Un programa envía una lista de textos y recibe una lista, en el mismo orden:

```sh
printf 'Authorization: Bearer %s' "$(cat shop.key)" | curl -sS -H @- \
  -H "Content-Type: application/json" \
  -d '{"texts": ["Reembolsa el pedido de mario.rossi@acme.it a IT60X0542811101000000123456, por favor."]}' \
  https://latet.acme.lan/_latet/apps/v1/mask
```

```json
{"results": [{"text": "Reembolsa el pedido de tommaso.gallo@margie-6e5a.example a IT17F3945471087320356678487, por favor.",
  "replacements": [{"type": "EMAIL", "class": "pii", "start": 23, "end": 56},
                   {"type": "IBAN", "class": "pii", "start": 59, "end": 86}],
  "counts": {"EMAIL": 1, "IBAN": 1}}]}
```

`shop.key` contiene el primer token de la clave, el que escribe `keys add`
([más abajo](#claves-de-api-y-tokens)).
`printf`, que la propia shell ejecuta, pasa la cabecera a curl por su entrada estándar (`-H @-`): así
el token no aparece en la línea de órdenes de curl, donde otros usuarios del equipo podrían leerlo.
Los valores ficticios son los de la clave: otra clave, u otro servidor, da otros. Con un token que
puede restaurar, el mismo texto enviado a `/_latet/apps/v1/unmask` vuelve como era, con
`"restored": 2`.

## Claves de API y tokens

Una clave de API es un espacio en el servidor: sus reglas, su caja fuerte de valores ficticios, su
historial. Puede tener varios **tokens**, los secretos que actúan en ese espacio, y cada token tiene
sus propios **permisos**:

| Permiso | Lo que puede hacer el token |
| --- | --- |
| `scan` | Decir qué contiene un texto y dónde; no cambia nada. |
| `mask` | Sustituir los valores de un texto por los valores ficticios de la clave. Un token que puede enmascarar también puede analizar. |
| `unmask` | Volver a poner los valores reales en lugar de los valores ficticios de la clave. |
| `forget` | Quitar valores de la caja fuerte de la clave ([olvidar valores](forget/)). Ningún otro permiso lo incluye. |

Da a cada programa solo lo que necesita. Una aplicación que envía sus registros necesita `mask`; la
herramienta de soporte que lee una línea copiada del servicio de registros necesita `unmask`; una
aplicación RAG que enmascara sus documentos y restaura la respuesta del modelo necesita los dos.

```sh
latet server --server https://latet.acme.lan keys add "Shop" --out shop.key
latet server --server https://latet.acme.lan keys add-token "Shop" shipper --mask --out shipper.token
latet server --server https://latet.acme.lan keys add-token "Shop" support --unmask --out support.token
latet server --server https://latet.acme.lan keys tokens "Shop"
```

El primer token de una clave, el que crea `keys add`, tiene `scan`, `mask` y `unmask`, igual que el
token de cada clave creada antes de que los tokens tuvieran permisos: esas claves funcionan como antes.
El primer token de la clave tiene además una dirección para modelos, la
[puerta de enlace](../getting-started/server/#dar-una-clave-de-api-a-un-programa), y a través de ella
sirve a los agentes de la clave: `keys tokens` lo señala con `serves agents (has an address)`, para que
no se revoque en lugar de un token añadido con los mismos permisos. Un token creado con `keys add-token` no tiene dirección, tenga los
permisos que tenga, `--mask --unmask` incluidos: la puerta de enlace y el bridge del plugin lo
rechazan (403), y solo llama a la API para aplicaciones.

Cada token se detiene por separado: `keys tokens "Shop" --revoke t_4kxq2m7zap` revoca uno por el id
que muestra `keys tokens`, en pocos segundos en cada instancia. `keys rotate "Shop" --token
t_4kxq2m7zap --out shipper.token` sustituye ese token por uno nuevo, con un secreto nuevo y un id
nuevo, el mismo nombre y los mismos permisos; una clave con un solo token no necesita `--token`. Cada
cambio queda en el registro de administración (`token.added`, `token.revoked`, `key.rotated`), con los
ids de los tokens, nunca un secreto.

La API para aplicaciones solo acepta tokens de claves de API: el token del equipo de una persona se
rechaza (403), igual que una solicitud que hace una página web. Las personas usan su panel.

## Prosa y datos

`scan` y `mask` aceptan un `kind` para los textos de una llamada:

- `prose` (el predeterminado): texto escrito por personas, como mensajes, tickets y documentos. Se
  enmascara como la puerta de enlace enmascara tus propios mensajes.
- `data`: registros, fichas, la salida de un programa. Se enmascaran como la puerta de enlace
  enmascara la salida de una herramienta.

La diferencia está en lo que es probable: en el perfil `balanced`, los nombres solo se buscan en la
prosa (con el [modelo de nombres local](../concepts/server-operations/#modelos-de-reconocimiento-en-un-servidor)
instalado en el servidor), y una serie de dígitos solo cuenta como número de teléfono en la prosa,
mientras que en un registro es mucho más a menudo un id. Con `phone_regions: [IT]` en las reglas de la
clave, el número en `Llámame mañana al 3471234567.` recibe un valor ficticio como prosa y se queda
como está como datos. Un documento JSON enviado como texto se enmascara como texto, conservando su
formato, así que las posiciones de la respuesta son posiciones en él.

## Qué lee un enmascaramiento

`scan` y `mask` usan los ajustes de la clave tal como están cuando llega la llamada: las reglas
propias de la clave sobre los ajustes compartidos del servidor, como hace la puerta de enlace con las
solicitudes de la clave. No hay reglas solo para la API. Se configuran con `latet server config --key`
y `latet server rules --key`
([ajustes compartidos y reglas de las claves de API](../getting-started/server/#establecer-los-ajustes-compartidos-y-las-reglas-de-las-claves-de-api)).

- **El revisor local**, cuando las reglas de la clave indican uno, lee cada texto de una llamada
  `mask` antes de la respuesta, diga lo que diga `llm_review.inline` para la puerta de enlace: una
  aplicación no tiene un momento posterior. Dispone de `llm_review.budget_ms` si está configurado, y
  si no de 30 segundos. Un texto cuya revisión falla sigue `llm_review.on_failure`: con `stop` (el
  predeterminado) la respuesta es `blocked`, con `send` es el texto tal como lo enmascararon las
  reglas. Así que, con un revisor caído, cada texto se bloquea hasta que vuelva a responder.
- **`scan` no ejecuta el revisor local.** El revisor enseña a la caja fuerte lo que encuentra, y el
  análisis no cambia nada. En una clave con revisor, `scan` puede informar de menos de lo que `mask`
  sustituye.
- **El Revisor en directo nunca retiene el texto de una aplicación.** Una aplicación no puede esperar
  a una persona: un texto se enmascara solo con las reglas y el revisor local, aunque las reglas de la
  clave activen el Revisor en directo.
- **La protección contra volcados** se aplica a los textos `data` como a la salida de una herramienta:
  uno que contiene más valores personales de los que permite `dump_guard` recibe `blocked`.

Un texto bloqueado vuelve como `{"blocked": "<motivo>"}` sin texto, y el motivo no nombra ningún
valor. Cuando algo funcionó de forma reducida en la llamada (un detector que no pudo leer un texto,
una revisión que no terminó), la respuesta lo dice en `"degraded"`, con los nombres de lo afectado,
como `ner` o `llm-review`.

## Enmascarado dos veces, restaurado dos veces

`mask` toma por real cada valor que encuentra, también los propios valores ficticios de la clave:
nadie puede saber por una cadena si es un valor ficticio o un valor real que se le parece. Un texto
enmascarado dos veces contiene valores ficticios de valores ficticios, y una restauración lo devuelve
un paso atrás:

| Paso | Texto |
| --- | --- |
| Original | `Escribe a mario.rossi@acme.it` |
| Enmascarado | `Escribe a tommaso.gallo@margie-6e5a.example` |
| Enmascarado de nuevo | `Escribe a simone.desantis@humongous-0e54.example` |
| Restaurado una vez | `Escribe a tommaso.gallo@margie-6e5a.example` |

Enmascara un texto una sola vez, a partir de su original. La puerta de enlace sigue la misma regla: un
texto enmascarado con la API y luego enviado a un modelo a través de la puerta de enlace de Latet se
vuelve a enmascarar allí, y la respuesta del modelo vuelve con los valores ficticios de la API. Por
eso una aplicación RAG envía el texto enmascarado directamente al modelo y restaura la respuesta con
la API ([RAG](rag/)).

## Quién puede saber qué

**Restaurar** da los valores reales de los valores ficticios de la clave, sea cual sea la llamada o la
conversación de la clave que los creó, también los del año pasado. No alcanza los valores ficticios de
otra clave ni los de una persona: el texto que no es un valor ficticio de la clave se queda como está.
El nombre de una persona solo vuelve entero: una parte suya sola (un nombre de pila) puede ser una
palabra corriente, o el nombre de otra persona, y se queda como está. Cada restauración se escribe en el
registro de administración antes de enviar su respuesta (`apps.unmasked`, con el token, cuántos textos y
cuántos valores; nunca un texto ni un valor), y un registro que no puede escribirla rechaza la llamada
(503). El primer token de la clave, que puede analizar, enmascarar y restaurar, también sirve a agentes
a través de la puerta de enlace: sus respuestas se restauran como las de cualquier agente, sin una
entrada del registro de administración por cada restauración. Un token creado con `keys add-token`
nunca lo hace, así que cada una de sus restauraciones es una entrada `apps.unmasked`. Da `unmask` a las
herramientas de las
personas que pueden ver los datos reales, y lee el registro con
`latet server log --action apps.unmasked`.

**Enmascarar también revela algo.** Con un token que puede enmascarar y acceso a los registros
enmascarados, alguien puede confirmar una sospecha: enmascarar `mario.rossi@acme.it` y buscar su valor
ficticio en los registros. Si está, Mario Rossi es cliente. Por eso da `mask` solo a los programas que
lo necesitan, guarda el token donde guardan sus secretos, y trata los registros enmascarados como
datos seudonimizados, no anónimos.

**Analizar también revela algo.** No crea valores ficticios ni cambia nada, pero informa de los
valores que la caja fuerte de la clave ya conoce, dondequiera que aparezcan. Un token que solo puede
analizar puede, por tanto, confirmar que un valor que ningún detector encontraría por sí solo (un
nombre en un texto `data`, un valor que una regla de la clave encontró antes de que la quitaran) es
uno de los de la clave. Da `scan` con el mismo cuidado que `mask`.

**Olvidar también revela algo.** Su respuesta dice cuántos de los valores guardaba la caja fuerte de la
clave, así que un token que puede olvidar puede confirmar que un valor es de la clave, como uno que
puede analizar. Da `forget` solo al proceso que atiende las peticiones de borrado; cada olvido queda en
el registro de administración (`apps.forgotten`, con el token y las cifras, nunca un valor). Ninguno
de los tokens que crea `keys add` puede olvidar: `forget` se da a un token propio
(`keys add-token "Shop" erasure --forget`).

## La caja fuerte de una clave

La caja fuerte de una clave conserva cada valor que ha enmascarado, con su valor ficticio, mientras la
clave exista: una restauración funciona con una línea de registro del año pasado, y un cliente conserva
el mismo valor ficticio durante años. Los valores no caducan: un valor sale de la caja fuerte cuando se
olvida ([olvidar valores](forget/)), y todos se van con la clave, ya que `latet server keys remove`
borra su espacio, y con él su caja fuerte. Un valor encontrado una vez se enmascara desde entonces
dondequiera que aparezca, incluso donde ningún detector lo volvería a encontrar: la dirección de un
cliente que una regla de la clave encontró una vez, y que luego se quitó, sigue enmascarada.

Un espacio de registros puede contener millones de valores, así que una instancia no guarda en
memoria la caja fuerte de una clave. Guarda los valores que la clave usó por última vez
(`key_working_values`, 5.000 por defecto, unos 10 MiB por clave) y encuentra cualquier otro en
PostgreSQL cuando los textos de una llamada lo necesitan, con una sola búsqueda para todos, mediante
un índice de las palabras de los valores y de los valores ficticios. Ese índice contiene hashes
calculados con una clave secreta, nunca una palabra en claro.

**Secretos pegados a otras letras.** En PostgreSQL, un secreto escrito como una sola serie de letras y
dígitos, sin espacios ni puntuación, se encuentra por su palabra, y por sus seis primeros y seis
últimos caracteres cuando otras letras o dígitos están pegados a uno de sus lados. Pegado por ambos
lados (`xhunter2y`), o pegado de cualquier modo cuando la palabra de su borde tiene seis caracteres o
menos, ninguna palabra del texto lo encuentra. Por eso una instancia guarda en memoria para siempre los
secretos cortos de una clave, los de una sola serie de 16 caracteres o menos o con una palabra así de
corta en un borde: los `key_held_secrets` más recientes (1.000 si los ajustes del servidor no dicen
otra cosa), que se encuentran estén pegados como estén. Uno más antiguo se encuentra donde nada está
pegado a él, o donde un lado está libre y tiene siete caracteres o más; uno más largo, dondequiera que
un lado esté libre. Sube `key_held_secrets` para una clave que ve muchos secretos cortos, como un token
de sesión en cada línea de registro: cada uno que se guarda ocupa unos 2,2 KB en cada instancia, y cada
texto que la clave enmascara o restaura se recorre para buscarlo, lo que hace sus llamadas un poco más
lentas. Medida como abajo con un token de sesión propio en cada línea (un valor más que guardar en
cada una), una clave nueva con 1.000 de esos secretos guardados enmascaró 73 líneas por segundo y
restauró 1.935, frente a 75 y 2.103 sin ninguno guardado. Tampoco se encuentra
un valor ficticio pegado de modo que ninguna de sus palabras quede suelta; el enmascaramiento nunca
produce uno. Como en todo Latet, un valor se vuelve a reconocer a partir de cuatro caracteres, el
nombre de una persona a partir de tres ([valores conocidos](../concepts/detection/#valores-conocidos)).

**Qué conserva una llamada que falla.** Una llamada `mask` que falla mientras enmascara (un 507, una
confirmación que no llega a PostgreSQL) no conserva ninguno de sus valores nuevos cuando ocupaba para
ellos una de las transacciones de la instancia, tres a la vez con el `database_pool` predeterminado: el
espacio de la clave los olvida. Si esas estaban ocupadas, la llamada conservó cada valor nuevo sobre la
marcha, y esos se quedan en la caja fuerte de la clave. Un fallo después del enmascaramiento, en la revisión local o al preparar la
respuesta, llega después de la confirmación, así que los valores nuevos de la llamada también se
quedan. Volver a enmascarar los textos es seguro en cualquier caso: un valor que la caja fuerte
conservó tiene su valor ficticio, y uno que olvidó recibe otra vez uno. Un agente de la misma clave
que usó mientras tanto un valor olvidado a través de la puerta de enlace ya envió su valor ficticio,
que ninguna restauración posterior devolverá.

## Límites

- **La caja fuerte.** La caja fuerte de una clave admite los `vault_limit` valores del servidor
  (200.000 si los ajustes del servidor no dicen otra cosa), o un límite propio:
  `latet server keys limit "Shop" 5000000` (de 1.000 a 100.000.000; `default` le devuelve el del
  servidor). Una caja fuerte llena rechaza con 507 la llamada `mask` que añadiría un valor: no vuelve
  nada de la llamada, y nada sale en claro. El estado del servidor lista las identidades al 90 % de su
  límite, y la métrica `latet_vault_near_limit` las cuenta, así que sube un límite antes de alcanzarlo.
  Cada instancia cuenta los valores nuevos de una llamada antes de confirmarlos: dos instancias, o una
  llamada y un agente de la misma clave, pueden superar el límite en los valores nuevos de una llamada
  cada uno antes de que se rechace el siguiente valor.
- **Una llamada** admite como mucho 1.000 textos y el `apps_request_mb` del servidor (8 MiB si sus
  ajustes no dicen otra cosa, nunca más que su `max_request_mb`): una llamada espera en memoria al
  hilo de la clave, y `latet-client` divide una lista más larga en llamadas de 8 MiB como mucho. Los
  textos cuyas palabras nombran más de 100.000 valores o valores
  ficticios de la caja fuerte se rechazan con 422, sin enmascarar nada: envía menos textos por
  llamada.
- **Llamadas simultáneas.** Una clave tiene como mucho `requests_in_flight` solicitudes de
  aplicaciones en una instancia (16 si los ajustes no dicen otra cosa); por encima, 429 con
  `Retry-After`.

## Velocidad

Las llamadas de una clave se ejecutan una tras otra en una instancia, en el hilo de su espacio; las
llamadas de claves distintas, y las que llegan a instancias distintas, se ejecutan a la vez. Para ir
más rápido con una clave, pon más instancias detrás del equilibrador de carga.

Medido en una instancia (Intel i9-10850K, 64 GB, Windows 10, PostgreSQL en Docker Desktop en el mismo
equipo), con líneas de registro de unos 200 caracteres, cada una con una dirección de correo y una
dirección IPv4 pública, la mitad de las cuales nombra a un cliente ya visto, 500 líneas por llamada
como `data`; y con documentos de unos 20 KB de prosa con diez valores cada uno, uno por llamada:

| La caja fuerte de la clave | Enmascarar, líneas por segundo | Restaurar, líneas por segundo | Un documento, enmascarar / restaurar |
| --- | ---: | ---: | ---: |
| Nueva, aprende sus valores sobre la marcha | 189 | 2.691 | 77 / 28 ms |
| 200.000 valores | 440 | 1.691 | 87 / 30 ms |
| 1.000.000 de valores | 410 | 1.498 | 95 / 32 ms |

El enmascaramiento está limitado por los detectores: en un hilo, con todos los valores en memoria y
sin base de datos, el motor enmascara unas 900 líneas como estas por segundo. Una clave nueva además
escribe cada valor nuevo en PostgreSQL. Una instancia mantiene como mucho `database_pool` conexiones
a PostgreSQL (10 si los ajustes del servidor no dicen otra cosa) para el trabajo de todas las
identidades, y deja que tres décimas partes de ellas (tres de diez) las ocupen llamadas que conservan
sus valores nuevos en una transacción cada una; las demás conservan cada valor según llega, más
despacio. Sube `database_pool` para una instancia que enmascara para muchas claves nuevas a la vez, y
con él el `max_connections` de PostgreSQL
([operación del servidor](../concepts/server-operations/#conexiones-límites-y-confianza)).
`scripts/bench_apps.py`, en el repositorio, mide un servidor tuyo de la misma manera.

## Actualizar un servidor

Tras una actualización que cambia cómo se indexan las palabras de una caja fuerte, la primera
instancia que construye el espacio de una clave las vuelve a crear antes de responder a la primera
llamada de la clave: unos 40 segundos para 200.000 valores y cuatro minutos y medio para 1.000.000 en
el equipo indicado arriba. Una llamada que llega mientras tanto espera, y una instancia construye
cuatro espacios a la vez, así que unas pocas claves grandes retrasan las primeras llamadas de las demás
claves en ella. El registro de cambios dice cuándo una versión lo hace; entonces envía a cada clave
grande una llamada, con un tiempo de espera suficiente, antes de que sus programas vuelvan a arrancar.

Mientras instancias de dos versiones se ejecutan a la vez, un valor que escribe una instancia más
antigua lo encuentran las más nuevas mientras lo tienen en memoria, pero no en PostgreSQL hasta que el
espacio de la clave se vuelve a construir. Cuando la API para aplicaciones está en uso, detén cada
instancia más antigua antes de que arranquen las nuevas, aunque las tablas no cambien
([actualizar](../concepts/server-operations/#actualizar-y-añadir-instancias)).
