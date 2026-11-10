"""
voz.py — Bitácora por voz: el médico llama, TrazaRed escucha y la IA llena la bitácora.

El problema de raíz (Sentencia T-573 de 2023): las llamadas a otras instituciones no
quedan registradas porque, en medio de la urgencia, nadie tiene tiempo de escribir.
Aquí la llamada se transcribe y la IA propone la anotación (institución, quién
respondió, qué dijo y la causa); el médico la revisa y la confirma. Nada se guarda
sin esa confirmación: la anotación final pasa por la ruta normal de gestiones
(POST /registro/remisiones/{id}/gestiones), con su firma, su hora y su causa.

  POST /bitacora/voz/transcribir   audio → texto (Whisper en Groq, si hay GROQ_API_KEY).
                                   Sin llave, el navegador transcribe (dictado de Chrome/Edge).
  POST /bitacora/voz/extraer       texto de la llamada → campos de la bitácora propuestos.

Privacidad (Ley 1581): antes de que el texto vaya a cualquier modelo se quitan el
nombre, el documento y los teléfonos del paciente. La transcripción no se guarda:
en la auditoría queda solo que se usó el dictado, su longitud y el resultado.
"""
import json
import os
import re
import unicodedata

import httpx
import psycopg2.extras
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from auth import get_db, registrar_auditoria, requiere_rol
from causas import CAUSAS
from registro import clasificar_respuesta

router = APIRouter(tags=["Bitácora por voz"])

ORIGEN = "Hospital Universitario del Valle"
GENERICAS = {"clinica", "hospital", "fundacion", "centro", "medico", "de", "del", "la", "el", "los", "las",
             "san", "santa", "y", "e", "ips", "esp", "sede", "cali", "valle", "universitario", "departamental",
             "nuestra", "senora"}
TITULOS = r"(?:el\s+|la\s+)?(?:doctor|doctora|dr\.?|dra\.?|enfermer[oa](?:\s+jefe)?|jefe|coordinador[a]?|auxiliar|m[eé]dic[oa])"
CARGOS = re.compile(r"(coordinador[a]?\s+de\s+[a-záéíóúñ ]{2,30}?|jefe\s+de\s+[a-záéíóúñ ]{2,30}?|m[eé]dic[oa]\s+de\s+turno|de\s+referencia|"
                    r"enfermer[oa]\s+jefe|oficina\s+de\s+referencia|referencia\s+y\s+contrarreferencia)(?=[,.;]|\s+de\s+la|\s+del|\s+en|$)",
                    re.IGNORECASE)
NO_SON_NOMBRES = {"turno", "referencia", "urgencias", "uci", "la", "el", "de", "del", "hospital", "clinica", "clínica",
                  "fundacion", "fundación", "buenas", "buenos", "tardes", "dias", "días", "noches", "si", "sí", "aqui", "aquí"}


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t.lower()).strip()


# ─────────────────────────────────────────────── seudonimizar antes de la IA
def seudonimizar(texto: str, paciente: dict | None) -> str:
    salida = texto
    salida = re.sub(r"\b\d[\d .-]{6,12}\d\b", lambda m: "[TELÉFONO]" if len(re.sub(r"\D", "", m.group())) == 10
                    and re.sub(r"\D", "", m.group()).startswith("3") else "[NÚMERO]", salida)
    if paciente:
        doc = str(paciente.get("documento") or "")
        if doc:
            salida = salida.replace(doc, "[DOCUMENTO]")
        partes = [p for p in str(paciente.get("nombre") or "").split() if len(p) >= 3]
        for frase in [" ".join(partes), " ".join(partes[:2]), " ".join(partes[:1] + partes[1:2])] + partes:
            if frase:
                salida = re.sub(rf"\b{re.escape(frase)}\b", "[PACIENTE]", salida, flags=re.IGNORECASE)
        salida = re.sub(r"(\[PACIENTE\]\s*){2,}", "[PACIENTE] ", salida)
    return salida


# ─────────────────────────────────────────────── extracción por reglas
def _instituciones(db) -> list[str]:
    cur = db.cursor()
    cur.execute("""SELECT DISTINCT institucion_destino FROM remisiones
                   UNION SELECT DISTINCT institucion_origen FROM remisiones;""")
    nombres = sorted({f[0] for f in cur.fetchall() if f[0]})
    cur.close()
    return nombres


def buscar_institucion(texto: str, conocidas: list[str]) -> tuple[str | None, float]:
    """La institución mencionada que NO es el HUV (quien llama), por sus palabras distintivas."""
    t = " " + _norm(texto) + " "
    mejor, puntaje = None, 0.0
    for nombre in conocidas:
        if _norm(nombre) == _norm(ORIGEN):
            continue
        claves = [w for w in re.findall(r"[a-z0-9]+", _norm(nombre)) if w not in GENERICAS and len(w) > 2]
        if not claves:
            continue
        presentes = [w for w in claves if f" {w} " in t or f" {w}," in t or f" {w}." in t]
        if len(presentes) == len(claves):
            s = len(claves) + (1.0 if _norm(nombre) in t else 0)
            if s > puntaje:
                mejor, puntaje = nombre, s
    return mejor, (1.0 if mejor else 0.0)


def buscar_quien(texto: str) -> tuple[str | None, str | None]:
    """Quién respondió del otro lado: 'habla la doctora Paola Muñoz, coordinadora de UCI…'."""
    patron = re.compile(rf"(?:habla|le\s+habla|le\s+contesta|contesta|soy|con\s+usted|me\s+comunico\s+con|atiende)\s+"
                        rf"({TITULOS}\s+)?([a-záéíóúñ]+(?:\s+[a-záéíóúñ]+)?)", re.IGNORECASE)
    for m in patron.finditer(texto):
        titulo = (m.group(1) or "").strip()
        palabras = [w for w in m.group(2).split() if w.lower() not in NO_SON_NOMBRES]
        if not palabras or (not titulo and not palabras[0][0].isupper()):
            continue
        if _norm(palabras[0]) in ("el", "la", "hospital", "clinica"):
            continue
        nombre = " ".join(w.capitalize() for w in palabras[:2])
        titulo = {"dr": "Dr.", "dr.": "Dr.", "dra": "Dra.", "dra.": "Dra.", "doctor": "Dr.", "doctora": "Dra."}.get(
            _norm(titulo).replace("el ", "").replace("la ", ""), titulo.replace("el ", "").replace("la ", "").capitalize())
        cargo = CARGOS.search(texto[m.end():m.end() + 120])
        return (f"{titulo} {nombre}".strip(), cargo.group(1).strip() if cargo else None)
    cargo = CARGOS.search(texto)
    return None, (cargo.group(1).strip() if cargo else None)


PALABRAS_RESPUESTA = re.compile(r"cama|cupo|uci|convenio|autoriz|eps|acept|recib|especialista|disponib|lleno|"
                                r"historia|documento|epicrisis|llame|llamar|intente|espera|pendiente|revis|valora|"
                                r"contrato|no tenemos|no hay|s[ií] tenemos|pueden enviar|env[ií]enlo|ambulancia",
                                re.IGNORECASE)


def buscar_respuesta(texto: str) -> str:
    """Las frases con la decisión de la institución (no la presentación del caso del HUV)."""
    frases = [f.strip(" -—") for f in re.split(r"(?<=[.!?¿])\s+|\n+|\s+—\s+", texto) if f.strip(" -—")]
    # La presentación de quien llama suele ir primero ("le hablo del HUV, necesito remitir...")
    candidatas = [f for f in frases if PALABRAS_RESPUESTA.search(f)
                  and not re.search(r"le hablo|necesito remitir|quisiera remitir|tengo un paciente|les llamo|llamo del", f, re.I)
                  and not re.search(r"\b(habla|le contesta|me comunico con|soy)\b", f, re.I)]   # presentación, no decisión
    if not candidatas:
        candidatas = frases[-2:]
    respuesta = " ".join(candidatas[:3])
    return respuesta[:480]


# ─────────────────────────────────────────────── extracción con LLM (opcional)
PROMPT = """Eres el asistente de la Oficina de Referencia del Hospital Universitario del Valle (HUV).
Te doy la transcripción de una llamada del HUV a otra institución para remitir un paciente.
Extrae SOLO lo que se dijo. Devuelve JSON con estas claves:
 "institucion": la institución que RESPONDE (nunca el HUV), tal como se dijo, o null;
 "quien_respondio": nombre y título de quien respondió, o null;
 "cargo": su cargo, o null;
 "respuesta": en una o dos frases, qué respondió la institución (decisión y motivo);
 "decision": una de: acepta, sin_cama, sin_especialista, problema_eps, pide_documentos, en_espera, sin_respuesta, otra.
No inventes datos. El texto ya viene sin datos personales del paciente."""


def extraer_con_llm(texto: str) -> dict | None:
    llave = os.getenv("GROQ_API_KEY")
    if not llave:
        return None
    try:
        r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                       headers={"Authorization": f"Bearer {llave}"},
                       json={"model": os.getenv("MODELO_GROQ", "openai/gpt-oss-120b"), "temperature": 0,
                             "response_format": {"type": "json_object"},
                             "messages": [{"role": "system", "content": PROMPT},
                                          {"role": "user", "content": texto[:4000]}]}, timeout=40)
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as e:
        print(f"[voz] el LLM no pudo extraer: {type(e).__name__}")
        return None


# ─────────────────────────────────────────────── rutas
class Transcripcion(BaseModel):
    remision_id: int
    transcripcion: str = Field(min_length=10, max_length=8000)


@router.post("/bitacora/voz/extraer")
def extraer(body: Transcripcion, db=Depends(get_db), usuario=Depends(requiere_rol("admin", "medico"))):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT r.id, r.institucion_destino, p.nombre, p.documento, p.telefono
                   FROM remisiones r JOIN pacientes p ON p.id = r.paciente_id WHERE r.id = %s;""", (body.remision_id,))
    rem = cur.fetchone()
    cur.close()
    if rem is None:
        raise HTTPException(404, "Remisión no encontrada")
    texto = seudonimizar(body.transcripcion, rem)           # 1. nada personal sale de aquí
    if rem.get("telefono"):
        texto = texto.replace(str(rem["telefono"]), "[TELÉFONO]")

    conocidas = _instituciones(db)
    institucion, _ = buscar_institucion(texto, conocidas)    # 2. reglas (siempre)
    quien, cargo = buscar_quien(texto)
    respuesta = buscar_respuesta(texto)
    modo = "reglas"

    llm = extraer_con_llm(texto)                              # 3. LLM, si hay llave
    if llm:
        modo = "llm"
        if llm.get("institucion"):
            institucion = buscar_institucion(llm["institucion"], conocidas)[0] or llm["institucion"]
        quien = llm.get("quien_respondio") or quien
        cargo = llm.get("cargo") or cargo
        respuesta = (llm.get("respuesta") or respuesta)[:480]

    quien_txt = " · ".join(x for x in (quien, cargo) if x)
    respuesta_bitacora = (f"{quien_txt}: {respuesta}" if quien_txt else respuesta)[:500]
    clasificacion = clasificar_respuesta(respuesta)           # 4. la causa, con el mismo clasificador del libro
    if llm and llm.get("decision") in CAUSAS and clasificacion.get("causa") == "otra":
        c = llm["decision"]
        clasificacion = {"causa": c, "etiqueta": CAUSAS[c]["etiqueta"], "efecto": CAUSAS[c]["efecto"], "origen": "llm"}

    faltan = [n for n, v in (("institución", institucion), ("quién respondió", quien), ("respuesta", respuesta)) if not v]
    registrar_auditoria(db, usuario["id"], "bitacora_voz_extraccion", "remisiones", body.remision_id,
                        f"modo={modo} caracteres={len(body.transcripcion)} institucion={institucion} "
                        f"causa={clasificacion.get('causa')}")
    return {
        "modo": modo,
        "transcripcion_seudonimizada": texto,
        "campos": {"institucion_contactada": institucion or rem["institucion_destino"], "medio": "telefono",
                   "contactado_por": usuario["nombre"], "respuesta": respuesta_bitacora},
        "quien_respondio": quien, "cargo": cargo,
        "institucion_detectada": bool(institucion),
        "clasificacion": clasificacion,
        "faltan": faltan,
        "nota": "Propuesta de la IA: revísala y corrígela antes de anotar. Nada se guarda sin tu confirmación.",
    }


@router.post("/bitacora/voz/transcribir")
async def transcribir(audio: UploadFile = File(...), remision_id: int = Form(...), db=Depends(get_db),
                      usuario=Depends(requiere_rol("admin", "medico"))):
    """Audio de la llamada → texto con Whisper (Groq). El audio no se guarda."""
    llave = os.getenv("GROQ_API_KEY")
    if not llave:
        raise HTTPException(503, "La transcripción del audio necesita GROQ_API_KEY. Mientras tanto usa el dictado "
                                 "del navegador (Chrome o Edge) o escribe/pega lo que se dijo.")
    datos = await audio.read()
    if len(datos) > 24 * 1024 * 1024:
        raise HTTPException(413, "El audio supera 24 MB")
    try:
        r = httpx.post("https://api.groq.com/openai/v1/audio/transcriptions",
                       headers={"Authorization": f"Bearer {llave}"},
                       files={"file": (audio.filename or "llamada.webm", datos, audio.content_type or "audio/webm")},
                       data={"model": os.getenv("MODELO_WHISPER", "whisper-large-v3-turbo"), "language": "es",
                             "response_format": "json"}, timeout=120)
        r.raise_for_status()
        texto = r.json().get("text", "").strip()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"No se pudo transcribir el audio: {type(e).__name__}")
    registrar_auditoria(db, usuario["id"], "bitacora_voz_transcripcion", "remisiones", remision_id,
                        f"audio {len(datos) // 1024} KB · {len(texto)} caracteres (el audio no se guarda)")
    return {"texto": texto}
