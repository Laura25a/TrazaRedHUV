from fastapi import FastAPI

app = FastAPI(
    title="API TrazaRed HUV",
    description="API de trazabilidad de remisiones y traslados de pacientes del HUV, con roles y FHIR.",
    version="2.0.0",
)


@app.get("/")
def inicio():
    return {"mensaje": "TrazaRed HUV API - viva y funcionando"}


import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / "pass.env", override=True)

PG_CONNECTION_STRING = os.getenv("PG_CONNECTION_STRING")
MONGO_CONNECTION_STRING = os.getenv("MONGO_CONNECTION_STRING")
SECRET_KEY = os.getenv("SECRET_KEY")

if not PG_CONNECTION_STRING:
    raise ValueError("Falta PG_CONNECTION_STRING en pass.env")
if not MONGO_CONNECTION_STRING:
    raise ValueError("Falta MONGO_CONNECTION_STRING en pass.env")
if not SECRET_KEY:
    raise ValueError("Falta SECRET_KEY en pass.env")

print("Variables cargadas correctamente.")


from pydantic import BaseModel
from datetime import date, datetime, timedelta
from typing import Optional, List


class PacienteCreate(BaseModel):
    nombre: str
    documento: str
    genero: str
    eps: str


class PacienteOut(BaseModel):
    id: int
    nombre: str
    documento: str
    genero: str
    eps: str


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


class UsuarioCreate(BaseModel):
    nombre: str
    correo: str
    contrasena: str
    rol: str
    paciente_id: Optional[int] = None
    eps_nombre: Optional[str] = None


class UsuarioOut(BaseModel):
    id: int
    nombre: str
    correo: str
    rol: str
    activo: bool
    paciente_id: Optional[int] = None
    eps_nombre: Optional[str] = None


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


import psycopg2
import psycopg2.extras
from fastapi import Depends, HTTPException, Query


def get_db():
    conn = psycopg2.connect(PG_CONNECTION_STRING)
    try:
        yield conn
    finally:
        conn.close()


from passlib.context import CryptContext
from jose import jwt, JWTError
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verificar_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def crear_token(datos: dict) -> str:
    datos = datos.copy()
    datos["exp"] = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(datos, SECRET_KEY, algorithm=ALGORITHM)


def get_usuario_actual(token: str = Depends(oauth2_scheme), db=Depends(get_db)) -> dict:
    credenciales_invalidas = HTTPException(status_code=401, detail="Credenciales inválidas")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        usuario_id = payload.get("usuario_id")
        if usuario_id is None:
            raise credenciales_invalidas
    except JWTError:
        raise credenciales_invalidas

    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT id, nombre, correo, rol, activo, paciente_id, eps_nombre FROM usuarios WHERE id = %s;",
        (usuario_id,),
    )
    usuario = cur.fetchone()
    cur.close()
    if usuario is None or not usuario["activo"]:
        raise credenciales_invalidas
    return usuario


def requiere_rol(*roles_permitidos):
    def verificador(usuario=Depends(get_usuario_actual)):
        if usuario["rol"] not in roles_permitidos:
            raise HTTPException(status_code=403, detail="No tienes permiso para esta acción")
        return usuario
    return verificador


def registrar_auditoria(db, usuario_id: int, accion: str, tabla: str, registro_id: int):
    cur = db.cursor()
    cur.execute(
        "INSERT INTO auditoria (usuario_id, accion, tabla, registro_id) VALUES (%s, %s, %s, %s);",
        (usuario_id, accion, tabla, registro_id),
    )
    db.commit()
    cur.close()


@app.post("/login", response_model=Token)
def login(form: OAuth2PasswordRequestForm = Depends(), db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, contrasena_hash, rol, activo FROM usuarios WHERE correo = %s;", (form.username,))
    usuario = cur.fetchone()
    cur.close()
    if usuario is None or not usuario["activo"] or not verificar_password(form.password, usuario["contrasena_hash"]):
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos")
    token = crear_token({"usuario_id": usuario["id"], "rol": usuario["rol"]})
    return Token(access_token=token)


@app.post("/usuarios", response_model=UsuarioOut, status_code=201,
          dependencies=[Depends(requiere_rol("admin"))])
def crear_usuario(usuario: UsuarioCreate, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """INSERT INTO usuarios (nombre, correo, contrasena_hash, rol, paciente_id, eps_nombre)
           VALUES (%s, %s, %s, %s, %s, %s)
           RETURNING id, nombre, correo, rol, activo, paciente_id, eps_nombre;""",
        (usuario.nombre, usuario.correo, hash_password(usuario.contrasena),
         usuario.rol, usuario.paciente_id, usuario.eps_nombre),
    )
    fila = cur.fetchone()
    db.commit()
    cur.close()
    return fila


@app.get("/pacientes", response_model=List[PacienteOut],
         dependencies=[Depends(requiere_rol("admin", "medico"))])
def listar_pacientes(db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, nombre, documento, genero, eps FROM pacientes ORDER BY id;")
    filas = cur.fetchall()
    cur.close()
    return filas


@app.post("/pacientes", response_model=PacienteOut, status_code=201,
          dependencies=[Depends(requiere_rol("admin", "medico"))])
def crear_paciente(paciente: PacienteCreate, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """INSERT INTO pacientes (nombre, documento, genero, eps)
           VALUES (%s, %s, %s, %s)
           RETURNING id, nombre, documento, genero, eps;""",
        (paciente.nombre, paciente.documento, paciente.genero, paciente.eps),
    )
    fila = cur.fetchone()
    db.commit()
    cur.close()
    return fila


@app.get("/remisiones")
def listar_remisiones(
    estado: Optional[str] = Query(None),
    paciente_id: Optional[int] = Query(None),
    db=Depends(get_db),
    usuario=Depends(get_usuario_actual),
):
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

    condiciones = ["r.activo = TRUE"]
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
                     r.creado_por, r.activo
              FROM remisiones r JOIN pacientes p ON p.id = r.paciente_id
              WHERE """ + " AND ".join(condiciones) + " ORDER BY r.id;"

    cur.execute(sql, tuple(valores))
    filas = cur.fetchall()
    cur.close()
    return filas


@app.get("/remisiones/{remision_id}")
def obtener_remision(remision_id: int, db=Depends(get_db), usuario=Depends(get_usuario_actual)):
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
                  r.creado_por, r.activo, p.eps
           FROM remisiones r JOIN pacientes p ON p.id = r.paciente_id
           WHERE r.id = %s AND r.activo = TRUE;""",
        (remision_id,),
    )
    fila = cur.fetchone()
    cur.close()
    if fila is None:
        raise HTTPException(status_code=404, detail="Remisión no encontrada")
    if usuario["rol"] == "eps" and fila["eps"] != usuario["eps_nombre"]:
        raise HTTPException(status_code=403, detail="Esta remisión no pertenece a tu EPS")
    return fila


@app.post("/remisiones", response_model=RemisionOut, status_code=201,
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
    return fila


@app.put("/remisiones/{remision_id}", response_model=RemisionOut,
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

    import json
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


@app.delete("/remisiones/{remision_id}", status_code=204,
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


@app.post("/remisiones/{remision_id}/restaurar", response_model=RemisionOut,
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


@app.get("/observaciones", response_model=List[ObservacionOut],
         dependencies=[Depends(requiere_rol("admin", "medico"))])
def listar_observaciones(remision_id: Optional[int] = Query(None), db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if remision_id is not None:
        cur.execute("SELECT * FROM observaciones WHERE remision_id = %s ORDER BY id;", (remision_id,))
    else:
        cur.execute("SELECT * FROM observaciones ORDER BY id;")
    filas = cur.fetchall()
    cur.close()
    return filas


@app.post("/observaciones", response_model=ObservacionOut, status_code=201,
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


import pymongo
from bson import ObjectId

mongo_client = pymongo.MongoClient(MONGO_CONNECTION_STRING)
db_mongo = mongo_client["trazared_huv"]
coleccion_gestiones = db_mongo["gestiones_contacto"]


def documento_a_dict(doc):
    doc["_id"] = str(doc["_id"])
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


@app.get("/gestiones-contacto", dependencies=[Depends(requiere_rol("admin", "medico"))])
def listar_gestiones(remision_id: Optional[int] = Query(None)):
    filtro = {}
    if remision_id is not None:
        filtro["remision_id"] = remision_id
    documentos = list(coleccion_gestiones.find(filtro))
    return [documento_a_dict(d) for d in documentos]


@app.get("/gestiones-contacto/{gestion_id}", dependencies=[Depends(requiere_rol("admin", "medico"))])
def obtener_gestion(gestion_id: str):
    try:
        oid = ObjectId(gestion_id)
    except Exception:
        raise HTTPException(status_code=400, detail="id inválido")
    doc = coleccion_gestiones.find_one({"_id": oid})
    if doc is None:
        raise HTTPException(status_code=404, detail="Gestión de contacto no encontrada")
    return documento_a_dict(doc)


@app.post("/gestiones-contacto", status_code=201, dependencies=[Depends(requiere_rol("admin", "medico"))])
def crear_gestion(gestion: GestionContactoCreate):
    datos = gestion.model_dump()
    datos["actualizado_en"] = datetime.utcnow()
    resultado = coleccion_gestiones.insert_one(datos)
    nuevo = coleccion_gestiones.find_one({"_id": resultado.inserted_id})
    return documento_a_dict(nuevo)


@app.put("/gestiones-contacto/{gestion_id}", dependencies=[Depends(requiere_rol("admin", "medico"))])
def actualizar_gestion(gestion_id: str, gestion: GestionContactoCreate):
    try:
        oid = ObjectId(gestion_id)
    except Exception:
        raise HTTPException(status_code=400, detail="id inválido")
    datos = gestion.model_dump()
    datos["actualizado_en"] = datetime.utcnow()
    resultado = coleccion_gestiones.update_one({"_id": oid}, {"$set": datos})
    if resultado.matched_count == 0:
        raise HTTPException(status_code=404, detail="Gestión de contacto no encontrada")
    nuevo = coleccion_gestiones.find_one({"_id": oid})
    return documento_a_dict(nuevo)


@app.delete("/gestiones-contacto/{gestion_id}", status_code=204,
            dependencies=[Depends(requiere_rol("admin"))])
def borrar_gestion(gestion_id: str):
    try:
        oid = ObjectId(gestion_id)
    except Exception:
        raise HTTPException(status_code=400, detail="id inválido")
    resultado = coleccion_gestiones.delete_one({"_id": oid})
    if resultado.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Gestión de contacto no encontrada")
