"""Sella en PostgreSQL los cambios y su intención de auditar escrita en Mongo."""
from contextlib import closing
from datetime import datetime, timezone
from time import sleep
from uuid import uuid4

import psycopg2
import psycopg2.extras
from pymongo.errors import PyMongoError

from auth import PG_CONNECTION_STRING, db_mongo, registrar_auditoria


CAMPO = "auditoria_pendiente"
COLECCIONES = ("gestiones_contacto", "facturas")


def preparar_indices():
    for nombre in COLECCIONES:
        db_mongo[nombre].create_index(f"{CAMPO}.id", sparse=True, name="pendientes_de_auditoria")


def evento(actor, accion, tabla, registro_id, detalle, **datos):
    return {"id": str(uuid4()), "actor": actor, "accion": accion, "tabla": tabla,
            "registro_id": registro_id, "detalle": detalle,
            "fecha": datetime.now(timezone.utc), **datos}


def sellar(db, coleccion, oid, *pendientes):
    """Confirma una sola vez y retira la intención solo después del commit."""
    nuevo = None
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for pendiente in pendientes:
            nuevo = None
            asiento = None
            if pendiente["accion"] == "gestion_anotada":
                cur.execute("SELECT * FROM remisiones WHERE id = %s FOR UPDATE;", (pendiente["registro_id"],))
                asiento = cur.fetchone()
            cur.execute("""INSERT INTO auditoria_operaciones_mongo (operacion)
                           VALUES (%s) ON CONFLICT DO NOTHING RETURNING operacion;""", (pendiente["id"],))
            if cur.fetchone():
                if asiento and asiento["activo"]:
                    from registro import avanzar_por_gestion

                    nuevo = avanzar_por_gestion(db, asiento, pendiente)
                registrar_auditoria(db, pendiente["actor"], pendiente["accion"], pendiente["tabla"],
                                    pendiente["registro_id"], pendiente["detalle"], confirmar=False)
    db.commit()
    if not pendientes:
        return nuevo
    try:
        coleccion.update_one({"_id": oid}, {"$pull": {CAMPO: {"id": {"$in": [p["id"] for p in pendientes]}}}})
    except PyMongoError as exc:
        print(f"[auditoría] limpieza pendiente: {type(exc).__name__}")
    return nuevo


def sellar_documento(db, coleccion, oid):
    """Aplica los pendientes en orden aun cuando una solicitud previa perdió el sello."""
    doc = coleccion.find_one({"_id": oid}, {CAMPO: 1})
    return sellar(db, coleccion, oid, *doc.get(CAMPO, ()))


def reintentar():
    for nombre in COLECCIONES:
        coleccion = db_mongo[nombre]
        for doc in coleccion.find({f"{CAMPO}.id": {"$exists": True}}, {CAMPO: 1}).batch_size(50):
            try:
                with closing(psycopg2.connect(PG_CONNECTION_STRING, connect_timeout=5)) as db:
                    sellar(db, coleccion, doc["_id"], *doc[CAMPO])
            except (psycopg2.Error, PyMongoError) as exc:
                print(f"[auditoría] sello pendiente: {type(exc).__name__}")


def iniciar_reintentos():
    while True:
        try:
            reintentar()
        except (psycopg2.Error, PyMongoError) as exc:
            print(f"[auditoría] reintento aplazado: {type(exc).__name__}")
        sleep(15)
