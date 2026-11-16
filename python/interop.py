"""
interop.py — Interoperabilidad entre sistemas (proyecto final: R29, R30, R31, R32, R33).

Cada equipo es una institución. El médico de la institución A remite un paciente al
especialista de la institución B y la información viaja en FHIR, de API a API:

    A  ── POST /oauth/token (client_id, client_secret, scope) ──────────▶  B
    A  ◀── JWT de sistema, 10 minutos ──────────────────────────────────   B
    A  ── POST /interop/Bundle  (Bearer + firma HMAC + Bundle FHIR) ────▶  B   201 / 202
                                    B valida, crea la remisión y avisa EN VIVO al especialista
    A  ◀── POST /interop/Task   (Bearer + firma HMAC + Task) ───────────   B   aceptada / rechazada
    A avisa EN VIVO al médico. Los dos lados auditan con "origen=externo".

Lo que responde el receptor (el código le dice al otro qué corregir):
    201  aceptado y asociado a un paciente          401  sin token, token vencido o firma inválida
    202  aceptado; falta conciliar al paciente      403  token válido, sin el permiso (scope)
    400  no es JSON / no es un recurso FHIR         409  mensaje repetido
    422  no cumple el perfil: va un OperationOutcome y NO se guarda nada

Controles (cada uno responde a una fila de docs/STRIDE.md):
  · R31  El socio entra con credenciales de SISTEMA (OAuth2 client credentials). Su token
         lleva tipo="sistema" y solo abre /interop/*: la API clínica lo rechaza con 403.
  · R29  Cada mensaje va firmado: X-Signature = v1=HMAC-SHA256(secreto, "ts.id." + cuerpo).
         Con eso se sabe QUIÉN lo envió, QUÉ envió (cualquier cambio rompe la firma) y
         CUÁNDO (X-Timestamp, ±5 min), y X-Message-Id impide que se procese DOS veces.
  · R30  Perfil FHIR: LOINC en las Observation, SNOMED en el ServiceRequest, UCUM en las
         unidades. Un recurso inválido se rechaza completo (una sola transacción).
  · R32  Consentimiento y minimización: solo se envía si hay Consent vigente del paciente
         para ESA institución, y el Bundle no lleva documento, teléfono, correo ni
         dirección: el paciente se nombra con un identificador federado (HMAC).

El contrato exacto (rutas, cabeceras, ejemplo de cada mensaje) está en docs/INTEROP.md.
"""
import json
import os
import re
import time
import uuid
from datetime import date, datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from pydantic import BaseModel, Field

import auth
import cripto
import seguridad
from validaciones import bloquear_remision, hoy_bogota
from auth import (PG_CONNECTION_STRING, ROLES_CLINICOS, get_db, get_usuario_actual, registrar_auditoria,
                  requiere_rol)

router = APIRouter(tags=["Interoperabilidad entre sistemas"])

INSTITUCION = (os.getenv("INSTITUCION_NOMBRE") or "Hospital Universitario del Valle")
# Secreto con el que las instituciones del acuerdo calculan el identificador federado.
# El valor por defecto es el de la demo de clase: se cambia en el .env de ambos equipos.
SECRETO_FEDERADO = (os.getenv("SECRETO_FEDERADO") or "secreto-federado-del-curso-solo-demo")
SID_FEDERADO = os.getenv("SID_FEDERADO") or cripto.SID_FEDERADO
SID_REMISION = "urn:trazared:remision"
URL_LOCAL = (os.getenv("INTEROP_URL_LOCAL") or "http://127.0.0.1:8000")     # para el socio de prueba (bucle local)
MINUTOS_TOKEN_SISTEMA = 10
TOLERANCIA_S = int(os.getenv("INTEROP_TOLERANCIA_S") or "300")             # reloj del emisor: ±5 min
SCOPES = ("system/Bundle.write", "system/Task.write", "system/Observation.write")
SCOPE_DE = {"Bundle": SCOPES[0], "Task": SCOPES[1], "Observation": SCOPES[2]}

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
GLUCOSA_CRITICA = (54, 300)                                     # por debajo o desde aquí: alerta crítica (R20)


# ── Respuestas FHIR ───────────────────────────────────────────────────────
def operation_outcome(status: int, codigo: str, textos, severidad: str = "error") -> JSONResponse:
    textos = textos if isinstance(textos, list) else [textos]
    return JSONResponse(status_code=status, media_type="application/fhir+json", content={
        "resourceType": "OperationOutcome",
        "issue": [{"severity": severidad, "code": codigo, "diagnostics": t} for t in textos]})


class Rechazo(Exception):
    def __init__(self, status: int, codigo: str, textos):
        self.status, self.codigo, self.textos = status, codigo, textos


def _conexion():
    return psycopg2.connect(PG_CONNECTION_STRING, connect_timeout=5)


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Secretos de los socios (cifrados en reposo) ───────────────────────────
def _guardar_secreto(client_id: str, campo: str, valor: Optional[str]) -> Optional[str]:
    return cripto.cifrar(valor, f"socios:{client_id}:{campo}") if valor else None


def _leer_secreto(socio: dict, campo: str) -> Optional[str]:
    valor = socio.get(campo)
    return cripto.descifrar(valor, f"socios:{socio['client_id']}:{campo}") if valor else None


def _socio(cur, socio_id: int = None, client_id: str = None) -> Optional[dict]:
    if socio_id is not None:
        cur.execute("SELECT * FROM sistemas_socios WHERE id = %s;", (socio_id,))
    else:
        cur.execute("SELECT * FROM sistemas_socios WHERE client_id = %s;", (client_id,))
    return cur.fetchone()


# ══════════════════════════════════════════════════════════════════════════
# 1. R31 · Token de sistema (OAuth 2.0 client credentials)
# ══════════════════════════════════════════════════════════════════════════
def _emitir_token(client_id: str, client_secret: str, scope: str, ip: str) -> dict:
    conn = _conexion()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        socio = _socio(cur, client_id=client_id)
        cur.close()
        valido = bool(socio) and socio["activo"] and auth.verificar_password(client_secret or "", socio["client_secret_hash"])
        if not valido:
            if not socio:
                auth.hash_password(client_secret or "x")       # mismo costo de cómputo: no revela si el client_id existe
            registrar_auditoria(conn, None, "interop_token_rechazado", "sistemas_socios", socio["id"] if socio else None,
                                f"origen=externo ip={ip} client_id={str(client_id)[:40]!r}")
            raise Rechazo(401, "invalid_client", "client_id o client_secret incorrectos")
        permitidos = set(socio["scopes"].split())
        pedidos = set((scope or socio["scopes"]).split())
        if not pedidos or not pedidos <= permitidos:
            raise Rechazo(403, "invalid_scope", f"scope no permitido para este sistema: {sorted(pedidos - permitidos)}")
        token = auth.crear_token({"tipo": "sistema", "sub": client_id, "socio_id": socio["id"],
                                  "scope": " ".join(sorted(pedidos))}, minutos=MINUTOS_TOKEN_SISTEMA)
        registrar_auditoria(conn, None, "interop_token_emitido", "sistemas_socios", socio["id"],
                            f"origen=externo socio={socio['nombre']} scope={' '.join(sorted(pedidos))} ip={ip}")
        return {"access_token": token, "token_type": "bearer", "expires_in": MINUTOS_TOKEN_SISTEMA * 60,
                "scope": " ".join(sorted(pedidos))}
    finally:
        conn.close()


@router.post("/oauth/token")
async def token_de_sistema(request: Request):
    """R31 · Un sistema también se identifica. Recibe grant_type=client_credentials,
    client_id, client_secret y scope (formulario o JSON) y entrega un JWT de 10 minutos
    que SOLO sirve en /interop/*."""
    try:
        if "json" in (request.headers.get("content-type") or ""):
            datos = await request.json()
        else:
            datos = dict(await request.form())
    except Exception:
        datos = {}
    if not isinstance(datos, dict) or datos.get("grant_type") != "client_credentials":
        return JSONResponse(status_code=400, content={"error": "unsupported_grant_type",
                                                      "error_description": "Usa grant_type=client_credentials"})
    try:
        return await run_in_threadpool(_emitir_token, str(datos.get("client_id") or ""),
                                       str(datos.get("client_secret") or ""), str(datos.get("scope") or ""),
                                       seguridad.ip_cliente(request))
    except Rechazo as r:
        return JSONResponse(status_code=r.status, content={"error": r.codigo, "error_description": r.textos},
                            headers={"WWW-Authenticate": "Bearer"} if r.status == 401 else None)


# ══════════════════════════════════════════════════════════════════════════
# 2. R29 · Autenticar CADA mensaje: token + scope + firma + tiempo + una sola vez
# ══════════════════════════════════════════════════════════════════════════
def _firma_esperada(secreto: str, ts: str, mensaje_id: str, cuerpo: bytes) -> str:
    return "v1=" + cripto.firmar(secreto, f"{ts}.{mensaje_id}.".encode() + cuerpo)


def _autenticar(conn, cabeceras: dict, cuerpo: bytes, tipo: str, ip: str) -> tuple[dict, str]:
    """Devuelve (socio, mensaje_id) o lanza Rechazo con el código que corresponde."""
    autorizacion = cabeceras.get("authorization") or ""
    if not autorizacion.lower().startswith("bearer "):
        raise Rechazo(401, "login", "Falta el token (Authorization: Bearer ...)")
    try:
        datos = jwt.decode(autorizacion[7:].strip(), auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
    except JWTError:
        raise Rechazo(401, "login", "Token inválido o vencido: pide uno nuevo en /oauth/token")
    if datos.get("tipo") != "sistema":
        raise Rechazo(403, "forbidden", "Este endpoint solo acepta tokens de sistema (client credentials)")
    if SCOPE_DE[tipo] not in (datos.get("scope") or "").split():
        raise Rechazo(403, "forbidden", f"El token no tiene el permiso {SCOPE_DE[tipo]}")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    socio = _socio(cur, socio_id=datos.get("socio_id"))
    cur.close()
    if socio is None or not socio["activo"]:
        raise Rechazo(403, "forbidden", "El sistema socio está desactivado")

    ts = cabeceras.get("x-timestamp") or ""
    mensaje_id = cabeceras.get("x-message-id") or cabeceras.get("x-nonce") or cabeceras.get("idempotency-key") or ""
    firma = cabeceras.get("x-signature") or ""
    if not (ts and mensaje_id and firma):
        raise Rechazo(401, "security", "Faltan cabeceras de firma: X-Timestamp, X-Message-Id y X-Signature")
    if not re.fullmatch(r"[\w.:-]{8,80}", mensaje_id):
        raise Rechazo(401, "security", "X-Message-Id inválido (8 a 80 caracteres: letras, números, . : - _)")
    esperado = _firma_esperada(_leer_secreto(socio, "secreto_firma"), ts, mensaje_id, cuerpo)
    if not cripto.iguales(firma if firma.startswith("v1=") else "v1=" + firma, esperado):
        seguridad.registrar_incidente(conn, "firma_invalida", None, ip, "interop",
                                      f"socio={socio['nombre']} mensaje={mensaje_id[:40]}", bloquear=False)
        raise Rechazo(401, "security", "Firma inválida: el mensaje fue alterado o el secreto no coincide")
    try:
        desfase = abs(time.time() - float(ts))
    except ValueError:
        raise Rechazo(401, "security", "X-Timestamp debe ser la hora UTC en segundos (epoch)")
    if desfase > TOLERANCIA_S:
        raise Rechazo(401, "security", f"Mensaje vencido: X-Timestamp difiere {int(desfase)} s (máximo {TOLERANCIA_S})")
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM interop_mensajes WHERE socio_id = %s AND direccion = 'entrante' AND mensaje_id = %s;",
                (socio["id"], mensaje_id))
    repetido = cur.fetchone() is not None
    cur.close()
    if repetido:
        raise Rechazo(409, "duplicate", f"Mensaje repetido: {mensaje_id} ya fue procesado")
    return socio, mensaje_id


def _registrar_mensaje(cur, socio_id, direccion, mensaje_id, tipo, estado, codigo, remision_id=None,
                       paciente_id=None, referencia=None, detalle=None, recurso=None) -> int:
    cur.execute("""INSERT INTO interop_mensajes (socio_id, direccion, mensaje_id, tipo, estado, codigo_http,
                                                 remision_id, paciente_id, referencia_externa, detalle, recurso)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;""",
                (socio_id, direccion, mensaje_id, tipo, estado, codigo, remision_id, paciente_id, referencia,
                 (detalle or "")[:500] or None, psycopg2.extras.Json(recurso) if recurso is not None else None))
    fila = cur.fetchone()
    return fila["id"] if isinstance(fila, dict) else fila[0]


def _avisar(roles: tuple, evento: dict, extra_ids: tuple = ()) -> None:
    try:
        from eventos import ids_con_rol, publicar
        conn = _conexion()
        try:
            destinatarios = set(ids_con_rol(conn, *roles)) | {i for i in extra_ids if i}
        finally:
            conn.close()
        publicar(sorted(destinatarios), evento)
    except Exception as e:
        print(f"[interop] no se pudo publicar el evento {evento.get('tipo')}: {e}")


def _rechazar(conn, socio, mensaje_id, tipo, r: Rechazo, ip: str) -> JSONResponse:
    """Deja constancia del rechazo (auditoría con origen externo, bitácora de mensajes
    y aviso al admin) y arma la respuesta. No queda NINGÚN dato clínico a medias."""
    try:
        conn.rollback()
        textos = r.textos if isinstance(r.textos, list) else [r.textos]
        if socio is not None and r.status in (400, 422):
            cur = conn.cursor()
            _registrar_mensaje(cur, socio["id"], "entrante", f"{mensaje_id}#rechazo-{int(time.time() * 1000)}", tipo,
                               "rechazado", r.status, detalle="; ".join(textos))
            conn.commit()
            cur.close()
        registrar_auditoria(conn, None, "interop_rechazo", "interop_mensajes", None,
                            f"origen=externo socio={socio['nombre'] if socio else 'desconocido'} tipo={tipo} "
                            f"http={r.status} ip={ip} motivo={'; '.join(textos)[:220]}")
        if socio is not None and r.status in (400, 422):
            _avisar(("admin",), {"tipo": "interop_rechazo", "origen": "externo", "socio": socio["nombre"],
                                 "mensaje": f"Se rechazó un {tipo} de {socio['nombre']} por validación ({r.status}): {textos[0][:120]}"})
    except Exception as e:
        print(f"[interop] no se pudo registrar el rechazo: {e}")
    cabeceras = {"WWW-Authenticate": "Bearer"} if r.status == 401 else None
    respuesta = operation_outcome(r.status, r.codigo, r.textos)
    if cabeceras:
        respuesta.headers.update(cabeceras)
    return respuesta


def _json(cuerpo: bytes, tipo_esperado: str) -> dict:
    try:
        recurso = json.loads(cuerpo.decode("utf-8"))
    except Exception:
        raise Rechazo(400, "structure", "El cuerpo no es JSON válido")
    if not isinstance(recurso, dict) or recurso.get("resourceType") != tipo_esperado:
        raise Rechazo(400, "structure", f"Se esperaba un recurso FHIR de tipo {tipo_esperado}")
    return recurso


# ══════════════════════════════════════════════════════════════════════════
# 3. R30 · Perfil FHIR: validar ANTES de guardar
# ══════════════════════════════════════════════════════════════════════════
def _federado_de(recurso: dict, campo: str = "identifier") -> Optional[str]:
    """El identificador federado: el que trae nuestro system; si el socio usa otro
    nombre de system, el primer identificador con forma de HMAC (64 hexadecimales)."""
    idents = recurso.get(campo)
    if isinstance(idents, dict):
        idents = [idents]
    idents = [i for i in (idents or []) if isinstance(i, dict)]
    for i in idents:
        if i.get("system") == SID_FEDERADO and i.get("value"):
            return str(i["value"]).lower()
    for i in idents:
        if re.fullmatch(r"[0-9a-fA-F]{64}", str(i.get("value") or "")):
            return str(i["value"]).lower()
    return None


def _validar_observation(o: dict, pos: str, exigir_sujeto: bool = False) -> tuple[list, Optional[dict]]:
    errores = []
    if o.get("status") not in ("final", "amended", "corrected"):
        errores.append(f"{pos}: status debe ser final (llegó {o.get('status')!r})")
    codigos = [c for c in ((o.get("code") or {}).get("coding") or []) if isinstance(c, dict)]
    loinc = next((c.get("code") for c in codigos if c.get("system") == LOINC), None)
    if loinc is None:
        errores.append(f"{pos}: code.coding debe traer un código con system {LOINC}")
    elif loinc not in CATALOGO_LOINC:
        errores.append(f"{pos}: el código LOINC {loinc!r} no está en el perfil ({', '.join(sorted(CATALOGO_LOINC))})")
    cantidad = o.get("valueQuantity") or {}
    valor = cantidad.get("value")
    if not isinstance(valor, (int, float)) or isinstance(valor, bool):
        errores.append(f"{pos}: valueQuantity.value debe ser un número")
    if cantidad.get("system") != UCUM or not (cantidad.get("code") or cantidad.get("unit")):
        errores.append(f"{pos}: valueQuantity debe traer la unidad UCUM (system {UCUM} y code)")
    federado = _federado_de(o.get("subject") or {}) if exigir_sujeto else None
    if exigir_sujeto and not federado:
        errores.append(f"{pos}: subject.identifier debe traer el identificador federado del paciente")
    if errores:
        return errores, None
    return [], {"loinc": loinc, "valor": float(valor), "unidad": cantidad.get("code") or cantidad.get("unit"),
                "nombre": CATALOGO_LOINC[loinc][0], "federado": federado}


def _validar_bundle(b: dict) -> dict:
    """Devuelve las partes ya interpretadas o lanza Rechazo(422) con TODOS los problemas."""
    errores = []
    entradas = b.get("entry")
    if b.get("type") not in ("collection", "message", "transaction", "document") or not isinstance(entradas, list) or not entradas:
        raise Rechazo(422, "invalid", "Bundle.type debe ser collection (o message) y traer al menos una entrada")
    recursos = [e.get("resource") for e in entradas if isinstance(e, dict) and isinstance(e.get("resource"), dict)]
    por_tipo: dict = {}
    for r in recursos:
        por_tipo.setdefault(r.get("resourceType"), []).append(r)
    desconocidos = set(por_tipo) - {"Patient", "ServiceRequest", "Observation", "DiagnosticReport", "Consent",
                                    "Organization", "Practitioner", "MessageHeader", "Condition"}
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
        if paciente.get("gender") not in (None, "male", "female", "other", "unknown"):
            errores.append("Patient.gender debe ser male, female, other o unknown")
        if paciente.get("birthDate") and not re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", str(paciente["birthDate"])):
            errores.append("Patient.birthDate debe tener formato AAAA-MM-DD")

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
        if not any(isinstance(c, dict) and c.get("system") == SNOMED and c.get("code")
                   for c in ((sr.get("code") or {}).get("coding") or [])):
            errores.append(f"ServiceRequest.code.coding debe traer un código SNOMED CT (system {SNOMED})")
        idents = [i for i in (sr.get("identifier") or []) if isinstance(i, dict) and i.get("value")]
        referencia = str(idents[0]["value"])[:80] if idents else None
        if not referencia:
            errores.append("ServiceRequest.identifier debe traer el número de la remisión en el sistema de origen")
        razones = [r.get("text") or ((r.get("coding") or [{}])[0].get("display")) for r in (sr.get("reasonCode") or [])
                   if isinstance(r, dict)]
        motivo = "; ".join(str(t) for t in razones if t) or (sr.get("code") or {}).get("text") or ""
        notas = "; ".join(str(n.get("text")) for n in (sr.get("note") or []) if isinstance(n, dict) and n.get("text"))
        motivo = (motivo + (f". {notas}" if notas else "")).strip()
        if len(motivo) < 5:
            errores.append("ServiceRequest.reasonCode[].text debe decir el motivo de la remisión")
        prioridad = sr.get("priority") or "routine"
        if prioridad not in ("routine", "urgent", "asap", "stat"):
            errores.append("ServiceRequest.priority debe ser routine, urgent, asap o stat")

    observaciones = []
    for n, o in enumerate(por_tipo.get("Observation", []), 1):
        errs, obs = _validar_observation(o, f"Observation[{n}]")
        errores.extend(errs)
        if obs:
            observaciones.append(obs)

    # DiagnosticReport de IA: solo el APROBADO viaja (status final). Nunca uno pendiente ni rechazado.
    reporte = None
    for n, d in enumerate(por_tipo.get("DiagnosticReport", []), 1):
        if d.get("status") != "final":
            errores.append(f"DiagnosticReport[{n}]: solo se acepta un reporte aprobado (status final); llegó {d.get('status')!r}")
        else:
            reporte = {"conclusion": str(d.get("conclusion") or "")[:600],
                       "codigo": (d.get("code") or {}).get("text") or "Reporte de apoyo diagnóstico"}

    consentimientos = por_tipo.get("Consent", [])
    if consentimientos and any(c.get("status") != "active" for c in consentimientos):
        errores.append("Consent.status debe ser active")
    if errores:
        raise Rechazo(422, "invalid", errores)
    nombre = None
    for nm in paciente.get("name") or []:
        if isinstance(nm, dict):
            nombre = nm.get("text") or " ".join([*(nm.get("given") or []), nm.get("family") or ""]).strip()
            if nombre:
                break
    return {"federado": federado, "nombre": (nombre or "")[:120], "genero": paciente.get("gender"),
            "nacimiento": paciente.get("birthDate"), "referencia": referencia, "motivo": motivo[:600],
            "prioridad": prioridad, "observaciones": observaciones, "reporte": reporte,
            "con_consent": bool(consentimientos)}


# ── Encontrar al paciente sin recibir su documento ────────────────────────
_federados: dict = {}          # {paciente_id: id federado} (se calcula una vez por paciente)


def paciente_por_federado(cur, federado: str) -> Optional[int]:
    cur.execute("SELECT id FROM pacientes WHERE id_federado = %s LIMIT 1;", (federado,))
    fila = cur.fetchone()
    if fila:
        return fila[0] if not isinstance(fila, dict) else fila["id"]
    cur.execute("SELECT id, documento FROM pacientes WHERE NOT por_conciliar ORDER BY id;")
    for fila in cur.fetchall():
        pid, doc = (fila["id"], fila["documento"]) if isinstance(fila, dict) else fila
        if pid not in _federados:
            try:
                _federados[pid] = cripto.id_federado(cripto.descifrar(doc, cripto.aad_paciente(pid, "documento")),
                                                     SECRETO_FEDERADO)
            except ValueError:
                continue
        if cripto.iguales(_federados[pid], federado):
            return pid
    return None


def federado_de_paciente(paciente_id: int, documento_claro: str) -> str:
    return cripto.id_federado(documento_claro, SECRETO_FEDERADO)


# ══════════════════════════════════════════════════════════════════════════
# 4. Entrada: POST /interop/Bundle · la remisión que llega de otra institución
# ══════════════════════════════════════════════════════════════════════════
PRIORIDAD_TXT = {"stat": "CRÍTICA", "asap": "URGENTE", "urgent": "URGENTE", "routine": "PROGRAMADA"}
GENERO = {"male": "M", "female": "F"}


def _recibir_bundle(cabeceras: dict, cuerpo: bytes, ip: str):
    conn = _conexion()
    socio, mensaje_id = None, cabeceras.get("x-message-id") or "sin-id"
    try:
        socio, mensaje_id = _autenticar(conn, cabeceras, cuerpo, "Bundle", ip)
        partes = _validar_bundle(_json(cuerpo, "Bundle"))
        motivo, habia_orden = seguridad.neutralizar(partes["motivo"])       # texto de otro sistema = DATO (R25)

        cur = conn.cursor()
        paciente_id = paciente_por_federado(cur, partes["federado"])
        conciliado = paciente_id is not None
        nacimiento = partes["nacimiento"]
        if nacimiento:
            try:
                nacimiento = date.fromisoformat(nacimiento)
                if nacimiento > hoy_bogota():
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise Rechazo(422, "invalid", "Patient.birthDate debe ser una fecha válida y no futura") from exc
        if not conciliado:      # 202: se recibe, pero el paciente queda "por conciliar" con su identificador federado
            cur.execute("""INSERT INTO pacientes (nombre, documento, genero, eps, fecha_nacimiento, id_federado, por_conciliar)
                           VALUES (%s, '', %s, %s, %s, %s, TRUE) RETURNING id;""",
                        (partes["nombre"] or f"Paciente por conciliar {partes['federado'][:8]}",
                         GENERO.get(partes["genero"], "O"), "Por conciliar", nacimiento or None,
                         partes["federado"]))
            paciente_id = cur.fetchone()[0]
            cripto.cifrar_fila_paciente(cur, paciente_id, {"documento": f"PC-{partes['federado'][:16]}"})
        cur.execute("""INSERT INTO remisiones (paciente_id, institucion_origen, institucion_destino, fecha_solicitud,
                                               motivo, estado, convenio_vigente, origen_externo, socio_id,
                                               referencia_externa, estado_interop)
                       VALUES (%s, registrar_institucion(%s), registrar_institucion(%s),
                               (now() AT TIME ZONE 'America/Bogota')::date, %s, 'pendiente', FALSE, TRUE, %s, %s, 'recibida')
                       RETURNING id;""",
                    (paciente_id, socio["nombre"], INSTITUCION,
                     f"[{PRIORIDAD_TXT[partes['prioridad']]}] {motivo}", socio["id"], partes["referencia"]))
        remision_id = cur.fetchone()[0]
        for o in partes["observaciones"]:
            cur.execute("""INSERT INTO observaciones (remision_id, tipo, codigo_loinc, valor, unidad)
                           VALUES (%s, %s, %s, %s, %s);""", (remision_id, o["nombre"], o["loinc"], o["valor"], o["unidad"]))
        resumen = {"referencia": partes["referencia"], "prioridad": partes["prioridad"], "motivo": motivo,
                   "observaciones": [{k: o[k] for k in ("loinc", "nombre", "valor", "unidad")} for o in partes["observaciones"]],
                   "reporte_ia": partes["reporte"], "consentimiento_declarado": partes["con_consent"],
                   "paciente_federado": partes["federado"][:12] + "…"}
        codigo = 201 if conciliado else 202
        _registrar_mensaje(cur, socio["id"], "entrante", mensaje_id, "Bundle",
                           "aceptado" if conciliado else "por_conciliar", codigo, remision_id, paciente_id,
                           partes["referencia"], recurso=resumen)
        cur.close()

        registrar_auditoria(conn, None, "interop_remision_recibida", "remisiones", remision_id,
                            f"origen=externo socio={socio['nombre']} mensaje={mensaje_id} http={codigo} "
                            f"referencia={partes['referencia']} observaciones={len(partes['observaciones'])} "
                            f"reporte_ia={'sí' if partes['reporte'] else 'no'} ip={ip}")
        if habia_orden:
            seguridad.registrar_inyeccion_indirecta(None, f"interop:{socio['nombre']}",
                                                    f"orden escondida en el motivo de la remisión {remision_id}")
        _avisar(("especialista", "admin"), {
            "tipo": "remision_externa_recibida", "remision_id": remision_id, "paciente_id": paciente_id,
            "origen": "externo", "socio": socio["nombre"],
            "mensaje": f"Remisión recibida de {socio['nombre']}: {motivo[:90]}"
                       + ("" if conciliado else " (paciente por conciliar)")})
        return JSONResponse(status_code=codigo, content={
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "information", "code": "informational",
                       "diagnostics": "Remisión aceptada y asociada a un paciente" if conciliado
                       else "Remisión aceptada; falta conciliar al paciente"}],
            "remision": {"id": remision_id, "radicado": f"REM-{datetime.now().year}-{remision_id:06d}",
                         "estado": "pendiente", "referencia": partes["referencia"], "paciente_conciliado": conciliado}})
    except Rechazo as r:
        return _rechazar(conn, socio, mensaje_id, "Bundle", r, ip)
    except psycopg2.errors.CheckViolation as exc:
        return _rechazar(conn, socio, mensaje_id, "Bundle", Rechazo(422, "invalid", exc.diag.message_primary), ip)
    except psycopg2.errors.UniqueViolation:
        return _rechazar(conn, socio, mensaje_id, "Bundle", Rechazo(409, "duplicate", "Mensaje repetido"), ip)
    finally:
        conn.close()


@router.post("/interop/Bundle")
async def recibir_remision(request: Request):
    """R30 · Entrada de remisiones del sistema socio: Bundle con Patient, ServiceRequest,
    Observation(s), DiagnosticReport aprobado y Consent. Token de sistema + firma HMAC."""
    cuerpo = await request.body()
    return await run_in_threadpool(_recibir_bundle, dict(request.headers), cuerpo, seguridad.ip_cliente(request))


# ══════════════════════════════════════════════════════════════════════════
# 5. Entrada: POST /interop/Task · la respuesta del socio a una remisión NUESTRA
# ══════════════════════════════════════════════════════════════════════════
ESTADO_DE_TASK = {"accepted": "aceptada", "completed": "aceptada", "in-progress": "en_gestion",
                  "rejected": "rechazada", "cancelled": "rechazada", "failed": "rechazada"}


def _recibir_task(cabeceras: dict, cuerpo: bytes, ip: str):
    conn = _conexion()
    socio, mensaje_id = None, cabeceras.get("x-message-id") or "sin-id"
    try:
        socio, mensaje_id = _autenticar(conn, cabeceras, cuerpo, "Task", ip)
        t = _json(cuerpo, "Task")
        errores = []
        if t.get("status") not in ESTADO_DE_TASK:
            errores.append(f"Task.status debe ser uno de: {', '.join(ESTADO_DE_TASK)}")
        foco = t.get("focus") or {}
        ident = foco.get("identifier") if isinstance(foco.get("identifier"), dict) else {}
        referencia = str(ident.get("value") or "")[:80]
        if not referencia:
            errores.append("Task.focus.identifier.value debe traer el número de la remisión que se responde")
        if errores:
            raise Rechazo(422, "invalid", errores)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT r.id, r.paciente_id, r.creado_por, r.estado, r.activo FROM interop_mensajes m
                       JOIN remisiones r ON r.id = m.remision_id
                       WHERE m.socio_id = %s AND m.direccion = 'saliente' AND m.tipo = 'Bundle'
                         AND m.referencia_externa = %s ORDER BY m.id DESC LIMIT 1 FOR UPDATE OF r;""", (socio["id"], referencia))
        rem = cur.fetchone()
        if rem is None:
            raise Rechazo(422, "not-found", f"No hay una remisión enviada a {socio['nombre']} con el número {referencia}")
        nuevo = ESTADO_DE_TASK[t["status"]]
        if not rem["activo"] or (rem["estado"] != nuevo and rem["estado"] not in ("pendiente", "en_gestion")):
            raise Rechazo(409, "conflict", "La remisión ya no admite cambios de estado del socio")
        nota, _ = seguridad.neutralizar(str((t.get("statusReason") or {}).get("text")
                                            or ((t.get("note") or [{}])[0].get("text") if t.get("note") else "") or "")[:300])
        if rem["estado"] != nuevo:
            _guardar_historial(cur, rem["id"], None)
        cur.execute("""UPDATE remisiones SET estado = %s, estado_interop = 'respondida',
                              institucion_acepta = CASE WHEN %s = 'aceptada' THEN %s ELSE institucion_acepta END,
                              cerrada_en = CASE WHEN %s = 'rechazada' THEN now() ELSE cerrada_en END
                       WHERE id = %s;""", (nuevo, nuevo, socio["nombre"], nuevo, rem["id"]))
        _registrar_mensaje(cur, socio["id"], "entrante", mensaje_id, "Task", "aceptado", 201, rem["id"],
                           rem["paciente_id"], referencia, detalle=nota,
                           recurso={"status": t["status"], "nota": nota, "referencia": referencia})
        cur.close()
        registrar_auditoria(conn, None, "interop_respuesta_recibida", "remisiones", rem["id"],
                            f"origen=externo socio={socio['nombre']} mensaje={mensaje_id} estado={nuevo} ip={ip}")
        verbo = {"aceptada": "ACEPTÓ", "rechazada": "RECHAZÓ", "en_gestion": "está gestionando"}[nuevo]
        _avisar(("admin",), {"tipo": "respuesta_socio", "remision_id": rem["id"], "paciente_id": rem["paciente_id"],
                             "origen": "externo", "socio": socio["nombre"], "estado": nuevo,
                             "mensaje": f"{socio['nombre']} {verbo} la remisión {referencia}" + (f": {nota[:80]}" if nota else "")},
                extra_ids=(rem["creado_por"],))
        return JSONResponse(status_code=201, content={
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "information", "code": "informational",
                       "diagnostics": f"Respuesta registrada: la remisión {referencia} quedó {nuevo}"}]})
    except Rechazo as r:
        return _rechazar(conn, socio, mensaje_id, "Task", r, ip)
    except psycopg2.errors.UniqueViolation:
        return _rechazar(conn, socio, mensaje_id, "Task", Rechazo(409, "duplicate", "Mensaje repetido"), ip)
    finally:
        conn.close()


@router.post("/interop/Task")
async def recibir_respuesta(request: Request):
    """La institución socia responde (acepta o rechaza) una remisión que nosotros enviamos."""
    cuerpo = await request.body()
    return await run_in_threadpool(_recibir_task, dict(request.headers), cuerpo, seguridad.ip_cliente(request))


# ══════════════════════════════════════════════════════════════════════════
# 6. Entrada: POST /interop/Observation · un dato clínico suelto (práctica de clase)
# ══════════════════════════════════════════════════════════════════════════
def _recibir_observation(cabeceras: dict, cuerpo: bytes, ip: str):
    conn = _conexion()
    socio, mensaje_id = None, cabeceras.get("x-message-id") or "sin-id"
    try:
        socio, mensaje_id = _autenticar(conn, cabeceras, cuerpo, "Observation", ip)
        errores, obs = _validar_observation(_json(cuerpo, "Observation"), "Observation", exigir_sujeto=True)
        if errores:
            raise Rechazo(422, "invalid", errores)
        cur = conn.cursor()
        paciente_id = paciente_por_federado(cur, obs["federado"])
        codigo = 201 if paciente_id else 202
        remision_id = None
        if paciente_id:
            cur.execute("""SELECT id FROM remisiones WHERE paciente_id = %s AND activo
                           ORDER BY fecha_solicitud DESC, id DESC LIMIT 1;""", (paciente_id,))
            fila = cur.fetchone()
            if fila:
                remision_id = fila[0]
                cur.execute("""INSERT INTO observaciones (remision_id, tipo, codigo_loinc, valor, unidad)
                               VALUES (%s, %s, %s, %s, %s);""",
                            (remision_id, obs["nombre"], obs["loinc"], obs["valor"], obs["unidad"]))
        _registrar_mensaje(cur, socio["id"], "entrante", mensaje_id, "Observation",
                           "aceptado" if paciente_id else "por_conciliar", codigo, remision_id, paciente_id,
                           recurso={k: obs[k] for k in ("loinc", "nombre", "valor", "unidad")})
        cur.close()
        registrar_auditoria(conn, None, "interop_observacion_recibida", "pacientes", paciente_id,
                            f"origen=externo socio={socio['nombre']} mensaje={mensaje_id} http={codigo} "
                            f"loinc={obs['loinc']} valor={obs['valor']:g} {obs['unidad']} ip={ip}")
        if paciente_id:
            critica = obs["loinc"] == "2339-0" and (obs["valor"] < GLUCOSA_CRITICA[0] or obs["valor"] >= GLUCOSA_CRITICA[1])
            texto = f"{obs['nombre']} {obs['valor']:g} {obs['unidad']} recibida de {socio['nombre']} para el paciente {paciente_id}"
            _avisar(("medico", "especialista", "admin"), {
                "tipo": "alerta_critica" if critica else "observacion_fhir", "paciente_id": paciente_id,
                "origen": "externo", "socio": socio["nombre"], "nivel": "critico",
                "mensaje": ("Valor crítico: " if critica else "") + texto})
        return JSONResponse(status_code=codigo, content={
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "information", "code": "informational",
                       "diagnostics": "Observación aceptada y asociada a un paciente" if paciente_id
                       else "Observación aceptada; falta conciliar al paciente"}]})
    except Rechazo as r:
        return _rechazar(conn, socio, mensaje_id, "Observation", r, ip)
    except psycopg2.errors.UniqueViolation:
        return _rechazar(conn, socio, mensaje_id, "Observation", Rechazo(409, "duplicate", "Mensaje repetido"), ip)
    finally:
        conn.close()


@router.post("/interop/Observation")
async def recibir_observacion(request: Request):
    """Una Observation del sistema socio (p. ej. glucosa LOINC 2339-0). Si el valor es
    crítico nace una alerta que queda activa hasta que un clínico la reconozca (R20)."""
    cuerpo = await request.body()
    return await run_in_threadpool(_recibir_observation, dict(request.headers), cuerpo, seguridad.ip_cliente(request))


# ══════════════════════════════════════════════════════════════════════════
# 7. Salida: hablarle al sistema socio (token + firma)
# ══════════════════════════════════════════════════════════════════════════
_tokens: dict = {}       # {socio_id: (token, vence_epoch)}


def _token_saliente(socio: dict, scope: str) -> str:
    _url_valida(socio.get("url_base"), bucle=bool(socio.get("de_prueba")))
    guardado = _tokens.get((socio["id"], scope))
    if guardado and guardado[1] - time.time() > 30:
        return guardado[0]
    if not (socio.get("url_base") and socio.get("cliente_saliente") and socio.get("secreto_saliente")):
        raise HTTPException(status_code=409, detail=f"Faltan la URL o las credenciales que {socio['nombre']} nos entregó. "
                                                    "El administrador las registra en Interoperabilidad › Socios.")
    try:
        r = requests.post(socio["url_base"].rstrip("/") + "/oauth/token", timeout=15, allow_redirects=False, data={
            "grant_type": "client_credentials", "client_id": socio["cliente_saliente"],
            "client_secret": _leer_secreto(socio, "secreto_saliente"), "scope": scope})
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"No se pudo conectar con {socio['nombre']}: {type(e).__name__}")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"{socio['nombre']} no entregó el token ({r.status_code}): {r.text[:160]}")
    datos = r.json()
    _tokens[(socio["id"], scope)] = (datos["access_token"], time.time() + int(datos.get("expires_in", 600)))
    return datos["access_token"]


def _enviar(socio: dict, tipo: str, recurso: dict) -> tuple[int, str, str]:
    """POST firmado al socio. Devuelve (código, mensaje_id, texto de la respuesta)."""
    token = _token_saliente(socio, SCOPE_DE[tipo])
    cuerpo = json.dumps(recurso, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ts, mensaje_id = str(int(time.time())), str(uuid.uuid4())
    secreto = _leer_secreto(socio, "firma_saliente")
    if not secreto:
        raise HTTPException(status_code=409, detail=f"Falta el secreto de firma que {socio['nombre']} nos entregó")
    try:
        r = requests.post(socio["url_base"].rstrip("/") + f"/interop/{tipo}", data=cuerpo, timeout=20, allow_redirects=False, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/fhir+json",
            "X-Timestamp": ts, "X-Message-Id": mensaje_id,
            "X-Signature": _firma_esperada(secreto, ts, mensaje_id, cuerpo)})
    except requests.RequestException as e:
        return 0, mensaje_id, f"sin conexión con {socio['nombre']}: {type(e).__name__}"
    if r.status_code == 401:
        _tokens.pop((socio["id"], SCOPE_DE[tipo]), None)
    try:
        cuerpo_resp = r.json()
        texto = "; ".join(str(i.get("diagnostics")) for i in cuerpo_resp.get("issue", []) if i.get("diagnostics")) \
            or json.dumps(cuerpo_resp, ensure_ascii=False)[:300]
    except Exception:
        texto = r.text[:300]
    return r.status_code, mensaje_id, texto


# ── Armar el Bundle: solo lo necesario para la atención (R32) ─────────────
def _radicado(remision: dict) -> str:
    return f"REM-{(remision.get('fecha_solicitud') or datetime.now()).year}-{remision['id']:06d}"


def _prioridad_fhir(motivo: str) -> str:
    m = (motivo or "").upper()
    return "stat" if "[CRITICA]" in m or "[CRÍTICA]" in m else ("urgent" if "[URGENTE]" in m else "routine")


def armar_bundle(cur, remision: dict, paciente: dict, socio: dict, consentimiento: dict) -> dict:
    """Patient (identificador federado, sin documento ni teléfono ni dirección),
    ServiceRequest (SNOMED), Observation (LOINC + UCUM), DiagnosticReport SOLO si un
    clínico lo aprobó, y el Consent que autoriza compartir con esa institución."""
    federado = federado_de_paciente(paciente["id"], paciente["documento"])
    ref_pac = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, federado))
    sujeto = {"reference": ref_pac, "identifier": {"system": SID_FEDERADO, "value": federado}}
    motivo = re.sub(r"^\[[^\]]+\]\s*", "", remision["motivo"] or "").split(" (propuesta del asistente")[0]
    entradas = [{"fullUrl": ref_pac, "resource": {
        "resourceType": "Patient", "identifier": [{"system": SID_FEDERADO, "value": federado}],
        "name": [{"text": paciente["nombre"]}],
        "gender": {"M": "male", "F": "female"}.get(paciente.get("genero"), "unknown"),
        **({"birthDate": paciente["fecha_nacimiento"].isoformat()} if paciente.get("fecha_nacimiento") else {})}}]
    entradas.append({"resource": {
        "resourceType": "ServiceRequest", "status": "active", "intent": "order",
        "priority": _prioridad_fhir(remision["motivo"]),
        "identifier": [{"system": SID_REMISION, "value": _radicado(remision)}],
        "code": {"coding": [{"system": SNOMED, "code": SNOMED_REMISION[0], "display": SNOMED_REMISION[1]}],
                 "text": "Remisión a otra institución"},
        "subject": sujeto, "authoredOn": _ahora(),
        "requester": {"display": INSTITUCION}, "performer": [{"display": socio["nombre"]}],
        "reasonCode": [{"text": motivo[:500]}]}})
    cur.execute("""SELECT DISTINCT ON (codigo_loinc) codigo_loinc, valor, unidad, fecha_observacion
                   FROM observaciones WHERE remision_id = %s AND codigo_loinc IS NOT NULL
                   ORDER BY codigo_loinc, fecha_observacion DESC, id DESC;""", (remision["id"],))
    for o in cur.fetchall():
        if o["codigo_loinc"] not in CATALOGO_LOINC:
            continue
        nombre, ucum = CATALOGO_LOINC[o["codigo_loinc"]]
        entradas.append({"resource": {
            "resourceType": "Observation", "status": "final",
            "code": {"coding": [{"system": LOINC, "code": o["codigo_loinc"], "display": nombre}]},
            "subject": sujeto, "effectiveDateTime": o["fecha_observacion"].isoformat(timespec="seconds"),
            "valueQuantity": {"value": float(o["valor"]), "unit": o["unidad"] or ucum, "system": UCUM, "code": ucum}}})
    cur.execute("""SELECT id, probabilidad_urgente, prediccion, critico_segun_umbral, modelo, revisado_en
                   FROM reportes_ia WHERE paciente_id = %s AND estado = 'aprobado'
                   ORDER BY revisado_en DESC NULLS LAST, id DESC LIMIT 1;""", (paciente["id"],))
    rep = cur.fetchone()
    if rep:                                    # nunca uno pendiente ni rechazado
        prob = f"{float(rep['probabilidad_urgente']):.2f}" if rep["probabilidad_urgente"] is not None else "sin dato"
        entradas.append({"resource": {
            "resourceType": "DiagnosticReport", "status": "final",
            "code": {"text": "Apoyo diagnóstico por IA: clasificación de urgencia (aprobado por un clínico)"},
            "subject": sujeto,
            "issued": (rep["revisado_en"] or datetime.now()).isoformat(timespec="seconds"),
            "conclusion": f"Probabilidad de urgencia {prob} ({rep['prediccion']}"
                          f"{', caso crítico' if rep['critico_segun_umbral'] else ''}). "
                          f"Modelo {rep['modelo'] or 'XGBoost'}. Reporte revisado y aprobado por un clínico."}})
    entradas.append({"resource": {
        "resourceType": "Consent", "status": "active",
        "scope": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/consentscope", "code": "patient-privacy"}]},
        "category": [{"coding": [{"system": LOINC, "code": "59284-0", "display": "Consent Document"}]}],
        "patient": sujeto, "dateTime": consentimiento["otorgado_en"].isoformat(timespec="seconds"),
        "organization": [{"display": INSTITUCION}],
        "policyRule": {"text": "Ley 1581 de 2012: autorización del titular para transferir datos sensibles de salud"},
        "provision": {"type": "permit", "actor": [{"role": {"text": "destinatario"}, "reference": {"display": socio["nombre"]}}],
                      "purpose": [{"display": consentimiento["finalidad"]}]}}})
    return {"resourceType": "Bundle", "type": "collection", "timestamp": _ahora(), "entry": entradas}


def _consentimiento_vigente(cur, paciente_id: int, socio_id: int) -> Optional[dict]:
    cur.execute("""SELECT id, finalidad, medio, otorgado_en FROM consentimientos
                   WHERE paciente_id = %s AND socio_id = %s AND vigente ORDER BY id DESC LIMIT 1;""",
                (paciente_id, socio_id))
    return cur.fetchone()


class EnviarIn(BaseModel):
    socio_id: int


@router.get("/interop/remisiones/{remision_id}/vista-previa")
def vista_previa(remision_id: int, socio_id: int = Query(...), db=Depends(get_db),
                 usuario=Depends(requiere_rol("admin", "medico"))):
    """Lo que saldría hacia el socio, ANTES de enviarlo: para que el médico vea que el
    Bundle no lleva documento, teléfono ni dirección, y si falta el consentimiento."""
    bloquear_remision(db, remision_id, usuario)
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    rem, pac, socio = _cargar_para_envio(cur, remision_id, socio_id)
    consent = _consentimiento_vigente(cur, pac["id"], socio["id"])
    bundle = armar_bundle(cur, rem, pac, socio, consent or {"otorgado_en": datetime.now(), "finalidad": "(sin registrar)"})
    cur.close()
    tipos = [e["resource"]["resourceType"] for e in bundle["entry"]]
    return {"consentimiento": bool(consent), "socio": socio["nombre"], "radicado": _radicado(rem),
            "recursos": {t: tipos.count(t) for t in dict.fromkeys(tipos)},
            "reporte_ia_aprobado": "DiagnosticReport" in tipos,
            "no_viaja": ["documento", "teléfono", "correo", "dirección", "EPS", "alergias", "tipo de sangre"],
            "bundle": bundle if consent else None}


def _guardar_historial(cur, remision_id, usuario_id):
    cur.execute("""INSERT INTO remisiones_historial (remision_id, dato_anterior, modificado_por)
                   SELECT id, to_jsonb(r), %s FROM remisiones r WHERE id = %s;""",
                (usuario_id, remision_id))


def _cargar_para_envio(cur, remision_id: int, socio_id: int):
    cur.execute("SELECT * FROM remisiones WHERE id = %s AND activo;", (remision_id,))
    rem = cur.fetchone()
    if rem is None:
        raise HTTPException(status_code=404, detail="Remisión no encontrada")
    if rem["origen_externo"]:
        raise HTTPException(status_code=409, detail="Esta remisión llegó de otra institución; no se reenvía")
    cur.execute("SELECT id, nombre, documento, genero, fecha_nacimiento FROM pacientes WHERE id = %s;", (rem["paciente_id"],))
    pac = cripto.descifrar_paciente(cur.fetchone())
    socio = _socio(cur, socio_id=socio_id)
    if socio is None or not socio["activo"]:
        raise HTTPException(status_code=404, detail="Institución socia no encontrada o desactivada")
    return rem, pac, socio


@router.post("/interop/remisiones/{remision_id}/enviar")
def enviar_remision(remision_id: int, body: EnviarIn, db=Depends(get_db),
                    usuario=Depends(requiere_rol("admin", "medico"))):
    """R30/R32 · El médico remite a la institución socia. Sin consentimiento vigente
    del paciente para ESA institución no sale nada (403)."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    bloquear_remision(db, remision_id, usuario, exigir_abierta=True)
    rem, pac, socio = _cargar_para_envio(cur, remision_id, body.socio_id)
    if rem["estado"] not in ("pendiente", "en_gestion"):
        raise HTTPException(409, "La remisión ya no está pendiente de gestión")
    consent = _consentimiento_vigente(cur, pac["id"], socio["id"])
    if consent is None:
        cur.close()
        registrar_auditoria(db, usuario["id"], "interop_envio_bloqueado", "remisiones", remision_id,
                            f"sin consentimiento del paciente para {socio['nombre']} (Ley 1581/2012)")
        raise HTTPException(status_code=403, detail=(
            f"El paciente no tiene consentimiento registrado para compartir sus datos con {socio['nombre']}. "
            "Regístralo antes de remitir (Ley 1581 de 2012)."))
    bundle = armar_bundle(cur, rem, pac, socio, consent)
    codigo, mensaje_id, texto = _enviar(socio, "Bundle", bundle)
    tipos = [e["resource"]["resourceType"] for e in bundle["entry"]]
    ok = codigo in (200, 201, 202)
    _registrar_mensaje(cur, socio["id"], "saliente", mensaje_id, "Bundle", "enviado" if ok else "error", codigo or None,
                       remision_id, pac["id"], _radicado(rem), detalle=texto,
                       recurso={"recursos": {t: tipos.count(t) for t in dict.fromkeys(tipos)}, "consentimiento_id": consent["id"]})
    if ok:
        _guardar_historial(cur, remision_id, usuario["id"])
        cur.execute("""UPDATE remisiones SET socio_id = %s, estado_interop = 'enviada', institucion_destino = registrar_institucion(%s),
                              estado = CASE WHEN estado = 'pendiente' THEN 'en_gestion' ELSE estado END
                       WHERE id = %s;""", (socio["id"], socio["nombre"], remision_id))
    cur.close()
    registrar_auditoria(db, usuario["id"], "interop_remision_enviada" if ok else "interop_envio_fallido",
                        "remisiones", remision_id,
                        f"origen=externo socio={socio['nombre']} mensaje={mensaje_id} http={codigo} "
                        f"recursos={len(tipos)} reporte_ia={'sí' if 'DiagnosticReport' in tipos else 'no'} "
                        f"consentimiento={consent['id']}")
    if not ok:
        raise HTTPException(status_code=502, detail=f"{socio['nombre']} no aceptó la remisión ({codigo or 'sin conexión'}): {texto}")
    return {"enviada": True, "codigo": codigo, "socio": socio["nombre"], "radicado": _radicado(rem),
            "detalle": texto, "paciente_conciliado": codigo == 201,
            "recursos": {t: tipos.count(t) for t in dict.fromkeys(tipos)}}


# ── El especialista responde una remisión recibida ────────────────────────
class ResponderIn(BaseModel):
    decision: str = Field(pattern="^(aceptar|rechazar)$")
    nota: Optional[str] = Field(default=None, max_length=300)


@router.post("/interop/recibidas/{remision_id}/responder")
def responder_remision(remision_id: int, body: ResponderIn, db=Depends(get_db),
                       usuario=Depends(requiere_rol("admin", "especialista"))):
    """El especialista acepta o rechaza; el sistema le responde al socio con un Task
    y el médico remitente recibe el aviso en vivo en SU sistema."""
    if body.decision == "rechazar" and not (body.nota or "").strip():
        raise HTTPException(status_code=422, detail="Escribe el motivo del rechazo")
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM remisiones WHERE id = %s AND origen_externo AND activo FOR UPDATE;", (remision_id,))
    rem = cur.fetchone()
    if rem is None:
        cur.close()
        raise HTTPException(status_code=404, detail="Remisión recibida no encontrada")
    nuevo = "aceptada" if body.decision == "aceptar" else "rechazada"
    if rem["estado"] not in ("pendiente", "en_gestion") and (
        rem["estado"] != nuevo or rem["estado_interop"] == "respondida"
    ):
        cur.close()
        raise HTTPException(status_code=409, detail=f"Esta remisión ya fue respondida ({rem['estado']})")
    socio = _socio(cur, socio_id=rem["socio_id"])
    if rem["estado"] != nuevo:
        _guardar_historial(cur, remision_id, usuario["id"])
    cur.execute("""UPDATE remisiones SET estado = %s,
                          institucion_acepta = CASE WHEN %s = 'aceptada' THEN %s ELSE institucion_acepta END,
                          cerrada_en = CASE WHEN %s = 'rechazada' THEN now() ELSE cerrada_en END
                   WHERE id = %s;""", (nuevo, nuevo, INSTITUCION, nuevo, remision_id))
    registrar_auditoria(db, usuario["id"], f"interop_remision_{nuevo}", "remisiones", remision_id,
                        f"origen=externo socio={socio['nombre']} referencia={rem['referencia_externa']}")
    task = {"resourceType": "Task", "status": "accepted" if nuevo == "aceptada" else "rejected", "intent": "order",
            "focus": {"identifier": {"system": SID_REMISION, "value": rem["referencia_externa"]}},
            "owner": {"display": INSTITUCION}, "lastModified": _ahora(),
            **({"statusReason": {"text": body.nota.strip()}} if (body.nota or "").strip() else {})}
    try:
        codigo, mensaje_id, texto = _enviar(socio, "Task", task)
    except HTTPException as e:
        codigo, mensaje_id, texto = 0, str(uuid.uuid4()), str(e.detail)
    ok = codigo in (200, 201, 202)
    _registrar_mensaje(cur, socio["id"], "saliente", mensaje_id, "Task", "enviado" if ok else "error", codigo or None,
                       remision_id, rem["paciente_id"], rem["referencia_externa"], detalle=texto,
                       recurso={"status": task["status"], "nota": body.nota})
    if ok:
        cur.execute("UPDATE remisiones SET estado_interop = 'respondida' WHERE id = %s;", (remision_id,))
    cur.close()
    registrar_auditoria(db, usuario["id"], "interop_respuesta_enviada" if ok else "interop_respuesta_fallida",
                        "remisiones", remision_id,
                        f"origen=externo socio={socio['nombre']} mensaje={mensaje_id} http={codigo} estado={nuevo}")
    return {"estado": nuevo, "respuesta_enviada": ok, "codigo": codigo,
            "detalle": texto if not ok else f"{socio['nombre']} recibió la respuesta"}


# ══════════════════════════════════════════════════════════════════════════
# 8. R32 · Consentimiento del paciente
# ══════════════════════════════════════════════════════════════════════════
class ConsentimientoIn(BaseModel):
    socio_id: int
    medio: str = Field(default="escrito", pattern="^(escrito|verbal|digital)$")
    finalidad: str = Field(default="Remisión y continuidad de la atención", min_length=5, max_length=200)


@router.get("/pacientes/{paciente_id}/consentimientos", dependencies=[Depends(requiere_rol(*ROLES_CLINICOS))])
def listar_consentimientos(paciente_id: int, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT c.id, c.socio_id, s.nombre AS institucion, c.finalidad, c.medio, c.vigente,
                          c.otorgado_en AT TIME ZONE current_setting('TimeZone') AS otorgado_en,
                          c.revocado_en AT TIME ZONE current_setting('TimeZone') AS revocado_en,
                          u.nombre AS registrado_por
                   FROM consentimientos c JOIN sistemas_socios s ON s.id = c.socio_id
                   LEFT JOIN usuarios u ON u.id = c.registrado_por
                   WHERE c.paciente_id = %s ORDER BY c.id DESC;""", (paciente_id,))
    filas = cur.fetchall()
    cur.close()
    return filas


@router.post("/pacientes/{paciente_id}/consentimientos", status_code=201)
def registrar_consentimiento(paciente_id: int, body: ConsentimientoIn, db=Depends(get_db),
                             usuario=Depends(requiere_rol("admin", "medico"))):
    """Deja constancia de que el paciente autorizó compartir sus datos con UNA
    institución y para UNA finalidad (Ley 1581/2012, datos sensibles)."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT 1 FROM pacientes WHERE id = %s;", (paciente_id,))
    if cur.fetchone() is None:
        cur.close()
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    socio = _socio(cur, socio_id=body.socio_id)
    if socio is None:
        cur.close()
        raise HTTPException(status_code=404, detail="Institución socia no encontrada")
    existente = _consentimiento_vigente(cur, paciente_id, body.socio_id)
    if existente:
        cur.close()
        return {"id": existente["id"], "vigente": True, "institucion": socio["nombre"], "ya_existia": True}
    cur.execute("""INSERT INTO consentimientos (paciente_id, socio_id, finalidad, medio, registrado_por)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id;""",
                (paciente_id, body.socio_id, body.finalidad, body.medio, usuario["id"]))
    cid = cur.fetchone()["id"]
    cur.close()
    registrar_auditoria(db, usuario["id"], "consentimiento_registrado", "pacientes", paciente_id,
                        f"consentimiento={cid} institucion={socio['nombre']} medio={body.medio}")
    return {"id": cid, "vigente": True, "institucion": socio["nombre"], "ya_existia": False}


@router.post("/consentimientos/{consentimiento_id}/revocar")
def revocar_consentimiento(consentimiento_id: int, db=Depends(get_db),
                           usuario=Depends(requiere_rol("admin", "medico"))):
    """El titular puede retirar la autorización en cualquier momento: desde ahí no sale nada más."""
    cur = db.cursor()
    cur.execute("""UPDATE consentimientos SET vigente = FALSE, revocado_en = now(), revocado_por = %s
                   WHERE id = %s AND vigente RETURNING paciente_id;""", (usuario["id"], consentimiento_id))
    fila = cur.fetchone()
    cur.close()
    if fila is None:
        raise HTTPException(status_code=404, detail="Consentimiento no encontrado o ya revocado")
    registrar_auditoria(db, usuario["id"], "consentimiento_revocado", "pacientes", fila[0],
                        f"consentimiento={consentimiento_id}")
    return {"revocado": True}


# ══════════════════════════════════════════════════════════════════════════
# 9. Administración de socios (solo admin) y bandejas
# ══════════════════════════════════════════════════════════════════════════
class SocioIn(BaseModel):
    nombre: str = Field(min_length=3, max_length=120)
    url_base: Optional[str] = Field(default=None, max_length=300)


class SocioCambios(BaseModel):
    nombre: Optional[str] = Field(default=None, min_length=3, max_length=120)
    url_base: Optional[str] = Field(default=None, max_length=300)
    cliente_saliente: Optional[str] = Field(default=None, max_length=120)
    secreto_saliente: Optional[str] = Field(default=None, max_length=300)
    firma_saliente: Optional[str] = Field(default=None, max_length=300)
    activo: Optional[bool] = None


def _url_valida(url: Optional[str], *, bucle: bool = False) -> Optional[str]:
    if not url:
        return None
    url = url.strip().rstrip("/")
    if bucle and re.fullmatch(r"http://(?:127\.0\.0\.1|\[::1\])(?::[0-9]+)?(?:/[\w.\-/]*)?", url):
        return url
    if not re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]+)?(?:/[\w.\-/]*)?", url):
        raise HTTPException(status_code=422, detail="La URL del socio debe usar HTTPS y no incluir credenciales ni parámetros")
    return url


def _publico(s: dict, completo: bool) -> dict:
    base = {"id": s["id"], "nombre": s["nombre"], "activo": s["activo"], "de_prueba": s["de_prueba"],
            "listo_para_enviar": bool(s["url_base"] and s["cliente_saliente"] and s["secreto_saliente"] and s["firma_saliente"])}
    if completo:
        base.update({"client_id": s["client_id"], "scopes": s["scopes"].split(), "url_base": s["url_base"],
                     "cliente_saliente": s["cliente_saliente"], "tiene_secreto_saliente": bool(s["secreto_saliente"]),
                     "tiene_firma_saliente": bool(s["firma_saliente"]), "creado_en": s["creado_en"]})
    return base


@router.get("/interop/socios")
def listar_socios(db=Depends(get_db), usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    """El admin ve la configuración (nunca los secretos); médico y especialista, solo
    el nombre para elegir a quién remitir."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM sistemas_socios ORDER BY id;")
    filas = cur.fetchall()
    cur.close()
    return [_publico(s, usuario["rol"] == "admin") for s in filas if usuario["rol"] == "admin" or s["activo"]]


def _crear_socio(cur, nombre: str, url_base: Optional[str], admin_id: int, de_prueba: bool = False) -> dict:
    base = re.sub(r"[^a-z0-9]+", "-", cripto.sin_tildes(nombre).lower()).strip("-")[:24] or "socio"
    client_id = f"sis-{base}-{uuid.uuid4().hex[:6]}"
    client_secret, secreto_firma = cripto.secreto_nuevo(), cripto.secreto_nuevo()
    cur.execute("""INSERT INTO sistemas_socios (nombre, client_id, client_secret_hash, secreto_firma, url_base,
                                                creado_por, de_prueba)
                   VALUES (registrar_institucion(%s), %s, %s, %s, %s, %s, %s) RETURNING *;""",
                (nombre.strip(), client_id, auth.hash_password(client_secret),
                 _guardar_secreto(client_id, "secreto_firma", secreto_firma), _url_valida(url_base, bucle=de_prueba), admin_id, de_prueba))
    return {"socio": cur.fetchone(), "client_secret": client_secret, "secreto_firma": secreto_firma}


def _entrega(creado: dict) -> dict:
    """Lo que se le entrega EN PRIVADO al otro equipo. Los secretos se muestran una sola vez."""
    s = creado["socio"]
    return {**_publico(s, True),
            "credenciales_para_entregar": {
                "client_id": s["client_id"], "client_secret": creado["client_secret"],
                "secreto_firma": creado["secreto_firma"], "scopes": s["scopes"].split(),
                "token_url": "<URL pública de este sistema>/api/oauth/token",
                "entrada": "<URL pública de este sistema>/api/interop/{Bundle|Task|Observation}"},
            "aviso": "Copia estos secretos ahora y entrégalos por un canal privado: no se vuelven a mostrar."}


@router.post("/interop/socios", status_code=201)
def crear_socio(body: SocioIn, db=Depends(get_db), admin=Depends(requiere_rol("admin"))):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    creado = _crear_socio(cur, body.nombre, body.url_base, admin["id"])
    cur.close()
    registrar_auditoria(db, admin["id"], "interop_socio_creado", "sistemas_socios", creado["socio"]["id"],
                        f"socio={body.nombre} client_id={creado['socio']['client_id']}")
    return _entrega(creado)


@router.post("/interop/socios/prueba", status_code=201)
def crear_socio_de_prueba(db=Depends(get_db), admin=Depends(requiere_rol("admin"))):
    """Para ensayar sin el otro equipo: registra una institución socia que es ESTE mismo
    sistema (bucle local). La remisión sale, entra por /interop/Bundle con token y firma
    reales, el especialista la responde y la respuesta vuelve por /interop/Task."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM sistemas_socios WHERE de_prueba ORDER BY id LIMIT 1;")
    existente = cur.fetchone()
    if existente:
        cur.close()
        return {**_publico(existente, True), "ya_existia": True}
    creado = _crear_socio(cur, "Clínica Socia de Prueba (bucle local)", URL_LOCAL, admin["id"], de_prueba=True)
    s = creado["socio"]
    cur.execute("""UPDATE sistemas_socios SET cliente_saliente = %s, secreto_saliente = %s, firma_saliente = %s
                   WHERE id = %s RETURNING *;""",
                (s["client_id"], _guardar_secreto(s["client_id"], "secreto_saliente", creado["client_secret"]),
                 _guardar_secreto(s["client_id"], "firma_saliente", creado["secreto_firma"]), s["id"]))
    s = cur.fetchone()
    cur.close()
    registrar_auditoria(db, admin["id"], "interop_socio_creado", "sistemas_socios", s["id"], "socio de prueba (bucle local)")
    return {**_publico(s, True), "ya_existia": False}


@router.put("/interop/socios/{socio_id}")
def actualizar_socio(socio_id: int, body: SocioCambios, db=Depends(get_db), admin=Depends(requiere_rol("admin"))):
    """Registra dónde vive el socio y las credenciales que ÉL nos entregó (se guardan cifradas)."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    s = _socio(cur, socio_id=socio_id)
    if s is None:
        cur.close()
        raise HTTPException(status_code=404, detail="Socio no encontrado")
    cambios, valores = [], []
    datos = body.model_dump(exclude_unset=True)
    for campo in ("nombre", "cliente_saliente", "activo"):
        if campo in datos and datos[campo] is not None:
            cambios.append("nombre = registrar_institucion(%s)" if campo == "nombre" else f"{campo} = %s")
            valores.append(datos[campo].strip() if isinstance(datos[campo], str) else datos[campo])
    if "url_base" in datos:
        cambios.append("url_base = %s"); valores.append(_url_valida(datos["url_base"], bucle=bool(s["de_prueba"])))
    for campo in ("secreto_saliente", "firma_saliente"):
        if datos.get(campo):
            cambios.append(f"{campo} = %s"); valores.append(_guardar_secreto(s["client_id"], campo, datos[campo].strip()))
    if cambios:
        cur.execute(f"UPDATE sistemas_socios SET {', '.join(cambios)} WHERE id = %s RETURNING *;", (*valores, socio_id))
        s = cur.fetchone()
        _tokens.clear()
    cur.close()
    registrar_auditoria(db, admin["id"], "interop_socio_actualizado", "sistemas_socios", socio_id,
                        f"campos={sorted(datos)}")
    return _publico(s, True)


@router.post("/interop/socios/{socio_id}/rotar")
def rotar_credenciales(socio_id: int, db=Depends(get_db), admin=Depends(requiere_rol("admin"))):
    """Si un secreto se filtró, borrarlo no basta: se generan otros y los viejos dejan de servir."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    s = _socio(cur, socio_id=socio_id)
    if s is None:
        cur.close()
        raise HTTPException(status_code=404, detail="Socio no encontrado")
    client_secret, secreto_firma = cripto.secreto_nuevo(), cripto.secreto_nuevo()
    cur.execute("UPDATE sistemas_socios SET client_secret_hash = %s, secreto_firma = %s WHERE id = %s RETURNING *;",
                (auth.hash_password(client_secret), _guardar_secreto(s["client_id"], "secreto_firma", secreto_firma), socio_id))
    s = cur.fetchone()
    if s["de_prueba"]:                      # el socio de prueba se habla a sí mismo: se actualizan los dos lados
        cur.execute("UPDATE sistemas_socios SET secreto_saliente = %s, firma_saliente = %s WHERE id = %s RETURNING *;",
                    (_guardar_secreto(s["client_id"], "secreto_saliente", client_secret),
                     _guardar_secreto(s["client_id"], "firma_saliente", secreto_firma), socio_id))
        s = cur.fetchone()
    cur.close()
    _tokens.clear()
    registrar_auditoria(db, admin["id"], "interop_credenciales_rotadas", "sistemas_socios", socio_id, f"socio={s['nombre']}")
    return _entrega({"socio": s, "client_secret": client_secret, "secreto_firma": secreto_firma})


@router.post("/interop/socios/{socio_id}/probar")
def probar_socio(socio_id: int, db=Depends(get_db), admin=Depends(requiere_rol("admin"))):
    """¿Las credenciales que nos dio el socio funcionan? Pide un token y dice qué pasó."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    s = _socio(cur, socio_id=socio_id)
    cur.close()
    if s is None:
        raise HTTPException(status_code=404, detail="Socio no encontrado")
    _tokens.pop((s["id"], SCOPES[0]), None)
    try:
        _token_saliente(s, SCOPES[0])
        return {"ok": True, "detalle": f"{s['nombre']} entregó un token de sistema: la conexión funciona."}
    except HTTPException as e:
        return {"ok": False, "detalle": e.detail}


@router.get("/interop/mensajes", dependencies=[Depends(requiere_rol("admin"))])
def listar_mensajes(limite: int = Query(100, ge=1, le=500), db=Depends(get_db)):
    """Bitácora técnica de todo lo que entró y salió (solo admin)."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT m.id, s.nombre AS socio, m.direccion, m.tipo, m.estado, m.codigo_http, m.remision_id,
                          m.referencia_externa, m.detalle, split_part(m.mensaje_id, '#', 1) AS mensaje_id,
                          m.creado_en AT TIME ZONE current_setting('TimeZone') AS creado_en
                   FROM interop_mensajes m LEFT JOIN sistemas_socios s ON s.id = m.socio_id
                   ORDER BY m.id DESC LIMIT %s;""", (limite,))
    filas = cur.fetchall()
    cur.close()
    return filas


@router.get("/interop/recibidas")
def remisiones_recibidas(db=Depends(get_db), usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    """Bandeja del especialista: remisiones que llegaron de otra institución."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT r.id, r.paciente_id, p.nombre AS paciente_nombre, p.por_conciliar, r.institucion_origen,
                          r.motivo, r.estado, r.estado_interop, r.referencia_externa,
                          r.radicada_en AT TIME ZONE current_setting('TimeZone') AS recibida_en,
                          m.recurso, m.codigo_http
                   FROM remisiones r JOIN pacientes p ON p.id = r.paciente_id
                   LEFT JOIN LATERAL (SELECT recurso, codigo_http FROM interop_mensajes
                                      WHERE remision_id = r.id AND direccion = 'entrante' AND tipo = 'Bundle'
                                      ORDER BY id LIMIT 1) m ON TRUE
                   WHERE r.origen_externo AND r.activo ORDER BY r.id DESC LIMIT 200;""")
    filas = cur.fetchall()
    cur.close()
    return filas


@router.get("/interop/enviadas")
def remisiones_enviadas(db=Depends(get_db), usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    """Remisiones que este sistema envió a una institución socia y en qué van."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT r.id, r.paciente_id, p.nombre AS paciente_nombre, s.nombre AS socio, r.motivo, r.estado,
                          r.estado_interop, m.codigo_http, m.detalle,
                          m.creado_en AT TIME ZONE current_setting('TimeZone') AS enviada_en,
                          t.detalle AS nota_respuesta,
                          t.creado_en AT TIME ZONE current_setting('TimeZone') AS respondida_en
                   FROM remisiones r JOIN pacientes p ON p.id = r.paciente_id
                   JOIN sistemas_socios s ON s.id = r.socio_id
                   LEFT JOIN LATERAL (SELECT codigo_http, detalle, creado_en FROM interop_mensajes
                                      WHERE remision_id = r.id AND direccion = 'saliente' AND tipo = 'Bundle'
                                      ORDER BY id DESC LIMIT 1) m ON TRUE
                   LEFT JOIN LATERAL (SELECT detalle, creado_en FROM interop_mensajes
                                      WHERE remision_id = r.id AND direccion = 'entrante' AND tipo = 'Task'
                                      ORDER BY id DESC LIMIT 1) t ON TRUE
                   WHERE NOT r.origen_externo AND r.estado_interop IS NOT NULL AND r.activo
                   ORDER BY r.id DESC LIMIT 200;""")
    filas = cur.fetchall()
    cur.close()
    return filas


@router.get("/interop/perfil")
def perfil():
    """El contrato, en JSON, para el equipo socio (no requiere token: no tiene datos)."""
    return {
        "institucion": INSTITUCION, "fhir": "R4",
        "token": {"ruta": "/oauth/token", "grant_type": "client_credentials", "vigencia_s": MINUTOS_TOKEN_SISTEMA * 60,
                  "scopes": list(SCOPES)},
        "entradas": {"/interop/Bundle": SCOPES[0], "/interop/Task": SCOPES[1], "/interop/Observation": SCOPES[2]},
        "firma": {"cabeceras": ["Authorization: Bearer <token>", "X-Timestamp: <epoch UTC en segundos>",
                                "X-Message-Id: <uuid único por mensaje>", "X-Signature: v1=<hex>"],
                  "calculo": 'HMAC-SHA256(secreto_firma, X-Timestamp + "." + X-Message-Id + "." + cuerpo exacto)',
                  "tolerancia_s": TOLERANCIA_S},
        "identificador_federado": {"system": SID_FEDERADO,
                                   "calculo": 'HMAC-SHA256(secreto federado del acuerdo, "CC|" + documento sin puntos)'},
        "bundle": {"type": "collection", "obligatorios": ["Patient", "ServiceRequest"],
                   "opcionales": ["Observation", "DiagnosticReport (status final)", "Consent (status active)"],
                   "ServiceRequest.code": f"SNOMED CT ({SNOMED})", "ServiceRequest.identifier": SID_REMISION},
        "loinc_aceptados": {k: {"nombre": v[0], "ucum": v[1]} for k, v in CATALOGO_LOINC.items()},
        "unidades": UCUM,
        "respuestas": {"201": "aceptado y asociado a un paciente", "202": "aceptado; falta conciliar al paciente",
                       "400": "no es JSON o no es el recurso esperado", "401": "sin token, token vencido o firma inválida",
                       "403": "token válido, sin el permiso", "409": "mensaje repetido",
                       "422": "no cumple el perfil (OperationOutcome)", "429": "demasiadas peticiones"},
    }
