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

Quién lo llama hoy (y quién lo llamará):
  * auth.py          -> "usuario_bloqueado"   (Persona 1)
  * remisiones.py    -> "remision_creada", "remision_aceptada"   (Persona 3)
  * ia.py            -> "reporte_aprobado", "alerta_critica"     (Persona 2)
  * fhir (rest-hook) -> "observacion_fhir"                       (Persona 3)

Lo que falta (Persona 4): que publicar() guarde la notificación en la bandeja
de cada destinatario (R18), la empuje por el canal en tiempo real SSE (R17) y
dispare las reacciones automáticas, como crear la factura al aprobar un
reporte (R19). Mientras tanto, publicar() solo deja el evento en la consola
del contenedor, y NUNCA lanza errores: un aviso que falla no puede tumbar el
login ni una remisión.
"""
from datetime import datetime, timezone
from typing import Iterable



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

    TODO (Persona 4): guardar en la bandeja, enviar por SSE y correr los
    reactores de R19/R20. Este es el ÚNICO lugar que hay que tocar: los demás
    módulos ya lo llaman.
    """
    try:
        destinatarios = list(destinatarios)
        evento = dict(evento)
        evento.setdefault("fecha", datetime.now(timezone.utc).isoformat())
        print(f"[evento] {evento.get('tipo')} -> usuarios {destinatarios}: {evento.get('mensaje')}")
    except Exception as e:  # un aviso nunca debe romper la acción que lo disparó
        print(f"[evento] no se pudo publicar: {e}")
