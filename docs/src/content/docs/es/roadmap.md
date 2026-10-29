---
title: Hoja de ruta
description: Lo próximo para Latet, empezando por lo que llegará antes.
---

Lo que viene a continuación, empezando por lo que llegará antes. Las ideas y la ayuda son
bienvenidas: abre una [incidencia](https://github.com/Latet-AI/latet/issues), o consulta
[Contribuir](../contributing/). Lo que ya ha cambiado está en el [registro de cambios](../changelog/).

## El modo automático de Claude Code, un rechazo claro

Cuando una revisión del modo automático llevaría un detalle que mantienes en local (tu nombre de
usuario, tus rutas), hoy Latet la detiene, y Claude Code indica que su revisor no está disponible y
rechaza la acción. Latet podría, en cambio, responder con el propio veredicto del revisor, *blocked*,
para que el rechazo se presente como lo que es. Esa respuesta vendría de Latet en lugar del revisor de
Anthropic, así que primero preguntamos: una incidencia explica el caso con la pull request preparada,
se la enviamos a Anthropic, y la pull request solo se integra con su conformidad, adjunta a la
incidencia.

## Un instalador firmado para Windows

Los antivirus comprueban los programas que no conocen cada vez que arrancan, y los lanzadores de una
instalación de Python no están firmados, así que en Windows cada comando puede esperar varios segundos
antes de arrancar (consulta [Solución de problemas](../troubleshooting/#los-comandos-van-lentos-en-windows)). Un
instalador firmado, también a través de winget, hace que arranquen enseguida.

## Tu propio modelo de nombres

Hoy Latet usa un solo modelo de nombres, [Privacy Filter](https://huggingface.co/openai/privacy-filter)
de OpenAI, que descarga cuando lo pides (`latet ner install`) y que lee muchos idiomas. Un modelo
propio encontraría los nombres y los términos de tu sector,
como fármacos y diagnósticos en una clínica, o partes y tribunales en un despacho de abogados: un
clasificador de tokens ONNX en tu equipo, o un punto de conexión que gestiones tú, como un servicio
GLiNER o Presidio. Varios podrían trabajar juntos, cada uno añadiendo lo que encuentre.

## Más de la Gemini API de Google

Leer el resto de lo que envían los clientes de Gemini: la Interactions API, la Live API, la Batch API,
la subida de archivos, la generación de imágenes y las peticiones `:predict` de Vertex (sus propios
modelos de embeddings y de imágenes). Hasta entonces Latet los rechaza en sus rutas de Gemini en
lugar de enviarlos sin enmascarar (ver [Gemini CLI y Google GenAI](../agents/gemini/)).

## Más agentes

OpenCode con inicio de sesión de ChatGPT, o con sus modelos en Amazon Bedrock o Azure. La API Mantle
de Amazon Bedrock para Claude Code, y Bedrock a través de un punto de conexión de VPC o de su
dirección FIPS con tu inicio de sesión de AWS (con una clave de API de Bedrock ya funcionan las dos).
Antigravity CLI, verificado con Latet.

## pi, oh-my-pi y ThinkRail

Un plugin para [pi](https://github.com/earendil-works/pi) y
[oh-my-pi](https://github.com/can1357/oh-my-pi) enmascararía los valores sensibles dentro del propio
agente, antes de construir cada petición, como hace el plugin de Claude Code. El enmascaramiento no
dependería del proveedor, así que cubriría JetBrains AI y las suscripciones además de las claves de
API, y cada petición pasaría también por la puerta de enlace de Latet y aparecería en el panel.
JetBrains ThinkRail usa pi, así que sus conversaciones también quedarían protegidas; sus subagentes
y un botón para el panel esperan a que ThinkRail cargue extensiones hechas por otros.

## Latet Enterprise

Latet Enterprise es para una empresa cuyo equipo de seguridad fija las reglas de todos. Sobre la
Community Edition añade reglas para todos que nadie puede relajar, roles de administración como un
auditor de solo lectura, aprobaciones centrales de los cambios que reducen la protección, inicio de
sesión único, informes de auditoría y eventos para un SIEM.
[Apúntate a la lista de espera](https://latet.ai/#waitlist) para saber cuándo sale.

## Más idiomas

El panel, el tutorial y la documentación están disponibles en inglés, italiano, alemán, francés y
español. Se aceptan contribuciones para más idiomas: consulta
[Contribuir](../contributing/#cómo-añadir-un-idioma).
