"""Notificaciones persistentes, stream SSE y escalamiento de alertas."""
import asyncio
import json
import os
import threading
import time
from contextlib import closing
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
    "remision_creada", "remision_aceptada",
    "alerta_critica", "alerta_escalada", "alerta_reconocida", "observacion_fhir",
    "observacion_clinica", "imagen_clinica", "reporte_pendiente",
    # Proyecto final · eventos externos (interoperabilidad con el sistema socio)
    "remision_externa_recibida", "respuesta_socio", "interop_conciliacion", "interop_resultado",
}

# Segundos sin reconocer una alerta antes de escalarla al admin (R20).
ESCALAMIENTO_SEGUNDOS = int(os.getenv("ESCALAMIENTO_SEGUNDOS", "600"))

# Valor de la factura automática del análisis de IA (R19). La Persona 3 puede
# moverlo con la variable de entorno VALOR_ANALISIS_IA.
VALOR_ANALISIS_IA = float(os.getenv("VALOR_ANALISIS_IA", "80000"))


# ── Utilidades de acceso (imports diferidos: ver docstring) ────────────────
def _conexion_pg():
    import auth
    return psycopg2.connect(auth.PG_CONNECTION_STRING, connect_timeout=5)


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
    """Publica desde operaciones ya confirmadas. Los avisos críticos usan su propia transacción de origen."""
    try:
        with closing(_conexion_pg()) as db, db:
            registrar_evento(db, destinatarios, evento)
    except Exception as e:
        print(f"[evento] no se pudo publicar {evento.get('tipo')}: {type(e).__name__}")


def registrar_evento(db, destinatarios: Iterable[int], evento: dict) -> None:
    """Escribe el hecho y su bandeja dentro de la transacción del llamador."""
    from auth import registrar_auditoria

    evento = dict(evento)
    tipo = evento["tipo"]
    if tipo == "alerta_critica":
        with db.cursor() as cur:
            cur.execute("""INSERT INTO alertas (paciente_id, nivel, estado, mensaje, reporte_id, responsable_id)
                           VALUES (%s, 'critico', 'activa', %s, %s, %s) RETURNING id;""",
                        (evento.get("paciente_id"), evento["mensaje"], evento.get("reporte_id"),
                         evento.get("responsable_id")))
            evento["alerta_id"] = cur.fetchone()[0]
    _guardar_notificaciones(db, destinatarios, evento)
    if tipo == "alerta_critica":
        registrar_auditoria(db, None, "alerta_creada", "alertas", evento["alerta_id"], confirmar=False)
    if tipo == "observacion_fhir":
        registrar_auditoria(db, None, "evento_fhir", "fhir", evento.get("paciente_id"),
                            f"Observation/{evento['fhir_observation_id']} recibida por rest-hook",
                            confirmar=False)


def completar_facturas_ia(reporte_id=None):
    """La factura es idempotente. Si Mongo falla, la solicitud queda pendiente."""
    from contabilidad import crear_factura_ia

    try:
        with closing(_conexion_pg()) as db:
            with db, db.cursor() as cur:
                cur.execute("""SELECT t.reporte_id, r.paciente_id FROM facturas_ia_pendientes t
                               JOIN reportes_ia r ON r.id = t.reporte_id
                               WHERE (%s::integer IS NULL OR t.reporte_id = %s)
                               ORDER BY t.reporte_id LIMIT 20;""", (reporte_id, reporte_id))
                solicitudes = cur.fetchall()
            for reporte, paciente in solicitudes:
                try:
                    crear_factura_ia(paciente, reporte, VALOR_ANALISIS_IA)
                    with db, db.cursor() as cur:
                        cur.execute("DELETE FROM facturas_ia_pendientes WHERE reporte_id = %s;", (reporte,))
                except Exception as e:
                    print(f"[evento] factura de IA pendiente: {type(e).__name__}")
    except Exception as e:
        print(f"[evento] no se pudo consultar las facturas pendientes: {type(e).__name__}")


# Los IDs de la bandeja también son el cursor SSE. Serializar la asignación
# hasta el commit impide que un ID menor se confirme después de uno mayor.
def _guardar_notificaciones(db, destinatarios, evento):
    ids = sorted({int(d) for d in destinatarios if d is not None})
    if not ids:
        return
    campos = {"tipo", "mensaje", "fecha", "paciente_id", "reporte_id", "remision_id", "alerta_id"}
    extras = {k: v for k, v in evento.items() if k not in campos}
    roles_excluidos = ["paciente", "contable", "director", "biomedico", "eps"] if evento["tipo"] in EVENTOS_CLINICOS else []
    with db.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(82401937);")
        cur.execute(
            """INSERT INTO notificaciones
                   (usuario_id, tipo, mensaje, paciente_id, reporte_id, remision_id, alerta_id, datos)
               SELECT id, %s, %s, %s, %s, %s, %s, %s FROM usuarios
                WHERE activo AND id = ANY(%s) AND NOT (rol = ANY(%s::text[]))
                ORDER BY id;""",
            (evento["tipo"], evento["mensaje"], evento.get("paciente_id"), evento.get("reporte_id"),
             evento.get("remision_id"), evento.get("alerta_id"),
             psycopg2.extras.Json(extras) if extras else None, ids, roles_excluidos),
        )


def _escalar_vencidas() -> None:
    """Confirma el escalamiento junto con el aviso. Una falla deja la alerta reintentable."""
    with closing(_conexion_pg()) as db, db, db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        admins = ids_con_rol(db, "admin")
        if not admins:
            return
        cur.execute(
            """SELECT id, paciente_id FROM alertas
               WHERE estado = 'activa' AND NOT escalada
                 AND creada_en < now() - make_interval(secs => %s)
               ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED;""", (ESCALAMIENTO_SEGUNDOS,),
        )
        for alerta in cur.fetchall():
            registrar_evento(db, admins, {
                "tipo": "alerta_escalada",
                "mensaje": f"Alerta {alerta['id']} sin reconocer tras {ESCALAMIENTO_SEGUNDOS} s",
                "paciente_id": alerta["paciente_id"], "alerta_id": alerta["id"],
            })
            cur.execute("UPDATE alertas SET escalada = TRUE WHERE id = %s;", (alerta["id"],))


def _vigilar_alertas() -> None:
    while True:
        try:
            _escalar_vencidas()
        except Exception as e:
            print(f"[evento] vigilancia de alertas: {type(e).__name__}")
        completar_facturas_ia()
        time.sleep(20)


def iniciar_escalamiento() -> None:
    threading.Thread(target=_vigilar_alertas, daemon=True, name="escalamiento-alertas").start()


def _notificaciones_desde(usuario, despues):
    with closing(_conexion_pg()) as db, db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if despues is None:
            cur.execute("SELECT COALESCE(max(id), 0) AS ultimo FROM notificaciones WHERE usuario_id = %s;",
                        (usuario["id"],))
            return cur.fetchone()["ultimo"]
        cur.execute("""SELECT id, tipo, mensaje, leida, fecha, paciente_id, reporte_id,
                              remision_id, alerta_id, datos
                         FROM notificaciones WHERE usuario_id = %s AND id > %s
                         ORDER BY id LIMIT 100;""", (usuario["id"], despues))
        return [_fila_notificacion(fila) for fila in cur.fetchall()]


# ── Endpoints del contrato (tester.yaml) ──────────────────────────────────
@router.get("/eventos/stream")
async def canal_eventos(request: Request):
    """Canal SSE autenticado mediante la cabecera Authorization."""
    authorization = request.headers.get("Authorization")

    def validar_sesion():
        db = _conexion_pg()
        try:
            return _usuario_actual(authorization, db)
        finally:
            db.close()

    usuario = await asyncio.to_thread(validar_sesion)

    ultimo = request.headers.get("Last-Event-ID", "")
    if ultimo and (not ultimo.isascii() or not ultimo.isdecimal() or len(ultimo) > 10
                   or int(ultimo) > 2147483647):
        raise HTTPException(422, "Cursor de eventos inválido")
    inicial = int(ultimo) if ultimo else await asyncio.to_thread(_notificaciones_desde, usuario, None)

    async def generador():
        cursor = inicial
        try:
            yield f"id: {cursor}\ndata: " + json.dumps({
                "tipo": "conectado", "mensaje": "canal en tiempo real abierto", "fecha": _ahora_iso(),
            }) + "\n\n"
            siguiente_revision = time.monotonic() + 15
            while True:
                if time.monotonic() >= siguiente_revision:
                    vigente = await asyncio.to_thread(validar_sesion)
                    if vigente["rol"] != usuario["rol"]:
                        return
                    siguiente_revision = time.monotonic() + 15
                    yield ": ping\n\n"
                filas = await asyncio.to_thread(_notificaciones_desde, usuario, cursor)
                for fila in filas:
                    cursor = fila["id"]
                    if fila["tipo"] in EVENTOS_CLINICOS and usuario["rol"] not in ("admin", "medico", "especialista"):
                        continue
                    yield f"id: {cursor}\ndata: {json.dumps(fila, default=str)}\n\n"
                await asyncio.sleep(1 if len(filas) == 100 else 2)
        except (HTTPException, psycopg2.Error):
            return  # El cliente vuelve a conectar con su último evento recibido.

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
    return {**datos, **fila}


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
            filtro = ""
            valores = ()
            if tabla == "reportes_ia" and usuario["rol"] != "admin":
                filtro = " AND (huella_automatica IS NULL OR creado_por = %s)"
                valores = (usuario["id"],)
            cur.execute(f"SELECT COUNT(*) FROM {tabla} WHERE estado = 'pendiente'{filtro};", valores)
            res["reportes_por_revisar"] += cur.fetchone()[0]
        except Exception:
            db.rollback()
    try:
        cur.execute(
            """SELECT COUNT(*) FROM alertas
               WHERE estado = 'activa' AND (%s OR responsable_id = %s);""",
            (usuario["rol"] == "admin", usuario["id"]))
        res["alertas_activas"] = cur.fetchone()[0]
    except Exception:
        db.rollback()
    if usuario["rol"] in ("especialista", "admin"):
        try:
            # Proyecto final (R30): remisiones que llegaron de otra institución y nadie ha respondido
            cur.execute("""SELECT COUNT(*) FROM remisiones
                           WHERE origen_externo AND activo AND estado = 'pendiente';""")
            res["remisiones_recibidas"] += cur.fetchone()[0]
        except Exception:
            db.rollback()
    if usuario["rol"] == "especialista":
        try:
            # R08: la bandeja del especialista vive en remisiones_medicas
            # (Persona 3); las que recibe él y siguen pendientes.
            cur.execute(
                """SELECT COUNT(*) FROM remisiones_medicas
                   WHERE especialista_id = %s AND estado = 'pendiente';""",
                (usuario["id"],))
            res["remisiones_recibidas"] += cur.fetchone()[0]
        except Exception:
            # Respaldo por si la migración de la Persona 3 todavía no corrió:
            # la tabla remisiones del Corte 1 (traslados), como antes.
            db.rollback()
            try:
                cur.execute("SELECT COUNT(*) FROM remisiones WHERE activo = TRUE AND estado = 'pendiente';")
                res["remisiones_recibidas"] += cur.fetchone()[0]
            except Exception:
                db.rollback()
    cur.close()
    return res


@router.get("/alertas")
def listar_alertas(usuario=Depends(_usuario_clinico), db=Depends(_db)):
    """R20 · El administrador ve todas las alertas. Los clínicos ven las asignadas."""
    if usuario["rol"] == "admin":
        sql = "SELECT * FROM alertas ORDER BY creada_en DESC LIMIT 100;"
        valores = []
    else:
        sql = ("SELECT * FROM alertas WHERE responsable_id = %s "
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
    from auth import registrar_auditoria

    with db.cursor() as cur:
        cur.execute("SELECT estado, responsable_id FROM alertas WHERE id = %s FOR UPDATE;", (alerta_id,))
        fila = cur.fetchone()
        if not fila or (fila[1] != usuario["id"] and usuario["rol"] != "admin"):
            raise HTTPException(404, "Alerta no encontrada")
        if fila[0] == "activa":
            cur.execute("""UPDATE alertas SET estado = 'reconocida', reconocida_en = now(),
                                  reconocida_por = %s, nota = %s WHERE id = %s;""",
                        (usuario["id"], datos.nota or "", alerta_id))
            registrar_evento(db, [fila[1], usuario["id"], *ids_con_rol(db, "admin")], {
                "tipo": "alerta_reconocida", "alerta_id": alerta_id,
                "mensaje": f"Alerta {alerta_id} reconocida",
            })
            registrar_auditoria(db, usuario["id"], "alerta_reconocida", "alertas", alerta_id,
                                (datos.nota or "")[:200])
    return {"id": alerta_id, "estado": "reconocida"}
