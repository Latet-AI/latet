---
title: Puntos de conexión de empresa
description: Claude Code y Codex a través de la puerta de enlace de tu empresa, Amazon Bedrock, Google Vertex, Microsoft Foundry o Azure OpenAI, con Latet delante.
---

En el trabajo, Claude Code y Codex suelen llegar al modelo por una ruta que eligió tu empresa, en lugar
de ir directamente a Anthropic o a OpenAI: su propia puerta de enlace de LLM (LiteLLM, Portkey...),
Amazon Bedrock, Google Vertex, Microsoft Foundry o Azure OpenAI. El modelo es el mismo; lo que cambia
es quién lo vende y adónde van las peticiones.

Latet lee esa ruta de los propios ajustes del agente y se pone delante. Las peticiones llegan al mismo
punto de conexión, con las mismas claves, después de que Latet las haya comprobado (Claude Code) o
enmascarado (Codex). No hay nada que configurar a mano: lo hacen `latet claude`, `latet codex` y
`latet global-install`.

## Cómo funciona

- **Latet lee adónde va el agente.** Para Claude Code: las variables de tu shell, tu propio
  `settings.json` y los ajustes gestionados de tu organización, tal como los combina Claude Code. Para
  Codex: `config.toml` y los archivos de los perfiles (`<name>.config.toml`). Los ajustes de un
  repositorio nunca se leen: un proyecto que clonas no puede decidir adónde van tus peticiones.
- **La dirección de una empresa se convierte en un destino.** Se guarda en `config.yaml`, en
  [`upstreams`](../../reference/configuration/#upstreams-items), con un nombre sacado de la dirección
  (`https://llm.acme.com` se convierte en `llm-acme-com`), y el agente envía en su lugar a
  `http://127.0.0.1:5283/llm-acme-com`. La misma dirección recibe siempre el mismo destino.
  Algunos nombres son de las rutas de Latet, y un destino no puede usarlos:
  `anthropic`, `openai`, `chatgpt`, `openrouter`, `gemini`, `bedrock`, `vertex` y `backend-api`.
- **Bedrock y Vertex tienen sus propias rutas**, `/bedrock/<region>/` y `/vertex/`, que solo llegan a
  las direcciones de Amazon y de Google.
- **Cuando Latet no puede ponerse delante, lo dice.** El agente no se inicia, y Latet dice por qué y
  qué hacer. Nunca deja que un agente lo esquive en silencio.

Para una sesión, `latet claude` pasa sus ajustes con la opción `--settings` de Claude Code, que se
impone a tus ajustes y a los del proyecto; `latet codex` añade opciones `-c`. De forma permanente,
`latet global-install` escribe lo mismo en los archivos de los agentes, y `latet global-uninstall`
restablece cada dirección que sustituyó, aunque hayas editado el archivo desde entonces.

## La puerta de enlace de tu empresa

Claude Code configurado con la puerta de enlace de tu empresa:

```json title="~/.claude/settings.json"
{ "env": { "ANTHROPIC_BASE_URL": "https://llm.acme.com", "ANTHROPIC_AUTH_TOKEN": "sk-acme-..." } }
```

Con `latet claude`, Claude Code envía a `http://127.0.0.1:5283/llm-acme-com`, y Latet reenvía cada
petición a `https://llm.acme.com`, con la clave de tu empresa tal como la envió Claude Code. Codex
funciona igual, con `openai_base_url` o con un proveedor propio:

```toml title="~/.codex/config.toml"
model_provider = "acme"

[model_providers.acme]
name = "Acme gateway"
base_url = "https://llm.acme.com/v1"
env_key = "ACME_API_KEY"
```

Solo se mueve la dirección de ese proveedor; su clave, y todo lo demás, se quedan como los escribiste.
Un proveedor escrito en el archivo de un perfil (`~/.codex/work.config.toml`) se mueve ahí, y
`latet codex --profile work` pasa por Latet los proveedores de ese perfil y su `openai_base_url`.

## Amazon Bedrock

Claude Code en Bedrock (`CLAUDE_CODE_USE_BEDROCK=1`) demuestra quién es de una de dos maneras, y Latet
gestiona las dos.

- **Una clave de API de Bedrock** (`AWS_BEARER_TOKEN_BEDROCK`) viaja con cada petición como una
  contraseña: pasa por Latet tal cual.
- **Tu inicio de sesión de AWS** (un perfil de AWS, un inicio de sesión SSO, claves en `~/.aws`):
  Claude Code firma cada petición para la dirección a la que la envía. Una firma hecha para la
  dirección de Latet no valdría en Amazon, como una carta certificada a la que se le ha cambiado la
  dirección. Por eso Claude Code envía a Latet peticiones sin firmar (`CLAUDE_CODE_SKIP_BEDROCK_AUTH=1`),
  y Latet firma cada una para Amazon, después de comprobarla, con tu perfil de AWS.

Para firmar, Latet lee tu perfil como lo hacen las propias herramientas de Amazon: con la AWS CLI
(`aws configure export-credentials`) cuando está instalada, lo que cubre los inicios de sesión SSO y
los roles, o a partir de las claves escritas en `~/.aws`. Latet busca `aws` en su propio PATH, y después
en `/usr/local/bin`, `/opt/homebrew/bin`, `/usr/bin`, `~/.local/bin` y `/snap/bin` (en
`C:\Program Files\Amazon\AWSCLIV2` en Windows): el Latet que arranca al iniciar sesión no ve el PATH de
tu shell. Si tu AWS CLI está en otro sitio, inicia Latet desde un shell en el que funcione `aws`. Las
credenciales se quedan en la memoria de Latet, nunca se escriben en ningún sitio y solo firman
peticiones a Amazon Bedrock. Cuando caduca tu inicio de sesión, Claude Code lo dice con palabras
claras: `run aws sso login --profile work`; una vez que lo hayas ejecutado, vuelve a funcionar sin
reiniciar nada. Los ajustes `awsAuthRefresh` y `awsCredentialExport` de Claude Code no se usan mientras
Latet firma: Latet lee tu perfil de AWS por sí mismo, así que ejecuta tú el inicio de sesión que
habrían ejecutado ellos. `latet doctor` avisa cuando están definidos.

La ruta que firma lleva una clave que solo tu Latet puede generar
(`/bedrock/eu-west-1/profile/work/<key>`), así que ningún otro programa ni usuario de tu equipo puede
conseguir que se firmen peticiones en tu nombre. Las credenciales que solo existen en las variables de
tu shell (aws-vault, por ejemplo) se las pasa `latet claude` a Latet para esa sesión. Latet las guarda
en memoria, y solo mientras esa sesión está en marcha: `latet claude` mantiene una conexión abierta con
Latet, y cuando termina (aunque lo mates) Latet olvida las credenciales en pocos segundos. También las
olvida cuando caducan, si tu herramienta les pone una caducidad (`AWS_SESSION_EXPIRATION` o
`AWS_CREDENTIAL_EXPIRATION`). Después, Claude Code dice con palabras claras que las credenciales ya no
están: renuévalas y vuelve a iniciar `latet claude`. `global-install` necesita un perfil de AWS, que
Latet puede leer por sí mismo.

Con tu inicio de sesión de AWS, escribir en tus ajustes la dirección pública de Bedrock
(`ANTHROPIC_BEDROCK_BASE_URL` con el valor `https://bedrock-runtime.eu-west-1.amazonaws.com`) equivale a
no escribir ninguna: Latet firma para ella. Se usa la región de la dirección, aunque `AWS_REGION` indique
otra: la dirección es el lugar al que van las peticiones. La dirección FIPS de Bedrock y un punto de
conexión de VPC son otros casos: consulta
[Dónde Latet no puede ponerse delante](#dónde-latet-no-puede-ponerse-delante).

Las peticiones a Bedrock se comprueban, nunca se reescriben: vienen de Claude Code, cuyo plugin ya las
ha enmascarado. Las respuestas de Bedrock llegan a Claude Code exactamente como las envió Amazon; Latet
solo las lee para mostrarlas en el panel. Los ID de modelo y los ARN de los perfiles de inferencia
llegan a Amazon exactamente como los escribió Claude Code.

## Google Vertex

Claude Code en Vertex (`CLAUDE_CODE_USE_VERTEX=1`) envía tu inicio de sesión de Google como un token,
que pasa por Latet tal cual: no hay nada que firmar. Cada petición indica su ubicación, y Latet la
envía a la dirección de Google que le corresponde: el punto de conexión global, las multirregiones
`eu` y `us`, o una región como `us-east5`. También funcionan modelos distintos en regiones distintas.

## Microsoft Foundry y Azure OpenAI

Claude Code en Microsoft Foundry (`CLAUDE_CODE_USE_FOUNDRY=1`, con `ANTHROPIC_FOUNDRY_RESOURCE` o
`ANTHROPIC_FOUNDRY_BASE_URL`) y Codex en Azure OpenAI (un proveedor cuyo `base_url` es
`https://<resource>.openai.azure.com/openai/v1`) son destinos como una puerta de enlace: se guarda la
dirección del recurso (`foundry-acme`, `azure-acme`) y la clave pasa tal cual.

## Un modelo en este equipo

Una dirección en este equipo (`http://localhost:11434` para Ollama, por ejemplo) se deja como está: un
modelo que se ejecuta aquí no envía nada fuera, y Latet no puede distinguirlo de un proxy que reenvía
a la nube. Si es un proxy así, LiteLLM por ejemplo, añádelo como destino, y Latet se pone delante:

```sh
latet config add upstreams "{name: litellm, url: http://localhost:4000}"
```

Después inicia una nueva sesión de `latet claude` o de `latet codex`; si configuraste tus agentes con
`latet global-install`, vuelve a ejecutar `latet agents apply`: hasta entonces, sus ajustes siguen
enviando las peticiones directamente al proxy.

Latet te lo recuerda cada vez que deja como está una dirección así. Las direcciones de tu red local y
de internet siempre pasan por Latet.

## Dónde Latet no puede ponerse delante

- **Ajustes gestionados que imponen una dirección.** Cuando los ajustes gestionados de tu organización
  dan a `ANTHROPIC_BASE_URL` (o a la dirección de una nube) otro valor, nada puede anularlos. Latet lo
  dice y no inicia Claude Code. Tu departamento de TI puede hacer que esa dirección apunte a Latet,
  como se muestra más abajo.
- **La API Mantle de Amazon Bedrock** (`CLAUDE_CODE_USE_MANTLE`): Latet todavía no la lee. Usa la API
  Invoke de Bedrock (`CLAUDE_CODE_USE_BEDROCK`) o la puerta de enlace de tu empresa.
- **Amazon Bedrock a través de un punto de conexión de VPC** (un `ANTHROPIC_BEDROCK_BASE_URL` en
  `vpce-….vpce.amazonaws.com`): todavía no se admite. Latet tendría que firmar para ese punto de
  conexión, y Amazon rechaza una firma hecha para la dirección de Latet. Latet lo dice y no inicia
  Claude Code: usa la dirección pública de Bedrock, o la puerta de enlace de tu empresa. Con una clave
  de API de Bedrock (`AWS_BEARER_TOKEN_BEDROCK`) no hay que firmar nada, y el punto de conexión es un
  destino como cualquier dirección de empresa.
- **La dirección FIPS de Amazon Bedrock** (un `ANTHROPIC_BEDROCK_BASE_URL` en
  `bedrock-runtime-fips.us-east-1.amazonaws.com`): todavía no se admite. Latet solo puede enviar a la
  dirección estándar, que no es un punto de conexión FIPS, así que lo dice y no inicia Claude Code, en
  lugar de saltarse tu requisito de FIPS. Usa la puerta de enlace de tu empresa, o la dirección
  estándar si no necesitas FIPS. Con una clave de API de Bedrock (`AWS_BEARER_TOKEN_BEDROCK`) no hay
  que firmar nada, y la dirección FIPS sigue siendo el destino, como cualquier dirección de empresa.
- **Más de una nube a la vez** (`CLAUDE_CODE_USE_BEDROCK` y `CLAUDE_CODE_USE_VERTEX`, por ejemplo):
  Latet no puede saber cuál usa Claude Code, así que lo dice y no lo inicia. Deja solo una.
- **Una dirección que dos programas podrían leer de forma distinta** (con una barra invertida, un
  espacio, un nombre de usuario o una contraseña dentro), o un servidor fuera de tu red local al que se
  llega por `http`: Latet no la enruta, y dice por qué.
- **La pestaña Code de Claude Desktop** ignora estas variables: ahí el plugin enmascara, pero el daemon
  no ve las peticiones (consulta [Claude Code](../claude-code/#cobertura)).
- **OpenCode** pasa por Latet con Anthropic, OpenAI y OpenRouter; sus demás proveedores, no.

## Latet para toda una empresa

Donde los ajustes gestionados deciden la dirección de Claude Code, el departamento de TI puede hacer
que apunte al Latet de cada equipo, y dar a cada Latet el punto de conexión de la empresa como destino:

```json title="managed-settings.json"
{ "env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:5283/llm-acme-com" } }
```

```sh
latet config add upstreams "{name: llm-acme-com, url: https://llm.acme.com}"
```

Cada equipo sigue necesitando el plugin de Latet para Claude Code: ejecuta `latet global-install` en él,
o inicia Claude Code con `latet claude`. Sin el plugin, las peticiones de Claude Code llegan a Latet sin
enmascarar, así que Latet solo las comprueba y detiene cada una que lleve un valor sensible en claro. La
dirección nombra el puerto 5283: en un equipo cuyo Latet funciona en otro puerto, Latet avisa de que los
ajustes gestionados envían Claude Code a otro sitio, y no lo inicia.

## Comprobaciones por el camino

- **El plugin comprueba cada sesión.** Cuando Claude Code se inicia, el plugin mira adónde van
  realmente sus peticiones, ajustes incluidos. Si el `.claude/settings.json` de un proyecto establece
  adónde van (una dirección, una nube, un proxy), el plugin detiene sus prompts e indica el archivo,
  aunque apunte a Latet: en su lugar, pon esa línea en tus propios ajustes. Un proyecto tampoco puede
  hacer que el plugin apunte a otro Latet. Cuando la propia comprobación falla, los prompts también se detienen.
- **`latet agents status`** muestra la ruta de cada agente. **`latet doctor`** también la muestra, junto
  con una variable de tu shell que los ajustes de Claude Code anulan (no la dirección de tu empresa
  cuando Latet ha puesto la suya delante: así es como debe ser), y si la AWS CLI está presente cuando
  Latet firma para Bedrock.
- **El panel** enumera tus destinos en Agentes protegidos → Adónde van las peticiones, con el agente que
  indicó cada uno y cuándo se usó por última vez, y muestra en cada intercambio adónde fue y si Latet
  lo firmó. Quitar uno ahí elimina ese destino y ningún otro. Uno que sigue en uso no se puede quitar:
  los ajustes de Claude Code o de Codex, o los ajustes gestionados de tu organización, apuntan a él, o
  se registró después de que Latet se iniciara, por una sesión de `latet claude` o de `latet codex`,
  por `latet agents apply` o por `latet global-install`. Latet no ve un destino que solo usa otro
  agente (OpenCode, Hermes, un programa iniciado con `latet run`): comprueba que ninguno lo use antes de
  quitarlo.
- **Un destino no se mueve sin ti.** Cambiar la dirección de un destino, o quitarlo, fuera del panel
  (el comando de un agente, `config.yaml` editado a mano) espera tu aprobación en Aprobaciones, con la
  dirección de antes y la de después: enviaría tus peticiones, y tus claves, a otro sitio. Añadir uno
  no: solo lo usa un agente que apunte a él. Si hay un cambio en espera cuando un agente añade uno,
  Aprobaciones vuelve a mostrar el cambio, con el nuevo destino dentro y lo que movería ahora; un
  cambio que ya no reduce nada se aplica en el acto.

## Con qué se ha probado

El Claude Code real (2.1.294) y el Codex real (0.161.0), a través de Latet, contra proveedores que
responden como cada nube, en un solo equipo: la puerta de enlace de una empresa, Bedrock con una clave
de API, con un perfil para el que firmó Latet y con el ARN de un modelo, Vertex, y un proveedor de
Codex con la forma de Azure. Las firmas de Latet coinciden con las de la propia biblioteca de AWS,
petición a petición. Si usas una de estas nubes en el trabajo,
[cuéntanos qué tal te va](https://github.com/Latet-AI/latet/issues).
