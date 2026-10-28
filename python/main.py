"""
main.py — Arma la API de TrazaRed HUV.

Desde el Corte 2 la API está repartida en módulos, para que cada integrante
trabaje en su archivo sin pisar el de los demás:

  auth.py          configuración, conexiones, login con bloqueo, roles, /me, usuarios, auditoría
  remisiones.py    remisiones, soft delete, observaciones, gestiones de contacto
  ia.py            análisis de IA con aprobación y seudonimización (Persona 2)
  contabilidad.py  libro contable por radicado + facturas en MongoDB
  registro.py      libro de radicación, formato de la remisión, descargas e IA de remisiones
  libro.py         (sin rutas) cómo se arma un asiento del libro; lo usan registro y contabilidad
  exportes.py      (sin rutas) archivos descargables: CSV, Excel, PDF
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
import threading

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import auth
import contabilidad
import eventos
import historia
import ia
import registro
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
app.include_router(historia.router)
app.include_router(ia.router)
app.include_router(contabilidad.router)
app.include_router(registro.router)
app.include_router(eventos.router)


def _suscripcion_fhir_al_arrancar():
    """R21: deja creada la Subscription rest-hook de HAPI FHIR (glucosa LOINC
    2339-0). Sin ella, el servidor FHIR publica la Observation pero nadie
    recibe el aviso. La función ya espera a que HAPI bootee, reintenta y es
    idempotente (reemplaza la Subscription anterior)."""
    try:
        import fhir_sync
        fhir_sync.crear_subscription_fhir()
    except Exception as e:              # sin FHIR arriba, la API igual sirve
        print(f"[FHIR] no se pudo crear la Subscription al arrancar: {e}")


@app.on_event("startup")
def al_arrancar():
    auth.migrar_base()               # db/schema.sql al día (roles nuevos, columnas nuevas...)
    auth.crear_usuarios_iniciales()  # un usuario de prueba por rol
    # En un hilo aparte para no retrasar el arranque de la API mientras
    # HAPI FHIR termina de bootear (puede tardar ~30 s la primera vez).
    threading.Thread(target=_suscripcion_fhir_al_arrancar, daemon=True,
                     name="suscripcion-fhir").start()
    # IA del libro de radicación: pronostica sola las remisiones abiertas al
    # arrancar y luego cada PRONOSTICO_CADA_MIN minutos (por defecto 30)
    registro.iniciar_pronosticos_automaticos()


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

    # R15 · Interoperabilidad FHIR: sincronizar el paciente con HAPI FHIR.
    # Si HAPI no responde, se registra el error pero no se tumba la creación
    # del paciente en Postgres (ya está guardado y es lo importante).
    try:
        import fhir_sync
        fhir_id = fhir_sync.sincronizar_paciente(fila["id"])
        print(f"[FHIR] Patient {fhir_id} creado para paciente {fila['id']} ({fila['documento']})")
    except Exception as e:
        print(f"[FHIR] no se pudo sincronizar el paciente {fila['id']}: {e}")

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
# R21 · Interoperabilidad reactiva con FHIR
# ----------------------------------------------------------------------------
# HAPI FHIR, al detectar una Observation nueva de glucosa (LOINC 2339-0),
# hace POST aquí (rest-hook). Identificamos al paciente por subject.reference,
# notificamos al médico y dejamos constancia en la auditoría con origen FHIR.
#
# También ofrecemos /fhir/observacion/simular para probar el flujo completo
# desde la interfaz sin necesidad de Postman.
class ObservacionFHIRIn(BaseModel):
    resourceType: str
    id: Optional[str] = None
    status: Optional[str] = None
    code: Optional[dict] = None
    subject: Optional[dict] = None
    valueQuantity: Optional[dict] = None
    effectiveDateTime: Optional[str] = None


class SimularObservacionIn(BaseModel):
    paciente_id: int
    valor: float
    unidad: str = "mg/dL"


@app.post("/fhir/observacion", tags=["R21 FHIR reactivo"], status_code=202)
@app.put("/fhir/observacion/Observation/{observation_id}", tags=["R21 FHIR reactivo"], status_code=202)
async def recibir_observacion_fhir(
    observacion: ObservacionFHIRIn,
    observation_id: Optional[str] = None,
):
    """R21: receptor del rest-hook de HAPI FHIR para Observation de glucosa.

    Acepta dos formas:
      * POST /fhir/observacion                     → pruebas manuales.
      * PUT  /fhir/observacion/Observation/{id}    → formato real de HAPI FHIR.

    HAPI FHIR reintenta el webhook varias veces si no recibe una respuesta
    que considere "válida". Para no crear notificaciones duplicadas, usamos
    el fhir_observation_id como clave de idempotencia: si ya procesamos esa
    Observation, no volvemos a notificar.
    """
    import fhir_sync
    from eventos import publicar

    # 0. Idempotencia: ¿ya procesamos esta Observation?
    obs_id = observacion.id or observation_id or "desconocido"

    conn = psycopg2.connect(PG_CONNECTION_STRING)
    try:
        cur = conn.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS fhir_observaciones_procesadas (
                   fhir_id TEXT PRIMARY KEY,
                   paciente_id INTEGER,
                   valor NUMERIC,
                   procesado_en TIMESTAMP DEFAULT now()
               );"""
        )
        cur.execute(
            "SELECT 1 FROM fhir_observaciones_procesadas WHERE fhir_id = %s;",
            (obs_id,),
        )
        ya_procesada = cur.fetchone() is not None

        if ya_procesada:
            print(f"[FHIR] Observation {obs_id} ya procesada; reintento ignorado")
            cur.close()
            return {"recibido": True, "motivo": "ya procesada", "fhir_id": obs_id}

        # 1. Identificar al paciente por subject.reference
        referencia = (observacion.subject or {}).get("reference") or ""
        paciente_id = fhir_sync.paciente_id_por_fhir_reference(referencia)
        if paciente_id is None:
            print(f"[FHIR] Observation recibida sin paciente conocido: {referencia}")
            cur.close()
            return {"recibido": False, "motivo": "paciente no encontrado"}

        # 2. Extraer el valor de glucosa
        valor = (observacion.valueQuantity or {}).get("value")
        unidad = (observacion.valueQuantity or {}).get("unit") or "mg/dL"

        # 3. Marcar como procesada ANTES de notificar (evita race conditions)
        cur.execute(
            """INSERT INTO fhir_observaciones_procesadas (fhir_id, paciente_id, valor)
               VALUES (%s, %s, %s) ON CONFLICT (fhir_id) DO NOTHING;""",
            (obs_id, paciente_id, valor),
        )
        conn.commit()

        # 4. Notificar a los clínicos activos
        cur.execute(
            """SELECT id FROM usuarios
               WHERE activo = TRUE AND rol IN ('medico','especialista','admin');"""
        )
        responsables = [f[0] for f in cur.fetchall()]
        cur.close()
    finally:
        conn.close()

    # 5. Publicar evento
    publicar(responsables, {
        "tipo": "observacion_fhir",
        "mensaje": f"Glucosa {valor} {unidad} recibida por FHIR para el paciente {paciente_id}",
        "paciente_id": paciente_id,
        "valor_glucosa": valor,
        "unidad": unidad,
        "origen": "FHIR",
        "fhir_observation_id": obs_id,
    })

    return {"recibido": True, "paciente_id": paciente_id, "valor": valor, "fhir_id": obs_id}


@app.post("/fhir/observacion/simular", tags=["R21 FHIR reactivo"], status_code=201)
def simular_observacion(body: SimularObservacionIn,
                        usuario=Depends(requiere_rol("admin", "medico"))):
    """R21: simula la llegada de una Observation desde otro sistema.

    Publica la Observation en HAPI FHIR; HAPI, al cumplir el criteria de la
    Subscription, hace POST al endpoint /fhir/observacion y el médico recibe
    la notificación en vivo. Sirve para probar desde la interfaz sin Postman.
    """
    import fhir_sync
    try:
        resultado = fhir_sync.sincronizar_observacion_fhir(
            paciente_id=body.paciente_id,
            valor=body.valor,
            unidad=body.unidad,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo publicar en HAPI FHIR: {e}")
    return {
        "fhir_observation_id": resultado.get("id"),
        "paciente_id": body.paciente_id,
        "valor": body.valor,
        "unidad": body.unidad,
        "mensaje": "Observation publicada; la Subscription notificará al médico.",
    }

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
