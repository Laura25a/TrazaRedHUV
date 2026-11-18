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

### Task 1: Despliegue repetible

**Files:** docker-compose.yml, python/Dockerfile, ml/Dockerfile, scripts/probar_integracion.sh, pruebas/requirements.txt.
**Interfaces:** Compose conserva servicios, nombres, rutas y volúmenes; el script consume un intérprete Python con dependencias instaladas y genera JUnit en una carpeta externa.

- [ ] Sustituir imágenes flotantes por sus digests inspeccionados; fijar Python 3.11 de ambos builds.
- [ ] Versionar el arranque y limpieza de PostgreSQL/Mongo efímeros en loopback.
Run: `bash scripts/probar_integracion.sh /tmp/trazared-s12-venv/bin/python /tmp/trazared-sustentacion`
Expected: pytest termina sin fallos y guarda JUnit; los contenedores efímeros se eliminan.

### Task 2: Contrato con escenarios verificables

**Files:** tester.yaml, scripts/escenarios_integridad.py, scripts/verificar_tester.py, pruebas/test_integridad.py, pruebas/test_entregable.py.
**Interfaces:** ejecutar_escenario(nombre, peticion, guardar, secreto_federado) usa HTTP; peticion recibe método/ruta/rol y guardar recibe requisito/caso/estado/detalle/código. Tres escenarios: fechas, huérfanos y conciliación FHIR.

- [ ] Declarar pasos, rutas, códigos e indicadores de los tres escenarios en YAML.
- [ ] Ejecutarlos desde el tester y las pruebas de integración; comprobar falta de persistencia y conciliación idempotente.
- [ ] Guardar nivel de comprobación y revisión local sin cuerpos clínicos ni tokens.
Run: `/tmp/trazared-s12-venv/bin/python -m pytest -q --tb=short pruebas/test_entregable.py`
Expected: contrato y detección de degradación aprobados.

### Task 3: Evidencia pública y guía de sustentación

**Files:** scripts/verificar_entrega_publica.py, docs/SUSTENTACION.md, README.md.
**Interfaces:** verificador público consume URL HTTPS y salida JSON; solo realiza GET anónimos y compara recursos con frontend local. R33 siempre permanece pendiente.

- [ ] Comprobar salud completa, OpenAPI y rutas nuevas, acceso anónimo restringido y hashes de recursos.
- [ ] Ejecutar Chromium sobre las seis entradas públicas y guardar evidencia de API/SPA.
- [ ] Documentar acceso docente privado, comandos, tiempos, límites y pendientes humanos.
Run: `/tmp/trazared-s12-venv/bin/python scripts/verificar_entrega_publica.py --salida /tmp/trazared-entrega-publica.json`
Expected: salud y recursos públicos correctos, reporte sanitizado; L05 pendiente.
