# INTEROP.md — Interoperabilidad entre sistemas (R30 a R33)

Cada equipo es una institución. El médico de una institución remite un paciente al
especialista de la otra, y la información viaja **en FHIR, de API a API**: sin correos,
sin archivos, sin que nadie copie datos a mano.

Este documento es el **contrato** que se le entrega al equipo socio. Lo mismo, en JSON y
siempre al día con el código, está en `GET /api/interop/perfil` (no requiere token).

## 1. El recorrido completo

```mermaid
sequenceDiagram
    autonumber
    participant M as Médico (institución A)
    participant A as API de A
    participant B as API de B
    participant E as Especialista (institución B)

    M->>A: Remitir a institución socia (botón en el formato)
    A->>A: ¿Consentimiento vigente del paciente para B? Si no: 403
    A->>B: POST /oauth/token (client_id, client_secret, scope)
    B-->>A: JWT de sistema, 10 minutos
    A->>B: POST /interop/Bundle (Bearer + firma HMAC + Bundle FHIR)
    B->>B: token, permiso, firma, hora, repetido, perfil FHIR
    B-->>A: 201 (paciente asociado) o 202 (falta conciliar)
    B-->>E: aviso EN VIVO + bandeja + contador (R17–R19)
    E->>B: Aceptar o rechazar
    B->>A: POST /interop/Task (Bearer + firma HMAC + Task)
    A-->>M: aviso EN VIVO: «la institución B aceptó la remisión»
    Note over A,B: Los dos lados auditan cada paso con origen=externo
```

## 2. Antes de empezar: intercambiar credenciales **en privado**

Cada institución registra a la otra (Admin › **Interoperabilidad** › *Instituciones socias*):

1. **Registrar institución socia.** El sistema genera lo que **ella** usará para entrar aquí:
   `client_id`, `client_secret` y `secreto de firma`. Se muestran **una sola vez**.
2. Entregarle esos tres datos por un canal privado (nunca por el repositorio ni por un chat
   abierto), junto con la URL pública: `https://<tunel>/api`.
3. Recibir los tres datos que **ella** generó para nosotros y registrarlos en *Configurar envío*
   (se guardan cifrados con AES-256-GCM).
4. *Probar conexión*: pide un token al socio y dice si funcionó.

Si un secreto se filtra: *Rotar credenciales*. Borrarlo no basta; los viejos dejan de servir.

Para ensayar **sin el otro equipo**: *Crear socio de prueba (bucle local)*. Este mismo sistema
hace de socio: la remisión sale, entra por `/interop/Bundle` con token y firma reales, el
especialista la responde y la respuesta vuelve por `/interop/Task`.

## 3. Autenticación (R31): un sistema también se identifica

```http
POST /api/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=sis-clinica-socia-1a2b3c&client_secret=...&scope=system/Bundle.write
```

```json
{"access_token": "eyJ...", "token_type": "bearer", "expires_in": 600, "scope": "system/Bundle.write"}
```

| Scope | Abre |
|---|---|
| `system/Bundle.write` | `POST /api/interop/Bundle` · la remisión |
| `system/Task.write` | `POST /api/interop/Task` · la respuesta a una remisión |
| `system/Observation.write` | `POST /api/interop/Observation` · un dato clínico suelto |

El token de sistema lleva `tipo=sistema` y **solo sirve en `/interop/*`**: con él, la API
clínica (`/pacientes`, `/remisiones`…) responde 403. Y un token de usuario no abre `/interop/*`.

## 4. Firma de cada mensaje (R29): quién, qué y cuándo, una sola vez

Cada `POST /interop/...` lleva cuatro cabeceras:

```http
Authorization: Bearer <token de sistema>
X-Timestamp: 1794700000                      ← hora UTC en segundos (epoch)
X-Message-Id: 6f1c2b9e-...                   ← único por mensaje (UUID)
X-Signature: v1=<64 hexadecimales>
Content-Type: application/fhir+json
```

```
firma = HMAC-SHA256( secreto_de_firma,  X-Timestamp + "." + X-Message-Id + "." + cuerpo )
```

El cuerpo es **exactamente** los bytes que se envían (firmar primero, enviar esos mismos bytes).

```python
import hashlib, hmac, json, time, uuid, requests

cuerpo = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
ts, mid = str(int(time.time())), str(uuid.uuid4())
firma = hmac.new(SECRETO_FIRMA.encode(), f"{ts}.{mid}.".encode() + cuerpo, hashlib.sha256).hexdigest()
requests.post(URL + "/interop/Bundle", data=cuerpo, headers={
    "Authorization": f"Bearer {token}", "Content-Type": "application/fhir+json",
    "X-Timestamp": ts, "X-Message-Id": mid, "X-Signature": f"v1={firma}"})
```

| Qué garantiza | Cómo |
|---|---|
| **Quién** lo envió | Solo el socio conoce el secreto de firma |
| **Qué** envió | Cambiar un carácter del cuerpo rompe la firma → 401 |
| **Cuándo** | `X-Timestamp` debe estar a ±5 minutos de la hora del receptor → si no, 401 |
| **Una sola vez** | Un `X-Message-Id` ya procesado → 409 |

## 5. Lo que responde el receptor: el código le dice al otro qué corregir

| Código | Significa | Qué hace el emisor |
|---|---|---|
| **201** | Aceptado y asociado a un paciente | Nada: quedó |
| **202** | Aceptado; falta conciliar al paciente (el receptor no lo conoce todavía) | Nada: el receptor lo concilia |
| **400** | No es JSON o no es el recurso esperado | Revisar el cuerpo |
| **401** | Sin token, token vencido, firma inválida o mensaje vencido | Pedir token nuevo; revisar secreto, reloj y que se firmen los mismos bytes |
| **403** | Token válido, sin el permiso (`scope`) o sistema desactivado | Pedir el token con el scope correcto |
| **409** | Mensaje repetido | No reenviar: ya se procesó |
| **422** | No cumple el perfil. Va un `OperationOutcome` con **todos** los problemas y **no se guarda nada** | Corregir el recurso y enviar con un `X-Message-Id` nuevo |
| **429** | Demasiadas peticiones | Esperar lo que dice `Retry-After` |

```json
{"resourceType": "OperationOutcome", "issue": [
  {"severity": "error", "code": "invalid",
   "diagnostics": "Observation[2]: el código LOINC '9999-9' no está en el perfil (2339-0, 8480-6, ...)"}]}
```

## 6. Perfil FHIR R4 común (R30)

### Identificador federado: el mismo paciente, sin el documento

`Patient/17` solo existe en una base. Para nombrar al mismo paciente en las dos instituciones
**sin enviar su documento**, ambas calculan:

```
identificador = HMAC-SHA256( secreto federado del acuerdo,  "CC|" + documento sin puntos ni espacios )
```

| | |
|---|---|
| `system` | `urn:salud-digital:sid-federado` (variable `SID_FEDERADO`) |
| Secreto | variable `SECRETO_FEDERADO`, **igual en los dos equipos** (el valor por defecto es el de la demo de clase: cámbienlo) |
| Ejemplo | `CC|1144123456` → `fa0d2121aa10d0c2bb7a5acd8b596f2f…` |

El receptor calcula el mismo HMAC sobre sus fichas locales. Si coincide, guarda el dato y responde
**201**. Si desconoce al paciente, responde **202** y conserva la remisión o el resultado en una ficha
provisional compartida por los mensajes del mismo identificador. El administrador verifica el
documento de la ficha local en **Interoperabilidad → Por conciliar**. La coincidencia requerida
reasigna los datos a esa ficha. La bitácora conserva el HTTP 202 original y muestra el estado
`conciliado`. Si hace falta, primero se registra al paciente localmente.

### `POST /interop/Bundle` — la remisión

El intercambio admite `Bundle.type = "collection"`. Cada entrada requiere un `fullUrl` único y un
recurso. Se rechazan `entry.request`, `entry.response` y `entry.search`, y los tipos de Bundle
reservados para otras interacciones de FHIR. La colección requiere **un** `Patient` y **un**
`ServiceRequest`. [HL7 FHIR R4 Bundle](https://hl7.org/fhir/R4/bundle.html)

| Recurso | Reglas |
|---|---|
| `Patient` | Un único `identifier` federado de 64 hexadecimales. Nombre, documento, fecha de nacimiento, teléfono, correo y dirección quedan en el sistema local |
| `ServiceRequest` | `status: active`, `intent: order`, `priority` (`routine`/`urgent`/`asap`/`stat`), SNOMED CT `3457005` y un único `identifier` con sistema `urn:trazared:remision`. `subject` identifica el `Patient` |
| `Encounter` (0..1) | `identifier` de sistema `urn:trazared:encuentro` y el mismo `subject`. Si se incluye, la solicitud y las mediciones pueden referenciarlo. TrazaRed declara `urn:trazared:clase-encuentro\|remision` para el episodio de coordinación. El perfil también admite las clases de atención de HL7 R4 |
| `Observation` (0..n) | `status: final`, `amended` o `corrected`, un LOINC del catálogo, su UCUM exacto en `valueQuantity.code`, valor finito y el mismo `subject`. Si se incluye, `effectiveDateTime` lleva zona horaria |
| `DiagnosticReport` (0..1) | Reporte de IA aprobado por un clínico, con `status: final` y el mismo `subject` |
| `Consent` (0..1) | Autorización con `status: active` y `patient` vinculado al mismo paciente |

`ServiceRequest.requester` y `Observation.performer` pueden identificar al profesional mediante
`urn:trazared:profesional`. El seudónimo identifica al profesional dentro del acuerdo sin
compartir su nombre ni el ID interno de su cuenta.

LOINC aceptados:

| LOINC | Qué es | UCUM |
|---|---|---|
| `8867-4` | Frecuencia cardíaca | `/min` |
| `8480-6` | Presión arterial sistólica | `mm[Hg]` |
| `8462-4` | Presión arterial diastólica | `mm[Hg]` |
| `9279-1` | Frecuencia respiratoria | `/min` |
| `59408-5` / `2708-6` | Saturación de oxígeno | `%` |
| `8310-5` | Temperatura corporal | `Cel` |
| `9269-2` | Escala de Glasgow | `{score}` |
| `2339-0` | Glucosa en sangre | `mg/dL` |

En el Bundle, la diastólica debe ser menor que la sistólica de la misma toma.
El perfil agrupa por instante, con zonas equivalentes normalizadas a UTC.
Un único par sin fecha también se comprueba. Si hay varios valores distintos
en una misma toma, usa instantes distintos para identificar cada par.

Ejemplo mínimo:

```json
{"resourceType": "Bundle", "type": "collection", "entry": [
  {"fullUrl": "urn:uuid:35cb1363-ce64-423b-aeb6-269643640b18", "resource": {"resourceType": "Patient",
    "identifier": [{"system": "urn:salud-digital:sid-federado", "value": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}]}},
  {"fullUrl": "urn:uuid:5906b83d-43e2-4932-a82a-3a189df3b87b", "resource": {"resourceType": "ServiceRequest",
    "status": "active", "intent": "order", "priority": "urgent",
    "identifier": [{"system": "urn:trazared:remision", "value": "REM-2026-000123"}],
    "subject": {"reference": "urn:uuid:35cb1363-ce64-423b-aeb6-269643640b18"},
    "code": {"coding": [{"system": "http://snomed.info/sct", "code": "3457005"}]},
    "reasonCode": [{"text": "Solicitud de evaluación en la institución receptora"}]}}
]}
```

### `POST /interop/Task` — la respuesta

```json
{"resourceType": "Task", "status": "accepted", "intent": "order",
 "focus": {"identifier": {"system": "urn:trazared:remision", "value": "REM-2026-000123"}},
 "owner": {"display": "Clínica del equipo 3"}, "statusReason": {"text": "Cama UCI disponible"}}
```

`status`: `accepted` · `rejected` · `in-progress` · `completed` · `cancelled`. `focus.identifier.value`
es el número que venía en `ServiceRequest.identifier` de la remisión original.

### `POST /interop/Observation` — un dato suelto

La entrada acepta una medición con `subject.identifier` federado. Cuando viene sin
`basedOn`, queda en la historia del paciente sin asignación a un radicado. Para devolver
un resultado clínico a la institución de origen, `basedOn[].identifier` debe identificar la
`ServiceRequest` original con sistema `urn:trazared:remision`. Si se envía
`encounter.identifier`, debe coincidir con el episodio de esa solicitud. El receptor valida
el socio, el radicado y el paciente antes de insertar el resultado.

El especialista a cargo registra una medición en **Interoperabilidad → Recibidas → Devolver
resultado**. Con consentimiento vigente para compartir con el origen, puede enviar una
`Observation` firmada. El médico remitente ve el resultado en el formato del radicado y el
conteo en **Enviadas**. El resultado se conserva como una nueva observación con procedencia
del socio. `Task` sigue comunicando la decisión clínica.

### Réplica interna a HAPI FHIR

Las altas y modificaciones de la API se guardan primero en PostgreSQL con una cola persistente.
La réplica consume la cola y usa `PUT /{tipo}?identifier={sistema}|{valor}` para crear o actualizar
`Patient`, `Encounter` y `Observation`. Las dependencias se publican antes de usar sus referencias.
Si HAPI no responde, la operación clínica queda guardada y la cola vuelve a intentarlo. El
trabajador sincroniza altas y cambios desde la activación de la cola. [HL7 FHIR R4 REST](https://www.hl7.org/fhir/R4/http.html)

## 7. Consentimiento y minimización (R32)

- **Sin consentimiento no sale nada.** Antes de enviar, el sistema busca un consentimiento
  vigente del paciente **para esa institución**; si no existe, el envío responde 403 y queda
  auditado (`interop_envio_bloqueado`). El médico lo registra desde el mismo diálogo de envío
  (escrito, verbal o digital) y el paciente puede revocarlo cuando quiera.
- **Solo lo necesario.** El diálogo muestra, antes de enviar, qué viaja y qué **no** sale del
  hospital: nombre, fecha de nacimiento, documento, teléfono, correo, dirección, EPS, alergias y tipo de sangre.
  El motivo de remisión sigue siendo texto clínico. Se enmascaran los identificadores detectados
  antes del envío.
- El reporte de IA solo viaja si un clínico lo **aprobó**.
- Ley 1581 de 2012: los datos de salud son sensibles (arts. 5 y 6) y su tratamiento exige
  autorización del titular (art. 9).

## 8. Eventos y auditoría

| Evento (`tipo`) | Cuándo | A quién le llega en vivo |
|---|---|---|
| `remision_externa_recibida` | Llegó y se aceptó una remisión del socio | especialistas y admin |
| `respuesta_socio` | El socio aceptó o rechazó una remisión nuestra | el médico que la envió y admin |
| `interop_resultado` | Un resultado recibido queda asociado o pendiente de conciliación | responsable del episodio y admin |
| `interop_conciliacion` | Se vincularon datos externos a la ficha local verificada | admin |
| `interop_rechazo` | Se rechazó un mensaje del socio por validación (400/422) | admin |

Auditoría (todas con `origen=externo` y el nombre del socio): `interop_token_emitido`,
`interop_token_rechazado`, `interop_remision_enviada`, `interop_remision_recibida`,
`interop_remision_aceptada` / `_rechazada`, `interop_respuesta_enviada`,
`interop_respuesta_recibida`, `interop_observacion_recibida`, `interop_rechazo`,
`interop_envio_bloqueado`, `consentimiento_registrado`, `consentimiento_revocado`.

La bitácora técnica de cada mensaje (dirección, recurso, código HTTP, detalle) está en
Admin › Interoperabilidad › *Mensajes*. La conciliación y el envío de resultados también quedan
en la cadena de auditoría.

## 9. La práctica en parejas y la demostración cruzada (R33)

1. **Intercambiar credenciales en privado** (sección 2) y acordar `SECRETO_FEDERADO`.
2. **Enviar tres mensajes en vivo**: una remisión válida (201 o 202), una alterada (401) y una
   con LOINC errado (422).
3. **Correr el tester del otro equipo** contra la URL pública.
4. En la sustentación: el médico de A remite, el especialista de B recibe el aviso sin
   recargar y acepta, el médico de A ve la respuesta sin recargar, y cada admin muestra su
   auditoría y sus mensajes.

Prueba automática de todo el recorrido contra el propio sistema (bucle local):

```bash
docker compose exec api python /app/pruebas/probar_final.py
```

La prueba con navegador `pruebas/probar_interop_fhir.py` usa solo un laboratorio local
con cuentas y datos sintéticos. Ejecuta el intercambio con un socio en bucle local, verifica la
conciliación y el retorno en Chromium, y consulta los recursos que quedaron almacenados en HAPI.

El intercambio con otro equipo requiere acordar qué versión del perfil acepta. Para consultar
camas, quirófanos o transporte todavía se necesita el contrato del equipo que publica cada dato,
incluidos los permisos de lectura y una URL de prueba.

## 10. Variables de entorno

| Variable | Para qué | Si no se define |
|---|---|---|
| `INSTITUCION_NOMBRE` | Nombre con el que este sistema se presenta | `Hospital Universitario del Valle` |
| `SECRETO_FEDERADO` | Secreto del identificador federado (igual en los dos equipos) | el de la demo de clase |
| `SID_FEDERADO` | `system` del identificador federado | `urn:salud-digital:sid-federado` |
| `INTEROP_TOLERANCIA_S` | Diferencia máxima de reloj aceptada | `300` |
| `INTEROP_URL_LOCAL` | URL de este sistema visto desde sí mismo (socio de prueba) | `http://127.0.0.1:8000` |
