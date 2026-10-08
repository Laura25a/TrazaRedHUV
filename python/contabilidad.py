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
from fastapi import APIRouter

# Útiles ya listos para este módulo:
# from auth import db_mongo, con_zona, requiere_rol, registrar_auditoria, get_db
# coleccion_facturas = db_mongo["facturas"]

router = APIRouter(tags=["Contabilidad"])
