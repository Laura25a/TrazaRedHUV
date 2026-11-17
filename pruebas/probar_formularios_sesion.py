"""Regresión de U06 y U08 en Chromium con respuestas de API sintéticas.

Ejecutar con pytest y Playwright. TRAZARED_FRONT_URL apunta al origen de nginx.
"""

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

AHORA = datetime(2026, 11, 17, 15, tzinfo=timezone.utc)
HUV = "Hospital Universitario del Valle"
DESTINO = "Hospital de prueba"
HISTORICA = "Institución histórica"
PACIENTE = {
    "id": 501, "documento": "9000000501", "nombre": "Paciente de prueba",
    "fecha_nacimiento": "2000-01-02",
}
REMISION = {
    "id": 41, "paciente_id": PACIENTE["id"], "radicado": "REM-2026-000041",
    "fecha_solicitud": "2026-11-14", "radicada_en": "2026-11-16T15:00:00-05:00",
    "institucion_origen": HISTORICA, "institucion_destino": DESTINO,
    "estado": "pendiente", "motivo": "Motivo original", "convenio_vigente": False,
    "abierta": True, "horas_abierta": 24, "marcas": [], "folio": 2, "tomo": "2026-II",
    "paciente_nombre": PACIENTE["nombre"], "paciente_documento": PACIENTE["documento"],
}


@pytest.fixture(scope="module")
def navegador():
    with sync_playwright() as playwright:
        navegador = playwright.chromium.launch()
        yield navegador
        navegador.close()


@pytest.fixture
def entorno(navegador):
    pagina = navegador.new_page()
    pagina.clock.install(time=AHORA - timedelta(seconds=1))
    pagina.clock.pause_at(AHORA)
    solicitudes = []
    errores = []
    pagina.on("pageerror", lambda error: errores.append(error))
    remisiones = {41: dict(REMISION)}
    configuracion = {"minutos": 7}

    def api(ruta):
        peticion = ruta.request
        path = urlsplit(peticion.url).path
        status = 200
        if path == "/api/login" and peticion.method == "POST":
            datos = json.dumps({"exp": int(AHORA.timestamp()) + configuracion["minutos"] * 60})
            payload = base64.urlsafe_b64encode(datos.encode()).decode().rstrip("=")
            cuerpo = {"access_token": f"simulado.{payload}.simulado"}
        elif path == "/api/me":
            cuerpo = {"id": 3, "nombre": "Médico de prueba", "rol": "medico"}
        elif path == "/api/pacientes":
            cuerpo = [PACIENTE]
        elif path == "/api/instituciones":
            cuerpo = [HUV, DESTINO]
        elif path == "/api/registro/libro":
            cuerpo = {"asientos": list(remisiones.values()), "resumen": {
                "radicadas_hoy": 0, "abiertas": len(remisiones), "sin_movimiento": 0,
                "en_riesgo": 0, "umbral_sin_movimiento_h": 6,
            }}
        elif path.startswith("/api/registro/remisiones/") and path.endswith("/formato"):
            remision = remisiones[int(path.split("/")[-2])]
            cuerpo = {"remision": remision, "paciente": PACIENTE, "permisos": {"corregir": True},
                      "diagnostico": remision["motivo"], "signos": [], "bitacora": [], "eventos": [], "cierre": {}}
        elif path == "/api/remisiones" and peticion.method == "POST":
            datos = peticion.post_data_json
            solicitudes.append(("POST", datos))
            cuerpo = remisiones[42] = {**REMISION, **datos, "id": 42, "radicado": "REM-2026-000042"}
            status = 201
        elif path == "/api/remisiones/41" and peticion.method == "PATCH":
            datos = peticion.post_data_json
            solicitudes.append(("PATCH", datos))
            remisiones[41].update(datos)
            cuerpo = remisiones[41]
        elif path == "/api/remisiones":
            cuerpo = list(remisiones.values())
        elif path in ("/api/alertas", "/api/notificaciones", "/api/pendientes"):
            cuerpo = []
        else:
            cuerpo = {}
        ruta.fulfill(status=status, json=cuerpo)

    pagina.route("**/api/**", api)
    yield pagina, solicitudes, configuracion
    pagina.close()
    assert not errores


def entrar(pagina):
    pagina.goto(os.getenv("TRAZARED_FRONT_URL", "http://127.0.0.1:8080"))
    pagina.locator("#login-usuario").fill("medico@example.test")
    pagina.locator("#login-clave").fill("simulacion")
    pagina.get_by_role("button", name="Iniciar sesión").click()
    expect(pagina.get_by_role("heading", name="Libro de radicación de remisiones")).to_be_visible()


def test_radicacion_guia_y_valida_fechas(entorno):
    pagina, solicitudes, _ = entorno
    pagina.set_viewport_size({"width": 390, "height": 844})
    entrar(pagina)
    pagina.get_by_role("button", name="Radicar remisión").click()
    dialogo = pagina.get_by_role("dialog", name="Radicar remisión en el libro")
    dialogo.locator("#m-pac").fill("paciente de prueba")
    dialogo.get_by_role("button", name=f'{PACIENTE["documento"]} — {PACIENTE["nombre"]}').click()
    dialogo.get_by_role("checkbox", name="Confirmo la ficha", exact=False).check()
    expect(dialogo.get_by_label("Institución de origen")).to_have_value(HUV)
    destino = dialogo.get_by_label("Institución de destino", exact=True)
    expect(destino.locator("option")).to_have_count(3)
    enviar = dialogo.get_by_role("button", name="Radicar", exact=True)
    enviar.click()
    expect(dialogo.get_by_role("alert")).to_have_text("Selecciona una institución de destino del catálogo.")
    expect(destino).to_be_focused()
    assert not solicitudes
    destino.select_option(DESTINO)
    dialogo.get_by_label("Motivo", exact=False).fill("Motivo radicado")
    campo = dialogo.get_by_label("Fecha de solicitud")
    expect(campo).to_have_attribute("min", "2000-01-02")
    expect(campo).to_have_attribute("max", "2026-11-17")
    for fecha, mensaje in (
        ("", "Ingresa la fecha de solicitud."),
        ("1999-12-31", "La fecha de solicitud no puede ser anterior al nacimiento"),
        ("2026-11-18", "La fecha de solicitud no puede ser posterior a hoy"),
    ):
        campo.fill(fecha)
        enviar.click()
        expect(dialogo.get_by_role("alert")).to_contain_text(mensaje, timeout=1500)
        expect(campo).to_be_focused()
        expect(campo).to_have_attribute("aria-invalid", "true")
        assert not solicitudes
    campo.fill("2026-11-17")
    expect(campo).not_to_have_attribute("aria-invalid", "true")
    enviar.click()
    expect(pagina.get_by_label("Formato de registro REM-2026-000042")).to_be_visible()
    assert solicitudes == [("POST", {"paciente_id": PACIENTE["id"], "institucion_origen": HUV,
                                      "institucion_destino": DESTINO, "fecha_solicitud": "2026-11-17",
                                      "motivo": "Motivo radicado", "convenio_vigente": False})]


def test_correccion_precarga_y_solo_envia_cambios(entorno):
    pagina, solicitudes, _ = entorno
    entrar(pagina)
    pagina.locator("#l-tabla").get_by_text(REMISION["radicado"], exact=True).click()
    pagina.get_by_role("button", name="Anotar corrección").click()
    dialogo = pagina.get_by_role("dialog", name="Anotar corrección", exact=False)
    expect(dialogo.get_by_label("Paciente", exact=False)).to_be_disabled()
    expect(dialogo.get_by_label("Estado", exact=True)).to_be_disabled()
    origen = dialogo.get_by_label("Institución de origen")
    expect(origen).to_have_value(HISTORICA)
    expect(origen.locator("option:checked")).to_contain_text("registro histórico")
    campo = dialogo.get_by_label("Fecha de solicitud")
    motivo = dialogo.get_by_label("Motivo", exact=False)
    expect(campo).to_have_value("2026-11-14")
    expect(motivo).to_have_value("Motivo original")
    expect(campo).to_have_attribute("max", "2026-11-16")
    campo.fill("2026-11-17")
    guardar = dialogo.get_by_role("button", name="Anotar corrección")
    guardar.click()
    expect(dialogo.get_by_role("alert")).to_contain_text("no puede ser posterior a la radicación")
    assert not solicitudes
    campo.fill("2026-11-14")
    motivo.fill("Motivo corregido")
    guardar.click()
    expect(pagina.get_by_text("Motivo corregido", exact=True)).to_be_visible()
    assert solicitudes == [("PATCH", {"motivo": "Motivo corregido"})]


@pytest.mark.parametrize("minutos", [7, 30])
def test_reloj_usa_vencimiento_real(entorno, minutos):
    pagina, _, configuracion = entorno
    configuracion["minutos"] = minutos
    entrar(pagina)
    pagina.locator("#btn-usuario").click()
    reloj = pagina.get_by_role("timer")
    expect(reloj).to_have_text(f"Tu sesión vence en {minutos:02d}:00", timeout=1500)
    pagina.clock.fast_forward((minutos * 60 - 1) * 1000)
    expect(reloj).to_have_text("Tu sesión vence en 00:01")
    expect(reloj).to_have_class("reloj pronto")
    pagina.clock.run_for(1000)
    expect(pagina.get_by_role("heading", name="Bienvenido")).to_be_visible()
    expect(pagina.locator("#login-mensaje")).to_contain_text("Tu sesión venció. Vuelve a iniciar sesión.")
    assert pagina.evaluate("sessionStorage.getItem('trazared_token')") is None
