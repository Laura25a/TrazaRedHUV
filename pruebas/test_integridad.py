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
            for nombre, rol in (
                ("Auditor", "admin"), ("Medico", "medico"), ("Colega", "medico"),
                ("Especialista", "especialista"),
            ):
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


def test_seguridad_alta_acceso_transporte_y_auditoria(entorno, autorizaciones, monkeypatch):
    import interop
    import registro

    _, api, pg = entorno
    admin, medico, colega = (autorizaciones[n] for n in ("auditor", "medico", "colega"))
    correo = "seguridad@pruebas.test"
    alta = {"nombre": "Cuenta de seguridad", "correo": correo, "rol": "medico"}
    debil = secrets.token_hex(8)
    respuesta = api.post("/usuarios", headers=admin, json={**alta, "contrasena": debil})
    comprobar(respuesta, 422)
    assert debil not in respuesta.text
    fuerte = secrets.token_hex(8) + "Aa1!"
    comprobar(api.post("/usuarios", headers=admin, json={**alta, "contrasena": fuerte}), 201)

    ficha = comprobar(paciente(api, admin), 201)
    asiento = comprobar(api.post("/remisiones", headers=medico, json=datos_remision(ficha["id"])), 201)
    radicado = asiento["id"]
    signos = {"remision_id": radicado, "tipo": "Pulso", "valor": 70, "unidad": "lat/min"}
    comprobar(api.post("/observaciones", headers=colega, json=signos), 403)
    comprobar(api.post("/observaciones", headers=medico, json=signos), 201)
    comprobar(api.get(f"/pacientes/{ficha['id']}/historia", headers=colega), 404)
    comprobar(api.patch(f"/pacientes/{ficha['id']}", headers=colega,
                        json={"telefono": "6010000058"}), 404)
    comprobar(api.get(f"/pacientes/{ficha['id']}/historia", headers=medico), 200)
    comprobar(api.post(f"/interop/remisiones/{radicado}/enviar", headers=colega, json={"socio_id": 1}), 403)
    comprobar(api.post("/interop/socios", headers=admin, json={"nombre": "Socio prueba",
                                                              "url_base": "http://socio.test"}), 422)
    comprobar(api.post("/interop/socios", headers=admin, json={"nombre": "Socio seguro",
                                                              "url_base": "https://socio.test"}), 201)
    with monkeypatch.context() as parche:
        def sin_red(*args, **kwargs):
            pytest.fail("Una URL HTTP persistida no debe abrir una conexión")
        parche.setattr(interop.requests, "post", sin_red)
        with pytest.raises(interop.HTTPException) as error:
            interop._token_saliente({"url_base": "http://socio.test", "id": 1}, "remisiones")
        assert error.value.status_code == 422
    comprobar(api.get("/eventos/stream", params={"token": admin["Authorization"].split()[1]}), 401)

    registrar = registro.registrar_auditoria
    with monkeypatch.context() as parche:
        def fallar_sello(db, usuario, accion, *args, **kwargs):
            if accion == "cierre_remision":
                raise RuntimeError("Fallo simulado del sello")
            return registrar(db, usuario, accion, *args, **kwargs)
        parche.setattr(registro, "registrar_auditoria", fallar_sello)
        with pytest.raises(RuntimeError, match="Fallo simulado del sello"):
            api.post(f"/registro/remisiones/{radicado}/cierre", headers=medico, json={"resultado": "rechazada"})
    with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
        cur.execute("SELECT estado FROM remisiones WHERE id = %s", (radicado,))
        assert cur.fetchone() == ("pendiente",)
        cur.execute("SELECT COUNT(*) FROM auditoria WHERE registro_id = %s AND accion = 'cambio_estado'", (radicado,))
        assert cur.fetchone() == (0,)
    comprobar(api.post(f"/registro/remisiones/{radicado}/cierre", headers=medico,
                       json={"resultado": "rechazada"}), 200)


def test_interconsulta_y_auditoria_son_atomicas(entorno, autorizaciones, monkeypatch):
    import remisiones

    _, api, pg = entorno
    admin, medico = (autorizaciones[n] for n in ("auditor", "medico"))
    especialista = autenticado(api, "especialista")
    destinatario = comprobar(api.get("/me", headers=especialista), 200)["id"]
    ficha = comprobar(paciente(api, admin), 201)
    datos = {"paciente_id": ficha["id"], "especialista_id": destinatario, "motivo": "Interconsulta de prueba"}
    monkeypatch.setattr(remisiones.fhir_sync, "obtener_o_crear_paciente_fhir", lambda _: "prueba")
    monkeypatch.setattr(remisiones.fhir_sync, "sincronizar_remision_medica", lambda *a, **kw: "prueba")

    def fallar_sello(*args, **kwargs):
        raise RuntimeError("Fallo simulado del sello")

    with monkeypatch.context() as parche:
        parche.setattr(remisiones, "registrar_auditoria", fallar_sello)
        with pytest.raises(RuntimeError, match="Fallo simulado del sello"):
            api.post("/remitir", headers=medico, json=datos)
    with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
        cur.execute("SELECT estado FROM remisiones_medicas WHERE paciente_id = %s;", (ficha["id"],))
        assert cur.fetchall() == []

    creada = comprobar(api.post("/remitir", headers=medico, json=datos), 201)
    ruta = f"/remitir/{creada['id']}/aceptar"
    with monkeypatch.context() as parche:
        parche.setattr(remisiones, "registrar_auditoria", fallar_sello)
        with pytest.raises(RuntimeError, match="Fallo simulado del sello"):
            api.post(ruta, headers=especialista)
    with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
        cur.execute("SELECT estado FROM remisiones_medicas WHERE id = %s;", (creada["id"],))
        assert cur.fetchone() == ("pendiente",)

    comprobar(api.post(ruta, headers=especialista), 200)
    comprobar(api.post(ruta, headers=especialista), 409)
    with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
        cur.execute("""SELECT accion FROM auditoria
                        WHERE tabla = 'remisiones_medicas' AND registro_id = %s AND hash IS NOT NULL
                        ORDER BY id;""", (creada["id"],))
        assert cur.fetchall() == [("remision_creada",), ("remision_aceptada",)]
        cur.execute("""SELECT tipo, mensaje, remision_id FROM notificaciones
                        WHERE paciente_id = %s AND datos->>'interconsulta_id' = %s
                        ORDER BY id;""", (ficha["id"], str(creada["id"])))
        avisos = cur.fetchall()
        assert [tipo for tipo, _, _ in avisos] == ["remision_creada", "remision_aceptada"]
        assert all(ficha["nombre"] not in mensaje and remision_id is None
                   for _, mensaje, remision_id in avisos)


def test_reportes_ia_y_auditoria_son_atomicos(entorno, autorizaciones, monkeypatch):
    import ia

    _, api, pg = entorno
    admin, medico = (autorizaciones[n] for n in ("auditor", "medico"))
    ficha = comprobar(paciente(api, admin), 201)
    datos = {"paciente_id": ficha["id"]}
    monkeypatch.setattr(ia, "llamar_modelo", lambda _: {
        "version_modelo": "pruebas", "probabilidad": 0.1, "critico": False, "nivel": "no_urgente",
    })

    def fallar_sello(*args, **kwargs):
        raise RuntimeError("Fallo simulado del sello")

    with monkeypatch.context() as parche:
        parche.setattr(ia, "registrar_auditoria", fallar_sello)
        with pytest.raises(RuntimeError, match="Fallo simulado del sello"):
            api.post("/ia/analizar", headers=medico, json=datos)
    with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
        cur.execute("SELECT id FROM reportes_ia WHERE paciente_id = %s;", (ficha["id"],))
        assert cur.fetchall() == []

    for accion, estado in (("aprobar", "aprobado"), ("rechazar", "rechazado")):
        reporte = comprobar(api.post("/ia/analizar", headers=medico, json=datos), 201)
        ruta = f"/ia/reportes/{reporte['id']}/{accion}"
        with monkeypatch.context() as parche:
            parche.setattr(ia, "registrar_auditoria", fallar_sello)
            with pytest.raises(RuntimeError, match="Fallo simulado del sello"):
                api.post(ruta, headers=medico)
        with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
            cur.execute("SELECT estado FROM reportes_ia WHERE id = %s;", (reporte["id"],))
            assert cur.fetchone() == ("pendiente",)
        assert comprobar(api.post(ruta, headers=medico), 200)["estado"] == estado
        with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
            cur.execute("""SELECT accion FROM auditoria
                            WHERE tabla = 'reportes_ia' AND registro_id = %s AND hash IS NOT NULL
                            ORDER BY id;""", (reporte["id"],))
            assert cur.fetchall() == [("analisis_ia",), (f"reporte_{estado}",)]


def test_navegador_ficha_identidad_y_correccion(entorno, autorizaciones):
    import uvicorn
    from playwright.sync_api import expect, sync_playwright

    main, api, _ = entorno
    h = autorizaciones["auditor"]
    ficha = comprobar(paciente(api, h, "9010788221", nombre="Paciente de navegación"), 201)
    especialista = autenticado(api, "especialista")
    destinatario = comprobar(api.get("/me", headers=especialista), 200)["id"]
    interconsulta = comprobar(api.post("/remitir", headers=h, json={
        "paciente_id": ficha["id"], "especialista_id": destinatario, "motivo": "Interconsulta en navegador",
    }), 201)
    anulado = comprobar(api.post("/remisiones", json=datos_remision(ficha["id"]), headers=h), 201)
    comprobar(api.delete(f"/remisiones/{anulado['id']}", headers=h), 204)

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
                pagina = navegador.new_page(permissions=["clipboard-read", "clipboard-write"])
                pagina.goto(base + "/app/")
                pagina.locator("#login-usuario").fill("auditor@pruebas.test")
                pagina.locator("#login-clave").fill(CLAVE_PRUEBA)
                with pagina.expect_request(lambda r: r.url.endswith("/eventos/stream")) as canal:
                    pagina.locator("#form-login button[type=submit]").click()
                assert canal.value.header_value("authorization").startswith("Bearer ")
                pagina.wait_for_function("Eventos.conectado()")
                pagina.locator('[data-seccion="seguridad"]').click()
                with pagina.expect_response(lambda r: r.url.endswith("/auditoria/verificar")) as auditoria:
                    pagina.locator("#seg-verificar").click()
                assert auditoria.value.json()["integra"] is True
                pagina.locator('[data-seccion="usuarios"]').click()
                pagina.locator("#btn-nuevo-usr").click()
                pagina.locator('[name="nombre"]').last.fill("Alta desde navegador")
                pagina.locator('[name="correo"]').last.fill("navegacion@pruebas.test")
                pagina.locator("#m-ucl").fill(secrets.token_hex(8))
                pagina.get_by_role("button", name="Crear usuario", exact=True).click()
                expect(pagina.locator(".error-form")).to_contain_text("mayúscula")
                pagina.locator("#m-ucl").fill(secrets.token_hex(8) + "Aa1!")
                with pagina.expect_response(lambda r: r.url.endswith("/usuarios") and r.request.method == "POST") as alta:
                    pagina.get_by_role("button", name="Crear usuario", exact=True).click()
                assert alta.value.status == 201
                pagina.locator('[data-seccion="pacientes"]').click()
                pagina.get_by_role("button", name="Corregir ficha de Paciente de navegación").click()
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
                abiertos = pagina.locator('[data-vista="abiertas"]')
                cantidad = abiertos.inner_text()
                with pagina.expect_response(lambda r: "incluir_inactivas=true" in r.url):
                    pagina.locator("#l-inact").check()
                expect(abiertos).to_have_text(cantidad)
                fila_anulada = pagina.locator(f"#l-tabla tr[data-id=\"{anulado['id']}\"]")
                expect(fila_anulada).to_have_count(0)
                pagina.locator('[data-vista="todas"]').click()
                expect(fila_anulada).to_contain_text("Anulado")
                filtro = pagina.get_by_role("searchbox", name="Buscar en el libro")
                filtro.fill("sin coincidencia de prueba")
                expect(fila_anulada).to_have_count(0)
                filtro.fill("")
                expect(fila_anulada).to_be_visible()

                pagina.locator("#btn-radicar").click()
                dialogo = pagina.get_by_role("dialog", name="Radicar remisión en el libro")
                expect(dialogo).to_be_visible()
                pagina.keyboard.press("Escape")
                expect(dialogo).to_have_count(0)
                expect(pagina.locator("#btn-radicar")).to_be_focused()
                pagina.locator("#btn-radicar").click()
                paciente_input = dialogo.get_by_label("Paciente (documento o nombre)")
                confirmacion = dialogo.get_by_role("checkbox", name="Confirmo la ficha", exact=False)
                pagina.evaluate("navigator.clipboard.writeText('9.010.788.221')")
                paciente_input.press("Control+v")
                expect(confirmacion).to_be_visible()
                confirmacion.check()
                paciente_input.fill("")
                expect(confirmacion).to_be_hidden()
                paciente_input.fill("de navegacion")
                dialogo.get_by_role("button", name="9010788221 — Paciente de navegación").click()
                expect(confirmacion).to_be_focused()
                expect(confirmacion).not_to_be_checked()
                pagina.locator("#m-des").select_option(LILI)
                pagina.locator("#m-mot").fill("Solicitud probada en navegador")
                pagina.get_by_role("button", name="Radicar", exact=True).click()
                expect(pagina.locator(".error-form")).to_contain_text("confirma su nombre y documento")
                confirmacion.check()
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
                pagina_especialista = navegador.new_context().new_page()
                pagina_especialista.goto(base + "/app/")
                pagina_especialista.locator("#login-usuario").fill("especialista@pruebas.test")
                pagina_especialista.locator("#login-clave").fill(CLAVE_PRUEBA)
                pagina_especialista.locator("#form-login button[type=submit]").click()
                pagina_especialista.locator('[data-seccion="remitir"]').click()
                boton = pagina_especialista.locator(f'[data-aceptar="{interconsulta["id"]}"]')
                ruta_aceptar = f"/remitir/{interconsulta['id']}/aceptar"
                with pagina_especialista.expect_response(lambda r: r.url.endswith(ruta_aceptar)) as aceptacion:
                    boton.click()
                assert aceptacion.value.status == 200
                tarjeta = pagina_especialista.locator("#remitir-contenido article").filter(
                    has_text="Interconsulta en navegador",
                )
                expect(tarjeta.get_by_text("aceptada", exact=True)).to_be_visible()
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
                pagina.get_by_role("button", name="Abrir a Traza, la asistente").click(timeout=3000)
                traza = pagina.get_by_role("dialog", name="Traza, asistente de TrazaRed")
                expect(traza).to_be_visible()
                traza.get_by_role("button", name="Cerrar").click()
                expect(traza).to_be_hidden()
                navegador.close()
        finally:
            servidor.should_exit = True
            hilo.join(timeout=10)


def test_agente_contexto_seudonimo_y_navegador(entorno, autorizaciones, monkeypatch):
    from contextlib import asynccontextmanager
    import json
    import httpx
    import uvicorn
    from playwright.sync_api import expect, sync_playwright
    from agente import herramientas, router as agente_router

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    main, api, pg = entorno
    medico, admin = autorizaciones["medico"], autorizaciones["auditor"]
    nombre, direccion, correo, documento, telefono = (
        "Aurelia Ficticia", "Calle 81 # 73-92", "aurelia.canario@pruebas.test", "9010793221", "3007109301",
    )
    a = comprobar(paciente(api, medico, documento, nombre=nombre, direccion=direccion,
                          correo=correo, telefono=telefono), 201)["id"]
    b = comprobar(paciente(api, medico, nombre="Bernardo Ficticio"), 201)["id"]
    c = comprobar(paciente(api, admin, nombre="Caso no asignado"), 201)["id"]
    rid = comprobar(api.post("/remisiones", json=datos_remision(a, motivo=nombre), headers=medico), 201)["id"]
    comprobar(api.post("/remisiones", json=datos_remision(b), headers=medico), 201)
    with closing(psycopg2.connect(pg)) as db, db, db.cursor() as cur:
        cur.execute("""INSERT INTO agente_invocaciones (usuario_id, payload, creado_en)
                       VALUES ((SELECT id FROM usuarios WHERE correo = 'medico@pruebas.test'),
                               '{"mensajes": [{"contenido": "registro expirado"}]}', now() - interval '31 days')
                       RETURNING id;""")
        expirada = cur.fetchone()[0]
    @asynccontextmanager
    async def ciclo_agente(app):
        await agente_router.iniciar_agente()
        try:
            yield
        finally:
            await agente_router.cerrar_agente()

    monkeypatch.setattr(main.app.router, "lifespan_context", ciclo_agente)
    with socket.socket() as puerto:
        puerto.bind(("127.0.0.1", 0))
        puerto.listen()
        base = f"http://127.0.0.1:{puerto.getsockname()[1]}"
        servidor = uvicorn.Server(uvicorn.Config(main.app, lifespan="on", log_level="error"))
        hilo = threading.Thread(target=servidor.run, kwargs={"sockets": [puerto]}, daemon=True)
        hilo.start()
        try:
            with sync_playwright() as control, httpx.Client(base_url=base, timeout=25) as http:
                navegador = control.chromium.launch()
                pagina = navegador.new_page()
                pagina.goto(base + "/app/")
                pagina.locator("#login-usuario").fill("medico@pruebas.test")
                pagina.locator("#login-clave").fill(CLAVE_PRUEBA)
                pagina.locator("#form-login button[type=submit]").click()
                pagina.locator('[data-seccion="asistente"]').click()

                def abrir(pid):
                    pagina.locator("#ast-pid").fill(str(pid))
                    with pagina.expect_response(lambda r: "/agente/historial?" in r.url and f"paciente_id={pid}" in r.url) as historial:
                        pagina.locator("#ast-abrir").click()
                    assert historial.value.status == 200
                    assert f"paciente_id={pid}" in historial.value.url

                abrir(a)
                expect(pagina.locator("#ast-pinfo")).to_contain_text(nombre)
                conv = "misma-conversacion"
                mensaje = f"¿Qué tan grave está este paciente {nombre}, {direccion}, {correo}, {documento}, {telefono}?"
                datos = {"conversacion_id": conv, "paciente_id": a, "mensaje": mensaje}
                turno = comprobar(http.post("/agente/chat", headers=medico, json=datos), 200)
                assert turno["estado"] == "respondido", turno
                assert turno["pasos"] == [] and turno["invocaciones"]
                for inv in turno["invocaciones"]:
                    payload = comprobar(http.get(f"/agente/invocaciones/{inv}/payload", headers=admin), 200)
                    assert not any(valor in json.dumps(payload, ensure_ascii=False)
                                   for valor in (nombre, direccion, correo, documento, telefono))
                assert comprobar(http.get("/agente/estado", headers=admin), 200)["memoria"].startswith("PostgreSQL")
                for pid in (None, b):
                    assert comprobar(http.get("/agente/historial", headers=medico, params={
                        "conversacion_id": conv, **({"paciente_id": pid} if pid else {}),
                    }), 200) == {"mensajes": [], "pendiente": None}
                comprobar(http.post("/agente/chat", headers=medico, json={**datos, "paciente_id": c}), 404)
                comprobar(http.get(f"/agente/invocaciones/{expirada}/payload", headers=admin), 410)

                pagina.locator("#ast-texto").fill("¿Qué tan grave está este paciente? MarcadorNavegacion")
                with pagina.expect_response(lambda r: r.url.endswith("/agente/chat")) as respuesta:
                    pagina.locator("#ast-enviar").click()
                assert respuesta.value.json()["estado"] == "respondido"
                expect(pagina.locator("#ast-chat")).to_contain_text("MarcadorNavegacion")
                abrir(b)
                expect(pagina.locator("#ast-chat")).not_to_contain_text("MarcadorNavegacion")
                abrir(a)
                expect(pagina.locator("#ast-chat")).to_contain_text("MarcadorNavegacion")

                propuesta = comprobar(http.post("/agente/chat", headers=medico, json={
                    **datos, "mensaje": "Remite este paciente a UCI",
                }), 200)
                assert propuesta["estado"] == "pendiente_aprobacion", propuesta
                decision = {"conversacion_id": conv, "decision": "reject", "motivo": "Seguimiento local"}
                assert comprobar(http.post("/agente/decidir", headers=medico, json={
                    **decision, "paciente_id": b,
                }), 200)["estado"] == "sin_pendientes"
                assert comprobar(http.post("/agente/decidir", headers=medico, json={
                    **decision, "paciente_id": a,
                }), 200)["estado"] == "respondido"
                assert comprobar(http.get("/agente/historial", headers=medico, params={
                    "conversacion_id": conv, "paciente_id": a,
                }), 200)["pendiente"] is None

                pagina.locator("#ast-texto").fill("Remite este paciente a UCI")
                with pagina.expect_response(lambda r: r.url.endswith("/agente/chat")) as por_aprobar:
                    pagina.locator("#ast-enviar").click()
                assert por_aprobar.value.json()["estado"] == "pendiente_aprobacion"
                with pagina.expect_response(lambda r: r.url.endswith("/agente/decidir")) as aprobada:
                    pagina.get_by_role("button", name="Aprobar", exact=True).click()
                id_creado = aprobada.value.json()["remision_id"]
                assert comprobar(http.get(f"/remisiones/{id_creado}", headers=medico), 200)["paciente_id"] == a
                expect(pagina.locator("#ast-chat")).to_contain_text("Aprobada por ti")

                pagina.evaluate("id => ir('ficha', { remision: id })", rid)
                dictado = pagina.get_by_role("dialog", name="Dictar llamada")
                pagina.locator("#f-dictar").click()
                dictado.get_by_role("button", name="Usar llamada de ejemplo").click()
                with pagina.expect_response(lambda r: r.url.endswith("/bitacora/voz/extraer")) as extraida:
                    dictado.locator("#voz-extraer").click()
                resultado = extraida.value.json()
                assert resultado["modo"] == "reglas"
                assert nombre not in resultado["transcripcion_seudonimizada"]
                expect(dictado.locator("#v-res")).not_to_have_value("")
                comprobar(http.post("/bitacora/voz/transcribir", headers=medico), 410)

                def fallo(paciente_id):
                    raise RuntimeError("detalle privado que no debe salir")

                with monkeypatch.context() as parche:
                    parche.setattr(herramientas, "datos_clinicos", fallo)
                    respuesta = comprobar(http.post("/agente/chat", headers=medico, json={
                        **datos, "conversacion_id": "error-herramienta", "mensaje": "Datos del paciente",
                    }), 200)
                    assert respuesta["estado"] == "respondido"
                    assert "detalle privado" not in json.dumps(respuesta)
                    parche.setattr(agente_router.ESTADO["harness"], "timeout_s", 0.001)
                    assert comprobar(http.post("/agente/chat", headers=medico, json={
                        **datos, "conversacion_id": "tiempo-agotado",
                    }), 200)["estado"] == "tiempo_agotado"
                navegador.close()

            with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
                cur.execute("SELECT payload FROM agente_invocaciones WHERE id = %s;", (expirada,))
                assert cur.fetchone()[0] == {"expirado": True, "cantidad_mensajes": 1}
                cur.execute("""SELECT i.tipo, u.rol, i.ip, i.detalle FROM incidentes_seguridad i
                               JOIN usuarios u ON u.id = i.usuario_id
                               WHERE i.tipo IN ('agente_error_herramienta', 'agente_tiempo_agotado');""")
                assert {(tipo, rol, ip) for tipo, rol, ip, _ in cur.fetchall()} == {
                    ("agente_error_herramienta", "medico", "127.0.0.1"),
                    ("agente_tiempo_agotado", "medico", "127.0.0.1"),
                }
        finally:
            servidor.should_exit = True
            hilo.join(timeout=10)


def test_limites_del_agente_y_del_rag(entorno, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from agente.harness import ContextoExcedido, MAX_CONTEXT_BYTES, VentanaContexto
    from rag import motor

    class Solicitud(SimpleNamespace):
        def override(self, **cambios):
            return Solicitud(**(vars(self) | cambios))

    nueva = HumanMessage(content="Consulta vigente")
    resultado = ToolMessage(content="Dato de la guía", tool_call_id="llamada")
    llamada = AIMessage(content="", tool_calls=[{"name": "buscar_guias", "args": {}, "id": "llamada"}])
    solicitud = Solicitud(messages=[HumanMessage(content="á" * MAX_CONTEXT_BYTES), AIMessage(content="Otra"),
                                    nueva, llamada, resultado], system_prompt="Reglas")
    ventana = VentanaContexto()
    assert ventana._recortar(solicitud).messages == [nueva, llamada, resultado]
    with pytest.raises(ContextoExcedido):
        ventana._recortar(solicitud.override(messages=[HumanMessage(content="á" * MAX_CONTEXT_BYTES)]))

    def proveedor(url, **opciones):
        assert opciones["json"]["max_completion_tokens"] == 800
        assert opciones["timeout"] == 20
        assert "instrucción interna" not in str(opciones["json"])
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "choices": [{"message": {"content": "<think>detalle técnico</think>Respuesta [1]"}}],
        })

    fragmentos = [{"fuente": "Guía sintética", "texto": "a" * 1200 + "instrucción interna"}]
    monkeypatch.setenv("GROQ_API_KEY", "solo-prueba-sin-red")
    monkeypatch.setattr(httpx, "post", proveedor)
    mensajes = motor.construir_mensajes("¿Qué dice la guía?", fragmentos)
    assert motor.generar(mensajes, fragmentos) == ("Respuesta [1]", "llm")


def test_taller_agente_imc_grasa_y_formatos(monkeypatch):
    from pydantic import ValidationError
    from xml.etree import ElementTree as ET

    monkeypatch.syspath_prepend(str(RAIZ / "docs"))
    from taller_agente_03nov import Medidas, calcular_masa_grasa, ejecutar

    medidas = Medidas(peso_kg=95, altura_m=1.82, grasa_pct=24)
    xml = ET.fromstring(ejecutar(medidas, "xml"))
    assert xml.findtext("imc") == "28.68"
    assert xml.findtext("masa_grasa") == "22.80"
    assert "| Masa grasa (kg) | 22.80 |" in ejecutar(medidas, "markdown")
    with pytest.raises(ValidationError):
        Medidas(peso_kg=95, altura_m=0, grasa_pct=24)
    with pytest.raises(ValidationError):
        calcular_masa_grasa.invoke({"peso_kg": 95, "grasa_pct": 101})
