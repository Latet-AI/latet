---
title: Enmascarar los textos de un flujo de n8n
description: Los nodos de Latet para n8n analizan, enmascaran, restauran y olvidan los textos de los elementos de un flujo, a través de la API para aplicaciones.
sidebar:
  label: n8n
---

Los nodos de Latet para n8n ([`@latet-ai/n8n-nodes-latet`](https://www.npmjs.com/package/@latet-ai/n8n-nodes-latet),
con licencia MIT) llaman a la [API para aplicaciones](../) de un servidor de Latet desde un flujo.
Analizan, enmascaran, restauran y olvidan los textos de los elementos del flujo: un flujo enmascara lo
que envía a un modelo o a un servicio, y restaura lo que vuelve. Necesitan un servidor de Latet, 0.10.0
o posterior, y el token de una clave de API; el token del equipo de una persona se rechaza.

## Instalar los nodos

En n8n: **Settings > Community Nodes > Install**, y luego escribe `@latet-ai/n8n-nodes-latet`. La
[guía de instalación](https://docs.n8n.io/integrations/community-nodes/installation/) de n8n tiene los
detalles.

## Un token para el flujo

El administrador del servidor de Latet crea un token con los permisos que usa el flujo, y ninguno más
([claves de API y tokens](../#claves-de-api-y-tokens)):

```sh
latet server --server https://latet.acme.lan keys add-token "Support desk" n8n --mask --unmask --out n8n.token
```

| Operación | Permiso |
| --- | --- |
| Scan | `scan`, o `mask` |
| Mask | `mask` |
| Unmask | `unmask` |
| Forget | `forget`, que ningún otro permiso trae |

En n8n, las credenciales **Latet API** guardan la dirección del servidor (`https://latet.acme.lan`) y
el token, es decir, el contenido de `n8n.token`. Usa una dirección `https://`: por `http://`, el token
y los textos viajan en claro. n8n prueba las credenciales con el análisis de una palabra, que no guarda
nada y necesita `scan` o `mask`: un token que solo puede restaurar u olvidar no pasa la prueba, y aun
así funciona en el nodo.

## Qué hace cada operación

Cada operación lee el texto de cada elemento de un campo (**Text Field**, `text` por defecto) y envía
los elementos en llamadas de 1.000 textos como mucho.

- **Scan** dice dónde están los valores en cada texto, por tipo, y no guarda nada.
- **Mask** devuelve cada texto con valores ficticios en lugar de sus valores, el mismo valor ficticio
  para el mismo valor en cada llamada de la clave. **Kind** dice qué son los textos: **Prose** para el
  texto que escriben las personas, **Data** para registros, filas y la salida de un programa
  ([prosa y datos](../#prosa-y-datos)).
- **Unmask** vuelve a poner los valores reales en lugar de los valores ficticios de la clave. El
  servidor escribe cada restauración en su registro de administración.
- **Forget** quita valores de la caja fuerte de la clave, uno por elemento (**Value Field**, `value`
  por defecto), escritos como se enmascararon ([olvidar valores](../forget/)).

Un texto al que el servidor no responde, porque su revisor local lo detuvo o su protección contra
volcados lo retuvo, sale con `blocked` y sin texto, para que el texto en claro no vaya más allá.

## Un modelo que nunca ve los valores

Un flujo que responde a los mensajes de soporte con un modelo:

1. Un disparador recibe el mensaje.
2. **Latet: Mask**, kind **Prose**, enmascara el mensaje.
3. El nodo del modelo (OpenAI, Anthropic, Gemini, un AI Agent) recibe el mensaje enmascarado en su
   prompt.
4. **Latet: Unmask**, con **Text Field** en el campo que guarda la respuesta del modelo, vuelve a poner
   los valores reales.
5. El flujo envía la respuesta.

El modelo solo ve valores ficticios, los mismos en los mensajes de un mismo cliente, y el cliente lee
los valores reales. No le des a un agente de IA la operación Unmask como herramienta: le entregaría al
modelo justo los valores que el enmascaramiento le oculta.

El [README](https://github.com/Latet-AI/n8n-nodes-latet#readme) del paquete enumera cada campo que
escriben los nodos y los errores que dan.
