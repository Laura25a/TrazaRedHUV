"""Comprueba la entrada, los recursos estáticos y el formulario de TrazaRed."""

import argparse
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def verificar_entrada(navegador, origen, ruta, ruta_final, movil, captura=None):
    vista = {"width": 390, "height": 844} if movil else {"width": 1440, "height": 900}
    pagina = navegador.new_page(viewport=vista)
    respuestas, fallos = [], []
    def registrar_fallo(peticion):
        if peticion.url.startswith(origen + "/"):
            fallos.append(urlsplit(peticion.url).path)

    pagina.on("response", lambda respuesta: respuestas.append(respuesta))
    pagina.on("requestfailed", registrar_fallo)
    try:
        entrada = pagina.goto(origen + ruta, wait_until="load")
        assert entrada.ok
        recursos = {"stylesheet": [], "script": []}
        for respuesta in respuestas:
            tipo = respuesta.request.resource_type
            if tipo not in recursos or not respuesta.url.startswith(origen + "/"):
                continue
            path = urlsplit(respuesta.url).path
            recursos[tipo].append(path)
            mime = respuesta.headers.get("content-type", "")
            correcto = "text/css" in mime if tipo == "stylesheet" else "javascript" in mime
            if not respuesta.ok or not correcto:
                fallos.append(f"{path} ({respuesta.status}, {mime})")
        assert not fallos, ", ".join(fallos)
        assert urlsplit(pagina.url).path == ruta_final, f"ruta final {pagina.url}"
        for tipo, archivos in recursos.items():
            assert archivos, f"sin archivos de tipo {tipo}"
        expect(pagina.locator("#login-usuario")).to_be_visible()
        expect(pagina.locator("#vista-app")).to_be_hidden()
        boton = pagina.get_by_role("button", name="Mostrar contraseña")
        boton.click()
        expect(pagina.locator("#login-clave")).to_have_attribute("type", "text")
        assert pagina.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Desbordamiento horizontal"
        if captura:
            pagina.screenshot(path=str(captura), full_page=True)
        etiqueta = "móvil" if movil else "escritorio"
        print(f"{ruta} OK ({etiqueta}). {sum(map(len, recursos.values()))} recursos; estilos y controles activos.")
        return sum(map(len, recursos.values()))
    finally:
        pagina.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Origen de nginx")
    args = parser.parse_args()
    origen = args.url.rstrip("/")
    recorridos = (("/", "/", False), ("/index.html", "/index.html", False),
                  ("/app", "/", False), ("/app/", "/", False),
                  ("/app/index.html?origen=prueba", "/", True), ("/libro/remisiones/", "/", False))
    fallas = 0
    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch()
        for ruta, ruta_final, movil in recorridos:
            try:
                verificar_entrada(navegador, origen, ruta, ruta_final, movil)
            except AssertionError as exc:
                fallas += 1
                print(f"{ruta} FALLA. {exc}")
        navegador.close()
        cliente = playwright.request.new_context(base_url=origen)
        for ruta in ("/css/__ausente__.css", "/js/__ausente__.js", "/app/css/__ausente__.css"):
            respuesta = cliente.get(ruta, max_redirects=0)
            if respuesta.status != 404:
                fallas += 1
                print(f"{ruta} FALLA. Devolvió {respuesta.status}, se esperaba 404.")
        respuesta = cliente.get("/app/index.html?origen=prueba", max_redirects=0)
        if (respuesta.headers.get("location") != "/?origen=prueba"
                or "no-store" not in respuesta.headers.get("cache-control", "")):
            fallas += 1
            print("El enlace antiguo debe redirigir con su consulta y sin caché.")
        cliente.dispose()
    return int(bool(fallas))


if __name__ == "__main__":
    raise SystemExit(main())
