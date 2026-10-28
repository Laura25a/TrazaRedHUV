"""
causas.py — Causa estandarizada de cada respuesta de la bitácora.

Es la MISMA tabla de reglas que usa el servicio de IA (ml/remisiones_modelo.py)
para etiquetar su clasificador. La API la necesita para dos cosas:
  * pintar el libro sin llamar al servicio ml 2.000 veces (una por contacto),
  * seguir funcionando si el servicio ml está caído (respaldo).
Cuando se anota una gestión nueva, la API le pide la causa al clasificador del
servicio ml y la guarda junto al contacto ("causa"); estas reglas solo se usan
para los contactos que no la tengan guardada.
"""
import re
import unicodedata

CAUSAS = {
    "acepta":           {"etiqueta": "Acepta",                      "efecto": "acepta",  "marca": "v"},
    "sin_cama":         {"etiqueta": "Sin cama / servicio",         "efecto": "rechazo", "marca": "x"},
    "sin_especialista": {"etiqueta": "Sin especialista",            "efecto": "rechazo", "marca": "x"},
    "problema_eps":     {"etiqueta": "Convenio / autorización EPS", "efecto": "rechazo", "marca": "x"},
    "sin_respuesta":    {"etiqueta": "Sin respuesta",               "efecto": "neutra",  "marca": "o"},
    "pide_documentos":  {"etiqueta": "Pide documentos",             "efecto": "neutra",  "marca": "e"},
    "en_espera":        {"etiqueta": "En evaluación / espera",      "efecto": "neutra",  "marca": "e"},
    "otra":             {"etiqueta": "Otra",                        "efecto": "neutra",  "marca": "o"},
}

REGLAS = [
    ("acepta",           r"\bacept[ao]|cama asignada|se recibe|lo reciben|la reciben"),
    ("problema_eps",     r"convenio|autoriza|\beps\b|contrato"),
    ("pide_documentos",  r"historia cl|documento|ekg|electro|resumen|epicrisis|enviar (la )?h"),
    ("sin_respuesta",    r"sin respuesta|no contesta|no responde|no hay quien|buz[oó]n|ocupad"),
    ("en_espera",        r"llamar en|pendiente|en estudio|evaluaci|valoraci|confirmar|revisando|viendo|responden|esperar"),
    ("sin_especialista", r"especialista|no hay m[eé]dico|sin m[eé]dico|sin (neuro|cardio|ciruj)"),
    ("sin_cama",         r"cupo|cama|uci|disponib|hemodin|camilla|lleno|sobrecupo|quir[oó]fano"),
]


def _sin_tildes(t: str) -> str:
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode()
    return t.lower().strip()


_REGLAS = [(c, re.compile(_sin_tildes(p))) for c, p in REGLAS]


def causa_por_reglas(texto: str) -> str:
    t = _sin_tildes(texto)
    for causa, patron in _REGLAS:
        if patron.search(t):
            return causa
    return "otra"


def causa_de(contacto: dict) -> str:
    """La causa guardada (la puso el clasificador al anotar) o, si no hay, la regla."""
    c = contacto.get("causa")
    return c if c in CAUSAS else causa_por_reglas(contacto.get("respuesta", ""))
