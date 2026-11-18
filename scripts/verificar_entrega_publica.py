"""Verificación pública de lectura: HTTPS, API, contrato y frontend actual."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

import requests

RAIZ = Path(__file__).resolve().parents[1]
SERVICIOS = ("api", "postgres", "mongo", "pacs", "fhir")


def salud_completa(respuesta):
    return respuesta.status_code == 200 and all(respuesta.json().get(k) == "ok" for k in SERVICIOS)


def estatico_correcto(respuesta, local):
    mime = respuesta.headers.get("content-type", "")
    tipo = "text/css" if local.suffix == ".css" else "javascript" if local.suffix == ".js" else "text/html"
    return (respuesta.status_code == 200 and tipo in mime
            and hashlib.sha256(respuesta.content).digest() == hashlib.sha256(local.read_bytes()).digest())


def guardar_json(destino, reporte):
    destino.parent.mkdir(parents=True, exist_ok=True)
    # El reporte no guarda cuerpos, nombres, documentos, tokens ni variables de entorno.
    fd = os.open(destino, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as archivo:
        json.dump(reporte, archivo, ensure_ascii=False, indent=2)
        archivo.write("\n")


def verificar(url, salida, navegador=False):
    resultados = []
    def anotar(requisito, caso, pasa, codigo=None, **extra):
        resultados.append(dict(requisito=requisito, caso=caso, estado="pasa" if pasa else "falla",
                               http=codigo, **extra))
    def get(ruta):
        return requests.get(url + ruta, timeout=35, allow_redirects=False,
                            headers={"Cache-Control": "no-cache"})
    acciones = [("L03", "salud_completa", "/api/health", salud_completa),
                ("L04", "api_exige_sesion", "/api/me", lambda r: r.status_code == 401),
                ("L04", "hapi_protegido", "/fhir/Patient", lambda r: r.status_code == 404)]
    for req, caso, ruta, comprobar in acciones:
        try:
            r = get(ruta)
            anotar(req, caso, comprobar(r), r.status_code)
        except (requests.RequestException, ValueError):
            anotar(req, caso, False)
    try:
        r = get("/api/openapi.json")
        paths = r.json().get("paths", {})
        esperadas = {"/remisiones": "post", "/gestiones-contacto": "post",
                    "/interop/conciliaciones/{provisional_id}": "post",
                    "/pacientes/{paciente_id}/historia.fhir.json": "get"}
        anotar("L04", "openapi_flujos_actuales", r.status_code == 200 and
               all(m in paths.get(p, {}) for p, m in esperadas.items()), r.status_code)
    except (requests.RequestException, ValueError):
        anotar("L04", "openapi_flujos_actuales", False)
    archivos = [RAIZ / "frontend/index.html", RAIZ / "frontend/privacidad.html"]
    archivos += sorted((RAIZ / "frontend/css").rglob("*.css"))
    archivos += sorted((RAIZ / "frontend/js").rglob("*.js"))
    for local in archivos:
        ruta = "/" + local.relative_to(RAIZ / "frontend").as_posix()
        try:
            r = get(ruta)
            anotar("L03", "recurso:" + ruta, estatico_correcto(r, local), r.status_code,
                   sha256_local=hashlib.sha256(local.read_bytes()).hexdigest())
        except requests.RequestException:
            anotar("L03", "recurso:" + ruta, False)
    if navegador:
        sys.path.insert(0, str(RAIZ / "pruebas"))
        from probar_front_publico import verificar_entrada
        from playwright.sync_api import sync_playwright
        recorridos = (("/", "/", False), ("/index.html", "/index.html", False),
                      ("/app", "/", False), ("/app/", "/", False),
                      ("/app/index.html?origen=prueba", "/", True), ("/libro/remisiones/", "/", False))
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for n, (ruta, final, movil) in enumerate(recorridos):
                try:
                    captura = salida.parent / f"entrada-publica-{n}.png"
                    recursos = verificar_entrada(browser, url, ruta, final, movil, captura)
                    anotar("L04", "spa:" + ruta, True, recursos=recursos, captura=captura.name)
                except Exception as error:
                    anotar("L04", "spa:" + ruta, False, error=type(error).__name__)
            browser.close()
    else:
        resultados.append(dict(requisito="L04", caso="spa_navegador", estado="pendiente", http=None))
    resultados.append(dict(requisito="L05", caso="R33_equipo_real", estado="pendiente",
                           motivo="Sin implementar ni ejecutar hasta nueva orden"))
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=RAIZ, text=True).strip()
    estado = subprocess.check_output(["git", "status", "--porcelain", "--", ".", ":(exclude)pruebas/descargas"],
                                    cwd=RAIZ, text=True)
    reporte = {"fecha_utc": datetime.now(timezone.utc).isoformat(), "url": url,
               "revision_local": commit, "cambios_locales": bool(estado.strip()),
               "alcance": "GET anónimos y navegador sin sesión; hashes de frontend. No acredita backend idéntico, roles autenticados ni R33.",
               "resultados": resultados}
    guardar_json(salida, reporte)
    resumen = {e: sum(r["estado"] == e for r in resultados) for e in ("pasa", "falla", "pendiente")}
    print(resumen)
    return int(bool(resumen["falla"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://trazared.kogcake.com")
    parser.add_argument("--salida", type=Path, required=True)
    parser.add_argument("--navegador", action="store_true")
    args = parser.parse_args()
    url = args.url.rstrip("/")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query
            or parsed.fragment or parsed.username or parsed.password):
        parser.error("Usa un origen HTTPS sin ruta, credenciales ni parámetros")
    return verificar(url, args.salida, args.navegador)


if __name__ == "__main__":
    raise SystemExit(main())
