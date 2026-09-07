# =============================================================================
# test_roles.py — Suite de diferenciación de roles de TrazaRed HUV
#
# Pruebas end-to-end contra la API corriendo (por defecto http://localhost:8000;
# se puede apuntar a la URL pública del túnel con API_BASE=... pytest).
# Requiere los usuarios de prueba creados por el notebook (celdas §6b):
#   admin@trazared.huv / cambia-esta-clave
#   medico@huv.gov.co  / medico123
#   eps@coosalud.com   / eps123
#   paciente@correo.com / paciente123   (vinculado a la paciente 1: Ana Torres / Coosalud)
#
# Ejecutar (con la API arriba):
#   cd python && pytest test_roles.py -v
# =============================================================================
import os
import time

import pytest
import requests

BASE = os.getenv("API_BASE", "http://localhost:8000")

CREDENCIALES = {
    "admin": ("admin@trazared.huv", "cambia-esta-clave"),
    "medico": ("medico@huv.gov.co", "medico123"),
    "eps": ("eps@coosalud.com", "eps123"),
    "paciente": ("paciente@correo.com", "paciente123"),
}

CAMPOS_PACIENTE = {"id", "institucion_destino", "fecha_solicitud", "estado"}


@pytest.fixture(scope="module")
def tokens():
    """Un login por rol, válido para toda la corrida."""
    salida = {}
    for rol, (correo, clave) in CREDENCIALES.items():
        r = requests.post(f"{BASE}/login", data={"username": correo, "password": clave},
                          timeout=15)
        assert r.status_code == 200, f"login {rol} falló: {r.status_code} {r.text}"
        salida[rol] = r.json()["access_token"]
    return salida


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# --- autenticación ------------------------------------------------------------

def test_login_rechaza_clave_equivocada():
    r = requests.post(f"{BASE}/login",
                      data={"username": "medico@huv.gov.co", "password": "incorrecta"},
                      timeout=15)
    assert r.status_code == 401


def test_sin_token_no_hay_acceso():
    r = requests.get(f"{BASE}/pacientes", timeout=15)
    assert r.status_code in (401, 403)


# --- tabla de permisos por rol --------------------------------------------------

def test_paciente_no_puede_listar_pacientes(tokens):
    r = requests.get(f"{BASE}/pacientes", headers=auth(tokens["paciente"]), timeout=15)
    assert r.status_code == 403


def test_eps_no_puede_listar_pacientes(tokens):
    r = requests.get(f"{BASE}/pacientes", headers=auth(tokens["eps"]), timeout=15)
    assert r.status_code == 403


def test_medico_lista_pacientes(tokens):
    r = requests.get(f"{BASE}/pacientes", headers=auth(tokens["medico"]), timeout=15)
    assert r.status_code == 200
    assert any(p["nombre"] == "Ana Torres" for p in r.json())


def test_medico_no_crea_usuarios(tokens):
    r = requests.post(f"{BASE}/usuarios", headers=auth(tokens["medico"]), json={}, timeout=15)
    assert r.status_code == 403


def test_paciente_no_ve_observaciones(tokens):
    r = requests.get(f"{BASE}/observaciones", headers=auth(tokens["paciente"]), timeout=15)
    assert r.status_code == 403


# --- filtrado por dueño del dato --------------------------------------------------

def test_paciente_ve_solo_sus_remisiones_con_campos_reducidos(tokens):
    r = requests.get(f"{BASE}/remisiones", headers=auth(tokens["paciente"]), timeout=15)
    assert r.status_code == 200
    filas = r.json()
    assert filas, "el paciente de prueba debería tener al menos su remisión 1"
    for fila in filas:
        assert set(fila.keys()) <= CAMPOS_PACIENTE, \
            f"el paciente ve campos de más: {set(fila.keys()) - CAMPOS_PACIENTE}"


def test_eps_solo_ve_remisiones_de_su_eps(tokens):
    # Paciente de OTRA EPS + remisión creada por el médico: la EPS Coosalud
    # no debe verla, pero sí la remisión 1 (Ana Torres es Coosalud).
    sufijo = str(int(time.time()))
    r = requests.post(f"{BASE}/pacientes", headers=auth(tokens["medico"]), json={
        "nombre": f"Paciente OtraEPS {sufijo}",
        "documento": f"otraeps-{sufijo}",
        "genero": "F",
        "eps": "Sanitas",
    }, timeout=15)
    assert r.status_code == 201, r.text
    paciente_otra_eps = r.json()["id"]

    r = requests.post(f"{BASE}/remisiones", headers=auth(tokens["medico"]), json={
        "paciente_id": paciente_otra_eps,
        "institucion_origen": "HUV",
        "institucion_destino": "Clinica Foranea",
        "fecha_solicitud": "2026-09-06",
        "motivo": "prueba de filtrado por EPS",
        "estado": "pendiente",
        "convenio_vigente": False,
    }, timeout=15)
    assert r.status_code == 201, r.text
    remision_otra_eps = r.json()["id"]

    try:
        r = requests.get(f"{BASE}/remisiones", headers=auth(tokens["eps"]), timeout=15)
        assert r.status_code == 200
        ids = {f["id"] for f in r.json()}
        assert remision_otra_eps not in ids, "la EPS vio una remisión de otra EPS"
        assert 1 in ids, "la EPS debería ver la remisión de su afiliada (Ana Torres)"
        # El detalle también se niega (403), no solo la lista:
        r = requests.get(f"{BASE}/remisiones/{remision_otra_eps}",
                         headers=auth(tokens["eps"]), timeout=15)
        assert r.status_code == 403
    finally:
        # Limpieza: soft delete de lo creado (el médico la creó él mismo)
        requests.delete(f"{BASE}/remisiones/{remision_otra_eps}",
                        headers=auth(tokens["medico"]), timeout=15)


def test_medico_no_elimina_remision_de_otros_y_admin_si(tokens):
    # Remisión creada por ADMIN: el médico no puede borrarla (403 por creado_por),
    # el admin sí (soft delete), y solo el admin puede restaurarla.
    sufijo = str(int(time.time()))
    r = requests.post(f"{BASE}/pacientes", headers=auth(tokens["admin"]), json={
        "nombre": f"Paciente Admin {sufijo}",
        "documento": f"admin-{sufijo}",
        "genero": "M",
        "eps": "Coosalud",
    }, timeout=15)
    assert r.status_code == 201, r.text
    paciente = r.json()["id"]

    r = requests.post(f"{BASE}/remisiones", headers=auth(tokens["admin"]), json={
        "paciente_id": paciente,
        "institucion_origen": "HUV",
        "institucion_destino": "Hospital Departamental",
        "fecha_solicitud": "2026-09-06",
        "motivo": "prueba de permisos de borrado",
        "estado": "pendiente",
        "convenio_vigente": True,
    }, timeout=15)
    assert r.status_code == 201, r.text
    remision = r.json()["id"]

    try:
        r = requests.delete(f"{BASE}/remisiones/{remision}",
                            headers=auth(tokens["medico"]), timeout=15)
        assert r.status_code == 403, "el médico borró una remisión que no creó él"

        r = requests.delete(f"{BASE}/remisiones/{remision}",
                            headers=auth(tokens["admin"]), timeout=15)
        assert r.status_code == 204, r.text

        # Solo el admin restaura
        r = requests.post(f"{BASE}/remisiones/{remision}/restaurar",
                          headers=auth(tokens["medico"]), timeout=15)
        assert r.status_code == 403, "el médico pudo restaurar"
        r = requests.post(f"{BASE}/remisiones/{remision}/restaurar",
                          headers=auth(tokens["admin"]), timeout=15)
        assert r.status_code == 200 and r.json()["activo"] is True
    finally:
        requests.delete(f"{BASE}/remisiones/{remision}",
                        headers=auth(tokens["admin"]), timeout=15)
