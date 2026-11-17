"""Regresiones del perfil FHIR R4 de TrazaRed."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import interop_perfil as interop


def observacion(codigo="2339-0", unidad="mg/dL", valor=123, federado="a" * 64):
    return {
        "resourceType": "Observation", "status": "final",
        "subject": {"identifier": {"system": interop.SID_FEDERADO, "value": federado}},
        "code": {"coding": [{"system": interop.LOINC, "code": codigo}]},
        "valueQuantity": {"system": interop.UCUM, "code": unidad, "value": valor},
    }


@pytest.mark.parametrize("dato", [
    observacion(unidad="Cel"),
    observacion(valor=float("nan")),
    observacion(codigo="59408-5", unidad="%", valor=110),
    observacion(federado="documento-en-claro"),
])
def test_rechaza_observacion_fuera_del_perfil(dato):
    errores, normalizada = interop._validar_observation(dato, "Observation", exigir_sujeto=True)
    assert errores and normalizada is None


def test_conserva_observacion_valida():
    errores, normalizada = interop._validar_observation(observacion(), "Observation", exigir_sujeto=True)
    assert not errores and normalizada["valor"] == 123


def bundle_minimo():
    paciente = "urn:uuid:35cb1363-ce64-423b-aeb6-269643640b18"
    solicitud = "urn:uuid:5906b83d-43e2-4932-a82a-3a189df3b87b"
    return {"resourceType": "Bundle", "type": "collection", "entry": [
        {"fullUrl": paciente, "resource": {"resourceType": "Patient", "identifier": [
            {"system": interop.SID_FEDERADO, "value": "a" * 64}]}},
        {"fullUrl": solicitud, "resource": {"resourceType": "ServiceRequest", "status": "active",
            "intent": "order", "subject": {"reference": paciente},
            "identifier": [{"system": interop.SID_REMISION, "value": "REM-2026-000001"}],
            "code": {"coding": [{"system": interop.SNOMED, "code": "3457005"}]},
            "reasonCode": [{"text": "Remisión de prueba"}]}},
    ]}


@pytest.mark.parametrize("tipo", ["transaction", "batch", "message", "document", "searchset"])
def test_tipos_de_bundle_no_son_intercambiables(tipo):
    with pytest.raises(interop.Rechazo) as exc:
        interop._validar_bundle({**bundle_minimo(), "type": tipo})
    assert exc.value.status == 422


def test_collection_acepta_solo_referencias_consistentes():
    b = bundle_minimo()
    assert interop._validar_bundle(b)["federado"] == "a" * 64
    b["entry"][1]["resource"]["subject"]["identifier"] = {"system": interop.SID_FEDERADO, "value": "b" * 64}
    with pytest.raises(interop.Rechazo) as exc:
        interop._validar_bundle(b)
    assert exc.value.status == 422
    assert "ServiceRequest.subject" in str(exc.value.textos)


def test_collection_no_ejecuta_operaciones_de_transaccion():
    b = bundle_minimo()
    b["entry"][0]["request"] = {"method": "PUT", "url": "Patient?identifier=un-identificador"}
    with pytest.raises(interop.Rechazo, match="collection"):
        interop._validar_bundle(b)


def test_collection_exige_snomed_de_remision():
    b = bundle_minimo()
    b["entry"][1]["resource"]["code"]["coding"][0]["code"] = "404684003"
    with pytest.raises(interop.Rechazo, match="3457005"):
        interop._validar_bundle(b)


def test_collection_no_acepta_nombre_en_la_identidad():
    b = bundle_minimo()
    b["entry"][0]["resource"]["name"] = [{"text": "Paciente sintético"}]
    with pytest.raises(interop.Rechazo, match="ID federado"):
        interop._validar_bundle(b)


def test_encuentro_acepta_clase_clinica_del_perfil():
    b = bundle_minimo()
    episodio = {
        "resourceType": "Encounter", "status": "planned",
        "subject": b["entry"][1]["resource"]["subject"],
        "identifier": [{"system": interop.SID_ENCUENTRO, "value": "REM-2026-000001"}],
        "class": {"system": interop.SISTEMA_CLASE_HL7, "code": "TRNS"},
    }
    b["entry"].append({"fullUrl": "urn:uuid:6830d1c0-99a9-422e-8615-b51060c5cf9c", "resource": episodio})
    with pytest.raises(interop.Rechazo, match="Encounter.class"):
        interop._validar_bundle(b)
    episodio["class"] = interop.CLASE_REMISION
    assert interop._validar_bundle(b)["encuentro"] == "REM-2026-000001"
