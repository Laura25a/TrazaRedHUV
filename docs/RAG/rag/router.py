# rag/router.py · Endpoint RAG
import hashlib, hmac, os
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from rag.motor import responder

router = APIRouter(prefix="/rag", tags=["RAG"])
LLAVE = (os.getenv("LLAVE_SEUDONIMO") or os.getenv("SECRET_KEY") or "cambia-esta-llave-en-pass.env").encode()
ROLES_CLINICOS = {"medico", "especialista"}


def seudonimo(paciente_id: int) -> str:
    return "PX-" + hmac.new(LLAVE, str(paciente_id).encode(), hashlib.sha256).hexdigest()[:8].upper()


def usuario_actual() -> dict:
    # Reemplazar por la dependencia real que valida el JWT del proyecto
    raise HTTPException(status_code=401, detail="Sin autenticación")


def auditar(usuario: dict, accion: str, recurso: str, detalle: dict) -> None:
    # Reemplazar por la función de auditoría del proyecto (R13)
    print("AUDITORIA", usuario.get("username"), accion, recurso, detalle)


class Pregunta(BaseModel):
    pregunta: str = Field(min_length=3, max_length=500)
    paciente_id: int | None = None


@router.post("/preguntar")
def preguntar(body: Pregunta, usuario: dict = Depends(usuario_actual)):
    if usuario.get("rol") not in ROLES_CLINICOS:
        raise HTTPException(status_code=403, detail="Solo personal clínico consulta el asistente")
    alias = seudonimo(body.paciente_id) if body.paciente_id is not None else None
    resultado = responder(body.pregunta, alias)
    auditar(usuario, "rag_consulta", f"paciente:{body.paciente_id}",
            {"fuentes": [f["chunk_id"] for f in resultado["fuentes"]], "modo": resultado["modo"]})
    return {k: resultado[k] for k in ("respuesta", "modo", "fuentes")}
