import hashlib
from datetime import datetime
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile

import joblib
import numpy as np
import pandas as pd
import sklearn
import xgboost
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.metrics import confusion_matrix, f1_score, precision_score, roc_auc_score
from sklearn.model_selection import GridSearchCV, StratifiedKFold, cross_val_predict, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from xgboost import XGBClassifier

BASE = Path(__file__).parent
RUTA_CSV = BASE / "data" / "ktas.csv"
RUTA_MODELO = BASE / "modelo" / "ktas_modelo.joblib"
UMBRAL, UMBRAL_CRITICO = 0.4, 0.9
SEMILLA = 42
VERSION_EVALUACION = 1

COLS_NUM = ["Age", "Patients number per hour", "NRS_pain", "SBP", "DBP", "HR", "RR", "BT", "Saturation"]
COLS_CAT = ["Sex", "Arrival mode", "Injury", "Mental", "Pain"]


def cargar_ktas(fuente=None):
    """Lee y limpia ktas.csv. Es la ÚNICA copia de la limpieza (la usan el
    entrenamiento supervisado y el agrupamiento), igual que en el cuaderno."""
    df = pd.read_csv(RUTA_CSV if fuente is None else fuente, sep=";", encoding="latin-1")
    df = df.replace(["??", "#BOÞ!", "#BOÃž!"], np.nan)
    for c in ["NRS_pain", "SBP", "DBP", "HR", "RR", "BT", "Saturation"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["KTAS_expert"] = pd.to_numeric(df["KTAS_expert"], errors="coerce")
    df = df.dropna(subset=["KTAS_expert"]).copy()
    df["urgente"] = (df["KTAS_expert"] <= 3).astype(int)
    df.loc[df["Pain"] == 0, "NRS_pain"] = 0
    df["Arrival mode"] = df["Arrival mode"].replace({6: 5, 7: 5})
    return df


def crear_preprocesador(estrategia="mediana"):
    numericas = "passthrough"
    if estrategia == "mediana":
        numericas = Pipeline([("mediana", SimpleImputer(strategy="median")),
                              ("escala", StandardScaler())])
    return ColumnTransformer([
        ("num", numericas, COLS_NUM),
        ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), COLS_CAT),
    ])


def crear_clasificador():
    return XGBClassifier(n_estimators=200, max_depth=3, learning_rate=0.05,
                         random_state=SEMILLA, n_jobs=1)


def _medir(y, proba):
    pred = (proba >= UMBRAL).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
    return {
        "auc": round(float(roc_auc_score(y, proba)), 3) if y.nunique() == 2 else None,
        "sensibilidad": round(float(tp / (tp + fn)), 3) if tp + fn else None,
        "especificidad": round(float(tn / (tn + fp)), 3) if tn + fp else None,
        "precision": round(float(precision_score(y, pred, zero_division=0)), 3),
        "f1": round(float(f1_score(y, pred, zero_division=0)), 3),
        "n": len(y),
        "matriz_confusion": {"TN": int(tn), "FP": int(fp), "FN": int(fn), "TP": int(tp)},
    }


def entrenar(version, guardar=True):
    contenido = RUTA_CSV.read_bytes()
    df = cargar_ktas(BytesIO(contenido))
    X_train, X_test, y_train, y_test = train_test_split(
        df[COLS_NUM + COLS_CAT], df["urgente"], test_size=0.2,
        stratify=df["urgente"], random_state=SEMILLA,
    )
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=SEMILLA)
    segmentos = {
        "SpO₂ registrada": X_train["Saturation"].notna(),
        "SpO₂ ausente": X_train["Saturation"].isna(),
        "Mujeres": X_train["Sex"] == 1,
        "Hombres": X_train["Sex"] == 2,
        "Menores de 65 años": X_train["Age"] < 65,
        "65 años o más": X_train["Age"] >= 65,
    }
    comparacion, busquedas, subgrupos = [], {}, {}
    for estrategia in ("mediana", "faltantes_nativos"):
        pipeline = Pipeline([("pre", crear_preprocesador(estrategia)),
                             ("clf", crear_clasificador())])
        busqueda = GridSearchCV(
            pipeline, {"clf__max_depth": [2, 3, 5], "clf__n_estimators": [100, 200],
                       "clf__learning_rate": [0.05, 0.1]},
            cv=cv, scoring="roc_auc", return_train_score=True, error_score="raise",
        ).fit(X_train, y_train)
        busquedas[estrategia] = busqueda
        resultados = busqueda.cv_results_
        for i, params in enumerate(resultados["params"]):
            auc_train = resultados["mean_train_score"][i]
            auc_val = resultados["mean_test_score"][i]
            comparacion.append({
                "estrategia": estrategia,
                "parametros": {k.removeprefix("clf__"): v for k, v in params.items()},
                "auc_entrenamiento": round(float(auc_train), 4),
                "auc_validacion": round(float(auc_val), 4),
                "brecha": round(float(auc_train - auc_val), 4),
            })
        # Los subgrupos comparten folds con la selección. Son un diagnóstico de
        # desarrollo, no una segunda estimación independiente.
        oof = cross_val_predict(busqueda.best_estimator_, X_train, y_train, cv=cv,
                                method="predict_proba")[:, 1]
        subgrupos[estrategia] = {
            grupo: _medir(y_train[mascara], oof[mascara.to_numpy()])
            for grupo, mascara in segmentos.items() if mascara.any()
        }

    elegida = max(busquedas, key=lambda estrategia: busquedas[estrategia].best_score_)
    seleccion = busquedas[elegida]
    modelo = seleccion.best_estimator_
    general = _medir(y_test, modelo.predict_proba(X_test)[:, 1])
    paquete = {
        "modelo": modelo, "version": version,
        "algoritmo": "XGBoost", "umbral": UMBRAL, "umbral_critico": UMBRAL_CRITICO,
        "columnas_num": COLS_NUM, "columnas_cat": COLS_CAT,
        "metricas": {**general, "auc_prueba": general["auc"],
                     "auc_cv": round(float(seleccion.best_score_), 3)},
        "evaluacion": {
            "version": VERSION_EVALUACION, "imputacion_elegida": elegida,
            "hiperparametros": {k.removeprefix("clf__"): v for k, v in seleccion.best_params_.items()},
            "candidatos": comparacion, "subgrupos": subgrupos,
            "prueba": {"dataset_sha256": hashlib.sha256(contenido).hexdigest(),
                       "semilla": SEMILLA, "filas": sorted(map(int, X_test.index)),
                       "n_desarrollo": len(X_train), "n_prueba": len(X_test), "folds": cv.n_splits},
        },
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "sklearn": sklearn.__version__,
        "xgboost": xgboost.__version__,
    }
    if guardar:
        guardar_paquete(paquete, RUTA_MODELO)
    return paquete


def guardar_paquete(paquete, ruta):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=ruta.parent, delete=False) as archivo:
        temporal = Path(archivo.name)
    try:
        joblib.dump(paquete, temporal)
        temporal.replace(ruta)
    finally:
        temporal.unlink(missing_ok=True)
