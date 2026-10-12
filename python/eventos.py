"""
eventos.py — El "pegante" del sistema reactivo (Corte 2: R17, R18, R19, R20).

Regla del equipo: cuando pasa algo importante, el módulo que lo detecta NO
notifica ni factura por su cuenta; solo llama a publicar(). Así:

    from eventos import publicar, ids_con_rol

    publicar(ids_con_rol(db, "admin"), {
        "tipo": "usuario_bloqueado",
        "mensaje": "La cuenta medico@huv.gov.co se bloqueó por 3 intentos fallidos",
        "usuario_afectado": "medico@huv.gov.co",
    })

Quién lo llama (y quién lo llamará):
  * auth.py          -> "usuario_bloqueado"   (Persona 1, ya conectado)
  * remisiones.py    -> "remision_creada", "remision_aceptada"   (Persona 3)
  * ia.py            -> "reporte_aprobado", "alerta_critica"     (Persona 2)
  * fhir (rest-hook) -> "observacion_fhir"                       (Persona 3)

Qué hace publicar() hoy (Persona 4):
  1. REACCIONES (R19/R20): según el tipo, crea la factura automática en MongoDB
     ("reporte_aprobado"), la alerta crítica ("alerta_critica") o solo audita
     ("observacion_fhir"). Es el "hecho dispara consecuencias" del Corte 2.
  2. BANDEJA (R18): guarda una notificación por destinatario en la tabla
     notificaciones; sobrevive a recargas y solo su dueño la ve o la marca leída.
  3. TIEMPO REAL (R17): empuja el evento por el canal SSE de cada destinatario
     conectado (GET /eventos/stream). El paciente y el contable jamás reciben
     eventos clínicos, aunque un módulo los pase por error en destinatarios.

NUNCA lanza errores: un aviso que falla no puede tumbar el login ni una remisión.

IMPORTACIÓN CIRCULAR: auth.py importa este módulo desde su línea 34 (antes de
definir get_db/SECRET_KEY/db_mongo). Por eso aquí NO se hace "from auth import
..." a nivel de módulo: cada función importa lo que necesita cuando ya corre,
con auth completamente cargado.
"""
import asyncio
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(tags=["Sistema reactivo"])

# Eventos clínicos: jamás se entregan por el canal de un paciente o un contable.
EVENTOS_CLINICOS = {
    "remision_creada", "remision_aceptada", "reporte_aprobado",
    "alerta_critica", "alerta_escalada", "observacion_fhir",
}

# Segundos sin reconocer una alerta antes de escalarla al admin (R20).
ESCALAMIENTO_SEGUNDOS = int(os.getenv("ESCALAMIENTO_SEGUNDOS", "600"))

# Valor de la factura automática del análisis de IA (R19). La Persona 3 puede
# moverlo con la variable de entorno VALOR_ANALISIS_IA.
VALOR_ANALISIS_IA = float(os.getenv("VALOR_ANALISIS_IA", "80000"))


# ── Canal en tiempo real (R17) ────────────────────────────────────────────
# Cada conexión SSE deja aquí su asyncio.Queue; publicar() empuja a las colas
# de los destinatarios. Un solo proceso de API: no hace falta broker (REQUISITOS §8).
class _Suscripcion:
    def __init__(self, usuario_id: int, rol: str, cola: "asyncio.Queue", loop):
        self.usuario_id = usuario_id
        self.rol = rol
        self.cola = cola
        self.loop = loop


_suscripciones: dict = {}
_candado = threading.Lock()


def _suscribir(usuario_id: int, rol: str, cola, loop) -> _Suscripcion:
    sus = _Suscripcion(usuario_id, rol, cola, loop)
    with _candado:
        _suscripciones[id(sus)] = sus
    return sus


def _desuscribir(sus: _Suscripcion) -> None:
    with _candado:
        _suscripciones.pop(id(sus), None)


def _emitir(destinatarios: list, evento: dict) -> None:
    """Empuja el evento a las colas SSE de los destinatarios conectados."""
    texto = json.dumps(evento, default=str)
    clinico = evento.get("tipo") in EVENTOS_CLINICOS
    with _candado:
        suscripciones = list(_suscripciones.values())
    for sus in suscripciones:
        if sus.usuario_id not in destinatarios:
            continue
        # Defensa extra del aislamiento (R17): rol paciente/contable nunca recibe clínicos.
        if clinico and sus.rol in ("paciente", "contable"):
            continue
        try:
            sus.loop.call_soon_threadsafe(sus.cola.put_nowait, texto)
        except RuntimeError:      # el loop ya cerró: conexión muerta que aún no se desuscribió
            _desuscribir(sus)
        except asyncio.QueueFull:  # consumidor atascado: se descarta este evento, no la acción
            print(f"[evento] cola SSE llena para el usuario {sus.usuario_id}; evento descartado")


# ── Utilidades de acceso (imports diferidos: ver docstring) ────────────────
def _conexion_pg():
    import auth
    return psycopg2.connect(auth.PG_CONNECTION_STRING)


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Funciones públicas del contrato del equipo ────────────────────────────
def ids_con_rol(db, *roles: str) -> list:
    """ids de los usuarios activos que tienen alguno de esos roles
    (p. ej. todos los admin, para avisarles de un bloqueo)."""
    try:
        cur = db.cursor()
        cur.execute("SELECT id FROM usuarios WHERE activo = TRUE AND rol = ANY(%s) ORDER BY id;",
                    (list(roles),))
        ids = [fila[0] for fila in cur.fetchall()]
        cur.close()
        return ids
    except Exception as e:  # igual que publicar(): nunca rompe la acción que avisa
        print(f"[evento] no se pudieron buscar los usuarios {roles}: {e}")
        return []


def publicar(destinatarios: Iterable[int], evento: dict) -> None:
    """Publica un evento para una lista de usuarios (ids).

    evento: dict con al menos "tipo" y "mensaje"; "paciente_id" cuando aplique.
    La fecha se agrega aquí si no viene.

    Hace las tres cosas del sistema activo: reacciona (factura/alerta/auditoría),
    deja la notificación en la bandeja de cada destinatario y la empuja en vivo
    por el canal SSE. Nunca lanza excepciones.
    """
    try:
        destinatarios = [int(d) for d in destinatarios]
        evento = dict(evento)
        evento.setdefault("fecha", _ahora_iso())
        print(f"[evento] {evento.get('tipo')} -> usuarios {destinatarios}: {evento.get('mensaje')}")
        _reaccionar(destinatarios, evento)          # R19/R20: hecho -> consecuencia
        _guardar_notificaciones(destinatarios, evento)  # R18: bandeja persistente
        _emitir(destinatarios, evento)              # R17: tiempo real
    except Exception as e:  # un aviso nunca debe romper la acción que lo disparó
        print(f"[evento] no se pudo publicar: {e}")


# ── Reactores (R19/R20): el hecho dispara consecuencias ───────────────────
def _reaccionar(destinatarios: list, evento: dict) -> None:
    tipo = evento.get("tipo", "")
    if tipo == "reporte_aprobado":
        _facturar_analisis(evento)
    elif tipo == "alerta_critica":
        _crear_alerta(evento)
    elif tipo == "observacion_fhir":
        _auditar_evento_fhir(evento)


def _facturar_analisis(evento: dict) -> None:
    """R19: al aprobarse un reporte de IA se crea SOLA la factura en MongoDB,
    ligada al reporte por reporte_id (colección facturas de la Persona 3)."""
    try:
        from auth import db_mongo
        reporte_id = evento.get("reporte_id")
        doc = {
            "paciente_id": evento.get("paciente_id"),
            "servicio": "analisis_ia",
            "valor": VALOR_ANALISIS_IA,
            "fecha": datetime.now(timezone.utc),
            "estado_pago": "pendiente",
            "reporte_id": reporte_id,
            "origen": "automatico",
        }
        db_mongo["facturas"].insert_one(doc)
        print(f"[evento] factura automática creada para el reporte {reporte_id}")
        _auditar(None, "factura_creada", "facturas", reporte_id,
                 f"factura automática por reporte de IA {reporte_id}")
    except Exception as e:
        print(f"[evento] no se pudo facturar el reporte: {e}")


def _crear_alerta(evento: dict) -> None:
    """R20: el caso crítico crea una alerta 'activa' que solo se va con el
    reconocimiento de un clínico. Deja el id en el evento para bandeja y SSE."""
    try:
        db = _conexion_pg()
        cur = db.cursor()
        cur.execute(
            """INSERT INTO alertas (paciente_id, nivel, estado, mensaje, reporte_id, responsable_id)
               VALUES (%s, %s, 'activa', %s, %s, %s) RETURNING id;""",
            (evento.get("paciente_id"), evento.get("nivel", "critico"),
             evento.get("mensaje", "Caso crítico"), evento.get("reporte_id"),
             evento.get("responsable_id")),
        )
        alerta_id = cur.fetchone()[0]
        evento["alerta_id"] = alerta_id
        db.commit()
        cur.close()
        db.close()
        _auditar(None, "alerta_creada", "alertas", alerta_id, evento.get("mensaje", ""))
        print(f"[evento] alerta crítica {alerta_id} creada")
    except Exception as e:
        print(f"[evento] no se pudo crear la alerta: {e}")


def _auditar_evento_fhir(evento: dict) -> None:
    """R13: 'evento recibido desde FHIR' queda en la auditoría con origen FHIR."""
    try:
        _auditar(None, "evento_fhir", "fhir", evento.get("paciente_id"),
                 f"Observation recibida por rest-hook: {evento.get('mensaje', '')}")
    except Exception as e:
        print(f"[evento] no se pudo auditar el evento FHIR: {e}")


def _auditar(usuario_id, accion: str, tabla: str, registro_id, detalle: str = None) -> None:
    from auth import registrar_auditoria
    db = _conexion_pg()
    try:
        registrar_auditoria(db, usuario_id, accion, tabla, registro_id, detalle)
    finally:
        db.close()


# ── Bandeja (R18) ─────────────────────────────────────────────────────────
def _guardar_notificaciones(destinatarios: list, evento: dict) -> None:
    """Una notificación por destinatario. Los campos extra del hecho van en
    'datos' (JSONB) y salen planos en la respuesta de GET /notificaciones."""
    if not destinatarios:
        return
    estandar = {"tipo", "mensaje", "fecha", "paciente_id", "reporte_id",
                "remision_id", "alerta_id"}
    extras = {k: v for k, v in evento.items() if k not in estandar}
    try:
        db = _conexion_pg()
        cur = db.cursor()
        for usuario_id in destinatarios:
            cur.execute(
                """INSERT INTO notificaciones
                       (usuario_id, tipo, mensaje, paciente_id, reporte_id,
                        remision_id, alerta_id, datos)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s);""",
                (usuario_id, evento.get("tipo", ""), evento.get("mensaje", ""),
                 evento.get("paciente_id"), evento.get("reporte_id"),
                 evento.get("remision_id"), evento.get("alerta_id"),
                 psycopg2.extras.Json(extras) if extras else None),
            )
        db.commit()
        cur.close()
        db.close()
    except Exception as e:
        print(f"[evento] no se pudieron guardar las notificaciones: {e}")


# ── Escalamiento de alertas (R20) ─────────────────────────────────────────
def _escalar_vencidas() -> None:
    """Alertas activas sin reconocer más allá del plazo -> aviso al admin."""
    db = _conexion_pg()
    try:
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT id, paciente_id, mensaje FROM alertas
               WHERE estado = 'activa' AND escalada = FALSE
                 AND creada_en < now() - make_interval(secs => %s);""",
            (ESCALAMIENTO_SEGUNDOS,),
        )
        vencidas = cur.fetchall()
        for alerta in vencidas:
            cur.execute("UPDATE alertas SET escalada = TRUE WHERE id = %s;", (alerta["id"],))
        db.commit()
        admins = ids_con_rol(db, "admin")
        cur.close()
        for alerta in vencidas:
            publicar(admins, {
                "tipo": "alerta_escalada",
                "mensaje": (f"Alerta {alerta['id']} sin reconocer tras "
                            f"{ESCALAMIENTO_SEGUNDOS} s (paciente {alerta['paciente_id']}): "
                            f"escalada al administrador"),
                "paciente_id": alerta["paciente_id"],
                "alerta_id": alerta["id"],
            })
    except Exception as e:
        print(f"[evento] no se pudo escalar alertas: {e}")
    finally:
        db.close()


def _vigilar_alertas() -> None:
    while True:
        time.sleep(20)
        try:
            _escalar_vencidas()
        except Exception as e:
            print(f"[evento] vigilancia de alertas: {e}")


threading.Thread(target=_vigilar_alertas, daemon=True).start()


# ── Endpoints del contrato (tester.yaml) ──────────────────────────────────
@router.get("/eventos/stream")
async def canal_eventos(request: Request, token: Optional[str] = Query(None)):
    """R17 · Canal en tiempo real (SSE).

    El EventSource del navegador no envía cabeceras, por eso el token también
    se admite como ?token=. Sin token válido: 401. Cada usuario solo recibe los
    eventos de los que es destinatario (alcance por rol)."""
    import auth
    tok = token
    if not tok:
        cabecera = request.headers.get("Authorization", "")
        if cabecera.startswith("Bearer "):
            tok = cabecera[len("Bearer "):]
    if not tok:
        raise HTTPException(status_code=401, detail="Falta el token")
    db = _conexion_pg()
    try:
        usuario = auth.get_usuario_actual(token=tok, db=db)  # lanza 401 si es inválido
    finally:
        db.close()

    cola: "asyncio.Queue" = asyncio.Queue(maxsize=200)
    loop = asyncio.get_running_loop()
    sus = _suscribir(usuario["id"], usuario["rol"], cola, loop)

    async def generador():
        try:
            yield "data: " + json.dumps({
                "tipo": "conectado",
                "mensaje": "canal en tiempo real abierto",
                "fecha": _ahora_iso(),
            }) + "\n\n"
            while True:
                try:
                    texto = await asyncio.wait_for(cola.get(), timeout=20)
                    yield f"data: {texto}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"   # heartbeat: mantiene viva la conexión
        finally:
            _desuscribir(sus)

    return StreamingResponse(
        generador(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ReconocerIn(BaseModel):
    nota: Optional[str] = ""


# Dependencias propias (no las de auth directamente) por el import circular:
# resuelven el token con auth.get_usuario_actual, pero en tiempo de ejecución.
def _db():
    """Una conexión a PostgreSQL por petición (misma semántica que auth.get_db)."""
    db = _conexion_pg()
    try:
        yield db
    finally:
        db.close()


def _usuario_actual(authorization: Optional[str] = Header(None), db=Depends(_db)) -> dict:
    import auth
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    return auth.get_usuario_actual(token=authorization[len("Bearer "):], db=db)


def _usuario_clinico(usuario=Depends(_usuario_actual)) -> dict:
    if usuario["rol"] not in ("admin", "medico", "especialista"):
        raise HTTPException(status_code=403, detail="No tienes permiso para esta acción")
    return usuario


def _fila_notificacion(fila: dict) -> dict:
    """Fila de notificaciones -> JSON del contrato, con los datos del hecho planos."""
    fila = dict(fila)
    datos = fila.pop("datos", None) or {}
    if fila.get("fecha"):
        fila["fecha"] = fila["fecha"].isoformat()
    fila.update(datos)
    return fila


@router.get("/notificaciones")
def listar_notificaciones(leidas: Optional[bool] = Query(None),
                          usuario=Depends(_usuario_actual), db=Depends(_db)):
    """R18 · Bandeja propia: solo la ve su dueño. ?leidas=true/false filtra."""
    sql = ("SELECT id, tipo, mensaje, leida, fecha, paciente_id, reporte_id, "
           "remision_id, alerta_id, datos FROM notificaciones WHERE usuario_id = %s")
    valores = [usuario["id"]]
    if leidas is not None:
        sql += " AND leida = %s"
        valores.append(leidas)
    sql += " ORDER BY fecha DESC, id DESC LIMIT 100;"
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, valores)
    filas = cur.fetchall()
    cur.close()
    return [_fila_notificacion(f) for f in filas]


@router.post("/notificaciones/{notificacion_id}/leer")
def marcar_leida(notificacion_id: int, usuario=Depends(_usuario_actual), db=Depends(_db)):
    """R18 · Marca una notificación propia como leída. La ajena: 403."""
    cur = db.cursor()
    cur.execute("SELECT usuario_id FROM notificaciones WHERE id = %s;", (notificacion_id,))
    fila = cur.fetchone()
    if fila is None:
        cur.close()
        raise HTTPException(status_code=404, detail="Notificación no encontrada")
    if fila[0] != usuario["id"]:
        cur.close()
        raise HTTPException(status_code=403, detail="No puedes marcar una notificación ajena")
    cur.execute("UPDATE notificaciones SET leida = TRUE WHERE id = %s;", (notificacion_id,))
    db.commit()
    cur.close()
    return {"id": notificacion_id, "leida": True}


@router.get("/pendientes")
def trabajo_pendiente(usuario=Depends(_usuario_actual), db=Depends(_db)):
    """R18 · Contadores de trabajo del usuario: suben cuando nace el trabajo
    y bajan cuando se resuelve. Los reportes los pone la Persona 2 (tabla
    reportes_ia o ml_reports), las remisiones la Persona 3, las alertas yo."""
    res = {"reportes_por_revisar": 0, "remisiones_recibidas": 0, "alertas_activas": 0}
    if usuario["rol"] not in ("admin", "medico", "especialista"):
        return res
    cur = db.cursor()
    # Reportes de IA pendientes de revisión clínica (compartidos entre clínicos).
    for tabla in ("reportes_ia", "ml_reports"):
        try:
            cur.execute(f"SELECT COUNT(*) FROM {tabla} WHERE estado = 'pendiente';")
            res["reportes_por_revisar"] += cur.fetchone()[0]
        except Exception:
            db.rollback()
    try:
        cur.execute(
            """SELECT COUNT(*) FROM alertas
               WHERE estado = 'activa' AND (responsable_id IS NULL OR responsable_id = %s);""",
            (usuario["id"],))
        res["alertas_activas"] = cur.fetchone()[0]
    except Exception:
        db.rollback()
    if usuario["rol"] == "especialista":
        try:
            cur.execute("SELECT COUNT(*) FROM remisiones WHERE activo = TRUE AND estado = 'pendiente';")
            res["remisiones_recibidas"] += cur.fetchone()[0]
        except Exception:
            db.rollback()
    cur.close()
    return res


@router.get("/alertas")
def listar_alertas(usuario=Depends(_usuario_clinico), db=Depends(_db)):
    """R20 · Alertas críticas. El admin ve todas; los clínicos las suyas y las
    sin asignar. Paciente y contable ni las ven (403)."""
    if usuario["rol"] == "admin":
        sql = "SELECT * FROM alertas ORDER BY creada_en DESC LIMIT 100;"
        valores = []
    else:
        sql = ("SELECT * FROM alertas WHERE responsable_id IS NULL OR responsable_id = %s "
               "ORDER BY creada_en DESC LIMIT 100;")
        valores = [usuario["id"]]
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, valores)
    filas = cur.fetchall()
    cur.close()
    for f in filas:
        for k in ("creada_en", "reconocida_en"):
            if f.get(k):
                f[k] = f[k].isoformat()
    return [dict(f) for f in filas]


@router.post("/alertas/{alerta_id}/reconocer")
def reconocer_alerta(alerta_id: int, datos: ReconocerIn,
                     usuario=Depends(_usuario_clinico), db=Depends(_db)):
    """R20 · El clínico reconoce la alerta con una nota; queda en la auditoría.
    Paciente y contable no pueden (403)."""
    cur = db.cursor()
    cur.execute("SELECT estado FROM alertas WHERE id = %s;", (alerta_id,))
    fila = cur.fetchone()
    if fila is None:
        cur.close()
        raise HTTPException(status_code=404, detail="Alerta no encontrada")
    if fila[0] != "reconocida":
        cur.execute(
            """UPDATE alertas
               SET estado = 'reconocida', reconocida_en = now(),
                   reconocida_por = %s, nota = %s
               WHERE id = %s;""",
            (usuario["id"], datos.nota or "", alerta_id))
        db.commit()
    cur.close()
    from auth import registrar_auditoria
    registrar_auditoria(db, usuario["id"], "alerta_reconocida", "alertas", alerta_id,
                        (datos.nota or "")[:200])
    return {"id": alerta_id, "estado": "reconocida"}
