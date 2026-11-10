# rag/router.py · Asistente clínico RAG de TrazaRed HUV
# Adaptación del taller de la semana 12: la autenticación, el rol y la auditoría
# son los reales del proyecto (auth.py). Integración en main.py, una línea junto a
# los otros routers:
#     from rag.router import router as rag_router
#     app.include_router(rag_router)
import hashlib
import hmac
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_db, get_usuario_actual, registrar_auditoria
from rag.motor import estado, responder

router = APIRouter(tags=["RAG"])
# El profe definió el asistente para personal clínico; admin queda fuera a propósito.
ROLES_ASISTENTE = {"medico", "especialista"}


def seudonimo(paciente_id: int) -> str:
    """Alias estable e irreversible del paciente: HMAC con la llave del sistema."""
    llave = (os.getenv("LLAVE_SEUDONIMO") or os.getenv("SECRET_KEY") or "cambia-esta-llave").encode()
    return "PX-" + hmac.new(llave, str(paciente_id).encode(), hashlib.sha256).hexdigest()[:8].upper()


class Pregunta(BaseModel):
    pregunta: str = Field(min_length=3, max_length=500)
    paciente_id: int | None = None


def _paciente_existe(db, paciente_id: int) -> bool:
    cur = db.cursor()
    cur.execute("SELECT 1 FROM pacientes WHERE id = %s;", (paciente_id,))
    ok = cur.fetchone() is not None
    cur.close()
    return ok


@router.post("/rag/preguntar")
def preguntar(body: Pregunta,
              usuario: dict = Depends(get_usuario_actual),
              db=Depends(get_db)):
    if usuario["rol"] not in ROLES_ASISTENTE:
        raise HTTPException(status_code=403, detail="Solo personal clínico consulta el asistente")
    if body.paciente_id is not None and not _paciente_existe(db, body.paciente_id):
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    alias = seudonimo(body.paciente_id) if body.paciente_id is not None else None
    # La historia aprobada del paciente se lee de la base y se filtra ANTES de rankear
    resultado = responder(body.pregunta, alias, paciente_id=body.paciente_id, db=db)
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
