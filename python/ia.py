"""
ia.py — Análisis de IA con aprobación clínica (Corte 2, Persona 2: R07, R10, R12).

Aquí van las rutas de la IA. La API es el "puente" con el servicio de IA (ml):
  * R12 · Capa de seudonimización: antes de llamar al modelo se quitan nombre,
    documento, correo, teléfono y dirección; al modelo solo le llegan las
    variables clínicas y un id seudónimo. El payload enviado se guarda.
  * R10 · Reporte "soft put": el análisis crea el reporte en estado "pendiente";
    el médico o el especialista lo aprueba o lo rechaza; el paciente y el
    contable reciben 403. Solo los aprobados van a la historia clínica.
  * R07 · Ruta de clusters: [{"cluster", "descripcion", "n_reales", "n_sinteticos"}].

Al aprobar un reporte se llama a eventos.publicar() con tipo "reporte_aprobado"
(se crea la factura) y, si el caso es crítico, con tipo "alerta_critica".

Las rutas se escriben con @router.get(...) / @router.post(...): main.py ya
incluye este router, así que quedan publicadas solas.
"""
from fastapi import APIRouter

# Útiles ya listos para este módulo:
# from auth import get_db, requiere_rol, registrar_auditoria
# from eventos import publicar

router = APIRouter(tags=["IA"])
