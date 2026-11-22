# Sustentación y evidencia Implementation Plan

**Goal:** hacer repetibles L03 y L04 y conservar L05 expresamente pendiente.
**Architecture:** fijar imágenes existentes por digest; compartir escenarios del contrato entre el ejecutor HTTP y pytest, con motores desechables. Verificar HTTPS y recursos públicos sin autenticación ni escrituras.
**Tech Stack:** Docker Compose, Python, pytest, requests, PyYAML y Chromium.
**Spec:** L01–L05 entregados por el dueño en este chat; L05 no se ejecuta hasta nueva orden.

**Ejecución:** completada en el árbol de trabajo y desplegada. 50 pruebas locales y 38 comprobaciones públicas aprobadas; evidencia sanitizada en docs/evidencia/2026-11-17-sustentacion.json. L05 permanece pendiente. Los pasos siguientes se conservan para reproducción.

## Global Constraints

Español es-CO, sin emojis. Secretos fuera de código, consola e informes. Conservar volúmenes y las nueve descargas previamente modificadas. No contactar socios reales. Un resultado de contrato básico no acredita un flujo completo.

## Review Focus

- HTTP 200 con dependencias caídas debe fallar la comprobación pública.
- Un error HTTP esperado también debe comprobar que no produjo escrituras.
- Un 202 requiere conciliación, conservación de Observation y repetición idempotente.
- CSS servido como HTML o de otra versión debe detectar el frontend corrupto.
- Un escenario no ejecutado queda pendiente y no se cuenta como aprobado.
