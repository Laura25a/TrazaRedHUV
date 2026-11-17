"""Avisos clínicos dirigidos y trabajo de análisis por episodio."""
from eventos import ids_con_rol, registrar_evento
from validacion_clinica import PAS

# Extremos de NEWS2 usados como señales de revisión en la demo (no es un
# puntaje NEWS2). Para SpO₂ se usa la escala 1. Glucosa mantiene el perfil local.
LIMITES_DEMO = {
    "2339-0": lambda valor: valor < 54 or valor >= 300,
    PAS: lambda valor: valor <= 90 or valor >= 220,
    "8867-4": lambda valor: valor <= 40 or valor >= 131,
    "9279-1": lambda valor: valor <= 8 or valor >= 25,
    "59408-5": lambda valor: valor <= 91,
    "8310-5": lambda valor: valor <= 35,
}
VITALES_KTAS = (PAS, "8462-4", "8867-4", "9279-1", "59408-5", "8310-5")


def responsable_episodio(db, episodio):
    if episodio:
        asignado = episodio.get("atendida_por") or episodio.get("creado_por")
        with db.cursor() as cur:
            cur.execute("""SELECT id FROM usuarios
                           WHERE id = %s AND activo AND rol IN ('medico', 'especialista');""", (asignado,))
            fila = cur.fetchone()
            if fila:
                return fila[0]
    return None


def episodio_activo(db, paciente_id):
    """Si el emisor solo da el paciente, asociar únicamente un episodio inequívoco."""
    import psycopg2.extras

    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT id, creado_por, atendida_por FROM remisiones
                       WHERE paciente_id = %s AND activo
                         AND estado IN ('pendiente', 'en_gestion', 'aceptada')
                       ORDER BY id LIMIT 2 FOR UPDATE;""", (paciente_id,))
        filas = cur.fetchall()
    return filas[0] if len(filas) == 1 else None


def destino_episodio(db, episodio):
    responsable = responsable_episodio(db, episodio)
    return responsable, [responsable] if responsable is not None else ids_con_rol(db, "admin")


def registrar_observaciones(db, episodio, paciente_id, filas, tipo="observacion_clinica", **datos):
    responsable, destinos = destino_episodio(db, episodio)
    remision_id = episodio["id"] if episodio else None
    criticas = []
    lecturas = []
    cambios_ktas = []
    for fila in filas:
        codigo = fila.get("codigo_loinc")
        if codigo == "2708-6":
            codigo = "59408-5"
        valor = float(fila["valor"])
        texto = f"{fila['tipo']} {valor:g} {fila.get('unidad') or ''}".strip()
        lecturas.append(texto)
        if codigo in LIMITES_DEMO and LIMITES_DEMO[codigo](valor):
            criticas.append(texto)
        if codigo in VITALES_KTAS and fila.get("id") is not None:
            cambios_ktas.append(fila["id"])
    contexto = {"paciente_id": paciente_id, "remision_id": remision_id, **datos}
    registrar_evento(db, destinos, {"tipo": tipo, "mensaje": "Recibido: " + ", ".join(lecturas), **contexto})
    if criticas:
        registrar_evento(db, destinos, {
            "tipo": "alerta_critica", "mensaje": "Valores para revisión prioritaria: " + ", ".join(criticas),
            "responsable_id": responsable, **contexto,
        })
    if remision_id is not None and responsable is not None and cambios_ktas:
        with db.cursor() as cur:
            cur.execute("""INSERT INTO analisis_automaticos_pendientes (remision_id, observacion_id)
                           VALUES (%s, %s)
                           ON CONFLICT (remision_id) DO UPDATE
                           SET observacion_id = EXCLUDED.observacion_id, intentar_en = now();""",
                        (remision_id, max(cambios_ktas)))
