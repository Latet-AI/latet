---
title: Referencia de la API para aplicaciones
description: El contrato HTTP de la API para aplicaciones de un servidor de Latet, sus respuestas, sus errores y sus límites.
sidebar:
  label: API para aplicaciones
---

La [API para aplicaciones](../../applications/) de un servidor de Latet responde a cuatro operaciones
bajo `https://<tu servidor>/_latet/apps/v1/`. Esta es la versión 1: un cambio que rompería un programa
escrito para ella llega como `v2`, a su lado. La [descripción OpenAPI](../../../apps-api.openapi.json)
dice lo mismo para las herramientas que generan un cliente; los programas en Python pueden usar
`latet-client` ([registros](../../applications/logs/), [RAG](../../applications/rag/)).

## Autenticación

Cada llamada lleva el token de una clave de API en una sola cabecera:

```http
Authorization: Bearer latet_key_...
```

Un token es `latet_key_` seguido de 43 caracteres, y lo crea el administrador (`latet server keys add`,
`keys add-token`). Cada operación necesita su propio permiso: `scan`, `mask` (que incluye `scan`),
`unmask` o `forget` (que ningún otro incluye, y que el primer token de una clave no tiene). El servidor
rechaza:

- con **401** y `WWW-Authenticate: Bearer`, una llamada sin cabecera `Authorization`, con dos, con un
  esquema distinto de `Bearer`, o con un token que no conoce (revocado, rotado, de una clave
  desactivada o eliminada);
- con **403**, el token del equipo de una persona, un token sin el permiso de la operación, y
  cualquier solicitud que lleve una cabecera `Origin` o `Sec-Fetch-Site`, como las de un navegador: la
  API es para programas, nunca para una página web.

Los tokens que el servidor rechaza cuentan contra la dirección de la que vienen, como en todas las
rutas del servidor: por encima del límite, 429. Llama al servidor por HTTPS; si no, el token y los
textos viajan en claro.

## Solicitudes

| Operación | Ruta | Permiso | Cuerpo |
| --- | --- | --- | --- |
| Análisis | `POST /_latet/apps/v1/scan` | `scan` | `{"texts": [...], "kind": "prose"}` |
| Enmascaramiento | `POST /_latet/apps/v1/mask` | `mask` | `{"texts": [...], "kind": "prose"}` |
| Restauración | `POST /_latet/apps/v1/unmask` | `unmask` | `{"texts": [...]}` |
| Olvido | `POST /_latet/apps/v1/forget` | `forget` | `{"values": [...]}` |

- `texts` es una lista de 1 a 1.000 cadenas, que se responden en el mismo orden. Un texto vacío vuelve
  como está, sin hallazgos y con `restored: 0`.
- `values` es una lista de 1 a 1.000 cadenas, los valores que deben salir de la caja fuerte de la clave,
  cada uno buscado exactamente como está escrito ([olvidar valores](../../applications/forget/)).
- `kind` es `prose` (texto escrito por personas, el predeterminado) o `data` (registros, fichas, la
  salida de un programa); consulta [prosa y datos](../../applications/#prosa-y-datos). `null` u otra
  palabra se rechaza. La restauración no lleva `kind`.
- El cuerpo es un objeto JSON de como mucho el `apps_request_mb` del servidor (8 MiB si sus ajustes no
  dicen otra cosa, nunca más que su `max_request_mb`).

El análisis y la restauración con curl, con el token pasado por la entrada estándar como en
[la primera llamada](../../applications/#una-primera-llamada), que enmascara; sus respuestas están más
abajo:

```sh
printf 'Authorization: Bearer %s' "$(cat shop.key)" | curl -sS -H @- -H "Content-Type: application/json" \
  -d '{"texts": ["Reembolsa el pedido de mario.rossi@acme.it a IT60X0542811101000000123456, por favor."]}' \
  https://latet.acme.lan/_latet/apps/v1/scan
printf 'Authorization: Bearer %s' "$(cat shop.key)" | curl -sS -H @- -H "Content-Type: application/json" \
  -d '{"texts": ["Reembolsa el pedido de tommaso.gallo@margie-6e5a.example a IT17F3945471087320356678487, por favor."]}' \
  https://latet.acme.lan/_latet/apps/v1/unmask
```

## Respuestas

Cada respuesta del análisis, el enmascaramiento y la restauración es un objeto JSON cuyos `results`
contienen una entrada por cada texto enviado, en orden; un olvido responde con dos cifras.

**El análisis** dice dónde están los valores en el texto enviado, y no conserva nada:

```json
{"results": [{"findings": [{"type": "EMAIL", "class": "pii", "start": 23, "end": 42},
                           {"type": "IBAN", "class": "pii", "start": 45, "end": 72}],
              "counts": {"EMAIL": 1, "IBAN": 1}}]}
```

**El enmascaramiento** da cada texto con los valores ficticios de la clave, y dónde están en el texto
devuelto:

```json
{"results": [{"text": "Reembolsa el pedido de tommaso.gallo@margie-6e5a.example a IT17F3945471087320356678487, por favor.",
              "replacements": [{"type": "EMAIL", "class": "pii", "start": 23, "end": 56},
                               {"type": "IBAN", "class": "pii", "start": 59, "end": 86}],
              "counts": {"EMAIL": 1, "IBAN": 1}},
             {"blocked": "the local reviewer could not read the user text (it took longer than the 30 seconds an application's call waits for it, which llm_review.budget_ms changes), so it was not sent"}],
 "degraded": ["llm-review"]}
```

- Un resultado con `blocked` y sin `text` es un texto al que Latet no responde: el revisor local lo
  detuvo, su revisión falló y el `llm_review.on_failure` de la clave es `stop`, o la protección contra
  volcados lo retuvo. El motivo, en inglés, no nombra ningún valor.
- `degraded`, presente solo cuando algo funcionó de forma reducida en la llamada, nombra lo afectado:
  un detector que no pudo leer un texto (como `ner`) o `llm-review`.

**La restauración** da cada texto con cada valor ficticio de la caja fuerte de la clave sustituido
por su valor, y cuántos valores distintos restauró:

```json
{"results": [{"text": "Reembolsa el pedido de mario.rossi@acme.it a IT60X0542811101000000123456, por favor.",
              "restored": 2}]}
```

**El olvido** dice cuántos de los valores guardaba la caja fuerte de la clave, que ya se fueron con sus
valores ficticios, y cuántos no guardaba, contando cada valor una vez:

```json
{"forgotten": 1, "unknown": 1}
```

### Posiciones

`start` y `end` cuentan puntos de código Unicode (`end` excluido): en Python, `text[start:end]` es el
valor. JavaScript, Java y C# cuentan unidades UTF-16, en las que un carácter fuera del plano
multilingüe básico, como la mayoría de los emoji, ocupa dos. En `Escribe a 👩‍💻 mario.rossi@acme.it`
la dirección empieza en 14 en puntos de código y en 16 en unidades UTF-16; en JavaScript la da
`Array.from(text).slice(start, end).join("")`.

### Tipos y clases

`type` es el tipo del valor: `EMAIL`, `PHONE`, `IBAN`, `CREDIT_CARD`, `IP_ADDRESS` y los demás de los
[detectores](../detectors/), la regla de un secreto, o el tipo de uno de los términos, reconocedores o
reglas de campo de la clave. `class` es la familia de lo que lo encontró: `secret`, `custom` (los
términos y reconocedores de la clave), `identity`, `pii`, `ner` (el modelo de nombres), `llm` (el
revisor local) o `struct` (el modo de datos estricto).

## Errores

Un error es `{"error": "<palabras>"}`. Las palabras, en inglés, son para personas: nunca citan un texto
enviado, y los rechazos de la entrada del servidor (autenticación, límites) empiezan por `latet: `.
Decide según el estado.

| Estado | Cuándo | Qué hacer |
| --- | --- | --- |
| 400 | El cuerpo no es un objeto JSON con `texts` una lista de 1 a 1.000 cadenas y, para análisis y enmascaramiento, un `kind` que sea `prose` o `data`; para el olvido, con `values` una lista de 1 a 1.000 cadenas. | Corregir la llamada. |
| 401 | Ningún token, dos cabeceras `Authorization`, otro esquema, o un token que el servidor no conoce. | Comprobar el token; pedir uno nuevo. |
| 403 | Al token le falta el permiso (`this token lacks the mask permission`), es el token de una persona, o una página web hizo la solicitud. | Usar un token con el permiso. |
| 404 | Otra ruta bajo `/_latet/apps/v1/`. | |
| 405 | Otro método distinto de `POST` (`Allow: POST`). | |
| 413 | El cuerpo supera `apps_request_mb`. | Enviar menos textos por llamada. |
| 422 | Análisis, enmascaramiento y restauración: los textos nombran más de 100.000 valores o valores ficticios de la caja fuerte, más de los que una llamada puede sopesar. No se enmascaró ni restauró nada. | Enviar menos textos por llamada. |
| 429 | Demasiados tokens rechazados desde esta dirección, o la clave tiene `requests_in_flight` solicitudes de aplicaciones en esta instancia (16 si los ajustes no dicen otra cosa). | Esperar `Retry-After` y volver a intentarlo. |
| 500 | Otro fallo; sus palabras no nombran ningún valor. | Volver a intentarlo; avisar al administrador si persiste. |
| 503 | Un almacén del servidor no respondió, el espacio de la clave no está disponible ahora en esta instancia, o el registro de administración no aceptó la entrada: en la restauración no se restauró nada, en el olvido no se olvidó nada. | Esperar (`Retry-After` si viene), y volver a intentarlo. |
| 507 | Solo en el enmascaramiento: la caja fuerte de la clave tiene el máximo de valores que puede. No volvió nada de la llamada, y nada salió en claro. | Un administrador sube el límite con `latet server keys limit`. |

Un enmascaramiento o una restauración se pueden repetir sin riesgo: un valor que la caja fuerte
conservó recibe de nuevo el mismo valor ficticio, y un valor que una llamada fallida no conservó
recibe otra vez uno. Un olvido también se puede repetir: un valor que ya no está se cuenta en
`unknown`. Una llamada `mask` fallida puede haber conservado algunos de sus valores nuevos
([la caja fuerte de una clave](../../applications/#la-caja-fuerte-de-una-clave) dice cuándo).

## Cabeceras

- Cada respuesta dice `Cache-Control: no-store`: ninguna caché guarda un texto o un valor.
- `Retry-After`, en segundos, acompaña a cada 429, y a algunos 503.

## Límites

| Límite | Valor | Lo fija |
| --- | --- | --- |
| Textos por llamada, o valores por olvido | 1.000 | Latet |
| Cuerpo de una llamada | 8 MiB | `apps_request_mb` del servidor |
| Solicitudes de aplicaciones simultáneas de una clave en una instancia | 16 | `requests_in_flight` del servidor |
| Valores o valores ficticios de la caja fuerte que pueden nombrar los textos de una llamada | 100.000 | Latet |
| Valores que admite la caja fuerte de una clave | 200.000 | `vault_limit` del servidor, o el propio de la clave: `latet server keys limit` |
| Valores de la caja fuerte de una clave que una instancia guarda en memoria | 5.000 | `key_working_values` del servidor |

## Qué registra el servidor

- **El registro de administración** recibe una entrada `apps.unmasked` por cada llamada de
  restauración, antes de enviar su respuesta: el id del token, cuántos textos y cuántos valores se
  restauraron (`{"token": "t_...", "texts": 3, "restored": 5}`), y la dirección del cliente. Nunca un
  texto ni un valor. El análisis y el enmascaramiento no aparecen en él. Lee esas entradas con
  `latet server log --action apps.unmasked`. Cada llamada de olvido es una entrada `apps.forgotten`,
  escrita en la misma transacción que el olvido: el id del token, cuántos valores se pidieron, se
  olvidaron y no estaban (`{"token": "t_...", "values": 2, "forgotten": 1, "unknown": 1}`), y la
  dirección del cliente.
- **Las métricas** cuentan las solicitudes de la API como `latet_http_requests_total{kind="apps"}`,
  por estado, y sus textos y valores por operación: `latet_apps_texts_total{operation}` y
  `latet_apps_values_total{operation}` (los valores que encontró el análisis, que sustituyó el
  enmascaramiento y que devolvió la restauración; en el olvido, los valores pedidos y los olvidados).
  No tienen una etiqueta para la clave: quién hizo qué
  está en el registro de administración.
- **El registro del servidor** tiene una línea por llamada: el método, la ruta, el estado y cuánto
  tardó, nunca una cabecera, el cuerpo ni un valor. Los rechazos (401, 403, 404, 405, 413, 429, y los
  del propio servidor) se resumen una vez por minuto, con cuántos hubo, en lugar de una línea cada uno.
