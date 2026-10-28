"""
remisiones.py — Remisiones, observaciones (signos vitales) y gestiones de contacto.

Lo que ya existía del Corte 1 y la semana 8: listar, ver, crear, editar con
historial, soft delete y restauración, observaciones y la bitácora de
gestiones de contacto en MongoDB.

Corte 2 (Persona 3, R08): aquí se agregan la remisión de médico a especialista,
la bandeja del especialista y la ruta para aceptar. Al remitir y al aceptar se
llama a eventos.publicar() para que avise (R19).
"""
import json
from datetime import date, datetime
from typing import List, Optional

import psycopg2
import psycopg2.extras
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import ROLES_CLINICOS, con_zona, db_mongo, get_db, get_usuario_actual, registrar_auditoria, requiere_rol
from eventos import publicar
import fhir_sync

router = APIRouter(tags=["Remisiones"])


def sin_contable(usuario: dict):
    """R03: las remisiones son datos clínicos; el contable no las ve (403)."""
    if usuario["rol"] == "contable":
        raise HTTPException(status_code=403, detail="El rol contable no tiene acceso a datos clínicos")

class RemisionCreate(BaseModel):
    paciente_id: int
    institucion_origen: str
    institucion_destino: str
    fecha_solicitud: date
    motivo: str
    estado: str
    convenio_vigente: bool


class RemisionOut(BaseModel):
    id: int
    paciente_id: int
    institucion_origen: str
    institucion_destino: str
    fecha_solicitud: date
    motivo: str
    estado: str
    convenio_vigente: bool
    creado_por: Optional[int] = None
    activo: bool


class RemisionPacienteOut(BaseModel):
    id: int
    institucion_destino: str
    fecha_solicitud: date
    estado: str


class ObservacionCreate(BaseModel):
    remision_id: int
    tipo: str
    codigo_loinc: Optional[str] = None
    valor: float
    unidad: Optional[str] = None


class ObservacionOut(BaseModel):
    id: int
    remision_id: int
    tipo: str
    codigo_loinc: Optional[str] = None
    valor: float
    unidad: Optional[str] = None
    fecha_observacion: datetime


@router.get("/remisiones")
def listar_remisiones(
    estado: Optional[str] = Query(None),
    paciente_id: Optional[int] = Query(None),
    incluir_inactivas: bool = Query(False, description="Solo admin: incluye las eliminadas (soft delete)"),
    db=Depends(get_db),
    usuario=Depends(get_usuario_actual),
):
    sin_contable(usuario)
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if usuario["rol"] == "paciente":
        cur.execute(
            """SELECT id, institucion_destino, fecha_solicitud, estado
               FROM remisiones WHERE paciente_id = %s AND activo = TRUE ORDER BY id;""",
            (usuario["paciente_id"],),
        )
        filas = cur.fetchall()
        cur.close()
        return filas

    condiciones = [] if (incluir_inactivas and usuario["rol"] == "admin") else ["r.activo = TRUE"]
    valores = []

    if usuario["rol"] == "eps":
        condiciones.append("p.eps = %s")
        valores.append(usuario["eps_nombre"])
    if estado is not None:
        condiciones.append("r.estado = %s")
        valores.append(estado)
    if paciente_id is not None:
        condiciones.append("r.paciente_id = %s")
        valores.append(paciente_id)

    sql = """SELECT r.id, r.paciente_id, r.institucion_origen, r.institucion_destino,
                     r.fecha_solicitud, r.motivo, r.estado, r.convenio_vigente,
                     r.creado_por, r.activo,
                     p.nombre AS paciente_nombre, p.documento AS paciente_documento, p.eps
              FROM remisiones r JOIN pacientes p ON p.id = r.paciente_id
              WHERE """ + (" AND ".join(condiciones) or "TRUE") + " ORDER BY r.id;"

    cur.execute(sql, tuple(valores))
    filas = cur.fetchall()
    cur.close()
    return filas


@router.get("/remisiones/{remision_id}")
def obtener_remision(remision_id: int, db=Depends(get_db), usuario=Depends(get_usuario_actual)):
    sin_contable(usuario)
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if usuario["rol"] == "paciente":
        cur.execute(
            """SELECT id, institucion_destino, fecha_solicitud, estado
               FROM remisiones WHERE id = %s AND paciente_id = %s AND activo = TRUE;""",
            (remision_id, usuario["paciente_id"]),
        )
        fila = cur.fetchone()
        cur.close()
        if fila is None:
            raise HTTPException(status_code=404, detail="Remisión no encontrada")
        return fila

    cur.execute(
        """SELECT r.id, r.paciente_id, r.institucion_origen, r.institucion_destino,
                  r.fecha_solicitud, r.motivo, r.estado, r.convenio_vigente,
                  r.creado_por, r.activo, p.eps,
                  p.nombre AS paciente_nombre, p.documento AS paciente_documento
           FROM remisiones r JOIN pacientes p ON p.id = r.paciente_id
           WHERE r.id = %s AND (r.activo = TRUE OR %s);""",
        (remision_id, usuario["rol"] == "admin"),
    )
    fila = cur.fetchone()
    cur.close()
    if fila is None:
        raise HTTPException(status_code=404, detail="Remisión no encontrada")
    if usuario["rol"] == "eps" and fila["eps"] != usuario["eps_nombre"]:
        raise HTTPException(status_code=403, detail="Esta remisión no pertenece a tu EPS")
    return fila


@router.post("/remisiones", response_model=RemisionOut, status_code=201,
          dependencies=[Depends(requiere_rol("admin", "medico"))])
def crear_remision(remision: RemisionCreate, db=Depends(get_db), usuario=Depends(get_usuario_actual)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """INSERT INTO remisiones
                   (paciente_id, institucion_origen, institucion_destino,
                    fecha_solicitud, motivo, estado, convenio_vigente, creado_por)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id, paciente_id, institucion_origen, institucion_destino,
                         fecha_solicitud, motivo, estado, convenio_vigente, creado_por, activo;""",
            (remision.paciente_id, remision.institucion_origen, remision.institucion_destino,
             remision.fecha_solicitud, remision.motivo, remision.estado,
             remision.convenio_vigente, usuario["id"]),
        )
    except psycopg2.errors.ForeignKeyViolation:
        db.rollback(); cur.close()
        raise HTTPException(status_code=400, detail="paciente_id no existe")
    except psycopg2.errors.CheckViolation:
        db.rollback(); cur.close()
        raise HTTPException(status_code=422, detail="estado inválido")
    fila = cur.fetchone()
    db.commit()
    cur.close()
    registrar_auditoria(db, usuario["id"], "remision_creada", "remisiones", fila["id"],
                        f"paciente {fila['paciente_id']}")
    return fila


@router.put("/remisiones/{remision_id}", response_model=RemisionOut,
          dependencies=[Depends(requiere_rol("admin", "medico"))])
def actualizar_remision(remision_id: int, remision: RemisionCreate, db=Depends(get_db), usuario=Depends(get_usuario_actual)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM remisiones WHERE id = %s;", (remision_id,))
    actual = cur.fetchone()
    if actual is None:
        cur.close()
        raise HTTPException(status_code=404, detail="Remisión no encontrada")

    if usuario["rol"] == "medico" and actual["creado_por"] != usuario["id"]:
        cur.close()
        raise HTTPException(status_code=403, detail="Solo puedes editar remisiones que tú creaste")

    cur.execute(
        "INSERT INTO remisiones_historial (remision_id, dato_anterior, modificado_por) VALUES (%s, %s, %s);",
        (remision_id, json.dumps(dict(actual), default=str), usuario["id"]),
    )

    try:
        cur.execute(
            """UPDATE remisiones
                  SET paciente_id = %s, institucion_origen = %s, institucion_destino = %s,
                      fecha_solicitud = %s, motivo = %s, estado = %s, convenio_vigente = %s
                WHERE id = %s
                RETURNING id, paciente_id, institucion_origen, institucion_destino,
                          fecha_solicitud, motivo, estado, convenio_vigente, creado_por, activo;""",
            (remision.paciente_id, remision.institucion_origen, remision.institucion_destino,
             remision.fecha_solicitud, remision.motivo, remision.estado,
             remision.convenio_vigente, remision_id),
        )
    except psycopg2.errors.ForeignKeyViolation:
        db.rollback(); cur.close()
        raise HTTPException(status_code=400, detail="paciente_id no existe")
    except psycopg2.errors.CheckViolation:
        db.rollback(); cur.close()
        raise HTTPException(status_code=422, detail="estado inválido")

    fila = cur.fetchone()
    db.commit()
    cur.close()
    registrar_auditoria(db, usuario["id"], "soft_edit", "remisiones", remision_id)
    return fila


@router.delete("/remisiones/{remision_id}", status_code=204,
            dependencies=[Depends(requiere_rol("admin", "medico"))])
def soft_delete_remision(remision_id: int, db=Depends(get_db), usuario=Depends(get_usuario_actual)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT creado_por FROM remisiones WHERE id = %s;", (remision_id,))
    actual = cur.fetchone()
    if actual is None:
        cur.close()
        raise HTTPException(status_code=404, detail="Remisión no encontrada")
    if usuario["rol"] == "medico" and actual["creado_por"] != usuario["id"]:
        cur.close()
        raise HTTPException(status_code=403, detail="Solo puedes eliminar remisiones que tú creaste")

    cur.execute("UPDATE remisiones SET activo = FALSE WHERE id = %s;", (remision_id,))
    db.commit()
    cur.close()
    registrar_auditoria(db, usuario["id"], "soft_delete", "remisiones", remision_id)


@router.post("/remisiones/{remision_id}/restaurar", response_model=RemisionOut,
          dependencies=[Depends(requiere_rol("admin"))])
def restaurar_remision(remision_id: int, db=Depends(get_db), usuario=Depends(get_usuario_actual)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """UPDATE remisiones SET activo = TRUE WHERE id = %s
           RETURNING id, paciente_id, institucion_origen, institucion_destino,
                     fecha_solicitud, motivo, estado, convenio_vigente, creado_por, activo;""",
        (remision_id,),
    )
    fila = cur.fetchone()
    db.commit()
    cur.close()
    if fila is None:
        raise HTTPException(status_code=404, detail="Remisión no encontrada")
    registrar_auditoria(db, usuario["id"], "restaurar", "remisiones", remision_id)
    return fila


@router.get("/observaciones", response_model=List[ObservacionOut],
         dependencies=[Depends(requiere_rol(*ROLES_CLINICOS))])
def listar_observaciones(remision_id: Optional[int] = Query(None), db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if remision_id is not None:
        cur.execute("SELECT * FROM observaciones WHERE remision_id = %s ORDER BY id;", (remision_id,))
    else:
        cur.execute("SELECT * FROM observaciones ORDER BY id;")
    filas = cur.fetchall()
    cur.close()
    return filas


@router.post("/observaciones", response_model=ObservacionOut, status_code=201,
          dependencies=[Depends(requiere_rol("admin", "medico"))])
def crear_observacion(observacion: ObservacionCreate, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """INSERT INTO observaciones (remision_id, tipo, codigo_loinc, valor, unidad)
               VALUES (%s, %s, %s, %s, %s)
               RETURNING *;""",
            (observacion.remision_id, observacion.tipo, observacion.codigo_loinc,
             observacion.valor, observacion.unidad),
        )
    except psycopg2.errors.ForeignKeyViolation:
        db.rollback(); cur.close()
        raise HTTPException(status_code=400, detail="remision_id no existe")
    fila = cur.fetchone()
    db.commit()
    cur.close()
    return fila


# ── Gestiones de contacto (MongoDB) ──────────────────────────────────────
coleccion_gestiones = db_mongo["gestiones_contacto"]


def documento_a_dict(doc):
    doc["_id"] = str(doc["_id"])
    doc["actualizado_en"] = con_zona(doc.get("actualizado_en"))
    for c in doc.get("contactos") or []:
        c["fecha"] = con_zona(c.get("fecha"))
    return doc


class Contacto(BaseModel):
    fecha: datetime
    medio: str
    institucion_contactada: str
    contactado_por: str
    respuesta: str


class GestionContactoCreate(BaseModel):
    remision_id: int
    contactos: List[Contacto] = []


@router.get("/gestiones-contacto", dependencies=[Depends(requiere_rol(*ROLES_CLINICOS))])
def listar_gestiones(remision_id: Optional[int] = Query(None)):
    filtro = {}
    if remision_id is not None:
        filtro["remision_id"] = remision_id
    documentos = list(coleccion_gestiones.find(filtro))
    return [documento_a_dict(d) for d in documentos]


@router.get("/gestiones-contacto/{gestion_id}", dependencies=[Depends(requiere_rol(*ROLES_CLINICOS))])
def obtener_gestion(gestion_id: str):
    try:
        oid = ObjectId(gestion_id)
    except Exception:
        raise HTTPException(status_code=400, detail="id inválido")
    doc = coleccion_gestiones.find_one({"_id": oid})
    if doc is None:
        raise HTTPException(status_code=404, detail="Gestión de contacto no encontrada")
    return documento_a_dict(doc)


@router.post("/gestiones-contacto", status_code=201, dependencies=[Depends(requiere_rol("admin", "medico"))])
def crear_gestion(gestion: GestionContactoCreate):
    datos = gestion.model_dump()
    datos["actualizado_en"] = datetime.utcnow()
    resultado = coleccion_gestiones.insert_one(datos)
    nuevo = coleccion_gestiones.find_one({"_id": resultado.inserted_id})
    return documento_a_dict(nuevo)


@router.put("/gestiones-contacto/{gestion_id}", dependencies=[Depends(requiere_rol("admin", "medico"))])
def actualizar_gestion(gestion_id: str, gestion: GestionContactoCreate):
    try:
        oid = ObjectId(gestion_id)
    except Exception:
        raise HTTPException(status_code=400, detail="id inválido")
    actual = coleccion_gestiones.find_one({"_id": oid})
    if actual is None:
        raise HTTPException(status_code=404, detail="Gestión de contacto no encontrada")
    # Libro de radicación: la bitácora NO se edita. Se acepta el PUT solo si
    # conserva intactos los contactos que ya estaban y agrega nuevos al final.
    previos = actual.get("contactos") or []
    nuevos = gestion.model_dump()["contactos"]
    clave = lambda c: (str(c.get("institucion_contactada")), str(c.get("respuesta")), str(c.get("medio")))
    if len(nuevos) < len(previos) or [clave(c) for c in nuevos[:len(previos)]] != [clave(c) for c in previos]:
        raise HTTPException(status_code=409,
                            detail="La bitácora no se edita ni se borra: solo se agregan contactos al final "
                                   "(usa POST /registro/remisiones/{id}/gestiones)")
    datos = {"remision_id": gestion.remision_id,
             "contactos": previos + nuevos[len(previos):],
             "actualizado_en": datetime.utcnow()}
    coleccion_gestiones.update_one({"_id": oid}, {"$set": datos})
    nuevo = coleccion_gestiones.find_one({"_id": oid})
    return documento_a_dict(nuevo)


@router.delete("/gestiones-contacto/{gestion_id}", status_code=204,
            dependencies=[Depends(requiere_rol("admin"))])
def borrar_gestion(gestion_id: str):
    try:
        oid = ObjectId(gestion_id)
    except Exception:
        raise HTTPException(status_code=400, detail="id inválido")
    resultado = coleccion_gestiones.delete_one({"_id": oid})
    if resultado.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Gestión de contacto no encontrada")
    
    # =====================================================
# R08 · Remisión entre médicos (médico → especialista)
# =====================================================
# La tabla `remisiones` del Corte 1 es para traslados a instituciones.
# Esta es para remitir un paciente a OTRO médico (especialista).


class RemitirIn(BaseModel):
    paciente_id: int
    especialista_id: int
    motivo: str
    observaciones: Optional[str] = None


@router.post("/remitir", status_code=201,
          dependencies=[Depends(requiere_rol("medico", "admin"))])
def remitir(body: RemitirIn, db=Depends(get_db), usuario=Depends(get_usuario_actual)):
    """R08: el médico remite un paciente a un especialista."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Validar paciente
    cur.execute("SELECT id, nombre, documento FROM pacientes WHERE id = %s;", (body.paciente_id,))
    paciente = cur.fetchone()
    if paciente is None:
        cur.close()
        raise HTTPException(404, "Paciente no encontrado")
      
    # Validar especialista
    cur.execute(
        "SELECT id, nombre, rol FROM usuarios WHERE id = %s AND activo = TRUE;",
        (body.especialista_id,),
    )
    especialista = cur.fetchone()
    if especialista is None:
        cur.close()
        raise HTTPException(404, "Especialista no encontrado")
    if especialista["rol"] != "especialista":
        cur.close()
        raise HTTPException(422, "El destinatario no es un especialista")

    # Crear la remisión
    cur.execute(
        """INSERT INTO remisiones_medicas
              (paciente_id, medico_id, especialista_id, motivo, observaciones, estado)
           VALUES (%s, %s, %s, %s, %s, 'pendiente')
           RETURNING id, estado, created_at;""",
        (body.paciente_id, usuario["id"], body.especialista_id,
         body.motivo, body.observaciones),
    )
    fila = cur.fetchone()
    db.commit()
    cur.close()

    # Auditoría (R13)
    registrar_auditoria(
        db, usuario["id"], "remision_creada", "remisiones_medicas", fila["id"],
        f"paciente {body.paciente_id} → especialista {body.especialista_id}"
    )
    # R08 + FHIR: reflejar la remisión en FHIR como ServiceRequest
    try:
        fhir_patient_id = fhir_sync.obtener_o_crear_paciente_fhir(paciente["documento"])
        fhir_sr_id = fhir_sync.sincronizar_remision_medica(
            fila["id"], fhir_patient_id,
            medico_nombre=usuario["nombre"],
            especialista_nombre=especialista["nombre"],
        )
        print(f"[FHIR] ServiceRequest {fhir_sr_id} creado para remisión {fila['id']}")
    except Exception as e:
        print(f"[FHIR] no se pudo sincronizar la remisión {fila['id']}: {e}")

    # R19: notificar al especialista
    publicar([body.especialista_id], {
        "tipo": "remision_creada",
        "mensaje": f"Nueva remisión de {usuario['nombre']}: {paciente['nombre']}",
        "paciente_id": body.paciente_id,
        "remision_id": fila["id"],
    })

    return {"id": fila["id"], "estado": fila["estado"]}


@router.post("/remitir/{remision_id}/aceptar",
          dependencies=[Depends(requiere_rol("especialista", "admin"))])
def aceptar_remision(
    remision_id: int,
    observaciones: Optional[str] = None,
    db=Depends(get_db),
    usuario=Depends(get_usuario_actual),
):
    """R08: el especialista acepta la remisión. Se avisa al médico remitente."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT r.*, p.nombre AS paciente_nombre, p.id AS paciente_id
             FROM remisiones_medicas r
             JOIN pacientes p ON p.id = r.paciente_id
            WHERE r.id = %s;""",
        (remision_id,),
    )
    r = cur.fetchone()
    if r is None:
        cur.close()
        raise HTTPException(404, "Remisión no encontrada")
    if r["especialista_id"] != usuario["id"] and usuario["rol"] != "admin":
        cur.close()
        raise HTTPException(403, "Esta remisión no es para usted")
    if r["estado"] != "pendiente":
        cur.close()
        raise HTTPException(409, f"La remisión ya está {r['estado']}")

    cur.execute(
        """UPDATE remisiones_medicas
              SET estado = 'aceptada',
                  aceptada_en = now(),
                  observaciones = COALESCE(%s, observaciones)
            WHERE id = %s
            RETURNING id, estado, aceptada_en;""",
        (observaciones, remision_id),
    )
    fila = cur.fetchone()
    db.commit()
    cur.close()

    registrar_auditoria(
        db, usuario["id"], "remision_aceptada", "remisiones_medicas", remision_id,
        f"paciente {r['paciente_id']}"
    )

    # R19: avisar al médico remitente
    publicar([r["medico_id"]], {
        "tipo": "remision_aceptada",
        "mensaje": f"El especialista aceptó la remisión de {r['paciente_nombre']}",
        "paciente_id": r["paciente_id"],
        "remision_id": remision_id,
    })

    return {"id": fila["id"], "estado": fila["estado"]}


@router.get("/remitir/bandeja",
         dependencies=[Depends(requiere_rol("especialista", "admin"))])
def bandeja_especialista(estado: Optional[str] = None,
                        db=Depends(get_db),
                        usuario=Depends(get_usuario_actual)):
    """R08: las remisiones dirigidas al especialista autenticado."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    sql = """
        SELECT r.id, r.paciente_id, r.motivo, r.observaciones, r.estado,
               r.created_at, r.aceptada_en,
               p.nombre AS paciente_nombre, p.documento AS paciente_documento,
               m.nombre AS medico_nombre, m.correo AS medico_correo
          FROM remisiones_medicas r
          JOIN pacientes p ON p.id = r.paciente_id
          JOIN usuarios  m ON m.id = r.medico_id
         WHERE r.especialista_id = %s
    """
    params = [usuario["id"]]
    if estado:
        sql += " AND r.estado = %s"
        params.append(estado)
    sql += " ORDER BY r.created_at DESC;"
    cur.execute(sql, tuple(params))
    filas = cur.fetchall()
    cur.close()
    return filas


@router.get("/remitir/enviadas",
         dependencies=[Depends(requiere_rol("medico", "admin"))])
def remisiones_enviadas(db=Depends(get_db), usuario=Depends(get_usuario_actual)):
    """R08: las remisiones que yo (médico) he enviado."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT r.id, r.paciente_id, r.motivo, r.estado, r.created_at,
                  r.aceptada_en,
                  p.nombre AS paciente_nombre,
                  e.nombre AS especialista_nombre, e.correo AS especialista_correo
             FROM remisiones_medicas r
             JOIN pacientes p ON p.id = r.paciente_id
             JOIN usuarios  e ON e.id = r.especialista_id
            WHERE r.medico_id = %s
            ORDER BY r.created_at DESC;""",
        (usuario["id"],),
    )
    filas = cur.fetchall()
    cur.close()
    return filas
