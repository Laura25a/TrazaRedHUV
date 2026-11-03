# Corpus del taller RAG · TrazaRed HUV (Semana 12)

Declaración de fuentes del corpus, tal como pide la tarea de la semana 12
("mínimo 5 documentos de guía con fuente declarada en `docs/RAG.md`").

El corpus tiene las DOS bases vectorizadas que pidió el profe el 27/10:

- **Base general del dominio del proyecto**: referencia y contrarreferencia,
  traslados, roles EPS/IPS, estados y trazabilidad, seguridad y datos personales
  (G01-G08).
- **Base específica del punto crítico del machine learning**: criterios de
  urgencia crítica, que es exactamente lo que predice el modelo XGBoost del
  sistema con los signos vitales (G09-G14).

Los documentos son guías docentes escritas para el curso con criterios clínicos
y normativos reales (Resolución 1995 de 1999, Ley 1581 de 2012, qSOFA, NEWS2,
Cincinnati, ventana trombolítica 4.5 h, regla 15-15, triage de 5 niveles).

## Documentos

| Doc | Base | Contenido | Fuente declarada |
|---|---|---|---|
| G01 | general | Historia clínica: no se borra, soft delete, auditoría, 10 años | Resolución 1995 de 1999 (historia clínica) |
| G02 | general | Contenido de una remisión y contrarreferencia | Guía docente (referencia y contrarreferencia) |
| G03 | general | Traslado interinstitucional, CRU, bitácora de contactos | Guía docente (regulación de urgencias) |
| G04 | general | EPS (asegurador) vs IPS (prestador), factura ligada a la remisión | Guía docente (SGSSS) |
| G05 | general | Catálogo de estados de la remisión, historial, notificación SSE | Guía docente (diseño de TrazaRed) |
| G06 | general | Saturación de urgencias del HUV y remisión oportuna | Guía docente (contexto HUV) |
| G07 | general | JWT, bloqueo al 3er intento, HTTPS por túnel, HL7 FHIR R4 | Guía docente (diseño de TrazaRed) |
| G08 | general | Portal del paciente y autorización del titular | Ley 1581 de 2012 (datos personales) |
| G09 | específica | Triage de urgencias en 5 niveles y tiempos objetivo | Guía docente (triage) |
| G10 | específica | qSOFA (FR >= 22, TAS <= 100, conciencia), primera hora de sepsis, shock | Guía docente (sepsis) |
| G11 | específica | ECG en < 10 min, IAMCEST, reperfusión < 90 min, remisión a hemodinamia | Guía docente (síndrome coronario) |
| G12 | específica | Cincinnati, hora de inicio, tomografía, trombólisis <= 4.5 h | Guía docente (EVC) |
| G13 | específica | SpO2 < 90 %, FR > 24, NEWS2 >= 7, remisión con oxígeno | Guía docente (deterioro respiratorio) |
| G14 | específica | Glucosa < 70, regla 15-15, glucagón si inconsciente, cuándo remitir | Guía docente (hipoglucemia) |
| N01 | norma | Nunca enviar identificadores a modelos externos; salida de IA = propuesta firmada por el clínico | Ley 1581 de 2012 + diseño de TrazaRed |
| H01-H05 | paciente | Reportes IA aprobados/pendientes/rechazados y notas clínicas de 2 pacientes | Didácticos, con la MISMA estructura que entrega la API (`GET /pacientes/{id}/historia` y `GET /ia/reportes/{id}`) |

Los documentos de paciente son didácticos pero conservan el formato real del
sistema (con nombre, documento y teléfono en crudo, para que la seudonimización
del taller demuestre que los elimina). Para volcar pacientes reales de la base
existe `docs/RAG/extraer_pacientes.py` (correr con la API arriba).

## Extensión al objetivo de 50-60 documentos

Para el proyecto final el corpus se engorda con documentos reales convertidos a
texto plano (PDF a txt, sin imágenes ni tablas sueltas): guías de práctica
clínica de Minsalud, protocolos de referencia y contrarreferencia de la red,
manuales del HUV. El notebook lee `corpus/*.md`, así que agregar un documento es
soltar un archivo más en la carpeta.

## Dónde está cada cosa

- Notebook del taller adaptado: `docs/RAG/taller_rag_trazared.ipynb`
- Material original del profe: `docs/RAG/semana12_rag_clinico.ipynb` (+ semana 9
  supervisado y semana 10 no supervisado como contexto del punto crítico de ML)
- Corpus: `docs/RAG/corpus/` (G##/N## en la raíz, H## en `pacientes/`)
- Motor y endpoint para el proyecto: `rag/` en la raíz del repo
- Métricas y guion de demo: `salud_digital/docs/INFORME_rag_taller.md` (fuera
  del repo, junto al resto de informes del equipo)
