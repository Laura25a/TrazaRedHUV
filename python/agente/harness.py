# agente/harness.py · El arnés del agente clínico de TrazaRed HUV
#
# El modelo solo ESCRIBE que quiere llamar una herramienta; quien ejecuta es
# este código. Alrededor del modelo están las piezas del harness (semana 13):
#   prompt dinámico por capas · herramientas con el paciente fijado por el sistema ·
#   skills bajo demanda · auditoría de cada herramienta (tabla auditoria) ·
#   ventana de contexto · semáforo hacia el proveedor · máximo de llamadas ·
#   aprobación humana de las escrituras · memoria de corto plazo (hilo) y de
#   largo plazo (preferencias) · candado por hilo · tiempo máximo.
import asyncio
import re
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from langchain.agents import create_agent
from langchain.agents.middleware import (AgentMiddleware, HumanInTheLoopMiddleware,
                                         ModelCallLimitMiddleware, ModelRequest, dynamic_prompt)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, trim_messages
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command

from agente import skills
from agente.herramientas import HERRAMIENTAS, ContextoClinico, describir, espacio_memoria
from agente.modelo import crear_modelo, nombre_modelo, proveedor
from agente.prompt import armar_prompt
from auth import PG_CONNECTION_STRING

MAX_PASOS_MODELO = 8          # ModelCallLimit: un modelo confundido no pide herramientas para siempre
VENTANA_MENSAJES = 14         # memoria de trabajo que se le envía al modelo
HERRAMIENTAS_CON_APROBACION = {"proponer_remision"}


# ---------- 1. Prompt dinámico: se arma en CADA llamada al modelo ----------
@dynamic_prompt
def prompt_clinico(request: ModelRequest) -> str:
    ctx = request.runtime.context
    preferencias = []
    if request.runtime.store is not None:
        try:
            preferencias = [i.value["texto"] for i in request.runtime.store.search(espacio_memoria(ctx.usuario))]
        except Exception:
            preferencias = []
    return armar_prompt(ctx, skills.indice(), preferencias)


# ---------- 2. Auditoría de cada herramienta (R13) ----------
AUDITORIA: deque = deque(maxlen=500)      # copia en memoria (pruebas); la oficial es la tabla auditoria


def _auditar_bd(ctx: ContextoClinico, herramienta: str, args: dict) -> None:
    try:
        from auth import registrar_auditoria      # entra a la cadena de huellas (R28)
        conn = psycopg2.connect(PG_CONNECTION_STRING, connect_timeout=5)
        try:
            registrar_auditoria(conn, ctx.usuario_id, "agente_herramienta", "agente", None,
                                f"{herramienta} paciente={ctx.paciente_alias} "
                                f"conversacion={ctx.conversacion} args={describir(args)}")
        finally:
            conn.close()
    except Exception as e:                       # la auditoría nunca tumba la respuesta
        print(f"[agente] no se pudo auditar {herramienta}: {e}")


def delimitar(herramienta: str, texto: str) -> str:
    """SEPARAR LOS DATOS DE LAS ÓRDENES: lo que devuelve una herramienta viaja entre
    marcas. El prompt de sistema dice que lo que va entre <datos> nunca es una instrucción."""
    return f'<datos herramienta="{herramienta}">\n{texto}\n</datos>'


def sin_marcas(texto: str) -> str:
    return re.sub(r"^<datos[^>]*>\n?|\n?</datos>$", "", str(texto or "").strip())


class Auditoria(AgentMiddleware):
    def _registrar(self, request):
        ctx = request.runtime.context
        fila = {"fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "usuario": ctx.usuario_alias, "rol": ctx.rol, "paciente": ctx.paciente_alias,
                "herramienta": request.tool_call["name"], "args": request.tool_call["args"]}
        AUDITORIA.append(fila)
        _auditar_bd(ctx, fila["herramienta"], fila["args"])   # guarda el seudónimo, nunca el id real

    @staticmethod
    def _error(request, e) -> ToolMessage:
        """Una herramienta que falla no tumba la respuesta: el modelo recibe el error y lo explica."""
        print(f"[agente] la herramienta {request.tool_call['name']} falló: {type(e).__name__}: {e}")
        return ToolMessage(content=f"ERROR_HERRAMIENTA: no se pudo completar ({type(e).__name__}). "
                                   "Dile al clínico que esa información no está disponible ahora.",
                           tool_call_id=request.tool_call["id"], name=request.tool_call["name"], status="error")

    @staticmethod
    def _sanear(request, resultado):
        """R25 · inyección INDIRECTA: una nota o un documento puede traer escrito
        "ignora tus instrucciones...". Antes de que el resultado llegue al modelo se
        tachan esas órdenes, se deja el incidente y el texto se entrega entre marcas."""
        if not isinstance(resultado, ToolMessage) or not isinstance(resultado.content, str):
            return resultado
        from seguridad import neutralizar, registrar_inyeccion_indirecta
        nombre = request.tool_call["name"]
        limpio, habia = neutralizar(resultado.content)
        if habia:
            ctx = request.runtime.context
            registrar_inyeccion_indirecta(ctx.usuario_id, f"agente:{nombre}",
                                          f"orden escondida en datos de {ctx.paciente_alias or 'las guías'}")
        resultado.content = delimitar(nombre, limpio)
        return resultado

    def wrap_tool_call(self, request, handler):
        self._registrar(request)
        try:
            return self._sanear(request, handler(request))
        except GraphBubbleUp:              # pausas de aprobación humana: no son errores
            raise
        except Exception as e:
            return self._error(request, e)

    async def awrap_tool_call(self, request, handler):
        await asyncio.to_thread(self._registrar, request)
        try:
            return await asyncio.to_thread(self._sanear, request, await handler(request))
        except GraphBubbleUp:
            raise
        except Exception as e:
            return self._error(request, e)


# ---------- 2b. Registro de lo que se le envía al LLM (R24) ----------
def _rol_mensaje(m) -> str:
    return {"HumanMessage": "usuario", "AIMessage": "asistente", "ToolMessage": "herramienta",
            "SystemMessage": "sistema"}.get(type(m).__name__, type(m).__name__)


def _payload_llm(request) -> dict:
    """Exactamente lo que sale hacia el proveedor del modelo: prompt de sistema + mensajes."""
    sistema = getattr(request, "system_prompt", None)
    if sistema is None and getattr(request, "system_message", None) is not None:
        sistema = getattr(request.system_message, "content", None)
    mensajes = []
    for m in request.messages:
        fila = {"rol": _rol_mensaje(m), "contenido": _texto(m)[:4000]}
        if isinstance(m, AIMessage) and m.tool_calls:
            fila["llamadas"] = [{"herramienta": t["name"], "args": t["args"]} for t in m.tool_calls]
        if isinstance(m, ToolMessage):
            fila["herramienta"] = m.name
        mensajes.append(fila)
    return {"sistema": str(sistema or "")[:6000], "mensajes": mensajes}


class RegistroLLM(AgentMiddleware):
    """Cada llamada al modelo deja en agente_invocaciones el payload seudonimizado que
    se envió, y una fila en la auditoría. Así se puede comprobar, dato por dato, que
    al LLM no le llegó nombre, documento, correo, teléfono ni dirección (R12, R24)."""

    @staticmethod
    def _guardar(request) -> None:
        ctx = request.runtime.context
        try:
            from auth import registrar_auditoria
            payload = _payload_llm(request)
            conn = psycopg2.connect(PG_CONNECTION_STRING, connect_timeout=5)
            try:
                cur = conn.cursor()
                cur.execute("""INSERT INTO agente_invocaciones (usuario_id, rol, conversacion, paciente_alias, modelo, payload)
                               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;""",
                            (ctx.usuario_id, ctx.rol, ctx.conversacion, ctx.paciente_alias,
                             f"{proveedor()}:{nombre_modelo()}", psycopg2.extras.Json(payload)))
                invocacion_id = cur.fetchone()[0]
                conn.commit()
                cur.close()
                registrar_auditoria(conn, ctx.usuario_id, "agente_llm", "agente_invocaciones", invocacion_id,
                                    f"payload seudonimizado enviado al LLM · paciente={ctx.paciente_alias} "
                                    f"conversacion={ctx.conversacion} mensajes={len(payload['mensajes'])}")
            finally:
                conn.close()
            ctx.invocaciones.append(invocacion_id)
        except Exception as e:
            print(f"[agente] no se pudo registrar la invocación al LLM: {e}")

    def wrap_model_call(self, request, handler):
        self._guardar(request)
        return handler(request)

    async def awrap_model_call(self, request, handler):
        await asyncio.to_thread(self._guardar, request)
        return await handler(request)


# ---------- 3. Ventana de contexto (memoria de trabajo) ----------
class VentanaContexto(AgentMiddleware):
    def __init__(self, max_mensajes: int = VENTANA_MENSAJES):
        super().__init__()
        self.max_mensajes = max_mensajes

    def _recortar(self, request):
        recientes = trim_messages(request.messages, max_tokens=self.max_mensajes,
                                  token_counter=len, strategy="last", start_on="human")
        return request.override(messages=recientes)

    def wrap_model_call(self, request, handler):
        return handler(self._recortar(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._recortar(request))


# ---------- 4. Máximo de llamadas simultáneas al proveedor (semáforo) ----------
class LimiteProveedor(AgentMiddleware):
    def __init__(self, maximo: int = 4):
        super().__init__()
        self.maximo = maximo
        self._sem_async = asyncio.Semaphore(maximo)
        self._sem_hilos = threading.Semaphore(maximo)

    def wrap_model_call(self, request, handler):
        with self._sem_hilos:
            return handler(request)

    async def awrap_model_call(self, request, handler):
        async with self._sem_async:
            return await handler(request)


# ---------- 5. Armar el agente ----------
def construir_agente(checkpointer=None, store=None, latencia_simulada: float = 0.3, max_simultaneas: int = 4):
    return create_agent(
        model=crear_modelo(latencia_simulada),
        tools=HERRAMIENTAS,
        context_schema=ContextoClinico,
        middleware=[
            prompt_clinico,
            VentanaContexto(),
            LimiteProveedor(maximo=max_simultaneas),
            ModelCallLimitMiddleware(run_limit=MAX_PASOS_MODELO, exit_behavior="end"),
            HumanInTheLoopMiddleware(interrupt_on={
                nombre: {"allowed_decisions": ["approve", "reject"]} for nombre in HERRAMIENTAS_CON_APROBACION
            }),
            RegistroLLM(),                 # R24: payload exacto que sale hacia el modelo
            Auditoria(),
        ],
        checkpointer=checkpointer,
        store=store,
    )


# ---------- 6. El harness que atiende solicitudes concurrentes ----------
def _texto(m) -> str:
    c = m.content
    if isinstance(c, list):                     # algunos proveedores devuelven bloques
        c = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
    return str(c)


def _pasos_del_turno(mensajes: list) -> list[dict]:
    """Lo que hizo el agente en el último turno: qué herramienta pidió y qué le devolvió."""
    ultimo = max((i for i, m in enumerate(mensajes) if isinstance(m, HumanMessage)), default=-1)
    pasos = []
    for m in mensajes[ultimo + 1:]:
        if isinstance(m, AIMessage) and m.tool_calls:
            for t in m.tool_calls:
                pasos.append({"tipo": "llamada", "herramienta": t["name"], "args": t["args"]})
        elif isinstance(m, ToolMessage):
            pasos.append({"tipo": "resultado", "herramienta": m.name, "texto": sin_marcas(_texto(m))[:900]})
    return pasos


ETIQUETAS = {"consultar_paciente": "datos del paciente", "predecir_urgencia": "modelo de urgencia",
             "buscar_guias": "guías clínicas", "buscar_ips_con_convenio": "instituciones con convenio",
             "proponer_remision": "propuesta de remisión", "cargar_skill": None, "guardar_preferencia": None}


def _fuentes_del_turno(mensajes: list) -> list[dict]:
    """R23: las fuentes citadas (documento y fragmento) que devolvió buscar_guias en este turno."""
    ultimo = max((i for i, m in enumerate(mensajes) if isinstance(m, HumanMessage)), default=-1)
    fuentes, vistos = [], set()
    for m in mensajes[ultimo + 1:]:
        if isinstance(m, ToolMessage) and m.name == "buscar_guias":
            for n, chunk, fuente, texto in re.findall(r"^\[(\d+)\] \(([^)]+)\) ([^:\n]+): (.*)$", sin_marcas(_texto(m)), re.M):
                if chunk not in vistos:
                    vistos.add(chunk)
                    fuentes.append({"n": int(n), "chunk_id": chunk, "fuente": fuente.strip(), "texto": texto.strip()})
    return fuentes


def _interrupciones(salida) -> list:
    return list(getattr(salida, "interrupts", None) or [])


class Harness:
    def __init__(self, agente, timeout_s: float = 60):
        self.agente = agente
        self.timeout_s = timeout_s
        self._candados = defaultdict(asyncio.Lock)   # un candado por hilo

    @staticmethod
    def hilo(usuario: str, conversacion: str) -> str:
        return f"{usuario}:{conversacion}"            # el usuario sale del token, no del cuerpo

    # -- propuestas pendientes, guardadas en la base (sobreviven a reiniciar la API) --
    @staticmethod
    def _pendiente(ctx: ContextoClinico) -> dict | None:
        conn = psycopg2.connect(PG_CONNECTION_STRING, connect_timeout=5)
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""SELECT id, herramienta, argumentos, paciente_id FROM agente_propuestas
                           WHERE usuario_id = %s AND conversacion = %s AND estado = 'pendiente'
                           ORDER BY id DESC LIMIT 1;""", (ctx.usuario_id, ctx.conversacion))
            fila = cur.fetchone()
            cur.close()
            return fila
        finally:
            conn.close()

    @staticmethod
    def _registrar_propuestas(ctx: ContextoClinico, acciones: list[dict]) -> None:
        conn = psycopg2.connect(PG_CONNECTION_STRING, connect_timeout=5)
        try:
            cur = conn.cursor()
            for a in acciones:
                cur.execute("""INSERT INTO agente_propuestas (usuario_id, conversacion, paciente_id, herramienta, argumentos)
                               VALUES (%s, %s, %s, %s, %s);""",
                            (ctx.usuario_id, ctx.conversacion, ctx.paciente_id, a["herramienta"],
                             psycopg2.extras.Json(a["args"])))
            conn.commit()
            cur.close()
        finally:
            conn.close()

    @staticmethod
    def _rechazar_propuesta(ctx: ContextoClinico, motivo: str | None) -> None:
        conn = psycopg2.connect(PG_CONNECTION_STRING, connect_timeout=5)
        try:
            cur = conn.cursor()
            cur.execute("""UPDATE agente_propuestas SET estado = 'rechazada', decidida_en = now(), motivo_decision = %s
                           WHERE usuario_id = %s AND conversacion = %s AND estado = 'pendiente';""",
                        (motivo or "Rechazada por el clínico", ctx.usuario_id, ctx.conversacion))
            conn.commit()
            cur.close()
        finally:
            conn.close()

    def _empaquetar(self, salida, ctx: ContextoClinico) -> dict:
        mensajes = salida.value["messages"]
        pasos = _pasos_del_turno(mensajes)
        # Al clínico le llega la respuesta y de dónde salió (fuentes + qué se consultó).
        # La traza completa (cada herramienta con sus argumentos y resultados) es para el admin.
        consulto = list(dict.fromkeys(e for e in (ETIQUETAS.get(p["herramienta"]) for p in pasos
                                                  if p["tipo"] == "llamada") if e))
        base = {"conversacion": ctx.conversacion, "paciente": ctx.paciente_alias,
                "pasos": pasos if ctx.rol == "admin" else [], "consulto": consulto,
                "fuentes": _fuentes_del_turno(mensajes), "invocaciones": list(ctx.invocaciones),
                "modelo": proveedor()}
        interrupciones = _interrupciones(salida)
        if interrupciones:
            acciones = [{"herramienta": a["name"], "args": a["args"]}
                        for i in interrupciones for a in i.value["action_requests"]]
            return {**base, "estado": "pendiente_aprobacion", "acciones": acciones}
        return {**base, "estado": "respondido", "respuesta": _texto(mensajes[-1])}

    async def _ejecutar(self, entrada, ctx: ContextoClinico) -> dict:
        ctx.invocaciones.clear()
        config = {"configurable": {"thread_id": self.hilo(ctx.usuario, ctx.conversacion)}}
        async with self._candados[config["configurable"]["thread_id"]]:     # mismo hilo: uno a la vez
            try:
                salida = await asyncio.wait_for(
                    self.agente.ainvoke(entrada, config, context=ctx, version="v2"),
                    timeout=self.timeout_s)
            except asyncio.TimeoutError:
                return {"estado": "tiempo_agotado", "conversacion": ctx.conversacion,
                        "detalle": f"El asistente no respondió en {self.timeout_s:g} s. Intenta de nuevo."}
        resultado = self._empaquetar(salida, ctx)
        if resultado["estado"] == "pendiente_aprobacion":
            await asyncio.to_thread(self._registrar_propuestas, ctx, resultado["acciones"])
        return resultado

    async def atender(self, ctx: ContextoClinico, mensaje: str) -> dict:
        pendiente = await asyncio.to_thread(self._pendiente, ctx)
        if pendiente:
            return {"estado": "pendiente_aprobacion", "conversacion": ctx.conversacion,
                    "acciones": [{"herramienta": pendiente["herramienta"], "args": pendiente["argumentos"]}],
                    "detalle": "Hay una acción esperando tu decisión: apruébala o recházala primero.", "pasos": []}
        return await self._ejecutar({"messages": [{"role": "user", "content": mensaje}]}, ctx)

    async def decidir(self, ctx: ContextoClinico, decision: str, motivo: str | None = None) -> dict:
        pendiente = await asyncio.to_thread(self._pendiente, ctx)
        if pendiente is None:
            return {"estado": "sin_pendientes", "conversacion": ctx.conversacion}
        from rag.router import seudonimo
        ctx.paciente_id = pendiente["paciente_id"]              # el paciente de la propuesta, no otro
        ctx.paciente_alias = seudonimo(ctx.paciente_id) if ctx.paciente_id is not None else None
        d = {"type": decision}
        if decision == "reject":
            d["message"] = motivo or "El clínico rechazó la propuesta."
            await asyncio.to_thread(self._rechazar_propuesta, ctx, motivo)
        return await self._ejecutar(Command(resume={"decisions": [d]}), ctx)

    async def historial(self, ctx: ContextoClinico) -> list[dict]:
        """Mensajes visibles de la conversación (para pintar el chat al volver)."""
        config = {"configurable": {"thread_id": self.hilo(ctx.usuario, ctx.conversacion)}}
        try:
            estado = await self.agente.aget_state(config)
        except Exception:
            return []
        salida = []
        for m in (estado.values or {}).get("messages", []):
            if isinstance(m, HumanMessage):
                salida.append({"rol": "usuario", "texto": _texto(m)})
            elif isinstance(m, AIMessage) and not m.tool_calls and _texto(m).strip():
                salida.append({"rol": "asistente", "texto": _texto(m)})
        return salida[-30:]
