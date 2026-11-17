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
from typing import List, Literal, Optional

import psycopg2
import psycopg2.extras
import os
import re
import requests
import threading

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator

import auth
import contabilidad
import cripto
import eventos
import fhir_sync
import historia
import ia
import registro
import reactividad
import remisiones
from auth import (PG_CONNECTION_STRING, ROLES_CLINICOS, get_db, get_usuario_actual,
                  mongo_client, registrar_auditoria, requiere_rol)
from rag.router import router as rag_router   # asistente clínico RAG (taller semana 12)
from agente.router import router as agente_router, iniciar_agente, cerrar_agente   # agente (semana 13)
import tablero                                   # tablero de población y tendencia (semana 10)
import recuperacion                              # eliminar/recuperar imágenes · ingeniería biomédica
import voz                                       # bitácora por voz con IA
import seguridad                                 # proyecto final: STRIDE (límite de tasa, guardia, panel)
from validaciones import exigir_asignacion_clinica, hoy_bogota
import interop                                   # proyecto final: interoperabilidad con el sistema socio

app = FastAPI(
    title="API TrazaRed HUV",
    description="API de trazabilidad de remisiones y traslados de pacientes del HUV, con roles y FHIR.",
    version="3.0.0",
    root_path=os.getenv("TRAZARED_API_ROOT_PATH", ""),
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
    allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
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


# Proyecto final · Denial of service: límite de peticiones por IP (10 por ventana en
# login, token de sistema, agente y entrada del socio). Va después de las cabeceras
# para que un 429 también salga con ellas.
app.middleware("http")(seguridad.limite_de_tasa)

# Cada módulo trae sus rutas en un "router"; aquí se conectan a la app.
@app.exception_handler(RequestValidationError)
async def errores_validacion(request: Request, exc: RequestValidationError):
    errores = [{k: v for k, v in error.items() if k != "input"} for error in exc.errors()]
    return JSONResponse(status_code=422, content=jsonable_encoder({"detail": errores}))


app.include_router(auth.router)
app.include_router(remisiones.router)
app.include_router(historia.router)
app.include_router(ia.router)
app.include_router(contabilidad.router)
app.include_router(registro.router)
app.include_router(rag_router)
app.include_router(agente_router)
app.include_router(tablero.router)
app.include_router(recuperacion.router)
app.include_router(voz.router)
app.include_router(seguridad.router)
app.include_router(interop.router)
app.include_router(eventos.router)


def _suscripcion_fhir_al_arrancar():
    """R21: deja creada la Subscription rest-hook de HAPI FHIR (glucosa LOINC
    2339-0). Sin ella, el servidor FHIR publica la Observation pero nadie
    recibe el aviso. La función ya espera a que HAPI bootee, reintenta y es
    idempotente (reemplaza la Subscription anterior)."""
    try:
        import fhir_sync
        fhir_sync.crear_subscription_fhir(max_intentos=60, espera_seg=5)
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
    threading.Thread(target=fhir_sync.ejecutar_replica, daemon=True, name="replica-fhir").start()
    # IA del libro de radicación: pronostica sola las remisiones abiertas al
    # arrancar y luego cada PRONOSTICO_CADA_MIN minutos (por defecto 30)
    registro.iniciar_pronosticos_automaticos()
    eventos.iniciar_escalamiento()
    ia.iniciar_analisis_automatico()
    # RAG: guías con su vector en pgvector (si la imagen de Postgres lo trae)
    threading.Thread(target=_preparar_rag, daemon=True, name="rag-pgvector").start()


def _preparar_rag():
    from rag.motor import preparar_pgvector
    print(f"[RAG] {preparar_pgvector(PG_CONNECTION_STRING)['motivo']}")


@app.on_event("startup")
async def al_arrancar_agente():
    """Semana 13: el agente y su memoria en PostgreSQL (necesita el bucle async)."""
    try:
        await iniciar_agente()
    except Exception as e:                 # sin agente, el resto del sistema sigue funcionando
        print(f"[agente] no se pudo iniciar: {e}")


@app.on_event("shutdown")
async def al_cerrar():
    await cerrar_agente()


@app.get("/")
def inicio():
    return {"mensaje": "TrazaRed HUV API - viva y funcionando"}


@app.get("/health")
def health():
    """Estado de las dependencias (lo usa la interfaz para los indicadores)."""
    import pacs
    import requests
    from fhir_sync import FHIR_BASE_URL

    estado = {"api": "ok", "postgres": "error", "mongo": "error", "pacs": "error", "fhir": "error"}
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
    try:
        if requests.get(f"{FHIR_BASE_URL.rstrip('/')}/metadata", timeout=2).ok:
            estado["fhir"] = "ok"
    except requests.RequestException:
        pass
    return estado


# ----------------------------------------------------------------------------
# Pacientes
# ----------------------------------------------------------------------------
class DatosFicha(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nombre: str
    genero: str
    eps: str
    fecha_nacimiento: Optional[date] = None
    telefono: Optional[str] = None
    tipo_sangre: Optional[str] = None
    alergias: Optional[str] = None
    direccion: Optional[str] = None
    correo: Optional[str] = None

    @field_validator("nombre", "eps", "genero")
    @classmethod
    def texto_requerido(cls, valor):
        if not valor.strip():
            raise ValueError("El campo no puede quedar vacío")
        return valor.strip()

    @field_validator("fecha_nacimiento")
    @classmethod
    def nacimiento_valido(cls, valor):
        if valor and valor > hoy_bogota():
            raise ValueError("La fecha de nacimiento no puede ser futura")
        return valor


class PacienteCreate(DatosFicha):
    documento: str

    @field_validator("documento")
    @classmethod
    def documento_valido(cls, valor):
        normal = cripto.normalizar_documento(valor)
        if not normal or not normal.isalnum():
            raise ValueError("Indica un número o identificador de documento válido")
        return normal


class PacientePatch(DatosFicha):
    nombre: str = None
    genero: str = None
    eps: str = None


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
    direccion: Optional[str] = None
    correo: Optional[str] = None
    por_conciliar: bool = False


COLUMNAS_PACIENTE = ("id, nombre, documento, genero, eps, fecha_nacimiento, telefono, tipo_sangre, "
                     "alergias, direccion, correo, por_conciliar")


@app.get("/pacientes", response_model=List[PacienteOut],
         dependencies=[Depends(requiere_rol(*ROLES_CLINICOS))])
def listar_pacientes(
    q: Optional[str] = Query(None, description="Busca por documento (exacto) o por nombre"),
    db=Depends(get_db),
):
    """Prioriza el documento exacto mediante su índice ciego. Luego busca por nombre."""
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if q and q.strip():
            q = q.strip()
            cur.execute(f"SELECT {COLUMNAS_PACIENTE} FROM pacientes WHERE documento_idx = %s AND NOT por_conciliar;",
                        (cripto.indice_ciego(q),))
            exactos = cur.fetchall()
            if exactos:
                return cripto.descifrar_pacientes(exactos)
            cur.execute(f"SELECT {COLUMNAS_PACIENTE} FROM pacientes WHERE NOT por_conciliar AND nombre ILIKE %s ORDER BY id;",
                        (f"%{q}%",))
        else:
            cur.execute(f"SELECT {COLUMNAS_PACIENTE} FROM pacientes WHERE NOT por_conciliar ORDER BY id;")
        return cripto.descifrar_pacientes(cur.fetchall())


@app.post("/pacientes", response_model=PacienteOut, status_code=201,
          dependencies=[Depends(requiere_rol("admin", "medico"))])
def crear_paciente(paciente: PacienteCreate, db=Depends(get_db), usuario=Depends(get_usuario_actual)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # El documento entra ya con su índice ciego (que es el que garantiza que no se
        # repita) y, en la MISMA transacción, se cifra junto con teléfono, dirección y
        # correo: el texto en claro nunca llega a confirmarse en la base.
        cur.execute(
            """INSERT INTO pacientes (nombre, documento, documento_idx, genero, eps,
                                      fecha_nacimiento, tipo_sangre, alergias)
               VALUES (%s, '', %s, %s, %s, %s, %s, %s)
               RETURNING id;""",
            (paciente.nombre, cripto.indice_ciego(paciente.documento), paciente.genero, paciente.eps,
             paciente.fecha_nacimiento, paciente.tipo_sangre, paciente.alergias),
        )
        nuevo_id = cur.fetchone()["id"]
        cripto.cifrar_fila_paciente(cur, nuevo_id, {
            "documento": cripto.normalizar_documento(paciente.documento) or paciente.documento,
            "telefono": paciente.telefono, "direccion": paciente.direccion, "correo": paciente.correo})
        cur.execute(f"SELECT {COLUMNAS_PACIENTE} FROM pacientes WHERE id = %s;", (nuevo_id,))
    except psycopg2.errors.UniqueViolation:
        db.rollback(); cur.close()
        raise HTTPException(status_code=409, detail="Ya existe un paciente con ese documento")
    fila = cripto.descifrar_paciente(cur.fetchone())
    cur.close()

    # R13: sin datos personales en el detalle; el id basta para rastrearlo
    registrar_auditoria(db, usuario["id"], "paciente_creado", "pacientes", fila["id"])

    return fila


@app.patch("/pacientes/{paciente_id}", response_model=PacienteOut,
           dependencies=[Depends(requiere_rol("admin", "medico"))])
def corregir_paciente(paciente_id: int, ficha: PacientePatch, db=Depends(get_db),
                      usuario=Depends(get_usuario_actual)):
    exigir_asignacion_clinica(db, paciente_id, usuario)
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT {COLUMNAS_PACIENTE} FROM pacientes WHERE id = %s FOR UPDATE;", (paciente_id,))
        actual = cur.fetchone()
        if actual is None:
            raise HTTPException(404, "Paciente no encontrado")
        anterior = cripto.descifrar_paciente(actual)
        cambios = {k: v for k, v in ficha.model_dump(exclude_unset=True).items() if anterior[k] != v}
        if not cambios:
            return anterior

        for campo in cambios.keys() & set(cripto.CAMPOS_CIFRADOS):
            cambios[campo] = cripto.cifrar(cambios[campo], cripto.aad_paciente(paciente_id, campo))
        asignaciones = ", ".join(f"{campo} = %s" for campo in cambios)
        try:
            cur.execute(f"UPDATE pacientes SET {asignaciones} WHERE id = %s RETURNING {COLUMNAS_PACIENTE};",
                        (*cambios.values(), paciente_id))
        except psycopg2.errors.CheckViolation as exc:
            raise HTTPException(422, exc.diag.message_primary) from exc
        fila = cripto.descifrar_paciente(cur.fetchone())

    registrar_auditoria(db, usuario["id"], "paciente_corregido", "pacientes", paciente_id,
                        "Campos: " + ", ".join(cambios))
    return fila


def obtener_paciente_visible(db, paciente_id: int, usuario: dict) -> dict:
    """Devuelve el paciente solo si este usuario puede verlo (si no, 404: no se
    revela si existe). Admin, médico y especialista ven a todos; la EPS, a sus afiliados; el
    paciente, solo a sí mismo. La usan la ficha, las imágenes del PACS y la historia clínica.
    R03: el contable nunca ve datos clínicos -> 403."""
    if usuario["rol"] in ("contable", "director", "biomedico"):
        # El director ve la población (tablero), nunca la historia de un paciente
        raise HTTPException(status_code=403, detail=f"El rol {usuario['rol']} no tiene acceso a datos clínicos individuales")
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SELECT {COLUMNAS_PACIENTE} FROM pacientes WHERE id = %s;", (paciente_id,))
    p = cripto.descifrar_paciente(cur.fetchone())
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
    exigir_asignacion_clinica(db, paciente_id, usuario)
    ocultas = recuperacion.ids_eliminadas(db)          # borrado lógico: siguen en el PACS, no en la ficha
    return {"imagenes": [i for i in pacs.list_images(paciente["documento"]) if i["instance_id"] not in ocultas]}


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
    exigir_asignacion_clinica(db, paciente_id, usuario)

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
    episodio = reactividad.episodio_activo(db, paciente_id)
    _, destinos = reactividad.destino_episodio(db, episodio)
    eventos.registrar_evento(db, destinos, {
        "tipo": "imagen_clinica", "mensaje": "Hay una nueva imagen clínica para revisar",
        "paciente_id": paciente_id, "remision_id": episodio["id"] if episodio else None,
    })
    registrar_auditoria(db, usuario["id"], "imagen_subida", "pacientes", paciente_id,
                        f"{descripcion[:60]} ({modalidad.upper()}) · instancia {instance_id}")
    return {"instance_id": instance_id}


@app.get("/imagenes/{instance_id}/preview", tags=["Imágenes (PACS)"])
def ver_imagen(instance_id: str, db=Depends(get_db),
               usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    """PNG de la imagen, solo si el paciente es visible para este usuario."""
    pacs.check_instance_id(instance_id)
    if instance_id in recuperacion.ids_eliminadas(db):  # eliminada: solo se ve desde la papelera
        raise HTTPException(status_code=404, detail="Imagen no encontrada")
    documento = pacs.instance_patient_id(instance_id)
    cur = db.cursor()
    cur.execute("SELECT id FROM pacientes WHERE documento_idx = %s;", (cripto.indice_ciego(documento),))
    fila = cur.fetchone()
    cur.close()
    if fila is None:
        raise HTTPException(status_code=404, detail="Imagen no encontrada")
    obtener_paciente_visible(db, fila[0], usuario)
    exigir_asignacion_clinica(db, fila[0], usuario)
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
    meta: Optional[dict] = None


class SimularObservacionIn(BaseModel):
    paciente_id: int
    valor: float = Field(gt=0, lt=2000)
    unidad: Literal["mg/dL"] = "mg/dL"


def _validar_observacion_glucosa(obs: ObservacionFHIRIn) -> Optional[str]:
    if obs.resourceType != "Observation":
        return "Se esperaba un recurso Observation"
    codigos = (obs.code or {}).get("coding")
    if not isinstance(codigos, list) or not any(
        isinstance(c, dict) and (c.get("system"), c.get("code")) == ("http://loinc.org", "2339-0")
        for c in codigos
    ):
        return "La Observation debe traer el código LOINC 2339-0 (glucosa) con system http://loinc.org"
    cantidad = obs.valueQuantity or {}
    valor = cantidad.get("value")
    if not isinstance(valor, (int, float)) or isinstance(valor, bool) or not (0 < valor < 2000):
        return "valueQuantity.value debe ser un número de glucosa válido"
    if (cantidad.get("system") != fhir_sync.UCUM or
        cantidad.get("code", cantidad.get("unit")) != "mg/dL" or
        cantidad.get("unit", "mg/dL") != "mg/dL"):
        return "La glucosa debe expresarse en mg/dL con el sistema UCUM"
    referencia = (obs.subject or {}).get("reference")
    if not isinstance(referencia, str) or not re.fullmatch(r"Patient/[A-Za-z0-9.-]{1,64}", referencia):
        return "subject.reference debe apuntar a un Patient"
    return None


def _auditar_webhook_rechazado(request, motivo: str) -> None:
    """Deja el rechazo en la auditoría con la IP (origen externo). Nunca tumba la respuesta."""
    try:
        conn = psycopg2.connect(PG_CONNECTION_STRING, connect_timeout=5)
        try:
            registrar_auditoria(conn, None, "webhook_rechazado", "fhir", None,
                                f"origen=externo ip={seguridad.ip_cliente(request)} motivo={str(motivo)[:160]}")
        finally:
            conn.close()
    except Exception as e:
        print(f"[FHIR] no se pudo auditar el rechazo del webhook: {e}")


@app.post("/fhir/observacion", tags=["R21 FHIR reactivo"], status_code=202)
@app.put("/fhir/observacion/Observation/{observation_id}", tags=["R21 FHIR reactivo"], status_code=202)
def recibir_observacion_fhir(
    observacion: ObservacionFHIRIn,
    request: Request,
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

    # R29 · Entrada externa autenticada: solo quien conoce el secreto (HAPI FHIR lo
    # envía en la cabecera X-Webhook-Secret de la Subscription) puede avisar. Sin
    # cabecera: 401. Con secreto equivocado: 403. Y el recurso se valida ANTES de
    # tocar la base: debe ser una Observation de glucosa (LOINC 2339-0) con valor numérico.
    try:
        seguridad.exigir_webhook(request)
    except HTTPException as e:
        _auditar_webhook_rechazado(request, e.detail)
        raise
    etiquetas = (observacion.meta or {}).get("tag")
    if isinstance(etiquetas, list) and fhir_sync.ETIQUETA_REPLICA in etiquetas:
        return {"recibido": True, "motivo": "réplica local"}
    problema = _validar_observacion_glucosa(observacion)
    if problema:
        _auditar_webhook_rechazado(request, problema)
        raise HTTPException(status_code=422, detail=problema)

    obs_id = observacion.id or observation_id
    if not obs_id or not re.fullmatch(r"[A-Za-z0-9.-]{1,64}", obs_id) or (observation_id and observation_id != obs_id):
        raise HTTPException(422, "La Observation debe tener un id válido y consistente")
    referencia = observacion.subject["reference"]
    try:
        paciente_id = fhir_sync.paciente_id_por_fhir_reference(referencia, reintentar=True)
    except requests.RequestException as exc:
        raise HTTPException(503, "No se pudo resolver el paciente en HAPI FHIR") from exc
    if paciente_id is None:
        return {"recibido": False, "motivo": "paciente no encontrado"}

    from contextlib import closing
    valor = observacion.valueQuantity["value"]
    with closing(psycopg2.connect(PG_CONNECTION_STRING)) as conn, conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO fhir_observaciones_procesadas (fhir_id, paciente_id, valor)
                       VALUES (%s, %s, %s) ON CONFLICT (fhir_id) DO NOTHING;""",
                    (obs_id, paciente_id, valor))
        if not cur.rowcount:
            return {"recibido": True, "motivo": "ya procesada", "fhir_id": obs_id}
        episodio = reactividad.episodio_activo(conn, paciente_id)
        reactividad.registrar_observaciones(conn, episodio, paciente_id, [{
            "tipo": "Glucosa", "codigo_loinc": "2339-0", "valor": valor, "unidad": "mg/dL",
        }], tipo="observacion_fhir", valor_glucosa=valor, unidad="mg/dL",
            origen="FHIR", fhir_observation_id=obs_id)

    return {"recibido": True, "paciente_id": paciente_id, "valor": valor, "fhir_id": obs_id}


@app.post("/fhir/observacion/simular", tags=["R21 FHIR reactivo"], status_code=201)
def simular_observacion(body: SimularObservacionIn, db=Depends(get_db),
                        usuario=Depends(requiere_rol("admin", "medico"))):
    """R21: simula la llegada de una Observation desde otro sistema.

    Publica la Observation en HAPI FHIR; HAPI, al cumplir el criteria de la
    Subscription, hace POST al endpoint /fhir/observacion y el médico recibe
    la notificación en vivo. Sirve para probar desde la interfaz sin Postman.
    """
    import fhir_sync

    exigir_asignacion_clinica(db, body.paciente_id, usuario)
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


# ── R15 / R21 · Entrada FHIR autenticada (reemplaza el acceso público a HAPI) ──
# Desde el proyecto final nginx ya no publica HAPI (docs/STRIDE.md, filas T6 e I6):
# HAPI guarda nombre y documento y no tiene autenticación propia. Estas dos rutas
# son la puerta FHIR hacia afuera: hablan FHIR (Patient, Observation) pero exigen
# JWT, rol clínico y que el paciente esté a cargo de quien pregunta; además dejan
# auditoría. La Observation se escribe en HAPI y su Subscription dispara R21 igual
# que antes (rest-hook → /fhir/observacion → notificación y alerta en vivo).
PATRON_IDENTIFICADOR = "urn:trazared:documento|"


def _patient_minimo(fhir_id: str, documento: str) -> dict:
    """Patient minimizado (Ley 1581/2012): identificador, sin nombre ni contacto."""
    return {"resourceType": "Patient", "id": fhir_id,
            "identifier": [{"system": "urn:trazared:documento", "value": documento}]}


@app.get("/fhir/Patient", tags=["R21 FHIR reactivo"])
def fhir_buscar_paciente(identifier: str = Query(..., max_length=80), db=Depends(get_db),
                         usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    """R15: busca (y si falta, sincroniza) el Patient de HAPI por documento.

    `identifier=urn:trazared:documento|<documento>`. Devuelve el Patient minimizado.
    """
    if not identifier.startswith(PATRON_IDENTIFICADOR) or not identifier[len(PATRON_IDENTIFICADOR):].strip():
        raise HTTPException(422, f"identifier debe ser {PATRON_IDENTIFICADOR}<documento>")
    documento = identifier[len(PATRON_IDENTIFICADOR):].strip()
    with db.cursor() as cur:
        cur.execute("SELECT id FROM pacientes WHERE documento_idx = %s;", (cripto.indice_ciego(documento),))
        fila = cur.fetchone()
    if fila is None:
        raise HTTPException(404, "Paciente no encontrado")
    exigir_asignacion_clinica(db, fila[0], usuario)
    try:
        fhir_id = fhir_sync.obtener_o_crear_paciente_fhir(documento)
    except Exception as e:
        raise HTTPException(502, f"No se pudo consultar HAPI FHIR: {type(e).__name__}")
    registrar_auditoria(db, usuario["id"], "fhir_patient_consultado", "pacientes", fila[0],
                        f"Patient/{fhir_id} por identificador")
    return _patient_minimo(fhir_id, documento)


@app.post("/fhir/Observation", tags=["R21 FHIR reactivo"], status_code=201)
def fhir_crear_observacion(observacion: ObservacionFHIRIn, db=Depends(get_db),
                           usuario=Depends(requiere_rol("admin", "medico"))):
    """R21: crea en HAPI una Observation de glucosa (LOINC 2339-0).

    HAPI aplica la Subscription y avisa a /fhir/observacion: el médico recibe la
    notificación en vivo y, si el valor es crítico, la alerta (R20).
    """
    problema = _validar_observacion_glucosa(observacion)
    if problema:
        raise HTTPException(422, problema)
    referencia = observacion.subject["reference"]
    paciente_id = fhir_sync.paciente_id_por_fhir_reference(referencia)
    if paciente_id is None:
        raise HTTPException(404, "El Patient referido no corresponde a un paciente del sistema")
    exigir_asignacion_clinica(db, paciente_id, usuario)
    recurso = observacion.model_dump(exclude_none=True)
    recurso.pop("id", None)
    recurso.pop("meta", None)
    recurso["valueQuantity"] = {"value": recurso["valueQuantity"]["value"],
                               "unit": "mg/dL", "system": fhir_sync.UCUM, "code": "mg/dL"}
    recurso["status"] = recurso.get("status") or "final"
    try:
        creado = fhir_sync.crear_recurso_fhir(recurso)
    except Exception as e:
        raise HTTPException(502, f"No se pudo publicar en HAPI FHIR: {type(e).__name__}")
    registrar_auditoria(db, usuario["id"], "fhir_observation_creada", "pacientes", paciente_id,
                        f"Observation/{creado.get('id')} glucosa={recurso['valueQuantity']['value']} {referencia}")
    return {"resourceType": "Observation", "id": creado.get("id"), "status": recurso["status"],
            "code": recurso["code"], "subject": {"reference": referencia},
            "valueQuantity": recurso["valueQuantity"]}

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
