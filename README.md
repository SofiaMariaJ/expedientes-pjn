# Expedientes PJN

Programa para Windows que baja todos los documentos de una causa del Poder Judicial de la Nación
(Sistema de Consulta Web): las causas propias ("Mis causas") o cualquier causa por número.

- `app/` — el programa (`expedientes_pjn.pyw`), la lógica de acceso al PJN (`pjn_core.py`) y el
  lanzador que lo actualiza solo (`lanzador.pyw`, lee `app/version.json`).
- `Instalar.bat` — instalación en Windows (una sola vez).
- `guia/` — guía de instalación paso a paso.

El usuario y la clave del PJN se usan solo en la computadora de quien lo instala; este repositorio no
guarda ningún dato personal ni de causas.

Para publicar una actualización: cambiar el código, subir `version` en `app/version.json` y hacer push.
