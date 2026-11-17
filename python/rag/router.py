# rag/router.py · Asistente clínico RAG de TrazaRed HUV
# Adaptación del taller de la semana 12: la autenticación, el rol y la auditoría
# son los reales del proyecto (auth.py). Integración en main.py, una línea junto a
# los otros routers:
#     from rag.router import router as rag_router
#     app.include_router(rag_router)
import hashlib
import hmac
import os

import cripto
import psycopg2.extras

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth import get_db, get_usuario_actual, registrar_auditoria
from rag.motor import estado, responder
from validaciones import exigir_asignacion_clinica

router = APIRouter(tags=["RAG"])
# Personal clínico. El admin entra en modo consulta (mismo criterio que el agente).
ROLES_ASISTENTE = {"medico", "especialista", "admin"}


def seudonimo(paciente_id: int) -> str:
    """Alias estable e irreversible del paciente: HMAC con la llave del sistema."""
    llave = (os.getenv("LLAVE_SEUDONIMO") or os.getenv("SECRET_KEY") or "cambia-esta-llave").encode()
    return "PX-" + hmac.new(llave, str(paciente_id).encode(), hashlib.sha256).hexdigest()[:8].upper()


class Pregunta(BaseModel):
    pregunta: str = Field(min_length=3, max_length=500)
    paciente_id: int | None = None


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
    paciente = ficha_para_seudonimizar(db, body.paciente_id, usuario)
    alias = seudonimo(body.paciente_id) if body.paciente_id is not None else None
    pregunta = seguridad.sanear_para_llm(body.pregunta, paciente)                  # R12: sin datos personales
    # La historia aprobada del paciente se lee de la base y se filtra ANTES de rankear
    resultado = responder(pregunta, alias, paciente_id=body.paciente_id, db=db)
    registrar_auditoria(db, usuario["id"], "rag_consulta", "paciente", body.paciente_id,
                        f"alias={alias} modo={resultado['modo']} "
                        f"fuentes={[f['chunk_id'] for f in resultado['fuentes']]}")
    return {"respuesta": resultado["respuesta"], "modo": resultado["modo"], "alias": alias,
            "fuentes": [{k: f[k] for k in ("n", "chunk_id", "fuente", "texto")} for f in resultado["fuentes"]]}


@router.get("/rag/estado")
def estado_rag(usuario: dict = Depends(get_usuario_actual)):
    """Qué embeddings usa, si pgvector está activo y los umbrales de abstención."""
    if usuario["rol"] not in ROLES_ASISTENTE | {"admin"}:
        raise HTTPException(status_code=403, detail="Solo personal clínico")
    return estado()
