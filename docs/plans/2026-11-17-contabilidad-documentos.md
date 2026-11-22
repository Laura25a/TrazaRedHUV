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

