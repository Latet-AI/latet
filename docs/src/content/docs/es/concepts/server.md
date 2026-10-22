---
title: Latet en un servidor
description: Espacios, almacenamiento, ajustes compartidos y límite de confianza de un servidor de Latet.
---

Un servidor de Latet recibe las peticiones protegidas de los agentes conectados. En modo
puerta de enlace las enmascara antes de reenviarlas al proveedor del modelo y restaura la
ruta de retorno protegida; las peticiones reconocidas como Claude Code usan el plugin y
el límite de solo verificación descrito abajo. Los datos originales pueden llegar al
servidor de Latet. Usar un servidor cambia en quién debes confiar; no vuelve infalible
la detección ni protege el tráfico que nunca llega a Latet.

## Clientes conectados

El equipo debe usar la ruta del servidor y mantener privadas las vinculaciones de conexión
y credenciales. El enmascarado en el servidor cubre los clientes puerta de enlace.
Claude Code también necesita el plugin: en el terminal Windows conectado admitido, la
vinculación privada del proceso auxiliar y los ajustes de confianza eligen el bridge del
servidor. El token personal pasa por stdin; la vinculación de metadatos no almacena tokens.
Una vinculación ausente o insegura detiene esa ruta.

Para las peticiones reconocidas como Claude, el plugin enmascara las descripciones de
herramientas, y la comprobación final del servidor también lee las descripciones, los esquemas
de parámetros y los ejemplos de las herramientas: un valor que quede en claro en ellos detiene la
petición. Los marcadores no certifican que el plugin se haya ejecutado. Lee el
[alcance de la protección](../protection-scope/) para conocer esta excepción limitada y
los controles separados de esquemas de salida. [Conectar un equipo](../../getting-started/server/)
explica la aplicación, el diagnóstico, el reenvío de identidad y la desconexión.

Si Latet está apagado en una carpeta, una petición que una instancia recibe para ella antes de que el plugin se
lo haya comunicado al servidor se enmascara como si Latet estuviera encendido: ante la duda, protege.

### El token y la clave de un equipo

Cuando un equipo se conecta, el servidor le da dos secretos, cada uno mostrado una sola vez. El
**token** va a los agentes: Claude Code lo guarda en sus ajustes y su entorno (`LATET_BRIDGE_TOKEN`),
donde cada comando que ejecuta puede leerlo. Desde cualquier lugar donde se alcance el servidor, el
token abre el bridge del plugin y el proxy del espacio (quien lo tenga puede hacer enmascarar textos en
el espacio y hacer restaurar los valores ficticios que tenga), dice de quién es y se revoca a sí mismo. La
**clave del equipo** se queda en el almacén de credenciales del equipo, o en el archivo privado del
token, y ningún agente la recibe: el servidor la pide también antes de dar un enlace de inicio de sesión
al panel o de añadir algo a la identidad de la persona, así que el token solo nunca abre el panel, donde
se puede mostrar cada original, y nunca reduce la protección de la persona: el único cambio que puede
hacer en sus ajustes es añadir un destino para una dirección, lo que no reduce nada. La clave impide que
un token filtrado (desde el entorno, los ajustes, un registro o con un solo `curl`) abra el panel; no
detiene un comando escrito para leer los dos del almacén de credenciales. En un equipo conectado,
`latet dashboard` y `latet identity send` se niegan a ejecutarse dentro de un agente, y
`latet disconnect` olvida los dos.

### Lo que queda en el equipo

- **El `latet.yaml` de un repositorio no se aplica.** El servidor no lee ningún archivo del equipo:
  lo que un equipo de trabajo comparte en su archivo de proyecto (un perfil más estricto, sus términos
  y reconocedores) no llega a las peticiones de una persona conectada. En un proyecto así,
  `latet doctor` y `latet claude` lo dicen. Ajústalo en tu panel: importa el `latet.yaml` del
  proyecto en **Reglas** con **Importar reglas**.
- **La comprobación de los archivos CLAUDE.md**, que Claude Code da a su revisor del modo
  automático, se ejecuta en el equipo, en `latet doctor`.
- **Claude Code a través de un servidor necesita un terminal Windows** en esta versión. En Linux y
  macOS su plugin retiene cada prompt, y Latet lo dice de entrada donde lo aplicaría. El modo de
  impresión (`claude -p`) se retiene en todo equipo conectado.

### Certificados y conexiones propias de los agentes

Cada agente confía en el certificado del servidor mediante una variable propia que Latet define:
`NODE_EXTRA_CA_CERTS` para Claude Code y OpenCode, `CODEX_CA_CERTIFICATE` para Codex. Se suma a lo que el agente
ya acepta, y la copia que escribe Latet conserva los certificados que la persona había indicado allí, por
ejemplo la autoridad de su empresa. Un certificado autofirmado solo llega a los agentes en la forma que le da
`latet server certificate --make`, limitado al nombre o la dirección del servidor; un certificado de la
autoridad de la empresa funciona tal cual.

Al arrancar, los agentes también llaman a sus propios proveedores, fuera de Latet: Claude Code
`api.anthropic.com`, con su clave de API, salvo que su tráfico no esencial esté desactivado en su configuración;
Codex `chatgpt.com` y `api.github.com` para sus plugins; OpenCode el registro de npm y GitHub. Ninguna de esas
llamadas lleva la dirección del espacio, su clave de ruta ni el token. Donde el servidor deba ser el único camino
hacia los modelos, bloquea los hosts de los proveedores de modelos, entre ellos `api.anthropic.com` y
`chatgpt.com`, y desactiva el tráfico no esencial de Claude Code; la lista de plugins de Codex fallará entonces.
Por ejemplo:

```text
allow  latet.acme.lan:443                        # the Latet server
allow  api.github.com:443, github.com:443        # Codex's plugins and OpenCode's start-up, if wanted
allow  registry.npmjs.org:443                    # OpenCode's start-up, if wanted
deny   api.anthropic.com:443, chatgpt.com:443    # the models only through the server
deny   every other host of the AI providers
```

## Espacios e instancias

Cada persona y cada clave de API tiene un espacio separado, con su caja fuerte, historial
y ajustes. Los equipos de una persona comparten ese espacio. Una clave de API representa
un programa, no tiene panel personal y recibe sus ajustes del administrador.

Una instancia es un proceso del servidor de Latet en ejecución. Varias instancias pueden
servir la misma dirección HTTPS pública detrás de un equilibrador de carga, usando la
misma base de datos PostgreSQL, Valkey y clave maestra. PostgreSQL guarda el estado
duradero; Valkey coordina el estado temporal. Las solicitudes no necesitan afinidad de
sesión. Añadir instancias no crea copias separadas del espacio de una persona. Consulta
los requisitos de los servicios en [Operación del servidor](../server-operations/).

## Qué guarda el servidor

Los valores y valores ficticios de la caja fuerte, los textos e imágenes registrados y los ajustes
de un espacio se cifran con su clave de identidad. Esa clave se cifra con la clave
maestra del servidor; los ajustes compartidos se cifran con la clave maestra. Guarda
la clave maestra separada de las copias de seguridad de la base de datos. Sin la clave
correspondiente no se pueden recuperar los datos cifrados.

Algunos metadatos siguen siendo legibles en PostgreSQL: nombres y direcciones de correo
de las identidades, identificadores externos, etiquetas y fechas de los tokens, nombres
de reglas de la caja fuerte, contadores y fechas, y actores, acciones, destinos, identificadores,
recuentos y direcciones de clientes del registro de administración. Los identificadores
de proyecto y conversación del historial usan hashes con clave. Valkey usa nombres
derivados con clave, datos sensibles cifrados y mensajes de coordinación firmados.

El cifrado en reposo no oculta los originales al servidor en ejecución ni a su operador.
Las vistas autorizadas del panel reciben los originales en el navegador. Un administrador
puede emitir el código de registro de una persona y usarlo para acceder a su panel;
esa operación queda registrada. También puede configurar un servicio revisor que reciba
texto original. Por tanto, debes confiar en los administradores del servidor, en quien
opera la instalación y en ese revisor. El cifrado y las firmas no impiden que alguien
con acceso de escritura a la base de datos restaure un estado válido anterior.

Borrar el historial registrado elimina los intercambios de este espacio, no su caja fuerte.
No borra copias de seguridad, transcripciones de agentes, registros del proveedor ni
copias ya recibidas por un navegador.

## Ajustes compartidos y reglas propias

Los ajustes compartidos son lo que el servidor necesita para funcionar, igual para cada espacio:
los destinos a los que pueden ir las peticiones, el revisor local compartido, el reconocimiento de
texto, el historial y su conservación. Los establecen los administradores. Las reglas de protección
(términos, reconocedores, el perfil, las políticas y lo demás) son propias de cada identidad: las
personas establecen las suyas en su panel, y un administrador establece las de una clave de API
con `latet server config --key` y `latet server rules --key`.

Un equipo de trabajo comparte sus reglas a mano: una persona las exporta (**Exportar reglas** en la página
**Reglas** del panel, o `latet rules export` en un equipo no conectado a un servidor) y cada uno
importa el archivo en su propio panel. [Exportar e importar](../export-import/) describe el
archivo. Las reglas establecidas para todos, que nadie puede relajar, están previstas para la
Enterprise Edition.

Un cambio compartido que reduce la protección, como un destino movido o el revisor apagado, espera
la aprobación de la persona. Mientras tanto, siguen en vigor los ajustes más estrictos que ya
aprobó. Guardar cambios propios no aprueba el cambio compartido pendiente, aunque el guardado
incluya el mismo elemento. Un guardado personal no puede eliminar sin avisar un destino
establecido para todos.

Una clave de API no tiene a nadie que apruebe un cambio así: llega al espacio de la clave de inmediato,
también un cambio que reduce la protección, así que comprueba qué hace en tus programas un cambio en
los ajustes compartidos antes de hacerlo.

Distingue estos resultados del guardado:

| Resultado | Significado |
| --- | --- |
| `409 settings_stale` | Los ajustes han cambiado desde que la página los leyó; no se ha conservado nada de este guardado. Recarga antes de volver a editarlos. |
| `409 settings_change_waits` | Ahora no se puede combinar el cambio pendiente con los ajustes aprobados; no se ha conservado nada de este guardado. |
| `409 settings_saved_change_pending`, `saved: true` | Tus cambios se han conservado, pero no se ha podido actualizar la propuesta pendiente. Recarga y revisa el cambio pendiente; no repitas el guardado a ciegas. |
| `409 settings_terms_too_long` | Un término supera la longitud aceptada; no se ha conservado nada de este guardado. |

Otros límites y fallos de almacenamiento también pueden rechazar un guardado. Un error
HTTP por sí solo no demuestra que no se haya escrito nada; conserva el motivo devuelto
y cualquier indicación de guardado parcial.

### Los destinos propios de una persona

Un destino que una persona define en sus propios ajustes nunca lleva a la red del servidor. Una dirección privada,
unique-local, de NAT de operador, de bucle local, link-local, multicast o reservada se rechaza, escrita como
dirección o como nombre que resuelve a ella; también las formas IPv6 que llevan una dirección IPv4 o conducen a una
pasarela (6to4, Teredo, prefijos NAT64). En una red con NAT64, el servidor descubre sus prefijos como describe la
RFC 7050, y hasta que los conoce no alcanza ninguna dirección IPv6 que un NAT64 pudiera traducir: un nombre que
también tiene una dirección IPv4 va allí. Un nombre se resuelve de nuevo cuando el servidor se conecta, y la petición
va a la dirección que se comprobó. Para estos destinos no se usa ningún proxy. Solo un administrador, en los ajustes
compartidos, define un destino dentro de la red.

Un servidor solo IPv6 detrás de un DNS64 con el prefijo conocido no alcanza, por tanto, ningún destino propio
accesible solo por IPv4.

## Los límites de un espacio

- **Reglas:** los ajustes propios de una persona contienen como máximo 500 reconocedores, 500 reglas de campos
  y 500 patrones de valores que nunca se enmascaran, contados sobre los ajustes y todos sus proyectos. Un
  guardado que los supere se rechaza.
- **Proyectos:** como máximo 200 por persona.
- **Valores:** un valor de más de 16.384 caracteres se suprime en lugar de sustituirse por un valor ficticio,
  y nunca se guarda ni se restaura; un término tiene como máximo 8.192 caracteres.
- **Memoria:** una instancia da a sus espacios 2 GiB fijos, y un espacio usa como máximo la mitad. Un espacio
  que llega a su parte no aprende valores nuevos: una petición que trae uno se detiene con un mensaje que lo
  dice, hasta que se olvidan valores en el panel, hay menos reglas o proyectos, o el servidor tiene más. No se
  cierra el espacio de ninguna otra persona para hacer sitio.
- **Las estadísticas** cuentan los últimos 100.000 intercambios, y una persona conserva sus diez sesiones del
  panel más recientes.

## El límite de la protección

Latet solo puede examinar las solicitudes compatibles que pasan por él. Si la política
de la empresa exige que todo el tráfico de los modelos pase por el servidor, la red
debe impedir que los equipos lleguen directamente a los proveedores. Configura y mantén
esa restricción en el límite de la red; Latet no la instala ni la aplica. El tráfico
de red arbitrario de las herramientas queda fuera de esta garantía.

[Qué está protegido y qué no](../protection-scope/) describe los límites de detección,
imágenes y protocolos. El contenido no compatible puede rechazarse o retenerse en una
ruta de servidor protegida; una marca histórica o un endpoint disponible del proveedor
no demuestra compatibilidad. Un servidor no puede leer el repositorio de un equipo
conectado solo porque aparezca un proyecto en su panel. La información del proyecto
que tiene es la que se le ha facilitado.

Para los comandos de administración, empieza por [Administrar un servidor de Latet](../../getting-started/server/).
