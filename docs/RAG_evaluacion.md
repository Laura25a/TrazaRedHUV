# Evaluación de la recuperación del RAG

Generado con `python -m rag.evaluacion` · embeddings en modo **lsa** (`lsa-tfidf-svd39`) · 26 preguntas con documento esperado.

| Método | Recall@1 | Recall@3 | MRR |
|---|---|---|---|
| BM25 | 0.85 | 0.96 | 0.91 |
| Denso | 0.85 | 0.96 | 0.91 |
| Híbrido (RRF) | 0.85 | 0.96 | 0.91 |
| Híbrido + multi-consulta | 0.92 | 0.96 | 0.94 |

## Abstención (umbral BM25 2.5 con 2+ términos compartidos, o coseno ≥ 0.72)

- Preguntas del dominio que sí responde: **26 de 26**
- Preguntas fuera del dominio en las que se abstiene: **5 de 5**

Recall@k: fracción de preguntas cuyo documento correcto aparece entre los k primeros. MRR: promedio de 1/posición del documento correcto (1 = siempre primero).
