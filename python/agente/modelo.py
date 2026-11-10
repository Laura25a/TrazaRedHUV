# agente/modelo.py · Punto único para elegir el modelo
#
# Orden: Groq (si hay GROQ_API_KEY) → Ollama local (si hay OLLAMA_URL y está
# instalado langchain-ollama) → modelo simulado. El resto del código nunca
# pregunta cuál es: solo llama a crear_modelo().
import os

from agente.simulado import ModeloSimulado


def proveedor() -> str:
    if os.getenv("GROQ_API_KEY"):
        return "groq"
    if os.getenv("OLLAMA_URL"):
        try:
            import langchain_ollama  # noqa: F401
            return "ollama"
        except ImportError:
            pass
    return "simulado"


def nombre_modelo() -> str:
    p = proveedor()
    if p == "groq":
        return os.getenv("MODELO_GROQ", "openai/gpt-oss-120b")
    if p == "ollama":
        return os.getenv("MODELO_OLLAMA", "qwen3:4b")
    return "simulado"


def crear_modelo(latencia_simulada: float = 0.3):
    p = proveedor()
    if p == "groq":
        from langchain_groq import ChatGroq
        extra = {"reasoning_effort": "low"} if nombre_modelo().startswith("openai/gpt-oss") else {}
        return ChatGroq(model=nombre_modelo(), temperature=0, max_retries=2, timeout=30,
                        max_tokens=int(os.getenv("GROQ_MAX_TOKENS", "800")), **extra)
    if p == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=nombre_modelo(), base_url=os.getenv("OLLAMA_URL"), temperature=0,
                          num_predict=600, num_ctx=8192, reasoning=False)
    return ModeloSimulado(latencia=latencia_simulada)
