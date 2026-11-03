# rag/motor.py · Motor RAG del sistema (Semana 12)
import json, math, os, re, unicodedata
from collections import Counter
from pathlib import Path

RUTA = Path(__file__).parent / "fragmentos.json"
STOP = set("""a al algo con como de del el en entre es esta este esto la las le lo los mas o para pero por que
se sin sobre su sus un una uno y ya cual cuales cuando donde debe deben puede ser son si no hay tiene tienen""".split())


def tokenizar(texto: str) -> list[str]:
    texto = "".join(ch for ch in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(ch) != "Mn")
    return [t for t in re.findall(r"[a-z0-9]+(?:,[0-9]+)?", texto) if t not in STOP and len(t) > 1]


def cargar_fragmentos() -> list[dict]:
    # En el proyecto: SELECT ... FROM rag_chunks (pgvector)
    return json.loads(RUTA.read_text(encoding="utf-8"))


class BM25:
    def __init__(self, textos, k1=1.2, b=0.75):
        self.k1, self.b = k1, b
        self.docs = [tokenizar(t) for t in textos]
        self.N = len(self.docs)
        self.avgdl = sum(map(len, self.docs)) / self.N
        self.tf = [Counter(d) for d in self.docs]
        df = Counter(t for d in self.docs for t in set(d))
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def puntajes(self, pregunta: str) -> list[float]:
        q = tokenizar(pregunta)
        salida = []
        for tf, d in zip(self.tf, self.docs):
            norm = self.k1 * (1 - self.b + self.b * len(d) / self.avgdl)
            salida.append(sum(self.idf[t] * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in q if tf.get(t)))
        return salida


FRAGMENTOS = cargar_fragmentos()
INDICE = BM25([f["texto"] for f in FRAGMENTOS])
UMBRAL_BM25 = float(os.getenv("RAG_UMBRAL_BM25", "2.5"))  # calibrado con corpus TrazaRed (PASO 8.1)


def permitido(f: dict, alias: str | None) -> bool:
    if f["tipo"] in ("guia", "norma"):
        return True
    return f["tipo"] == "historia" and f["estado"] == "aprobado" and alias is not None and f["paciente"] == alias


def buscar(pregunta: str, alias: str | None, k: int = 3) -> list[dict]:
    # Ademas del puntaje se exigen AL MENOS DOS terminos compartidos con el documento:
    # un solo termino raro compartido no basta para recuperar (calibracion del PASO 8.1).
    q = tokenizar(pregunta)
    puntajes = INDICE.puntajes(pregunta)
    orden = sorted(range(len(FRAGMENTOS)), key=lambda i: -puntajes[i])
    elegidos = []
    for i in orden:
        f = FRAGMENTOS[i]
        if not permitido(f, alias) or puntajes[i] < UMBRAL_BM25:
            continue
        if sum(1 for t in q if INDICE.tf[i].get(t)) < 2:
            continue
        elegidos.append(i)
    return [dict(FRAGMENTOS[i], puntaje=round(puntajes[i], 3)) for i in elegidos[:k]]


PROMPT_SISTEMA = (
    "Eres un asistente de apoyo a la decisión clínica para médicos. Responde SOLO con el CONTEXTO, "
    "cita cada afirmación con [n] y, si el contexto no alcanza, di: \"No hay evidencia suficiente en las "
    "fuentes del sistema.\" No inventes valores ni menciones datos personales. Máximo 5 frases."
)
SIN_EVIDENCIA = "No hay evidencia suficiente en las fuentes del sistema."


def construir_mensajes(pregunta: str, fragmentos: list[dict]) -> list[dict]:
    contexto = "\n\n".join(f"[{n}] ({f['fuente']})\n{f['texto']}" for n, f in enumerate(fragmentos, 1))
    return [{"role": "system", "content": PROMPT_SISTEMA},
            {"role": "user", "content": f"CONTEXTO:\n{contexto}\n\nPREGUNTA: {pregunta}"}]


def generar(mensajes: list[dict], fragmentos: list[dict]) -> tuple[str, str]:
    llave = os.getenv("GROQ_API_KEY")
    if llave:
        try:
            import httpx
            r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                           headers={"Authorization": f"Bearer {llave}"},
                           json={"model": os.getenv("GROQ_MODEL", "qwen/qwen3-32b"),
                                 "messages": mensajes, "temperature": 0}, timeout=60)
            r.raise_for_status()
            texto = r.json()["choices"][0]["message"]["content"]
            return re.sub(r"<think>.*?</think>", "", texto, flags=re.S).strip(), "llm"
        except Exception:
            pass
    return " ".join(f"{f['texto'][:220].rsplit(' ', 1)[0]}... [{n}]" for n, f in enumerate(fragmentos, 1)), "extractivo"


def responder(pregunta: str, alias: str | None) -> dict:
    fragmentos = buscar(pregunta, alias)
    if not fragmentos:
        return {"respuesta": SIN_EVIDENCIA, "modo": "abstencion", "fuentes": [], "mensajes": []}
    mensajes = construir_mensajes(pregunta, fragmentos)
    texto, modo = generar(mensajes, fragmentos)
    fuentes = [{"n": n, "chunk_id": f["chunk_id"], "fuente": f["fuente"]} for n, f in enumerate(fragmentos, 1)]
    return {"respuesta": texto, "modo": modo, "fuentes": fuentes, "mensajes": mensajes}
