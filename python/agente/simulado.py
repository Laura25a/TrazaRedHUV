# agente/simulado.py · Modelo de práctica: imita el FORMATO de un LLM real, no su inteligencia
#
# Se usa cuando no hay GROQ_API_KEY (ni Ollama). Decide con reglas de palabras
# clave qué herramientas pedir, en qué orden y con qué argumentos, y al final
# arma una respuesta con lo que devolvieron. Sirve para demostrar y probar el
# harness (aprobación humana, auditoría, concurrencia) sin internet ni llaves.
import asyncio
import re
import time
import uuid

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool

SERVICIOS = {
    "uci": "UCI", "cuidado intensivo": "UCI", "intensiv": "UCI", "ventila": "UCI",
    "hemodinam": "hemodinamia", "infarto": "hemodinamia", "cateter": "hemodinamia",
    "neurocirug": "neurocirugía", "neurolog": "neurología", "tomograf": "neurología",
    "hemodi": "hemodiálisis", "dialisis": "hemodiálisis", "diálisis": "hemodiálisis",
    "quemad": "quemados", "pediatr": "pediatría", "obstetr": "obstetricia", "cirug": "cirugía",
}


def _servicio(texto: str) -> str:
    t = texto.lower()
    return next((nombre for raiz, nombre in SERVICIOS.items() if raiz in t), "")


class ModeloSimulado(BaseChatModel):
    latencia: float = 0.3          # segundos que "tarda" en pensar
    herramientas: list = []        # nombres de las herramientas que le dimos

    @property
    def _llm_type(self) -> str:
        return "simulado"

    def bind_tools(self, tools, **kwargs):
        nombres = [convert_to_openai_tool(t)["function"]["name"] for t in tools]
        return self.model_copy(update={"herramientas": nombres})

    # ── reglas que reemplazan el razonamiento ───────────────────────────
    def _plan(self, texto: str) -> list:
        t, plan = texto.lower(), []

        def usar(nombre, args=None):
            if nombre in self.herramientas and nombre not in [p[0] for p in plan]:
                plan.append((nombre, args or {}))

        remitir = re.search(r"rem[ií]t|traslad|referir|enviar a otra", t)
        gravedad = re.search(r"grave|urgen|riesgo|predic|deterior|cr[ií]tic|estable|c[oó]mo est[aá]", t)
        if remitir:
            usar("cargar_skill", {"nombre": "remision-interinstitucional"})
        elif gravedad:
            usar("cargar_skill", {"nombre": "deterioro-clinico"})
        if remitir or gravedad or re.search(r"paciente|este caso|sus datos|signos", t):
            usar("consultar_paciente")
        if remitir or gravedad:
            usar("predecir_urgencia")
        if remitir or re.search(r"convenio|ips|cl[ií]nica|instituci|d[oó]nde|cupo", t):
            usar("buscar_ips_con_convenio", {"servicio": _servicio(t)})
        if remitir or gravedad or re.search(r"gu[ií]a|protocolo|qu[eé] hago|criterio|recomend|cu[aá]ndo|sepsis|"
                                            r"qsofa|news|triage|hipogluc|infarto|cerebro|saturaci|norma|ley|historia", t):
            if remitir:
                consulta = f"criterios de remisión urgente {_servicio(t)} cuidados intensivos traslado"
            elif gravedad:
                consulta = "signos de alarma deterioro respiratorio saturación qSOFA shock hipotensión"
            else:
                consulta = texto[:200]
            usar("buscar_guias", {"pregunta": consulta})
        if remitir:
            usar("proponer_remision", {})        # los argumentos se completan con lo que devuelvan las otras
        if re.search(r"recuerda que|prefiero", t):
            usar("guardar_preferencia", {"preferencia": texto[:160]})
        return plan

    @staticmethod
    def _completar_remision(pregunta: str, resultados: list) -> dict:
        por_nombre = {m.name: str(m.content) for m in resultados}
        ips = re.search(r"^1\. (.+?) —", por_nombre.get("buscar_ips_con_convenio", ""), re.M)
        pred = por_nombre.get("predecir_urgencia", "")
        prioridad = "critica" if "CRÍTICO" in pred else ("urgente" if "URGENTE" in pred else "programada")
        prob = re.search(r"probabilidad ([0-9.,]+)", pred)
        servicio = _servicio(pregunta) or "valoración y manejo de mayor complejidad"
        motivo = f"Requiere {servicio}"
        if prob:
            motivo += f"; modelo de urgencia {prob.group(1)} ({prioridad})"
        return {"institucion_destino": ips.group(1).strip() if ips else "Fundación Valle del Lili",
                "motivo": motivo, "prioridad": prioridad}

    def _responder(self, mensajes) -> AIMessage:
        humanos = [i for i, m in enumerate(mensajes) if isinstance(m, HumanMessage)]
        if not humanos:
            return AIMessage(content="No veo ninguna pregunta en la conversación.")
        i = humanos[-1]
        pregunta = str(mensajes[i].content)
        resultados = [m for m in mensajes[i + 1:] if isinstance(m, ToolMessage)]
        plan = self._plan(pregunta)
        if len(resultados) < len(plan):                      # falta una herramienta
            nombre, args = plan[len(resultados)]
            if nombre == "proponer_remision":
                args = self._completar_remision(pregunta, resultados)
            llamada = {"name": nombre, "args": args, "id": "call_" + uuid.uuid4().hex[:8], "type": "tool_call"}
            return AIMessage(content="", tool_calls=[llamada])
        if not resultados:
            return AIMessage(content=(
                "Puedo ayudarte con el paciente abierto: revisar sus signos, estimar su urgencia con el modelo, "
                "buscar en las guías, sugerir instituciones con convenio con su EPS y proponer una remisión "
                "para que tú la apruebes. ¿Qué necesitas?"))
        por = {m.name: str(m.content) for m in resultados}
        partes = []
        pred = por.get("predecir_urgencia", "")
        if pred and not pred.startswith(("SIN_", "NO_", "MODELO_")):
            prob = re.search(r"probabilidad ([0-9.,]+) → ([A-ZÁÉÍÓÚ ]+)", pred)
            if prob:
                partes.append(f"Conclusión: el modelo de urgencia clasifica al paciente como {prob.group(2).strip()} "
                              f"(probabilidad {prob.group(1)}).")
        if "consultar_paciente" in por:
            cp = por["consultar_paciente"]
            if "Últimos signos vitales: " in cp:
                signos = cp.split("Últimos signos vitales: ", 1)[1].split(". Remisión")[0].split(". Último reporte")[0]
                partes.append("Datos del paciente: " + signos.rstrip(".") + ".")
            else:
                partes.append(cp[:250])
        if "buscar_guias" in por:
            fuentes = re.findall(r"^\[(\d+)\] \(([^)]+)\) ([^:]+): (.{0,170})", por["buscar_guias"], re.M)
            if fuentes:
                partes.append("Evidencia de las guías:\n" + "\n".join(f"- {t.strip()}… [{n}]" for n, _, _, t in fuentes))
            else:
                partes.append("Guías: no hay evidencia suficiente en las fuentes del sistema.")
        if "buscar_ips_con_convenio" in por:
            partes.append("Instituciones sugeridas:\n" + "\n".join(por["buscar_ips_con_convenio"].splitlines()[1:4]))
        if "proponer_remision" in por:
            texto = por["proponer_remision"]
            partes.append(("Decisión del clínico: " if "REMISION_CREADA" not in texto else "Resultado: ") + texto[:300])
        if "guardar_preferencia" in por:
            partes.append("Listo, guardé tu preferencia para las próximas conversaciones.")
        if any(k in por for k in ("predecir_urgencia", "buscar_guias")):
            partes.append("Sugerencia para revisión del clínico: la decisión final es tuya.")
        return AIMessage(content="[modelo simulado]\n" + "\n\n".join(partes))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        time.sleep(self.latencia)
        return ChatResult(generations=[ChatGeneration(message=self._responder(messages))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        await asyncio.sleep(self.latencia)
        return ChatResult(generations=[ChatGeneration(message=self._responder(messages))])
