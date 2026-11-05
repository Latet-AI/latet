---
title: Codex (CLI y Desktop)
description: Pasa Codex CLI y Codex Desktop por la puerta de enlace de Latet con openai_base_url.
sidebar:
  label: Codex
---

Codex usa el **modo puerta de enlace**: el daemon local enmascara las peticiones y restaura las
respuestas. Funciona tanto si inicias sesión con ChatGPT como si usas una clave de API de OpenAI.

## Pasarlo por Latet

Codex CLI y Codex Desktop leen el mismo archivo de configuración, `~/.codex/config.toml` (o
`$CODEX_HOME/config.toml` si defines `CODEX_HOME`). Una sola línea en ese archivo pasa Codex por Latet.

`latet global-install`, o `latet agents apply codex`, la escribe por ti. Pregunta a Codex cómo inicias
sesión (`codex login status`) y elige la dirección correspondiente:

```toml title="~/.codex/config.toml"
# Signed in with ChatGPT:
openai_base_url = "http://127.0.0.1:5283/backend-api/codex"

# With an OpenAI API key instead:
# openai_base_url = "http://127.0.0.1:5283/openai/v1"
```

El resto del archivo, comentarios incluidos, se queda como estaba, y antes se hace una copia de
seguridad. Después, reinicia Codex (o la aplicación Codex Desktop).

- Un Codex anterior a la 0.115 ignora `openai_base_url`. `latet doctor` avisa de ello: actualiza Codex
  (`npm i -g @openai/codex`). `latet global-install` y `latet agents apply codex` no tocan un Codex
  así y explican por qué, ya que esa línea no lo pasaría por Latet. Hasta que lo actualices,
  `latet codex` le pasa a Codex la dirección en `OPENAI_BASE_URL`, la única variable que lee Codex, en
  lugar de la que defina tu shell. `latet codex` no inicia un Codex que tenga un proveedor propio
  (consulta más abajo): ese Codex esquivaría Latet.
- Si más adelante cambias entre el inicio de sesión con ChatGPT y una clave de API, vuelve a ejecutar
  `latet agents apply codex`, para que la dirección coincida.

**Sin tocar `config.toml`**, pasa una sola sesión por Latet con el wrapper. Inicia el daemon si hace
falta y pasa todos los argumentos a `codex`:

```sh
latet codex
latet codex exec "add the payments key from .env to config/local.toml"
```

`latet run codex` equivale a `latet codex`, que elige la dirección según tu inicio de sesión.

En un ordenador conectado a un servidor de Latet, `latet codex` inicia la sesión interactiva y
`codex exec`, `resume` y `fork`; se niega a ejecutar los demás subcomandos de Codex, como `review` o
`mcp`, que no puede enviar a través del servidor.

**Un proveedor propio** (Azure OpenAI, la puerta de enlace de tu empresa, Mistral...) también pasa por
Latet. Solo su `base_url` se mueve a Latet; su clave y todo lo demás se quedan como los escribiste.

- **Para una sesión**, `latet codex` mueve los proveedores de `config.toml` y del perfil que indiques
  (`--profile work`), con `-c model_providers.<id>.base_url=...`. Codex no lee ningún otro archivo, y
  se niega a arrancar con una opción para un proveedor que no carga. Un Codex anterior a la 0.115 no
  admite esas opciones: si tiene un proveedor propio, `latet codex` no lo inicia y te dice que
  actualices Codex.
- **De forma permanente**, `latet global-install` cambia cada dirección en el archivo que la escribe,
  incluidos los archivos de todos los perfiles, ya que un perfil puede hacer que Codex use cualquiera
  de esas direcciones.

El `openai_base_url` propio de un perfil (la dirección de una empresa en lugar de la de OpenAI) pasa
por Latet de la misma manera. Consulta [Puntos de conexión de empresa](../company-endpoints/).

## Cómo se comporta

- Con el inicio de sesión de ChatGPT, Codex habla con el proveedor a través de un WebSocket. El daemon
  enmascara cada petición que Codex envía por él y restaura los eventos que vuelven en streaming. Con
  una clave de API ocurre lo mismo por HTTP.
- El proveedor ve valores ficticios. Codex ve los originales, en el texto de la respuesta y en los
  argumentos de las llamadas a herramientas, así que los comandos que ejecuta y los parches que aplica
  usan los valores reales. [Cómo funciona](../../concepts/how-it-works/) sigue una clave de `.env` a lo
  largo de un turno de Codex, paso a paso.
- Los cuerpos comprimidos de las peticiones se descodifican, se enmascaran y se vuelven a comprimir. Un
  cuerpo que Latet no puede descodificar se rechaza, no se envía sin comprobar.
- Tu token de inicio de sesión o tu clave de API se reenvían sin cambios; su valor nunca se escribe en
  los registros ni en el panel.
- Si el daemon no está en marcha, Codex no puede llegar al proveedor. Es intencionado: no sale nada
  sin enmascarar.
- Codex lee los archivos con comandos de shell, así que el
  [modo de datos estricto](../../concepts/production-data/#qué-herramientas) cubre también los archivos que
  lee. Indica su herramienta de base de datos en `data_tools` para cubrir solo esa.
- La ruta, los remotos y el commit del repositorio que Codex añade a cada petición, en la cabecera
  `x-codex-turn-metadata` y en `client_metadata` del cuerpo, se quitan antes de enviarla, igual que
  la cabecera `x-openai-subagent`, el tipo o la etiqueta libre que Codex da a un subagente; el panel
  las cuenta. Su lista de modelos pasa; una llamada que Latet no lee, como recuperar una respuesta guardada, se rechaza.
- Las cabeceras tuyas en `http_headers` o `env_http_headers` de un proveedor se rechazan, nombrando
  cada una, hasta que una [excepción exacta](../../getting-started/upgrading-to-0-11/#aprobar-una-excepción-exacta) la
  deje pasar (`openai.responses.1`). Por el WebSocket de una sesión de ChatGPT, ninguna excepción se
  aplica en la 0.11.0: una cabecera o un parámetro de consulta que Latet no lee cierra la conexión antes
  de que se abra, y el cierre dice por qué y nombra la cabecera o el parámetro, nunca su valor. El
  historial registra el rechazo.
- **Su proyecto.** `latet codex` da a Codex una clave de lanzamiento a través de un proveedor de
  `model_providers` (`env_http_headers`), desde Codex 0.115: los ajustes de su proyecto se aplican
  entonces por completo. En los proveedores integrados de Codex (con la sesión de ChatGPT, o el
  proveedor de OpenAI), y antes de la 0.115, no puede viajar ninguna clave: `latet codex` lo dice al
  arrancar, y el proyecto que Codex nombra en sus peticiones queda declarado, así que sus reglas que
  añaden protección se aplican y sus relajaciones no
  ([Proyectos](../../concepts/projects/#de-confianza-y-declarados)).

Probado en condiciones reales: la CLI de Codex con la sesión iniciada con ChatGPT. Codex Desktop lee el
mismo `config.toml`; si algo no funciona ahí, por favor,
[abre una incidencia](https://github.com/Latet-AI/latet/issues).

Para dejar de usar Latet, ejecuta `latet agents revert codex`, o `latet global-uninstall` para todos
los agentes. Consulta [Desinstalación](../../getting-started/uninstall/).
