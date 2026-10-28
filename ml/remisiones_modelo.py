"""
remisiones_modelo.py — IA del registro de remisiones (rediseño "libro de radicación").

El modelo KTAS (entrenar.py) responde "¿qué tan urgente es el PACIENTE?".
Este módulo responde preguntas sobre la REMISIÓN, que es lo que lleva el libro:

  1. ¿Qué tan probable es que la remisión termine EFECTIVA?
       - modelo_radicacion: solo con lo que se sabe al radicar (paciente, EPS,
         convenio, motivo y signos vitales). Sirve en el asiento recién abierto.
       - modelo_gestion: lo mismo + lo que va dejando la bitácora HASTA ESE
         MOMENTO (contactos, rechazos, sin respuesta, horas de gestión...).
         Se recalcula cada vez que se anota una gestión.
  2. ¿A qué institución conviene llamar después?  (tasas históricas suavizadas)
  3. ¿Qué causa tiene la respuesta anotada?        (clasificador de texto)
  4. ¿Cuánto tarda en aceptarse una remisión parecida? (mediana histórica)

Datos: data/dataset_analitico.csv + data/gestiones_contacto.csv + data/remisiones.csv
(los mismos que genera python/cargar_dataset.py).

CUIDADO CON LA FUGA DE INFORMACIÓN (lo más importante del módulo):
  * "estado" decide el grupo (aceptada/completada = efectiva, rechazada = fallida,
    pendiente/en_gestion = borderline): NUNCA entra al modelo.
  * n_contactos, n_respuestas_negativas y horas_gestion son el FINAL de la
    gestión: no entran al modelo de radicación. En el de gestión entran
    contados solo hasta cada contacto (fotos en el tiempo), y la foto se toma
    ANTES del contacto que resolvió la remisión.
  * La validación del modelo de gestión es por remisión (StratifiedGroupKFold):
    las fotos de una misma remisión nunca quedan a ambos lados.
  * "borderline" no es un resultado: son remisiones abiertas. No se entrena con
    ellas; son justamente las que el modelo debe pronosticar.

Los datos son sintéticos y balanceados (200/200/200): las métricas salen altas
por cómo se generaron, no porque el problema real sea así de fácil.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
import xgboost
from sklearn.compose import ColumnTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, f1_score, roc_auc_score
from sklearn.model_selection import StratifiedGroupKFold, StratifiedKFold, cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from xgboost import XGBClassifier

BASE = Path(__file__).parent
DATOS = BASE / "data"
RUTA_MODELO = BASE / "modelo" / "remisiones_modelo.joblib"
SEMILLA = 42

# ─────────────────────────────────────────────────────────────────────────────
# Catálogos
# ─────────────────────────────────────────────────────────────────────────────
# Causa estandarizada de cada respuesta de la bitácora. "efecto" dice cómo
# cuenta en la gestión: rechazo, neutra (no decide) o aceptación.
CAUSAS = {
    "acepta":           {"etiqueta": "Acepta",                 "efecto": "acepta"},
    "sin_cama":         {"etiqueta": "Sin cama / servicio",    "efecto": "rechazo"},
    "sin_especialista": {"etiqueta": "Sin especialista",       "efecto": "rechazo"},
    "problema_eps":     {"etiqueta": "Convenio / autorización EPS", "efecto": "rechazo"},
    "sin_respuesta":    {"etiqueta": "Sin respuesta",          "efecto": "neutra"},
    "pide_documentos":  {"etiqueta": "Pide documentos",        "efecto": "neutra"},
    "en_espera":        {"etiqueta": "En evaluación / espera", "efecto": "neutra"},
    "otra":             {"etiqueta": "Otra",                   "efecto": "neutra"},
}

# Reglas por palabras clave: etiquetan el entrenamiento del clasificador y son
# el respaldo cuando el clasificador no está seguro. El orden importa.
REGLAS = [
    ("acepta",           r"\bacept[ao]|cama asignada|se recibe|lo reciben|la reciben"),
    ("problema_eps",     r"convenio|autoriza|\beps\b|contrato"),
    ("pide_documentos",  r"historia cl|documento|ekg|electro|resumen|epicrisis|enviar (la )?h"),
    ("sin_respuesta",    r"sin respuesta|no contesta|no responde|no hay quien|buz[oó]n|ocupad"),
    ("en_espera",        r"llamar en|pendiente|en estudio|evaluaci|valoraci|confirmar|revisando|viendo|responden|esperar"),
    ("sin_especialista", r"especialista|no hay m[eé]dico|sin m[eé]dico|sin (neuro|cardio|ciruj)"),
    ("sin_cama",         r"cupo|cama|uci|disponib|hemodin|camilla|lleno|sobrecupo|quir[oó]fano"),
]

# Frases extra (escritas a mano) para que el clasificador aprenda variantes que
# no están en el dataset sintético: así generaliza cuando la gente escribe libre.
VARIANTES = {
    "acepta": ["Aceptan al paciente", "Lo reciben, enviar ya", "Aceptada por cardiología",
               "Se acepta en urgencias", "Recibimos la paciente, cama 12", "Aceptan traslado en ambulancia"],
    "sin_cama": ["No hay camas", "UCI llena", "Sin disponibilidad de cama en piso", "Están en sobrecupo",
                 "No hay cupo hoy", "Quirófano sin disponibilidad", "Sin camilla en urgencias"],
    "sin_especialista": ["No hay neurocirujano de turno", "Sin especialista hasta mañana",
                         "El cardiólogo no está disponible", "No tienen ortopedista"],
    "problema_eps": ["No tenemos contrato con esa EPS", "Falta autorización", "La EPS no ha autorizado",
                     "Sin convenio vigente", "Piden número de autorización"],
    "sin_respuesta": ["No contestan", "Teléfono ocupado", "Nadie responde en referencia",
                      "Correo sin respuesta", "Sale buzón de voz"],
    "pide_documentos": ["Piden epicrisis", "Solicitan EKG y laboratorios", "Enviar resumen de historia",
                        "Necesitan la historia clínica completa"],
    "en_espera": ["Lo están revisando", "Llamar en una hora", "Pendiente respuesta del médico regulador",
                  "En evaluación por el especialista", "Esperar confirmación",
                  "Lo está viendo cardiología, responden en una hora", "Responden más tarde"],
}

# Motivo de remisión → grupo de servicio (para que el modelo no dependa del texto exacto)
GRUPOS_MOTIVO = [
    ("cardiovascular", r"infarto|tor[aá]cico|card[ií]a|coronar|arritm|hemodin"),
    ("respiratorio",   r"neumon|respirat|pulmon|asma|epoc|ventilaci"),
    ("neurologico",    r"subaracn|craneo|cerebr|acv|neuro|convuls"),
    ("trauma",         r"trauma|fractura|accidente|herida|politrauma"),
    ("quirurgico",     r"colecist|apendic|abdom|s[eé]ptico|peritonit|obstrucci"),
    ("metabolico",     r"diabet|renal|potas|cetoac|pie diab"),
    ("oncologico",     r"leucem|c[aá]ncer|tumor|linfom|onco"),
]

COLS_NUM = ["edad", "fc", "pas", "pad", "fr", "spo2", "temp", "glasgow"]
COLS_BIN = ["motivo_grave", "convenio_vigente", "sale_del_huv"]
COLS_CAT = ["genero", "eps", "grupo_motivo"]
COLS_GESTION = ["contactos_hasta", "rechazos_hasta", "sin_respuesta_hasta", "neutras_hasta",
                "instituciones_distintas", "horas_gestion_hasta", "rechazos_eps_hasta"]


def _sin_tildes(t: str) -> str:
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode()
    return t.lower().strip()


def causa_por_reglas(texto: str) -> str:
    t = _sin_tildes(texto)
    for causa, patron in REGLAS:
        if re.search(_sin_tildes(patron), t):
            return causa
    return "otra"


def grupo_motivo(motivo: str) -> str:
    t = _sin_tildes(motivo)
    for g, patron in GRUPOS_MOTIVO:
        if re.search(_sin_tildes(patron), t):
            return g
    return "otro"


# ─────────────────────────────────────────────────────────────────────────────
# Carga de datos
# ─────────────────────────────────────────────────────────────────────────────
def cargar_datos():
    d = pd.read_csv(DATOS / "dataset_analitico.csv")
    r = pd.read_csv(DATOS / "remisiones.csv")[["ref", "motivo"]].rename(columns={"ref": "remision_ref"})
    g = pd.read_csv(DATOS / "gestiones_contacto.csv")
    d = d.merge(r, on="remision_ref", how="left")
    d["grupo_motivo"] = d["motivo"].map(grupo_motivo)
    d["sale_del_huv"] = (d["sentido"] == "sale_del_HUV").astype(int)
    d["efectiva"] = (d["grupo"] == "efectiva").astype(int)
    g["fecha"] = pd.to_datetime(g["fecha"], utc=True)
    g = g.sort_values(["remision_ref", "orden"]).reset_index(drop=True)
    g["causa"] = g["respuesta"].map(causa_por_reglas)
    g["efecto"] = g["causa"].map(lambda c: CAUSAS[c]["efecto"])
    return d, g


def variables_gestion(contactos: list[dict]) -> dict:
    """Resume la bitácora HASTA este momento (lista de contactos ya anotados).
    Cada contacto: {fecha, institucion_contactada, respuesta, (causa)}. La usa
    tanto el entrenamiento (fotos) como la predicción en vivo: una sola fórmula."""
    if not contactos:
        return {c: 0.0 for c in COLS_GESTION}
    causas = [c.get("causa") or causa_por_reglas(c.get("respuesta", "")) for c in contactos]
    efectos = [CAUSAS.get(c, CAUSAS["otra"])["efecto"] for c in causas]
    fechas = pd.to_datetime([c.get("fecha") for c in contactos], utc=True, errors="coerce")
    fechas = fechas[~fechas.isna()]
    horas = float((fechas.max() - fechas.min()).total_seconds() / 3600) if len(fechas) > 1 else 0.0
    return {
        "contactos_hasta": float(len(contactos)),
        "rechazos_hasta": float(sum(e == "rechazo" for e in efectos)),
        "sin_respuesta_hasta": float(sum(c == "sin_respuesta" for c in causas)),
        "neutras_hasta": float(sum(e == "neutra" for e in efectos)),
        "instituciones_distintas": float(len({c.get("institucion_contactada") for c in contactos})),
        "horas_gestion_hasta": round(horas, 2),
        "rechazos_eps_hasta": float(sum(c == "problema_eps" for c in causas)),
    }


def fotos_de_gestion(d: pd.DataFrame, g: pd.DataFrame) -> pd.DataFrame:
    """Una fila por cada momento de la gestión de las remisiones CERRADAS.
    Foto k = lo que se sabía después de los primeros k contactos, con
    1 <= k <= (n - 1): nunca se incluye el contacto que resolvió el caso."""
    filas = []
    cerradas = d[d["grupo"].isin(["efectiva", "fallida"])].set_index("remision_ref")
    for ref, bloque in g.groupby("remision_ref"):
        if ref not in cerradas.index:
            continue
        base = cerradas.loc[ref]
        contactos = bloque.to_dict("records")
        for k in range(1, len(contactos)):
            fila = {c: base[c] for c in COLS_NUM + COLS_BIN + COLS_CAT}
            fila.update(variables_gestion(contactos[:k]))
            fila["remision_ref"] = ref
            fila["efectiva"] = int(base["efectiva"])
            filas.append(fila)
    return pd.DataFrame(filas)


# ─────────────────────────────────────────────────────────────────────────────
# Modelos
# ─────────────────────────────────────────────────────────────────────────────
def _pipeline(con_gestion: bool) -> Pipeline:
    num = COLS_NUM + COLS_BIN + (COLS_GESTION if con_gestion else [])
    prep = ColumnTransformer([
        ("num", Pipeline([("imp", SimpleImputer(strategy="median")), ("esc", StandardScaler())]), num),
        ("cat", Pipeline([("imp", SimpleImputer(strategy="most_frequent")),
                          ("oh", OneHotEncoder(handle_unknown="ignore"))]), COLS_CAT),
    ])
    clf = XGBClassifier(n_estimators=250, max_depth=3, learning_rate=0.05, subsample=0.9,
                        colsample_bytree=0.9, eval_metric="logloss", random_state=SEMILLA)
    return Pipeline([("prep", prep), ("clf", clf)])


def _metricas(y, p, umbral=0.5) -> dict:
    pred = (p >= umbral).astype(int)
    return {"auc": round(float(roc_auc_score(y, p)), 3),
            "f1": round(float(f1_score(y, pred)), 3),
            "exactitud": round(float(accuracy_score(y, pred)), 3),
            "brier": round(float(brier_score_loss(y, p)), 3),
            "n": int(len(y))}


def _importancias(pipe: Pipeline, top=8) -> list:
    nombres = pipe.named_steps["prep"].get_feature_names_out()
    imp = pipe.named_steps["clf"].feature_importances_
    orden = np.argsort(imp)[::-1][:top]
    return [{"variable": nombres[i].split("__", 1)[-1], "importancia": round(float(imp[i]), 3)} for i in orden]


def _clasificador_respuestas(g: pd.DataFrame):
    textos = list(g["respuesta"].unique())
    etiquetas = [causa_por_reglas(t) for t in textos]
    for causa, frases in VARIANTES.items():
        textos += frases
        etiquetas += [causa] * len(frases)
    pipe = Pipeline([
        ("tfidf", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), preprocessor=_sin_tildes)),
        ("clf", LogisticRegression(max_iter=2000, C=8, class_weight="balanced")),
    ])
    pipe.fit(textos, etiquetas)
    return pipe, len(textos)


def _tasas_instituciones(d: pd.DataFrame, g: pd.DataFrame) -> dict:
    """% de contactos que terminaron en 'acepta', por institución, suavizado
    hacia la tasa global (para no premiar una institución con 1 de 1)."""
    gg = g.merge(d[["remision_ref", "eps", "grupo_motivo"]], on="remision_ref", how="left")
    gg["acepto"] = (gg["causa"] == "acepta").astype(int)
    global_ = float(gg["acepto"].mean())
    A = 10  # peso del suavizado (equivale a 10 contactos "promedio")

    def tabla(cols):
        t = gg.groupby(cols)["acepto"].agg(["sum", "count"]).reset_index()
        t["tasa"] = (t["sum"] + A * global_) / (t["count"] + A)
        return t

    general = tabla(["institucion_contactada"])
    por_eps = tabla(["institucion_contactada", "eps"])
    por_motivo = tabla(["institucion_contactada", "grupo_motivo"])
    return {
        "global": global_,
        "general": {r.institucion_contactada: {"tasa": float(r.tasa), "n": int(r["count"])} for _, r in general.iterrows()},
        "por_eps": {f"{r.institucion_contactada}|{r.eps}": {"tasa": float(r.tasa), "n": int(r["count"])} for _, r in por_eps.iterrows()},
        "por_motivo": {f"{r.institucion_contactada}|{r.grupo_motivo}": {"tasa": float(r.tasa), "n": int(r["count"])} for _, r in por_motivo.iterrows()},
        "suavizado": A,
    }


def _tiempos_aceptacion(d: pd.DataFrame) -> dict:
    """Horas de gestión de las remisiones efectivas, por grupo de motivo."""
    ef = d[d["grupo"] == "efectiva"]
    tabla = {}
    for gm, b in ef.groupby("grupo_motivo"):
        tabla[gm] = {"mediana": round(float(b["horas_gestion"].median()), 2),
                     "p75": round(float(b["horas_gestion"].quantile(0.75)), 2), "n": int(len(b))}
    tabla["_todas"] = {"mediana": round(float(ef["horas_gestion"].median()), 2),
                       "p75": round(float(ef["horas_gestion"].quantile(0.75)), 2), "n": int(len(ef))}
    return tabla


def entrenar_remisiones(guardar: bool = True) -> dict:
    d, g = cargar_datos()
    cerradas = d[d["grupo"].isin(["efectiva", "fallida"])].reset_index(drop=True)
    X1, y1 = cerradas[COLS_NUM + COLS_BIN + COLS_CAT], cerradas["efectiva"]

    # Modelo 1 — radicación (validación cruzada estratificada, 5 particiones)
    cv1 = StratifiedKFold(n_splits=5, shuffle=True, random_state=SEMILLA)
    p1 = cross_val_predict(_pipeline(False), X1, y1, cv=cv1, method="predict_proba")[:, 1]
    m1 = _pipeline(False).fit(X1, y1)

    # Modelo 2 — gestión (fotos en el tiempo, validación AGRUPADA por remisión)
    fotos = fotos_de_gestion(d, g)
    X2, y2, grupos = fotos[COLS_NUM + COLS_BIN + COLS_CAT + COLS_GESTION], fotos["efectiva"], fotos["remision_ref"]
    cv2 = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=SEMILLA)
    p2 = cross_val_predict(_pipeline(True), X2, y2, cv=cv2, groups=grupos, method="predict_proba")[:, 1]
    m2 = _pipeline(True).fit(X2, y2)

    clasif, n_textos = _clasificador_respuestas(g)

    # Pronóstico para las abiertas (borderline): lo que el libro necesita priorizar
    abiertas = d[d["grupo"] == "borderline"]
    p_abiertas = m1.predict_proba(abiertas[COLS_NUM + COLS_BIN + COLS_CAT])[:, 1]

    version = f"rem-1.{datetime.now():%Y%m%d%H%M}"
    paquete = {
        "version": version,
        "entrenado_en": datetime.now().isoformat(timespec="seconds"),
        "modelo_radicacion": m1,
        "modelo_gestion": m2,
        "clasificador_respuestas": clasif,
        "tasas_instituciones": _tasas_instituciones(d, g),
        "tiempos_aceptacion": _tiempos_aceptacion(d),
        "instituciones": sorted(g["institucion_contactada"].unique().tolist()),
        "metricas": {
            "radicacion": _metricas(y1.values, p1),
            "gestion": _metricas(y2.values, p2),
            "clasificador_textos": n_textos,
        },
        "importancias": {"radicacion": _importancias(m1), "gestion": _importancias(m2)},
        "abiertas": {"n": int(len(abiertas)), "prob_media": round(float(p_abiertas.mean()), 3),
                     "en_riesgo": int((p_abiertas < 0.4).sum())},
        "datos": {"remisiones": int(len(d)), "cerradas": int(len(cerradas)),
                  "fotos_gestion": int(len(fotos)), "contactos": int(len(g))},
        "librerias": {"sklearn": sklearn.__version__, "xgboost": xgboost.__version__},
    }
    if guardar:
        RUTA_MODELO.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(paquete, RUTA_MODELO)
    return paquete


# ─────────────────────────────────────────────────────────────────────────────
# Uso en línea (lo llaman las rutas de main.py)
# ─────────────────────────────────────────────────────────────────────────────
def cargar_paquete():
    return joblib.load(RUTA_MODELO) if RUTA_MODELO.exists() else None


def clasificar(paquete: dict, texto: str) -> dict:
    regla = causa_por_reglas(texto)
    proba = paquete["clasificador_respuestas"].predict_proba([texto])[0]
    clases = paquete["clasificador_respuestas"].classes_
    i = int(np.argmax(proba))
    causa, confianza = str(clases[i]), float(proba[i])
    origen = "modelo"
    if confianza < 0.45:            # el modelo duda: manda la regla
        causa, origen = regla, "reglas"
    return {"causa": causa, "etiqueta": CAUSAS[causa]["etiqueta"], "efecto": CAUSAS[causa]["efecto"],
            "confianza": round(confianza, 3), "origen": origen}


def _fila_base(datos: dict) -> dict:
    fila = {c: datos.get(c) for c in COLS_NUM}
    fila["motivo_grave"] = int(bool(datos.get("motivo_grave")))
    fila["convenio_vigente"] = int(bool(datos.get("convenio_vigente")))
    fila["sale_del_huv"] = int(bool(datos.get("sale_del_huv")))
    fila["genero"] = datos.get("genero") or "F"
    fila["eps"] = datos.get("eps") or "desconocida"
    fila["grupo_motivo"] = datos.get("grupo_motivo") or grupo_motivo(datos.get("motivo", ""))
    return fila


HUV = "Hospital Universitario del Valle"


def recomendar(paquete: dict, eps: str, grupo_m: str, excluir: list, n=3, sale_del_huv=True) -> list:
    """Si el paciente SALE del HUV, se sugieren instituciones externas (nunca el HUV).
    Si LLEGA al HUV, la receptora es el propio HUV: no hay a quién más sugerir."""
    if not sale_del_huv:
        return []
    t = paquete["tasas_instituciones"]
    excluir = {e for e in (excluir or [])} | {HUV}
    salida = []
    for inst in paquete["instituciones"]:
        if inst in excluir:
            continue
        gen = t["general"].get(inst, {"tasa": t["global"], "n": 0})
        e = t["por_eps"].get(f"{inst}|{eps}")
        m = t["por_motivo"].get(f"{inst}|{grupo_m}")
        partes = [gen["tasa"]] + ([e["tasa"]] if e else []) + ([m["tasa"]] if m else [])
        salida.append({"institucion": inst, "tasa_aceptacion": round(float(np.mean(partes)), 3),
                       "contactos_historicos": gen["n"]})
    salida.sort(key=lambda x: -x["tasa_aceptacion"])
    return salida[:n]


def pronosticar(paquete: dict, datos: dict, contactos: list) -> dict:
    """datos: variables de la remisión (sin nombre ni documento: R12).
    contactos: bitácora hasta ahora (fecha, institucion_contactada, respuesta)."""
    fila = _fila_base(datos)
    # Si ya hubo un "acepta", la remisión está resuelta: el pronóstico lo dice tal cual
    clasificados = []
    for c in contactos or []:
        k = clasificar(paquete, c.get("respuesta", ""))
        clasificados.append({**c, "causa": k["causa"]})
    ya_acepto = any(c["causa"] == "acepta" for c in clasificados)

    if clasificados:
        fila.update(variables_gestion(clasificados))
        X = pd.DataFrame([fila])[COLS_NUM + COLS_BIN + COLS_CAT + COLS_GESTION]
        prob = float(paquete["modelo_gestion"].predict_proba(X)[0, 1])
        momento = "gestion"
    else:
        X = pd.DataFrame([fila])[COLS_NUM + COLS_BIN + COLS_CAT]
        prob = float(paquete["modelo_radicacion"].predict_proba(X)[0, 1])
        momento = "radicacion"
    if ya_acepto:
        prob, momento = 1.0, "resuelta"

    # El modelo de radicación como referencia (para mostrar "antes: 74 %")
    X0 = pd.DataFrame([_fila_base(datos)])[COLS_NUM + COLS_BIN + COLS_CAT]
    prob_inicial = float(paquete["modelo_radicacion"].predict_proba(X0)[0, 1])

    tiempos = paquete["tiempos_aceptacion"]
    t = tiempos.get(fila["grupo_motivo"], tiempos["_todas"])
    horas_ya = variables_gestion(clasificados)["horas_gestion_hasta"] if clasificados else 0.0
    sin_mov = horas_ya > max(6.0, 2 * t["p75"]) and not ya_acepto
    riesgo = "bajo" if prob >= 0.6 else "medio" if prob >= 0.4 else "alto"
    contactadas = [c.get("institucion_contactada") for c in clasificados]
    return {
        "probabilidad_efectiva": round(prob, 3),
        "probabilidad_al_radicar": round(prob_inicial, 3),
        "riesgo_estancamiento": riesgo,
        "momento": momento,
        "tiempo_estimado_horas": {"mediana": t["mediana"], "p75": t["p75"], "n_similares": t["n"]},
        "alerta_sin_movimiento": bool(sin_mov),
        "instituciones_sugeridas": [] if ya_acepto else recomendar(paquete, fila["eps"], fila["grupo_motivo"], contactadas,
                                                                    sale_del_huv=bool(fila["sale_del_huv"])),
        "causas_bitacora": [{"orden": i + 1, "causa": c["causa"], "etiqueta": CAUSAS[c["causa"]]["etiqueta"]}
                            for i, c in enumerate(clasificados)],
        "grupo_motivo": fila["grupo_motivo"],
        "version_modelo": paquete["version"],
    }


if __name__ == "__main__":
    # Se importa el módulo por su nombre para que el .joblib guarde referencias a
    # "remisiones_modelo" (y no a "__main__"): así main.py lo puede cargar.
    import remisiones_modelo as _rm
    p = _rm.entrenar_remisiones()
    print("versión:", p["version"])
    print("métricas:", p["metricas"])
    print("abiertas:", p["abiertas"])
    print("importancias radicación:", p["importancias"]["radicacion"][:5])
    print("importancias gestión:", p["importancias"]["gestion"][:5])
