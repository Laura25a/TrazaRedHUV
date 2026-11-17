"""
ia.py — Análisis de IA con aprobación clínica (Corte 2, Persona 2: R07, R10, R12, R22).

R12: seudonimización de datos clínicos antes de llamar al modelo.
R10: reportes pendientes de aprobación clínica (aprobar / rechazar).
R20: alerta crítica al momento del análisis (vía publicar()).
R19: evento de factura al aprobar un reporte (vía publicar()).
R22: /modelo y /entrenar a través de la API.
R07: ruta de clusters (/ia/clusters, la calcula el servicio ml).
"""
import hashlib
import hmac
import json
import os
import threading
import time
from contextlib import closing
from datetime import date, timedelta
from typing import Optional

import httpx
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from auth import SECRET_KEY, get_db, get_usuario_actual, registrar_auditoria, requiere_rol
from eventos import _conexion_pg, ids_con_rol, registrar_evento
from reactividad import VITALES_KTAS, responsable_episodio
from validacion_clinica import validar_presion

router = APIRouter(tags=["IA"])

ML_URL = os.getenv("ML_URL", "http://ml:8001")

MAPA_SEXO = {"F": 1, "M": 2}

CAMPOS_CLINICOS = (
    "sexo", "edad", "pacientes_por_hora", "modo_llegada", "lesion",
    "mental", "dolor", "nrs_dolor", "sbp", "dbp", "fc", "fr",
    "temperatura", "saturacion",
)


# ── Modelos de entrada ────────────────────────────────────────────────────
class PrediccionIn(BaseModel):
    """Variables de predicción, con o sin asociación al historial clínico."""
    sexo: Optional[int] = Field(None, ge=1, le=2)
    edad: Optional[int] = Field(None, ge=0, le=120)
    pacientes_por_hora: Optional[int] = Field(None, ge=0, le=200)
    modo_llegada: int = Field(3, ge=1, le=7)
    lesion: int = Field(1, ge=1, le=2)
    mental: int = Field(1, ge=1, le=4)
    dolor: int = Field(0, ge=0, le=1)
    nrs_dolor: Optional[float] = Field(None, ge=0, le=10)
    sbp: Optional[float] = Field(None, ge=40, le=300)
    dbp: Optional[float] = Field(None, ge=20, le=200)
    fc: Optional[float] = Field(None, ge=20, le=250)
    fr: Optional[float] = Field(None, ge=4, le=80)
    temperatura: Optional[float] = Field(None, ge=30, le=45)
    saturacion: Optional[float] = Field(None, ge=0, le=100)

    @model_validator(mode="after")
    def comprobar_presion(self):
        validar_presion(self.sbp, self.dbp)
        return self


class AnalisisIn(PrediccionIn):
    paciente_id: int
    remision_id: Optional[int] = None


class RevisionIn(BaseModel):
    observaciones: str = Field(min_length=1, max_length=300)

    @field_validator("observaciones", mode="before")
    @classmethod
    def limpiar_nota(cls, valor):
        return valor.strip() if isinstance(valor, str) else valor


# ── R12: seudonimización ──────────────────────────────────────────────────
def seudonimo(paciente_id: int) -> str:
    """Genera un identificador seudónimo estable por paciente."""
    h = hmac.new(
        SECRET_KEY.encode(),
        f"paciente:{paciente_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return "p-" + h[:12]


def armar_payload(paciente: dict, datos: AnalisisIn) -> dict:
    """Solo envía el identificador seudónimo y las variables clínicas."""
    d = datos.model_dump()

    if d["edad"] is None and paciente.get("fecha_nacimiento"):
        nacimiento = paciente["fecha_nacimiento"]
        hoy = date.today()
        d["edad"] = (
            hoy.year - nacimiento.year
            - ((hoy.month, hoy.day) < (nacimiento.month, nacimiento.day))
        )

    if d["sexo"] is None:
        d["sexo"] = MAPA_SEXO.get(paciente.get("genero"))

    payload = {"id_seudonimo": seudonimo(paciente["id"])}
    payload.update({campo: d[campo] for campo in CAMPOS_CLINICOS})

    return payload


def llamar_modelo(payload: dict) -> dict:
    """Envía el payload al servicio de IA."""
    try:
        respuesta = httpx.post(
            f"{ML_URL}/predecir",
            json=payload,
            timeout=15,
        )
        respuesta.raise_for_status()
        return respuesta.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 422:      # el modelo rechazó los datos: no es una caída
            raise HTTPException(
                status_code=422,
                detail="Datos clínicos fuera de rango o con campos no permitidos",
            )
        raise HTTPException(
            status_code=503,
            detail="El servicio de IA no está disponible",
        )
    except httpx.HTTPError:
        raise HTTPException(
            status_code=503,
            detail="El servicio de IA no está disponible",
        )


# ── Análisis (crea el reporte en estado pendiente) ────────────────────────
@router.post("/ia/analizar", status_code=201)
def analizar(
    datos: AnalisisIn,
    db=Depends(get_db),
    usuario=Depends(requiere_rol("medico", "especialista")),
):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute(
            """
            SELECT id, genero, fecha_nacimiento
            FROM pacientes
            WHERE id = %s;
            """,
            (datos.paciente_id,),
        )
        paciente = cur.fetchone()

        if paciente is None:
            raise HTTPException(
                status_code=404,
                detail="Paciente no encontrado",
            )

        # Se valida la remisión ANTES de llamar al modelo.
        # Si no viene remision_id, se usa la última remisión activa del paciente (si tiene).
        remision_id = datos.remision_id
        if remision_id is not None:
            cur.execute(
                "SELECT id FROM remisiones WHERE id = %s AND paciente_id = %s AND activo = TRUE;",
                (remision_id, datos.paciente_id),
            )
            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=404,
                    detail="No existe una remisión activa para ese paciente",
                )
        else:
            cur.execute(
                """SELECT id FROM remisiones
                    WHERE paciente_id = %s AND activo = TRUE
                    ORDER BY fecha_solicitud DESC, id DESC LIMIT 1;""",
                (datos.paciente_id,),
            )
            fila = cur.fetchone()
            remision_id = fila["id"] if fila else None

        payload = armar_payload(paciente, datos)
        resultado = llamar_modelo(payload)

        reporte_id = _guardar_reporte(db, paciente["id"], remision_id, usuario["id"], payload, resultado)
        registrar_auditoria(db, usuario["id"], "analisis_ia", "reportes_ia",
                            reporte_id, f"paciente_id={paciente['id']}")
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()

    return {"id": reporte_id, "estado": "pendiente"}


def _guardar_reporte(db, paciente_id, remision_id, responsable, payload, resultado, huella=None):
    nivel = "urgente" if resultado["nivel"] in ("critico", "urgente") else "no_urgente"
    with db.cursor() as cur:
        cur.execute("""INSERT INTO reportes_ia
                       (remision_id, paciente_id, creado_por, modelo, probabilidad_urgente,
                        prediccion, critico_segun_umbral, payload_seudonimizado, huella_automatica)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (remision_id, huella_automatica) WHERE huella_automatica IS NOT NULL
                       DO NOTHING RETURNING id;""",
                    (remision_id, paciente_id, responsable, resultado["version_modelo"], resultado["probabilidad"],
                     nivel, resultado["critico"], json.dumps(payload), huella))
        fila = cur.fetchone()
    if not fila:
        return None
    reporte_id = fila[0]
    if responsable:
        contexto = {"paciente_id": paciente_id, "remision_id": remision_id, "reporte_id": reporte_id}
        if huella:
            registrar_evento(db, [responsable], {
                "tipo": "reporte_pendiente", "mensaje": "Hay un borrador automático de urgencia para revisar", **contexto,
            })
        if resultado["critico"]:
            registrar_evento(db, [responsable], {
                "tipo": "alerta_critica", "mensaje": "El borrador de urgencia requiere revisión prioritaria",
                "responsable_id": responsable, **contexto,
            })
    return reporte_id


# ── Procesamiento de tomas pendientes ────────────────────────────────────────
CAMPOS_AUTOMATICOS = dict(zip(VITALES_KTAS, ("sbp", "dbp", "fc", "fr", "saturacion", "temperatura")))
VENTANA_TOMA = timedelta(minutes=30)


def _toma_automatica(db, remision_id):
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT r.id, r.paciente_id, r.creado_por, r.atendida_por, p.genero, p.fecha_nacimiento
                         FROM remisiones r JOIN pacientes p ON p.id = r.paciente_id
                        WHERE r.id = %s AND r.activo AND r.estado IN ('pendiente','en_gestion','aceptada')
                          AND NOT p.por_conciliar;""", (remision_id,))
        episodio = cur.fetchone()
        if not episodio or not episodio["fecha_nacimiento"] or episodio["genero"] not in MAPA_SEXO:
            return None
        cur.execute("""SELECT DISTINCT ON (codigo) codigo, id, valor, unidad, fecha_observacion
                         FROM (SELECT CASE WHEN codigo_loinc = '2708-6' THEN '59408-5'
                                           ELSE codigo_loinc END AS codigo,
                                      id, valor, unidad, fecha_observacion
                                 FROM observaciones WHERE remision_id = %s) o
                        WHERE codigo = ANY(%s)
                        ORDER BY codigo, fecha_observacion DESC, id DESC;""",
                    (remision_id, list(VITALES_KTAS)))
        signos = cur.fetchall()
    if len(signos) != len(VITALES_KTAS):
        return None
    fechas = [s["fecha_observacion"] for s in signos]
    if max(fechas) - min(fechas) > VENTANA_TOMA:
        return None
    responsable = responsable_episodio(db, episodio)
    if not responsable:
        return None
    from validacion_clinica import validar_signo
    from validaciones import hoy_bogota

    try:
        valores = {CAMPOS_AUTOMATICOS[s["codigo"]]: float(s["valor"]) for s in signos}
        for signo in signos:
            validar_signo(signo["codigo"], float(signo["valor"]), signo["unidad"])
        nacimiento = episodio["fecha_nacimiento"]
        hoy = hoy_bogota()
        edad = hoy.year - nacimiento.year - ((hoy.month, hoy.day) < (nacimiento.month, nacimiento.day))
        # El formulario manual también usa categorías base. Para el borrador
        # automático, las categorías no observadas se entregan como faltantes.
        datos = AnalisisIn(paciente_id=episodio["paciente_id"], edad=edad, **valores)
        payload = armar_payload({**episodio, "id": episodio["paciente_id"]}, datos)
        for campo in ("modo_llegada", "lesion", "mental", "dolor"):
            payload[campo] = None
    except ValueError:
        return None
    huella = ":".join(str(signo["id"]) for signo in signos)
    return episodio["paciente_id"], responsable, payload, huella


def procesar_analisis_automaticos():
    """El modelo se consulta fuera de la transacción de escritura."""
    with closing(_conexion_pg()) as db:
        with db, db.cursor() as cur:
            cur.execute("""SELECT remision_id, observacion_id FROM analisis_automaticos_pendientes
                            WHERE intentar_en <= now() ORDER BY intentar_en LIMIT 20;""")
            solicitudes = cur.fetchall()
        for remision_id, observacion_id in solicitudes:
            with db:
                toma = _toma_automatica(db, remision_id)
            try:
                resultado = llamar_modelo(toma[2]) if toma else None
            except HTTPException as e:
                with db, db.cursor() as cur:
                    cur.execute("""UPDATE analisis_automaticos_pendientes
                                      SET intentar_en = now() + interval '1 minute'
                                    WHERE remision_id = %s AND observacion_id = %s;""",
                                (remision_id, observacion_id))
                print(f"[IA] análisis automático reprogramado ({e.status_code})")
                continue
            with db, db.cursor() as cur:
                cur.execute("SELECT id FROM remisiones WHERE id = %s FOR UPDATE;", (remision_id,))
                cur.execute("""DELETE FROM analisis_automaticos_pendientes
                                WHERE remision_id = %s AND observacion_id = %s RETURNING remision_id;""",
                            (remision_id, observacion_id))
                if not cur.fetchone():
                    continue
                toma_vigente = _toma_automatica(db, remision_id)
                if toma_vigente != toma:
                    if toma_vigente:
                        cur.execute("""INSERT INTO analisis_automaticos_pendientes (remision_id, observacion_id)
                                       VALUES (%s, %s);""", (remision_id, observacion_id))
                    continue
                if toma:
                    paciente_id, responsable, payload, huella = toma
                    reporte_id = _guardar_reporte(db, paciente_id, remision_id, responsable, payload, resultado, huella)
                    if reporte_id is not None:
                        registrar_auditoria(db, None, "analisis_ia_automatico", "reportes_ia", reporte_id, confirmar=False)


def _ciclo_analisis_automatico():
    while True:
        try:
            procesar_analisis_automaticos()
        except Exception as e:
            print(f"[IA] no se pudo procesar la cola automática: {type(e).__name__}")
        time.sleep(5)


def iniciar_analisis_automatico():
    threading.Thread(target=_ciclo_analisis_automatico, daemon=True, name="analisis-automatico").start()


# ── Consulta de reportes ──────────────────────────────────────────────────
def _buscar_reporte(db, reporte_id: int, usuario: dict) -> dict:
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute(
            "SELECT * FROM reportes_ia WHERE id = %s;",
            (reporte_id,),
        )
        fila = cur.fetchone()
    finally:
        cur.close()

    if fila is None or (fila["huella_automatica"] and usuario["rol"] != "admin" and fila["creado_por"] != usuario["id"]):
        raise HTTPException(
            status_code=404,
            detail="Reporte no encontrado",
        )

    return fila


@router.get("/ia/reportes")
def listar_reportes(
    paciente_id: Optional[int] = None,
    estado: Optional[str] = None,
    db=Depends(get_db),
    usuario=Depends(requiere_rol("admin", "medico", "especialista")),
):
    cond, params = [], []
    if usuario["rol"] != "admin":
        cond.append("(huella_automatica IS NULL OR creado_por = %s)")
        params.append(usuario["id"])
    if paciente_id is not None:
        cond.append("paciente_id = %s")
        params.append(paciente_id)
    if estado:
        cond.append("estado = %s")
        params.append(estado)
    donde = ("WHERE " + " AND ".join(cond)) if cond else ""

    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            f"""
            SELECT id, paciente_id, remision_id, estado,
                   probabilidad_urgente AS probabilidad,
                   prediccion AS nivel,
                   critico_segun_umbral AS critico,
                   observacion_revision AS observaciones,
                   creado_en AS fecha_creacion, huella_automatica IS NOT NULL AS automatico
            FROM reportes_ia {donde}
            ORDER BY id DESC;
            """,
            params,
        )
        filas = cur.fetchall()
    finally:
        cur.close()

    for f in filas:
        f["probabilidad"] = float(f["probabilidad"]) if f["probabilidad"] is not None else 0.0
    return filas


@router.get("/ia/reportes/{reporte_id}")
def ver_reporte(
    reporte_id: int,
    db=Depends(get_db),
    usuario=Depends(requiere_rol("admin", "medico", "especialista")),
):
    r = _buscar_reporte(db, reporte_id, usuario)

    return {
        "id": r["id"],
        "estado": r["estado"],
        "paciente_id": r["paciente_id"],
        "probabilidad": float(r["probabilidad_urgente"]),
        "nivel": r["prediccion"],
        "critico": r["critico_segun_umbral"],
        "version_modelo": r["modelo"],
    }


@router.get("/ia/reportes/{reporte_id}/payload")
def ver_payload(
    reporte_id: int,
    db=Depends(get_db),
    usuario=Depends(requiere_rol("admin", "medico", "especialista")),
):
    """Devuelve el payload seudonimizado enviado al modelo."""
    return {
        "payload": _buscar_reporte(db, reporte_id, usuario)["payload_seudonimizado"]
    }


# ── Aprobación clínica (R10) ──────────────────────────────────────────────
def _revisar(db, reporte_id: int, usuario: dict, nuevo_estado: str,
             observaciones: str) -> dict:
    """Pasa un reporte de 'pendiente' a aprobado/rechazado. Nada se borra."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """
            UPDATE reportes_ia
               SET estado = %s,
                   observacion_revision = %s,
                   revisado_por = %s,
                   revisado_en = now()
             WHERE id = %s AND estado = 'pendiente'
                   AND (huella_automatica IS NULL OR creado_por = %s)
         RETURNING id, paciente_id;
            """,
            (nuevo_estado, observaciones, usuario["id"], reporte_id, usuario["id"]),
        )
        fila = cur.fetchone()
        if fila is None:
            actual = _buscar_reporte(db, reporte_id, usuario)  # 404 si no existe
            raise HTTPException(
                status_code=409,
                detail=f"El reporte ya está {actual['estado']}",
            )
        if nuevo_estado == "aprobado":
            registrar_evento(db, ids_con_rol(db, "contable"), {
                "tipo": "reporte_aprobado", "mensaje": "Se aprobó un análisis de IA: genera su factura",
                "reporte_id": reporte_id,
            })
            cur.execute("INSERT INTO facturas_ia_pendientes (reporte_id) VALUES (%s);", (reporte_id,))
        registrar_auditoria(
            db, usuario["id"], f"reporte_{nuevo_estado}", "reportes_ia",
            reporte_id, f"paciente_id={fila['paciente_id']}",
        )
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()

    return fila


@router.post("/ia/reportes/{reporte_id}/aprobar")
def aprobar_reporte(
    reporte_id: int,
    cuerpo: RevisionIn,
    db=Depends(get_db),
    usuario=Depends(requiere_rol("medico", "especialista")),
):
    _revisar(db, reporte_id, usuario, "aprobado", cuerpo.observaciones)
    from eventos import completar_facturas_ia
    completar_facturas_ia(reporte_id)
    return {"id": reporte_id, "estado": "aprobado"}


@router.post("/ia/reportes/{reporte_id}/rechazar")
def rechazar_reporte(
    reporte_id: int,
    cuerpo: RevisionIn,
    db=Depends(get_db),
    usuario=Depends(requiere_rol("medico", "especialista")),
):
    _revisar(db, reporte_id, usuario, "rechazado", cuerpo.observaciones)
    return {"id": reporte_id, "estado": "rechazado"}


# ── El modelo (R22): la API es la única que le habla al servicio ml ───────
def _pedir_a_ml(metodo: str, ruta: str, espera: int = 10) -> dict:
    try:
        r = httpx.request(metodo, f"{ML_URL}{ruta}", timeout=espera)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError:
        raise HTTPException(
            status_code=503,
            detail="El servicio de IA no está disponible",
        )


@router.get("/modelo")
def modelo(usuario=Depends(get_usuario_actual)):
    return _pedir_a_ml("GET", "/modelo")


@router.post("/entrenar")
def entrenar(db=Depends(get_db), admin=Depends(requiere_rol("admin"))):
    resultado = _pedir_a_ml("POST", "/entrenar", espera=180)
    registrar_auditoria(db, admin["id"], "modelo_entrenado", "modelo", None,
                        f"version={resultado.get('version')}")
    return resultado
  
# R07: consulta de agrupamientos de pacientes
@router.get("/ia/clusters")
def listar_clusters(
    usuario=Depends(requiere_rol("admin", "medico", "especialista", "director")),
):
    return _pedir_a_ml("GET", "/clusters", espera=60)["clusters"]

@router.get("/ia/clusters/detalle")
def detalle_clusters(
      usuario=Depends(requiere_rol("admin", "medico", "especialista", "director")),
):
      return _pedir_a_ml("GET", "/clusters/detalle", espera=120)
  

@router.post("/ia/predecir")
def predecir_sin_guardar(
    datos: PrediccionIn,
    db=Depends(get_db),
    usuario=Depends(requiere_rol("admin", "medico", "especialista")),
):
    """Calcula la probabilidad con el modelo SIN guardar reporte ni asociarlo a un paciente."""
    d = datos.model_dump()
    payload = {"id_seudonimo": "consulta-sin-paciente"}   # no hay paciente: nada que seudonimizar
    payload.update({campo: d[campo] for campo in CAMPOS_CLINICOS})
    resultado = llamar_modelo(payload)
    registrar_auditoria(db, usuario["id"], "prediccion_ia", "modelo", None, "consulta sin paciente")
    return resultado

# ── Semana 10: asignar un paciente a su perfil (pertenencia suave + atípicos) ──
@router.post("/ia/asignar")
def asignar_perfil(
    datos: PrediccionIn,
    db=Depends(get_db),
    usuario=Depends(requiere_rol("admin", "medico", "especialista")),
):
    """Perfil del paciente (K-means), pertenencia suave (GMM esférica) y si es atípico
    (Isolation Forest). Solo variables clínicas: no se guarda ni se asocia a un paciente."""
    d = datos.model_dump()
    payload = {"id_seudonimo": "consulta-sin-paciente"}
    payload.update({campo: d[campo] for campo in CAMPOS_CLINICOS})
    try:
        r = httpx.post(f"{ML_URL}/asignar", json=payload, timeout=120)
        r.raise_for_status()
        resultado = r.json()
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="El servicio de IA no está disponible")
    registrar_auditoria(db, usuario["id"], "asignacion_perfil", "modelo", None,
                        f"perfil={resultado.get('perfil')} atipico={resultado.get('atipico')}")
    return resultado
