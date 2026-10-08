"""
auth.py — Configuración, conexiones, usuarios, login y auditoría.

Es el archivo que importan todos los demás módulos, por eso aquí está también
lo que se comparte: las variables de pass.env, la conexión a PostgreSQL
(get_db) y el cliente de MongoDB.

Contiene:
  * Variables de entorno y conexiones (PostgreSQL y MongoDB).
  * Contraseñas (bcrypt) y tokens JWT.
  * get_usuario_actual y requiere_rol: las dependencias que usan TODAS las
    rutas protegidas.
  * registrar_auditoria: la usan todos los módulos para dejar rastro.
  * Rutas: /login (con bloqueo al 3er intento), /me, /usuarios, /auditoria.
  * crear_usuarios_iniciales: la llama main.py al arrancar.
"""
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import psycopg2
import psycopg2.extras
import pymongo
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, field_validator

router = APIRouter(tags=["Usuarios y acceso"])


# ── Variables de entorno ──────────────────────────────────────────────────
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


# ── Conexiones ────────────────────────────────────────────────────────────
def get_db():
    """Una conexión a PostgreSQL por petición; FastAPI la cierra al terminar."""
    conn = psycopg2.connect(PG_CONNECTION_STRING)
    try:
        yield conn
    finally:
        conn.close()


# timeout corto: si Mongo no responde, el error sale en 5 s y no en 30
mongo_client = pymongo.MongoClient(MONGO_CONNECTION_STRING, serverSelectionTimeoutMS=5000)
db_mongo = mongo_client["trazared_huv"]


def con_zona(valor):
    """MongoDB guarda las fechas en UTC pero pymongo las devuelve "sin zona":
    se les marca UTC para que la interfaz las muestre en hora de Colombia
    (sin esto, un contacto de la 1:00 p. m. aparecía a las 6:00 p. m.)."""
    if isinstance(valor, datetime) and valor.tzinfo is None:
        return valor.replace(tzinfo=timezone.utc)
    return valor


# ── Modelos ───────────────────────────────────────────────────────────────
# Usuario (correo) seguro: solo letras, números y . _ - @ ; máximo 50 caracteres.
# Así se evita que un nombre de usuario lleve comillas, ;, $, #, espacios, etc.
# (primera barrera contra inyección SQL, tal como se vio en la clase de la semana 8).
PATRON_USUARIO = re.compile(r"^[A-Za-z0-9._@-]{3,50}$")

# Roles válidos (si se agrega uno, también va en el CHECK de usuarios en db/schema.sql)
ROLES = ("admin", "medico", "eps", "paciente")


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
        if v not in ROLES:
            raise ValueError("rol debe ser uno de: " + ", ".join(ROLES))
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


COLUMNAS_USUARIO = "id, nombre, correo, rol, activo, paciente_id, eps_nombre, intentos_fallidos, bloqueado"


# ── Contraseñas y tokens ──────────────────────────────────────────────────
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


# ── Auditoría ─────────────────────────────────────────────────────────────
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


@router.post("/login", response_model=Token, responses={
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


@router.get("/me", response_model=UsuarioOut)
def quien_soy(usuario=Depends(get_usuario_actual), db=Depends(get_db)):
    """Datos del usuario de la sesión (lo usa la interfaz para saber qué mostrar)."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SELECT {COLUMNAS_USUARIO} FROM usuarios WHERE id = %s;", (usuario["id"],))
    fila = cur.fetchone()
    cur.close()
    return fila


@router.get("/usuarios", response_model=List[UsuarioOut],
            dependencies=[Depends(requiere_rol("admin"))])
def listar_usuarios(solo_bloqueados: bool = Query(False), db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    filtro = "WHERE bloqueado = TRUE" if solo_bloqueados else ""
    cur.execute(f"SELECT {COLUMNAS_USUARIO} FROM usuarios {filtro} ORDER BY id;")
    filas = cur.fetchall()
    cur.close()
    return filas


@router.post("/usuarios/{usuario_id}/desbloquear", response_model=UsuarioOut)
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


@router.get("/auditoria", dependencies=[Depends(requiere_rol("admin"))])
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


@router.post("/usuarios", response_model=UsuarioOut, status_code=201,
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


# ----------------------------------------------------------------------------
# Arranque en Docker: usuarios iniciales (main.py la llama en el "startup")
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


def crear_usuarios_iniciales():
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
