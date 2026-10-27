---
title: Enmascarar los registros antes de que salgan
description: Una aplicación enmascara sus líneas de registro con un servidor de Latet antes de enviarlas, y una herramienta de soporte restaura la línea que necesita.
sidebar:
  label: Registros
---

Los registros de una aplicación nombran a sus clientes: sus direcciones de correo, sus direcciones
IP, sus IBAN, de vez en cuando un token. Enviados tal cual, pasan meses en un servicio de registros,
leídos por todos los que leen los registros. Con la [API para aplicaciones](../), la aplicación
enmascara cada lote de líneas con un servidor de Latet antes de enviarlo, y el servicio de registros
recibe valores ficticios. Como un valor recibe siempre el mismo valor ficticio, las líneas de un
cliente se pueden seguir buscando, contando y siguiendo durante días; cuando soporte necesita saber
de quién se trataba, una herramienta con el token adecuado restaura la línea.

## Los tokens

Una clave de API para los registros cuyos clientes quieres seguir juntos, con un token para cada lado:

```sh
latet server --server https://latet.acme.lan keys add "Shop logs" --out shop-logs.key
latet server --server https://latet.acme.lan keys add-token "Shop logs" shipper --mask --out shipper.token
latet server --server https://latet.acme.lan keys add-token "Shop logs" support --unmask --out support.token
```

La aplicación tiene `shipper.token`, que puede enmascarar y no puede ver ningún valor real; la
herramienta de soporte tiene `support.token`, que puede restaurar y nada más. El primer token,
`shop-logs.key`, se queda con el administrador: tiene todos los permisos. Las líneas enmascaradas con
otra clave reciben otros valores ficticios, así que usa una sola clave para los servicios cuyos
registros buscas juntos.

## Enmascarar las líneas

Con el cliente de Python `latet-client` (`pip install latet-client`, Python 3.10 o posterior):

```python
from pathlib import Path

from latet_client import Latet, LatetError

latet = Latet("https://latet.acme.lan", Path("shipper.token").read_text().strip())


def ship(lines: list[str]) -> None:
    """Mask a batch of log lines, then send them to the log service."""
    try:
        results = latet.mask(lines, kind="data")
    except LatetError:
        keep_for_later(lines)  # never send the lines as they are
        return
    for result in results:
        if result.text is not None:
            send_to_log_service(result.text)
        else:  # blocked: the reason names no value
            send_to_log_service(f"[line withheld by Latet: {result.blocked}]")
```

Las líneas

```text
2026-10-26T09:14:02Z INFO payment accepted customer=mario.rossi@acme.it iban=IT60X0542811101000000123456
2026-10-26T09:14:05Z WARN card declined customer=mario.rossi@acme.it ip=93.184.216.34
```

salen así:

```text
2026-10-26T09:14:02Z INFO payment accepted customer=tommaso.gallo@margie-6e5a.example iban=IT17F3945471087320356678487
2026-10-26T09:14:05Z WARN card declined customer=tommaso.gallo@margie-6e5a.example ip=198.51.100.41
```

Envía las líneas por lotes, no una llamada por línea: una llamada admite hasta 1.000 líneas, y el
cliente envía una lista más larga en varias llamadas, una tras otra. Las líneas de una llamada se
enmascaran juntas: un valor que una línea de la llamada enseña a la caja fuerte se enmascara también
en las demás líneas de la llamada. Un valor que solo enseña una llamada posterior no se enmascara en
las líneas de las llamadas anteriores, que ya tienen respuesta. Usa `kind="data"` para los registros
([prosa y datos](../#prosa-y-datos)).

Si la llamada falla, guarda las líneas y vuelve a intentarlo, o descártalas: nunca las envíes sin
enmascarar. El cliente reintenta por su cuenta un 429 y un 503 (tres veces, tras la espera que pide
el servidor) antes de lanzar el error. Enmascarar dos veces las mismas líneas da los mismos valores
ficticios, así que reintentar es seguro.

Cada rechazo del servidor es un `LatetError`, con su estado y sus palabras, y una llamada que no llegó
a hacerse es un `ConnectionFailed` (estado 0). `ConnectionFailed` ocupa el lugar de los errores de
`httpx`, también los de un `httpx.Client` que pases como `client=`, y no contiene la solicitud, para
que el token no aparezca en ningún error: captura los errores del cliente, no los de `httpx`. Una
dirección `http://` de otro equipo da un `ClearTextWarning`, porque el token y las líneas viajarían en
claro. El cliente juzga solo por la dirección, así que un `client=` tuyo que llega al servidor por TLS
por otro camino también recibe el aviso; el módulo `warnings` de Python lo silencia.

## El formato de las líneas

Lo que los detectores reconocen se enmascara esté donde esté en una línea: direcciones de correo,
IBAN, números de tarjeta, direcciones IP públicas, tokens y claves, los términos y reconocedores de la
clave. Para lo demás, configura las reglas de la clave
([ajustes compartidos y reglas de las claves de API](../../getting-started/server/#establecer-los-ajustes-compartidos-y-las-reglas-de-las-claves-de-api)):

- Las **reglas de campo** enmascaran los valores de un campo por su nombre, como `phone` o
  `customer_name`, y el **modo de datos estricto** (`data: strict`) enmascara cada valor de una ficha
  ([datos de producción](../../concepts/production-data/)). Encuentran un valor por su clave en JSON,
  y en las líneas `clave=valor` donde un valor con espacios va entre comillas:
  `phone="+39 347 123 4567"`. Un valor sin comillas termina en el primer espacio. Escribe tus líneas
  de registro en JSON, o pon esos valores entre comillas.
- Los **términos** y los **reconocedores** atrapan tus propios nombres y formatos: el nombre de un
  cliente, un número de pedido escrito `ORD-123456` ([reconocedores personalizados](../../concepts/custom-recognizers/)).
- Los **nombres** en el texto libre de un mensaje se encuentran con el modelo de nombres local
  instalado en el servidor, en los textos `prose`; en los textos `data`, con el perfil `max`.

`scan`, con un token que puede analizar, muestra lo que enmascararía una muestra de tus líneas, sin
conservar nada: pruébalo con líneas reales antes de activar el enmascaramiento.

## Restaurar una línea

Una persona de soporte copia una línea enmascarada del servicio de registros; su herramienta la
restaura:

```python
support = Latet("https://latet.acme.lan", Path("support.token").read_text().strip())
line = "2026-10-26T09:14:05Z WARN card declined customer=tommaso.gallo@margie-6e5a.example ip=198.51.100.41"
print(support.unmask(line).text)
# 2026-10-26T09:14:05Z WARN card declined customer=mario.rossi@acme.it ip=93.184.216.34
```

Para encontrar todas las líneas de un cliente, enmascara su dirección con un token que puede
enmascarar y busca el valor ficticio en el servicio de registros. Cada restauración se escribe en el
registro de administración del servidor, con el token y cuántos valores restauró;
`latet server log --action apps.unmasked` las lista.

## Qué velocidad, y cuántas instancias

En una instancia, el enmascaramiento procesa unas 190 líneas por segundo para una clave nueva y de
410 a 440 para una clave cuya caja fuerte contiene de 200.000 a 1.000.000 de valores; la restauración,
de 1.500 a 2.700 ([las mediciones](../#velocidad)). Las llamadas de una clave se ejecutan una tras otra
en una instancia, así que un servicio que registra más deprisa necesita más instancias detrás del
equilibrador de carga, o repartir sus líneas entre claves cuyos clientes no hace falta seguir juntos.

Una llamada guarda las líneas en memoria hasta que recibe respuesta: envía desde una cola propia, para
que un servidor lento o inalcanzable retrase los registros en lugar de perderlos o enviarlos sin
enmascarar.

## Sin Python

Cualquier lenguaje puede llamar a la API por HTTPS: un `POST` con un cuerpo JSON y el token en una
cabecera `Authorization: Bearer`. La [referencia HTTP](../../reference/apps-api/) tiene el contrato,
sus errores y una descripción OpenAPI a partir de la cual generar un cliente.
