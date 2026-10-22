---
title: Gemini CLI y Google GenAI
description: Haz pasar Gemini CLI, los SDK Google GenAI y Gemini en Vertex AI por la puerta de enlace de Latet.
---

Gemini CLI y los programas construidos sobre los SDK GenAI de Google usan el **modo puerta de
enlace**: el daemon local enmascara las peticiones y restaura las respuestas. Cubre la Gemini API con
una clave de API, y Gemini en Vertex AI, con un proyecto de Google Cloud o en modo express con una
clave de API.

## URL base

| Servicio | En lugar de | Usa | Variable |
| --- | --- | --- | --- |
| Gemini API | `https://generativelanguage.googleapis.com` | `http://127.0.0.1:5283/gemini` | `GOOGLE_GEMINI_BASE_URL` |
| Vertex AI | `https://<location>-aiplatform.googleapis.com` | `http://127.0.0.1:5283/vertex` | `GOOGLE_VERTEX_BASE_URL` |

Tu clave de API o tu acceso a Google Cloud se quedan donde están. La puerta de enlace los reenvía sin
cambios (una clave en la cabecera `x-goog-api-key` o en el parámetro `key` de la dirección, un token
de Vertex en `Authorization`) y nunca escribe su valor en los registros, en el panel ni en una
captura. En Vertex, cada petición va a la dirección de la location que nombra.

## Gemini CLI

Haz pasar Gemini CLI por Latet con el wrapper. Inicia el daemon si hace falta, fija las dos URL base
solo para esa ejecución y pasa cada argumento a `gemini`:

```sh
latet gemini
latet gemini -p "write the release notes for the payments module"
```

`latet run gemini` hace lo mismo. Gemini CLI toma su dirección solo del entorno, así que no está
disponible en modo global: `latet global-install` no lo configura, y no hay un
`latet agents apply gemini`. Un cambio permanente no llegaría a un proyecto con su propio archivo
`.env`, y allí Gemini CLI hablaría con Google directamente sin decirlo.

### Elige una clave de API o Vertex AI

En Gemini CLI, `/auth` elige cómo llega a su modelo, y la elección se guarda en
`~/.gemini/settings.json`:

```json title="~/.gemini/settings.json"
{ "security": { "auth": { "selectedType": "gemini-api-key" } } }
```

- **Una clave de API** (`gemini-api-key`): la contiene `GEMINI_API_KEY`.
- **Vertex AI** (`vertex-ai`): `GOOGLE_GENAI_USE_VERTEXAI=true`, con `GOOGLE_CLOUD_PROJECT` y
  `GOOGLE_CLOUD_LOCATION` y tu acceso a Google Cloud, o solo `GOOGLE_API_KEY` en modo express.

Si todavía no hay nada elegido, `latet gemini` propone la clave de API en el diálogo de Gemini CLI, o
Vertex AI cuando está definido `GOOGLE_GENAI_USE_VERTEXAI=true`. Una ejecución con `-p` necesita que la elección esté hecha antes: fija `selectedType` como arriba.

**Con el acceso de Google**, ya lo elijan tus ajustes, los del proyecto (`.gemini/settings.json`
en la carpeta en la que lo inicias) o los del equipo, Gemini CLI envía sus peticiones a un endpoint
que Latet no lee, así que no se enmascararía nada. En ese caso `latet gemini` no lo inicia, y dice
dónde está hecha la elección y qué elegir en su lugar.

### Estadísticas de uso

Gemini CLI envía sus estadísticas de uso directamente a Google, no a través de Latet. Para
desactivarlas:

```json title="~/.gemini/settings.json"
{ "privacy": { "usageStatisticsEnabled": false } }
```

## Scripts y los SDK Google GenAI

`latet run` fija `GOOGLE_GEMINI_BASE_URL` y `GOOGLE_VERTEX_BASE_URL` junto a las variables de OpenAI y
Anthropic, y `latet env` las imprime para toda una shell. Los SDK de Google para Python
(`google-genai`) y JavaScript (`@google/genai`) las leen:

```python title="welcome.py"
from google import genai

client = genai.Client()  # reads GEMINI_API_KEY and GOOGLE_GEMINI_BASE_URL
reply = client.models.generate_content(
    model="gemini-3-pro",
    contents="Write a short welcome email to emily.carter@northwind-legal.com",
)
print(reply.text)
```

Ejecútalo con `latet run python welcome.py`: Google lee una dirección falsa de la misma forma, y el
script imprime el correo con la verdadera. Las respuestas en streaming funcionan igual, trozo a
trozo.

El endpoint de Google compatible con OpenAI también pasa por Latet: da a un cliente de OpenAI la URL
base `http://127.0.0.1:5283/gemini/v1beta/openai`.

## Qué lee la puerta de enlace

- **Generar**, en streaming o no (`generateContent`, `streamGenerateContent`), **contar tokens**
  (`countTokens`), los **embeddings** (`embedContent`, `batchEmbedContents`) y **crear una caché**
  (`cachedContents`): la instrucción de sistema, cada mensaje, las llamadas a funciones y sus
  resultados, las declaraciones de las funciones y el esquema de la respuesta se enmascaran, y las
  respuestas se restauran.
- Las **imágenes** enviadas en la petición se leen y se enmascaran con el
  [reconocimiento de texto](../../concepts/images/) opcional. Los demás archivos, y los archivos
  dados por su dirección, se envían tal cual, y en el panel llevan el indicador *No inspeccionado*.
- El **razonamiento** del modelo, y el código que Google ejecuta para él, quedan como el modelo los
  escribió.
- **Cualquier otra petición con cuerpo** en estas rutas se rechaza, con un error que lo dice, y un
  WebSocket de la Live API se cierra antes de que pase nada: la Interactions API, la Live API, la
  Batch API, la subida de archivos y el `:predict` de Vertex (sus propios embeddings y modelos de
  imágenes) llevan una conversación en formas que Latet todavía no lee (están en la
  [hoja de ruta](../../roadmap/)). Una petición sin cuerpo, como la lista de modelos, pasa.

## Límites

- **Gemini CLI con el acceso de Google** no está cubierto (ver arriba).
- **Antigravity CLI** acepta una URL base con una clave de API (`GOOGLE_GEMINI_BASE_URL`), pero su
  documentación no dice qué protocolo de Google habla entonces, y no está verificado con Latet. Si
  usa la Interactions API, Latet rechaza sus peticiones en lugar de enviarlas sin enmascarar.
- Una respuesta que usa una **caché explícita** recupera los originales de los valores que lleva su
  propia conversación.
