---
title: Los agentes y tus ajustes
description: Deja que tu agente consulte y cambie los ajustes de Latet. Lo que añade protección se aplica; lo que la reduce te espera.
---

:::caution[Una barrera de protección, no una cerradura]
Esto permite que un agente te ayude a configurar Latet y tus proyectos sin reducir la protección por
error. No resiste a un agente empeñado en saltárselo, ni a uno engañado por una inyección de
prompts: el agente se ejecuta con tu usuario. Un límite de seguridad real requiere más, como que
Latet se ejecute con otro usuario o en otro equipo: consulta
[qué valor tiene esto](#cómo-lo-reconoce-latet-y-qué-valor-tiene).
:::

Puedes pedirle a tu agente que configure Latet por ti:
*"enmascara en todas partes a nuestro cliente Falcon"*, *"activa el Revisor en directo en este proyecto"*,
*"¿por qué se ha enmascarado este valor?"*. Remítelo a
[`llms-config.md`](https://github.com/Latet-AI/latet/blob/main/llms-config.md), en el repositorio:
está escrito para agentes, y les dice qué comandos usar y qué preguntarte antes.

## Lo que un agente puede hacer por su cuenta

Consulta con `latet config get`, `latet project show` y `latet status`, y hace cambios con
`latet config set`, `add` y `remove`. Cada valor se valida con el esquema de los ajustes, y el
cambio se aplica al momento al daemon en ejecución, **siempre que añada protección**: un término, un
reconocedor, un perfil más estricto, que el revisor local lea más, que se active el
Revisor en directo.

```sh
latet config add terms '{value: Project Falcon, type: PROJECT}'
latet config set live_review true --project .
```

## Lo que espera tu aprobación

Un cambio que **reduce** la protección no se aplica hasta que lo apruebas, lo haga quien lo haga:

- un comando `latet` ejecutado por un agente, o por un script;
- `config.yaml` editado a mano, cuando el daemon lo vuelve a cargar;
- `config.yaml` modificado, si el daemon lo encuentra así al iniciarse: hasta que decidas, funciona
  con los últimos ajustes que aprobaste.

Son cambios que reducen la protección, por ejemplo, un valor que no se enmascara nunca, un perfil
más bajo, un término o un reconocedor eliminado, una política cambiada a `allow`, el revisor local o
el Revisor en directo desactivados o leyendo menos, Latet desactivado en un proyecto, el servidor
del revisor local trasladado a otro equipo, o un cambio en [sus indicaciones](../llm-review/#tus-indicaciones):
un texto libre podría decirle que no señale nada, así que cualquier cambio espera, y el panel
muestra las indicaciones antes y después. Lo mismo vale para un
[destino](../../agents/company-endpoints/) trasladado a otra dirección, o eliminado: un agente que
apunta a él enviaría sus peticiones, y sus claves, a otro sitio, así que el panel muestra la
dirección antes y después.

El cambio espera en la página **Aprobaciones** del panel, en *Ajustes*, con lo que reduce, de dónde
viene (el agente que ejecutó el comando, cuando Latet puede saberlo) y qué ajustes toca. **Aprobar**
lo aplica. **Deshacerlo** deja `config.yaml` como estaba. Solo espera un cambio cada vez; uno más
reciente lo sustituye. En un terminal tuyo, en cambio, es el comando el que te pregunta.

Al agente se le dice qué reduce el cambio, nunca un valor, y que espera tu aprobación: la guía le
pide que te diga qué ha cambiado y por qué.

**Un repositorio que Latet no puede leer ahora.** Un cambio también se pesa en cada repositorio con
un `latet.yaml` propio que Latet ha visto. Un proceso auxiliar vuelve a leer sus archivos, con 10
segundos para todos y 2 para cada uno, en una recarga o un arranque cuyo cambio podría reducir la
protección en un repositorio, y en una importación en el panel (consulta
[Los repositorios que Latet recuerda](../projects/#los-repositorios-que-latet-recuerda)). Cuando uno
no se puede leer en ese tiempo, o no se puede leer en absoluto por ahora (una carpeta compartida que
no responde, un permiso denegado), un cambio que allí podría reducir la protección ni se aplica ni
espera tu aprobación: `latet reload` lo dice y no cambia nada, y lo mismo una importación en el
panel, hasta que el repositorio se pueda volver a leer. Un cambio que no puede reducir nada en un
repositorio (un término añadido, el puerto, el historial) se aplica como siempre. Un arranque que
encuentra `config.yaml` cambiado y no puede pesarlo así ejecuta los ajustes que aprobaste por última
vez. `latet repositories` muestra de qué repositorio se trata, y `latet repositories forget` lo saca
de la lista, en un terminal tuyo.

## Lo que los agentes no pueden hacer

`latet global-uninstall`, `latet agents revert` y `latet service uninstall` quitan la protección a
todos los agentes a la vez: si se ejecutan en un agente, se niegan, y te piden que los ejecutes en
un terminal tuyo. Lo mismo hacen `latet repositories forget`, `latet backup` y `latet restore` (tu
caja fuerte y su clave), `latet connect` y `latet disconnect` (dónde se enmascaran las peticiones de
tus agentes), `latet dashboard` y `latet identity send` en un equipo conectado, la administración de
`latet server`, y `latet rules suggest --show-values`, que mostraría valores reales.

## Cómo lo reconoce Latet, y qué valor tiene

Un agente marca los shells en los que ejecuta comandos con variables propias, que Latet reconoce:
Claude Code, Codex, OpenCode, Gemini CLI, Cursor, Hermes Agent, Goose, OpenClaw, y cualquier agente
que defina `AGENT`. El daemon también indica el agente en el cambio que te muestra.

Son protecciones contra los errores, no cerraduras:

- **La comprobación que cuenta es la del daemon.** Compara cada configuración nueva con la última
  que aprobaste, la haya escrito quien la haya escrito. Un agente que edita `config.yaml` en lugar
  de usar los comandos recibe la misma respuesta.
- **Un agente se ejecuta con tu usuario.** Uno que se lo proponga, o que esté engañado por un texto
  que ha leído (una inyección de prompts), puede quitar sus variables, leer los archivos que lee
  Latet y aprobar su propio cambio a través de la interfaz local del daemon. Latet no puede impedir
  lo que puede hacer tu propia cuenta de usuario: para eso hace falta que Latet se ejecute con otro
  usuario, o en otro equipo ([Latet en un servidor](../server/)).

Para un agente que supervisas, esto basta: no puede debilitar Latet por error, y ves cada cambio
que reduce la protección antes de que se aplique. Consulta [Seguridad](../../security/) para el
resto del modelo de amenazas.

## En un equipo conectado

Los ajustes compartidos y los ajustes y reglas propios de cada identidad residen en el servidor.
Usa `latet dashboard` para una persona y la administración `latet server` para los ajustes
compartidos o los de una clave de API. Mientras hay conexión, `config`, `rules`, `llm`, `project`,
`identity`, `repositories` y `reload` rechazan tanto leer los ajustes como cambiarlos; solo funcionan
`config schema`, `rules test` e `identity send`. `latet identity send` reenvía
explícitamente los datos de identidad de este equipo.

Metadatos de conexión, huella del certificado, credenciales y vinculaciones de agentes
siguen en el equipo. `LATET_CONNECTION` elige metadatos, no un archivo del token personal.
La integración de Claude escribe el token en Windows en sus ajustes de usuario como excepción de
instalación documentada (en Linux y macOS no se escribe ningún token, porque Claude Code pasa por un
servidor solo desde Windows); desconectar elimina la vinculación. Destinos del revisor del
servidor, claves, certificados e instalación OCR/NER corresponden a los administradores.
Un proyecto en el panel no permite al servidor escanear el repositorio del equipo.
Consulta [configuración del servidor](../../getting-started/server/) y
[ajustes compartidos y reglas propias](../server/).

## Comandos

| Comando | Qué hace |
| --- | --- |
| `latet config get [KEY]` | Un ajuste (con puntos para lo que va dentro: `llm_review.enabled`), o todos |
| `latet config set KEY VALUE` | Cambia un ajuste (el valor en YAML); `--unset` vuelve a poner su valor predeterminado |
| `latet config add KEY VALUE` / `remove KEY VALUE` | Añade un elemento a una lista, o quita uno (los términos por valor, los reconocedores por nombre) |
| `--project FOLDER` | Tus propios ajustes para el proyecto de esa carpeta, en lugar de los globales |
| `--wait` | Cuando el cambio espera tu aprobación, espera tu respuesta |
| `latet reload` | Aplica un `config.yaml` editado a mano (lo que reduce la protección espera tu aprobación) |

Códigos de salida: `0` aplicado, o guardado mientras el daemon no se está ejecutando (se aplica
cuando el daemon arranca, y lo que reduce la protección espera entonces tu aprobación); `3` espera tu
aprobación; `1` el daemon rechazó el cambio (guardado, no aplicado: `latet reload` dice por qué), o el
elemento que se quiere quitar no está; `2` no válido (no se guarda nada).
