"""
TrazaRed HUV — Dataset del proyecto final (semana 8)
====================================================

Genera y carga un dataset SINTÉTICO de remisiones, pensado para el análisis de
la próxima clase (agrupamiento, estadística básica y toma de decisiones).

Tal como lo pidió el profesor (ejemplo diabéticos / sanos / borderline), los
datos NO son aleatorios: se construyen alrededor de tres grupos con
distribuciones definidas según el RESULTADO de la remisión:

  * efectiva   (~200): aceptada o completada con 1-2 contactos, casi siempre
                       con convenio vigente; paciente menos grave.
  * fallida    (~200): rechazada tras 4-7 contactos ("sin cupo en UCI", "no
                       hay especialista"...), casi nunca con convenio;
                       paciente más grave (triage alterado).
  * borderline (~200): pendiente o en gestión con 3-4 contactos, convenio
                       mixto; signos vitales intermedios que se traslapan con
                       los otros dos grupos.

Usa SOLO las tablas y colecciones que ya existen:
  PostgreSQL -> pacientes, remisiones, observaciones (signos vitales LOINC/UCUM)
  MongoDB    -> gestiones_contacto (misma estructura: remision_id + contactos[])

Paso 1 (generar): escribe los CSV en data/ (reproducibles, semilla fija).
Paso 2 (cargar):  lee esos CSV y los inserta. Es idempotente: si un paciente
                  (documento) ya existe, se salta él y su remisión.

Uso:
  python cargar_dataset.py                 # genera (si faltan) y carga
  python cargar_dataset.py --solo-generar  # solo escribe los CSV
  python cargar_dataset.py --regenerar     # vuelve a escribir los CSV y carga
  python cargar_dataset.py --sin-mongo     # carga solo PostgreSQL

En Docker:  docker compose exec api python cargar_dataset.py
En local:   usa PG_CONNECTION_STRING / MONGO_CONNECTION_STRING de pass.env
            (OJO: en local eso es Neon / Atlas, la base compartida del equipo).
"""
import argparse
import csv
import json
import os
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
DATA = RAIZ / "data"
load_dotenv(RAIZ / "pass.env", override=False)

SEMILLA = 2026
POR_GRUPO = 200
PREFIJO_DOC = "11"            # documentos sintéticos: 11xxxxxxxx (10 dígitos)
BOGOTA = timezone(timedelta(hours=-5))

# ---------------------------------------------------------------------------
# Catálogos (instituciones y EPS reales de Valle del Cauca / Cauca)
# ---------------------------------------------------------------------------
HUV = "Hospital Universitario del Valle"
DESTINOS = [  # a dónde remite el HUV (alta complejidad / servicios que le faltan cupo)
    "Fundación Valle del Lili", "Clínica Imbanaco", "Clínica de Occidente",
    "Clínica Nuestra Señora de los Remedios", "Clínica Farallones",
    "Hospital San Juan de Dios Cali", "Clínica Colombia Cali", "Clínica Versalles",
    "Clínica Sebastián de Belalcázar", "Hospital Universitario San José (Popayán)",
]
ORIGENES = [  # quién remite al HUV (baja/mediana complejidad de la red)
    "Hospital Isaías Duarte Cancino", "Hospital Mario Correa Rengifo",
    "Hospital Carlos Holmes Trujillo", "Hospital Departamental Tomás Uribe Uribe (Tuluá)",
    "Hospital San Rafael (Buga)", "Hospital Francisco de Paula Santander (Santander de Quilichao)",
    "Hospital Local de Villa Rica", "Hospital Raúl Orejuela Bueno (Palmira)",
    "Hospital Departamental de Buenaventura", "ESE Norte 2 (Caloto)",
]
EPS = [("Emssanar", 22), ("Coosalud", 16), ("Nueva EPS", 16), ("Asmet Salud", 12),
       ("SOS", 10), ("Sanitas", 9), ("Sura", 8), ("Salud Total", 7)]

# Motivo clínico -> servicio que se busca. Los "graves" pesan más en fallidas.
MOTIVOS_GRAVES = [
    "Choque séptico de origen abdominal; requiere UCI adultos",
    "Infarto agudo de miocardio con elevación del ST; requiere hemodinamia",
    "Trauma craneoencefálico severo; requiere neurocirugía y UCI",
    "Insuficiencia respiratoria aguda; requiere ventilación mecánica en UCI",
    "Hemorragia subaracnoidea; requiere neurocirugía",
    "Politraumatismo por accidente de tránsito; requiere UCI y cirugía",
    "Falla renal aguda con hiperpotasemia; requiere hemodiálisis urgente",
]
MOTIVOS_MODERADOS = [
    "Neumonía adquirida en la comunidad; requiere hospitalización en piso",
    "Fractura de cadera; requiere ortopedia y cirugía programada",
    "Diabetes descompensada; requiere manejo por medicina interna",
    "Dolor torácico en estudio; requiere valoración por cardiología",
    "Colecistitis aguda; requiere cirugía general",
    "Leucemia en estudio; requiere hematología oncológica",
    "Insuficiencia cardiaca descompensada; requiere cardiología",
    "Pie diabético infectado; requiere cirugía vascular",
]

NEGATIVAS = [
    "Sin cupo en UCI", "No hay especialista disponible", "Sin respuesta en la línea de referencia",
    "No hay convenio con la EPS del paciente", "Cama no disponible, llamar mañana",
    "Servicio de hemodinamia sin disponibilidad", "Rechazan por no tener autorización de la EPS",
]
PENDIENTES = [
    "En estudio, solicitan enviar historia clínica", "Pendiente valoración del especialista de turno",
    "Llamar en 2 horas para confirmar cupo", "Solicitan autorización de la EPS antes de aceptar",
]
ACEPTAS = [
    "Acepta paciente, cama asignada", "Acepta paciente en UCI", "Acepta paciente, enviar en ambulancia medicalizada",
    "Acepta paciente para valoración por especialista",
]
MEDIOS = [("telefono", 60), ("plataforma", 30), ("correo", 10)]
REFERENCISTAS = ["Dra. Paola Muñoz", "Dr. Andrés Caicedo", "Enf. Luz Mery Rivas", "Dr. Julián Mosquera",
                 "Dra. Carolina Ararat", "Enf. Jhon Fredy Balanta"]

NOMBRES_F = ["María", "Luz", "Ana", "Carmen", "Yolanda", "Rosa", "Diana", "Sandra", "Paola", "Daniela",
             "Valentina", "Laura", "Leidy", "Yuliana", "Marleny", "Gloria", "Nubia", "Angélica", "Natalia", "Karen"]
NOMBRES_M = ["José", "Luis", "Carlos", "Jorge", "Andrés", "Juan", "Jhon", "Fredy", "Wilson", "Diego",
             "Jhonatan", "Brayan", "Óscar", "Hernán", "Álvaro", "Víctor", "Julián", "Camilo", "Edwin", "Nelson"]
APELLIDOS = ["Carabalí", "Mina", "Balanta", "Mosquera", "Lucumí", "Ararat", "Viveros", "González", "Rodríguez",
             "Muñoz", "Gómez", "Rivas", "Caicedo", "Angulo", "Valencia", "Ortiz", "Castillo", "Zapata", "Díaz",
             "Hurtado", "Torres", "Ramírez", "Cortés", "Sinisterra", "Possú", "Obregón", "Quintero", "Loboa"]

# Signos vitales: (loinc, tipo, unidad, límites fisiológicos, decimales) y la
# media / desviación de cada grupo. Los grupos se traslapan a propósito.
VITALES = [
    ("8867-4",  "Frecuencia cardiaca",         "/min",    (35, 190), 0),
    ("8480-6",  "Presión arterial sistólica",  "mm[Hg]",  (60, 220), 0),
    ("8462-4",  "Presión arterial diastólica", "mm[Hg]",  (30, 130), 0),
    ("9279-1",  "Frecuencia respiratoria",     "/min",    (8, 45),   0),
    ("59408-5", "Saturación de oxígeno",       "%",       (70, 100), 0),
    ("8310-5",  "Temperatura corporal",        "Cel",     (34.5, 41.5), 1),
    ("9269-2",  "Escala de Glasgow",           "{score}", (3, 15),   0),
]
PERFIL = {  #            FC         PAS        PAD       FR        SpO2      Temp        Glasgow
    "efectiva":   [(88, 12), (124, 14), (77, 9), (18, 3.0), (95.5, 2.0), (37.0, 0.5), (14.7, 0.6)],
    "borderline": [(104, 13), (110, 16), (68, 9), (22, 3.5), (91.5, 3.0), (37.6, 0.7), (13.0, 1.8)],
    "fallida":    [(118, 15), (98, 18), (60, 10), (26, 4.0), (88.0, 4.0), (38.1, 0.8), (11.0, 2.5)],
}


# Semana 8: datos de la ficha. La edad depende del grupo (los pacientes de
# remisiones fallidas son, en promedio, mayores y más complejos).
EDAD = {"efectiva": (46, 17), "borderline": (56, 16), "fallida": (63, 14)}
TIPOS_SANGRE = [("O+", 61), ("A+", 26), ("B+", 8), ("AB+", 2), ("O-", 2), ("A-", 1)]
ALERGIAS = [("Sin alergias conocidas", 75), ("Penicilina", 9), ("AINES", 5), ("Medios de contraste yodados", 3),
            ("Sulfonamidas", 3), ("Mariscos", 3), ("Látex", 2)]


def elegir(rng, pares):
    return rng.choices([p[0] for p in pares], weights=[p[1] for p in pares])[0]


# ---------------------------------------------------------------------------
# Paso 1: generar
# ---------------------------------------------------------------------------
def generar():
    rng = random.Random(SEMILLA)
    pacientes, remisiones, observaciones, contactos = [], [], [], []
    usados = set()
    inicio, fin = date(2026, 3, 1), date(2026, 9, 20)

    grupos = ["efectiva"] * POR_GRUPO + ["fallida"] * POR_GRUPO + ["borderline"] * POR_GRUPO
    rng.shuffle(grupos)

    for i, grupo in enumerate(grupos, start=1):
        # --- paciente ---
        genero = rng.choice("FM")
        nombre = f"{rng.choice(NOMBRES_F if genero == 'F' else NOMBRES_M)} " \
                 f"{rng.choice(APELLIDOS)} {rng.choice(APELLIDOS)}"
        if i == 1:
            nombre, genero = "Ana Torres Mina", "F"   # la usa el usuario de rol paciente
        while True:
            doc = PREFIJO_DOC + f"{rng.randrange(10**8):08d}"
            if doc not in usados:
                usados.add(doc)
                break
        eps = elegir(rng, EPS)
        pacientes.append({"ref": i, "nombre": nombre, "documento": doc, "genero": genero, "eps": eps})

        # --- remisión ---
        if rng.random() < 0.7:
            origen, destino = HUV, rng.choice(DESTINOS)
        else:
            origen, destino = rng.choice(ORIGENES), HUV
        grave = rng.random() < {"efectiva": 0.25, "borderline": 0.5, "fallida": 0.75}[grupo]
        motivo = rng.choice(MOTIVOS_GRAVES if grave else MOTIVOS_MODERADOS)
        fecha = inicio + timedelta(days=rng.randrange((fin - inicio).days + 1))

        if grupo == "efectiva":
            estado = "completada" if rng.random() < 0.55 else "aceptada"
            convenio = rng.random() < 0.85
            n = 1 if rng.random() < 0.6 else 2
        elif grupo == "fallida":
            estado = "rechazada"
            convenio = rng.random() < 0.15
            n = rng.randint(4, 7)
        else:
            estado = "en_gestion" if rng.random() < 0.65 else "pendiente"
            convenio = rng.random() < 0.5
            n = rng.randint(3, 4)

        remisiones.append({
            "ref": i, "paciente_ref": i, "grupo": grupo, "institucion_origen": origen,
            "institucion_destino": destino, "fecha_solicitud": fecha.isoformat(), "motivo": motivo,
            "estado": estado, "convenio_vigente": convenio,
        })

        # --- signos vitales del triage (una toma al momento de la solicitud) ---
        hora = datetime(fecha.year, fecha.month, fecha.day, rng.randint(0, 20), rng.randint(0, 59), tzinfo=BOGOTA)
        for (loinc, tipo, unidad, (lo, hi), dec), (media, de) in zip(VITALES, PERFIL[grupo]):
            valor = min(max(rng.gauss(media, de), lo), hi)
            if loinc == "8462-4":  # la diastólica nunca por encima de la sistólica
                sistolica = observaciones[-1]["valor"]
                valor = min(valor, sistolica - 15)
            valor = round(valor, dec) if dec else int(round(valor))
            observaciones.append({
                "remision_ref": i, "tipo": tipo, "codigo_loinc": loinc, "valor": valor,
                "unidad": unidad, "fecha_observacion": hora.isoformat(),
            })

        # --- bitácora de contactos (MongoDB) ---
        t = hora + timedelta(minutes=rng.randint(10, 90))
        destinos_intentados = [destino] if grupo == "efectiva" else []
        for k in range(n):
            ultimo = k == n - 1
            if grupo == "efectiva" and ultimo:
                inst, resp = destino, rng.choice(ACEPTAS)
            elif grupo == "borderline" and ultimo:
                inst, resp = destino, rng.choice(PENDIENTES)
            else:
                inst = rng.choice([d for d in (DESTINOS if origen == HUV else [HUV] + DESTINOS)
                                   if d not in destinos_intentados] or DESTINOS)
                destinos_intentados.append(inst)
                resp = rng.choice(NEGATIVAS)
                if grupo == "fallida" and ultimo:
                    inst = destino
            contactos.append({
                "remision_ref": i, "orden": k + 1, "fecha": t.isoformat(),
                "medio": elegir(rng, MEDIOS), "institucion_contactada": inst,
                "contactado_por": rng.choice(REFERENCISTAS), "respuesta": resp,
            })
            espera = {"efectiva": (15, 120), "borderline": (60, 360), "fallida": (60, 480)}[grupo]
            t += timedelta(minutes=rng.randint(*espera))

    # Datos de la ficha con OTRA semilla: así los documentos y todo lo anterior
    # quedan idénticos a la primera versión del dataset.
    rng2 = random.Random(SEMILLA + 1)
    grupo_de = {r["paciente_ref"]: (r["grupo"], r["fecha_solicitud"]) for r in remisiones}
    for p in pacientes:
        grupo, fecha = grupo_de[p["ref"]]
        media, de = EDAD[grupo]
        edad = int(min(max(rng2.gauss(media, de), 18), 95))
        nacimiento = date.fromisoformat(fecha) - timedelta(days=365 * edad + rng2.randrange(365))
        p["fecha_nacimiento"] = nacimiento.isoformat()
        p["telefono"] = f"3{rng2.choice(['00','01','04','05','10','12','13','14','15','16','17','18','20','21'])}" \
                        f"{rng2.randrange(10**7):07d}"
        p["tipo_sangre"] = elegir(rng2, TIPOS_SANGRE)
        p["alergias"] = elegir(rng2, ALERGIAS)

    DATA.mkdir(exist_ok=True)
    escribir("pacientes.csv", pacientes)
    escribir("remisiones.csv", remisiones)
    escribir("observaciones.csv", observaciones)
    escribir("gestiones_contacto.csv", contactos)
    escribir_analitico(remisiones, pacientes, observaciones, contactos)
    print(f"[generar] {len(pacientes)} pacientes, {len(remisiones)} remisiones, "
          f"{len(observaciones)} signos vitales y {len(contactos)} contactos en {DATA}")


def escribir(nombre, filas):
    with open(DATA / nombre, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0].keys()))
        w.writeheader()
        w.writerows(filas)


def escribir_analitico(remisiones, pacientes, observaciones, contactos):
    """Una fila por remisión con todas las variables: la tabla lista para
    agrupamiento / estadística de la próxima clase."""
    pac = {p["ref"]: p for p in pacientes}
    vit = {}
    for o in observaciones:
        vit.setdefault(o["remision_ref"], {})[o["codigo_loinc"]] = o["valor"]
    con = {}
    for c in contactos:
        con.setdefault(c["remision_ref"], []).append(c)
    columnas_vit = {"8867-4": "fc", "8480-6": "pas", "8462-4": "pad", "9279-1": "fr",
                    "59408-5": "spo2", "8310-5": "temp", "9269-2": "glasgow"}
    filas = []
    for r in remisiones:
        cs = con[r["ref"]]
        t0 = datetime.fromisoformat(cs[0]["fecha"])
        t1 = datetime.fromisoformat(cs[-1]["fecha"])
        p = pac[r["paciente_ref"]]
        edad = (date.fromisoformat(r["fecha_solicitud"]) - date.fromisoformat(p["fecha_nacimiento"])).days // 365
        fila = {
            "remision_ref": r["ref"], "documento": p["documento"],
            "genero": p["genero"], "edad": edad, "eps": p["eps"],
            "sentido": "sale_del_HUV" if r["institucion_origen"] == HUV else "llega_al_HUV",
            "institucion_destino": r["institucion_destino"], "fecha_solicitud": r["fecha_solicitud"],
            "motivo_grave": int(any(r["motivo"] == m for m in MOTIVOS_GRAVES)),
            "convenio_vigente": int(r["convenio_vigente"]), "estado": r["estado"],
        }
        fila.update({nombre: vit[r["ref"]][loinc] for loinc, nombre in columnas_vit.items()})
        fila.update({
            "n_contactos": len(cs),
            "n_respuestas_negativas": sum(c["respuesta"] in NEGATIVAS for c in cs),
            "horas_gestion": round((t1 - t0).total_seconds() / 3600, 1),
            "grupo": r["grupo"],
        })
        filas.append(fila)
    escribir("dataset_analitico.csv", filas)


# ---------------------------------------------------------------------------
# Paso 2: cargar
# ---------------------------------------------------------------------------
def leer(nombre):
    with open(DATA / nombre, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def cargar(con_mongo=True):
    import psycopg2
    pg = os.getenv("PG_CONNECTION_STRING")
    if not pg:
        raise SystemExit("Falta PG_CONNECTION_STRING (pass.env o variable de entorno)")
    pacientes = leer("pacientes.csv")
    remisiones = {r["ref"]: r for r in leer("remisiones.csv")}
    observaciones, contactos = {}, {}
    for o in leer("observaciones.csv"):
        observaciones.setdefault(o["remision_ref"], []).append(o)
    for c in leer("gestiones_contacto.csv"):
        contactos.setdefault(c["remision_ref"], []).append(c)

    conn = psycopg2.connect(pg)
    cur = conn.cursor()
    cur.execute("SELECT id FROM usuarios WHERE correo = 'medico@huv.gov.co';")
    fila = cur.fetchone()
    medico_id = fila[0] if fila else None

    coleccion = None
    if con_mongo:
        import pymongo
        cliente = pymongo.MongoClient(os.getenv("MONGO_CONNECTION_STRING"), serverSelectionTimeoutMS=8000)
        coleccion = cliente["trazared_huv"]["gestiones_contacto"]

    nuevos = saltados = 0
    docs_mongo = []
    for p in pacientes:
        extra = (p.get("fecha_nacimiento") or None, p.get("telefono") or None,
                 p.get("tipo_sangre") or None, p.get("alergias") or None)
        cur.execute(
            """INSERT INTO pacientes (nombre, documento, genero, eps,
                                      fecha_nacimiento, telefono, tipo_sangre, alergias)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (documento) DO NOTHING RETURNING id;""",
            (p["nombre"], p["documento"], p["genero"], p["eps"], *extra),
        )
        fila = cur.fetchone()
        if fila is None:          # ya estaba cargado: solo se completan los datos de la ficha
            cur.execute(
                """UPDATE pacientes SET fecha_nacimiento = COALESCE(fecha_nacimiento, %s),
                          telefono = COALESCE(telefono, %s), tipo_sangre = COALESCE(tipo_sangre, %s),
                          alergias = COALESCE(alergias, %s)
                    WHERE documento = %s;""",
                (*extra, p["documento"]),
            )
            saltados += 1
            continue
        paciente_id = fila[0]
        r = remisiones[p["ref"]]
        cur.execute(
            """INSERT INTO remisiones (paciente_id, institucion_origen, institucion_destino,
                   fecha_solicitud, motivo, estado, convenio_vigente, creado_por)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;""",
            (paciente_id, r["institucion_origen"], r["institucion_destino"], r["fecha_solicitud"],
             r["motivo"], r["estado"], r["convenio_vigente"] == "True", medico_id),
        )
        remision_id = cur.fetchone()[0]
        for o in observaciones[r["ref"]]:
            cur.execute(
                """INSERT INTO observaciones (remision_id, tipo, codigo_loinc, valor, unidad, fecha_observacion)
                   VALUES (%s, %s, %s, %s, %s, %s);""",
                (remision_id, o["tipo"], o["codigo_loinc"], o["valor"], o["unidad"],
                 datetime.fromisoformat(o["fecha_observacion"]).astimezone(timezone.utc).replace(tzinfo=None)),
            )
        cs = contactos[r["ref"]]
        docs_mongo.append({
            "remision_id": remision_id,
            "contactos": [{
                "fecha": datetime.fromisoformat(c["fecha"]).astimezone(timezone.utc),
                "medio": c["medio"], "institucion_contactada": c["institucion_contactada"],
                "contactado_por": c["contactado_por"], "respuesta": c["respuesta"],
            } for c in cs],
            "actualizado_en": datetime.fromisoformat(cs[-1]["fecha"]).astimezone(timezone.utc),
            "origen": "dataset_sintetico_semana8",
        })
        nuevos += 1
    conn.commit()

    if coleccion is not None and docs_mongo:
        coleccion.insert_many(docs_mongo)

    # Usuario de rol paciente (necesita un paciente existente para vincularse)
    clave = os.getenv("DEMO_PACIENTE_PASSWORD")
    if clave:
        from passlib.context import CryptContext
        cur.execute("SELECT id FROM pacientes WHERE documento = %s;", (pacientes[0]["documento"],))
        fila = cur.fetchone()
        if fila:
            cur.execute(
                """INSERT INTO usuarios (nombre, correo, contrasena_hash, rol, paciente_id)
                   VALUES (%s, 'paciente@correo.com', %s, 'paciente', %s)
                   ON CONFLICT (correo) DO NOTHING;""",
                (pacientes[0]["nombre"], CryptContext(schemes=["bcrypt"]).hash(clave), fila[0]),
            )
            if cur.rowcount:
                print(f"[cargar] usuario paciente@correo.com vinculado a {pacientes[0]['nombre']}")
    conn.commit()
    cur.close()
    conn.close()
    print(f"[cargar] {nuevos} remisiones nuevas cargadas "
          f"({nuevos * 7} signos vitales, {len(docs_mongo)} bitácoras en MongoDB); "
          f"{saltados} ya existían y se saltaron")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Dataset sintético de remisiones TrazaRed HUV")
    ap.add_argument("--solo-generar", action="store_true", help="solo escribe los CSV en data/")
    ap.add_argument("--regenerar", action="store_true", help="reescribe los CSV aunque ya existan")
    ap.add_argument("--sin-mongo", action="store_true", help="no carga las gestiones en MongoDB")
    args = ap.parse_args()

    if args.regenerar or args.solo_generar or not (DATA / "remisiones.csv").exists():
        generar()
    if not args.solo_generar:
        cargar(con_mongo=not args.sin_mongo)
