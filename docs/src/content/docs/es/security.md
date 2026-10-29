---
title: Seguridad y modelo de amenazas
description: De qué protege Latet, de qué no, y cómo se protege a sí mismo.
---

:::note[Una capa de defensa]
Latet enmascara lo que reconocen sus detectores, y algunos agentes y superficies están cubiertos por
diseño, no probados en condiciones reales (la página de cada agente dice cuáles). No lo conviertas en
tu única salvaguarda para datos que nunca deben salir de tu equipo.
:::

## Qué protege Latet

Los secretos y los datos personales reconocibles de tu espacio de trabajo y de tus conversaciones:
claves de API, tokens, contraseñas, claves privadas, credenciales en cadenas de conexión, correos
electrónicos, números de teléfono, IBAN, números de tarjeta, números de identidad nacionales, números de
identificación fiscal y de IVA, direcciones IP, los términos que añades y la identidad de este equipo.
La [página de detección](../concepts/detection/) dice exactamente qué se reconoce.

## Límites de confianza

En un despliegue conectado también se aplica el [límite de confianza del servidor](../concepts/server/):
los originales pueden llegar al servidor y a las vistas autenticadas del panel.
Los administradores gestionan la configuración del servidor y los ajustes que comparte, como sus
destinos y su revisor; huellas, credenciales y vinculaciones de agentes siguen en los equipos.
Impón por separado las restricciones de acceso a proveedores si el servidor debe ser el punto de
control de la empresa.
Un servidor no toma los marcadores de una petición como prueba de que el plugin de Claude Code se
ha ejecutado: comprueba también las descripciones, los esquemas y los ejemplos de las herramientas.

- **Tu equipo** es de confianza: el agente, sus herramientas, el plugin y el daemon de Latet, y tus
  archivos.
- **El proveedor del modelo** no es de confianza para esos valores. Latet no da nada por supuesto sobre
  cómo se registran, se conservan o se usan las peticiones una vez que llegan, así que mantiene los
  originales lejos de él.
- **La red** que hay en medio está protegida por TLS, como siempre.

## Conversaciones, proyectos y restauración

Los identificadores de conversación agrupan peticiones; no autentican a una persona ni crean una
bóveda. La pasarela usa la primera cabecera no vacía en este orden: `x-claude-code-session-id`,
`session-id`, `session_id`, `thread-id`, `x-session-id`. Para la Responses API siguen un
`previous_response_id` conocido y después un `prompt_cache_key` de tipo cadena. Estos nombres se
buscan en el espacio de la identidad actual del servidor o en la instancia local.

Sin uno de estos identificadores, Latet deriva una clave del inicio del historial de la petición,
usando solo los primeros 8192 caracteres de su serialización compacta. Chats distintos con inicios
idénticos, o con diferencias solo después de ese prefijo, pueden compartir una conversación. Es la
reutilización de la misma entrada, no una colisión criptográfica del hash. Mantiene juntas las
peticiones sucesivas de clientes sin estado, pero no permite determinar qué chat lógico pretendía
identificar el cliente.

En esa conversación compartida, la respuesta puede restaurar valores registrados por cualquiera de
los dos chats. Por ejemplo, si el proveedor responde en el chat B con un sustituto emitido antes en
el chat A, Latet puede restaurar localmente el original de A. Identificadores explícitos distintos
evitan esa agrupación accidental cuando el cliente los usa de forma coherente. No son un límite de
autorización. En el texto de las peticiones HTTP normales, un sustituto pegado por el cliente se
enmascara como un nuevo valor literal; no significa el original que representaba. Una respuesta del
proveedor con un sustituto desconocido no da acceso a los originales de otro espacio del servidor.

Cada identidad del servidor tiene su propia bóveda y sus registros de conversación. Cabeceras,
identificadores de respuesta, claves de caché o inicios iguales no atraviesan ese límite. Los mismos
bytes de un sustituto pueden existir en bóvedas separadas; la restauración usa la asociación de la
bóveda receptora. En un equipo, los proyectos de una misma instancia de Latet comparten su bóveda:
cuando se admite, `x-latet-project`
selecciona los ajustes del proyecto, no un almacén separado ni una conversación nueva. Carpetas de
datos y bóvedas configuradas por separado proporcionan almacenes separados; trabajar en repositorios
distintos no basta. Consulta [Proyectos](../concepts/projects/) y
[Latet en un servidor](../concepts/server/).

## Dentro del alcance

- La revelación accidental de valores reconocibles al proveedor a través de los prompts, los archivos,
  la salida de las herramientas y el historial de la conversación.
- Las peticiones que saldrían con un valor sensible en claro en el modo cliente, por ejemplo por una
  vía que el plugin no cubre: la comprobación final del plugin y el daemon las bloquean.
- Las páginas web que intentan usar el daemon local (DNS rebinding, peticiones entre sitios).

## Fuera del alcance

- **Los valores que se les escapan a los detectores.** Consulta
  [Qué está protegido y qué no](../concepts/protection-scope/).
- **Un equipo comprometido.** El malware, otros usuarios de tu cuenta, o un plugin o un agente
  malicioso pueden leer tus archivos directamente. Latet no es un entorno aislado.
- **Un agente decidido a reducir la protección de Latet, o engañado para que lo haga.** Un cambio en
  los ajustes que reduce la protección espera tu aprobación, y los comandos que desactivan Latet se
  niegan a ejecutarse dentro de un agente: una barrera contra los errores, no un candado. Un agente se
  ejecuta con tu usuario, así que uno empeñado en esquivarla, o engañado por una inyección de prompts,
  puede leer lo que lee Latet y aprobar su propio cambio a través de la interfaz local del daemon. Un
  límite real requiere que Latet se ejecute como otro usuario, o en otro equipo
  ([Latet en un servidor](../concepts/server/)). Consulta
  [Los agentes y tus ajustes](../concepts/agents-and-settings/).
- **La exfiltración a través de herramientas.** Los originales se restauran en los argumentos de las
  llamadas a herramientas para que el trabajo del agente sea correcto en disco. Una instrucción
  introducida mediante una inyección de prompts podría hacer que el agente pasara un valor ficticio a
  un comando que envía datos por la red, como `curl`; el original restaurado saldría entonces por ese
  comando. Latet mantiene los valores ficticios en las herramientas web y en los conectores de
  claude.ai, pero un comando de shell o un servidor MCP local recibe el valor real. Mantén activadas
  las solicitudes de permiso de tu agente para los comandos de red.
- **La deducción a partir del contexto.** Los valores ficticios mantienen el formato del original, y el
  texto que los rodea puede revelar para qué se usa un valor.
- **Las superficies que Latet no puede ver**: las sesiones en la nube, Chat y Cowork de Claude Desktop,
  Cursor y los resultados de los conectores de claude.ai, que vuelven a través de los servidores de
  Anthropic.

## Cómo se protege Latet

**El daemon y el panel**

- Solo escuchan en `127.0.0.1`, así que nada de tu red puede llegar a ellos.
- Las peticiones cuya cabecera `Host` no es una dirección local se rechazan, lo que frustra el DNS
  rebinding.
- Las peticiones que vienen de una página web (con una cabecera `Origin`, o con un `Sec-Fetch-Site`
  entre sitios) se rechazan en las rutas del proxy.
- El panel necesita un enlace de inicio de sesión de un solo uso que da `latet dashboard`, después una
  cookie de sesión `HttpOnly`, `SameSite=Strict`, y cada cambio necesita una cabecera que otros sitios
  no pueden enviar. Consulta la [guía del panel](../dashboard/#la-privacidad-del-propio-panel).
- El plugin de Claude Code habla con el daemon con un token aleatorio que empieza por `latet_brg_`,
  guardado en `bridge.token` en la carpeta de datos (un token creado por una versión anterior no tiene
  prefijo y funciona como antes). Para sustituirlo por uno con prefijo, ejecuta `latet stop`, borra
  `bridge.token` en la [carpeta de datos](../getting-started/uninstall/), ejecuta `latet start` (crea
  uno nuevo) y `latet agents apply`, que escribe el nuevo token en los ajustes de tus agentes. Sin el
  token, el daemon se niega a enmascarar, restaurar o comprobar nada.
- **Tus agentes llegan al daemon por http simple en `127.0.0.1`, y es a propósito.** Ese tráfico nunca
  llega a una tarjeta de red: ningún otro equipo, ni siquiera en tu wifi, puede verlo. Un programa de
  tu equipo que pudiera leerlo (con permisos de administrador, o con tu usuario) podría leer
  directamente la caja fuerte y tus archivos, así que cifrarlo no lo detendría. Exigiría instalar en el
  sistema operativo una autoridad de certificación propia, cuya clave, si la robaran, permitiría a
  alguien hacerse pasar por cualquier sitio web en tu equipo. Lo que sale de tu equipo va cifrado: las
  peticiones a tu proveedor, tal como las envía el agente, y al revisor local en otro equipo (más
  abajo).

**El tutorial**

- `latet tutorial` ejecuta su entorno aislado dentro del propio comando, en `127.0.0.1` y en un puerto
  libre, protegido como el panel: un enlace de un solo uso y después una cookie propia, que nunca
  sustituye al inicio de sesión de tu panel.
- El entorno aislado tiene sus propios datos, ajustes y caja fuerte, en una carpeta temporal, y
  ninguna de tus claves ni de tus tokens. Su única ruta lleva a un modelo simulado en el mismo proceso,
  así que ninguna petición llega a un proveedor; lo que saldría fuera de él, como un destino o el
  servidor de un revisor local, se rechaza ahí. Consulta
  [Tutorial](../getting-started/tutorial/#un-entorno-aislado).

**El revisor local en otro equipo**

- Recibe los textos originales, así que se llega a él por `https://`. Fuera de tu red local no se
  acepta nada más.
- Un servidor tuyo con un certificado autofirmado se considera de confianza por su huella digital, que
  comparas una vez con la del servidor (`latet llm trust`, o *Es la misma: confiar en él* en el panel).
  A partir de entonces solo se acepta ese certificado, que se comprueba antes de enviar ningún texto, y
  no se añade ninguna autoridad al sistema operativo.
- En tu red local todavía se acepta `http://`, para los servidores configurados antes: el panel,
  `latet doctor` y el registro del daemon avisan de que los textos cruzan tu red sin cifrar.

**El archivo de un proyecto**

- Un `latet.yaml` en un repositorio solo puede añadir protección: un perfil más estricto, más términos
  y reconocedores. Lo que reduciría la protección (valores que no se enmascaran nunca, reglas
  desactivadas, Latet desactivado) se ignora, así que un repositorio que clonas no puede debilitar tus
  ajustes. Consulta [Proyectos](../concepts/projects/#el-archivo-del-proyecto).
- Sus reconocedores son expresiones regulares que ejecuta RE2, en un tiempo lineal respecto al texto, y
  el archivo tiene un tamaño máximo: un archivo hostil no puede bloquear el enmascaramiento.

**Tus valores**

- La caja fuerte cifra cada original en disco con AES-GCM. La clave se deriva de una clave raíz propia
  de cada instalación, guardada en el almacén de credenciales de tu sistema operativo (el Administrador
  de credenciales de Windows, el Llavero de macOS, el llavero del escritorio en Linux), o en un archivo
  privado, `root.key`, cuando no hay ninguno.
- Los valores ficticios se derivan con HMAC de esa clave propia de la instalación: sin la clave, nadie
  puede deducir el original a partir de un valor ficticio.
- Un valor ficticio solo se restaura en la conversación a la que se mostró, así que otra sesión no
  puede usar la caja fuerte como oráculo.
- El historial del panel guarda las peticiones tal como salieron de tu equipo, ya enmascaradas (las
  bloqueadas, tal como habrían salido), cifradas con la misma clave, durante 14 días por defecto.
  Define `history: metadata` (solo recuentos y tiempos) o `history: off` en la
  [configuración](../reference/configuration/) para guardar menos.

**Copias de seguridad**

`latet backup` escribe [tu instalación](../concepts/export-import/#tu-instalación-para-otro-equipo)
en un único archivo:
- **La clave sale de tu frase de contraseña.** Se deriva con Argon2id (128 MiB de memoria, 3 pasadas)
  y una sal aleatoria.
- **El cuerpo se sella por fragmentos.** Es un flujo tar cortado en fragmentos de 1 MiB, cada uno
  sellado con AES-256-GCM. El nonce de un fragmento es su número y si es el último, y la cabecera del
  archivo (su formato, y los parámetros y la sal de la clave) se autentica con cada fragmento.
- **Qué se rechaza, antes de usar nada del archivo:**
  - otra frase de contraseña;
  - un fragmento modificado, movido, quitado o añadido;
  - un archivo truncado, o una cabecera modificada;
  - parámetros de la clave más allá de lo que Latet acepta (un archivo no puede hacerle usar toda la
    memoria);
  - cualquier cosa dentro que no sean los archivos esperados.
- **No hay nada en claro en disco.** Las bases de datos se copian tal cual (con sus valores sellados),
  la clave raíz solo existe dentro del flujo cifrado, y el archivo se escribe entero, legible solo por
  ti.
- **Con `--vault`, el archivo contiene tus valores reales y la clave para leerlos:** guárdalo como
  guardarías los valores.

`latet upgrade` copia, antes de instalar una versión, la caja fuerte, el historial y los ajustes en
`backups/before-<versión>-<hora>` en la carpeta de datos de Latet
([Actualizar](../getting-started/installation/#actualizar)):
- **Selladas como la caja fuerte.** Las bases de datos se copian tal cual, sus valores sellados con
  la clave de este equipo, que no se copia; la carpeta es legible solo por ti.
- **Siguen lo que quitas**, mientras funciona Latet 0.9.10 o una versión posterior.
  *Olvidar* quita un valor de cada copia, borrar el historial borra
  también el suyo, y su historial no se conserva más tiempo que el tuyo. Una copia que no se puede
  limpiar se borra, o se aparta y no se vuelve a ofrecer.
- **Tres como máximo, y volver atrás es cosa tuya.** Restaurar una copia solo se ofrece en un
  terminal tuyo, y un agente no puede ejecutar `latet upgrade --to` hacia una versión más antigua.

**El revisor local** (opcional, [desactivado hasta que lo actives](../concepts/llm-review/))

- Se ejecuta en tu propio servidor: en este equipo, o en otro equipo tuyo una vez que lo confirmes
  (`remote: true`), por https fuera de tu red local. Lee el texto original, así que dale solo un
  servidor que controles tú.
- Puede añadir a lo que se enmascara, nunca quitar nada; lo que encuentra se comprueba contra el texto
  (un valor que se inventa, o uno dentro de un marcador de posición, se descarta).
- Una salida de una herramienta con instrucciones para él puede, como mucho, hacer que encuentre menos;
  los detectores se siguen aplicando, y una respuesta vacía a un texto que se dirige al revisor no se da
  por buena (la petición se detiene).
- La clave de API de su servidor se guarda en el almacén de credenciales del sistema operativo (o viene
  de una variable o de un archivo), solo se envía a ese servidor, y nunca se escribe en los registros,
  en el panel ni en config.yaml. El panel puede guardarla o quitarla, nunca mostrarla.
- Lo que ha encontrado se recuerda sellado en la base de datos de la caja fuerte, como los originales
  de la caja fuerte.
- Solo se le muestran imágenes cuando lo activas (`llm_review.images`): mostrárselas a un servidor en
  otro equipo espera tu aprobación cuando lo pide un agente.

**Imágenes** (con el [reconocimiento de texto](../concepts/images/) opcional)

- Las imágenes se leen en tu equipo; los modelos se descargan una vez, en una revisión fijada, cada
  archivo se comprueba contra un SHA-256 fijado, y después se ejecutan sin conexión.
- Las imágenes tal como se enviaron se guardan en el historial del panel, selladas, solo con
  `history: full` (10 MB como máximo cada una), y se van con su intercambio.
- Lo que decides sobre una imagen en el Revisor en directo, y una imagen que el revisor local retuvo,
  se recuerdan sellados en la base de datos de la caja fuerte, mediante un hash con clave de la imagen.
- Una imagen que no se puede leer se retiene, nunca se envía tal como estaba.

**Tráfico**

- Las cabeceras de credenciales (`Authorization`, claves de API, cookies y similares) se reenvían sin
  cambios, pero nunca se escriben en los registros ni en el panel: solo se registra su longitud. Las
  cabeceras propias de Latet (`x-latet-*`, una de las cuales nombra una carpeta tuya) nunca salen de tu
  equipo.
- Una excepción, cuando usas Amazon Bedrock con tu inicio de sesión de AWS: Claude Code no envía
  credenciales, y Latet firma cada petición para Amazon (SigV4) con tu perfil de AWS. Lee el perfil como
  lo hace la AWS CLI, guarda las credenciales solo en memoria, nunca las escribe en ningún sitio y solo
  firma peticiones a Amazon Bedrock. Las credenciales que solo están en tu shell (aws-vault, claves
  exportadas) las pasa `latet claude` solo para su sesión: Latet las olvida cuando esa sesión termina,
  aunque se mate `latet claude`, y cuando caducan. Una ruta que firma lleva una clave generada a
  partir del propio token de Latet, así que otro usuario de tu equipo, o un programa que no puede leer
  tus archivos, no puede conseguir que se firmen peticiones en tu nombre; un programa que se ejecuta con
  tu usuario puede leer ese token, igual que puede leer tus archivos de AWS. La clave nunca llega a un
  registro. Consulta [Puntos de conexión de empresa](../agents/company-endpoints/#amazon-bedrock).
- Un repositorio no puede decidir adónde van tus peticiones: Latet lee la ruta de un agente de tus
  propios ajustes, nunca de los de un proyecto, y el plugin de Claude Code detiene los prompts cuando
  los ajustes de un proyecto establecen adónde van, o un proxy para ellas, aunque sea a través de Latet.
  Una dirección escrita de forma que dos programas podrían leerla de manera distinta (con una barra
  invertida, un espacio, un nombre de usuario dentro) se rechaza, nunca se adivina. Cuando el plugin no
  puede comprobar la ruta de una sesión, sus prompts se detienen.
- Las peticiones de Claude Code, o que llevan el token de una suscripción de Claude, nunca se
  reescriben, solo se comprueban.
- Cuando el enmascaramiento falla, la petición se rechaza en lugar de enviarse: un cuerpo que Latet no
  puede descodificar, un detector obligatorio que da error o un valor que no puede sustituir acaban
  todos en un error que dice que no se ha enviado nada. En Claude Code, cuando no se puede llegar al
  daemon, el plugin retiene el contenido.
- El TLS hacia el destino se verifica con el almacén de certificados de tu sistema operativo, así que
  un proxy de empresa cuyo certificado raíz esté instalado ahí sigue funcionando.
- La captura en bruto de los intercambios para depuración está desactivada por defecto. Cuando la
  activas (`latet serve --capture <folder>`), las cabeceras de credenciales se suprimen, pero las
  capturas siguen conteniendo el contenido de las conversaciones: guárdalas en tu equipo.

El daemon no abre ninguna conexión de red, salvo para reenviar las peticiones de tu agente al proveedor
(o al punto de conexión de empresa) que tu agente ya usa y, cuando lo activas, para consultar al
revisor local, en este equipo o en otro equipo tuyo. Cuando firma para Amazon Bedrock con un perfil
SSO, la AWS CLI que ejecuta puede contactar con el servicio de inicio de sesión de AWS para renovar tu
sesión, como hace cuando la usas tú. Los comandos solo usan la red para lo que pides: `latet ner install` y
`latet ocr install` descargan sus modelos de huggingface.co, y `latet upgrade` busca la última versión
en GitHub y la descarga. No hay telemetría ni analítica, ni en el daemon, ni en el panel, ni en este
sitio de documentación.

**En un servidor Latet** ([Latet en un servidor](../concepts/server/))

- La administración responde solo por HTTPS, a un programa: una petición desde una página web se
  rechaza, y un token de administración solo se comprueba después de que su dirección supere un límite
  de intentos rechazados.
- El servidor no guarda ningún secreto que emite: solo un hash con clave de cada token, clave y
  código. La clave de cada espacio está sellada con la clave maestra del servidor, que se guarda aparte
  de las copias de seguridad de la base de datos.
- Cada llamada de administración se anota en un registro encadenado, también las lecturas y los
  rechazos; `latet server log verify` comprueba la cadena.
- El token que tienen los agentes de un equipo abre el bridge del plugin y el proxy del espacio, nunca
  el panel: este necesita además la clave del equipo, que no se da a ningún agente.

## Informar de una vulnerabilidad

Por favor, no abras una incidencia pública. Usa el informe privado de vulnerabilidades de GitHub en el
[repositorio de Latet](https://github.com/Latet-AI/latet/security).
