---
title: Exportar e importar
description: Tus reglas en un archivo para un compañero, y toda tu instalación en un archivo cifrado para otro equipo.
---

Hay dos cosas distintas que pueden viajar:

- **Tus reglas, para un compañero:** términos, reconocedores, políticas y lo demás, en un archivo
  YAML. Al importarlas, se suman a las suyas.
- **Tu instalación, para otro equipo:** tus ajustes y, si lo pides, la caja fuerte con su clave, en
  un único archivo cifrado.

## Tus reglas, para un compañero

```sh
latet rules export acme-rules.yaml     # here
latet rules import acme-rules.yaml     # on your colleague's machine
```

O **Exportar reglas** e **Importar reglas**, en la parte de arriba de **Reglas** en el panel: la
importación muestra lo que haría antes de que cambie nada. Las tablas de *Términos*,
*Reconocedores* y *Valores permitidos* tienen su propio *Exportar* e *Importar*, solo para esa
lista: los términos y los valores en CSV, los reconocedores como archivo de reglas. Una exportación
así tiene las mismas cautelas: un reconocedor cuyos ejemplos contienen un valor que Latet ha visto
de verdad se queda fuera, y una celda CSV que una hoja de cálculo ejecutaría como fórmula se
escribe tras un apóstrofo. Una importación así pasa por la misma vista previa, añade y nunca
sustituye, y retiene del mismo modo lo que baja la protección; la tabla de *Reconocedores* acepta
un archivo solo de reconocedores.

**Qué contiene el archivo:**
- los términos, los reconocedores, las políticas y las reglas de campo;
- los tipos de datos personales, y los países cuyos números de identificación y de teléfono se
  reconocen;
- el perfil, las direcciones IP privadas, la detección local de nombres, el modo de datos estricto
  con los archivos y las direcciones web que cubre, la protección contra volcados, las imágenes y el
  reconocimiento de texto;
- las excepciones: los valores y patrones que no se enmascaran nunca, las reglas de secretos
  desactivadas, las herramientas que cubre el modo de datos estricto;
- las [indicaciones](../llm-review/#tus-indicaciones) para el revisor local.

Nunca la identidad, el puerto, las carpetas de proyectos ni los servidores de este equipo.

Un archivo de reglas contiene como máximo 2 MB. Pasados 5.000 términos, 500 reconocedores, 500 reglas
de campo, 5.000 valores o 500 patrones que no se enmascaran nunca, solo se leen los primeros, y la
importación lo dice. Un archivo hecho por un Latet más reciente se rechaza.

**Los términos son valores en claro.** El nombre de un cliente que esté entre tus términos aparece
en el archivo tal cual: envía el archivo como enviarías los propios nombres. Un reconocedor cuyos
ejemplos contengan valores que Latet ha visto de verdad se deja fuera, ya que los ejemplos deben ser
inventados.

**Una importación añade protección, y eso se aplica al momento:**
- los términos, reconocedores y reglas de campo nuevos se añaden, y los tuyos se quedan;
- se adopta un valor más estricto que el tuyo: un perfil más alto, `redact` para un tipo que
  enmascaras;
- un reconocedor con el nombre o el tipo de uno de los tuyos, o una regla para un campo para el que
  ya tienes una, se deja fuera: se conserva lo tuyo.

**Lo que reduciría la protección se enumera y se deja fuera:** las excepciones, un valor más débil,
las indicaciones del revisor, un reconocedor que deja pasar lo que encuentra (`policy: allow`), un
campo que se deja en claro. `--all`, o *Aplicar también estas* en el panel, también los aplica. Si
la importación la ejecuta un agente o un script, `--all` convierte toda la importación en un solo
cambio, que espera tu aprobación como cualquier
[cambio que reduce la protección](../agents-and-settings/): lo que añade protección espera con él,
y nada de la importación se aplica hasta que lo apruebes. `--dry-run` solo indica lo que haría una
importación.

```text
$ latet rules import acme-rules.yaml
Would add:
  terms: Fonderia Brambilla, Banca Ligure
  recognizer badge-number
  policy for IBAN: redact (yours: mask)
Yours already, or kept as yours:
  recognizer ticket: yours, with the same name, is kept
Left out, it would lower protection:
  values never to mask: acme-dev
--all applies them too: they wait for your approval.
saved C:\Users\you\AppData\Roaming\latet\config.yaml
Rules reloaded.
```

**Lo que se ha aplicado lo dice la respuesta del daemon.** El comando indica primero lo que
añadiría, luego guarda `config.yaml`, pide al daemon que lo vuelva a cargar e indica cómo ha ido:
*Rules reloaded.* (código de salida `0`), un cambio que espera tu aprobación (`3`) o, si el daemon no
ha respondido, que el archivo está guardado y que no está confirmado que se aplique (`1`:
`latet reload` lo aplica cuando el daemon esté en marcha). El comando no lee los repositorios con un
`latet.yaml` propio que Latet ha visto: el daemon pesa la importación en ellos al volver a cargar, y
una adición que reduce la protección en uno de ellos también espera tu aprobación.

También se puede importar un `config.yaml` o el `latet.yaml` de un proyecto: lo que no es una regla
(el puerto, la identidad...) se deja fuera, y la importación lo indica.

### Las reglas de un proyecto

Las reglas compartidas de un proyecto ya viajan con él: su [`latet.yaml`](../projects/), que el
panel escribe cuando las cambias, está en el repositorio, y quien lo clona las tiene.

Tus propios ajustes para un proyecto, como los nombres de clientes que mantienes fuera del
repositorio, o sus excepciones, van en un archivo aparte:

```sh
latet rules export shop-rules.yaml --project C:\dev\shop
latet rules import shop-rules.yaml --project D:\work\shop
```

En el panel también están en la página **Reglas** de un proyecto, bajo
*Tus ajustes, para este proyecto*: *Exportar estos ajustes* e *Importar ajustes*.

### En un servidor Latet

En un equipo conectado a un [servidor Latet](../server/), tus reglas están en tu espacio en el
servidor, y `latet rules export` e `import` se niegan a ejecutarse. **Exportar reglas** e
**Importar reglas**, en la parte de arriba de **Reglas** en tu panel del servidor, funcionan como
arriba, sobre tus propias reglas: un equipo de trabajo comparte sus reglas como archivo, y cada
persona lo importa. El `latet.yaml` de un proyecto no se aplica allí: impórtalo de la misma manera
para usar sus reglas.

Un administrador exporta e importa las reglas de una clave de API:

```sh
latet server --server https://latet.acme.lan rules --key "Billing CI" export billing-rules.yaml
latet server --server https://latet.acme.lan rules --key "Billing CI" import team-rules.yaml
```

Las reglas establecidas para todos en un servidor, que nadie puede relajar, están previstas para la
Enterprise Edition.

## Tu instalación, para otro equipo

```sh
latet backup latet.backup --vault    # here
latet restore latet.backup           # on the other machine
```

`latet backup` pide dos veces una frase de contraseña (al menos 12 caracteres: unas cuantas palabras
son fáciles de recordar y difíciles de adivinar), y escribe un único archivo:

| | Sin `--vault` | Con `--vault` |
| --- | --- | --- |
| Tus ajustes (`config.yaml`) | sí | sí |
| Los certificados en los que confía el revisor local | sí | sí |
| La caja fuerte y su clave raíz | no | sí |
| El historial del panel | no | con `--history` |

- **Con la caja fuerte**, el otro equipo genera los mismos valores ficticios que este, recuerda todos
  los valores encontrados hasta ahora (por los detectores, por el revisor local, por ti en el
  Revisor en directo) y sigue restaurando las conversaciones iniciadas aquí. El archivo contiene
  entonces tus valores reales, cifrados: guárdalo donde guardarías esos valores.
- **Sin ella**, el otro equipo genera sus propios valores ficticios.
- **Nunca en él:** la clave de API del revisor local, el token del puente (entre el plugin de
  Claude Code y el daemon), los registros, el modelo local de nombres, los modelos de reconocimiento
  de texto y los ajustes de tus agentes. Cada equipo tiene los suyos.

`latet restore` primero lee y comprueba el archivo entero, muestra lo que contiene y qué de sus
ajustes reduce la protección en comparación con los tuyos, y te pregunta. Después:

- **Este equipo conserva lo suyo:** su puerto, su identidad (que se añade a la de la copia de
  seguridad, para que se enmascaren las dos), y nada de `capture_dir` ni de `api_key_file`, que son
  rutas del otro equipo.
- **Carpetas de proyectos en otro sitio:** `--paths C:\Users\anna\dev=D:\work` traslada las que
  están bajo la primera carpeta a la segunda. Se enumeran las carpetas que no están aquí.
- **Ya hay una caja fuerte aquí:** si la caja fuerte de este equipo tiene valores propios, solo se
  sustituye con `--replace`. Antes se guardan en `before-restore-<date>.latet`, junto a la
  caja fuerte, con la misma frase de contraseña, para que `latet restore` pueda recuperarlos.
- **El daemon** se detiene antes y se vuelve a iniciar después, y nada lo inicia mientras hay una
  restauración en curso. Una restauración interrumpida (un fallo, un corte de luz) lo deja detenido
  hasta que `latet restore --abort` deja las cosas como estaban.
- **Lo que tenía este equipo** (la caja fuerte, el historial, `config.yaml`) se queda en una carpeta
  `before-restore-` con fecha, junto a la caja fuerte.

Después: `latet llm key` para la clave de API del revisor, `latet ner install` para la detección
local de nombres, `latet ocr install` para el texto de las imágenes (hasta entonces las imágenes se
envían tal cual, marcadas *No inspeccionado*, o no se envían en absoluto con `images: mask`), y
`latet global-install` (o `latet claude`) para tus agentes.

**Solo en tu terminal.** Los dos comandos se niegan a ejecutarse dentro de un agente: manejan tu
caja fuerte y su clave. `latet restore` también necesita un terminal para preguntarte, y se niega
cuando la clave raíz viene de `LATET_ROOT_KEY` o de `LATET_ROOT_KEY_FILE`, que una clave restaurada
no podría sustituir.

Sin la frase de contraseña nadie puede leer el archivo, ni siquiera tú. Un archivo modificado,
recortado o montado con trozos de otros se rechaza antes de usar nada de su contenido: consulta
[Seguridad](../../security/#cómo-se-protege-latet).

### En Docker

La caja fuerte y su clave están en el volumen `latet-data`, y los ajustes en `latet-config`:
copiando los dos volúmenes se traslada todo. Con los comandos:

```sh
docker exec -it latet latet backup /data/latet.backup --vault
docker cp latet:/data/latet.backup .
```

En el otro equipo, con su contenedor detenido:

```sh
docker compose stop
docker cp latet.backup latet:/data/
docker compose run --rm -it latet latet restore /data/latet.backup
docker compose start
```

Si el contenedor toma su clave de `LATET_ROOT_KEY_FILE`, `latet restore` se niega: mueve en su lugar
los volúmenes `latet-data` y `latet-config` y el archivo de la clave.
