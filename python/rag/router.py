# rag/router.py · Asistente clínico RAG de TrazaRed HUV
# Adaptación del taller de la semana 12: la autenticación, el rol y la auditoría
# son los reales del proyecto (auth.py). Integración en main.py, una línea junto a
# los otros routers:
#     from rag.router import router as rag_router
#     app.include_router(rag_router)
import hashlib
import hmac
import os
from typing import Literal

import cripto
import psycopg2.extras

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth import get_db, get_usuario_actual, registrar_auditoria
from rag.motor import REVISION_CORPUS, estado, responder
from validaciones import exigir_asignacion_clinica

router = APIRouter(tags=["RAG"])
# Personal clínico. El admin entra en modo consulta (mismo criterio que el agente).
ROLES_REVISION = {"medico", "especialista"}
ROLES_ASISTENTE = ROLES_REVISION | {"admin"}


def seudonimo(paciente_id: int) -> str:
    """Alias estable e irreversible del paciente: HMAC con la llave del sistema."""
    llave = (os.getenv("LLAVE_SEUDONIMO") or os.getenv("SECRET_KEY") or "cambia-esta-llave").encode()
    return "PX-" + hmac.new(llave, str(paciente_id).encode(), hashlib.sha256).hexdigest()[:8].upper()


class Pregunta(BaseModel):
    pregunta: str = Field(min_length=3, max_length=500)
    paciente_id: int | None = None
    enviar_a_revision: bool = False


def ficha_para_seudonimizar(db, paciente_id: int | None, usuario: dict) -> dict | None:
    if paciente_id is None:
        return None
    exigir_asignacion_clinica(db, paciente_id, usuario)
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, nombre, documento, telefono, correo, direccion FROM pacientes WHERE id = %s;",
                    (paciente_id,))
        ficha = cripto.descifrar_paciente(cur.fetchone())
    if ficha is None:
        raise HTTPException(404, "Paciente no encontrado")
    return ficha


@router.post("/rag/preguntar")
def preguntar(body: Pregunta, request: Request,
              usuario: dict = Depends(get_usuario_actual),
              db=Depends(get_db)):
    """R23 · Responde con las guías del sistema y SIEMPRE dice de dónde salió: cada
    respuesta trae `fuentes` (documento y fragmento). Si no hay soporte, lo dice."""
    if usuario["rol"] not in ROLES_ASISTENTE:
        raise HTTPException(status_code=403, detail="Solo personal clínico consulta el asistente")
    import seguridad
    ip = seguridad.ip_cliente(request)
    incidente = seguridad.guardia(db, usuario, ip, body.pregunta, "rag")          # R25
    if incidente:
        return {"respuesta": seguridad.RESPUESTA_GUARDIA, "modo": "bloqueada", "alias": None, "fuentes": [],
                "seguridad": {"motivo": "inyeccion_detectada", **incidente}}
    if body.enviar_a_revision:
        if usuario["rol"] not in ROLES_REVISION:
            raise HTTPException(403, "Solo el personal clínico asignado envía resultados a revisión")
        if body.paciente_id is None:
            raise HTTPException(422, "Abre un paciente para enviar el resultado a revisión")
    paciente = ficha_para_seudonimizar(db, body.paciente_id, usuario)
    alias = seudonimo(body.paciente_id) if body.paciente_id is not None else None
    pregunta = seguridad.sanear_para_llm(body.pregunta, paciente)                  # R12: sin datos personales
    # La historia aprobada del paciente se lee de la base y se filtra ANTES de rankear
    resultado = responder(pregunta, alias, paciente_id=body.paciente_id, db=db)
    fuentes = [{k: f[k] for k in ("n", "chunk_id", "fuente", "texto", "url", "seccion")}
               for f in resultado["fuentes"]]
    revision_id = None
    if body.enviar_a_revision and fuentes:
        with db.cursor() as cur:
            cur.execute("""INSERT INTO revisiones_rag (paciente_id, creado_por, respuesta, fuentes, revision_corpus)
                           VALUES (%s, %s, %s, %s, %s) RETURNING id;""",
                        (body.paciente_id, usuario["id"], resultado["respuesta"],
                         psycopg2.extras.Json(fuentes), REVISION_CORPUS))
            revision_id = cur.fetchone()[0]
        registrar_auditoria(db, usuario["id"], "rag_enviado_revision", "revisiones_rag",
                            revision_id, confirmar=False)
    registrar_auditoria(db, usuario["id"], "rag_consulta", "paciente", body.paciente_id,
                        f"alias={alias} modo={resultado['modo']} "
                        f"fuentes={[f['chunk_id'] for f in fuentes]}")
    return {"respuesta": resultado["respuesta"], "modo": resultado["modo"], "alias": alias,
            "fuentes": fuentes, "revision_id": revision_id}


def consultar_revisiones(db, paciente_id: int, estado_revision: str) -> list[dict]:
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT id, estado, respuesta, fuentes, revision_corpus, creado_en, creado_por,
                              revisado_en, revisado_por
                       FROM revisiones_rag WHERE paciente_id = %s AND estado = %s
                       ORDER BY id DESC;""", (paciente_id, estado_revision))
        return cur.fetchall()


@router.get("/pacientes/{paciente_id}/rag/pendientes")
def pendientes(paciente_id: int, usuario: dict = Depends(get_usuario_actual), db=Depends(get_db)):
    if usuario["rol"] not in ROLES_REVISION:
        raise HTTPException(403, "Solo personal clínico revisa resultados")
    exigir_asignacion_clinica(db, paciente_id, usuario)
    return consultar_revisiones(db, paciente_id, "pendiente")


class Revision(BaseModel):
    decision: Literal["aprobado", "rechazado"]
    motivo: str = Field(default="", max_length=300)


@router.post("/pacientes/{paciente_id}/rag/{resultado_id}/revisar")
def revisar(paciente_id: int, resultado_id: int, body: Revision,
            usuario: dict = Depends(get_usuario_actual), db=Depends(get_db)):
    if usuario["rol"] not in ROLES_REVISION:
        raise HTTPException(403, "Solo personal clínico revisa resultados")
    exigir_asignacion_clinica(db, paciente_id, usuario)
    motivo = body.motivo.strip()
    if body.decision == "rechazado" and not motivo:
        raise HTTPException(422, "Escribe el motivo del rechazo")
    with db.cursor() as cur:
        cur.execute("""UPDATE revisiones_rag SET estado = %s, revisado_por = %s,
                           revisado_en = now(), motivo = %s
                       WHERE id = %s AND paciente_id = %s AND estado = 'pendiente'
                       RETURNING revisado_en;""",
                    (body.decision, usuario["id"], motivo or None, resultado_id, paciente_id))
        fila = cur.fetchone()
        if fila is None:
            cur.execute("SELECT 1 FROM revisiones_rag WHERE id = %s AND paciente_id = %s;",
                        (resultado_id, paciente_id))
            if cur.fetchone() is None:
                raise HTTPException(404, "Resultado RAG no encontrado")
            raise HTTPException(409, "Este resultado ya fue revisado")
    registrar_auditoria(db, usuario["id"], f"rag_{body.decision}", "revisiones_rag", resultado_id)
    return {"id": resultado_id, "estado": body.decision, "revisado_en": fila[0]}


@router.get("/rag/estado")
def estado_rag(usuario: dict = Depends(get_usuario_actual)):
    """Qué embeddings usa, si pgvector está activo y los umbrales de abstención."""
    if usuario["rol"] not in ROLES_ASISTENTE | {"admin"}:
        raise HTTPException(status_code=403, detail="Solo personal clínico")
    return estado()
