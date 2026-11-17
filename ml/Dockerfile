# Imagen del servicio de IA de TrazaRed HUV (R05: cada servicio con build tiene su Dockerfile).
# Contexto: raíz del repositorio (también incluye las reglas clínicas compartidas).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Bogota

WORKDIR /app

# 1) Dependencias primero: si solo cambia el código, Docker reutiliza esta capa
COPY ml/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2) Código del servicio (y, cuando existan, el modelo entrenado y los datos)
COPY ml/ .
COPY python/validacion_clinica.py .

EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
