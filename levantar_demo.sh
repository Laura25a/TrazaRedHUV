#!/usr/bin/env bash
# =============================================================================
# levantar_demo.sh — TrazaRed HUV
#
# Arranca todo lo necesario para la demostración, en orden y esperando a que
# cada servicio responda antes de continuar (Fase 5, tarea 1 del plan):
#
#   0. Despierta Neon y MongoDB Atlas (evita el retardo del autosuspend)
#   1. HAPI FHIR con Docker Compose      -> http://localhost:8081/fhir
#   2. API FastAPI con uvicorn           -> http://localhost:8000/docs
#   3. Dos Quick Tunnels de Cloudflare   -> URLs públicas (detección automática)
#
# Las URLs públicas se imprimen al final, quedan en URLs_demo.txt y se
# actualizan solas en la sección "Disponibilidad en línea" del README
# (entre los marcadores URLS-DEMO). Son Quick Tunnels: cambian en cada
# arranque — el guion de la demo depende de este script, no de URLs memorizadas.
#
# Uso:
#   ./levantar_demo.sh           # arranca todo (reutiliza servicios ya activos)
#   ./levantar_demo.sh --stop    # detiene túneles, API y contenedor HAPI
# =============================================================================
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$RAIZ"

CARPETA_LOGS=".demo"
ARCHIVO_URLS="URLs_demo.txt"
ARCHIVO_PIDS="$CARPETA_LOGS/pids.txt"
mkdir -p "$CARPETA_LOGS"

azul()  { printf '\033[1;34m%s\033[0m\n' "$*"; }
verde() { printf '\033[1;32m%s\033[0m\n' "$*"; }
rojo()  { printf '\033[1;31m%s\033[0m\n' "$*"; }

esperar_url() { # <url> <nombre> <timeout_seg=180>
  local url="$1" nombre="$2" timeout="${3:-180}" intento=0 host=""
  printf 'Esperando a %s ' "$nombre"
  while true; do
    if curl -s -m 8 -o /dev/null "$url"; then break; fi
    # Si el resolvedor local cacheó un NXDOMAIN previo a la publicación del
    # DNS, verificamos directo contra el edge de Cloudflare (el enrutado es
    # por SNI/Host, las IPs de trycloudflare son anycast estables).
    host="$(printf '%s' "${url#http://}" | sed 's#^https://##' | cut -d/ -f1)"
    if [[ "$url" == *trycloudflare.com* ]] && \
       curl -s -m 8 -o /dev/null --resolve "$host:443:104.16.230.132" "$url"; then
      break
    fi
    intento=$((intento + 1))
    if [ "$intento" -ge "$timeout" ]; then
      printf '\n'; rojo "✗ $nombre no respondió en ${timeout}s ($url)"; exit 1
    fi
    if [ $((intento % 10)) -eq 0 ]; then resolvectl flush-caches 2>/dev/null || true; fi
    printf '.'; sleep 1
  done
  printf '\n'; verde "✓ $nombre responde ($url)"
}

encontrar_python() {
  local cand
  for cand in "$HOME/venv_global/bin/python" "$RAIZ/venv/bin/python" python3; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import requests, psycopg2, pymongo, dotenv" 2>/dev/null; then
      echo "$cand"; return 0
    fi
  done
  rojo "✗ Ningún Python tiene requests/psycopg2/pymongo/dotenv. Instala las dependencias del README."
  exit 1
}

CLOUDFLARED="$(command -v cloudflared || true)"
if [ -z "$CLOUDFLARED" ] && [ -x "$HOME/.local/bin/cloudflared" ]; then
  CLOUDFLARED="$HOME/.local/bin/cloudflared"
fi

# --- --stop ------------------------------------------------------------------
if [ "${1:-}" = "--stop" ]; then
  azul "Deteniendo la demo…"
  if [ -f "$ARCHIVO_PIDS" ]; then
    while read -r pid nombre; do
      [ -n "$pid" ] || continue
      if kill "$pid" 2>/dev/null; then verde "✓ detenido: $nombre (pid $pid)"; fi
    done < "$ARCHIVO_PIDS"
    rm -f "$ARCHIVO_PIDS"
  fi
  docker compose -f docker/docker-compose.yml down >/dev/null 2>&1 && verde "✓ contenedores HAPI detenidos" || true
  verde "Listo. (Las URLs públicas dejaron de funcionar en el momento del stop.)"
  exit 0
fi

[ -z "$CLOUDFLARED" ] && { rojo "✗ cloudflared no está instalado (ver README, sección Disponibilidad en línea)."; exit 1; }
command -v docker >/dev/null || { rojo "✗ docker no está instalado."; exit 1; }

# --- 0. Despertar Neon y MongoDB --------------------------------------------
azul "[0/4] Despertando Neon y MongoDB Atlas (autosuspend)…"
PY="$(encontrar_python)"
if "$PY" - <<'EOF'
import os
from dotenv import dotenv_values
cfg = dotenv_values("pass.env")
import psycopg2, pymongo
psycopg2.connect(cfg["PG_CONNECTION_STRING"]).close()
pymongo.MongoClient(cfg["MONGO_CONNECTION_STRING"], serverSelectionTimeoutMS=8000).admin.command("ping")
print("bases despiertas")
EOF
then verde "✓ PostgreSQL (Neon) y MongoDB (Atlas) responden"
else rojo "⚠ No pude despertar las bases (¿pass.env correcto?). Continúo, pero revisa antes de la demo."
fi

# --- 1. HAPI FHIR ------------------------------------------------------------
azul "[1/4] Servidor HAPI FHIR (Docker)…"
if curl -s -m 3 -o /dev/null http://localhost:8081/fhir/metadata; then
  verde "✓ HAPI ya estaba corriendo (http://localhost:8081/fhir)"
else
  docker compose -f docker/docker-compose.yml up -d >/dev/null
  esperar_url http://localhost:8081/fhir/metadata "HAPI FHIR" 300
fi

# --- 2. API FastAPI ----------------------------------------------------------
azul "[2/4] API FastAPI (uvicorn, puerto 8000)…"
if curl -s -m 3 -o /dev/null http://localhost:8000/docs; then
  verde "✓ la API ya estaba corriende (http://localhost:8000/docs)"
else
  : > "$CARPETA_LOGS/api.log"
  nohup "$PY" -m uvicorn --app-dir "$RAIZ/python" main:app --host 127.0.0.1 --port 8000 \
    >> "$CARPETA_LOGS/api.log" 2>&1 &
  echo "$! api" >> "$ARCHIVO_PIDS"
  esperar_url http://localhost:8000/docs "API FastAPI" 60
fi

# --- 3. Túneles de Cloudflare -------------------------------------------------
azul "[3/4] Quick Tunnels de Cloudflare…"
# Los túneles previos registrados se detienen: sus URLs ya murieron igual.
if [ -f "$ARCHIVO_PIDS" ] && grep -q ' tunel_' "$ARCHIVO_PIDS" 2>/dev/null; then
  grep ' tunel_' "$ARCHIVO_PIDS" | while read -r pid _; do kill "$pid" 2>/dev/null || true; done
  sed -i '/ tunel_/d' "$ARCHIVO_PIDS"
fi

iniciar_tunel() { # <puerto_local> <nombre>
  local puerto="$1" nombre="$2"
  local log="$CARPETA_LOGS/tunel_$nombre.log"
  local url=""
  : > "$log"
  nohup "$CLOUDFLARED" tunnel --url "http://localhost:$puerto" --no-autoupdate > "$log" 2>&1 &
  echo "$! tunel_$nombre" >> "$ARCHIVO_PIDS"
  for _ in $(seq 1 60); do
    url="$(grep -aoE 'https://[a-z0-9-]+\.trycloudflare\.com' "$log" | head -1 || true)"
    [ -n "$url" ] && { echo "$url"; return 0; }
    sleep 1
  done
  rojo "✗ el túnel $nombre no entregó URL en 60s (mira $log)"; exit 1
}

URL_API="$(iniciar_tunel 8000 api)"
verde "✓ túnel API:    $URL_API"
URL_HAPI="$(iniciar_tunel 8081 hapi)"
verde "✓ túnel HAPI:   $URL_HAPI"

# --- 4. Verificación y registro ----------------------------------------------
azul "[4/4] Verificando las URLs públicas…"
sleep 3   # margen para que el DNS de trycloudflare se publique
esperar_url "$URL_API/docs"    "API pública (/docs)"    90
esperar_url "$URL_HAPI/fhir/metadata" "HAPI público (/fhir/metadata)" 120

FECHA="$(date '+%Y-%m-%d %H:%M %Z')"
{
  echo "# URLs públicas generadas por levantar_demo.sh — $FECHA"
  echo "API (FastAPI):    $URL_API"
  echo "Servidor FHIR:    $URL_HAPI"
} > "$ARCHIVO_URLS"

# Actualiza la sección del README entre marcadores
"$PY" - "$URL_API" "$URL_HAPI" "$FECHA" <<'EOF'
import re, sys
url_api, url_hapi, fecha = sys.argv[1:4]
ruta = "README.md"
texto = open(ruta, encoding="utf-8").read()
bloque = (f"URLs públicas generadas el **{fecha}** con `levantar_demo.sh` (Quick Tunnel:\n"
          f"efímeras — cambian en cada arranque):\n\n"
          f"- **API (FastAPI):** {url_api} — `/docs` verificado\n"
          f"- **Servidor FHIR (HAPI):** {url_hapi} — `/fhir/metadata` verificado\n")
nuevo = re.sub(r"(<!-- URLS-DEMO:ini -->).*?(<!-- URLS-DEMO:fin -->)",
               r"\1\n" + bloque + r"\2", texto, flags=re.S)
assert nuevo != texto or "<!-- URLS-DEMO:ini -->" in texto
open(ruta, "w", encoding="utf-8").write(nuevo)
EOF

verde "✓ README actualizado (sección Disponibilidad en línea)"
echo
azul "================ DEMO LISTA ($FECHA) ================"
echo "  API pública:  $URL_API/docs"
echo "  HAPI público: $URL_HAPI/fhir"
echo "  URLs también en $ARCHIVO_URLS"
azul "====================================================="
echo "Recuerda: probar desde datos móviles, y si quieres dejar estas URLs"
echo "en el repo, haz commit del README actualizado."
