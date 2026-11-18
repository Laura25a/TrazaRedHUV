"""Ejecuta las rutas de tester.yaml sobre un entorno de demostración autorizado."""

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from secrets import token_urlsafe
import sys
import subprocess
import time
from urllib.parse import quote, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
import requests
import yaml

from escenarios_integridad import ejecutar_escenario


RAIZ = Path(__file__).resolve().parents[1]
CUERPOS_LIBRO = {
    "registro_gestiones": {
        "institucion_contactada": "Fundación Valle del Lili",
        "respuesta": "La institución rechaza por falta de cama",
    },
    "registro_cierre": {"resultado": "rechazada", "nota": "Verificación del tester"},
    "registro_ia_clasificar": {"texto": "Rechazan la remisión por falta de cama"},
}
REQUISITOS = {
    "ia_predecir": "R22", "modelo": "R22", "entrenar": "R22",
    "pendientes": "R18", "ia_simular": "R21",
}


def sustituir(valor, datos):
    if isinstance(valor, dict):
        return {k: sustituir(v, datos) for k, v in valor.items()}
    if isinstance(valor, list):
        return [sustituir(v, datos) for v in valor]
    if not isinstance(valor, str):
        return valor
    entero = re.fullmatch(r"\{(\w+)\}", valor)
    if entero and entero[1] in datos:
        return datos[entero[1]]
    return re.sub(r"\{(\w+)\}", lambda m: str(datos.get(m[1], m[0])), valor)


def requisitos(texto):
    actual = "CONTRATO"
    salida = {}
    for linea in texto.splitlines():
        encabezado = re.match(r"  # ─+ (R\d+)", linea)
        if encabezado:
            actual = encabezado[1]
        elif linea.startswith("  # ─") and "Libro de radicación" in linea:
            actual = "LIBRO"
        ruta = re.match(r"  (\w+):(?:\s+#.*)?$", linea)
        if ruta:
            salida[ruta[1]] = REQUISITOS.get(ruta[1], actual)
    return salida


def secreto(valor, entorno):
    match = re.fullmatch(r"\$\{(\w+)\}", valor)
    return entorno.get(match[1]) if match else valor


class Verificador:
    def __init__(self, url, contrato, entorno):
        self.url = url
        self.contrato = contrato
        self.entorno = entorno
        self.http = requests.Session()
        self.tokens = {}
        self.resultados = []
        self.datos = {**contrato["datos"], **contrato["datos_final"]}
        self.ids = {}
        self.socio_id = None
        self.firma = None
        self.turno = uuid4().hex[:12]
        self.datos["fhir_patient_id"] = entorno.get("TRAZARED_TEST_FHIR_PATIENT_ID") or self.datos["paciente_id_fhir"]
        self.datos["webhook_fhir"] = secreto(contrato["secretos"]["webhook_fhir"], entorno)

    def peticion(self, metodo, ruta, *, api=True, rol=None, headers=None, **opciones):
        cabeceras = dict(headers or {})
        if rol:
            cabeceras["Authorization"] = f"Bearer {self.tokens[rol]}"
        return self.http.request(metodo, self.url + ("/api" if api else "") + ruta,
                                 headers=cabeceras, timeout=opciones.pop("timeout", 35),
                                 allow_redirects=False, **opciones)

    def guardar(self, requisito, caso, estado, detalle, codigo=None):
        self.resultados.append(dict(requisito=requisito, caso=caso, estado=estado,
                                    detalle=detalle, http=codigo,
                                    comprobacion="contrato_basico"))

    def comprobar_integridad(self):
        for nombre, caso in self.contrato.get("escenarios_integridad", {}).items():
            faltan = set(caso["rol_requerido"]) - self.tokens.keys()
            if faltan:
                self.guardar(caso["requisito"], nombre, "pendiente", "Falta autenticación de prueba")
                continue
            def guardar(*args):
                self.guardar(*args)
                self.resultados[-1]["comprobacion"] = "flujo_integridad"
            try:
                ejecutar_escenario(nombre, self.peticion, guardar,
                    secreto(self.contrato["secretos"]["secreto_federado"], self.entorno))
            except (requests.RequestException, ValueError, KeyError) as error:
                guardar(caso["requisito"], nombre, "falla", type(error).__name__)

    def preparar(self):
        for rol, cuenta in self.contrato["usuarios"].items():
            clave = secreto(cuenta["password"], self.entorno) or self.entorno.get(f"DEMO_{rol.upper()}_PASSWORD")
            if not clave:
                self.guardar("R01", f"login_{rol}", "pendiente", "Credencial ausente")
                continue
            try:
                r = self.peticion("POST", "/login", data={"username": cuenta["usuario"], "password": clave})
                if r.ok:
                    self.tokens[rol] = r.json()["access_token"]
                self.guardar("R01", f"login_{rol}", "pasa" if r.ok else "falla", "Inicio de sesión", r.status_code)
            except (requests.RequestException, ValueError, KeyError) as error:
                self.guardar("R01", f"login_{rol}", "falla", type(error).__name__)
        if "admin" not in self.tokens:
            return
        self.probar_bloqueo()
        if "medico" in self.tokens:
            pacientes = self.peticion("GET", "/pacientes", rol="medico",
                                      params={"q": self.datos["documento_paciente"]})
            if pacientes.ok and len(pacientes.json()) == 1:
                paciente_id = pacientes.json()[0]["id"]
                self.datos.update(paciente_id_prueba=paciente_id, canario_paciente_id=paciente_id)
            r = self.peticion("GET", "/especialistas", rol="medico")
            if r.ok and r.json():
                self.datos["especialista_id"] = r.json()[0]["id"]
            r = self.peticion("POST", "/remisiones", rol="medico", json={
                "paciente_id": self.datos["paciente_id_prueba"],
                "institucion_origen": "Hospital Universitario del Valle",
                "institucion_destino": "Fundación Valle del Lili",
                "fecha_solicitud": datetime.now(ZoneInfo("America/Bogota")).date().isoformat(),
                "motivo": "Asiento de verificación del tester",
                "convenio_vigente": True,
            })
            if r.ok:
                self.ids["libro"] = r.json()["id"]
            self.guardar("LIBRO", "preparar_asiento", "pasa" if r.ok else "pendiente",
                        "Radicar asiento de prueba", r.status_code)
        r = self.peticion("POST", "/interop/socios/prueba", rol="admin")
        if r.ok:
            self.socio_id = r.json()["id"]
            r = self.peticion("POST", f"/interop/socios/{self.socio_id}/rotar", rol="admin")
            if r.ok:
                credenciales = r.json()["credenciales_para_entregar"]
                self.datos["socio_client_id"] = credenciales["client_id"]
                self.datos["socio_client_secret"] = credenciales["client_secret"]
                self.firma = credenciales["secreto_firma"]

    def probar_bloqueo(self):
        correo = f"tester-{self.turno[:8]}@example.test"
        clave = "Az7!" + token_urlsafe(20)
        r = self.peticion("POST", "/usuarios", rol="admin", json={
            "nombre": "Cuenta de prueba de bloqueo", "correo": correo,
            "contrasena": clave, "rol": "contable",
        })
        if not r.ok:
            self.guardar("R02", "bloqueo_tercer_intento", "pendiente", "Alta de cuenta temporal", r.status_code)
            return
        self.ids["desbloquear_usuario"] = r.json()["id"]
        codigos = [self.peticion("POST", "/login", data={
            "username": correo, "password": "incorrecta-para-esta-cuenta",
        }).status_code for _ in range(3)]
        bloqueado = self.peticion("POST", "/login", data={"username": correo, "password": clave}).status_code
        pasa = codigos == [401, 401, 423] and bloqueado == 423
        self.guardar("R02", "bloqueo_tercer_intento", "pasa" if pasa else "falla",
                    f"Intentos fallidos {codigos}, acceso posterior {bloqueado}", bloqueado)

    def comprobar_acceso(self):
        for rol, codigo in ((None, 401), ("medico", 403)):
            if rol and rol not in self.tokens:
                estado, observado = "pendiente", None
            else:
                observado = self.peticion("GET", "/auditoria", rol=rol).status_code
                estado = "pasa" if observado == codigo else "falla"
            self.guardar("R03", f"auditoria_{rol or 'sin_sesion'}", estado, f"Espera {codigo}", observado)

        for rol in self.tokens:
            r = self.peticion("GET", "/me", rol=rol)
            pasa = r.ok and r.json().get("rol") == rol
            self.guardar("R01", f"yo_{rol}", "pasa" if pasa else "falla", "Identidad y rol", r.status_code)
        if "paciente" in self.tokens:
            r = self.peticion("GET", "/registro/mi-seguimiento", rol="paciente")
            pasa = r.ok and isinstance(r.json(), list)
            self.guardar("LIBRO", "mi_seguimiento_paciente", "pasa" if pasa else "falla",
                        "El paciente consulta su seguimiento", r.status_code)

    def firmado(self, ruta, scope):
        if not self.firma:
            return "pendiente", "Falta el socio de bucle local", None
        token = self.peticion("POST", "/oauth/token", data={
            "client_id": self.datos["socio_client_id"], "client_secret": self.datos["socio_client_secret"],
            "grant_type": "client_credentials", "scope": scope,
        })
        if not token.ok:
            return "falla", "Emisión de token para el alcance FHIR", token.status_code
        cuerpo = b"{}"
        sello, mensaje = str(int(time.time())), str(uuid4())
        firma = hmac.new(self.firma.encode(), f"{sello}.{mensaje}.".encode() + cuerpo, hashlib.sha256)
        r = self.peticion("POST", ruta, data=cuerpo, headers={
            "Content-Type": "application/fhir+json",
            "Authorization": f"Bearer {token.json()['access_token']}",
            "X-Timestamp": sello, "X-Message-Id": mensaje, "X-Signature": f"v1={firma.hexdigest()}",
        })
        pasa = r.status_code in (400, 422) and r.json().get("resourceType") == "OperationOutcome"
        return ("pasa" if pasa else "falla"), "FHIR firmado con recurso inválido y OperationOutcome", r.status_code

    def id_para(self, nombre):
        if nombre.startswith("registro_") or nombre.startswith("interop_remision"):
            return self.ids.get("libro")
        if nombre == "ia_rechazar":
            return self.ids.get("reporte_rechazar")
        if nombre.startswith("ia_"):
            return self.ids.get("reporte")
        if nombre in {"historia", "historia_pdf", "imagenes_paciente", "consentimientos", "consentimiento_registrar"}:
            return self.datos["paciente_id_prueba"]
        if nombre in {"interop_enviar", "interop_vista_previa"}:
            return self.ids.get("libro")
        return self.ids.get(nombre)

    def actualizar(self, nombre, datos):
        if not isinstance(datos, (dict, list)):
            return
        if isinstance(datos, dict):
            destinos = {
                "remitir": "remision_aceptar", "ia_analizar": "reporte",
                "consentimiento_registrar": "consentimiento_revocar",
            }
            if nombre in destinos and "id" in datos:
                self.ids[destinos[nombre]] = datos["id"]
            if nombre == "ia_aprobar" and "medico" in self.tokens:
                cuerpo = sustituir(self.contrato["rutas"]["ia_analizar"]["body"], self.datos)
                r = self.peticion("POST", "/ia/analizar", rol="medico", json=cuerpo)
                if r.ok:
                    self.ids["reporte_rechazar"] = r.json()["id"]
            if nombre == "agente_chat" and datos.get("invocaciones"):
                self.ids["agente_payload"] = datos["invocaciones"][0]
            if nombre == "token_sistema" and datos.get("access_token"):
                self.tokens["sistema"] = datos["access_token"]
            if nombre == "fhir_paciente_sync" and datos.get("id") and not self.entorno.get("TRAZARED_TEST_FHIR_PATIENT_ID"):
                self.datos["fhir_patient_id"] = datos["id"]
        if isinstance(datos, list) and datos:
            destino = {"notificaciones": "notif_leer", "alertas": "alerta_reconocer",
                       "bloqueos": "bloqueo_levantar", "interop_recibidas": "interop_responder"}.get(nombre)
            if destino:
                self.ids[destino] = datos[0].get("id")

    def ejecutar(self, nombre, caso):
        rol = next((r for r in caso.get("rol_requerido", []) if r in self.tokens), None)
        if caso.get("rol_requerido") and rol is None:
            return "pendiente", "Falta autenticación del rol", None
        ruta = caso.get("path", "/fhir" + caso.get("path_fhir", ""))
        api = "path_fhir" not in caso and nombre != "app_raiz"
        valores = {**self.datos, "id": self.id_para(nombre)}
        if "{id}" in ruta and valores["id"] is None:
            return "pendiente", "Falta el identificador que produce un caso previo", None
        ruta = sustituir(ruta, {k: quote(str(v), safe="") for k, v in valores.items()})
        cuerpo = sustituir(caso.get("body", CUERPOS_LIBRO.get(nombre)), valores)
        if nombre == "login":
            pasa = all(r in self.tokens for r in self.contrato["usuarios"])
            return ("pasa" if pasa else "falla"), "Ver casos de inicio de sesión por rol", None
        if nombre == "ia_predecir":
            cuerpo = dict(self.contrato["rutas"]["ia_analizar"]["body"])
            cuerpo.pop("paciente_id")
        if nombre == "remitir":
            cuerpo["especialista_id"] = self.datos["especialista_id"]
        if nombre in {"agente_chat", "agente_inyeccion"}:
            cuerpo["conversacion_id"] += "-" + self.turno
        if nombre == "token_sistema_en_api_clinica":
            if "sistema" not in self.tokens:
                return "pendiente", "La emisión del token de sistema debe pasar primero", None
            rol = "sistema"
        if "scope" in caso:
            return self.firmado(ruta, caso["scope"])
        if nombre in {"token_sistema", "interop_enviar", "interop_vista_previa", "consentimiento_registrar"}:
            if not self.socio_id or not self.firma:
                return "pendiente", "Falta el socio de bucle local", None
            if cuerpo and "socio_id" in cuerpo:
                cuerpo["socio_id"] = self.socio_id
            if nombre == "interop_vista_previa":
                ruta = ruta.replace("socio_id=1", f"socio_id={self.socio_id}")
        query = sustituir(caso.get("query"), valores)
        headers = sustituir(caso.get("headers", {}), valores)
        if nombre == "fhir_receptor" and not headers.get("X-Webhook-Secret"):
            return "pendiente", "Secreto del webhook ausente", None
        if nombre == "fhir_receptor":
            cuerpo["id"] = "tester-" + self.turno
        if not api and "path_fhir" in caso:
            headers["Content-Type"] = "application/fhir+json"
        formato = "data" if caso.get("content_type") == "application/x-www-form-urlencoded" else "json"
        flujo = caso.get("tipo") == "sse"
        if flujo:
            headers["Accept"] = "text/event-stream"
        espera = 185 if nombre in {"entrenar", "registro_ia_entrenar"} else caso.get("timeout_segundos", 35)
        with self.peticion(caso["metodo"], ruta, api=api, rol=rol, headers=headers,
                           params=query, stream=flujo, timeout=espera, **{formato: cuerpo}) as r:
            codigo = r.status_code
            if nombre == "app_raiz" and codigo == 308 and r.headers.get("Location") == "/":
                portada = self.peticion("GET", "/", api=False)
                pasa = portada.ok and "text/html" in portada.headers.get("content-type", "")
                return ("pasa" if pasa else "falla"), "Redirección a la portada en el mismo dominio", portada.status_code
            esperado = caso.get("espera_codigo")
            if (codigo != esperado if esperado is not None else not 200 <= codigo < 300):
                return "falla", f"Estado HTTP inesperado{f', se esperaba {esperado}' if esperado else ''}", codigo
            if esperado is not None:
                return "pasa", "Estado HTTP esperado", codigo
            tipo = r.headers.get("content-type", "")
            tipo_esperado = caso.get("content_type", "")
            if tipo_esperado and formato != "data" and tipo_esperado not in tipo:
                return "falla", "Tipo de contenido distinto al contrato", codigo
            if flujo:
                primera = next(r.iter_lines(chunk_size=1), b"")
                return ("pasa" if primera else "falla"), "Handshake SSE y primera línea", codigo
            datos = r.json() if "json" in tipo else None
            self.actualizar(nombre, datos)
            if nombre == "agente_payload":
                canarios = sustituir(caso.get("canarios_ausentes", []), valores)
                if any(str(canario) in json.dumps(datos.get("payload"), ensure_ascii=False) for canario in canarios):
                    return "falla", "El payload contiene un canario personal", codigo
            if nombre == "interop_enviar" and datos.get("codigo") not in (200, 201, 202):
                return "falla", "El socio no aceptó el Bundle", codigo
            if nombre == "interop_responder" and not datos.get("respuesta_enviada"):
                return "falla", "El socio no aceptó el Task", codigo
            if isinstance(datos, list) and not datos and caso.get("campos"):
                return "pendiente", "Lista vacía, campos sin verificar", codigo
            muestra = datos[0] if isinstance(datos, list) and datos else datos
            faltan = set(caso.get("campos", [])) - (set(muestra) if isinstance(muestra, dict) else set())
            if faltan:
                return "falla", "Campos ausentes: " + ", ".join(sorted(faltan)), codigo
            if any(not isinstance(muestra, dict) or muestra.get(k) != v for k, v in caso.get("espera", {}).items()):
                return "falla", "Contenido distinto a la expectativa declarada", codigo
            if nombre == "rag_preguntar" and not muestra.get("fuentes"):
                return "falla", "Faltaron fuentes de respaldo", codigo
            if nombre == "agente_inyeccion":
                neutralizada = datos.get("seguridad", {}).get("motivo") == "inyeccion_detectada" and not datos.get("invocaciones")
                ultimos = [self.peticion("POST", ruta, rol=rol, json=cuerpo).status_code for _ in range(2)]
                self.guardar("R25", "bloqueo_al_tercer_intento", "pasa" if ultimos[-1] == caso["al_tercer_intento"] else "falla",
                            "La guardia bloquea al tercer intento", ultimos[-1])
                if not neutralizada:
                    return "falla", "No se confirmó la neutralización sin invocar el modelo", codigo
            return "pasa", "Respuesta y contrato básico", codigo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="URL HTTPS del despliegue, sin /api")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--salida", type=Path, required=True)
    parser.add_argument("--entorno-de-prueba", action="store_true", required=True,
                        help="Confirma autorización para ejecutar casos que escriben datos y reentrenan")
    args = parser.parse_args()
    texto = (RAIZ / "tester.yaml").read_text()
    contrato = yaml.safe_load(texto)
    url = (args.url or contrato["url_publica"]).rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.username or parsed.query or parsed.fragment:
        parser.error("Usa una URL HTTPS base sin credenciales ni parámetros")
    v = Verificador(url, contrato, {**dotenv_values(args.env_file), **os.environ})
    por_ruta = requisitos(texto)
    casos = contrato["rutas"] | contrato["rutas_final"]
    try:
        salud = v.peticion("GET", "/health")
        completa = salud.ok and all(salud.json().get(k) == "ok" for k in ("api", "postgres", "mongo", "pacs", "fhir"))
        v.guardar("R04", "salud_publica", "pasa" if completa else "falla", "API y dependencias por HTTPS", salud.status_code)
        v.preparar()
        v.comprobar_acceso()
    except (requests.RequestException, ValueError, KeyError) as error:
        v.guardar("CONTRATO", "preparar", "pendiente", type(error).__name__)
    orden = list(casos)
    orden.remove("consentimiento_registrar")
    orden.insert(orden.index("interop_enviar"), "consentimiento_registrar")
    orden.remove("registro_cierre")
    orden.insert(orden.index("consentimiento_revocar") + 1, "registro_cierre")
    for nombre in orden:
        caso = casos[nombre]
        try:
            estado, detalle, codigo = v.ejecutar(nombre, caso)
        except (requests.RequestException, ValueError, KeyError) as error:
            estado, detalle, codigo = "falla", type(error).__name__, None
        v.guardar(por_ruta.get(nombre, "CONTRATO"), nombre, estado, detalle, codigo)
        time.sleep(0.12)
    v.comprobar_integridad()
    v.guardar("R33", "demostracion_equipo_socio", "pendiente", "L05: sin ejecutar hasta nueva orden")
    v.resultados[-1]["comprobacion"] = "manual_no_ejecutada"
    resumen = {}
    for fila in v.resultados:
        resumen.setdefault(fila["requisito"], Counter())[fila["estado"]] += 1
    reporte = {"url": url, "fecha": datetime.now().astimezone().isoformat(),
               "revision_local": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=RAIZ, text=True).strip(),
               "sha256_contrato": hashlib.sha256(texto.encode()).hexdigest(),
               "total_rutas_yaml": len(casos), "total_escenarios_integridad": len(contrato.get("escenarios_integridad", {})),
               "alcance": "Contrato básico y escenarios de integridad; no certifica automáticamente todos los requisitos ni R33",
               "resumen_por_requisito": resumen, "resultados": v.resultados}
    args.salida.write_text(json.dumps(reporte, indent=2, ensure_ascii=False) + "\n")
    print(dict(Counter(fila["estado"] for fila in v.resultados)))
    return 1 if any(fila["estado"] == "falla" for fila in v.resultados) else 0


if __name__ == "__main__":
    sys.exit(main())
