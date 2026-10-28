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
from agrupacion import detalle_clusters, resumen_clusters
from entrenar import entrenar, RUTA_CSV
import joblib
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

app = FastAPI(title="TrazaRed HUV · servicio de IA", version="0.1.0")

# El modelo se carga una sola vez, al arrancar
RUTA_MODELO = Path(__file__).parent / "modelo" / "ktas_modelo.joblib"
paquete = joblib.load(RUTA_MODELO) if RUTA_MODELO.exists() else None

# 1.x fue la regresión logística; 2.x es XGBoost
VERSION_MAYOR = 2


class DatosClinicos(BaseModel):
    """Solo variables clínicas + id seudónimo: nada de nombre, documento, etc. (R12).

    extra="forbid": si llega un campo que no está aquí (nombre, documento...), 422.
    Los rangos son límites clínicamente plausibles: un valor imposible (saturación
    500) responde 422 antes de llegar al modelo.
    """
    model_config = ConfigDict(extra="forbid")

    id_seudonimo: str = Field(min_length=1, max_length=40)
    sexo: Optional[int] = Field(None, ge=1, le=2)
    edad: Optional[int] = Field(None, ge=0, le=120)
    pacientes_por_hora: Optional[int] = Field(None, ge=0, le=200)
    modo_llegada: int = Field(ge=1, le=7)
    lesion: int = Field(ge=1, le=2)
    mental: int = Field(ge=1, le=4)
    dolor: int = Field(ge=0, le=1)
    nrs_dolor: Optional[float] = Field(None, ge=0, le=10)
    sbp: Optional[float] = Field(None, ge=40, le=300)
    dbp: Optional[float] = Field(None, ge=20, le=200)
    fc: Optional[float] = Field(None, ge=20, le=250)
    fr: Optional[float] = Field(None, ge=4, le=80)
    temperatura: Optional[float] = Field(None, ge=30, le=45)
    saturacion: Optional[float] = Field(None, ge=0, le=100)


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
    if datos["dolor"] == 0:          # igual que en el entrenamiento: sin dolor, NRS = 0
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


# R07: agrupamiento de pacientes reales y sintéticos (lógica en agrupacion.py)
@app.get("/clusters")
def obtener_clusters():
    if not RUTA_CSV.exists():
        raise HTTPException(503, "No se encuentra el dataset real de KTAS")
    # La primera llamada entrena (unos segundos); las siguientes salen de memoria
    return {"clusters": resumen_clusters()}

@app.get("/clusters/detalle")
def obtener_detalle_clusters():
    """Perfiles por grupo, mapa PCA y calidad del agrupamiento (para el tablero)."""
    if not RUTA_CSV.exists():
        raise HTTPException(503, "No se encuentra el dataset real de KTAS")
    return detalle_clusters()

# ═════════════════════════════════════════════════════════════════════════════
# IA DEL REGISTRO DE REMISIONES (rediseño "libro de radicación")
# ═════════════════════════════════════════════════════════════════════════════
# La lógica vive en remisiones_modelo.py. Aquí solo van las rutas. Igual que
# /predecir, NADA de esto recibe nombre ni documento (R12): la API manda un id
# seudónimo, las variables clínicas/administrativas y la bitácora de contactos.
from typing import List
import warnings

import remisiones_modelo as rem

warnings.filterwarnings("ignore", category=UserWarning)


def _cargar_o_entrenar_remisiones():
    """El volumen ml_modelo puede venir de antes del rediseño (sin este .joblib),
    o el .joblib puede venir de otra versión de Python: en ambos casos se entrena
    de nuevo al arrancar (tarda unos segundos)."""
    try:
        p = rem.cargar_paquete()
        if p is not None:
            return p
    except Exception as e:          # pickle de otra versión
        print(f"[ml] no se pudo cargar el modelo de remisiones ({e}); se reentrena")
    try:
        return rem.entrenar_remisiones()
    except Exception as e:
        print(f"[ml] no se pudo entrenar el modelo de remisiones: {e}")
        return None


paquete_rem = _cargar_o_entrenar_remisiones()


def _rem():
    if paquete_rem is None:
        raise HTTPException(503, "Modelo de remisiones no cargado")
    return paquete_rem


class ContactoBitacora(BaseModel):
    model_config = ConfigDict(extra="ignore")       # contactado_por, medio... se ignoran
    fecha: Optional[str] = None
    institucion_contactada: str = Field(max_length=200)
    respuesta: str = Field(max_length=500)


class DatosRemision(BaseModel):
    """Variables de la remisión. extra="forbid": un nombre o documento que se
    cuele responde 422 (R12), igual que en /predecir."""
    model_config = ConfigDict(extra="forbid")

    id_seudonimo: str = Field(min_length=1, max_length=40)
    edad: Optional[int] = Field(None, ge=0, le=120)
    genero: Optional[str] = Field(None, pattern="^[FM]$")
    eps: Optional[str] = Field(None, max_length=80)
    sale_del_huv: bool = True
    convenio_vigente: bool = False
    motivo_grave: bool = False
    motivo: Optional[str] = Field(None, max_length=500)   # diagnóstico, sin datos personales
    fc: Optional[float] = Field(None, ge=20, le=250)
    pas: Optional[float] = Field(None, ge=40, le=300)
    pad: Optional[float] = Field(None, ge=20, le=200)
    fr: Optional[float] = Field(None, ge=4, le=80)
    spo2: Optional[float] = Field(None, ge=0, le=100)
    temp: Optional[float] = Field(None, ge=30, le=45)
    glasgow: Optional[float] = Field(None, ge=3, le=15)
    contactos: List[ContactoBitacora] = []


class TextoRespuesta(BaseModel):
    texto: str = Field(min_length=1, max_length=500)


@app.get("/remisiones/modelo")
def modelo_remisiones():
    p = _rem()
    return {k: p[k] for k in ("version", "entrenado_en", "metricas", "importancias",
                              "abiertas", "datos", "librerias")}


@app.post("/remisiones/pronostico")
def pronostico_remision(d: DatosRemision):
    datos = d.model_dump()
    contactos = datos.pop("contactos")
    salida = rem.pronosticar(_rem(), datos, contactos)
    salida["id_seudonimo"] = d.id_seudonimo
    return salida


@app.post("/remisiones/clasificar")
def clasificar_respuesta(t: TextoRespuesta):
    return rem.clasificar(_rem(), t.texto)


@app.get("/remisiones/causas")
def causas():
    return rem.CAUSAS


@app.post("/remisiones/entrenar")
def reentrenar_remisiones():
    global paquete_rem
    paquete_rem = rem.entrenar_remisiones()
    return {"version": paquete_rem["version"], "metricas": paquete_rem["metricas"]}
