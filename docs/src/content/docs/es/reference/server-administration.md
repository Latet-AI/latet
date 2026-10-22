---
title: Administración del servidor
description: Administra un servidor desde tu equipo y mantén privadas sus credenciales.
---

Ejecuta `latet server` en tu propio terminal. Los comandos que cambian la administración
rechazan a un agente; `config get` y `rules export`, al terminal o a un archivo, también lo
rechazan, porque pueden mostrar valores en claro (los términos de una clave de API, las indicaciones
para el revisor). El estado, las listas de identidades y el registro
de administración no contienen credenciales.

Indica el servidor e introduce el token en la entrada oculta:

```sh
latet server --server https://latet.acme.lan status
```

O usa `--admin-token-file FILE`: un archivo UTF-8 privado que contenga solo el token, o un
registro `{"url": "https://latet.acme.lan", "token": "<administration token>"}`.
Un registro solo se acepta para su servidor. En POSIX usa `chmod 600 FILE` (o `0400` para
la cuenta que ejecuta un contenedor), en una carpeta que otros usuarios no puedan modificar.
En Windows, el archivo y su carpeta deben permitir el acceso solo a tu cuenta, SYSTEM y
Administrators; sigue la corrección `icacls` que muestra el comando.
`--admin-token-file -` lee una línea de la entrada estándar.

En un equipo donde se ejecutan agentes, mantén los tokens de administración fuera de los perfiles
del shell y las variables de entorno de la cuenta: los procesos del mismo usuario pueden leer
el entorno de otro proceso. `LATET_SERVER_ADMIN_TOKEN` es para el almacén de secretos de una CI
o un contenedor, junto con `LATET_SERVER_URL`; nunca se envía a otro servidor.

`keys add`, `keys rotate` y `admin-token issue` aceptan `--out FILE`: el nuevo archivo privado
recibe el secreto y el comando solo muestra su id y el archivo. Nunca pongas códigos, claves
o tokens de administración en un ticket, canal compartido o conversación con un agente.
El código de un solo uso de una persona permite a quien lo use conectar un equipo como esa
persona y abrir su panel; su emisión queda registrada.

Usa `admin-token issue --label "Daniel's laptop" --days 30` para un token con caducidad.
Un token `--never` requiere una etiqueta y permite todo lo que puede hacer un administrador
hasta su revocación: personas y sus códigos, claves API, ajustes compartidos y tokens de
administración. Un token emite otros que terminan como muy tarde con él, así que solo un token
que no caduque puede emitir otro que no caduque; en el equipo del servidor,
`latet server init --new-admin-token --never` crea uno. Guárdalo aparte y úsalo
solo para emitir otros. Una CI que lo tenga concede estos poderes para siempre a quien pueda
cambiar sus tareas; dale un token con `--days` y emítelo de nuevo antes de que caduque.

Las eliminaciones piden confirmación en tu terminal incluso con `--yes`; los scripts necesitan
`--yes`. `config reset` solo sustituye, tras la confirmación, ajustes compartidos que el
servidor no puede abrir. `config get` y `rules export` pueden mostrar valores en claro: mantén
privada su salida. `log verify` muestra la cabecera de la cadena; guarda una copia en otro
lugar y compárala la próxima vez: quitar entradas del final deja intacta la cadena restante.

## Quién administra, y perder un token

**El administrador es quien tenga un token de administración.** No existe una cuenta de
administrador: `latet server init` muestra el primer token a quien inicializa el servidor, y cada
token puede emitir otros. Un token no es una persona: el registro de administración nombra el token
que hizo cada cosa (`admin:a_3fk7…`), y un administrador no necesita un espacio propio en el
servidor, aunque puede añadirse como persona igual que cualquier otra. En la Community Edition
cada token puede hacerlo todo; los roles, como un auditor de solo lectura, están previstos para la
Enterprise Edition.

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

Sin `--days`, el token nuevo dura lo que dura el predeterminado del servidor, recortado al final del
token que lo emite. Un token con `--days` no puede caducar después del token que lo emite: el servidor
lo rechaza, y `admin-token list` muestra cuándo caduca cada token.

**Ningún token disponible**, porque todos se perdieron o caducaron: donde se pueda llegar a los
ajustes del servidor, a su clave maestra y a su base de datos, `latet server init --new-admin-token`
emite uno nuevo (con `--never`, uno de reserva que guardas aparte). En el despliegue de Compose,
vuelve a ejecutar el `docker run` de la inicialización de
[Operación del servidor](../../concepts/server-operations/), terminado en
`server init --new-admin-token`, con el registro de Docker desactivado como antes. Después revoca los
perdidos, si aún no han caducado. Las personas, las claves API, los ajustes, las cajas fuertes y el
historial quedan igual en ambos casos.

**Un token filtrado**: revócalo de inmediato desde otro token y luego lee lo que hizo:

```sh
latet server --server https://latet.acme.lan log --actor admin:a_3fk7qz2mwd --since 30d
```

Perder la **clave maestra** es peor que perder un token: sella la clave de cada espacio, y sin ella
no se podrá volver a leer ninguna caja fuerte. Guarda una copia separada de las copias de seguridad
de la base de datos, como explica [Operación del servidor](../../concepts/server-operations/).

## Leer el registro de administración

`latet server log --limit 100000 --json` lee páginas sucesivas, primero las marcas de tiempo más
recientes y después los id mayores cuando las marcas coinciden. `--limit` acepta como máximo
1.000.000 de entradas. Cada petición solicita como máximo 10.000; una página más corta puede tener
continuación, porque las páginas también tienen un límite de bytes. La salida JSON solo contiene
entradas, una por línea; los avisos se escriben en stderr.

`--since` incluye su límite y `--until` lo excluye. `--actor` selecciona una identidad o un token
de administración; `--action` selecciona una acción o un prefijo literal que termina en `*`.
Las acciones nombran lo que cambió (`person.added`, `code.issued`, `token.revoked`, `key.rotated`,
`settings.changed`, `shared.changed`, `admin-token.issued`, `admin-token.revoked`), y las lecturas se
registran con el nombre de la llamada (`status.read`, `log.read`): `--action 'admin-token.*'`
selecciona todos los cambios de los tokens de administración.
Para reanudar, pasa el cursor del registro mostrado a `--cursor` con exactamente los mismos
filtros originales. Usa fechas absolutas o segundos epoch exactos si vas a reanudar más tarde:
un tiempo relativo como `7d` cambia entre ejecuciones. Nunca sustituyas `--until` por una marca
de tiempo redondeada de la salida de texto. Un cursor indica una posición; no es una credencial
ni demuestra que el registro no haya cambiado.

Cada ejecución está limitada a 1.000 peticiones de páginas, 128 MiB de cuerpos recibidos, incluidas
las respuestas de los reintentos, 128 MiB de salida UTF-8 producida y 300 segundos de lectura y
procesamiento. El JSON con escapes puede ser mayor que el cuerpo recibido. Las páginas se validan
y preparan antes de imprimirse; una página que no cabe en el presupuesto restante no se imprime
parcialmente. Estos controles de tiempo no pueden interrumpir una tubería de salida bloqueada del sistema operativo.

Un límite alcanzado, una página no válida o un fallo del servidor termina con un código distinto
de cero, indica cuántas entradas se imprimieron y señala que la lectura está incompleta. Un cursor
de reanudación validado, cuando está disponible, apunta después de la última página impresa.
Si la primera página supera el límite de salida, solicita menos filas. Un rechazo como
`log_entry_too_large` no salta la entrada afectada. Alcanzar el número de filas solicitado es un
final normal; el aviso indica si queda una continuación.

La primera página fija un límite de inserción: las entradas añadidas después, incluso con fechas
anteriores, no se incluyen en esa lectura. La retención o la eliminación aún pueden quitar entradas
sin leer. El agotamiento solo significa que no había más filas coincidentes visibles en esas
lecturas; no es una exportación inmutable ni una verificación de la cadena. Usa `log verify` por
separado, teniendo presente el límite sobre la eliminación de las últimas entradas descrito arriba.

## Métricas de imágenes sin leer

`latet_ocr_unread_total{reason}` cuenta las apariciones de imágenes cuyo resultado
OCR final es sin leer en cada instancia del servidor, tanto cuando se rechaza una
petición comprobada como cuando se retiene una imagen reescrita. Su única etiqueta
es `reason`, con cinco valores normales:

| Motivo | Significado |
| --- | --- |
| `busy` | La identidad, el lector o el pool no admitió la imagen dentro del límite de espera o de cola. |
| `too_long` | La lectura superó su tiempo permitido. |
| `too_many_tiles` | La imagen necesitaba más mosaicos de los que permite el OCR. |
| `too_wide` | Una línea de texto superó el límite de anchura del reconocedor. |
| `unavailable` | El OCR estaba cargándose, no pudo cargarse o encontró otro fallo de ejecución controlado. |

Una aparición posterior de la misma imagen original vuelve a contar si recibe un
fallo OCR almacenado en caché. El contador mide apariciones de imágenes sin leer,
no llamadas a los modelos. Los reintentos internos, mosaicos, lotes y reparaciones
de lectores sin una imagen enviada no añaden nada. Se excluyen el OCR correcto,
la retención manual, los fallos de decodificación o de política de píxeles, el OCR
desactivado o no instalado, la repetición exacta de una imagen pintada o un marcador
de Latet y el OCR local de un ordenador. Las comprobaciones finales del mismo trabajo
no vuelven a contar. Ninguna etiqueta contiene imágenes, rutas ni identidades.
