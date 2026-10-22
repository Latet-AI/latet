---
title: Compatibles con OpenAI y OpenRouter
description: Usa Latet con cualquier cliente que te permita cambiar la URL base de OpenAI o de Anthropic, y con OpenRouter.
---

Cualquier cliente que te permita cambiar su URL base de OpenAI o de Anthropic puede usar el **modo
puerta de enlace**: tus propios scripts, los SDK oficiales y las herramientas construidas sobre ellos.

## URL base

| Proveedor | En lugar de | Usa |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `http://127.0.0.1:5283/openai/v1` |
| Anthropic | `https://api.anthropic.com` | `http://127.0.0.1:5283/anthropic` |
| OpenRouter | `https://openrouter.ai/api/v1` | `http://127.0.0.1:5283/openrouter/api/v1` |
| Google Gemini | `https://generativelanguage.googleapis.com` | `http://127.0.0.1:5283/gemini` |

Mantén tu clave de API en el cliente, como siempre. La puerta de enlace la reenvía sin cambios y nunca
escribe su valor en los registros ni en el panel.

## Para un comando: `latet run`

`latet run` inicia el daemon si hace falta y ejecuta cualquier comando con las URL base definidas:

```sh
latet run python welcome.py
```

El comando ve `OPENAI_BASE_URL` y `OPENAI_API_BASE` con el valor `http://127.0.0.1:5283/openai/v1`,
`ANTHROPIC_BASE_URL` con el valor `http://127.0.0.1:5283/anthropic`, y `GOOGLE_GEMINI_BASE_URL` y
`GOOGLE_VERTEX_BASE_URL` con los valores `http://127.0.0.1:5283/gemini` y `http://127.0.0.1:5283/vertex`.
Los SDK oficiales de OpenAI, de Anthropic y Google GenAI leen estas variables: para los de Google, ver
[Gemini CLI y Google GenAI](../gemini/).

## Para todo un shell: `latet env`

`latet env` imprime las mismas variables con la sintaxis de tu shell. Para aplicarlas a la sesión
actual:

```sh
eval "$(latet env)"              # macOS and Linux
```

```powershell
latet env | Invoke-Expression    # Windows PowerShell
```

`--shell bash`, `--shell powershell` o `--shell cmd` elige la sintaxis; la predeterminada sigue tu
sistema. Para conservar las variables, añade las líneas impresas al perfil de tu shell. Claude Code
iniciado con `claude` en una shell así llega a Latet sin su plugin: sus peticiones solo se comprueban, y
cada una que lleve un valor sensible en claro se detiene. Inícialo con `latet claude`, o configúralo con
`latet global-install`, que le dan también el plugin.

## Ejemplo

Un script que pide a un modelo que redacte un correo electrónico:

```python title="welcome.py"
from openai import OpenAI

client = OpenAI()  # reads OPENAI_API_KEY and OPENAI_BASE_URL
reply = client.chat.completions.create(
    model="your-model",
    messages=[{"role": "user", "content": "Write a short welcome email to emily.carter@northwind-legal.com"}],
)
print(reply.choices[0].message.content)
```

Ejecútalo con `latet run python welcome.py`:

1. El daemon sustituye la dirección antes de que salga la petición. El proveedor lee *"Write a short
   welcome email to laura.bennett@contoso-4f1a.example"*.
2. El modelo escribe un correo electrónico a `laura.bennett@contoso-4f1a.example`.
3. A la vuelta, el daemon coloca de nuevo el original, así que el script imprime un correo electrónico
   dirigido a `emily.carter@northwind-legal.com`.

Solo se restaura el valor ficticio en sí. Si el modelo también escribió *Dear Laura*, un nombre que
dedujo de la dirección ficticia, esa palabra se queda tal como la escribió el modelo.

Las respuestas en streaming funcionan igual, fragmento a fragmento.

## Frameworks de agentes

LangChain, LangGraph, AutoGen y CrewAI llegan al modelo a través de los SDK oficiales o de LiteLLM,
que leen las variables que establece `latet run`: inicia tu programa con `latet run`, como
cualquier script. Cada uno está probado de verdad a través de Latet, configurado como se indica
abajo:

| Framework | Configuración | Probado |
| --- | --- | --- |
| LangChain y LangGraph | `ChatOpenAI`, sin URL base (`ChatAnthropic` lee `ANTHROPIC_BASE_URL` de la misma manera) | Un modelo de chat y un grafo sobre él |
| AutoGen | `OpenAIChatCompletionClient`, sin URL base | Un `AssistantAgent` |
| CrewAI | `LLM(model="openai/gpt-5.1")`, sin URL base | Su `LLM` y una crew con una tarea |

Para fijar la dirección en tu código, indícala como `base_url`: `http://127.0.0.1:5283/openai/v1` para
los clientes de OpenAI, `http://127.0.0.1:5283/anthropic` para los de Anthropic.

Aider llega al modelo a través de LiteLLM, que lee `OPENAI_API_BASE`: inícialo con
`latet run aider --model openai/gpt-5.1`, o mantén `openai-api-base: http://127.0.0.1:5283/openai/v1` en
su `.aider.conf.yml`. Está probado de verdad de la misma manera.

**Lo que Latet no ve.** Latet está entre tu programa y el modelo, y en ningún otro sitio. Una
herramienta que tu agente llama por su cuenta, como una API de búsqueda web, una base de datos o un
almacén de vectores en la nube, recibe los valores reales: el modelo la pide con valores ficticios,
y Latet vuelve a poner los originales en su respuesta antes de que tu programa ejecute la
herramienta. Mantén esas herramientas en tu máquina, o da por enviado en claro lo que reciben.

## Qué lee la puerta de enlace

- **Chat Completions** (`/chat/completions`, y el antiguo `/completions`), la **Responses API**
  (`/responses`) y **Anthropic Messages** (`/v1/messages`, también el recuento de tokens): se
  enmascaran el prompt de sistema, cada mensaje, las llamadas a herramientas y los resultados de las
  herramientas, y se restauran las respuestas, en streaming o no.
- **Embeddings** (`/embeddings`): se enmascara la entrada.
- Las **imágenes** enviadas como URL `data:` se leen y se enmascaran, con el
  [reconocimiento de texto](../../concepts/images/) opcional. Las imágenes indicadas por su dirección,
  el audio y los archivos se envían tal cual, y en el panel llevan el indicador *No inspeccionado*.
- **Cualquier otra petición** a OpenAI, Anthropic u OpenRouter, como la que pide la lista de modelos,
  pasa sin cambios. En las rutas de Google, en cambio, se rechaza una petición con un cuerpo que Latet
  no lee: lo que Latet lee ahí está en
  [Gemini CLI y Google GenAI](../gemini/#qué-lee-la-puerta-de-enlace).

Una petición a Anthropic que viene de Claude Code, o que lleva el token de una suscripción de Claude,
nunca se reescribe: solo se comprueba, y se bloquea si contiene un valor sensible. Consulta
[Claude Code](../claude-code/).

## A tener en cuenta

- La puerta de enlace solo escucha en `127.0.0.1` y rechaza las peticiones que vienen de una página
  web.
- Una ruta que no es ninguna de las de arriba recibe un error `404`: `no upstream for this path`.
- **Otro proveedor compatible con OpenAI** (DeepSeek, Mistral, un servidor de modelos en tu red):
  añádelo como destino y después usa su ruta como URL base del agente, aquí
  `http://127.0.0.1:5283/deepseek/v1`:

  ```sh
  latet config add upstreams "{name: deepseek, url: https://api.deepseek.com}"
  ```

  Consulta [`upstreams`](../../reference/configuration/#upstreams-items).
- Los agentes autónomos como Hermes Agent y OpenClaw aceptan estas URL base en sus propios ajustes:
  consulta [Hermes Agent, OpenClaw y otros agentes](../autonomous-agents/).
