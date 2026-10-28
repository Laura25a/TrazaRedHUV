# Rediseño "libro de radicación"

TrazaRed HUV pasa de verse como una herramienta para *hacer* remisiones a ser lo que es: el **registro** de cada remisión del HUV, con su número de radicado, su recorrido completo y lo que costó. Nada de lo que se anota se edita ni se borra.

## Qué cambió para cada rol

| Rol | Al entrar ve | Puede hacer |
|---|---|---|
| Admin | Libro de radicación | Todo, más costos en el formato, anular / restaurar asientos, recalcular pronósticos y reentrenar la IA |
| Médico | Libro de radicación | Radicar, anotar gestiones, cerrar asientos y anotar correcciones de los suyos |
| Especialista | Libro de radicación | Consultar formatos y anexos clínicos; sus interconsultas siguen en **Interconsultas** |
| EPS | Libro con los asientos de sus afiliados | Consultar y descargar (solo lectura) |
| Contable | **Libro contable** | Facturar radicados cerrados, registrar glosas, pagos y anulaciones. No ve datos clínicos (R03) |
| Paciente | **Seguimiento** tipo rastreo de envío | Ver en qué va su remisión, en palabras sencillas |

## Pantallas

* **Libro de radicación** (`js/registro.js → vistaLibro`): hoja con margen, tomos por mes, asientos numerados (`000482`), radicado `REM-2026-000482`, una marca por intento de contacto (✕ no acepta, – sin respuesta, ? en evaluación, ✓ acepta), pronóstico de la IA, sello de cierre y quién asentó. Resalta los asientos con más de 6 h sin movimiento o en riesgo según la IA.
* **Formato de la remisión** (`vistaFormato`): el formato TR-FR-01 con 1) paciente, 2) solicitud y signos vitales, 3) bitácora firmada con la causa que le puso la IA, 4) apoyo de IA, 5) costos (solo admin), 6) cierre y 7) recorrido completo (radicación, contactos, correcciones con *antes → después*, cambios de estado, pronósticos, facturas). Los anexos clínicos (datos, signos, imágenes PACS, historia PDF) quedan debajo.
* **Libro contable** (`vistaLibroContable`), **Seguimiento del paciente** (`vistaSeguimiento`) y **Pronósticos de remisiones** (`vistaPronosticos`).
* Paleta nueva: verde petróleo `#0e6b68` / `#0a4f4d`, papel marfil `#fffdf8`, coral `#e0634a` para anotar y mostaza `#f2c14e` de acento. Se cambió en las variables de `css/estilos.css` (los nombres se conservaron) y lo nuevo está en `css/registro.css`.

## Descargas (todas quedan en auditoría como `exporte`)

| Qué | Ruta | Formatos |
|---|---|---|
| Libro de radicación (respeta la vista y la búsqueda) | `GET /registro/libro.{csv,xlsx,pdf}` | CSV, Excel, PDF del folio |
| Constancia de una remisión | `GET /registro/remisiones/{id}/constancia.pdf` | PDF |
| Bitácora y recorrido | `GET /registro/remisiones/{id}/bitacora.xlsx` | Excel |
| La remisión en FHIR R4 | `GET /registro/remisiones/{id}/fhir.json` | Bundle: Patient + ServiceRequest + una Communication por contacto + Observation |
| Libro contable | `GET /contabilidad/libro.{xlsx,pdf}` | Excel, PDF |
| Facturas | `GET /contabilidad/rips.json` | JSON con estructura simplificada inspirada en RIPS (no validada contra el esquema oficial) |

## Lo que se anota en el registro

* `POST /registro/remisiones/{id}/gestiones`: un intento de contacto. La IA clasifica la respuesta y el estado avanza solo (pendiente → en gestión; si la institución acepta → aceptada). Todo queda en `remisiones_historial` y `auditoria`.
* `POST /registro/remisiones/{id}/cierre`: trasladado o rechazada. Pone `cerrada_en`, deja el radicado *por facturar* y avisa al contable.
* Las correcciones siguen por `PUT /remisiones/{id}` (guarda la versión anterior). El `PUT /gestiones-contacto/{id}` viejo ahora responde **409** si intenta modificar o quitar contactos: solo se puede agregar al final.

## Base de datos (todo idempotente, en `db/schema.sql`)

* `remisiones.radicada_en` (fecha **y hora**), `remisiones.cerrada_en`, `remisiones.institucion_acepta`.
* Tabla `pronosticos_remision`: cada pronóstico de la IA es un evento más del registro (para saber al cierre si acertó).
* `cargar_dataset.py` ahora llena `radicada_en` (hora del triage), `cerrada_en` (último contacto si la remisión terminó) e `institucion_acepta`.
* Mongo: cada contacto nuevo guarda `causa`, `causa_origen`, `registrado_por` y `registrado_en`. Las facturas de remisión llevan `numero` (FV-xxxxx), `radicado`, `conceptos` e `historial`.

## IA de remisiones (`ml/remisiones_modelo.py`)

El modelo KTAS (urgencia del paciente) se conserva. El nuevo responde preguntas sobre la **remisión**:

1. **Probabilidad de que termine efectiva**, con dos modelos XGBoost:
   * *radicación*: solo lo que se sabe al radicar (edad, sexo, EPS, sentido, convenio, motivo grave, grupo de motivo y signos vitales);
   * *gestión*: lo anterior más la bitácora **contada hasta ese momento** (contactos, rechazos, sin respuesta, instituciones distintas, horas de gestión). Se entrena con "fotos" tomadas antes del contacto que resolvió el caso y se valida con `StratifiedGroupKFold` por remisión.
2. **Institución sugerida**: tasa histórica de aceptación por institución, EPS y tipo de motivo, suavizada hacia la tasa global.
3. **Causa de cada respuesta**: TF-IDF de caracteres + regresión logística, entrenado con las respuestas del dataset (etiquetadas con reglas) más variantes escritas a mano. Si duda, mandan las reglas (`python/causas.py` es la misma tabla).
4. **Tiempo estimado hasta aceptación**: mediana y percentil 75 de remisiones efectivas del mismo tipo.

Rutas del servicio `ml`: `GET /remisiones/modelo`, `POST /remisiones/pronostico`, `POST /remisiones/clasificar`, `POST /remisiones/entrenar`. La API las usa desde `/registro/ia/*` y nunca les manda nombre ni documento (R12, mismo seudónimo de `ia.py`).

**Fuga de información evitada:** el `estado` decide el grupo y nunca entra; `n_contactos` y `horas_gestion` finales no entran al modelo de radicación; las abiertas (borderline) no se usan para entrenar: son las que se pronostican.

**Limitación honesta:** con el dataset sintético el AUC sale ≈ 1,00 porque los grupos se generaron con reglas marcadas (Glasgow, SpO₂ y convenio los separan casi solos). Con datos reales las métricas serían menores; la interfaz lo dice en la vista de pronósticos.

Entrenar a mano: `cd ml && python remisiones_modelo.py`. En Docker, si el volumen `ml_modelo` no trae `remisiones_modelo.joblib` (o no se puede cargar), el servicio lo entrena solo al arrancar.

## Contabilidad por radicado (`python/contabilidad.py`)

Cada remisión acumula: estancia en urgencias o UCI mientras espera (por media hora, tope 72 h), cada gestión registrada y el traslado (básico o medicalizado) al completarse. Recorrido del cobro: *acumulando → por facturar → facturada → pagada*, con *glosada* y *anulada*. Las tarifas son **de referencia del proyecto** y se cambian con variables de entorno (`TARIFA_ESTANCIA_URGENCIAS_HORA`, `TARIFA_ESTANCIA_UCI_HORA`, `TARIFA_GESTION_CONTACTO`, `TARIFA_TRASLADO_BASICO`, `TARIFA_TRASLADO_MEDICALIZADO`). Las facturas automáticas de análisis de IA (R19) y `/facturas` siguen funcionando.

## Después de actualizar

```bash
cd docker
docker compose up -d --build          # la API aplica schema.sql sola al arrancar
docker compose exec api python cargar_dataset.py   # en una base nueva: carga el dataset con horas de radicación y cierre
python ../pruebas/probar_registro.py   # prueba de punta a punta: debe terminar en "FALLAS: ninguna"
```

Si ya tenían el dataset cargado, no hace falta recargar: las remisiones viejas quedan con `radicada_en` = fecha de solicitud a las 00:00 y sin `cerrada_en`. Para tener las horas reales hay que cargar el dataset en una base vacía (`docker compose down -v` borra los volúmenes: acuérdenlo en equipo).

## La IA trabaja sola

Nadie tiene que dar clic en "Recalcular". La IA pronostica en cuatro momentos:

1. **Al arrancar la API**, un hilo espera a que el servicio `ml` esté listo y pronostica todas las remisiones abiertas que no tengan pronóstico del modelo vigente (en el log: `[IA] pronósticos automáticos: {'hechas': …}`).
2. **Cada 30 minutos** repite lo anterior (variable `PRONOSTICO_CADA_MIN`; `0` lo apaga).
3. **Cada vez que se anota una gestión**, se clasifica la respuesta y se recalcula el pronóstico de esa remisión.
4. **Cada vez que alguien abre el formato** de una remisión abierta.

Cada pronóstico queda escrito en `pronosticos_remision` y aparece en el recorrido del asiento; al cerrar se anota si la IA acertó. Si se reentrena el modelo, el ciclo automático vuelve a pronosticar todo con la versión nueva.

## Cómo probar

`pruebas/probar_registro.py` recorre la API con los seis roles (40 verificaciones) y guarda las descargas en `pruebas/descargas/` para abrirlas a mano. Escribe en la base (anota 2 gestiones, cierra y factura 1 remisión): úsenlo con los datos de prueba.
