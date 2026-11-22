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
