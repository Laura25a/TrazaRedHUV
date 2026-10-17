"""
R07: agrupamiento de pacientes reales y sintéticos, todo dentro del servicio ml.

Los mismos pasos del cuaderno semana9_final:
  1. preprocesar (mediana + escala + one-hot) y agrupar con K-means (k = 4),
  2. nombrar cada grupo por su PERFIL (no por su número),
  3. medir dónde se equivoca el modelo supervisado (zona de confusión),
  4. generar sintéticos DENTRO de cada grupo con ese mismo K-means.

Todo se calcula una sola vez (la primera vez que alguien lo pide) y queda en
memoria. Como los sintéticos nacen del K-means que los cuenta, el número de
cluster no se puede desalinear. Con semilla fija, al reiniciar el contenedor se
regeneran idénticos.
"""
import threading

import numpy as np
import pandas as pd
from scipy.stats import binomtest
from sklearn.cluster import KMeans
from sklearn.metrics import confusion_matrix
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.pipeline import Pipeline

from entrenar import (COLS_CAT, COLS_NUM, UMBRAL, cargar_ktas, crear_clasificador,
                      crear_preprocesador)

K = 4
SEMILLA = 42
N_SINTETICOS_GRUPO = 60      # sintéticos por grupo "grande"
N_SINTETICOS_PEQUENO = 10    # sintéticos por grupo con muy pocos pacientes
UMBRAL_GRUPO_PEQUENO = 30    # menos de esto = grupo pequeño
RUIDO = 0.2                  # 20 % de la desviación estándar del grupo

ESTADO: dict = {}
_CANDADO = threading.Lock()


# ── 1. Entrenar el agrupamiento ───────────────────────────────────────────
def _denso(Z):
    return Z.toarray() if hasattr(Z, "toarray") else Z


def _nombrar(reales: pd.DataFrame) -> dict:
    """Nombre de cada grupo según su perfil (igual que el cuaderno)."""
    r = reales.groupby("cluster").agg(
        sat=("Saturation", "median"), sbp=("SBP", "median"), nrs=("NRS_pain", "mean"))
    c_hipox = r["sat"].idxmin()                    # la saturación más baja
    r = r.drop(c_hipox)
    c_hta = r["sbp"].idxmax()                      # la presión sistólica más alta de los demás
    r = r.drop(c_hta)
    c_sin_dolor = r["nrs"].idxmin()                # el menor dolor de los que quedan
    c_general = r.drop(c_sin_dolor).index[0]       # el que sobra
    return {
        int(c_hipox): "Hipoxemia severa con frecuencia respiratoria alta",
        int(c_hta): "Presión arterial alta",
        int(c_sin_dolor): "Sin dolor y con alta proporción de urgentes",
        int(c_general): "Grupo general (dolor por encima del promedio, signos vitales normales)",
    }


def _zona_confusion(X: pd.DataFrame, y: pd.Series, grupos: np.ndarray) -> dict:
    """Dónde se equivoca el modelo supervisado: predicciones fuera de muestra
    (validación cruzada) con el umbral de decisión, y prueba binomial con
    corrección por comparar varios grupos (como en el cuaderno)."""
    modelo = Pipeline([("pre", crear_preprocesador()), ("clf", crear_clasificador())])
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=SEMILLA)
    proba = cross_val_predict(modelo, X, y, cv=cv, method="predict_proba")[:, 1]
    pred = (proba >= UMBRAL).astype(int)

    tn, fp, fn, tp = confusion_matrix(y, pred).ravel()
    sens_g, espec_g = tp / (tp + fn), tn / (tn + fp)
    alfa = 0.05 / (2 * len(np.unique(grupos)))     # grupos x 2 tipos de error

    salida = {}
    for c in np.unique(grupos):
        m = grupos == c
        yc, pc = y.to_numpy()[m], pred[m]
        n_urg, n_no = int((yc == 1).sum()), int((yc == 0).sum())
        fn_c = int(((yc == 1) & (pc == 0)).sum())
        fp_c = int(((yc == 0) & (pc == 1)).sum())
        p_perd = binomtest(fn_c, n_urg, 1 - sens_g, alternative="greater").pvalue if n_urg else 1.0
        p_fa = binomtest(fp_c, n_no, 1 - espec_g, alternative="greater").pvalue if n_no else 1.0
        salida[int(c)] = {
            "error": round(float((fn_c + fp_c) / m.sum()), 3),
            "FN": fn_c, "FP": fp_c,
            "pierde_urgentes": bool(p_perd < alfa),        # zona por urgentes perdidos
            "falsas_alarmas": bool(p_fa < alfa),           # zona por falsas alarmas
        }
    return salida


def _entrenar() -> dict:
    df = cargar_ktas().reset_index(drop=True)
    X, y = df[COLS_NUM + COLS_CAT], df["urgente"]

    pre = crear_preprocesador()
    Z = _denso(pre.fit_transform(X))
    kmeans = KMeans(n_clusters=K, n_init=10, random_state=SEMILLA).fit(Z)

    reales = X.copy()
    reales["cluster"] = kmeans.labels_
    reales["urgente"] = y.to_numpy()

    return {
        "pre": pre, "kmeans": kmeans, "reales": reales,
        "nombres": _nombrar(reales),
        "confusion": _zona_confusion(X, y, kmeans.labels_),
    }


# ── 2. Generar sintéticos dentro de cada grupo ────────────────────────────
def _generar(estado: dict) -> pd.DataFrame:
    reales, kmeans, pre = estado["reales"], estado["kmeans"], estado["pre"]
    rng = np.random.default_rng(SEMILLA)
    partes = []
    for c, grupo in sorted(reales.groupby("cluster"), key=lambda t: t[0]):
        # La cantidad depende del TAMAÑO del grupo, no de su número
        n = N_SINTETICOS_GRUPO if len(grupo) >= UMBRAL_GRUPO_PEQUENO else N_SINTETICOS_PEQUENO
        base = grupo[COLS_NUM + COLS_CAT]
        sd, lo, hi = base[COLS_NUM].std(), base[COLS_NUM].min(), base[COLS_NUM].max()
        m = base.sample(n, replace=True, random_state=SEMILLA).reset_index(drop=True)
        for col in COLS_NUM:
            m[col] = (m[col] + rng.normal(0, RUIDO * sd[col], n)).clip(lo[col], hi[col])
        m.loc[m["Pain"] == 0, "NRS_pain"] = 0
        for col in COLS_NUM:
            m[col] = m[col].round(1) if col == "BT" else m[col].round()
        m["cluster"] = int(c)
        partes.append(m)

    sint = pd.concat(partes, ignore_index=True)
    sint["origen"] = "sintetico"
    # ¿El mismo K-means los reconoce en el grupo donde nacieron?
    reasignado = kmeans.predict(_denso(pre.transform(sint[COLS_NUM + COLS_CAT])))
    sint["consistente"] = reasignado == sint["cluster"].to_numpy()
    return sint


def obtener_estado() -> dict:
    """Entrena la primera vez; después devuelve lo que ya está en memoria."""
    if not ESTADO:
        with _CANDADO:
            if not ESTADO:                      # otro hilo pudo terminar mientras esperaba
                e = _entrenar()
                e["sinteticos"] = _generar(e)
                ESTADO.update(e)
    return ESTADO


# ── 3. Lo que devuelve la ruta /clusters ──────────────────────────────────
def _num(valor, decimales=1):
    return "no disponible" if pd.isna(valor) else f"{valor:.{decimales}f}"


def resumen_clusters() -> list:
    e = obtener_estado()
    reales, sint = e["reales"], e["sinteticos"]
    resultado = []
    for c in sorted(reales["cluster"].unique()):
        g = reales[reales["cluster"] == c]
        s = sint[sint["cluster"] == c]
        conf = e["confusion"][int(c)]
        texto = (f"{e['nombres'][int(c)]}. "
                 f"Edad mediana: {_num(g['Age'].median())} años; "
                 f"dolor NRS medio: {_num(g['NRS_pain'].mean())}; "
                 f"saturación mediana: {_num(g['Saturation'].median())}; "
                 f"{_num(g['urgente'].mean() * 100)} % de los registros del grupo tienen KTAS ≤ 3.")
        if conf["pierde_urgentes"]:
            texto += (f" Zona de confusión: el modelo pierde más pacientes urgentes de lo esperado "
                      f"({conf['FN']} urgentes no detectados).")
        if conf["falsas_alarmas"]:
            texto += (f" Zona de confusión: el modelo genera más falsas alarmas de lo esperado "
                      f"({conf['FP']}).")
        resultado.append({
            "cluster": int(c),
            "descripcion": texto,
            "n_reales": int(len(g)),
            "n_sinteticos": int(len(s)),
            "zona_confusion": bool(conf["pierde_urgentes"] or conf["falsas_alarmas"]),
            "tasa_error_modelo": conf["error"],
            "sinteticos_reconocidos": round(float(s["consistente"].mean()), 3),
        })
    return resultado
