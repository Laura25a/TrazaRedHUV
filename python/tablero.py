"""
tablero.py — Vista de POBLACIÓN para la dirección (semana 10, "Para su Proyecto 2").

El administrador y el director ven tendencias agregadas por semana; nunca un
paciente individual (el director recibe 403 en la ficha y la historia).

  GET /tablero/tendencia?semanas=12
      reportes de IA por semana (total, urgentes, críticos, aprobados, rechazados, pendientes),
      alertas críticas por semana (total, reconocidas, escaladas, minutos hasta reconocer)
      y remisiones radicadas por semana (total, aceptadas o completadas, rechazadas).
"""
import psycopg2.extras
from fastapi import APIRouter, Depends, Query

from auth import get_db, registrar_auditoria, requiere_rol

router = APIRouter(tags=["Tablero de población"])


def _serie(cur, sql: str, semanas: int) -> list[dict]:
    cur.execute(sql, {"semanas": semanas})
    filas = cur.fetchall()
    for f in filas:
        f["semana"] = f["semana"].date().isoformat()
        for k, v in list(f.items()):
            if hasattr(v, "is_integer") and k != "semana":   # Decimal / float -> número JSON
                f[k] = round(float(v), 1)
    return filas


@router.get("/tablero/tendencia")
def tendencia(semanas: int = Query(12, ge=2, le=52), db=Depends(get_db),
              usuario=Depends(requiere_rol("admin", "director"))):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    reportes = _serie(cur, """
        SELECT date_trunc('week', creado_en) AS semana,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE prediccion = 'urgente') AS urgentes,
               COUNT(*) FILTER (WHERE critico_segun_umbral) AS criticos,
               COUNT(*) FILTER (WHERE estado = 'aprobado') AS aprobados,
               COUNT(*) FILTER (WHERE estado = 'rechazado') AS rechazados,
               COUNT(*) FILTER (WHERE estado = 'pendiente') AS pendientes
        FROM reportes_ia
        WHERE creado_en >= date_trunc('week', now()) - make_interval(weeks => %(semanas)s - 1)
        GROUP BY 1 ORDER BY 1;""", semanas)
    alertas = _serie(cur, """
        SELECT date_trunc('week', creada_en) AS semana,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE estado = 'reconocida') AS reconocidas,
               COUNT(*) FILTER (WHERE escalada) AS escaladas,
               AVG(EXTRACT(EPOCH FROM (reconocida_en - creada_en)) / 60.0)
                   FILTER (WHERE reconocida_en IS NOT NULL) AS minutos_hasta_reconocer
        FROM alertas
        WHERE creada_en >= date_trunc('week', now()) - make_interval(weeks => %(semanas)s - 1)
        GROUP BY 1 ORDER BY 1;""", semanas)
    remisiones = _serie(cur, """
        SELECT date_trunc('week', COALESCE(radicada_en, fecha_solicitud::timestamp)) AS semana,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE estado IN ('aceptada', 'completada')) AS efectivas,
               COUNT(*) FILTER (WHERE estado = 'rechazada') AS rechazadas,
               COUNT(*) FILTER (WHERE convenio_vigente) AS con_convenio
        FROM remisiones
        WHERE activo = TRUE
          AND COALESCE(radicada_en, fecha_solicitud::timestamp)
              >= date_trunc('week', now()) - make_interval(weeks => %(semanas)s - 1)
        GROUP BY 1 ORDER BY 1;""", semanas)
    cur.execute("""SELECT
                     (SELECT COUNT(*) FROM reportes_ia WHERE estado = 'pendiente') AS reportes_pendientes,
                     (SELECT COUNT(*) FROM alertas WHERE estado = 'activa') AS alertas_activas,
                     (SELECT COUNT(*) FROM agente_propuestas WHERE estado = 'pendiente') AS propuestas_agente_pendientes,
                     (SELECT COUNT(*) FROM agente_propuestas WHERE estado = 'aprobada') AS propuestas_agente_aprobadas,
                     (SELECT COUNT(*) FROM agente_propuestas WHERE estado = 'rechazada') AS propuestas_agente_rechazadas;""")
    totales = cur.fetchone()
    cur.close()
    registrar_auditoria(db, usuario["id"], "tablero_tendencia", "tablero", None, f"semanas={semanas}")
    return {"semanas": semanas, "reportes_ia": reportes, "alertas": alertas, "remisiones": remisiones,
            "totales": totales,
            "nota": "Datos agregados por semana: no identifican pacientes. Orientan la gestión, no deciden casos."}
