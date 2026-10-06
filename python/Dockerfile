# Imagen de la API de TrazaRed HUV.
# Se construye desde la RAÍZ del proyecto (context: .. en docker-compose.yml),
# por eso las rutas de COPY son relativas a la raíz.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Bogota

WORKDIR /app

# 1) Dependencias primero: si solo cambia el código, Docker reutiliza esta capa
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2) Código de la API y la interfaz (main.py también la publica en /app)
COPY python/ ./python/
COPY frontend/ ./frontend/
# 3) CSV del dataset del proyecto final (los carga cargar_dataset.py)
COPY data/ ./data/

WORKDIR /app/python
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
