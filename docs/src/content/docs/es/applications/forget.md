---
title: Olvidar valores
description: Quitar los valores de una persona de la caja fuerte de una clave de API cuando pide que sus datos desaparezcan.
sidebar:
  label: Olvidar valores
---

La caja fuerte de una clave de API guarda cada valor que ha enmascarado, con su valor ficticio,
mientras la clave existe: una restauración funciona sobre una línea de registro del año pasado. Cuando
una persona pide que sus datos desaparezcan de tus registros o del índice de tu aplicación RAG, el
servidor olvida sus valores. Cada valor sale de la caja fuerte de la clave, su valor ficticio ya no
restaura nada a partir de ese momento, y el valor recibe un valor ficticio nuevo si un texto lo vuelve a
contener.

Hay dos caminos: un administrador ejecuta `latet server keys forget`, o una aplicación llama a la API
para aplicaciones con un token que tiene el permiso `forget`. Los dos hacen lo mismo, y los dos quedan en
el registro de administración con cifras, nunca con un valor.

## El administrador

Pon los valores en un archivo, uno por línea, en UTF-8:

```text
mario.rossi@acme.it
+39 347 123 4567
Mario Rossi
```

```sh
latet server --server https://latet.acme.lan keys forget "Shop" --file erasure.txt
```

El comando dice cuántos valores ha leído, pregunta antes de enviarlos, y luego dice cuántos guardaba la
caja fuerte de la clave y cuántos no. Sin `--file`, te pide que escribas los valores en tu terminal, uno
por línea, y una línea vacía los termina. Nunca toma un valor de la línea de comandos, donde la shell lo
guardaría en su historial. Se niega a funcionar dentro de un agente de IA, porque los valores son datos
de una persona, y en un script necesita `--yes`. Envía los valores en llamadas de 1.000 como máximo.
Cada llamada es una entrada `key.forgotten` del registro de administración, con cuántos valores se
pidieron, se olvidaron y no estaban en la caja fuerte. Borra el archivo cuando termines: contiene los
valores.

El primer olvido de una clave después de una actualización indexa primero lo que el revisor local de
la clave recordaba y lo que su historial guardaba antes de la actualización: en una clave con mucho
historial tarda minutos. El comando espera hasta cinco minutos cada llamada, y la llamada de una
aplicación puede tardar lo mismo. Si una llamada deja de esperar, vuelve a ejecutarla: lo que el
servidor olvidó sigue olvidado, y entonces cuenta como no presente en la caja fuerte.

Funciona tanto con la clave activada como desactivada. La caja fuerte de una persona no se olvida así:
las personas olvidan valores en su panel (Caja fuerte).

## Una aplicación

Da a la aplicación un token propio con `forget`. Ningún otro permiso lo incluye, y el primer token de
la clave, que puede analizar, enmascarar y restaurar, no puede olvidar:

```sh
latet server --server https://latet.acme.lan keys add-token "Shop" erasure --forget --out erasure.token
```

La aplicación envía hasta 1.000 valores por llamada:

```sh
printf 'Authorization: Bearer %s' "$(cat erasure.token)" | curl -sS -H @- \
  -H "Content-Type: application/json" \
  -d '{"values": ["mario.rossi@acme.it", "+39 347 123 4567"]}' \
  https://latet.acme.lan/_latet/apps/v1/forget
```

```json
{"forgotten": 1, "unknown": 1}
```

`forgotten` es cuántos valores guardaba la caja fuerte de la clave, `unknown` cuántos no, contando cada
valor una sola vez. Cada llamada es una entrada `apps.forgotten` del registro de administración (el
token, cuántos valores se pidieron, se olvidaron y no estaban), escrita en la misma transacción en que
se olvidan: si el registro no la acepta, no se olvida nada y la llamada responde 503. Con el cliente de
Python, `latet.forget([...])` envía una lista más larga en varias llamadas y devuelve
`ForgetResult(forgotten, unknown)`. Un token sin el permiso recibe 403.

## Qué se quita

- **Cada valor tal como está escrito.** Otras mayúsculas, otros espacios u otra forma escrita
  (`mario.rossi%40acme.it` en una URL) son otro valor. La caja fuerte guarda cada forma que ha
  encontrado como una entrada propia, así que enumera todas las formas que deben desaparecer.
- **Todo lo que lo nombra en la caja fuerte:** su entrada y su valor ficticio, sellados con la clave
  raíz de la clave de API, los hashes que el índice guarda de sus palabras, y las respuestas recordadas
  del revisor local que encontraron el valor, lo guardara la caja fuerte o no. Después cada instancia lo
  quita de su memoria, junto con los textos que tiene enmascarados con su valor ficticio, en cuanto
  terminan las llamadas que ya están en curso en ella.
- **Lo que los agentes de la clave dejaron en Latet.** Cuando el primer token de la clave sirve a
  agentes a través de la puerta de enlace, la entrada del valor sale también de sus conversaciones, y
  el historial de la clave pierde los textos, las respuestas del modelo y los hallazgos del revisor
  local que lo nombraban, con su valor ficticio antiguo. Un mensaje anterior que un agente envía de
  nuevo se enmascara otra vez, con el valor ficticio nuevo.
- **Su valor ficticio antiguo ya no restaura nada, y ningún otro valor lo recibe nunca.** Una línea de
  registro enmascarada antes conserva el valor ficticio antiguo, y la restauración la deja como está.
  El valor ficticio se retira: la caja fuerte guarda solo su hash con clave, sin ninguna entrada,
  mientras exista la caja fuerte, así que nunca se vuelve a sacar para otro valor y una línea antigua
  nunca se restaura con el valor de otra persona. Un valor ficticio retirado no ocupa sitio, porque el
  límite de la caja fuerte cuenta valores, pero se queda: el estado del servidor y
  `latet_vault_near_limit` pesan los valores ficticios retirados de una clave junto con sus valores,
  así que un token que enmascara y olvida en bucle se ve ahí. Da `forget` solo al proceso que atiende
  las solicitudes de borrado.
- **El valor recibe un valor ficticio nuevo si vuelve.** Los valores ficticios y las entradas se sacan
  del valor, así que tras un olvido la caja fuerte de la clave los saca en una nueva generación: el
  valor recibe un valor ficticio nuevo y una entrada nueva, y el valor ficticio antiguo no vuelve
  nunca. Los textos enmascarados que salieron de Latet antes no se pueden unir a los nuevos por sus
  valores ficticios.
- **Una dirección guarda aparte el dominio de su empresa.** Una dirección con el dominio de una empresa
  (`mario.rossi@acme.it`) guarda el dominio como un valor propio, y el dominio del valor ficticio es el
  valor ficticio de ese dominio. Olvidar la dirección deja el dominio, así que la parte del dominio del
  valor ficticio antiguo todavía se restaura en `acme.it`; olvida también `acme.it` si debe desaparecer.
  El dominio de un proveedor de correo, como `gmail.com`, no se guarda nunca.

Qué queda:

- las cifras en el registro de administración, y los intercambios del historial cuyos textos se
  quitaron, sin esos textos;
- los textos enmascarados que ya salieron de Latet, en tu servicio de registros o en tu índice, que
  contienen el valor ficticio antiguo y que borras allí si deben desaparecer;
- en Valkey, sellados, durante una semana después de su último uso: los textos de las conversaciones
  de los agentes de la clave tal como salieron y las respuestas del modelo tal como las escribió, que
  contienen el valor ficticio antiguo, nunca el valor. Un mensaje anterior enviado de nuevo se
  enmascara otra vez; una respuesta del modelo le vuelve tal como la escribió;
- durante una semana como mucho después del último uso de una conversación, el identificador de la
  entrada antigua en esa conversación de los agentes de la clave (un hash con clave del valor, nunca el
  valor), cuando Valkey no respondió justo después del olvido, o cuando una instancia perdió el mensaje
  que lo anuncia (Valkey no guarda los mensajes) y volvió a escribir el identificador desde una
  conversación que tiene;
- lo que PostgreSQL mismo guarda de las filas borradas, sellado, en sus archivos hasta que reutiliza
  su espacio, en su registro de escritura anticipada (WAL) y en las copias de seguridad de la base de
  datos.

Una llamada en curso mientras se hace el olvido puede usar todavía el valor ficticio antiguo, y lo
que guarda en ese momento puede nombrar todavía el valor: una respuesta del revisor local, que un
segundo olvido un momento después quita, y un intercambio que el historial registra, que queda
mientras el historial lo guarda (`retention_days`). Las instancias quitan el valor en cuanto terminan
las llamadas que ya están en curso en ellas.

## Quién puede saber qué

La respuesta dice cuántos valores guardaba la caja fuerte, así que un token que puede olvidar sabe si un
valor es de la clave, como lo sabe uno que puede analizar o enmascarar
([quién puede saber qué](../#quién-puede-saber-qué)). Da `forget` solo al proceso que atiende las
peticiones de borrado, y lee el registro con `latet server log --action apps.forgotten`.
