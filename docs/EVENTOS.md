# Eventos y notificaciones de TrazaRed

El stream autenticado `GET /api/eventos/stream` lee la bandeja del usuario en PostgreSQL cada dos segundos. Nginx mantiene abierta la respuesta SSE sin almacenar sus fragmentos. El navegador conserva el ID del último mensaje y lo envía en `Last-Event-ID` al reconectar durante la sesión. Al abrir una sesión nueva, la campana carga los avisos anteriores desde `GET /api/notificaciones`. Solo el dueño puede marcar un aviso como leído.

`registrar_evento(db, destinatarios, evento)` deja el aviso dentro de la transacción de origen. Las interconsultas, las entradas clínicas de socios, el webhook FHIR y las decisiones del análisis usan esa transacción. `publicar()` abre una transacción propia para avisos de operaciones ya confirmadas, como el bloqueo de cuenta.

| Evento | Destinatario y efecto |
|---|---|
| `remision_creada`, `remision_aceptada` (interconsulta) | Aviso al especialista al recibir y al remitente al aceptar. Las bandejas abiertas se actualizan en vivo. |
| `observacion_clinica`, `observacion_fhir`, `interop_resultado` | Aviso al responsable del episodio. Si el mensaje identifica únicamente al paciente, se usa un solo episodio abierto inequívoco. Si no hay responsable, recibe el aviso administración. |
| `imagen_clinica` | Aviso al responsable al guardar el anexo. El archivo queda disponible para revisión clínica. |
| `alerta_critica` | Crea la alerta dirigida y auditada junto con el aviso. Los clínicos ven y reconocen las suyas. Administración supervisa las alertas. |
| `alerta_escalada` | Si sigue activa tras `ESCALAMIENTO_SEGUNDOS` (600 por defecto), el proceso interno marca el escalamiento y guarda en la misma transacción un aviso por administrador. Ante una falla de escritura, se vuelve a intentar. |
| `alerta_reconocida` | Audita el reconocimiento y actualiza la barra de alertas en las sesiones del responsable y de administración. |
| `reporte_pendiente` | Avisa al responsable al quedar disponible un borrador automático KTAS. |
| `reporte_aprobado` | La decisión clínica y su nota van a auditoría. La bandeja de contabilidad recibe un aviso sin identificador de paciente y se solicita la factura automática. Si falla MongoDB, la solicitud queda pendiente y se reintenta. |
| `remision_externa_recibida`, `respuesta_socio` | Avisos en Interoperabilidad al recibir un Bundle o una respuesta Task, con las mismas reglas de roles y contrato de socios. |
| `usuario_bloqueado`, `incidente_seguridad` | Aviso a administración. |

## Criterios de la demostración

- El webhook R21 exige glucosa LOINC 2339-0 en mg/dL con sistema UCUM. Su ID de FHIR se confirma junto con la notificación para no repetirla si HAPI reenvía el aviso.
- Las alertas de signos vitales usan algunos extremos de la tabla [NEWS2 del Royal College of Physicians](https://www.rcp.ac.uk/media/eczf5mvm/news2-chart-3_news-observation-chart_2022_0_0.pdf), incluida la escala 1 de saturación. Para glucosa, la demo conserva el perfil local de menos de 54 o desde 300 mg/dL. Estas reglas requieren validación institucional para uso asistencial.
- El borrador automático KTAS requiere presión sistólica y diastólica, pulso, respiración, saturación y temperatura del mismo episodio, con una separación máxima de 30 minutos entre lecturas. Las categorías no observadas se envían al modelo como faltantes. El borrador solo entra a la historia tras la aprobación documentada de un médico o especialista. Ambas decisiones exigen nota.
- Las pruebas de estos flujos viven en `pruebas/test_integridad.py`. Cubren las sesiones simultáneas, la reconexión, el control por destinatario, el reintento, la revisión clínica y el recálculo concurrente.
