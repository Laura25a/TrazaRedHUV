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
from datetime import date, datetime, timezone
from typing import List, Optional

import auditoria_mongo
import cripto
import psycopg2
import psycopg2.extras
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from auth import ROLES_CLINICOS, con_zona, db_mongo, get_db, get_usuario_actual, registrar_auditoria, requiere_rol
from causas import causa_por_reglas
from eventos import publicar, registrar_evento
import reactividad
import fhir_sync
from validacion_clinica import PAD, PAS, SIGNOS, codigo_signo, validar_presion, validar_signo
from validaciones import (bloquear_remision, exigir_asignacion_clinica, filtro_asignacion_clinica,
                          hoy_bogota, institucion_del_catalogo)

router = APIRouter(tags=["Remisiones"])


def sin_contable(usuario: dict):
    """R03: las remisiones son datos clínicos; el contable no las ve (403)."""
    if usuario["rol"] in ("contable", "director", "biomedico"):
        raise HTTPException(status_code=403, detail=f"El rol {usuario['rol']} no tiene acceso a datos clínicos individuales")

class DatosRemision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    institucion_origen: str
    institucion_destino: str
    fecha_solicitud: date
    motivo: str
    convenio_vigente: bool

    @field_validator("institucion_origen", "institucion_destino", "motivo")
    @classmethod
    def texto_requerido(cls, valor):
        if not valor.strip():
            raise ValueError("El campo no puede quedar vacío")
        return valor.strip()


class RemisionCreate(DatosRemision):
    paciente_id: int
    estado: str = "pendiente"


class RemisionPatch(DatosRemision):
    institucion_origen: str = None
    institucion_destino: str = None
    fecha_solicitud: date = None
    motivo: str = None
    convenio_vigente: bool = None


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
    tipo: str = Field(min_length=1)
    codigo_loinc: Optional[str] = None
    valor: float = Field(allow_inf_nan=False)
    unidad: Optional[str] = None
    valor_complementario: Optional[float] = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def comprobar_signo(self):
        self.tipo = self.tipo.strip()
        if not self.tipo:
            raise ValueError("Indica el tipo de observación.")
        codigo_tipo = codigo_signo(self.tipo)
        if codigo_tipo and self.codigo_loinc and self.codigo_loinc != codigo_tipo:
            raise ValueError("El código LOINC no corresponde al signo indicado.")
        self.codigo_loinc = self.codigo_loinc or codigo_tipo
        if self.codigo_loinc in SIGNOS:
            signo = validar_signo(self.codigo_loinc, self.valor, self.unidad)
            self.tipo, self.unidad = signo.tipo, signo.unidad
        if self.valor_complementario is not None:
            if self.codigo_loinc not in (PAS, PAD):
                raise ValueError("El valor complementario solo aplica para presión arterial.")
            otro = PAD if self.codigo_loinc == PAS else PAS
            validar_signo(otro, self.valor_complementario, self.unidad)
            presion = {self.codigo_loinc: self.valor, otro: self.valor_complementario}
            validar_presion(presion[PAS], presion[PAD])
        return self


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
    asignacion, parametros = filtro_asignacion_clinica(usuario)
    condiciones.append(asignacion)
    valores.extend(parametros)

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
    return cripto.descifrar_pacientes(filas, "paciente_id", "paciente_")     # R27


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
    fila = cripto.descifrar_paciente(cur.fetchone(), "paciente_id", "paciente_")
    cur.close()
    if fila is None:
        raise HTTPException(status_code=404, detail="Remisión no encontrada")
    if usuario["rol"] == "eps" and fila["eps"] != usuario["eps_nombre"]:
        raise HTTPException(status_code=403, detail="Esta remisión no pertenece a tu EPS")
    exigir_asignacion_clinica(db, fila["paciente_id"], usuario)
    return fila


@router.get("/instituciones", dependencies=[Depends(requiere_rol(*ROLES_CLINICOS))])
def listar_instituciones(db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT nombre FROM instituciones ORDER BY nombre;")
        return [fila[0] for fila in cur.fetchall()]


@router.post("/remisiones", response_model=RemisionOut, status_code=201,
          dependencies=[Depends(requiere_rol("admin", "medico"))])
def crear_remision(remision: RemisionCreate, db=Depends(get_db), usuario=Depends(get_usuario_actual)):
    if remision.estado != "pendiente":
        raise HTTPException(422, "Una remisión nueva debe quedar pendiente")
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        datos = preparar_correccion(cur, remision.model_dump(), limite=hoy_bogota())
        try:
            cur.execute(
                """INSERT INTO remisiones
                       (paciente_id, institucion_origen, institucion_destino,
                        fecha_solicitud, motivo, estado, convenio_vigente, creado_por)
                   VALUES (%(paciente_id)s, %(institucion_origen)s, %(institucion_destino)s,
                           %(fecha_solicitud)s, %(motivo)s, 'pendiente', %(convenio_vigente)s, %(usuario)s)
                   RETURNING *;""",
                {**datos, "usuario": usuario["id"]},
            )
        except psycopg2.errors.CheckViolation as exc:
            raise HTTPException(422, exc.diag.message_primary) from exc
        fila = cur.fetchone()
    registrar_auditoria(db, usuario["id"], "remision_creada", "remisiones", fila["id"],
                        f"paciente {fila['paciente_id']}")
    return fila


def preparar_correccion(cur, datos, limite, paciente_id=None):
    for campo in ("institucion_origen", "institucion_destino"):
        if campo in datos:
            datos[campo] = institucion_del_catalogo(cur, datos[campo])
    if "fecha_solicitud" in datos:
        fecha = datos["fecha_solicitud"]
        cur.execute("SELECT fecha_nacimiento FROM pacientes WHERE id = %s FOR SHARE;",
                    (paciente_id if paciente_id is not None else datos["paciente_id"],))
        paciente = cur.fetchone()
        if paciente is None:
            raise HTTPException(404, "Paciente no encontrado")
        if fecha > limite:
            raise HTTPException(422, "La solicitud no puede ser posterior a la radicación ni a hoy")
        if paciente["fecha_nacimiento"] and fecha < paciente["fecha_nacimiento"]:
            raise HTTPException(422, "La solicitud no puede ser anterior al nacimiento")
    return datos


@router.patch("/remisiones/{remision_id}", response_model=RemisionOut,
              dependencies=[Depends(requiere_rol("admin", "medico"))])
def corregir_remision(remision_id: int, remision: RemisionPatch, db=Depends(get_db),
                      usuario=Depends(get_usuario_actual)):
    actual = bloquear_remision(db, remision_id, usuario)
    return _guardar_correccion(db, usuario, actual, remision.model_dump(exclude_unset=True))


@router.put("/remisiones/{remision_id}", response_model=RemisionOut,
          dependencies=[Depends(requiere_rol("admin", "medico"))])
def actualizar_remision(remision_id: int, remision: RemisionCreate, db=Depends(get_db),
                        usuario=Depends(get_usuario_actual)):
    actual = bloquear_remision(db, remision_id, usuario)
    if remision.paciente_id != actual["paciente_id"] or remision.estado != actual["estado"]:
        raise HTTPException(409, "La corrección no cambia el paciente ni el estado del radicado")
    return _guardar_correccion(db, usuario, actual, remision.model_dump(exclude={"paciente_id", "estado"}))


def _guardar_correccion(db, usuario, actual, datos):
    cambios = {campo: valor for campo, valor in datos.items() if valor != actual[campo]}
    if not cambios:
        return actual
    remision_id = actual["id"]
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        radicada = actual["radicada_en"]
        limite = min(hoy_bogota(), radicada.date()) if radicada else hoy_bogota()
        preparar_correccion(cur, cambios, limite, actual["paciente_id"])
        cambios = {campo: valor for campo, valor in cambios.items() if valor != actual[campo]}
        if not cambios:
            return actual
        cur.execute(
            "INSERT INTO remisiones_historial (remision_id, dato_anterior, modificado_por) VALUES (%s, %s, %s);",
            (remision_id, json.dumps(dict(actual), default=str), usuario["id"]),
        )
        asignaciones = ", ".join(f"{campo} = %s" for campo in cambios)
        cur.execute(f"UPDATE remisiones SET {asignaciones} WHERE id = %s RETURNING *;",
                    (*cambios.values(), remision_id))
        fila = cur.fetchone()
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
    cur.close()
    if fila is None:
        raise HTTPException(status_code=404, detail="Remisión no encontrada")
    registrar_auditoria(db, usuario["id"], "restaurar", "remisiones", remision_id)
    return fila


@router.get("/observaciones", response_model=List[ObservacionOut])
def listar_observaciones(remision_id: Optional[int] = Query(None), db=Depends(get_db),
                         usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    asignacion, parametros = filtro_asignacion_clinica(usuario)
    if remision_id is not None:
        _autorizar_episodio(db, remision_id, usuario)
        cur.execute("SELECT o.* FROM observaciones o JOIN remisiones r ON r.id = o.remision_id "
                    "WHERE o.remision_id = %s AND " + asignacion + " ORDER BY o.id;", (remision_id, *parametros))
    else:
        cur.execute("SELECT o.* FROM observaciones o JOIN remisiones r ON r.id = o.remision_id "
                    "WHERE " + asignacion + " ORDER BY o.id;", parametros)
    filas = cur.fetchall()
    cur.close()
    return filas


@router.post("/observaciones", response_model=ObservacionOut, status_code=201,
          dependencies=[Depends(requiere_rol("admin", "medico", "especialista"))])
def crear_observacion(observacion: ObservacionCreate, db=Depends(get_db),
                      usuario=Depends(get_usuario_actual)):
    episodio = bloquear_remision(db, observacion.remision_id, usuario, exigir_abierta=True, admite_responsable_externo=True)
    codigo = observacion.codigo_loinc
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        otro = {PAS: PAD, PAD: PAS}.get(codigo)
        complemento = observacion.valor_complementario
        if otro and complemento is None:
            cur.execute(
                """SELECT valor, unidad FROM observaciones
                    WHERE remision_id = %s AND codigo_loinc = %s
                    ORDER BY fecha_observacion DESC, id DESC LIMIT 1;""",
                (observacion.remision_id, otro),
            )
            anterior = cur.fetchone()
            if anterior:
                presion = {codigo: observacion.valor, otro: float(anterior["valor"])}
                try:
                    validar_signo(otro, presion[otro], anterior["unidad"])
                    validar_presion(presion[PAS], presion[PAD])
                except ValueError as exc:
                    raise HTTPException(422, f"{exc} Si es una nueva toma, registra ambos valores juntos.") from exc
        insertar = """INSERT INTO observaciones (remision_id, tipo, codigo_loinc, valor, unidad, profesional_id)
                      VALUES (%s, %s, %s, %s, %s, %s) RETURNING *;"""
        cur.execute(insertar, (observacion.remision_id, observacion.tipo, codigo,
                               observacion.valor, observacion.unidad, usuario["id"]))
        fila = cur.fetchone()
        filas = [fila]
        if otro and complemento is not None:
            signo = SIGNOS[otro]
            cur.execute(insertar, (observacion.remision_id, signo.tipo, otro, complemento, signo.unidad, usuario["id"]))
            filas.append(cur.fetchone())
    reactividad.registrar_observaciones(db, episodio, episodio["paciente_id"], filas)
    registrar_auditoria(db, usuario["id"], "observacion_creada", "remisiones", observacion.remision_id)
    return fila


# ── Gestiones de contacto (MongoDB) ──────────────────────────────────────
coleccion_gestiones = db_mongo["gestiones_contacto"]


def documento_a_dict(doc):
    doc.pop(auditoria_mongo.CAMPO, None)
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
    contactos: List[Contacto] = Field(default_factory=list)


@router.get("/gestiones-contacto")
def listar_gestiones(remision_id: Optional[int] = Query(None), db=Depends(get_db),
                    usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    filtro = {}
    if remision_id is not None:
        _autorizar_episodio(db, remision_id, usuario)
        filtro["remision_id"] = remision_id
    elif usuario["rol"] != "admin":
        with db.cursor() as cur:
            asignacion, parametros = filtro_asignacion_clinica(usuario)
            cur.execute("SELECT r.id FROM remisiones r WHERE " + asignacion, parametros)
            filtro["remision_id"] = {"$in": [f[0] for f in cur.fetchall()]}
    documentos = list(coleccion_gestiones.find(filtro))
    registrar_auditoria(db, usuario["id"], "bitacora_consultada", "remisiones", remision_id,
                        f"listado autorizado; cantidad={len(documentos)}")
    return [documento_a_dict(d) for d in documentos]


def _autorizar_episodio(db, remision_id, usuario):
    with db.cursor() as cur:
        cur.execute("SELECT paciente_id FROM remisiones WHERE id = %s", (remision_id,))
        fila = cur.fetchone()
    if fila is None:
        raise HTTPException(404, "Gestión de contacto no encontrada")
    exigir_asignacion_clinica(db, fila[0], usuario)


@router.get("/gestiones-contacto/{gestion_id}")
def obtener_gestion(gestion_id: str, db=Depends(get_db),
                    usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    try:
        oid = ObjectId(gestion_id)
    except Exception:
        raise HTTPException(status_code=400, detail="id inválido")
    doc = coleccion_gestiones.find_one({"_id": oid})
    if doc is None:
        raise HTTPException(status_code=404, detail="Gestión de contacto no encontrada")
    _autorizar_episodio(db, doc["remision_id"], usuario)
    registrar_auditoria(db, usuario["id"], "bitacora_consultada", "remisiones", doc["remision_id"],
                        "consulta individual autorizada")
    return documento_a_dict(doc)


@router.post("/gestiones-contacto", status_code=201)
def crear_gestion(gestion: GestionContactoCreate, db=Depends(get_db),
                   usuario=Depends(requiere_rol("admin", "medico"))):
    from registro import evento_gestion

    bloquear_remision(db, gestion.remision_id, usuario, exigir_abierta=True)
    datos = gestion.model_dump()
    datos["actualizado_en"] = datetime.now(timezone.utc)
    if coleccion_gestiones.find_one({"remision_id": gestion.remision_id}):
        raise HTTPException(409, "Ya hay una bitácora para esta remisión. Agrega contactos desde el registro")
    datos["contactos"] = contactos_legados(datos["contactos"], usuario)
    datos[auditoria_mongo.CAMPO] = [evento_gestion(usuario, gestion.remision_id, datos["contactos"])]
    resultado = coleccion_gestiones.insert_one(datos)
    auditoria_mongo.sellar_documento(db, coleccion_gestiones, resultado.inserted_id)
    return documento_a_dict(coleccion_gestiones.find_one({"_id": resultado.inserted_id}))


@router.put("/gestiones-contacto/{gestion_id}")
def actualizar_gestion(gestion_id: str, gestion: GestionContactoCreate, db=Depends(get_db),
                       usuario=Depends(requiere_rol("admin", "medico"))):
    try:
        oid = ObjectId(gestion_id)
    except Exception:
        raise HTTPException(400, "id inválido")
    from registro import evento_gestion

    bloquear_remision(db, gestion.remision_id, usuario, exigir_abierta=True)
    actual = coleccion_gestiones.find_one({"_id": oid})
    if actual is None:
        raise HTTPException(404, "Gestión de contacto no encontrada")
    if actual["remision_id"] != gestion.remision_id:
        raise HTTPException(409, "La bitácora pertenece a otro radicado")
    previos = actual.get("contactos") or []
    nuevos = gestion.model_dump()["contactos"]

    def clave(contacto):
        fecha = con_zona(contacto["fecha"])
        return (fecha.replace(microsecond=fecha.microsecond // 1000 * 1000),
                contacto["institucion_contactada"], contacto["respuesta"],
                contacto["medio"], contacto["contactado_por"])

    if [clave(c) for c in nuevos[:len(previos)]] != [clave(c) for c in previos]:
        raise HTTPException(409, "La bitácora solo admite agregar contactos al final")
    pendientes = contactos_legados(nuevos[len(previos):], usuario)
    if pendientes:
        coleccion_gestiones.update_one({"_id": oid}, {
            "$push": {"contactos": {"$each": pendientes},
                      auditoria_mongo.CAMPO: evento_gestion(usuario, gestion.remision_id, pendientes)},
            "$set": {"actualizado_en": datetime.now(timezone.utc)},
        })
        auditoria_mongo.sellar_documento(db, coleccion_gestiones, oid)
    return documento_a_dict(coleccion_gestiones.find_one({"_id": oid}))


def contactos_legados(contactos, usuario):
    ahora = datetime.now(timezone.utc)
    return [{**c, "causa": causa_por_reglas(c["respuesta"]), "causa_origen": "reglas",
             "registrado_por": usuario["id"], "registrado_en": ahora} for c in contactos]


@router.delete("/gestiones-contacto/{gestion_id}")
def borrar_gestion(gestion_id: str, usuario=Depends(requiere_rol("admin"))):
    if not ObjectId.is_valid(gestion_id):
        raise HTTPException(400, "id inválido")
    if coleccion_gestiones.find_one({"_id": ObjectId(gestion_id)}, {"_id": 1}) is None:
        raise HTTPException(404, "Gestión de contacto no encontrada")
    raise HTTPException(409, "La bitácora es permanente y se conserva al anular el radicado")


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
    paciente = cripto.descifrar_paciente(cur.fetchone())
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
    cur.close()

    # R19: notificar al especialista
    registrar_evento(db, [body.especialista_id], {
        "tipo": "remision_creada",
        "mensaje": f"Nueva interconsulta #{fila['id']} de {usuario['nombre']}",
        "paciente_id": body.paciente_id,
        "interconsulta_id": fila["id"],
    })

    # El sello confirma la interconsulta en la misma transacción.
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
        """SELECT r.*
             FROM remisiones_medicas r
            WHERE r.id = %s
            FOR UPDATE OF r;""",
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
    cur.close()

    # R19: avisar al médico remitente
    registrar_evento(db, [r["medico_id"]], {
        "tipo": "remision_aceptada",
        "mensaje": f"El especialista aceptó la interconsulta #{remision_id}",
        "paciente_id": r["paciente_id"],
        "interconsulta_id": remision_id,
    })

    registrar_auditoria(
        db, usuario["id"], "remision_aceptada", "remisiones_medicas", remision_id,
        f"paciente {r['paciente_id']}"
    )


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
    filas = cripto.descifrar_pacientes(cur.fetchall(), "paciente_id", "paciente_")
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
