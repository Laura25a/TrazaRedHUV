#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""levantar_demo.py — TrazaRed HUV.

Funciona igual en Windows, macOS y Linux (Python 3.10+ con las dependencias del
proyecto instaladas: requests, psycopg2-binary, pymongo, python-dotenv).

Orden de arranque (esperando a que cada servicio responda antes de continuar):
  0. Despierta Neon y MongoDB Atlas (evita el retardo del autosuspend)
  1. HAPI FHIR con Docker Compose      -> http://localhost:8081/fhir
  2. API FastAPI con uvicorn           -> http://localhost:8000/docs
  3. Dos Quick Tunnels de Cloudflare   -> URLs públicas (detección automática)

Las URLs se imprimen al final, quedan en URLs_demo.txt y se actualizan solas en
la sección "Disponibilidad en línea" del README (entre los marcadores
URLS-DEMO). Son Quick Tunnels: cambian en cada arranque.

Uso:
  python levantar_demo.py            # arranca todo (reutiliza lo que ya corra)
  python levantar_demo.py --stop     # detiene túneles, API y contenedor HAPI
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
DEMO = RAIZ / ".demo"
PIDS = DEMO / "pids.txt"
URLS_TXT = RAIZ / "URLs_demo.txt"
MARCADOR_RE = re.compile(r"(<!-- URLS-DEMO:ini -->).*?(<!-- URLS-DEMO:fin -->)", re.S)
RE_URL_TUNEL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
ES_WINDOWS = os.name == "nt"

import requests  # noqa: E402  (después de las rutas, igual que el resto del proyecto)


def ok(msg):    print(f"[OK]  {msg}")
def info(msg):  print(f"[..]  {msg}")
def error(msg):
    print(f"[XX]  {msg}")
    return 1


def espera_url(url: str, nombre: str, timeout: int = 120) -> bool:
    info(f"Esperando a {nombre} ({url})")
    for _ in range(timeout):
        try:
            r = requests.get(url, timeout=8)
            if r.ok:
                ok(f"{nombre} responde")
                return True
            # 5xx (p. ej. 530 = túnel muerto): no es éxito, seguimos esperando
        except requests.RequestException:
            pass
        # Si el resolvedor local cacheó un NXDOMAIN previo a la publicación del
        # DNS del túnel, probamos directo contra el edge de Cloudflare por IP
        # (el enrutado es por SNI/Host). Requiere curl (viene en Windows 10+).
        # Con -f, curl falla también en respuestas 5xx: evita falsos "OK" con
        # errores tipo 530 (túnel inexistente).
        curl = shutil.which("curl")
        host = url.split("//", 1)[1].split("/", 1)[0]
        if curl and "trycloudflare.com" in host:
            r = subprocess.run(
                [curl, "-sf", "-m", "8", "-o", os.devnull,
                 "--resolve", f"{host}:443:104.16.230.132", url],
                capture_output=True)
            if r.returncode == 0:
                ok(f"{nombre} responde (vía edge de Cloudflare)")
                return True
        time.sleep(1)
    error(f"{nombre} no respondió en {timeout}s")
    return False


def encontrar_cloudflared() -> str | None:
    for cand in (shutil.which("cloudflared"), shutil.which("cloudflared.exe"),
                 str(Path.home() / ".local" / "bin" /
                     ("cloudflared.exe" if ES_WINDOWS else "cloudflared")),
                 str(RAIZ / ("cloudflared.exe" if ES_WINDOWS else "cloudflared"))):
        if cand and Path(cand).exists():
            return cand
    return None


def pids_registrados() -> list[tuple[int, str]]:
    if not PIDS.exists():
        return []
    salida = []
    for linea in PIDS.read_text().splitlines():
        partes = linea.split(None, 1)
        if len(partes) == 2 and partes[0].isdigit():
            salida.append((int(partes[0]), partes[1].strip()))
    return salida


def matar(pid: int) -> None:
    try:
        if ES_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(pid, 15)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def detener_todo() -> None:
    info("Deteniendo la demo…")
    for pid, nombre in pids_registrados():
        matar(pid)
        ok(f"detenido: {nombre} (pid {pid})")
    PIDS.unlink(missing_ok=True)
    subprocess.run(["docker", "compose", "-f", str(RAIZ / "docker" / "docker-compose.yml"),
                    "down"], capture_output=True)
    ok("contenedores HAPI detenidos (si había)")
    print("Las URLs públicas dejaron de funcionar en el momento del stop.")


def despertar_bases() -> bool:
    info("Despertando Neon y MongoDB Atlas (autosuspend)…")
    try:
        from dotenv import dotenv_values
        import psycopg2, pymongo  # noqa: F401
        cfg = dotenv_values(RAIZ / "pass.env")
        psycopg2.connect(cfg["PG_CONNECTION_STRING"]).close()
        pymongo.MongoClient(cfg["MONGO_CONNECTION_STRING"],
                            serverSelectionTimeoutMS=8000).admin.command("ping")
        ok("PostgreSQL (Neon) y MongoDB (Atlas) responden")
        return True
    except Exception as e:  # noqa: BLE001
        error(f"No pude despertar las bases ({e}). Continúo, pero revisa pass.env.")
        return False


def arrancar(args: list[str], log: Path, nombre: str) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as fh:
        proc = subprocess.Popen(args, stdout=fh, stderr=subprocess.STDOUT,
                                cwd=str(RAIZ), creationflags=(
                                    subprocess.CREATE_NEW_PROCESS_GROUP if ES_WINDOWS else 0))
    with PIDS.open("a") as fh:
        fh.write(f"{proc.pid} {nombre}\n")
    return proc.pid


def iniciar_tunel(cloudflared: str, puerto: int, nombre: str) -> str:
    # los túneles previos mueren con sus URLs igual: los matamos por higiene
    for pid, nom in pids_registrados():
        if nom == f"tunel_{nombre}":
            matar(pid)
    log = DEMO / f"tunel_{nombre}.log"
    # Truncar SIEMPRE antes de arrancar: si el log quedara de una corrida
    # anterior, la búsqueda de URL encontraría la URL vieja (de un túnel ya
    # muerto) en vez de la del túnel recién creado.
    log.write_bytes(b"")
    arrancar([cloudflared, "tunnel", "--url", f"http://localhost:{puerto}",
              "--no-autoupdate"], log, f"tunel_{nombre}")
    for _ in range(60):
        if log.exists():
            m = RE_URL_TUNEL.search(log.read_text(errors="ignore"))
            if m:
                return m.group(0)
        time.sleep(1)
    raise SystemExit(error(f"el túnel {nombre} no entregó URL en 60s (mira {log})"))


def actualizar_readme(url_api: str, url_hapi: str, fecha: str) -> None:
    readme = RAIZ / "README.md"
    texto = readme.read_text(encoding="utf-8")
    bloque = (f"URLs públicas generadas el **{fecha}** con `levantar_demo` (Quick Tunnel:\n"
              f"efímeras — cambian en cada arranque):\n\n"
              f"- **API (FastAPI):** {url_api} — `/docs` verificado\n"
              f"- **Servidor FHIR (HAPI):** {url_hapi} — `/fhir/metadata` verificado\n")
    nuevo = MARCADOR_RE.sub(r"\1\n" + bloque + r"\2", texto)
    if nuevo == texto and "<!-- URLS-DEMO:ini -->" not in texto:
        error("no encontré los marcadores URLS-DEMO en el README (no lo actualicé)")
        return
    readme.write_text(nuevo, encoding="utf-8")
    ok("README actualizado (sección Disponibilidad en línea)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Arranque de la demo TrazaRed HUV")
    parser.add_argument("--stop", action="store_true", help="detiene todo lo registrado")
    args = parser.parse_args()

    DEMO.mkdir(exist_ok=True)
    if args.stop:
        detener_todo()
        return

    cloudflared = encontrar_cloudflared()
    if cloudflared is None:
        raise SystemExit(error("cloudflared no está instalado ni en PATH ni junto a este "
                               "script (ver README, sección Disponibilidad en línea)."))

    # 0. bases
    despertar_bases()

    # 1. HAPI
    info("Servidor HAPI FHIR (Docker)…")
    try:
        hapi_ya = requests.get("http://localhost:8081/fhir/metadata", timeout=8).ok
    except requests.RequestException:
        hapi_ya = False
    if hapi_ya:
        ok("HAPI ya estaba corriendo (http://localhost:8081/fhir)")
    else:
        subprocess.run(["docker", "compose", "-f", str(RAIZ / "docker" / "docker-compose.yml"),
                        "up", "-d"], check=True, capture_output=True)
        if not espera_url("http://localhost:8081/fhir/metadata", "HAPI FHIR", 300):
            raise SystemExit(1)

    # 2. API
    info("API FastAPI (uvicorn, puerto 8000)…")
    try:
        requests.get("http://localhost:8000/docs", timeout=8)
        ok("la API ya estaba corriendo (http://localhost:8000/docs)")
    except requests.RequestException:
        arrancar([sys.executable, "-m", "uvicorn", "--app-dir", str(RAIZ / "python"),
                  "main:app", "--host", "127.0.0.1", "--port", "8000"],
                 DEMO / "api.log", "api")
        if not espera_url("http://localhost:8000/docs", "API FastAPI", 90):
            raise SystemExit(1)

    # 3. túneles
    info("Quick Tunnels de Cloudflare…")
    url_api = iniciar_tunel(cloudflared, 8000, "api")
    ok(f"túnel API:    {url_api}")
    url_hapi = iniciar_tunel(cloudflared, 8081, "hapi")
    ok(f"túnel HAPI:   {url_hapi}")

    # 4. verificación + registro
    info("Verificando las URLs públicas…")
    time.sleep(3)  # margen para que el DNS de trycloudflare se publique
    if not espera_url(f"{url_api}/docs", "API pública (/docs)", 90):
        raise SystemExit(1)
    if not espera_url(f"{url_hapi}/fhir/metadata", "HAPI público (/fhir/metadata)", 120):
        raise SystemExit(1)

    fecha = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    URLS_TXT.write_text(
        f"# URLs públicas generadas por levantar_demo.py — {fecha}\n"
        f"API (FastAPI):    {url_api}\n"
        f"Servidor FHIR:    {url_hapi}\n", encoding="utf-8")
    actualizar_readme(url_api, url_hapi, fecha)

    print()
    print(f"================ DEMO LISTA ({fecha}) ================")
    print(f"  API pública:  {url_api}/docs")
    print(f"  HAPI público: {url_hapi}/fhir")
    print(f"  URLs también en {URLS_TXT.name}")
    print("=====================================================")
    print("Recuerda: probar desde datos móviles, y si quieres dejar estas URLs")
    print("en el repo, haz commit del README actualizado.")


if __name__ == "__main__":
    main()
