"""Recorrido API, navegador y HAPI en un laboratorio local con datos sintéticos.

Requiere Playwright, Chromium y un TrazaRed de prueba. Las claves de las cuentas
provienen de un archivo privado del laboratorio que recibe mediante FHIR_ENV_PRUEBAS.
"""
import hashlib
import hmac
import json
import os
from datetime import datetime
from pathlib import Path
import time
import uuid
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from dotenv import dotenv_values
from playwright.sync_api import expect, sync_playwright


BASE = os.getenv("FHIR_APP_PRUEBAS", "http://127.0.0.1:28080")
HAPI = os.getenv("FHIR_HAPI_PRUEBAS", "http://127.0.0.1:28082/fhir")
FED = "urn:salud-digital:sid-federado"
ORIGEN = "Hospital Universitario del Valle"
DESTINO = "Fundación Valle del Lili"
CUENTAS = {
    "ADMIN": "admin@trazared.huv",
    "MEDICO": "medico@huv.gov.co",
    "ESPECIALISTA": "especialista@huv.gov.co",
}


def dar_estado(resp, codigo):
    assert resp.status_code == codigo, (resp.request.method, urlsplit(resp.url).path, resp.status_code,
                                        resp.text[:350] if resp.status_code >= 400 else "")
    return resp.json()


def asegurar_local(url):
    partes = urlsplit(url)
    assert partes.hostname in ("127.0.0.1", "localhost") and partes.scheme == "http"


def esperar_recurso(tipo, sistema, identificador, condicion=lambda recurso: True, timeout=45):
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        r = requests.get(f"{HAPI}/{tipo}", params={"identifier": f"{sistema}|{identificador}",
                                                  "_total": "accurate"},
                         headers={"Cache-Control": "no-cache"}, timeout=6)
        if r.ok:
            recursos = [e["resource"] for e in r.json().get("entry", [])]
            assert len(recursos) <= 1, f"Se duplicó {tipo}"
            if recursos and condicion(recursos[0]):
                return recursos[0]
        time.sleep(0.8)
    raise AssertionError(f"HAPI no reflejó el estado esperado de {tipo}")


def sesion(rol, env):
    s = requests.Session()
    r = s.post(BASE + "/api/login", data={"username": CUENTAS[rol], "password": env[f"DEMO_{rol}_PASSWORD"]}, timeout=12)
    s.headers["Authorization"] = "Bearer " + dar_estado(r, 200)["access_token"]
    return s


def ficha(s, nombre, documento):
    return dar_estado(s.post(BASE + "/api/pacientes", json={
        "nombre": nombre, "documento": documento, "genero": "F", "eps": "Coosalud",
        "fecha_nacimiento": "1980-07-11",
    }, timeout=12), 201)


def federado(env, documento):
    return hmac.new(env["SECRETO_FEDERADO"].encode(), f"CC|{documento}".encode(), hashlib.sha256).hexdigest()


def referencia(tipo, sistema, valor):
    return {"type": tipo, "identifier": {"system": sistema, "value": valor}}


def observacion(fed, valor, unidad="mg/dL"):
    return {"resourceType": "Observation", "status": "final",
            "subject": referencia("Patient", FED, fed),
            "code": {"coding": [{"system": "http://loinc.org", "code": "2339-0"}]},
            "valueQuantity": {"value": valor, "system": "http://unitsofmeasure.org", "code": unidad}}


def login_browser(pagina, rol, env):
    pagina.goto(BASE)
    pagina.locator("#login-usuario").fill(CUENTAS[rol])
    pagina.locator("#login-clave").fill(env[f"DEMO_{rol}_PASSWORD"])
    pagina.get_by_role("button", name="Iniciar sesión").click()
    pagina.locator('#vista-app').wait_for(state="visible")


def bandeja(pagina, nombre):
    pagina.get_by_text("Interoperabilidad", exact=True).first.click()
    pagina.get_by_role("button", name=nombre, exact=True).click()


def main():
    asegurar_local(BASE)
    asegurar_local(HAPI)
    env = dotenv_values(os.environ["FHIR_ENV_PRUEBAS"])
    evidencia = Path(os.environ["FHIR_EVIDENCIA_PRUEBAS"]) if "FHIR_EVIDENCIA_PRUEBAS" in os.environ else None
    if evidencia:
        evidencia.mkdir(parents=True, exist_ok=True)
    estado_fhir = dar_estado(requests.get(f"{HAPI}/metadata", timeout=8), 200)
    assert estado_fhir["fhirVersion"] == "4.0.1"

    admin, medico = sesion("ADMIN", env), sesion("MEDICO", env)
    socio = dar_estado(admin.post(BASE + "/api/interop/socios", json={"nombre": "Socio sintético para conciliación"}, timeout=12), 201)
    cred = socio["credenciales_para_entregar"]
    scopes = "system/Observation.write system/Bundle.write"
    oauth = dar_estado(requests.post(BASE + "/api/oauth/token", data={
        "grant_type": "client_credentials", "client_id": cred["client_id"],
        "client_secret": cred["client_secret"], "scope": scopes,
    }, timeout=12), 200)

    def socio_envia(recurso, codigo, mensaje_id=None):
        tipo = recurso["resourceType"]
        cuerpo = json.dumps(recurso).encode()
        instante = str(int(time.time()))
        mensaje_id = mensaje_id or str(uuid.uuid4())
        firmado = f"{instante}.{mensaje_id}.".encode() + cuerpo
        firma = hmac.new(cred["secreto_firma"].encode(), firmado, hashlib.sha256).hexdigest()
        resp = requests.post(BASE + f"/api/interop/{tipo}", data=cuerpo, headers={
            "Authorization": f"Bearer {oauth['access_token']}", "Content-Type": "application/fhir+json",
            "X-Timestamp": instante, "X-Message-Id": mensaje_id, "X-Signature": f"v1={firma}",
        }, timeout=12)
        return dar_estado(resp, codigo)

    doc_entrada = str(9500000000 + uuid.uuid4().int % 1000000)
    doc_flujo = str(int(doc_entrada) + 1000000)
    local = ficha(admin, "Paciente sintético de retorno", doc_flujo)
    federado_entrada = federado(env, doc_entrada)
    med_otro = observacion(federado(env, doc_flujo), 112)
    mensaje = str(uuid.uuid4())
    socio_envia(med_otro, 201, mensaje)
    socio_envia(med_otro, 409, mensaje)
    historia = dar_estado(admin.get(BASE + f"/api/pacientes/{local['id']}/historia", timeout=10), 200)
    assert any(float(o["valor"]) == 112 and o["remision_id"] is None for o in historia["observaciones"])
    socio_envia(observacion(federado_entrada, 156), 202)
    socio_envia(observacion(federado_entrada, 190, "Cel"), 422)
    episodio_inexistente = {**med_otro, "basedOn": [
        referencia("ServiceRequest", "urn:trazared:remision", "REM-INEXISTENTE")]}
    socio_envia(episodio_inexistente, 422)
    referencia_ext = "EXT-" + uuid.uuid4().hex[:12]
    url_pac, url_srv = ["urn:uuid:" + str(uuid.uuid4()) for _ in range(2)]
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"fullUrl": url_pac, "resource": {"resourceType": "Patient",
            "identifier": [{"system": FED, "value": federado_entrada}]}},
        {"fullUrl": url_srv, "resource": {"resourceType": "ServiceRequest", "status": "active", "intent": "order",
            "identifier": [{"system": "urn:trazared:remision", "value": referencia_ext}],
            "subject": {"reference": url_pac},
            "code": {"coding": [{"system": "http://snomed.info/sct", "code": "3457005"}]},
            "reasonCode": [{"text": "Solicitud sintética de conciliación FHIR"}]}},
    ]}
    recibida = socio_envia(bundle, 202)["remision"]["id"]
    socio_envia(bundle, 409)
    prospecto = next(p for p in dar_estado(admin.get(BASE + "/api/interop/conciliaciones", timeout=8), 200)
                     if p["nombre"].endswith(federado_entrada[:8]))
    assert prospecto["remisiones"] == 1 and prospecto["observaciones"] == 1
    local_entrada = ficha(admin, "Paciente sintético conciliado", doc_entrada)
    dar_estado(medico.post(BASE + f"/api/interop/conciliaciones/{prospecto['id']}",
                           json={"documento": doc_entrada}, timeout=8), 403)
    print("API firmada: 201 persistido sin remisión, 202 reutiliza provisional, 409 duplicado y 422 UCUM o episodio inválidos; conciliación exige rol.")

    socio_loop = dar_estado(admin.post(BASE + "/api/interop/socios/prueba", timeout=10), 201)
    caso = dar_estado(medico.post(BASE + "/api/remisiones", json={
        "paciente_id": local["id"], "institucion_origen": ORIGEN, "institucion_destino": DESTINO,
        "fecha_solicitud": datetime.now(ZoneInfo("America/Bogota")).date().isoformat(),
        "convenio_vigente": True,
        "motivo": f"Traslado sintético por control. {local['nombre']}. Documento {doc_flujo}.",
    }, timeout=12), 201)
    rid = caso["id"]
    lectura = dar_estado(medico.post(BASE + "/api/observaciones", json={
        "remision_id": rid, "tipo": "Temperatura", "codigo_loinc": "8310-5", "unidad": "Cel", "valor": 37.2,
    }, timeout=8), 201)
    dar_estado(medico.post(BASE + f"/api/interop/remisiones/{rid}/enviar", json={"socio_id": socio_loop["id"]}, timeout=10), 403)
    dar_estado(admin.post(BASE + f"/api/pacientes/{local['id']}/consentimientos", json={
        "socio_id": socio_loop["id"], "medio": "escrito"}, timeout=10), 201)
    previa = dar_estado(medico.get(BASE + f"/api/interop/remisiones/{rid}/vista-previa",
                                   params={"socio_id": socio_loop["id"]}, timeout=10), 200)
    assert previa["bundle"]["type"] == "collection"
    copia = json.dumps(previa["bundle"], ensure_ascii=False)
    assert all(s not in copia for s in (local["nombre"], local["documento"], local["fecha_nacimiento"]))
    tipos = {e["resource"]["resourceType"] for e in previa["bundle"]["entry"]}
    assert {"Encounter", "Observation"} <= tipos

    with sync_playwright() as p:
        navegador = p.chromium.launch()
        pag_admin, pag_med, pag_esp = (navegador.new_page(viewport={"width": 1440, "height": 1000}) for _ in range(3))
        errores = []
        for pag in (pag_admin, pag_med, pag_esp):
            pag.on("pageerror", lambda exc: errores.append(exc.stack))

        login_browser(pag_admin, "ADMIN", env)
        bandeja(pag_admin, "Por conciliar")
        tarjeta = pag_admin.locator("article").filter(has_text=federado_entrada[:8])
        tarjeta.get_by_role("button", name="Conciliar paciente").click()
        doc = pag_admin.get_by_label("Documento de la ficha local")
        doc.fill(doc_flujo)
        pag_admin.get_by_role("button", name="Confirmar coincidencia").click()
        expect(pag_admin.get_by_text("El documento no coincide con el identificador federado recibido")).to_be_visible()
        doc.fill(doc_entrada)
        pag_admin.get_by_role("button", name="Confirmar coincidencia").click()
        expect(tarjeta).to_have_count(0)
        bandeja(pag_admin, "Recibidas")
        expect(pag_admin.locator("article").filter(has_text=referencia_ext)).to_contain_text(local_entrada["nombre"])
        if evidencia:
            pag_admin.screenshot(path=evidencia / "conciliacion.png")
        r = dar_estado(admin.get(BASE + f"/api/pacientes/{local_entrada['id']}/historia", timeout=10), 200)
        assert any(float(o["valor"]) == 156 and o["remision_id"] is None for o in r["observaciones"])
        assert next(x for x in dar_estado(admin.get(BASE + "/api/interop/recibidas", timeout=8), 200)
                    if x["id"] == recibida)["paciente_id"] == local_entrada["id"]
        print("Chromium: conciliación rechaza documento ajeno, confirma coincidencia y reasigna remisión e historia.")

        login_browser(pag_med, "MEDICO", env)
        fila = pag_med.locator(f'tr[data-id="{rid}"]')
        fila.wait_for()
        fila.click()
        pag_med.get_by_role("button", name="Remitir a institución socia").click()
        modal = pag_med.get_by_role("dialog", name="Remitir a institución socia")
        modal.locator("#itp-socio").select_option(str(socio_loop["id"]))
        expect(modal).to_contain_text("identificador federado del paciente")
        modal.get_by_role("button", name="Enviar remisión (FHIR)").click()
        expect(modal).to_have_count(0, timeout=15000)
        bandeja(pag_med, "Enviadas")
        enviada = pag_med.locator(f'tr[data-ver="{rid}"]')
        expect(enviada.get_by_text("recibida y asociada")).to_be_visible()
        login_browser(pag_esp, "ESPECIALISTA", env)
        bandeja(pag_esp, "Recibidas")
        tarjeta = pag_esp.locator("article").filter(has_text=previa["radicado"])
        tarjeta.get_by_role("button", name="Aceptar", exact=True).click()
        expect(tarjeta).to_contain_text("Respuesta entregada")
        expect(enviada.get_by_text("Esperando respuesta")).to_have_count(0, timeout=12000)
        tarjeta.get_by_role("button", name="Devolver resultado").click()
        pag_esp.get_by_role("button", name="Registrar signo vital").click()
        pag_esp.get_by_label("Signo vital (código LOINC)").select_option("8310-5")
        pag_esp.get_by_label("Valor", exact=True).fill("37.7")
        pag_esp.get_by_role("button", name="Registrar", exact=True).click()
        modal = pag_esp.get_by_role("dialog", name="Devolver resultado")
        modal.get_by_role("button", name="Enviar resultado FHIR").click()
        expect(modal).to_contain_text("Enviado y asociado en origen")
        expect(enviada.get_by_text("Resultados recibidos: 1")).to_be_visible(timeout=12000)
        if evidencia:
            pag_med.screenshot(path=evidencia / "resultado-en-origen.png")
        pag_med.reload()
        bandeja(pag_med, "Enviadas")
        expect(enviada.get_by_text("Resultados recibidos: 1")).to_be_visible()
        assert not errores, errores
        print("Chromium: envío, Task, registro y retorno de signo vital. El origen se actualizó sin recargar y conservó el resultado.")

    paciente = esperar_recurso("Patient", "urn:trazared:documento", doc_flujo)
    episodio = esperar_recurso("Encounter", "urn:trazared:replica", rid, lambda r: r["status"] == "in-progress")
    obs = esperar_recurso("Observation", "urn:trazared:replica", lectura["id"])
    assert paciente["birthDate"] == "1980-07-11"
    assert obs["subject"]["reference"] == f"Patient/{paciente['id']}"
    assert obs["encounter"]["reference"] == f"Encounter/{episodio['id']}"
    assert episodio["class"]["code"] == "remision"
    assert obs["valueQuantity"]["code"] == "Cel" and obs["performer"]
    corregido = "Paciente sintético de retorno corregido"
    dar_estado(admin.patch(BASE + f"/api/pacientes/{local['id']}", json={"nombre": corregido}, timeout=10), 200)
    actualizado = esperar_recurso("Patient", "urn:trazared:documento", doc_flujo, lambda r: r["name"][0]["text"] == corregido)
    assert actualizado["id"] == paciente["id"]
    assert dar_estado(admin.get(BASE + "/api/auditoria/verificar", timeout=10), 200)["integra"]
    print("HAPI R4: Patient, Encounter y Observation con referencias; corrección conserva el mismo ID y auditoría íntegra.")


if __name__ == "__main__":
    main()
