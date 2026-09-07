# Guion de pitch y demo en vivo — TrazaRed HUV (Corte 1)

> Ajusta las secciones 14.1 y la tabla 13 del plan de acción al sistema **realmente
> implementado** (3 recursos FHIR, 4 roles, gestiones en MongoDB, sincronización por
> `fhir_sync.py`). Tiempos totales: pitch 10 min · demo 7 min, como exige el enunciado.

## Pitch (10 minutos — un único presentador)

| Minutos | Bloque | Contenido |
|---|---|---|
| 1–3 | **El problema** | La remisión sin trazabilidad en el HUV; la Sentencia T-573-23 (la EPS que afirmó gestionar con 13 IPS y el hospital que nunca recibió la solicitud); ocupación del 130 % en urgencias. Cierre: *"el hospital no necesita una pantalla nueva, necesita información confiable del traslado"*. |
| 3–5 | **La propuesta de valor** | Una sola versión de la verdad por traslado: quién contactó, a quién, cuándo, con qué respuesta y bajo qué convenio. Cuádruple objetivo: salud poblacional de la red del suroccidente, experiencia del paciente, costos y reprocesos, decisiones informadas. |
| 5–8 | **Arquitectura y lo construido** | Diagrama general; modelo relacional multi-tabla (6 tablas en PostgreSQL/Neon **+ MongoDB para las gestiones de contacto**: persistencia políglota); servidor HAPI FHIR R4 con Patient, Encounter y Observation (terminología LOINC); sincronización BD → FHIR (`fhir_sync.py`); **4 roles con JWT** (admin, médico, EPS y paciente); soft delete, soft edit con historial y restauración con auditoría; URLs públicas con Cloudflare Tunnel. |
| 8–10 | **Estado y siguientes pasos** | Cobertura completa de la rúbrica con evidencia; desviaciones documentadas (`documentacion_mapeo_roles.md` §7); camino a la visión TrazaRed completa (acceso del paciente, integración con EPS, analítica de red). |

## Demo en vivo (7 minutos — contra las URLs públicas, NO localhost)

**Preparación (10–15 min antes):** correr `python levantar_demo.py` (despierta Neon y Atlas,
levanta HAPI, API y los dos túneles, y deja las URLs frescas en pantalla y en el README);
verificar una URL desde el teléfono con datos móviles; tener el notebook abierto en §8.b
(auditoría) y §8.c (sincronización FHIR) como respaldo; video de contingencia grabado.

| # | Rol | Acción y resultado esperado | Tiempo |
|---|---|---|---|
| 1 | Presentador | Abrir Swagger (`/docs`) con la URL pública de la API y, en otra pestaña, la interfaz web de HAPI con la segunda URL pública. | 0:30 |
| 2 | Admin | Login → `GET /pacientes` → crear una remisión nueva. | 1:00 |
| 3 | Médico | Login → `POST /gestiones-contacto` (llamada a una IPS: "sin cupo", con respuesta) → `POST /observaciones` con código LOINC. | 1:30 |
| 4 | Médico | Sincronización FHIR: ejecutar la celda §8.c del notebook (o `python fhir_sync.py`) → GET de Patient/Encounter/Observation con los ids asignados. *Nota honesta: la sincronización no es idempotente (desviación documentada §7.5).* | 1:00 |
| 5 | Presentador | En la web de HAPI: buscar el Patient y su Encounter mostrando las referencias cruzadas. | 1:00 |
| 6 | Médico | Soft delete de una remisión que él creó → intentar restaurarla → **403** (no es admin). | 0:45 |
| 7 | Admin | Restaurar la remisión (único rol autorizado) → mostrar la auditoría de la secuencia (celda §8.b del notebook: qué usuario, qué operación, cuándo). | 0:45 |
| 8 | Paciente | Login → `GET /remisiones`: **solo sus remisiones y con 4 campos** — el cierre del caso T-573-23 (transparencia al titular del dato). | 0:30 |

**Total: 7:00.**

### Contingencias

- **URL pública no responde** → regenerar todo con `python levantar_demo.py` (≈1 min) y usar
  las URLs nuevas; el guion no depende de URLs memorizadas.
- **Primera petición lenta** → autosuspend de Neon; el script ya despierta las bases, y
  se puede re-ejecutar su paso 0 solos con las primeras líneas.
- **Todo lo demás falla** → video de respaldo de la demo completa (grabado con las URLs
  públicas); la demo en vivo es la exigida, el video es el plan B.
