---
title: Datos de producción
description: Enmascarar los resultados de consultas, las respuestas de API y los registros que lee un agente, todos los valores o campo a campo.
---

Un agente que investiga un fallo suele leer datos reales: una consulta a la base de datos de
producción, la respuesta de una API REST, un registro, una página en el navegador. Los detectores de
Latet encuentran ahí lo que tiene un formato reconocible, como direcciones de correo electrónico,
IBAN, tokens y números de identificación. Una nota, un salario o una cláusula de un contrato no
tienen formato: en una tabla saldrían tal cual.

Latet lee esas salidas por su estructura. La salida de una herramienta suele ser una tabla, un
documento JSON o líneas de pares `key=value`, y el nombre de cada columna o clave dice qué son sus
valores. Hay tres cosas que lo aprovechan:

| | Cuándo | Qué enmascara |
| --- | --- | --- |
| **Columnas secretas** | siempre | los valores de las columnas y las claves con nombre de secreto: `password_hash`, `api_key`, `reset_token` |
| **Reglas de campo** | cuando las escribes | los valores de los campos que indicas, como un tipo propio |
| **Modo de datos estricto** | con el perfil `max`, o cuando lo activas | todos los demás valores de la estructura |

## Las estructuras que lee Latet

- **Tablas:** psql, CSV, TSV y separadas por `;`, markdown, HTML, y las tablas de la instantánea de
  accesibilidad de un navegador (`columnheader`, `cell`), y las tablas dibujadas con caracteres de
  recuadro (`console.table` de Node, el estilo unicode de psql).
- **Pares `key=value`:** dos o más en una línea, como en un registro. Un valor sin comillas termina en su
  primer espacio, salvo un número de teléfono que lo empiece: entonces incluye sus grupos
  (`phone=+39 347 123 4567`).
- **JSON:** un documento entero valor a valor, cada uno con su ruta (`items[].sku`); un fragmento
  `"key": value` dentro de otro texto, como una línea de registro, por su clave.

## Columnas secretas

Siempre están activadas. Un CSV de usuarios:

```text
id,email,api_key,password_hash,reset_token,role
7,ops@acme-intra.com,ak_live_9fK2LmQx7Rt3vW8zB1nY5pD,$2b$12$Kq3v0rZQ8xGm1o5t,8f14e45fceea167a,admin
```

sale así:

```text
id,email,api_key,password_hash,reset_token,role
7,oliver@consolidated-e55f.example,ak_live_3zF1ObRs6Wa1eH2mU6tO2fM,p5cp22yIa9h4bDH9pBx9p8n,a03c15f272977c30,admin
```

La clave, el hash y el token son secretos por su columna, y la dirección de correo electrónico, por
su formato; `admin` se deja tal cual. Una columna secreta sigue enmascarada aunque una regla de campo
deje su campo en claro.

## Modo de datos estricto

Lo activa el [perfil `max`](../profiles/). Con los demás perfiles, o para indicarlo de forma
explícita:

```yaml
data: strict
```

`data: normal` lo desactiva también con `max`; si no pones `data`, decide el perfil. Desactivarlo
donde está activado reduce la protección: si lo hace un agente o la línea de comandos, espera tu
aprobación.

Se enmascaran todos los valores de una tabla, de un documento JSON o de pares `key=value` en la
salida de una herramienta; los nombres de las columnas y de las claves se mantienen, salvo una clave
que es en sí misma un valor, como una dirección de correo, que se enmascara como los valores en
todas partes. El resultado de una consulta:

```text
 id | status  | amount | note
----+---------+--------+-------------------------------
 17 | SHIPPED | 1250.5 | call back on +44 20 7946 0958
```

sale así:

```text
 id | status  | amount | note
----+---------+--------+-------------------------------
 17 | QXWRLTE | 2939.3 | wkqp xoaj zd +44 20 3318 4452
```

- **El mismo valor recibe siempre el mismo valor ficticio.** Los recuentos, las agrupaciones y los
  joins siguen funcionando con lo que lee el modelo.
- **Una consulta escrita con un valor ficticio se ejecuta con el valor real.** El modelo escribe
  `WHERE status = 'QXWRLTE'`; la herramienta ejecuta `WHERE status = 'SHIPPED'`.
- **Un valor ficticio mantiene el formato del valor.** Un número sigue siendo un número con la misma
  cantidad de dígitos; una fecha ISO, una fecha válida (a menos de dos años de la real), y una hora,
  una hora válida. Cuando a un formato no le quedan valores ficticios libres, como con cientos de
  identificadores de tres dígitos en una misma salida, el valor ficticio tiene un carácter más.
- **Un enlace sigue siendo un enlace.** Una dirección web conserva su esquema (`https://`,
  `s3://`...); el resto se enmascara.
- **Lo que nombra un valor o dice cómo se ve una página queda en claro:** un marcador de posición de
  una plantilla o de una shell (`${from}`, `{{ subject }}`, `$MAIL_FROM`), y los atributos `class` y
  `style` del marcado que imprime un comando, salvo que el estilo cargue una dirección. Una fila de
  tabla que no encaja en sus columnas se enmascara entera.
- **Lo que se encuentra dentro de un valor mantiene su propio tipo.** El número de teléfono de la
  nota recibe un número de teléfono ficticio; el resto de la nota se enmascara como nota.
- **Un valor se queda en su estructura.** `SHIPPED` se enmascara en la tabla, nunca en tu código: el
  modo de datos estricto no añade sus valores a los que se enmascaran en todas partes.
- **Los valores de uno o dos caracteres se quedan en claro,** como `17` arriba: dicen poco, y un
  valor ficticio tan corto no se podría distinguir en una respuesta. Los valores vacíos y las
  palabras que significan ninguno o sí y no, como `null`, `n/a`, `true` y `false`, también se quedan
  en claro.
- **Un valor de más de 16.384 caracteres se oculta,** no se sustituye por un valor ficticio: el modelo
  recibe `[REDACTED:DATA]`, y no se restaura nada.
- **Los archivos se dejan tal cual.** La salida de las herramientas que leen archivos (`Read`,
  `Grep`, `Glob`, `LS`... de Claude Code) es código y documentos, no datos, salvo que una regla
  nombre el archivo ([archivos que cuentan como datos](#archivos-que-cuentan-como-datos)). Los demás
  detectores se le siguen aplicando.
- **Y también los resultados web.** Los resultados de búsqueda y las páginas (`WebSearch`,
  `WebFetch`) son contenido público, no tus datos, y llegan como JSON: el modo de datos estricto
  convertiría cada título y cada dirección en letras al azar, y el modelo no tendría nada que leer
  ni que citar. Los demás detectores se les siguen aplicando, así que una clave o una dirección de
  correo electrónico en una página se enmascara como en cualquier otro sitio. Para cubrir de todos
  modos una página web, indica su dirección en `data_hosts`
  ([direcciones web que cuentan como datos](#direcciones-web-que-cuentan-como-datos)). Para cubrir una
  herramienta web, añádela a `data_tools` junto con todas las demás herramientas que el modo de datos
  estricto deba cubrir: en cuanto `data_tools` está definido, el modo de datos estricto cubre solo las
  herramientas que nombra, y `"*"` nombra todas menos las que leen archivos.

### Qué herramientas

El modo de datos estricto cubre la salida de todas las herramientas, excepto las que leen archivos y
las herramientas web. Para cubrir solo algunas, indícalas, con `*` para cualquier secuencia de
caracteres:

```yaml
data: strict
data_tools: [mcp__postgres__*, Bash]
```

Una herramienta de un servidor MCP lleva el nombre del servidor: `mcp__crm__*` nombra todas las
herramientas del servidor `crm`.

Un comando que ejecutas con `!` en Claude Code cuenta como salida de la herramienta `Bash`.

Codex lee los archivos con comandos de shell, así que en Codex el modo de datos estricto cubre
también los archivos que lee. Indica su herramienta de base de datos en `data_tools` para cubrir
solo esa.

### Archivos que cuentan como datos

```yaml
data: strict
data_files: ["exports/**", "data/*.csv", ".env*"]
```

Un archivo que un patrón nombra cuenta como datos tanto si el modo de datos estricto está activado como si no: basta con nombrarlo. Un archivo que lee el agente y cuya ruta coincide con un patrón cuenta como datos: se enmascaran
todos sus valores, como en la salida de una herramienta. Un patrón sin barra nombra un archivo por su
nombre (`*.json`, `.env*`), uno con barra el final de su ruta (`exports/**`, `**/*.csv`); `*`
coincide con cualquier secuencia de caracteres, y las mayúsculas y minúsculas no importan. Los
valores se encuentran según el formato del archivo:

- **JSON** (`.json`, `.jsonl`, o un archivo de otro nombre que contiene JSON): cada cadena y cada
  número, según la ruta de su clave, como en la salida JSON de una herramienta. Las claves se
  mantienen.
- **Tablas** (`.csv`, `.tsv`): cada celda, según su columna.
- **Archivos `.env`, properties e ini:** el valor de cada línea `KEY=value`.
- **YAML:** cada valor, según su ruta (`servers[].host`); las claves y la estructura se mantienen.
- **Cualquier otro archivo:** las tablas, los pares `key=value` y los fragmentos JSON que contiene.

Cada valor se sustituye donde está: el archivo conserva sus líneas, sus números y sus claves, así que
el agente puede seguir editándolo según el texto que leyó, y una edición escrita con un valor
ficticio escribe el valor real. Solo las herramientas que devuelven el texto de un archivo lo
convierten en datos (`Read` y `NotebookRead` de Claude Code): las líneas de una búsqueda (`Grep`) y
un listado (`Glob`, `LS`) se quedan como están. Cuando `Edit`, `MultiEdit` o `Write` de Claude Code
cambian un archivo que un patrón nombra, las líneas del archivo que muestran, que el agente quizá
nunca leyó, se enmascaran como el archivo, aunque `data_tools` nombre otras herramientas.

- **Un patrón amplio cubre más que datos.** `*.json` cubre también `package.json` y `tsconfig.json`,
  cuyos valores se enmascararían entonces en toda la conversación: indica las carpetas que contienen
  datos (`exports/**`).
- **El markdown solo cuenta cuando un patrón lo nombra** (`*.md`, `docs/**/*.md`): los agentes leen
  sus instrucciones y sus documentos en markdown, y un patrón para todos los archivos (`*`) los deja
  tal cual. En un archivo markdown que un patrón nombra solo se enmascaran sus tablas, nunca su
  texto.
- **Los archivos PDF no se cubren:** Latet no lee el texto de un PDF. `images: mask` o `block` evita
  que los documentos se envíen ([imágenes](../images/)).

### Direcciones web que cuentan como datos

```yaml
data: strict
data_hosts: [api.internal.example.com, "*.salesforce.com"]
```

Una dirección que un patrón nombra cuenta como datos tanto si el modo de datos estricto está activado como si no. Una página que una herramienta web (`WebFetch`) obtiene de un host que coincide con un patrón cuenta
como datos, como la salida de una herramienta: se enmascaran todos los valores de sus tablas, de su
JSON y de sus pares `key=value`. `*` coincide con cualquier secuencia de caracteres; las mayúsculas,
las minúsculas y el puerto no importan. Un patrón para todos los subdominios nombra también el
propio dominio: `*.salesforce.com` nombra también `salesforce.com`. Un host escrito con un punto
final (`example.com.`) es el mismo host que sin él. Una búsqueda (`WebSearch`) no tiene una dirección
propia. Para cubrir las búsquedas, añade `WebSearch` a `data_tools` con las demás herramientas que
quieras cubrir, o escribe `data_tools: ["*"]` para todas menos las que leen archivos: un `data_tools`
que nombre solo `WebSearch` cubre únicamente las búsquedas, y deja fuera tus herramientas de base de
datos y de shell. Con el modo de datos estricto activado, definir `data_tools` donde estaba vacío lo
restringe, así que espera tu aprobación.

Añadir un patrón o una dirección se aplica enseguida; quitar uno cubre menos, así que reduce la
protección: si lo hace un agente o la línea de comandos, espera tu aprobación. En el panel se
configuran en **Reglas** → **Protección** → **Datos de producción**, junto a las herramientas.

### En un intercambio

El modo de datos estricto se configura en **Reglas** → **Protección** → **Datos de producción**, o
en tus ajustes para un proyecto; un intercambio no tiene un interruptor para él. Mientras está
desactivado para un intercambio (en el proyecto del intercambio, o en tus reglas globales para un
intercambio sin proyecto), el panel pregunta a Latet si una de las diez salidas de herramienta más
recientes de la petición contiene una tabla, JSON o pares `key=value`, leídos como para
[elegir las columnas que dejar en claro](#elegir-las-columnas-que-dejar-en-claro), y viene de una
herramienta que cubre el modo de datos estricto ([qué herramientas](#qué-herramientas): no una que
lee archivos, ni una herramienta web, salvo que una regla nombre el archivo, la dirección o la herramienta). Si alguna los contiene, una línea
encima de los bloques de la petición lo indica, con un enlace a donde se configura el modo de datos
estricto para ese intercambio: tus ajustes para su proyecto cuando configuran `data`, si no
**Reglas** → **Protección** → **Datos de producción**. Latet lee las salidas de los textos que
guarda el historial: con `history: metadata` u `off` no hay línea.

## Reglas de campo

Una regla de campo indica una columna, una clave JSON o una ruta JSON (`items[].sku`); `*` equivale
a cualquier secuencia de caracteres, y no se distingue entre mayúsculas y minúsculas. Cualquier otro
carácter se toma tal como está escrito, incluidos `?` y los corchetes, así que
`orders[].items[].sku` indica esa ruta. Sus valores se enmascaran como el tipo que indiques, con el
modo de datos estricto o sin él:

```yaml
fields:
  - {name: note_*, type: NOTE}
  - {name: ral, type: SALARY}
  - {name: iban_fornitore, type: IBAN, secret: true}
```

- `secret: true` convierte sus valores en secretos: uno que llega en claro a una petición la
  bloquea.
- `mask: false` deja un campo en claro en el modo de datos estricto. Va en tus propios ajustes, no
  en el `latet.yaml` de un proyecto, que solo puede añadir protección.

Para un campo se comparan tu primera regla que coincide con él (primero tus ajustes para un
proyecto, luego tus reglas globales) y la primera del `latet.yaml` del proyecto que coincide con él:
se aplica la más estricta (ocultado, luego enmascarado, luego en claro), la tuya a igualdad.

## Elegir las columnas que dejar en claro

El modo de datos estricto enmascara todos los valores, así que el modelo tampoco puede leer un
estado ni una fecha: `SHIPPED` pasa a ser `QXWRLTE`, y un agente al que se le pregunta qué pedidos se
enviaron tarde no tiene nada en lo que basarse. La mayoría de las tablas tienen columnas que no
dicen nada de una persona, y el panel las propone.

En un intercambio, debajo de la salida de una herramienta que el modo de datos estricto ha
enmascarado, abre *Elegir las columnas que dejar en claro*. Cada columna de la salida viene con una
propuesta y su motivo:

| Propuesta | Para |
| --- | --- |
| **Dejar en claro** | un nombre que indica identificadores o códigos, fechas de eventos (`created_at`, `order_date`), estados (`status`, `state`), sí o no (`active`), recuentos (`qty`) |
| **Mantener enmascarada** | datos personales o un secreto encontrados en sus valores; un nombre que habla de personas (`customer`, `created_by`, `doctor`), de secretos (`api_key`), de salud, creencias o vida privada (`hiv_status`, `marital_status`); texto libre; fechas de nacimiento o de defunción |
| **Tú decides** | importes; fechas, sí o no, o unos pocos valores que se repiten, cuando el nombre no dice qué son; las columnas sobre las que nada da indicaciones |

Una columna solo se propone en claro cuando su nombre dice lo que contiene y nada en sus valores es
un dato personal o un secreto: los comprueban los mismos detectores que todo lo demás. El formato de
los valores por sí solo nunca deja una columna en claro, porque los nombres de usuario se repiten
igual que los estados. Las propuestas son un punto de partida: mira los valores que se muestran
junto a cada columna antes de aplicar.

Las columnas propuestas en claro vienen marcadas. *Dejar 2 columnas en claro* añade una regla para
cada una, como `{name: status, mask: false}`, al principio de tus ajustes para el proyecto
(*Este proyecto*) o de tus reglas globales (*En todas partes*): de tus reglas gana la primera que
coincide, así que una regla tuya más amplia como `note_*` no la vuelve a enmascarar; una regla del
`latet.yaml` del proyecto que enmascara la columna sigue valiendo ahí, elijas lo que elijas. Las
salidas siguientes muestran esas columnas tal cual. No se aplica nada hasta que haces clic.

- **Se calcula cuando abres la lista.** Las columnas se leen de la salida que conserva el
  historial, en tu equipo, y no se guarda nada nuevo. Con `history: metadata` u `off` no hay lista.
- **La opinión del revisor local, si la pides.** Con el [revisor local](../llm-review/) activado,
  *Preguntar al revisor local* le envía los nombres de las columnas y algunos valores de cada una, y
  muestra su opinión junto a las propuestas. Nunca se aplica por sí sola.
- **Las reglas propias de un proyecto se mantienen.** Una columna que tus ajustes para un proyecto
  enmascaran sigue enmascarada ahí, añada lo que añada *En todas partes*: la lista lo indica, y
  *Este proyecto* la cambia. Una columna que el `latet.yaml` del proyecto enmascara sigue
  enmascarada ahí elijas lo que elijas, y la lista dice cuál.
- **Después de un reinicio,** los ajustes de un proyecto se cargan en su siguiente sesión: hasta
  entonces, la elección solo puede ir en tus reglas globales.
- **Codex y OpenCode** envían toda la conversación con cada petición, y Latet la vuelve a enmascarar
  cada vez: a partir de la petición siguiente, las salidas que ya contiene también se envían con
  esas columnas en claro. En Claude Code, una salida se queda como la enmascaró el plugin cuando
  llegó.

Para una sola columna, haz clic en uno de sus valores: el inspector ofrece
*No enmascarar `status` en este proyecto*, y dice cuándo el nombre de una columna parece un
identificador, una fecha, un estado o un recuento.

## La protección contra volcados

```yaml
dump_guard: 50
```

La salida de una herramienta que contenga más valores personales que este número (nombres,
direcciones de correo electrónico, números de teléfono, números de identificación...) se retiene: en
su lugar, el modelo recibe una nota breve que le dice que pida menos filas o recuentos. El panel
marca el intercambio como *Retenido*. Sin definir, o 0: sin límite.

En el perfil `max`, con el [modelo de nombres local](../../getting-started/installation/) instalado,
la salida de una herramienta también se lee en busca de nombres y direcciones: los textos y las
claves de una salida JSON se leen de una vez, y una que el modelo no puede leer en cinco segundos se
retiene del mismo modo, con una nota que le dice al modelo que pida menos filas o campos. Nada de lo
que habría encontrado en ella sale en claro.

## El navegador

Un agente que maneja un navegador lee las páginas como salidas de herramientas: instantáneas de
accesibilidad, HTML, los resultados de scripts, las respuestas de red. El modo de datos estricto se
les aplica como al resto, tablas incluidas.

Sus capturas de pantalla son imágenes: con el [reconocimiento de texto](../images/) instalado, su
texto se lee y se enmascara, y cada valor se cubre con su valor ficticio. Para enviar en un proyecto
solo las imágenes que Latet ha leído y enmascarado, o ninguna:

```yaml
images: mask    # or block: no image or PDF is sent
```

## En un proyecto

Estas claves funcionan en [config.yaml](../../reference/configuration/), en el
[`latet.yaml`](../projects/) de un proyecto y en tus propios ajustes para un proyecto. El archivo
del proyecto solo puede añadir protección:

| Clave | En `latet.yaml` | En tus ajustes para el proyecto |
| --- | --- | --- |
| `data` | solo `strict` | `strict` o `normal` |
| `data_tools` | se ignora | se suma al tuyo |
| `data_files` | se suma | se suma al tuyo |
| `data_hosts` | se suma | se suma al tuyo |
| `fields` | solo reglas que enmascaran, de un tipo enmascarado allí | cualquier regla, incluida `mask: false` |
| `dump_guard` | solo más bajo | cualquier valor; 0 la desactiva |
| `images` | solo menos imágenes: `mask` y después `block` | `allow`, `mask` o `block` |
| `ocr` | solo `auto` | `auto` u `off` |

Un proyecto cuyo perfil es `max`, en su archivo o en tus ajustes para él, tiene el modo de datos
estricto activado salvo que `data` diga otra cosa ahí o en tus reglas globales.
