---
title: Reconocedores personalizados
description: Enseña a Latet un formato propio, como un número de empleado o un código de contrato.
---

Latet conoce los secretos, los datos personales y los números de identificación nacionales, pero no
los formatos de tu empresa: números de acreditación, códigos de contrato, identificadores de
clientes. Un **reconocedor** le enseña uno. Es un patrón, opcionalmente con dígitos de control y
palabras clave, y unos cuantos ejemplos inventados con los que se comprueba.

Usa en su lugar un [término](../detection/#tus-propios-términos) cuando haya un único valor exacto que
ocultar, como el nombre de un cliente. Un reconocedor sirve para todos los valores escritos de una
determinada forma.

## No pegues nunca valores reales

Un reconocedor describe un formato. Sus ejemplos son inventados: escríbelos a partir de la
descripción, o toma un valor real y cambia cada letra y cada dígito, manteniendo la disposición.
`latet rules add` rechaza un ejemplo que Latet ya ha visto como valor real. Cuando sea un agente de
IA quien escriba el reconocedor por ti, descríbele el formato con palabras: dos letras mayúsculas,
un guion, seis dígitos, en la columna badge de la exportación de recursos humanos.

## Encontrar los formatos

¿No sabes bien qué formatos contienen tus archivos? Deja que Latet los busque, en este equipo:

```sh
latet rules suggest ./my-project
```

Enumera los formatos de los valores con aspecto de código que todavía no cubre ninguna regla, como
`AA-999999` (A para una letra, 9 para un dígito), con cuántos valores distintos ha visto, en qué
archivos, los dígitos de control que superan y las etiquetas que tienen al lado
(*badge*, *invoice_ref*). Nunca muestra un valor. En un repositorio git solo lee los archivos que
git conoce, así que los archivos ignorados se quedan fuera. `--yaml` crea un borrador de reconocedor
para cada formato, con ejemplos inventados a partir del formato; describe lo que encuentra cada uno
antes de añadirlo. `--show-values` muestra tres valores de cada formato en tu propio terminal, y se
niega a ejecutarse dentro de un agente de IA o en una tubería.

## Un ejemplo

```yaml
recognizers:
  - name: employee-id
    description: Badge numbers of the HR system
    pattern: '\b[A-Z]{2}-\d{6}\b'
    context:
      words: [badge, employee]
    examples:
      match: ["badge AB-123456", "employee: QX-904417"]
      no_match: ["AB-123456 alone", "build AB-12345"]
```

Ponlo en un archivo aparte, como `rules.yaml`, y después:

```sh
latet rules test rules.yaml     # every example, with the reason when one fails
latet rules add rules.yaml      # saves it in config.yaml and applies it
latet rules list
```

A partir de entonces, `AB-123456` junto a la palabra *badge* se enmascara como `EMPLOYEE_ID`, y el
panel llama a la regla `recognizer:employee-id`. El valor ficticio conserva el formato: dos letras,
un guion, seis dígitos.

La página **Reglas** del panel hace lo mismo en su pestaña *Reconocedores*: una tabla de tus
reconocedores, cada fila con su nombre, su tipo, lo que encuentra (el patrón, acortado), lo que
pasa con sus valores y cómo se cargó. *Añadir reconocedor* abre un recuadro lateral con cada campo:
rellénalo y *Probarlo* con un texto tuyo (no se guarda nada de lo que escribas ahí). Antes de que
guardes, muestra el resultado de cada ejemplo y las coincidencias en código corriente. Selecciona
filas para activarlas, desactivarlas o eliminarlas; *Exportar* e *Importar* las mueven como archivo
de reglas. Con el [revisor local](../llm-review/#reconocedores-a-partir-de-una-descripción)
activado, *Borrador a partir de una descripción* escribe el reconocedor por ti a partir de una frase
(*"números de acreditación: BD, un guion y seis dígitos"*), con ejemplos inventados, y lo abre en el
recuadro si los supera, para que lo revises y lo guardes.

## Para un solo proyecto

Un reconocedor puede pertenecer a un [proyecto](../projects/) en lugar de a todo Latet:

```sh
latet rules add rules.yaml --repo .          # into the project's latet.yaml, shared with the team
latet rules add rules.yaml --project .       # into your own settings for the project
latet rules list --project .
```

La página **Reglas** del panel hace lo mismo una vez que eliges el proyecto en el selector de arriba:
su tabla *Reconocedores* muestra los del proyecto, de tus ajustes y de `latet.yaml`, junto con los
globales, y uno nuevo va donde dice el *Guardar en* de su recuadro.

## Qué tiene un reconocedor

| Clave | Qué hace |
| --- | --- |
| `name` | Letras minúsculas, dígitos y guiones, único. |
| `type` | El tipo que se muestra para sus valores, en mayúsculas. Si no se define, el nombre en mayúsculas. No puede ser un tipo que Latet ya tenga. |
| `pattern` | Una expresión regular [RE2](https://github.com/google/re2/wiki/Syntax). Un grupo llamado `value` elige la parte que enmascarar. |
| `checksum` | Dígitos de control que el valor debe superar: `luhn`, `iban`, `mod97-10`, `mod11-10`, `ean`, `verhoeff`, `damm`, o una suma ponderada. Los valores ficticios también los superan. |
| `context` | Palabras clave (`words`, sin distinguir mayúsculas y minúsculas; `acronyms`, con las mayúsculas exactas) que deben estar en la misma línea, a no más de `window` caracteres, o en la cabecera de la columna de una tabla. |
| `examples` | `match` y `no_match`: textos inventados, que se comprueban cada vez que se cargan las reglas. |
| `class` | `custom`, o `secret` para los valores que nunca deben salir en claro: se enmascaran como de costumbre, y una petición que fuera a llevar uno en claro se detiene, incluso en Claude Code. |
| `policy` | `mask` (el predeterminado), `redact` o `allow`. Una entrada en `policies` tiene prioridad. |
| `surrogate` | `shape`, o `{keep: 3}` para conservar un prefijo tal cual. |
| `enabled` | `false` mantiene la regla en el archivo, desactivada. |

La [referencia de la configuración](../../reference/configuration/) enumera todas las claves, y el
[JSON Schema](https://docs.latet.ai/config.schema.json) permite que tu editor las autocomplete.

## Los patrones son RE2

RE2 se ejecuta en tiempo lineal, así que ningún patrón puede atascar una petición. No tiene
lookarounds (`(?=`, `(?<=`) ni referencias inversas (`\1`). Donde usarías un lookbehind, incluye el
contexto en el patrón y marca el valor con un grupo con nombre:

```yaml
recognizers:
  - name: contract-code
    pattern: '(?i)\bcontract[ #:]+(?P<value>[A-Z]{3}\d{5})\b'
    examples:
      match: ["Contract #KLM48213"]
      no_match: ["contract signed"]
```

Solo se enmascara `KLM48213`. En YAML, escribe los patrones entre comillas simples, para que las
barras invertidas se queden como están.

Un valor necesita una letra o un dígito. Una coincidencia hecha solo de símbolos, como el `---` de
una tabla Markdown, se deja tal cual, porque su valor ficticio sería el propio original;
`latet rules test` y *Probarlo* lo indican.

## Reglas precisas

Una regla que salta por todas partes en código corriente enmascararía el propio código, así que
Latet comprueba cada una con una muestra de la biblioteca estándar de Python: con más de 50
coincidencias, se desactiva. Las palabras clave y los dígitos de control son el remedio. Por
ejemplo, un número cuyo último dígito es un dígito de control Luhn:

```yaml
recognizers:
  - name: card-ref
    type: CARD_REF
    pattern: '\bCR(?P<value>\d{8})\b'
    checksum: luhn
    class: secret
    examples:
      match: ["ref CR12345674"]
      no_match: ["ref CR12345678"]
```

`latet rules test rules.yaml --corpus src/` también indica cuántos valores encontraría la regla en
tus propios archivos. Solo muestra sus formatos (`AA-999999 x12`), nunca los valores.

## Cuando algo falla

Un reconocedor con un error (un patrón que RE2 rechaza, un checksum desconocido, una clave mal
escrita) se desactiva; los demás siguen funcionando, y el archivo lo conserva tal como lo
escribiste. `latet reload`, `latet doctor` y la página **Reglas** del panel indican cuál y por qué,
y `latet rules test` explica cada ejemplo. `latet config validate` comprueba el archivo entero.

¿Escribes reconocedores con un agente de IA? Remítelo a
[`llms-rules.md`](https://github.com/Latet-AI/latet/blob/main/llms-rules.md), en el repositorio: los
mismos pasos, escritos para él.
