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
        main.contabilidad.preparar_indices_facturas()
        importar_usuario = main.auth.hash_password(CLAVE_PRUEBA)
        with closing(psycopg2.connect(pg)) as db, db, db.cursor() as cur:
            for nombre, rol in (
                ("Auditor", "admin"), ("Medico", "medico"), ("Colega", "medico"),
                ("Especialista", "especialista"), ("Contable", "contable"),
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


def test_historia_documental(entorno, autorizaciones, monkeypatch):
    import io
    from fastapi import HTTPException
    from pypdf import PdfReader
    import pacs

    _, api, pg = entorno
    h = autorizaciones["medico"]
    p = comprobar(paciente(api, h, nombre="Paciente <documental> & prueba"), 201)
    rid = comprobar(api.post("/remisiones", headers=h, json=datos_remision(p["id"])), 201)["id"]
    eid = comprobar(api.get("/me", headers=autenticado(api, "especialista")), 200)["id"]
    iid = comprobar(api.post("/remitir", headers=h, json={
        "paciente_id": p["id"], "especialista_id": eid, "motivo": "Concepto documental de cardiología",
    }), 201)["id"]
    comprobar(api.post(f"/remitir/{iid}/aceptar", headers=autenticado(api, "especialista")), 200)
    uid = comprobar(api.get("/me", headers=h), 200)["id"]
    with closing(psycopg2.connect(pg)) as db, db, db.cursor() as cur:
        for estado in ("aprobado", "pendiente", "rechazado"):
            cur.execute("""INSERT INTO reportes_ia
                (paciente_id, remision_id, probabilidad_urgente, prediccion,
                 payload_seudonimizado, estado, revisado_por, revisado_en, observacion_revision)
                VALUES (%s,%s,0.2,%s,'{}',%s,%s,now(),%s);""",
                (p["id"], rid, f"RESULTADO-{estado}", estado, uid, "Revisión clínica documental"))
        cur.execute("""INSERT INTO imagenes_eliminadas
            (instance_id,paciente_id,descripcion,modalidad,motivo,eliminada_por)
            VALUES ('oculta-documental',%s,'No debe exportarse','CR','Prueba de borrado lógico',%s);""", (p["id"], uid))
    monkeypatch.setattr(pacs, "list_images", lambda documento: [
        {"instance_id": "estudio-documental", "descripcion": "Radiografía de tórax documental",
         "modalidad": "CR", "fecha": "20261117", "hora": "103000"},
        {"instance_id": "oculta-documental", "descripcion": "No debe exportarse"},
    ])
    ruta = f"/pacientes/{p['id']}/historia"
    historia = comprobar(api.get(ruta, headers=h), 200)
    assert historia["interconsultas"][0]["id"] == iid
    assert historia["interconsultas"][0]["estado"] == "aceptada"
    assert historia["interconsultas"][0]["especialista_nombre"] == "Especialista"
    assert [i["instance_id"] for i in historia["estudios"]] == ["estudio-documental"]
    assert historia["estudios_disponibles"] is True
    [reporte] = historia["reportes_ia"]
    assert reporte["revisor_nombre"] == "Medico" and reporte["revisado_en"]
    assert reporte["observacion_revision"] == "Revisión clínica documental"
    pdf = api.get(ruta + ".pdf", headers=h)
    assert pdf.status_code == 200
    texto = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
    for esperado in ("Concepto documental de cardiología", "Radiografía de tórax documental",
                     "Revisión clínica documental", "RESULTADO-aprobado", "Medico"):
        assert esperado in texto
    assert "RESULTADO-pendiente" not in texto and "RESULTADO-rechazado" not in texto
    assert "No debe exportarse" not in texto
    assert "no-store" in pdf.headers["cache-control"]
    comprobar(api.get(ruta, headers=autorizaciones["colega"]), 404)
    monkeypatch.setattr(pacs, "list_images", lambda _: (_ for _ in ()).throw(HTTPException(503, "PACS caído")))
    parcial = comprobar(api.get(ruta, headers=h), 200)
    assert parcial["estudios_disponibles"] is False and parcial["advertencias"]
    degradado = api.get(ruta + ".pdf", headers=h)
    texto_degradado = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(degradado.content)).pages)
    assert "inventario de estudios está incompleto" in texto_degradado
    with closing(psycopg2.connect(pg)) as db, db, db.cursor() as cur:
        cur.execute("UPDATE remisiones_medicas SET observaciones = %s WHERE id = %s;",
                    ("Nota extensa para verificar la paginación clínica. " * 220, iid))
    largo = api.get(ruta + ".pdf", headers=h)
    assert largo.status_code == 200 and len(PdfReader(io.BytesIO(largo.content)).pages) > 1


def test_facturacion_documental_concurrente(entorno, autorizaciones):
    from pymongo.errors import DuplicateKeyError
    import contabilidad

    _, api, _ = entorno
    h = autorizaciones["auditor"]
    contabilidad.preparar_indices_facturas()
    contabilidad.preparar_indices_facturas()  # arranque idempotente
    p = comprobar(paciente(api, h), 201)
    rid = comprobar(api.post("/remisiones", headers=h, json=datos_remision(p["id"])), 201)["id"]
    comprobar(api.post(f"/registro/remisiones/{rid}/cierre", headers=h, json={"resultado": "rechazada"}), 200)
    ruta = f"/contabilidad/remisiones/{rid}/facturar"
    barrera = threading.Barrier(4)
    def emitir(_):
        barrera.wait(timeout=10)
        if _ % 2:
            return api.post("/facturas", headers=h, json={
                "paciente_id": p["id"], "servicio": "remision", "remision_id": rid, "valor": 1,
            })
        return api.post(ruta, headers=h)
    with ThreadPoolExecutor(max_workers=4) as ejecutor:
        respuestas = list(ejecutor.map(emitir, range(4)))
    assert sorted(r.status_code for r in respuestas) == [201, 409, 409, 409]
    factura = next(r.json() for r in respuestas if r.status_code == 201)
    vigente = contabilidad.coleccion_facturas.find_one({"remision_id": rid, "estado_pago": "pendiente"})
    copia = {k: v for k, v in vigente.items() if k != "_id"}
    with pytest.raises(DuplicateKeyError):
        contabilidad.coleccion_facturas.insert_one(copia)
    comprobar(api.put(f"/facturas/{factura['_id']}/anular", headers=h, json={"motivo": "Refacturación documental"}), 200)
    nueva = comprobar(api.post(ruta, headers=h), 201)
    assert nueva["_id"] != factura["_id"]
    assert contabilidad.coleccion_facturas.count_documents({"remision_id": rid}) == 2
    assert contabilidad.coleccion_facturas.count_documents({"remision_id": rid, "estado_pago": {"$ne": "anulada"}}) == 1
    recibo = comprobar(api.get(f"/contabilidad/remisiones/{rid}/recibo.json", headers=h), 200)
    assert recibo["total"] == nueva["valor"] and len(recibo["facturas"]) == 1
    assert recibo["facturas_anuladas"][0]["_id"] == factura["_id"]


def test_recibo_documental(entorno, autorizaciones, monkeypatch):
    import io
    from pypdf import PdfReader
    from contabilidad import crear_factura_ia, TARIFAS

    _, api, pg = entorno
    h = autorizaciones["auditor"]
    p = comprobar(paciente(api, h), 201)
    rid = comprobar(api.post("/remisiones", headers=h, json=datos_remision(p["id"])), 201)["id"]
    ruta = f"/contabilidad/remisiones/{rid}"
    comprobar(api.get(ruta + "/recibo.json", headers=h), 409)
    comprobar(api.post(f"/registro/remisiones/{rid}/cierre", headers=h, json={"resultado": "rechazada"}), 200)
    comprobar(api.get(ruta + "/recibo.json", headers=h), 409)
    factura = comprobar(api.post(ruta + "/facturar", headers=h), 201)
    with closing(psycopg2.connect(pg)) as db, db, db.cursor() as cur:
        cur.execute("""INSERT INTO reportes_ia (paciente_id,remision_id,probabilidad_urgente,
            prediccion,payload_seudonimizado,estado) VALUES (%s,%s,0.2,'Diagnóstico secreto','{}','aprobado')
            RETURNING id;""", (p["id"], rid))
        reporte_id = cur.fetchone()[0]
    fid_ia = crear_factura_ia(p["id"], reporte_id)
    monkeypatch.setitem(TARIFAS, "estancia_urgencias_hora", 999999)
    recibo = comprobar(api.get(ruta + "/recibo.json", headers=h), 200)
    assert len(recibo["facturas"]) == 2 and recibo["radicado"] == factura["radicado"]
    assert recibo["total"] == factura["valor"] + 30000
    assert recibo["saldo"] == recibo["total"] and recibo["pagado"] == 0
    assert recibo["estado_cobro"] == "pendiente"
    assert all(f["fecha"].endswith("+00:00") for f in recibo["facturas"])
    comprobar(api.get(ruta + "/recibo.pdf", headers=autorizaciones["medico"]), 403)
    comprobar(api.get(f"/contabilidad/remisiones/2147483000/recibo.json", headers=h), 404)
    comprobar(api.put(f"/facturas/{factura['_id']}/glosar", headers=h, json={"motivo": "Conciliación de la EPS"}), 200)
    assert comprobar(api.get(ruta + "/recibo.json", headers=h), 200)["estado_cobro"] == "glosada"
    for fid in (factura["_id"], fid_ia):
        comprobar(api.put(f"/facturas/{fid}/pagar", headers=h), 200)
    final = comprobar(api.get(ruta + "/recibo.json", headers=h), 200)
    assert final["saldo"] == 0 and final["pagado"] == final["total"]
    assert final["estado_cobro"] == "pagada"
    assert all(f["conceptos"] for f in final["facturas"])
    pdf = api.get(ruta + "/recibo.pdf", headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    texto = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
    assert factura["numero"] in texto and "Saldo" in texto and "pagada" in texto.lower()
    assert "Diagnóstico secreto" not in texto


def test_pago_y_anulacion_documental_no_se_sobrescriben(entorno, autorizaciones, monkeypatch):
    from bson import ObjectId
    import contabilidad

    _, api, _ = entorno
    h = autorizaciones["auditor"]
    p = comprobar(paciente(api, h), 201)
    f = comprobar(api.post("/facturas", headers=h, json={
        "paciente_id": p["id"], "servicio": "consulta", "valor": 1000,
    }), 201)
    barrera = threading.Barrier(2)
    original = contabilidad.coleccion_facturas.find_one_and_update
    def escritura_concurrente(filtro, *args, **kwargs):
        if filtro.get("_id") == ObjectId(f["_id"]):
            barrera.wait(timeout=10)
        return original(filtro, *args, **kwargs)
    monkeypatch.setattr(contabilidad.coleccion_facturas, "find_one_and_update", escritura_concurrente)
    with ThreadPoolExecutor(max_workers=2) as ejecutor:
        pagar = ejecutor.submit(api.put, f"/facturas/{f['_id']}/pagar", headers=h)
        anular = ejecutor.submit(api.put, f"/facturas/{f['_id']}/anular", headers=h, json={"motivo": "Anulación concurrente"})
        assert sorted([pagar.result().status_code, anular.result().status_code]) == [200, 409]
    final = comprobar(api.get(f"/facturas/{f['_id']}", headers=h), 200)
    assert final["estado_pago"] in ("pagada", "anulada") and len(final["historial"]) == 1


def test_factura_documental_ia_solo_tras_aprobar_y_reintenta(entorno, autorizaciones, monkeypatch):
    import contabilidad
    from eventos import completar_facturas_ia

    _, api, pg = entorno
    h = autorizaciones["medico"]
    p = comprobar(paciente(api, h), 201)
    uid = comprobar(api.get("/me", headers=h), 200)["id"]
    with closing(psycopg2.connect(pg)) as db, db, db.cursor() as cur:
        cur.execute("""INSERT INTO reportes_ia (paciente_id,creado_por,probabilidad_urgente,
            prediccion,payload_seudonimizado) VALUES (%s,%s,0.2,'Prueba documental','{}') RETURNING id;""", (p["id"], uid))
        reporte = cur.fetchone()[0]
    filtro = {"servicio": "analisis_ia", "reporte_id": reporte}
    assert contabilidad.coleccion_facturas.count_documents(filtro) == 0
    def no_disponible(*args, **kwargs):
        raise RuntimeError("Mongo de facturación no disponible (simulado)")
    with monkeypatch.context() as parche:
        parche.setattr(contabilidad, "crear_factura_ia", no_disponible)
        comprobar(api.post(f"/ia/reportes/{reporte}/aprobar", headers=h,
                           json={"observaciones": "Análisis revisado clínicamente"}), 200)
    assert contabilidad.coleccion_facturas.count_documents(filtro) == 0
    with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
        cur.execute("SELECT reporte_id FROM facturas_ia_pendientes WHERE reporte_id = %s;", (reporte,))
        assert cur.fetchone() == (reporte,)
    completar_facturas_ia(reporte)
    completar_facturas_ia(reporte)
    assert contabilidad.coleccion_facturas.count_documents(filtro) == 1
    with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
        cur.execute("SELECT reporte_id FROM facturas_ia_pendientes WHERE reporte_id = %s;", (reporte,))
        assert cur.fetchone() is None


def test_documentos_en_navegador(entorno, autorizaciones, monkeypatch, tmp_path):
    import io
    import json
    import uvicorn
    import pacs
    from pypdf import PdfReader
    from playwright.sync_api import expect, sync_playwright

    main, api, pg = entorno
    medico = autorizaciones["medico"]
    p = comprobar(paciente(api, medico, nombre="Paciente documental navegador"), 201)
    rid = comprobar(api.post("/remisiones", headers=medico, json=datos_remision(p["id"])), 201)["id"]
    especialista = autenticado(api, "especialista")
    eid = comprobar(api.get("/me", headers=especialista), 200)["id"]
    ic = comprobar(api.post("/remitir", headers=medico, json={
        "paciente_id": p["id"], "especialista_id": eid,
        "motivo": "Interconsulta exportada desde el navegador",
    }), 201)
    comprobar(api.post(f"/remitir/{ic['id']}/aceptar", headers=especialista), 200)
    uid = comprobar(api.get("/me", headers=medico), 200)["id"]
    with closing(psycopg2.connect(pg)) as db, db, db.cursor() as cur:
        cur.execute("""INSERT INTO reportes_ia (paciente_id,remision_id,probabilidad_urgente,prediccion,
            payload_seudonimizado,estado,revisado_por,revisado_en,observacion_revision)
            VALUES (%s,%s,0.2,'Resultado aprobado documental','{}','aprobado',%s,now(),%s);""",
            (p["id"], rid, uid, "Revisión clínica exportable desde el navegador"))
    monkeypatch.setattr(pacs, "list_images", lambda _: [{
        "instance_id": "estudio-navegador", "descripcion": "Estudio de tórax para traslado",
        "modalidad": "CR", "fecha": "20261117", "hora": "103000",
    }])
    comprobar(api.post(f"/registro/remisiones/{rid}/cierre", headers=medico, json={"resultado": "rechazada"}), 200)
    salida = Path(os.getenv("TRAZARED_EVIDENCIA_DOCUMENTOS", str(tmp_path)))
    salida.mkdir(parents=True, exist_ok=True)
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
                pagina = navegador.new_page(viewport={"width": 1440, "height": 1000})
                pagina.goto(base + "/app/")
                with pagina.expect_popup() as aviso:
                    pagina.get_by_role("link", name="Tratamiento de datos y alcance académico").click()
                expect(aviso.value.get_by_role("heading", name="Tratamiento de datos y alcance académico")).to_be_visible()
                aviso.value.close()
                pagina.locator("#login-usuario").fill("medico@pruebas.test")
                pagina.locator("#login-clave").fill(CLAVE_PRUEBA)
                pagina.locator("#form-login button[type=submit]").click()
                expect(pagina.locator("#vista-app")).to_be_visible()
                pagina.locator('[data-seccion="pacientes"]').click()
                pagina.locator(f'tr[data-pac="{p["id"]}"] td').first.click()
                pagina.locator("#anexos summary").click()
                expect(pagina.locator("#btn-historia-pdf")).to_be_visible()
                for formato in ("pdf", "json", "fhir"):
                    with pagina.expect_download() as descarga:
                        pagina.locator(f"#btn-historia-{formato}").click()
                    archivo = salida / f"historia_navegador.{formato}"
                    descarga.value.save_as(archivo)
                    assert archivo.stat().st_size > 100
                doc_fhir = json.loads((salida / "historia_navegador.fhir").read_text())
                assert doc_fhir["type"] == "document"
                assert doc_fhir["entry"][0]["resource"]["status"] == "preliminary"
                historia = json.loads((salida / "historia_navegador.json").read_text())
                assert historia["interconsultas"][0]["id"] == ic["id"]
                assert historia["reportes_ia"][0]["observacion_revision"]
                assert historia["estudios"][0]["instance_id"] == "estudio-navegador"
                pagina.screenshot(path=str(salida / "historia_navegador.png"), full_page=True)
                pagina.close()
                contable = navegador.new_page(viewport={"width": 1440, "height": 1000})
                errores_documentos = []
                contable.on("pageerror", lambda e: errores_documentos.append(str(e)))
                contable.goto(base + "/app/")
                contable.locator("#login-usuario").fill("contable@pruebas.test")
                contable.locator("#login-clave").fill(CLAVE_PRUEBA)
                contable.locator("#form-login button[type=submit]").click()
                boton = contable.locator(f'[data-facturar="{rid}"]')
                expect(boton).to_be_visible()
                boton.click()
                recibo = contable.locator(f'[data-recibo-pdf="{rid}"]')
                expect(recibo).to_be_visible()
                factura = comprobar(api.get("/facturas", headers=autenticado(api, "contable"),
                                            params={"remision_id": rid}), 200)[0]
                contable.on("dialog", lambda d: d.accept("Glosa documental en navegador"))
                contable.locator(f'[data-glosar="{factura["_id"]}"]').click()
                expect(contable.locator(f'[data-anular="{factura["_id"]}"]')).to_be_visible()
                contable.locator(f'[data-pagar="{factura["_id"]}"]').click()
                expect(contable.locator(f'[data-pagar="{factura["_id"]}"]')).to_have_count(0)
                contable.reload()
                expect(recibo).to_be_visible()
                assert not errores_documentos
                for formato in ("pdf", "json"):
                    with contable.expect_download() as descarga:
                        contable.locator(f'[data-recibo-{formato}="{rid}"]').click()
                    descarga.value.save_as(salida / f"recibo_navegador.{formato}")
                final = json.loads((salida / "recibo_navegador.json").read_text())
                # El análisis sembrado aún no se ha facturado: no se oculta este pendiente.
                assert final["estado_cobro"] == "pendiente_facturacion"
                assert final["saldo"] == 0 and final["reportes_sin_factura"]
                from contabilidad import crear_factura_ia
                fid_ia = crear_factura_ia(p["id"], historia["reportes_ia"][0]["id"])
                comprobar(api.put(f"/facturas/{fid_ia}/pagar", headers=autenticado(api, "contable")), 200)
                with contable.expect_download() as descarga:
                    recibo.click()
                descarga.value.save_as(salida / "recibo_navegador.pdf")
                with contable.expect_download() as descarga:
                    contable.locator(f'[data-recibo-json="{rid}"]').click()
                descarga.value.save_as(salida / "recibo_navegador.json")
                final = json.loads((salida / "recibo_navegador.json").read_text())
                assert final["estado_cobro"] == "pagada" and final["saldo"] == 0
                assert len(final["facturas"]) == 2
                contable.screenshot(path=str(salida / "contabilidad_navegador.png"), full_page=True)
                for nombre in ("historia_navegador.pdf", "recibo_navegador.pdf"):
                    lector = PdfReader(io.BytesIO((salida / nombre).read_bytes()))
                    assert lector.pages and all(p.extract_text() for p in lector.pages)
                denegada = contable.evaluate("""async pid => {
                    try { await API.get(`/pacientes/${pid}/historia`); return 200; }
                    catch (e) { return e.status; }
                }""", p["id"])
                assert denegada == 403
                navegador.close()
        finally:
            servidor.should_exit = True
            hilo.join(timeout=10)


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
                api.post(ruta, headers=medico, json={"observaciones": "Revisión sintética"})
        with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
            cur.execute("SELECT estado FROM reportes_ia WHERE id = %s;", (reporte["id"],))
            assert cur.fetchone() == ("pendiente",)
        assert comprobar(api.post(ruta, headers=medico, json={"observaciones": "Revisión sintética"}), 200)["estado"] == estado
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
                expect(pagina.locator("#m-fec-error")).to_contain_text("no puede ser anterior al nacimiento")
                expect(fecha).to_have_attribute("aria-invalid", "true")
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

                aviso = "Asistente bloqueado temporalmente. Intenta más tarde."
                pagina.route("**/agente/chat", lambda ruta: ruta.fulfill(status=403, json={"detail": aviso}))
                pagina.locator("#ast-texto").fill("Consulta de prueba")
                pagina.locator("#ast-enviar").click()
                expect(pagina.locator("#ast-chat")).to_contain_text(aviso)
                pagina.unroute("**/agente/chat")

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
            "choices": [{"message": {"content": "<think>detalle técnico</think>[1]"}}],
        })

    fragmentos = [{"fuente": "Guía sintética", "texto": "a" * 1200 + "instrucción interna"}]
    monkeypatch.setenv("GROQ_API_KEY", "solo-prueba-sin-red")
    monkeypatch.setattr(httpx, "post", proveedor)
    mensajes = motor.construir_mensajes("¿Qué dice la guía?", fragmentos)
    assert motor.generar(mensajes, fragmentos) == ([1], "llm")


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


def test_rag_citas_verificadas_y_evaluacion(entorno, monkeypatch):
    import httpx
    from rag import evaluacion, evaluacion_generacion, motor

    pregunta = "¿En cuánto tiempo se debe atender un triage nivel II en Colombia?"
    monkeypatch.setenv("GROQ_API_KEY", "solo-prueba-sin-red")
    contenido = "[2]"

    def proveedor(url, **opciones):
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "choices": [{"message": {"content": contenido}}],
        })

    monkeypatch.setattr(httpx, "post", proveedor)
    resultado = motor.responder(pregunta, None)
    assert resultado["modo"] == "llm"
    fuente, = resultado["fuentes"]
    assert fuente["n"] == 1
    assert fuente["texto"] == resultado["contextos_recuperados"][1]
    assert fuente["url"].startswith("https://www.minsalud.gov.co/")
    assert resultado["respuesta"] == f"{fuente['texto']} [1]"
    assert "treinta minutos" in resultado["respuesta"]

    for contenido in ("Afirmación inventada [1]", "[4]", "[1, 1]", "[true]", '{"url":"https://pruebas.test"}'):
        resultado = motor.responder(pregunta, None)
        assert resultado["modo"] == "extractivo"
        assert resultado["respuesta"] == "\n\n".join(
            f"{f['texto']} [{f['n']}]" for f in resultado["fuentes"]
        )

    contenido = "[]"
    resultado = motor.responder(pregunta, None)
    assert resultado["modo"] == "abstencion" and resultado["fuentes"] == []
    filas = evaluacion_generacion.recolectar()
    triage = next(f for f in filas if f["caso"] == "triage")
    assert triage["respuesta"] == motor.SIN_EVIDENCIA
    assert triage["contextos"] and triage["referencia"]
    assert "abstencion_inesperada" in triage["fallas"]

    monkeypatch.delenv("GROQ_API_KEY")
    resultado = evaluacion.abstencion()
    assert resultado["se_abstiene_fuera"] == resultado["total_fuera"]
    assert resultado["responde_dentro"] == resultado["total_dentro"]


def test_rag_pgvector_sincroniza_corpus(entorno, monkeypatch):
    from rag import motor

    _, _, pg = entorno
    with closing(psycopg2.connect(pg)) as db, db, db.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector');")
        if not cur.fetchone()[0]:
            pytest.skip("La prueba del índice requiere la imagen pgvector")
    monkeypatch.setattr(motor, "PGVECTOR", {})
    assert motor.preparar_pgvector(pg)["listo"]
    with closing(psycopg2.connect(pg)) as db, db, db.cursor() as cur:
        cur.execute("""INSERT INTO rag_chunks
                       SELECT 'retirado', doc_id, tipo, estado, paciente, fuente, texto, modelo, embedding
                       FROM rag_chunks WHERE chunk_id = 'G15-0';""")
        cur.execute("UPDATE rag_chunks SET fuente = 'Edición previa' WHERE chunk_id = 'G15-0';")
    assert motor.preparar_pgvector(pg)["listo"]
    with closing(psycopg2.connect(pg)) as db, db, db.cursor() as cur:
        cur.execute("SELECT chunk_id, fuente FROM rag_chunks WHERE chunk_id IN ('retirado','G15-0');")
        assert cur.fetchall() == [("G15-0", "OMS y CICR · Atención básica de emergencia")]
    recuperados = motor.buscar("¿Qué evalúa el método ABCDE de atención básica de emergencia?", None)
    assert recuperados[0]["chunk_id"] == "G15-0"


def test_rag_revision_api_y_navegador(entorno, autorizaciones, monkeypatch):
    import shutil
    import subprocess
    import uvicorn
    from playwright.sync_api import expect, sync_playwright

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    main, api, pg = entorno
    medico, admin, colega = [autorizaciones[k] for k in ("medico", "auditor", "colega")]
    pid = comprobar(paciente(api, medico, nombre="Caso sintético RAG"), 201)["id"]
    comprobar(api.post("/remisiones", json=datos_remision(pid), headers=medico), 201)
    otro = comprobar(paciente(api, medico, nombre="Otro caso sintético RAG"), 201)["id"]
    comprobar(api.post("/remisiones", json=datos_remision(otro), headers=medico), 201)
    pregunta = "¿En cuánto tiempo se debe atender un triage nivel II en Colombia?"
    datos = {"pregunta": pregunta, "paciente_id": pid, "enviar_a_revision": True}
    base_paciente = f"/pacientes/{pid}"
    pendientes = f"{base_paciente}/rag/pendientes"
    historia = f"{base_paciente}/historia"
    comprobar(api.get(pendientes, headers=colega), 404)
    comprobar(api.post("/rag/preguntar", headers=admin, json=datos), 403)
    comprobar(api.post("/rag/preguntar", headers=medico, json=dict(datos, paciente_id=None)), 422)
    sin_fuentes = comprobar(api.post("/rag/preguntar", headers=medico,
                                      json=dict(datos, pregunta="¿Quién ganó el mundial de fútbol de 2014?")), 200)
    assert sin_fuentes["revision_id"] is None
    assert comprobar(api.get(pendientes, headers=medico), 200) == []

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
                pagina.locator("#login-usuario").fill("medico@pruebas.test")
                pagina.locator("#login-clave").fill(CLAVE_PRUEBA)
                pagina.locator("#form-login button[type=submit]").click()
                asistente = pagina.locator('[data-seccion="asistente"]')
                asistente.click()
                pagina.get_by_role("button", name="Consulta a guías (RAG)").click()
                expect(pagina.locator("#ast-enviar-revision")).to_be_hidden()
                pagina.locator("#ast-pid").fill(str(pid))
                pagina.locator("#ast-abrir").click()
                expect(pagina.locator("#ast-pinfo")).to_contain_text("Caso sintético RAG")

                def consultar():
                    pagina.locator("#ast-enviar-revision").check()
                    pagina.locator("#ast-texto").fill(pregunta)
                    with pagina.expect_response(lambda r: r.url.endswith("/rag/preguntar")) as respuesta:
                        pagina.locator("#ast-enviar").click()
                    assert respuesta.value.status == 200
                    return respuesta.value.json()["revision_id"]

                aprobado = consultar()
                tarjeta = pagina.locator(f'[data-rag-id="{aprobado}"]')
                expect(tarjeta).to_be_visible()
                tarjeta.locator("summary").click()
                enlace = tarjeta.get_by_role("link").first
                expect(enlace).to_have_attribute("href", "https://www.minsalud.gov.co/sites/rid/Lists/BibliotecaDigital/RIDE/DE/DIJ/resolucion-5596-de-2015.pdf")
                expect(tarjeta).to_contain_text("treinta minutos")
                assert comprobar(api.get(historia, headers=medico), 200)["resultados_rag"] == []
                revision = f"{base_paciente}/rag/{aprobado}/revisar"
                comprobar(api.post(revision, headers=colega, json={"decision": "aprobado"}), 404)
                comprobar(api.post(revision, headers=admin, json={"decision": "aprobado"}), 403)
                comprobar(api.post(f"/pacientes/{otro}/rag/{aprobado}/revisar",
                                   headers=medico, json={"decision": "aprobado"}), 404)
                comprobar(api.post(revision, headers=medico, json={"decision": "rechazado", "motivo": " "}), 422)

                # Cambiar de vista y volver obliga a recuperar la propuesta persistida por API.
                pagina.locator('[data-seccion="pacientes"]').click()
                asistente.click()
                expect(tarjeta).to_be_visible()
                tarjeta.get_by_role("button", name="Aprobar para la historia clínica").click()
                expect(tarjeta).to_have_count(0)
                comprobar(api.post(revision, headers=medico, json={"decision": "aprobado"}), 409)

                rechazado = consultar()
                tarjeta = pagina.locator(f'[data-rag-id="{rechazado}"]')
                tarjeta.get_by_role("button", name="Rechazar resultado").click()
                expect(tarjeta).to_be_visible()
                tarjeta.get_by_role("textbox", name="Motivo del rechazo").fill("Resultado de prueba")
                tarjeta.get_by_role("button", name="Rechazar resultado").click()
                expect(tarjeta).to_have_count(0)

                pagina.locator('[data-seccion="pacientes"]').click()
                pagina.locator(f'[data-pac="{pid}"]').click()
                pagina.get_by_text("Anexos clínicos: datos del paciente", exact=False).click()
                panel = pagina.locator("#historia-rag-aprobado")
                expect(panel).to_contain_text(f"Consulta #{aprobado}")
                expect(panel).not_to_contain_text(f"Consulta #{rechazado}")
                panel.locator("summary").first.click()
                expect(panel).to_contain_text("treinta minutos")
                navegador.close()
        finally:
            servidor.should_exit = True
            hilo.join(timeout=10)
            assert not hilo.is_alive()

    filas = comprobar(api.get(historia, headers=medico), 200)["resultados_rag"]
    assert [f["id"] for f in filas] == [aprobado]
    assert filas[0]["revisado_por"]
    assert comprobar(api.get(f"/pacientes/{otro}/historia", headers=medico), 200)["resultados_rag"] == []
    pdf = api.get(historia + ".pdf", headers=medico)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    if shutil.which("pdftotext"):
        texto = subprocess.run(["pdftotext", "-", "-"], input=pdf.content, capture_output=True, check=True).stdout
        assert f"Consulta #{aprobado}".encode() in texto
        assert f"Consulta #{rechazado}".encode() not in texto

    concurrente = comprobar(api.post("/rag/preguntar", headers=medico, json=datos), 200)["revision_id"]
    url = f"{base_paciente}/rag/{concurrente}/revisar"
    from rag import router as router_rag

    def sello_fallido(*args, **kwargs):
        raise RuntimeError("Fallo simulado del sello RAG")

    with monkeypatch.context() as parche:
        parche.setattr(router_rag, "registrar_auditoria", sello_fallido)
        with pytest.raises(RuntimeError, match="Fallo simulado del sello RAG"):
            api.post(url, headers=medico, json={"decision": "aprobado"})
    assert [r["id"] for r in comprobar(api.get(pendientes, headers=medico), 200)] == [concurrente]
    with ThreadPoolExecutor(max_workers=2) as tareas:
        estados = list(tareas.map(lambda _: api.post(url, headers=medico, json={"decision": "aprobado"}).status_code,
                                   range(2)))
    assert sorted(estados) == [200, 409]
    with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
        cur.execute("""SELECT registro_id, accion FROM auditoria
                       WHERE tabla = 'revisiones_rag' AND accion IN ('rag_aprobado','rag_rechazado')
                        AND hash IS NOT NULL AND registro_id = ANY(%s);""", ([aprobado, rechazado, concurrente],))
        assert set(cur.fetchall()) == {(aprobado, "rag_aprobado"), (rechazado, "rag_rechazado"),
                                       (concurrente, "rag_aprobado")}


def test_observaciones_y_prediccion_validan_signos_coherentes(entorno, autorizaciones, monkeypatch):
    import ia

    _, api, _ = entorno
    admin = autorizaciones["auditor"]
    medico = autorizaciones["medico"]
    ficha = comprobar(paciente(api, admin), 201)
    remision = comprobar(api.post("/remisiones", headers=medico,
                                  json=datos_remision(ficha["id"])), 201)
    rid = remision["id"]

    def guardar(tipo, valor, **cambios):
        return api.post("/observaciones", headers=medico,
                        json={"remision_id": rid, "tipo": tipo, "valor": valor, **cambios})

    def observaciones():
        return comprobar(api.get("/observaciones", headers=medico,
                                 params={"remision_id": rid}), 200)

    comprobar(guardar("Saturación de oxígeno", 101, unidad="%"), 422)
    comprobar(guardar("Temperatura corporal", 37, unidad="°F"), 422)
    comprobar(guardar("Frecuencia cardiaca", 80, codigo_loinc="8310-5"), 422)
    comprobar(guardar("Escala de Glasgow", 13.5), 422)
    comprobar(guardar("Presión sistólica", 90, valor_complementario=92), 422)
    comprobar(guardar("Presión diastólica", 72, valor_complementario=400), 422)
    assert observaciones() == []

    comprobar(guardar("Presión sistólica", 110, unidad="mmHg", valor_complementario=72), 201)
    comprobar(guardar("Presión diastólica", 120), 422)
    primera_toma = observaciones()
    assert [(v["codigo_loinc"], float(v["valor"]), v["unidad"]) for v in primera_toma] == [
        ("8480-6", 110, "mm[Hg]"), ("8462-4", 72, "mm[Hg]"),
    ]
    assert primera_toma[0]["fecha_observacion"] == primera_toma[1]["fecha_observacion"]
    # El último valor de la toma anterior no debe impedir registrar una toma completa.
    comprobar(guardar("Presión diastólica", 120, valor_complementario=150), 201)
    assert len(observaciones()) == 4

    with monkeypatch.context() as parche:
        def sin_modelo(*args, **kwargs):
            pytest.fail("Una presión incoherente no debe llegar al modelo")
        parche.setattr(ia.httpx, "post", sin_modelo)
        for ruta in ("/ia/predecir", "/ia/asignar", "/ia/analizar"):
            comprobar(api.post(ruta, headers=medico, json={
                "paciente_id": ficha["id"], "remision_id": rid, "sbp": 90, "dbp": 90,
            }), 422)


def test_webhook_dirigido_y_reintento_atomico(entorno, autorizaciones, monkeypatch):
    main, api, pg = entorno
    admin, medico, colega = (autorizaciones[n] for n in ("auditor", "medico", "colega"))
    ficha = comprobar(paciente(api, admin), 201)
    comprobar(api.post("/remisiones", headers=medico, json=datos_remision(ficha["id"])), 201)
    monkeypatch.setattr(main.fhir_sync, "paciente_id_por_fhir_reference", lambda _, **opciones: ficha["id"])
    hapi = []
    monkeypatch.setattr(main.fhir_sync, "crear_recurso_fhir", lambda recurso: hapi.append(recurso) or {"id": "obs-prueba"})
    recurso = {
        "resourceType": "Observation", "subject": {"reference": "Patient/sintetico"},
        "code": {"coding": [{"system": "http://loinc.org", "code": "2339-0"}]},
        "valueQuantity": {"system": main.fhir_sync.UCUM, "unit": "mmHg", "value": 320},
    }
    comprobar(api.post("/fhir/Observation", headers=medico, json=recurso), 422)
    assert not hapi
    recurso["valueQuantity"]["unit"] = "mg/dL"
    comprobar(api.post("/fhir/Observation", headers=colega, json=recurso), 404)
    comprobar(api.post("/fhir/Observation", headers=medico, json=recurso), 201)
    assert hapi[0]["valueQuantity"]["code"] == "mg/dL"

    recurso["id"] = "obs-prueba"
    webhook = {"X-Webhook-Secret": main.seguridad.secreto_webhook()}

    def falla(*args):
        raise RuntimeError("Interrupción en la bandeja")

    with monkeypatch.context() as parche:
        parche.setattr(main.eventos, "_guardar_notificaciones", falla)
        with pytest.raises(RuntimeError, match="Interrupción en la bandeja"):
            api.put("/fhir/observacion/Observation/obs-prueba", headers=webhook, json=recurso)
    with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
        cur.execute("SELECT 1 FROM fhir_observaciones_procesadas WHERE fhir_id = 'obs-prueba';")
        assert cur.fetchone() is None
    for _ in range(2):
        comprobar(api.put("/fhir/observacion/Observation/obs-prueba", headers=webhook, json=recurso), 202)
    bandeja = comprobar(api.get("/notificaciones", headers=medico), 200)
    propios = [n for n in bandeja if n.get("fhir_observation_id") == "obs-prueba"]
    assert sorted(n["tipo"] for n in propios) == ["alerta_critica", "observacion_fhir"]
    for otros in (colega, admin):
        assert not any(n.get("fhir_observation_id") == "obs-prueba"
                       for n in comprobar(api.get("/notificaciones", headers=otros), 200))
    alerta = next(n["alerta_id"] for n in propios if n["tipo"] == "alerta_critica")
    comprobar(api.post(f"/alertas/{alerta}/reconocer", headers=colega, json={"nota": "No soy responsable"}), 404)
    with closing(psycopg2.connect(pg)) as db, db, db.cursor() as cur:
        cur.execute("UPDATE alertas SET creada_en = now() - interval '1 day' WHERE id = %s;", (alerta,))
    with monkeypatch.context() as parche:
        parche.setattr(main.eventos, "_guardar_notificaciones", falla)
        with pytest.raises(RuntimeError, match="Interrupción en la bandeja"):
            main.eventos._escalar_vencidas()
    with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
        cur.execute("SELECT escalada FROM alertas WHERE id = %s;", (alerta,))
        assert cur.fetchone() == (False,)
    with ThreadPoolExecutor(max_workers=2) as hilos:
        list(hilos.map(lambda _: main.eventos._escalar_vencidas(), range(2)))
    assert sum(n["tipo"] == "alerta_escalada" and n["alerta_id"] == alerta
               for n in comprobar(api.get("/notificaciones", headers=admin), 200)) == 1
    comprobar(api.post(f"/alertas/{alerta}/reconocer", headers=medico, json={"nota": "Visto"}), 200)
    comprobar(api.post(f"/notificaciones/{propios[0]['id']}/leer", headers=colega), 403)
    comprobar(api.post(f"/notificaciones/{propios[0]['id']}/leer", headers=medico), 200)


def test_toma_completa_y_revision_en_navegador(entorno, autorizaciones, monkeypatch):
    import io
    import uvicorn
    from PIL import Image
    from fastapi import HTTPException
    from playwright.sync_api import sync_playwright, expect

    main, api, pg = entorno
    admin, medico, colega = (autorizaciones[n] for n in ("auditor", "medico", "colega"))
    ficha = comprobar(paciente(api, admin), 201)
    remision = comprobar(api.post("/remisiones", headers=medico, json=datos_remision(ficha["id"])), 201)
    pid, rid = ficha["id"], remision["id"]

    def registrar(codigo, valor, tipo="Signo de prueba", **extra):
        return comprobar(api.post("/observaciones", headers=medico, json={
            "remision_id": rid, "codigo_loinc": codigo, "valor": valor, "tipo": tipo, **extra,
        }), 201)

    llamadas = []
    def inferir(payload):
        llamadas.append(payload)
        if len(llamadas) == 1:
            raise HTTPException(503, "Modelo temporalmente no disponible")
        return {"version_modelo": "toma-sintetica", "probabilidad": 0.8, "critico": False, "nivel": "urgente"}
    monkeypatch.setattr(main.ia, "llamar_modelo", inferir)
    registrar("8480-6", 120, valor_complementario=80)
    for codigo, valor in (("8867-4", 75), ("9279-1", 16), ("59408-5", 97)):
        registrar(codigo, valor)
    main.ia.procesar_analisis_automaticos()
    assert not llamadas
    monkeypatch.setattr(main.pacs, "dicomize", lambda *args: "imagen-sintetica")
    archivo = io.BytesIO()
    Image.new("L", (10, 10)).save(archivo, "PNG")
    especialista = autenticado(api, "especialista")
    id_especialista = comprobar(api.get("/me", headers=especialista), 200)["id"]

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
                paginas = {}
                for usuario in ("medico", "especialista", "colega"):
                    pagina = navegador.new_page()
                    pagina.goto(base + "/app/")
                    pagina.locator("#login-usuario").fill(f"{usuario}@pruebas.test")
                    pagina.locator("#login-clave").fill(CLAVE_PRUEBA)
                    pagina.locator("#form-login button[type=submit]").click()
                    pagina.wait_for_function("Eventos.conectado()")
                    paginas[usuario] = pagina
                pagina, receptor, ajeno = (paginas[n] for n in ("medico", "especialista", "colega"))
                for sesion in (pagina, receptor):
                    sesion.locator('[data-seccion="remitir"]').click()
                    expect(sesion.locator("#remitir-contenido")).to_be_visible()
                solicitud = comprobar(api.post("/remitir", headers=medico, json={
                    "paciente_id": pid, "especialista_id": id_especialista, "motivo": "Prueba viva de interconsulta",
                }), 201)
                receptor.locator(f'[data-aceptar="{solicitud["id"]}"]').click()
                expect(pagina.locator("#remitir-contenido tr").filter(
                    has_text="Prueba viva de interconsulta")).to_contain_text("Aceptada")
                pagina.locator('[data-seccion="revision"]').click()
                pagina.evaluate("Eventos.detener()")
                registrar("8310-5", 37)
                main.ia.procesar_analisis_automaticos()
                assert len(llamadas) == 1
                with closing(psycopg2.connect(pg)) as db, db, db.cursor() as cur:
                    cur.execute("UPDATE analisis_automaticos_pendientes SET intentar_en = now() WHERE remision_id = %s;", (rid,))
                main.ia.procesar_analisis_automaticos()
                main.ia.procesar_analisis_automaticos()
                assert len(llamadas) == 2
                assert llamadas[-1]["id_seudonimo"] == main.ia.seudonimo(pid)
                assert llamadas[-1]["modo_llegada"] is None and llamadas[-1]["saturacion"] == 97
                reporte, = comprobar(api.get(f"/ia/reportes?paciente_id={pid}", headers=medico), 200)
                assert reporte["automatico"] and reporte["estado"] == "pendiente"
                assert not comprobar(api.get(f"/ia/reportes?paciente_id={pid}", headers=colega), 200)
                historia = f"/pacientes/{pid}/historia"
                assert comprobar(api.get(historia, headers=medico), 200)["reportes_ia"] == []
                ruta = f"/ia/reportes/{reporte['id']}/aprobar"
                comprobar(api.post(ruta, headers=medico), 422)
                comprobar(api.post(ruta, headers=medico, json={"observaciones": "  "}), 422)
                comprobar(api.post(ruta, headers=colega, json={"observaciones": "Ajeno"}), 404)
                with pagina.expect_request(lambda r: r.url.endswith("/eventos/stream")) as canal:
                    pagina.evaluate("() => { Eventos.iniciar(); }")
                assert canal.value.header_value("last-event-id") is not None
                fila = pagina.locator(f'#rev-lista tr[data-id="{reporte["id"]}"]')
                expect(fila).to_contain_text("Automático")
                fila.get_by_role("button", name="Aprobar", exact=True).click()
                expect(fila.get_by_role("textbox")).to_be_focused()
                fila.get_by_role("textbox").fill("Se validó la toma sintética.")
                with pagina.expect_response(lambda r: r.url.endswith(ruta)) as revision:
                    fila.get_by_role("button", name="Aprobar", exact=True).click()
                assert revision.value.status == 200
                assert len(comprobar(api.get(historia, headers=medico), 200)["reportes_ia"]) == 1

                comprobar(api.post(f"/pacientes/{pid}/imagenes", headers=medico, files={
                    "archivo": ("prueba.png", archivo.getvalue(), "image/png"),
                }), 201)
                registrar("59408-5", 89)
                pagina.locator("#btn-campana").click()
                panel = pagina.locator("#panel-notif")
                expect(panel).to_contain_text("Hay una nueva imagen clínica")
                expect(panel).to_contain_text("Valores para revisión prioritaria")
                expect(pagina.locator("#alertas-contenedor")).to_contain_text("sin reconocer")
                ajeno.locator("#btn-campana").click()
                expect(ajeno.locator("#panel-notif")).not_to_contain_text("Hay una nueva imagen clínica")
                expect(ajeno.locator("#alertas-contenedor")).not_to_contain_text("Valores para revisión prioritaria")
                navegador.close()
        finally:
            servidor.should_exit = True
            hilo.join(timeout=5)


def test_lote_descarta_pronostico_si_asiento_cierra(entorno, autorizaciones, monkeypatch):
    main, api, pg = entorno
    admin, medico = (autorizaciones[n] for n in ("auditor", "medico"))
    ficha = comprobar(paciente(api, admin), 201)
    remision = comprobar(api.post("/remisiones", headers=medico, json=datos_remision(ficha["id"])), 201)
    rid = remision["id"]
    visto = threading.Event()
    continuar = threading.Event()
    alias = main.ia.seudonimo(ficha["id"])

    def pronosticar(carga):
        if carga["id_seudonimo"] != alias:
            return None
        visto.set()
        assert continuar.wait(5)
        return {"probabilidad_efectiva": 0.15, "probabilidad_al_radicar": 0.15,
                "riesgo_estancamiento": False, "momento": "radicacion", "version_modelo": "obsoleto"}

    monkeypatch.setattr(main.registro, "_pedir_pronostico", pronosticar)
    with ThreadPoolExecutor(max_workers=1) as hilo:
        lote = hilo.submit(api.post, "/registro/ia/recalcular", headers=admin)
        try:
            assert visto.wait(5)
            comprobar(api.post(f"/registro/remisiones/{rid}/cierre", headers=medico,
                               json={"resultado": "rechazada", "nota": "Cierre simultáneo"}), 200)
        finally:
            continuar.set()
        assert comprobar(lote.result(timeout=12), 200)["cambiaron"] == 1
    with closing(psycopg2.connect(pg)) as db, db.cursor() as cur:
        cur.execute("SELECT 1 FROM pronosticos_remision WHERE remision_id = %s AND version_modelo = 'obsoleto';", (rid,))
        assert cur.fetchone() is None


def test_normativa_circulacion_bitacora(entorno, autorizaciones):
    from datetime import datetime, timezone

    _, api, _ = entorno
    medico, colega, admin = [autorizaciones[n] for n in ("medico", "colega", "auditor")]
    p = comprobar(paciente(api, medico), 201)
    rid = comprobar(api.post('/remisiones', headers=medico, json=datos_remision(p['id'])), 201)['id']
    gestion = comprobar(api.post('/gestiones-contacto', headers=medico, json={
        'remision_id': rid, 'contactos': [{'fecha': datetime.now(timezone.utc).isoformat(),
        'medio': 'telefono', 'institucion_contactada': LILI,
        'contactado_por': 'Operador sintético', 'respuesta': 'Sin cama, caso reservado'}]}), 201)
    ruta = '/gestiones-contacto/' + gestion['_id']
    obs = comprobar(api.post('/observaciones', headers=medico, json={
        'remision_id': rid, 'tipo': 'Pulso', 'valor': 70, 'unidad': 'lat/min'}), 201)
    assert obs['id'] not in {o['id'] for o in comprobar(api.get('/observaciones', headers=colega), 200)}
    comprobar(api.get(ruta, headers=colega), 404)
    for endpoint in (f'/remisiones/{rid}', f'/registro/remisiones/{rid}/formato',
                     f'/registro/remisiones/{rid}/bitacora.xlsx', f'/observaciones?remision_id={rid}'):
        comprobar(api.get(endpoint, headers=colega), 404)
    assert rid not in {r['id'] for r in comprobar(api.get('/remisiones', headers=colega), 200)}
    libro_ajeno = comprobar(api.get('/registro/libro?vista=todas', headers=colega), 200)
    assert rid not in {r['id'] for r in libro_ajeno['asientos']}
    comprobar(api.get('/gestiones-contacto', params={'remision_id': rid}, headers=colega), 404)
    assert gestion['_id'] not in {g['_id'] for g in comprobar(api.get('/gestiones-contacto', headers=colega), 200)}
    assert comprobar(api.get(ruta, headers=medico), 200)['contactos'] == gestion['contactos']
    comprobar(api.delete(ruta, headers=admin), 409)
    comprobar(api.delete(f'/remisiones/{rid}', headers=medico), 204)
    assert comprobar(api.get(ruta, headers=admin), 200)['contactos'] == gestion['contactos']
    comprobar(api.post(f'/remisiones/{rid}/restaurar', headers=admin), 200)
    assert comprobar(api.get(ruta, headers=medico), 200)['contactos'] == gestion['contactos']
    especialista = autenticado(api, 'especialista')
    eid = comprobar(api.get('/me', headers=especialista), 200)['id']
    comprobar(api.post('/remitir', headers=medico, json={
        'paciente_id': p['id'], 'especialista_id': eid, 'motivo': 'Continuidad del caso sintético'}), 201)
    assert comprobar(api.get(ruta, headers=especialista), 200)['contactos'] == gestion['contactos']
    assert rid in {r['id'] for r in comprobar(api.get('/remisiones', headers=especialista), 200)}
    assert rid in {r['id'] for r in comprobar(api.get('/registro/libro?vista=todas', headers=especialista), 200)['asientos']}


def test_normativa_documento_y_limites(entorno, autorizaciones, monkeypatch):
    import pacs
    main, api, pg = entorno
    monkeypatch.setattr(pacs, 'list_images', lambda _: [])
    h = autorizaciones['medico']
    p = comprobar(paciente(api, h, nombre='Nombre <script>reservado</script>'), 201)
    rid = comprobar(api.post('/remisiones', headers=h, json=datos_remision(p['id'])), 201)['id']
    base = f"/pacientes/{p['id']}"
    comprobar(api.post('/observaciones', headers=h, json={
        'remision_id': rid, 'tipo': 'Pulso', 'valor': 70, 'unidad': 'lat/min'}), 201)
    with closing(psycopg2.connect(pg)) as db, db, db.cursor() as cur:
        cur.execute("SELECT id FROM usuarios WHERE correo = 'medico@pruebas.test'")
        uid = cur.fetchone()[0]
        for estado, resultado in [('aprobado', 'Resultado <script>seguro</script>'), ('pendiente', 'NO_INCLUIR_BORRADOR'), ('rechazado', 'NO_INCLUIR_RECHAZADO')]:
            cur.execute("""INSERT INTO reportes_ia (paciente_id, remision_id, estado, prediccion,
                probabilidad_urgente, payload_seudonimizado, revisado_por, revisado_en, observacion_revision)
                VALUES (%s,%s,%s,%s,0.2,'{}',%s,now(),'Nota clínica aprobada')""",
                (p['id'], rid, estado, resultado, uid))
    for sufijo in ('/historia.fhir.json', '/preparacion-rda'):
        comprobar(api.get(base + sufijo), 401)
        comprobar(api.get(base + sufijo, headers=autorizaciones['colega']), 404)
        comprobar(api.get(base + sufijo, headers=autenticado(api, 'contable')), 403)
    respuesta = api.get(base + '/historia.fhir.json', headers=h)
    documento = comprobar(respuesta, 200)
    assert respuesta.headers['content-type'].startswith('application/fhir+json')
    assert respuesta.headers['cache-control'] == 'no-store'
    assert documento['type'] == 'document' and documento['identifier']['value'] and documento['timestamp']
    entradas = documento['entry']
    composicion = entradas[0]['resource']
    assert composicion['resourceType'] == 'Composition' and composicion['status'] == 'preliminary'
    assert 'profile' not in composicion.get('meta', {})
    assert 'seguridad' not in str(documento).lower()
    referencias = {e['fullUrl'] for e in entradas}
    def verificar(nodo):
        if isinstance(nodo, dict):
            if 'reference' in nodo:
                assert nodo['reference'] in referencias
            for v in nodo.values(): verificar(v)
        elif isinstance(nodo, list):
            for v in nodo: verificar(v)
    verificar(documento)
    assert any(e['resource']['resourceType'] == 'Encounter' for e in entradas)
    assert '<script>' not in str(composicion['section'])
    assert '&lt;script&gt;' in str(composicion['section'])
    assert 'NO_INCLUIR' not in str(documento)
    obs = next(e['resource'] for e in entradas if e['resource']['resourceType'] == 'Observation')
    assert obs['valueQuantity']['value'] == 70
    assert obs['valueQuantity']['code'] == '/min'
    assert rid in [int(e['resource']['id'].split('-')[-1]) for e in entradas if e['resource']['resourceType'] == 'Encounter']
    diagnostico = comprobar(api.get(base + '/preparacion-rda', headers=h), 200)
    assert diagnostico['apto_envio_nacional'] is False and diagnostico['brechas']
    assert p['documento'] not in str(diagnostico)
    rips = comprobar(api.get('/contabilidad/rips.json', headers=autenticado(api, 'contable')), 200)
    assert rips['validacion_oficial'] is False and rips['uso'] == 'demostracion_academica'
    assert rips['brechas']
    assert api.get('/me', headers=h).headers['cache-control'] == 'no-store'
