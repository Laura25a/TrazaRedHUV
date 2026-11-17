"""Perfil FHIR R4 de intercambio entre TrazaRed y las instituciones socias."""
import json
import os
import re
from datetime import datetime, timedelta, timezone

import cripto
from validacion_clinica import SIGNOS, validar_signo

SID_FEDERADO = os.getenv("SID_FEDERADO") or cripto.SID_FEDERADO
SID_REMISION = "urn:trazared:remision"
SID_ENCUENTRO = "urn:trazared:encuentro"
SID_PROFESIONAL = "urn:trazared:profesional"
SID_OBSERVACION = "urn:trazared:observacion"
CLASE_REMISION = {"system": "urn:trazared:clase-encuentro", "code": "remision", "display": "Episodio de remisión"}
SISTEMA_CLASE_HL7 = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
CLASES_HL7 = ("AMB", "EMER", "FLD", "HH", "IMP", "ACUTE", "NONAC", "OBSENC", "PRENC", "SS", "VR")

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
UCUM = "http://unitsofmeasure.org"
# Perfil común: códigos LOINC que este sistema entiende (signos vitales y glucosa)
CATALOGO_LOINC = {
    "8867-4": ("Frecuencia cardíaca", "/min"), "8480-6": ("Presión arterial sistólica", "mm[Hg]"),
    "8462-4": ("Presión arterial diastólica", "mm[Hg]"), "9279-1": ("Frecuencia respiratoria", "/min"),
    "59408-5": ("Saturación de oxígeno", "%"), "2708-6": ("Saturación de oxígeno", "%"),
    "8310-5": ("Temperatura corporal", "Cel"), "9269-2": ("Escala de Glasgow", "{score}"),
    "2339-0": ("Glucosa en sangre", "mg/dL"),
}
SNOMED_REMISION = ("3457005", "Remisión del paciente")        # Patient referral

class Rechazo(Exception):
    def __init__(self, status: int, codigo: str, textos):
        self.status, self.codigo, self.textos = status, codigo, textos


def _json(cuerpo: bytes, tipo_esperado: str) -> dict:
    try:
        recurso = json.loads(cuerpo.decode("utf-8"))
    except Exception:
        raise Rechazo(400, "structure", "El cuerpo no es JSON válido")
    if not isinstance(recurso, dict) or recurso.get("resourceType") != tipo_esperado:
        raise Rechazo(400, "structure", f"Se esperaba un recurso FHIR de tipo {tipo_esperado}")
    return recurso


def _obj(valor):
    return valor if isinstance(valor, dict) else {}


def _lista(valor):
    return valor if isinstance(valor, list) else []


def _federado_de(recurso):
    ids = recurso.get("identifier")
    valores = {i["value"].lower() for i in ([ids] if isinstance(ids, dict) else _lista(ids))
               if _obj(i).get("system") == SID_FEDERADO and isinstance(i.get("value"), str)
               and re.fullmatch(r"[0-9a-fA-F]{64}", i["value"])}
    return next(iter(valores)) if len(valores) == 1 else None


def _identificador(referencia, sistema):
    referencia = _obj(referencia)
    tipo = {SID_REMISION: "ServiceRequest", SID_ENCUENTRO: "Encounter",
            SID_PROFESIONAL: "Practitioner", SID_OBSERVACION: "Observation"}[sistema]
    if referencia.get("type") not in (None, tipo, f"http://hl7.org/fhir/StructureDefinition/{tipo}"):
        return None
    ident = _obj(referencia.get("identifier"))
    valor = ident.get("value")
    if ident.get("system") == sistema and isinstance(valor, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", valor):
        return valor
    return None


def _identificador_recurso(recurso, sistema):
    ids = _lista(_obj(recurso).get("identifier"))
    return _identificador({"identifier": ids[0]}, sistema) if len(ids) == 1 else None


def _validar_observation(o, pos, exigir_sujeto=False):
    errores = []
    if o.get("status") not in ("final", "amended", "corrected"):
        errores.append(f"{pos}: status debe ser final, amended o corrected")
    codigos = [c.get("code") for c in _lista(_obj(o.get("code")).get("coding")) if _obj(c).get("system") == LOINC]
    loinc = codigos[0] if len(codigos) == 1 and isinstance(codigos[0], str) else None
    perfil = CATALOGO_LOINC.get(loinc)
    if not perfil:
        errores.append(f"{pos}: se requiere un LOINC del perfil")
    cantidad = _obj(o.get("valueQuantity"))
    valor = cantidad.get("value")
    if type(valor) not in (int, float) or not -1e9 < valor < 1e9:
        errores.append(f"{pos}: valueQuantity.value debe ser un número finito")
    unidad = cantidad.get("code")
    if cantidad.get("system") != UCUM or not perfil or unidad != perfil[1]:
        errores.append(f"{pos}: unidad UCUM esperada: {perfil[1] if perfil else 'según LOINC'}")
    sujeto = _obj(o.get("subject"))
    federado = _federado_de(sujeto)
    if exigir_sujeto and (not federado or sujeto.get("type") not in (None, "Patient")):
        errores.append(f"{pos}: subject.identifier debe traer el identificador federado del paciente")
    if exigir_sujeto and ("reference" in sujeto or any(
        "reference" in _obj(ref) for ref in [*_lista(o.get("basedOn")), o.get("encounter"), *_lista(o.get("performer"))]
    )):
        errores.append(f"{pos}: una Observation suelta usa identificadores lógicos del perfil")

    fecha = o.get("effectiveDateTime")
    instante = None
    if fecha is not None:
        try:
            instante = datetime.fromisoformat(fecha.replace("Z", "+00:00"))
            if instante.tzinfo is None:
                raise ValueError
            instante = instante.astimezone(timezone.utc)
            if instante > datetime.now(timezone.utc) + timedelta(minutes=5):
                raise ValueError
            instante = instante.isoformat()
        except (AttributeError, ValueError):
            errores.append(f"{pos}: effectiveDateTime debe tener fecha, hora y zona y no estar en el futuro")
    solicitudes = _lista(o.get("basedOn"))
    referencia = _identificador(solicitudes[0], SID_REMISION) if len(solicitudes) == 1 else None
    encuentro = _identificador(o.get("encounter"), SID_ENCUENTRO)
    actuantes = _lista(o.get("performer"))
    profesional = _identificador(actuantes[0], SID_PROFESIONAL) if len(actuantes) == 1 else None
    identificador = _identificador_recurso(o, SID_OBSERVACION)
    for campo, valor_id in (("basedOn", referencia), ("encounter", encuentro),
                           ("performer", profesional), ("identifier", identificador)):
        if campo in o and not valor_id:
            errores.append(f"{pos}: {campo} debe traer un identificador del perfil")
    if not errores:
        try:
            if loinc in SIGNOS or loinc == "2708-6":
                validar_signo("59408-5" if loinc == "2708-6" else loinc, valor, unidad)
            elif valor <= 0:
                raise ValueError("La glucosa debe ser positiva")
        except ValueError as exc:
            errores.append(f"{pos}: {exc}")
    if errores:
        return errores, None
    return [], {"loinc": loinc, "valor": float(valor), "unidad": unidad, "nombre": perfil[0],
                "federado": federado, "fecha": instante, "referencia": referencia, "encuentro": encuentro,
                "profesional": profesional, "identificador": identificador}


def _sujeto_valido(sujeto, federado, referencias):
    sujeto = _obj(sujeto)
    por_id = "identifier" in sujeto
    por_ref = "reference" in sujeto
    return (federado and (por_id or por_ref) and sujeto.get("type") in (None, "Patient")
            and (not por_id or _federado_de(sujeto) == federado)
            and (not por_ref or sujeto["reference"] in referencias))


def _referencia_coincide(referencia, recurso, por_url):
    if "reference" not in referencia:
        return True
    url = referencia["reference"]
    return isinstance(url, str) and recurso is not None and por_url.get(url) is recurso


def _validar_bundle(b: dict) -> dict:
    """Devuelve las partes ya interpretadas o lanza Rechazo(422) con TODOS los problemas."""
    errores = []
    entradas = b.get("entry")
    if b.get("type") != "collection" or not isinstance(entradas, list) or not entradas:
        raise Rechazo(422, "invalid", "El intercambio admite Bundle.type collection con al menos una entrada")
    recursos = []
    por_url = {}
    for entrada in entradas:
        entrada = _obj(entrada)
        recurso, url = entrada.get("resource"), entrada.get("fullUrl")
        if not isinstance(recurso, dict) or not isinstance(url, str) or "/_history/" in url:
            raise Rechazo(422, "invalid", "Cada entrada requiere resource y fullUrl sin versión")
        if not re.fullmatch(r"urn:uuid:[0-9a-fA-F-]{36}|https?://[^\s]+", url):
            raise Rechazo(422, "invalid", "Bundle.entry.fullUrl debe ser una URL absoluta o urn:uuid")
        if any(campo in entrada for campo in ("request", "response", "search")):
            raise Rechazo(422, "invalid", "Un Bundle collection no admite request, response ni search")
        if url in por_url:
            raise Rechazo(422, "invalid", "Bundle.entry.fullUrl está repetido")
        recursos.append(recurso)
        por_url[url] = recurso
    por_tipo: dict = {}
    for r in recursos:
        tipo = r.get("resourceType")
        por_tipo.setdefault(tipo if isinstance(tipo, str) else "(sin tipo válido)", []).append(r)
    desconocidos = set(por_tipo) - {"Patient", "ServiceRequest", "Observation", "DiagnosticReport", "Consent",
                                    "Organization", "Practitioner", "Encounter"}
    if desconocidos:
        errores.append(f"Recursos fuera del perfil: {', '.join(sorted(map(str, desconocidos)))}")

    # Patient: exactamente uno, identificado SOLO por el identificador federado
    pacientes = por_tipo.get("Patient", [])
    paciente, federado = (pacientes[0] if len(pacientes) == 1 else None), None
    if paciente is None:
        errores.append("El Bundle debe traer exactamente un Patient")
    else:
        federado = _federado_de(paciente)
        if not federado:
            errores.append(f"Patient.identifier debe traer el identificador federado (system {SID_FEDERADO})")
        ids = _lista(paciente.get("identifier"))
        if (len(ids) != 1 or any(k in paciente for k in ("name", "birthDate", "address", "telecom", "photo",
                                                          "contact", "text", "contained", "extension"))):
            errores.append("Patient solo debe identificar al paciente mediante el ID federado")

    # ServiceRequest: exactamente uno, con código SNOMED y un identificador para responderle
    solicitudes = por_tipo.get("ServiceRequest", [])
    sr, referencia, motivo, prioridad = (solicitudes[0] if len(solicitudes) == 1 else None), None, "", "routine"
    if sr is None:
        errores.append("El Bundle debe traer exactamente un ServiceRequest (la remisión)")
    else:
        if sr.get("status") not in ("active", "draft"):
            errores.append(f"ServiceRequest.status debe ser active (llegó {sr.get('status')!r})")
        if sr.get("intent") not in ("order", "proposal", "plan", "original-order"):
            errores.append("ServiceRequest.intent debe ser order")
        snomed = [c.get("code") for c in _lista(_obj(sr.get("code")).get("coding"))
                  if _obj(c).get("system") == SNOMED]
        if snomed != [SNOMED_REMISION[0]]:
            errores.append(f"ServiceRequest.code debe usar SNOMED CT {SNOMED_REMISION[0]} (remisión del paciente)")
        referencia = _identificador_recurso(sr, SID_REMISION)
        if not referencia:
            errores.append("ServiceRequest.identifier debe traer el número de remisión con el sistema del perfil")
        razones = [_obj(r).get("text") for r in _lista(sr.get("reasonCode"))]
        motivo = "; ".join(t for t in razones if isinstance(t, str)) or str(_obj(sr.get("code")).get("text") or "")
        notas = "; ".join(str(n["text"]) for n in _lista(sr.get("note")) if _obj(n).get("text"))
        motivo = (motivo + (f". {notas}" if notas else "")).strip()
        if len(motivo) < 5:
            errores.append("ServiceRequest.reasonCode[].text debe decir el motivo de la remisión")
        prioridad = sr.get("priority") or "routine"
        if prioridad not in ("routine", "urgent", "asap", "stat"):
            errores.append("ServiceRequest.priority debe ser routine, urgent, asap o stat")

    referencias = [e["fullUrl"] for e in entradas if _obj(e).get("resource") is paciente and _obj(e).get("fullUrl")]
    if paciente and isinstance(paciente.get("id"), str):
        referencias.append("Patient/" + paciente["id"])
    encuentros = por_tipo.get("Encounter", [])
    encuentro = _identificador_recurso(encuentros[0], SID_ENCUENTRO) if len(encuentros) == 1 else None
    if encuentros and not encuentro:
        errores.append("Encounter debe identificar un único episodio del perfil")
    elif encuentro:
        episodio = encuentros[0]
        clase = _obj(episodio.get("class"))
        sistema, codigo = clase.get("system"), clase.get("code")
        if not ((sistema, codigo) == (CLASE_REMISION["system"], CLASE_REMISION["code"])
                or (sistema == SISTEMA_CLASE_HL7 and codigo in CLASES_HL7)):
            errores.append("Encounter.class debe usar la clase de remisión o una clase de atención de HL7 R4")
        if episodio.get("status") not in ("planned", "arrived", "triaged", "in-progress",
                                         "onleave", "finished", "cancelled", "entered-in-error", "unknown"):
            errores.append("Encounter.status no es un estado FHIR R4 válido")
    for tipo, campo in (("ServiceRequest", "subject"), ("Observation", "subject"),
                        ("DiagnosticReport", "subject"), ("Consent", "patient"), ("Encounter", "subject")):
        for recurso in por_tipo.get(tipo, []):
            if not _sujeto_valido(recurso.get(campo), federado, referencias):
                errores.append(f"{tipo}.{campo} debe referirse al Patient del Bundle")
    profesional = _identificador(_obj(sr).get("requester"), SID_PROFESIONAL)
    if sr and "encounter" in sr and (not encuentro or _identificador(sr["encounter"], SID_ENCUENTRO) != encuentro
                                   or not _referencia_coincide(sr["encounter"], encuentros[0], por_url)):
        errores.append("ServiceRequest.encounter debe corresponder al Encounter del Bundle")

    observaciones = []
    for n, o in enumerate(por_tipo.get("Observation", []), 1):
        errs, obs = _validar_observation(o, f"Observation[{n}]")
        errores.extend(errs)
        if obs:
            if obs["referencia"] and (obs["referencia"] != referencia
                                      or not _referencia_coincide(o["basedOn"][0], sr, por_url)):
                errores.append(f"Observation[{n}].basedOn no coincide con la solicitud del Bundle")
            if obs["encuentro"] and (obs["encuentro"] != encuentro
                                     or not _referencia_coincide(o["encounter"], encuentros[0], por_url)):
                errores.append(f"Observation[{n}].encounter no coincide con el episodio del Bundle")
            observaciones.append(obs)

    # DiagnosticReport de IA: solo el APROBADO viaja (status final). Nunca uno pendiente ni rechazado.
    reporte = None
    for n, d in enumerate(por_tipo.get("DiagnosticReport", []), 1):
        if d.get("status") != "final":
            errores.append(f"DiagnosticReport[{n}]: solo se acepta un reporte aprobado (status final); llegó {d.get('status')!r}")
        else:
            reporte = {"conclusion": str(d.get("conclusion") or "")[:600],
                       "codigo": _obj(d.get("code")).get("text") or "Reporte de apoyo diagnóstico"}

    consentimientos = por_tipo.get("Consent", [])
    if consentimientos and any(c.get("status") != "active" for c in consentimientos):
        errores.append("Consent.status debe ser active")
    if errores:
        raise Rechazo(422, "invalid", errores)
    return {"federado": federado, "referencia": referencia, "motivo": motivo[:600],
            "prioridad": prioridad, "observaciones": observaciones, "reporte": reporte,
            "encuentro": encuentro, "profesional": profesional, "con_consent": bool(consentimientos)}
