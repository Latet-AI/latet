---
title: Configurar y administrar un servidor de Latet
description: Desplegar un servidor, conectar un equipo y gestionar personas, claves de API y ajustes compartidos.
---

Ejecuta los comandos de administración en tu propio terminal, en un equipo con Latet instalado.
Lee [los espacios y la confianza](../../concepts/server/) antes de conceder acceso.
Un administrador prepara el despliegue; cada persona conecta su propio equipo.
Un agente de IA puede explicarte los pasos y revisar la máquina, pero los comandos que cambian la
administración, `config get`, `rules export`, `latet server init`, `latet server service install`
y `latet connect` se niegan a ejecutarse dentro de un agente: indícale [`llms-server.md`](https://github.com/Latet-AI/latet/blob/main/llms-server.md).
No des nunca un token de administración a un agente: con uno, los comandos que solo leen (el estado,
las listas de personas y de claves, el registro) se ejecutan para él.

## Preparar el despliegue

Elige el [servicio del sistema o Compose](../../concepts/server-operations/).
El servicio necesita una instalación de confianza para todo el sistema y una configuración
protegida; el perfil servidor de Compose incluye PostgreSQL, Valkey, dos instancias y Caddy.
Configura la dirección HTTPS pública y prepara primero la clave maestra y los archivos secretos.
La inicialización imprime un token de administración: guárdalo en un terminal privado y en
un almacén de secretos. Para un contenedor, sigue la inicialización con los registros Docker
desactivados de la guía operativa; una inicialización con registros puede conservar ese token.

Usa un certificado en el que confíen los equipos. Para una primera prueba, crea un certificado
de servidor autofirmado y comprueba su huella digital:

```sh
latet server certificate --make --url https://latet.acme.lan --out certs
latet server certificate certs/server.crt
```

Mantén privado `server.key`. Compose monta `certs/server.crt` y `certs/server.key` en Caddy;
sin contenedores, configura TLS en la instancia o en su proxy inverso de confianza.
Entrega la huella verificada por un canal de confianza separado. Una huella obtenida de una
conexión de red sin verificar no autentica por sí sola al servidor.

## Comprobar el servidor

```sh
# a self-signed certificate: its fingerprint, verified as above, for every `latet server` command in this shell
export LATET_SERVER_FINGERPRINT="<fingerprint>"
latet server --server https://latet.acme.lan status
```

Introduce el token de administración en la entrada oculta. La respuesta informa sobre
la base de datos, Valkey, las identidades y la instancia que ha respondido. No comprueba
cada agente ni demuestra que todo el tráfico de un equipo pase por Latet.

Para administrar el servidor repetidamente, `--admin-token-file FILE` lee un archivo
privado con el token o un registro vinculado a este servidor. Guárdalo fuera de los
repositorios y de los espacios de trabajo de los agentes. Sigue las comprobaciones de
permisos y origen de [Administración del servidor](../../reference/server-administration/).
No exportes un token de administración al entorno de la shell de una cuenta donde se ejecuten agentes.

En la respuesta, `database` es siempre verdadero, porque una instancia que no alcanza PostgreSQL no da
ningún estado, y `valkey` suele serlo por la misma razón: consulta su salud en las métricas
(`latet_database_up`, `latet_valkey_up`). `audit` da el último veredicto de la cadena (`ok`, `broken` o
`unknown`), y `vault` enumera las identidades al 90 % o más del límite de su caja fuerte.

## Dar a una persona su propio espacio

```sh
latet server --server https://latet.acme.lan people add "Emily Carter" --email emily.carter@acme.com
latet server --server https://latet.acme.lan people list
```

El primer comando muestra un código de un solo uso. Entrégaselo en privado a esa
persona: quien lo use puede conectarse como ella y abrir su panel. La emisión del código
se anota en el registro de administración. El código funciona una sola vez, en 24 horas.
`people code "Emily Carter"` emite otro cuando ha caducado o se ha perdido, y el anterior deja de
funcionar; nunca pongas un código en una incidencia, un canal compartido o una conversación con un agente.

Los equipos de una persona comparten un espacio: su caja fuerte, historial y ajustes.
Para consultar los tokens de los equipos, usa:

```sh
latet server --server https://latet.acme.lan people tokens "Emily Carter"
```

Revoca el token de un equipo perdido mediante su id en la lista con `--revoke TOKEN_ID`.
`people disable "Emily Carter"` detiene en segundos todos sus tokens y conserva su espacio
(`people enable` se lo devuelve); `people remove "Emily Carter"` borra su espacio activo en el
servidor, incluidos caja fuerte, historial y ajustes, una vez que lo confirmas. `keys disable`,
`enable` y `remove` hacen lo mismo con una clave de API. Ninguna operación borra copias en
copias de seguridad, navegadores o proveedores del modelo. Lee la confirmación antes de eliminarla.

Al eliminar a una persona, sus datos en PostgreSQL se van con ella, sus datos en Valkey se borran una vez desde
una instancia (de nuevo en la siguiente comprobación si no puede terminar), lo que quede allí caduca por sí solo,
en ocho días como máximo, y sus sesiones del panel y de AWS terminan al momento.

## Conectar el equipo de una persona

Cuando el servidor esté listo y el administrador haya emitido el código de la persona, ejecuta:

```sh
latet connect https://latet.acme.lan
```

Introduce el código de un solo uso cuando se solicite. Con un certificado autofirmado, añade
`--fingerprint FINGERPRINT` con la huella verificada con el administrador. No elimines la
vinculación al certificado para evitar un error. El registro de conexión, incluido el archivo
elegido por `LATET_CONNECTION`, contiene metadatos y la huella; el token personal y la clave
del equipo se guardan juntos, por separado, en el almacén de credenciales o en un archivo privado.

El proceso auxiliar conectado de Claude Code está admitido en un **terminal Windows**.
Aplica la integración y reinicia Claude Code, o inicia una sola sesión:

```sh
latet agents apply claude
latet claude
```

`latet agents apply claude` y `latet global-install` escriben el token personal en el
`settings.json` del usuario de Claude como `LATET_BRIDGE_TOKEN`; el comando indica dónde.
Es la excepción de instalación a la regla que excluye el token de los archivos de los agentes.
La vinculación privada del proceso auxiliar contiene metadatos; recibe el token existente
por stdin. Un programa que pueda observar esa entrada puede leerlo. Este contrato no admite
el proceso auxiliar conectado en Claude Desktop, VS Code, JetBrains, Linux ni macOS: en Linux
y macOS estos comandos lo dicen de entrada y no escriben el token, y el plugin retiene cada prompt.

Ese token abre el bridge del plugin y el proxy del espacio desde cualquier lugar donde se alcance el
servidor; nunca abre el panel, nunca cambia la identidad de la persona y nunca reduce su protección: el
único cambio que puede hacer en sus ajustes es añadir un destino para una dirección, lo que no reduce
nada. El panel y la identidad necesitan también la clave del equipo, que `latet connect` guarda con el
token en el almacén de credenciales (o en el archivo privado) y que solo leen `latet dashboard` y
`latet identity send`, que se niegan a ejecutarse dentro de un agente; ningún agente la recibe.

Usa estos comandos en el equipo conectado:

```sh
latet status
latet doctor
latet dashboard
latet identity send
latet disconnect
```

`status` y `doctor` ofrecen diagnósticos acotados, no una prueba de cada ruta de los agentes.
`dashboard` abre el panel de la persona en el servidor; en un equipo conectado, un agente no puede
ejecutarlo. Solo imprime el enlace privado de
inicio de sesión con `--print-url`; mantenlo privado. Mientras hay conexión, `config`, `rules`,
`llm`, `project`, `identity` y `reload` rechazan tanto leer los ajustes como cambiarlos; solo
funcionan `config schema`, `rules test` e `identity send`. Usa el panel del servidor o los
comandos de administración. `latet identity send` reenvía explícitamente los datos de identidad
de este equipo. `latet start` también se niega: mientras hay conexión, nada usa el daemon de
este equipo.

`latet disconnect` revoca el token de este equipo y elimina las vinculaciones de los agentes
a la conexión, incluido el token de Claude guardado. La revocación también termina las
sesiones del panel de la persona en sus otros equipos. Si no puedes confirmar la revocación,
lee el resultado y pide a un administrador que compruebe los id de los tokens antes de
reintentar. En un equipo conectado, el modo de impresión (`claude -p`) no se admite: el plugin
retiene sus peticiones y lo dice. Usa el terminal interactivo. La
[guía de Claude Code](../../agents/claude-code/) distingue el uso conectado de las superficies locales.

Quitar las copias de este equipo no revoca el token. Con `LATET_KEYRING=off`, Latet no guarda
credenciales en el llavero del sistema operativo, pero puede quedar allí una copia más antigua:
`latet connect` se niega mientras exista, y `latet disconnect` la lee, la revoca en su servidor y la
elimina; un llavero bloqueado puede pedir que lo desbloquees. Si una conexión o una desconexión se
interrumpe, un archivo temporal de token que haya quedado se borra sin leerlo, así que el equipo no puede
revocar el token que contenía. En ese caso, o cuando `latet disconnect` indica que queda una copia, lo
revoca un administrador en el servidor: lista los tokens de la persona como arriba, encuentra el correcto
por su id (la etiqueta con el nombre del equipo es solo una pista: dos equipos pueden llamarse igual) y lo
revoca con `--revoke TOKEN_ID`. Si no se puede distinguir el token correcto, `--revoke-all` revoca todos
los tokens de la persona, y cada uno de sus equipos tendrá que conectarse de nuevo.

## Dar una clave de API a un programa

Una clave de API tiene su propio espacio, sin panel personal ni registro de equipos.
Su administrador gestiona sus ajustes y sus reglas. Crea el secreto en un archivo privado nuevo:

```sh
latet server --server https://latet.acme.lan keys add "Billing CI" --out billing.key
latet server --server https://latet.acme.lan keys list
```

Usa una carpeta protegida fuera del repositorio. El comando muestra la identidad y la
ruta del archivo sin mostrar el secreto. Guarda la clave en el almacén de secretos del
programa. `keys rotate "Billing CI" --out replacement.key` sustituye su credencial;
actualiza a la vez la clave y las direcciones de ruta del programa. Una clave de ruta
dentro de una dirección también es sensible.

## Establecer los ajustes compartidos y las reglas de las claves de API

Los ajustes compartidos contienen lo que el servidor necesita para funcionar, igual para cada
espacio: los destinos a los que pueden ir las peticiones (`upstreams`), el revisor local
compartido (`llm_review`), el reconocimiento de texto (`ocr`), el historial y cuánto tiempo se
guarda (`retention_days`).

```sh
latet server --server https://latet.acme.lan config get
latet server --server https://latet.acme.lan config add upstreams "{name: llm-acme-com, url: https://llm.acme.com}"
```

Sin `--key`, `config get`, `set`, `add` y `remove` actúan solo sobre estos ajustes. Una regla de
protección, como un término o el perfil, se rechaza, con un mensaje que explica cómo compartirla a
mano. Con `--key`, los mismos comandos leen y cambian los ajustes y las reglas propios de una clave de API,
y `latet server rules --key` gestiona sus reconocedores e importa un archivo de reglas en sus reglas:

```sh
latet server --server https://latet.acme.lan config --key "Billing CI" set profile max
latet server --server https://latet.acme.lan rules --key "Billing CI" import team-rules.yaml
```

`config get` y `rules export` pueden mostrar valores en claro (los términos de una clave de API,
las indicaciones para el revisor): mantén privada su salida. Las reglas propias de una persona se
gestionan en su panel, donde ella misma las establece.

Para dar las mismas reglas a un equipo de trabajo, compártelas a mano: expórtalas desde un sitio
(`latet rules export FILE` en un equipo no conectado a un servidor, o **Exportar reglas** en la
página **Reglas** de un panel) e importa el archivo en cada uno de los demás (`latet rules import
FILE`, o **Importar reglas** en el panel); las de una clave de API con
`latet server rules --key KEY import FILE`. En los equipos no conectados a un servidor, el
`latet.yaml` de un proyecto las lleva en su repositorio. Las reglas establecidas para todos, que
nadie puede relajar, están previstas para la Enterprise Edition.

Un cambio en los ajustes compartidos que reduce la protección de una persona, como un destino
movido a otra dirección o el revisor apagado, espera su aprobación. Guardar sus
propios cambios no aprueba un cambio compartido pendiente. Un guardado puede conservar
esos cambios sin conseguir actualizar la propuesta pendiente; lee el resultado y recarga
antes de hacer otro cambio.
Una clave de API no tiene a nadie que apruebe un cambio así: llega al espacio de la clave de inmediato,
también un cambio que reduce la protección, así que comprueba qué hace en tus programas un cambio en
los ajustes compartidos antes de hacerlo.
[Ajustes compartidos y reglas propias](../../concepts/server/) explica la diferencia.

Usa tokens de administración con fecha de caducidad para el trabajo cotidiano. La
[referencia de administración](../../reference/server-administration/) explica su
emisión y revocación, los archivos de salida privados, la recuperación y la lectura del registro.
