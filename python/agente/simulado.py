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


def _limpio(contenido) -> str:
    """Quita las marcas <datos>…</datos> con las que el harness entrega cada resultado."""
    return re.sub(r"^<datos[^>]*>\n?|\n?</datos>$", "", str(contenido or "").strip())


# Qué signo cuenta como alterado (para nombrar solo lo que explica la conclusión)
_SIGNOS = [
    (r"SpO2 \(%\) ([\d.]+)", lambda v: v < 92, "SpO2 {:g} %"),
    (r"frecuencia respiratoria \(rpm\) ([\d.]+)", lambda v: v > 22 or v < 10, "FR {:g}/min"),
    (r"presión sistólica \(mmHg\) ([\d.]+)", lambda v: v < 90 or v > 180, "PAS {:g} mmHg"),
    (r"frecuencia cardíaca \(lpm\) ([\d.]+)", lambda v: v > 110 or v < 50, "FC {:g} lpm"),
    (r"temperatura \(°C\) ([\d.]+)", lambda v: v >= 38.3 or v < 36, "T {:g} °C"),
    (r"Glasgow ([\d.]+)", lambda v: v < 15, "Glasgow {:g}"),
]


def _alterados(texto: str) -> list[str]:
    fuera = []
    for patron, es_anormal, plantilla in _SIGNOS:
        m = re.search(patron, texto)
        if m and es_anormal(float(m.group(1))):
            fuera.append(plantilla.format(float(m.group(1))).replace(".", ","))
    return fuera


def _frase(texto: str, largo: int = 210) -> str:
    """Primera idea completa de un fragmento, sin cortarla a media palabra."""
    texto = " ".join(texto.split())
    titulo = re.match(r"^((?:\S+ ){0,5}\S+)\. (?=[A-ZÁÉÍÓÚ])", texto)     # "Deterioro respiratorio. Los signos..."
    if titulo and len(texto) > len(titulo.group(0)) + 40:
        texto = texto[len(titulo.group(0)):]
    if len(texto) <= largo:
        return texto.rstrip(".") + "."
    corte = texto[:largo]
    punto = max(corte.rfind(". "), corte.rfind("; "))
    return (corte[:punto] if punto > 80 else corte[:corte.rfind(" ")]).rstrip(".,;") + "…"


def _redactar(pregunta: str, por: dict) -> str:
    """Respuesta corta, como la daría un colega: primero la conclusión, luego el dato
    que la sustenta con su cita. Nada de contar qué herramientas se usaron."""
    for texto in por.values():
        if texto.startswith("SIN_PACIENTE"):
            return "Primero abre un paciente (búscalo por nombre o cédula) y te respondo sobre él."
        if texto.startswith("NO_AUTORIZADO"):
            return "Tu rol no puede consultar esa información."
    if "proponer_remision" in por:
        t = por["proponer_remision"]
        if "REMISION_CREADA" in t:
            rad = re.search(r"radicado=(\S+)", t)
            dest = re.search(r"destino=(.+?) prioridad=(\w+)", t)
            return (f"Listo: quedó radicada{' ' + rad.group(1) if rad else ''}"
                    f"{' hacia ' + dest.group(1) + ', prioridad ' + dest.group(2).replace('critica', 'crítica') if dest else ''}. "
                    "Está en el libro como pendiente; falta confirmar el cupo por teléfono y anotarlo en la bitácora.")
        if t.startswith("POLITICA") or t.startswith("NO_"):
            return t.split(": ", 1)[-1]
        return "No radiqué la remisión. Dime qué conducta prefieres y la ajusto."

    partes = []
    pred = por.get("predecir_urgencia", "")
    nivel = re.search(r"probabilidad ([0-9.,]+) → (CRÍTICO|URGENTE|NO URGENTE)", pred)
    cp = por.get("consultar_paciente", "")
    alterados = _alterados(cp)
    if nivel:
        prob = float(nivel.group(1).replace(",", "."))
        pct = min(round(prob * 100), 99) if prob < 1 else 100
        titulo = {"CRÍTICO": "Está crítico", "URGENTE": "Es urgente", "NO URGENTE": "No se ve urgente por ahora"}[nivel.group(2)]
        partes.append(f"{titulo}: el modelo estima {pct} % de probabilidad de urgencia."
                      + (f" Lo que más pesa: {', '.join(alterados)}." if alterados else ""))
    elif pred.startswith("SIN_DATOS"):
        partes.append("No tiene signos vitales registrados, así que no puedo estimar la urgencia.")
    elif pred.startswith("MODELO_"):
        partes.append("El modelo de urgencia no está disponible en este momento.")
    elif cp and not cp.startswith("NO_"):
        quien = re.search(r": (\d+) años, sexo (\w+)", cp)
        rem = re.search(r"Remisión actual: (.+?) → (.+?), estado (\w+)", cp)
        partes.append((f"{quien.group(1)} años, sexo {quien.group(2)}. " if quien else "")
                      + (f"Alterado: {', '.join(alterados)}." if alterados else "Signos vitales sin alteraciones de alarma."))
        if rem:
            partes.append(f"Remisión: {_frase(rem.group(1), 90).rstrip('.…')} hacia {rem.group(2)} ({rem.group(3).replace('_', ' ')}).")

    guias = por.get("buscar_guias", "")
    fuentes = re.findall(r"^\[(\d+)\] \(([^)]+)\) ([^:\n]+): (.*)$", guias, re.M)
    if fuentes:
        n, _, fuente, texto = fuentes[0]
        partes.append(f"{'Conducta según' if nivel else 'Según'} {fuente.strip()}: {_frase(texto)} [{n}]")
        if not nivel and len(fuentes) > 1:
            n2, _, fuente2, texto2 = fuentes[1]
            partes.append(f"Además ({fuente2.strip()}): {_frase(texto2, 150)} [{n2}]")
    elif guias.startswith("SIN_EVIDENCIA") and not partes:
        partes.append("No encontré soporte para eso en las guías del sistema; prefiero no inventar una respuesta.")

    ips = re.findall(r"^\d+\. (.+?) — (.+)$", por.get("buscar_ips_con_convenio", ""), re.M)
    if ips:
        partes.append(f"Para su EPS la mejor opción es {ips[0][0]} ({ips[0][1]})."
                      + (f" Alternativas: {', '.join(n for n, _ in ips[1:3])}." if len(ips) > 1 else ""))
    elif por.get("buscar_ips_con_convenio", "").startswith("SIN_HISTORIAL"):
        partes.append("No hay historial de remisiones de su EPS para sugerir una institución.")

    if "guardar_preferencia" in por:
        partes.append("Listo, lo tendré en cuenta en las próximas conversaciones.")
    if nivel or ips:
        partes.append("La decisión es tuya.")
    return " ".join(partes) or "No encontré información para responder eso."


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
        # "¿a dónde lo remito?" es una PREGUNTA: se responde con las instituciones y no se
        # propone nada. Solo una orden ("remítelo", "remite este paciente a UCI") arma la propuesta.
        solo_destino = remitir and re.search(r"a d[oó]nde|d[oó]nde|a qu[eé] |cu[aá]l(es)? |qu[eé] (instituci|cl[ií]nica|ips|hospital)", t)
        if solo_destino:
            usar("buscar_ips_con_convenio", {"servicio": _servicio(t)})
            return plan
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
            usar("guardar_preferencia", {"preferencia": "detallada" if "detallad" in t else "lista" if "lista" in t else "breve"})
        return plan

    @staticmethod
    def _completar_remision(pregunta: str, resultados: list) -> dict:
        por_nombre = {m.name: _limpio(m.content) for m in resultados}
        ips = re.search(r"^1\. (.+?) —", por_nombre.get("buscar_ips_con_convenio", ""), re.M)
        pred = por_nombre.get("predecir_urgencia", "")
        prioridad = "critica" if "CRÍTICO" in pred else ("urgente" if "URGENTE" in pred and "NO URGENTE" not in pred else "programada")
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
                "Puedo decirte qué tan grave está el paciente abierto, qué dicen las guías, a qué institución "
                "conviene remitirlo según su EPS y dejarte lista la remisión para que la apruebes. ¿Qué necesitas?"))
        return AIMessage(content=_redactar(pregunta, {m.name: _limpio(m.content) for m in resultados}))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        time.sleep(self.latencia)
        return ChatResult(generations=[ChatGeneration(message=self._responder(messages))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        await asyncio.sleep(self.latencia)
        return ChatResult(generations=[ChatGeneration(message=self._responder(messages))])
