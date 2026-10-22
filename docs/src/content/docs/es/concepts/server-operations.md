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
  uv tool install "latet[server] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
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

## HTTPS, proxies y capacidad

Termina TLS en la instancia o en el equilibrador de carga. Establece `forwarded_allow_ips`
en los proxies realmente fiables, nunca `*`; no se confía en las cabeceras reenviadas
desde otras direcciones. Mantén los relojes sincronizados, con una diferencia máxima
de 60 segundos. Restringe por separado el acceso directo a los proveedores si el servidor
es el punto de control obligatorio de la empresa.

El límite predeterminado del cuerpo de una solicitud es de 32 MiB. `requests_in_flight`
vale 16 por identidad en una instancia, con cuotas separadas para solicitudes a destinos,
al panel y al bridge. Las esperas largas y los flujos tienen sus propios límites. Un
rechazo por capacidad exige revisar carga y límites, no desactivar la protección.
Al apagarse, una instancia tiene 30 segundos para terminar; los flujos largos pueden
cortarse, así que los clientes deben gestionar la desconexión.

El OCR usa lectores limitados, cada uno con sus modelos en memoria. `ocr_readers` vale
2 por defecto y no puede superar los procesadores disponibles para la instancia.
`ocr_read_seconds` vale 30; `ocr_wait_seconds` vale 35 y no puede ser menor que el tiempo
de lectura. Aumentar los lectores consume memoria además de CPU. Una imagen no leída
se retiene o se rechaza su solicitud verificada, según la ruta protegida; aumentar los
tiempos de espera no mejora la precisión del reconocimiento.

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
  por encima, 429 con `Retry-After`. Un cuerpo se lee hasta `max_request_mb` (32 MiB por defecto); un cuerpo JSON
  cerca de ese límite retiene el bloqueo del intérprete de uno a tres segundos dondequiera que se analice, así que
  mantén el límite tan bajo como permitan tus agentes y ejecuta varias instancias.
- **PostgreSQL:** cada instancia mantiene hasta 10 conexiones; el `max_connections` de PostgreSQL debe cubrir las
  10 de cada instancia y los comandos de los administradores.
- **Valkey:** cada instancia mantiene hasta `valkey_connections` conexiones (100 por defecto), y por encima una
  llamada espera cinco segundos como máximo. El `maxclients` de Valkey debe cubrir las conexiones de todas las
  instancias. La conexión a Valkey, su TLS y su primera respuesta comparten un único límite de cinco segundos.
- **Los certificados de los proveedores:** una instancia confía en el almacén de certificados del sistema
  operativo; en Linux, en las rutas predeterminadas de OpenSSL, que `SSL_CERT_FILE` y `SSL_CERT_DIR` cambian para
  todo el proceso. Cámbialas solo para añadir la autoridad de tu empresa.
- **`SSLKEYLOGFILE`** no debe definirse para una instancia: las conexiones TLS que la leen, entre ellas la de
  Valkey, escriben sus claves de sesión en ese archivo, y quien lo lee lee ese tráfico. Una instancia arrancada con
  ella lo indica en su registro.

## Actualizar y añadir instancias

Las instancias comparten las mismas tablas, y una instancia solo arranca sobre tablas de su versión. Antes de una
actualización, pregunta a la nueva versión si las cambia:

```sh
latet server migrate --check
```

- **Las tablas no cambian:** sustituye las instancias una a una detrás del equilibrador de carga. Una
  instancia detenida termina sus peticiones en 30 segundos como máximo.
- **Las tablas cambian:** detén todas las instancias, ejecuta `latet server migrate` una vez con la nueva
  versión y luego arranca las instancias con ella. Una instancia no arranca sobre tablas de otra versión.

En el ejemplo de Compose, cuando las tablas cambian, los mismos pasos se ejecutan en contenedores de la
nueva imagen:

```sh
docker compose -p latet --profile server pull
docker compose -p latet --profile server run --rm latet-server-1 server migrate --check
docker compose -p latet --profile server stop latet-server-1 latet-server-2
docker compose -p latet --profile server run --rm latet-server-1 server migrate
docker compose -p latet --profile server up -d latet-server-1 latet-server-2
```

Para añadir una instancia, arranca otra con los mismos ajustes, la misma clave maestra y los mismos
PostgreSQL y Valkey, y añade su dirección al equilibrador de carga. Las instancias guardan su estado en
PostgreSQL y Valkey, así que cualquiera puede responder a cualquier petición.

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

Con el servicio de Windows, el procedimiento para instalar los modelos todavía no está documentado.

## Programas con una clave de API

Un programa llega a sus rutas con su clave de API y conserva su propia clave del proveedor. Escribe la
clave de API en un archivo privado y luego muestra las variables que hacen pasar el programa por el
servidor:

```sh
latet env --key-file program.key --server https://latet.acme.lan
```

Las variables mostradas llevan la clave de ruta: mantenlas tan secretas como la clave, nunca en un repositorio ni
en un perfil compartido.

El programa debe confiar en el certificado del servidor: uno de una autoridad en la que ya confía, o el
certificado añadido a su propia configuración de confianza.

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
