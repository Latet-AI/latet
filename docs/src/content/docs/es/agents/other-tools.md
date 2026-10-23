---
title: GitHub Copilot, JetBrains, Zed y otras herramientas
description: Pasa por la puerta de enlace de Latet GitHub Copilot, Junie y AI Assistant de JetBrains, Zed, Cline, Kilo Code, Continue, Qwen Code, Crush, Android Studio y Claude Desktop, y consulta qué herramientas no puede proteger Latet.
sidebar:
  label: Copilot, JetBrains y más
---

Muchas herramientas de IA te dejan elegir la dirección del modelo al que llaman. Si les das la
dirección de la **puerta de enlace** de Latet, cada petición que envía la herramienta se enmascara
antes de salir de tu equipo, y la respuesta se restaura en él. No se instala nada en la herramienta:
basta con un ajuste.

Mantienes la clave de API de tu proveedor en la herramienta, como siempre. Latet la reenvía sin
cambios al proveedor y nunca la escribe en sus registros ni la muestra en el panel.

| Proveedor | Dirección que dar a la herramienta |
| --- | --- |
| OpenAI | `http://127.0.0.1:5283/openai/v1` |
| Anthropic | `http://127.0.0.1:5283/anthropic`, o `http://127.0.0.1:5283/anthropic/v1` para una herramienta que solo le añade `/messages` |
| OpenRouter | `http://127.0.0.1:5283/openrouter/api/v1` |

Cada sección de abajo indica la dirección exacta que necesita la herramienta. Una herramienta cuyo
proveedor de OpenRouter integrado no te deja cambiar la dirección llega a OpenRouter a través de un
proveedor compatible con OpenAI, con la dirección de OpenRouter de la tabla.

Junie CLI, Kilo Code, Continue y Crush también leen ajustes del proyecto que abres, y los ajustes
propios de un proyecto pueden apuntarlos a otra dirección: revísalos antes de trabajar en un
repositorio que no escribiste tú.

## De un vistazo

Probado significa que la propia herramienta se ejecutó a través de Latet contra un proveedor de
pruebas, con cada API que habla: el proveedor recibió valores ficticios, y la herramienta mostró los
valores reales.

| Herramienta | Qué pasa por Latet | Qué no pasa | Probado |
| --- | --- | --- | --- |
| GitHub Copilot en VS Code | El chat y los agentes, con un modelo de Custom Endpoint | Las sugerencias en línea y las demás funciones que usan el propio servicio de GitHub | Sí |
| GitHub Copilot CLI | Cada petición, con un proveedor propio | Nada, en modo sin conexión | Sí |
| GitHub Copilot en los IDE de JetBrains y en Visual Studio | El chat, con un punto de conexión personalizado compatible con OpenAI | Las sugerencias en línea | No |
| JetBrains Junie CLI | Cada petición, con un modelo personalizado | Nada | Sí |
| JetBrains AI Assistant | AI Chat y las funciones a las que asignas el modelo | El completado de código y los agentes de AI Chat | No |
| Zed | El panel del agente y el asistente en línea | Las predicciones de edición | No |
| Cline | Cada petición | Nada | La CLI |
| Kilo Code | Cada petición, con un proveedor personalizado | Nada | La CLI |
| Continue | Cada petición de un modelo cuyo `apiBase` es Latet | Nada | La CLI |
| Qwen Code | Cada petición | Una dirección de correo electrónico escrita en el prompt | Sí |
| Crush | Cada petición | Nada | Sí |
| Android Studio | El chat y el modo agente, con un proveedor remoto de terceros | Las funciones propias de Gemini | No |
| Claude Desktop en una puerta de enlace | Cada petición se comprueba, y se detiene si llevaría un valor sensible | El enmascaramiento, fuera de la pestaña Code | No |

## GitHub Copilot en VS Code

El **Custom Endpoint** de VS Code envía Copilot Chat a un modelo en la dirección que elijas. Funciona
con la sesión de GitHub iniciada o sin ella, con un plan de Copilot o sin él; en Copilot Business y
Enterprise, un administrador puede desactivarlo.

1. Ejecuta **Chat: Manage Language Models** y después **Add Models** → **Custom Endpoint**.
2. Introduce un nombre y la clave de API de tu proveedor. VS Code guarda la clave en su almacén de
   secretos.
3. Describe cada modelo en el archivo que abre VS Code, `chatLanguageModels.json`, junto a tu
   `settings.json`. La `url` de cada modelo es la dirección completa de la llamada, y `apiType` es la
   API que habla:

```json title="chatLanguageModels.json"
[
  {
    "name": "Latet",
    "vendor": "customendpoint",
    "models": [
      {
        "id": "gpt-5.1",
        "name": "GPT-5.1 through Latet",
        "url": "http://127.0.0.1:5283/openai/v1/responses",
        "apiType": "responses",
        "toolCalling": true,
        "vision": true,
        "maxInputTokens": 272000,
        "maxOutputTokens": 128000
      },
      {
        "id": "claude-sonnet-4-5",
        "name": "Claude Sonnet 4.5 through Latet",
        "url": "http://127.0.0.1:5283/anthropic/v1/messages",
        "apiType": "messages",
        "toolCalling": true,
        "vision": true,
        "maxInputTokens": 200000,
        "maxOutputTokens": 64000
      }
    ]
  }
]
```

Para la API de Chat Completions, usa `http://127.0.0.1:5283/openai/v1/chat/completions` con
`"apiType": "chat-completions"`. Elige el modelo en el selector de modelos del chat; un modelo
necesita `"toolCalling": true` para funcionar en modo agente.

**Lo que no pasa por Latet.** Las sugerencias en línea (los completados de código y las sugerencias
de la siguiente edición), la búsqueda de código y las demás funciones que se basan en los embeddings
de GitHub usan el propio servicio de GitHub, no tu modelo: con la sesión iniciada, salen sin pasar
por Latet. Con la sesión iniciada, Copilot también encarga tareas breves, como los títulos de los
chats y los mensajes de commit, a los modelos de GitHub; para que se queden en tu modelo, define
`"chat.byokUtilityModelDefault": "mainAgent"` en tus ajustes, o elige modelos para ellas con
`chat.utilityModel` y `chat.utilitySmallModel`.

## GitHub Copilot CLI

La CLI de Copilot pasa por Latet con un proveedor propio: una clave de OpenAI, Anthropic u
OpenRouter, u otra dirección, definida en variables de entorno. Con la sesión de GitHub iniciada, con
los modelos de un plan de Copilot, envía al servicio Copilot de GitHub, delante del cual Latet no
puede ponerse: `latet copilot` lo dice y no inicia nada.

```sh
export COPILOT_PROVIDER_TYPE=openai          # Chat Completions
export COPILOT_PROVIDER_API_KEY=...          # your OpenAI key
export COPILOT_MODEL=gpt-5.1
export COPILOT_OFFLINE=true                  # no GitHub sign-in, no telemetry
latet copilot
```

En PowerShell:

```powershell
$env:COPILOT_PROVIDER_TYPE = "openai"
$env:COPILOT_PROVIDER_API_KEY = "..."        # your OpenAI key
$env:COPILOT_MODEL = "gpt-5.1"
$env:COPILOT_OFFLINE = "true"
latet copilot
```

`latet copilot` dirige la dirección del proveedor a Latet solo para esa sesión: las de OpenAI,
Anthropic y OpenRouter van a las rutas propias de Latet, otra dirección se convierte en uno de tus
destinos, y una en este ordenador se queda como está. Con OpenRouter, define
`COPILOT_PROVIDER_BASE_URL=https://openrouter.ai/api/v1` y tu clave de OpenRouter; para Anthropic,
`COPILOT_PROVIDER_TYPE=anthropic` y tu clave de Anthropic. Para iniciar `copilot` tú mismo, define en
su lugar `COPILOT_PROVIDER_BASE_URL` con la dirección de Latet: `http://127.0.0.1:5283/openai/v1`,
`/openrouter/api/v1` o `/anthropic`.

`COPILOT_PROVIDER_WIRE_API=responses` usa la Responses API en su lugar. El modelo tiene que admitir
la llamada a herramientas y el streaming. Sin `COPILOT_OFFLINE=true`, la CLI sigue enviando
telemetría a GitHub, y funciones como `/delegate` necesitan que inicies sesión en GitHub; tus prompts
solo van a tu proveedor.

## GitHub Copilot en los IDE de JetBrains y en Visual Studio

El plugin de Copilot para JetBrains acepta un punto de conexión personalizado compatible con OpenAI:
abre **Manage Models** desde el selector de modelos del chat, añade el punto de conexión
`http://127.0.0.1:5283/openai/v1` con tu clave de API y elige su modelo en el chat. Visual Studio
2026 hace lo mismo en modo agente, desde sus propios ajustes de modelos. En los dos, las sugerencias
en línea siguen usando el servicio de GitHub.

## JetBrains Junie CLI

Junie CLI acepta un modelo personalizado a partir de un archivo de perfil en `~/.junie/models/`, o en
`.junie/models/` dentro de un proyecto. Su `baseUrl` es la dirección completa de la llamada: Junie no
le añade nada.

```json title="~/.junie/models/latet.json"
{
  "id": "gpt-5.1",
  "baseUrl": "http://127.0.0.1:5283/openai/v1/responses",
  "apiType": "OpenAIResponses",
  "apiKey": "${OPENAI_API_KEY}"
}
```

Inícialo con `junie --model custom:latet`, o elige el modelo en **Custom** dentro de `/model`. Otros
valores de `apiType`:

| `apiType` | `baseUrl` |
| --- | --- |
| `OpenAICompletion` (Chat Completions) | `http://127.0.0.1:5283/openai/v1/chat/completions` |
| `Anthropic` | `http://127.0.0.1:5283/anthropic/v1/messages` |

Un modelo personalizado no necesita una cuenta de JetBrains. `JUNIE_SHARE_ANONYMOUS_STATISTICS=false`
desactiva las estadísticas de uso anónimas. Junie dentro del IDE solo acepta claves de OpenAI y de
Anthropic, no una dirección personalizada.

## JetBrains AI Assistant

En **Settings | Tools | AI Assistant | Providers & API keys**, en **Third-party AI providers**, añade
un proveedor compatible con OpenAI con la URL `http://127.0.0.1:5283/openai/v1` y tu clave de API, y
activa **Tool calling**. Sus modelos aparecen en el selector de modelos de AI Chat; en **Models
Assignment** también puedes asignarles las demás funciones, como los mensajes de commit.

El completado de código no usa un modelo tuyo, y los agentes de AI Chat, como Junie, Claude Agent y
Codex, aceptan las claves de sus propios proveedores, no una dirección personalizada: no pasan por
Latet.

## Zed

Añade un proveedor en el `settings.json` de Zed y después elige su modelo en el panel del agente:

```json title="settings.json"
{
  "language_models": {
    "openai_compatible": {
      "Latet": {
        "api_url": "http://127.0.0.1:5283/openai/v1",
        "available_models": [{ "name": "gpt-5.1", "max_tokens": 272000 }]
      }
    },
    "anthropic": { "api_url": "http://127.0.0.1:5283/anthropic" }
  }
}
```

Zed lee la clave del proveedor `Latet` de los ajustes del agente, o de la variable `LATET_API_KEY`;
el proveedor de Anthropic conserva su propia clave. Los proveedores de OpenAI y de OpenRouter
integrados en Zed también aceptan un `api_url`.

Las **predicciones de edición** vienen por defecto del propio servicio de Zed y no pasan por Latet:
define `"edit_predictions": { "provider": "none" }` para desactivarlas.

## Cline

En los ajustes de Cline, elige el proveedor **OpenAI Compatible** con la URL base
`http://127.0.0.1:5283/openai/v1`, tu clave de API y un ID de modelo. Para Anthropic, elige el
proveedor **Anthropic**, marca **Use custom base URL** e introduce
`http://127.0.0.1:5283/anthropic/v1`: Cline le añade `/messages`.

La CLI de Cline acepta el proveedor compatible con OpenAI desde la línea de comandos:

```sh
cline auth -p openai-compatible -k "$OPENAI_API_KEY" -m gpt-5.1 -b http://127.0.0.1:5283/openai/v1
```

Para su proveedor de Anthropic, define `baseUrl` en `~/.cline/data/settings/providers.json`:
`cline auth` solo acepta una URL base para los proveedores compatibles con OpenAI.

## Kilo Code

En la extensión de Kilo Code, añade un **proveedor personalizado** en sus ajustes, con la API que
habla (OpenAI Compatible, OpenAI Responses o Anthropic Messages) y la URL base de abajo. La CLI
`kilo` lee el mismo proveedor de `kilo.json`, en `~/.config/kilo/` o en un proyecto:

```json title="kilo.json"
{
  "provider": {
    "latet": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Latet",
      "options": { "baseURL": "http://127.0.0.1:5283/openai/v1", "apiKey": "{env:OPENAI_API_KEY}" },
      "models": { "gpt-5.1": { "name": "GPT-5.1 through Latet" } }
    }
  },
  "model": "latet/gpt-5.1",
  "disabled_providers": ["kilo"]
}
```

`disabled_providers` desactiva el proveedor propio de Kilo, cuyas peticiones van al servicio de Kilo sin
pasar por Latet.
Para Anthropic, usa `"npm": "@ai-sdk/anthropic"` con la URL base
`http://127.0.0.1:5283/anthropic/v1`; para la Responses API, `"npm": "@ai-sdk/openai"` con
`http://127.0.0.1:5283/openai/v1`.

## Continue

Las extensiones de Continue para VS Code y JetBrains y su CLI `cn` leen el mismo
`~/.continue/config.yaml`. Da a cada modelo la dirección de Latet en `apiBase`:

```yaml title="~/.continue/config.yaml"
name: Latet
version: 1.0.0
schema: v1
models:
  - name: GPT-5.1 through Latet
    provider: openai
    model: gpt-5.1
    apiBase: http://127.0.0.1:5283/openai/v1
    apiKey: ${{ secrets.OPENAI_API_KEY }}
  - name: Claude through Latet
    provider: anthropic
    model: claude-sonnet-4-5
    apiBase: http://127.0.0.1:5283/anthropic/v1
    apiKey: ${{ secrets.ANTHROPIC_API_KEY }}
```

El `apiBase` de Anthropic termina en `/v1`: Continue le añade `/messages`.

## Qwen Code

Qwen Code lee su dirección de las mismas variables que los SDK oficiales, así que `latet run` lo hace
pasar por Latet:

```sh
latet run qwen
```

Usa `OPENAI_BASE_URL` con su proveedor compatible con OpenAI y `ANTHROPIC_BASE_URL` con el de
Anthropic; elige el proveedor en `~/.qwen/settings.json`
(`"security": { "auth": { "selectedType": "openai" } }`, o `"anthropic"`), y define la clave y el
modelo en `OPENAI_API_KEY` y `OPENAI_MODEL`, o en `ANTHROPIC_API_KEY` y `ANTHROPIC_MODEL`. Sin
`latet run`, define tú las URL base: `http://127.0.0.1:5283/openai/v1` y
`http://127.0.0.1:5283/anthropic`.

**Una dirección de correo electrónico que escribes.** Qwen Code lee cada `@` de un prompt que
escribes como un archivo que adjuntar, y envía `mario.rossi@acme.it` como `mario.rossi @acme.it`.
Latet no reconoce eso como una dirección, así que sale en claro. Las direcciones de los archivos, de
la salida de los comandos y de las páginas que lee Qwen Code no cambian, y Latet las enmascara.

## Crush

Añade un proveedor al `crush.json` de Crush, en un proyecto o en tu carpeta de ajustes de Crush:

```json title="crush.json"
{
  "providers": {
    "latet": {
      "type": "openai-compat",
      "base_url": "http://127.0.0.1:5283/openai/v1",
      "api_key": "$OPENAI_API_KEY",
      "models": [{ "id": "gpt-5.1", "name": "GPT-5.1 through Latet", "context_window": 272000, "default_max_tokens": 16000 }]
    }
  }
}
```

Elige ese proveedor para los dos modelos de Crush, de modo que ninguna petición, ni siquiera las cortas
que nombran una sesión, vaya a un proveedor que Crush haya configurado por sí mismo a partir de tu
entorno:

```json title="crush.json"
"models": {
  "large": { "model": "gpt-5.1", "provider": "latet" },
  "small": { "model": "gpt-5.1", "provider": "latet" }
}
```

Para Anthropic, usa `"type": "anthropic"` con `http://127.0.0.1:5283/anthropic`. El tipo `openai` de
Crush habla la Responses API, y su tipo `openrouter` no acepta una URL base: llega a OpenRouter con
`openai-compat` y `http://127.0.0.1:5283/openrouter/api/v1`.

## Android Studio

En **Settings → Tools → AI → Model Providers**, añade un **Third-Party Remote Provider** con la
dirección de Latet en lugar de la del proveedor, y tu clave de API; después actualiza la lista de
modelos y elige los que quieras: `http://127.0.0.1:5283/openai/v1` para OpenAI,
`http://127.0.0.1:5283/anthropic` para Anthropic, `http://127.0.0.1:5283/openrouter/api/v1` para
OpenRouter. El chat y el modo agente usan el modelo que elijas; las funciones propias de Gemini
siguen usando el servicio de Google.

## Claude Desktop en una puerta de enlace

Claude Desktop puede funcionar sobre una puerta de enlace de inferencia en lugar del servicio de
Anthropic, sin el inicio de sesión de Claude: una organización lo usa para dirigirlo a su propio
proveedor. Entonces no hay suscripción Pro ni Max: las peticiones se pagan por uso, con una clave de
API de Anthropic o a través del proveedor de tu empresa. En un solo equipo, sin iniciar sesión,
activa **Help → Troubleshooting → Enable Developer Mode** y después abre **Developer → Configure
Third-Party Inference**; una organización define las mismas claves desde un perfil de gestión de
dispositivos. Haz que la puerta de enlace sea Latet:

| Clave | Valor |
| --- | --- |
| `inferenceProvider` | `gateway` |
| `inferenceGatewayBaseUrl` | `http://127.0.0.1:5283/anthropic` |
| `inferenceGatewayApiKey` | Tu clave de API de Anthropic |
| `inferenceGatewayAuthScheme` | `x-api-key` |

Claude Desktop acepta una dirección `http://` en este equipo. Chat, Cowork y Code envían entonces
cada petición a través de Latet, y la lista de modelos llega de Anthropic a través de él.

**Lo que Latet hace con ellas.** El motor de Claude Code construye estas peticiones, y Latet nunca
reescribe una petición de Claude Code: comprueba cada una y detiene la que llevaría un secreto o
datos personales en claro, con un mensaje en lugar de una respuesta. No las enmascara, a diferencia
de lo que hace con las demás herramientas de esta página: la
[guía de puertas de enlace](https://code.claude.com/docs/en/llm-gateway-protocol) de Anthropic pide
a una puerta de enlace que inspeccione las peticiones de Claude Code, no que las modifique. En la
pestaña Code, el plugin de Latet enmascara el contenido antes de que se construya la petición, así
que la pestaña Code recibe tanto el enmascaramiento como la comprobación.

Con el inicio de sesión de Claude, Claude Desktop no usa una puerta de enlace: Chat y Cowork no
están cubiertos, y el plugin enmascara en la pestaña Code sin ninguna comprobación detrás. Consulta
[Claude Code](../claude-code/#cobertura).

## Lo que Latet no puede proteger

Latet se ejecuta en tu equipo, entre la herramienta y el modelo. Estas herramientas envían sus
peticiones desde los propios servidores del fabricante, o llaman a un servicio que ningún ajuste
puede cambiar, así que sus peticiones nunca pasan por Latet:

| Herramienta | Por qué |
| --- | --- |
| Cursor y su CLI | Cursor construye y envía cada petición al modelo desde sus propios servidores, incluso con tu propia clave de API. |
| Windsurf y Devin Desktop | Las peticiones van a los servidores del fabricante, que llaman al modelo. |
| Google Antigravity (el IDE) | Las peticiones van al servicio de Google, que llama al modelo. |
| Jules | Se ejecuta en la nube de Google, no en tu equipo. |
| Kiro y Amazon Q Developer | Las peticiones van al servicio de Amazon, que llama al modelo. |
| Warp | Las peticiones van a los servidores de Warp, que llaman al modelo. |
| La aplicación de escritorio de ChatGPT | Solo habla con el servicio de OpenAI; no tiene ningún ajuste para otra dirección. |
| Claude Desktop con el inicio de sesión de Claude, y las sesiones en la nube | Chat y Cowork llaman directamente al servicio de Anthropic, y las sesiones en la nube se ejecutan en la infraestructura de Anthropic. |

Si una herramienta que usas no aparece en esta página, busca un ajuste llamado base URL, API base,
endpoint o custom provider (URL base, punto de conexión o proveedor personalizado): si lo tiene, pasa
por Latet como las herramientas de arriba. Si no funciona, por favor,
[abre una incidencia](https://github.com/Latet-AI/latet/issues).
