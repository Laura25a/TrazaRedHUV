"""Evalúa el RAG de producción con preguntas públicas, sin historias de pacientes."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from statistics import mean

from rag import motor
from rag.evaluacion import FUERA


# Referencias redactadas antes de puntuar; cada grupo admite variantes equivalentes.
CASOS = [
    ("remision", "¿Qué debe contener una remisión médica?",
     "Debe incluir identificación del paciente y del remitente, origen y destino, motivo, resumen clínico, diagnóstico, complejidad y urgencia.",
     (("identificación",), ("origen",), ("destino",), ("motivo",), ("resumen clínico",), ("diagnóstico",), ("complejidad",), ("urgencia",))),
    ("contrarreferencia", "¿Qué es la contrarreferencia?",
     "Es la respuesta del receptor que comunica el diagnóstico y el seguimiento al prestador de origen.",
     (("receptor",), ("diagnóstico",), ("seguimiento",), ("origen",))),
    ("qsofa", "¿Cuáles son los tres criterios de qSOFA?",
     "Frecuencia respiratoria de al menos 22 por minuto, presión arterial sistólica de 100 mmHg o menos y alteración del estado mental.",
     (("22",), ("100",), ("estado mental", "conciencia"))),
    ("retencion", "¿Cuántos años se conserva la historia clínica desde la última atención?",
     "El mínimo es quince años desde la última atención según la Resolución 839 de 2017.",
     (("quince años", "15 años"), ("última atención",))),
    ("triage", "¿En cuánto tiempo se debe atender un triage nivel II en Colombia?",
     "El plazo máximo de atención para triage II es de treinta minutos.",
     (("treinta minutos", "30 minutos"),)),
    ("abcde", "¿Qué evalúa el método ABCDE de atención básica de emergencia?",
     "Evalúa vía aérea, respiración, circulación, estado neurológico y exposición.",
     (("vía aérea",), ("respiración",), ("circulación",), ("estado neurológico",), ("exposición",))),
] + [(f"fuera-{i}", pregunta, None, ()) for i, pregunta in enumerate(FUERA, 1)]


def recolectar() -> list[dict]:
    filas = []
    for caso, pregunta, referencia, criterios in CASOS:
        salida = motor.responder(pregunta, None)
        texto = salida["respuesta"]
        faltantes = [opciones[0] for opciones in criterios
                     if not any(opcion.casefold() in texto.casefold() for opcion in opciones)]
        abstencion = salida["modo"] == "abstencion"
        fallas = []
        if (referencia is None) != abstencion:
            fallas.append("abstencion_inesperada" if abstencion else "respuesta_fuera_del_dominio")
        if faltantes:
            fallas.append("respuesta_incompleta")
        filas.append({"caso": caso, "pregunta": pregunta, "referencia": referencia, "respuesta": texto,
                      "modo": salida["modo"], "fuentes": salida["fuentes"],
                      "contextos": salida.get("contextos_recuperados", []),
                      "criterios_faltantes": faltantes, "fallas": fallas})
    return filas


def puntuar_rouge(filas: list[dict]) -> dict:
    from ragas.metrics.collections import RougeScore

    metrica = RougeScore(rouge_type="rougeL")
    for fila in filas:
        if fila["referencia"]:
            fila["rouge_l"] = (0.0 if fila["modo"] == "abstencion" else
                               metrica.score(response=fila["respuesta"], reference=fila["referencia"]).value)
    puntuados = [f["rouge_l"] for f in filas if "rouge_l" in f]
    return {"media": mean(puntuados) if puntuados else None, "total": len(puntuados)}


def puntuar_ragas(filas: list[dict], modelo: str) -> dict:
    # Importaciones optativas. Solo se envían preguntas públicas del banco anterior.
    from ragas import SingleTurnSample, EvaluationDataset, evaluate
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import AspectCritic, Faithfulness, LLMContextPrecisionWithReference, LLMContextRecall
    from ragas.run_config import RunConfig
    from langchain_openai import ChatOpenAI

    metricas = [
        Faithfulness(),
        LLMContextPrecisionWithReference(),
        LLMContextRecall(),
        AspectCritic(name="relevancia", definition="La respuesta aborda de manera directa la pregunta del usuario.",
                     strictness=1),
    ]
    evaluables = [fila for fila in filas if fila["referencia"] and fila["modo"] != "abstencion"]
    datos = EvaluationDataset(samples=[
        SingleTurnSample(user_input=f["pregunta"], response=f["respuesta"],
                         reference=f["referencia"], retrieved_contexts=f["contextos"])
        for f in evaluables
    ])
    if evaluables:
        resultado = evaluate(datos, metrics=metricas, llm=LangchainLLMWrapper(
            ChatOpenAI(model=modelo, temperature=0, timeout=35, max_retries=0)),
            raise_exceptions=False, run_config=RunConfig(max_workers=1, max_retries=1))
        for fila, puntajes in zip(evaluables, resultado.scores, strict=True):
            fila["ragas"] = {m.name: float(puntajes[m.name]) if math.isfinite(puntajes[m.name]) else None
                             for m in metricas}
            if None in fila["ragas"].values():
                fila["fallas"].append("error_del_juez")
    # Las abstenciones incorrectas aportan cero; las correctas se miden por separado.
    for fila in filas:
        if fila["referencia"] and fila["modo"] == "abstencion":
            fila["ragas"] = {m.name: 0.0 for m in metricas}
    resumen = {}
    for metrica in metricas:
        valores = [f["ragas"][metrica.name] for f in filas if "ragas" in f]
        validos = [v for v in valores if v is not None]
        resumen[metrica.name] = {"media": mean(validos) if validos else None,
                                 "puntuados": len(validos), "total": len(valores)}
    return resumen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("salida", type=Path, help="Ruta del informe JSON por caso")
    parser.add_argument("--rouge", action="store_true", help="Calcula ROUGE-L de RAGAS sin juez externo.")
    parser.add_argument("--juez", help="Modelo de ChatOpenAI para RAGAS. Requiere la extra de evaluación.")
    parser.add_argument("--generacion-llm", action="store_true",
                        help="Usa el proveedor configurado para seleccionar pasajes del corpus público.")
    args = parser.parse_args()
    if not args.generacion_llm:
        os.environ.pop("GROQ_API_KEY", None)
    os.environ["RAGAS_DO_NOT_TRACK"] = "true"
    filas = recolectar()
    informe = {"fecha_utc": datetime.now(timezone.utc).isoformat(), "revision_corpus": motor.REVISION_CORPUS,
               "embeddings": motor.EMBEBER.nombre, "casos": filas}
    try:
        if args.rouge:
            informe["resumen_rouge_l"] = puntuar_rouge(filas)
        if args.juez:
            informe["juez_ragas"] = args.juez
            informe["resumen_ragas"] = puntuar_ragas(filas, args.juez)
    finally:
        args.salida.write_text(json.dumps(informe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    total_fallas = sum(bool(fila["fallas"]) for fila in filas)
    print(f"{len(filas)} casos · {total_fallas} con fallas · informe en {args.salida}")


if __name__ == "__main__":
    main()
