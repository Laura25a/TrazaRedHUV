from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
import xgboost
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.metrics import confusion_matrix, f1_score, precision_score, roc_auc_score
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from xgboost import XGBClassifier

BASE = Path(__file__).parent
RUTA_CSV = BASE / "data" / "ktas.csv"
RUTA_MODELO = BASE / "modelo" / "ktas_modelo.joblib"
UMBRAL, UMBRAL_CRITICO = 0.4, 0.9

COLS_NUM = ["Age", "Patients number per hour", "NRS_pain", "SBP", "DBP", "HR", "RR", "BT", "Saturation"]
COLS_CAT = ["Sex", "Arrival mode", "Injury", "Mental", "Pain"]


def entrenar(version):
    # Limpieza (igual que en el cuaderno)
    df = pd.read_csv(RUTA_CSV, sep=";", encoding="latin-1")
    df = df.replace(["??", "#BOÞ!"], np.nan)
    for c in ["NRS_pain", "SBP", "DBP", "HR", "RR", "BT", "Saturation"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["urgente"] = (df["KTAS_expert"] <= 3).astype(int)
    df.loc[df["Pain"] == 0, "NRS_pain"] = 0
    df["Arrival mode"] = df["Arrival mode"].replace({6: 5, 7: 5})

    X, y = df[COLS_NUM + COLS_CAT], df["urgente"]
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, stratify=y, random_state=42
    )

    pre = ColumnTransformer([
        ("num", Pipeline([("mediana", SimpleImputer(strategy="median")),
                          ("escala", StandardScaler())]), COLS_NUM),
        ("cat", OneHotEncoder(handle_unknown="ignore"), COLS_CAT),
    ])
    # Mismo algoritmo y parámetros que eligió la comparación del cuaderno
    modelo = Pipeline([
        ("pre", pre),
        ("clf", XGBClassifier(n_estimators=200, max_depth=3,
                              learning_rate=0.05, random_state=42)),
    ])

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    auc_cv = cross_val_score(modelo, X_train, y_train, cv=cv, scoring="roc_auc").mean()
    modelo.fit(X_train, y_train)

    proba = modelo.predict_proba(X_test)[:, 1]
    pred = (proba >= UMBRAL).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_test, pred).ravel()

    paquete = {
        "modelo": modelo, "version": version,
        "algoritmo": "XGBoost",
        "umbral": UMBRAL, "umbral_critico": UMBRAL_CRITICO,
        "columnas_num": COLS_NUM, "columnas_cat": COLS_CAT,
        "metricas": {
            "auc_prueba": round(float(roc_auc_score(y_test, proba)), 3),
            "auc_cv": round(float(auc_cv), 3),
            "sensibilidad": round(float(tp / (tp + fn)), 3),
            "especificidad": round(float(tn / (tn + fp)), 3),
            "precision": round(float(precision_score(y_test, pred)), 3),
            "f1": round(float(f1_score(y_test, pred)), 3),
            "matriz_confusion": {"TN": int(tn), "FP": int(fp), "FN": int(fn), "TP": int(tp)},
        },
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "sklearn": sklearn.__version__,
        "xgboost": xgboost.__version__,
    }
    RUTA_MODELO.parent.mkdir(exist_ok=True)
    joblib.dump(paquete, RUTA_MODELO)
    return paquete