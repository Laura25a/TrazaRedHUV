# Dataset del proyecto final — TrazaRed HUV

Dataset **sintético** de 600 remisiones, generado por `python/cargar_dataset.py`
(semilla fija `2026`: siempre sale igual). No contiene datos reales de pacientes.

## Por qué tres grupos

Siguiendo la indicación de la clase (ejemplo diabéticos / sanos / borderline), los
datos no son aleatorios: se construyen alrededor de tres grupos definidos por el
**resultado de la remisión**, con distribuciones que se traslapan a propósito para que
el agrupamiento de la próxima clase tenga sentido.

| Grupo | n | Estado final | Contactos | Convenio vigente | Triage |
|---|---|---|---|---|---|
| `efectiva` | 200 | aceptada / completada | 1–2 | ~85 % | signos casi normales |
| `borderline` | 200 | pendiente / en_gestion | 3–4 | ~50 % | intermedio (se traslapa con los dos) |
| `fallida` | 200 | rechazada | 4–7 | ~15 % | alterado (taquicardia, hipotensión, SpO2 baja, Glasgow bajo) |

Medias por grupo (de `dataset_analitico.csv`):

| Variable | efectiva | borderline | fallida |
|---|---|---|---|
| Edad (años) | 46 | 56 | 62 |
| Frecuencia cardiaca (/min) | 88 | 104 | 118 |
| PA sistólica (mmHg) | 127 | 110 | 95 |
| Saturación O2 (%) | 95.4 | 91.3 | 88.2 |
| Glasgow | 14.6 | 13.0 | 11.0 |
| N.º de contactos | 1.4 | 3.5 | 5.5 |
| Horas de gestión | 0.4 | 8.5 | 19.9 |

## Archivos

| Archivo | Se carga en | Contenido |
|---|---|---|
| `pacientes.csv` | PostgreSQL `pacientes` | nombre, documento (10 dígitos, empiezan por `11`), género, EPS, fecha de nacimiento, teléfono, tipo de sangre, alergias |
| `remisiones.csv` | PostgreSQL `remisiones` | origen, destino, fecha, motivo, estado, convenio + **`grupo`** (etiqueta, no se carga a la BD) |
| `observaciones.csv` | PostgreSQL `observaciones` | 7 signos vitales de triage por remisión, con código **LOINC** y unidad **UCUM** |
| `gestiones_contacto.csv` | MongoDB `gestiones_contacto` | bitácora de intentos de contacto (un documento por remisión con `contactos[]`) |
| `dataset_analitico.csv` | — (para análisis) | **una fila por remisión** con todas las variables juntas, lista para pandas / clustering |

`ref` es el identificador dentro del CSV; al cargar, la base asigna sus propios `id`.

## Signos vitales (LOINC / UCUM)

| LOINC | Signo vital | Unidad UCUM | Rango de referencia (adulto) |
|---|---|---|---|
| 8867-4 | Frecuencia cardiaca | `/min` | 60–100 |
| 8480-6 | Presión arterial sistólica | `mm[Hg]` | 90–139 |
| 8462-4 | Presión arterial diastólica | `mm[Hg]` | 60–89 |
| 9279-1 | Frecuencia respiratoria | `/min` | 12–20 |
| 59408-5 | Saturación de oxígeno | `%` | 92–100 |
| 8310-5 | Temperatura corporal | `Cel` | 36–37.9 |
| 9269-2 | Escala de Glasgow | `{score}` | 15 |

## Columnas de `dataset_analitico.csv`

`remision_ref, documento, genero, edad, eps, sentido (sale_del_HUV / llega_al_HUV),
institucion_destino, fecha_solicitud, motivo_grave (0/1), convenio_vigente (0/1), estado,
fc, pas, pad, fr, spo2, temp, glasgow, n_contactos, n_respuestas_negativas,
horas_gestion, grupo`

Para el análisis, `grupo` (y `estado`, que lo delata) son la **etiqueta**: se dejan por
fuera al agrupar y se usan después para comparar qué tan bien separó el algoritmo.

## Base de imágenes (PACS)

`python/sembrar_imagenes.py` crea imágenes **sintéticas** (fantomas, no son clínicas) en Orthanc como DICOM, enlazadas por
`PatientID` = documento: radiografía de tórax (CR) cuando el motivo es respiratorio, cardiológico o politrauma, y TAC de
cráneo (CT) cuando es trauma craneoencefálico o hemorragia. La severidad del hallazgo sigue al grupo (más marcada en
`fallida`). Por defecto crea imágenes para 60 pacientes (`--max` para cambiarlo).

## Cómo cargarlo

```bash
# Con Docker (recomendado): carga en los contenedores db y mongo
cd docker
docker compose exec api python cargar_dataset.py
docker compose exec api python sembrar_imagenes.py     # base de imágenes en el PACS

# Sin Docker: usa pass.env -> OJO, eso es Neon / Atlas (la base compartida del equipo)
cd python
python cargar_dataset.py
```

Es idempotente: si se corre dos veces no duplica nada (salta los documentos que ya existen).
