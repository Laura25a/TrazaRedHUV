"""
contabilidad.py — Facturación en MongoDB (Corte 2, Persona 3: R09).

Aquí van las rutas de facturas: crear y listar, solo para "contable" y
"admin" (el médico recibe 403). Cada factura es un documento de la colección
"facturas" con paciente_id, servicio (consulta, analisis_ia, remision), valor,
fecha y estado de pago. Las facturas automáticas (al aprobar un reporte de IA,
R19) llevan además "reporte_id". Se documenta en docs/CONTABILIDAD.md.

Las rutas se escriben con @router.get(...) / @router.post(...): main.py ya
incluye este router, así que quedan publicadas solas.
"""
from datetime import datetime
from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import db_mongo, con_zona, requiere_rol, registrar_auditoria, get_db, get_usuario_actual

router = APIRouter(tags=["Contabilidad"])
coleccion_facturas = db_mongo["facturas"]


# ── Modelos ──────────────────────────────────────────────────────────────
class FacturaCreate(BaseModel):
    paciente_id: int
    servicio: str                    # "consulta" | "analisis_ia" | "remision"
    valor: float
    moneda: str = "COP"
    estado_pago: str = "pendiente"   # "pendiente" | "pagada" | "anulada"
    reporte_id: Optional[int] = None # solo para análisis de IA (R19)
    remision_id: Optional[int] = None


SERVICIOS_VALIDOS = ("consulta", "analisis_ia", "remision")
ESTADOS_VALIDOS = ("pendiente", "pagada", "anulada")


def _doc_a_dict(doc: dict) -> dict:
    """Convierte ObjectId a str y normaliza fechas con zona."""
    doc["_id"] = str(doc["_id"])
    doc["fecha"] = con_zona(doc.get("fecha"))
    doc["creado_en"] = con_zona(doc.get("creado_en"))
    return doc


# ── Rutas ────────────────────────────────────────────────────────────────
@router.get("/facturas")
def listar_facturas(
    paciente_id: Optional[int] = Query(None),
    servicio: Optional[str] = Query(None),
    estado_pago: Optional[str] = Query(None),
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
    """Crea una factura (solo admin y contable)."""
    if factura.servicio not in SERVICIOS_VALIDOS:
        raise HTTPException(status_code=422,
                            detail=f"servicio debe ser uno de: {', '.join(SERVICIOS_VALIDOS)}")
    if factura.estado_pago not in ESTADOS_VALIDOS:
        raise HTTPException(status_code=422,
                            detail=f"estado_pago debe ser uno de: {', '.join(ESTADOS_VALIDOS)}")

    datos = factura.model_dump()
    datos["fecha"] = datetime.utcnow()
    datos["creado_en"] = datetime.utcnow()
    datos["creado_por"] = usuario["id"]
    resultado = coleccion_facturas.insert_one(datos)
    nuevo = coleccion_facturas.find_one({"_id": resultado.inserted_id})

    registrar_auditoria(
        db, usuario["id"], "factura_creada", "facturas",
        factura.paciente_id,
        f"factura {resultado.inserted_id} · paciente {factura.paciente_id} · {factura.servicio} · ${factura.valor}",
    )
    return _doc_a_dict(nuevo)


@router.put("/facturas/{factura_id}/pagar")
def marcar_pagada(
    factura_id: str,
    db=Depends(get_db),
    usuario=Depends(requiere_rol("admin", "contable")),
):
    """Marca una factura como pagada."""
    try:
        oid = ObjectId(factura_id)
    except Exception:
        raise HTTPException(status_code=400, detail="id inválido")
    resultado = coleccion_facturas.update_one(
        {"_id": oid},
        {"$set": {"estado_pago": "pagada", "pagada_en": datetime.utcnow()}},
    )
    if resultado.matched_count == 0:
        raise HTTPException(status_code=404, detail="Factura no encontrada")
    doc = coleccion_facturas.find_one({"_id": oid})
    registrar_auditoria(db, usuario["id"], "factura_pagada", "facturas",
                        doc['paciente_id'], f"factura {oid} · paciente {doc['paciente_id']}")
    return _doc_a_dict(doc)


# ── Función para facturación automática (R19) ────────────────────────────
def crear_factura_ia(paciente_id: int, reporte_id: int, valor: float = 30000):
    """Crea una factura automática al aprobar un reporte de IA (R19).

    La llama ia.py cuando el clínico aprueba un reporte. No requiere usuario
    (es automática), por eso no valida rol.
    """
    datos = {
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