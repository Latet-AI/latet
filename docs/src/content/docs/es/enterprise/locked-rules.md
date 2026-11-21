---
title: Reglas bloqueadas
description: Directrices que tu empresa define en su servidor de Latet, por debajo de las cuales no puede quedar ninguna persona ni ninguna clave de API.
---

Con Latet Enterprise una empresa define un **mínimo** en su [servidor de Latet](../../getting-started/server/): ajustes
por debajo de los cuales no puede quedar ninguna persona ni ninguna clave de API. Cada persona conserva sus propios
ajustes y siempre puede añadir protección. Un cambio que quedaría por debajo del mínimo se rechaza, con un aviso de que
ese ajuste lo define la empresa; se rechaza, no se deja en espera de aprobación, porque la persona no puede aprobarlo.
Un cambio solo se rechaza por lo que él mismo reduce: los ajustes que ya estaban por debajo del mínimo no impiden
cambiar otra cosa.

## El archivo de directrices

Las directrices son un archivo de reglas, en el formato que escribe `latet rules export` (véase
[Exportar e importar](../../concepts/export-import/)), así que una empresa puede partir de una exportación:

```yaml
latet_rules: 1
exceptions: company
profile: max
terms:
  - {value: Project Falcon, type: PROJECT}
  - {value: Northwind Traders, type: CLIENT}
policies: {IBAN: redact}
ocr: auto
```

**Lo que dice queda bloqueado, y lo que omite sigue siendo de cada persona.** Con este archivo nadie puede elegir un
perfil inferior a `max` ni dejar de leer el texto de las imágenes, Project Falcon y Northwind Traders se enmascaran
siempre, los IBAN se suprimen siempre, y solo cuentan las excepciones de la empresa (aquí no define ninguna); cualquiera
puede seguir añadiendo términos, reconocedores y valores más estrictos propios. Para bloquear un ajuste en el valor
predeterminado de Latet, escríbelo en el archivo.

- **El perfil trae lo que lo acompaña:** con `profile: max` también quedan bloqueados los números de identificación de
  todos los países, las direcciones IP privadas, el modo de datos estricto y la regla de max para las imágenes.
- **Las listas son un mínimo:** los términos, reconocedores, reglas de campo, tipos de datos personales, países y
  regiones telefónicas de las directrices, y los archivos y direcciones web que cubre el modo de datos estricto, están
  siempre en vigor, y los de cada persona se suman a ellos. En cambio, un término, un reconocedor o una regla de campo
  de un tipo que las directrices permiten (`policies: {CLIENT: allow}` y un término de tipo `CLIENT`) es una excepción
  de la empresa: no se añade a los ajustes de nadie, y una persona que enmascara ese valor con un término o un
  reconocedor propios lo sigue enmascarando.
- **Los reconocedores y las reglas de campo de la empresa se usan junto a los de cada persona:** los de la empresa
  nunca dejan fuera de uso un reconocedor o una regla de campo propios de una persona. Cuando un reconocedor de una persona
  tiene el nombre o el tipo de uno de la empresa, el de la empresa se usa con un nombre o un tipo propios; cuando una
  regla de campo de una persona tiene el nombre de una de la empresa, las dos se fusionan y el campo se enmascara.
- **Las indicaciones para el revisor local** que contienen las directrices se le dan siempre, antes que las de la
  persona.
- **También en cada proyecto:** Latet sigue activado en cada proyecto, y los ajustes propios de un proyecto nunca lo
  bajan del mínimo.
- **Nuevos miembros de las peticiones:** `unknown_members: refuse` rechaza, para cada persona y cada clave de API, un
  miembro de una petición que ningún perfil de petición nombra todavía, como hacía Latet antes de la 0.15.0, mientras que
  un texto o una lista de textos fuera de un elemento del proveedor sigue pasando, en una petición que Latet solo
  comprueba (Claude Code, Bedrock, Vertex), una vez que la comprobación final lo ha leído ([Nuevos miembros de las
  peticiones](../../getting-started/upgrading-to-0-11/#nuevos-miembros-de-las-peticiones)); `unknown_members: check` no
  bloquea nada, porque cualquiera puede rechazarlos.

`latet policy check policy.yaml` lee un archivo antes de ponerlo en vigor: qué bloquea, qué deja a cada persona, de
quién cuentan las excepciones y qué no se pudo leer en él. Un ajuste con un error, o escrito sin valor (`ocr:` solo),
no se lee, así que no bloquea nada. El comando no abre ninguna conexión, y termina con el código 0 cuando todo va bien,
1 cuando algo del archivo no se lee y 2 cuando el archivo no se puede leer en absoluto.

## Excepciones

Los valores y patrones que nunca se enmascaran, las reglas de secretos desactivadas y lo que deja valores en claro (un
reconocedor cuya política es `allow`, `allow` para un tipo en `policies`, una regla de campo con `mask: false`) son las
excepciones. La clave `exceptions` de las directrices dice de quién cuentan:

- **`personal`** (el valor predeterminado): cada persona puede seguir marcando un valor como **No sensible** para sí
  misma, y conserva sus propias elecciones `allow`, salvo para los tipos de los términos que la empresa enmascara.
- **`company`**: solo cuentan las excepciones de las directrices. Las propias de una persona se rechazan cuando las
  añade, y no se usan donde ya estaban: sus valores y patrones que nunca se enmascaran, sus reglas de secretos
  desactivadas, sus reconocedores que dejan en claro lo que encuentran, sus `allow` para un tipo que la empresa no
  permite y sus reglas de campo con `mask: false`. Una regla de campo propia de un tipo que la empresa permite
  (`policies: {URL: allow}` en las directrices, y una regla de campo suya de tipo `URL`) también es una excepción,
  porque los valores del campo quedarían en claro, incluso donde el modo de datos estricto los enmascararía: se
  rechaza cuando la añade, no se usa donde ya estaba, y **Reglas** dice cuántas de las suyas quedan fuera así. Donde un
  tipo así se enmascara en uno de sus proyectos, la regla de campo se conserva, y su tipo se enmascara allí donde
  habría dejado el campo en claro. Un término suyo de un tipo así se conserva, y se guarda cuando lo añade: nunca deja
  en claro nada que la empresa enmascare (un término de la empresa, lo que encuentran un reconocedor o una regla de
  campo de la empresa, los datos personales) ni el dominio de una dirección, allí donde el enmascaramiento lo
  enmascararía sin los términos propios de la persona. Entre sus propios términos gana la coincidencia más larga, como
  sin directrices. Su identidad (su nombre, sus nombres de usuario, sus direcciones de correo electrónico y sus nombres
  de host), de un tipo que la empresa permite, se conserva del mismo modo: nunca deja en claro lo que el
  enmascaramiento enmascararía sin ella. Donde su cuenta está vinculada al proveedor de identidad de la empresa (SCIM, o
  el inicio de sesión único) y la empresa deja pasar los nombres (`PERSON`), su nombre es el que da el proveedor donde
  sus propios ajustes también dejan pasar los nombres, en cada uno de sus proyectos; si no, su propio nombre sigue en
  uso, y se enmascara allí donde se enmascaran los nombres. En ambos casos, se rechaza cambiarlo desde sus ajustes; un
  equipo solo lo rellena donde no hay ninguno definido.

Los términos de la empresa de un tipo que no permite siguen enmascarados, tal como los escribió la empresa. Están
bloqueados en el propio enmascaramiento: un término se enmascara sea cual sea el valor que nunca se enmascara, el
patrón, el reconocedor o la política `allow` que una persona defina, y nadie puede marcar uno como **No sensible**.
Cuando una persona ha permitido un texto más largo que contiene un término, el resto queda como lo permitió, y el
término se enmascara. Un valor de `exceptions` que Latet no puede leer, o ningún valor (`exceptions:` solo), cuenta
como `company`, el más estricto, y `latet policy check` lo dice.

Con `personal`, solo los términos de la empresa están protegidos de este modo. Las excepciones propias de una persona
aún pueden dejar en claro lo que encuentra un reconocedor o una regla de campo de la empresa: un texto más largo que ha
permitido y que lo contiene, o un término, un reconocedor o una regla de campo suyos de un tipo que permite. Para
evitarlo, usa `exceptions: company`.

Las excepciones de petición (las [excepciones
exactas](../../getting-started/upgrading-to-0-11/#aprobar-una-excepción-exacta) que dejan pasar una cabecera, un
parámetro de consulta o un miembro del cuerpo) siguen la misma clave. Con `personal`, las de una persona son suyas: las
aprueba como sin directrices, y sobre las de una clave de API se decide como sobre sus otros cambios. Con `company` son
de la empresa: quien pide una la propone, y espera al equipo de seguridad en **Aprobaciones de la empresa**, decida
quien decida sobre el resto de sus cambios; su propia aprobación, o la de quienes controlan una clave de API, no concede
ninguna. Esto vale también para las aprobadas antes de que las directrices dijeran `company`, incluidas las permanentes:
desde que el servidor aplica las directrices no conceden nada, el panel muestra cada una como *No está en vigor: sobre
las excepciones de petición decide el equipo de seguridad de tu empresa, que no aprobó esta*, y `latet server status`
la lista como `forbidden` entre las excepciones que ahora no conceden nada, una vez que su espacio aplicó las
directrices: un espacio que no se ha abierto en ninguna instancia desde que cambiaron las directrices muestra allí sus
excepciones como estaban hasta que se vuelve a construir, mientras que sus peticiones ya se juzgan según las nuevas
directrices. Pedida de nuevo, una excepción espera al equipo de seguridad; esas excepciones cuentan de nuevo cada vez
que la empresa deja de decidir sobre ellas: cuando las directrices vuelven a `personal` o se quitan, cuando la licencia
se quita, o cuando se instala una licencia que no incluye las reglas bloqueadas. Sin las aprobaciones centrales en la
licencia nadie puede aprobar una: una excepción pedida espera una aprobación que solo puede rechazarla (quien la pidió
puede retirarla, y su propia aprobación se rechaza, con un aviso de que decide el equipo de seguridad), y no concede
nada. Para que las personas puedan tener excepciones de petición con `company`, la licencia debe incluir `approvals`;
sin ella, `company` sigue bloqueando todas las demás excepciones. Guardar los ajustes de una clave de API desde la
administración sigue aprobando las excepciones que añade. Con cualquiera de los dos valores, una excepción en espera no
concede nada y nunca se rechaza como si quedara por debajo de las directrices: lo que juzgan las directrices es su
aprobación, cuando llega.

## En un servidor de Latet

Con [Latet en un servidor](../../getting-started/server/), un administrador define las directrices desde cualquier
equipo que tenga el token de administración del servidor:

```sh
latet policy check policy.yaml
latet server policy set policy.yaml
```

`latet server policy set` lee primero el archivo, y no cambia nada si no se puede leer. Lo que no puede leer del
archivo lo dice antes de enviar nada, y pide confirmación: un ajuste con un error queda fuera entero, así que un solo
término mal formado deja fuera de las directrices todos los términos del archivo. También pide confirmación antes de
definir unas directrices que bloqueen menos ajustes que las del servidor, que permitan de nuevo a cada persona añadir
excepciones donde solo contaban las de la empresa, o que sustituyan unas directrices que no puede leer aquí; en un
terminal tuyo pide confirmación, y si no, `--yes` indica que estás de acuerdo. Sustituye por el archivo las directrices
con las que lo comparó: si alguien cambia entretanto las directrices del servidor, en la página **Directrices** o con
otro `latet server policy set`, el servidor rechaza el cambio, y el comando vuelve a leer las directrices, las compara
de nuevo y vuelve a pedir confirmación. Se rechaza a un agente de IA: definir o quitar las directrices corresponde a una
persona. El `PUT` de la API de administración rechaza un archivo con un ajuste que queda fuera entero (400, con el
nombre del ajuste), salvo que la petición diga también `"accept_notices": true`. Un `PUT` que
indica también `"etag"`, las directrices que sustituye tal como las dio el `GET` (`null` si no hay ninguna guardada),
o un `DELETE` cuyo cuerpo indica del mismo modo las directrices que quita, se rechaza con 409 cuando ya no son las
directrices guardadas.

Los ajustes de cada persona y de cada clave de API del servidor se suben al nivel de las directrices: cada instancia
sube cada espacio en cuanto recibe el aviso, y vuelve a comprobarlo cada 10 segundos, de modo que una instancia que
perdió el mensaje tiene las directrices en menos de 10 segundos. El servidor las guarda en su base de datos, con su texto sellado con la clave maestra (los
términos que contienen son valores), y registra cada cambio en el registro de administración, con los ajustes que
bloquea y nunca un valor. `latet server policy show` dice cuándo y por quién se definieron, qué definen y si están en
vigor, y `latet server policy show --file policy.yaml` vuelve a escribir su texto en un archivo, para editarlo.
`latet server policy remove` las quita, tras pedir confirmación: quita las directrices que leyó, y si alguien
cambia entretanto las directrices del servidor, el servidor rechaza la eliminación, y el comando vuelve a leer las
directrices y vuelve a pedir confirmación.

Con `roles` en la licencia, las personas con el rol Seguridad también las definen desde el panel
([Inicio de sesión, grupos y roles](../sign-in/)): en la página **Directrices** editan el texto de las directrices y
luego eligen **Guardar** o **Quitar**. Un texto que el servidor no puede leer se rechaza con el motivo, y las
directrices en vigor se mantienen. Un guardado indica las directrices que sustituye: si alguien las cambió desde que se
abrió su texto, se rechaza, y la página muestra las directrices como están ahora. El texto de las directrices contiene
los términos y los valores de la empresa, así que mientras el servidor pida el inicio de sesión de empresa
(`company_changes: sso`, el valor por defecto una vez configurado el inicio de sesión único), para leerlo y cambiarlo
hace falta una sesión abierta con la cuenta de empresa. Cada cambio queda en el registro de administración, a nombre de
la persona que lo hizo.

**Cada instancia aplica las directrices en cuanto se actualiza.** Una instancia de una versión anterior, o sin la
Enterprise Edition, no aplica directrices. Define las directrices cuando todas las instancias ejecuten esta versión, o
vuelve a definirlas en ese momento: `latet server policy set` y el `PUT` de la API de administración avisan, diciendo
cuántas instancias en marcha no las aplican. Véase
[Actualizar y añadir instancias](../../concepts/server-operations/#actualizar-y-añadir-instancias).

Mientras las directrices bloquean términos (alguno de sus términos de un tipo que no permiten) o dicen
`exceptions: company`, el servidor lee cada texto de una llamada a la [API para aplicaciones](../../reference/apps-api/) por
separado, como hacía Latet 0.10, mientras que de lo contrario lee juntos los textos de una llamada: el enmascaramiento es el
mismo, y una llamada con muchos textos tarda más. Los términos sincronizados hacen lo mismo mientras se enmascara alguno
([Sincronización de términos](../term-sync/)).

Los ajustes compartidos (`latet server config`, `latet server rules`) quedan como los define un administrador: lo que
hay en ellos por debajo de las directrices se sube en cada espacio, como los ajustes propios de una persona. Definir
los ajustes de una clave de API por debajo de las directrices se rechaza: `latet server config --key ... set` termina
con el código 2.

## Lo que ven las personas

- **En el panel**, un ajuste que define la empresa muestra un candado y **Definido por tu empresa** en **Reglas** y en
  las reglas de un proyecto. Una persona ve qué está bloqueado y cuántos términos, excepciones y reglas hay, por
  ejemplo que 2 términos de la empresa siguen enmascarados; nunca los términos, ni ningún otro valor de la empresa. En
  un servidor, la página **Directrices** es para las personas con el rol Seguridad, que leen y cambian allí el texto
  de las directrices, y para los auditores, que leen lo que bloquean; mientras la licencia no tenga `roles`, todos leen lo que bloquean. Los
  administradores leen las directrices con `latet server policy show`.
- **Un cambio rechazado nombra el ajuste:** `Set by your company, not changed: profile: from max to balanced`. El
  panel responde con un conflicto (409) y muestra el rechazo donde se hizo el cambio, y en el terminal el comando
  termina con el código 2. Un solo cambio que baja los ajustes de más de 20 carpetas de proyecto se rechaza entero:
  cambia menos carpetas a la vez.
- **Tus propios ajustes siguen como los definiste:** lo que está por debajo del mínimo se sube al mínimo mientras está
  en uso, y tus ajustes conservan lo que escribiste, así que valen tal como están escritos si se quitan las
  directrices.
- **`latet server status`** tiene una línea para las directrices: cuándo se definieron (en UTC), y un aviso cuando no
  están en vigor o no se pueden leer.

## La licencia

Las directrices están en vigor con una licencia de Latet Enterprise que incluye las reglas bloqueadas
([Ediciones y licencia](../)). La protección nunca baja: cuando la licencia vence, supera los usuarios comprados, está en pausa
o termina una prueba, las directrices siguen en vigor exactamente como están. Definir o quitar las directrices de un
servidor requiere una licencia que aún pueda gestionar los ajustes Enterprise: cuando termina una prueba, las
directrices quedan congeladas, y `latet server policy show` sigue mostrándolas, hasta que se instale una licencia.

Las directrices dejan de estar en vigor solo en dos casos: cuando se quita la licencia y cuando se instala una licencia
que no incluye las reglas bloqueadas. `latet server license remove` y `latet server license install` lo dicen, piden
confirmación antes y rechazan a un agente de IA; las directrices del servidor se conservan, y vuelven a estar en vigor
con una licencia que incluya las reglas bloqueadas.

## Los límites

- **Solo en un servidor de Latet:** la Enterprise Edition es propia de un servidor. Latet en un equipo sin servidor es
  la Community Edition, tenga lo que tenga instalado, y no tiene directrices.
- **Un equipo conectado a un servidor de Latet:** lo que envía lo enmascara el servidor, con las directrices del
  servidor.
- **Una instancia que no puede leer las directrices:** unas directrices guardadas pero ilegibles (creadas por una
  versión más reciente, o selladas con otra clave maestra) dejan a una instancia que ya había leído unas directrices
  aplicando las últimas que leyó: sus espacios siguen en ese mínimo más antiguo, que puede ser más bajo que el nuevo, y
  `latet server status` dice que las nuevas directrices no se pueden leer. Una instancia que nunca leyó unas
  directrices, o que aún no ha leído la licencia, no atiende a ninguna persona ni a ninguna clave de API hasta que
  pueda: la petición se rechaza con un 503 que dice que se vuelva a intentar. `latet server policy remove` repara unas
  directrices que no se pueden leer; volver a definirlas con una versión que ejecuten todas las instancias también las
  repara.
- **La base de datos es de confianza:** quien puede escribir en el PostgreSQL del servidor puede borrar la fila
  `ee_policy`, o volver a poner una más antigua. El mínimo entonces baja, o vuelve a las directrices más antiguas, en
  cada instancia, y ninguna entrada del registro de administración lo dice. Las entradas `policy.changed` del registro,
  cada una con el etag de las directrices definidas, son la referencia con la que comparar las directrices en vigor
  (`latet server policy show`); deja el acceso de escritura a esa base de datos solo al servidor.
- **El texto de las imágenes:** unas directrices que bloquean `terms` pero no `ocr` ni `images` dejan a una persona
  libre de desactivar la lectura del texto de las imágenes, y un término de la empresa dentro de una imagen no se lee
  entonces. Bloquea también `ocr: auto` e `images` para que los términos cubran las imágenes.
- **Un agente apuntado directamente a un proveedor de IA** no lo enmascara nadie. Un servidor es el punto de control de
  la empresa solo cuando su red permite llegar a los proveedores únicamente desde el servidor
  ([Latet en un servidor](../../concepts/server/)).
