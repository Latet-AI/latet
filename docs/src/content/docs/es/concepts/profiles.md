---
title: Perfiles y políticas
description: Elige cuánto enmascara Latet (balanced, max, secrets) y qué ocurre con cada tipo de valor.
---

## Perfiles

Un perfil decide qué [detectores](../detection/) están activos. Hay tres:

| Qué se enmascara | `balanced` (predeterminado) | `max` | `secrets` |
| --- | --- | --- | --- |
| Secretos: claves de API, tokens, contraseñas, claves privadas | ✅ | ✅ | ✅ |
| Los valores de las columnas y los campos secretos (`password`, `api_key`, `reset_token`...) en tablas y JSON | ✅ | ✅ | ✅ |
| Tus propios términos | ✅ | ✅ | ✅ |
| Tus propios [reconocedores](../custom-recognizers/) | ✅ | ✅ | ✅ |
| Los valores que ya están en la caja fuerte | ✅ | ✅ | ✅ |
| Datos personales: correos electrónicos, números de teléfono, IBAN, números de tarjeta, números de IVA, direcciones IP públicas, direcciones de Bitcoin | ✅ | ✅ | ❌ |
| Números de identificación nacional y números fiscales ([por país](../detection/)) | los países de `id_regions` | todos los países | ❌ |
| La identidad de este equipo: tu nombre de usuario en las rutas, el nombre de host, el autor de git y el dominio de su empresa | ✅ | ✅ | ❌ |
| Direcciones IP privadas (`10.x`, `192.168.x`...) | ❌ | ✅ | ❌ |
| Todos los valores de las tablas, del JSON y de los pares `key=value` que muestra una herramienta ([modo de datos estricto](../production-data/#modo-de-datos-estricto)) | ❌ | ✅ | ❌ |
| Nombres y direcciones postales, con el [modelo de nombres local opcional](../../getting-started/installation/) | en tus prompts, adjuntos y el texto de las imágenes | en todas partes | ❌ |
| Lo que ningún patrón encuentra, con el [revisor local opcional](../llm-review/) | cuando está activado | cuando está activado | cuando está activado |
| El texto de las imágenes, con el [reconocimiento de texto opcional](../images/) | enmascarado | no se envía | enmascarado |
| Los PDF, y las imágenes sin reconocimiento de texto | se envían, con un indicador | no se envían | se envían, con un indicador |

- **`balanced`** sirve para la mayor parte del trabajo con código. Es el predeterminado.
- **`max`** añade las direcciones IP privadas y los números de identificación de todos los países,
  activa el [modo de datos estricto](../production-data/#modo-de-datos-estricto) (se enmascaran todos los
  valores de una tabla que muestra una herramienta, también un identificador o un estado), busca
  nombres en todas partes cuando el modelo local está instalado y se niega a enviar una petición que
  lleve una imagen o un PDF. El error dice qué hacer: quitar la imagen de la conversación (en Claude
  Code, `/rewind`) o cambiar a `balanced`. Un proyecto tuyo puede enviar imágenes con su texto
  enmascarado, con `images: mask`.
- **`secrets`** solo enmascara los secretos y tus propios términos. Los datos personales y tu
  identidad pasan. Elígelo cuando las falsas alarmas con los datos personales te estorben.

### Elegir un perfil

- **Cuando haces pasar tus agentes por Latet de forma permanente:**
  `latet global-install --profile max`.
- **En el panel:** la página **Reglas**. El cambio se aplica al momento a las peticiones nuevas, sin
  reiniciar.
- **En `config.yaml`:** pon `profile: max` y después ejecuta `latet reload` para aplicarlo sin
  reiniciar.

### Ajuste fino

Más allá del perfil, la página **Reglas** del panel y `config.yaml` te permiten elegir:

- `pii`: qué tipos de datos personales detectar, por ejemplo `[EMAIL, IBAN]`. La lista sustituye a la
  elección del perfil: un tipo que omite no se detecta, incluidos los números de identificación de
  `id_regions` (y los de todos los países en `max`). Si no se indica, se detectan todos los tipos que
  activa el perfil. En tus ajustes para un proyecto, `pii` añade tipos a los globales.
- `phone_regions`: los países cuyos números de teléfono se reconocen sin prefijo internacional (de
  forma predeterminada, `IT`, `US`, `GB`).
- `include_private_ips: true`: enmascara las direcciones IP privadas también en `balanced`.
- `data`: `strict` activa el modo de datos estricto también en `balanced` y `secrets`, y `normal` lo
  desactiva en `max`. Si no se indica, decide el perfil.
- `disabled_secret_rules`: las reglas de gitleaks que desactivar, como `generic-api-key`.
- `identity`: desactiva el enmascaramiento de la identidad de este equipo, o escribe tú mismo sus
  valores.

Todas las claves aparecen en la [referencia de la configuración](../../reference/configuration/).

## Políticas

Una política decide qué ocurre con un tipo de valor una vez detectado. La predeterminada es `mask`:

| Política | Lo que ve el modelo | Lo que recibes de vuelta |
| --- | --- | --- |
| `mask` (predeterminada) | Un valor ficticio: `+44 20 3318 4452` | El original: `+44 20 7946 0958` |
| `redact` | Un marcador fijo: `[REDACTED:PHONE]` | El marcador. No se restaura nada, así que un archivo que escribe el agente también contiene `[REDACTED:PHONE]` |
| `allow` | El original, sin cambios | El original |

Define una política por tipo en `config.yaml`, o en la página **Reglas** del panel:

```yaml
policies:
  PHONE: redact
  IP_ADDRESS: allow
```

Los nombres de los tipos son los que muestra el panel junto a cada valor enmascarado, como `EMAIL`,
`PHONE`, `IBAN`, `PASSWORD`, o el tipo que le diste a un término personalizado. Los tipos sin
política se enmascaran.

`allow` deja en claro el valor de ese tipo, y nada más: un valor de un tipo enmascarado escrito
dentro de él nunca sale con él. Con `EMAIL: allow`, `IT60X0542811101000000123456.ops@acme-intra.it`
llega al modelo con el IBAN sustituido por uno ficticio y `.ops@acme-intra.it` tal como está
escrito; un secreto o un término tuyo escrito en una dirección se enmascara con la dirección
entera, como antes. Un término o una regla de campo de un tipo que se deja pasar deja en claro lo
que solo él encuentra; lo que un detector de un tipo enmascarado encuentra ahí se enmascara.

`redact` sirve para los valores que el modelo nunca necesita, ni siquiera en forma ficticia.
Funciona en un solo sentido: el modelo recibe el marcador, y Latet nunca vuelve a poner el original
en las respuestas ni en los archivos. Que Latet guarde el original o no depende de cómo se ha
encontrado el valor:

- Un valor que encuentran los detectores, incluidos tus términos y tus reconocedores, no se guarda.
- Un valor que encuentra el [revisor local](../llm-review/), o que marcas tú en el
  [Revisor en directo](../live-review/), se guarda de todos modos: sellado en la caja fuerte, con un
  valor ficticio propio que no se envía. Así Latet lo vuelve a encontrar dondequiera que aparezca, y
  también lo suprime allí.

Un valor de más de 16.384 caracteres, como una cadena JSON entera en el
[modo de datos estricto](../production-data/#modo-de-datos-estricto), se oculta sea cual sea la
política de su tipo, salvo que ese tipo sea `allow`: es demasiado largo para que la caja fuerte lo
guarde, así que nunca se restaura, y un archivo que el agente escriba a partir de él contiene el
marcador.

## Listas de permitidos

Cuando Latet enmascara algo que no es sensible (un falso positivo), dile que deje ese valor tal
cual:

```yaml
allow_values:
  - demo@northwind-legal.com # exact value, any letter case
allow_patterns:
  - "test_[0-9a-f]{16}"      # RE2 regular expression, matched against the whole value
```

En el panel puedes hacerlo con un clic: abre una petición, haz clic en el valor enmascarado y elige
**No sensible: permitir siempre**. La acción contraria, **Enmascarar siempre**, añade el valor como
término personalizado. En una petición de un [proyecto](../projects/), elige primero si se aplica a
*Este proyecto* o *En todas partes*.

Cada clic así añade un valor, de modo que la lista crece. En la página **Reglas** del panel, la
pestaña *Valores permitidos* la muestra como tabla, valores y patrones juntos con su clase:
búscala, fíltrala, abre una fila para cambiarla (el recuadro de un patrón lo prueba con un valor), o
selecciona filas y elimínalas. *Exportar* e *Importar* mueven la lista en CSV (`kind,value`); una
importación añade lo que es nuevo y, como deja pasar valores, solo lo aplica cuando marcas *Aplicar
también estas*.

El nombre de Latet siempre queda en claro: es la herramienta de la que habla el agente, nunca un
valor tuyo.

Para probar tus reglas con un texto sin enviar nada, pégalo en el **Banco de pruebas** del panel.

## En modo cliente

En [modo cliente](../how-it-works/) (Claude Code), el enmascaramiento se hace en el plugin, con el
mismo perfil, las mismas políticas y las mismas listas de permitidos. El daemon nunca reescribe una
petición: solo puede dejarla pasar o bloquearla. Cuando bloquea una porque un valor saldría en
claro, abre la petición en el panel: ese valor aparece recuadrado en rojo. Si no es sensible, haz
clic en él y elige **No sensible: permitir siempre**, como arriba, y vuelve a intentarlo.
