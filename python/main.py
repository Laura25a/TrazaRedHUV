"""
main.py — Arma la API de TrazaRed HUV.

Desde el Corte 2 la API está repartida en módulos, para que cada integrante
trabaje en su archivo sin pisar el de los demás:

  auth.py          configuración, conexiones, login con bloqueo, roles, /me, usuarios, auditoría
  remisiones.py    remisiones, soft delete, observaciones, gestiones de contacto
  ia.py            análisis de IA con aprobación y seudonimización (Persona 2)
  contabilidad.py  facturación en MongoDB (Persona 3)
  eventos.py       publicar(): avisos, tiempo real y flujos automáticos (Persona 4)
  main.py          (este) crea la app, incluye los routers, pacientes e imágenes del PACS

Para agregar rutas se escriben en el módulo que corresponde con @router.get(...)
y quedan publicadas solas, porque aquí se incluyen todos los routers.
"""
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

import psycopg2
import psycopg2.extras
import os

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import auth
import contabilidad
import ia
import remisiones
from auth import (PG_CONNECTION_STRING, ROLES_CLINICOS, get_db, get_usuario_actual,
                  mongo_client, registrar_auditoria, requiere_rol)

app = FastAPI(
    title="API TrazaRed HUV",
    description="API de trazabilidad de remisiones y traslados de pacientes del HUV, con roles y FHIR.",
    version="3.0.0",
)

# ----------------------------------------------------------------------------
# R03 · Seguridad que aplica a TODAS las rutas
# ----------------------------------------------------------------------------
# CORS restringido: solo los orígenes de CORS_ORIGINS (en .env, separados por
# comas) reciben Access-Control-Allow-Origin. Un origen desconocido no recibe
# "*" ni su propio origen reflejado. La interfaz no lo necesita porque nginx la
# sirve desde el mismo origen que la API (/ y /api).
ORIGENES_CORS = [o.strip().rstrip("/") for o in
                 os.getenv("CORS_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080").split(",")
                 if o.strip() and o.strip() != "*"]          # "*" nunca se acepta
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGENES_CORS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Cabeceras de seguridad en todas las respuestas de la API (nginx también las pone):
#   nosniff -> el navegador no "adivina" el tipo de archivo
#   DENY    -> nadie puede meter la aplicación dentro de un iframe (clickjacking)
CABECERAS_SEGURIDAD = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


@app.middleware("http")
async def cabeceras_de_seguridad(request, call_next):
    respuesta = await call_next(request)
    for nombre, valor in CABECERAS_SEGURIDAD.items():
        respuesta.headers.setdefault(nombre, valor)
    return respuesta


# Cada módulo trae sus rutas en un "router"; aquí se conectan a la app.
app.include_router(auth.router)
app.include_router(remisiones.router)
app.include_router(ia.router)
app.include_router(contabilidad.router)


@app.on_event("startup")
def al_arrancar():
    auth.migrar_base()               # db/schema.sql al día (roles nuevos, columnas nuevas...)
    auth.crear_usuarios_iniciales()  # un usuario de prueba por rol


@app.get("/")
def inicio():
    return {"mensaje": "TrazaRed HUV API - viva y funcionando"}


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


# ----------------------------------------------------------------------------
# Pacientes
# ----------------------------------------------------------------------------
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


@app.get("/pacientes", response_model=List[PacienteOut],
         dependencies=[Depends(requiere_rol(*ROLES_CLINICOS))])
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
def crear_paciente(paciente: PacienteCreate, db=Depends(get_db), usuario=Depends(get_usuario_actual)):
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
    # R13: sin datos personales en el detalle; el id basta para rastrearlo
    registrar_auditoria(db, usuario["id"], "paciente_creado", "pacientes", fila["id"])
    return fila


def obtener_paciente_visible(db, paciente_id: int, usuario: dict) -> dict:
    """Devuelve el paciente solo si este usuario puede verlo (si no, 404: no se
    revela si existe). Admin, médico y especialista ven a todos; la EPS, a sus afiliados; el
    paciente, solo a sí mismo. La usan la ficha, las imágenes del PACS y la historia clínica.
    R03: el contable nunca ve datos clínicos -> 403."""
    if usuario["rol"] == "contable":
        raise HTTPException(status_code=403, detail="El rol contable no tiene acceso a datos clínicos")
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SELECT {COLUMNAS_PACIENTE} FROM pacientes WHERE id = %s;", (paciente_id,))
    p = cur.fetchone()
    cur.close()
    visible = p is not None and (
        usuario["rol"] in ROLES_CLINICOS
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
                    usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
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
               usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
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
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

RUTA_FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/app", include_in_schema=False)
def redirigir_app():
    return RedirectResponse(url="/app/")


if RUTA_FRONTEND.is_dir():
    app.mount("/app", StaticFiles(directory=RUTA_FRONTEND, html=True), name="frontend")
