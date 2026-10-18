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
from datetime import date
from typing import Optional

import httpx
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import SECRET_KEY, get_db, get_usuario_actual, registrar_auditoria, requiere_rol
from eventos import ids_con_rol, publicar

router = APIRouter(tags=["IA"])

ML_URL = os.getenv("ML_URL", "http://ml:8001")

MAPA_SEXO = {"F": 1, "M": 2}

CAMPOS_CLINICOS = (
    "sexo", "edad", "pacientes_por_hora", "modo_llegada", "lesion",
    "mental", "dolor", "nrs_dolor", "sbp", "dbp", "fc", "fr",
    "temperatura", "saturacion",
)


# ── Modelos de entrada ────────────────────────────────────────────────────
class AnalisisIn(BaseModel):
    # Mismos rangos que valida el servicio ml: así el error sale aquí, claro, y no como "IA caída"
    paciente_id: int
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
    remision_id: Optional[int] = None


class RevisionIn(BaseModel):
    observaciones: Optional[str] = None


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

        nivel_bd = (
            "urgente"
            if resultado["nivel"] in ("critico", "urgente")
            else "no_urgente"
        )

        # Nombres de columnas de la tabla reportes_ia existente
        cur.execute(
            """
            INSERT INTO reportes_ia (
                remision_id,
                paciente_id,
                creado_por,
                modelo,
                probabilidad_urgente,
                prediccion,
                critico_segun_umbral,
                payload_seudonimizado
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
            """,
            (
                remision_id,
                paciente["id"],
                usuario["id"],
                resultado["version_modelo"],
                resultado["probabilidad"],
                nivel_bd,
                resultado["critico"],
                json.dumps(payload),
            ),
        )

        reporte_id = cur.fetchone()["id"]
        db.commit()

    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()

    registrar_auditoria(
        db,
        usuario["id"],
        "analisis_ia",
        "reportes_ia",
        reporte_id,
        f"paciente_id={paciente['id']}",
    )

    # R20: alerta crítica en el momento del análisis
    if resultado["critico"]:
        publicar([usuario["id"]], {
            "tipo": "alerta_critica",
            "mensaje": f"Caso crítico en el paciente {paciente['id']} (probabilidad {resultado['probabilidad']})",
            "paciente_id": paciente["id"],
            "reporte_id": reporte_id,
            "nivel": "critico",
        })

    return {"id": reporte_id, "estado": "pendiente"}


# ── Consulta de reportes ──────────────────────────────────────────────────
def _buscar_reporte(db, reporte_id: int) -> dict:
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute(
            "SELECT * FROM reportes_ia WHERE id = %s;",
            (reporte_id,),
        )
        fila = cur.fetchone()
    finally:
        cur.close()

    if fila is None:
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
                   creado_en AS fecha_creacion
            FROM reportes_ia {donde}
            ORDER BY id DESC;
            """,
            params,
        )
        filas = cur.fetchall()
    finally:
        cur.close()

    for f in filas:
        f["probabilidad"] = float(f["probabilidad"])
    return filas


@router.get("/ia/reportes/{reporte_id}")
def ver_reporte(
    reporte_id: int,
    db=Depends(get_db),
    usuario=Depends(requiere_rol("admin", "medico", "especialista")),
):
    r = _buscar_reporte(db, reporte_id)

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
        "payload": _buscar_reporte(db, reporte_id)["payload_seudonimizado"]
    }


# ── Aprobación clínica (R10) ──────────────────────────────────────────────
def _revisar(db, reporte_id: int, usuario: dict, nuevo_estado: str,
             observaciones: Optional[str]) -> dict:
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
         RETURNING id, paciente_id;
            """,
            (nuevo_estado, observaciones, usuario["id"], reporte_id),
        )
        fila = cur.fetchone()
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()

    if fila is None:
        actual = _buscar_reporte(db, reporte_id)  # 404 si no existe
        raise HTTPException(
            status_code=409,
            detail=f"El reporte ya está {actual['estado']}",
        )
    return fila


@router.post("/ia/reportes/{reporte_id}/aprobar")
def aprobar_reporte(
    reporte_id: int,
    cuerpo: Optional[RevisionIn] = None,
    db=Depends(get_db),
    usuario=Depends(requiere_rol("medico", "especialista")),
):
    fila = _revisar(db, reporte_id, usuario, "aprobado",
                    cuerpo.observaciones if cuerpo else None)
    registrar_auditoria(db, usuario["id"], "reporte_aprobado", "reportes_ia",
                        reporte_id, f"paciente_id={fila['paciente_id']}")

    # R19: de aquí la Persona 4 arma la factura (sin datos clínicos)
    publicar(ids_con_rol(db, "contable"), {
        "tipo": "reporte_aprobado",
        "mensaje": "Se aprobó un análisis de IA: genera su factura",
        "paciente_id": fila["paciente_id"],
        "reporte_id": reporte_id,
    })
    return {"id": reporte_id, "estado": "aprobado"}


@router.post("/ia/reportes/{reporte_id}/rechazar")
def rechazar_reporte(
    reporte_id: int,
    cuerpo: Optional[RevisionIn] = None,
    db=Depends(get_db),
    usuario=Depends(requiere_rol("medico", "especialista")),
):
    fila = _revisar(db, reporte_id, usuario, "rechazado",
                    cuerpo.observaciones if cuerpo else None)
    registrar_auditoria(db, usuario["id"], "reporte_rechazado", "reportes_ia",
                        reporte_id, f"paciente_id={fila['paciente_id']}")
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
    usuario=Depends(requiere_rol("admin", "medico", "especialista")),
):
    return _pedir_a_ml("GET", "/clusters", espera=60)["clusters"]

@router.get("/ia/clusters/detalle")
def detalle_clusters(
      usuario=Depends(requiere_rol("admin", "medico", "especialista")),
):
      return _pedir_a_ml("GET", "/clusters/detalle", espera=120)
  

class PrediccionIn(BaseModel):
    """Mismos campos y rangos que AnalisisIn, pero sin paciente ni remisión: es una consulta suelta."""
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