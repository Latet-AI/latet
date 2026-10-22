---
title: Tutorial
description: Mira Latet en acción en tu equipo en unos 30 minutos, con un agente simulado, un modelo simulado y datos inventados, antes de conectarle un agente real.
---

`latet tutorial` te muestra Latet en acción en tu navegador, en unos 30 minutos, antes de que le
conectes un agente real. Un agente simulado envía sus peticiones a un modelo simulado. Latet, entre
ellos, es el de verdad: el mismo enmascaramiento, las mismas comprobaciones y el mismo panel que
tienes con tu propio agente. Los datos son inventados, y no sale nada de tu equipo.

## Inícialo

```sh
latet tutorial
```

Abre el tutorial en tu navegador, en el menú de capítulos, y se ejecuta hasta que lo terminas. El
terminal indica dónde está:

```text
Tutorial at http://127.0.0.1:52817 — Ctrl+C ends it
```

| Comando | Qué hace |
| --- | --- |
| `latet tutorial` | Abre el menú de capítulos. |
| `latet tutorial terms` | Abre un capítulo por su identificador: aquí, *Tus propios términos*. |
| `latet tutorial --list` | Muestra los capítulos, su duración en minutos y cuáles has terminado, y después se detiene. |
| `latet tutorial --print-url` | Muestra el enlace de inicio de sesión en lugar de abrir el navegador. Funciona una sola vez, durante dos minutos. |
| `latet tutorial --keep` | Conserva la carpeta del entorno aislado cuando termina el tutorial, e indica dónde está. |

El tutorial se ejecuta en tu equipo, no en el [contenedor](../docker/) de Latet.

## Los capítulos

Cada capítulo tiene unos pocos pasos. Envías al agente un prompt, ya escrito para ti, y el recuadro
de Latet muestra lo que le ha pasado a su petición, con las propias vistas del panel. Algunos pasos te
piden que actúes ahí: hacer clic en un valor, añadir una palabra a tus términos, aprobar una petición,
guardar tus indicaciones.

| Capítulo | Minutos | Qué ocurre |
| --- | --- | --- |
| `basics`: Lo básico | 5 | Una clave en `.env`, una dirección de correo electrónico y un número de teléfono salen como valores ficticios; el archivo que escribe el agente contiene la clave real. |
| `terms`: Tus propios términos | 3 | Un nombre en clave sale en claro hasta que lo añades a tus términos; a partir de entonces el modelo recibe un valor ficticio, un nombre que nunca ha visto. |
| `false-positives`: Un valor enmascarado sin necesidad | 3 | La tarjeta de prueba de Stripe se enmascara como la tarjeta de un cliente; *No sensible* la deja pasar. |
| `stopped`: Una petición detenida | 3 | Una clave pegada en una conversación de antes de que Latet estuviera activo detiene la petición; una conversación nueva sin ella pasa. |
| `live`: El Revisor en directo | 3 | Una petición espera tu aprobación: enmascaras a mano el nombre de un cliente y después la apruebas. |
| `settings`: Un agente que relaja tus reglas | 3 | El agente pide a Latet que deje pasar las direcciones de correo electrónico, y el cambio te espera: apruébalo o deshazlo. |
| `data`: Datos de producción | 4 | El modo de datos estricto enmascara todos los valores de la tabla de una consulta; tú dejas en claro los identificadores, las fechas y los planes. |
| `llm`: El revisor local | 4 | Una incidencia contiene una dirección de correo electrónico escrita con palabras, un IBAN con una errata, un nombre y un diagnóstico: ningún patrón los encuentra, el revisor local sí. Después, con tus indicaciones, encuentra también las unidades del hospital. El revisor es simulado: es el Tutorial Reviewer. |
| `images`: Capturas de pantalla | 3 | El agente envía una captura de pantalla de un pago rechazado: Latet lee en ella la dirección de correo electrónico, el IBAN y el número de teléfono, y cubre cada uno con su valor ficticio antes de que salga la imagen. El capítulo necesita reconocimiento de texto: `latet ocr install`. |

Un capítulo termina con lo que has aprendido y dónde está en el panel. El último termina con cómo
usar Latet con tu propio agente. El menú marca los capítulos que has terminado, de una ejecución a la
siguiente, y *Empezar de nuevo* deja un capítulo como estaba al principio.

## Un entorno aislado

Los capítulos enmascaran valores, detienen peticiones y cambian ajustes, así que se ejecutan en un
Latet propio, en un entorno aislado:

- **Sus propios datos, ajustes e historial**, en una carpeta temporal, y su propio servidor en
  `127.0.0.1`, en un puerto libre. Tu daemon no se toca, y no hace falta que esté en ejecución.
- **No cambia nada tuyo:** tus ajustes, tu caja fuerte y tu historial, y el inicio de sesión de tu
  panel (la página del tutorial inicia sesión con una cookie propia).
- **No sale nada de tu equipo.** La única ruta del entorno aislado lleva al modelo simulado, que se
  ejecuta dentro del comando `latet tutorial`: ninguna petición puede llegar a un proveedor, y no
  hace falta ninguna clave de API ni ningún inicio de sesión.
- **Los mismos valores ficticios en cada ejecución:** el entorno aislado los crea a partir de una
  clave fija, no de la tuya.
- **Un solo archivo fuera de él:** `tutorial.json`, en tu
  [carpeta de datos](../uninstall/#3-borra-los-datos-locales-opcional), con los identificadores de los
  capítulos que has terminado.

El entorno aislado no tiene detección de nombres ni nada de tu identidad: los capítulos muestran lo
que encuentran los detectores, y lo que enmascaras a mano. En el capítulo sobre el revisor local, el
revisor es el Tutorial Reviewer: simulado, encuentra lo que dice el capítulo, y ninguna petición
llega a ningún servidor. En el capítulo sobre las capturas de pantalla, el entorno aislado lee el
texto de las imágenes con tus propios modelos de reconocimiento de texto, una vez que estén
instalados (`latet ocr install`). Todas las demás páginas de su panel están ahí para explorarlas,
con una barra arriba que te devuelve al capítulo.

## Termínalo

Haz clic en *Terminar el tutorial* en la página, o pulsa Ctrl+C en el terminal: el entorno aislado
se detiene, y su carpeta se borra. Una ejecución interrumpida de otra forma, por ejemplo cerrando su
terminal, deja la carpeta en tu carpeta temporal, y el siguiente `latet tutorial` la borra. Una
ejecución que se detiene por un error muestra las últimas líneas de su registro, y conserva la
carpeta, con el registro completo, hasta la siguiente ejecución.

## En tu propio panel

Lo que muestran los capítulos, en el panel de tu propio Latet (`latet dashboard`):

| Qué | Dónde |
| --- | --- |
| Cada petición: el texto de tu equipo, y lo que recibió el modelo | **En directo**, y después la petición |
| Cada valor que ha sustituido Latet, y su valor ficticio | **Caja fuerte** |
| Tus términos | **Reglas** → **Términos** |
| Los valores que dejas pasar | **Reglas** → **Valores permitidos** |
| Las peticiones que ha detenido Latet, y por qué | **En directo**, con el filtro *Bloqueada*, y después la petición |
| Lo que espera tu aprobación: peticiones, y cambios en tus ajustes | **Aprobaciones** |
| El Revisor en directo, para todos los proyectos o para uno | **Reglas** → **Revisores** → **Revisor en directo** |
| El modo de datos estricto, y las columnas que dejas en claro | **Reglas** → **Protección** → **Datos de producción** |
| Lo que ha encontrado el revisor local en una petición | **En directo**, y después la petición: *Revisor local* |
| El revisor local, y tus indicaciones | **Reglas** → **Revisores** → **Revisor local** |
| Una imagen incluida en una petición, tal como se envió, y el texto leído en ella | **En directo**, y después la petición: *Imágenes* |
| Lo que sale con una imagen o un PDF, y si se lee el texto de las imágenes | **Reglas** → **Protección** → **Datos de producción**: *Imágenes y PDF* |

Después pruébalo con tu propio agente, en una sesión, con una clave inventada: consulta el
[Inicio rápido](../quickstart/).
