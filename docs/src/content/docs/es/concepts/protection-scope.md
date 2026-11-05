---
title: Qué está protegido y qué no
description: Los límites de Latet. Lee esto antes de confiar en él.
---

Latet reduce lo que tu agente revela al proveedor del modelo. No es una garantía de que nada
sensible salga nunca de tu equipo. Esta página enumera lo que está cubierto y lo que no.

## Protegido

Cuando un [detector](../detection/) reconoce un valor, Latet lo sustituye por un valor ficticio
antes de que la petición protegida llegue al proveedor del modelo. Con un servidor, los
datos originales pueden llegar primero a ese servidor. Eso cubre:

- **En Claude Code (modo cliente):** los prompts que escribes, el prompt de sistema, los archivos
  `CLAUDE.md` y el resto del contexto que añade Claude Code, los adjuntos, los prompts de las
  skills, las descripciones de las herramientas y cada resultado de una herramienta: el contenido
  de los archivos, la salida de los comandos, la salida de los hooks. Después, una comprobación
  final lee los textos de cada petición antes de que se envíe. Lo que escribió el modelo no detiene
  nada cuando vuelve exactamente como el modelo lo escribió, lo que Latet sabe por las respuestas que
  dejó pasar y por las llamadas a herramientas que el plugin hizo restaurar; cualquier otro texto en
  el lugar del modelo se lee como tuyo. En un equipo, el daemon guarda todo esto en memoria: después
  de un reinicio, un texto que el modelo escribió antes en la conversación se lee como tuyo, y uno que
  contiene un valor que Latet enmascara detiene la petición; en ese caso, empieza una conversación
  nueva. Un servidor de Latet lo guarda para cada instancia, durante una hora desde su último uso.
- **En modo puerta de enlace:** el prompt de sistema o las instrucciones, y cada mensaje de la
  conversación: tu texto, las respuestas anteriores del modelo, las llamadas a herramientas y sus
  resultados. También la entrada de los embeddings. Después, una comprobación final lee esos textos
  antes de que se envíe la petición, como en el modo cliente.

Las respuestas puerta de enlace, flujos y argumentos de herramientas se restauran donde
se ejecuta Latet: en el equipo para uso local, en el servidor para una puerta de enlace
conectada. El plugin de Claude Code restaura argumentos de herramientas y texto interactivo
en pantalla en el equipo; stdout de `claude -p` sigue conteniendo valores ficticios.
OCR/NER se ejecutan donde está el motor Latet correspondiente; un revisor configurado
puede recibir texto original.

Las claves de un documento JSON, como la salida de una herramienta, se enmascaran como sus valores:
una dirección de correo, un número de teléfono, un secreto, un nombre o uno de tus términos escrito
como clave se sustituye por el valor ficticio que tiene en todas partes, y una clave en la que el
modelo escribe un valor ficticio se restaura para el agente. Una regla de campo y el modo de datos
estricto siguen encontrando un valor por su clave tal como llegó, y una clave que solo nombra un
campo, como `id` o `email`, queda tal cual.

Con el [revisor local](../llm-review/) activado, un modelo de lenguaje en tu equipo también lee esos
textos antes de que salgan, y enmascara los nombres, los datos de salud y los demás valores que
ningún detector encuentra, también en las claves de un documento JSON. Una clave escrita como el
nombre de un campo, en letras minúsculas, cifras, `_` y `-` (`exit_code`, pero también
`mario_rossi`), no se le muestra: los detectores la leen como leen cada clave.

## Cada parte de una petición

Latet lee cada parte de una petición antes de enviarla, en un equipo igual que en un servidor, y no
envía nada de lo que no haya dado cuenta:

- **La ruta** debe coincidir con una ruta que conozca uno de los perfiles de petición de Latet, y cada
  una de sus partes debe tener su forma: el id de un modelo, una región, un recurso que hayas
  enumerado. Cualquier otra llamada se rechaza, también una sin cuerpo.
- **Los parámetros de consulta** los nombra el perfil, y cada valor debe tener su forma. Uno que
  ningún perfil nombra se rechaza.
- **Las cabeceras.** Una credencial se envía tal como llegó, nunca se registra ni se guarda. Cualquier
  otra cabecera debe tener la forma que le da su agente, o la lee la comprobación final cuando puede
  contener texto libre (`user-agent`). Las cabeceras que añaden los SDK y el trazado
  (`x-stainless-*`, `traceparent`...), y las propias de los agentes que pueden contener texto libre
  (los id de un subagente de Claude Code, los metadatos del turno de Codex...), se quitan y se cuentan
  en el panel: [Las cabeceras que quita Latet](../../getting-started/upgrading-to-0-11/#las-cabeceras-que-quita-latet) las enumera por agente. Cualquier otra cabecera
  se rechaza, y su nombre solo se le dice al agente.
- **El cuerpo.** Sus textos se enmascaran, o se comprueban donde Latet solo comprueba la petición.
  Cualquier otro miembro debe tener su forma (el id de un modelo, el nombre de una herramienta, un
  ajuste) y lo lee la comprobación final donde contiene texto. Un miembro que ningún perfil nombra se
  rechaza, y al agente se le dice dónde estaba, nunca su nombre.

En el panel, cada intercambio dice si se leyó cada parte de su petición, o qué dejó pasar una parte:
*solo comprobada* (un valor que la comprobación final leyó sin tener que enmascararlo), *permitida
por una regla* (una de las reglas integradas de Latet, como la de los campos cifrados que devuelve el
propio proveedor), o una
[excepción](../../getting-started/upgrading-to-0-11/#aprobar-una-excepción-exacta) que aprobaste. Un rechazo le dice al agente que no se envió nada. Cuando se rechaza un segundo
intento después de que un primero llegara al proveedor (una petición a Bedrock firmada de nuevo
después de caducar su sesión de AWS), al agente se le dice que no se envió ningún intento más y que el
primero pudo llegar, nunca que no se envió nada.

Dos cosas quedan fuera: lo que un agente envía sin la dirección de Latet, que Latet nunca ve, y el
[revisor local](../llm-review/), que recibe los textos originales porque lo configuraste así.

## No protegido

**Valores que los detectores no reconocen.** Latet solo enmascara lo que detecta. Un secreto en un
formato poco habitual, dividido en varias líneas o codificado (por ejemplo, en base64 bajo un nombre
corriente) puede pasar. Los nombres y las direcciones postales en texto libre solo se encuentran con
el [modelo de nombres local](../../getting-started/installation/) opcional o con el
[revisor local](../llm-review/), ambos sin garantías; sin ellos, solo se enmascaran tu propio nombre
de autor de git, tus términos y los valores que ya están en la caja fuerte. Las fechas de nacimiento
no se detectan. La detección siempre tiene falsos negativos.

**Imágenes.** Con el [reconocimiento de texto](../images/) opcional, el texto de una captura de
pantalla se lee en tu equipo y cada valor encontrado se cubre con su valor ficticio. No ofrece
garantías, igual que con los nombres: el texto que no lee sale tal cual. Sin él, una petición que
lleva una imagen no se envía. En una petición que Latet solo comprueba, como las de Claude Code,
también se rechazan la ubicación de una foto, las notas del fabricante de una cámara y una imagen
dentro de la imagen ([Las fotos y sus metadatos](../images/#las-fotos-y-sus-metadatos)). Los códigos QR
y los códigos de barras no se descodifican: los datos que contienen salen con la imagen.

**PDF y otros documentos, imágenes indicadas por su dirección, clips de audio.** Su contenido no se
lee, así que una petición que lleve uno no se envía, en un equipo igual que en un servidor, diga lo
que diga `images`. Dale al agente el texto en su lugar. Las páginas de un navegador, leídas como texto, se enmascaran como cualquier salida de una herramienta
([datos de producción](../production-data/)); sus capturas de pantalla son imágenes.

**Las herramientas web, los subagentes y los conectores de claude.ai se quedan con los valores
ficticios (Claude Code).** WebFetch, WebSearch, los subagentes y los conectores de claude.ai (Gmail,
Calendar...) reciben los valores ficticios que escribió el modelo, no los originales, porque sus
argumentos salen de tu equipo o inician otra conversación con un modelo. Una búsqueda web de un
nombre enmascarado buscaría a otra persona, y un conector al que se le pide enviar un correo a
`emily.carter@northwind-legal.com` recibiría la dirección ficticia, así que una llamada a una
herramienta web o a un conector que contiene un valor ficticio se detiene antes de ejecutarse: el
modelo te pregunta, y *No sensible* en el panel deja salir el valor en claro. Los resultados de los
conectores de claude.ai vuelven a través de los servidores de Anthropic, donde se ejecuta el
conector: lo que contengan ya está allí antes de que Latet pueda enmascararlo.

**Herramientas que ejecuta el proveedor (agentes en modo puerta de enlace).** Una búsqueda web o un
servidor MCP remoto que el propio proveedor ejecuta, como puede hacer Codex, trabaja con lo que
escribió el modelo: el valor ficticio. Latet solo ve una llamada así cuando ya se ha ejecutado, en
el historial de la petición siguiente: el panel la marca (*Valor ficticio a herramienta externa*),
con *No sensible* para cada valor, para que la próxima vez el modelo use el real.

**Entornos que Latet no puede ver:**

- Chat y Cowork de Claude Desktop, con el inicio de sesión de Claude;
- las sesiones en la nube, que se ejecutan en la infraestructura del proveedor en lugar de en tu
  equipo;
- Cursor, Windsurf y las demás herramientas cuyas peticiones al modelo se envían desde los
  servidores del fabricante ([la lista](../../agents/other-tools/#lo-que-latet-no-puede-proteger));
- las sugerencias en línea de GitHub Copilot y las predicciones de edición de Zed, que vienen de los
  propios servicios de sus fabricantes;
- OpenCode con el inicio de sesión de ChatGPT (en la [hoja de ruta](../../roadmap/));
- lo que envían las herramientas propias de un agente cuando se ejecutan, como una API de búsqueda
  web, una base de datos o un almacén de vectores en la nube: reciben los valores reales (consulta
  [Frameworks de agentes](../../agents/openai-compatible/#frameworks-de-agentes));
- cualquier agente o entorno que no hayas configurado para usar Latet.

Con el inicio de sesión de Claude, la pestaña Code de Claude Desktop enmascara el contenido con el
plugin, pero la aplicación ignora `ANTHROPIC_BASE_URL`: no hay comprobación del daemon detrás del
plugin ni registro en el panel. En una puerta de enlace de terceros, sin el inicio de sesión de
Claude, Chat, Cowork y Code envían cada petición a través de Latet: Latet comprueba cada una y
detiene la que llevaría un valor sensible en claro, pero enmascara solo en la pestaña Code, con el
plugin. Consulta [Claude Code](../../agents/claude-code/#cobertura) y
[Claude Desktop en una puerta de enlace](../../agents/other-tools/#claude-desktop-en-una-puerta-de-enlace).

**Contexto y metadatos.** Los nombres de archivo, las rutas, la estructura del código, los
comentarios y la lógica de negocio se envían tal cual, salvo los valores que Latet detecta en ellos,
como tu nombre de usuario dentro de una ruta del directorio personal. Los valores ficticios
mantienen la forma del original (longitud, formato, prefijo; un secreto de menos de doce caracteres
recibe un valor ficticio más largo), y el texto que los rodea puede revelar qué es un valor ("la
contraseña de la base de datos de producción").

**Lo que un documento JSON contiene donde el enmascaramiento no llega.** Un número de tarjeta escrito
como número JSON, que el perfil `balanced` no enmascara, lo encuentra la comprobación final, que
entonces detiene la petición: el agente recibe el motivo, y el panel muestra dónde está el valor.
Mientras una salida así siga en la conversación, cada una de sus peticiones se detiene: empieza una
conversación nueva y pídele al agente los datos de otra forma. El valor ficticio elegido para una
clave nunca es otra clave de su objeto; una llamada a una herramienta cuya entrada contiene un valor
y su valor ficticio como dos claves, que serían una sola una vez enmascaradas, detiene la petición,
porque un objeto contiene cada clave una sola vez, y dos claves así quedan tal como las escribió el
modelo cuando Latet restaura su respuesta para el agente. Un objeto JSON que repite una clave se lee
con su última copia y sale reescrito solo con esa copia, para que la copia que Latet no leyó no salga
nunca; una petición que Latet solo comprueba, como las de Claude Code, se detiene cuando es su propio
cuerpo el que repite una clave.

**Llamadas con claves en conflicto en el historial.** Conservar ambas claves en una llamada
restaurada preserva sus datos, pero volver a enmascararla puede hacer que las claves sean idénticas.
Es una limitación documentada: se rechazan la petición siguiente y todas las que conserven esa llamada.
Empieza una conversación nueva, o elimina o reformula la llamada con los controles de historial
compatibles con tu cliente. Reintentar la petición sin cambios no recupera la conversación.

**Argumentos de herramientas en flujo.** Latet retiene los argumentos JSON hasta que termina la
llamada y después restaura el documento completo sin unir claves distintas. El texto normal sigue
llegando en flujo. Los canales JSON retenidos comparten un único presupuesto limitado de la respuesta,
incluidos los metadatos y el crecimiento al reconstruir y serializar. Superarlo, o recibir argumentos
incompletos o no válidos, termina el flujo con un error. Una llamada Anthropic con entrada inicial no
vacía no puede aportar también una segunda fuente JSON no vacía de argumentos. Una llamada interrumpida
antes de terminar no se registra como salida completa del modelo.

**Registros aceptados de la salida del modelo.** Tras validar un lote de respuestas, Latet acepta
los registros de la salida y pone su almacenamiento en cola antes de devolver la salida de ese
lote. El almacenamiento se realiza en segundo plano: la aceptación no demuestra ni la entrega al
cliente ni que los datos ya estén guardados. Una cancelación anterior a la aceptación no publica
registros; después de la aceptación, su almacenamiento puede terminar aunque se cancele la entrega.

En una misma instancia, la puerta de enlace de cada identidad admite como máximo 32 MiB de estos
registros y 256 lotes entre sus sesiones, contando tanto el trabajo en cola como el que está en ejecución.
Se cuentan el texto enmascarado y
restaurado, los identificadores de las respuestas y el coste de las estructuras de registro. Esto
limita el nuevo trabajo retenido, no la memoria total del proceso (RSS): la retención más prolongada
de datos en claro y el intérprete siguen consumiendo memoria. Si la admisión está llena o cerrada,
o falla la puesta en cola, se rechaza la salida de ese lote. El texto ya transmitido en flujo no
se puede retirar.

Los registros se hacen visibles más tarde. Las peticiones encoladas después en el mismo proceso de
trabajo de la puerta de enlace siguen su intento de registro. En un servidor Latet, esto no garantiza
ni que Valkey los haya escrito, ni que otra instancia los vea, ni su orden respecto al trabajo del
plugin de Claude Code. El apagado puede descartar registros en cola. Un fallo al guardar después de la entrega genera un
diagnóstico con frecuencia limitada, que solo contiene una categoría fija y el tipo de excepción;
no puede deshacer la entrega ni revertir atómicamente una transacción. Si faltan registros, se
mantienen las reglas habituales de búsqueda en la sesión, enmascaramiento y rechazo.

**Campos de la petición fuera de la conversación.** En modo puerta de enlace, las descripciones de las
herramientas, los esquemas de sus parámetros y los ejemplos de su entrada se enmascaran como la
conversación, y también el esquema de una salida estructurada, una salida prevista y lo que una
petición dice de para quién es (`user`, `safety_identifier`, `metadata`). Los nombres de las
herramientas, lo que permite al proveedor ejecutar una herramienta propia (la dirección y las
cabeceras de un servidor MCP remoto, la ubicación de una búsqueda web) y los demás ajustes de la
petición deben tener cada uno la forma que les da su perfil, y los lee la comprobación final donde
contienen texto; uno que ningún perfil nombra detiene la petición
([Cada parte de una petición](#cada-parte-de-una-petición)). En Claude Code, el plugin enmascara las
descripciones de las herramientas, y la comprobación final lee `metadata`, el esquema de una salida
estructurada y los esquemas de los parámetros de las herramientas (ver más abajo). Un cliente distinto de Claude Code en una vía que Latet solo comprueba (el
acceso de una suscripción, Bedrock, Vertex) no tiene plugin: la comprobación final lee también las
descripciones, los parámetros y los ejemplos de sus herramientas, y detiene una petición que dejaría
salir en ellos un valor en claro.

**Las herramientas de Claude Code.** El plugin enmascara las descripciones de las herramientas,
también las de `tool_addition`. Latet no toma los marcadores de la petición como prueba de que el
plugin se ha ejecutado, en un equipo igual que en un servidor: su comprobación final lee la descripción de
cada herramienta, el esquema de sus parámetros y los ejemplos de su entrada, como para un cliente sin
plugin, y detiene una petición que dejaría salir en ellos un valor en claro. Por eso, una herramienta
MCP cuyo esquema nombra la dirección de un cliente detiene las peticiones de Claude Code hasta que
el valor se quita del esquema o se permite en tus ajustes. Una definición de
herramienta en una forma que Latet no lee se rechaza, como para cualquier cliente. Siguen
aplicándose los controles de metadatos, salidas estructuradas y esquemas de reserva descritos aquí.

En Anthropic, la lectura de salidas estructuradas incluye también las descripciones, claves y valores
de los esquemas de cada `fallbacks[].output_config.format`. La reescritura enmascara los esquemas
sensibles; en peticiones que solo se comprueban, se rechazan y los bytes seguros quedan sin cambios.
Siguen siendo compatibles los modelos alternativos que solo indican el modelo. Un servidor con la
protección activa rechaza contenedores de modelos alternativos y esquemas de salida mal formados.

**Lo que Latet no lee.** Latet rechaza lo que no lee, en un equipo igual que en un servidor, con un
error que dice por qué. Rechaza una llamada que ningún perfil nombra: files o batches, por ejemplo, o
un método que la ruta no acepta. Rechaza contenido en una forma que Latet no lee: un prompt de completions dado como
tokens; una variable de un prompt, una definición de herramienta o el formato de una respuesta de un
tipo que no conoce; una parte de contenido, un bloque de un mensaje o un elemento de la entrada de un
tipo que no conoce, tanto en las chat completions como en los mensajes de Anthropic y en la
Responses API, o uno que no es un objeto; un campo que Latet lee dado en otra forma, como la entrada
de una llamada a una herramienta que no es un objeto o un prompt de sistema que no es ni texto ni
bloques; y, en la Responses API, el audio de una conversación en directo (`audio`, `output_audio`) y
lo que se oyó en un clip de audio. Rechaza un frame de WebSocket que no es un turno
(`response.create`; `response.cancel` pasa) y, en una petición que solo comprueba (Claude Code,
Bedrock, Vertex), una imagen cuyo texto no pudo leer, diga lo que diga `images`. Lo que el proveedor
produjo y se le devuelve tal como lo produjo pasa cuando Latet lo vio llegar del proveedor en esa
conversación: el razonamiento del modelo y sus firmas, una compactación, las llamadas de las
herramientas propias del proveedor y sus resultados. Donde Latet no lo vio (el daemon de un equipo se
reinició, por ejemplo), una regla integrada deja pasar los campos cifrados del propio proveedor, solo
en los turnos del modelo y solo hacia la dirección del proveedor, nunca a través de una pasarela de
empresa; el panel los muestra como *permitida por una regla*. También pasa lo que no contiene nada,
como una referencia a un elemento anterior, y una petición sin cuerpo en una ruta que nombra un
perfil, por ejemplo la lista de modelos.

Latet también rechaza mensajes chat con `name`, bloques Anthropic `search_result` y bloques de
documento con `title` o `context`, incluso dentro de resultados de herramientas, en un equipo igual que
en un servidor. Estas formas opcionales no son compatibles ni con valores vacíos o null. Un documento
sin título ni contexto se rechaza como cualquier documento.

**La revisión del modo automático de Claude Code.** El revisor del modo automático recibe tu nombre
de usuario del sistema operativo y las rutas de tu directorio personal y de tu directorio de
trabajo, además de cualquier nombre de host o autor de git que haya en tu CLAUDE.md o en la acción,
y ningún plugin puede enmascararlos. De forma predeterminada, el daemon detiene esas revisiones, así
que esos datos se quedan en local y el modo automático no puede aprobar acciones por sí solo.
`auto_mode_review` deja pasar cada dato al revisor, y solo a él, o le da al revisor un nombre de
usuario ficticio; consulta [Claude Code](../../agents/claude-code/).

**Tu propio equipo.** Latet no es un entorno aislado: no protege contra el malware, contra otros
usuarios de tu cuenta ni contra un agente comprometido. Las herramientas que el agente ejecuta en tu
equipo, incluidos los servidores MCP locales, reciben los valores reales, para que los archivos y
los comandos funcionen: un comando que envía datos por la red envía el valor real. Ejecuta el agente
en un entorno aislado cuando lo necesite.

**Reglas añadidas a mitad de una conversación (Claude Code).** Imagina que añades `Project Falcon`
como término personalizado a mitad de una conversación. El contenido nuevo se enmascara a partir de
entonces, pero los turnos anteriores, que ya llegaron a Anthropic con el nombre en claro, se vuelven
a enviar con cada petición. Latet los marca en el panel en lugar de bloquearlos: ya se enviaron, y
bloquearlos detendría la conversación. Empieza una conversación nueva para dejarlos atrás.

**Datos enviados antes.** Todo lo que se envió antes de instalar Latet o, antes de la 0.11, desde un
proyecto en el que estaba desactivado, ya está en manos del proveedor.

Los motivos de estos límites están en [Seguridad y modelo de amenazas](../../security/).
