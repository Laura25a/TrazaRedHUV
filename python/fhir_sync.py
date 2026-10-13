"""
Servicio de integración TrazaRed HUV: sincroniza pacientes, remisiones y observaciones
de PostgreSQL (Neon) hacia el servidor HAPI FHIR, como recursos Patient, Encounter y Observation.
"""
import os
from pathlib import Path
import requests
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / "pass.env", override=True)
PG_CONNECTION_STRING = os.getenv("PG_CONNECTION_STRING")
FHIR_BASE_URL = os.getenv("FHIR_BASE_URL", "http://localhost:8081/fhir")

HEADERS = {"Content-Type": "application/fhir+json"}


def get_db():
    return psycopg2.connect(PG_CONNECTION_STRING)


# ── Mapeo: fila de Postgres -> recurso FHIR ─────────────────────

def paciente_a_fhir(paciente: dict) -> dict:
    """pacientes -> Patient"""
    return {
        "resourceType": "Patient",
        "identifier": [{"system": "urn:trazared:documento", "value": paciente["documento"]}],
        "name": [{"text": paciente["nombre"]}],
        "gender": {"F": "female", "M": "male"}.get(paciente["genero"].upper(), "unknown"),
    }


def remision_a_fhir(remision: dict, patient_fhir_id: str) -> dict:
    """remisiones -> Encounter"""
    estado_fhir = {
        "pendiente": "planned", "en_gestion": "arrived", "aceptada": "in-progress",
        "rechazada": "cancelled", "completada": "finished",
    }.get(remision["estado"], "unknown")
    return {
        "resourceType": "Encounter",
        "status": estado_fhir,
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "TRNS", "display": "transfer"},
        "subject": {"reference": f"Patient/{patient_fhir_id}"},
        "reasonCode": [{"text": remision["motivo"]}],
        "period": {"start": str(remision["fecha_solicitud"])},
    }


def observacion_a_fhir(observacion: dict, encounter_fhir_id: str, patient_fhir_id: str) -> dict:
    """observaciones -> Observation"""
    code = {"text": observacion["tipo"]}
    if observacion["codigo_loinc"]:
        code = {"coding": [{"system": "http://loinc.org", "code": observacion["codigo_loinc"]}],
                "text": observacion["tipo"]}
    return {
        "resourceType": "Observation",
        "status": "final",
        "code": code,
        "subject": {"reference": f"Patient/{patient_fhir_id}"},
        "encounter": {"reference": f"Encounter/{encounter_fhir_id}"},
        "valueQuantity": {"value": float(observacion["valor"]), "unit": observacion["unidad"] or ""},
    }


# ── POST: crear recursos en HAPI FHIR ───────────────────────────

def crear_recurso_fhir(recurso: dict) -> dict:
    tipo = recurso["resourceType"]
    r = requests.post(f"{FHIR_BASE_URL}/{tipo}", json=recurso, headers=HEADERS)
    r.raise_for_status()
    return r.json()


def sincronizar_paciente(paciente_id: int) -> str:
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM pacientes WHERE id = %s;", (paciente_id,))
    paciente = cur.fetchone()
    cur.close(); conn.close()
    if paciente is None:
        raise ValueError("Paciente no encontrado en Postgres")
    resultado = crear_recurso_fhir(paciente_a_fhir(paciente))
    return resultado["id"]  # id asignado por HAPI FHIR


def sincronizar_remision(remision_id: int, patient_fhir_id: str) -> str:
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM remisiones WHERE id = %s;", (remision_id,))
    remision = cur.fetchone()
    cur.close(); conn.close()
    if remision is None:
        raise ValueError("Remisión no encontrada en Postgres")
    resultado = crear_recurso_fhir(remision_a_fhir(remision, patient_fhir_id))
    return resultado["id"]


def sincronizar_observacion(observacion_id: int, encounter_fhir_id: str, patient_fhir_id: str) -> str:
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM observaciones WHERE id = %s;", (observacion_id,))
    observacion = cur.fetchone()
    cur.close(); conn.close()
    if observacion is None:
        raise ValueError("Observación no encontrada en Postgres")
    resultado = crear_recurso_fhir(observacion_a_fhir(observacion, encounter_fhir_id, patient_fhir_id))
    return resultado["id"]


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
        "requester": {"display": medico_nombre},
        "performer": [{"display": especialista_nombre}],
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
    cur.execute("SELECT * FROM pacientes WHERE documento = %s;", (documento,))
    paciente = cur.fetchone()
    cur.close(); conn.close()
    if paciente is None:
        raise ValueError(f"Paciente con documento {documento} no existe en Postgres")
    resultado = crear_recurso_fhir(paciente_a_fhir(paciente))
    return resultado["id"]


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

if __name__ == "__main__":
    # Ejemplo de uso manual: sincronizar el paciente y remisión creados en la Sección 6
    pid_fhir = sincronizar_paciente(paciente_id=1)
    print("Patient creado en FHIR con id:", pid_fhir)

    eid_fhir = sincronizar_remision(remision_id=1, patient_fhir_id=pid_fhir)
    print("Encounter creado en FHIR con id:", eid_fhir)

    print(consultar_recurso_fhir("Patient", pid_fhir))
