# agente/router.py · Endpoints del agente clínico
#
#   POST /agente/chat     {mensaje, conversacion_id, paciente_id}  → respuesta o acción por aprobar
#   POST /agente/decidir  {conversacion_id, decision, motivo}      → aprueba o rechaza la acción
#   GET  /agente/historial?conversacion_id=...                     → mensajes de la conversación
#   GET  /agente/estado                                            → modelo, memoria, herramientas
#
# Control de rol (R03), paciente fijado por el sistema (R12) y auditoría (R13).
# La memoria (hilos y preferencias) vive en PostgreSQL si está disponible; si no, en memoria.
import os
from contextlib import AsyncExitStack
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from agente import skills
from agente.harness import AUDITORIA, MAX_PASOS_MODELO, Harness, construir_agente
from agente.herramientas import CATALOGO, ROLES_CLINICOS, ContextoClinico
from agente.modelo import nombre_modelo, proveedor
from auth import PG_CONNECTION_STRING, get_db, get_usuario_actual, registrar_auditoria
from rag.router import seudonimo

router = APIRouter(prefix="/agente", tags=["Agente clínico"])

TIMEOUT_S = float(os.getenv("AGENTE_TIMEOUT_S", "60"))
MAX_SIMULTANEAS = int(os.getenv("AGENTE_MAX_SIMULTANEAS", "4"))
ESTADO = {"harness": None, "memoria": "sin iniciar"}
_pila = AsyncExitStack()


async def iniciar_agente() -> None:
    """Se llama al arrancar la API: memoria en PostgreSQL (checkpointer + store) o, si falla, en RAM."""
    checkpointer = store = None
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.store.postgres.aio import AsyncPostgresStore
        checkpointer = await _pila.enter_async_context(AsyncPostgresSaver.from_conn_string(PG_CONNECTION_STRING))
        store = await _pila.enter_async_context(AsyncPostgresStore.from_conn_string(PG_CONNECTION_STRING))
        await checkpointer.setup()
        await store.setup()                        # crea sus tablas la primera vez
        ESTADO["memoria"] = "PostgreSQL (checkpointer + store)"
    except Exception as e:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.store.memory import InMemoryStore
        checkpointer, store = InMemorySaver(), InMemoryStore()
        ESTADO["memoria"] = f"en memoria (PostgreSQL no disponible: {type(e).__name__})"
    ESTADO["harness"] = Harness(construir_agente(checkpointer=checkpointer, store=store,
                                                 max_simultaneas=MAX_SIMULTANEAS), timeout_s=TIMEOUT_S)
    print(f"[agente] listo · modelo {proveedor()} ({nombre_modelo()}) · memoria {ESTADO['memoria']}")


async def cerrar_agente() -> None:
    await _pila.aclose()


async def _harness() -> Harness:
    if ESTADO["harness"] is None:
        await iniciar_agente()
    return ESTADO["harness"]


class Mensaje(BaseModel):
    mensaje: str = Field(min_length=2, max_length=1000)
    conversacion_id: str = Field(default="principal", pattern=r"^[\w-]{1,40}$")
    paciente_id: int | None = None


class Decision(BaseModel):
    conversacion_id: str = Field(pattern=r"^[\w-]{1,40}$")
    decision: Literal["approve", "reject"]
    motivo: str | None = Field(default=None, max_length=300)


def exigir_clinico(usuario: dict) -> None:
    if usuario["rol"] not in ROLES_CLINICOS:
        raise HTTPException(status_code=403, detail="Solo personal clínico (médico o especialista) usa el asistente")


def _contexto(usuario: dict, conversacion: str, paciente_id: int | None) -> ContextoClinico:
    return ContextoClinico(usuario=usuario["correo"], usuario_id=usuario["id"], rol=usuario["rol"],
                           conversacion=conversacion, paciente_id=paciente_id,
                           paciente_alias=seudonimo(paciente_id) if paciente_id is not None else None)


@router.post("/chat")
async def chat(body: Mensaje, usuario: dict = Depends(get_usuario_actual), db=Depends(get_db)):
    exigir_clinico(usuario)                                                         # R03
    if body.paciente_id is not None:                   # médico y especialista ven a todos (R03 del sistema)
        cur = db.cursor()
        cur.execute("SELECT 1 FROM pacientes WHERE id = %s;", (body.paciente_id,))
        existe = cur.fetchone() is not None
        cur.close()
        if not existe:
            raise HTTPException(status_code=404, detail="Paciente no encontrado")
    ctx = _contexto(usuario, body.conversacion_id, body.paciente_id)                 # R12: seudónimo
    resultado = await (await _harness()).atender(ctx, body.mensaje)
    registrar_auditoria(db, usuario["id"], "agente_chat", "pacientes", body.paciente_id,
                        f"alias={ctx.paciente_alias} conversacion={body.conversacion_id} "
                        f"estado={resultado['estado']}")                             # R13
    return resultado


@router.post("/decidir")
async def decidir(body: Decision, usuario: dict = Depends(get_usuario_actual), db=Depends(get_db)):
    exigir_clinico(usuario)
    ctx = _contexto(usuario, body.conversacion_id, None)
    resultado = await (await _harness()).decidir(ctx, body.decision, body.motivo)
    registrar_auditoria(db, usuario["id"], f"agente_{'aprobado' if body.decision == 'approve' else 'rechazado'}",
                        "agente_propuestas", None,
                        f"conversacion={body.conversacion_id} estado={resultado['estado']} motivo={body.motivo or ''}")
    return resultado


@router.get("/historial")
async def historial(conversacion_id: str = Query("principal", pattern=r"^[\w-]{1,40}$"),
                    usuario: dict = Depends(get_usuario_actual)):
    exigir_clinico(usuario)
    h = await _harness()
    ctx = _contexto(usuario, conversacion_id, None)
    pendiente = Harness._pendiente(ctx)
    return {"mensajes": await h.historial(ctx),
            "pendiente": None if pendiente is None else
            {"herramienta": pendiente["herramienta"], "args": pendiente["argumentos"]}}


@router.get("/estado")
async def estado(usuario: dict = Depends(get_usuario_actual)):
    if usuario["rol"] not in ROLES_CLINICOS | {"admin"}:
        raise HTTPException(status_code=403, detail="Solo personal clínico")
    await _harness()
    from rag.motor import estado as estado_rag
    return {"proveedor": proveedor(), "modelo": nombre_modelo(), "memoria": ESTADO["memoria"],
            "herramientas": CATALOGO, "skills": skills.indice().splitlines(),
            "limites": {"max_llamadas_modelo_por_turno": MAX_PASOS_MODELO, "tiempo_maximo_s": TIMEOUT_S,
                        "llamadas_simultaneas_al_proveedor": MAX_SIMULTANEAS},
            "rag": estado_rag(), "auditoria_reciente": list(AUDITORIA)[-5:] if usuario["rol"] == "admin" else []}
