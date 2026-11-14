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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from agente import skills
from agente.harness import AUDITORIA, MAX_PASOS_MODELO, Harness, construir_agente
from agente.herramientas import CATALOGO, ROLES_CLINICOS, ContextoClinico
from agente.modelo import nombre_modelo, proveedor
import psycopg2.extras

import seguridad
from auth import PG_CONNECTION_STRING, get_db, get_usuario_actual, registrar_auditoria, requiere_rol
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
        raise HTTPException(status_code=403, detail="Tu rol no puede usar el asistente clínico")


def _contexto(usuario: dict, conversacion: str, paciente_id: int | None) -> ContextoClinico:
    return ContextoClinico(usuario=usuario["correo"], usuario_id=usuario["id"], rol=usuario["rol"],
                           conversacion=conversacion, paciente_id=paciente_id,
                           paciente_alias=seudonimo(paciente_id) if paciente_id is not None else None)


@router.post("/chat")
async def chat(body: Mensaje, request: Request, usuario: dict = Depends(get_usuario_actual), db=Depends(get_db)):
    exigir_clinico(usuario)                                                         # R03, R24
    ip = seguridad.ip_cliente(request)
    # R25 · GUARDIA: si la pregunta trae una orden para el asistente ("ignora tus
    # instrucciones...") no llega al modelo; queda el incidente con usuario e IP y, si
    # insiste, la cuenta y la IP se bloquean un rato. Una cuenta bloqueada recibe 403.
    incidente = seguridad.guardia(db, usuario, ip, body.mensaje, "agente")
    if incidente:
        return {"estado": "respondido", "conversacion": body.conversacion_id, "paciente": None,
                "respuesta": seguridad.RESPUESTA_GUARDIA + (
                    " Tu acceso al asistente quedó bloqueado temporalmente." if incidente["bloqueado"] else ""),
                "pasos": [], "consulto": [], "fuentes": [], "invocaciones": [],
                "seguridad": {"motivo": "inyeccion_detectada", **incidente}}
    paciente = None
    if body.paciente_id is not None:                   # médico y especialista ven a todos (R03 del sistema)
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, nombre FROM pacientes WHERE id = %s;", (body.paciente_id,))
        paciente = cur.fetchone()
        cur.close()
        if paciente is None:
            raise HTTPException(status_code=404, detail="Paciente no encontrado")
    ctx = _contexto(usuario, body.conversacion_id, body.paciente_id)                 # R12: seudónimo
    # R12/R24: lo que escribe el clínico también se minimiza antes de salir hacia el LLM
    mensaje = seguridad.sanear_para_llm(body.mensaje, paciente)
    resultado = await (await _harness()).atender(ctx, mensaje)
    registrar_auditoria(db, usuario["id"], "agente_chat", "pacientes", body.paciente_id,
                        f"alias={ctx.paciente_alias} conversacion={body.conversacion_id} "
                        f"estado={resultado['estado']} invocaciones={resultado.get('invocaciones', [])} ip={ip}")   # R13
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


@router.get("/invocaciones")
def invocaciones(limite: int = Query(50, ge=1, le=300), usuario: dict = Depends(get_usuario_actual), db=Depends(get_db)):
    """R24 · Bitácora de lo enviado al LLM. El admin ve todas; el clínico, solo las suyas."""
    exigir_clinico(usuario)
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT i.id, i.usuario_id, u.correo AS usuario, i.rol, i.conversacion, i.paciente_alias, i.modelo,
                          jsonb_array_length(i.payload->'mensajes') AS mensajes,
                          i.creado_en AT TIME ZONE current_setting('TimeZone') AS creado_en
                   FROM agente_invocaciones i LEFT JOIN usuarios u ON u.id = i.usuario_id
                   WHERE %s OR i.usuario_id = %s ORDER BY i.id DESC LIMIT %s;""",
                (usuario["rol"] == "admin", usuario["id"], limite))
    filas = cur.fetchall()
    cur.close()
    return filas


@router.get("/invocaciones/{invocacion_id}/payload")
def payload_invocacion(invocacion_id: int, usuario: dict = Depends(get_usuario_actual), db=Depends(get_db)):
    """R24 · El payload seudonimizado EXACTO que se envió al LLM en esa invocación.
    Sirve para comprobar que no viajó ningún dato personal (prueba del "canario")."""
    exigir_clinico(usuario)
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, usuario_id, paciente_alias, modelo, payload FROM agente_invocaciones WHERE id = %s;",
                (invocacion_id,))
    fila = cur.fetchone()
    cur.close()
    if fila is None or (usuario["rol"] != "admin" and fila["usuario_id"] != usuario["id"]):
        raise HTTPException(status_code=404, detail="Invocación no encontrada")
    return {"invocacion_id": fila["id"], "alias": fila["paciente_alias"], "modelo": fila["modelo"],
            "payload": fila["payload"]}


@router.get("/estado")
async def estado(usuario: dict = Depends(get_usuario_actual)):
    exigir_clinico(usuario)
    await _harness()
    from rag.motor import estado as estado_rag
    return {"proveedor": proveedor(), "modelo": nombre_modelo(), "memoria": ESTADO["memoria"],
            "herramientas": CATALOGO, "skills": skills.indice().splitlines(),
            "limites": {"max_llamadas_modelo_por_turno": MAX_PASOS_MODELO, "tiempo_maximo_s": TIMEOUT_S,
                        "llamadas_simultaneas_al_proveedor": MAX_SIMULTANEAS},
            "rag": estado_rag(), "auditoria_reciente": list(AUDITORIA)[-5:] if usuario["rol"] == "admin" else []}
