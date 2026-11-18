# Imagen de la API de TrazaRed HUV.
# Se construye desde la RAÍZ del proyecto (context: .. en docker-compose.yml),
# por eso las rutas de COPY son relativas a la raíz.
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Bogota

WORKDIR /app

# 1) Dependencias primero: si solo cambia el código, Docker reutiliza esta capa
COPY requirements.txt .
COPY requirements-api.lock.txt .
RUN pip install --no-cache-dir -r requirements.txt -c requirements-api.lock.txt && pip check

# 2) Código de la API y la interfaz (main.py también la publica en /app)
COPY python/ ./python/
COPY frontend/ ./frontend/
# 3) CSV del dataset del proyecto final (los carga cargar_dataset.py)
COPY data/ ./data/
# 4) Esquema de la base: la API lo aplica en cada arranque (es idempotente)
COPY db/ ./db/
# 5) Baterías de prueba, para correrlas dentro del contenedor:
#      docker compose exec api python /app/pruebas/probar_final.py
COPY pruebas/*.py ./pruebas/

WORKDIR /app/python
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
