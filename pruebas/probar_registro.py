"""
probar_registro.py — Prueba de punta a punta del rediseño "libro de radicación".

Recorre la API como lo haría cada rol y dice OK / FALLA en cada punto:
libro, formato, permisos por rol, anotar gestiones (con la IA clasificando),
cambio de estado automático, bitácora no editable, cierre, liquidación,
facturación, glosa y pago, las 9 descargas, el seguimiento del paciente y que
la IA haya pronosticado SOLA las remisiones abiertas.

OJO: escribe en la base (anota 2 gestiones, cierra 1 remisión y la factura).
Córranlo sobre los datos de prueba, no sobre datos reales.

Uso (con el sistema arriba en Docker):
    pip install httpx
    python pruebas/probar_registro.py
Variables opcionales: TRAZARED_API (por defecto http://localhost:8080/api) y las
contraseñas DEMO_* del .env (se leen solas si existe ../.env o ../pass.env).
"""
import json
import os
import sys
import time
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parent.parent
for nombre in (".env", "pass.env"):
    archivo = RAIZ / nombre
    if archivo.exists():
        for linea in archivo.read_text(encoding="utf-8").splitlines():
            if "=" in linea and not linea.lstrip().startswith("#"):
                k, v = linea.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

B = os.getenv("TRAZARED_API", "http://localhost:8080/api").rstrip("/")
CLAVE = lambda var, defecto: os.getenv(var, defecto)
fallas = []


def login(u, c):
    r = httpx.post(f"{B}/login", data={"username": u, "password": c})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def ok(cond, msg):
    print(("  OK  " if cond else "  FALLA ") + msg)
    if not cond:
        fallas.append(msg)


adm = login("admin@trazared.huv", CLAVE("DEMO_ADMIN_PASSWORD", "admin123"))
med = login("medico@huv.gov.co", CLAVE("DEMO_MEDICO_PASSWORD", "medico123"))
con = login("contable@huv.gov.co", CLAVE("DEMO_CONTABLE_PASSWORD", "conta123"))
eps = login("eps@coosalud.com", CLAVE("DEMO_EPS_PASSWORD", "eps123"))
pac = login("paciente@correo.com", CLAVE("DEMO_PACIENTE_PASSWORD", "paciente123"))
T = 120

print("1. La IA pronosticó sola las remisiones abiertas (sin dar clic en nada)")
for intento in range(24):                     # hasta 2 minutos: el hilo arranca con la API
    r = httpx.get(f"{B}/registro/libro", params={"vista": "abiertas"}, headers=adm, timeout=T).json()
    abiertas = r["asientos"]
    con_ia = [a for a in abiertas if a.get("probabilidad_efectiva") is not None]
    if abiertas and len(con_ia) == len(abiertas):
        break
    time.sleep(5)
print(f"    {len(con_ia)} de {len(abiertas)} abiertas con pronóstico")
ok(abiertas and len(con_ia) == len(abiertas), "pronósticos automáticos al arrancar")

print("2. Libro de radicación")
r = httpx.get(f"{B}/registro/libro", params={"vista": "abiertas"}, headers=med, timeout=T)
d = r.json(); ok(r.status_code == 200, "libro responde"); print("   resumen:", d["resumen"])
a = d["asientos"][0]
print("   asiento:", {k: a[k] for k in ("radicado", "folio", "estado", "intentos", "horas_abierta", "sin_movimiento", "probabilidad_efectiva")})
ok(a["radicado"].startswith("REM-2026-"), "radicado con formato")
r = httpx.get(f"{B}/registro/libro", params={"vista": "riesgo"}, headers=med, timeout=T); ok(r.status_code == 200, f"vista riesgo ({r.json()['total_filtrado']})")
r = httpx.get(f"{B}/registro/libro", params={"vista": "todas", "q": "Imbanaco"}, headers=med, timeout=T); ok(r.json()["total_filtrado"] > 0, "búsqueda")
r = httpx.get(f"{B}/registro/libro", headers=eps, timeout=T); ok(all(x["eps"] == "Coosalud" for x in r.json()["asientos"]), "EPS solo ve sus afiliados")
ok(httpx.get(f"{B}/registro/libro", headers=con).status_code == 403, "contable no ve el libro clínico")
ok(httpx.get(f"{B}/registro/libro", headers=pac).status_code == 403, "paciente no ve el libro")

print("3. Formato de una remisión abierta")
rid = next(x["id"] for x in d["asientos"] if x["estado"] in ("en_gestion", "pendiente") and x.get("activo") is not False)
r = httpx.get(f"{B}/registro/remisiones/{rid}/formato", headers=med, timeout=T); f = r.json()
ok(r.status_code == 200, "formato responde")
print("   ", f["remision"]["radicado"], f["estado_texto"], f["tiempo_texto"], "| bitácora:", len(f["bitacora"]),
      "| eventos:", len(f["eventos"]), "| IA:", f["pronostico"] and f["pronostico"]["probabilidad_efectiva"])
ok(f["costos"] is None, "médico no ve costos")
fa = httpx.get(f"{B}/registro/remisiones/{rid}/formato", headers=adm, timeout=T).json()
ok(fa["costos"] and fa["costos"]["total"] > 0, f"admin ve costos ({fa['costos']['total'] if fa['costos'] else None})")

print("4. Anotar gestiones (append-only)")
r = httpx.post(f"{B}/registro/remisiones/{rid}/gestiones", headers=med, timeout=T,
               json={"institucion_contactada": "Clínica Imbanaco", "medio": "telefono", "respuesta": "No hay camas en la UCI ahora"})
g = r.json(); print("   ", r.status_code, g.get("clasificacion"), g.get("estado"))
ok(r.status_code == 201 and g["clasificacion"]["causa"] == "sin_cama", "gestión anotada y clasificada")
r = httpx.post(f"{B}/registro/remisiones/{rid}/gestiones", headers=med, timeout=T,
               json={"institucion_contactada": "Fundación Valle del Lili", "medio": "telefono", "respuesta": "Aceptan al paciente, enviar en ambulancia medicalizada"})
g = r.json(); print("   ", r.status_code, g.get("clasificacion"), g.get("estado"), g.get("estado_cambio"))
ok(g.get("estado") == "aceptada", "acepta → estado aceptada automático")
docs = httpx.get(f"{B}/gestiones-contacto", params={"remision_id": rid}, headers=adm).json()
r = httpx.put(f"{B}/gestiones-contacto/{docs[0]['_id']}", headers=adm,
              json={"remision_id": rid, "contactos": docs[0]["contactos"][1:]})
ok(r.status_code == 409, "PUT que borra contactos → 409")
ok(httpx.post(f"{B}/registro/remisiones/{rid}/gestiones", headers=eps, json={"institucion_contactada": "x", "respuesta": "yy"}).status_code == 403,
   "EPS no anota")

print("5. Cierre y contabilidad")
r = httpx.post(f"{B}/registro/remisiones/{rid}/cierre", headers=med, json={"resultado": "trasladado"}, timeout=T)
print("   ", r.status_code, r.json()); ok(r.status_code == 200 and r.json()["estado"] == "completada", "cierre trasladado")
ok(httpx.post(f"{B}/registro/remisiones/{rid}/gestiones", headers=med, json={"institucion_contactada": "Clínica X", "respuesta": "prueba"}).status_code == 409,
   "asiento cerrado no admite gestiones")
f = httpx.get(f"{B}/registro/remisiones/{rid}/formato", headers=adm, timeout=T).json()
print("   eventos:", [e["titulo"] for e in f["eventos"]][-6:])
print("   cierre:", f["cierre"], "| ia:", f["resultado_ia"])
liq = httpx.get(f"{B}/contabilidad/remisiones/{rid}", headers=con).json()
print("   liquidación:", liq["estado_cobro"], liq["total"], [c["codigo"] for c in liq["conceptos"]])
ok(liq["estado_cobro"] == "por_facturar" and any(c["codigo"] == "TRA-MED" for c in liq["conceptos"]), "por facturar con traslado medicalizado")
ok("motivo" not in liq and "diagnostico" not in liq, "liquidación sin datos clínicos")
r = httpx.post(f"{B}/contabilidad/remisiones/{rid}/facturar", headers=con); fac = r.json()
print("   factura:", r.status_code, fac.get("numero"), fac.get("valor")); ok(r.status_code == 201, "facturada")
ok(httpx.post(f"{B}/contabilidad/remisiones/{rid}/facturar", headers=con).status_code == 409, "no se factura dos veces")
r = httpx.put(f"{B}/facturas/{fac['_id']}/glosar", headers=con, json={"motivo": "Falta soporte de traslado"}); ok(r.json()["estado_pago"] == "glosada", "glosa")
r = httpx.put(f"{B}/facturas/{fac['_id']}/pagar", headers=con); ok(r.json()["estado_pago"] == "pagada", "pago tras glosa")
abierta = next(x["id"] for x in d["asientos"] if x["id"] != rid and x["estado"] in ("pendiente", "en_gestion"))
ok(httpx.post(f"{B}/contabilidad/remisiones/{abierta}/facturar", headers=con).status_code == 409, "no se factura una abierta")
res = httpx.get(f"{B}/contabilidad/resumen", headers=con, timeout=T).json()
print("   resumen contable:", {k: res[k] for k in ("acumulando", "por_facturar", "facturado", "glosado", "pagado", "costo_fallidas")})
ok(httpx.get(f"{B}/contabilidad/libro", headers=med).status_code == 403, "médico no ve libro contable")
ok(len(f["pronosticos"]) >= 2, f"la IA dejó escrito cada pronóstico ({len(f['pronosticos'])} en el recorrido)")

print("6. Descargas")
for ruta, h, tipo in [("/registro/libro.csv", med, "text/csv"), ("/registro/libro.xlsx", med, "spreadsheet"),
                      ("/registro/libro.pdf", med, "pdf"), (f"/registro/remisiones/{rid}/constancia.pdf", adm, "pdf"),
                      (f"/registro/remisiones/{rid}/bitacora.xlsx", med, "spreadsheet"),
                      (f"/registro/remisiones/{rid}/fhir.json", med, "json"),
                      ("/contabilidad/libro.xlsx", con, "spreadsheet"), ("/contabilidad/libro.pdf", con, "pdf"),
                      ("/contabilidad/rips.json", con, "json")]:
    r = httpx.get(f"{B}{ruta}", headers=h, timeout=T)
    ok(r.status_code == 200 and tipo in r.headers.get("content-type", ""), f"{ruta} ({len(r.content)} bytes)")
    nombre = ruta.strip("/").replace("/", "_")
    carpeta = RAIZ / "pruebas" / "descargas"
    carpeta.mkdir(exist_ok=True)
    (carpeta / nombre).write_bytes(r.content)
b = json.loads((RAIZ / "pruebas" / "descargas" / f"registro_remisiones_{rid}_fhir.json").read_text(encoding="utf-8"))
ok(b["resourceType"] == "Bundle" and any(e["resource"]["resourceType"] == "Communication" for e in b["entry"]), "Bundle FHIR con Communication")

print("7. Paciente e IA")
r = httpx.get(f"{B}/registro/mi-seguimiento", headers=pac); print("   ", r.status_code, r.json()[:1]); ok(r.status_code == 200, "seguimiento paciente")
r = httpx.post(f"{B}/registro/ia/clasificar", headers=med, json={"texto": "falta la autorización de la EPS"}); ok(r.json()["causa"] == "problema_eps", "clasificar")
r = httpx.get(f"{B}/registro/ia/resumen", headers=med, timeout=T); print("   ", {k: v for k, v in r.json()["en_libro"].items()}); ok(r.status_code == 200, "resumen IA")

print("\nLos archivos descargados quedaron en pruebas/descargas/ para abrirlos a mano.")
print("FALLAS:", fallas or "ninguna")
sys.exit(1 if fallas else 0)
