---
title: OpenCode
description: Pasa por la puerta de enlace de Latet los proveedores de OpenCode que usan clave de API.
---

OpenCode usa el **modo puerta de enlace**: el daemon local enmascara las peticiones y restaura las
respuestas. Cubre los proveedores que usas con una clave de API: Anthropic, OpenAI y OpenRouter.

## Pasarlo por Latet

`latet global-install`, o `latet agents apply opencode`, hace que esos tres proveedores apunten al
daemon en tu configuración global de OpenCode, `~/.config/opencode/opencode.jsonc` (u `opencode.json`
si ese es el archivo que tienes). Antes se hace una copia de seguridad del archivo, y sus comentarios
se conservan. Añade:

```json title="~/.config/opencode/opencode.jsonc"
{
  "provider": {
    "anthropic": { "options": { "baseURL": "http://127.0.0.1:5283/anthropic/v1" } },
    "openai": { "options": { "baseURL": "http://127.0.0.1:5283/openai/v1" } },
    "openrouter": { "options": { "baseURL": "http://127.0.0.1:5283/openrouter/api/v1" } }
  }
}
```

Tus claves de API se quedan en OpenCode como antes: la puerta de enlace las reenvía sin cambios y nunca
escribe sus valores en los registros ni en el panel. Después, reinicia OpenCode.

El `opencode.json` propio de un proyecto se lee después de tu archivo global, y una dirección que
establezca para uno de estos proveedores sustituye a la de Latet: en un proyecto así, inicia OpenCode
con `latet opencode`, cuyas direcciones OpenCode lee en último lugar.

**Sin tocar tu configuración**, pasa una sola sesión por Latet con el wrapper. Le pasa a OpenCode los
mismos ajustes solo para esa ejecución (a través de `OPENCODE_CONFIG_CONTENT`), inicia el daemon si
hace falta y pasa todos los argumentos a `opencode`:

```sh
latet opencode
latet opencode run "add the payments key from .env to config/local.toml"
```

## Límites

- **Solo pasan por Latet estos tres proveedores.** Un modelo de cualquier otro proveedor (Amazon
  Bedrock o Azure en OpenCode, por ejemplo) habla directamente con su proveedor, sin Latet, y nada te
  avisa: elige un modelo de Anthropic, de OpenAI o de OpenRouter.
  Claude Code y Codex también pasan por Latet con esas nubes: consulta
  [Puntos de conexión de empresa](../company-endpoints/).
- **El inicio de sesión con ChatGPT no está cubierto.** El plugin de ChatGPT de OpenCode tiene
  integrada la dirección del proveedor, así que una URL base no puede redirigirlo. OpenCode con el inicio
  de sesión con ChatGPT está en la [hoja de ruta](../../roadmap/).
- **Sin inicio de sesión con Claude Pro o Max.** Anthropic permite usar una suscripción en Claude Code
  y en sus propias aplicaciones, y Latet nunca reescribe una petición que lleve el token de una
  suscripción de Claude: solo la comprueba, y la bloquea si contiene un valor sensible. Sin un plugin
  que enmascare antes el contenido, esas peticiones se bloquearían en lugar de enmascararse. Usa una
  clave de API de Anthropic.

Para dejar de usar Latet, ejecuta `latet agents revert opencode`, o `latet global-uninstall` para todos
los agentes.
