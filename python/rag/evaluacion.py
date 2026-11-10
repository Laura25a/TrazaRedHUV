# rag/evaluacion.py · Evaluación de la recuperación (semana 12, tarea 3)
#
# Uso, desde la carpeta python/:   python -m rag.evaluacion
# Escribe la tabla en docs/RAG_evaluacion.md y la imprime.
#
# Cada pregunta tiene el documento que DEBERÍA aparecer (ground truth). Mezcla
# preguntas técnicas (como las escribiría el médico) y coloquiales (como las
# escribiría alguien sin vocabulario clínico, o un agente que no reformula).
from pathlib import Path

from rag import motor

EVAL = [
    # Historia clínica y auditoría
    ("¿se puede borrar un dato de la historia clínica?", "G01"),
    ("¿cuántos años se conserva la historia clínica?", "G01"),
    # Remisión
    ("¿qué debe contener una remisión?", "G02"),
    ("¿qué es la contrarreferencia?", "G02"),
    # Traslados y bitácora
    ("¿quién coordina el traslado cuando otra institución rechaza al paciente?", "G03"),
    ("¿qué hay que anotar de cada llamada a otra clínica?", "G03"),
    # EPS e IPS
    ("¿cuál es la diferencia entre EPS e IPS?", "G04"),
    ("¿la aseguradora tiene que autorizar la remisión?", "G04"),
    # Estados
    ("¿qué estados puede tener una remisión?", "G05"),
    # Saturación
    ("urgencias del HUV está saturado, ¿qué se hace?", "G06"),
    # Seguridad
    ("¿qué pasa si me equivoco tres veces con la contraseña?", "G07"),
    ("¿con qué estándar se intercambia información con otros hospitales?", "G07"),
    # Datos personales
    ("¿el paciente necesita autorizar el uso de sus datos de salud?", "G08"),
    # Triage
    ("¿en cuánto tiempo se atiende un triage nivel 2?", "G09"),
    # Sepsis
    ("criterios de qSOFA para sospechar sepsis", "G10"),
    ("paciente con fiebre, confundido y presión de 90", "G10"),
    # Coronario
    ("dolor en el pecho opresivo con sudoración, ¿qué hago primero?", "G11"),
    ("infarto con elevación del ST y no tenemos hemodinamia", "G11"),
    # EVC
    ("se le torció la cara y no puede hablar bien hace una hora", "G12"),
    ("¿hasta cuántas horas se puede hacer trombólisis?", "G12"),
    # Respiratorio
    ("saturación de 85 % y respira muy rápido", "G13"),
    ("¿qué puntaje de NEWS2 indica emergencia?", "G13"),
    # Hipoglucemia
    ("tengo el azúcar muy bajito y me tiemblan las manos", "G14"),
    ("paciente inconsciente con glucosa de 45", "G14"),
    # Norma de IA
    ("¿puedo mandar la cédula del paciente al modelo de IA?", "N01"),
    ("¿quién es responsable de lo que dice la inteligencia artificial?", "N01"),
]

# Preguntas fuera del dominio: el sistema debe abstenerse
FUERA = [
    "¿quién ganó el mundial de fútbol de 2014?",
    "receta de arepa de choclo",
    "¿cuál es la capital de Australia?",
    "recomiéndame una película de terror",
    "¿cómo se cambia el aceite de una moto?",
]

METODOS = ["bm25", "denso", "hibrido", "multiconsulta"]
NOMBRES = {"bm25": "BM25", "denso": "Denso", "hibrido": "Híbrido (RRF)", "multiconsulta": "Híbrido + multi-consulta"}


def evaluar(metodo: str) -> dict:
    corpus = motor._Corpus(alias=None, extra=[])          # sin paciente: solo guías y normas
    r1 = r3 = rr = 0.0
    for pregunta, esperado in EVAL:
        docs = [corpus.frag[i]["doc_id"] for i in corpus.ranking(pregunta, metodo)]
        vistos = list(dict.fromkeys(docs))                 # un documento cuenta una vez
        r1 += esperado in vistos[:1]
        r3 += esperado in vistos[:3]
        rr += 1.0 / (vistos.index(esperado) + 1) if esperado in vistos else 0.0
    n = len(EVAL)
    return {"recall@1": r1 / n, "recall@3": r3 / n, "mrr": rr / n}


def abstencion() -> dict:
    corpus = motor._Corpus(alias=None, extra=[])
    dentro = sum(corpus.hay_evidencia(p) for p, _ in EVAL)
    fuera = sum(not corpus.hay_evidencia(p) for p in FUERA)
    return {"responde_dentro": dentro, "total_dentro": len(EVAL), "se_abstiene_fuera": fuera, "total_fuera": len(FUERA)}


def tabla() -> str:
    filas = ["| Método | Recall@1 | Recall@3 | MRR |", "|---|---|---|---|"]
    resultados = {m: evaluar(m) for m in METODOS}
    for m, r in resultados.items():
        filas.append(f"| {NOMBRES[m]} | {r['recall@1']:.2f} | {r['recall@3']:.2f} | {r['mrr']:.2f} |")
    a = abstencion()
    texto = "\n".join([
        "# Evaluación de la recuperación del RAG",
        "",
        f"Generado con `python -m rag.evaluacion` · embeddings en modo **{motor.MODO_EMBEDDING}** "
        f"(`{motor.EMBEBER.nombre}`) · {len(EVAL)} preguntas con documento esperado.",
        "",
        *filas,
        "",
        "## Abstención (umbral BM25 "
        f"{motor.UMBRAL_BM25} con 2+ términos compartidos, o coseno ≥ {motor.UMBRAL_COS})",
        "",
        f"- Preguntas del dominio que sí responde: **{a['responde_dentro']} de {a['total_dentro']}**",
        f"- Preguntas fuera del dominio en las que se abstiene: **{a['se_abstiene_fuera']} de {a['total_fuera']}**",
        "",
        "Recall@k: fracción de preguntas cuyo documento correcto aparece entre los k primeros. "
        "MRR: promedio de 1/posición del documento correcto (1 = siempre primero).",
    ])
    return texto


if __name__ == "__main__":
    salida = tabla()
    print(salida)
    destino = Path(__file__).resolve().parents[2] / "docs" / "RAG_evaluacion.md"
    destino.write_text(salida + "\n", encoding="utf-8")
    print(f"\nGuardado en {destino}")
