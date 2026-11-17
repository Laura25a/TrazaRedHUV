# Contabilidad y documentos: C01-C06

## Qué probar en la interfaz

Como médico o especialista asignado, abre **Pacientes**, la fila del paciente y
**Anexos clínicos → Datos y documentos**. Las acciones **Descargar historia
clínica (PDF)** e **Historia clínica (JSON)** entregan los mismos datos:

- Identificación, observaciones con episodio y código, y recorrido de remisiones.
- Interconsultas con remitente, especialista, estado, fechas y observaciones.
- Reportes de IA aprobados, con nombre/ID/rol de revisor, fecha y nota.
- Consultas RAG aprobadas, respuesta, nota de revisión y fuentes.
- Inventario de estudios activos del paciente en PACS; las imágenes ocultas no
  aparecen. Si PACS falla, se señala que el inventario está incompleto.

El inventario identifica las instancias DICOM; no incorpora los archivos de
imagen ni produce informes radiológicos. Los archivos se consultan mediante el
visor autorizado. Un reporte histórico sin nota o revisor queda marcado como
tal; la exportación no inventa una revisión faltante. Pendientes y rechazados
de IA/RAG se excluyen. Cada consulta/descarga se audita y lleva `no-store`.

Como **contable**, el inicio es el libro contable; como administrador se abre
**Libro contable**. Cierra el asiento desde la cuenta clínica y pulsa
**Facturar**. La fila habilita **Recibo PDF** y **Recibo JSON**. El recibo reúne:

- La factura del cierre y las facturas de IA de reportes aprobados con
  `remision_id` explícito del mismo episodio.
- Conceptos, cantidades, unitarios, subtotales e importe emitido por factura.
- Estado e historial de cobro, glosas, pago y facturas anuladas por separado.
- Total vigente, pagado y saldo; los cobros anulados no suman.

No se recalculan los importes emitidos cuando cambian las tarifas. Si hay
análisis aprobados del episodio sin factura vigente, el recibo dice
`pendiente_facturacion`, incluso si el resto está pagado. Los análisis sin
episodio se mantienen en **Facturas de otros servicios**, sin asignarlos
automáticamente a la última remisión. El recibo exige cierre y una factura
vigente del cierre; no sirve como acreditación de pago mientras esté pendiente.
Es un resumen con tarifas de referencia del proyecto, no una factura electrónica
ni una implementación del esquema oficial RIPS.

## API y permisos

| Ruta GET | Permiso | Contenido |
| --- | --- | --- |
| `/pacientes/{id}/historia` | Admin o clínico asignado | Historia JSON |
| `/pacientes/{id}/historia.pdf` | Admin o clínico asignado | Historia PDF |
| `/contabilidad/remisiones/{id}/recibo.json` | Admin/contable | Recibo JSON |
| `/contabilidad/remisiones/{id}/recibo.pdf` | Admin/contable | Recibo PDF |

El contable no obtiene historia clínica, diagnóstico ni signos desde el recibo.
Un clínico no descarga documentos contables. Las rutas ya existentes del libro,
constancia, bitácora y FHIR permanecen disponibles con sus permisos.

## Duplicados y concurrencia

Se conserva el bloqueo `FOR UPDATE` de la remisión al facturar, también para
la entrada genérica `/facturas`. Al iniciar, la API crea en MongoDB 7 dos
índices únicos parciales: `uq_factura_vigente_remision` y
`uq_factura_vigente_analisis_ia`. Cubren referencias numéricas de su servicio en
estados `pendiente`, `glosada` y `pagada`. Una anulación libera la referencia,
conservando la factura y permitiendo refacturar. Un conflicto responde `409`.
Base técnica: [índices parciales y unicidad en MongoDB 7](https://www.mongodb.com/docs/v7.0/core/index-partial/).

No se borran ni anulan duplicados históricos al crear índices. Si existen, la
construcción del índice falla y se requiere conciliarlos expresamente. Pago,
glosa y anulación comparan el estado leído al actualizar para impedir que una
operación simultánea sobrescriba a otra. Las facturas por remisión/reporte
también toman el mismo bloqueo que la emisión.

La aprobación clínica y la tarea de facturación automática de IA se conservan
juntas en PostgreSQL y se reintentan hasta obtener una factura en MongoDB.
Esto no convierte PostgreSQL y MongoDB en una transacción distribuida: una
emisión manual puede persistir en MongoDB si falla después el sello de
auditoría SQL. La unicidad evita emitir otro cobro vigente al reintentar, pero
esa eventual falta de auditoría requiere revisión administrativa.

## Pruebas reproducibles

Instalar las dependencias de la API y `pruebas/requirements.txt`, y ejecutar
`playwright install chromium`. `pruebas/test_integridad.py` exige una base PG
vacía `trazared_validacion` y un Mongo desechable, ambos en loopback, configurados
con `TRAZARED_PG_PRUEBAS` y `TRAZARED_MONGO_PRUEBAS`.

```bash
python -m pytest -q --tb=short pruebas/test_integridad.py pruebas/test_fhir.py
```

Las pruebas documentales cubren cuatro peticiones concurrentes por ambas rutas
de facturación, inserción duplicada directa, anulación/refacturación, glosa,
pago, importes históricos frente a nuevas tarifas, revisión clínica exportada,
PACS caído, estudios ocultos, paginación extensa y permisos. Chromium entra con
cuentas sintéticas, descarga y lee PDF/JSON de ambas áreas, y comprueba glosa,
pago y persistencia al recargar. `TRAZARED_EVIDENCIA_DOCUMENTOS` permite guardar
las descargas y capturas sintéticas fuera del repositorio.
