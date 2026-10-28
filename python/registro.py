"""
registro.py — El sistema como REGISTRO de remisiones (rediseño "libro de radicación").

Lo que el usuario ve:
  * Libro de radicación  GET /registro/libro           (+ .csv / .xlsx / .pdf)
  * Formato de remisión  GET /registro/remisiones/{id}/formato
                         (+ constancia.pdf / bitacora.xlsx / fhir.json)
  * Seguimiento paciente GET /registro/mi-seguimiento

Lo que se anota (nunca se edita ni se borra, todo queda en auditoría):
  * POST /registro/remisiones/{id}/gestiones   un intento de contacto. La IA le
    pone la causa, el estado avanza solo (pendiente → en gestión; si la
    institución acepta → aceptada) y se recalcula el pronóstico.
  * POST /registro/remisiones/{id}/cierre      trasladado o rechazada: cierra el
    asiento y lo deja listo para facturar en el libro contable.
  * Las correcciones de datos siguen usando PUT /remisiones/{id}, que ya guarda
    la versión anterior en remisiones_historial; aquí se muestran como eventos.

IA (servicio ml, ver ml/remisiones_modelo.py): la API arma el payload SIN
nombre ni documento (R12, mismo seudónimo de ia.py) y guarda cada pronóstico
en pronosticos_remision para saber, al cierre, si la IA acertó.
"""
import json
import re
from datetime import datetime, timezone
from statistics import median
from typing import Optional

import httpx
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import contabilidad
import exportes
import libro
from auth import ROLES_CLINICOS, get_db, get_usuario_actual, registrar_auditoria, requiere_rol
from causas import CAUSAS, causa_de, causa_por_reglas
from eventos import ids_con_rol, publicar
from ia import ML_URL, seudonimo

router = APIRouter(tags=["Registro de remisiones"])

HUV = "Hospital Universitario del Valle"
ROLES_LIBRO = (*ROLES_CLINICOS, "eps")
ESTADOS_TEXTO = {"pendiente": "Sin gestión", "en_gestion": "En curso", "aceptada": "Aceptada",
                 "rechazada": "Rechazada", "completada": "Trasladado"}
LOINC = {"8867-4": "fc", "8480-6": "pas", "8462-4": "pad", "9279-1": "fr",
         "59408-5": "spo2", "8310-5": "temp", "9269-2": "glasgow"}
GRAVE = re.compile(r"uci|ventilaci|neurocirug|hemodinam|hemodi[aá]lisis|choque|severo|infarto|hemorragia|politrauma",
                   re.IGNORECASE)


def _no_contable(usuario: dict):
    if usuario["rol"] == "contable":
        raise HTTPException(403, "El rol contable no tiene acceso a datos clínicos")


def _asiento(db, usuario: dict, remision_id: int) -> tuple:
    """El asiento enriquecido y su bitácora, respetando lo que cada rol puede ver."""
    _no_contable(usuario)
    filas = libro.asientos(db, usuario, ["r.id = %s"], [remision_id], incluir_inactivas=True)
    if not filas:
        raise HTTPException(404, "Remisión no encontrada")
    return filas[0], libro.cargar_contactos([remision_id])[remision_id]


def _horas_texto(h: Optional[float]) -> str:
    if h is None:
        return "—"
    if h < 1:
        return f"{round(h * 60)} min"
    if h < 48:
        return f"{int(h)} h {round((h - int(h)) * 60):02d} min"
    return f"{h / 24:.1f} días"


# ── IA: pronóstico de la remisión ────────────────────────────────────────────
def _signos(db, remision_id: int) -> list:
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT DISTINCT ON (COALESCE(codigo_loinc, tipo)) tipo, codigo_loinc, valor, unidad,
                          fecha_observacion
                     FROM observaciones WHERE remision_id = %s
                    ORDER BY COALESCE(codigo_loinc, tipo), fecha_observacion DESC, id DESC;""", (remision_id,))
    filas = cur.fetchall()
    cur.close()
    for f in filas:
        f["valor"] = float(f["valor"])
    return filas


def _payload_ia(asiento: dict, contactos: list, signos: list) -> dict:
    """R12: solo seudónimo + variables clínicas/administrativas. Ni nombre ni documento."""
    diag = str(asiento.get("motivo") or "").split(";")[0].strip()
    datos = {
        "id_seudonimo": seudonimo(asiento["paciente_id"]),
        "edad": libro.edad_de(asiento.get("fecha_nacimiento")),
        "genero": asiento.get("genero") if asiento.get("genero") in ("F", "M") else None,
        "eps": asiento.get("eps"),
        "sale_del_huv": HUV.lower() in str(asiento.get("institucion_origen", "")).lower()
                        or "huv" in str(asiento.get("institucion_origen", "")).lower(),
        "convenio_vigente": bool(asiento.get("convenio_vigente")),
        "motivo_grave": bool(GRAVE.search(str(asiento.get("motivo") or ""))),
        "motivo": diag[:500],
        "contactos": [{"fecha": c["fecha"].isoformat() if isinstance(c.get("fecha"), datetime) else c.get("fecha"),
                       "institucion_contactada": c.get("institucion_contactada") or "—",
                       "respuesta": c.get("respuesta") or ""} for c in contactos],
    }
    for s in signos:
        campo = LOINC.get(s.get("codigo_loinc"))
        if campo:
            datos[campo] = s["valor"]
    return datos


def _pedir_pronostico(payload: dict) -> Optional[dict]:
    try:
        r = httpx.post(f"{ML_URL}/remisiones/pronostico", json=payload, timeout=10)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        print(f"[registro] servicio de IA no disponible para el pronóstico: {e}")
        return None


def pronosticar(db, asiento: dict, contactos: list, usuario_id: Optional[int], guardar=True) -> Optional[dict]:
    """Pide el pronóstico y, si cambió, lo deja escrito (es un evento más del registro)."""
    if asiento["estado"] in libro.ESTADOS_CERRADOS:
        return None
    p = _pedir_pronostico(_payload_ia(asiento, contactos, _signos(db, asiento["id"])))
    if p is None or not guardar:
        return p
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT probabilidad_efectiva, contactos, version_modelo FROM pronosticos_remision
                    WHERE remision_id = %s ORDER BY id DESC LIMIT 1;""", (asiento["id"],))
    ultimo = cur.fetchone()
    cambio = (ultimo is None or ultimo["contactos"] != len(contactos)
              or ultimo["version_modelo"] != p["version_modelo"]
              or abs(float(ultimo["probabilidad_efectiva"]) - p["probabilidad_efectiva"]) > 0.005)
    if cambio:
        detalle = {k: p.get(k) for k in ("tiempo_estimado_horas", "instituciones_sugeridas", "causas_bitacora",
                                          "alerta_sin_movimiento", "grupo_motivo")}
        cur.execute("""INSERT INTO pronosticos_remision (remision_id, probabilidad_efectiva, probabilidad_al_radicar,
                              riesgo, momento, contactos, version_modelo, detalle, creado_por)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);""",
                    (asiento["id"], p["probabilidad_efectiva"], p["probabilidad_al_radicar"],
                     p["riesgo_estancamiento"], p["momento"], len(contactos), p["version_modelo"],
                     json.dumps(detalle, default=str), usuario_id))
        db.commit()
    cur.close()
    return p


def clasificar_respuesta(texto: str) -> dict:
    try:
        r = httpx.post(f"{ML_URL}/remisiones/clasificar", json={"texto": texto}, timeout=6)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError:
        c = causa_por_reglas(texto)        # respaldo: las mismas reglas del modelo
        return {"causa": c, "etiqueta": CAUSAS[c]["etiqueta"], "efecto": CAUSAS[c]["efecto"],
                "confianza": None, "origen": "reglas (IA no disponible)"}


# ── Libro de radicación ──────────────────────────────────────────────────────
def _filtrar(asientos: list, vista: str, q: Optional[str]) -> list:
    if vista == "abiertas":
        asientos = [a for a in asientos if a["abierta"]]
    elif vista == "cerradas":
        asientos = [a for a in asientos if not a["abierta"]]
    elif vista == "riesgo":
        asientos = [a for a in asientos if a["abierta"] and (a["sin_movimiento"] or (
            a.get("probabilidad_efectiva") is not None and a["probabilidad_efectiva"] < 0.4))]
    if q:
        t = q.strip().lower()
        asientos = [a for a in asientos if any(t in str(v or "").lower() for v in (
            a["radicado"], a["id"], a.get("paciente_nombre"), a.get("paciente_documento"),
            a.get("institucion_origen"), a.get("institucion_destino"), a.get("eps")))]
    return asientos


def _horas_hasta_aceptacion(asientos: list, bitacoras: dict) -> list:
    horas = []
    for a in asientos:
        if a["estado"] not in ("aceptada", "completada"):
            continue
        acepta = next((c for c in bitacoras.get(a["id"], []) if causa_de(c) == "acepta"), None)
        if acepta and a.get("radicada_en"):
            h = (acepta["fecha"] - a["radicada_en"]).total_seconds() / 3600
            if h >= 0:
                horas.append(h)
    return horas


def resumen_libro(asientos: list) -> dict:
    hoy = datetime.now(exportes.ZONA).date()
    es_hoy = lambda f: bool(f) and f.astimezone(exportes.ZONA).date() == hoy
    abiertas = [a for a in asientos if a["abierta"]]
    bitacoras = libro.cargar_contactos([a["id"] for a in asientos if a["estado"] in ("aceptada", "completada")])
    horas = _horas_hasta_aceptacion(asientos, bitacoras)
    con_ia = [a for a in abiertas if a.get("probabilidad_efectiva") is not None]
    return {
        "total": len(asientos),
        "radicadas_hoy": sum(1 for a in asientos if es_hoy(a.get("radicada_en"))),
        "abiertas": len(abiertas),
        "cerradas": len(asientos) - len(abiertas),
        "cerradas_hoy": sum(1 for a in asientos if es_hoy(a.get("cerrada_en"))),
        "sin_movimiento": sum(1 for a in abiertas if a["sin_movimiento"]),
        "en_riesgo": sum(1 for a in abiertas if a["sin_movimiento"] or (
            a.get("probabilidad_efectiva") is not None and a["probabilidad_efectiva"] < 0.4)),
        "con_pronostico": len(con_ia),
        "mediana_horas_aceptacion": round(median(horas), 2) if horas else None,
        "umbral_sin_movimiento_h": libro.HORAS_SIN_MOVIMIENTO,
        "folio_actual": max((a["folio"] for a in asientos), default=1),
        "asientos_desde": min((a["id"] for a in asientos), default=None),
        "asientos_hasta": max((a["id"] for a in asientos), default=None),
    }


def _libro(db, usuario, vista, q, incluir_inactivas):
    _no_contable(usuario)
    if usuario["rol"] == "paciente":
        raise HTTPException(403, "El paciente consulta su remisión en /registro/mi-seguimiento")
    todos = libro.asientos(db, usuario, incluir_inactivas=incluir_inactivas)
    return todos, _filtrar(todos, vista, q)


@router.get("/registro/libro")
def ver_libro(
    vista: str = Query("abiertas", pattern="^(abiertas|cerradas|riesgo|todas)$"),
    q: Optional[str] = Query(None, max_length=80),
    incluir_inactivas: bool = Query(False),
    limite: int = Query(400, ge=1, le=2000),
    db=Depends(get_db), usuario=Depends(get_usuario_actual),
):
    todos, filas = _libro(db, usuario, vista, q, incluir_inactivas)
    return {"resumen": resumen_libro([a for a in todos if a.get("activo") is not False]),
            "total_filtrado": len(filas), "asientos": filas[:limite]}


COLUMNAS_LIBRO = ["N.° asiento", "Radicado", "Folio", "Radicada", "Paciente", "Documento", "EPS",
                  "Remite", "Receptora", "Intentos", "Instituciones contactadas", "Último movimiento",
                  "Pronóstico IA (% efectiva)", "Estado", "Cerrada", "Asentó"]


def _filas_libro(filas):
    for a in filas:
        um = a.get("ultimo_movimiento") or {}
        yield [a["id"], a["radicado"], a["folio"], a.get("radicada_en"), a.get("paciente_nombre"),
               a.get("paciente_documento"), a.get("eps"), a.get("institucion_origen"), a.get("institucion_destino"),
               a["intentos"], ", ".join(filter(None, a["instituciones_contactadas"])),
               f"{um.get('institucion') or ''}: {um.get('respuesta') or ''}" if um else "",
               round(100 * a["probabilidad_efectiva"]) if a.get("probabilidad_efectiva") is not None else "",
               ESTADOS_TEXTO.get(a["estado"], a["estado"]), a.get("cerrada_en"), a.get("asento_nombre")]


@router.get("/registro/libro.csv")
def libro_csv(vista: str = Query("todas"), q: Optional[str] = None, db=Depends(get_db),
              usuario=Depends(get_usuario_actual)):
    _, filas = _libro(db, usuario, vista, q, False)
    registrar_auditoria(db, usuario["id"], "exporte", "remisiones", None, f"libro CSV ({vista}, {len(filas)} asientos)")
    return exportes.archivo(exportes.a_csv(COLUMNAS_LIBRO, _filas_libro(filas)),
                            f"libro_radicacion_{datetime.now():%Y%m%d}.csv", "csv")


@router.get("/registro/libro.xlsx")
def libro_xlsx(vista: str = Query("todas"), q: Optional[str] = None, db=Depends(get_db),
               usuario=Depends(get_usuario_actual)):
    todos, filas = _libro(db, usuario, vista, q, False)
    r = resumen_libro(todos)
    contenido = exportes.a_xlsx([
        {"titulo": "Libro de radicación",
         "encabezado": ["Libro de radicación de remisiones — HUV",
                        f"Vista: {vista} · {len(filas)} asientos · generado {datetime.now(exportes.ZONA):%d/%m/%Y %H:%M}"],
         "columnas": COLUMNAS_LIBRO, "filas": list(_filas_libro(filas)),
         "anchos": [9, 18, 7, 17, 28, 14, 13, 30, 30, 9, 40, 50, 12, 12, 17, 22]},
        {"titulo": "Resumen", "columnas": ["Indicador", "Valor"],
         "filas": [["Asientos", r["total"]], ["Abiertos", r["abiertas"]], ["Cerrados", r["cerradas"]],
                   [f"Sin movimiento > {r['umbral_sin_movimiento_h']} h", r["sin_movimiento"]],
                   ["En riesgo (IA o sin movimiento)", r["en_riesgo"]],
                   ["Mediana de horas hasta aceptación", r["mediana_horas_aceptacion"]]], "anchos": [38, 14]},
    ])
    registrar_auditoria(db, usuario["id"], "exporte", "remisiones", None, f"libro Excel ({vista}, {len(filas)} asientos)")
    return exportes.archivo(contenido, f"libro_radicacion_{datetime.now():%Y%m%d}.xlsx", "xlsx")


@router.get("/registro/libro.pdf")
def libro_pdf(vista: str = Query("abiertas"), q: Optional[str] = None, db=Depends(get_db),
              usuario=Depends(get_usuario_actual)):
    _, filas = _libro(db, usuario, vista, q, False)
    datos = []
    for a in filas[:600]:
        marcas = " ".join({"x": "×", "o": "–", "e": "?", "v": "•"}.get(m["marca"], "·") for m in a["marcas"])
        datos.append([f"{a['id']:06d}", exportes.texto(a.get("radicada_en")),
                      f"{a.get('paciente_nombre')}\n{a.get('paciente_documento')} · {a.get('eps')}",
                      f"{a.get('institucion_origen')}\n→ {a.get('institucion_destino')}",
                      f"{marcas}\n{a['intentos']} intentos",
                      f"{round(100 * a['probabilidad_efectiva'])} %" if a.get("probabilidad_efectiva") is not None else "—",
                      ESTADOS_TEXTO.get(a["estado"], a["estado"]).upper(), a.get("asento_nombre") or "—"])
    folios = sorted({a["folio"] for a in filas})
    contenido = exportes.tabla_pdf(
        "Libro de radicación de remisiones",
        f"Folios {folios[0] if folios else '—'}–{folios[-1] if folios else '—'} · vista {vista} · {len(filas)} asientos",
        ["N.°", "Fecha y hora", "Paciente", "Remite → Receptora", "Gestiones", "IA", "Cierre", "Asentó"],
        datos, anchos=[1.4, 2.4, 4.6, 6.2, 2.4, 1.2, 2.2, 2.6],
        nota="× no acepta · – sin respuesta · ? en evaluación / pide documentos · • acepta. "
             "Los asientos no se tachan: toda corrección es un asiento nuevo que remite al original.")
    registrar_auditoria(db, usuario["id"], "exporte", "remisiones", None, f"libro PDF ({vista}, {len(filas)} asientos)")
    return exportes.archivo(contenido, f"libro_radicacion_{datetime.now():%Y%m%d}.pdf", "pdf")


# ── Formato de la remisión ───────────────────────────────────────────────────
ACCIONES_EVENTO = {
    "remision_creada": "Remisión radicada", "soft_edit": "Corrección de datos", "soft_delete": "Asiento anulado",
    "restaurar": "Asiento restaurado", "cambio_estado": "Cambio de estado", "cierre_remision": "Cierre del asiento",
    "factura_creada": "Factura emitida", "factura_pagada": "Factura pagada", "factura_glosada": "Factura glosada",
    "factura_anulada": "Factura anulada",
}


def _correcciones(db, asiento: dict) -> list:
    """Cada edición guardó la versión ANTERIOR: comparando versiones seguidas se
    sabe exactamente qué campo cambió, de qué valor a qué valor."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT h.dato_anterior, h.modificado_en AT TIME ZONE current_setting('TimeZone') AS fecha,
                          u.nombre AS por
                     FROM remisiones_historial h LEFT JOIN usuarios u ON u.id = h.modificado_por
                    WHERE h.remision_id = %s ORDER BY h.id;""", (asiento["id"],))
    filas = cur.fetchall()
    cur.close()
    campos = {"institucion_origen": "Institución que remite", "institucion_destino": "Institución receptora",
              "fecha_solicitud": "Fecha de solicitud", "motivo": "Motivo", "estado": "Estado",
              "convenio_vigente": "Convenio vigente", "paciente_id": "Paciente"}
    salida = []
    for i, h in enumerate(filas):
        antes = h["dato_anterior"] if isinstance(h["dato_anterior"], dict) else json.loads(h["dato_anterior"])
        despues = (filas[i + 1]["dato_anterior"] if i + 1 < len(filas) else asiento)
        if not isinstance(despues, dict):
            despues = json.loads(despues)
        cambios = [{"campo": etq, "antes": str(antes.get(k)), "despues": str(despues.get(k))}
                   for k, etq in campos.items() if str(antes.get(k)) != str(despues.get(k))]
        # Los cambios de estado ya salen como su propio evento ("Cambio de estado")
        solo_estado = bool(cambios) and all(c["campo"] == "Estado" for c in cambios)
        salida.append({"fecha": h["fecha"], "por": h["por"], "cambios": cambios, "solo_estado": solo_estado})
    return salida


def _auditoria_de(db, remision_id: int) -> list:
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT a.accion, a.detalle, a.fecha AT TIME ZONE current_setting('TimeZone') AS fecha,
                          u.nombre AS por, u.rol
                     FROM auditoria a LEFT JOIN usuarios u ON u.id = a.usuario_id
                    WHERE a.tabla = 'remisiones' AND a.registro_id = %s ORDER BY a.id;""", (remision_id,))
    filas = cur.fetchall()
    cur.close()
    return filas


def _pronosticos_de(db, remision_id: int) -> list:
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT probabilidad_efectiva, probabilidad_al_radicar, riesgo, momento, contactos,
                          version_modelo, detalle, creado_en AT TIME ZONE current_setting('TimeZone') AS fecha
                     FROM pronosticos_remision WHERE remision_id = %s ORDER BY id;""", (remision_id,))
    filas = cur.fetchall()
    cur.close()
    for f in filas:
        f["probabilidad_efectiva"] = float(f["probabilidad_efectiva"])
        if f["probabilidad_al_radicar"] is not None:
            f["probabilidad_al_radicar"] = float(f["probabilidad_al_radicar"])
    return filas


def _bitacora(contactos: list) -> list:
    salida = []
    for i, c in enumerate(contactos, start=1):
        k = causa_de(c)
        salida.append({"orden": i, "fecha": c.get("fecha"), "institucion": c.get("institucion_contactada"),
                       "medio": c.get("medio"), "respuesta": c.get("respuesta"), "por": c.get("contactado_por"),
                       "causa": k, "causa_etiqueta": CAUSAS[k]["etiqueta"], "efecto": CAUSAS[k]["efecto"],
                       "marca": CAUSAS[k]["marca"], "causa_origen": c.get("causa_origen") or "reglas"})
    return salida


def _eventos(asiento, bitacora, correcciones, auditoria, pronosticos) -> list:
    ev = [{"tipo": "radicacion", "fecha": asiento.get("radicada_en"), "titulo": "Remisión radicada",
           "detalle": f"{asiento.get('institucion_origen')} → {asiento.get('institucion_destino')}",
           "por": asiento.get("asento_nombre")}]
    for b in bitacora:
        ev.append({"tipo": "contacto", "fecha": b["fecha"], "titulo": f"Contacto #{b['orden']} · {b['institucion']}",
                   "detalle": f"{b['medio'] or ''} · {b['respuesta']}", "por": b["por"], "causa": b["causa"],
                   "efecto": b["efecto"]})
    for c in correcciones:
        if c.get("solo_estado"):
            continue
        ev.append({"tipo": "correccion", "fecha": c["fecha"], "titulo": "Corrección de datos",
                   "detalle": "; ".join(f"{x['campo']}: «{x['antes']}» → «{x['despues']}»" for x in c["cambios"]) or "Sin cambios de campos",
                   "por": c["por"]})
    for a in auditoria:
        if a["accion"] in ("cambio_estado", "cierre_remision", "soft_delete", "restaurar") or a["accion"].startswith("factura_"):
            ev.append({"tipo": "estado" if a["accion"] in ("cambio_estado", "cierre_remision") else "admin",
                       "fecha": a["fecha"], "titulo": ACCIONES_EVENTO.get(a["accion"], a["accion"]),
                       "detalle": a["detalle"], "por": a["por"]})
    for p in pronosticos:
        ev.append({"tipo": "ia", "fecha": p["fecha"],
                   "titulo": f"Pronóstico IA: {round(100 * p['probabilidad_efectiva'])} % de terminar efectiva",
                   "detalle": f"Riesgo {p['riesgo']} · con {p['contactos']} contacto(s) · modelo {p['version_modelo']}",
                   "por": "TrazaRed · modelo de remisiones"})
    ev.sort(key=lambda e: e["fecha"] or datetime.min.replace(tzinfo=timezone.utc))
    return ev


def formato_remision(db, usuario: dict, remision_id: int, recalcular=True) -> dict:
    a, contactos = _asiento(db, usuario, remision_id)
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM pacientes WHERE id = %s;", (a["paciente_id"],))
    p = cur.fetchone() or {}
    cur.close()
    pron = pronosticar(db, a, contactos, usuario["id"]) if (recalcular and usuario["rol"] != "eps") else None
    pronosticos = _pronosticos_de(db, remision_id)
    if pron is None and pronosticos:
        ult = pronosticos[-1]
        pron = {"probabilidad_efectiva": ult["probabilidad_efectiva"],
                "probabilidad_al_radicar": ult["probabilidad_al_radicar"], "riesgo_estancamiento": ult["riesgo"],
                "momento": ult["momento"], "version_modelo": ult["version_modelo"], "guardado_en": ult["fecha"],
                **(ult["detalle"] or {})}
    bitacora = _bitacora(contactos)
    correcciones = _correcciones(db, a)
    auditoria = _auditoria_de(db, remision_id)
    diag, _, req = str(a.get("motivo") or "").partition(";")
    resultado_ia = None
    if not a["abierta"] and pronosticos:
        efectiva = a["estado"] == "completada"
        ult = pronosticos[-1]
        resultado_ia = {"acerto": (ult["probabilidad_efectiva"] >= 0.5) == efectiva,
                        "ultimo_pronostico": ult["probabilidad_efectiva"]}
    costos = None
    if usuario["rol"] == "admin":
        costos = contabilidad.liquidacion_de(db, remision_id)
    rol = usuario["rol"]
    puede_anotar = rol in ("admin", "medico") and a["abierta"] and a.get("activo") is not False
    return {
        "remision": a,
        "paciente": {"id": p.get("id"), "nombre": p.get("nombre"), "documento": p.get("documento"),
                     "genero": {"F": "F", "M": "M"}.get(p.get("genero"), p.get("genero")),
                     "edad": libro.edad_de(p.get("fecha_nacimiento")), "fecha_nacimiento": p.get("fecha_nacimiento"),
                     "eps": p.get("eps"), "telefono": p.get("telefono"), "tipo_sangre": p.get("tipo_sangre"),
                     "alergias": p.get("alergias")},
        "diagnostico": diag.strip() or "—",
        "requiere": (req.strip()[:1].upper() + req.strip()[1:]) if req.strip() else "—",
        "signos": _signos(db, remision_id),
        "bitacora": bitacora,
        "correcciones": [c for c in correcciones if not c.get("solo_estado")],
        "eventos": _eventos(a, bitacora, correcciones, auditoria, pronosticos),
        "pronostico": pron,
        "pronosticos": pronosticos,
        "resultado_ia": resultado_ia,
        "costos": costos,
        "cierre": {"institucion_acepta": a.get("institucion_acepta"), "cerrada_en": a.get("cerrada_en"),
                   "resultado": {"completada": "efectiva", "rechazada": "fallida"}.get(a["estado"]),
                   "resultado_texto": {"completada": "Efectiva · paciente trasladado",
                                       "rechazada": "Fallida · no se logró el traslado"}.get(a["estado"])},
        "responsable": next((c.get("contactado_por") for c in reversed(contactos) if c.get("contactado_por")), None),
        "estado_texto": ESTADOS_TEXTO.get(a["estado"], a["estado"]),
        "tiempo_texto": (f"{_horas_texto(a.get('horas_abierta'))} {'abierta' if a['abierta'] else 'en total'}"),
        "permisos": {
            "anotar": puede_anotar,
            "cerrar": puede_anotar,
            "corregir": rol in ("admin", "medico") and a.get("activo") is not False
                        and (rol == "admin" or a.get("creado_por") == usuario["id"]),
            "anular": rol in ("admin", "medico") and a.get("activo") is not False
                      and (rol == "admin" or a.get("creado_por") == usuario["id"]),
            "restaurar": rol == "admin" and a.get("activo") is False,
            "anexos_clinicos": rol in ROLES_CLINICOS,
            "costos": rol == "admin",
        },
    }


@router.get("/registro/remisiones/{remision_id}/formato")
def ver_formato(remision_id: int, db=Depends(get_db), usuario=Depends(get_usuario_actual)):
    if usuario["rol"] == "paciente":
        raise HTTPException(403, "El paciente consulta su remisión en /registro/mi-seguimiento")
    return formato_remision(db, usuario, remision_id)


@router.get("/registro/remisiones/{remision_id}/constancia.pdf")
def constancia(remision_id: int, db=Depends(get_db), usuario=Depends(get_usuario_actual)):
    if usuario["rol"] == "paciente":
        raise HTTPException(403, "No disponible para el rol paciente")
    f = formato_remision(db, usuario, remision_id, recalcular=False)
    if f.get("costos"):
        f["costos"]["estado_cobro_texto"] = f["costos"]["estado_cobro_texto"]
    pdf = exportes.constancia_pdf(f)
    registrar_auditoria(db, usuario["id"], "exporte", "remisiones", remision_id, "constancia PDF")
    return exportes.archivo(pdf, f"{f['remision']['radicado']}_constancia.pdf", "pdf")


@router.get("/registro/remisiones/{remision_id}/bitacora.xlsx")
def bitacora_xlsx(remision_id: int, db=Depends(get_db), usuario=Depends(get_usuario_actual)):
    if usuario["rol"] == "paciente":
        raise HTTPException(403, "No disponible para el rol paciente")
    f = formato_remision(db, usuario, remision_id, recalcular=False)
    r = f["remision"]
    contenido = exportes.a_xlsx([
        {"titulo": "Bitácora", "encabezado": [f"Bitácora de gestión · {r['radicado']}",
                                              f"{f['paciente']['nombre']} · {r.get('institucion_origen')} → {r.get('institucion_destino')}"],
         "columnas": ["N.°", "Fecha y hora", "Institución", "Medio", "Respuesta anotada", "Causa", "Clasificó", "Registró"],
         "filas": [[b["orden"], b["fecha"], b["institucion"], b["medio"], b["respuesta"], b["causa_etiqueta"],
                    b["causa_origen"], b["por"]] for b in f["bitacora"]],
         "anchos": [6, 17, 34, 12, 50, 26, 12, 24]},
        {"titulo": "Recorrido", "columnas": ["Fecha y hora", "Evento", "Detalle", "Por"],
         "filas": [[e["fecha"], e["titulo"], e["detalle"], e["por"]] for e in f["eventos"]],
         "anchos": [17, 40, 70, 28]},
    ])
    registrar_auditoria(db, usuario["id"], "exporte", "remisiones", remision_id, "bitácora Excel")
    return exportes.archivo(contenido, f"{r['radicado']}_bitacora.xlsx", "xlsx")


ESTADO_FHIR = {"pendiente": "draft", "en_gestion": "active", "aceptada": "active",
               "rechazada": "revoked", "completada": "completed"}


@router.get("/registro/remisiones/{remision_id}/fhir.json")
def fhir_bundle(remision_id: int, db=Depends(get_db), usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    """La remisión como Bundle FHIR R4: Patient + ServiceRequest + una
    Communication por cada contacto de la bitácora + las Observation de triage."""
    f = formato_remision(db, usuario, remision_id, recalcular=False)
    r, p = f["remision"], f["paciente"]
    iso = lambda d: d.isoformat() if isinstance(d, datetime) else (str(d) if d else None)
    pac_ref = f"urn:uuid:paciente-{p['id']}"
    sr_ref = f"urn:uuid:remision-{r['id']}"
    entradas = [
        {"fullUrl": pac_ref, "resource": {
            "resourceType": "Patient",
            "identifier": [{"system": "https://www.registraduria.gov.co/cc", "value": p["documento"]}],
            "name": [{"text": p["nombre"]}],
            "gender": {"F": "female", "M": "male"}.get(p.get("genero"), "unknown"),
            **({"birthDate": iso(p["fecha_nacimiento"])} if p.get("fecha_nacimiento") else {})}},
        {"fullUrl": sr_ref, "resource": {
            "resourceType": "ServiceRequest",
            "identifier": [{"system": "https://trazared.huv/radicado", "value": r["radicado"]}],
            "status": ESTADO_FHIR.get(r["estado"], "unknown"), "intent": "order",
            "category": [{"text": "Referencia y contrarreferencia"}],
            "code": {"text": f["requiere"]},
            "reasonCode": [{"text": f["diagnostico"]}],
            "subject": {"reference": pac_ref, "display": p["nombre"]},
            "authoredOn": iso(r.get("radicada_en")),
            "requester": {"display": r.get("institucion_origen")},
            "performer": [{"display": r.get("institucion_acepta") or r.get("institucion_destino")}],
            "insurance": [{"display": p.get("eps")}],
            "note": [{"text": f"Convenio vigente con destino: {'sí' if r.get('convenio_vigente') else 'no'}"}]}},
    ]
    for b in f["bitacora"]:
        entradas.append({"fullUrl": f"urn:uuid:contacto-{r['id']}-{b['orden']}", "resource": {
            "resourceType": "Communication", "status": "completed",
            "basedOn": [{"reference": sr_ref}], "subject": {"reference": pac_ref},
            "sent": iso(b["fecha"]), "medium": [{"text": b["medio"] or ""}],
            "recipient": [{"display": b["institucion"]}], "sender": {"display": b["por"] or ""},
            "reasonCode": [{"text": b["causa_etiqueta"]}],
            "payload": [{"contentString": b["respuesta"]}]}})
    for i, s in enumerate(f["signos"], start=1):
        entradas.append({"fullUrl": f"urn:uuid:obs-{r['id']}-{i}", "resource": {
            "resourceType": "Observation", "status": "final",
            "code": {"coding": [{"system": "http://loinc.org", "code": s.get("codigo_loinc"), "display": s["tipo"]}]},
            "subject": {"reference": pac_ref}, "effectiveDateTime": iso(s.get("fecha_observacion")),
            "valueQuantity": {"value": s["valor"], "unit": s.get("unidad"), "system": "http://unitsofmeasure.org"}}})
    bundle = {"resourceType": "Bundle", "type": "collection",
              "identifier": {"system": "https://trazared.huv/radicado", "value": r["radicado"]},
              "timestamp": datetime.now(exportes.ZONA).isoformat(timespec="seconds"), "entry": entradas}
    registrar_auditoria(db, usuario["id"], "exporte", "remisiones", remision_id, "Bundle FHIR")
    return exportes.archivo(json.dumps(bundle, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
                            f"{r['radicado']}_fhir.json", "json")


# ── Anotar en el registro ────────────────────────────────────────────────────
class GestionIn(BaseModel):
    institucion_contactada: str = Field(min_length=2, max_length=200)
    medio: str = Field("telefono", pattern="^(telefono|plataforma|correo|presencial)$")
    respuesta: str = Field(min_length=2, max_length=500)
    contactado_por: Optional[str] = Field(None, max_length=120)


def _cambiar_estado(db, usuario, a: dict, nuevo: str, motivo: str, extra_sql="", extra_vals=()):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM remisiones WHERE id = %s;", (a["id"],))
    actual = cur.fetchone()
    cur.execute("INSERT INTO remisiones_historial (remision_id, dato_anterior, modificado_por) VALUES (%s, %s, %s);",
                (a["id"], json.dumps(dict(actual), default=str), usuario["id"]))
    cur.execute(f"UPDATE remisiones SET estado = %s{extra_sql} WHERE id = %s;", (nuevo, *extra_vals, a["id"]))
    db.commit()
    cur.close()
    registrar_auditoria(db, usuario["id"], "cambio_estado", "remisiones", a["id"],
                        f"{ESTADOS_TEXTO.get(a['estado'])} → {ESTADOS_TEXTO.get(nuevo)} · {motivo}")


@router.post("/registro/remisiones/{remision_id}/gestiones", status_code=201)
def anotar_gestion(remision_id: int, g: GestionIn, db=Depends(get_db),
                   usuario=Depends(requiere_rol("admin", "medico"))):
    a, contactos = _asiento(db, usuario, remision_id)
    if not a["abierta"] or a.get("activo") is False:
        raise HTTPException(409, "El asiento está cerrado: no se le pueden anotar gestiones")
    k = clasificar_respuesta(g.respuesta)
    ahora = datetime.now(timezone.utc)
    contacto = {"fecha": ahora, "medio": g.medio, "institucion_contactada": g.institucion_contactada.strip(),
                "contactado_por": (g.contactado_por or usuario["nombre"]).strip(), "respuesta": g.respuesta.strip(),
                "causa": k["causa"], "causa_origen": k["origen"], "causa_confianza": k.get("confianza"),
                "registrado_por": usuario["id"], "registrado_en": ahora}
    col = libro.coleccion_gestiones
    doc = col.find_one({"remision_id": remision_id})
    if doc:
        col.update_one({"_id": doc["_id"]}, {"$push": {"contactos": contacto}, "$set": {"actualizado_en": ahora}})
    else:
        col.insert_one({"remision_id": remision_id, "contactos": [contacto], "actualizado_en": ahora})
    registrar_auditoria(db, usuario["id"], "gestion_anotada", "remisiones", remision_id,
                        f"{contacto['institucion_contactada']} · {k['etiqueta']}")

    # El estado avanza solo con lo que dice la bitácora (y queda en historial y auditoría)
    nuevo = None
    if k["causa"] == "acepta" and a["estado"] in ("pendiente", "en_gestion"):
        _cambiar_estado(db, usuario, a, "aceptada", f"{contacto['institucion_contactada']} acepta",
                        ", institucion_acepta = %s", (contacto["institucion_contactada"],))
        nuevo = "aceptada"
        if a.get("creado_por"):
            publicar([a["creado_por"]], {"tipo": "remision_aceptada", "remision_id": remision_id,
                                         "paciente_id": a["paciente_id"],
                                         "mensaje": f"{a['radicado']}: {contacto['institucion_contactada']} aceptó al paciente"})
    elif a["estado"] == "pendiente":
        _cambiar_estado(db, usuario, a, "en_gestion", "primera gestión anotada")
        nuevo = "en_gestion"

    a, contactos = _asiento(db, usuario, remision_id)
    pron = pronosticar(db, a, contactos, usuario["id"])
    return {"contacto": {**contacto, "registrado_en": ahora}, "clasificacion": k,
            "estado": nuevo or a["estado"], "estado_cambio": nuevo is not None, "pronostico": pron}


class CierreIn(BaseModel):
    resultado: str = Field(pattern="^(trasladado|rechazada)$")
    institucion_acepta: Optional[str] = Field(None, max_length=200)
    nota: Optional[str] = Field(None, max_length=300)


@router.post("/registro/remisiones/{remision_id}/cierre")
def cerrar_asiento(remision_id: int, c: CierreIn, db=Depends(get_db),
                   usuario=Depends(requiere_rol("admin", "medico"))):
    a, contactos = _asiento(db, usuario, remision_id)
    if not a["abierta"] or a.get("activo") is False:
        raise HTTPException(409, "El asiento ya está cerrado")
    nuevo = "completada" if c.resultado == "trasladado" else "rechazada"
    institucion = (c.institucion_acepta or a.get("institucion_acepta") or "").strip() or None
    if nuevo == "completada" and not institucion:
        raise HTTPException(422, "Indica la institución que recibió al paciente")
    _cambiar_estado(db, usuario, a, nuevo, c.nota or ("paciente trasladado" if nuevo == "completada" else "sin institución receptora"),
                    ", cerrada_en = now(), institucion_acepta = COALESCE(%s, institucion_acepta)", (institucion,))
    pronosticos = _pronosticos_de(db, remision_id)
    acierto = None
    if pronosticos:
        acierto = (pronosticos[-1]["probabilidad_efectiva"] >= 0.5) == (nuevo == "completada")
    registrar_auditoria(db, usuario["id"], "cierre_remision", "remisiones", remision_id,
                        f"{a['radicado']} · {ESTADOS_TEXTO[nuevo]}"
                        + (f" · IA {'acertó' if acierto else 'no acertó'}" if acierto is not None else ""))
    publicar(ids_con_rol(db, "contable"), {"tipo": "remision_cerrada", "remision_id": remision_id,
                                           "paciente_id": a["paciente_id"],
                                           "mensaje": f"{a['radicado']} cerrada ({ESTADOS_TEXTO[nuevo]}): lista para facturar"})
    return {"id": remision_id, "radicado": a["radicado"], "estado": nuevo, "ia_acerto": acierto}


@router.post("/registro/remisiones/{remision_id}/pronostico")
def recalcular_pronostico(remision_id: int, db=Depends(get_db),
                          usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    a, contactos = _asiento(db, usuario, remision_id)
    p = pronosticar(db, a, contactos, usuario["id"])
    if p is None:
        raise HTTPException(503 if a["abierta"] else 409,
                            "El servicio de IA no está disponible" if a["abierta"] else "El asiento está cerrado")
    return p


class TextoIn(BaseModel):
    texto: str = Field(min_length=1, max_length=500)


@router.post("/registro/ia/clasificar")
def ia_clasificar(t: TextoIn, usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    """Vista previa de la causa mientras se escribe la respuesta (no guarda nada)."""
    return clasificar_respuesta(t.texto)


@router.get("/registro/ia/resumen")
def ia_resumen(db=Depends(get_db), usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    """El modelo (métricas del servicio ml) + cómo le ha ido en este libro."""
    try:
        r = httpx.get(f"{ML_URL}/remisiones/modelo", timeout=8)
        r.raise_for_status()
        modelo = r.json()
    except httpx.HTTPError:
        modelo = None
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT r.id, r.estado, p.probabilidad_efectiva
                     FROM remisiones r
                     JOIN LATERAL (SELECT probabilidad_efectiva FROM pronosticos_remision
                                    WHERE remision_id = r.id ORDER BY id DESC LIMIT 1) p ON TRUE
                    WHERE r.activo = TRUE;""")
    filas = cur.fetchall()
    cur.close()
    cerradas = [f for f in filas if f["estado"] in libro.ESTADOS_CERRADOS]
    aciertos = sum(1 for f in cerradas if (float(f["probabilidad_efectiva"]) >= 0.5) == (f["estado"] == "completada"))
    abiertas = [f for f in filas if f["estado"] not in libro.ESTADOS_CERRADOS]
    asientos = [a for a in libro.asientos(db, usuario, ["r.estado IN ('pendiente','en_gestion','aceptada')"])
                if a.get("probabilidad_efectiva") is not None]
    asientos.sort(key=lambda a: a["probabilidad_efectiva"])
    return {
        "modelo": modelo,
        "en_libro": {"con_pronostico": len(filas), "abiertas": len(abiertas), "cerradas_evaluadas": len(cerradas),
                     "aciertos": aciertos,
                     "tasa_acierto": round(aciertos / len(cerradas), 3) if cerradas else None,
                     "en_riesgo": sum(1 for f in abiertas if float(f["probabilidad_efectiva"]) < 0.4)},
        "mas_riesgo": [{"id": a["id"], "radicado": a["radicado"], "paciente_nombre": a.get("paciente_nombre"),
                        "institucion_destino": a.get("institucion_destino"), "eps": a.get("eps"),
                        "probabilidad_efectiva": a["probabilidad_efectiva"], "intentos": a["intentos"],
                        "horas_abierta": a.get("horas_abierta")} for a in asientos[:12]],
    }


@router.post("/registro/ia/recalcular")
def ia_recalcular(db=Depends(get_db), usuario=Depends(requiere_rol("admin"))):
    """Pronostica TODAS las remisiones abiertas (útil después de cargar el dataset
    o de reentrenar). Las que no cambian no se vuelven a escribir."""
    abiertas = libro.asientos(db, usuario, ["r.estado IN ('pendiente','en_gestion','aceptada')"])
    hechos = fallos = 0
    for a in abiertas:
        p = pronosticar(db, a, libro.cargar_contactos([a["id"]])[a["id"]], usuario["id"])
        if p is None:
            fallos += 1
        else:
            hechos += 1
    registrar_auditoria(db, usuario["id"], "pronosticos_recalculados", "modelo", None,
                        f"{hechos} remisiones abiertas · {fallos} sin respuesta de la IA")
    return {"recalculadas": hechos, "sin_respuesta": fallos}


@router.post("/registro/ia/entrenar")
def ia_entrenar(db=Depends(get_db), usuario=Depends(requiere_rol("admin"))):
    try:
        r = httpx.post(f"{ML_URL}/remisiones/entrenar", timeout=300)
        r.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(503, "El servicio de IA no está disponible")
    datos = r.json()
    registrar_auditoria(db, usuario["id"], "modelo_entrenado", "modelo", None, f"remisiones {datos.get('version')}")
    return datos


# ── Seguimiento del paciente ─────────────────────────────────────────────────
FRASES = {
    "acepta": "{inst} aceptó recibirte.",
    "sin_cama": "{inst} no tenía cama o servicio disponible; se sigue buscando.",
    "sin_especialista": "{inst} no tenía el especialista disponible; se sigue buscando.",
    "problema_eps": "{inst} pidió un trámite con tu EPS; el hospital lo está gestionando.",
    "sin_respuesta": "Se intentó comunicar con {inst} y no hubo respuesta todavía.",
    "pide_documentos": "{inst} pidió documentos de tu historia clínica; ya se están enviando.",
    "en_espera": "{inst} está revisando tu caso.",
    "otra": "Se habló con {inst}.",
}


@router.get("/registro/mi-seguimiento", dependencies=[Depends(requiere_rol("paciente"))])
def mi_seguimiento(db=Depends(get_db), usuario=Depends(get_usuario_actual)):
    salida = []
    for a in libro.asientos(db, usuario):
        contactos = libro.cargar_contactos([a["id"]])[a["id"]]
        um = contactos[-1] if contactos else None
        paso = {"pendiente": 1, "en_gestion": 2, "aceptada": 3, "completada": 4, "rechazada": 2}[a["estado"]]
        salida.append({
            "radicado": a["radicado"], "estado": a["estado"], "estado_texto": ESTADOS_TEXTO[a["estado"]],
            "radicada_en": a.get("radicada_en"), "cerrada_en": a.get("cerrada_en"),
            "horas_abierta": a.get("horas_abierta"), "tiempo_texto": _horas_texto(a.get("horas_abierta")),
            "paso": paso, "instituciones_contactadas": len(a["instituciones_contactadas"]),
            "institucion_acepta": a.get("institucion_acepta"),
            "ultimo_movimiento": ({"fecha": um.get("fecha"),
                                   "texto": FRASES[causa_de(um)].format(inst=um.get("institucion_contactada") or "La institución")}
                                  if um else None),
        })
    return salida


# ── IA automática: nadie tiene que dar clic en "Recalcular" ─────────────────
# Al arrancar la API (y cada PRONOSTICO_CADA_MIN minutos) se pronostican las
# remisiones abiertas que no tengan pronóstico o cuyo pronóstico sea de otra
# versión del modelo. Además, cada gestión anotada y cada formato abierto ya
# recalculan su pronóstico en el momento. Se corre en un hilo aparte.
import os as _os
import threading as _threading
import time as _time


def _ml_listo(espera_max=240) -> bool:
    fin = _time.time() + espera_max
    while _time.time() < fin:
        try:
            r = httpx.get(f"{ML_URL}/remisiones/modelo", timeout=5)
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        _time.sleep(5)
    return False


def pronosticar_pendientes() -> dict:
    """Pronostica las abiertas sin pronóstico vigente. Devuelve cuántas hizo."""
    import psycopg2
    from auth import PG_CONNECTION_STRING
    try:
        version = httpx.get(f"{ML_URL}/remisiones/modelo", timeout=8).json()["version"]
    except Exception:
        return {"hechas": 0, "error": "servicio de IA no disponible"}
    db = psycopg2.connect(PG_CONNECTION_STRING)
    hechas = fallos = 0
    try:
        sistema = {"rol": "admin", "id": None}
        abiertas = libro.asientos(db, sistema, ["r.estado IN ('pendiente','en_gestion','aceptada')"])
        for a in abiertas:
            if a.get("version_modelo") == version:
                continue                      # ya tiene pronóstico de este modelo
            p = pronosticar(db, a, libro.cargar_contactos([a["id"]])[a["id"]], None)
            hechas += p is not None
            fallos += p is None
        if hechas:
            registrar_auditoria(db, None, "pronosticos_automaticos", "modelo", None,
                                f"{hechas} remisiones abiertas · modelo {version}")
    finally:
        db.close()
    return {"hechas": hechas, "fallos": fallos, "version": version}


def _ciclo_automatico():
    if not _ml_listo():
        print("[IA] el servicio ml no respondió; los pronósticos se harán al anotar gestiones")
        return
    cada = int(_os.getenv("PRONOSTICO_CADA_MIN", "30"))
    while True:
        try:
            r = pronosticar_pendientes()
            print(f"[IA] pronósticos automáticos: {r}")
        except Exception as e:
            print(f"[IA] error en los pronósticos automáticos: {e}")
        if cada <= 0:
            return
        _time.sleep(cada * 60)


def iniciar_pronosticos_automaticos():
    _threading.Thread(target=_ciclo_automatico, daemon=True, name="pronosticos-ia").start()
