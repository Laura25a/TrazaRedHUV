"""Escenarios HTTP sintéticos compartidos por tester y pytest; no imprimen cuerpos."""
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import time
from uuid import uuid4
from zoneinfo import ZoneInfo


class FalloEscenario(Exception):
    """El paso ya quedó registrado sin exponer la respuesta clínica."""


class Escenario:
    def __init__(self, peticion, guardar, requisito, nombre):
        self.peticion, self.guardar = peticion, guardar
        self.requisito, self.nombre = requisito, nombre
        self.turno = uuid4().hex

    def verificar(self, paso, condicion, codigo=None):
        self.guardar(self.requisito, f"{self.nombre}.{paso}",
                     "pasa" if condicion else "falla", paso.replace("_", " "), codigo)
        if not condicion:
            raise FalloEscenario(paso)

    def pedir(self, paso, metodo, ruta, codigo, rol="admin", **opciones):
        r = self.peticion(metodo, ruta, rol=rol, **opciones)
        self.verificar(paso, r.status_code == codigo, r.status_code)
        return r.json() if r.content else None

    def paciente(self, documento=None):
        return {"nombre": "Ficha sintética de verificación", "documento": documento or self.turno[:18],
                "genero": "F", "eps": "EPS de prueba", "fecha_nacimiento": "2000-01-01"}

    def remision(self, pid):
        return {"paciente_id": pid, "institucion_origen": "Hospital Universitario del Valle",
                "institucion_destino": "Fundación Valle del Lili",
                "fecha_solicitud": datetime.now(ZoneInfo("America/Bogota")).date().isoformat(),
                "motivo": "Verificación sintética de integridad", "convenio_vigente": False}


def fechas(e):
    hoy = datetime.now(ZoneInfo("America/Bogota")).date()
    ficha = e.paciente()
    e.pedir("nacimiento_futuro_422", "POST", "/pacientes", 422,
            json={**ficha, "fecha_nacimiento": (hoy + timedelta(days=1)).isoformat()})
    encontrados = e.pedir("buscar_ficha_rechazada", "GET", "/pacientes", 200,
                          params={"q": ficha["documento"]})
    e.verificar("ficha_invalida_no_persistida", not encontrados)
    p = e.pedir("crear_ficha_valida", "POST", "/pacientes", 201, json=ficha)
    rem = e.remision(p["id"])
    antes = e.pedir("remisiones_antes", "GET", "/remisiones", 200)
    e.pedir("solicitud_anterior_nacimiento_422", "POST", "/remisiones", 422,
            json={**rem, "fecha_solicitud": "1999-12-31"})
    e.pedir("solicitud_futura_422", "POST", "/remisiones", 422,
            json={**rem, "fecha_solicitud": (hoy + timedelta(days=1)).isoformat()})
    despues = e.pedir("remisiones_despues", "GET", "/remisiones", 200)
    e.verificar("remisiones_invalidas_no_persistidas",
                {r["id"] for r in antes if r["paciente_id"] == p["id"]} ==
                {r["id"] for r in despues if r["paciente_id"] == p["id"]})


def huerfanos(e):
    rid = 2147483000
    e.pedir("remision_inexistente_404", "GET", f"/remisiones/{rid}", 404)
    antes = e.pedir("bitacoras_antes", "GET", "/gestiones-contacto", 200)
    contacto = {"fecha": datetime.now().astimezone().isoformat(), "medio": "telefono",
                "institucion_contactada": "Fundación Valle del Lili", "respuesta": "Sin respuesta",
                "contactado_por": "Operador sintético"}
    e.pedir("post_legado_huerfano_404", "POST", "/gestiones-contacto", 404,
            json={"remision_id": rid, "contactos": [contacto]})
    e.pedir("post_registro_huerfano_404", "POST", f"/registro/remisiones/{rid}/gestiones", 404,
            json={"institucion_contactada": contacto["institucion_contactada"], "respuesta": "Sin respuesta"})
    despues = e.pedir("bitacoras_despues", "GET", "/gestiones-contacto", 200)
    e.verificar("mongo_no_crea_huerfanos", not any(r["remision_id"] == rid for r in antes + despues))


def preparar_conciliacion(e, secreto_federado):
    """Solo socio nuevo sintético, sin URL externa ni rotación de socios existentes."""
    socio = e.pedir("crear_socio_aislado", "POST", "/interop/socios", 201,
                    json={"nombre": "Socio sintético " + e.turno[:12]})
    cred = socio["credenciales_para_entregar"]
    token = e.pedir("token_sistema", "POST", "/oauth/token", 200, rol=None, data={
        "grant_type": "client_credentials", "client_id": cred["client_id"],
        "client_secret": cred["client_secret"], "scope": "system/Observation.write"})["access_token"]
    documento = e.turno[:18]
    # El contrato normaliza documentos a mayúsculas antes de calcular el federado.
    federado = hmac.new(secreto_federado.encode(), f"CC|{documento.upper()}".encode(), hashlib.sha256).hexdigest()
    recurso = {"resourceType": "Observation", "status": "final",
               "subject": {"identifier": {"system": "urn:salud-digital:sid-federado", "value": federado}},
               "code": {"coding": [{"system": "http://loinc.org", "code": "2339-0"}]},
               "valueQuantity": {"value": 123, "system": "http://unitsofmeasure.org", "code": "mg/dL"}}
    cuerpo = json.dumps(recurso).encode()
    sello, mensaje = str(int(time.time())), str(uuid4())
    firma = hmac.new(cred["secreto_firma"].encode(), f"{sello}.{mensaje}.".encode() + cuerpo, hashlib.sha256).hexdigest()
    pendientes = e.pedir("pendientes_antes", "GET", "/interop/conciliaciones", 200)
    headers = {"Authorization": f"Bearer {token}", "X-Timestamp": sello,
               "X-Message-Id": mensaje, "X-Signature": "v1=" + firma, "Content-Type": "application/fhir+json"}
    resultado = e.pedir("observacion_desconocida_202", "POST", "/interop/Observation", 202,
                        rol=None, headers=headers, data=cuerpo)
    e.verificar("respuesta_operationoutcome", resultado.get("resourceType") == "OperationOutcome")
    e.pedir("mensaje_repetido_409", "POST", "/interop/Observation", 409,
            rol=None, headers=headers, data=cuerpo)
    nuevos = e.pedir("pendientes_202", "GET", "/interop/conciliaciones", 200)
    nuevos = [p for p in nuevos if p["id"] not in {p["id"] for p in pendientes}
              and p["nombre"] == f"Paciente por conciliar {federado[:8]}"]
    e.verificar("un_pendiente_con_observacion", len(nuevos) == 1 and nuevos[0]["observaciones"] == 1)
    p = e.pedir("registrar_identidad_local", "POST", "/pacientes", 201, json=e.paciente(documento))
    return nuevos[0]["id"], p


def conciliacion(e, secreto_federado):
    provisional, p = preparar_conciliacion(e, secreto_federado)
    ajeno = e.pedir("crear_identidad_distinta", "POST", "/pacientes", 201,
                   json=e.paciente("otra-" + e.turno[:16]))
    ruta = f"/interop/conciliaciones/{provisional}"
    e.pedir("documento_incompatible_422", "POST", ruta, 422, json={"documento": ajeno["documento"]})
    e.pedir("conciliacion_sin_rol_403", "POST", ruta, 403, rol="medico", json={"documento": p["documento"]})
    r = e.pedir("conciliar_200", "POST", ruta, 200, json={"documento": p["documento"]})
    e.verificar("destino_local_y_una_observacion", r["paciente_id"] == p["id"] and len(r["observaciones"]) == 1)
    repetido = e.pedir("conciliar_idempotente_200", "POST", ruta, 200, json={"documento": p["documento"]})
    e.verificar("repeticion_sin_duplicados", repetido.get("ya_conciliado") and repetido["paciente_id"] == p["id"])
    pendientes = e.pedir("pendientes_finales", "GET", "/interop/conciliaciones", 200)
    e.verificar("pendiente_resuelto", provisional not in {p["id"] for p in pendientes})
    historia = e.pedir("historia_local", "GET", f"/pacientes/{p['id']}/historia", 200)
    obs = [o for o in historia["observaciones"] if o["id"] in r["observaciones"]]
    e.verificar("observacion_conservada_en_historia", len(obs) == 1 and float(obs[0]["valor"]) == 123
                and obs[0]["codigo_loinc"] == "2339-0" and obs[0]["unidad"] == "mg/dL")


def ejecutar_escenario(nombre, peticion, guardar, secreto_federado=None):
    e = Escenario(peticion, guardar, "L04" if nombre != "conciliacion_202" else "R30", nombre)
    if nombre == "conciliacion_202" and not secreto_federado:
        guardar(e.requisito, nombre, "pendiente", "Falta secreto federado de prueba", None)
        return
    acciones = {"fechas_imposibles": lambda: fechas(e), "gestiones_huerfanas": lambda: huerfanos(e),
                "conciliacion_202": lambda: conciliacion(e, secreto_federado)}
    try:
        acciones[nombre]()
    except FalloEscenario:
        return
