# Contabilidad y documentos Implementation Plan

**Estado de ejecución:** Completado el 17/11/2026: 41 pruebas aprobadas y despliegue público verificado. Las casillas se conservan como instrucciones de reproducción, en el formato que admite el validador del plan.

**Goal:** Completar C03, C04 y C06 y comprobar que C01, C02 y C05 se conservan.
**Architecture:** Unificar la lectura de historia para JSON/PDF; conservar autorización clínica. Emitir recibos administrativos por episodio con valores de facturas y estados de cobro. Mantener bloqueo de PostgreSQL y reforzarlo con índices únicos parciales de MongoDB.
**Tech Stack:** FastAPI, PostgreSQL, MongoDB 7, ReportLab, SPA JavaScript, pytest y Chromium.
**Spec:** Tabla C01-C06 suministrada por el dueño en este chat.

## Global Constraints

- Español; no exponer credenciales ni alterar las nueve descargas existentes.
- Solo resultados aprobados en historia; contabilidad sin diagnóstico ni signos.
- No borrar ni anular automáticamente documentos históricos.
- Pruebas en motores desechables; no usar cuentas clínicas públicas para probar.
- Implementación directa autorizada por el dueño; despliegue público ya autorizado en este chat.

## Review Focus

- Un PACS caído debe quedar explícito, sin afirmar que no hay estudios.
- Una factura anulada no suma al saldo; su trazabilidad se conserva.
- Pago y anulación simultáneos no pueden sobrescribirse.
- El recibo debe usar importes emitidos, no recalcular tarifas históricas.
- Una historia larga debe paginar y envolver textos sin cortar columnas.

### Task 1: Historia clínica trasladable

**Files:** Modificar `python/historia.py`, `frontend/js/app.js`; probar `pruebas/test_integridad.py`.
**Interfaces:** `obtener_historia(db, paciente_id, usuario) -> dict` produce paciente, observaciones, reportes aprobados con revisor/nota/fecha, RAG aprobado, interconsultas, estudios y disponibilidad del PACS. `_crear_pdf(historia) -> bytes` consume exactamente el mismo objeto.

- [ ] Agregar prueba que exporte una interconsulta aceptada, un reporte aprobado y uno pendiente y un estudio simulado; comprobar exclusión de pendientes y estudios ocultos.
- [ ] Ejecutar `bash /tmp/trazared-reactividad-pytest.sh -q pruebas/test_integridad.py -k historia_documental`.
  Expected: falla por ausencia de interconsultas/estudios/revisión exportable.
- [ ] Unificar consultas, hacer explícita la indisponibilidad del PACS y paginar el PDF con párrafos escapados.
- [ ] Añadir descarga JSON en la ficha, usando `descargar(ruta, nombre, boton)`.

### Task 2: Unicidad y recibo del caso

**Files:** Modificar `python/contabilidad.py`, `python/main.py`, `frontend/js/registro.js`; probar `pruebas/test_integridad.py`.
**Interfaces:** `preparar_indices_facturas()` crea índices únicos parciales al arrancar; `recibo_de(db, remision_id) -> dict` alimenta GET `/contabilidad/remisiones/{id}/recibo.json` y `.pdf`, solo admin/contable.

- [ ] Probar rechazo de duplicado directo en Mongo y solicitudes API concurrentes, anulación/refacturación y concurrencia de pago/anulación.
- [ ] Probar recibo cerrado con factura de remisión y análisis IA del mismo episodio, estados, saldo e historial; negar acceso clínico y ausencia de factura.
- [ ] Ejecutar `bash /tmp/trazared-reactividad-pytest.sh -q pruebas/test_integridad.py -k 'facturacion_documental or recibo_documental'`.
  Expected: falla por ausencia de índice y rutas de recibo.
- [ ] Crear índices por referencia/servicio en estados pendiente/pagada/glosada; traducir DuplicateKeyError a 409, conservar bloqueos actuales y actualizar estado con comparación del estado previo.
- [ ] Generar recibo desde facturas vigentes, incluyendo por separado las anuladas; añadir acciones PDF/JSON al libro contable.

### Task 3: Verificación y publicación

**Files:** Modificar `pruebas/test_integridad.py`; crear `docs/CONTABILIDAD_DOCUMENTOS.md`.
**Interfaces:** Chromium consume SPA y API reales de los motores desechables; despliegue usa proyecto `trazared-publico` existente.

- [ ] Descargar PDF/JSON desde cuenta clínica y cuenta contable sintéticas; verificar contenido y permisos, glosa/pago y persistencia al recargar.
- [ ] Ejecutar `bash /tmp/trazared-reactividad-pytest.sh -q pruebas/test_integridad.py pruebas/test_fhir.py`.
  Expected: cero fallos.
- [ ] Renderizar historia y recibo sintéticos con `pdftoppm` y revisar visualmente todas las páginas.
- [ ] Reconstruir API pública, verificar salud, OpenAPI, permisos sin token y seis rutas del frontend; documentar límites del comprobante y del inventario de imágenes.
