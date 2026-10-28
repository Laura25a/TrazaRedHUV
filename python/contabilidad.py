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
import math
import os
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

import exportes
import libro
from auth import con_zona, db_mongo, get_db, registrar_auditoria, requiere_rol

router = APIRouter(tags=["Contabilidad"])
coleccion_facturas = db_mongo["facturas"]
coleccion_contadores = db_mongo["contadores"]


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
    fila = liquidacion_de(db, remision_id)
    if fila["abierta"]:
        raise HTTPException(409, "La remisión sigue abierta: se factura cuando se cierra el asiento")
    if fila["estado_cobro"] in ("facturada", "pagada", "glosada"):
        raise HTTPException(409, f"Ya tiene la factura {fila['factura'].get('numero')} ({fila['estado_cobro_texto']})")
    ahora = datetime.utcnow()
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
    r = coleccion_facturas.insert_one(doc)
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
    if doc.get("estado_pago") == "anulada":
        raise HTTPException(409, "La factura está anulada")
    if doc.get("estado_pago") == "pagada":
        raise HTTPException(409, "La factura ya está pagada")
    ahora = datetime.utcnow()
    cambios = {"estado_pago": nuevo, f"{nuevo}_en": ahora}
    if motivo:
        cambios[f"motivo_{nuevo}"] = motivo
    coleccion_facturas.update_one({"_id": oid}, {"$set": cambios, "$push": {
        "historial": {"evento": nuevo, "fecha": ahora, "por": usuario["id"], "motivo": motivo}}})
    doc = coleccion_facturas.find_one({"_id": oid})
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
    valor: float
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

    datos = factura.model_dump()
    datos["numero"] = _siguiente_numero()
    datos["fecha"] = datetime.utcnow()
    datos["creado_en"] = datetime.utcnow()
    datos["creado_por"] = usuario["id"]
    resultado = coleccion_facturas.insert_one(datos)
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
    """Crea una factura automática al aprobar un reporte de IA (R19).

    La llama ia.py cuando el clínico aprueba un reporte. No requiere usuario
    (es automática), por eso no valida rol.
    """
    datos = {
        "numero": _siguiente_numero(),
        "paciente_id": paciente_id,
        "servicio": "analisis_ia",
        "valor": valor,
        "moneda": "COP",
        "estado_pago": "pendiente",
        "reporte_id": reporte_id,
        "remision_id": None,
        "fecha": datetime.utcnow(),
        "creado_en": datetime.utcnow(),
        "creado_por": None,
        "automatica": True,
    }
    resultado = coleccion_facturas.insert_one(datos)
    return str(resultado.inserted_id)
