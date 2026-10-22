---
title: Hermes Agent, OpenClaw y otros agentes
description: Los agentes autónomos a través de la puerta de enlace de Latet, entre ellos Hermes Agent, OpenClaw, Goose y OpenHands, y cualquier agente que acepte una URL base compatible con OpenAI.
sidebar:
  label: Agentes autónomos
---

Los agentes autónomos trabajan por su cuenta, durante horas: leen buzones de correo, navegan,
consultan bases de datos, ejecutan comandos y llaman a API, y nadie lee cada paso. Cada página,
archivo y resultado que leen va al modelo con la siguiente petición. Si apuntan a Latet, cada una de
esas peticiones se enmascara antes de salir de tu equipo, y las respuestas se restauran en él.

Pasan por la **puerta de enlace** de Latet, como cualquier cliente compatible con OpenAI: haces que la
URL base del agente apunte a Latet y mantienes la clave de API de tu proveedor en el agente, como
siempre. `latet global-install` no cambia los archivos de estos agentes: basta con los ajustes de
abajo.

| Proveedor | URL base que configurar | Clave |
| --- | --- | --- |
| OpenAI | `http://127.0.0.1:5283/openai/v1` | Tu clave de API de OpenAI |
| OpenRouter (Claude, Gemini y cientos de modelos más) | `http://127.0.0.1:5283/openrouter/api/v1` | Tu clave de API de OpenRouter |
| Anthropic, para un agente que habla el formato de Anthropic | `http://127.0.0.1:5283/anthropic` | Tu clave de API de Anthropic |

Una suscripción Claude Pro o Max solo funciona en Claude Code, como dicen las condiciones de
Anthropic, así que estos agentes usan una clave de API: llegan a los modelos Claude a través de
OpenRouter, o con una clave de API de Anthropic. La clave pasa por Latet hasta el proveedor y nunca se
escribe en los registros ni se muestra en el panel.

## Hermes Agent

Hermes lee su modelo y su punto de conexión de `config.yaml`, en su carpeta: `~/.hermes` en macOS
y Linux, `%LOCALAPPDATA%\hermes` en Windows (`HERMES_HOME` fija otra). Haz que un proveedor
personalizado apunte a Latet:

```yaml title="config.yaml"
model:
  provider: custom
  base_url: http://127.0.0.1:5283/openrouter/api/v1   # or http://127.0.0.1:5283/openai/v1
  key_env: OPENROUTER_API_KEY                          # the variable that holds your key
  default: anthropic/claude-sonnet-4.5                 # a model your provider lists
```

`hermes model` configura lo mismo de forma interactiva. `OPENAI_BASE_URL` en el entorno no basta:
Hermes solo lo tiene en cuenta para su proveedor `openai-api`, así que `latet run hermes` no pasaría
por Latet un proveedor personalizado.

## OpenClaw

OpenClaw lee `~/.openclaw/openclaw.json` (JSON5) y aplica por sí mismo los cambios que le hagas. Añade
un proveedor que apunte a Latet y haz que uno de sus modelos sea el predeterminado:

```json5 title="~/.openclaw/openclaw.json"
{
  agents: {
    defaults: { model: { primary: "latet/gpt-5.1" } },
  },
  models: {
    providers: {
      latet: {
        baseUrl: "http://127.0.0.1:5283/openai/v1",
        apiKey: "${OPENAI_API_KEY}",
        api: "openai-completions",
        models: [{ id: "gpt-5.1", name: "GPT-5.1 through Latet" }],
      },
    },
  },
}
```

Para OpenRouter, usa `http://127.0.0.1:5283/openrouter/api/v1`, tu clave de OpenRouter y sus nombres
de modelo.

## Goose

El proveedor OpenAI de Goose acepta un host y una ruta, que une:

```sh
export OPENAI_HOST=http://127.0.0.1:5283
export OPENAI_BASE_PATH=openai/v1/chat/completions
export OPENAI_API_KEY=...        # your OpenAI key, as usual
export GOOSE_PROVIDER=openai
export GOOSE_MODEL=gpt-5.1
goose session
```

`GOOSE_PROVIDER` y `GOOSE_MODEL` eligen el proveedor y el modelo: sin ellas, Goose te pide que
ejecutes antes `goose configure`, que guarda los mismos valores. Para OpenRouter, añade con
`goose configure` un proveedor personalizado compatible con OpenAI, con la URL base
`http://127.0.0.1:5283/openrouter/api/v1` y tu clave de OpenRouter; esta configuración no está entre
las probadas.

## OpenHands

En los ajustes de LLM de OpenHands, en **Advanced**, elige un modelo personalizado con el prefijo de
su proveedor (`openai/gpt-5.1`), define la URL base como `http://127.0.0.1:5283/openai/v1` e
introduce tu clave de API. Desde la línea de comandos:

```sh
LLM_MODEL=openai/gpt-5.1 LLM_BASE_URL=http://127.0.0.1:5283/openai/v1 LLM_API_KEY=... \
  openhands --override-with-envs
```

## Cualquier otro agente

Un agente que acepta una URL base compatible con OpenAI pasa por Latet con las direcciones de la tabla
de arriba. Uno que lee `OPENAI_BASE_URL` o `ANTHROPIC_BASE_URL` del entorno se puede iniciar con
[`latet run`](../openai-compatible/#para-un-comando-latet-run), que las define solo para él.

**Un agente en un contenedor** no llega a Latet en `127.0.0.1`: ahí, `127.0.0.1` es el propio
contenedor, y Latet solo responde a las peticiones dirigidas a `127.0.0.1` o a `localhost`. Ejecuta el
agente en tu equipo; en Linux, un contenedor iniciado con `--network host` también comparte el
`127.0.0.1` de tu equipo.

## Para un agente que nadie vigila

- **Activa el [revisor local](../../concepts/llm-review/)** si tienes un servidor de modelos local. Lee
  lo que el agente obtiene, la parte que nadie escribe, y por defecto una petición espera su revisión y
  se detiene cuando el revisor no puede responder.
- **Usa el [modo de datos estricto](../../concepts/production-data/)** para un agente que lee bases de
  datos o registros: se enmascara cada valor de una tabla o de un documento JSON, y los joins siguen
  funcionando.
- **Síguelo en el panel:** `latet dashboard` muestra cada petición que envió el agente, qué se
  enmascaró en ella y qué se bloqueó.
- **Deja que gestione Latet, con seguridad:** indícale
  [`llms-config.md`](https://github.com/Latet-AI/latet/blob/main/llms-config.md). Lo que añade
  protección se aplica; lo que la reduce espera tu aprobación en el panel. Consulta
  [Los agentes y tus ajustes](../../concepts/agents-and-settings/).

Cada uno de estos cuatro agentes está probado de verdad, configurado como arriba: envía una tarea
a través de Latet a un proveedor de prueba que responde como OpenAI; el proveedor recibe valores
ficticios y el agente recupera los reales. Si un agente no funciona a través de Latet, por favor,
[abre una incidencia](https://github.com/Latet-AI/latet/issues).
