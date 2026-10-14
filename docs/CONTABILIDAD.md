# Contabilidad en MongoDB (R09)

La facturación de servicios del HUV se registra en **MongoDB**, en la colección
`facturas` de la base `trazared_huv`. MongoDB es una base no relacional: cada
factura es un documento JSON que puede tener campos distintos según el tipo de
servicio (por ejemplo, `reporte_id` solo en análisis de IA, `remision_id` solo
en remisiones).

## Estructura del documento

```json
{
  "_id": ObjectId("..."),
  "paciente_id": 69,
  "servicio": "consulta" | "analisis_ia" | "remision",
  "valor": 50000.0,
  "moneda": "COP",
  "estado_pago": "pendiente" | "pagada" | "anulada",
  "reporte_id": null,              // solo en análisis de IA
  "remision_id": null,             // solo en remisiones
  "fecha": "2026-10-13T07:47:27Z", // cuándo se generó la factura
  "creado_en": "2026-10-13T07:47:27Z",
  "creado_por": 4,                 // usuario contable (null si es automática)
  "automatica": false              // true si la creó el sistema (R19)
}