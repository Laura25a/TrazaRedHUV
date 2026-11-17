"""
contabilidad.py — Libro contable POR RADICADO (rediseño del registro; antes R09).

Antes: facturas sueltas por paciente (consulta, análisis de IA, remisión) que
el contable creaba a mano.

Ahora: cada remisión del libro de radicación ACUMULA sus costos sola, a partir
de lo que ya quedó registrado en la bitácora:
  * estancia en el HUV mientras se gestiona el traslado (horas, urgencias o UCI)
  * cada gestión de contacto anotada
  * el traslado en ambulancia (básica o medicalizada) cuando se completa
Al cerrarse el asiento queda "por facturar" a la EPS del paciente; el contable
solo factura, registra glosas y pagos. El recorrido del cobro es:

    acumulando (abierta) → por facturar (cerrada) → facturada → pagada
                                                              ↘ glosada → pagada
                                                  anulada → vuelve a "por facturar"

Las facturas siguen en MongoDB (colección "facturas"), ahora con remision_id,
radicado, número FV-xxxxx y la lista de conceptos. Las facturas viejas y las
automáticas de análisis de IA (R19) se siguen listando igual en /facturas.

R03: el contable NO ve datos clínicos. El libro contable muestra paciente,
documento y EPS (necesarios para cobrar), nunca motivo, diagnóstico ni signos.

TARIFAS: son valores de REFERENCIA del proyecto (no son el tarifario real del
HUV). Se pueden cambiar con variables de entorno sin tocar el código.
"""
import json
from contextlib import closing
import math
import os
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

import exportes
import libro
import psycopg2
from auth import PG_CONNECTION_STRING, con_zona, db_mongo, get_db, registrar_auditoria, requiere_rol

router = APIRouter(tags=["Contabilidad"])
coleccion_facturas = db_mongo["facturas"]
coleccion_contadores = db_mongo["contadores"]

ESTADOS_VIGENTES = ["pendiente", "pagada", "glosada"]


def preparar_indices_facturas():
    """Refuerzo en Mongo: una factura vigente por servicio y referencia.

    No borra ni corrige duplicados históricos. Si existen, el índice falla y
    exige conciliarlos antes del despliegue, en lugar de escoger un cobro.
    """
    for servicio, referencia in (("remision", "remision_id"), ("analisis_ia", "reporte_id")):
        coleccion_facturas.create_index(
            [(referencia, 1), ("servicio", 1)], unique=True,
            name=f"uq_factura_vigente_{servicio}",
            partialFilterExpression={"servicio": servicio, referencia: {"$type": "number"},
                                     "estado_pago": {"$in": ESTADOS_VIGENTES}},
        )


def _insertar_factura(datos):
    try:
        return coleccion_facturas.insert_one(datos)
    except DuplicateKeyError:
        raise HTTPException(409, "El servicio ya tiene una factura vigente") from None


def _tarifa(nombre: str, defecto: int) -> int:
    try:
        return int(os.getenv(f"TARIFA_{nombre.upper()}", defecto))
    except ValueError:
        return defecto


TARIFAS = {
    "estancia_urgencias_hora": _tarifa("estancia_urgencias_hora", 38_000),
    "estancia_uci_hora": _tarifa("estancia_uci_hora", 165_000),
    "gestion_contacto": _tarifa("gestion_contacto", 12_000),
    "traslado_basico": _tarifa("traslado_basico", 420_000),
    "traslado_medicalizado": _tarifa("traslado_medicalizado", 980_000),
}
TOPE_HORAS_ESTANCIA = 72       # una remisión abierta no acumula más de 3 días de estancia

ESTADOS_COBRO = {
    "acumulando": "Acumulando",
    "por_facturar": "Por facturar",
    "facturada": "Facturada",
    "glosada": "Glosada",
    "pagada": "Pagada",
    "anulada": "Anulada",
}


# ── Liquidación de una remisión ───────────────────────────────────────────────
def _es_uci(asiento: dict) -> bool:
    t = f"{asiento.get('institucion_origen', '')} {asiento.get('motivo', '')}".lower()
    return "uci" in t or "ventilación" in t or "ventilacion" in t


def _aware(f):
    if isinstance(f, datetime) and f.tzinfo is None:
        return f.replace(tzinfo=timezone.utc)
    return f


def liquidar(asiento: dict, contactos: list) -> dict:
    """Conceptos y total de una remisión con lo que hay hoy en el registro."""
    inicio = _aware(asiento.get("radicada_en"))
    fin = _aware(asiento.get("cerrada_en")) or libro.ahora()
    horas = max(0.0, (fin - inicio).total_seconds() / 3600) if inicio else 0.0
    tope = horas > TOPE_HORAS_ESTANCIA
    horas = min(horas, TOPE_HORAS_ESTANCIA)
    horas_cobro = math.ceil(horas * 2) / 2          # se cobra por media hora
    uci = _es_uci(asiento)
    tarifa_h = TARIFAS["estancia_uci_hora" if uci else "estancia_urgencias_hora"]
    conceptos = [{
        "codigo": "EST-UCI" if uci else "EST-URG",
        "concepto": f"Estancia en {'UCI' if uci else 'urgencias'} en espera de traslado"
                    + (f" (tope {TOPE_HORAS_ESTANCIA} h)" if tope else ""),
        "cantidad": horas_cobro, "unidad": "h", "valor_unitario": tarifa_h,
        "valor": round(horas_cobro * tarifa_h),
    }]
    if contactos:
        conceptos.append({
            "codigo": "GES-REF", "concepto": "Gestión de referencia (contactos registrados)",
            "cantidad": len(contactos), "unidad": "contactos",
            "valor_unitario": TARIFAS["gestion_contacto"],
            "valor": len(contactos) * TARIFAS["gestion_contacto"],
        })
    if asiento.get("estado") == "completada":
        medicalizado = uci or any("medicaliz" in str(c.get("respuesta", "")).lower() for c in contactos)
        clave = "traslado_medicalizado" if medicalizado else "traslado_basico"
        conceptos.append({
            "codigo": "TRA-MED" if medicalizado else "TRA-BAS",
            "concepto": f"Traslado en ambulancia {'medicalizada' if medicalizado else 'básica'}",
            "cantidad": 1, "unidad": "traslado", "valor_unitario": TARIFAS[clave], "valor": TARIFAS[clave],
        })
    return {
        "conceptos": conceptos,
        "total": sum(c["valor"] for c in conceptos),
        "horas_estancia": round(horas, 2),
        "tope_aplicado": tope,
        "cobro_a": asiento.get("eps") or "—",
        "moneda": "COP",
    }


def _facturas_por_remision(ids: list) -> dict:
    salida = {}
    if not ids:
        return salida
    for f in coleccion_facturas.find({"remision_id": {"$in": ids}, "servicio": "remision"}).sort("fecha", 1):
        salida.setdefault(f["remision_id"], []).append(f)
    return salida


def estado_cobro(asiento: dict, facturas: list) -> tuple:
    vigentes = [f for f in facturas if f.get("estado_pago") != "anulada"]
    if vigentes:
        f = vigentes[-1]
        e = {"pendiente": "facturada", "pagada": "pagada", "glosada": "glosada"}.get(f.get("estado_pago"), "facturada")
        return e, f
    if facturas:                                   # solo anuladas: vuelve a quedar por facturar
        return ("acumulando" if asiento.get("abierta") else "por_facturar"), None
    return ("acumulando" if asiento.get("abierta") else "por_facturar"), None


def fila_contable(asiento: dict, contactos: list, facturas: list) -> dict:
    liq = liquidar(asiento, contactos)
    e, f = estado_cobro(asiento, facturas)
    return {
        "remision_id": asiento["id"],
        "radicado": asiento["radicado"],
        "folio": asiento["folio"],
        "radicada_en": asiento.get("radicada_en"),
        "cerrada_en": asiento.get("cerrada_en"),
        "paciente_id": asiento["paciente_id"],
        "paciente_nombre": asiento.get("paciente_nombre"),
        "paciente_documento": asiento.get("paciente_documento"),
        "eps": asiento.get("eps"),
        "estado": asiento["estado"],
        "abierta": asiento["abierta"],
        "conceptos": (f.get("conceptos") if f else None) or liq["conceptos"],
        "total": float(f["valor"]) if f else liq["total"],
        "total_liquidado": liq["total"],
        "horas_estancia": liq["horas_estancia"],
        "tope_aplicado": liq["tope_aplicado"],
        "cobro_a": liq["cobro_a"],
        "estado_cobro": e,
        "estado_cobro_texto": ESTADOS_COBRO[e],
        "factura": _doc_a_dict(dict(f)) if f else None,
        "facturas_anuladas": sum(1 for x in facturas if x.get("estado_pago") == "anulada"),
    }


def libro_contable(db, eps: Optional[str] = None) -> list:
    # El contable no tiene filtro de rol en el libro de radicación: ve todo,
    # pero solo los campos administrativos (fila_contable no lleva datos clínicos).
    cond, vals = ["r.activo = TRUE"], []
    if eps:
        cond.append("p.eps = %s"); vals.append(eps)
    filas = libro.cargar_remisiones(db, cond, vals)
    bitacoras = libro.cargar_contactos([f["id"] for f in filas])
    facturas = _facturas_por_remision([f["id"] for f in filas])
    salida = []
    for f in filas:
        a = libro.enriquecer(f, bitacoras.get(f["id"], []))
        salida.append(fila_contable(a, bitacoras.get(f["id"], []), facturas.get(f["id"], [])))
    return salida


def liquidacion_de(db, remision_id: int) -> dict:
    filas = libro.cargar_remisiones(db, ["r.id = %s"], [remision_id])
    if not filas:
        raise HTTPException(404, "Remisión no encontrada")
    contactos = libro.cargar_contactos([remision_id])[remision_id]
    a = libro.enriquecer(filas[0], contactos)
    return fila_contable(a, contactos, _facturas_por_remision([remision_id]).get(remision_id, []))


# ── Rutas: libro contable ─────────────────────────────────────────────────────
@router.get("/contabilidad/libro")
def ver_libro_contable(
    estado_cobro_: Optional[str] = Query(None, alias="estado_cobro"),
    eps: Optional[str] = Query(None),
    db=Depends(get_db),
    usuario=Depends(requiere_rol("admin", "contable")),
):
    filas = libro_contable(db, eps)
    if estado_cobro_:
        filas = [f for f in filas if f["estado_cobro"] == estado_cobro_]
    return filas


@router.get("/contabilidad/resumen")
def resumen_contable(db=Depends(get_db), usuario=Depends(requiere_rol("admin", "contable"))):
    filas = libro_contable(db)

    def bloque(cond):
        sel = [f for f in filas if cond(f)]
        return {"valor": sum(f["total"] for f in sel), "n": len(sel)}

    por_eps = {}
    for f in filas:
        e = por_eps.setdefault(f["eps"] or "—", {"eps": f["eps"] or "—", "horas_espera": 0.0, "costo": 0,
                                                    "rechazadas": 0, "remisiones": 0})
        e["horas_espera"] += f["horas_estancia"]
        e["costo"] += f["total"]
        e["remisiones"] += 1
        e["rechazadas"] += int(f["estado"] == "rechazada")
    return {
        "acumulando": bloque(lambda f: f["estado_cobro"] == "acumulando"),
        "por_facturar": bloque(lambda f: f["estado_cobro"] == "por_facturar"),
        "facturado": bloque(lambda f: f["estado_cobro"] == "facturada"),
        "glosado": bloque(lambda f: f["estado_cobro"] == "glosada"),
        "pagado": bloque(lambda f: f["estado_cobro"] == "pagada"),
        "costo_fallidas": bloque(lambda f: f["estado"] == "rechazada"),
        "por_eps": sorted(({**e, "horas_espera": round(e["horas_espera"], 1)} for e in por_eps.values()),
                          key=lambda e: -e["horas_espera"]),
        "tarifas": TARIFAS,
        "tope_horas": TOPE_HORAS_ESTANCIA,
    }


@router.get("/contabilidad/remisiones/{remision_id}")
def liquidacion_remision(remision_id: int, db=Depends(get_db),
                         usuario=Depends(requiere_rol("admin", "contable"))):
    return liquidacion_de(db, remision_id)


def _siguiente_numero() -> str:
    c = coleccion_contadores.find_one_and_update({"_id": "factura"}, {"$inc": {"seq": 1}},
                                                 upsert=True, return_document=ReturnDocument.AFTER)
    return f"FV-{10000 + int(c['seq'])}"


@router.post("/contabilidad/remisiones/{remision_id}/facturar", status_code=201)
def facturar_remision(remision_id: int, db=Depends(get_db),
                      usuario=Depends(requiere_rol("admin", "contable"))):
    """Factura a la EPS lo liquidado de una remisión CERRADA (una sola factura vigente)."""
    with db.cursor() as cur:
        cur.execute("SELECT id FROM remisiones WHERE id = %s FOR UPDATE;", (remision_id,))
    fila = liquidacion_de(db, remision_id)
    if fila["abierta"]:
        raise HTTPException(409, "La remisión sigue abierta: se factura cuando se cierra el asiento")
    with db.cursor() as cur:
        cur.execute("SELECT activo FROM remisiones WHERE id = %s;", (remision_id,))
        if not cur.fetchone()[0]:
            raise HTTPException(409, "No se puede facturar un asiento anulado")
    if fila["estado_cobro"] in ("facturada", "pagada", "glosada"):
        raise HTTPException(409, f"Ya tiene la factura {fila['factura'].get('numero')} ({fila['estado_cobro_texto']})")
    ahora = datetime.now(timezone.utc)
    doc = {
        "numero": _siguiente_numero(),
        "paciente_id": fila["paciente_id"],
        "servicio": "remision",
        "remision_id": remision_id,
        "radicado": fila["radicado"],
        "eps": fila["eps"],
        "conceptos": fila["conceptos"],
        "valor": float(fila["total_liquidado"]),
        "moneda": "COP",
        "estado_pago": "pendiente",
        "reporte_id": None,
        "fecha": ahora, "creado_en": ahora,
        "creado_por": usuario["id"], "automatica": False,
        "historial": [{"evento": "facturada", "fecha": ahora, "por": usuario["id"]}],
    }
    r = _insertar_factura(doc)
    registrar_auditoria(db, usuario["id"], "factura_creada", "remisiones", remision_id,
                        f"{doc['numero']} · {fila['radicado']} · {fila['eps']} · ${doc['valor']:,.0f}")
    return _doc_a_dict(coleccion_facturas.find_one({"_id": r.inserted_id}))


class MotivoIn(BaseModel):
    motivo: str = Field(min_length=3, max_length=300)


def _cambiar_factura(factura_id: str, nuevo: str, usuario: dict, db, motivo: Optional[str] = None):
    try:
        oid = ObjectId(factura_id)
    except Exception:
        raise HTTPException(400, "id inválido")
    doc = coleccion_facturas.find_one({"_id": oid})
    if doc is None:
        raise HTTPException(404, "Factura no encontrada")
    with db.cursor() as cur:
        if doc.get("remision_id") is not None:
            cur.execute("SELECT id FROM remisiones WHERE id = %s FOR UPDATE;", (doc["remision_id"],))
        elif doc.get("reporte_id") is not None:
            cur.execute("SELECT pg_advisory_xact_lock(197731, %s);", (doc["reporte_id"],))
    doc = coleccion_facturas.find_one({"_id": oid})
    if doc.get("estado_pago") == "anulada":
        raise HTTPException(409, "La factura está anulada")
    if doc.get("estado_pago") == "pagada":
        raise HTTPException(409, "La factura ya está pagada")
    ahora = datetime.now(timezone.utc)
    cambios = {"estado_pago": nuevo, f"{nuevo}_en": ahora}
    if motivo:
        cambios[f"motivo_{nuevo}"] = motivo
    doc = coleccion_facturas.find_one_and_update(
        {"_id": oid, "estado_pago": doc.get("estado_pago")},
        {"$set": cambios, "$push": {
            "historial": {"evento": nuevo, "fecha": ahora, "por": usuario["id"], "motivo": motivo}}},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        raise HTTPException(409, "La factura cambió mientras registrabas la acción. Actualiza el libro")
    registrar_auditoria(db, usuario["id"], f"factura_{nuevo}", "remisiones" if doc.get("remision_id") else "facturas",
                        doc.get("remision_id") or doc.get("paciente_id"),
                        f"{doc.get('numero') or oid}{' · ' + motivo if motivo else ''}")
    return _doc_a_dict(doc)


@router.put("/facturas/{factura_id}/glosar")
def glosar_factura(factura_id: str, cuerpo: MotivoIn, db=Depends(get_db),
                   usuario=Depends(requiere_rol("admin", "contable"))):
    """La EPS objetó la factura (glosa): queda con el motivo, se puede pagar después."""
    return _cambiar_factura(factura_id, "glosada", usuario, db, cuerpo.motivo)


@router.put("/facturas/{factura_id}/anular")
def anular_factura(factura_id: str, cuerpo: MotivoIn, db=Depends(get_db),
                   usuario=Depends(requiere_rol("admin", "contable"))):
    """Anula la factura (no se borra): la remisión vuelve a quedar por facturar."""
    return _cambiar_factura(factura_id, "anulada", usuario, db, cuerpo.motivo)


def recibo_de(db, remision_id: int) -> dict:
    """Resumen administrativo del episodio. No es una constancia de pago.

    Importes/conceptos de las facturas emitidas; nunca se recalculan con tarifas
    actuales. Un reporte sin remision_id no se asigna por proximidad a un caso.
    """
    with db.cursor() as cur:
        cur.execute("SELECT id FROM remisiones WHERE id = %s FOR SHARE;", (remision_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, "Remisión no encontrada")
    fila = liquidacion_de(db, remision_id)
    if fila["abierta"]:
        raise HTTPException(409, "Cierra el asiento antes de descargar el recibo del caso")
    if fila["factura"] is None:
        raise HTTPException(409, "Emite la factura del cierre antes de descargar el recibo")
    with db.cursor() as cur:
        cur.execute("SELECT id FROM reportes_ia WHERE remision_id = %s AND estado = 'aprobado';", (remision_id,))
        reportes = [r[0] for r in cur.fetchall()]
    filtro = {"$or": [{"servicio": "remision", "remision_id": remision_id},
                       {"servicio": "analisis_ia", "reporte_id": {"$in": reportes}}]}
    documentos = list(coleccion_facturas.find(filtro).sort([("fecha", 1), ("_id", 1)]))
    facturas, anuladas = [], []
    campos = ("numero", "servicio", "estado_pago", "valor", "moneda", "reporte_id", "fecha",
              "pagada_en", "glosada_en", "anulada_en", "motivo_glosada", "motivo_anulada", "historial")
    for d in documentos:
        d = _doc_a_dict(d)  # Mongo devuelve UTC sin zona; PDF y JSON deben conservarla.
        f = {k: d.get(k) for k in campos}
        f["_id"] = str(d["_id"])
        f["conceptos"] = d.get("conceptos") or [{
            "codigo": "IA-ANAL" if d["servicio"] == "analisis_ia" else "REM-HIST",
            "concepto": "Análisis de IA aprobado" if d["servicio"] == "analisis_ia" else "Remisión (factura histórica)",
            "cantidad": 1, "unidad": "servicio", "valor_unitario": d["valor"], "valor": d["valor"],
        }]
        (anuladas if d.get("estado_pago") == "anulada" else facturas).append(f)
    if any(f["moneda"] != "COP" for f in facturas):
        raise HTTPException(409, "El caso contiene otra moneda. Concilia los cobros antes de emitir el recibo en COP")
    total = sum((Decimal(str(f["valor"])) for f in facturas), Decimal(0))
    pagado = sum((Decimal(str(f["valor"])) for f in facturas if f["estado_pago"] == "pagada"), Decimal(0))
    pendientes = sorted(set(reportes) - {f["reporte_id"] for f in facturas if f["servicio"] == "analisis_ia"})
    estado = ("pendiente_facturacion" if pendientes else "pagada" if all(f["estado_pago"] == "pagada" for f in facturas)
              else "glosada" if any(f["estado_pago"] == "glosada" for f in facturas) else "pendiente")
    return {"radicado": fila["radicado"], "remision_id": remision_id, "paciente_id": fila["paciente_id"],
            "paciente_nombre": fila["paciente_nombre"], "paciente_documento": fila["paciente_documento"],
            "eps": fila["eps"], "cerrada_en": fila["cerrada_en"], "estado_cobro": estado,
            "facturas": facturas, "facturas_anuladas": anuladas, "reportes_sin_factura": pendientes,
            "total": float(total), "pagado": float(pagado), "saldo": float(total - pagado), "moneda": "COP",
            "generado_en": datetime.now(exportes.ZONA),
            "nota": "Resumen del cobro con tarifas de referencia del proyecto. Solo las facturas pagadas acreditan pago. "
                    "Los análisis sin vínculo explícito con este episodio se consultan en Facturas de otros servicios."}


def _recibo_pdf(recibo: dict) -> bytes:
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.6*cm, rightMargin=1.6*cm,
                            topMargin=1.5*cm, bottomMargin=1.6*cm, title=f"Recibo {recibo['radicado']}")
    story = []
    def par(t, estilo=exportes.E_CELDA):
        story.append(exportes._p(t, estilo))
    def dinero(v):
        return f"$ {v:,.2f} COP".replace(",", "X").replace(".", ",").replace("X", ".")
    par("HOSPITAL UNIVERSITARIO DEL VALLE · TrazaRed HUV", exportes.E_SUB)
    par("Recibo desglosado del caso", exportes.E_TITULO)
    par(recibo["radicado"], exportes.E_CELDA_B)
    par(f"Generado: {exportes.texto(recibo['generado_en'])} · Cierre: {exportes.texto(recibo['cerrada_en'])}")
    par(f"Paciente: {recibo['paciente_nombre']} · Documento: {recibo['paciente_documento']}")
    par(f"EPS actual: {recibo['eps']} · Estado del cobro: {recibo['estado_cobro']}")
    for f in recibo["facturas"]:
        story.append(Spacer(1, .4*cm))
        par(f"Factura {f['numero'] or f['_id']} · {f['servicio']} · {f['estado_pago']}", exportes.E_CELDA_B)
        par(f"Emisión: {exportes.texto(f['fecha'])} · Pago: {exportes.texto(f['pagada_en']) or 'Sin pago registrado'}")
        datos = [[exportes._p(c, exportes.E_CELDA_B) for c in ("Concepto", "Cantidad", "Unitario", "Subtotal")]]
        datos += [[exportes._p(v) for v in (f"{c['codigo']} · {c['concepto']}",
                  f"{c['cantidad']} {c['unidad']}", dinero(c['valor_unitario']), dinero(c['valor']))] for c in f['conceptos']]
        ancho = A4[0] - doc.leftMargin - doc.rightMargin
        tabla = Table(datos, colWidths=[ancho*p for p in (.42, .16, .21, .21)], repeatRows=1)
        tabla.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), exportes.LINEA),
                                   ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                   ('LINEBELOW', (0, 0), (-1, -1), .4, exportes.LINEA)]))
        story.append(tabla)
        par(f"Total factura: {dinero(f['valor'])}", exportes.E_CELDA_B)
        if f['motivo_glosada']:
            par(f"Motivo de glosa: {f['motivo_glosada']}")
        for evento in f['historial'] or []:
            par(f"{exportes.texto(evento['fecha'])} · {evento['evento']} · Actor #{evento.get('por') or '-'}"
                + (f" · {evento['motivo']}" if evento.get('motivo') else ''), exportes.E_SUB)
    if recibo['facturas_anuladas']:
        story.append(Spacer(1, .4*cm))
        par("Facturas anuladas (excluidas del total)", exportes.E_CELDA_B)
        for f in recibo['facturas_anuladas']:
            par(f"{f['numero'] or f['_id']} · {dinero(f['valor'])} · {exportes.texto(f['anulada_en'])} · {f['motivo_anulada'] or 'Sin motivo registrado'}")
    story.append(Spacer(1, .4*cm))
    for titulo, valor in (("Total facturado vigente", recibo['total']), ("Pagado", recibo['pagado']), ("Saldo", recibo['saldo'])):
        par(f"{titulo}: {dinero(valor)}", exportes.E_CELDA_B)
    if recibo['reportes_sin_factura']:
        par(f"Facturación incompleta: {len(recibo['reportes_sin_factura'])} análisis aprobados sin factura vigente.", exportes.E_CELDA_B)
    story.append(Spacer(1, .3*cm))
    par(recibo['nota'], exportes.E_PIE)
    doc.build(story, onFirstPage=exportes._pie, onLaterPages=exportes._pie)
    return buf.getvalue()


@router.get("/contabilidad/remisiones/{remision_id}/recibo.json")
def recibo_json(remision_id: int, db=Depends(get_db), usuario=Depends(requiere_rol("admin", "contable"))):
    recibo = recibo_de(db, remision_id)
    contenido = json.dumps(recibo, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    registrar_auditoria(db, usuario["id"], "exporte", "remisiones", remision_id, "recibo individual (JSON)")
    return exportes.archivo(contenido, f"{recibo['radicado']}_recibo.json", "json")


@router.get("/contabilidad/remisiones/{remision_id}/recibo.pdf")
def recibo_pdf(remision_id: int, db=Depends(get_db), usuario=Depends(requiere_rol("admin", "contable"))):
    recibo = recibo_de(db, remision_id)
    contenido = _recibo_pdf(recibo)
    registrar_auditoria(db, usuario["id"], "exporte", "remisiones", remision_id, "recibo individual (PDF)")
    return exportes.archivo(contenido, f"{recibo['radicado']}_recibo.pdf", "pdf")


# ── Descargas del libro contable ──────────────────────────────────────────────
COLUMNAS_LIBRO = ["Radicado", "Folio", "Radicada", "Cerrada", "Paciente", "Documento", "EPS",
                  "Resultado", "Horas de estancia", "Conceptos", "Total (COP)", "Estado del cobro", "Factura"]
RESULTADO = {"pendiente": "En curso", "en_gestion": "En curso", "aceptada": "Aceptada",
             "rechazada": "Rechazada", "completada": "Trasladado"}


def _filas_export(filas):
    for f in filas:
        yield [f["radicado"], f["folio"], f["radicada_en"], f["cerrada_en"], f["paciente_nombre"],
               f["paciente_documento"], f["eps"], RESULTADO.get(f["estado"], f["estado"]), f["horas_estancia"],
               " · ".join(f"{c['concepto']} ({c['cantidad']} {c['unidad']})" for c in f["conceptos"]),
               f["total"], f["estado_cobro_texto"], (f["factura"] or {}).get("numero", "")]


@router.get("/contabilidad/libro.xlsx")
def libro_contable_xlsx(db=Depends(get_db), usuario=Depends(requiere_rol("admin", "contable"))):
    filas = libro_contable(db)
    contenido = exportes.a_xlsx([
        {"titulo": "Libro contable", "encabezado": ["Libro contable por radicado — TrazaRed HUV",
                                                    f"Generado {datetime.now(exportes.ZONA):%d/%m/%Y %H:%M} · valores de referencia del proyecto"],
         "columnas": COLUMNAS_LIBRO, "filas": list(_filas_export(filas)),
         "anchos": [18, 7, 17, 17, 28, 14, 14, 12, 10, 60, 14, 14, 11]},
        {"titulo": "Tarifas", "columnas": ["Concepto", "Valor (COP)"],
         "filas": [[k.replace("_", " "), v] for k, v in TARIFAS.items()], "anchos": [34, 14]},
    ])
    registrar_auditoria(db, usuario["id"], "exporte", "contabilidad", None, "libro contable (Excel)")
    return exportes.archivo(contenido, f"libro_contable_{datetime.now():%Y%m%d}.xlsx", "xlsx")


@router.get("/contabilidad/libro.pdf")
def libro_contable_pdf(db=Depends(get_db), usuario=Depends(requiere_rol("admin", "contable"))):
    filas = libro_contable(db)
    total = sum(f["total"] for f in filas)
    datos = [[f["radicado"], exportes.texto(f["radicada_en"]), f["paciente_nombre"], f["eps"],
              RESULTADO.get(f["estado"], f["estado"]), f"{f['horas_estancia']:.1f} h",
              f"$ {f['total']:,.0f}".replace(",", "."), f["estado_cobro_texto"],
              (f["factura"] or {}).get("numero", "")] for f in filas]
    contenido = exportes.tabla_pdf(
        "Libro contable por radicado",
        f"{len(filas)} asientos · total $ {total:,.0f} COP".replace(",", "."),
        ["Radicado", "Radicada", "Paciente", "EPS", "Resultado", "Estancia", "Total", "Cobro", "Factura"],
        datos, anchos=[3.2, 2.6, 4.4, 2.6, 2.2, 1.8, 2.6, 2.2, 1.8],
        nota="Valores calculados con las tarifas de referencia del proyecto (no es el tarifario oficial del HUV).")
    registrar_auditoria(db, usuario["id"], "exporte", "contabilidad", None, "libro contable (PDF)")
    return exportes.archivo(contenido, f"libro_contable_{datetime.now():%Y%m%d}.pdf", "pdf")


@router.get("/contabilidad/rips.json")
def rips_json(db=Depends(get_db), usuario=Depends(requiere_rol("admin", "contable"))):
    """Facturas de remisiones en una estructura SIMPLIFICADA inspirada en los RIPS
    (un usuario por factura con sus servicios). Sirve para mostrar el
    intercambio; NO está validada contra el esquema oficial del Ministerio."""
    filas = [f for f in libro_contable(db) if f["factura"]]
    contenido = {
        "nota": "Estructura simplificada inspirada en RIPS; no validada contra el esquema oficial.",
        "prestador": "Hospital Universitario del Valle «Evaristo García» E.S.E.",
        "generado": datetime.now(exportes.ZONA).isoformat(timespec="seconds"),
        "facturas": [{
            "numFactura": f["factura"].get("numero"),
            "radicado": f["radicado"],
            "pagador": f["eps"],
            "estado": f["estado_cobro"],
            "usuario": {"numDocumentoIdentificacion": f["paciente_documento"], "nombre": f["paciente_nombre"]},
            "servicios": [{"codigo": c["codigo"], "descripcion": c["concepto"], "cantidad": c["cantidad"],
                           "unidad": c["unidad"], "vrUnitario": c["valor_unitario"], "vrServicio": c["valor"]}
                          for c in f["conceptos"]],
            "vrTotal": f["factura"]["valor"],
        } for f in filas],
    }
    registrar_auditoria(db, usuario["id"], "exporte", "contabilidad", None, "RIPS (JSON)")
    return exportes.archivo(json.dumps(contenido, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
                            f"rips_{datetime.now():%Y%m%d}.json", "json")


# ═════════════════════════════════════════════════════════════════════════════
# Facturas sueltas (lo que ya existía: se conserva para no romper nada)
# ═════════════════════════════════════════════════════════════════════════════
class FacturaCreate(BaseModel):
    paciente_id: int
    servicio: str                    # "consulta" | "analisis_ia" | "remision"
    valor: float = Field(gt=0, allow_inf_nan=False)
    moneda: str = "COP"
    estado_pago: str = "pendiente"   # "pendiente" | "pagada" | "anulada"
    reporte_id: Optional[int] = None # solo para análisis de IA (R19)
    remision_id: Optional[int] = None


SERVICIOS_VALIDOS = ("consulta", "analisis_ia", "remision")
ESTADOS_VALIDOS = ("pendiente", "pagada", "anulada", "glosada")


def _doc_a_dict(doc: dict) -> dict:
    """Convierte ObjectId a str y normaliza fechas con zona."""
    doc["_id"] = str(doc["_id"])
    for k in ("fecha", "creado_en", "pagada_en", "glosada_en", "anulada_en"):
        if k in doc:
            doc[k] = con_zona(doc.get(k))
    for h in doc.get("historial") or []:
        h["fecha"] = con_zona(h.get("fecha"))
    return doc


@router.get("/facturas")
def listar_facturas(
    paciente_id: Optional[int] = Query(None),
    servicio: Optional[str] = Query(None),
    estado_pago: Optional[str] = Query(None),
    remision_id: Optional[int] = Query(None),
    usuario=Depends(requiere_rol("admin", "contable")),
):
    """Lista facturas (solo admin y contable)."""
    filtro = {}
    if paciente_id is not None:
        filtro["paciente_id"] = paciente_id
    if servicio is not None:
        filtro["servicio"] = servicio
    if estado_pago is not None:
        filtro["estado_pago"] = estado_pago
    if remision_id is not None:
        filtro["remision_id"] = remision_id
    documentos = list(coleccion_facturas.find(filtro).sort("fecha", -1))
    return [_doc_a_dict(d) for d in documentos]


@router.get("/facturas/{factura_id}")
def obtener_factura(factura_id: str,
                    usuario=Depends(requiere_rol("admin", "contable"))):
    """Devuelve una factura por su id."""
    try:
        oid = ObjectId(factura_id)
    except Exception:
        raise HTTPException(status_code=400, detail="id inválido")
    doc = coleccion_facturas.find_one({"_id": oid})
    if doc is None:
        raise HTTPException(status_code=404, detail="Factura no encontrada")
    return _doc_a_dict(doc)


@router.post("/facturas", status_code=201)
def crear_factura(
    factura: FacturaCreate,
    db=Depends(get_db),
    usuario=Depends(requiere_rol("admin", "contable")),
):
    """Crea una factura suelta (solo admin y contable). Para remisiones se usa
    POST /contabilidad/remisiones/{id}/facturar, que la arma desde el registro."""
    if factura.servicio not in SERVICIOS_VALIDOS:
        raise HTTPException(status_code=422,
                            detail=f"servicio debe ser uno de: {', '.join(SERVICIOS_VALIDOS)}")
    if factura.estado_pago not in ESTADOS_VALIDOS:
        raise HTTPException(status_code=422,
                            detail=f"estado_pago debe ser uno de: {', '.join(ESTADOS_VALIDOS)}")

    if factura.servicio == "consulta" and (factura.reporte_id is not None or factura.remision_id is not None):
        raise HTTPException(422, "Una consulta no lleva referencias de análisis o remisión")
    if factura.servicio == "analisis_ia" and (factura.reporte_id is None or factura.remision_id is not None):
        raise HTTPException(422, "El análisis IA requiere un reporte y no admite radicado")
    if factura.servicio == "remision" and (factura.remision_id is None or factura.reporte_id is not None):
        raise HTTPException(422, "La factura de remisión requiere un radicado y no admite reporte")

    with db.cursor() as cur:
        cur.execute("SELECT id FROM pacientes WHERE id = %s FOR KEY SHARE;", (factura.paciente_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, "Paciente no encontrado")
        if factura.servicio == "remision":
            cur.execute("SELECT paciente_id FROM remisiones WHERE id = %s;", (factura.remision_id,))
            referencia = cur.fetchone()
            if referencia is None:
                raise HTTPException(404, "Remisión no encontrada")
            if referencia[0] != factura.paciente_id:
                raise HTTPException(422, "La remisión pertenece a otro paciente")
        elif factura.servicio == "analisis_ia":
            cur.execute("SELECT pg_advisory_xact_lock(197731, %s);", (factura.reporte_id,))
            cur.execute("SELECT paciente_id, estado FROM reportes_ia WHERE id = %s FOR SHARE;", (factura.reporte_id,))
            referencia = cur.fetchone()
            if referencia is None:
                raise HTTPException(404, "Reporte no encontrado")
            if referencia[0] != factura.paciente_id:
                raise HTTPException(422, "El reporte pertenece a otro paciente")
            if referencia[1] != "aprobado":
                raise HTTPException(409, "El reporte debe estar aprobado")

    if factura.servicio == "remision":
        if factura.estado_pago != "pendiente" or factura.moneda != "COP":
            raise HTTPException(422, "La factura de remisión se emite pendiente y en COP")
        return facturar_remision(factura.remision_id, db, usuario)
    if factura.servicio == "analisis_ia":
        if coleccion_facturas.find_one({"servicio": "analisis_ia", "reporte_id": factura.reporte_id,
                                       "estado_pago": {"$ne": "anulada"}}):
            raise HTTPException(409, "El análisis ya tiene una factura vigente")

    datos = factura.model_dump()
    datos["numero"] = _siguiente_numero()
    datos["fecha"] = datetime.now(timezone.utc)
    datos["creado_en"] = datetime.now(timezone.utc)
    datos["creado_por"] = usuario["id"]
    resultado = _insertar_factura(datos)
    nuevo = coleccion_facturas.find_one({"_id": resultado.inserted_id})

    registrar_auditoria(
        db, usuario["id"], "factura_creada", "facturas",
        factura.paciente_id,
        f"factura {datos['numero']} · paciente {factura.paciente_id} · {factura.servicio} · ${factura.valor}",
    )
    return _doc_a_dict(nuevo)


@router.put("/facturas/{factura_id}/pagar")
def marcar_pagada(
    factura_id: str,
    db=Depends(get_db),
    usuario=Depends(requiere_rol("admin", "contable")),
):
    """Marca una factura como pagada."""
    return _cambiar_factura(factura_id, "pagada", usuario, db)


# ── Función para facturación automática (R19) ────────────────────────────
def crear_factura_ia(paciente_id: int, reporte_id: int, valor: float = 30000):
    """Emite la factura de un reporte aprobado. Un evento repetido reutiliza la vigente."""
    with closing(psycopg2.connect(PG_CONNECTION_STRING)) as db:
        with db.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(197731, %s);", (reporte_id,))
            existente = coleccion_facturas.find_one({"servicio": "analisis_ia", "reporte_id": reporte_id,
                                                      "estado_pago": {"$ne": "anulada"}})
            if existente:
                return str(existente["_id"])
            cur.execute("""SELECT id FROM reportes_ia
                           WHERE id = %s AND paciente_id = %s AND estado = 'aprobado' FOR SHARE;""",
                        (reporte_id, paciente_id))
            if cur.fetchone() is None:
                raise ValueError("El reporte aprobado no corresponde al paciente")
        ahora = datetime.now(timezone.utc)
        resultado = coleccion_facturas.insert_one({
            "numero": _siguiente_numero(), "paciente_id": paciente_id, "servicio": "analisis_ia",
            "valor": valor, "moneda": "COP", "estado_pago": "pendiente", "reporte_id": reporte_id,
            "remision_id": None, "fecha": ahora, "creado_en": ahora, "creado_por": None,
            "automatica": True, "origen": "automatico",
        })
        registrar_auditoria(db, None, "factura_creada", "facturas", reporte_id,
                            "Factura automática por reporte de IA")
        return str(resultado.inserted_id)
