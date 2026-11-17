# Normativa colombiana Implementation Plan

**Goal:** cerrar accesos indebidos a bitácoras y ofrecer documentos clínicos y alcance normativo verificable.
**Architecture:** reutilizar la autorización clínica y la historia aprobada; separar el documento FHIR R4 del contrato collection del socio y de la acreditación nacional.
**Tech Stack:** FastAPI, PostgreSQL, MongoDB, SPA JavaScript, pytest y Chromium.
**Spec:** N01–N05 aportados por el dueño; fuentes oficiales y límites en docs/NORMATIVA.md.

**Ejecución:** completada técnicamente y desplegada; 43 pruebas de integridad/FHIR aprobadas. Los pasos se conservan como guía de reproducción. Las brechas institucionales y nacionales siguen explícitas en docs/NORMATIVA.md.

## Global Constraints

Español es-CO; sin credenciales ni cambios de datos públicos de demostración. Conservar descargas modificadas por el dueño. No inventar identificación REPS, CUPS, diagnósticos ni aprobación institucional.

## Review Focus

- Médico ajeno no puede enumerar ni consultar contactos; administrador conserva custodia.
- Anulación y restauración conservan contactos.
- Documento FHIR contiene referencias internas resueltas y solo resultados aprobados.
- Documento académico no declara conformidad nacional ni firma clínica del exportador.
- Exportes contables no aparentan RIPS oficial y no se almacenan en caché.

### Task 1: Circulación restringida y custodia

**Files:** python/remisiones.py, python/libro.py, python/validaciones.py, python/main.py, pruebas/test_integridad.py.
**Interfaces:** GET /gestiones-contacto conserva su formato; filtra por paciente asignado. Respuestas API usan Cache-Control no-store.

- [ ] Escribir prueba de lectura por médico ajeno y repetir conservación ante DELETE.
- [ ] Aplicar autorización por asignación y auditar lecturas exitosas sin copiar contactos.
- [ ] Ejecutar pruebas sobre motores desechables.
Run: `bash /tmp/trazared-reactividad-pytest.sh -q --tb=short pruebas/test_integridad.py -k normativa`
Expected: pruebas de circulación restringida aprobadas.

### Task 2: Documento clínico FHIR y alcance nacional

**Files:** python/documento_clinico.py, python/historia.py, frontend/js/app.js, pruebas/test_integridad.py.
**Interfaces:** GET /pacientes/{id}/historia.fhir.json: application/fhir+json, Bundle document con Composition preliminar. GET /pacientes/{id}/preparacion-rda: mapeo y brechas sin datos identificables.

- [ ] Probar rutas ausentes y acceso restringido.
- [ ] Construir documento con Patient, Organization, Device ensamblador, Encounter, Observation y secciones de revisión; ningún meta.profile nacional ficticio.
- [ ] Añadir descarga y probarla en Chromium con paciente sintético.
Run: `bash /tmp/trazared-reactividad-pytest.sh -q --tb=short pruebas/test_integridad.py pruebas/test_fhir.py`
Expected: pruebas de integridad y FHIR aprobadas.

### Task 3: Información de tratamiento y límites del exporte

**Files:** frontend/privacidad.html, frontend/index.html, frontend/js/registro.js, python/contabilidad.py, docs/NORMATIVA.md, docs/STRIDE.md, README.md.
**Interfaces:** página pública de alcance académico, enlace desde login; /contabilidad/rips.json conserva compatibilidad e informa validación oficial no realizada.

- [ ] Identificar exporte como demostración en interfaz y JSON; incluir referencia oficial y datos faltantes, nunca fabricar códigos.
- [ ] Documentar retención mínima de historia (839/2017), custodia y restauración; separar plazos de trazas técnicas de historia.
- [ ] Corregir referencia 1732/2026 revocada por 2080/2026 y citar guía RDA y 0948/2026.
- [ ] Verificar estilos públicos y desplegar API con configuración privada existente.
Run: `/tmp/trazared-s12-venv/bin/python pruebas/probar_front_publico.py https://trazared.kogcake.com`
Expected: seis rutas públicas con estilos y controles activos.
