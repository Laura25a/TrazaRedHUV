"""Integración sobre dos motores de prueba en loopback y la SPA real.

TRAZARED_PG_PRUEBAS debe apuntar a una base desechable y vacía llamada
trazared_validacion. TRAZARED_MONGO_PRUEBAS debe apuntar a un servidor
desechable con la base trazared_huv vacía.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from pathlib import Path
import os
import secrets
import socket
import sys
import threading

import psycopg2
from psycopg2.extensions import parse_dsn
from pymongo import MongoClient
from pymongo.uri_parser import parse_uri
import pytest


RAIZ = Path(__file__).resolve().parents[1]
HUV = "Hospital Universitario del Valle"
LILI = "Fundación Valle del Lili"
CLAVE_PRUEBA = secrets.token_hex(18)


@pytest.fixture(scope="module")
def entorno():
    from fastapi.testclient import TestClient

    pg = os.environ.get("TRAZARED_PG_PRUEBAS")
    mongo = os.environ.get("TRAZARED_MONGO_PRUEBAS")
    if not pg or not mongo:
        pytest.skip("Configura las dos conexiones de prueba desechables")
    dsn = parse_dsn(pg)
    assert dsn.get("host") == "127.0.0.1" and dsn["dbname"] == "trazared_validacion"
    assert all(host == "127.0.0.1" for host, _ in parse_uri(mongo)["nodelist"])

    with closing(psycopg2.connect(pg)) as db, MongoClient(mongo) as cliente_mongo, db:
        with db.cursor() as cur:
            cur.execute("SELECT to_regclass('public.pacientes');")
            assert cur.fetchone()[0] is None, "La base de pruebas debe estar vacía"
            assert cliente_mongo.trazared_huv.list_collection_names() == []
            cur.execute((RAIZ / "db/schema.sql").read_text())
            cur.execute((RAIZ / "db/migracion_persona3.sql").read_text())
            cur.execute((RAIZ / "db/schema.sql").read_text())

    with pytest.MonkeyPatch.context() as parche:
        for clave, valor in {
            "PG_CONNECTION_STRING": pg, "MONGO_CONNECTION_STRING": mongo,
            "SECRET_KEY": secrets.token_hex(48), "PYTHON_DOTENV_DISABLED": "1",
            "FHIR_BASE_URL": "http://127.0.0.1:1/fhir",
            "ML_URL": "http://127.0.0.1:1", "ORTHANC_URL": "http://127.0.0.1:1",
        }.items():
            parche.setenv(clave, valor)
        parche.syspath_prepend(str(RAIZ / "python"))
        import main
        importar_usuario = main.auth.hash_password(CLAVE_PRUEBA)
        with closing(psycopg2.connect(pg)) as db, db, db.cursor() as cur:
            for nombre, rol in (("Auditor", "admin"), ("Medico", "medico"), ("Colega", "medico")):
                cur.execute("""INSERT INTO usuarios (nombre, correo, contrasena_hash, rol)
                               VALUES (%s, %s, %s, %s);""",
                            (nombre, f"{nombre.lower()}@pruebas.test", importar_usuario, rol))
        yield main, TestClient(main.app), pg


def autenticado(cliente, nombre="auditor"):
    r = cliente.post("/login", data={"username": f"{nombre}@pruebas.test", "password": CLAVE_PRUEBA})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def autorizaciones(entorno):
    _, cliente, _ = entorno
    return {nombre: autenticado(cliente, nombre) for nombre in ("auditor", "medico", "colega")}


def comprobar(respuesta, estado):
    assert respuesta.status_code == estado, respuesta.text
    return respuesta.json() if respuesta.content else None


def paciente(cliente, cabeceras, documento=None, **cambios):
    return cliente.post("/pacientes", headers=cabeceras, json={
        "nombre": "Ficha sintética", "documento": documento or secrets.token_hex(5),
        "genero": "F", "eps": "EPS de prueba", "fecha_nacimiento": "2000-02-15", **cambios,
    })


def datos_remision(paciente_id, **cambios):
    from validaciones import hoy_bogota

    return {"paciente_id": paciente_id, "institucion_origen": HUV, "institucion_destino": LILI,
            "fecha_solicitud": hoy_bogota().isoformat(), "motivo": "Valoración clínica de prueba",
            "estado": "pendiente", "convenio_vigente": False, **cambios}


def test_identidad_fechas_y_ficha_parcial(entorno, autorizaciones):
    from validaciones import hoy_bogota

    _, api, pg = entorno
    h = autorizaciones["auditor"]
    futura = (hoy_bogota() + timedelta(days=1)).isoformat()
    comprobar(paciente(api, h, " - . "), 422)
    comprobar(paciente(api, h, fecha_nacimiento=futura), 422)
    p = comprobar(paciente(api, h, "9.010.778-001", telefono="6010000055"), 201)
    comprobar(paciente(api, h, "9010778001"), 409)
    assert p["documento"] == "9010778001"
    [ficha] = comprobar(api.get("/pacientes", params={"q": "9.010.778-001"}, headers=h), 200)
    assert ficha["id"] == p["id"]

    rid = comprobar(api.post("/remisiones", headers=h, json=datos_remision(
        p["id"], fecha_solicitud="2001-02-15")), 201)["id"]
    comprobar(api.patch(f"/pacientes/{p['id']}", headers=h, json={"fecha_nacimiento": "2002-02-15"}), 422)
    comprobar(api.patch(f"/pacientes/{p['id']}", headers=h, json={"documento": "987"}), 422)
    nuevo = comprobar(api.patch(f"/pacientes/{p['id']}", headers=h, json={"telefono": "6010000056"}), 200)
    assert nuevo["telefono"] == "6010000056" and nuevo["nombre"] == p["nombre"]
    assert comprobar(api.get(f"/remisiones/{rid}", headers=h), 200)["paciente_id"] == p["id"]
    with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
        cur.execute("SELECT telefono FROM pacientes WHERE id = %s;", (p["id"],))
        assert cur.fetchone()[0] != nuevo["telefono"]


def test_catalogo_fechas_estados_y_correccion_parcial(entorno, autorizaciones):
    from validaciones import hoy_bogota

    _, api, _ = entorno
    h = autorizaciones["medico"]
    p = comprobar(paciente(api, h), 201)
    otra = comprobar(paciente(api, h), 201)
    nueva = datos_remision(p["id"])
    comprobar(api.post("/remisiones", headers=h, json={**nueva, "estado": "aceptada"}), 422)
    comprobar(api.post("/remisiones", headers=h, json={**nueva, "fecha_solicitud": "1999-12-31"}), 422)
    comprobar(api.post("/remisiones", headers=h, json={
        **nueva, "fecha_solicitud": (hoy_bogota() + timedelta(days=1)).isoformat()}), 422)
    comprobar(api.post("/remisiones", headers=h, json={**nueva, "institucion_destino": "Sede inventada"}), 422)
    rem = comprobar(api.post("/remisiones", headers=h, json={
        **nueva, "institucion_origen": "HUV", "institucion_destino": "  FUNDACIÓN  VALLE del lili "}), 201)
    assert LILI in comprobar(api.get("/instituciones", headers=h), 200)
    assert rem["institucion_origen"] == HUV and rem["institucion_destino"] == LILI
    ruta = f"/remisiones/{rem['id']}"
    comprobar(api.put(ruta, headers=h, json={**nueva, "paciente_id": otra["id"]}), 409)
    comprobar(api.put(ruta, headers=h, json={**nueva, "estado": "completada"}), 409)
    comprobar(api.patch(ruta, headers=h, json={"estado": "completada"}), 422)
    comprobar(api.patch(ruta, headers=h, json={"motivo": None}), 422)
    comprobar(api.patch(ruta, headers=autorizaciones["colega"], json={"motivo": "No autorizado"}), 403)
    editada = comprobar(api.patch(ruta, headers=h, json={"motivo": "Corrección parcial"}), 200)
    assert editada == {**rem, "motivo": "Corrección parcial"}


def test_bitacora_sin_huerfanos_ni_reescrituras(entorno, autorizaciones):
    from datetime import datetime, timezone

    _, api, _ = entorno
    h = autorizaciones["medico"]
    admin = autorizaciones["auditor"]
    p = comprobar(paciente(api, h), 201)
    rem, otra = [comprobar(api.post("/remisiones", headers=h, json=datos_remision(p["id"])), 201)
                 for _ in range(2)]
    contacto = {"fecha": datetime.now(timezone.utc).isoformat(), "medio": "telefono",
                "institucion_contactada": LILI, "contactado_por": "Operador de prueba",
                "respuesta": "Sin respuesta"}
    ruta = "/gestiones-contacto"
    comprobar(api.post(ruta, headers=h, json={"remision_id": 2147483000, "contactos": [contacto]}), 404)
    cuerpo = {"remision_id": rem["id"], "contactos": [contacto]}
    comprobar(api.post(ruta, headers=autorizaciones["colega"], json=cuerpo), 403)
    creada = comprobar(api.post(ruta, headers=h, json=cuerpo), 201)
    id_gestion = creada["_id"]
    comprobar(api.put(f"{ruta}/{id_gestion}", headers=h, json={**cuerpo, "remision_id": otra["id"]}), 409)
    comprobar(api.put(f"{ruta}/{id_gestion}", headers=h, json={
        **cuerpo, "contactos": [{**contacto, "contactado_por": "Otro nombre"}]}), 409)
    comprobar(api.delete(f"{ruta}/{id_gestion}", headers=admin), 409)
    comprobar(api.delete(f"/remisiones/{rem['id']}", headers=h), 204)
    comprobar(api.post(f"/registro/remisiones/{rem['id']}/gestiones", headers=h, json=contacto), 409)
    comprobar(api.post(f"/remisiones/{rem['id']}/restaurar", headers=admin), 200)
    final = comprobar(api.get(f"{ruta}/{id_gestion}", headers=h), 200)
    assert final["contactos"] == creada["contactos"]
    assert comprobar(api.get(f"/remisiones/{rem['id']}", headers=h), 200)["estado"] == "en_gestion"


def test_concurrencia_de_correcciones_y_primer_contacto(entorno, autorizaciones):
    _, api, pg = entorno
    h = autorizaciones["medico"]
    p = comprobar(paciente(api, h), 201)
    r = comprobar(api.post("/remisiones", headers=h, json=datos_remision(p["id"])), 201)
    rid = r["id"]

    with ThreadPoolExecutor(max_workers=2) as ejecutor:
        cambios = list(ejecutor.map(
            lambda cuerpo: api.patch(f"/remisiones/{rid}", headers=h, json=cuerpo),
            [{"motivo": "Corrección simultánea"}, {"convenio_vigente": True}],
        ))
    assert all(r.status_code == 200 for r in cambios)
    rem = comprobar(api.get(f"/remisiones/{rid}", headers=h), 200)
    assert rem["motivo"] == "Corrección simultánea" and rem["convenio_vigente"]
    with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
        cur.execute("SELECT dato_anterior FROM remisiones_historial WHERE remision_id=%s ORDER BY id;", (rid,))
        primera, segunda = [fila[0] for fila in cur.fetchall()]
        assert primera["motivo"] == r["motivo"] and not primera["convenio_vigente"]
        assert (segunda["motivo"] != primera["motivo"]) != (segunda["convenio_vigente"] != primera["convenio_vigente"])

    def anotar(numero):
        return api.post(f"/registro/remisiones/{rid}/gestiones", headers=h, json={
            "institucion_contactada": LILI, "respuesta": f"Sin respuesta, registro {numero}",
        })

    with ThreadPoolExecutor(max_workers=4) as ejecutor:
        assert all(res.status_code == 201 for res in ejecutor.map(anotar, range(4)))
    gestiones = comprobar(api.get(f"/gestiones-contacto?remision_id={rid}", headers=h), 200)
    assert len(gestiones) == 1
    assert len({g["respuesta"] for g in gestiones[0]["contactos"]}) == 4


def test_referencias_facturas_y_un_solo_cobro(entorno, autorizaciones):
    from contabilidad import crear_factura_ia

    _, api, pg = entorno
    h = autorizaciones["auditor"]
    p = comprobar(paciente(api, h), 201)
    otra = comprobar(paciente(api, h), 201)
    rem = comprobar(api.post("/remisiones", headers=h, json=datos_remision(p["id"])), 201)
    cuerpo = {"paciente_id": p["id"], "servicio": "remision", "valor": 1000, "remision_id": rem["id"]}
    comprobar(api.post("/facturas", headers=h, json={**cuerpo, "paciente_id": otra["id"]}), 422)
    comprobar(api.post("/facturas", headers=h, json={**cuerpo, "remision_id": 2147483000}), 404)
    comprobar(api.post("/facturas", headers=h, json={**cuerpo, "paciente_id": 2147483000}), 404)
    comprobar(api.post("/facturas", headers=h, json=cuerpo), 409)
    comprobar(api.post(f"/registro/remisiones/{rem['id']}/cierre", headers=h, json={"resultado": "rechazada"}), 200)
    factura = comprobar(api.post("/facturas", headers=h, json=cuerpo), 201)
    assert factura["radicado"] and factura["paciente_id"] == p["id"]
    comprobar(api.post("/facturas", headers=h, json=cuerpo), 409)

    with closing(psycopg2.connect(pg)) as db, db, db.cursor() as cur:
        cur.execute("""INSERT INTO reportes_ia (paciente_id, probabilidad_urgente, prediccion, payload_seudonimizado, estado)
                       VALUES (%s, 0.2, 'prueba', '{}', 'aprobado') RETURNING id;""", (p["id"],))
        reporte_id = cur.fetchone()[0]
    ia = {"paciente_id": otra["id"], "servicio": "analisis_ia", "valor": 1000, "reporte_id": reporte_id}
    comprobar(api.post("/facturas", headers=h, json=ia), 422)
    with ThreadPoolExecutor(max_workers=2) as ejecutor:
        ids = list(ejecutor.map(lambda _: crear_factura_ia(p["id"], reporte_id), range(2)))
    assert len(set(ids)) == 1


def test_restricciones_sql_sin_api(entorno, autorizaciones):
    _, api, pg = entorno
    h = autorizaciones["auditor"]
    p = comprobar(paciente(api, h), 201)
    rem = comprobar(api.post("/remisiones", headers=h, json=datos_remision(p["id"])), 201)
    with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
        for sentencia, valores, error in (
            ("UPDATE pacientes SET fecha_nacimiento = CURRENT_DATE + 1 WHERE id=%s", (p["id"],), psycopg2.errors.CheckViolation),
            ("UPDATE remisiones SET fecha_solicitud = DATE '1900-01-01' WHERE id=%s", (rem["id"],), psycopg2.errors.CheckViolation),
            ("DELETE FROM remisiones WHERE id=%s", (rem["id"],), psycopg2.errors.CheckViolation),
            ("UPDATE remisiones SET institucion_destino = 'Fuera del catálogo' WHERE id=%s", (rem["id"],), psycopg2.errors.ForeignKeyViolation),
        ):
            with pytest.raises(error):
                cur.execute(sentencia, valores)
            db.rollback()
        cur.execute("UPDATE remisiones SET estado='rechazada' WHERE id=%s", (rem["id"],))
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE remisiones SET estado='pendiente' WHERE id=%s", (rem["id"],))


def test_navegador_ficha_identidad_y_correccion(entorno, autorizaciones):
    import uvicorn
    from playwright.sync_api import expect, sync_playwright

    main, api, _ = entorno
    h = autorizaciones["auditor"]
    comprobar(paciente(api, h, "9010788221", nombre="Paciente de navegador"), 201)

    with socket.socket() as puerto:
        puerto.bind(("127.0.0.1", 0))
        puerto.listen()
        base = f"http://127.0.0.1:{puerto.getsockname()[1]}"
        servidor = uvicorn.Server(uvicorn.Config(main.app, lifespan="off", log_level="error"))
        hilo = threading.Thread(target=servidor.run, kwargs={"sockets": [puerto]}, daemon=True)
        hilo.start()
        try:
            with sync_playwright() as control:
                navegador = control.chromium.launch()
                pagina = navegador.new_page()
                pagina.goto(base + "/app/")
                pagina.locator("#login-usuario").fill("auditor@pruebas.test")
                pagina.locator("#login-clave").fill(CLAVE_PRUEBA)
                pagina.locator("#form-login button[type=submit]").click()
                pagina.locator('[data-seccion="pacientes"]').click()
                pagina.get_by_role("button", name="Corregir ficha de Paciente de navegador").click()
                expect(pagina.locator("#m-doc")).to_be_disabled()
                pagina.locator("#m-nac").fill("2999-01-01")
                pagina.get_by_role("button", name="Guardar corrección").click()
                expect(pagina.locator(".error-form")).to_contain_text("no puede ser futura")
                pagina.locator("#m-nac").fill("2000-02-15")
                pagina.locator("#m-tel").fill("6010000012")
                with pagina.expect_response(lambda r: r.request.method == "PATCH") as respuesta:
                    pagina.get_by_role("button", name="Guardar corrección").click()
                assert respuesta.value.status == 200
                expect(pagina.get_by_text("6010000012")).to_be_visible()
                pagina.get_by_role("link", name="Libro de radicación").click()
                pagina.locator("#btn-radicar").click()
                pagina.locator("#m-pac").fill("9.010.788.221")
                pagina.locator("#m-des").select_option(LILI)
                pagina.locator("#m-mot").fill("Solicitud probada en navegador")
                pagina.get_by_role("button", name="Radicar", exact=True).click()
                expect(pagina.locator(".error-form")).to_contain_text("confirma su nombre y documento")
                pagina.get_by_role("checkbox", name="Confirmo la ficha", exact=False).check()
                fecha = pagina.locator("#m-fec")
                correcta = fecha.input_value()
                fecha.fill("1999-01-01")
                pagina.get_by_role("button", name="Radicar", exact=True).click()
                expect(pagina.locator(".error-form")).to_contain_text("entre el nacimiento y la fecha de radicación")
                fecha.fill(correcta)
                with pagina.expect_response(lambda r: r.url.endswith("/remisiones") and r.request.method == "POST") as respuesta:
                    pagina.get_by_role("button", name="Radicar", exact=True).click()
                assert respuesta.value.status == 201
                pagina.locator("#f-corregir").click()
                expect(pagina.locator("#m-pac")).to_be_disabled()
                pagina.locator("#m-mot").fill("Corrección probada en navegador")
                with pagina.expect_response(lambda r: r.request.method == "PATCH") as respuesta:
                    pagina.get_by_role("button", name="Anotar corrección", exact=True).last.click()
                assert respuesta.value.status == 200
                assert respuesta.value.request.post_data_json == {"motivo": "Corrección probada en navegador"}
                pagina.locator("#f-anotar").click()
                expect(pagina.locator("#g-res")).to_be_visible(timeout=3000)
                expect(pagina.locator("#dl-g-ins option").first).to_be_attached()
                pagina.locator("#g-res").fill("Sin respuesta")
                with pagina.expect_response(lambda r: r.url.endswith("/gestiones") and r.request.method == "POST") as respuesta:
                    pagina.get_by_role("button", name="Anotar en la bitácora").click()
                assert respuesta.value.status == 201
                assert respuesta.value.json()["estado"] == "en_gestion"

                pagina.locator("#f-cerrar").click()
                expect(pagina.locator("#c-res")).to_be_visible(timeout=3000)
                pagina.locator("#c-res").select_option("rechazada")
                with pagina.expect_response(lambda r: r.url.endswith("/cierre") and r.request.method == "POST") as respuesta:
                    pagina.locator(".modal button[type=submit]").click()
                assert respuesta.value.status == 200
                assert respuesta.value.json()["estado"] == "rechazada"
                expect(pagina.locator("#f-cerrar")).to_have_count(0)
                navegador.close()
        finally:
            servidor.should_exit = True
            hilo.join(timeout=10)
