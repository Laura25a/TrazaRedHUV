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
from rag.motor import responder

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


@router.post("/rag/preguntar")
def preguntar(body: Pregunta,
              usuario: dict = Depends(get_usuario_actual),
              db=Depends(get_db)):
    if usuario["rol"] not in ROLES_ASISTENTE:
        raise HTTPException(status_code=403, detail="Solo personal clínico consulta el asistente")
    alias = seudonimo(body.paciente_id) if body.paciente_id is not None else None
    resultado = responder(body.pregunta, alias)
    registrar_auditoria(db, usuario["id"], "rag_consulta", "paciente", body.paciente_id,
                        f"alias={alias} modo={resultado['modo']} "
                        f"fuentes={[f['chunk_id'] for f in resultado['fuentes']]}")
    return {k: resultado[k] for k in ("respuesta", "modo", "fuentes")}
