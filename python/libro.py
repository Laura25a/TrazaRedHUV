"""
libro.py — Datos del libro de radicación (sin rutas).

Reúne en un solo lugar cómo se arma un "asiento" del libro a partir de lo que
ya existe en las bases:

  PostgreSQL  remisiones (+ radicada_en / cerrada_en), pacientes, usuarios,
              remisiones_historial, auditoria, pronosticos_remision
  MongoDB     gestiones_contacto  (la bitácora de contactos de cada remisión)

Lo usan registro.py (libro, formato, descargas) y contabilidad.py (libro
contable), así los dos libros muestran exactamente los mismos números.

Radicado y folio NO se guardan: salen del id y del año de radicación, así
nunca se desincronizan:
    radicado = REM-<año>-<id con 6 dígitos>      p. ej. REM-2026-000482
    folio    = un folio cada 30 asientos (como la hoja de un libro físico)
"""
from datetime import datetime, timezone
from typing import Iterable, Optional

import psycopg2.extras

from auth import con_zona, db_mongo
from causas import CAUSAS, causa_de

ASIENTOS_POR_FOLIO = 30
ESTADOS_ABIERTOS = ("pendiente", "en_gestion", "aceptada")
ESTADOS_CERRADOS = ("rechazada", "completada")
HORAS_SIN_MOVIMIENTO = 6          # umbral de alerta del libro

coleccion_gestiones = db_mongo["gestiones_contacto"]


def radicado(remision_id: int, radicada_en) -> str:
    anio = radicada_en.year if isinstance(radicada_en, datetime) else datetime.now().year
    return f"REM-{anio}-{remision_id:06d}"


def folio(remision_id: int) -> int:
    return (remision_id - 1) // ASIENTOS_POR_FOLIO + 1


def tomo(radicada_en) -> str:
    if not isinstance(radicada_en, datetime):
        return "—"
    return f"{radicada_en.year}-{'I' if radicada_en.month <= 6 else 'II'}"


def ahora() -> datetime:
    return datetime.now(timezone.utc)


# Las columnas TIMESTAMP (sin zona) guardan la hora en la zona de la base: se
# les pega esa zona en el SELECT para que la API las entregue con zona explícita
# (igual que hace /auditoria). Así no hay corrimientos de 5 horas.
SQL_ASIENTO = """
SELECT r.id, r.paciente_id, r.institucion_origen, r.institucion_destino,
       r.fecha_solicitud, r.motivo, r.estado, r.convenio_vigente, r.creado_por, r.activo,
       r.institucion_acepta,
       r.radicada_en AT TIME ZONE current_setting('TimeZone') AS radicada_en,
       r.cerrada_en  AT TIME ZONE current_setting('TimeZone') AS cerrada_en,
       p.nombre AS paciente_nombre, p.documento AS paciente_documento, p.eps,
       p.genero, p.fecha_nacimiento,
       u.nombre AS asento_nombre, u.rol AS asento_rol,
       pr.probabilidad_efectiva, pr.probabilidad_al_radicar, pr.riesgo AS riesgo_ia,
       pr.momento AS momento_ia, pr.version_modelo,
       pr.creado_en AT TIME ZONE current_setting('TimeZone') AS pronostico_en
  FROM remisiones r
  JOIN pacientes p ON p.id = r.paciente_id
  LEFT JOIN usuarios u ON u.id = r.creado_por
  LEFT JOIN LATERAL (
        SELECT probabilidad_efectiva, probabilidad_al_radicar, riesgo, momento, version_modelo, creado_en
          FROM pronosticos_remision WHERE remision_id = r.id ORDER BY id DESC LIMIT 1) pr ON TRUE
"""


def cargar_remisiones(db, condiciones: Iterable[str] = (), valores: Iterable = (), orden="r.id DESC",
                      limite: Optional[int] = None) -> list:
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where = " AND ".join(condiciones) or "TRUE"
    sql = f"{SQL_ASIENTO} WHERE {where} ORDER BY {orden}" + (" LIMIT %s" if limite else "")
    cur.execute(sql, (*valores, *( [limite] if limite else [] )))
    filas = cur.fetchall()
    cur.close()
    for f in filas:
        for k in ("probabilidad_efectiva", "probabilidad_al_radicar"):
            if f.get(k) is not None:
                f[k] = float(f[k])
    return filas


def condiciones_por_rol(usuario: dict, incluir_inactivas=False):
    """Qué asientos puede ver cada rol (misma regla que /remisiones)."""
    cond, vals = [], []
    if not (incluir_inactivas and usuario["rol"] == "admin"):
        cond.append("r.activo = TRUE")
    if usuario["rol"] == "eps":
        cond.append("p.eps = %s"); vals.append(usuario.get("eps_nombre"))
    if usuario["rol"] == "paciente":
        cond.append("r.paciente_id = %s"); vals.append(usuario.get("paciente_id"))
    return cond, vals


def cargar_contactos(ids: Iterable[int]) -> dict:
    """{remision_id: [contactos ordenados por fecha]} — un documento de Mongo
    por remisión (puede haber más de uno si alguien usó POST varias veces)."""
    ids = list(ids)
    salida = {i: [] for i in ids}
    if not ids:
        return salida
    for doc in coleccion_gestiones.find({"remision_id": {"$in": ids}}):
        for c in doc.get("contactos") or []:
            c = dict(c)
            c["fecha"] = con_zona(c.get("fecha"))
            salida.setdefault(doc["remision_id"], []).append(c)
    for lista in salida.values():
        lista.sort(key=lambda c: c.get("fecha") or datetime.min.replace(tzinfo=timezone.utc))
    return salida


def _como_aware(f):
    if isinstance(f, datetime) and f.tzinfo is None:
        return f.replace(tzinfo=timezone.utc)
    return f


def resumen_bitacora(asiento: dict, contactos: list) -> dict:
    """Lo que el libro muestra de la gestión: marcas por intento, tiempos, alerta."""
    marcas = []
    for c in contactos:
        k = causa_de(c)
        marcas.append({"marca": CAUSAS[k]["marca"], "causa": k, "etiqueta": CAUSAS[k]["etiqueta"]})
    instituciones = []
    for c in contactos:
        if c.get("institucion_contactada") not in instituciones:
            instituciones.append(c.get("institucion_contactada"))
    radicada = _como_aware(asiento.get("radicada_en"))
    cerrada = _como_aware(asiento.get("cerrada_en"))
    ultimo = _como_aware(contactos[-1]["fecha"]) if contactos else radicada
    abierta = asiento["estado"] in ESTADOS_ABIERTOS
    fin = cerrada or (ahora() if abierta else ultimo)
    horas_abierta = max(0.0, (fin - radicada).total_seconds() / 3600) if radicada and fin else None
    horas_sin_mov = max(0.0, (ahora() - ultimo).total_seconds() / 3600) if (abierta and ultimo) else 0.0
    return {
        "marcas": marcas,
        "intentos": len(contactos),
        "instituciones_contactadas": instituciones,
        "ultimo_movimiento": (contactos[-1] if contactos else None),
        "ultimo_movimiento_en": ultimo,
        "horas_abierta": round(horas_abierta, 2) if horas_abierta is not None else None,
        "horas_sin_movimiento": round(horas_sin_mov, 2),
        "sin_movimiento": bool(abierta and asiento["estado"] != "aceptada" and horas_sin_mov > HORAS_SIN_MOVIMIENTO),
    }


def enriquecer(asiento: dict, contactos: list) -> dict:
    a = dict(asiento)
    a["radicado"] = radicado(a["id"], a.get("radicada_en"))
    a["folio"] = folio(a["id"])
    a["tomo"] = tomo(a.get("radicada_en"))
    a["abierta"] = a["estado"] in ESTADOS_ABIERTOS
    a.update(resumen_bitacora(a, contactos))
    um = a.get("ultimo_movimiento")
    if um:
        a["ultimo_movimiento"] = {"fecha": um.get("fecha"), "institucion": um.get("institucion_contactada"),
                                  "respuesta": um.get("respuesta"), "medio": um.get("medio"),
                                  "por": um.get("contactado_por")}
    return a


def asientos(db, usuario: dict, condiciones=(), valores=(), incluir_inactivas=False, limite=None) -> list:
    cond, vals = condiciones_por_rol(usuario, incluir_inactivas)
    filas = cargar_remisiones(db, [*cond, *condiciones], [*vals, *valores], limite=limite)
    bitacoras = cargar_contactos([f["id"] for f in filas])
    return [enriquecer(f, bitacoras.get(f["id"], [])) for f in filas]


def edad_de(fecha_nacimiento) -> Optional[int]:
    if not fecha_nacimiento:
        return None
    hoy = datetime.now().date()
    return hoy.year - fecha_nacimiento.year - ((hoy.month, hoy.day) < (fecha_nacimiento.month, fecha_nacimiento.day))
