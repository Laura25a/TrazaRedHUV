"""
voz.py — Bitácora por voz: el médico llama, TrazaRed escucha y la IA llena la bitácora.

El problema de raíz (Sentencia T-573 de 2023): las llamadas a otras instituciones no
quedan registradas porque, en medio de la urgencia, nadie tiene tiempo de escribir.
Aquí la llamada se transcribe y la IA propone la anotación (institución, quién
respondió, qué dijo y la causa); el médico la revisa y la confirma. Nada se guarda
sin esa confirmación: la anotación final pasa por la ruta normal de gestiones
(POST /registro/remisiones/{id}/gestiones), con su firma, su hora y su causa.

El dictado se procesa en el dispositivo. La API extrae campos del texto mediante
reglas locales y solo registra metadatos de la operación.
"""
import re
import unicodedata

import cripto
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_db, registrar_auditoria, requiere_rol
from causas import CAUSAS, causa_por_reglas
from seguridad import sanear_para_llm

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
# ─────────────────────────────────────────────── rutas
class Transcripcion(BaseModel):
    remision_id: int
    transcripcion: str = Field(min_length=10, max_length=8000)


@router.post("/bitacora/voz/extraer")
def extraer(body: Transcripcion, db=Depends(get_db), usuario=Depends(requiere_rol("admin", "medico"))):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT r.id, r.paciente_id, r.institucion_destino, p.nombre, p.documento, p.telefono, p.correo, p.direccion
                   FROM remisiones r JOIN pacientes p ON p.id = r.paciente_id WHERE r.id = %s AND r.activo AND (%s OR r.creado_por = %s);""",
                (body.remision_id, usuario["rol"] == "admin", usuario["id"]))
    rem = cripto.descifrar_paciente(cur.fetchone(), "paciente_id")
    cur.close()
    if rem is None:
        raise HTTPException(404, "Remisión no encontrada")
    texto = sanear_para_llm(body.transcripcion, rem)
    conocidas = _instituciones(db)
    institucion, _ = buscar_institucion(texto, conocidas)
    quien, cargo = buscar_quien(texto)
    respuesta = buscar_respuesta(texto)
    quien_txt = " · ".join(x for x in (quien, cargo) if x)
    respuesta_bitacora = (f"{quien_txt}: {respuesta}" if quien_txt else respuesta)[:500]
    causa = causa_por_reglas(respuesta)
    clasificacion = {"causa": causa, "etiqueta": CAUSAS[causa]["etiqueta"],
                     "efecto": CAUSAS[causa]["efecto"], "origen": "reglas"}

    faltan = [n for n, v in (("institución", institucion), ("quién respondió", quien), ("respuesta", respuesta)) if not v]
    registrar_auditoria(db, usuario["id"], "bitacora_voz_extraccion", "remisiones", body.remision_id,
                        f"modo=reglas caracteres={len(body.transcripcion)} institucion={institucion} "
                        f"causa={clasificacion.get('causa')}")
    return {
        "modo": "reglas",
        "transcripcion_seudonimizada": texto,
        "campos": {"institucion_contactada": institucion or rem["institucion_destino"], "medio": "telefono",
                   "contactado_por": usuario["nombre"], "respuesta": respuesta_bitacora},
        "quien_respondio": quien, "cargo": cargo,
        "institucion_detectada": bool(institucion),
        "clasificacion": clasificacion,
        "faltan": faltan,
        "nota": "Propuesta de la IA: revísala y corrígela antes de anotar. Nada se guarda sin tu confirmación.",
    }


@router.post("/bitacora/voz/transcribir", deprecated=True)
def transcribir(usuario=Depends(requiere_rol("admin", "medico"))):
    raise HTTPException(410, "La transcripción de audio se hace en el dispositivo. Usa dictado local o pega el texto.")
