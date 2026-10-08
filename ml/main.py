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
from fastapi import FastAPI

app = FastAPI(title="TrazaRed HUV · servicio de IA", version="0.1.0")


@app.get("/health")
def health():
    return {"servicio": "ml", "estado": "ok", "modelo_cargado": False}
