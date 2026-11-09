---
title: Registro de cambios
description: Los cambios importantes de Latet, los más recientes primero.
---

Latet sigue el [versionado semántico](https://semver.org). Los cambios que todavía no están en una
versión aparecen en **Sin publicar**; cada versión enumera sus cambios, los más recientes primero.

Esta página recoge los cambios sin publicar y las versiones desde la 0.10.2. Las versiones
anteriores, de la 0.10.0 hacia atrás, están en el
[registro de cambios en inglés](../../changelog/#0100-2026-10-27).

## Sin publicar

Lo que viene después está en la [hoja de ruta](../roadmap/): un rechazo claro del modo automático,
un instalador firmado para Windows, tus propios modelos de nombres, más agentes, y la documentación en
más idiomas.

## 0.12.1 (2026-11-09)

- **Un destino alcanzado en sus otras direcciones.** En un servidor, un destino propio de una
  persona cuyo nombre tiene varias direcciones se alcanza en la siguiente, en el orden que da el
  sistema, cuando la conexión a la primera se rechaza o no se logra a tiempo, antes de que salga
  nada de la petición. Un sistema puede dar primero la dirección IPv6 de un nombre (Windows lo hace
  con `localhost`), y un proveedor que responde solo en su dirección IPv4 se alcanza entonces allí;
  una conexión todavía abierta a una de ellas se usa primero, así que, mientras esa conexión siga
  abierta, una dirección que rechaza cuesta su tiempo una sola vez. Solo se prueban las direcciones
  comprobadas en esa misma resolución, ocho como máximo, en el tiempo que tiene una conexión; una
  petición que alcanzó una nunca se envía a otra, y un nombre con cualquier dirección dentro de la
  red del servidor se sigue rechazando.
- **Una restauración rechazada mientras cambian los ajustes ya no se registra.** Una restauración
  por la API de aplicaciones se rechazaba (`503`, *send it again*) cuando los ajustes de la clave
  empezaban a aplicarse mientras se preparaba su respuesta. No devolvía nada, pero aun así dejaba
  una entrada `apps.unmasked` en el registro de administración, y su nuevo intento una segunda. Una
  restauración rechazada al llegar, porque los ajustes ya se estaban aplicando, no dejaba ninguna.
  Ahora la entrada solo se escribe para una restauración que recibe respuesta, así que cada entrada
  es una restauración que devolvió sus textos. Un análisis, un enmascaramiento o una restauración
  rechazados mientras se preparaba su respuesta tampoco se cuentan ya en `latet_apps_texts_total` y
  `latet_apps_values_total`.

## 0.12.0 (2026-11-09)

- **Una petición cuyos ajustes cambian antes de salir.** Una petición cuyos ajustes cambian entre su
  decisión y su envío se rechaza como una cuyos ajustes cambiaron mientras se preparaba: a su agente
  se le dice que la envíe de nuevo y el historial registra un rechazo, donde antes recibía un `500`
  *Latet could not send this request as it was decided* y se registraba como un intercambio. Una
  petición preparada y enviada mientras un guardado todavía se está aplicando ya no sale con los
  ajustes aplicados a medias: se prepara de nuevo cuando están todos en su sitio, o se rechaza del
  mismo modo. Un guardado que falla a medio camino vuelve a poner los ajustes de antes; si ni siquiera
  esos pueden volver, cada petición se rechaza del mismo modo hasta que un guardado posterior salga
  bien o Latet vuelva a arrancar.
- **Cabeceras nuevas de Claude Code.** En una petición que Latet solo comprueba (Claude Code,
  Bedrock, Vertex), una cabecera `anthropic-*` que ningún perfil nombra todavía, o un valor de
  `anthropic-beta` de forma nueva, pasa una vez leída por la comprobación final, mostrada como *solo
  comprobada*, como ya ocurre con un miembro nuevo del cuerpo que contiene texto; una cabecera
  `x-claude-code-*` nueva se quita y se cuenta. Claude Code sigue funcionando con una versión que
  añade una antes de que Latet la nombre. Un secreto o un valor tuyo en una de estas cabeceras se
  sigue rechazando.
- **Actualizar un servidor desde la 0.10.** Calcular las huellas de una caja fuerte guardada por la
  0.10 ya no ocupa una de las dos construcciones de índice de una instancia: los índices de las
  demás identidades se construyen mientras tanto, y solo a las identidades cuyas huellas aún no
  están todas calculadas se les responde con la búsqueda en PostgreSQL, como dicen los pasos de la
  actualización.
- **Python 3.14.** Latet funciona también con Python 3.14, con el que se prueba igual que con 3.12 y
  3.13. Una instalación nueva en un ordenador sin Python recibe 3.14; `latet upgrade` pasa un Latet
  ya instalado a 3.14 solo donde ya hay un 3.14.

## 0.11.1 (2026-11-06)

- **Las cabeceras de indicación de Claude Code.** Configurado para enviar sus cabeceras de indicación a
  las pasarelas (`CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`), Claude Code ya no se rechaza tras una
  compactación o una llamada a una herramienta: `x-claude-code-compaction` y
  `x-claude-code-context-compacted` pasan, limitadas a sus tres valores, y
  `x-claude-code-prev-tool-durations`, que nombra las herramientas ejecutadas, se quita y se cuenta,
  como los id de sus subagentes.
- **Una petición enviada mientras cambian los ajustes.** Una petición preparada mientras sus ajustes
  se aplicaban de nuevo (un guardado, o en un servidor un cambio compartido) se prepara de nuevo con
  los nuevos, como Latet ya hacía cuando solo cambiaban las reglas, en lugar de rechazarse con
  *could not account for every part of this request*. Una petición cuya ruta cambió mientras tanto,
  o cuyos ajustes cambiaron después de prepararla, sigue sin enviarse, y a su agente se le dice que
  los ajustes cambiaron y que la envíe de nuevo; el historial, el registro y el panel dicen lo
  mismo, con un motivo propio.
- **Una respuesta transmitida entera se registra entera.** Una respuesta transmitida que llegó entera a
  su cliente se registra entera aunque el cliente cierre la conexión justo después, como ya ocurría
  con un turno de WebSocket; antes, el historial la mostraba como un error, sin su texto.
- **Un WebSocket que el proveedor rechaza queda en el historial.** Un WebSocket cuyo handshake el
  proveedor rechaza (`401`, `403`) o redirige, o cuyo proveedor no se alcanza, se registra en el
  historial como la misma respuesta por HTTP; antes no dejaba registro.
- **El índice de excepciones de peticiones del servidor.** Un espacio construido en varios workers o
  instancias a la vez ya no reescribe cada vez el índice de excepciones de peticiones de la identidad:
  se escribe solo cuando cambió, uno a la vez, así que el registro ya no avisa de que no se escribió,
  y `latet server status` nunca lista una excepción revocada mientras tanto.
- **Una ruta que desaparece entre dos intentos de AWS.** Cuando una ruta de Bedrock se quita entre los
  dos intentos de AWS de una petición (al primero se respondió con un token caducado), al agente se le
  dice que no se envió ningún otro intento y que el primero pudo llegar, en lugar de solo *no
  upstream for this path*.
- **Quién aprobó una excepción de petición.** Una excepción aprobada cuyo `issuer` (quién la aprobó)
  se cambió a mano vuelve a esperar aprobación, aunque su hora de aprobación también se haya
  adelantado; antes, esa hora adelantada la mantenía en vigor con el nuevo `issuer`.

## 0.11.0 (2026-11-05)

Latet 0.11.0 bloquea las solicitudes de forma predeterminada cuando una parte de la ruta, los
parámetros de consulta, las cabeceras o el cuerpo no está inspeccionada ni autorizada explícitamente.
Un cuerpo vacío no basta para permitir una solicitud. Las credenciales necesarias del proveedor y los
componentes firmados se conservan según las reglas definidas. Revisa las rutas y las excepciones
limitadas antes de actualizar; las llamadas antes enviadas sin inspección pueden quedar bloqueadas.
El panel identifica las partes no inspeccionadas permitidas explícitamente. Un rechazo solo le dice
al agente que no se envió nada cuando no salió nada: cuando se rechaza un segundo intento después de
que un primero llegara al proveedor, al agente se le dice que no se envió ningún intento más y que el
primero pudo llegar. [Pasar a la 0.11](../getting-started/upgrading-to-0-11/) explica qué comprobar
primero, y `latet doctor` enumera lo que se detendrá.

- **Lo mismo en un equipo y en un servidor.** Un equipo rechaza ahora lo que rechazaba un servidor de
  Latet: una petición cuyo cuerpo Latet no lee, contenido en una forma que no lee, un frame de
  WebSocket que no es un turno. Las definiciones de herramientas de Claude Code también se comprueban
  en un equipo. En un equipo, el cuerpo de una petición tiene un límite de 32 MiB.
- **Llamadas sin cuerpo.** La lista de modelos y la lectura de un modelo pasan en cada proveedor que
  Latet conoce, incluidos los perfiles de inferencia de Bedrock, y se leen sus parámetros de consulta.
  Cualquier otra llamada se rechaza, sin nombrar ninguna ruta tuya: archivos, subidas, lotes, audio,
  generación de imágenes, moderación, una respuesta guardada, las lecturas de una caché de Gemini.
- **Cabeceras y parámetros de consulta.** Cada uno se lee con una regla propia. Una cabecera que
  ningún perfil nombra se rechaza, y su nombre solo se le dice al agente. Las cabeceras de telemetría
  y de trazado de los SDK (`x-stainless-*`, `traceparent`, `amz-sdk-*` del SDK de AWS,
  `x-goog-api-client`), los id de los subagentes de Claude Code y los valores que copia de su entorno
  (`x-claude-code-agent-id`, `x-claude-remote-session-id`...), y `x-codex-turn-metadata` y
  `x-openai-subagent` de Codex se quitan y se cuentan en el panel, y un `401` o un `403` que siga lo
  dice ([la lista](../getting-started/upgrading-to-0-11/#las-cabeceras-que-quita-latet)). En una petición que Latet solo comprueba, el cuerpo sigue saliendo byte a
  byte. Un cuerpo de petición comprimido sale comprimido de nuevo a partir de lo que Latet leyó,
  nunca tal como llegó, así que un nombre de archivo en su cabecera gzip o los bytes tras sus datos
  comprimidos nunca llegan al proveedor; en una petición que Latet solo comprueba, un cuerpo
  comprimido se rechaza, con palabras que dicen que se envíe sin comprimir. Un parámetro de consulta que ningún perfil nombra se rechaza, y su nombre solo se le dice al
  agente. Una petición rechazada por una cabecera, un parámetro de consulta o su ruta queda en el
  historial con esa categoría, sin cabeceras; un WebSocket rechazado así se cierra con el código 1008
  y un motivo que dice lo mismo. Las cabeceras que
  defines para un agente (`ANTHROPIC_CUSTOM_HEADERS`, `http_headers` de Codex,
  `COPILOT_PROVIDER_HEADERS`) se rechazan hasta que una excepción las nombre.
- **Cada miembro del cuerpo.** Un miembro que ningún perfil nombra se rechaza, diciendo dónde está,
  nunca su nombre; en una petición que Latet solo comprueba (Claude Code, Bedrock, Vertex), un miembro
  nuevo que contiene texto pasa una vez comprobado, mostrado como *solo comprobada*. `client_metadata`
  de Codex se quita antes de enmascarar la petición. Los id de modelo pasan en las formas que
  documentan sus proveedores; un ARN de Bedrock, un proyecto de Vertex, el nombre de modelo propio de
  una pasarela o una `api-version` se enumeran antes en `request_resources`. En OpenRouter un modelo
  pasa como `anthropic/`, `openai/` o `google/` seguido de un modelo de ese proveedor, o como
  `openrouter/auto`; el modelo de otro proveedor también se enumera antes allí.
- **Lo que devolvió el proveedor.** Las firmas del razonamiento, el razonamiento cifrado, las
  compactaciones y los resultados de búsquedas web pasan cuando Latet los vio llegar del proveedor en
  esa conversación, en cualquier instancia de un servidor; tras un reinicio del daemon de un equipo,
  una regla integrada deja pasar los campos cifrados del propio proveedor, solo en los turnos del
  modelo y solo hacia la dirección del proveedor. Una conversación a través de una pasarela de empresa
  que reenvía las firmas del razonamiento se rechaza después de un reinicio.
- **Las herramientas y el código de Google en Gemini.** El código que Google ejecuta para el modelo
  y lo que ese código imprimió, y las entradas y salidas de las herramientas integradas de Google
  (Google Search, contexto de URL, Maps, File Search), ahora se enmascaran, como la llamada a una
  función y su resultado; la 0.10 los dejaba como se habían escrito. El razonamiento del modelo
  sigue quedando como el modelo lo escribió.
- **Imágenes y documentos.** Una petición con una imagen solo se envía cuando Latet ha leído su texto:
  sin reconocimiento de texto, o con `ocr: off`, se rechaza con palabras que nombran
  `latet ocr install`. `images: allow` ya no envía una imagen que Latet no haya leído, y los
  documentos, el audio y las imágenes indicadas por su dirección se rechazan. En una petición que
  Latet solo comprueba, cada byte de una imagen tiene que tener explicación: la ubicación de una foto,
  las notas del fabricante de una cámara, una miniatura, una segunda imagen y el vídeo de una foto en
  movimiento se rechazan, con el modo de quitarlos, y una imagen WebP original se rechaza; los códigos
  QR no se descodifican. Las peticiones que Latet reescribe envían las imágenes reescritas, sin sus
  metadatos.
  En un servidor cuyo reconocimiento de texto no está disponible (no pudo cargarse),
  `latet_ocr_unread_total{reason="unavailable"}` cuenta una vez cada petición o turno de WebSocket
  rechazado, lleve las imágenes que lleve.
- **Un proyecto en el que Latet está desactivado no envía nada.** Sus peticiones se rechazan, por HTTP
  y por un WebSocket, con palabras que dicen cómo activar Latet ahí o llegar directamente al
  proveedor. `latet project off` y el interruptor del panel lo dicen antes.
  `latet serve --passthrough` ya no existe: la opción oculta termina con un mensaje.
- **Proyectos de confianza y declarados.** Una carpeta que nombra una petición (`x-latet-project`, la
  carpeta de trabajo que escriben Codex, OpenCode y Gemini CLI) solo añade protección: las
  relajaciones y las excepciones de petición de su proyecto no se aplican, y un proyecto declarado que
  está desactivado se rechaza. Un proyecto es de confianza gracias al vínculo del plugin de Claude
  Code, que termina tras 30 minutos sin llamadas, o gracias a una clave de lanzamiento. Las llamadas de
  Claude Code antes de la primera llamada del plugin se deciden solo con tus propias reglas.
- **Claves de lanzamiento.** `latet claude`, `latet codex`, `latet opencode`, `latet gemini` y
  `latet copilot` dan al agente una clave secreta de ese lanzamiento, en su propio ajuste de cabeceras,
  que Latet comprueba y quita; Codex en sus proveedores integrados, Codex anterior a la 0.115,
  OpenCode anterior a la 1.19.5, Gemini CLI en modo ACP, Copilot CLI fuera de Chat Completions y
  `latet run` no pasan ninguna, y el comando que los lanza lo dice en
  una frase. Una clave vigente en la salida de una herramienta se enmascara como el token de Latet. El
  panel muestra para cada petición si su proyecto era de confianza o declarado, y su lanzamiento:
  *Iniciado por Latet, clave verificada*, *Iniciado por Latet, falta la clave*, *Vinculado por el
  plugin de Claude Code* o *No iniciado por Latet*; `latet doctor` nombra los agentes vistos sin su
  clave, y un Codex, un OpenCode o un Gemini CLI demasiado antiguo para llevar una.
- **Excepciones de petición.** `request_exceptions` nombra exactamente lo que deja pasar (una
  cabecera, un parámetro de consulta o un miembro del cuerpo, en un destino y un perfil de petición),
  para todas tus peticiones o, con un vínculo de confianza, para un proyecto. Cada una espera tu
  aprobación, dura 30 días de forma predeterminada y 90 como máximo, con un aviso siete días antes de
  que acabe, o se hace permanente a propósito, marcada como tal y enumerada por `latet doctor` y
  `latet server status`. Una revisión de un perfil que cambia lo que nombra una excepción la suspende
  hasta que vuelvas a aprobarla. En la 0.11.0 ninguna excepción se aplica a un WebSocket, ni a
  un miembro del cuerpo dentro de un elemento del proveedor o en la fuente de una imagen, y ninguna
  nombra una ruta entera: el panel no ofrece ninguna ahí. Con el historial desactivado, las peticiones que dejó pasar cada una se
  cuentan en memoria, y se suman entre las instancias de un servidor. Un servidor guarda en
  PostgreSQL un índice de las excepciones aprobadas de cada persona, que lee `latet server status`:
  solo ids, tipos de alcance, fines y estados, nunca lo que nombra una excepción ni su descripción.
- **Cobertura visible.** Cada intercambio del panel dice cómo se cubrió su petición: bloqueada, cada
  parte leída, o dejada pasar por una regla o una excepción nombrada, con las cabeceras y los miembros
  que quitó su perfil y su perfil de petición. Un intercambio registrado antes de la 0.11 nunca se
  muestra como verificado: dice *Registrada por una versión anterior de Latet. No consta cuánto de
  esta solicitud leyó, así que no figura como verificada.* Tampoco uno que dejó pasar una parte sin
  leerla, ni una comprobación del plugin de Claude Code.
- **Lo que guardan los registros.** Los registros, los metadatos del historial y los registros de las
  decisiones de un servidor guardan categorías fijas y plantillas de ruta, nunca el nombre de una
  cabecera o de una herramienta, un valor ni la ruta de una petición tal como se envió.
- `latet doctor`, y el primer arranque del daemon con la 0.11 en su registro, enumeran por tipo y por
  número lo que la 0.11 detiene y la 0.10 enviaba: los proyectos en los que Latet está desactivado, el
  reconocimiento de texto sin instalar o desactivado, `--passthrough` en una entrada que arranca Latet,
  y las llamadas de tu historial que ningún perfil cubre.

- Las carpetas de los proyectos en tus ajustes se guardan en forma canónica: barras invertidas y letra
  de unidad en mayúscula en Windows, y sin separador final (`C:/work/shop/` se guarda como
  `C:\work\shop`). El id de cada proyecto no cambia, ni tampoco los ajustes que se le aplican.

- La caja fuerte de una persona en un servidor se mantiene entera en memoria cuando tiene 35.000
  valores o menos al construirse su espacio (unos 1,6 KB por valor en cada instancia y cada worker que
  atiende a la persona, contados en la memoria del espacio): los textos de la persona se enmascaran
  sin preguntar sus valores a PostgreSQL, unas dos veces más rápido que buscándolos. Por encima de
  35.000 valores, el espacio de una persona guarda en memoria los valores que la persona usó por
  última vez (`key_working_values`) y encuentra cualquier otro en PostgreSQL, como hace el de una clave
  de API: el espacio de una persona de 200.000 valores ocupa 24 MB, donde el de la 0.10.0 ocupaba 495.
  En los dos casos, cada petición pregunta primero a PostgreSQL qué cambiaron las demás
  instancias, de modo que un valor guardado u olvidado en una instancia lo ve la siguiente petición en
  cualquier otra ([Operación del servidor](../concepts/server-operations/)).
- **Un servidor enmascara más deprisa.** Los detectores pasan menos patrones por cada texto, los
  textos de una llamada de la API para aplicaciones se leen juntos, y una instancia encuentra los
  valores de una clave de API o de una persona mediante huellas de las palabras de su caja fuerte que
  guarda en memoria (unos 15 MB por un millón de valores), y pregunta a PostgreSQL solo por las
  palabras que pueden ser un valor o un valor ficticio. Medido en una instancia del equipo de la
  documentación, una clave de API de 200.000 valores enmascara 871 líneas de registro por segundo y
  restaura 2.960, donde la 0.10.0, medida de la misma manera, enmascaraba 350 y restauraba 2.114; una
  de 1.000.000 de valores 773 y 2.967, frente a 304 y 2.014; una clave nueva enmascara 191, frente
  a 153. El objetivo de 1.000 líneas por segundo con esos tamaños no se alcanza
  ([Velocidad](../applications/#velocidad)).
- **La actualización detiene el servidor una vez.** La 0.11 añade a cada fila de palabra de una caja
  fuerte un sitio para su huella y para el número del cambio que la escribió por última vez: detén
  cada instancia y cada
  worker, ejecuta `latet server migrate` una vez y arráncalos con la 0.11. Una caja fuerte guardada
  por la 0.10 recibe sus huellas tras el reinicio, en segundo plano, una vez, una instancia cada vez,
  mientras sus peticiones y llamadas se responden mediante la búsqueda en PostgreSQL, más despacio,
  sin rechazar ninguna; luego cada instancia construye su índice de las huellas, en 1,5 segundos para
  1.000.000 de valores ([Actualizar un servidor](../applications/#actualizar-un-servidor)).
- `latet server migrate`, y `latet server init` sobre tablas a las que les toca una migración, no
  cambian nada mientras una instancia del servidor está viva, de la 0.10 o de la 0.11, o mientras
  Valkey no responde: dicen cuántas instancias están vivas y de qué versión, y terminan con 2. Una
  instancia de la 0.10 cuenta como viva durante 30 segundos después de detenerse. Una vez aplicadas
  las migraciones vuelven a preguntar, y cuando una instancia se ha puesto viva entretanto lo dicen y
  terminan con un error (`migrate` 1, `init` 2): una instancia de la 0.10 que siguiera en marcha sobre
  las tablas de la 0.11 seguiría sirviendo peticiones sin leerlas como lo hace la 0.11.
- `latet server status` lista una instancia de Latet 0.10 todavía viva junto a las de la 0.11, por su
  id, como Latet 0.10 o anterior, y dice cuándo su lista se cortó en su límite (`instances: the list
  is partial, cut at its bound: an instance may be alive and not listed`, e `instances_whole` false
  en su respuesta).
- **Leer cada parte de una petición cuesta tiempo.** Medido en una instancia, como mediana, frente a
  la misma versión sin esa lectura: una petición de chat pequeña tarda alrededor de 1 ms más en un
  equipo (2,02 ms frente a 0,94) y en un servidor (6,99 ms frente a 5,95); una petición de 4.000
  huecos tarda 3,8 veces más en un equipo (362 ms frente a 96) y 1,4 veces en un servidor; una
  petición de 500 textos ya enmascarados y uno nuevo 2,8 veces en un equipo (36 ms frente a 13) y
  1,16 veces en un servidor.
- `latet server run --workers N` (o el ajuste `workers`) ejecuta N instancias del servidor en una máquina, en el
  mismo puerto, cada una en un proceso propio, para que un servidor use más de sus núcleos. En Linux cada worker
  escucha en el puerto con un socket propio, y el kernel reparte las conexiones nuevas entre ellos según un hash
  de las direcciones y los puertos de cada conexión: muchas conexiones se reparten de forma uniforme, unas pocas
  de larga duración, como las de un equilibrador de carga, pueden caer de forma desigual. El proceso que arranca
  los workers mantiene el puerto todo el tiempo, y solo los programas que se ejecutan con el usuario del
  servidor pueden escuchar ahí junto a ellos, así que ese usuario solo ejecuta Latet. Medido en Linux a través
  de la API para aplicaciones, ocho workers enmascaran 6,28 veces lo que uno y restauran 5,21 veces, y cada uno
  ocupa de 185 a 227 MB con el espacio de una clave de API de 200.000 valores. Varios workers se rechazan en
  Windows, y en macOS comparten un socket ([Operación del servidor](../concepts/server-operations/), Varias
  instancias en una máquina).
- Corregido: `latet upgrade` solo nombraba, en lo que instalaba, la detección de nombres y el texto en
  imágenes, y uv, que instala una herramienta con exactamente los extras que nombra su requisito,
  quitaba los demás: un servidor instalado con `latet[server]` que actualizaba perdía psycopg y redis,
  y `latet server run` se negaba entonces a arrancar. La actualización nombra ahora cada extra que
  tiene el Latet instalado, tal como los enumera el registro de uv de su instalación (donde ese
  registro falta o no se puede leer, cada extra cuyos paquetes están todos instalados), junto a la
  detección de nombres y el texto en imágenes; donde esos dos no se pueden instalar, los demás extras
  se quedan. `--to` y la ventana propia de la actualización en Windows también los conservan, y lo
  mismo los instaladores, ejecutados de nuevo sobre un Latet instalado con otros extras, tal como uv
  los enumera (un uv demasiado antiguo para enumerarlos no da ninguno, como antes).

  Una actualización ejecuta el código de la versión instalada, así que un Latet hasta la 0.10.4 sigue
  nombrando solo la detección de nombres y el texto en imágenes al pasar a esta versión. Un servidor
  con la 0.10.4 o anterior toma por tanto esta versión una vez por otra vía: el comando con el que se
  instaló, que nombra `server` y sus carpetas `UV_*`, o el instalador descargado de nuevo de GitHub y
  ejecutado con esas variables. Un servidor cuya actualización ya quitó `server` lo recupera con ese
  comando ([Operación del servidor](../concepts/server-operations/), Actualizar un Latet instalado
  como servicio del sistema). Donde falta el extra, `latet server run` da ahora ese comando con los
  demás extras dentro, y `latet upgrade` y la Solución de problemas dicen qué extras nombra
  `LATET_SOURCE` para una instalación a mano.
- Corregido: `latet upgrade` instalaba en la carpeta de herramientas propia de uv, fuera cual fuera la
  carpeta del Latet en ejecución: un servidor instalado con un `UV_TOOL_DIR` propio, como dice
  Operación del servidor, nunca se actualizaba, y se instalaba y arrancaba un segundo Latet en la
  carpeta personal de la cuenta que ejecutaba el comando. `latet upgrade` se detiene ahora ahí antes
  de que cambie nada, y muestra en su lugar el comando que hay que ejecutar, con las carpetas que
  encontró. Operación del servidor tiene una sección nueva sobre cómo actualizar un Latet instalado
  como servicio del sistema, e Instalación remite a ella a un servidor.

## 0.10.4 (2026-11-04)

- Corregido: en Windows, la detección local de nombres podía dejar de encontrar nombres cuando Latet
  se ejecutaba dentro de una aplicación instalada como paquete, como la aplicación de escritorio de
  Claude. Windows guarda los archivos de una aplicación así en una carpeta propia de la aplicación
  (`...\Packages\Claude_...\LocalCache\Local\latet`) y los muestra en el lugar de siempre, así que
  la carpeta del modelo de nombres podía encontrarse en el lugar de siempre mientras sus archivos
  estaban en la carpeta de la aplicación, también cuando se llegaba a ella a través de un enlace al
  lugar de siempre. ONNX Runtime, desde la versión 1.24 (una instalación recibe ahora la 1.30.0), se
  negaba entonces a cargar el modelo ("External data path escapes model directory"), y las
  peticiones seguían con los nombres en claro: solo el registro del daemon decía que el modelo local de nombres
  no se podía cargar. Latet abre ahora el modelo desde el lugar donde está realmente su archivo, y
  los nombres se vuelven a encontrar. En todos los demás casos, el modelo se carga como antes.

## 0.10.3 (2026-10-29)

- Corregido: en un servidor de Latet, un guardado de los ajustes que comprueba los nombres de los
  destinos (desde el panel o desde la administración) iniciaba la búsqueda del nombre siguiente después
  de que una búsqueda hubiera agotado todo el tiempo del guardado, cuando el reloj del sistema aún no
  había avanzado (visto en Windows). Ahora no inicia ninguna: esos nombres se comprueban cuando el
  servidor se conecta, como ya pasaba con los nombres que no responden a tiempo.
- La página de [seguridad](../security/) explica cómo agrupa Latet las peticiones en conversaciones:
  las cabeceras y los campos que lee, el principio del historial que usa cuando no hay ninguno, por qué
  dos chats que empiezan igual pueden compartir una conversación y restaurar cada uno los valores del
  otro, y que cada identidad de un servidor tiene su propia caja fuerte y sus propias conversaciones.
- Una versión solo se publica después de que la CI completa de su commit exacto en `main` haya salido
  bien: el flujo de publicación lo comprueba antes de construir nada, y si no, se detiene. La página
  nueva [Publicar Latet](../releasing/) describe los pasos, las guías para contribuir remiten a ella, y
  la plantilla de pull request pregunta qué comprobaciones se ejecutaron, quién revisó el cambio y
  dónde están las pruebas.

## 0.10.2 (2026-10-28)

- Corregido: un secreto de menos de doce caracteres (`password=1234`, `token=ab12f3`) recibía un valor
  ficticio de su misma forma, cuatro cifras o seis cifras hexadecimales, que un texto posterior podía
  contener por casualidad dentro de un id o un número. El unmask de la API de aplicaciones, que lee
  cada valor ficticio de la caja fuerte de una clave, ponía entonces ahí el secreto: una línea de
  registro que nunca lo había contenido volvía cambiada (`request_id=347bf309420a2290` como
  `347bf573020a2290`), y su mask tomaba el valor ficticio encontrado dentro de una palabra por un valor
  propio. Un secreto así recibe ahora un valor ficticio más largo de los mismos tipos, con sus signos
  en su sitio: quince cifras para un número (con su signo y su separador decimal, para que un número
  JSON siga siendo un número), doce cifras hexadecimales para un valor hexadecimal, y si no, doce
  letras y cifras. Un secreto de doce caracteres o más, y los datos personales, conservan valores
  ficticios de su forma. Un valor ficticio corto dado antes (ocho caracteres o menos) se queda, como
  cualquier valor ficticio. Una conversación, en un equipo o en un servidor, lo devuelve a su sitio
  como antes, también dentro de una palabra, porque solo restaura los valores ficticios que se le
  mostraron. El unmask de la API de aplicaciones solo lo devuelve donde está como palabra entera, sin
  letra, cifra ni guion bajo al lado: donde el valor de un secreto así se enmascaró dentro de una
  palabra o un número más largos (`postgres` en `postgresql://`, `1234` en `order_id=9912343`), unmask
  deja ahí su valor ficticio. Un valor ficticio de 9 a 11 caracteres se devuelve como antes en todas
  partes. Un reconocedor tuyo de la clase `secret` conserva el valor ficticio que da su regla, de la
  forma del valor; uno de ocho caracteres o menos se devuelve como un valor ficticio corto dado antes.
  Actualiza cada instancia de un servidor: una que todavía ejecute la 0.10.0 da valores ficticios
  cortos a los secretos cortos nuevos, y esos también se quedan.
- Corregido: una petición que Latet rechaza antes de enmascararla (su enmascaramiento falla o, en un
  servidor, su cuerpo se descodificaría por encima de `max_request_mb`) aparecía en el Historial como
  enviada sin leer, con un error, aunque no se había enviado nada. Ahora aparece como bloqueada y no
  enviada, con su motivo en las palabras de Latet y el tamaño de su cuerpo, y en un servidor la métrica
  de las paradas (`latet_agent_stops_total`) la cuenta.
- Corregido: un texto enmascarado antes de que la caja fuerte aprendiera un valor alrededor de un valor
  ya enmascarado en él dejaba en claro el resto de ese valor cuando se enmascaraba de nuevo. Un IBAN
  pegado a letras por los dos lados (`MarioIT60X0542811101000000123456Ferri`) que contenía un secreto
  conocido por la caja fuerte solo tenía enmascarado ese secreto; una vez que un texto posterior había
  enseñado a la caja fuerte el IBAN entero, el mismo texto enmascarado de nuevo como se había
  enmascarado antes (por una llamada posterior a las aplicaciones, en una conversación nueva, o dentro
  de la misma llamada o petición) seguía mostrando los demás caracteres del IBAN. Un nombre aprendido
  entero (`Mario Rossi`) alrededor de un apellido enmascarado antes (`Rossi`) dejaba `Mario` en claro
  del mismo modo. La comprobación final no lo detenía, porque ningún valor entero estaba en claro. Un
  texto así se enmascara ahora de nuevo, entero. También se enmascara de nuevo el texto de una
  conversación que nunca salió (su petición bloqueada o abandonada); uno que salió se vuelve a enviar
  tal como salió, como antes.
