"""
Servicio de integración TrazaRed HUV: sincroniza pacientes, remisiones y observaciones
de PostgreSQL (Neon) hacia el servidor HAPI FHIR, como recursos Patient, Encounter y Observation.
"""
import os
import time
from contextlib import closing
from zoneinfo import ZoneInfo
from pathlib import Path
import requests
import cripto
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from interop_perfil import CLASE_REMISION
from validacion_clinica import SIGNOS, validar_signo

load_dotenv(Path(__file__).resolve().parent / "pass.env", override=True)
PG_CONNECTION_STRING = os.getenv("PG_CONNECTION_STRING")
FHIR_BASE_URL = os.getenv("FHIR_BASE_URL", "http://localhost:8081/fhir")

HEADERS = {"Content-Type": "application/fhir+json"}
SID_LOCAL = "urn:trazared:replica"
SID_DOCUMENTO = "urn:trazared:documento"
ETIQUETA_REPLICA = {"system": SID_LOCAL, "code": "replica-clinica"}
UCUM = "http://unitsofmeasure.org"
BOGOTA = ZoneInfo("America/Bogota")


def get_db():
    return psycopg2.connect(PG_CONNECTION_STRING)


# ── Mapeo: fila de Postgres -> recurso FHIR ─────────────────────

def paciente_a_fhir(paciente: dict) -> dict:
    """pacientes -> Patient"""
    recurso = {
        "resourceType": "Patient",
        "identifier": [{"system": "urn:trazared:documento", "value": paciente["documento"]}],
        "name": [{"text": paciente["nombre"]}],
        "gender": {"F": "female", "M": "male"}.get(paciente["genero"].upper(), "unknown"),
    }
    if paciente.get("fecha_nacimiento"):
        recurso["birthDate"] = paciente["fecha_nacimiento"].isoformat()
    return recurso


def _profesional_local(profesional_id):
    return {"type": "Practitioner", "identifier": {"system": SID_LOCAL, "value": f"profesional-{profesional_id}"}}


def remision_a_fhir(remision: dict, patient_fhir_id: str) -> dict:
    """Registro clínico local del episodio de la remisión."""
    estado_fhir = {
        "pendiente": "planned", "en_gestion": "arrived", "aceptada": "in-progress",
        "rechazada": "cancelled", "completada": "finished",
    }[remision["estado"]] if remision["activo"] else "entered-in-error"
    recurso = {
        "resourceType": "Encounter",
        "status": estado_fhir,
        "class": CLASE_REMISION.copy(),
        "subject": {"reference": f"Patient/{patient_fhir_id}"},
        "reasonCode": [{"text": remision["motivo"]}],
        "period": {"start": str(remision["fecha_solicitud"])},
    }
    ids = dict.fromkeys(i for i in (remision.get("creado_por"), remision.get("atendida_por")) if i)
    participantes = [{"individual": _profesional_local(i)} for i in ids]
    if remision.get("profesional_fhir"):
        participantes.append({"individual": {"type": "Practitioner", "identifier": {
            "system": "urn:trazared:profesional", "value": remision["profesional_fhir"]}}})
    if participantes:
        recurso["participant"] = participantes
    return recurso


def observacion_a_fhir(observacion: dict, encounter_fhir_id: str, patient_fhir_id: str) -> dict:
    codigo = observacion["codigo_loinc"]
    unidad, valor = observacion["unidad"], float(observacion["valor"])
    code = {"text": observacion["tipo"]}
    cantidad = {"value": valor, **({"unit": unidad} if unidad else {})}
    if codigo:
        code["coding"] = [{"system": "http://loinc.org", "code": codigo}]
        codigo_regla = "59408-5" if codigo == "2708-6" else codigo
        if codigo_regla in SIGNOS and unidad:
            signo = validar_signo(codigo_regla, valor, unidad)
            cantidad.update(system=UCUM, code=signo.unidad)
        elif codigo == "2339-0" and unidad == "mg/dL":
            cantidad.update(system=UCUM, code=unidad)
    fecha = observacion["fecha_observacion"]
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=BOGOTA)
    recurso = {
        "resourceType": "Observation", "status": "final", "meta": {"tag": [ETIQUETA_REPLICA]},
        "code": code, "subject": {"reference": f"Patient/{patient_fhir_id}"},
        "effectiveDateTime": fecha.isoformat(timespec="seconds"), "valueQuantity": cantidad,
    }
    if encounter_fhir_id:
        recurso["encounter"] = {"reference": f"Encounter/{encounter_fhir_id}"}
    if observacion.get("profesional_id"):
        recurso["performer"] = [_profesional_local(observacion["profesional_id"])]
    elif observacion.get("profesional_fhir"):
        recurso["performer"] = [{"type": "Practitioner", "identifier": {
            "system": "urn:trazared:profesional", "value": observacion["profesional_fhir"]}}]
    return recurso


# ── POST: crear recursos en HAPI FHIR ───────────────────────────

def crear_recurso_fhir(recurso: dict) -> dict:
    tipo = recurso["resourceType"]
    r = requests.post(f"{FHIR_BASE_URL}/{tipo}", json=recurso, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def _actualizar_fhir(recurso, sistema, identificador):
    respuesta = requests.put(f"{FHIR_BASE_URL}/{recurso['resourceType']}",
                             params={"identifier": f"{sistema}|{identificador}"},
                             json=recurso, headers={**HEADERS, "Prefer": "return=representation"}, timeout=5)
    respuesta.raise_for_status()
    return respuesta.json()["id"]


def _replicar_paciente(cur, paciente_id):
    cur.execute("SELECT * FROM pacientes WHERE id = %s AND NOT por_conciliar;", (paciente_id,))
    paciente = cripto.descifrar_paciente(cur.fetchone())
    if paciente is None:
        return None
    recurso = paciente_a_fhir(paciente)
    return _actualizar_fhir(recurso, SID_DOCUMENTO, paciente["documento"])


def _replicar_remision(cur, remision_id):
    cur.execute("SELECT * FROM remisiones WHERE id = %s;", (remision_id,))
    remision = cur.fetchone()
    if not remision:
        return None, None
    paciente = _replicar_paciente(cur, remision["paciente_id"])
    if not paciente:
        return None, None
    recurso = remision_a_fhir(remision, paciente)
    recurso["identifier"] = [{"system": SID_LOCAL, "value": str(remision_id)}]
    return _actualizar_fhir(recurso, SID_LOCAL, remision_id), paciente


def _replicar_observacion(cur, observacion_id):
    cur.execute("""SELECT o.*, o.fecha_observacion AT TIME ZONE current_setting('TimeZone') AS instante
                   FROM observaciones o WHERE id = %s;""", (observacion_id,))
    observacion = cur.fetchone()
    if not observacion:
        return
    if observacion["remision_id"]:
        encuentro, paciente = _replicar_remision(cur, observacion["remision_id"])
    else:
        encuentro, paciente = None, _replicar_paciente(cur, observacion["paciente_id"])
    if paciente:
        observacion["fecha_observacion"] = observacion["instante"]
        recurso = observacion_a_fhir(observacion, encuentro, paciente)
        recurso["identifier"] = [{"system": SID_LOCAL, "value": str(observacion_id)}]
        _actualizar_fhir(recurso, SID_LOCAL, observacion_id)


def sincronizar_siguiente():
    """Toma un registro de la cola con bloqueo y conserva los fallos para reintento."""
    with closing(get_db()) as conn, conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Serializa el acceso a HAPI entre trabajadores. Los dependientes también
        # pueden actualizar Patient y Encounter, inclusive tras una reconstrucción.
        cur.execute("SELECT pg_try_advisory_xact_lock(638140762109);")
        if not next(iter(cur.fetchone().values())):
            return False
        cur.execute("""SELECT tipo, registro_id, intentos FROM fhir_pendientes
                       WHERE intentar_en <= now() ORDER BY intentar_en, tipo, registro_id
                       LIMIT 1 FOR UPDATE SKIP LOCKED;""")
        fila = cur.fetchone()
        if not fila:
            return False
        tipo, registro_id = fila["tipo"], fila["registro_id"]
        try:
            {"Patient": _replicar_paciente, "Encounter": _replicar_remision,
             "Observation": _replicar_observacion}[tipo](cur, registro_id)
        except (requests.RequestException, ValueError, KeyError) as exc:
            intento = fila["intentos"] + 1
            cur.execute("""UPDATE fhir_pendientes SET intentos = %s,
                           intentar_en = now() + make_interval(secs => %s)
                           WHERE tipo = %s AND registro_id = %s;""",
                        (intento, min(300, 2 ** min(intento, 9)), tipo, registro_id))
            print(f"[FHIR] pendiente {tipo}/{registro_id}, intento {intento}: {type(exc).__name__}")
        else:
            cur.execute("DELETE FROM fhir_pendientes WHERE tipo = %s AND registro_id = %s;", (tipo, registro_id))
    return True


def ejecutar_replica():
    while True:
        try:
            if sincronizar_siguiente():
                continue
        except psycopg2.Error as exc:
            print(f"[FHIR] cola de sincronización no disponible: {type(exc).__name__}")
        time.sleep(2)


# ── GET: consultar recursos ya creados en HAPI FHIR ─────────────

def consultar_recurso_fhir(tipo: str, fhir_id: str) -> dict:
    r = requests.get(f"{FHIR_BASE_URL}/{tipo}/{fhir_id}", headers=HEADERS)
    r.raise_for_status()
    return r.json()
# ── R08: Remisión entre médicos (remisiones_medicas) → ServiceRequest ─────

def remision_medica_a_fhir(remision: dict, patient_fhir_id: str,
                            medico_nombre: str = "Médico HUV",
                            especialista_nombre: str = "Especialista HUV") -> dict:
    """remisiones_medicas -> ServiceRequest (remisión a especialista)."""
    estado_fhir = {
        "pendiente": "active",
        "aceptada": "completed",
        "rechazada": "revoked",
        "completada": "completed",
    }.get(remision["estado"], "active")
    return {
        "resourceType": "ServiceRequest",
        "status": estado_fhir,
        "intent": "order",
        "priority": "routine",
        "code": {
            "coding": [{
                "system": "http://snomed.info/sct",
                "code": "306206005",
                "display": "Referral to service"
            }],
            "text": remision["motivo"],
        },
        "subject": {"reference": f"Patient/{patient_fhir_id}"},
        "requester": {**_profesional_local(remision["medico_id"]), "display": medico_nombre},
        "performer": [{**_profesional_local(remision["especialista_id"]), "display": especialista_nombre}],
        "reasonCode": [{"text": remision["motivo"]}],
        "note": [{"text": remision.get("observaciones") or ""}],
    }


def buscar_paciente_fhir_por_documento(documento: str):
    """Busca un Patient en FHIR por su documento. Devuelve el id o None."""
    r = requests.get(
        f"{FHIR_BASE_URL}/Patient",
        params={"identifier": f"urn:trazared:documento|{documento}"},
        headers=HEADERS,
        timeout=10,
    )
    if r.status_code != 200:
        return None
    bundle = r.json()
    entradas = bundle.get("entry", [])
    if not entradas:
        return None
    return entradas[0]["resource"]["id"]


def obtener_o_crear_paciente_fhir(documento: str) -> str:
    """Busca el Patient en FHIR; si no existe, lo crea desde Postgres."""
    fhir_id = buscar_paciente_fhir_por_documento(documento)
    if fhir_id:
        return fhir_id
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # R27: el documento está cifrado en la base; se busca por su índice ciego
    cur.execute("SELECT * FROM pacientes WHERE documento_idx = %s;", (cripto.indice_ciego(documento),))
    paciente = cripto.descifrar_paciente(cur.fetchone())
    cur.close(); conn.close()
    if paciente is None:
        raise ValueError(f"Paciente con documento {documento} no existe en Postgres")
    return _actualizar_fhir(paciente_a_fhir(paciente), SID_DOCUMENTO, paciente["documento"])


def sincronizar_remision_medica(remision_id: int, patient_fhir_id: str,
                                 medico_nombre: str = "Médico HUV",
                                 especialista_nombre: str = "Especialista HUV") -> str:
    """Envía una remisión médica a FHIR como ServiceRequest. Devuelve el id FHIR."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM remisiones_medicas WHERE id = %s;", (remision_id,))
    remision = cur.fetchone()
    cur.close(); conn.close()
    if remision is None:
        raise ValueError("Remisión médica no encontrada en Postgres")
    recurso = remision_medica_a_fhir(remision, patient_fhir_id,
                                      medico_nombre, especialista_nombre)
    resultado = crear_recurso_fhir(recurso)
    return resultado["id"]

# ── R21 · Suscripción reactiva a Observation de glucosa (LOINC 2339-0) ─────
# HAPI FHIR, al detectar una Observation nueva con este código, hace POST al
# endpoint configurado en el canal rest-hook. La API responde y publica el
# evento para que el médico sea notificado en vivo.

URL_API_RECEPTORA = os.getenv("API_WEBHOOK_URL", "http://api:8000/fhir/observacion")
GLUCOSA_LOINC = "2339-0"


def _cabecera_webhook() -> str:
    return "X-Webhook-Secret"


def _secreto_webhook() -> str:
    return os.getenv("FHIR_WEBHOOK_SECRET") or cripto.derivar("webhook-fhir")


def crear_subscription_fhir(max_intentos: int = 10, espera_seg: int = 3) -> dict:
    """Crea (o reemplaza) la Subscription de HAPI FHIR que avisa a la API
    cuando llega una Observation de glucosa (LOINC 2339-0).

    El canal es rest-hook, apuntando a POST {URL_API_RECEPTORA}.
    Si ya existe una con el mismo criterio, la elimina primero (idempotente).

    Reintenta si HAPI FHIR todavía no está listo (bootea en ~30s).
    """
    import time

    criterio = f"Observation?code={GLUCOSA_LOINC}"

    # Esperar a que HAPI FHIR esté disponible
    for intento in range(1, max_intentos + 1):
        try:
            r = requests.get(f"{FHIR_BASE_URL}/metadata", timeout=5)
            if r.status_code == 200:
                print(f"[FHIR] HAPI disponible tras {intento} intento(s)")
                break
        except requests.RequestException:
            pass
        print(f"[FHIR] esperando a HAPI FHIR… (intento {intento}/{max_intentos})")
        time.sleep(espera_seg)
    else:
        print("[FHIR] HAPI FHIR no respondió; no se creó la Subscription")
        return {}

    # 1. Eliminar Subscriptions previas con el mismo criterio (idempotente)
    try:
        r = requests.get(
            f"{FHIR_BASE_URL}/Subscription",
            params={"criteria": criterio, "_count": "100"},
            headers=HEADERS, timeout=10,
        )
        if r.status_code == 200:
            for entrada in r.json().get("entry", []):
                sub_id = entrada["resource"]["id"]
                requests.delete(f"{FHIR_BASE_URL}/Subscription/{sub_id}",
                                headers=HEADERS, timeout=5)
                print(f"[FHIR] Subscription vieja {sub_id} eliminada")
    except requests.RequestException as e:
        print(f"[FHIR] no se pudieron listar Subscriptions: {e}")

    # 2. Crear la nueva Subscription
    subscription = {
        "resourceType": "Subscription",
        "status": "requested",
        "reason": "Notificar a TrazaRed HUV cuando llegue una Observation de glucosa",
        "criteria": criterio,
        "channel": {
            "type": "rest-hook",
            "endpoint": URL_API_RECEPTORA,
            "payload": "application/fhir+json",
            # R29: HAPI envía este secreto en cada aviso; la API rechaza los que no lo traen.
            "header": ["Content-Type: application/fhir+json",
                       f"{_cabecera_webhook()}: {_secreto_webhook()}"],
        },
    }
    r = requests.post(f"{FHIR_BASE_URL}/Subscription",
                      json=subscription, headers=HEADERS, timeout=10)
    if r.status_code not in (200, 201):
        print(f"[FHIR] error creando Subscription: {r.status_code} {r.text[:300]}")
        return {}
    resultado = r.json()
    print(f"[FHIR] Subscription creada: {resultado.get('id')} → {URL_API_RECEPTORA}")
    return resultado


def paciente_id_por_fhir_reference(reference: str):
    """Dada una referencia FHIR 'Patient/abc-123', devuelve el id de Postgres
    o None si no se encuentra."""
    if not reference or "/" not in reference:
        return None
    fhir_id = reference.split("/")[-1]
    try:
        r = requests.get(f"{FHIR_BASE_URL}/Patient/{fhir_id}", headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        patient = r.json()
        for ident in patient.get("identifier", []):
            if ident.get("system") == "urn:trazared:documento":
                documento = ident.get("value")
                conn = get_db()
                cur = conn.cursor()
                cur.execute("SELECT id FROM pacientes WHERE documento_idx = %s;", (cripto.indice_ciego(documento),))
                fila = cur.fetchone()
                cur.close(); conn.close()
                return fila[0] if fila else None
    except requests.RequestException:
        return None
    return None


def sincronizar_observacion_fhir(paciente_id: int, valor: float,
                                  unidad: str = "mg/dL",
                                  loinc: str = GLUCOSA_LOINC,
                                  fecha: str = None) -> dict:
    """Crea una Observation de glucosa en HAPI FHIR para el paciente dado.

    Se usa desde el endpoint `/fhir/observacion/simular` para la demo.
    """
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM pacientes WHERE id = %s;", (paciente_id,))
    paciente = cripto.descifrar_paciente(cur.fetchone())
    cur.close(); conn.close()
    if paciente is None:
        raise ValueError("Paciente no encontrado")

    # Buscar o crear el Patient en FHIR
    patient_fhir_id = obtener_o_crear_paciente_fhir(paciente["documento"])

    obs = {
        "resourceType": "Observation",
        "status": "final",
        "code": {
            "coding": [{"system": "http://loinc.org", "code": loinc, "display": "Glucose"}],
            "text": "Glucosa en sangre",
        },
        "subject": {"reference": f"Patient/{patient_fhir_id}"},
        "valueQuantity": {"value": float(valor), "unit": unidad, "system": UCUM, "code": unidad},
    }
    if fecha:
        obs["effectiveDateTime"] = fecha

    r = requests.post(f"{FHIR_BASE_URL}/Observation", json=obs, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()
