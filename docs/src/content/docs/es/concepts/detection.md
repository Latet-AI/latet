---
title: Detección
description: Cómo reconoce Latet los secretos y los datos personales, por completo en tu equipo.
---

La detección se ejecuta en tu equipo, o en el servidor Latet al que está conectado tu equipo: nunca se
llama a ningún servicio de detección en la nube. Varios detectores leen cada fragmento de texto que lleva una petición. Esta página los
explica con ejemplos; la [referencia de los detectores](../../reference/detectors/) enumera todos
los detectores y todas las reglas de secretos.

## Secretos

Los secretos se enmascaran en todos los [perfiles](../profiles/).

Se incluyen las credenciales de Latet: `LATET_PERSONAL_TOKEN` (`latet_pat_`), `LATET_COMPUTER_KEY`
(`latet_ck_`), `LATET_API_KEY` (`latet_key_`) y `LATET_ADMIN_TOKEN` (`latet_adm_`),
cada prefijo seguido de 43 caracteres seguros
para URL (`A-Z`, `a-z`, `0-9`, `_`, `-`). En `https://<host>/u/<route-key>`, los 16 a 128 caracteres seguros para URL
de la clave de ruta se enmascaran como `LATET_ROUTE_KEY`; el host y los demás segmentos de la ruta
se conservan. Esto se aplica en tu equipo y en un espacio del servidor, incluidos los volcados del
entorno, el archivo `settings.json` de Claude Code y el archivo `config.toml` de Codex. Las llamadas
a herramientas locales recuperan los valores reales. Una llamada a una herramienta externa que
contiene un valor ficticio se detiene donde Latet puede interceptarla (consulta [Alcance de la protección](../protection-scope/)).

- **Formatos conocidos.** El conjunto de reglas predeterminado de
  [gitleaks](https://github.com/gitleaks/gitleaks) (222 reglas) viene incluido en Latet y se ejecuta
  sobre [RE2](https://github.com/google/re2), con las palabras clave, los umbrales de entropía y las
  listas de permitidos que usa gitleaks. Cubre las claves de API y los tokens de muchos proveedores,
  como GitHub, Anthropic, OpenAI, AWS, Stripe o Slack.
- **Valores asignados a nombres sensibles**, en archivos `.env`, en la configuración y en el código.
  Decide el nombre, tenga el valor el aspecto que tenga: `DB_PASSWORD=...`, `"api_key": "..."` y
  `$env:TOKEN = ...` se enmascaran; `APP_NAME=shop` y `DEBUG=true`, no. Los marcadores de posición
  como `changeme`, `${DB_PASSWORD}` o `<your-key>` se dejan tal cual, y también las referencias en
  el código como `process.env.API_KEY`.
- **Claves privadas**: todo el cuerpo de un bloque de clave privada PEM, OpenSSH o PGP. Las líneas
  `BEGIN` y `END` siguen visibles, así que el modelo sigue sabiendo que ahí hay una clave.
- **Credenciales**: la contraseña en `postgres://user:password@host/db` y en otras URL, los JSON Web
  Tokens y las credenciales `Bearer` o `Basic`. En una cadena de conexión (una variable
  `connectionString`, una columna `connection_string`) solo se enmascaran las credenciales: los
  valores de `Password=`, `PWD=`, `AccountKey=` y similares, o la contraseña de su URL. El servidor,
  el usuario y la base de datos se quedan, como en `Server=db;User Id=app;Password=…;`.

Por ejemplo, un archivo `.env` tal como está en tu disco:

```sh title=".env"
APP_NAME=shop
SMTP_HOST=mail.northwind-legal.com
SMTP_PASSWORD=hunter2hunter
PAYMENTS_API_KEY=pk_9f2c4e7a1b3d5f8e0a6c
```

y lo que recibe el modelo cuando el agente lo lee:

```sh
APP_NAME=shop
SMTP_HOST=mail.northwind-legal.com
SMTP_PASSWORD=jeeepo3ixhbkw
PAYMENTS_API_KEY=pk_3a8d1f6b0e9c2a7d4b5e
```

El nombre de host no es un secreto y se queda. La contraseña y la clave se sustituyen por valores
ficticios con la misma longitud y el mismo formato (los tuyos serán otros).

### Marcadores de posición

Un valor que solo ocupa el lugar de un secreto se deja tal cual: en una asignación, como contraseña
de una URL, en una columna secreta y como credencial de una cadena de conexión
(`User Id={{username}};Password={{password}};`). El valor entero debe ser uno de estos, sin
distinguir entre mayúsculas y minúsculas:

| Forma | Ejemplos |
| --- | --- |
| Entre paréntesis angulares o corchetes | `<password>`, `<your-key>`, `[password]` |
| Una variable o una plantilla | `${DB_PASSWORD}`, `$DB_PASSWORD`, `%DB_PASSWORD%`, `{{password}}`, `{env:DB_PASSWORD}`, `{file:./secret}` |
| *Your* y un nombre | `YOUR_PASSWORD`, `your-api-key`, `your.token` |
| Una palabra que representa un valor | `changeme`, `change_me`, `password`, `passwd`, `secret`, `token`, `placeholder`, `example`, `redacted`, `todo`, `tbd` |
| Relleno | `xxx`, `***`, `...`, `---`, `___` |
| Ningún valor | `null`, `none`, `nil`, `undefined`, `empty`, `true`, `false` |

`changeme123` no es un marcador de posición: podría ser una contraseña real. En una asignación,
también se deja tal cual una referencia en el código: `process.env.API_KEY`, `os.environ["TOKEN"]`,
`os.getenv(...)`, `config.`, `settings.`. Para dejar en claro un marcador de posición de otra forma,
añádelo a los valores que nunca se enmascaran ([listas de permitidos](../profiles/#listas-de-permitidos)), en
tus ajustes para un proyecto si pertenece a uno.

Tampoco se toca un valor hecho solo de símbolos, lo encuentre quien lo encuentre y sea cual sea su
longitud: `!!!!!!!!` como contraseña, `--------------------` después de `Bearer`, una coincidencia de
uno de tus [reconocedores](../custom-recognizers/) o un valor que señala el
[revisor local](../llm-review/). El valor ficticio conserva los símbolos del original, así que sería
el propio original. Una clave de ruta de Latet es la excepción: cada carácter de su valor ficticio
se genera de nuevo, incluso cuando el original solo contiene `_` o `-`.

## Datos personales

Los datos personales se enmascaran en los perfiles `balanced` y `max`. Cada coincidencia se valida
antes de enmascararla, lo que reduce las falsas alarmas en el código:

| Tipo | Ejemplo | Cómo se confirma una coincidencia |
| --- | --- | --- |
| Correo electrónico | `emily.carter@northwind-legal.com` | El dominio de primer nivel se comprueba con la lista de la IANA, incluida para funcionar sin conexión |
| Número de teléfono | `+44 20 7946 0958` | Se analiza y se valida con [phonenumbers](https://github.com/daviddrysdale/python-phonenumbers), una adaptación de libphonenumber de Google |
| IBAN | `GB82WEST12345698765432` | La longitud según el país y la suma de control mod-97 |
| Número de tarjeta | `4111 1111 1111 1111` | El prefijo del emisor y la suma de control de Luhn |
| Número de identificación nacional y número fiscal | `SSN NNN-NN-NNNN`, `DNI NNNNNNNNA` | Dígitos de control, y una palabra clave cuando el número por sí solo podría ser cualquier cosa (más abajo) |
| Número de IVA | `DENNNNNNNNN`, `CHE-NNN.NNN.NNN MWST` | El prefijo del país y los dígitos de control de ese país |
| Dirección IP | `151.101.1.69` | Direcciones públicas; las privadas, solo en `max` o con `include_private_ips` |
| Dirección de Bitcoin | `bc1q...` | La suma de control Base58Check o bech32 |

Los reconocedores de correos electrónicos, IBAN, tarjetas, codice fiscale y partita IVA están
portados de [Presidio](https://github.com/data-privacy-stack/presidio).

### Números de identificación y fiscales por país

Latet conoce los números de identificación nacional y los números fiscales de 15 países: Alemania,
Bélgica, Canadá, España, Estados Unidos, Finlandia, Francia, Italia, Letonia, Países Bajos,
Polonia, Portugal, Reino Unido, Suecia y Suiza. `id_regions` en `config.yaml` elige los países (`IT`,
`US` y `GB` de forma predeterminada); `latet global-install` propone el tuyo, y el perfil `max` los
incluye todos. La página **Reglas** del panel enumera los números de cada país.

Muchos de estos números son simples secuencias de nueve u once dígitos, como infinidad de
identificadores en el código. Por eso cada uno se confirma con sus dígitos de control, y los que aun
así podrían ser cualquier cosa necesitan una palabra clave cerca: *SSN* o *social security* junto a
un número de la Seguridad Social estadounidense, *BSN* junto a un número de servicio al ciudadano
neerlandés, *Steuer-ID* junto a un número de identificación fiscal alemán. La palabra clave cuenta
en la misma línea, a pocos caracteres de distancia, o en el encabezado de la columna en la que está
el número (tablas CSV, TSV y Markdown):

```text
name,ssn,city
Jane,NNN-NN-NNNN,Boston        ← found: the column is called ssn
```

El DNI y el NIE españoles, el número AHV suizo y el código de identidad personal finlandés llevan una
letra de control o una estructura característica, así que no necesitan palabra clave. Nunca se toma
un número del interior de un número más largo, de una versión, de un decimal o de una ruta. La
[referencia de los detectores](../../reference/detectors/) tiene la lista completa, con la estructura
y la comprobación de cada número.

Los números de IVA se reconocen en los 27 países de la UE, Irlanda del Norte, el Reino Unido, Suiza
y Noruega, diga lo que diga `id_regions`: siempre llevan el prefijo de su país.

Algunas cosas se dejan tal cual a propósito:

- las direcciones de correo electrónico de los dominios de documentación `example.com`,
  `example.net` y `example.org`, o bajo `.example`, `.test`, `.invalid` y `.localhost`, y los
  remitentes de sistema como `git@` o `noreply@`;
- en el código y en la salida de las herramientas, las secuencias de dígitos sin `+`, `00`, espacios
  ni paréntesis, que con mucha más frecuencia son identificadores, fechas o números de compilación
  que números de teléfono. En tus propios mensajes las reglas son más flexibles. Los números sin
  prefijo internacional solo cuentan para los países de `phone_regions` (`IT`, `US` y `GB` de forma
  predeterminada);
- los números de versión que parecen direcciones IP, como `v1.2.3.4`.

### Nombres

Los nombres en texto libre, como en *Emily Carter escribió esto*, necesitan un modelo estadístico.
Con el [modelo de nombres local](../../getting-started/installation/) opcional instalado, Latet
encuentra por sí mismo los nombres y las direcciones postales; el [revisor local](../llm-review/),
un modelo de lenguaje en tu equipo o en un servidor tuyo, también los encuentra, junto con los datos
de salud y los demás valores que ningún patrón describe. Sin ninguno de los dos, un nombre solo se
enmascara cuando es tu propio nombre de autor de git (más abajo), uno de tus propios términos o un
valor que ya está en la caja fuerte.

Por ejemplo, este prompt:

```text
Hi, I'm Emily Carter and I live at 42 Mill Lane, Bristol BS1 4DJ.
```

Con el modelo de nombres, en el perfil `balanced`, el modelo en la nube recibe una versión como
esta:

```text
Hi, I'm Hannah Mitchell and I live at 45 Moore Lane, Derby YN4 9CP.
```

Tu agente sigue viendo el nombre y la dirección reales cuando vuelve la respuesta.

Lo que busca el modelo de nombres depende del [perfil](../profiles/):

- **`balanced`**: nombres completos (dos palabras o más) y direcciones postales, en lo que escribes
  y adjuntas y en el texto leído en las imágenes. Una ciudad o un código postal solo cuentan junto a
  una calle. El código, la salida de las herramientas y el texto de sistema no se leen.
- **`max`**: también los nombres de una sola palabra y las ciudades, y además el texto de sistema y
  la salida de las herramientas.
- **`secrets`**: nunca.

Una vez encontrado, un nombre pasa a los [valores conocidos](#valores-conocidos) y desde entonces se
enmascara en todas partes, salida de las herramientas incluida, en todos los perfiles, `secrets`
también.

El modelo no ofrece garantías:

- **Se le escapan nombres.** Los nombres poco comunes, las iniciales y los nombres escritos en
  minúsculas son los que más se le escapan. Los saludos y los tratamientos delante de un nombre
  (*Hi Paul*, *Dr. Sarah Mitchell*) quedan fuera de lo que se enmascara.
- **Confunde algunas palabras con nombres.** Las palabras técnicas como *Redis* o *Postgres* se
  filtran, y `balanced` necesita al menos dos palabras.
- **Es lento con los textos largos.** Lee unos 700 tokens por segundo en la CPU de un ordenador de
  sobremesa, en un hilo propio, así que las demás peticiones no lo esperan. Un texto de más de
  4096 tokens (8192 en `max`), o de 16.000 caracteres (64.000 en `max`), no se lee, y la petición
  queda marcada en el panel.

## Tus propios términos

Añade las palabras que solo tú sabes que son sensibles: nombres de clientes, nombres en clave de
proyectos, dominios internos. Se enmascaran en todos los perfiles, dondequiera que aparezcan:

```yaml title="config.yaml"
terms:
  - value: Project Falcon
    type: PROJECT
  - value: Acme Logistics
    type: CLIENT
```

Con estos términos, *"Project Falcon se lanza con Acme Logistics en mayo"* llega al modelo como algo
parecido a *"Consolidated Margie se lanza con Northwind Adatum en mayo"*. De forma predeterminada,
un término coincide sin distinguir entre mayúsculas y minúsculas, y solo como palabra completa;
`case_sensitive` y `whole_word` cambian eso. También puedes añadir términos en la página **Reglas**
del panel, o con **Enmascarar siempre** en cualquier valor de una petición.

Un término necesita al menos una letra o un dígito: el valor ficticio conserva los símbolos del
original, así que unos símbolos solos, como `--` o `###`, no se pueden enmascarar. El panel y
`latet config` rechazan un término así. Uno escrito a mano en `config.yaml` o en el `latet.yaml` de
un proyecto se deja fuera, y la página **Reglas** y `latet config validate` lo señalan.

Un término tiene como máximo 8.192 caracteres: uno más largo se busca aparte, y uno de alrededor de
un millón de letras no se puede buscar en absoluto. El panel, `latet config` y una importación
rechazan un término más largo. Uno escrito a mano en `config.yaml` se deja fuera, y `latet reload`,
el registro del daemon y `latet config validate` dicen qué entrada; uno en el `latet.yaml` de un
proyecto también se deja fuera, y la página del proyecto lo dice.

Para un formato en lugar de un valor, como todos los números de acreditación escritos `AB-123456`,
escribe un [reconocedor personalizado](../custom-recognizers/): un patrón, dígitos de control y
palabras clave, probado con ejemplos inventados.

## La identidad de este equipo

En `balanced` y `max`, Latet detecta quién eres y dónde estás cada vez que se inicia, y lo
enmascara:

- tu nombre de usuario del sistema operativo dentro de las rutas del directorio personal:
  `C:\Users\anna\shop\app.py`, `/home/anna/...` o `/Users/anna/...`. Un nombre de usuario de seis
  caracteres o más se enmascara dondequiera que aparezca;
- tu nombre de host;
- el nombre y el correo electrónico de tu autor de git, tomados de `git config --global`, y el
  dominio de ese correo cuando es el de tu empresa (`studio-rossi.it`, no `gmail.com`), dondequiera
  que aparezca: también se enmascara `git.studio-rossi.it`.

Como se detectan de nuevo en cada inicio, un correo electrónico de git o un nombre de usuario nuevos
se enmascaran sin que tengas que hacer nada. La sección `identity` de la
[configuración](../../reference/configuration/) lo desactiva o fija los valores: una vez que los
escribes ahí, Latet usa esos.

## Valores conocidos

Una vez detectado un valor, Latet lo recuerda en la caja fuerte. Las apariciones posteriores se
enmascaran incluso donde los detectores no las reconocerían por sí solos: por ejemplo, la contraseña
de tu `.env` mencionada más tarde en un mensaje de commit. Dos clases no se recuerdan de este modo: un
valor que el modo de datos estricto o una regla de campo enmascara en una tabla o en un JSON se
queda enmascarado solo en su estructura ([datos de producción](../production-data/#modo-de-datos-estricto)),
y un valor de un tipo con la política `redact` no se guarda en absoluto. Los valores conocidos son
también lo que busca la comprobación final en las peticiones salientes.

Un valor se vuelve a reconocer a partir de cuatro caracteres, el nombre de una persona a partir de
tres (*Ugo*, *Ada*), y solo tal como está escrito: *Ugo* no es *ugo*, ni parte de *Ugolino*.

Un valor también se encuentra donde está escrito con secuencias de escape: como `\u00f2` o
`\\u00f2` en un JSON que Latet no lee como documento (un bloque de código, líneas JSON, un documento
guardado en una cadena), como bytes en un registro (`\xc3\xb2`) o en una ruta que git pone entre
comillas (`\303\262`), codificado en una URL (`%C3%B2`, `%20`, o `+` en una consulta o un
formulario), o como referencia de carácter HTML (`&#242;`, `&ograve;`). Lo mismo vale para tus
términos y la identidad de este equipo y, donde una secuencia escribe un carácter, para los datos
personales y los valores de tus reconocedores, así que también se encuentra una dirección escrita
`mario.rossi%40acme.it` (un secreto o un valor de una estructura solo se encuentra tal como está
escrito). Un valor encontrado así se guarda tal como está escrito, como un valor propio con su
propio valor ficticio, escrito con las mismas secuencias donde forman parte del texto:
`Paolo%20Fontana` en un enlace, `\'` en una cadena entre comillas simples. Al agente le vuelve el
valor exactamente como estaba escrito, así que un enlace sigue siendo un enlace y una edición vuelve
a encontrar su línea. Donde el modelo escribe el valor ficticio tal como se lee, `Paolo Fontana`, al
agente le vuelve el valor tal como se lee. La caja fuerte guarda cómo se encontró cada valor, tal
como está escrito o donde se leen sus secuencias de escape, así que esto vale para un valor de
cualquier tipo encontrado de ese modo: un número de teléfono conocido como `333 1234567` y hallado
como `333+1234567` en una consulta vuelve también como `333 1234567`.

## Cuando dos detectores encuentran el mismo texto

Las coincidencias que se solapan se unen en una sola que las abarca todas, así que un valor nunca se
sustituye a medias. El tipo más fuerte da nombre al resultado: primero los secretos, después tus
términos y la identidad de este equipo, y después los datos personales. Tus términos y los valores
conocidos que se solapan entre sí también se encuentran todos: si se conocen tanto *Anna Maria* como
*Maria Rossi*, el nombre *Anna Maria Rossi* se enmascara entero. Los solapamientos se buscan hasta un
límite en cada texto: más allá de él, el resto del texto se enmascara como antes, un valor tras otro.

Si falla cualquier detector distinto del de datos personales y del modelo local de nombres (secretos,
tus términos, identidad, valores conocidos), el enmascaramiento falla y la petición se bloquea: nada
sale sin comprobar. Si falla el detector de datos personales o el modelo local de nombres, se omite y
la petición queda marcada en el panel.

## Por qué RE2

RE2 busca coincidencias en tiempo lineal, así que una entrada hostil, o simplemente enorme, no puede
provocar un retroceso catastrófico de las expresiones regulares y dejar bloqueado a tu agente.

## Falsos positivos y falsos negativos

Todo detector busca un equilibrio entre los valores que se le escapan y las falsas alarmas. Los
[perfiles](../profiles/) eligen dónde situarse en ese equilibrio, y las
[listas de permitidos](../profiles/#listas-de-permitidos) corrigen falsas alarmas concretas. Para ver cómo se
enmascararía un texto, pégalo en el **Banco de pruebas** del panel: nada de lo que escribas ahí se
guarda ni se envía a un modelo.
