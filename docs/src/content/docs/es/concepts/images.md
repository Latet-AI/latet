---
title: Capturas de pantalla e imágenes
description: El texto de una imagen, leído en tu equipo, enmascarado como cualquier otro texto y cubierto en la imagen con su valor ficticio.
---

Una captura de pantalla de un terminal, de un formulario o de una factura contiene los mismos valores
que el texto que la rodea: una dirección de correo electrónico, un IBAN, el nombre de un cliente.
Con el reconocimiento de texto instalado, Latet lee en tu equipo el texto de cada imagen que lleva
una petición, lo enmascara como cualquier otro texto y, encima de cada valor que encuentra, pinta su
valor ficticio. El modelo lee el valor ficticio en la imagen; cuando lo vuelve a escribir, Latet
restaura el valor real, como hace con el texto.

## Activarlo

```sh
latet ocr install   # downloads the models once (about 31 MB)
```

- Los modelos funcionan con el extra `ocr`, ONNX Runtime y Pillow, que los instaladores instalan
  junto con Latet; `latet upgrade` lo añade a un Latet instalado sin él.
- Son los modelos [PP-OCRv6 small](https://huggingface.co/PaddlePaddle/PP-OCRv6_small_rec_onnx) de
  PaddleOCR, uno que encuentra las líneas de texto y otro que las lee, con licencia Apache-2.0.
  Vienen de huggingface.co en una revisión fijada, y cada archivo se comprueba con un SHA-256 fijado
  antes de usarlo.
- Se ejecutan en tu CPU, en alrededor de un segundo para una captura de pantalla Full HD, en pocos
  segundos para la de una pantalla 4K o 5K, y en varios segundos para la captura de una página
  entera. No se envía nada a ningún sitio, y Latet solo se conecta a huggingface.co cuando ejecutas
  `latet ocr install`.
- Un daemon en ejecución empieza a usarlos al momento. `latet ocr status` muestra si están en uso;
  `latet ocr remove` los borra. Para mantenerlos instalados pero sin usarlos, define `ocr: off`.
- `latet global-install --ocr` los descarga con el resto de la instalación. En
  [Docker](../../getting-started/docker/): `docker exec latet latet ocr install --yes`.

## Qué le pasa a una imagen

1. Latet lee sus líneas de texto, y dónde está cada carácter. Una imagen de más de unos 3.000
   píxeles de largo, como la captura de una pantalla 4K o 5K, de una página entera mucho más alta
   que una pantalla o de varias pantallas una al lado de otra, se lee en partes de 2.000 a 3.000
   píxeles que se solapan, a un tamaño en el que su texto se lee: si se redujera para leerla de una
   vez, su texto pequeño se leería mal, o no se encontraría.
2. El texto leído pasa a ser un texto más de la petición, *Texto en una imagen*: los detectores, tus
   términos, la caja fuerte, el [revisor local](../llm-review/) y el
   [Revisor en directo](../live-review/) lo ven como cualquier otro.
3. Cada valor encontrado se cubre en la imagen: un rectángulo del color del fondo, con el valor
   ficticio escrito encima en letras de un tamaño parecido. Un tipo cuya
   [política](../profiles/#políticas) es `redact` recibe una simple barra. La imagen conserva su
   formato, sin sus metadatos; una que saldría más grande de lo que aceptan los proveedores (5 MB)
   se envía como JPEG, o se retiene si incluso así es demasiado grande.
4. Cuando se vuelve a enviar, ya que todos los agentes reenvían la conversación, la misma imagen sale
   igual, byte a byte, también después de un reinicio: la caché de prompts del proveedor sigue
   funcionando.

Una imagen sin nada que enmascarar sale tal como estaba. Una imagen que Latet no puede leer se
retiene: el modelo recibe en su lugar una nota breve, *[image withheld by Latet: ...]*, con el
motivo. Eso le pasa a una imagen animada, a una de más de 20 MB o 40 megapíxeles, a una que no es
una imagen válida, a una tan larga para su anchura, o tan ancha para su altura, que habría que leerla
en más de 16 partes (la captura de una página de más de unos 29.000 píxeles de largo), y a cualquier
imagen mientras los modelos se siguen cargando (una petición los espera hasta 20 segundos).

## Qué imágenes

| Qué | Se leen y se enmascaran |
| --- | --- |
| Imágenes en los mensajes: las imágenes base64 de Claude, las URL `data:` de OpenAI Chat y Responses, Codex por HTTP y por su WebSocket, las imágenes en línea de Gemini (`inlineData`) | sí |
| Capturas de pantalla que devuelven las herramientas: Read de Claude Code sobre una imagen, servidores MCP como el de un navegador, el computer use de OpenAI | sí |
| Imágenes indicadas por su dirección (una URL `https://`, el ID de un archivo), PDF y otros documentos, audio | no: se envían tal cual, con el indicador *No inspeccionado*, o no se envían (ver más abajo) |

## Los ajustes

`images` indica qué puede salir, y `ocr`, si se leen las imágenes. Los dos están en **Reglas** →
**Protección** → **Datos de producción**, en la [configuración](../../reference/configuration/) y en
un proyecto.

| `images` | Con reconocimiento de texto | Sin él |
| --- | --- | --- |
| `allow` (predeterminado) | las imágenes se leen y se enmascaran | las imágenes se envían tal cual, con el indicador *No inspeccionado* |
| `mask` | solo se envían las imágenes leídas y enmascaradas: una petición que lleva una que Latet no puede leer (un PDF, una imagen indicada por su dirección) no se envía | no se envía ninguna petición con una imagen |
| `block` | no se envía ninguna petición que lleve una imagen o un documento | lo mismo |

- El [perfil `max`](../profiles/) no envía imágenes, como `block`. Un proyecto tuyo puede definir
  `images: mask` para enviarlas enmascaradas.
- El `latet.yaml` de un repositorio solo puede hacer que se envíen menos imágenes. Enviar más, desde
  tus ajustes o desde un proyecto tuyo, y `ocr: off` esperan tu aprobación cuando los pide un agente
  ([Los agentes y tus ajustes](../agents-and-settings/)).
- Un agente al que se detiene recibe un error que explica por qué y qué hacer.

## En Claude Code

En [modo cliente](../../agents/claude-code/), el plugin enmascara las imágenes antes de que
Claude Code las envíe:

- **Las imágenes que devuelven las herramientas** (Read sobre un PNG, la captura de pantalla de un
  servidor MCP) se sustituyen por su copia enmascarada antes de que Claude las vea. Tu pantalla
  muestra el original.
- **Una captura de pantalla que pegas** no se puede modificar, solo quitar del prompt. Cuando Latet
  encuentra en ella valores que enmascarar, el plugin la quita y añade a tu prompt una nota con la
  ruta de una copia enmascarada, `.latet/masked-<id>.png` en tu proyecto, que Claude abre con Read.
  No se escribe nada ahí: el propio plugin responde a esa llamada a Read.
- La comprobación previa a cada petición también cubre las imágenes: una imagen con un valor en
  claro que haya llegado a la petición por otra vía la detiene.

## Revisar las imágenes

- **En el [Revisor en directo](../live-review/),** el texto de una imagen espera como cualquier
  texto, con la imagen encima: un marco donde cada valor recibe su valor ficticio, los valores que
  enmascaras en su texto cubiertos también, y puedes arrastrar un rectángulo sobre cualquier parte
  para cubrirla con una barra, o no enviar la imagen en absoluto. Tu decisión se guarda, sellada,
  para esa imagen: si se vuelve a enviar, sale de la misma forma.
- **El [revisor local](../llm-review/#imágenes)** lee de todos modos el texto de cada imagen. También
  se le puede mostrar la propia imagen, solo si lo activas tú, para que encuentre lo que se le ha
  escapado al reconocimiento de texto.

## En el panel

- Un intercambio muestra sus imágenes tal como se enviaron (con `history: full`), cada una con un
  enlace al texto leído en ella, y los indicadores *Imagen: enmascarada* e *Imagen: retenida*.
- **Reglas** muestra si el reconocimiento de texto está instalado y en uso, y el comando que lo
  instala.

## Lo que no puede hacer el reconocimiento de texto

- **No es infalible, como con los nombres.** El texto que no lee (muy pequeño, tenue, manuscrito,
  girado, en una fuente estilizada) sale tal cual. Para las capturas de pantalla que nunca deben
  salir, usa `images: block`, los rectángulos del Revisor en directo o el revisor local,
  mostrándole las imágenes.
- **Un valor mal leído se enmascara tal como se ha leído.** Una `O` leída en lugar de un `0` en un
  IBAN o en un código fiscal italiano se corrige gracias a sus dígitos de control; otro valor mal
  leído puede no reconocerse.
- **El modelo lee el valor ficticio.** Si lo lee mal, su respuesta lleva el valor ficticio mal
  leído, y Latet no puede restaurar el valor real.
- **Los PDF y otros documentos no se leen**, ni las imágenes indicadas por su dirección: consulta la
  tabla de arriba.
- **Solo texto.** Las caras, las firmas, los logotipos y la escritura a mano no se cubren.
