# agente/herramientas.py · Lo ÚNICO que el agente puede hacer en TrazaRed
#
# Seis reglas que cumplen todas (semana 13, Parte 6):
#   1. El docstring dice cuándo usarla: es lo que lee el modelo.
#   2. El paciente NO es un argumento: lo fija el harness en el contexto. Así el
#      modelo no puede pedir "los datos del paciente 205" teniendo abierto otro.
#   3. Defensa en profundidad: cada herramienta vuelve a revisar el rol.
#   4. Minimización (R12): al modelo solo le llega el seudónimo y variables
#      clínicas; nombre, documento y teléfono se quedan en la base.
#   5. Las de escritura se llaman "proponer_*" y el harness las pausa hasta que
#      el clínico apruebe (R10).
#   6. Reutilizan el camino del sistema (tablas, auditoría, eventos), no inventan uno paralelo.
import json
import uuid
from dataclasses import dataclass
from datetime import date

import psycopg2
import psycopg2.extras
from langchain.tools import ToolRuntime, tool

from auth import PG_CONNECTION_STRING, registrar_auditoria


# ---------- Contexto que fija el harness en cada solicitud ----------
@dataclass
class ContextoClinico:
    usuario: str                       # correo del usuario (sale del token)
    usuario_id: int                    # id del usuario (para auditoría y remisiones)
    rol: str                           # medico, especialista...
    conversacion: str = "principal"
    paciente_id: int | None = None     # id REAL: el modelo nunca lo ve
    paciente_alias: str | None = None  # seudónimo: lo único que circula hacia el modelo


ROLES_CLINICOS = {"medico", "especialista"}
INSTITUCION_ORIGEN = "Hospital Universitario del Valle"
LOINC = {"8867-4": "fc", "8480-6": "sbp", "8462-4": "dbp", "9279-1": "fr",
         "59408-5": "saturacion", "8310-5": "temperatura", "9269-2": "glasgow"}
NOMBRES_SIGNOS = {"fc": "frecuencia cardíaca (lpm)", "sbp": "presión sistólica (mmHg)",
                  "dbp": "presión diastólica (mmHg)", "fr": "frecuencia respiratoria (rpm)",
                  "saturacion": "SpO2 (%)", "temperatura": "temperatura (°C)", "glasgow": "Glasgow"}


def espacio_memoria(usuario: str) -> tuple:
    """Namespace del store: LangGraph no admite puntos, y los usuarios son correos."""
    return ("preferencias", usuario.replace(".", "_").replace("@", "_at_"))


def _db():
    return psycopg2.connect(PG_CONNECTION_STRING, connect_timeout=5)


def _edad(nacimiento) -> int | None:
    if not nacimiento:
        return None
    hoy = date.today()
    return hoy.year - nacimiento.year - ((hoy.month, hoy.day) < (nacimiento.month, nacimiento.day))


def datos_clinicos(paciente_id: int) -> dict | None:
    """Variables clínicas mínimas del paciente (sin identificadores)."""
    conn = _db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT genero, fecha_nacimiento, eps, alergias FROM pacientes WHERE id = %s;", (paciente_id,))
        p = cur.fetchone()
        if p is None:
            return None
        cur.execute("""SELECT id, institucion_destino, motivo, estado, convenio_vigente, fecha_solicitud
                       FROM remisiones WHERE paciente_id = %s AND activo = TRUE
                       ORDER BY fecha_solicitud DESC, id DESC LIMIT 1;""", (paciente_id,))
        rem = cur.fetchone()
        cur.execute("""SELECT o.codigo_loinc, o.valor FROM observaciones o
                       JOIN remisiones r ON r.id = o.remision_id
                       WHERE r.paciente_id = %s
                       ORDER BY o.fecha_observacion DESC, o.id DESC;""", (paciente_id,))
        signos = {}
        for fila in cur.fetchall():
            campo = LOINC.get(fila["codigo_loinc"])
            if campo and campo not in signos:
                signos[campo] = float(fila["valor"])
        cur.execute("""SELECT probabilidad_urgente, critico_segun_umbral, estado FROM reportes_ia
                       WHERE paciente_id = %s AND probabilidad_urgente IS NOT NULL
                       ORDER BY id DESC LIMIT 1;""", (paciente_id,))
        ia = cur.fetchone()
        cur.close()
    finally:
        conn.close()
    return {
        "edad": _edad(p["fecha_nacimiento"]), "sexo": p["genero"], "eps": p["eps"],
        "alergias": p["alergias"] or "sin dato", "signos": signos,
        "remision": None if rem is None else {
            "motivo": rem["motivo"], "estado": rem["estado"], "destino": rem["institucion_destino"],
            "convenio_vigente": rem["convenio_vigente"], "fecha": rem["fecha_solicitud"].isoformat()},
        "ultimo_reporte_ia": None if ia is None else {
            "probabilidad": float(ia["probabilidad_urgente"]), "critico": ia["critico_segun_umbral"],
            "estado": ia["estado"]},
    }


def _verificar(ctx: ContextoClinico, necesita_paciente: bool = True) -> str | None:
    if ctx.rol not in ROLES_CLINICOS:
        return "NO_AUTORIZADO: solo personal clínico (médico o especialista) usa esta herramienta."
    if necesita_paciente and ctx.paciente_id is None:
        return "SIN_PACIENTE: no hay un paciente abierto en esta conversación. Pide al clínico que abra uno."
    return None


# ---------- Herramienta 1 · lectura ----------
@tool
def consultar_paciente(runtime: ToolRuntime[ContextoClinico]) -> str:
    """Devuelve las variables clínicas del paciente abierto: edad, sexo, últimos
    signos vitales (FC, presión, FR, SpO2, temperatura, Glasgow), su remisión actual y el último
    reporte de IA. Úsala cuando la pregunta trate de "este paciente", su estado o sus datos.
    No recibe argumentos: el paciente lo define el sistema, nunca el chat."""
    ctx = runtime.context
    if (error := _verificar(ctx)):
        return error
    d = datos_clinicos(ctx.paciente_id)
    if d is None:
        return "NO_ENCONTRADO: el paciente no existe."
    signos = ", ".join(f"{NOMBRES_SIGNOS[k]} {v:g}" for k, v in d["signos"].items()) or "sin signos registrados"
    rem = d["remision"]
    # Minimización (R12): ni identificadores ni EPS ni alergias viajan al modelo; la EPS se usa
    # por dentro en buscar_ips_con_convenio sin escribirla en el texto.
    texto = f"Paciente {ctx.paciente_alias}: {d['edad']} años, sexo {d['sexo']}. Últimos signos vitales: {signos}."
    if rem:
        texto += (f" Remisión actual: {rem['motivo']} → {rem['destino']}, estado {rem['estado']}, "
                  f"convenio {'vigente' if rem['convenio_vigente'] else 'NO vigente'} ({rem['fecha']}).")
    if d["ultimo_reporte_ia"]:
        r = d["ultimo_reporte_ia"]
        texto += f" Último reporte de IA: probabilidad {r['probabilidad']:.2f}, estado {r['estado']}."
    return texto


# ---------- Herramienta 2 · lectura: el RAG de la semana 12 ----------
@tool
def buscar_guias(pregunta: str, runtime: ToolRuntime[ContextoClinico]) -> str:
    """Busca en las guías clínicas y normas del sistema (triage, sepsis/qSOFA, síndrome coronario,
    EVC, deterioro respiratorio/NEWS2, hipoglucemia, remisión, traslados, EPS/IPS, datos personales)
    y en la historia APROBADA del paciente abierto. Úsala para cualquier criterio, conducta o
    requisito. Escribe la pregunta con términos clínicos. Devuelve fragmentos numerados [1], [2]...;
    cita esos números. Si devuelve SIN_EVIDENCIA, dilo y no inventes."""
    ctx = runtime.context
    if (error := _verificar(ctx, necesita_paciente=False)):
        return error
    from rag.motor import responder
    conn = _db() if ctx.paciente_id is not None else None
    try:
        r = responder(pregunta, ctx.paciente_alias, paciente_id=ctx.paciente_id, db=conn)
    finally:
        if conn is not None:
            conn.close()
    if not r["fuentes"]:
        return "SIN_EVIDENCIA: " + r["respuesta"]
    lineas = ["Fuentes:"]
    for f in r["fuentes"]:
        lineas.append(f"[{f['n']}] ({f['chunk_id']}) {f['fuente']}: {f['texto'][:320]}")
    return "\n".join(lineas)


# ---------- Herramienta 3 · lectura: el modelo de urgencia (XGBoost) ----------
def _mental_desde_glasgow(g: float | None) -> int:
    if g is None or g >= 15:
        return 1                 # alerta
    if g >= 13:
        return 2                 # responde a la voz
    if g >= 9:
        return 3                 # responde al dolor
    return 4                     # sin respuesta


@tool
def predecir_urgencia(runtime: ToolRuntime[ContextoClinico]) -> str:
    """Estima con el modelo XGBoost la probabilidad de que el paciente abierto sea urgente
    (KTAS 1 a 3), usando sus últimos signos vitales. No guarda nada en la historia: es una
    consulta. Úsala cuando pregunten qué tan grave está, su riesgo o su prioridad.
    No recibe argumentos."""
    ctx = runtime.context
    if (error := _verificar(ctx)):
        return error
    d = datos_clinicos(ctx.paciente_id)
    if d is None:
        return "NO_ENCONTRADO: el paciente no existe."
    s = d["signos"]
    if not s:
        return "SIN_DATOS: el paciente no tiene signos vitales registrados; no se puede estimar la urgencia."
    import ia
    payload = {"id_seudonimo": ia.seudonimo(ctx.paciente_id), "sexo": ia.MAPA_SEXO.get(d["sexo"]),
               "edad": d["edad"], "pacientes_por_hora": None, "modo_llegada": 2, "lesion": 1,
               "mental": _mental_desde_glasgow(s.get("glasgow")), "dolor": 0, "nrs_dolor": None,
               "sbp": s.get("sbp"), "dbp": s.get("dbp"), "fc": s.get("fc"), "fr": s.get("fr"),
               "temperatura": s.get("temperatura"), "saturacion": s.get("saturacion")}
    try:
        r = ia.llamar_modelo(payload)
    except Exception as e:
        return f"MODELO_NO_DISPONIBLE: {getattr(e, 'detail', e)}"
    nivel = "CRÍTICO" if r["critico"] else ("URGENTE" if r["urgente"] else "NO URGENTE")
    return (f"Modelo de urgencia (XGBoost {r.get('version_modelo', '')}) para {ctx.paciente_alias}: "
            f"probabilidad {r['probabilidad']:.3f} → {nivel} (umbral urgente 0,4; crítico 0,9). "
            f"Es apoyo a la decisión, no un diagnóstico; no se guardó en la historia.")


# ---------- Herramienta 4 · lectura: instituciones con convenio ----------
@tool
def buscar_ips_con_convenio(servicio: str, runtime: ToolRuntime[ContextoClinico]) -> str:
    """Lista las instituciones (IPS) a las que conviene remitir al paciente abierto según la EPS
    del paciente: cuántas remisiones de esa EPS tuvieron convenio vigente y cuántas fueron
    aceptadas, con base en el historial del sistema. `servicio` es opcional (por ejemplo "UCI",
    "hemodinamia", "neurocirugía"); si se da, también cuenta casos similares. Úsala antes de proponer una remisión."""
    ctx = runtime.context
    if (error := _verificar(ctx)):
        return error
    d = datos_clinicos(ctx.paciente_id)
    if d is None:
        return "NO_ENCONTRADO: el paciente no existe."
    conn = _db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT r.institucion_destino AS ips,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE r.convenio_vigente) AS con_convenio,
                   COUNT(*) FILTER (WHERE r.estado IN ('aceptada', 'completada')) AS aceptadas,
                   COUNT(*) FILTER (WHERE %(servicio)s <> '' AND r.motivo ILIKE '%%' || %(servicio)s || '%%') AS similares
            FROM remisiones r JOIN pacientes p ON p.id = r.paciente_id
            WHERE p.eps = %(eps)s AND r.activo = TRUE AND r.institucion_destino <> %(origen)s
            GROUP BY r.institucion_destino
            ORDER BY con_convenio DESC, aceptadas DESC, total DESC
            LIMIT 5;""", {"eps": d["eps"], "origen": INSTITUCION_ORIGEN, "servicio": (servicio or "").strip()})
        filas = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    if not filas:
        return "SIN_HISTORIAL: no hay remisiones previas de afiliados de la EPS del paciente para comparar."
    lineas = ["Instituciones para afiliados de la EPS del paciente (historial del sistema):"]
    for n, f in enumerate(filas, 1):
        tasa = round(100 * f["aceptadas"] / f["total"]) if f["total"] else 0
        extra = f", {f['similares']} casos de {servicio}" if servicio else ""
        lineas.append(f"{n}. {f['ips']} — {f['con_convenio']} de {f['total']} con convenio vigente, "
                      f"aceptación {tasa} %{extra}")
    lineas.append("El convenio real se confirma por teléfono antes del traslado y queda en la bitácora.")
    return "\n".join(lineas)


# ---------- Herramienta 5 · ESCRITURA (requiere aprobación humana) ----------
@tool
def proponer_remision(institucion_destino: str, motivo: str, prioridad: str,
                      runtime: ToolRuntime[ContextoClinico]) -> str:
    """Propone radicar una remisión del paciente abierto desde el HUV hacia `institucion_destino`
    con un `motivo` clínico y una `prioridad` (critica, urgente o programada). Úsala solo cuando
    los datos del paciente y las guías lo justifiquen, y preferiblemente hacia una institución con
    convenio. El harness la PAUSA: la remisión solo se crea si el médico la aprueba."""
    ctx = runtime.context
    if ctx.rol != "medico":
        return "NO_AUTORIZADO: solo el médico tratante radica remisiones (R08)."
    if ctx.paciente_id is None:
        return "SIN_PACIENTE: abre un paciente antes de remitir."
    prioridad = (prioridad or "urgente").lower()
    motivo_final = f"[{prioridad.upper()}] {motivo.strip()[:400]} (propuesta del asistente, aprobada por el médico)"
    conn = _db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT bool_or(r.convenio_vigente) AS convenio
                       FROM remisiones r JOIN pacientes p ON p.id = r.paciente_id
                       WHERE r.institucion_destino = %s
                         AND p.eps = (SELECT eps FROM pacientes WHERE id = %s);""",
                    (institucion_destino, ctx.paciente_id))
        convenio = bool((cur.fetchone() or {}).get("convenio"))
        cur.execute("""INSERT INTO remisiones (paciente_id, institucion_origen, institucion_destino,
                                               fecha_solicitud, motivo, estado, convenio_vigente, creado_por)
                       VALUES (%s, %s, %s, CURRENT_DATE, %s, 'pendiente', %s, %s) RETURNING id;""",
                    (ctx.paciente_id, INSTITUCION_ORIGEN, institucion_destino, motivo_final, convenio, ctx.usuario_id))
        remision_id = cur.fetchone()["id"]
        cur.execute("""UPDATE agente_propuestas SET estado = 'aprobada', decidida_en = now(), remision_id = %s
                       WHERE id = (SELECT id FROM agente_propuestas
                                   WHERE usuario_id = %s AND conversacion = %s AND estado = 'pendiente'
                                   ORDER BY id DESC LIMIT 1);""",
                    (remision_id, ctx.usuario_id, ctx.conversacion))
        conn.commit()
        cur.close()
        registrar_auditoria(conn, ctx.usuario_id, "remision_creada", "remisiones", remision_id,
                            f"desde el agente (aprobada por el médico) paciente={ctx.paciente_alias} "
                            f"destino={institucion_destino} prioridad={prioridad}")
    finally:
        conn.close()
    try:                                          # mismo aviso en vivo que una remisión normal (R19)
        from eventos import ids_con_rol, publicar
        conn = _db()
        destinatarios = ids_con_rol(conn, "admin")
        conn.close()
        publicar(destinatarios, {"tipo": "remision_creada", "remision_id": remision_id,
                                 "paciente_id": ctx.paciente_id,
                                 "mensaje": f"Remisión radicada desde el asistente hacia {institucion_destino}"})
    except Exception:
        pass
    radicado = f"REM-{date.today().year}-{remision_id:06d}"
    return (f"REMISION_CREADA radicado={radicado} paciente={ctx.paciente_alias} destino={institucion_destino} "
            f"prioridad={prioridad} convenio={'vigente' if convenio else 'por confirmar'}. "
            f"Quedó en el libro de radicación en estado pendiente.")


# ---------- Herramienta 6 · skills bajo demanda ----------
@tool
def cargar_skill(nombre: str) -> str:
    """Carga el procedimiento completo de una skill de la lista 'Skills disponibles'.
    Úsala antes de una tarea que coincida con la descripción de una skill y sigue sus pasos."""
    from agente.skills import cargar
    return cargar(nombre)


# ---------- Herramienta 7 · memoria de largo plazo ----------
@tool
def guardar_preferencia(preferencia: str, runtime: ToolRuntime[ContextoClinico]) -> str:
    """Guarda una preferencia de trabajo del usuario (formato, nivel de detalle, idioma).
    Úsala cuando el usuario diga "recuerda que..." o "prefiero...".
    Nunca guardes datos de pacientes con esta herramienta."""
    if runtime.store is None:
        return "SIN_MEMORIA: este agente no tiene memoria de largo plazo."
    clave = "pref-" + uuid.uuid4().hex[:6]
    runtime.store.put(espacio_memoria(runtime.context.usuario), clave, {"texto": preferencia[:300]})
    return "PREFERENCIA_GUARDADA"


HERRAMIENTAS = [consultar_paciente, buscar_guias, predecir_urgencia, buscar_ips_con_convenio,
                proponer_remision, cargar_skill, guardar_preferencia]
# Si una herramienta falla, el middleware Auditoria (harness.py) le devuelve al modelo
# "ERROR_HERRAMIENTA: ..." como resultado, en vez de tumbar la respuesta con un Error 500.

CATALOGO = [  # para docs/AGENTE.md y para la interfaz
    {"nombre": "consultar_paciente", "tipo": "lectura", "roles": "médico, especialista", "aprobacion": False},
    {"nombre": "buscar_guias", "tipo": "lectura (RAG)", "roles": "médico, especialista", "aprobacion": False},
    {"nombre": "predecir_urgencia", "tipo": "lectura (XGBoost)", "roles": "médico, especialista", "aprobacion": False},
    {"nombre": "buscar_ips_con_convenio", "tipo": "lectura", "roles": "médico, especialista", "aprobacion": False},
    {"nombre": "proponer_remision", "tipo": "escritura", "roles": "médico", "aprobacion": True},
    {"nombre": "cargar_skill", "tipo": "lectura", "roles": "médico, especialista", "aprobacion": False},
    {"nombre": "guardar_preferencia", "tipo": "memoria", "roles": "médico, especialista", "aprobacion": False},
]


def describir(args: dict) -> str:
    return json.dumps(args, ensure_ascii=False)[:300]
