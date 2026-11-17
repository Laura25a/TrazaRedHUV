"""Reglas de entrada compartidas entre la API, el registro y los modelos."""
from dataclasses import dataclass
from unicodedata import normalize


PAS, PAD = "8480-6", "8462-4"


@dataclass(frozen=True)
class Signo:
    campo: str
    tipo: str
    unidad: str
    minimo: float
    maximo: float
    alias_unidad: tuple[str, ...] = ()


SIGNOS = {
    "8867-4": Signo("fc", "Frecuencia cardiaca", "/min", 20, 250, ("lat/min", "lpm")),
    PAS: Signo("pas", "Presión arterial sistólica", "mm[Hg]", 40, 300, ("mmHg",)),
    PAD: Signo("pad", "Presión arterial diastólica", "mm[Hg]", 20, 200, ("mmHg",)),
    "9279-1": Signo("fr", "Frecuencia respiratoria", "/min", 4, 80, ("resp/min", "rpm")),
    "59408-5": Signo("spo2", "Saturación de oxígeno", "%", 0, 100),
    "8310-5": Signo("temp", "Temperatura corporal", "Cel", 30, 45, ("°C",)),
    "9269-2": Signo("glasgow", "Escala de Glasgow", "{score}", 3, 15, ("/15",)),
}


def _normalizar_tipo(tipo):
    return "".join(c for c in normalize("NFKD", tipo.casefold()) if c.isascii())


CODIGOS_POR_TIPO = {_normalizar_tipo(signo.tipo): codigo for codigo, signo in SIGNOS.items()}
CODIGOS_POR_TIPO.update({"pulso": "8867-4", "presion sistolica": PAS,
                         "presion diastolica": PAD})


def codigo_signo(tipo):
    return CODIGOS_POR_TIPO.get(_normalizar_tipo(tipo))


def validar_presion(sistolica, diastolica):
    if sistolica is not None and diastolica is not None and diastolica >= sistolica:
        raise ValueError("La presión diastólica debe ser menor que la sistólica.")


def validar_signo(codigo, valor, unidad):
    signo = SIGNOS[codigo]
    if not signo.minimo <= valor <= signo.maximo:
        raise ValueError(f"{signo.tipo} fuera de rango ({signo.minimo} a {signo.maximo} {signo.unidad}).")
    if codigo == "9269-2" and valor % 1:
        raise ValueError("La escala de Glasgow requiere un valor entero.")
    if unidad is not None and unidad not in (signo.unidad, *signo.alias_unidad):
        raise ValueError(f"La unidad de {signo.tipo.lower()} debe ser {signo.unidad}.")
    return signo
