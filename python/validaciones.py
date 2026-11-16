"""Reglas compartidas por las escrituras clínicas."""
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2.extras
from fastapi import HTTPException


def hoy_bogota():
    return datetime.now(ZoneInfo("America/Bogota")).date()


def institucion_del_catalogo(cur, nombre):
    cur.execute(
        "SELECT nombre FROM instituciones WHERE clave = clave_institucion(%s);",
        (nombre,),
    )
    fila = cur.fetchone()
    if fila is None:
        raise HTTPException(422, "Selecciona una institución del catálogo")
    return fila[0] if isinstance(fila, tuple) else fila["nombre"]


def bloquear_remision(db, remision_id, usuario, exigir_abierta=False):
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM remisiones WHERE id = %s FOR UPDATE;", (remision_id,))
        fila = cur.fetchone()
    if fila is None:
        raise HTTPException(404, "Remisión no encontrada")
    if usuario["rol"] != "admin" and fila["creado_por"] != usuario["id"]:
        raise HTTPException(403, "Solo puedes modificar remisiones que tú creaste")
    if not fila["activo"]:
        raise HTTPException(409, "La remisión está anulada")
    if exigir_abierta and fila["estado"] in ("completada", "rechazada"):
        raise HTTPException(409, "El asiento está cerrado")
    return fila


def exigir_asignacion_clinica(db, paciente_id: int, usuario: dict) -> None:
    """La historia y los anexos requieren un caso a cargo o una remisión interna asignada."""
    if usuario["rol"] not in ("medico", "especialista"):
        return
    usuario_id = usuario["id"]
    with db.cursor(cursor_factory=psycopg2.extensions.cursor) as cur:
        cur.execute(
            """SELECT EXISTS (
                   SELECT 1 FROM remisiones WHERE paciente_id = %s AND activo AND creado_por = %s
                   UNION ALL
                   SELECT 1 FROM remisiones_medicas
                    WHERE paciente_id = %s AND (medico_id = %s OR especialista_id = %s)
               );""", (paciente_id, usuario_id, paciente_id, usuario_id, usuario_id),
        )
        if not cur.fetchone()[0]:
            raise HTTPException(404, "Paciente no encontrado")
