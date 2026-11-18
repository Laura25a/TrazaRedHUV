#!/usr/bin/env bash
# Crea únicamente motores efímeros; nunca reutiliza volúmenes ni lee .env.
set -euo pipefail
raiz=$(cd "$(dirname "$0")/.." && pwd)
interprete=${1:?Uso: bash scripts/probar_integracion.sh PYTHON CARPETA_EVIDENCIA [archivos pytest]}
salida=${2:?Indica una carpeta de evidencia fuera del repositorio}
shift 2
mkdir -p "$salida"
salida=$(cd "$salida" && pwd)
case "$salida/" in "$raiz/"*) echo "La evidencia debe quedar fuera del repositorio" >&2; exit 2;; esac
pg=trazared-prueba-pg-$$
mongo=trazared-prueba-mongo-$$
trap 'docker rm -fv "$pg" "$mongo" >/dev/null 2>&1 || true' EXIT
cd "$raiz"
# Los digests son los mismos del despliegue; trust solo en loopback, sin volumen.
docker run -d --name "$pg" -p 127.0.0.1::5432 -e POSTGRES_HOST_AUTH_METHOD=trust \
  -e POSTGRES_DB=trazared_validacion pgvector/pgvector:pg16@sha256:484e838076e04939d27d3e176e91c86eea96236126023be8d3059761122e579f >/dev/null
docker run -d --name "$mongo" -p 127.0.0.1::27017 \
  mongo:7@sha256:884c055bff8b7144ca44830d01826facd3083b33c4dea44b94a1476c62de3c32 >/dev/null
listo=0
for _ in $(seq 1 60); do
  if docker exec "$pg" pg_isready -h 127.0.0.1 -U postgres -d trazared_validacion >/dev/null 2>&1 && \
     docker exec "$mongo" mongosh --quiet --eval 'db.runCommand({ping:1}).ok' >/dev/null 2>&1; then listo=1; break; fi
  sleep 1
done
if [ "$listo" != 1 ]; then echo "Los motores de prueba no estuvieron listos en 60 segundos" >&2; exit 1; fi
export TRAZARED_PG_PRUEBAS="postgresql://postgres@$(docker port "$pg" 5432/tcp)/trazared_validacion"
export TRAZARED_MONGO_PRUEBAS="mongodb://$(docker port "$mongo" 27017/tcp)"
export TRAZARED_EVIDENCIA_DOCUMENTOS="$salida"
export TRAZARED_EVIDENCIA_ENTREGA="$salida"
if [ "$#" = 0 ]; then set -- pruebas/test_integridad.py pruebas/test_fhir.py pruebas/test_entregable.py; fi
"$interprete" -m pytest -q --tb=short --disable-warnings --junitxml="$salida/junit.xml" "$@"
