"""
Servicio de IA de TrazaRed HUV (contenedor "ml", puerto 8001 solo dentro de Docker).

Esqueleto que deja listo la Persona 1 para que el docker-compose levante todo
(R05). La Persona 2 lo completa con:
  * cargar el modelo entrenado (joblib) al arrancar,
  * POST /predecir  -> recibe SOLO variables clínicas + id seudónimo (R12) y
                       devuelve la probabilidad y el nivel de riesgo,
  * GET  /modelo    -> versión, métricas y umbrales,
  * POST /entrenar  -> reentrena (la API solo deja llamarlo al admin).

Importante: este servicio NO se publica hacia afuera (no tiene ruta en nginx
ni pasa por el túnel). Solo la API le habla, por http://ml:8001, y es la API
la que quita los datos personales antes de llamarlo.
"""
from pathlib import Path
from typing import Optional
from entrenar import entrenar, COLS_NUM, COLS_CAT, RUTA_CSV
from sklearn.cluster import KMeans
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
import joblib
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="TrazaRed HUV · servicio de IA", version="0.1.0")

# El modelo se carga una sola vez, al arrancar
RUTA_MODELO = Path(__file__).parent / "modelo" / "ktas_modelo.joblib"
paquete = joblib.load(RUTA_MODELO) if RUTA_MODELO.exists() else None

# 1.x fue la regresión logística; 2.x es XGBoost
VERSION_MAYOR = 2


class DatosClinicos(BaseModel):
    """Solo variables clínicas + id seudónimo: nada de nombre, documento, etc. (R12)."""
    id_seudonimo: str
    sexo: Optional[int] = None
    edad: Optional[int] = None
    pacientes_por_hora: Optional[int] = None
    modo_llegada: int
    lesion: int
    mental: int
    dolor: int
    nrs_dolor: Optional[float] = None
    sbp: Optional[float] = None
    dbp: Optional[float] = None
    fc: Optional[float] = None
    fr: Optional[float] = None
    temperatura: Optional[float] = None
    saturacion: Optional[float] = None


# Nombre del contrato -> nombre de columna con el que se entrenó el modelo
COLUMNAS = {
    "sexo": "Sex", "edad": "Age", "pacientes_por_hora": "Patients number per hour",
    "modo_llegada": "Arrival mode", "lesion": "Injury", "mental": "Mental",
    "dolor": "Pain", "nrs_dolor": "NRS_pain", "sbp": "SBP", "dbp": "DBP",
    "fc": "HR", "fr": "RR", "temperatura": "BT", "saturacion": "Saturation",
}


@app.get("/health")
def health():
    return {"servicio": "ml", "estado": "ok", "modelo_cargado": paquete is not None}


@app.get("/modelo")
def modelo():
    if paquete is None:
        raise HTTPException(503, "Modelo no cargado")
    claves = ("version", "algoritmo", "metricas", "umbral", "umbral_critico",
              "fecha", "sklearn", "xgboost")
    # .get: un modelo guardado antes del cambio no tiene "algoritmo" ni "xgboost"
    return {k: paquete.get(k) for k in claves}


@app.post("/predecir")
def predecir(d: DatosClinicos):
    if paquete is None:
        raise HTTPException(503, "Modelo no cargado")

    datos = d.model_dump()
    # Mismas reglas de limpieza que en el entrenamiento
    if datos["dolor"] == 0 and datos["nrs_dolor"] is None:
        datos["nrs_dolor"] = 0
    if datos["modo_llegada"] in (6, 7):
        datos["modo_llegada"] = 5

    fila = {col: (np.nan if datos[k] is None else datos[k]) for k, col in COLUMNAS.items()}
    X = pd.DataFrame([fila])
    X[paquete["columnas_num"]] = X[paquete["columnas_num"]].astype(float)

    prob = float(paquete["modelo"].predict_proba(X)[0, 1])
    critico = prob >= paquete["umbral_critico"]
    urgente = prob >= paquete["umbral"]
    return {
        "id_seudonimo": d.id_seudonimo,
        "probabilidad": round(prob, 4),
        "urgente": urgente,
        "critico": critico,
        "nivel": "critico" if critico else ("urgente" if urgente else "no urgente"),
        "version_modelo": paquete["version"],
    }


def siguiente_version(actual):
    """Primer reentrenamiento tras el cambio: 2.0; después 2.1, 2.2..."""
    mayor, menor = (int(x) for x in actual.split("."))
    if mayor < VERSION_MAYOR:
        return f"{VERSION_MAYOR}.0"
    return f"{mayor}.{menor + 1}"


@app.post("/entrenar")
def reentrenar():
    global paquete
    version = siguiente_version(paquete["version"]) if paquete else f"{VERSION_MAYOR}.0"
    paquete = entrenar(version)
    return {"version": paquete["version"], "metricas": paquete["metricas"]}


# R07: agrupamiento de pacientes reales y sintéticos
RUTA_SINTETICOS = Path(__file__).parent / "data" / "pacientes_sinteticos.csv"


@app.get("/clusters")
def obtener_clusters():
    if not RUTA_CSV.exists():
        raise HTTPException(503, "No se encuentra el dataset real de KTAS")

    if not RUTA_SINTETICOS.exists():
        raise HTTPException(503, "No se encuentra el dataset de pacientes sintéticos")

    # Leer y limpiar los datos reales como en el notebook de agrupamiento.
    df = pd.read_csv(RUTA_CSV, sep=";", encoding="latin-1")
    df = df.replace(["??", "#BOÞ!", "#BOÃž!"], np.nan)

    for col in COLS_NUM:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Aplicar las mismas reglas de limpieza del entrenamiento
    df.loc[df["Pain"] == 0, "NRS_pain"] = 0
    df["Arrival mode"] = df["Arrival mode"].replace({6: 5, 7: 5})

    df["KTAS_expert"] = pd.to_numeric(
        df["KTAS_expert"], errors="coerce"
    )

    df = df.dropna(subset=["KTAS_expert"]).copy()

    X = df[COLS_NUM + COLS_CAT]
    urgente = (df["KTAS_expert"] <= 3).astype(int)

    # Mismo preprocesamiento y configuración del notebook.
    pre = ColumnTransformer([
        ("num", Pipeline([
            ("mediana", SimpleImputer(strategy="median")),
            ("escala", StandardScaler()),
        ]), COLS_NUM),
        ("cat", OneHotEncoder(handle_unknown="ignore"), COLS_CAT),
    ])

    Z = pre.fit_transform(X)

    if hasattr(Z, "toarray"):
        Z = Z.toarray()

    kmeans = KMeans(
        n_clusters=4,
        n_init=10,
        random_state=42,
    )
    etiquetas = kmeans.fit_predict(Z)

    reales = X.copy()
    reales["cluster"] = etiquetas
    reales["urgente"] = urgente.to_numpy()

    # El archivo sintético ya contiene la etiqueta de cluster.
    sinteticos = pd.read_csv(RUTA_SINTETICOS)

    if "cluster" not in sinteticos.columns:
        raise HTTPException(
            503, "El CSV sintético no contiene la columna cluster"
        )

    sinteticos["cluster"] = pd.to_numeric(
        sinteticos["cluster"], errors="coerce"
    )

    def mostrar_numero(valor, decimales=1):
        if pd.isna(valor):
            return "no disponible"
        return f"{valor:.{decimales}f}"

    resultado = []

    for cluster in range(4):
        grupo = reales[reales["cluster"] == cluster]
        grupo_sintetico = sinteticos[
            sinteticos["cluster"] == cluster
        ]

        edad = pd.to_numeric(
            grupo["Age"], errors="coerce"
        ).median()
        dolor = pd.to_numeric(
            grupo["NRS_pain"], errors="coerce"
        ).median()
        saturacion = pd.to_numeric(
            grupo["Saturation"], errors="coerce"
        ).median()
        pct_urgente = grupo["urgente"].mean() * 100

        descripcion = (
            f"Edad mediana: {mostrar_numero(edad)} años; "
            f"dolor NRS mediano: {mostrar_numero(dolor)}; "
            f"saturación mediana: {mostrar_numero(saturacion)}; "
            f"{mostrar_numero(pct_urgente)}% de los registros "
            "históricos del grupo tienen KTAS ≤ 3."
        )

        resultado.append({
            "cluster": cluster,
            "descripcion": descripcion,
            "n_reales": int(len(grupo)),
            "n_sinteticos": int(len(grupo_sintetico)),
        })

    return {"clusters": resultado}