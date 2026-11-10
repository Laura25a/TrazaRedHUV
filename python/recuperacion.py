"""
recuperacion.py — Eliminar y recuperar información sin destruirla, y el rol de
Ingeniería biomédica (gestión técnica del PACS y de los servicios).

Regla (Res. 1995 de 1999): nada de la historia clínica se destruye. "Eliminar" una
imagen es un borrado LÓGICO: deja de verse en la ficha, pero el archivo DICOM sigue
en el PACS (Orthanc) y queda registrado quién la ocultó, cuándo y por qué.

Quién hace qué (mínimo privilegio, Ley 1581):
  médico, admin   → eliminan una imagen (con motivo obligatorio)
  admin           → restaura imágenes y remisiones (custodio de la historia clínica)
  biomédico       → restaura IMÁGENES (administra el PACS), ve el estado técnico de los
                    servicios y la auditoría técnica. No ve fichas, diagnósticos ni signos
                    vitales: los pacientes le aparecen con seudónimo.

Rutas:
  POST   /imagenes/{id}/eliminar              eliminar (motivo)
  GET    /recuperacion/imagenes               imágenes eliminadas (y restauradas, con ?todas=true)
  GET    /recuperacion/imagenes/{id}/preview  miniatura de una imagen eliminada (para saber cuál es)
  POST   /recuperacion/imagenes/{id}/restaurar
  GET    /tecnico/estado                      servicios, PACS, modelo de IA, RAG y agente
  GET    /tecnico/auditoria                   eventos técnicos (imágenes, modelo, recuperaciones)
"""
import os

import httpx
import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

import pacs
from auth import PG_CONNECTION_STRING, get_db, mongo_client, registrar_auditoria, requiere_rol
from eventos import ids_con_rol, publicar

router = APIRouter(tags=["Recuperación e ingeniería biomédica"])

ROLES_RESTAURAN_IMAGENES = ("admin", "biomedico")
FHIR_URL = os.getenv("FHIR_BASE_URL", "http://hapi-fhir:8080/fhir")
ML_URL = os.getenv("ML_URL", "http://ml:8001")


class Motivo(BaseModel):
    motivo: str = Field(min_length=5, max_length=300)


def _alias(paciente_id: int | None) -> str | None:
    if paciente_id is None:
        return None
    from rag.router import seudonimo
    return seudonimo(paciente_id)


def ids_eliminadas(db) -> set:
    """instance_id de las imágenes que hoy están ocultas (borrado lógico vigente)."""
    cur = db.cursor()
    cur.execute("SELECT instance_id FROM imagenes_eliminadas WHERE vigente = TRUE;")
    ids = {f[0] for f in cur.fetchall()}
    cur.close()
    return ids


def _paciente_de_instancia(db, instance_id: str) -> dict:
    pacs.check_instance_id(instance_id)
    documento = pacs.instance_patient_id(instance_id)
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, documento FROM pacientes WHERE documento = %s;", (documento,))
    fila = cur.fetchone()
    cur.close()
    if fila is None:
        raise HTTPException(status_code=404, detail="Imagen no encontrada")
    return fila


# ─────────────────────────────────────────────── eliminar (borrado lógico)
@router.post("/imagenes/{instance_id}/eliminar")
def eliminar_imagen(instance_id: str, body: Motivo, db=Depends(get_db),
                    usuario=Depends(requiere_rol("admin", "medico"))):
    paciente = _paciente_de_instancia(db, instance_id)
    if instance_id in ids_eliminadas(db):
        raise HTTPException(status_code=409, detail="La imagen ya está eliminada")
    t = pacs.orthanc("GET", f"/instances/{instance_id}/simplified-tags").json()
    descripcion = t.get("SeriesDescription") or t.get("StudyDescription") or "Imagen"
    modalidad = t.get("Modality") or "OT"
    cur = db.cursor()
    cur.execute("""INSERT INTO imagenes_eliminadas (instance_id, paciente_id, descripcion, modalidad, motivo, eliminada_por)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;""",
                (instance_id, paciente["id"], descripcion, modalidad, body.motivo.strip(), usuario["id"]))
    db.commit()
    cur.close()
    registrar_auditoria(db, usuario["id"], "imagen_eliminada", "pacientes", paciente["id"],
                        f"instancia {instance_id} · {descripcion} · motivo: {body.motivo.strip()[:120]}")
    publicar(ids_con_rol(db, *ROLES_RESTAURAN_IMAGENES), {
        "tipo": "imagen_eliminada",
        "mensaje": f"{usuario['nombre']} eliminó una imagen ({descripcion}). Se puede recuperar.",
    })
    return {"instance_id": instance_id, "estado": "eliminada",
            "detalle": "La imagen ya no se ve en la ficha, pero el archivo sigue en el PACS y se puede recuperar."}


# ─────────────────────────────────────────────── papelera de imágenes
@router.get("/recuperacion/imagenes")
def imagenes_eliminadas(todas: bool = Query(False, description="Incluye las ya restauradas"),
                        db=Depends(get_db), usuario=Depends(requiere_rol(*ROLES_RESTAURAN_IMAGENES))):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"""
        SELECT e.id, e.instance_id, e.paciente_id, e.descripcion, e.modalidad, e.motivo, e.vigente,
               e.eliminada_en, ue.nombre AS eliminada_por, ue.rol AS eliminada_por_rol,
               e.restaurada_en, ur.nombre AS restaurada_por, e.motivo_restauracion,
               p.nombre AS paciente_nombre
        FROM imagenes_eliminadas e
        JOIN usuarios ue ON ue.id = e.eliminada_por
        LEFT JOIN usuarios ur ON ur.id = e.restaurada_por
        LEFT JOIN pacientes p ON p.id = e.paciente_id
        {"" if todas else "WHERE e.vigente = TRUE"}
        ORDER BY e.eliminada_en DESC LIMIT 200;""")
    filas = cur.fetchall()
    cur.close()
    for f in filas:
        f["paciente"] = _alias(f["paciente_id"])
        if usuario["rol"] != "admin":                 # el biomédico no ve identidad del paciente
            f.pop("paciente_nombre", None)
            f["paciente_id"] = None
    return filas


@router.get("/recuperacion/imagenes/{instance_id}/preview")
def ver_eliminada(instance_id: str, db=Depends(get_db),
                  usuario=Depends(requiere_rol(*ROLES_RESTAURAN_IMAGENES))):
    """Miniatura de una imagen ELIMINADA, para reconocerla antes de restaurarla."""
    pacs.check_instance_id(instance_id)
    if instance_id not in ids_eliminadas(db):
        raise HTTPException(status_code=404, detail="Esa imagen no está en la papelera")
    png = pacs.orthanc("GET", f"/instances/{instance_id}/preview").content
    registrar_auditoria(db, usuario["id"], "imagen_vista_papelera", "imagenes_eliminadas", None,
                        f"instancia {instance_id}")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "private, max-age=60"})


@router.post("/recuperacion/imagenes/{instance_id}/restaurar")
def restaurar_imagen(instance_id: str, body: Motivo, db=Depends(get_db),
                     usuario=Depends(requiere_rol(*ROLES_RESTAURAN_IMAGENES))):
    pacs.check_instance_id(instance_id)
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""UPDATE imagenes_eliminadas
                   SET vigente = FALSE, restaurada_por = %s, restaurada_en = now(), motivo_restauracion = %s
                   WHERE instance_id = %s AND vigente = TRUE
                   RETURNING id, paciente_id, descripcion;""",
                (usuario["id"], body.motivo.strip(), instance_id))
    fila = cur.fetchone()
    db.commit()
    cur.close()
    if fila is None:
        raise HTTPException(status_code=404, detail="Esa imagen no está en la papelera")
    registrar_auditoria(db, usuario["id"], "imagen_restaurada", "pacientes", fila["paciente_id"],
                        f"instancia {instance_id} · {fila['descripcion']} · motivo: {body.motivo.strip()[:120]}")
    publicar(ids_con_rol(db, *ROLES_RESTAURAN_IMAGENES), {
        "tipo": "imagen_restaurada",
        "mensaje": f"{usuario['nombre']} restauró una imagen ({fila['descripcion']}).",
    })
    return {"instance_id": instance_id, "estado": "restaurada"}


# ─────────────────────────────────────────────── estado técnico (biomédico)
def _probar(nombre: str, fn) -> dict:
    try:
        detalle = fn()
        return {"servicio": nombre, "estado": "ok", **(detalle or {})}
    except Exception as e:
        return {"servicio": nombre, "estado": "error", "detalle": f"{type(e).__name__}: {str(e)[:120]}"}


@router.get("/tecnico/estado")
def estado_tecnico(usuario=Depends(requiere_rol("admin", "biomedico"))):
    def postgres():
        conn = psycopg2.connect(PG_CONNECTION_STRING, connect_timeout=3)
        cur = conn.cursor()
        cur.execute("SELECT pg_size_pretty(pg_database_size(current_database())), "
                    "(SELECT extversion FROM pg_extension WHERE extname = 'vector');")
        tam, vector = cur.fetchone()
        cur.close(); conn.close()
        return {"detalle": f"base {tam}", "pgvector": vector or "no instalada"}

    def mongo():
        mongo_client.admin.command("ping")
        return {"detalle": "responde"}

    def orthanc():
        s = pacs.orthanc("GET", "/statistics").json()
        v = pacs.orthanc("GET", "/system").json()
        return {"detalle": f"{s.get('CountInstances', 0)} imágenes · {s.get('TotalDiskSizeMB', 0)} MB",
                "version": v.get("Version"), "imagenes": s.get("CountInstances", 0)}

    def fhir():
        r = httpx.get(f"{FHIR_URL}/metadata?_summary=true", timeout=5)
        r.raise_for_status()
        return {"detalle": f"FHIR {r.json().get('fhirVersion', '')}"}

    def ml():
        r = httpx.get(f"{ML_URL}/modelo", timeout=5)
        r.raise_for_status()
        m = r.json()
        return {"detalle": f"XGBoost {m.get('version')} · AUC {m.get('metricas', {}).get('auc_prueba')}",
                "version": m.get("version")}

    servicios = [_probar("PostgreSQL", postgres), _probar("MongoDB", mongo), _probar("PACS Orthanc", orthanc),
                 _probar("HAPI FHIR", fhir), _probar("Servicio de IA", ml)]
    try:
        from rag.motor import estado as estado_rag
        rag = estado_rag()
    except Exception as e:
        rag = {"error": str(e)}
    try:
        from agente.modelo import nombre_modelo, proveedor
        from agente.router import ESTADO
        agente = {"proveedor": proveedor(), "modelo": nombre_modelo(), "memoria": ESTADO.get("memoria")}
    except Exception as e:
        agente = {"error": str(e)}
    return {"servicios": servicios, "rag": rag, "agente": agente}


ACCIONES_TECNICAS = ("imagen_subida", "imagen_eliminada", "imagen_restaurada", "imagen_vista_papelera",
                     "modelo_entrenado", "restaurar", "soft_delete", "desbloqueo_usuario", "bloqueo_usuario")


@router.get("/tecnico/auditoria")
def auditoria_tecnica(limite: int = Query(100, ge=1, le=500), db=Depends(get_db),
                      usuario=Depends(requiere_rol("admin", "biomedico"))):
    """Eventos técnicos y de recuperación. El biomédico ve el paciente solo como seudónimo."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT a.id, a.fecha, a.accion, a.tabla, a.registro_id, a.detalle, u.nombre AS usuario, u.rol
                   FROM auditoria a LEFT JOIN usuarios u ON u.id = a.usuario_id
                   WHERE a.accion = ANY(%s) OR a.accion LIKE 'imagen%%'
                   ORDER BY a.id DESC LIMIT %s;""", (list(ACCIONES_TECNICAS), limite))
    filas = cur.fetchall()
    cur.close()
    for f in filas:
        if f["tabla"] == "pacientes":
            f["paciente"] = _alias(f["registro_id"])
            if usuario["rol"] != "admin":
                f["registro_id"] = None
    return filas
