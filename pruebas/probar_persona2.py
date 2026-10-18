"""Prueba de la Persona 2 — servicio de IA: modelo (R22), agrupamiento (R07), seudonimización (R12),
reportes con aprobación clínica (R10), alerta crítica (R20), factura automática (R19) y auditoría (R13),
más el historial (R11) y las imágenes (R06) si completas sus rutas más abajo.
Correr desde la raíz del repo, con el sistema levantado:
    python pruebas/probar_persona2.py              # no cambia el modelo guardado
    python pruebas/probar_persona2.py --entrenar   # además reentrena (sube la versión del modelo)
Usa las contraseñas DEMO_* de pass.env y prueba por nginx (http://localhost:8080/api).

Cada corrida crea un paciente "canario", dos reportes de IA y una factura, y reconoce la alerta
que generó: se puede repetir las veces que sea. Para elegir los pacientes de prueba usa el propio
modelo guardado (docker compose exec ml), así el perfil crítico es crítico de verdad."""
import json
import queue
import re
import secrets
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
ENTRENAR = "--entrenar" in sys.argv
ok_total, fallas = 0, 0

# Rutas que cada equipo nombra a su manera (cópiar de  tester.yaml). "{id}" = id del paciente.

RUTA_HISTORIA = ""        # p. ej. "/pacientes/{id}/historia"
RUTA_HISTORIA_PDF = ""    # p. ej. "/pacientes/{id}/historia/pdf"
RUTA_IMAGENES = ""        # p. ej. "/pacientes/{id}/imagenes"

CAMPOS_CLINICOS = ("sexo", "edad", "pacientes_por_hora", "modo_llegada", "lesion", "mental",
                   "dolor", "nrs_dolor", "sbp", "dbp", "fc", "fr", "temperatura", "saturacion")
PROHIBIDOS = ("nombre", "documento", "correo", "telefono", "direccion", "eps", "alergias", "fecha_nacimiento")
# Del cuaderno: (n_reales, n_sinteticos) por grupo. Si cambia la numeración de K-means, esto lo delata.
CLUSTERS_ESPERADOS = {0: (32, 60), 1: (305, 60), 2: (404, 60), 3: (526, 10)}


def revisar(texto, condicion, detalle=""):
    global ok_total, fallas
    if condicion:
        ok_total += 1
        print(f"  OK     {texto}")
    else:
        fallas += 1
        print(f"  FALLA  {texto}  -> {detalle}")


def omitir(texto):
    print(f"  [..]   omitido: {texto}")


def entrar(correo, clave):
    if not clave:
        return None
    r = requests.post(f"{API}/login", data={"username": correo, "password": clave})
    return {"Authorization": f"Bearer {r.json()['access_token']}"} if r.status_code == 200 else None


def compose_py(servicio, codigo, espera=120):
    """Corre código Python dentro de un contenedor (por stdin, sin problemas de comillas)."""
    try:
        r = subprocess.run(["docker", "compose", "exec", "-T", servicio, "python", "-"], input=codigo,
                           cwd=RAIZ / "docker", capture_output=True, text=True, encoding="utf-8",
                           timeout=espera)
        return r.stdout.strip() if r.returncode == 0 else None
    except (FileNotFoundError, NotADirectoryError, subprocess.TimeoutExpired):
        return None


def iguales(a, b):
    if a is None or b is None:
        return a is b
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return a == b


class EscuchaSSE:
    """Consume el canal SSE en un hilo y guarda los eventos que llegan."""

    def __init__(self, correo, clave):
        self.eventos = queue.Queue()
        token = requests.post(f"{API}/login", data={"username": correo, "password": clave}).json()["access_token"]
        threading.Thread(target=self._correr, args=(token,), daemon=True).start()
        time.sleep(1)

    def _correr(self, token):
        try:
            r = requests.get(f"{API}/eventos/stream?token={token}", stream=True, timeout=(5, 120))
            for linea in r.iter_lines(decode_unicode=True):
                if linea and linea.startswith("data: "):
                    self.eventos.put(json.loads(linea[6:]))
        except Exception:
            pass

    def esperar(self, condicion, segundos=10):
        fin = time.time() + segundos
        while time.time() < fin:
            try:
                ev = self.eventos.get(timeout=max(0.1, fin - time.time()))
            except queue.Empty:
                return None
            if condicion(ev):
                return ev
        return None


# Se ejecuta DENTRO del contenedor ml: elige, con el modelo guardado, el paciente real del dataset
# con la mayor y la menor probabilidad de urgencia, ya traducidos a los campos de la API, y cuenta
# cuántos pacientes reales rechazaría la validación de DatosClinicos (rangos y campos permitidos).
# Usa cargar_ktas() de entrenar.py: es la única copia de la limpieza del dataset.
CODIGO_PERFILES = r'''
import json
import numpy as np
import pandas as pd
from pydantic import ValidationError
import main
from entrenar import cargar_ktas

ENTEROS = ("sexo", "edad", "pacientes_por_hora", "modo_llegada", "lesion", "mental", "dolor")
p = main.paquete
df = cargar_ktas()
X = df[p["columnas_num"] + p["columnas_cat"]]
proba = p["modelo"].predict_proba(X)[:, 1]


def a_payload(i):
    fila = X.iloc[i]
    d = {}
    for k, col in main.COLUMNAS.items():
        v = fila[col]
        d[k] = None if pd.isna(v) else (int(v) if k in ENTEROS else float(v))
    return d


def valido(d):
    try:
        main.DatosClinicos(id_seudonimo="p-busqueda", **d)
        return True
    except ValidationError:
        return False


validos = np.array([valido(a_payload(i)) for i in range(len(X))])
orden = np.argsort(proba)
il = int(next(i for i in orden if validos[i]))
ig = int(next(i for i in orden[::-1] if validos[i]))
print(json.dumps({"grave": a_payload(ig), "p_grave": float(proba[ig]),
                  "leve": a_payload(il), "p_leve": float(proba[il]),
                  "n_total": int(len(X)), "n_fuera_de_rango": int((~validos).sum()),
                  "umbral": p["umbral"], "umbral_critico": p["umbral_critico"]}))
'''

CODIGO_FACTURA = """
from auth import db_mongo
import json
f = db_mongo['facturas'].find_one({{'reporte_id': {rid}}}, {{'_id': 0}})
print(json.dumps(f, default=str))
"""


def factura_de(rid, intentos=8):
    """Busca la factura automática del reporte en MongoDB (el reactor tarda un instante)."""
    for _ in range(intentos):
        salida = compose_py("api", CODIGO_FACTURA.format(rid=rid), espera=30)
        if salida is None:
            return "sin_docker"
        try:
            f = json.loads(salida.splitlines()[-1])
        except (ValueError, IndexError):
            f = None
        if f:
            return f
        time.sleep(1)
    return None


# ── Sesiones ───────────────────────────────────────────────────────────────
admin = entrar("admin@trazared.huv", ENV.get("DEMO_ADMIN_PASSWORD"))
medico = entrar("medico@huv.gov.co", ENV.get("DEMO_MEDICO_PASSWORD"))
especialista = entrar("especialista@huv.gov.co", ENV.get("DEMO_ESPECIALISTA_PASSWORD"))
paciente = entrar("paciente@correo.com", ENV.get("DEMO_PACIENTE_PASSWORD"))
contable = entrar("contable@huv.gov.co", ENV.get("DEMO_CONTABLE_PASSWORD"))
if not (admin and medico and paciente and contable):
    print("No entraron los usuarios básicos de prueba (admin, médico, paciente, contable): revisa pass.env.")
    sys.exit(1)

# ── 1. El modelo (R22) ─────────────────────────────────────────────────────
print("1. R22 · El modelo (GET /modelo y POST /entrenar)")
r = requests.get(f"{API}/modelo", headers=medico)
info = r.json() if r.status_code == 200 else {}
metricas = info.get("metricas", {})
revisar("GET /modelo responde 200 a un usuario autenticado", r.status_code == 200, r.status_code)
revisar("sin token, /modelo da 401 o 403", requests.get(f"{API}/modelo").status_code in (401, 403))
revisar("trae versión, métricas y los dos umbrales",
        all(k in info for k in ("version", "metricas", "umbral", "umbral_critico")), list(info))
revisar("las métricas traen AUC, sensibilidad, especificidad, precisión, F1 y matriz de confusión",
        all(k in metricas for k in ("auc_prueba", "sensibilidad", "especificidad", "precision", "f1",
                                    "matriz_confusion")), list(metricas))
revisar("el umbral crítico es mayor que el de decisión",
        info.get("umbral_critico", 0) > info.get("umbral", 1), (info.get("umbral"), info.get("umbral_critico")))
revisar("el AUC de prueba es razonable (entre 0.5 y 1)", 0.5 < metricas.get("auc_prueba", 0) <= 1,
        metricas.get("auc_prueba"))
print(f"         modelo: versión {info.get('version')} · algoritmo {info.get('algoritmo')} · "
      f"AUC prueba {metricas.get('auc_prueba')}")
revisar("un médico NO reentrena (403)", requests.post(f"{API}/entrenar", headers=medico).status_code == 403)
revisar("un paciente NO reentrena (403)", requests.post(f"{API}/entrenar", headers=paciente).status_code == 403)
revisar("sin token, /entrenar da 401 o 403", requests.post(f"{API}/entrenar").status_code in (401, 403))

# ── 2. Agrupamiento (R07) ──────────────────────────────────────────────────
print("\n2. R07 · Agrupamiento de pacientes (GET /ia/clusters)")
r = requests.get(f"{API}/ia/clusters", headers=medico)
grupos = r.json() if r.status_code == 200 else []
revisar("el médico consulta los clusters (200)", r.status_code == 200, r.status_code)
revisar("hay al menos 2 grupos", isinstance(grupos, list) and len(grupos) >= 2, len(grupos))
revisar("cada grupo trae cluster, descripcion, n_reales y n_sinteticos",
        grupos and all({"cluster", "descripcion", "n_reales", "n_sinteticos"} <= set(g) for g in grupos),
        grupos[:1])
revisar("cada grupo tiene pacientes reales y sintéticos",
        grupos and all(g.get("n_reales", 0) > 0 and g.get("n_sinteticos", 0) > 0 for g in grupos),
        [(g.get("cluster"), g.get("n_reales"), g.get("n_sinteticos")) for g in grupos])
revisar("cada grupo tiene una descripción no vacía",
        grupos and all(isinstance(g.get("descripcion"), str) and len(g["descripcion"]) > 20 for g in grupos))
obtenido = {g["cluster"]: (g["n_reales"], g["n_sinteticos"]) for g in grupos if "cluster" in g}
revisar("los grupos coinciden con los del cuaderno (reales y sintéticos por grupo)",
        obtenido == CLUSTERS_ESPERADOS, f"obtenido {obtenido} · esperado {CLUSTERS_ESPERADOS}")
revisar("el especialista y el admin también consultan los clusters",
        all(requests.get(f"{API}/ia/clusters", headers=h).status_code == 200
            for h in (admin, especialista) if h))
revisar("un paciente NO ve los clusters (403)", requests.get(f"{API}/ia/clusters", headers=paciente).status_code == 403)
revisar("un contable NO ve los clusters (403)", requests.get(f"{API}/ia/clusters", headers=contable).status_code == 403)
revisar("sin token, /ia/clusters da 401 o 403", requests.get(f"{API}/ia/clusters").status_code in (401, 403))

# ── Preparación: pacientes de prueba con el propio modelo guardado ─────────
print("\n3. Preparación · paciente canario y perfiles de prueba")
salida = compose_py("ml", CODIGO_PERFILES)
perfiles = None
if salida:
    try:
        perfiles = json.loads(salida.splitlines()[-1])
    except (ValueError, IndexError):
        perfiles = None
if perfiles is None:
    omitir("no se pudo consultar el modelo dentro de Docker (docker compose exec ml): se omiten las pruebas "
           "de reportes y alertas, que necesitan un perfil crítico real")
else:
    revisar("el dataset trae un paciente que supera el umbral crítico del modelo",
            perfiles["p_grave"] >= perfiles["umbral_critico"],
            f"la mayor probabilidad es {perfiles['p_grave']:.4f} y el umbral crítico {perfiles['umbral_critico']}")
    revisar("ningún paciente real del dataset queda fuera de los rangos de validación del servicio",
            perfiles["n_fuera_de_rango"] == 0,
            f"{perfiles['n_fuera_de_rango']} de {perfiles['n_total']} pacientes reales serían rechazados (422): "
            "ensancha los rangos de DatosClinicos en ml/main.py")
    revisar("y otro que queda por debajo del umbral de decisión",
            perfiles["p_leve"] < perfiles["umbral"], perfiles["p_leve"])
    print(f"         paciente_critico para tester.yaml: {json.dumps(perfiles['grave'], ensure_ascii=False)}")
    print(f"         (probabilidad {perfiles['p_grave']:.4f}; umbral crítico {perfiles['umbral_critico']})")

sufijo = secrets.token_hex(3)
canario = {"nombre": f"CanarioNombre{sufijo}", "documento": f"CAN{sufijo}", "genero": "F",
           "eps": f"CanarioEPS{sufijo}", "telefono": f"3009{sufijo}"}
r = requests.post(f"{API}/pacientes", headers=medico, json=canario)
if r.status_code != 201:                      # si la ficha no admite teléfono, se prueba sin él
    canario.pop("telefono")
    r = requests.post(f"{API}/pacientes", headers=medico, json=canario)
pid = r.json().get("id") if r.status_code == 201 else None
revisar("se creó el paciente canario (con nombre, documento y EPS reconocibles)", pid is not None,
        (r.status_code, r.text[:120]))

if pid and perfiles:
    leve, grave = perfiles["leve"], perfiles["grave"]

    def analizar(headers, perfil, paciente_id=pid):
        return requests.post(f"{API}/ia/analizar", headers=headers, json={"paciente_id": paciente_id, **perfil})

    # ── 4. Quién puede analizar ───────────────────────────────────────────
    print("\n4. R10 · Quién puede analizar")
    revisar("un paciente NO analiza (403)", analizar(paciente, leve).status_code == 403)
    revisar("un contable NO analiza (403)", analizar(contable, leve).status_code == 403)
    revisar("sin token, analizar da 401 o 403",
            requests.post(f"{API}/ia/analizar", json={"paciente_id": pid, **leve}).status_code in (401, 403))
    revisar("un paciente inexistente da 404", analizar(medico, leve, paciente_id=99999999).status_code == 404)
    r_imp = analizar(medico, {**leve, "saturacion": 500})
    revisar("un valor clínico imposible (saturación 500) responde 422 y no llega a crear reporte",
            r_imp.status_code == 422,
            f"{r_imp.status_code}: si es 503, llamar_modelo() de ia.py convierte el 422 del servicio en "
            "'servicio de IA no disponible'")

    # ── 5. Seudonimización (R12) ──────────────────────────────────────────
    print("\n5. R12 · Seudonimización: el payload enviado al modelo")
    r = analizar(medico, leve)
    cuerpo = r.json() if r.status_code == 201 else {}
    rid_leve = cuerpo.get("id")
    revisar("analizar responde 201 con {id, estado: 'pendiente'}",
            r.status_code == 201 and cuerpo.get("estado") == "pendiente" and rid_leve is not None, r.text[:150])
    if rid_leve:
        rep = requests.get(f"{API}/ia/reportes/{rid_leve}", headers=medico).json()
        revisar("el reporte nace en estado pendiente",
                rep.get("estado") == "pendiente", rep.get("estado"))
        revisar("el reporte trae probabilidad, nivel, crítico y versión del modelo",
                isinstance(rep.get("probabilidad"), float) and rep.get("nivel") in ("urgente", "no_urgente")
                and isinstance(rep.get("critico"), bool) and rep.get("version_modelo"), rep)
        revisar("la probabilidad del reporte coincide con la del modelo",
                abs(rep.get("probabilidad", 9) - perfiles["p_leve"]) < 1e-3,
                (rep.get("probabilidad"), perfiles["p_leve"]))
        resp = requests.get(f"{API}/ia/reportes/{rid_leve}/payload", headers=medico)
        payload = resp.json().get("payload", resp.json()) if resp.status_code == 200 else {}
        revisar("el payload se puede consultar (200)", resp.status_code == 200 and bool(payload), resp.status_code)
        revisar("el payload trae SOLO el id seudónimo y las 14 variables clínicas",
                set(payload) == {"id_seudonimo", *CAMPOS_CLINICOS}, sorted(set(payload) ^ {"id_seudonimo", *CAMPOS_CLINICOS}))
        revisar("es el payload exacto: coincide con los valores que se enviaron",
                all(iguales(payload.get(k), leve.get(k)) for k in CAMPOS_CLINICOS),
                {k: (payload.get(k), leve.get(k)) for k in CAMPOS_CLINICOS if not iguales(payload.get(k), leve.get(k))})
        texto = json.dumps(payload, ensure_ascii=False).lower()
        revisar("ningún dato personal canario (nombre, documento, EPS, teléfono) aparece en el payload",
                not any(len(str(v)) > 3 and str(v).lower() in texto for v in canario.values()),
                [k for k, v in canario.items() if len(str(v)) > 3 and str(v).lower() in texto])
        revisar("el payload no tiene campos de identidad (nombre, documento, correo, teléfono, dirección)",
                not (set(payload) & set(PROHIBIDOS)), set(payload) & set(PROHIBIDOS))
        seud = payload.get("id_seudonimo", "")
        revisar("el seudónimo tiene el formato p-<12 hex> y no es el id ni el documento",
                re.fullmatch(r"p-[0-9a-f]{12}", seud) and seud not in (str(pid), canario["documento"]), seud)
        revisar("un paciente NO consulta el payload (403)",
                requests.get(f"{API}/ia/reportes/{rid_leve}/payload", headers=paciente).status_code == 403)
        revisar("un contable NO consulta el payload (403)",
                requests.get(f"{API}/ia/reportes/{rid_leve}/payload", headers=contable).status_code == 403)

        # ── 6. Aprobación clínica (R10) ──────────────────────────────────
        print("\n6. R10 · Aprobación clínica del reporte")
        pend = requests.get(f"{API}/ia/reportes", headers=medico, params={"paciente_id": pid, "estado": "pendiente"}).json()
        revisar("el filtro por paciente y estado lista el reporte pendiente",
                any(x["id"] == rid_leve for x in pend) and all(x["estado"] == "pendiente" for x in pend), pend[:1])
        revisar("un paciente NO ve la lista de reportes (403)",
                requests.get(f"{API}/ia/reportes", headers=paciente).status_code == 403)
        revisar("un paciente NO aprueba (403)",
                requests.post(f"{API}/ia/reportes/{rid_leve}/aprobar", headers=paciente).status_code == 403)
        revisar("un contable NO aprueba (403)",
                requests.post(f"{API}/ia/reportes/{rid_leve}/aprobar", headers=contable).status_code == 403)
        revisar("un reporte inexistente da 404",
                requests.post(f"{API}/ia/reportes/99999999/aprobar", headers=medico).status_code == 404)
        antes = requests.get(f"{API}/ia/reportes/{rid_leve}", headers=medico).json().get("estado")
        revisar("los intentos rechazados no cambiaron el estado", antes == "pendiente", antes)

        r = requests.post(f"{API}/ia/reportes/{rid_leve}/aprobar", headers=medico,
                          json={"observaciones": "Prueba P2: coincide con la clínica"})
        revisar("el médico aprueba con observaciones (200, estado aprobado)",
                r.status_code == 200 and r.json().get("estado") == "aprobado", r.text[:150])
        revisar("una segunda aprobación da 409",
                requests.post(f"{API}/ia/reportes/{rid_leve}/aprobar", headers=medico).status_code == 409)
        revisar("rechazar un reporte ya aprobado da 409",
                requests.post(f"{API}/ia/reportes/{rid_leve}/rechazar", headers=medico).status_code == 409)
        revisar("el reporte queda aprobado",
                requests.get(f"{API}/ia/reportes/{rid_leve}", headers=medico).json().get("estado") == "aprobado")

        # ── 7. Factura automática (R19) ──────────────────────────────────
        print("\n7. R19 · Al aprobar se crea la factura sola")
        fac = factura_de(rid_leve)
        if fac == "sin_docker":
            omitir("no se pudo consultar MongoDB dentro de Docker (docker compose exec api)")
        else:
            revisar("al aprobar, el reactor crea la factura en MongoDB con el reporte_id del reporte",
                    isinstance(fac, dict) and fac.get("reporte_id") == rid_leve, fac)

        # ── 8. Alerta crítica (R20) ──────────────────────────────────────
        print("\n8. R20 · Alerta crítica: llega en vivo, queda activa y se reconoce")
        alertas0 = requests.get(f"{API}/alertas", headers=medico).json()
        revisar("un análisis NO crítico no genera alerta para ese paciente",
                not any(a.get("paciente_id") == pid for a in alertas0),
                [a for a in alertas0 if a.get("paciente_id") == pid][:1])

        escucha = EscuchaSSE("medico@huv.gov.co", ENV["DEMO_MEDICO_PASSWORD"])
        r = analizar(medico, grave)
        rid_critico = r.json().get("id") if r.status_code == 201 else None
        revisar("el análisis del paciente crítico responde 201", rid_critico is not None, r.text[:150])
        if rid_critico:
            rep_c = requests.get(f"{API}/ia/reportes/{rid_critico}", headers=medico).json()
            revisar("el modelo lo marca como crítico (probabilidad por encima del umbral crítico)",
                    rep_c.get("critico") is True and rep_c.get("probabilidad", 0) >= info.get("umbral_critico", 0.9),
                    rep_c)
            revisar("la probabilidad del reporte coincide con la del modelo",
                    abs(rep_c.get("probabilidad", 9) - perfiles["p_grave"]) < 1e-3,
                    (rep_c.get("probabilidad"), perfiles["p_grave"]))
            ev = escucha.esperar(lambda e: e.get("paciente_id") == pid and
                                 any(s in e.get("tipo", "").lower() for s in ("alerta", "critic", "crític")), 10)
            revisar("el evento llega EN VIVO al médico por el canal (tipo con 'alerta' o 'crítico' y paciente_id)",
                    ev is not None, "no llegó ningún evento en 10 s")
            alertas = requests.get(f"{API}/alertas", headers=medico).json()
            la_alerta = next((a for a in alertas if a.get("paciente_id") == pid and a.get("estado") == "activa"), None)
            revisar("la alerta queda ACTIVA y visible para el clínico", la_alerta is not None,
                    [a for a in alertas if a.get("paciente_id") == pid][:1])
            revisar("un paciente NO ve las alertas (403)", requests.get(f"{API}/alertas", headers=paciente).status_code == 403)
            revisar("un contable NO ve las alertas (403)", requests.get(f"{API}/alertas", headers=contable).status_code == 403)

            # ── 9. Rechazo (R10): no se borra y no genera factura ────────
            print("\n9. R10 · Rechazo: el reporte se conserva como evidencia")
            revisor = especialista or medico
            r = requests.post(f"{API}/ia/reportes/{rid_critico}/rechazar", headers=revisor,
                              json={"observaciones": "Prueba P2: no concuerda con la clínica"})
            revisar("rechaza con observaciones (200, estado rechazado) "
                    + ("— lo hace el especialista" if especialista else "— lo hace el médico"),
                    r.status_code == 200 and r.json().get("estado") == "rechazado", r.text[:150])
            revisar("rechazar de nuevo da 409",
                    requests.post(f"{API}/ia/reportes/{rid_critico}/rechazar", headers=medico).status_code == 409)
            revisar("aprobar un reporte ya rechazado da 409",
                    requests.post(f"{API}/ia/reportes/{rid_critico}/aprobar", headers=medico).status_code == 409)
            conservado = requests.get(f"{API}/ia/reportes/{rid_critico}", headers=medico)
            revisar("el reporte rechazado NO se borra (sigue consultable, estado rechazado)",
                    conservado.status_code == 200 and conservado.json().get("estado") == "rechazado", conservado.status_code)
            lista = requests.get(f"{API}/ia/reportes", headers=medico, params={"paciente_id": pid}).json()
            fila = next((x for x in lista if x["id"] == rid_critico), {})
            revisar("sus observaciones quedan guardadas", "no concuerda" in (fila.get("observaciones") or ""), fila)
            fac_rech = factura_de(rid_critico, intentos=2)
            if fac_rech == "sin_docker":
                omitir("no se pudo consultar MongoDB para confirmar que el rechazo no genera factura")
            else:
                revisar("un reporte rechazado NO genera factura", fac_rech is None, fac_rech)

            if la_alerta:
                r = requests.post(f"{API}/alertas/{la_alerta['id']}/reconocer", headers=medico,
                                  json={"nota": "Prueba P2: alerta atendida"})
                revisar("el clínico reconoce la alerta con nota (2xx)", r.status_code // 100 == 2, r.status_code)

        # ── 10. Auditoría (R13) ──────────────────────────────────────────
        print("\n10. R13 · Auditoría de análisis, aprobación y rechazo")

        def acciones_de(rid):
            filas = requests.get(f"{API}/auditoria", headers=admin,
                                 params={"tabla": "reportes_ia", "registro_id": rid}).json()
            return {f["accion"] for f in filas}, filas

        acc, filas = acciones_de(rid_leve)
        revisar("el análisis y la aprobación del reporte quedan en la auditoría",
                {"analisis_ia", "reporte_aprobado"} <= acc, acc)
        revisar("cada fila trae usuario, acción, recurso y fecha",
                filas and all(f.get(k) for f in filas for k in ("usuario", "accion", "recurso", "fecha")), filas[:1])
        if rid_critico:
            acc_c, _ = acciones_de(rid_critico)
            revisar("el análisis y el rechazo quedan en la auditoría", {"analisis_ia", "reporte_rechazado"} <= acc_c, acc_c)
        revisar("un médico NO consulta la auditoría (403)", requests.get(f"{API}/auditoria", headers=medico).status_code == 403)

        # ── 11. Historia clínica (R10/R11) ───────────────────────────────
        print("\n11. R10/R11 · Historia clínica: solo reportes aprobados")
        if not RUTA_HISTORIA:
            omitir("completa RUTA_HISTORIA al inicio del script (cópiala de tu tester.yaml)")
        else:
            r = requests.get(f"{API}{RUTA_HISTORIA.format(id=pid)}", headers=medico)
            hist = r.json() if r.status_code == 200 else {}
            ids = {x.get("id") for x in hist.get("reportes_ia", [])}
            revisar("la historia responde 200 y trae la lista reportes_ia", r.status_code == 200 and "reportes_ia" in hist,
                    (r.status_code, list(hist)))
            revisar("el reporte APROBADO aparece en la historia", rid_leve in ids, ids)
            revisar("el reporte RECHAZADO no aparece en la historia", rid_critico not in ids, ids)
            revisar("un contable NO ve la historia clínica (403)",
                    requests.get(f"{API}{RUTA_HISTORIA.format(id=pid)}", headers=contable).status_code == 403)
            if RUTA_HISTORIA_PDF:
                r = requests.get(f"{API}{RUTA_HISTORIA_PDF.format(id=pid)}", headers=medico)
                revisar("la historia en PDF responde application/pdf",
                        r.status_code == 200 and "application/pdf" in r.headers.get("Content-Type", ""),
                        (r.status_code, r.headers.get("Content-Type")))

# ── 12. Imágenes (R06) ─────────────────────────────────────────────────────
print("\n12. R06 · Imágenes del paciente en el PACS")
if not RUTA_IMAGENES:
    omitir("completa RUTA_IMAGENES al inicio del script (cópiala de tu tester.yaml)")
else:
    pacientes = requests.get(f"{API}/pacientes", headers=medico).json()
    encontrada = None
    for p in pacientes[:10]:
        r = requests.get(f"{API}{RUTA_IMAGENES.format(id=p['id'])}", headers=medico)
        lista = r.json().get("imagenes", r.json()) if r.status_code == 200 else []
        if lista:
            encontrada = (p["id"], lista)
            break
    revisar("la lista de imágenes de un paciente trae al menos una", encontrada is not None,
            "ninguno de los 10 primeros pacientes tiene imágenes")
    if encontrada:
        item = encontrada[1][0]
        revisar("cada imagen trae url o id", all(("url" in i or "id" in i) for i in encontrada[1]), item)
        if "url" in item:
            url = item["url"] if item["url"].startswith("http") else API[:-4] + item["url"]
            d = requests.get(url, headers=medico)
            revisar("la imagen se descarga (200 y con contenido)", d.status_code == 200 and len(d.content) > 1000,
                    (d.status_code, len(d.content)))
        else:
            omitir("la imagen trae solo id: completa la ruta de descarga para probarla")
    revisar("un contable NO ve las imágenes (403)", not pacientes or requests.get(
        f"{API}{RUTA_IMAGENES.format(id=pacientes[0]['id'])}", headers=contable).status_code in (401, 403))

# ── 13. Reentrenar (solo con --entrenar) ───────────────────────────────────
print("\n13. R22 · Reentrenar como admin")
if not ENTRENAR:
    omitir("corre el script con --entrenar para probarlo (cambia la versión del modelo guardado)")
else:
    antes = requests.get(f"{API}/modelo", headers=admin).json()
    r = requests.post(f"{API}/entrenar", headers=admin, timeout=200)
    nuevo = r.json() if r.status_code == 200 else {}
    revisar("el admin reentrena (200) y recibe versión y métricas",
            r.status_code == 200 and "version" in nuevo and "metricas" in nuevo, (r.status_code, r.text[:150]))
    revisar("la versión cambió", nuevo.get("version") != antes.get("version"), (antes.get("version"), nuevo.get("version")))
    ma, mn = antes.get("metricas", {}), nuevo.get("metricas", {})
    revisar("las métricas se reproducen (semillas fijas): mismo AUC de prueba y de validación",
            abs(ma.get("auc_prueba", 0) - mn.get("auc_prueba", 1)) < 0.005 and abs(ma.get("auc_cv", 0) - mn.get("auc_cv", 1)) < 0.005,
            (ma.get("auc_prueba"), mn.get("auc_prueba"), ma.get("auc_cv"), mn.get("auc_cv")))
    revisar("/modelo muestra la versión nueva", requests.get(f"{API}/modelo", headers=admin).json().get("version") == nuevo.get("version"))
    acc = {a["accion"] for a in requests.get(f"{API}/auditoria?tabla=modelo", headers=admin).json()}
    revisar("el reentrenamiento queda en la auditoría", "modelo_entrenado" in acc, acc)

print(f"\nResultado: {ok_total} OK, {fallas} FALLA")
sys.exit(1 if fallas else 0)