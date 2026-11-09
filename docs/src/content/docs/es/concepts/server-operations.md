---
title: Operación del servidor
description: Gestionar almacenamiento, red, claves, límites y observabilidad de un servidor de Latet.
---

Estos requisitos se aplican a cada instancia de un servidor de Latet configurado. Mantén
la misma dirección HTTPS pública, base de datos PostgreSQL, Valkey y clave maestra entre
instancias. [Latet en un servidor](../server/) explica qué datos llegan al servidor y
cuáles siguen siendo legibles en sus almacenes.

## Configuración y claves

`server.yaml` contiene los ajustes de la instancia. `--config` elige el archivo para
los comandos del servidor; `LATET_SERVER_CONFIG` lo elige mediante el entorno. Las
variables `LATET_SERVER_*` correspondientes prevalecen sobre el archivo. Los ajustes
de la instancia son distintos de los ajustes compartidos y de los ajustes y reglas propios de cada
identidad, guardados en PostgreSQL.
La [tabla generada de ajustes del servidor](../../../reference/configuration/#server-settings), en
inglés, enumera cada ajuste de la instancia, su valor predeterminado y su función.

Proporciona la clave maestra mediante `master_key_file` o la entrada de entorno compatible
para esta clave. Prefiere un archivo secreto protegido; no la pongas en un repositorio
ni en una línea de comandos. Todas las instancias necesitan la misma clave. Guarda una
copia separada con acceso restringido: sustituir el archivo por una clave aleatoria
nueva no rota los datos cifrados y vuelve ilegibles los datos existentes. Una entrada de la
caja fuerte que no se abre (sellada con otra clave, o cambiada en la base de datos) se deja
fuera: el registro da su id, nunca su valor, y el enmascaramiento sigue con las demás entradas.

Usa `database_password_file`, `valkey_password_file`, `metrics_token_file` y
`reviewer_key_file` para sus respectivos secretos. La clave del revisor se lee una vez
al arrancar y se envía solo al revisor compartido elegido por los administradores. Debe
ser la credencial de ese revisor, nunca una clave maestra ni un token de administración.
El servidor no busca la clave del revisor en el entorno, el llavero ni una clave local
guardada de una persona.

## Servicios del sistema

Instala el paquete servidor en una ubicación de confianza para todo el sistema, fuera
de las carpetas personales. Prepara `server.yaml` con las URL, rutas de archivos secretos
y configuración TLS descritas arriba; el servicio debe poder leer esos archivos protegidos.
Crea antes la clave maestra e inicializa desde tu terminal privado de administrador:
el primer token de administración se imprime una vez. No guardes la salida en registros
compartidos.

En Linux van primero varias cosas, como root. Instala Latet con el extra `server` para toda la
máquina: las carpetas de uv para herramientas, comandos y Python, todas fuera de las carpetas
personales. Crea el usuario y el grupo `latet` con los que se ejecuta el servicio, y después la
carpeta que guarda los ajustes y los secretos, con la clave maestra y la contraseña de la base de
datos dentro:

```sh
UV_TOOL_DIR=/opt/latet/tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/latet/python \
  uv tool install --python ">=3.12,<3.15" "latet[server] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
useradd --system --user-group --no-create-home --shell /usr/sbin/nologin latet
install -d -m 0750 -o root -g latet /etc/latet
(umask 077 && openssl rand -base64 32 > /etc/latet/master.key)
(umask 077 && openssl rand -hex 32 > /etc/latet/db.password)   # or the password your database administrator gives
```

Pon la contraseña de Valkey en `/etc/latet/valkey.password`, y el certificado TLS y su clave en
`/etc/latet/server.crt` y `/etc/latet/server.key`. Después escribe `/etc/latet/server.yaml`; uno
mínimo para un servicio en el host:

```yaml
url: https://latet.acme.lan
database_url: postgresql://latet@db.acme.lan:5432/latet?sslmode=verify-full&sslrootcert=system
database_password_file: /etc/latet/db.password
valkey_url: rediss://valkey.acme.lan:6379/0
valkey_password_file: /etc/latet/valkey.password
master_key_file: /etc/latet/master.key
certificate: /etc/latet/server.crt
key: /etc/latet/server.key
```

Da `server.yaml` y cada archivo secreto que nombra, `key` incluido, a root y al grupo `latet`,
legibles solo por ellos (`0640`, o `0440`):

```sh
cd /etc/latet
chown root:latet server.yaml master.key db.password valkey.password server.key
chmod 0640 server.yaml master.key db.password valkey.password server.key
```

Añade `ner` y `ocr` al extra para la detección de nombres y el reconocimiento de texto. La
instalación del servicio dice cuál de estas cosas falta: una instalación bajo una carpeta
personal, ningún usuario o grupo `latet`, o un archivo con otro propietario u otro modo.

La configuración predeterminada es `/etc/latet/server.yaml`, la unidad es `latet-server` y
el directorio de datos es `/var/lib/latet`:

```sh
latet server init --config /etc/latet/server.yaml
latet server service install --config /etc/latet/server.yaml --print
latet server service install --config /etc/latet/server.yaml
systemctl start latet-server
```

En Windows la configuración predeterminada es `C:/ProgramData/Latet/server.yaml`.
El servicio se ejecuta como `NT SERVICE\LatetServer`, con ACL protegidas en los directorios.
Su registro es `C:/ProgramData/Latet/logs/server.log`, con rotación en cinco archivos
de 10 MiB:

```powershell
latet server init --config C:/ProgramData/Latet/server.yaml
latet server service install --config C:/ProgramData/Latet/server.yaml --print
latet server service install --config C:/ProgramData/Latet/server.yaml
Start-Service LatetServer
```

`--print` muestra el plan sin instalar. La instalación requiere root en Linux o privilegios
de administrador en Windows y habilita el servicio al arrancar; inícialo explícitamente
cuando esté listo. Las invocaciones de agentes se rechazan incluso con `--print` o
`--yes`. En tu propio terminal, la instalación pregunta antes de cambiar nada, con `--yes` o sin él;
`--yes` permite que un script tuyo, sin terminal, instale sin la pregunta. La instalación en un
contenedor se rechaza. Estos comandos describen el procedimiento, no un servicio ya instalado en tu equipo.

## El perfil servidor de Compose

Usa `compose.yaml` y `deploy/Caddyfile` del repositorio. Prepara un directorio privado
`secrets/` en el host: en POSIX pertenece al administrador y tiene modo `0700`;
los archivos montados individualmente tienen modo `0444` para que el usuario de cada
contenedor pueda leerlos. En Windows limita las ACL de archivos y directorios a los
administradores y a la cuenta del servicio Docker que necesita acceso. Los secretos
montados por Compose no reasignan propietarios ni modos; cambiar todos los archivos a
`0600` puede hacerlos ilegibles dentro del contenedor.

Crea `master.key` (32 bytes aleatorios en base64), `db-admin.password`, `db.password`,
`valkey.password` y `metrics.token` en ese directorio antes de inicializar. Usa valores
aleatorios robustos; `db.password` y `valkey.password` deben ser hexadecimales para los
comandos de arranque incluidos (`openssl rand -hex 32`). No sustituyas una clave maestra
existente para simplificar la configuración. El archivo opcional `reviewer.key` requiere
descomentar juntos su ajuste, montaje y definición de secreto. Estos secretos solo proceden
de archivos, sin alternativa en el entorno. Proporciona los archivos TLS en `certs/` y
protege la clave privada.

Las instancias se conectan a PostgreSQL con el rol `latet`, que no es superusuario. En un
volumen de datos nuevo, `deploy/postgres-init.sh` lo crea con la contraseña de `db.password`,
como propietario de la base de datos `latet`. El superusuario de PostgreSQL, `postgres`,
tiene `db-admin.password`, que ninguna instancia recibe: una instancia comprometida no puede
ejecutar un programa en el contenedor de la base de datos (`COPY … PROGRAM` necesita un
superusuario). El script solo se ejecuta mientras el volumen es nuevo y no cambia ninguna
base de datos existente. Un despliegue cuyo volumen se creó antes mantiene `latet` como
superusuario de PostgreSQL, un atributo que PostgreSQL nunca deja perder a ese rol
(`ALTER ROLE latet NOSUPERUSER` se rechaza): para pasarlo a la nueva configuración, exporta la
base de datos con `pg_dump --no-owner --no-privileges`, arranca PostgreSQL en un volumen nuevo
y restaura la exportación como `latet`.

Escribe la dirección del servidor en un archivo llamado `.env` junto a `compose.yaml`, que Compose lee
en cada comando, para que una actualización ejecutada más tarde desde otro terminal la conserve (no
contiene ningún secreto):

```sh
printf 'LATET_SERVER_URL=https://latet.acme.lan\nLATET_SERVER_HOST=latet.acme.lan\n' > .env
```

Este ejemplo para shell POSIX elige el proyecto `latet`, con la red interna
`latet_latet-back`. La inicialización usa `--log-driver none` de Docker porque imprime
el token de administración; mantén privado el terminal y transfiere el token a tu almacén
de secretos. Comprueba que haya terminado correctamente antes de iniciar las instancias:

```sh
# --wait returns only once PostgreSQL and Valkey are healthy (at most 120 s): go on only if it succeeds
docker compose -p latet --profile server up -d --wait --wait-timeout 120 postgres valkey
docker run --rm --log-driver none --network latet_latet-back \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  --mount "type=bind,source=$PWD/secrets/master.key,target=/run/secrets/latet-master-key,readonly" \
  --mount "type=bind,source=$PWD/secrets/db.password,target=/run/secrets/latet-db-password,readonly" \
  --mount "type=bind,source=$PWD/secrets/valkey.password,target=/run/secrets/latet-valkey-password,readonly" \
  --mount "type=bind,source=$PWD/secrets/metrics.token,target=/run/secrets/latet-metrics-token,readonly" \
  --env LATET_SERVER_URL=https://latet.acme.lan \
  --env LATET_SERVER_DATABASE_URL=postgresql://latet@postgres:5432/latet \
  --env LATET_SERVER_DATABASE_PASSWORD_FILE=/run/secrets/latet-db-password \
  --env LATET_SERVER_DATABASE_PRIVATE_NETWORK=1 \
  --env LATET_SERVER_VALKEY_URL=redis://valkey:6379/0 \
  --env LATET_SERVER_VALKEY_PASSWORD_FILE=/run/secrets/latet-valkey-password \
  --env LATET_SERVER_VALKEY_PRIVATE_NETWORK=1 \
  --env LATET_MASTER_KEY_FILE=/run/secrets/latet-master-key \
  --env LATET_SERVER_METRICS_TOKEN_FILE=/run/secrets/latet-metrics-token \
  ghcr.io/latet-ai/latet:latest server init
```

Cuando la inicialización termine correctamente, inicia las instancias y Caddy:

```sh
docker compose -p latet --profile server up -d latet-server-1 latet-server-2 caddy
```

En el perfil servidor, solo Caddy publica un puerto: `443`. El servicio local separado
`latet` sigue en el archivo Compose; la lista explícita anterior no lo inicia.
La red frontal es `172.28.0.0/24`, con asignación automática de `172.28.0.128/25`;
la dirección fija de Caddy, `172.28.0.2`, queda fuera de ese rango y es el único proxy
de confianza. La red posterior es interna.

Caddy usa los archivos TLS proporcionados, un límite de petición de **32 MiB**, envío
inmediato de los flujos y cambios a WebSocket. Mantén desactivados los registros de acceso
y conserva la exclusión global `log default { exclude http.log.error }`: de lo contrario,
los errores del proxy pueden registrar URI y cabeceras de autenticación personalizadas.
Caddy no incluye un limitador de frecuencia; los límites por cliente corresponden a un
proxy externo o a un WAF.

Los contenedores servidor usan registros JSON-file acotados y no bloqueantes.
Las instancias se ejecutan con el usuario sin privilegios de la imagen, uid y gid `10001`,
nunca como root. Tienen raíz de solo lectura, capacidades eliminadas y `/tmp` temporal;
este ejemplo no impone un límite de memoria a las instancias de producción. Dimensiona
espacios, modelos y OCR concurrente sin copiar los límites de las pruebas de humo.
Valkey tiene memoria acotada con `noeviction`. La imagen incluye los avisos de terceros.

## PostgreSQL y Valkey

Para PostgreSQL fuera del equipo de la instancia, usa TLS verificado en la URL de
conexión, por ejemplo `?sslmode=verify-full&sslrootcert=system`, o indica el archivo del
certificado de confianza. Aquí `verify-full` sin `sslrootcert` no basta. Las URL sin
host y las conexiones por nombre de servicio se rechazan; las variables `PG*` no completan
los ajustes de conexión que faltan. Codifica los caracteres reservados de las contraseñas
en las URL con codificación porcentual, o usa el archivo de contraseña. El tiempo de
espera de conexión no incluye la resolución DNS.

Usa Valkey 7.2 o posterior, o Redis 7, como un único servidor lógico; se permiten réplicas,
pero no Cluster. Sus scripts operan entre varios prefijos de clave. Establece
`maxmemory-policy` en `noeviction`, reserva memoria para los espacios previstos y vigila
la capacidad. Perder estado de coordinación no equivale a descartar una caché de aplicación inocua.

Exige autenticación y TLS verificado (`rediss://`) cuando Valkey esté fuera del equipo
de la instancia. Las excepciones `database_private_network`, `valkey_private_network`
y `valkey_unauthenticated` son decisiones explícitas de instalación para servicios a
los que solo pueden acceder las instancias previstas; no las uses para ocultar un error
de certificado o autenticación en una red compartida ordinaria.

Antes de usar esta receta, comprueba que el usuario de Valkey `latet` no exista. La receta
crea un usuario nuevo; si ya existe, detente y pide al operador que revise su ACL,
contraseñas y selectores. Aplicar el comando a un usuario existente añade reglas a su
configuración; esta receta no la restablece ni realiza la rotación de sus contraseñas.

Asigna a la aplicación un usuario Valkey propio con esta ACL. Utiliza el espacio de
nombres predeterminado `latet:`; sustituye el marcador `password` mediante una conexión
administrativa protegida. Es un comando de Valkey, no de la shell: mantén el secreto real
fuera de los argumentos de la shell, el historial y los repositorios.

```text
ACL SETUSER latet on >password ~latet:* resetchannels &latet:identity:* &latet:approvals:* &latet:feed:* &latet:shared &latet:identities &latet:signins &latet:beat +@all -@dangerous -monitor -config -keys -debug
```

Conserva los patrones de suscripción literalmente: las suscripciones por patrón necesitan
esos permisos exactos. Los scripts Lua y `SCAN` siguen disponibles para la coordinación
y la limpieza selectiva; `MONITOR`, `CONFIG`, `KEYS` y `DEBUG` se deniegan. Un test automático
aislado comprueba esta receta sustituyendo solo usuario, contraseña y espacio de nombres;
no examina ni configura tu servicio desplegado.

Ejecuta las migraciones como propietario del esquema. La primera crea el esquema y
necesita `CREATE` en la base de datos; las siguientes no necesitan ese permiso de creación.
El bloqueo de migración espera como máximo 600 segundos. Un rol de aplicación sin
`DELETE` sobre `audit` y `audit_anchor` no puede purgar el registro de administración:
concede esos permisos o conserva el rol del propietario para las instancias. Guarda
una copia recuperable antes de una actualización. Un pooler de conexiones debe conservar
las `options` de arranque requeridas por PostgreSQL.

### Dimensionar Valkey

Los presupuestos en bytes por identidad son 32 MiB para textos tal como salieron,
64 MiB para la cola de aprobación y 16 MiB para eventos del flujo guardados por referencia.
La suma, **112 MiB, no es un límite de memoria total**. Otras familias se limitan por
número de entradas, por lo que también importa el tamaño de sus registros. Las conversaciones
admiten 1.000.000 de campos y textos aprobados en un máximo de 10.000 conversaciones.
Las marcas de bloques comprobados y registrados admiten 200.000 entradas cada una;
los textos inversos y las asociaciones de respuestas, 50.000 cada uno; las sesiones
AWS, 200, y las sesiones del panel, 10 por persona.
Los textos inversos son salidas del modelo guardadas para reconocerlas cuando vuelven
en una solicitud posterior.

Son ejemplos de planificación, no mediciones. Se suponen 256 bytes asignados por entrada
de conversación y 192 por marca de bloque. Los otros tamaños indican texto en claro
codificado: las estimaciones añaden los 30 bytes de la estructura de cifrado, el redondeo
de la asignación y 256 bytes estimados para clave e índice. Los tres presupuestos en
bytes ya cuentan estos costes.

| Familia | Ejemplo de ocupación moderada | Cantidades saturadas, mismos tamaños de registro |
| --- | --- | --- |
| Textos / cola / eventos | 8 / 8 / 1 MiB | 32 / 64 / 16 MiB |
| Entradas de conversación | 20.000 de 256 bytes | 1.000.000 de 256 bytes |
| Marcas comprobadas / registradas | 10.000 cada una de 192 bytes | 200.000 cada una de 192 bytes |
| Textos inversos | 5.000 de 2 KiB | 50.000 de 2 KiB |
| Asociaciones de respuestas | 5.000 de 64 bytes | 50.000 de 64 bytes |
| Sesiones AWS | 10 de 2 KiB | 200 de 2 KiB |
| Sesiones del panel | 2 de 128 bytes | 10 de 128 bytes |
| **Subtotal modelado por identidad** | **40,76 MiB** | **581,75 MiB** |

Con una reserva ilustrativa del 50% resultan 61,13 y 872,62 MiB por identidad, o unos
5,97 y 85,22 GiB para 100 identidades. Presupuesta por separado la memoria base de Valkey,
las conexiones, la replicación/persistencia y las estructuras restantes o fragmentación
que superen esa reserva. Cuenta las identidades cuyos datos siguen retenidos, no solo
las que hacen solicitudes ahora. Las cachés de la aplicación y los modelos OCR requieren
RAM separada.

El ejemplo saturado **no es el peor caso absoluto**: los registros pequeños siguen siendo
una suposición. La cola de escrituras pendientes de 4 MiB no limita a 4 MiB la familia
persistente de textos inversos. Una sucesión de registros cercanos a 4 MiB puede aproximarse
a 195 GiB solo para 50.000 registros inversos, antes de otras familias. Es una estimación
amplia derivada del código, no una carga medida ni una cuota total. Una clave de conversación
o una ruta de carpeta larga también puede superar el tamaño supuesto. Conserva `noeviction`,
vigila los tamaños y la ocupación reales y dimensiona para la carga admitida: los recuentos
por sí solos no garantizan un pequeño techo de memoria por identidad.

Latet 0.11 añade tres tipos de claves, cada uno acotado, ninguno con un texto, un valor o un valor
ficticio:

- **Lo que el proveedor dio a una conversación** (firmas del razonamiento, razonamiento cifrado,
  compactaciones), como hashes con clave, para que el turno siguiente pase en cualquier instancia: como
  máximo 20.000 entradas y 4 MiB por identidad, y 256 MiB para todo el servidor, guardados una semana
  después de su último uso. Una escritura que Valkey rechaza (un límite, o `noeviction` sin memoria) no
  registra nada: ese elemento pasa entonces por la regla integrada para los campos cifrados del
  proveedor donde se aplica, o se rechaza, nunca con una respuesta `503`.
- **Las claves de lanzamiento** de los agentes que Latet lanzó en equipos conectados: un registro
  sellado de 16 KiB como máximo por cada lanzamiento abierto en ese momento, como máximo 64 a la vez
  por identidad.
- **Las excepciones de petición usadas con el historial desactivado**: una fila por instancia e
  identidad, solo con ids y números, actualizada cuando la instancia revisa sus espacios y borrada 30
  segundos después de que la instancia se detiene. Valkey tal como lo entrega el perfil de Compose no
  guarda nada en disco (`save ""`, `appendonly no`); un operador que active su persistencia escribe
  estos recuentos en disco.

## HTTPS, proxies y capacidad

Termina TLS en la instancia o en el equilibrador de carga. Establece `forwarded_allow_ips`
en los proxies realmente fiables, nunca `*`; no se confía en las cabeceras reenviadas
desde otras direcciones. Mantén los relojes sincronizados, con una diferencia máxima
de 60 segundos. Restringe por separado el acceso directo a los proveedores si el servidor
es el punto de control obligatorio de la empresa.

El límite predeterminado del cuerpo de una solicitud es de 32 MiB. `requests_in_flight`
vale 16 por identidad en una instancia, con cuotas separadas para solicitudes a destinos,
al panel, al bridge y a la API para aplicaciones. Las esperas largas y los flujos tienen sus propios límites. Un
rechazo por capacidad exige revisar carga y límites, no desactivar la protección.
Al apagarse, una instancia tiene 30 segundos para terminar; los flujos largos pueden
cortarse, así que los clientes deben gestionar la desconexión.

Una instancia hace la mayor parte de su trabajo en un solo núcleo del procesador. Una máquina con más
núcleos sirve más con varios workers en Linux ([más abajo](#varias-instancias-en-una-máquina)), y un
servidor sirve más con más instancias detrás del equilibrador de carga.
[Velocidad](../../applications/#velocidad) da lo que una instancia y varios workers enmascararon a
través de la API para aplicaciones en el equipo de medida.

Latet 0.11 lee cada parte de cada petición que reenvía
([Pasar a la 0.11](../../getting-started/upgrading-to-0-11/)), y eso cuesta tiempo en cada una.
Medido con una instancia en Windows en ese equipo, como mediana, frente a la misma versión sin esa
lectura: una petición de chat pequeña tardó 6,99 ms en lugar de 5,95, la primera petición de una
clave nueva 104 ms en lugar de 97, un cuerpo JSON de 1 MiB 391 ms en lugar de 373, una petición de
4.000 huecos 1.088 ms en lugar de 779, y una petición de 500 textos ya enmascarados y uno nuevo
183 ms en lugar de 158.

El OCR usa lectores limitados, cada uno con sus modelos en memoria. `ocr_readers` vale
2 por defecto y no puede superar los procesadores disponibles para la instancia (con varios workers, la parte de
un worker: los procesadores de la máquina divididos entre los workers).
`ocr_read_seconds` vale 30; `ocr_wait_seconds` vale 35 y no puede ser menor que el tiempo
de lectura. Aumentar los lectores consume memoria además de CPU. Una imagen no leída
se retiene o se rechaza su solicitud verificada, según la ruta protegida; aumentar los
tiempos de espera no mejora la precisión del reconocimiento.

El espacio de una clave de API guarda en memoria los valores que la clave usó por última vez
(`key_working_values`, 5.000 por defecto, unos 10 MiB por clave), las huellas de las palabras de
toda su caja fuerte (unos 15 MB por un millón de valores) y, para siempre, sus secretos cortos más
recientes que ninguna palabra de un texto encuentra cuando otras letras están pegadas a ellos
(`key_held_secrets`, 1.000 por defecto, unos 23 KB cada uno, la mayor parte en el detector que los
encuentra). Cada instancia y cada worker que sirve a la clave guarda todo eso. Mientras construye
el índice de las huellas de una caja fuerte, una instancia guarda más por un momento: 49 MB más para
1.000.000 de valores, durante unos 1,4 segundos, dentro de los 64 MB que reserva antes, y dos
construcciones a la vez como mucho. Sube `key_held_secrets` para una clave que ve muchos secretos
cortos, como un token de sesión en cada línea de registro: cada texto que la clave enmascara o
restaura se recorre para buscar cada uno de los que se guardan, así que sus llamadas también se
vuelven un poco más lentas (restaurar es alrededor de una décima parte más lento con 1.000
guardados, medido con Latet 0.10.0)
([la caja fuerte de una clave](../../applications/#la-caja-fuerte-de-una-clave)).

El espacio de una persona construido cuando su caja fuerte tiene 35.000 valores o menos la
guarda entera en memoria (unos 1,6 KB por valor en cada instancia que sirve a la persona, contados en
la memoria del espacio): los textos de la persona se enmascaran sin pedir sus valores a
PostgreSQL, más o menos el doble de rápido que buscándolos. Cada petición pregunta igualmente
primero a PostgreSQL qué han cambiado las otras instancias, así que un valor guardado u
olvidado en una instancia lo ve la siguiente petición en cualquier otra. Por encima de 35.000
valores, y en un espacio construido por encima hasta que se reconstruye (aunque se olviden
valores), el espacio de una persona funciona como el de una clave de API: guarda en memoria los
valores que la persona usó por última vez (`key_working_values`) y, para siempre, cada valor
que ninguna palabra de un texto encuentra, sea cual sea su antigüedad, y busca cualquier otro
valor en PostgreSQL cuando un texto lo necesita, así que ninguna instancia guarda entera una
caja fuerte grande. Una caja fuerte guardada por una versión anterior de Latet se sirve desde
la memoria, como antes, hasta que sus palabras se crean, una sola vez, en segundo plano en
una instancia; el espacio construido después trabaja con PostgreSQL.

Medido con Latet 0.11 en el equipo de [velocidad](../../applications/#velocidad), en el motor (el
enmascaramiento de la pasarela solo, no a través de HTTP), una línea cada vez, en turnos de 500 líneas
alternados con la caja fuerte de Latet 0.10.0 en el mismo proceso, cinco cada uno (medianas de los turnos): la
caja fuerte de una persona de 20.000 valores, guardada entera, enmascaró 851 líneas por segundo frente a 558 de
la 0.10.0, 1,52 veces donde esta versión se proponía llegar al doble; una de 5.000 valores 885 frente a 1.094,
0,81 veces, porque la lectura del contador de cambios hecha antes de cada línea, de alrededor de medio
milisegundo allí, es una parte grande del tiempo de una línea; una de 200.000 valores, sobre su almacén, 385
frente a 59. Con 200.000 valores, el espacio de una persona ocupaba 24 MB más tras sus líneas, donde la caja
fuerte de la 0.10.0 ocupaba 495 MB.

El espacio de una persona guarda cada valor que ninguna palabra de un texto encuentra cuando
otras letras están pegadas a él, diga lo que diga `key_held_secrets` (unos 23 KB cada uno en
cada instancia). Una petición cuyos textos pasan los límites de una búsqueda, como un archivo
de bloqueo o un registro largo de hashes, nunca se rechaza: el espacio de una persona que
guarda entera su caja fuerte la lee de la memoria; uno más grande lee una vez su caja fuerte
entera para ella, comparte esa lectura con las peticiones que la necesitan al mismo tiempo y
la cuenta en la memoria del espacio mientras dura (con 200.000 valores, medido con Latet 0.11,
las primeras 500 líneas por encima de un límite tardaron unos 10 segundos, lectura incluida, y la
lectura ocupaba unos 380 MB en la instancia o el worker que la hizo, más o menos lo que guardaba la
caja fuerte de la 0.10.0); el espacio de una clave de API busca las palabras de sus textos como lo
hacía Latet 0.10.2.

### Conexiones, límites y confianza

- **Las credenciales rechazadas** cuentan para la dirección de la que llegan: una dirección IPv4 o, en IPv6, su
  /64 y, en conjunto, su /48. Una credencial reconocida como válida en la última hora, y un enlace de acceso emitido
  por el servidor, no cuentan para ese límite, así que los intentos fallidos de un vecino tras el mismo NAT solo frenan las
  credenciales no reconocidas como válidas recientemente, mientras duren los intentos; un proxy o un cortafuegos
  delante que limite a cada cliente lo evita. Mientras los hilos que comprueban credenciales están ocupados, los
  registros de equipos y las credenciales nuevas para todas las instancias reciben 503 con `Retry-After`, y un
  enlace de acceso al panel se envía a `?login=busy`.
- **El token y la clave de un equipo:** el token que guardan los agentes de un equipo abre el bridge del plugin,
  el proxy del espacio y las dos llamadas del equipo al iniciar un agente (un destino añadido para una
  dirección, lo que no reduce nada, y el valor ficticio del nombre de usuario), dice de quién es y se revoca
  a sí mismo; un enlace de acceso al panel y un cambio de la
  identidad de la persona piden también la clave del equipo (cabecera `x-latet-computer-key`), que se queda en
  el almacén de credenciales de ese equipo. Una clave ausente o incorrecta se rechaza (403
  `computer_key_required`) y cuenta para su dirección como una credencial rechazada.
- **Una identidad** tiene como máximo `requests_in_flight` peticiones en curso en una instancia (16 por defecto);
  por encima, 429 con `Retry-After`. Un cuerpo se lee hasta `max_request_mb` (32 MiB por defecto), una llamada de la API para
  aplicaciones hasta `apps_request_mb` (8 MiB); un cuerpo JSON
  cerca de ese límite retiene el bloqueo del intérprete de uno a tres segundos dondequiera que se analice, así que
  mantén el límite tan bajo como permitan tus agentes y ejecuta varias instancias.
- **PostgreSQL:** cada instancia mantiene hasta `database_pool` conexiones (10 por defecto) para el trabajo de
  todas las identidades, y cada worker es una instancia; el `max_connections` de PostgreSQL debe cubrir el pool
  de cada instancia y los comandos de los administradores. Mientras construye el índice de una caja fuerte, la
  de una clave de API o la de una persona, una instancia abre también hasta dos conexiones propias, que se
  cierran al terminar la construcción: cuéntalas también en `max_connections`. Si no queda ninguna libre, la
  construcción usa una conexión del pool. Una llamada a la API para aplicaciones que conserva valores nuevos
  ocupa una hasta que termina, como mucho tres décimas partes del pool (tres de diez); por encima, las llamadas
  conservan cada valor nuevo enseguida, más despacio. Sube `database_pool`, y con él `max_connections`, para una
  instancia que enmascara para muchas claves nuevas a la vez.
- **Valkey:** cada instancia, y también cada worker, mantiene hasta `valkey_connections` conexiones (100 por
  defecto), y por encima una llamada espera cinco segundos como máximo. El `maxclients` de Valkey debe cubrir las
  conexiones de todas las instancias. La conexión a Valkey, su TLS y su primera respuesta comparten un único
  límite de cinco segundos.
- **Memoria:** cada instancia, y también cada worker, da a los espacios que guarda 2 GiB, pesados según los
  recuentos que llevan sus partes en lugar de leídos del proceso. Un espacio ocupa como mucho la mitad, y sus
  detectores hasta una octava parte de esa mitad más (128 MiB), que un espacio ocupado conserva mientras sus
  peticiones están en curso: varios espacios ocupados pueden superar los 2 GiB en eso cada uno. El propio
  proceso, los modelos de reconocimiento y los índices en construcción van aparte. Un worker se vuelve a
  arrancar cuando termina o se queda en silencio, nunca por su memoria: planifica la memoria de la máquina para
  N workers ([varias instancias en una máquina](#varias-instancias-en-una-máquina)).
- **Los certificados de los proveedores:** una instancia confía en el almacén de certificados del sistema
  operativo; en Linux, en las rutas predeterminadas de OpenSSL, que `SSL_CERT_FILE` y `SSL_CERT_DIR` cambian para
  todo el proceso. Cámbialas solo para añadir la autoridad de tu empresa.
- **`SSLKEYLOGFILE`** no debe definirse para una instancia: las conexiones TLS que la leen, entre ellas la de
  Valkey, escriben sus claves de sesión en ese archivo, y quien lo lee lee ese tráfico. Una instancia arrancada con
  ella lo indica en su registro.

## Actualizar y añadir instancias

De la 0.10 a la 0.11, lee primero [Pasar a la 0.11](../../getting-started/upgrading-to-0-11/): la 0.11
rechaza las partes de las peticiones que no lee, en un servidor igual que en un equipo, y
`latet doctor` enumera lo que se detendrá.

La 0.11 también cambia las tablas, así que el servidor se detiene para su migración:

1. Haz la copia de la base de datos que se describe más abajo, y ejecuta
   `latet server migrate --check` con la 0.11: dice que las tablas cambian.
2. Detén cada instancia, cada worker de cada máquina. Un servidor que sigue en la 0.10 responde a
   `latet server status` solo con la instancia que ha respondido, porque la 0.10 no informa de sus
   instancias: la comprobación de que no queda ninguna es la del paso siguiente.
3. Espera 30 segundos y luego ejecuta `latet server migrate` una vez, con la 0.11: una instancia de
   la 0.10 deja su clave en Valkey al detenerse, y cuenta como viva hasta que la clave caduca, 30
   segundos después de su último latido. Mientras una instancia de la 0.10 o de la 0.11 está viva, no
   migra nada, dice cuántas están vivas y de qué versión (`1 instance of the server is alive (1 of
   Latet 0.10 or older): nothing was migrated`) y termina con 2: detenlas y vuelve a ejecutarlo. Con
   una migración pendiente, ni él ni `latet server init` migran nada tampoco mientras Valkey no
   responde. La migración añade a cada fila de palabra de cada caja fuerte un sitio vacío para su
   huella y para el número del cambio que la escribió por última vez, con sus índices: en una base de
   datos con cajas fuertes grandes tarda más que las migraciones anteriores, y las huellas mismas se
   calculan después, en segundo plano (más abajo). Una vez aplicadas las migraciones, vuelve a
   preguntar a Valkey, y cuando una instancia se ha puesto viva entretanto lo dice tras lo que ha
   aplicado y termina con 1 (`The tables are migrated, and 1 instance of the server is alive (1 of
   Latet 0.10 or older): stop it, then start it with this version of Latet`).
4. Arranca cada instancia con la 0.11. Entre la parada y este arranque no se responde ninguna
   llamada.

Después, en segundo plano: las huellas de cada caja fuerte guardada por la 0.10 las calcula la
primera instancia que construye su espacio, una vez, una instancia cada vez, y cada instancia
construye su propio índice de esas huellas, para la caja fuerte de una persona como para la de una
clave de API. Hasta entonces, las peticiones y las llamadas de esa identidad se responden buscando
sus palabras en PostgreSQL, como hacía la 0.10, más despacio, y ninguna se rechaza por ello. Una
instancia cuyo índice no puede construirse (las huellas aún no todas calculadas, PostgreSQL o Valkey
que no responden, una actualización del índice que falló) deja esa identidad en la búsqueda en
PostgreSQL y vuelve a construir el índice: enseguida cuando termina el cálculo de las huellas, en esa
instancia o en otra, y si no 10, 20 y 30 segundos después de cada intento y luego una vez por minuto,
dos construcciones a la vez en cada instancia, la siguiente en cuanto termina una. El registro dice
una vez cuando el índice de un espacio deja de usarse (`the vault index of <id> is not used (<why>):
its lookups go to PostgreSQL until it is built again`) y una vez cuando vuelve a usarse. Una caja
fuerte por encima de 16.777.216 huellas, unos ocho millones de valores, no tiene índice mientras dura
su espacio: sus búsquedas van a PostgreSQL, y un espacio nuevo lo intenta una vez más. La caja fuerte
de una persona guardada antes de Latet 0.10.0, cuyos valores aún no tienen palabras, se guarda entera
en memoria, como antes, hasta que sus palabras y sus huellas se crean, una vez, en una instancia; el
espacio de la persona construido después trabaja con PostgreSQL. La 0.11 no vuelve a hacer las
palabras de ninguna clave ([actualizar un servidor](../../applications/#actualizar-un-servidor)).

Latet lee cada petición según el perfil de su ruta, que nombra cada campo, cabecera y parámetro de
consulta que lleva. En cada versión de Latet, y al menos cada dos semanas, las versiones más recientes
de los agentes que Latet admite pasan por las peticiones que estos perfiles describen. En una
petición que Latet solo comprueba (Claude Code, Bedrock, Vertex), pasan, leídos por la comprobación
final, un miembro nuevo del cuerpo que contiene texto, una cabecera `anthropic-*` nueva o un valor
nuevo de `anthropic-beta`, y se quita una cabecera `x-claude-code-*` nueva. Cuando la actualización
de un agente envía cualquier otra cosa que ningún perfil nombra, la nueva revisión del perfil sale en
una versión de corrección; hasta que la instales, esa petición se rechaza, nombrando su categoría, y no
sale nada de ella. Mantén las instancias en la última versión de corrección de su versión.

Las instancias comparten las mismas tablas, y una instancia solo arranca sobre tablas de su versión. La
base de datos es tuya de guardar: Latet no copia nada de ella. Antes de descargar una imagen nueva y de
ejecutar `latet server migrate`, haz una copia con `pg_dump`, que es coherente incluso mientras las
instancias escriben:

```sh
pg_dump --format=custom --file=latet-before-upgrade.dump "postgresql://latet@db.acme.lan/latet"
```

Para volver atrás, restaura esa copia (`pg_restore --clean --if-exists`) y arranca la imagen anterior: una
instancia no arranca sobre tablas de una versión más nueva. Los valores de la copia siguen cifrados, como en
la base de datos: solo la clave maestra del servidor los abre. Antes de una actualización, pregunta a la nueva versión
si cambia las tablas:

```sh
latet server migrate --check
```

- **Las tablas no cambian:** sustituye las instancias una a una detrás del equilibrador de carga. Una
  instancia detenida termina sus peticiones en 30 segundos como máximo.
- **Las tablas cambian:** detén todas las instancias, ejecuta `latet server migrate` una vez con la nueva
  versión y luego arranca las instancias con ella. `latet server migrate` se niega mientras una
  instancia está viva (una de la 0.10 durante 30 segundos después de detenerse), y una instancia no
  arranca sobre tablas de otra versión.
- **Hay programas que usan la API para aplicaciones:** detén cada instancia más antigua antes de que
  arranquen las nuevas, aunque las tablas no cambien. Mientras dos versiones se ejecutan a la vez, un
  valor que una instancia más antigua escribe en la caja fuerte de una clave puede no encontrarlo una
  más nueva hasta que el espacio de la clave se vuelva a construir
  ([actualizar un servidor](../../applications/#actualizar-un-servidor)).
- **Una versión que rehace las palabras de una clave** (el changelog lo dice; la 0.11 no): la primera instancia
  que construye el espacio de cada clave de API rehace las palabras de su caja fuerte antes de responder a la
  primera llamada de esa clave, lo que a Latet 0.10.0 le llevaba unos 40 segundos para 200.000 valores y cuatro
  minutos y medio para 1.000.000. Se hace una vez por clave, pero una instancia construye cuatro espacios a la
  vez, de personas y de claves, así que varias claves grandes construidas a la vez tras un reinicio retienen las
  primeras llamadas de todos los demás espacios de esa instancia: envía una llamada a cada clave grande, una
  clave tras otra, antes de que vuelva el tráfico.

En el ejemplo de Compose, cuando las tablas cambian, los mismos pasos se ejecutan en contenedores de la
nueva imagen (la pausa deja caducar las claves de las instancias de la 0.10 detenidas; una instancia
de la 0.11 quita la suya al detenerse):

```sh
docker compose -p latet --profile server pull
docker compose -p latet --profile server run --rm latet-server-1 server migrate --check
docker compose -p latet --profile server stop latet-server-1 latet-server-2
sleep 30
docker compose -p latet --profile server run --rm latet-server-1 server migrate
docker compose -p latet --profile server up -d latet-server-1 latet-server-2
```

Para añadir una instancia, arranca otra con los mismos ajustes, la misma clave maestra y los mismos
PostgreSQL y Valkey, y añade su dirección al equilibrador de carga. Las instancias guardan su estado en
PostgreSQL y Valkey, así que cualquiera puede responder a cualquier petición.

### Varias instancias en una máquina

Una instancia hace la mayor parte de su trabajo en un solo núcleo del procesador. Para usar más núcleos de
una máquina, `latet server run --workers N` (o el ajuste `workers`, `LATET_SERVER_WORKERS`) arranca N
instancias, llamadas workers, en el mismo puerto, cada una en un proceso propio: comparten PostgreSQL y
Valkey como lo harían instancias en N máquinas, y el equilibrador de carga se queda como está. En el
ejemplo de Compose, define `LATET_SERVER_WORKERS` en el archivo `.env`. Empieza con un worker por cada
núcleo que la máquina da al servidor.

- **Cuántos:** `--workers` en la línea de comandos, si no `LATET_SERVER_WORKERS`, si no `workers` en
  `server.yaml`, si no uno; de 1 a 64. Los ajustes rechazan un número cuyos puertos de métricas (más
  abajo) pasarían de 65535 o tomarían el puerto de la propia instancia. En el ejemplo de Compose cada
  contenedor del servidor arranca sus propios workers: dos contenedores con `LATET_SERVER_WORKERS=4`
  son ocho instancias, con ocho pools de conexiones.
- **Lo que aportan:** medido en Linux a través de la API para aplicaciones, ocho workers enmascararon
  6,28 veces lo que uno y restauraron 5,21 veces, donde esta versión se proponía llegar a 5 veces
  ([velocidad](../../applications/#velocidad)). Menos núcleos libres, o unas pocas conexiones de larga
  duración, dan menos: N workers no son N veces uno.
- **Cómo les llegan las conexiones:** en Linux cada worker escucha en el puerto con un socket
  propio, y el sistema reparte las conexiones nuevas entre ellos según un hash de las direcciones y
  los puertos de cada una. Muchas conexiones se reparten de forma uniforme; unas pocas conexiones de
  larga duración, como las de un equilibrador de carga, pueden aun así repartirse de forma desigual,
  y cuantas más conexiones haya, mejor se reparten. En macOS, que daría cada conexión a uno solo de
  los sockets abiertos en un puerto, los workers comparten en cambio un único socket, y las
  conexiones les llegan de forma menos uniforme. Para que sea el kernel quien reparta las
  conexiones, ejecuta el servidor en Linux o en la imagen del contenedor.
- **El usuario del servidor:** en Linux el proceso que arranca los workers retiene el puerto durante
  todo el funcionamiento del servidor, así que ningún otro programa lo toma, ni siquiera mientras
  ningún worker escucha; justo después de que se detenga un servidor de una sola instancia en el
  mismo puerto, espera hasta un minuto a que desaparezcan las conexiones cerradas de ese servidor.
  Un programa solo puede escuchar en el puerto junto a los workers si se ejecuta con el mismo
  usuario que el servidor, y entonces podría quedarse con una parte de las conexiones, o con todas.
  Ejecuta el servidor con un usuario que no ejecute nada más: el usuario `latet` del servicio del
  sistema, o el propio del contenedor.
- **Memoria y conexiones:** cada worker ocupa más o menos tanta memoria como una instancia: en Linux
  unos 110 MB residentes en reposo, y de 185 a 227 MB con el espacio de una clave de API de 200.000
  valores tras servir 200.000 líneas de registro distintas, medidos por cada worker sin modelos de
  reconocimiento; más por otros espacios, sus cachés y los modelos de reconocimiento cuando están
  instalados. Un worker que sirve a una persona cuya caja fuerte se guarda entera la guarda toda,
  unos 1,6 KB por valor (unos 58 MB con 35.000 valores). Prevé N veces eso, más hasta unos 70 MB para
  el proceso que los arranca y los vigila. El reconocimiento de texto da a cada worker su parte de los
  procesadores de la máquina. Cada worker tiene además sus propias conexiones, `database_pool` hacia
  PostgreSQL y `valkey_connections` hacia Valkey: haz que `max_connections` de PostgreSQL y
  `maxclients` de Valkey cubran cada worker de cada máquina.
- **El proceso que los arranca:** no construye ningún espacio ni abre ninguna base de datos, clave
  maestra o certificado; cada worker lo hace en su propio proceso. Arranca con los ajustes y el
  entorno del servidor y se los pasa a los workers, así que protégelo como una instancia.
- **Métricas:** el worker 0 responde a Prometheus en `metrics_port`, cada otro worker en el puerto
  siguiente (7283, 7284 y así sucesivamente): consulta cada uno. `latet_process_memory_bytes` es la
  memoria de un worker (su memoria residente en Linux, sus bytes privados en Windows), y
  `latet_vault_index_bytes` la parte que ocupan los índices de sus cajas fuertes.
- **Vigilancia:** un worker que termina, o que no envía ningún latido durante 30 segundos, se cierra
  y se arranca de nuevo, como mucho una vez cada diez segundos. Un worker que no puede arrancar tres
  veces seguidas (sus ajustes, su base de datos o su certificado lo rechazan) detiene todos los
  workers y el servidor, como se detiene una instancia sola, para que decida el gestor de servicios.
  Un worker que no está listo (PostgreSQL o Valkey no responden) se queda: decide la comprobación de
  disponibilidad del equilibrador. `latet server status` lista cada instancia viva, cada worker de
  cada máquina, con su número de worker, si está lista, su memoria y su versión; un worker que murió
  sigue en la lista hasta 30 segundos después de que arranque el que lo sustituye. Una petición que un
  worker estaba sirviendo cuando terminó no recibe respuesta: su cliente recibe un error y tiene que
  volver a enviarla, y ninguna recibe una respuesta sin enmascarar. En Linux, las conexiones que
  aún esperaban a un worker que terminó se cortan, y sus clientes tienen que volver a conectarse, y
  un worker que deja de enviar latidos hace esperar su parte de las conexiones nuevas hasta que se
  cierra.
- **Pasar las conexiones en espera a otros workers (opcional):** desde Linux 5.14, con
  `net.ipv4.tcp_migrate_req` a 1, el sistema pasa a los demás workers las conexiones que esperan a
  un worker que termina, en lugar de cortarlas. En el ejemplo de Compose, quita el comentario de la
  entrada `sysctls` de los servicios del servidor; para un servicio del sistema, pon
  `net.ipv4.tcp_migrate_req = 1` en un archivo bajo `/etc/sysctl.d/` y cárgalo con
  `sysctl --system`. No está activado de forma predeterminada: en un kernel más antiguo, Docker se
  niega a arrancar un contenedor que lo define.
- **Parada:** una señal de parada llega a cada worker, y cada uno tiene 30 segundos para terminar el
  trabajo en curso, como una instancia sola.
- **Windows:** varios workers necesitan Linux o la imagen del contenedor. En Windows `latet server run`
  rechaza más de uno: allí un socket sirve a un solo proceso, y el único bucle de eventos que podría
  compartirlo detiene un worker a partir de 512 conexiones. Arranca una instancia en cada máquina Windows
  detrás del equilibrador de carga; el servicio de Windows arranca una, diga lo que diga el ajuste.

### Actualizar un Latet instalado como servicio del sistema

Un Latet instalado como servicio del sistema, como arriba, se actualiza con el comando con el que se
instaló, ejecutado de nuevo con el wheel de la nueva versión, las mismas carpetas `UV_*` y los mismos
extras (`server`, con `ner` y `ocr` si los tiene):

```sh
UV_TOOL_DIR=/opt/latet/tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/latet/python \
  uv tool install --upgrade --python ">=3.12,<3.15" "latet[server] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
```

Después pregunta a la nueva versión si cambia las tablas, como arriba (`latet server migrate --check`),
y reinicia el servicio (`systemctl restart latet-server`). En Windows, que no sustituye ningún archivo
de un programa en ejecución, detén el servicio antes de la instalación (`Stop-Service LatetServer`) e
inícialo después (`Start-Service LatetServer`). `latet upgrade` instala en las carpetas de uv: en un
Latet instalado en carpetas propias, se detiene antes de cambiar nada e imprime este comando, con las
carpetas que ha encontrado.

Hasta la 0.10.4, `latet upgrade` solo nombraba la detección de nombres y la lectura de texto en
imágenes, y uv quitaba los demás extras: un servidor actualizado así perdía `server` (psycopg y
redis), y `latet server run` se negaba después a arrancar. Un servidor con la 0.10.4 o anterior pasa
por eso una vez a una versión posterior con el comando de arriba, nombrando `server`, o con el
instalador descargado de nuevo desde GitHub y ejecutado con las mismas variables `UV_*`. Un servidor
que ya ha perdido `server` lo recupera con el comando de arriba: el instalador no puede saber de un
extra que ya no está.

## Modelos de reconocimiento en un servidor

La detección de nombres y el reconocimiento de texto leen sus modelos de la carpeta de datos del
servidor, no del ordenador del administrador. Instálalos allí, con la cuenta con la que se ejecutan las
instancias: `latet ner install` y `latet ocr install` con `LATET_DATA_DIR` apuntando a esa carpeta. En
el ejemplo de Compose, la carpeta es el volumen `latet-server-data`, montado en `/data` en cada
instancia: instálalos desde un contenedor de una instancia y después reinicia las instancias.

```sh
docker compose -p latet --profile server run --rm latet-server-1 ner install --yes
docker compose -p latet --profile server run --rm latet-server-1 ocr install --yes
docker compose -p latet --profile server restart latet-server-1 latet-server-2
```

Con el servicio de Linux, las instancias se ejecutan como el usuario `latet`, y su carpeta es
`/var/lib/latet`, que systemd crea cuando el servicio arranca por primera vez. Una vez que ha
arrancado, instala los modelos como ese usuario y después reinicia el servicio:

```sh
sudo -u latet env LATET_DATA_DIR=/var/lib/latet latet ner install --yes
sudo -u latet env LATET_DATA_DIR=/var/lib/latet latet ocr install --yes
systemctl restart latet-server
```

Para el servicio de Windows, esta página no da un procedimiento para instalar los modelos.

## Programas con una clave de API

Un programa llega a sus rutas con su clave de API y conserva su propia clave del proveedor. Escribe la
clave de API en un archivo privado y luego muestra las variables que hacen pasar el programa por el
servidor:

```sh
latet env --key-file program.key --server https://latet.acme.lan
```

Las variables mostradas llevan la clave de ruta: mantenlas tan secretas como la clave, nunca en un repositorio ni
en un perfil compartido. Solo el primer token de la clave tiene estas direcciones: un token creado con
`keys add-token` no tiene ninguna, tenga los permisos que tenga, y solo llama a la API para aplicaciones.
`latet env` pregunta al servidor de qué tipo es el token: para un token así no muestra ninguna variable
y lo dice.

El programa debe confiar en el certificado del servidor: uno de una autoridad en la que ya confía, o el
certificado añadido a su propia configuración de confianza.

Un programa que enmascara sus propios registros o documentos llama en su lugar a la
[API para aplicaciones](../../applications/): sin variables, con un token de la clave en su cabecera
`Authorization`, con solo los permisos que necesita. Sus llamadas comparten el espacio de la clave con los
agentes de la clave, y por tanto su caja fuerte y sus valores ficticios.

Dos tipos de su trabajo hacen cola para todo el servidor, no para cada clave. La entrada del registro de
administración de cada restauración se escribe antes de su respuesta, una a la vez en la cadena del
registro, por dos hilos de cada instancia para las restauraciones de todas las claves; y los olvidos se
ejecutan de dos en dos en cada instancia, para todas las claves. Una clave que envía muchas
restauraciones a la vez, u olvida una lista larga, retrasa las restauraciones o los olvidos de las
demás claves, nunca una inscripción, una revocación ni lo que envían los agentes. El registro de
administración crece una fila por cada llamada de restauración.

## Registros, métricas y recuperación

Los registros de la aplicación van a la salida estándar en `text` o `json`. Protege
y rota los archivos de registro de la instalación. El registro de administración es
separado y anota operaciones, incluidas emisiones de códigos y cambios de credenciales.
Léelo con `latet server log` y comprueba su cadena con `latet server log verify`. Guarda
la cabecera devuelta fuera de la base de datos: borrar el final puede dejar válida la cadena restante.

El resultado sobre la cadena del comando status está en caché y puede ser desconocido;
no es una nueva comprobación completa. Las métricas Prometheus usan un listener separado,
`127.0.0.1:7283` por defecto, y requieren `metrics_token_file`. Distingue un resultado desconocido de una cadena rota.
La [referencia de administración](../../reference/server-administration/) define la
paginación del registro y el significado preciso de `latet_ocr_unread_total{reason}`.

Cada restauración a través de la API para aplicaciones es una entrada `apps.unmasked` del registro de
administración, con el token y cuántos valores restauró, y cada olvido una entrada `apps.forgotten`,
con el token y cuántos valores quitó de la caja fuerte de la clave; el análisis y el enmascaramiento
solo se
cuentan en las métricas (`latet_apps_texts_total` y `latet_apps_values_total`, por operación, y las
solicitudes como `latet_http_requests_total{kind="apps"}`), sin etiqueta para la clave
([qué registra el servidor](../../reference/apps-api/#qué-registra-el-servidor)).

En esta configuración Compose, consulta `127.0.0.1:7283` desde dentro de cada instancia con
su token de métricas. No hay puerto publicado, ruta Caddy ni red de monitorización incluida.
Otro despliegue debe preparar explícitamente una ruta de consulta protegida y autenticada;
el puerto HTTPS de la aplicación no sirve estas métricas. El receptor de las métricas solo
responde a `GET /_latet/metrics`, y a cualquier otra ruta con `404`: Prometheus pide `/metrics`
salvo que su `metrics_path` diga otra cosa.

Una instancia por hora recorre la cadena del registro de administración y guarda el veredicto; el estado y
las métricas muestran ese veredicto durante dos horas, y lo dan por desconocido cuando no hay ninguno (un
servidor nuevo, o un Valkey arrancado desde cero). Latet no tiene ninguna herramienta que repare una cadena
rota: compárala con la cabeza que guardaste fuera de la base de datos, busca la entrada que indica y restaura la
base de datos desde una copia de seguridad si el registro se alteró.

Un Prometheus junto a una instancia la consulta así, cada 30 segundos; sirve cualquier intervalo de hasta
dos minutos. Las alertas esperan más de una consulta, porque la primera tras un arranque aún no tiene
las lecturas de los servicios:

```yaml
scrape_configs:
  - job_name: latet
    scrape_interval: 30s
    metrics_path: /_latet/metrics
    authorization:
      credentials_file: /etc/prometheus/latet-metrics.token
    static_configs:
      - targets: ["127.0.0.1:7283"]
```

```yaml
groups:
  - name: latet
    rules:
      - alert: LatetDatabaseDown
        expr: latet_database_up == 0
        for: 2m
      - alert: LatetValkeyDown
        expr: latet_valkey_up == 0
        for: 2m
      - alert: LatetAuditChainBroken
        expr: latet_audit_chain_known == 1 and latet_audit_chain_ok == 0
        for: 5m
      - alert: LatetLogsDropped
        expr: increase(latet_log_dropped_total[10m]) > 0
```

Haz copias de seguridad de PostgreSQL, la clave maestra correspondiente y la configuración
con controles de acceso adecuados. Prueba la restauración en un entorno aislado antes
de confiar en la copia. Restaurar una base de datos antigua puede restaurar también
autorizaciones antiguas: reconcilia credenciales y revocaciones antes de reabrir el
servicio. La conservación del historial y su borrado desde el panel no eliminan copias
de seguridad ni la caja fuerte. Vigila esos almacenes por separado.
