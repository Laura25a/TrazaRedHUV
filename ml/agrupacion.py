"""
agrupacion.py — Agrupamiento de pacientes (R07). Réplica fiel del cuaderno semana9_final.ipynb.

Pasos (mismos que el cuaderno, en el mismo orden y con las mismas semillas):
  1. limpiar los datos reales (valores '??' / '#BOÞ!' -> NaN, Saturation = 20 -> NaN,
     NRS_pain = 0 si no hay dolor, Arrival mode 5 y 6 -> 7),
  2. preprocesar (mediana + escala + one-hot) y agrupar con K-means (k = 4),
  3. nombrar cada grupo POR SU PERFIL (los números 0..3 pueden cambiar entre versiones),
  4. medir dónde se equivoca el modelo de urgencia: XGBoost con validación cruzada de 5
     particiones sobre TODOS los pacientes y umbral 0.4 (celdas 125, 128 y 129),
  5. generar sintéticos DENTRO de cada grupo y comprobar que el K-means los reconoce,
  6. perfiles, mapa PCA, calidad (codo, silueta, Davies-Bouldin), distribución KTAS.

Todo se calcula una sola vez (la primera llamada tarda unos segundos) y queda en memoria.
Para recalcular: reiniciar el contenedor o llamar a recalcular().
"""
from functools import lru_cache

import numpy as np
import pandas as pd
from scipy.stats import binomtest
from sklearn.cluster import KMeans
from sklearn.compose import ColumnTransformer
from sklearn.decomposition import PCA
from sklearn.impute import SimpleImputer
from sklearn.metrics import confusion_matrix, davies_bouldin_score, silhouette_score
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from xgboost import XGBClassifier

from entrenar import RUTA_CSV

K = 4
SEMILLA = 42
UMBRAL = 0.4          # umbral de decisión "urgente" (el mismo del cuaderno y de /predecir)

COLS_NUM = ["Age", "Patients number per hour", "NRS_pain", "SBP", "DBP", "HR", "RR", "BT", "Saturation"]
COLS_CAT = ["Sex", "Arrival mode", "Injury", "Mental", "Pain"]
COLS = COLS_NUM + COLS_CAT

ETIQUETAS = {
    "Age": "Edad", "Patients number per hour": "Pacientes por hora", "NRS_pain": "Dolor (NRS)",
    "SBP": "Presión sistólica", "DBP": "Presión diastólica", "HR": "Frecuencia cardíaca",
    "RR": "Frecuencia respiratoria", "BT": "Temperatura", "Saturation": "Saturación O₂",
}

# Sintéticos por grupo (celda 132 del cuaderno: 60, 60, 60 y 10). Van por PERFIL y no por número.
N_SINTETICOS = {"hipoxemia": 60, "hta": 60, "sin_dolor": 60, "general": 10}
VARS_CLAVE = ["Age", "SBP", "HR", "Saturation", "NRS_pain"]


def _n(v, d=4):
    """Número redondeado, o None si es NaN (el JSON no admite NaN)."""
    if v is None:
        return None
    v = float(v)
    return None if np.isnan(v) else round(v, d)


# ---------------------------------------------------------------- datos y preprocesamiento
def _cargar():
    df = pd.read_csv(RUTA_CSV, sep=";", encoding="latin-1")
    y = (df["KTAS_expert"] <= 3).astype(int)
    ktas = df["KTAS_expert"].copy()
    X = df[COLS].copy()
    for c in COLS_NUM:                                   # '??' y '#BOÞ!' pasan a NaN
        X[c] = pd.to_numeric(X[c], errors="coerce")
    X.loc[X["Saturation"] == 20, "Saturation"] = np.nan  # el 20 % es un error de registro
    X.loc[X["Pain"] == 0, "NRS_pain"] = 0                # sin dolor, la escala vacía significa cero
    X["Arrival mode"] = X["Arrival mode"].replace({5: 7, 6: 7})
    return X, y, ktas


def _onehot(denso):
    if not denso:
        return OneHotEncoder(handle_unknown="ignore")
    try:
        return OneHotEncoder(handle_unknown="ignore", sparse_output=False)
    except TypeError:                                    # scikit-learn antiguo
        return OneHotEncoder(handle_unknown="ignore", sparse=False)


def _pre(denso=False):
    return ColumnTransformer([
        ("num", Pipeline([("mediana", SimpleImputer(strategy="median")), ("escalador", StandardScaler())]), COLS_NUM),
        ("cat", _onehot(denso), COLS_CAT),
    ])


def _denso(M):
    return M.toarray() if hasattr(M, "toarray") else np.asarray(M)


# ---------------------------------------------------------------- cálculo principal
@lru_cache(maxsize=1)
def _calcular():
    X, y, ktas = _cargar()

    # --- K-means sobre todos los pacientes reales
    pre = _pre()
    Z = _denso(pre.fit_transform(X))
    km = KMeans(n_clusters=K, n_init=10, random_state=SEMILLA).fit(Z)
    res = X.copy()
    res["cluster"] = km.labels_
    res["urgente"] = y.values
    res["KTAS"] = ktas.values
    ids = sorted(res["cluster"].unique())

    # --- Nombre de cada grupo asignado por su perfil (celda 122)
    g = res.groupby("cluster").agg(sat=("Saturation", "median"), sbp=("SBP", "median"), nrs=("NRS_pain", "mean"))
    c_hipox = g["sat"].idxmin()
    resto = g.drop(c_hipox)
    c_hta = resto["sbp"].idxmax()
    resto = resto.drop(c_hta)
    c_sin = resto["nrs"].idxmin()
    c_gen = resto.drop(c_sin).index[0]
    PERFIL = {c_hipox: "hipoxemia", c_hta: "hta", c_sin: "sin_dolor", c_gen: "general"}
    NOMBRE = {
        c_hipox: "Hipoxemia severa con frecuencia respiratoria alta",
        c_hta: "Presión arterial alta",
        c_sin: "Sin dolor y con alta proporción de urgentes",
        c_gen: "Grupo general (dolor por encima del promedio, signos vitales normales)",
    }

    # --- Dónde se equivoca el modelo (celdas 125, 128, 129): XGBoost, CV de 5 particiones, umbral 0.4
    modelo = Pipeline([("pre", _pre(denso=True)),
                       ("clf", XGBClassifier(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=SEMILLA))])
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=SEMILLA)
    proba = cross_val_predict(modelo, X, y, cv=cv, method="predict_proba")[:, 1]
    res["pred"] = (proba >= UMBRAL).astype(int)

    def medir(d):
        tn, fp, fn, tp = confusion_matrix(d["urgente"], d["pred"], labels=[0, 1]).ravel()
        return {"n": int(len(d)), "n_urgentes": int(tp + fn), "n_no_urgentes": int(tn + fp),
                "sens": tp / (tp + fn) if tp + fn else np.nan,
                "espec": tn / (tn + fp) if tn + fp else np.nan,
                "fn": int(fn), "fp": int(fp), "error": (fn + fp) / len(d)}

    glob = medir(res)
    ALFA = 0.05 / (2 * len(ids))                         # corrección: grupos x 2 tipos de error
    rend = []
    for c in ids:
        m = medir(res[res["cluster"] == c])
        p_perd = binomtest(m["fn"], m["n_urgentes"], 1 - glob["sens"], alternative="greater").pvalue if m["n_urgentes"] else np.nan
        p_fa = binomtest(m["fp"], m["n_no_urgentes"], 1 - glob["espec"], alternative="greater").pvalue if m["n_no_urgentes"] else np.nan
        zp = bool(not np.isnan(p_perd) and p_perd < ALFA)
        zf = bool(not np.isnan(p_fa) and p_fa < ALFA)
        rend.append({
            "cluster": int(c), "nombre": NOMBRE[c], **m,
            "fn_esperados": m["n_urgentes"] * (1 - glob["sens"]),
            "fp_esperadas": m["n_no_urgentes"] * (1 - glob["espec"]),
            "p_perdidos": p_perd, "p_falsas_alarmas": p_fa,
            "zona_por_perdidos": bool(not np.isnan(m["sens"]) and m["sens"] < glob["sens"]),   # regla simple (celda 128)
            "zona_por_error": bool(m["error"] > glob["error"]),
            "zona_perdidos_real": zp, "zona_falsas_alarmas_real": zf,                          # prueba estadística (celda 129)
            "zona_confusion": zp or zf,
        })
    R = {r["cluster"]: r for r in rend}

    # --- Sintéticos dentro de cada grupo (celda 132)
    rng = np.random.default_rng(SEMILLA)
    filas = []
    for c in ids:
        n = N_SINTETICOS[PERFIL[c]]
        base = res.loc[res["cluster"] == c, COLS]
        sd, lo, hi = base[COLS_NUM].std(), base[COLS_NUM].min(), base[COLS_NUM].max()
        m = base.sample(n, replace=True, random_state=SEMILLA).reset_index(drop=True)
        for col in COLS_NUM:
            m[col] = (m[col] + rng.normal(0, 0.2 * sd[col], n)).clip(lo[col], hi[col])
        m.loc[m["Pain"] == 0, "NRS_pain"] = 0
        for col in ["Age", "Patients number per hour", "NRS_pain", "SBP", "DBP", "HR", "RR", "Saturation"]:
            m[col] = m[col].round()
        m["BT"] = m["BT"].round(1)
        m["cluster"] = c
        filas.append(m)
    sint = pd.concat(filas, ignore_index=True)

    Zs = _denso(pre.transform(sint[COLS]))
    reasig = km.predict(Zs)
    acierto = reasig == sint["cluster"].to_numpy()
    fuera = int(sum(((sint[v] < X[v].min()) | (sint[v] > X[v].max())).sum() for v in COLS_NUM))
    medias_real = res.groupby("cluster")[VARS_CLAVE].mean()
    medias_sint = sint.groupby("cluster")[VARS_CLAVE].mean()

    # --- Resumen por grupo (lo que devuelve /clusters)
    clusters = []
    for c in ids:
        d = res[res["cluster"] == c]
        r = R[c]
        zona = ""
        if r["zona_perdidos_real"]:
            zona += f" Zona de confusión: el modelo pierde más pacientes urgentes de lo esperado ({r['fn']} urgentes no detectados)."
        if r["zona_falsas_alarmas_real"]:
            zona += f" Zona de confusión: el modelo genera más falsas alarmas de lo esperado ({r['fp']})."
        desc = (f"{NOMBRE[c]}. Edad mediana: {d['Age'].median():.1f} años; dolor NRS medio: {d['NRS_pain'].mean():.1f}; "
                f"saturación mediana: {d['Saturation'].median():.1f}; {d['urgente'].mean() * 100:.1f} % de los registros "
                f"del grupo tienen KTAS ≤ 3.{zona}")
        clusters.append({
            "cluster": int(c), "nombre": NOMBRE[c], "descripcion": desc,
            "n_reales": int(len(d)), "n_sinteticos": int((sint["cluster"] == c).sum()),
            "n_urgentes": r["n_urgentes"], "zona_confusion": r["zona_confusion"],
            "tasa_error_modelo": _n(r["error"], 3),
            "sensibilidad": _n(r["sens"], 3), "especificidad": _n(r["espec"], 3),
            "fn": r["fn"], "fp": r["fp"],
            "sinteticos_reconocidos": _n(acierto[sint["cluster"].to_numpy() == c].mean(), 3),
        })

    # --- Perfiles frente a la cohorte (celda 118)
    cohorte = X[COLS_NUM].mean()
    desv = X[COLS_NUM].std()
    perfiles = []
    for c in ids:
        d = res[res["cluster"] == c]
        prom = d[COLS_NUM].mean()
        perfiles.append({"cluster": int(c), "n": int(len(d)),
                         "promedios": {v: _n(prom[v]) for v in COLS_NUM},
                         "desviaciones": {v: _n((prom[v] - cohorte[v]) / desv[v]) for v in COLS_NUM}})

    # --- Mapa PCA (celda 115) y calidad del agrupamiento (celdas 97 y 100)
    pca = PCA(n_components=2, random_state=SEMILLA).fit(Z)
    Z2, Ps = pca.transform(Z), pca.transform(Zs)
    mapa = {
        "varianza": [round(float(v) * 100, 1) for v in pca.explained_variance_ratio_],
        "reales": [[round(float(a), 4), round(float(b), 4), int(c), int(u)]
                   for (a, b), c, u in zip(Z2, res["cluster"], res["urgente"])],
        "sinteticos": [[round(float(a), 4), round(float(b), 4), int(c)] for (a, b), c in zip(Ps, sint["cluster"])],
    }
    calidad = []
    for k in range(2, 9):
        kk = KMeans(n_clusters=k, n_init=10, random_state=SEMILLA).fit(Z)
        calidad.append({"k": k, "inercia": round(float(kk.inertia_), 2),
                        "silueta": round(float(silhouette_score(Z, kk.labels_)), 3),
                        "davies_bouldin": round(float(davies_bouldin_score(Z, kk.labels_)), 3)})

    # --- Distribución KTAS por grupo (celda 120)
    ktas_g = []
    for c in ids:
        d = res[res["cluster"] == c]
        dist = d["KTAS"].value_counts(normalize=True)
        ktas_g.append({"cluster": int(c), "nombre": NOMBRE[c],
                       "dist": {str(n): _n(dist.get(n, 0.0), 3) for n in (1, 2, 3, 4, 5)},
                       "pct_urgentes": _n(d["urgente"].mean() * 100, 1),
                       "pct_ktas12": _n((d["KTAS"] <= 2).mean() * 100, 1)})

    detalle = {
        "k_servicio": K, "umbral": UMBRAL,
        "variables": [{"id": v, "nombre": ETIQUETAS[v]} for v in COLS_NUM],
        "cohorte": {v: _n(cohorte[v]) for v in COLS_NUM},
        "perfiles": perfiles, "mapa": mapa, "calidad": calidad,
        "rendimiento": {
            "global": {k: (_n(v) if isinstance(v, float) else v) for k, v in glob.items()},
            "alfa": round(ALFA, 4),
            "grupos": [{k: (_n(v) if isinstance(v, float) else v) for k, v in r.items()} for r in rend],
        },
        "ktas": ktas_g,
        "sinteticos": {
            "global_reconocidos": _n(acierto.mean(), 3), "fuera_de_rango": fuera,
            "grupos": [{"cluster": int(c), "nombre": NOMBRE[c],
                        "n": int((sint["cluster"] == c).sum()),
                        "reconocidos": _n(acierto[sint["cluster"].to_numpy() == c].mean(), 3),
                        "real": {v: _n(medias_real.loc[c, v], 1) for v in VARS_CLAVE},
                        "sintetico": {v: _n(medias_sint.loc[c, v], 1) for v in VARS_CLAVE}} for c in ids],
            "variables": [{"id": v, "nombre": ETIQUETAS[v]} for v in VARS_CLAVE],
        },
    }
    return {"clusters": clusters, "detalle": detalle}


def resumen_clusters():
    return _calcular()["clusters"]


def detalle_clusters():
    return _calcular()["detalle"]


def recalcular():
    _calcular.cache_clear()
    return _calcular()