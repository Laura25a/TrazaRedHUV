"""Comprueba los recursos de la SPA desde sus tres entradas públicas."""

import argparse
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Origen de nginx")
    args = parser.parse_args()
    origen = args.url.rstrip("/")
    recorridos = ("/", "/app", "/app/")
    fallas = []
    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch()
        for ruta in recorridos:
            pagina = navegador.new_page()
            recursos = []
            pagina.on("response", lambda respuesta: recursos.append(respuesta))
            entrada = pagina.goto(origen + ruta, wait_until="load")
            recursos_propios = [
                r for r in recursos
                if r.url.startswith(origen + "/") and r.request.resource_type in {"stylesheet", "script"}
            ]
            incorrectos = [
                urlsplit(r.url).path for r in recursos_propios
                if not r.ok or not any(
                    tipo in r.headers.get("content-type", "") for tipo in ("text/css", "javascript")
                )
            ]
            if (not entrada.ok or incorrectos or not pagina.evaluate("typeof API === 'object'")
                    or urlsplit(pagina.url).path != "/"):
                fallas.append(ruta)
                print(f"{ruta} FALLA. Ruta final {urlsplit(pagina.url).path}. "
                      f"Recursos inválidos: {', '.join(incorrectos) or 'ninguno'}")
            else:
                expect(pagina.locator("#login-usuario")).to_be_visible()
                print(f"{ruta} OK. {len(recursos_propios)} archivos de interfaz y formulario de ingreso.")
            pagina.close()
        navegador.close()
    return int(bool(fallas))


if __name__ == "__main__":
    raise SystemExit(main())
