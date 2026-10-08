"""Prueba de la Persona 1 — pasos 1 a 6: main.py partido, cinco roles, aviso del bloqueo, seguridad de acceso (R03)
todo en Docker con túnel público (R04/R05) y auditoría completa (R13). Correr desde la raíz del repo:
    python pruebas/probar_persona1.py
Usa las contraseñas DEMO_* de pass.env y prueba por nginx (http://localhost:8080/api)."""
import secrets
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from dotenv import dotenv_values

RAIZ = Path(__file__).resolve().parent.parent
ENV = {**dotenv_values(RAIZ / "pass.env"), **dotenv_values(RAIZ / ".env")}
API = "http://localhost:8080/api"
ok_total, fallas = 0, 0


def revisar(texto, condicion, detalle=""):
    global ok_total, fallas
    if condicion:
        ok_total += 1
        print(f"  OK     {texto}")
    else:
        fallas += 1
        print(f"  FALLA  {texto}  -> {detalle}")


def entrar(correo, clave):
    r = requests.post(f"{API}/login", data={"username": correo, "password": clave})
    return {"Authorization": f"Bearer {r.json()['access_token']}"} if r.status_code == 200 else None


print("1. La API responde con el código partido")
r = requests.get(f"{API}/health")
revisar("GET /health", r.status_code == 200, r.text)
rutas = requests.get("http://localhost:8000/openapi.json").json()["paths"]
revisar("la API tiene sus 27 rutas", sum(len(m) for m in rutas.values()) == 27, len(rutas))

print("\n2. Login y roles (auth.py)")
admin = entrar("admin@trazared.huv", ENV["DEMO_ADMIN_PASSWORD"])
medico = entrar("medico@huv.gov.co", ENV["DEMO_MEDICO_PASSWORD"])
revisar("entra el admin", admin)
revisar("entra el médico", medico)
revisar("/me del médico dice rol=medico", requests.get(f"{API}/me", headers=medico).json().get("rol") == "medico")

print("\n2b. R01 · Cinco roles")
cuentas = {
    "admin": ("admin@trazared.huv", "DEMO_ADMIN_PASSWORD"),
    "medico": ("medico@huv.gov.co", "DEMO_MEDICO_PASSWORD"),
    "especialista": ("especialista@huv.gov.co", "DEMO_ESPECIALISTA_PASSWORD"),
    "contable": ("contable@huv.gov.co", "DEMO_CONTABLE_PASSWORD"),
    "paciente": ("paciente@correo.com", "DEMO_PACIENTE_PASSWORD"),
}
roles_vistos = set()
for rol, (correo, variable) in cuentas.items():
    if not ENV.get(variable):
        revisar(f"{variable} está en pass.env", False, "agrégala a pass.env")
        continue
    h = entrar(correo, ENV[variable])
    rol_me = requests.get(f"{API}/me", headers=h).json().get("rol") if h else None
    roles_vistos.add(rol_me)
    revisar(f"entra {correo} y /me dice rol={rol}", rol_me == rol, rol_me)
revisar("hay 5 roles distintos", len(roles_vistos - {None}) >= 5, roles_vistos)
nuevo = {"nombre": "x", "correo": f"x.{secrets.token_hex(3)}@huv.gov.co", "contrasena": "Clave123", "rol": "admin"}
revisar("un médico NO puede crear usuarios (403)",
        requests.post(f"{API}/usuarios", headers=medico, json=nuevo).status_code == 403)
revisar("sin token nadie se crea una cuenta (401)",
        requests.post(f"{API}/usuarios", json=nuevo).status_code == 401)

print("\n3. Seguridad")
revisar("sin token, /pacientes da 401", requests.get(f"{API}/pacientes").status_code == 401)
revisar("el médico NO ve la auditoría (403)", requests.get(f"{API}/auditoria", headers=medico).status_code == 403)
revisar("el admin SÍ ve la auditoría", requests.get(f"{API}/auditoria", headers=admin).status_code == 200)

print("\n4. Pacientes y remisiones (main.py y remisiones.py)")
pacientes = requests.get(f"{API}/pacientes", headers=medico).json()
revisar("lista de pacientes", len(pacientes) > 0, len(pacientes))
rem = requests.get(f"{API}/remisiones", headers=medico).json()
revisar("lista de remisiones", len(rem) > 0, len(rem))
gest = requests.get(f"{API}/gestiones-contacto?remision_id={rem[0]['id']}", headers=medico)
revisar("gestiones de contacto (MongoDB)", gest.status_code == 200, gest.status_code)

print("\n5. Soft delete y restauración")
nueva = requests.post(f"{API}/remisiones", headers=medico, json={
    "paciente_id": pacientes[0]["id"], "institucion_origen": "HUV", "institucion_destino": "Prueba P1",
    "fecha_solicitud": "2026-10-07", "motivo": "prueba persona 1", "estado": "pendiente",
    "convenio_vigente": True}).json()
revisar("el médico elimina su remisión (204)",
        requests.delete(f"{API}/remisiones/{nueva['id']}", headers=medico).status_code == 204)
revisar("el médico NO restaura (403)",
        requests.post(f"{API}/remisiones/{nueva['id']}/restaurar", headers=medico).status_code == 403)
revisar("el admin restaura (200)",
        requests.post(f"{API}/remisiones/{nueva['id']}/restaurar", headers=admin).status_code == 200)

print("\n6. Bloqueo al 3er intento")
correo = f"prueba.{secrets.token_hex(3)}@huv.gov.co"
requests.post(f"{API}/usuarios", headers=admin, json={"nombre": "Prueba bloqueo", "correo": correo,
                                                      "contrasena": "Clave123", "rol": "medico"})
codigos = [requests.post(f"{API}/login", data={"username": correo, "password": "mala"}).status_code for _ in range(3)]
revisar("3 claves malas: 401, 401, 423", codigos == [401, 401, 423], codigos)
revisar("la clave correcta también se rechaza",
        requests.post(f"{API}/login", data={"username": correo, "password": "Clave123"}).status_code == 423)
usuario = next(u for u in requests.get(f"{API}/usuarios", headers=admin).json() if u["correo"] == correo)
revisar("el admin desbloquea",
        requests.post(f"{API}/usuarios/{usuario['id']}/desbloquear", headers=admin).status_code == 200)
revisar("después entra con la clave correcta",
        requests.post(f"{API}/login", data={"username": correo, "password": "Clave123"}).status_code == 200)
acciones = {a["accion"] for a in requests.get(f"{API}/auditoria?usuario_id={usuario['id']}", headers=admin).json()}
revisar("bloqueo y desbloqueo en la auditoría", {"bloqueo_usuario"} <= acciones, acciones)

print("\n7. R02 · El bloqueo avisa al admin con publicar()")
try:
    salida = subprocess.run(["docker", "compose", "logs", "api", "--since", "5m"], capture_output=True, text=True,
                            cwd=RAIZ / "docker")
    if salida.returncode != 0:
        print("  (no se pudieron leer los logs de Docker; revisa a mano: docker compose logs api | findstr evento)")
    else:
        revisar("publicar() recibió el evento usuario_bloqueado de esta cuenta",
                "[evento] usuario_bloqueado" in salida.stdout and correo in salida.stdout,
                "no aparece en docker compose logs api")
except (FileNotFoundError, NotADirectoryError):
    print("  (no se encontró el comando docker; revisa a mano: docker compose logs api | findstr evento)")

print("\n8. R03 · Seguridad de acceso")
contable = entrar("contable@huv.gov.co", ENV.get("DEMO_CONTABLE_PASSWORD", ""))
paciente = entrar("paciente@correo.com", ENV.get("DEMO_PACIENTE_PASSWORD", ""))
id_propio = requests.get(f"{API}/me", headers=paciente).json().get("paciente_id") if paciente else None
otro = next((p["id"] for p in pacientes if p["id"] != id_propio), None)
if otro is None:   # base casi vacía: se crea un segundo paciente de prueba
    otro = requests.post(f"{API}/pacientes", headers=medico, json={
        "nombre": "Paciente prueba P1", "documento": f"99{secrets.token_hex(4)}", "genero": "F",
        "eps": "Coosalud"}).json()["id"]
revisar("el contable NO ve datos clínicos de un paciente (403)",
        requests.get(f"{API}/pacientes/{otro}", headers=contable).status_code == 403)
revisar("el contable NO ve remisiones (403)",
        requests.get(f"{API}/remisiones", headers=contable).status_code == 403)
revisar("el paciente NO ve los datos de otro paciente (403/404)",
        requests.get(f"{API}/pacientes/{otro}", headers=paciente).status_code in (403, 404))
revisar("el paciente SÍ ve sus propios datos",
        requests.get(f"{API}/pacientes/{id_propio}", headers=paciente).status_code == 200)
for ruta in ["/remisiones", "/usuarios", "/auditoria", "/me"]:
    revisar(f"sin token, {ruta} da 401/403", requests.get(f"{API}{ruta}").status_code in (401, 403))

malo = "https://sitio-malicioso.example"
r = requests.options(f"{API}/me", headers={"Origin": malo, "Access-Control-Request-Method": "GET"})
revisar("CORS: un origen desconocido no recibe * ni su origen reflejado",
        r.headers.get("Access-Control-Allow-Origin") not in ("*", malo), r.headers.get("Access-Control-Allow-Origin"))
for nombre, resp in [("la API", requests.get(f"{API}/me", headers=admin)), ("la interfaz", requests.get(API[:-4] + "/"))]:
    revisar(f"cabecera X-Content-Type-Options: nosniff en {nombre}",
            resp.headers.get("X-Content-Type-Options", "").lower() == "nosniff", dict(resp.headers))
    revisar(f"cabecera X-Frame-Options en {nombre}", bool(resp.headers.get("X-Frame-Options")))

try:
    archivos = subprocess.run(["git", "ls-files"], cwd=RAIZ, capture_output=True, text=True).stdout.split()
    nombres = {Path(a).name for a in archivos}
    revisar("en el repo NO hay .env ni pass.env", not ({".env", "pass.env"} & nombres))
    revisar("en el repo SÍ está .env.example", ".env.example" in nombres)
except FileNotFoundError:
    print("  (no se encontró git para revisar el repositorio)")

print("\n9. R04/R05 · Todo en Docker y en línea por el túnel")


def compose(*args):
    try:
        r = subprocess.run(["docker", "compose", *args], capture_output=True, text=True, cwd=RAIZ / "docker")
        return r.stdout if r.returncode == 0 else None
    except (FileNotFoundError, NotADirectoryError):
        return None


estado = compose("ps", "--format", "{{.Service}}|{{.State}}|{{.Ports}}")
if estado is None:
    print("  (no se pudo usar docker compose; revisa a mano: docker compose ps)")
else:
    filas = [linea.split("|") for linea in estado.strip().splitlines() if linea.count("|") == 2]
    corriendo = {s for s, st, _ in filas if st == "running"}
    esperados = {"db", "mongo", "api", "ml", "orthanc", "front", "fhir-db", "hapi-fhir", "cloudflared"}
    revisar("los 9 servicios están corriendo", esperados <= corriendo, sorted(esperados - corriendo))
    expuestos = [f"{s}: {p}" for s, _, p in filas
                 if s != "front" and ("0.0.0.0:" in p or "[::]:" in p)]
    revisar("bases, API, FHIR y PACS solo escuchan en 127.0.0.1 (afuera solo se entra por nginx)",
            not expuestos, expuestos)
    salud_ml = compose("exec", "-T", "api", "python", "-c",
                       "import urllib.request;print(urllib.request.urlopen('http://ml:8001/health').read().decode())")
    revisar("la API alcanza al servicio ml por dentro de Docker", salud_ml and '"ok"' in salud_ml, salud_ml)
revisar("ml/ tiene su propio Dockerfile", (RAIZ / "ml" / "Dockerfile").exists())
try:
    requests.get("http://localhost:8001/health", timeout=2)
    revisar("el servicio ml NO se ve desde afuera", False, "responde en localhost:8001")
except requests.exceptions.RequestException:
    revisar("el servicio ml NO se ve desde afuera", True)

publica = None
try:
    host = requests.get("http://localhost:2000/quicktunnel", timeout=5).json().get("hostname")
    publica = f"https://{host}" if host else None
except (requests.exceptions.RequestException, ValueError):
    pass
revisar("cloudflared entregó una URL pública", publica, "revisa: docker compose logs cloudflared")
if publica:
    print(f"         URL pública: {publica}   <- esta va en tester.yaml")
    r = None
    for _ in range(12):            # el DNS del túnel nuevo puede tardar unos segundos
        try:
            r = requests.get(f"{publica}/api/health", timeout=10)
            if r.status_code == 200:
                break
        except requests.exceptions.RequestException:
            pass
        time.sleep(5)
    revisar("por HTTPS público, /api/health responde 200", r is not None and r.status_code == 200,
            r.status_code if r is not None else "sin respuesta")
    try:
        r = requests.get(f"{publica}/", timeout=10)
        revisar("por el túnel la interfaz llega con cabeceras de seguridad",
                r.status_code == 200 and bool(r.headers.get("X-Frame-Options")), r.status_code)
        revisar("por el túnel el login funciona",
                requests.post(f"{publica}/api/login", timeout=10, data={
                    "username": "medico@huv.gov.co", "password": ENV["DEMO_MEDICO_PASSWORD"]}).status_code == 200)
    except requests.exceptions.RequestException as e:
        revisar("por el túnel la interfaz responde", False, e)

print("\n10. R13 · Auditoría: quién hizo qué, sobre qué y cuándo")
sufijo = secrets.token_hex(3)
cuenta = requests.post(f"{API}/usuarios", headers=admin, json={
    "nombre": "Prueba auditoria (se puede borrar)", "correo": f"auditoria.{sufijo}@huv.gov.co",
    "contrasena": "Clave123", "rol": "contable"})
pac = requests.post(f"{API}/pacientes", headers=medico, json={
    "nombre": "Paciente auditoria", "documento": f"98{sufijo}", "genero": "M", "eps": "Coosalud"})
rem_nueva = requests.post(f"{API}/remisiones", headers=medico, json={
    "paciente_id": pac.json().get("id") if pac.status_code == 201 else pacientes[0]["id"],
    "institucion_origen": "Hospital Universitario del Valle", "institucion_destino": "Clínica de prueba",
    "fecha_solicitud": "2026-10-07", "motivo": "Prueba de auditoría", "estado": "pendiente",
    "convenio_vigente": True})
revisar("se crearon usuario, paciente y remisión de prueba",
        (cuenta.status_code, pac.status_code, rem_nueva.status_code) == (201, 201, 201),
        (cuenta.status_code, pac.status_code, rem_nueva.status_code))


def auditado(accion, tabla, rid):
    filas = requests.get(f"{API}/auditoria", headers=admin,
                         params={"tabla": tabla, "registro_id": rid}).json()
    return next((f for f in filas if f["accion"] == accion), None)


if (cuenta.status_code, pac.status_code, rem_nueva.status_code) == (201, 201, 201):
    f_usr = auditado("usuario_creado", "usuarios", cuenta.json()["id"])
    f_pac = auditado("paciente_creado", "pacientes", pac.json()["id"])
    f_rem = auditado("remision_creada", "remisiones", rem_nueva.json()["id"])
    revisar("creación de usuario auditada (y quién la hizo: el admin)",
            f_usr and f_usr["usuario"] == "admin@trazared.huv", f_usr)
    revisar("creación de paciente auditada (sin datos personales en el detalle)",
            f_pac and not f_pac.get("detalle"), f_pac)
    revisar("creación de remisión auditada", f_rem, f_rem)
    revisar("cada fila trae usuario, acción, recurso y fecha",
            f_rem and all(f_rem.get(k) for k in ("usuario", "accion", "recurso", "fecha"))
            and f_rem["recurso"] == f"remisiones/{rem_nueva.json()['id']}", f_rem)
    solo_pac = requests.get(f"{API}/auditoria", headers=admin, params={"tabla": "pacientes"}).json()
    revisar("el filtro por tipo de recurso funciona", solo_pac and all(f["tabla"] == "pacientes" for f in solo_pac))
    requests.delete(f"{API}/remisiones/{rem_nueva.json()['id']}", headers=medico)   # deja limpia la bandeja

# al final, porque gasta el cupo de peticiones por unos segundos
with ThreadPoolExecutor(max_workers=20) as ex:
    codigos = list(ex.map(lambda _: requests.get(f"{API}/health").status_code, range(80)))
revisar("una ráfaga de 80 peticiones recibe 429", 429 in codigos, f"{codigos.count(429)} de 80 con 429")

print(f"\nResultado: {ok_total} OK, {fallas} FALLA")
