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
from datetime import datetime, timezone
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
import reactividad
from eventos import ids_con_rol, registrar_evento
import seguridad
from interop_perfil import (
    CATALOGO_LOINC, CLASE_REMISION, LOINC, SNOMED, SNOMED_REMISION, UCUM,
    SID_ENCUENTRO, SID_FEDERADO, SID_OBSERVACION, SID_PROFESIONAL, SID_REMISION, Rechazo,
    _identificador, _json, _lista, _obj, _validar_bundle, _validar_observation,
)
from validaciones import bloquear_remision
from validacion_clinica import SIGNOS, validar_signo
from auth import (PG_CONNECTION_STRING, ROLES_CLINICOS, get_db, get_usuario_actual, registrar_auditoria,
                  requiere_rol)

router = APIRouter(tags=["Interoperabilidad entre sistemas"])

INSTITUCION = (os.getenv("INSTITUCION_NOMBRE") or "Hospital Universitario del Valle")
# Secreto con el que las instituciones del acuerdo calculan el identificador federado.
# El valor por defecto es el de la demo de clase: se cambia en el .env de ambos equipos.
SECRETO_FEDERADO = (os.getenv("SECRETO_FEDERADO") or "secreto-federado-del-curso-solo-demo")
URL_LOCAL = (os.getenv("INTEROP_URL_LOCAL") or "http://127.0.0.1:8000")     # para el socio de prueba (bucle local)
MINUTOS_TOKEN_SISTEMA = 10
TOLERANCIA_S = int(os.getenv("INTEROP_TOLERANCIA_S") or "300")             # reloj del emisor: ±5 min
SCOPES = ("system/Bundle.write", "system/Task.write", "system/Observation.write")
SCOPE_DE = {"Bundle": SCOPES[0], "Task": SCOPES[1], "Observation": SCOPES[2]}


# ── Respuestas FHIR ───────────────────────────────────────────────────────
def operation_outcome(status: int, codigo: str, textos, severidad: str = "error") -> JSONResponse:
    textos = textos if isinstance(textos, list) else [textos]
    return JSONResponse(status_code=status, media_type="application/fhir+json", content={
        "resourceType": "OperationOutcome",
        "issue": [{"severity": severidad, "code": codigo, "diagnostics": t} for t in textos]})


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


def paciente_por_federado(cur, federado):
    cur.execute("SELECT id, documento FROM pacientes WHERE NOT por_conciliar AND conciliado_con IS NULL ORDER BY id;")
    for pid, doc in cur:
        try:
            calculado = federado_de_paciente(pid, cripto.descifrar(doc, cripto.aad_paciente(pid, "documento")))
        except ValueError:
            continue
        if cripto.iguales(calculado, federado):
            return pid
    return None


def _bloquear_federado(cur, federado):
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0));", ("interop:" + federado,))


def _resolver_paciente(cur, federado):
    _bloquear_federado(cur, federado)
    paciente_id = paciente_por_federado(cur, federado)
    if paciente_id:
        return paciente_id, True
    cur.execute("""SELECT id FROM pacientes WHERE por_conciliar AND id_federado = %s
                   AND conciliado_con IS NULL ORDER BY id LIMIT 1;""", (federado,))
    fila = cur.fetchone()
    if fila:
        return fila[0], False
    cur.execute("""INSERT INTO pacientes (nombre, documento, genero, eps, id_federado, por_conciliar)
                   VALUES (%s, '', 'O', 'Por conciliar', %s, TRUE) RETURNING id;""",
                (f"Paciente por conciliar {federado[:8]}", federado))
    paciente_id = cur.fetchone()[0]
    cripto.cifrar_fila_paciente(cur, paciente_id, {"documento": f"PC-{federado}"})
    return paciente_id, False


def _insertar_observacion(cur, obs, socio_id, paciente_id, remision_id):
    cur.execute("""INSERT INTO observaciones (remision_id, paciente_id, tipo, codigo_loinc, valor, unidad,
                       fecha_observacion, socio_id, profesional_fhir, identificador_fhir)
                   VALUES (%s, %s, %s, %s, %s, %s,
                           COALESCE(%s::timestamptz, now()) AT TIME ZONE current_setting('TimeZone'), %s, %s, %s) RETURNING id;""",
                (remision_id, None if remision_id is not None else paciente_id, obs["nombre"], obs["loinc"],
                 obs["valor"], obs["unidad"], obs["fecha"], socio_id, obs["profesional"], obs["identificador"]))
    fila = cur.fetchone()
    return fila["id"] if isinstance(fila, dict) else fila[0]


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
        paciente_id, conciliado = _resolver_paciente(cur, partes["federado"])
        referencia = partes["referencia"]
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0));",
                    (f"interop:remision:{socio['id']}:{referencia}",))
        cur.execute("""SELECT 1 FROM remisiones WHERE origen_externo AND socio_id = %s
                       AND referencia_externa = %s;""", (socio["id"], referencia))
        if cur.fetchone():
            raise Rechazo(409, "duplicate", "El socio ya registró esta solicitud")
        cur.execute("""INSERT INTO remisiones (paciente_id, institucion_origen, institucion_destino, fecha_solicitud,
                                               motivo, estado, convenio_vigente, origen_externo, socio_id,
                                               referencia_externa, estado_interop, encuentro_fhir, profesional_fhir)
                       VALUES (%s, registrar_institucion(%s), registrar_institucion(%s),
                               (now() AT TIME ZONE 'America/Bogota')::date, %s, 'pendiente', FALSE, TRUE, %s, %s, 'recibida', %s, %s)
                       RETURNING id;""",
                    (paciente_id, socio["nombre"], INSTITUCION,
                     f"[{PRIORIDAD_TXT[partes['prioridad']]}] {motivo}", socio["id"], partes["referencia"], partes["encuentro"], partes["profesional"]))
        remision_id = cur.fetchone()[0]
        for observacion in partes["observaciones"]:
            _insertar_observacion(cur, observacion, socio["id"], paciente_id, remision_id)
        resumen = {"referencia": partes["referencia"], "prioridad": partes["prioridad"], "motivo": motivo,
                   "observaciones": [{k: o[k] for k in ("loinc", "nombre", "valor", "unidad")} for o in partes["observaciones"]],
                   "reporte_ia": partes["reporte"], "consentimiento_declarado": partes["con_consent"],
                   "paciente_federado": partes["federado"][:12] + "…"}
        codigo = 201 if conciliado else 202
        _registrar_mensaje(cur, socio["id"], "entrante", mensaje_id, "Bundle",
                           "aceptado" if conciliado else "por_conciliar", codigo, remision_id, paciente_id,
                           partes["referencia"], recurso=resumen)
        cur.close()

        registrar_evento(conn, ids_con_rol(conn, "especialista", "admin"), {
            "tipo": "remision_externa_recibida", "remision_id": remision_id, "paciente_id": paciente_id,
            "origen": "externo", "socio": socio["nombre"],
            "mensaje": f"Remisión recibida de {socio['nombre']}: {motivo[:90]}"
                       + ("" if conciliado else " (paciente por conciliar)")})
        registrar_auditoria(conn, None, "interop_remision_recibida", "remisiones", remision_id,
                            f"origen=externo socio={socio['nombre']} mensaje={mensaje_id} http={codigo} "
                            f"referencia={partes['referencia']} observaciones={len(partes['observaciones'])} "
                            f"reporte_ia={'sí' if partes['reporte'] else 'no'} ip={ip}")
        if habia_orden:
            seguridad.registrar_inyeccion_indirecta(None, f"interop:{socio['nombre']}",
                                                    f"orden escondida en el motivo de la remisión {remision_id}")
        return JSONResponse(status_code=codigo, media_type="application/fhir+json", content={
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
        referencia = _identificador(t.get("focus"), SID_REMISION)
        if not referencia:
            errores.append("Task.focus.identifier.value debe traer el número de la remisión que se responde")
        if errores:
            raise Rechazo(422, "invalid", errores)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT r.id, r.paciente_id, r.creado_por, r.estado, r.activo FROM interop_mensajes m
                       JOIN remisiones r ON r.id = m.remision_id
                       WHERE m.socio_id = %s AND m.direccion = 'saliente' AND m.tipo = 'Bundle'
                         AND m.estado = 'enviado' AND m.referencia_externa = %s
                         AND r.socio_id = m.socio_id ORDER BY m.id DESC LIMIT 1 FOR UPDATE OF r;""", (socio["id"], referencia))
        rem = cur.fetchone()
        if rem is None:
            raise Rechazo(422, "not-found", f"No hay una remisión enviada a {socio['nombre']} con el número {referencia}")
        nuevo = ESTADO_DE_TASK[t["status"]]
        if not rem["activo"] or (rem["estado"] != nuevo and rem["estado"] not in ("pendiente", "en_gestion")):
            raise Rechazo(409, "conflict", "La remisión ya no admite cambios de estado del socio")
        notas = _lista(t.get("note"))
        nota, _ = seguridad.neutralizar(str(_obj(t.get("statusReason")).get("text")
                                            or (_obj(notas[0]).get("text") if notas else "") or "")[:300])
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
        verbo = {"aceptada": "ACEPTÓ", "rechazada": "RECHAZÓ", "en_gestion": "está gestionando"}[nuevo]
        registrar_evento(conn, [*ids_con_rol(conn, "admin"), rem["creado_por"]], {"tipo": "respuesta_socio", "remision_id": rem["id"], "paciente_id": rem["paciente_id"],
                             "origen": "externo", "socio": socio["nombre"], "estado": nuevo,
                             "mensaje": f"{socio['nombre']} {verbo} la remisión {referencia}" + (f": {nota[:80]}" if nota else "")})
        registrar_auditoria(conn, None, "interop_respuesta_recibida", "remisiones", rem["id"],
                            f"origen=externo socio={socio['nombre']} mensaje={mensaje_id} estado={nuevo} ip={ip}")
        return JSONResponse(status_code=201, media_type="application/fhir+json", content={
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
def _episodio_observacion(cur, socio_id, obs, paciente_id):
    if not obs["referencia"]:
        if obs["encuentro"]:
            raise Rechazo(422, "invalid", "Para asociar un encuentro se requiere basedOn")
        return None
    cur.execute("""SELECT r.id, r.paciente_id, r.creado_por, r.atendida_por, r.encuentro_fhir,
                          r.origen_externo
                   FROM remisiones r
                   WHERE r.activo AND r.socio_id = %s AND (
                       (r.origen_externo AND r.referencia_externa = %s) OR
                       (NOT r.origen_externo AND EXISTS (
                           SELECT 1 FROM interop_mensajes m WHERE m.remision_id = r.id
                           AND m.socio_id = r.socio_id AND m.tipo = 'Bundle' AND m.direccion = 'saliente'
                           AND m.estado = 'enviado' AND m.referencia_externa = %s)))
                   FOR UPDATE;""", (socio_id, obs["referencia"], obs["referencia"]))
    episodios = cur.fetchall()
    # El socio de simulación utiliza las mismas tablas en ambos extremos. Si están
    # las dos copias, el mensaje de retorno corresponde a la que se envió al socio.
    salientes = [e for e in episodios if not e["origen_externo"]]
    if salientes:
        episodios = salientes
    if len(episodios) != 1 or episodios[0]["paciente_id"] != paciente_id:
        raise Rechazo(422, "invalid", "La solicitud no corresponde al paciente y socio de este mensaje")
    episodio = episodios[0]
    esperado = episodio["encuentro_fhir"] if episodio["origen_externo"] else obs["referencia"]
    if obs["encuentro"] and obs["encuentro"] != esperado:
        raise Rechazo(422, "invalid", "La observación no corresponde al encuentro de esa solicitud")
    return episodio


def _recibir_observation(cabeceras: dict, cuerpo: bytes, ip: str):
    conn = _conexion()
    socio, mensaje_id = None, cabeceras.get("x-message-id") or "sin-id"
    try:
        socio, mensaje_id = _autenticar(conn, cabeceras, cuerpo, "Observation", ip)
        errores, obs = _validar_observation(_json(cuerpo, "Observation"), "Observation", exigir_sujeto=True)
        if errores:
            raise Rechazo(422, "invalid", errores)
        with conn.cursor() as cur:
            paciente_id, conciliado = _resolver_paciente(cur, obs["federado"])
        codigo = 201 if conciliado else 202
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            episodio = _episodio_observacion(cur, socio["id"], obs, paciente_id)
            remision_id = episodio["id"] if episodio else None
            observacion_id = _insertar_observacion(cur, obs, socio["id"], paciente_id, remision_id)
            _registrar_mensaje(cur, socio["id"], "entrante", mensaje_id, "Observation",
                               "aceptado" if conciliado else "por_conciliar", codigo, remision_id, paciente_id,
                               referencia=obs["referencia"],
                               recurso={k: obs[k] for k in ("loinc", "nombre", "valor", "unidad", "identificador")})
        if conciliado:
            destino = episodio or reactividad.episodio_activo(conn, paciente_id)
            reactividad.registrar_observaciones(conn, destino, paciente_id, [{
                "id": observacion_id if episodio else None, "tipo": obs["nombre"],
                "codigo_loinc": obs["loinc"], "valor": obs["valor"], "unidad": obs["unidad"],
            }], tipo="interop_resultado", origen="externo", socio=socio["nombre"],
                valor_glucosa=obs["valor"] if obs["loinc"] == "2339-0" else None, unidad=obs["unidad"])
        else:
            registrar_evento(conn, ids_con_rol(conn, "admin"), {
                "tipo": "interop_resultado", "origen": "externo",
                "mensaje": "Llegó una observación FHIR para un paciente por conciliar.",
            })
        registrar_auditoria(conn, None, "interop_observacion_recibida", "pacientes", paciente_id,
                            f"origen=externo socio={socio['nombre']} mensaje={mensaje_id} http={codigo} "
                            f"loinc={obs['loinc']} remision_id={remision_id} ip={ip}")
        return JSONResponse(status_code=codigo, media_type="application/fhir+json", content={
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "information", "code": "informational",
                       "diagnostics": "Observación aceptada y asociada a un paciente" if conciliado
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


def _referencia_logica(tipo, sistema, valor):
    return {"type": tipo, "identifier": {"system": sistema, "value": valor}}


def _profesional(profesional_id):
    return _referencia_logica("Practitioner", SID_PROFESIONAL,
                              cripto.id_federado(f"{INSTITUCION}|{profesional_id}", SECRETO_FEDERADO, "PR"))


def _fecha_fhir(instante):
    from zoneinfo import ZoneInfo
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=ZoneInfo("America/Bogota"))
    return instante.isoformat(timespec="seconds")


def _sin_identificadores(texto, paciente):
    for campo in ("nombre", "documento", "telefono", "correo", "direccion", "fecha_nacimiento"):
        valor = str(paciente.get(campo) or "").strip()
        if valor:
            texto = re.sub(r"(?<!\w)" + re.escape(valor) + r"(?!\w)", "[dato reservado]", texto, flags=re.I)
            if campo == "documento" and valor.isdecimal() and len(valor) >= 5:
                variante = r"[. -]?".join(valor)
                texto = re.sub(r"(?<!\d)" + variante + r"(?!\d)", "[dato reservado]", texto)
    return texto


def _recurso_observacion(o, sujeto, referencia, encuentro):
    codigo = o["codigo_loinc"]
    nombre, ucum = CATALOGO_LOINC[codigo]
    recurso = {
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": LOINC, "code": codigo, "display": nombre}]},
        "subject": sujeto, "effectiveDateTime": _fecha_fhir(o["fecha_observacion"]),
        "basedOn": [_referencia_logica("ServiceRequest", SID_REMISION, referencia)],
        "valueQuantity": {"value": float(o["valor"]), "unit": ucum, "system": UCUM, "code": ucum},
    }
    if encuentro:
        recurso["encounter"] = _referencia_logica("Encounter", SID_ENCUENTRO, encuentro)
    if o.get("profesional_id"):
        recurso["performer"] = [_profesional(o["profesional_id"])]
    return recurso


def armar_bundle(cur, remision: dict, paciente: dict, socio: dict, consentimiento: dict) -> dict:
    """Patient (identificador federado, sin documento ni teléfono ni dirección),
    ServiceRequest (SNOMED), Observation (LOINC + UCUM), DiagnosticReport SOLO si un
    clínico lo aprobó, y el Consent que autoriza compartir con esa institución."""
    federado = federado_de_paciente(paciente["id"], paciente["documento"])
    ref_pac = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, federado))
    sujeto = {"reference": ref_pac, "identifier": {"system": SID_FEDERADO, "value": federado}}
    radicado = _radicado(remision)
    motivo = re.sub(r"^\[[^\]]+\]\s*", "", remision["motivo"] or "").split(" (propuesta del asistente")[0]
    motivo = _sin_identificadores(motivo, paciente)
    encuentro = _referencia_logica("Encounter", SID_ENCUENTRO, radicado)
    encuentro["reference"] = "urn:uuid:" + str(uuid.uuid4())
    url_solicitud = "urn:uuid:" + str(uuid.uuid4())
    entradas = [{"fullUrl": ref_pac, "resource": {
        "resourceType": "Patient", "identifier": [{"system": SID_FEDERADO, "value": federado}]}}]
    entradas.append({"fullUrl": encuentro["reference"], "resource": {
        "resourceType": "Encounter", "identifier": [encuentro["identifier"]], "status": "planned",
        "class": CLASE_REMISION.copy(),
        "subject": sujeto,
        **({"participant": [{"individual": _profesional(remision["creado_por"])}]}
           if remision.get("creado_por") else {})}})
    entradas.append({"fullUrl": url_solicitud, "resource": {
        "resourceType": "ServiceRequest", "status": "active", "intent": "order",
        "priority": _prioridad_fhir(remision["motivo"]),
        "identifier": [{"system": SID_REMISION, "value": _radicado(remision)}],
        "code": {"coding": [{"system": SNOMED, "code": SNOMED_REMISION[0], "display": SNOMED_REMISION[1]}],
                 "text": "Remisión a otra institución"},
        "subject": sujeto, "authoredOn": _ahora(), "encounter": encuentro,
        "requester": (_profesional(remision["creado_por"]) if remision.get("creado_por") else {"display": INSTITUCION}), "performer": [{"display": socio["nombre"]}],
        "reasonCode": [{"text": motivo[:500]}]}})
    cur.execute("""SELECT DISTINCT ON (codigo_loinc) codigo_loinc, valor, unidad, profesional_id,
                          fecha_observacion AT TIME ZONE current_setting('TimeZone') AS fecha_observacion
                   FROM observaciones WHERE remision_id = %s AND codigo_loinc IS NOT NULL
                   ORDER BY codigo_loinc, fecha_observacion DESC, id DESC;""", (remision["id"],))
    for o in cur.fetchall():
        if o["codigo_loinc"] not in CATALOGO_LOINC:
            continue
        recurso = _recurso_observacion(o, sujeto, radicado, radicado)
        codigo_regla = "59408-5" if o["codigo_loinc"] == "2708-6" else o["codigo_loinc"]
        try:
            if codigo_regla in SIGNOS:
                validar_signo(codigo_regla, float(o["valor"]), o["unidad"])
            elif o["unidad"] != "mg/dL":
                continue
        except ValueError:
            continue
        if not _validar_observation(recurso, "Observation")[0]:
            recurso["basedOn"][0]["reference"] = url_solicitud
            recurso["encounter"] = encuentro
            entradas.append({"resource": recurso})
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
            "issued": _fecha_fhir(rep["revisado_en"] or datetime.now(timezone.utc)),
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
    for entrada in entradas:
        entrada.setdefault("fullUrl", "urn:uuid:" + str(uuid.uuid4()))
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
            "no_viaja": ["nombre", "fecha de nacimiento", "documento", "teléfono", "correo", "dirección", "EPS", "alergias", "tipo de sangre"],
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
    cur.execute("SELECT id, nombre, documento, genero, fecha_nacimiento, telefono, correo, direccion FROM pacientes WHERE id = %s;", (rem["paciente_id"],))
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
    if rem["estado"] not in ("pendiente", "en_gestion") or rem["estado_interop"] is not None:
        raise HTTPException(409, "La remisión ya no está pendiente de envío")
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
    if rem["atendida_por"] and rem["atendida_por"] != usuario["id"] and usuario["rol"] != "admin":
        raise HTTPException(403, "Solo el especialista a cargo puede responder")
    nuevo = "aceptada" if body.decision == "aceptar" else "rechazada"
    if rem["estado"] not in ("pendiente", "en_gestion") and (
        rem["estado"] != nuevo or rem["estado_interop"] == "respondida"
    ):
        cur.close()
        raise HTTPException(status_code=409, detail=f"Esta remisión ya fue respondida ({rem['estado']})")
    socio = _socio(cur, socio_id=rem["socio_id"])
    if rem["estado"] != nuevo:
        _guardar_historial(cur, remision_id, usuario["id"])
    cur.execute("""UPDATE remisiones SET estado = %s, atendida_por = COALESCE(atendida_por, %s),
                          institucion_acepta = CASE WHEN %s = 'aceptada' THEN %s ELSE institucion_acepta END,
                          cerrada_en = CASE WHEN %s = 'rechazada' THEN now() ELSE cerrada_en END
                   WHERE id = %s;""", (nuevo, usuario["id"], nuevo, INSTITUCION, nuevo, remision_id))
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


@router.get("/interop/conciliaciones", dependencies=[Depends(requiere_rol("admin"))])
def pendientes_conciliacion(db=Depends(get_db)):
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT p.id, p.nombre, count(*) FILTER (WHERE m.tipo = 'Bundle') AS remisiones,
                              count(*) FILTER (WHERE m.tipo = 'Observation') AS observaciones
                       FROM pacientes p JOIN interop_mensajes m ON m.paciente_id = p.id
                       WHERE p.por_conciliar AND p.conciliado_con IS NULL AND m.estado = 'por_conciliar'
                       GROUP BY p.id ORDER BY p.id DESC LIMIT 200;""")
        return cur.fetchall()


class ConciliarIn(BaseModel):
    documento: str = Field(min_length=1, max_length=40)


@router.post("/interop/conciliaciones/{provisional_id}")
def conciliar_paciente(provisional_id: int, body: ConciliarIn, db=Depends(get_db),
                      usuario=Depends(requiere_rol("admin"))):
    """Vincula datos clínicos solo si el documento local produce el mismo federado."""
    with db.cursor() as cur:
        cur.execute("SELECT id_federado FROM pacientes WHERE id = %s AND por_conciliar;", (provisional_id,))
        fila = cur.fetchone()
        if not fila or not fila[0]:
            raise HTTPException(404, "Paciente provisional no encontrado")
        federado = fila[0]
        _bloquear_federado(cur, federado)
        cur.execute("""SELECT id, documento FROM pacientes WHERE documento_idx = %s
                       AND NOT por_conciliar AND conciliado_con IS NULL;""", (cripto.indice_ciego(body.documento),))
        destino = cur.fetchone()
        if not destino:
            raise HTTPException(404, "La ficha local debe estar registrada antes de conciliar")
        paciente_id, cifrado = destino
        documento = cripto.descifrar(cifrado, cripto.aad_paciente(paciente_id, "documento"))
        if not cripto.iguales(federado_de_paciente(paciente_id, documento), federado):
            raise HTTPException(422, "El documento no coincide con el identificador federado recibido")
        cur.execute("SELECT conciliado_con FROM pacientes WHERE id = %s FOR UPDATE;", (provisional_id,))
        anterior = cur.fetchone()[0]
        if anterior:
            return {"paciente_id": anterior, "ya_conciliado": True}
        cur.execute("UPDATE pacientes SET conciliado_con = %s WHERE id = %s;", (paciente_id, provisional_id))
        try:
            cur.execute("UPDATE remisiones SET paciente_id = %s WHERE paciente_id = %s RETURNING id;",
                        (paciente_id, provisional_id))
        except psycopg2.errors.CheckViolation as exc:
            raise HTTPException(422, exc.diag.message_primary) from exc
        remisiones = [r[0] for r in cur.fetchall()]
        cur.execute("""UPDATE observaciones SET paciente_id = %s WHERE paciente_id = %s
                       RETURNING id;""", (paciente_id, provisional_id))
        observaciones = [r[0] for r in cur.fetchall()]
        cur.execute("""UPDATE interop_mensajes SET paciente_id = %s, estado = 'conciliado'
                       WHERE paciente_id = %s AND estado = 'por_conciliar';""", (paciente_id, provisional_id))
        # Las observaciones de un episodio ya sincronizado deben cambiar también de sujeto.
        cur.execute("""INSERT INTO fhir_pendientes(tipo, registro_id)
                       SELECT 'Observation', id FROM observaciones WHERE remision_id = ANY(%s)
                       ON CONFLICT (tipo, registro_id) DO UPDATE SET intentos = 0, intentar_en = now();""",
                    (remisiones,))
    registrar_evento(db, ids_con_rol(db, "admin"), {"tipo": "interop_conciliacion", "paciente_id": paciente_id,
                         "mensaje": "Paciente FHIR conciliado y datos vinculados a la ficha local."})
    registrar_auditoria(db, usuario["id"], "interop_paciente_conciliado", "pacientes", paciente_id,
                        f"provisional={provisional_id} remisiones={len(remisiones)} observaciones_sin_episodio={len(observaciones)}")
    return {"paciente_id": paciente_id, "remisiones": remisiones, "observaciones": observaciones}


def _caso_contrarreferencia(db, remision_id, usuario):
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT r.*, p.por_conciliar, p.documento
                       FROM remisiones r JOIN pacientes p ON p.id = r.paciente_id
                       WHERE r.id = %s AND r.activo AND r.origen_externo;""", (remision_id,))
        fila = cur.fetchone()
    if not fila:
        raise HTTPException(404, "Remisión recibida no encontrada")
    if usuario["rol"] != "admin" and fila["atendida_por"] != usuario["id"]:
        raise HTTPException(403, "Solo el especialista a cargo puede enviar resultados")
    if fila["estado"] not in ("aceptada", "completada") or fila["por_conciliar"]:
        raise HTTPException(409, "Primero acepta la remisión y concilia al paciente")
    return fila


@router.get("/interop/recibidas/{remision_id}/resultados")
def resultados_para_devolver(remision_id: int, db=Depends(get_db),
                             usuario=Depends(requiere_rol("admin", "especialista"))):
    remision = _caso_contrarreferencia(db, remision_id, usuario)
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT o.id, o.tipo, o.valor, o.unidad,
                              o.fecha_observacion AT TIME ZONE current_setting('TimeZone') AS fecha_observacion,
                              EXISTS (SELECT 1 FROM interop_mensajes m WHERE m.remision_id = o.remision_id
                                      AND m.tipo = 'Observation' AND m.direccion = 'saliente'
                                      AND m.estado = 'enviado' AND m.recurso->>'observacion_id' = o.id::text) AS enviado
                       FROM observaciones o WHERE o.remision_id = %s AND o.socio_id IS NULL AND o.codigo_loinc = ANY(%s)
                       ORDER BY o.fecha_observacion DESC, o.id DESC;""", (remision_id, list(CATALOGO_LOINC)))
        observaciones = cur.fetchall()
        consentimiento = _consentimiento_vigente(cur, remision["paciente_id"], remision["socio_id"])
    return {"observaciones": observaciones, "consentimiento": bool(consentimiento), "puede_registrar": remision["estado"] == "aceptada"}


@router.post("/interop/recibidas/{remision_id}/resultados/{observacion_id}")
def devolver_resultado(remision_id: int, observacion_id: int, db=Depends(get_db),
                      usuario=Depends(requiere_rol("admin", "especialista"))):
    rem = _caso_contrarreferencia(db, remision_id, usuario)
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        socio = _socio(cur, socio_id=rem["socio_id"])
        if not socio or not socio["activo"]:
            raise HTTPException(409, "La institución de origen está desactivada")
        cur.execute("""SELECT o.*, o.fecha_observacion AT TIME ZONE current_setting('TimeZone') AS instante
                       FROM observaciones o WHERE id = %s AND remision_id = %s AND socio_id IS NULL
                       FOR UPDATE;""", (observacion_id, remision_id))
        obs = cur.fetchone()
        if obs is None or obs["codigo_loinc"] not in CATALOGO_LOINC:
            raise HTTPException(404, "Resultado local del perfil no encontrado")
        cur.execute("""SELECT 1 FROM interop_mensajes WHERE remision_id = %s AND tipo = 'Observation'
                       AND direccion = 'saliente' AND estado = 'enviado' AND recurso->>'observacion_id' = %s;""",
                    (remision_id, str(observacion_id)))
        if cur.fetchone():
            raise HTTPException(409, "Este resultado ya se envió")
        consentimiento = _consentimiento_vigente(cur, rem["paciente_id"], socio["id"])
        if not consentimiento:
            raise HTTPException(403, "Se requiere consentimiento vigente para devolver resultados al socio")
        documento = cripto.descifrar(rem["documento"], cripto.aad_paciente(rem["paciente_id"], "documento"))
        federado = federado_de_paciente(rem["paciente_id"], documento)
        sujeto = _referencia_logica("Patient", SID_FEDERADO, federado)
        obs["fecha_observacion"] = obs["instante"]
        recurso = _recurso_observacion(obs, sujeto, rem["referencia_externa"], rem["encuentro_fhir"])
        recurso["identifier"] = [{"system": SID_OBSERVACION,
                                  "value": cripto.firmar(SECRETO_FEDERADO, f"{INSTITUCION}|OBS|{observacion_id}".encode())}]
        errores, _ = _validar_observation(recurso, "Observation", exigir_sujeto=True)
        signo = SIGNOS.get("59408-5" if obs["codigo_loinc"] == "2708-6" else obs["codigo_loinc"])
        if errores or obs["unidad"] not in ((signo.unidad, *signo.alias_unidad) if signo else ("mg/dL",)):
            raise HTTPException(422, "Verifica el código, valor y unidad del resultado antes de compartirlo")
        try:
            codigo, mensaje_id, texto = _enviar(socio, "Observation", recurso)
        except HTTPException as exc:
            codigo, mensaje_id, texto = 0, str(uuid.uuid4()), str(exc.detail)
        ok = codigo in (200, 201, 202)
        _registrar_mensaje(cur, socio["id"], "saliente", mensaje_id, "Observation", "enviado" if ok else "error",
                           codigo or None, remision_id, rem["paciente_id"], rem["referencia_externa"],
                           detalle=texto, recurso={"observacion_id": observacion_id, "consentimiento_id": consentimiento["id"]})
    registrar_auditoria(db, usuario["id"], "interop_resultado_enviado" if ok else "interop_resultado_fallido",
                        "observaciones", observacion_id, f"socio={socio['id']} remision={remision_id} mensaje={mensaje_id} http={codigo}")
    if not ok:
        raise HTTPException(502, f"El socio no confirmó el resultado ({codigo or 'sin conexión'}). {texto}")
    return {"codigo": codigo, "asociado": codigo in (200, 201)}


@router.get("/interop/recibidas")
def remisiones_recibidas(db=Depends(get_db), usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    """Bandeja del especialista: remisiones que llegaron de otra institución."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT r.id, r.paciente_id, p.nombre AS paciente_nombre, p.por_conciliar, r.institucion_origen,
                          r.motivo, r.estado, r.estado_interop, r.referencia_externa, r.atendida_por,
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
                          (SELECT count(*) FROM observaciones o WHERE o.remision_id = r.id AND o.socio_id = r.socio_id)
                            AS resultados_recibidos,
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
                   "opcionales": ["Encounter", "Observation", "DiagnosticReport (status final)", "Consent (status active)"],
                   "Patient": "Solo identificador federado",
                   "ServiceRequest.code": {"system": SNOMED, "code": SNOMED_REMISION[0]},
                   "ServiceRequest.identifier": SID_REMISION,
                   "Encounter.identifier": SID_ENCUENTRO, "Encounter.class": CLASE_REMISION},
        "observacion": {"subject.identifier": SID_FEDERADO, "basedOn[].identifier": SID_REMISION,
                        "encounter.identifier": SID_ENCUENTRO, "performer[].identifier": SID_PROFESIONAL,
                        "identifier": SID_OBSERVACION},
        "loinc_aceptados": {k: {"nombre": v[0], "ucum": v[1]} for k, v in CATALOGO_LOINC.items()},
        "unidades": UCUM,
        "respuestas": {"201": "aceptado y asociado a un paciente", "202": "aceptado; falta conciliar al paciente",
                       "400": "no es JSON o no es el recurso esperado", "401": "sin token, token vencido o firma inválida",
                       "403": "token válido, sin el permiso", "409": "mensaje repetido",
                       "422": "no cumple el perfil (OperationOutcome)", "429": "demasiadas peticiones"},
    }
