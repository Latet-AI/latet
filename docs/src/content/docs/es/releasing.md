---
title: Publicar Latet
description: Publica una versión solo cuando el workflow CI completo haya terminado con éxito para el commit exacto de main.
---

Este procedimiento cubre los tags `v*` de Latet y `.github/workflows/release.yml`. Mantén un solo commit
público por versión, con código, pruebas, números de versión y documentación en los cinco idiomas.

## Integrar, esperar la CI y crear el tag

1. Prepara y revisa la versión como un único commit público. Alinea las versiones del paquete y del
   plugin de Claude; la compilación de publicación las compara con el tag.
2. Integra ese commit en `main`. Registra su SHA completo.
3. Espera a que **todo el workflow CI** (`.github/workflows/ci.yml`), iniciado por ese push a `main`,
   termine con **éxito** para ese SHA exacto. Comprueba el resultado del workflow, incluidos todos los jobs
   de la matriz y las pruebas del servidor. No basta una pull request o un job individual con éxito.
4. Crea el tag de versión en ese commit exacto y envíalo solo cuando la publicación esté autorizada.
   Se admiten tags ligeros y anotados; para los anotados se comprueba el commit subyacente.
5. Comprueba el resultado del workflow de publicación. Conserva el SHA del tag y la URL de la CI correcta
   de main en el registro de verificación. El resumen del control indica la ejecución y el intento aceptados.

Si cambia el commit, vuelve a comprobar la CI de main para el nuevo SHA antes de crear el tag. Conserva
el único commit público de la versión; no añadas otro commit público solo para registrar comprobaciones correctas.

## Requisitos del control de publicación

Antes de compilar, emitir atestaciones o publicar, el control obtiene el ID real del workflow CI desde su archivo,
comprueba que el commit del tag pertenece a `main` y lee las ejecuciones `push` en `main` de ese SHA exacto.
Elige la ejecución más reciente y consulta su intento actual. Solo acepta `completed` con `success`.
Los jobs posteriores usan el SHA del commit verificado y dependen del control.

Los resultados ausentes, en cola, en curso, en espera, fallidos, cancelados, omitidos o neutrales bloquean la
publicación. También la bloquean los errores de API o autenticación, las respuestas incoherentes y la paginación
incompleta. El control rechaza listas que alcanzan el límite de 1.000 resultados de búsquedas filtradas de GitHub.
No espera ni reintenta hasta obtener un resultado favorable. Investiga el fallo, espera el éxito de toda la CI
en ese commit exacto de main y vuelve a ejecutar explícitamente la publicación cuando esté autorizada.
No muevas un tag publicado para eludir un fallo.

El resultado es una prueba en el momento de la comprobación; una ejecución posterior de la CI puede cambiar
el estado registrado. Un control verde confirma el éxito de la CI, no la ausencia de todo posible defecto.

## Un registro de verificación por versión

Enlaza desde las notas de versión un breve resumen de verificación en inglés, con un archivo Markdown
identificado por versión y manifiestos compartidos de fuentes y pruebas adjuntos fuera de Git.
Finaliza el registro después de crear el commit público y de que toda su CI de main termine con éxito;
tras el tag y la publicación autorizados, adjunta los archivos manualmente y añade los enlaces en las
notas de versión. El workflow de publicación actual no genera estos archivos de verificación.
Mantén un solo commit público por versión: el registro externo puede citar su SHA exacto sin
incluirlo en un archivo que determine ese mismo SHA.

Incluye:

- Versión, SHA completo del commit público y del commit subyacente al tag; por separado, las fuentes
  privadas realmente probadas (commit o base con parche/manifiesto conservado). Un manifiesto enumera
  rutas y hashes del contenido del código, pruebas, arnés y configuración verificados. Registra todas
  las diferencias respecto a las fuentes públicas, exclusiones y comprobaciones que deban repetirse;
  un resultado privado no se convierte en un resultado para el SHA público.
  Separa los inventarios de blobs Git canónicos y de bytes realmente ejecutados, tanto en copias de
  trabajo limpias como modificadas; registra conversiones EOL/filtros y diferencias sin normalizar
  los hashes de los bytes ejecutados.
- Participantes reales en las revisiones o roles identificables y referencias a sus informes, alcance,
  tipo de revisión y si hubo revisión humana independiente. La de un agente automatizado separado no
  es humana. Enumera hallazgos cerrados con pruebas de la nueva comprobación y abiertos con gravedad
  y trabajo pendiente.
- Comandos de las suites, entorno, resultados y pruebas omitidas; IDs de configuraciones fuzz, arnés y
  fuentes, semillas o corpus, recuentos previstos/reales y configuraciones terminadas/previstas. Indica
  expresamente el trabajo fallido, incompleto y no ejecutado. Las campañas de recuento no constituyen
  mediciones de rendimiento.
- Repositorio CI exacto, ruta del workflow, evento/ref, SHA, ID de ejecución, URL del intento,
  estado/conclusión de todo el workflow y momento de observación. Comprueba por separado la CI final
  `push`/`main` antes del tag; comprobaciones PR, resultados locales y publicación correcta no la sustituyen.
- Límites probados: upstream simulado o pruebas directas de runtimes de espacios frente a pruebas
  autenticadas de extremo a extremo, configuraciones omitidas y pruebas no disponibles. Los logs privados
  no son verificables públicamente; aporta un extracto público seguro cuando sea posible e indica lo que
  los lectores no pueden comprobar.

Mantén el resumen y los manifiestos fuera del commit que identifican. Asigna sumas de comprobación e
identificadores de revisión a los archivos de pruebas; conserva revisiones anteriores y explica las
correcciones. Un enlace o una suma de comprobación no prueba por sí solo una revisión independiente ni
almacenamiento inmutable. Usa datos sintéticos y extractos seguros, nunca credenciales ni tráfico real.
Redacta el informe por versión solo en inglés, con manifiestos técnicos compartidos; la guía de
publicación y el resto de la documentación pública se mantienen en los cinco idiomas. Una entrada
de plantilla o una casilla marcada no demuestra nada. Un informe solo aporta pruebas para las fuentes
y el alcance indicados; enumera expresamente campañas incompletas y pasos de publicación pendientes.

## Publicaciones independientes del cliente

Los tags `sdk-python-v*` del cliente Python usan `.github/workflows/sdk-python-release.yml` y versiones
independientes. Ese workflow queda fuera de este control; su publicación no prueba el éxito de la CI de main.
