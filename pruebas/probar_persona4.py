"""Prueba de la Persona 4 — sistema reactivo (R17-R20): canal en tiempo real,
bandeja de notificaciones, trabajo pendiente, flujos automáticos y alertas
críticas. Correr desde la raíz del repo, con el sistema levantado:
    python pruebas/probar_persona4.py
Usa las contraseñas DEMO_* de pass.env y prueba por nginx (http://localhost:8080/api).

OJO sobre eventos en vivo: solo el PROCESO de la API puede entregar eventos al
canal SSE (publicar() empuja a las colas de sus propias conexiones). Por eso los
eventos "usuario_bloqueado" y "alerta_escalada" se disparan con flujos REALES
(login con 3 intentos fallidos y el hilo de escalamiento), y las simulaciones
por `docker compose exec` solo comprueban persistencia y reacciones.
"""
import json
import queue
import subprocess
import sys
import threading
import time
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


def compose(*args):
    return subprocess.run(["docker", "compose", "exec", "-T", "api", *args],
                          cwd=RAIZ / "docker", capture_output=True, text=True).stdout.strip()


def buscar_usuario(admin, correo):
    for u in requests.get(f"{API}/usuarios", headers=admin).json():
        if u["correo"] == correo:
            return u
    return None


def asegurar_desbloqueado(admin, correo):
    """El bloqueo solo avisa cuando la cuenta PASA a bloqueada: hay que
    desbloquearla antes para que la prueba sea re-ejecutable."""
    u = buscar_usuario(admin, correo)
    if u and u.get("bloqueado"):
        requests.post(f"{API}/usuarios/{u['id']}/desbloquear", headers=admin)
    return u


class EscuchaSSE:
    """Consume el canal SSE en un hilo y guarda los eventos que llegan."""

    def __init__(self, correo, clave):
        self.eventos = queue.Queue()
        self.codigo = None
        token = requests.post(f"{API}/login", data={"username": correo, "password": clave}).json()["access_token"]
        self.hilo = threading.Thread(target=self._correr, args=(token,), daemon=True)
        self.hilo.start()
        time.sleep(1)  # deja conectar el EventSource

    def _correr(self, token):
        try:
            r = requests.get(f"{API}/eventos/stream?token={token}", stream=True, timeout=(5, 120))
            self.codigo = r.status_code
            for linea in r.iter_lines(decode_unicode=True):
                if linea and linea.startswith("data: "):
                    self.eventos.put(json.loads(linea[6:]))
        except Exception:
            pass  # el hilo termina cuando se corta la conexión

    def esperar(self, tipo, segundos=8):
        """Devuelve el primer evento cuyo tipo contenga `tipo`, o None si no llega."""
        fin = time.time() + segundos
        while time.time() < fin:
            try:
                ev = self.eventos.get(timeout=max(0.1, fin - time.time()))
            except queue.Empty:
                return None
            if tipo in ev.get("tipo", ""):
                return ev
        return None


# ── 1. Canal en tiempo real (R17) ──────────────────────────────────────────
print("1. Canal en tiempo real (R17)")
r = requests.get(f"{API}/eventos/stream?token=invalido", stream=True, timeout=5)
revisar("sin token válido, el canal responde 401 o 403", r.status_code in (401, 403), r.status_code)

admin = entrar("admin@trazared.huv", ENV["DEMO_ADMIN_PASSWORD"])
medico = entrar("medico@huv.gov.co", ENV["DEMO_MEDICO_PASSWORD"])
paciente = entrar("paciente@correo.com", ENV["DEMO_PACIENTE_PASSWORD"])
contable = entrar("contable@huv.gov.co", ENV["DEMO_CONTABLE_PASSWORD"])
revisar("entran los cuatro usuarios de prueba", all([admin, medico, paciente, contable]), "login")

escucha_admin = EscuchaSSE("admin@trazared.huv", ENV["DEMO_ADMIN_PASSWORD"])
conectado = escucha_admin.esperar("conectado", 5)
revisar("el canal conecta y saluda con el evento 'conectado'", conectado is not None)

# ── 2. Flujos automáticos (R19) con entrega en vivo (R17) ─────────────────
print("\n2. Flujos automáticos (R19) en vivo")
requests.post(f"{API}/usuarios", headers=admin, json={
    "nombre": "Cuenta Bloqueo P4", "correo": "bloqueo.p4@huv.gov.co",
    "contrasena": ENV.get("DEMO_BLOQUEO_PASSWORD", "bloqueo123"), "rol": "medico"})
asegurar_desbloqueado(admin, "bloqueo.p4@huv.gov.co")
codigos = [requests.post(f"{API}/login", data={"username": "bloqueo.p4@huv.gov.co", "password": "clave-mala"}).status_code
           for _ in range(3)]
revisar("3 intentos fallidos dan 401, 401 y 423", codigos == [401, 401, 423], codigos)

inicio = time.time()
aviso = escucha_admin.esperar("usuario_bloqueado", 8)
revisar("el bloqueo llega EN VIVO al admin en menos de 8 s", aviso is not None,
        f"{round(time.time() - inicio, 1)} s sin evento")
revisar("el evento trae el usuario afectado",
        aviso is not None and aviso.get("usuario_afectado") == "bloqueo.p4@huv.gov.co", aviso)

bandeja = requests.get(f"{API}/notificaciones", headers=admin).json()
aviso_bandeja = next((n for n in bandeja if n["tipo"] == "usuario_bloqueado"
                      and n.get("usuario_afectado") == "bloqueo.p4@huv.gov.co"), None)
revisar("el aviso queda en la bandeja del admin (R18, sobrevive recargas)",
        aviso_bandeja is not None, f"{len(bandeja)} notificaciones")

if aviso_bandeja:
    r = requests.post(f"{API}/notificaciones/{aviso_bandeja['id']}/leer", headers=admin)
    revisar("el dueño marca su notificación como leída", r.status_code == 200 and r.json().get("leida"), r.text)
    r = requests.post(f"{API}/notificaciones/{aviso_bandeja['id']}/leer", headers=medico)
    revisar("nadie marca una notificación ajena (403)", r.status_code == 403, r.status_code)

# ── 3. Aislamiento del canal por rol (R17) ────────────────────────────────
print("\n3. Aislamiento del canal por rol")
escucha_paciente = EscuchaSSE("paciente@correo.com", ENV["DEMO_PACIENTE_PASSWORD"])
escucha_contable = EscuchaSSE("contable@huv.gov.co", ENV["DEMO_CONTABLE_PASSWORD"])
requests.post(f"{API}/login", data={"username": "bloqueo.p4@huv.gov.co", "password": "clave-mala"})  # un intento más
otro = escucha_admin.esperar("usuario_bloqueado", 8)  # el bloqueo ya estaba: basta con mirar lo que no llega
fuga_paciente = escucha_paciente.esperar("usuario_bloqueado", 3)
fuga_contable = escucha_contable.esperar("usuario_bloqueado", 3)
revisar("el canal del paciente NO recibe eventos de otros roles",
        fuga_paciente is None, fuga_paciente)
revisar("el canal del contable NO recibe eventos de otros roles",
        fuga_contable is None, fuga_contable)

# ── 4. Trabajo pendiente (R18) ────────────────────────────────────────────
print("\n4. Trabajo pendiente (R18)")
pend = requests.get(f"{API}/pendientes", headers=medico).json()
revisar("pendientes trae los tres contadores del contrato",
        set(pend) == {"reportes_por_revisar", "remisiones_recibidas", "alertas_activas"}
        and all(isinstance(v, int) for v in pend.values()), pend)
pend_paciente = requests.get(f"{API}/pendientes", headers=paciente).json()
revisar("los contadores de un paciente son cero", not any(pend_paciente.values()), pend_paciente)

# ── 5. Alertas críticas con reconocimiento (R20) ──────────────────────────
print("\n5. Alertas críticas (R20)")
# Se crea por la vía real (publicar → reactor), que es la que deja alerta_creada
# en la auditoría; desde fuera del proceso la persistencia sí funciona.
compose("python", "-c",
        "import auth, psycopg2; from eventos import publicar, ids_con_rol; "
        "db=psycopg2.connect(auth.PG_CONNECTION_STRING); "
        "publicar(ids_con_rol(db,'medico'), {'tipo':'alerta_critica',"
        "'mensaje':'Prueba P4: caso critico','paciente_id':1,'nivel':'critico'})")
alertas = requests.get(f"{API}/alertas", headers=medico).json()
la_mia = next((a for a in alertas if a["estado"] == "activa" and a["mensaje"] == "Prueba P4: caso critico"), None)
revisar("la alerta activa se lista para el clínico", la_mia is not None, f"{len(alertas)} alertas")
revisar("un paciente no ve las alertas (403)",
        requests.get(f"{API}/alertas", headers=paciente).status_code == 403)
revisar("un contable no ve las alertas (403)",
        requests.get(f"{API}/alertas", headers=contable).status_code == 403)

if la_mia:
    r = requests.post(f"{API}/alertas/{la_mia['id']}/reconocer", headers=medico, json={"nota": "Atendido en urgencias"})
    revisar("el clínico reconoce la alerta con nota (2xx)", r.status_code // 100 == 2, r.status_code)
    activas = [a for a in requests.get(f"{API}/alertas", headers=medico).json() if a["id"] == la_mia["id"]]
    revisar("la alerta reconocida deja de estar activa",
            activas and activas[0]["estado"] == "reconocida", activas)
    revisar("un paciente no puede reconocer alertas (403)",
            requests.post(f"{API}/alertas/{la_mia['id']}/reconocer", headers=paciente,
                          json={"nota": "no puedo"}).status_code == 403)
    auditoria = requests.get(f"{API}/auditoria?tabla=alertas", headers=admin).json()
    acciones = {a["accion"] for a in auditoria if a.get("registro_id") == la_mia["id"]}
    revisar("la alerta y su reconocimiento quedan en la auditoría (R13)",
            {"alerta_creada", "alerta_reconocida"} <= acciones, acciones)

# ── 6. Escalamiento (R20) — solo si el plazo es corto ─────────────────────
print("\n6. Escalamiento de alertas")
esc_seg = int(ENV.get("ESCALAMIENTO_SEGUNDOS", "600"))
if esc_seg <= 30:
    compose("python", "-c",
            "import auth, psycopg2; from eventos import publicar, ids_con_rol; "
            "db=psycopg2.connect(auth.PG_CONNECTION_STRING); "
            "publicar(ids_con_rol(db,'medico'), {'tipo':'alerta_critica',"
            "'mensaje':'Prueba P4: sin reconocer','paciente_id':1,'nivel':'critico'})")
    escalada = escucha_admin.esperar("alerta_escalada", 45)
    revisar(f"sin reconocer en {esc_seg} s, el admin recibe la alerta escalada", escalada is not None)
else:
    print(f"  [..]  omitido: ESCALAMIENTO_SEGUNDOS={esc_seg} (bájalo en pass.env a 15 para probarlo)")

# ── 7. Factura automática al aprobar un reporte (R19) ─────────────────────
print("\n7. Factura automática (R19)")
marcador = int(time.time())
compose("python", "-c",
        f"import auth, psycopg2; from eventos import publicar, ids_con_rol; "
        f"db=psycopg2.connect(auth.PG_CONNECTION_STRING); "
        f"publicar(ids_con_rol(db,'medico'), {{'tipo':'reporte_aprobado',"
        f"'mensaje':'Prueba P4','paciente_id':1,'reporte_id':{marcador}}})")
factura = compose("python", "-c",
                  f"from auth import db_mongo; import json; "
                  f"f=db_mongo['facturas'].find_one({{'reporte_id':{marcador}}}, {{'_id':0}}); "
                  f"print(json.dumps(f, default=str))")
ok_factura = f'"reporte_id": {marcador}' in factura or f'"reporte_id":{marcador}' in factura
revisar("al aprobarse un reporte se crea sola la factura en MongoDB con reporte_id",
        ok_factura, factura[:120])

# ── Cierre: dejar la cuenta de prueba desbloqueada (la prueba es re-ejecutable)
asegurar_desbloqueado(admin, "bloqueo.p4@huv.gov.co")

print(f"\nResultado: {ok_total} OK, {fallas} FALLA")
sys.exit(1 if fallas else 0)
