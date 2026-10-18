"""
R06 - Radiografías reales (NIH ChestX-ray14) en el PACS, en lugar de las sintéticas.
Uso: docker compose run --rm -v RUTA\datos_imagenes:/datos_imagenes:ro api python sembrar_imagenes_reales.py --max 60
"""
import argparse
import csv
import io
import os
import random
from pathlib import Path

import psycopg2
import psycopg2.extras
from PIL import Image

import pacs


CARPETA = Path("/datos_imagenes")
DESC = "Radiografía de tórax PA (NIH ChestX-ray14)"
RAIZ = Path(__file__).resolve().parent.parent


def cargar_candidatas():
    """Radiografías de adultos, vista PA, primera visita, que existen en la carpeta."""
    disponibles = {f.name for f in (CARPETA / "images_001" / "images").glob("*.png")}
    con, sin = [], []
    with open(CARPETA / "Data_Entry_2017_v2020.csv", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            nombre = r["Image Index"]
            if nombre not in disponibles or r["Follow-up #"] != "0" or r["View Position"] != "PA":
                continue
            if not 18 <= int(r["Patient Age"]) <= 100:
                continue
            (sin if r["Finding Labels"] == "No Finding" else con).append(nombre)
    return con, sin


def main(maximo: int):
    con, sin = cargar_candidatas()
    print(f"[imágenes] candidatas: {len(con)} con hallazgo, {len(sin)} sin hallazgo")

    # grupo de cada paciente (igual que en sembrar_imagenes.py)
    grupos = {}
    ruta, pac_csv = RAIZ / "data" / "remisiones.csv", RAIZ / "data" / "pacientes.csv"
    if ruta.exists() and pac_csv.exists():
        docs = {p["ref"]: p["documento"] for p in csv.DictReader(open(pac_csv, encoding="utf-8"))}
        grupos = {docs[r["paciente_ref"]]: r["grupo"] for r in csv.DictReader(open(ruta, encoding="utf-8"))}

    conn = psycopg2.connect(os.getenv("PG_CONNECTION_STRING"))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT DISTINCT ON (p.id) p.id, p.nombre, p.documento, p.genero, p.fecha_nacimiento,
                  r.motivo, r.fecha_solicitud
             FROM pacientes p JOIN remisiones r ON r.paciente_id = p.id
            WHERE r.activo
            ORDER BY p.id, r.fecha_solicitud DESC;"""
    )
    candidatos = cur.fetchall()
    cur.close()
    conn.close()

    usadas, creadas, omitidas = set(), 0, 0
    for p in candidatos[:maximo]:
        previas = pacs.list_images(p["documento"])
        if any("NIH" in i["descripcion"] for i in previas):
            omitidas += 1
            continue
        grupo = grupos.get(p["documento"], "borderline")
        pool = con if grupo == "fallida" else sin if grupo == "efectiva" else con + sin
        pool = [n for n in pool if n not in usadas]
        nombre = random.Random(p["id"]).choice(pool)
        usadas.add(nombre)

        # quita la imagen sintética de este paciente
        for pid in pacs.orthanc("POST", "/tools/find",
                                json={"Level": "Patient", "Query": {"PatientID": p["documento"]}}).json():
            pacs.orthanc("DELETE", f"/patients/{pid}")

        buf = io.BytesIO()
        Image.open(CARPETA / "images_001" / "images" / nombre).convert("L").save(buf, format="PNG")
        pacs.dicomize(buf.getvalue(), p, DESC, "CR", p["fecha_solicitud"].strftime("%Y%m%d"))
        creadas += 1
        print(f"  {p['documento']}  {p['nombre'][:28]:28s} {nombre}  ({grupo})")
    print(f"[imágenes] {creadas} reales cargadas en el PACS; {omitidas} ya tenían la real")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Radiografías reales (NIH) para el PACS")
    ap.add_argument("--max", type=int, default=60, help="máximo de pacientes (60)")
    main(ap.parse_args().max)