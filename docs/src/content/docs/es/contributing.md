---
title: Contribuir
description: Prepara un entorno de desarrollo, ejecuta las comprobaciones, mantén la documentación al día y añade un idioma.
---

Antes de empezar con algo grande, abre una incidencia para comentarlo.

## Entorno de desarrollo

Necesitas [uv](https://docs.astral.sh/uv/), que instala la versión de Python que usa el proyecto.

```sh
git clone https://github.com/Latet-AI/latet.git
cd latet
uv sync
```

Ejecuta Latet desde la copia del código fuente con `uv run`:

```sh
uv run latet serve       # the daemon in the foreground, with its log in the terminal
uv run latet claude      # Claude Code with the plugin from src/latet/claude_plugin
uv run latet doctor
```

`latet serve --passthrough` reenvía el tráfico sin enmascararlo, y `--capture <folder>` escribe cada
intercambio en archivos locales (con las cabeceras de credenciales suprimidas), lo que ayuda cuando
estudias un protocolo. Las capturas contienen el contenido de las conversaciones: no las incluyas
nunca en un commit (por eso git ignora `notes/captures/`).

Para mantener una instancia de prueba aparte de un Latet que uses de verdad, define `LATET_DATA_DIR` y
`LATET_CONFIG` con una carpeta temporal y elige otro puerto en ese `config.yaml`.

## Comprobaciones

Ejecútalas antes de abrir una pull request. La CI las ejecuta en sus trabajos de lint y de pruebas,
las pruebas en Windows, macOS y Linux con más ejemplos de pruebas de propiedades
(`HYPOTHESIS_PROFILE=ci`). La CI también ejecuta `uv run python scripts/gen_notices.py --check`, las
pruebas del servidor y las comprobaciones del plugin, del panel y de la documentación que se describen
más abajo, y construye los instaladores y la imagen del contenedor.

```sh
uv run pytest
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pyright
uv run python scripts/gen_reference.py --check
```

- **Páginas de referencia.** Las páginas [CLI](../reference/cli/), [configuración](../reference/configuration/)
  y [detectores](../reference/detectors/) se generan a partir del código. Después de cambiar un
  comando, una clave de configuración o un detector, regenéralas con
  `uv run python scripts/gen_reference.py`. No las edites nunca a mano.
- **Plugin de Claude Code.** `claude plugin validate src/latet/claude_plugin` valida el plugin
  con una versión instalada compatible. `claude plugin test`, ejecutado en
  `src/latet/claude_plugin/`, no está disponible en todos los entornos instalados; úsalo solo si ese
  entorno ofrece el comando de pruebas.
- **Falsos positivos.** `tests/unit/test_fp_budget.py` ejecuta los detectores sobre la biblioteca
  estándar de Python, que no contiene secretos reales, con un presupuesto por perfil que solo puede
  bajar. `uv run python scripts/fp_corpus.py` imprime lo que se dispara en un corpus más amplio,
  regla por regla.
- **Avisos de terceros.** Después de cambiar las dependencias, ejecuta `npm ci` en `dashboard/` y en
  `docs/`, y regéneralos con `uv run python scripts/gen_notices.py`. Añade `--check` para comprobar
  que el archivo está al día sin reescribirlo; la CI ejecuta esa comprobación.

Regla básica: las pruebas usan solo datos sintéticos. No incluyas nunca en un commit secretos reales,
datos personales reales ni capturas de tráfico.

## Pruebas del servidor

Usa Docker y el script existente para servicios de prueba aislados: PostgreSQL,
Valkey y PostgreSQL con TLS. El primer arranque descarga las imágenes que falten:

```sh
uv run python scripts/server-services.py up
uv run python scripts/server-services.py env
```

Configura `LATET_TEST_DATABASE_URL`, `LATET_TEST_VALKEY_URL` y `LATET_TEST_TLS_DATABASE_URL`
mostradas en la shell de pruebas, con su sintaxis. Identifican servicios de prueba desechables,
nunca datos de producción. Configura `LATET_TEST_REQUIRE_SERVICES=1` y ejecuta:

```sh
uv run pytest -q -m server
```

Cada prueba usa su propio esquema de base de datos y prefijo Valkey. El trabajo CI `server`
proporciona y exige los servicios: un servicio no disponible debe producir un fallo,
no una omisión silenciosa. Detén tus servicios creados por el script con
`uv run python scripts/server-services.py down`; coordina los servicios compartidos con quien los gestione.

Las pruebas de agentes reales son opcionales, con `LATET_AGENT_TESTS=1` y
`uv run pytest -m agents`. Ejecutan programas de terceros instalados contra proveedores
simulados locales, pero esos programas pueden contactar con sus propios hosts externos.
Usa directorios personales aislados y credenciales sintéticas; las fixtures locales
no garantizan ausencia de tráfico externo. Las pruebas marcadas `live` usan los modelos locales de
detección de nombres y de reconocimiento de texto: se omiten donde esos modelos no están instalados,
como en la CI.

## Panel

El panel está en `dashboard/` (React, Vite e [i18next](https://www.i18next.com)) y necesita
Node.js 22.12 o posterior.

```sh
cd dashboard
npm ci
npm run dev:mock    # the dashboard with sample data, no daemon needed
npm run lint
npm run check:i18n  # every key the code uses is in en.json, and every language has its keys
npm test
npm run build       # writes the files the daemon serves into src/latet/web/
```

Una cadena nueva va en `en.json` y, traducida, en los archivos de los demás idiomas, en el mismo
cambio: `check:i18n` exige las mismas claves en todos los idiomas.

## El tutorial

Los capítulos de `latet tutorial` son archivos YAML en `src/latet/tutorial/chapters/`: el prompt y
la explicación de cada paso, la respuesta del modelo simulado, y las comprobaciones que deben
cumplirse después. Las pruebas reproducen cada capítulo de principio a fin con el enmascaramiento
real, dos veces, así que fallan si un cambio en Latet hace que un capítulo deje de ser cierto: cambia
el capítulo junto con el comportamiento. También rechazan un capítulo que nombre un lado de la
pantalla (en una pantalla estrecha, la página apila sus recuadros) o que contenga un valor que un
escáner de secretos tomaría por la clave de un proveedor.

Desde una copia del código fuente, `uv run latet tutorial` necesita el panel compilado
(`npm run build` en `dashboard/`). El modo mock del panel también reproduce los capítulos, con
`?mock-tutorial`, a partir de su propia copia en `dashboard/src/mock/tutorial.ts`: cámbiala junto con
el capítulo.

## Documentación

El sitio está en `docs/` y se construye con [Starlight](https://starlight.astro.build). Necesitas
Node.js 22.12 o posterior.

```sh
cd docs
npm ci
npm run dev     # http://localhost:4321/
npm run build   # static site in docs/dist/
```

Las páginas son archivos Markdown o MDX en `docs/src/content/docs/`. La barra lateral se define en
`docs/astro.config.mjs`.

**El inglés es la versión de referencia.** Una pull request que cambia lo que hace Latet actualiza las
páginas en inglés a las que afecta, en la misma pull request: una opción, un comando o un
comportamiento nuevo se documenta en inglés antes de integrarse. Las traducciones siguen a las
páginas en inglés, nunca al revés. Una pull request puede cambiar solo las páginas en inglés: los
mantenedores ponen al día los demás idiomas.

**Las capturas de pantalla del panel** las hace un script, a partir de sus datos de ejemplo, en todos
los idiomas y en los dos temas: después de un cambio que se vea en alguna de ellas, ejecuta
`npm run screenshots` en `dashboard/` (`npm run screenshots -- live it` solo rehace algunas). La
primera vez, `npx playwright install chromium` descarga el navegador que usa. Una página MDX muestra
una con `<Screenshot name="live" alt="…" />`, después de importar
`docs/src/components/Screenshot.astro`, como hace `dashboard.mdx`.

## Cómo añadir un idioma

### Documentación

La documentación está disponible en cinco idiomas: el inglés en la raíz, como referencia, y el
italiano, el alemán, el francés y el español en `/it/`, `/de/`, `/fr/` y `/es/` (los `locales` de
`docs/astro.config.mjs`). Una página que aún no está traducida muestra la inglesa, con un aviso. Las
páginas de referencia generadas a partir del código siguen en inglés en todos los idiomas.

- **Traducir una página** es crearla en `docs/src/content/docs/<lang>/`, en la misma ruta que la
  inglesa: `concepts/how-it-works.mdx` pasa a ser `it/concepts/how-it-works.mdx`. Sus importaciones
  y las rutas de sus imágenes llevan un `../` más, ya que el archivo está una carpeta más abajo.
- **Los enlaces entre páginas siguen siendo relativos**, así que una página traducida los mantiene en
  su idioma. Un encabezado traducido cambia su ancla, así que los enlaces a él también cambian:
  `scripts/check_docs_links.py` encuentra los que se hayan pasado por alto (ejecútalo desde la raíz
  del repositorio después de `npm run build`; la CI también lo ejecuta).
- **Las cadenas de la interfaz** que añade Latet están en `docs/src/content/i18n/<lang>.json`, y las
  etiquetas de la barra lateral, en las `translations` de `docs/astro.config.mjs`. La propia interfaz
  de Starlight ya está traducida.
- **Otro idioma** es una entrada más en `locales`, su `docs/src/content/i18n/<lang>.json` y sus
  `translations` de la barra lateral.

Ejecuta `npm run build` y comprueba el resultado con `npm run preview`.

### Panel

El panel está disponible en inglés, italiano, alemán, francés y español. Para añadir otro idioma
(aquí, el portugués):

1. Copia `dashboard/src/locales/en.json` en `pt.json` y traduce los valores. Mantén las claves y las
   `{{variables}}`, y añade todas las formas de plural que necesite el idioma (`key_many` en
   portugués).
2. Ejecuta `npm run check:i18n`: enumera las claves que faltan o que sobran, las variables que
   difieren y las formas de plural que le faltan al archivo.
3. Regístralo en `dashboard/src/i18n.ts`, con su nombre en su propio idioma (`LANGUAGE_NAMES`), y
   añádelo a los idiomas que espera `dashboard/src/i18n.test.ts`. Ese archivo de pruebas usa el
   portugués como un idioma que el panel no tiene (`pt-BR`, `docsUrl(…, 'pt')`): cambia esos casos a
   otro idioma. Mientras la documentación no exista en el nuevo idioma, haz que `docsUrl` lleve a las
   páginas en inglés para él; cuando exista, añade el idioma a `LANGUAGES` en
   `dashboard/scripts/screenshots.mjs`.

El panel se abre en el idioma elegido en su menú; si no hay ninguno, en el primero de los idiomas del
navegador que tenga; y si no, en inglés. `dashboard/README.md` tiene los detalles.

### Tutorial

Los textos de un capítulo en otro idioma están en `src/latet/tutorial/chapters/<lang>/<chapter>.yaml`:
sus títulos, sus explicaciones y sus prompts, las frases del agente y los turnos del modelo, paso a
paso, cada uno con los marcadores de posición de su texto en inglés. El resto del capítulo, sus
valores y sus comprobaciones, viene del archivo en inglés. Quien cambia los textos en inglés de un
capítulo los cambia en todos los idiomas. Las pruebas reproducen cada capítulo en cada idioma, y
exigen a cada idioma sus propias reglas editoriales (`tests/unit/tutorial/test_chapters.py`). Un
idioma nuevo necesita también sus propias reglas en ese archivo, en `ON_SCREEN` y `JARGON`, o las
pruebas fallan.

Una pull request puede cambiar solo los textos en inglés de un capítulo. Un texto que gana o
pierde un marcador de posición necesita el mismo cambio en los demás idiomas, o fallan las pruebas;
los mantenedores traducen el resto.

## Cómo añadir un país

Los números nacionales de identidad y de identificación fiscal están en
`src/latet/engine/detectors/ids/regions/`, un módulo por país, cada uno una tupla de `IdSpec`. Los
números de IVA están en `ids/vat.py`.

1. Escribe `regions/<cc>.py` (el código ISO 3166-1 alfa-2): para cada número, un patrón de cómo se
   escribe, un validador, un generador de valores ficticios válidos con el mismo formato, sus
   palabras clave, y cuándo se exige una palabra clave (`context`: `never`, `bare`, `code` o
   `always`). Escribe el validador a partir de la especificación pública de la autoridad emisora.
   python-stdnum es una referencia solo para las pruebas, con licencia LGPL: no copies nunca su
   código.
2. Registra el módulo en `regions/__init__.py`, y el país en `ID_REGIONS`, en
   `src/latet/config.py`.
3. Añade un formato y el módulo de stdnum, si lo hay, a `tests/unit/test_ids_oracle.py`, y una
   palabra clave en los casos de `test_ids.py` si hace falta. La prueba oráculo comprueba 200
   valores ficticios por tipo y compara el validador con stdnum en números próximos a los válidos;
   enumera ahí las diferencias deliberadas.
4. Añade la etiqueta del tipo a los cinco idiomas del panel, `dashboard/src/locales/<lang>.json`
   (`rules.pii.types`), a `PII_TYPES` en `dashboard/src/lib/labels.ts` y su descripción a `PII_HELP`
   en `dashboard/src/pages/RulesPage.tsx`; añade también el tipo y el país al catálogo del mock
   (`PII_CATALOG` e `id_regions_available` en `dashboard/src/mock/fixtures.ts`, `ID_TYPES` en
   `dashboard/src/mock/builder.ts`). Después ejecuta `uv run python scripts/gen_reference.py` para las
   tablas de referencia.
5. Ejecuta el presupuesto de falsos positivos (`tests/unit/test_fp_budget.py`): ninguna regla de
   números de identidad puede dispararse con la muestra de la biblioteca estándar.

## Cómo medir un revisor local

Qué modelo local funciona mejor como [revisor local](../concepts/llm-review/#cuánto-tarda-y-qué-modelo-usar)
sigue siendo una cuestión abierta, y tus mediciones son bienvenidas. Sirve un modelo en este equipo
(llama.cpp, Ollama o LM Studio), apunta `LATET_LLM_URL` y `LATET_LLM_MODEL` a él, y ejecuta el
benchmark con sus 93 casos inventados:

```sh
uv run python scripts/llm_review_bench.py run --label <model> --variants A
uv run python scripts/llm_review_bench.py report
```

Envía lo que ha escrito en `benchmarks/llm-review/` (tus filas en `results/`, y `tables.md`) en una
pull request, con tu tarjeta gráfica o tu CPU, el servidor y la cuantización del modelo.
`benchmarks/llm-review/README.md` explica cómo se obtuvieron las cifras publicadas.

## Licencia

Latet se distribuye bajo la Latet Sustainable Use License, su Enterprise Edition bajo la Latet
Enterprise License, y Latet también se ofrece bajo licencias comerciales ([LICENSING.md](https://github.com/Latet-AI/latet/blob/main/LICENSING.md)).
Las contribuciones se aceptan bajo el [acuerdo de licencia de contribuidor](https://github.com/Latet-AI/latet/blob/main/CLA.md):
conservas tus derechos de autor, y tu contribución puede formar parte de Latet bajo todas ellas.
Para aceptarlo, marca la casilla "I agree to the Latet contributor license agreement (CLA.md)." en la
plantilla de tu primera pull request.
