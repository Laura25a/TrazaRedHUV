"""Prueba de la Persona 1 — paso 1 (main.py partido). Correr desde la raíz del repo:
    python pruebas/probar_persona1.py
Usa las contraseñas DEMO_* de pass.env y prueba por nginx (http://localhost:8080/api)."""
import secrets
from pathlib import Path

import requests
from dotenv import dotenv_values

ENV = dotenv_values(Path(__file__).resolve().parent.parent / "pass.env")
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

print(f"\nResultado: {ok_total} OK, {fallas} FALLA")
