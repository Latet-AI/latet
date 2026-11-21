---
title: Administración del servidor
description: Administra un servidor desde tu equipo y mantén privadas sus credenciales.
---

Ejecuta `latet server` en tu propio terminal. Los comandos que cambian la administración
se niegan a ejecutarse en un agente; `config get` y `rules export`, al terminal o a un archivo,
también se niegan, porque pueden mostrar valores en claro (los términos de una clave de API, las indicaciones
para el revisor). El estado, las listas de identidades y el registro
de administración no contienen credenciales.

Indica el servidor e introduce el token en la entrada oculta:

```sh
latet server --server https://latet.acme.lan status
```

`status` lista cada instancia viva del servidor, cada worker de cada máquina en una línea propia: su ID,
su número de worker, si está lista, su memoria y los índices de sus cajas fuertes en MiB, sus espacios y
su versión; lo que una instancia no ha dicho se muestra como desconocido o se omite. Una línea propia,
`new request members:`, dice cómo se lee para cada identidad un miembro de una petición que ningún
perfil de petición nombra todavía: `read and checked` (`unknown_members: check`, donde los ajustes
propios de una identidad pueden poner `refuse`), `refused for every identity` donde las directrices de
la empresa bloquean `refuse`, o `cannot be said now` cuando el servidor no puede leer en ese momento sus
ajustes compartidos o las directrices ([Nuevos miembros de las
peticiones](../../getting-started/upgrading-to-0-11/#nuevos-miembros-de-las-peticiones)). Una instancia
de Latet 0.10 o anterior no dice nada de sí misma: cuando responde una instancia de la 0.11, lista una
de la 0.10 todavía viva por su ID, como Latet 0.10 o anterior; un servidor que sigue en la 0.10 responde
solo con la instancia que ha respondido. Antes de una actualización, la comprobación es `latet server
migrate`: no cambia ninguna tabla mientras una instancia de cualquier versión está viva, una de la 0.10
durante 30 segundos después de detenerse, y dice cuántas están vivas y de qué versión. La lista sale de
una lectura acotada de las claves de Valkey: cuando esa lectura se cortó en su límite, `status` dice
`instances: the list is partial, cut at its bound: an instance may be alive and not listed, and run
another release or edition`, y el campo `instances_whole` de su respuesta es false (null cuando Valkey
no respondió).

En la máquina del servidor, `latet server run --workers N` arranca N instancias, los workers, en el
mismo puerto, cada una en un proceso propio; `LATET_SERVER_WORKERS` o `workers` en `server.yaml` dan lo
mismo, y `--workers` tiene prioridad sobre ambos, la variable sobre el archivo. De 1 a 64, uno por defecto. Más de
uno necesita Linux o la imagen del contenedor: en Windows `latet server run` los rechaza, y el servicio
de Windows arranca uno
([varias instancias en una máquina](../../concepts/server-operations/#varias-instancias-en-una-máquina)).

O usa `--admin-token-file FILE`: un archivo UTF-8 privado que contenga solo el token, o un
objeto `{"url": "https://latet.acme.lan", "token": "<administration token>"}`.
Un objeto así solo se acepta para su servidor. En POSIX usa `chmod 600 FILE` (o `0400` para
la cuenta que ejecuta un contenedor), en una carpeta que otros usuarios no puedan modificar.
En Windows, el archivo y su carpeta deben permitir el acceso solo a tu cuenta, SYSTEM y
Administrators; sigue la corrección `icacls` que muestra el comando.
`--admin-token-file -` lee una línea de la entrada estándar.

En un equipo donde se ejecutan agentes, mantén los tokens de administración fuera de los perfiles
de la shell y las variables de entorno de la cuenta: los procesos del mismo usuario pueden leer
el entorno de otro proceso. `LATET_SERVER_ADMIN_TOKEN` es para el almacén de secretos de una CI
o un contenedor, junto con `LATET_SERVER_URL`; nunca se envía a otro servidor.

`keys add`, `keys add-token`, `keys rotate` y `admin-token issue` aceptan `--out FILE`: el nuevo archivo privado
recibe el secreto y el comando solo muestra su ID y el archivo. Nunca pongas códigos, claves
o tokens de administración en una incidencia, un canal compartido o una conversación con un agente.
El código de un solo uso de una persona permite a quien lo use conectar un equipo como esa
persona y abrir su panel; su emisión queda registrada.

Usa `admin-token issue --label "Daniel's laptop" --days 30` para un token con caducidad.
Un token `--never` requiere una etiqueta y permite todo lo que puede hacer un administrador
hasta su revocación: personas y sus códigos, claves de API, ajustes compartidos y tokens de
administración. Un token emite otros que caducan, como muy tarde, cuando caduca él, así que solo un token
que no caduque puede emitir otro que no caduque; en la máquina del servidor,
`latet server init --new-admin-token --never` crea uno. Guárdalo aparte y úsalo
solo para emitir otros. Una CI que lo tenga concede estos poderes para siempre a quien pueda
cambiar sus tareas; dale un token con `--days` y emítelo de nuevo antes de que caduque.

Las eliminaciones piden confirmación en tu terminal incluso con `--yes`; los scripts necesitan
`--yes`. `config reset` solo sustituye, tras la confirmación, ajustes compartidos que el
servidor no puede abrir. `config get` y `rules export` pueden mostrar valores en claro: mantén
privada su salida. `log verify` muestra la cabeza de la cadena; guarda una copia en otro
lugar y compárala la próxima vez: quitar entradas del final deja intacta la cadena restante.

## Quién administra, y perder un token

**El administrador es quien tenga un token de administración.** No existe una cuenta de
administrador: `latet server init` muestra el primer token a quien inicializa el servidor, y cada
token puede emitir otros. Un token no es una persona: el registro de administración nombra el token
que hizo cada cosa (`admin:a_3fk7…`), y un administrador no necesita un espacio propio en el
servidor, aunque puede añadirse como persona igual que cualquier otra. Cada token de
administración puede hacerlo todo, en las dos ediciones. Con la Enterprise Edition, a las personas de la empresa se les
pueden dar roles en el panel, como un auditor que lee los informes y el registro de administración sin cambiar nada.

Un token emitido sin `--days` ni `--never` caduca tras `admin_token_days` (90 días, salvo que los
ajustes del servidor digan otra cosa), o antes si caduca primero el token que lo emitió; uno con
`--days` caduca pasados esos días. Un token así, olvidado, deja de funcionar por sí solo. Un token
`--never` funciona hasta que se revoca: lista los tokens de vez en cuando con `admin-token list` y
revoca los que nadie use.

**Un token perdido, con otro que sigue válido:** desde cualquier equipo, lístalos, revoca el
perdido y emite uno nuevo:

```sh
latet server --server https://latet.acme.lan admin-token list
latet server --server https://latet.acme.lan admin-token revoke a_3fk7qz2mwd
latet server --server https://latet.acme.lan admin-token issue --label "Marco" --out marco.token
```

Sin `--days`, el token nuevo dura el plazo predeterminado del servidor, recortado al final del
token que lo emite. Un token con `--days` no puede caducar después del token que lo emite: el servidor
lo rechaza, y `admin-token list` muestra cuándo caduca cada token.

**Ningún token disponible**, porque todos se perdieron o caducaron: donde se pueda llegar a los
ajustes del servidor, a su clave maestra y a su base de datos, `latet server init --new-admin-token`
emite uno nuevo (con `--never`, uno de reserva que guardas aparte). En el despliegue de Compose,
vuelve a ejecutar el `docker run` de la inicialización de
[Operación del servidor](../../concepts/server-operations/), terminado en
`server init --new-admin-token`, con el registro de Docker desactivado como antes. Como allí, lo
ejecuta una persona, en su propio terminal, nunca un agente de IA: quien lo ejecuta tiene el nuevo
token, y dentro de un contenedor la protección de Latet contra los agentes no puede ver quién lo
ejecuta. Después revoca los perdidos, si aún no han caducado. Las personas, las claves de API, los
ajustes, las cajas fuertes y el historial quedan igual en ambos casos.

**Un token filtrado**: revócalo de inmediato desde otro token y luego lee lo que hizo:

```sh
latet server --server https://latet.acme.lan log --actor admin:a_3fk7qz2mwd --since 30d
```

Perder la **clave maestra** es peor que perder un token: sella la clave de cada espacio, y sin ella
no se podrá volver a leer ninguna caja fuerte. Guarda una copia separada de las copias de seguridad
de la base de datos, como explica [Operación del servidor](../../concepts/server-operations/).

## Olvidar valores de una clave de API

Cuando una persona pide que sus datos se quiten de la caja fuerte de una clave de API, `keys forget` quita
sus valores de ella, junto con lo que las conversaciones y el historial de la clave guardan de ellos, en
cada instancia en cuanto terminan las llamadas que ya están en curso en ella:

```sh
latet server --server https://latet.acme.lan keys forget "Shop" --file erasure.txt
```

El archivo contiene un valor por línea, en UTF-8; sin `--file`, escribe los valores en tu terminal, y
una línea vacía los termina. Un valor nunca se toma de la línea de comandos, que la shell guarda en su
historial, y el comando rechaza a un agente, como hace `config get`. Pregunta antes de enviarlos, en tu
terminal incluso con `--yes`; un script necesita `--yes`. Cada valor se busca exactamente como está
escrito: enumera todas las formas que deben desaparecer, y también el dominio de empresa de una
dirección, que la caja fuerte guarda como un valor propio. Cada llamada, de 1000 valores como máximo,
es una entrada `key.forgotten` con cuántos valores se pidieron, se olvidaron y no estaban en la caja
fuerte, nunca un valor. Funciona también con una clave desactivada. Una aplicación hace lo mismo con un
token al que has dado `--forget` (`keys add-token "Shop" erasure --forget`), que ningún otro permiso
incluye: consulta [olvidar valores](../../applications/forget/).

## Leer el registro de administración

`latet server log --limit 100000 --json` lee páginas sucesivas, primero las marcas de tiempo más
recientes y después los ID mayores cuando las marcas coinciden. `--limit` acepta como máximo
1.000.000 de entradas. Cada petición solicita como máximo 10.000; una página más corta puede tener
continuación, porque las páginas también tienen un límite de bytes. La salida JSON solo contiene
entradas, una por línea; los avisos se escriben en stderr.

`--since` incluye su límite y `--until` lo excluye. `--actor` selecciona una identidad o un token
de administración; `--action` selecciona una acción o un prefijo literal que termina en `*`.
Las acciones nombran lo que cambió (`person.added`, `code.issued`, `token.added`, `token.revoked`,
`key.rotated`, `key.updated`, `settings.changed`, `shared.changed`, `admin-token.issued`,
`admin-token.revoked`), y las lecturas se registran con el nombre de la llamada (`status.read`,
`log.read`): `--action 'admin-token.*'` selecciona todos los cambios de los tokens de administración.

Dos acciones registran cómo se lee un miembro de una petición que ningún perfil de petición nombra todavía
(`unknown_members`), nunca una carpeta ni el nombre de un miembro. `setting.defaulted` se escribe una sola
vez para el servidor, por `server`, en el primer arranque de la 0.15.0: su objetivo `unknown_members`, el
valor `check` al que pasó cada identidad, y la versión. `setting.loosened` es una aprobación que pasa los
ajustes de una persona o de una clave de API de `refuse` a `check`: el ajuste, el valor, el tipo de su
alcance (`identity` cuando cambian los propios ajustes, `project` cuando un proyecto deja de rechazarlos)
y cuántos proyectos; su autor es la identidad de la que es el cambio, o la persona que lo decidió por ella.
El guardado de los ajustes de una clave de API por parte de la administración sigue siendo
`settings.changed`, como antes.

Los tokens de una clave de API se nombran por su ID, nunca por su secreto ni por su nombre:
`token.added` da el nuevo token y sus permisos, `key.rotated` el token que queda (`token`) y el que
sustituyó (`replaced`), y `key.updated` un cambio como el límite de la caja fuerte de la clave
(`vault_limit`). `apps.unmasked` es una restauración a través de la
[API para aplicaciones](../apps-api/): el token, cuántos textos y cuántos valores restauró, nunca un
texto ni un valor. `--action apps.unmasked` lista quién restauró, y cuándo.
`key.forgotten` y `apps.forgotten` son valores quitados de la caja fuerte de una clave de API, con
`keys forget` y a través de la API para aplicaciones: cuántos valores se pidieron, se olvidaron y no
estaban, y para la API el token, nunca un valor.

Para reanudar, pasa el cursor del registro mostrado a `--cursor` con exactamente los mismos
filtros originales. Usa fechas absolutas o segundos epoch exactos si vas a reanudar más tarde:
un tiempo relativo como `7d` cambia entre ejecuciones. Nunca sustituyas `--until` por una marca
de tiempo redondeada de la salida de texto. Un cursor indica una posición; no es una credencial
ni demuestra que el registro no haya cambiado.

Cada ejecución está limitada a 1000 peticiones de páginas, 128 MiB de cuerpos recibidos, incluidas
las respuestas de los reintentos, 128 MiB de salida UTF-8 producida y 300 segundos de lectura y
procesamiento. El JSON con escapes puede ser mayor que el cuerpo recibido. Las páginas se validan
y preparan antes de imprimirse; una página que no cabe en el presupuesto restante no se imprime
parcialmente. Estas comprobaciones de tiempo no pueden interrumpir una tubería de salida bloqueada del sistema operativo.

Un límite alcanzado, una página no válida o un fallo del servidor termina con un código distinto
de cero, indica cuántas entradas se imprimieron y señala que la lectura está incompleta. Un cursor
de reanudación validado, cuando está disponible, apunta después de la última página impresa.
Si la primera página supera el límite de salida, solicita menos filas. Un rechazo del servidor como
`log_entry_too_large` no salta la entrada afectada. Alcanzar el número de filas solicitado es un
final normal; el aviso indica si queda una continuación.

La primera página fija un límite de inserción: las entradas añadidas después, incluso con fechas
anteriores, no se incluyen en esa lectura. La retención o la eliminación aún pueden quitar entradas
sin leer. El agotamiento solo significa que no había más filas coincidentes visibles en esas
lecturas; no es una exportación inmutable ni una verificación de la cadena. Usa `log verify` por
separado, teniendo presente el límite sobre la eliminación de las últimas entradas descrito arriba.

La sincronización de términos de Latet Enterprise registra cuatro acciones, con nombres de fuentes y recuentos,
nunca un término ni un secreto:

| Acción | Qué registra |
| --- | --- |
| `terms.source.changed` | Se definieron las fuentes de la sincronización de términos (`added`, `removed`, `changed`: sus nombres; `fields`: los campos que cambiaron en cada una, nunca sus valores) |
| `terms.secret.changed` | Se definió o quitó el secreto de una fuente (`saved`, `removed`: los nombres de las fuentes) |
| `terms.removed` | Se quitaron términos que ya no están en su fuente (`count`, `by_type`, `source`; `unreadable` cuando eran los que ninguna clave del servidor abre y ninguna fuente da ya) |
| `terms.sync.asked` | Se pidió una sincronización (`source`: su nombre, o ninguno para todas las fuentes) |

## Métricas de imágenes sin leer

`latet_ocr_unread_total{reason}` cuenta, en cada instancia del servidor, las apariciones de
imágenes que el OCR no ha podido leer al final, tanto cuando se rechaza una petición que solo
se comprueba como cuando se retiene una imagen reescrita. Su única etiqueta
es `reason`, con cinco valores normales:

| Motivo | Significado |
| --- | --- |
| `busy` | La identidad, el lector o el pool no admitió la imagen dentro del límite de espera o de cola. |
| `too_long` | La lectura superó su tiempo permitido. |
| `too_many_tiles` | La imagen necesitaba más mosaicos de los que permite el OCR. |
| `too_wide` | Una línea de texto superó el límite de anchura del reconocedor. |
| `unavailable` | El OCR estaba cargándose, no pudo cargarse o encontró otro fallo de ejecución controlado. |

Una aparición posterior de la misma imagen original vuelve a contar si recibe un fallo
OCR almacenado en caché. El contador mide apariciones de imágenes sin leer, no llamadas
a los modelos, con una excepción: una petición o un turno de WebSocket rechazado antes
de leer ninguna de sus imágenes, porque el reconocimiento de texto no está disponible,
cuenta `unavailable` una vez, lleve las imágenes que lleve. Los reintentos internos,
mosaicos, lotes y reparaciones de lectores sin una imagen enviada no añaden nada. Se
excluyen el OCR que tiene éxito, la retención manual, los fallos de decodificación o de
política de píxeles, el OCR desactivado o no instalado, la repetición exacta de una
imagen pintada o un marcador de Latet y el OCR local de un equipo. Las comprobaciones
finales del mismo trabajo no vuelven a contar. Ninguna etiqueta contiene imágenes, rutas
ni identidades.
