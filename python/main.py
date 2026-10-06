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

# override=False: si la variable ya viene del entorno (docker-compose la
# define para apuntar a los contenedores db/mongo), esa es la que manda;
# pass.env solo llena las que falten (caso local con Neon/Atlas).
load_dotenv(Path(__file__).resolve().parent.parent / "pass.env", override=False)

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
from datetime import date, datetime, timedelta, timezone
from typing import Optional, List


class PacienteCreate(BaseModel):
    nombre: str
    documento: str
    genero: str
    eps: str
    # Semana 8: datos opcionales de la ficha
    fecha_nacimiento: Optional[date] = None
    telefono: Optional[str] = None
    tipo_sangre: Optional[str] = None
    alergias: Optional[str] = None


class PacienteOut(BaseModel):
    id: int
    nombre: str
    documento: str
    genero: str
    eps: str
    fecha_nacimiento: Optional[date] = None
    telefono: Optional[str] = None
    tipo_sangre: Optional[str] = None
    alergias: Optional[str] = None


COLUMNAS_PACIENTE = "id, nombre, documento, genero, eps, fecha_nacimiento, telefono, tipo_sangre, alergias"


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


import re
from pydantic import field_validator

# Usuario (correo) seguro: solo letras, números y . _ - @ ; máximo 50 caracteres.
# Así se evita que un nombre de usuario lleve comillas, ;, $, #, espacios, etc.
# (primera barrera contra inyección SQL, tal como se vio en la clase de la semana 8).
PATRON_USUARIO = re.compile(r"^[A-Za-z0-9._@-]{3,50}$")


class UsuarioCreate(BaseModel):
    nombre: str
    correo: str
    contrasena: str
    rol: str
    paciente_id: Optional[int] = None
    eps_nombre: Optional[str] = None

    @field_validator("correo")
    @classmethod
    def validar_correo(cls, v: str) -> str:
        v = v.strip().lower()
        if not PATRON_USUARIO.match(v):
            raise ValueError("El usuario solo admite letras, números y . _ - @ (3 a 50 caracteres)")
        return v

    @field_validator("rol")
    @classmethod
    def validar_rol(cls, v: str) -> str:
        if v not in ("admin", "medico", "eps", "paciente"):
            raise ValueError("rol debe ser admin, medico, eps o paciente")
        return v

    @field_validator("contrasena")
    @classmethod
    def validar_contrasena(cls, v: str) -> str:
        if len(v) < 6 or len(v) > 72:
            raise ValueError("La contraseña debe tener entre 6 y 72 caracteres")
        return v


class UsuarioOut(BaseModel):
    id: int
    nombre: str
    correo: str
    rol: str
    activo: bool
    paciente_id: Optional[int] = None
    eps_nombre: Optional[str] = None
    intentos_fallidos: int = 0
    bloqueado: bool = False


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
        """SELECT id, nombre, correo, rol, activo, paciente_id, eps_nombre, bloqueado
           FROM usuarios WHERE id = %s;""",
        (usuario_id,),
    )
    usuario = cur.fetchone()
    cur.close()
    if usuario is None or not usuario["activo"]:
        raise credenciales_invalidas
    # Si lo bloquearon mientras tenía una sesión abierta, su token deja de servir.
    if usuario["bloqueado"]:
        raise HTTPException(status_code=401, detail="Usuario bloqueado: contacta al administrador")
    return usuario


def requiere_rol(*roles_permitidos):
    def verificador(usuario=Depends(get_usuario_actual)):
        if usuario["rol"] not in roles_permitidos:
            raise HTTPException(status_code=403, detail="No tienes permiso para esta acción")
        return usuario
    return verificador


def registrar_auditoria(db, usuario_id: Optional[int], accion: str, tabla: str,
                        registro_id: Optional[int], detalle: Optional[str] = None):
    cur = db.cursor()
    cur.execute(
        """INSERT INTO auditoria (usuario_id, accion, tabla, registro_id, detalle)
           VALUES (%s, %s, %s, %s, %s);""",
        (usuario_id, accion, tabla, registro_id, detalle),
    )
    db.commit()
    cur.close()


# ----------------------------------------------------------------------------
# SEMANA 8 — Login con bloqueo al 3er intento fallido
# ----------------------------------------------------------------------------
# Reglas:
#  * Cada contraseña errada suma 1 a usuarios.intentos_fallidos y queda en
#    auditoria (accion = 'login_fallido') con cuántos intentos le quedan.
#  * Al llegar a MAX_INTENTOS se pone usuarios.bloqueado = TRUE, se audita
#    'bloqueo_usuario' y se responde 423 (Locked).
#  * Un usuario bloqueado no puede entrar ni con la clave correcta
#    ('login_bloqueado' en auditoria) hasta que un admin lo desbloquee.
#  * Un login correcto reinicia el contador ('login_exitoso').
#  * Un correo que no existe también se audita (usuario_id NULL, el correo
#    intentado en "detalle"), pero la respuesta es la misma genérica para no
#    revelar qué correos existen.
MAX_INTENTOS = 3


@app.post("/login", response_model=Token, responses={
    401: {"description": "Credenciales incorrectas; detail.intentos_restantes dice cuántos quedan"},
    423: {"description": "Usuario bloqueado por intentos fallidos"},
})
def login(form: OAuth2PasswordRequestForm = Depends(), db=Depends(get_db)):
    correo = form.username.strip().lower()
    generico = HTTPException(status_code=401, detail={
        "mensaje": "Correo o contraseña incorrectos", "intentos_restantes": None})

    # El usuario con formato inválido ni siquiera llega a la consulta SQL.
    if not PATRON_USUARIO.match(correo):
        registrar_auditoria(db, None, "login_fallido", "usuarios", None,
                            f"usuario con formato inválido: {correo[:50]!r}")
        raise generico

    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT id, contrasena_hash, rol, activo, bloqueado, intentos_fallidos
           FROM usuarios WHERE correo = %s;""",
        (correo,),
    )
    usuario = cur.fetchone()

    if usuario is None:
        cur.close()
        registrar_auditoria(db, None, "login_fallido", "usuarios", None,
                            f"correo no registrado: {correo}")
        raise generico

    if not usuario["activo"]:
        cur.close()
        registrar_auditoria(db, usuario["id"], "login_fallido", "usuarios", usuario["id"],
                            f"{correo}: usuario inactivo")
        raise generico

    if usuario["bloqueado"]:
        cur.close()
        registrar_auditoria(db, usuario["id"], "login_bloqueado", "usuarios", usuario["id"],
                            f"{correo}: intento de ingreso con el usuario bloqueado")
        raise HTTPException(status_code=423, detail={
            "mensaje": "Usuario bloqueado por intentos fallidos. Solo un administrador puede desbloquearlo.",
            "intentos_restantes": 0, "bloqueado": True})

    if not verificar_password(form.password, usuario["contrasena_hash"]):
        # Suma el intento y bloquea en la MISMA sentencia (atómico: dos intentos
        # simultáneos no pueden "saltarse" el límite).
        cur.execute(
            """UPDATE usuarios
                  SET intentos_fallidos = intentos_fallidos + 1,
                      bloqueado = (intentos_fallidos + 1 >= %s)
                WHERE id = %s
            RETURNING intentos_fallidos, bloqueado;""",
            (MAX_INTENTOS, usuario["id"]),
        )
        estado = cur.fetchone()
        db.commit()
        cur.close()
        restantes = max(MAX_INTENTOS - estado["intentos_fallidos"], 0)
        registrar_auditoria(db, usuario["id"], "login_fallido", "usuarios", usuario["id"],
                            f"{correo}: contraseña incorrecta (intento {estado['intentos_fallidos']} "
                            f"de {MAX_INTENTOS}, le quedan {restantes})")
        if estado["bloqueado"]:
            registrar_auditoria(db, usuario["id"], "bloqueo_usuario", "usuarios", usuario["id"],
                                f"{correo}: bloqueado al llegar a {MAX_INTENTOS} intentos fallidos")
            raise HTTPException(status_code=423, detail={
                "mensaje": "Usuario bloqueado por intentos fallidos. Solo un administrador puede desbloquearlo.",
                "intentos_restantes": 0, "bloqueado": True})
        raise HTTPException(status_code=401, detail={
            "mensaje": f"Contraseña incorrecta. Te {'queda' if restantes == 1 else 'quedan'} "
                       f"{restantes} {'intento' if restantes == 1 else 'intentos'} antes del bloqueo.",
            "intentos_restantes": restantes})

    # Login correcto: se reinicia el contador.
    cur.execute("UPDATE usuarios SET intentos_fallidos = 0 WHERE id = %s;", (usuario["id"],))
    db.commit()
    cur.close()
    registrar_auditoria(db, usuario["id"], "login_exitoso", "usuarios", usuario["id"], correo)
    token = crear_token({"usuario_id": usuario["id"], "rol": usuario["rol"]})
    return Token(access_token=token)


COLUMNAS_USUARIO = "id, nombre, correo, rol, activo, paciente_id, eps_nombre, intentos_fallidos, bloqueado"


# ----------------------------------------------------------------------------
# Arranque en Docker: usuarios iniciales
# ----------------------------------------------------------------------------
# En un contenedor recién creado la base está vacía y POST /usuarios exige un
# admin... que todavía no existe. Al arrancar, la API crea los usuarios de
# prueba que falten, con las contraseñas DEMO_* de pass.env (si una variable
# no está definida, ese usuario simplemente no se crea). En Neon, donde ya
# existen, no hace nada. El usuario paciente lo crea cargar_dataset.py, porque
# necesita un paciente existente al cual quedar vinculado.
USUARIOS_INICIALES = [
    ("Administrador TrazaRed", "admin@trazared.huv", "admin", None, "DEMO_ADMIN_PASSWORD"),
    ("Médico HUV", "medico@huv.gov.co", "medico", None, "DEMO_MEDICO_PASSWORD"),
    ("EPS Coosalud", "eps@coosalud.com", "eps", "Coosalud", "DEMO_EPS_PASSWORD"),
]


@app.on_event("startup")
def crear_usuarios_iniciales():
    import time
    conn = None
    for intento in range(1, 16):   # hasta ~30 s esperando a que Postgres acepte conexiones
        try:
            conn = psycopg2.connect(PG_CONNECTION_STRING, connect_timeout=5)
            break
        except psycopg2.OperationalError as e:
            print(f"[arranque] PostgreSQL aún no responde (intento {intento}): {str(e).strip()[:80]}")
            time.sleep(2)
    if conn is None:
        print("[arranque] no se pudo conectar a PostgreSQL; no se crearon usuarios iniciales")
        return
    try:
        cur = conn.cursor()
        for nombre, correo, rol, eps, variable in USUARIOS_INICIALES:
            clave = os.getenv(variable)
            if not clave:
                continue
            cur.execute(
                """INSERT INTO usuarios (nombre, correo, contrasena_hash, rol, eps_nombre)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (correo) DO NOTHING;""",
                (nombre, correo, hash_password(clave), rol, eps),
            )
            if cur.rowcount:
                print(f"[arranque] usuario inicial creado: {correo} ({rol})")
        conn.commit()
        cur.close()
    except psycopg2.Error as e:
        print(f"[arranque] no se crearon usuarios iniciales: {e}")
    finally:
        conn.close()


@app.get("/health")
def health():
    """Estado de las dependencias (lo usa la interfaz para los indicadores)."""
    import pacs
    estado = {"api": "ok", "postgres": "error", "mongo": "error", "pacs": "error"}
    try:
        conn = psycopg2.connect(PG_CONNECTION_STRING, connect_timeout=3)
        conn.close()
        estado["postgres"] = "ok"
    except psycopg2.Error:
        pass
    try:
        mongo_client.admin.command("ping")
        estado["mongo"] = "ok"
    except Exception:
        pass
    estado["pacs"] = "ok" if pacs.is_alive() else "error"
    return estado


@app.get("/me", response_model=UsuarioOut)
def quien_soy(usuario=Depends(get_usuario_actual), db=Depends(get_db)):
    """Datos del usuario de la sesión (lo usa la interfaz para saber qué mostrar)."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SELECT {COLUMNAS_USUARIO} FROM usuarios WHERE id = %s;", (usuario["id"],))
    fila = cur.fetchone()
    cur.close()
    return fila


@app.get("/usuarios", response_model=List[UsuarioOut],
         dependencies=[Depends(requiere_rol("admin"))])
def listar_usuarios(solo_bloqueados: bool = Query(False), db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    filtro = "WHERE bloqueado = TRUE" if solo_bloqueados else ""
    cur.execute(f"SELECT {COLUMNAS_USUARIO} FROM usuarios {filtro} ORDER BY id;")
    filas = cur.fetchall()
    cur.close()
    return filas


@app.post("/usuarios/{usuario_id}/desbloquear", response_model=UsuarioOut)
def desbloquear_usuario(usuario_id: int, db=Depends(get_db), admin=Depends(requiere_rol("admin"))):
    """Solo el admin: quita el bloqueo y reinicia el contador de intentos."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        f"""UPDATE usuarios SET bloqueado = FALSE, intentos_fallidos = 0
             WHERE id = %s RETURNING {COLUMNAS_USUARIO};""",
        (usuario_id,),
    )
    fila = cur.fetchone()
    db.commit()
    cur.close()
    if fila is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    registrar_auditoria(db, admin["id"], "desbloqueo_usuario", "usuarios", usuario_id,
                        f"{fila['correo']} desbloqueado por {admin['correo']}")
    return fila


@app.get("/auditoria", dependencies=[Depends(requiere_rol("admin"))])
def listar_auditoria(
    accion: Optional[str] = Query(None, description="p. ej. login_fallido, bloqueo_usuario, soft_edit"),
    usuario_id: Optional[int] = Query(None),
    limite: int = Query(200, ge=1, le=1000),
    db=Depends(get_db),
):
    """Log de auditoría (más reciente primero). Solo admin."""
    condiciones, valores = [], []
    if accion:
        condiciones.append("a.accion = %s"); valores.append(accion)
    if usuario_id is not None:
        condiciones.append("a.usuario_id = %s"); valores.append(usuario_id)
    where = ("WHERE " + " AND ".join(condiciones)) if condiciones else ""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        # auditoria.fecha es TIMESTAMP sin zona: now() la guarda en la zona
        # horaria de la base (en Neon, UTC). "AT TIME ZONE current_setting(...)"
        # le pega esa zona, así la API entrega la hora con zona explícita y la
        # interfaz la muestra en hora de Colombia sin corrimientos de 5 horas.
        f"""SELECT a.id, a.usuario_id, u.correo, a.accion, a.tabla, a.registro_id,
                   a.detalle, a.fecha AT TIME ZONE current_setting('TimeZone') AS fecha
              FROM auditoria a LEFT JOIN usuarios u ON u.id = a.usuario_id
              {where} ORDER BY a.id DESC LIMIT %s;""",
        (*valores, limite),
    )
    filas = cur.fetchall()
    cur.close()
    return filas


@app.post("/usuarios", response_model=UsuarioOut, status_code=201,
          dependencies=[Depends(requiere_rol("admin"))])
def crear_usuario(usuario: UsuarioCreate, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            f"""INSERT INTO usuarios (nombre, correo, contrasena_hash, rol, paciente_id, eps_nombre)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING {COLUMNAS_USUARIO};""",
            (usuario.nombre, usuario.correo, hash_password(usuario.contrasena),
             usuario.rol, usuario.paciente_id, usuario.eps_nombre),
        )
    except psycopg2.errors.UniqueViolation:
        db.rollback(); cur.close()
        raise HTTPException(status_code=409, detail="Ya existe un usuario con ese correo")
    except psycopg2.errors.ForeignKeyViolation:
        db.rollback(); cur.close()
        raise HTTPException(status_code=400, detail="paciente_id no existe")
    fila = cur.fetchone()
    db.commit()
    cur.close()
    return fila


@app.get("/pacientes", response_model=List[PacienteOut],
         dependencies=[Depends(requiere_rol("admin", "medico"))])
def listar_pacientes(
    q: Optional[str] = Query(None, description="Busca por documento o por nombre"),
    db=Depends(get_db),
):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if q:
        patron = f"%{q.strip()}%"
        cur.execute(
            f"""SELECT {COLUMNAS_PACIENTE} FROM pacientes
               WHERE documento ILIKE %s OR nombre ILIKE %s ORDER BY id;""",
            (patron, patron),
        )
    else:
        cur.execute(f"SELECT {COLUMNAS_PACIENTE} FROM pacientes ORDER BY id;")
    filas = cur.fetchall()
    cur.close()
    return filas


@app.post("/pacientes", response_model=PacienteOut, status_code=201,
          dependencies=[Depends(requiere_rol("admin", "medico"))])
def crear_paciente(paciente: PacienteCreate, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            f"""INSERT INTO pacientes (nombre, documento, genero, eps,
                                       fecha_nacimiento, telefono, tipo_sangre, alergias)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING {COLUMNAS_PACIENTE};""",
            (paciente.nombre, paciente.documento, paciente.genero, paciente.eps,
             paciente.fecha_nacimiento, paciente.telefono, paciente.tipo_sangre, paciente.alergias),
        )
    except psycopg2.errors.UniqueViolation:
        db.rollback(); cur.close()
        raise HTTPException(status_code=409, detail="Ya existe un paciente con ese documento")
    fila = cur.fetchone()
    db.commit()
    cur.close()
    return fila


def obtener_paciente_visible(db, paciente_id: int, usuario: dict) -> dict:
    """Devuelve el paciente solo si este usuario puede verlo (si no, 404: no se
    revela si existe). Admin y médico ven a todos; la EPS, a sus afiliados; el
    paciente, solo a sí mismo. La usan la ficha y las imágenes del PACS."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SELECT {COLUMNAS_PACIENTE} FROM pacientes WHERE id = %s;", (paciente_id,))
    p = cur.fetchone()
    cur.close()
    visible = p is not None and (
        usuario["rol"] in ("admin", "medico")
        or (usuario["rol"] == "eps" and p["eps"] == usuario["eps_nombre"])
        or (usuario["rol"] == "paciente" and p["id"] == usuario["paciente_id"])
    )
    if not visible:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    return p


@app.get("/pacientes/{paciente_id}", response_model=PacienteOut)
def obtener_paciente(paciente_id: int, db=Depends(get_db), usuario=Depends(get_usuario_actual)):
    """Ficha del paciente (la pestaña "Datos del paciente" de la interfaz)."""
    return obtener_paciente_visible(db, paciente_id, usuario)


@app.get("/remisiones")
def listar_remisiones(
    estado: Optional[str] = Query(None),
    paciente_id: Optional[int] = Query(None),
    incluir_inactivas: bool = Query(False, description="Solo admin: incluye las eliminadas (soft delete)"),
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

# timeout corto: si Mongo no responde, el error sale en 5 s y no en 30
mongo_client = pymongo.MongoClient(MONGO_CONNECTION_STRING, serverSelectionTimeoutMS=5000)
db_mongo = mongo_client["trazared_huv"]
coleccion_gestiones = db_mongo["gestiones_contacto"]


def _con_zona(valor):
    """MongoDB guarda las fechas en UTC pero pymongo las devuelve "sin zona":
    se les marca UTC para que la interfaz las muestre en hora de Colombia
    (sin esto, un contacto de la 1:00 p. m. aparecía a las 6:00 p. m.)."""
    if isinstance(valor, datetime) and valor.tzinfo is None:
        return valor.replace(tzinfo=timezone.utc)
    return valor


def documento_a_dict(doc):
    doc["_id"] = str(doc["_id"])
    doc["actualizado_en"] = _con_zona(doc.get("actualizado_en"))
    for c in doc.get("contactos") or []:
        c["fecha"] = _con_zona(c.get("fecha"))
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


# ----------------------------------------------------------------------------
# SEMANA 8 — Imágenes médicas (PACS Orthanc)
# ----------------------------------------------------------------------------
# Las imágenes se guardan como DICOM en Orthanc, enlazadas al paciente por el
# tag PatientID = documento. El navegador nunca le habla a Orthanc: pasa por
# aquí, donde se verifica el token, el rol y se audita cada acceso.
import io
import pacs
from fastapi import File, Form, UploadFile, Response

MAX_IMAGEN = 15 * 1024 * 1024   # 15 MB


@app.get("/pacientes/{paciente_id}/imagenes", tags=["Imágenes (PACS)"])
def listar_imagenes(paciente_id: int, db=Depends(get_db),
                    usuario=Depends(requiere_rol("admin", "medico"))):
    paciente = obtener_paciente_visible(db, paciente_id, usuario)
    return {"imagenes": pacs.list_images(paciente["documento"])}


@app.post("/pacientes/{paciente_id}/imagenes", status_code=201, tags=["Imágenes (PACS)"])
def subir_imagen(
    paciente_id: int,
    archivo: UploadFile = File(...),
    descripcion: str = Form("Imagen clínica"),
    modalidad: str = Form("OT"),
    db=Depends(get_db),
    usuario=Depends(requiere_rol("admin", "medico")),
):
    from PIL import Image
    paciente = obtener_paciente_visible(db, paciente_id, usuario)

    datos = archivo.file.read(MAX_IMAGEN + 1)
    if len(datos) > MAX_IMAGEN:
        raise HTTPException(status_code=413, detail="La imagen supera los 15 MB")
    # No se confía en el nombre ni en el tipo declarado: se abre la imagen de verdad.
    try:
        img = Image.open(io.BytesIO(datos))
        formato = img.format
        img.load()
    except Exception:
        raise HTTPException(status_code=422, detail="El archivo no es una imagen válida")
    if formato not in ("PNG", "JPEG"):
        raise HTTPException(status_code=422, detail="Solo se aceptan imágenes PNG o JPEG")
    if max(img.size) > 4096:
        img.thumbnail((4096, 4096))
    if img.mode.startswith("I"):                   # PNG de 16 bits en escala de grises
        img = img.point(lambda v: v / 256).convert("L")
    elif img.mode not in ("L", "RGB"):
        img = img.convert("RGB")
    limpio = io.BytesIO()                          # re-codificar elimina metadatos ocultos (EXIF, GPS)
    img.save(limpio, format="PNG")

    instance_id = pacs.dicomize(limpio.getvalue(), paciente, descripcion, modalidad.upper(),
                                datetime.now().strftime("%Y%m%d"))
    registrar_auditoria(db, usuario["id"], "imagen_subida", "pacientes", paciente_id,
                        f"{descripcion[:60]} ({modalidad.upper()}) · instancia {instance_id}")
    return {"instance_id": instance_id}


@app.get("/imagenes/{instance_id}/preview", tags=["Imágenes (PACS)"])
def ver_imagen(instance_id: str, db=Depends(get_db),
               usuario=Depends(requiere_rol("admin", "medico"))):
    """PNG de la imagen, solo si el paciente es visible para este usuario."""
    pacs.check_instance_id(instance_id)
    documento = pacs.instance_patient_id(instance_id)
    cur = db.cursor()
    cur.execute("SELECT id FROM pacientes WHERE documento = %s;", (documento,))
    fila = cur.fetchone()
    cur.close()
    if fila is None:
        raise HTTPException(status_code=404, detail="Imagen no encontrada")
    obtener_paciente_visible(db, fila[0], usuario)
    png = pacs.orthanc("GET", f"/instances/{instance_id}/preview").content
    registrar_auditoria(db, usuario["id"], "imagen_vista", "pacientes", fila[0], f"instancia {instance_id}")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=300"})


# ----------------------------------------------------------------------------
# SEMANA 8 — Interfaz gráfica (frontend/) servida por la misma API
# ----------------------------------------------------------------------------
# La interfaz (HTML + CSS + JavaScript, tipo SPA) vive en la carpeta frontend/
# de la raíz y se publica en /app. Al estar en el mismo origen que la API no
# hay problemas de CORS, y el túnel de Cloudflare la expone sin configurar nada
# más: https://<tunel>/app
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

RUTA_FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/app", include_in_schema=False)
def redirigir_app():
    return RedirectResponse(url="/app/")


if RUTA_FRONTEND.is_dir():
    app.mount("/app", StaticFiles(directory=RUTA_FRONTEND, html=True), name="frontend")
