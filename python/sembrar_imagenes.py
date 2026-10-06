"""
TrazaRed HUV — Base de imágenes del proyecto final (semana 8)
=============================================================

Crea imágenes médicas SINTÉTICAS (no son clínicas, son "fantomas" de práctica)
y las guarda en el PACS (Orthanc) como DICOM, enlazadas a los pacientes del
dataset por el tag PatientID = documento.

El tipo de imagen depende del motivo de la remisión:
  * Radiografía de tórax (CR)  -> neumonía, insuficiencia respiratoria,
                                   politraumatismo, insuficiencia cardiaca,
                                   dolor torácico / infarto
  * TAC de cráneo (CT)         -> trauma craneoencefálico, hemorragia subaracnoidea

Los pacientes más graves (grupo "fallida" del dataset) tienen hallazgos más
marcados (opacidades más extensas, sangrado más grande), así la base de imágenes
queda alineada con los datos tabulares para el análisis posterior.

Las imágenes salen con POCO contraste a propósito: al subir el contraste en el
visor de la interfaz aparecen las estructuras (igual que el fantoma de la clase).

Es idempotente: si un paciente ya tiene imágenes en el PACS, se salta.

Uso (en Docker):  docker compose exec api python sembrar_imagenes.py
                  docker compose exec api python sembrar_imagenes.py --max 80
"""
import argparse
import csv
import io
import math
import os
import random
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

import pacs

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / "pass.env", override=False)

TORAX = ("neumonía", "insuficiencia respiratoria", "politraumatismo", "insuficiencia cardiaca",
         "dolor torácico", "infarto")
CRANEO = ("craneoencefálico", "subaracnoidea")


# ---------------------------------------------------------------------------
# Fantomas
# ---------------------------------------------------------------------------
def rx_torax(seed: int, severidad: float, size: int = 512) -> Image.Image:
    """Radiografía de tórax PA sintética. severidad 0..1 = extensión de la opacidad."""
    rng = random.Random(seed)
    img = Image.new("L", (size, size), 18)
    d = ImageDraw.Draw(img)
    c = size // 2
    # tronco (tejido blando)
    d.ellipse([c - 215, 40, c + 215, size + 160], fill=105)
    # campos pulmonares (aire = oscuro)
    for lado in (-1, 1):
        cx = c + lado * 92
        d.ellipse([cx - 78, 95, cx + 78, 440], fill=45)
    # corazón (silueta más clara, desplazada a la izquierda del paciente = derecha de la imagen)
    d.ellipse([c - 60, 250, c + 120, 430], fill=130)
    # columna y mediastino
    d.rectangle([c - 22, 60, c + 22, size], fill=150)
    # costillas: arcos claros
    for i in range(9):
        y = 110 + i * 34
        for lado in (-1, 1):
            x0 = c + lado * 20
            ancho = 150 - abs(i - 4) * 8          # más cortas arriba y abajo: quedan dentro del tórax
            caja = [min(x0, x0 + lado * ancho), y - 28, max(x0, x0 + lado * ancho), y + 40]
            d.arc(caja, 200 if lado < 0 else -20, 340 if lado < 0 else 160, fill=135, width=6)
    # clavículas
    d.line([c - 170, 95, c - 20, 120], fill=160, width=9)
    d.line([c + 20, 120, c + 170, 95], fill=160, width=9)
    # hallazgo: consolidación / opacidad parcheada en uno o ambos pulmones
    n = 1 + int(severidad * 6)
    for _ in range(n):
        lado = rng.choice((-1, 1))
        cx = c + lado * rng.randint(55, 120)
        cy = rng.randint(250, 410)
        r = rng.randint(22, 34 + int(40 * severidad))
        capa = Image.new("L", (size, size), 0)
        ImageDraw.Draw(capa).ellipse([cx - r, cy - r, cx + r, cy + r], fill=int(120 + 110 * severidad))
        capa = capa.filter(ImageFilter.GaussianBlur(12))
        img = Image.composite(Image.new("L", (size, size), 118), img, capa)
    img = img.filter(ImageFilter.GaussianBlur(2.2))
    ruido = Image.effect_noise((size, size), 10)
    img = Image.blend(img, ruido, 0.08)
    img = ImageEnhance.Contrast(img).enhance(0.55)
    return ImageEnhance.Brightness(img).enhance(1.35)


def tac_craneo(seed: int, severidad: float, size: int = 512) -> Image.Image:
    """Corte axial de TAC de cráneo sintético con sangrado (hiperdenso) según severidad."""
    rng = random.Random(seed)
    img = Image.new("L", (size, size), 8)
    d = ImageDraw.Draw(img)
    c = size // 2
    # cuero cabelludo, hueso (anillo muy claro) y parénquima
    d.ellipse([c - 200, c - 235, c + 200, c + 235], fill=70)
    d.ellipse([c - 188, c - 223, c + 188, c + 223], fill=235)
    d.ellipse([c - 170, c - 205, c + 170, c + 205], fill=112)
    # sustancia blanca (un poco más oscura) y surcos
    d.ellipse([c - 120, c - 150, c + 120, c + 150], fill=104)
    # cisura interhemisférica
    d.line([c, c - 205, c, c + 205], fill=80, width=3)
    # ventrículos laterales (líquido = oscuro), forma de mariposa
    for lado in (-1, 1):
        d.ellipse([c + lado * 8 - (40 if lado < 0 else 0), c - 70, c + lado * 8 + (0 if lado < 0 else 40), c + 45], fill=45)
    # hallazgo: hematoma / sangrado hiperdenso
    lado = rng.choice((-1, 1))
    r = int(16 + 45 * severidad)
    cx, cy = c + lado * rng.randint(70, 115), c + rng.randint(-90, 80)
    capa = Image.new("L", (size, size), 0)
    ImageDraw.Draw(capa).ellipse([cx - r, cy - int(r * 1.3), cx + r, cy + int(r * 1.3)], fill=255)
    capa = capa.filter(ImageFilter.GaussianBlur(4))
    img = Image.composite(Image.new("L", (size, size), 160), img, capa)
    img = img.filter(ImageFilter.GaussianBlur(1.3))
    img = Image.blend(img, Image.effect_noise((size, size), 12), 0.07)
    img = ImageEnhance.Contrast(img).enhance(0.6)
    return ImageEnhance.Brightness(img).enhance(1.25)


# ---------------------------------------------------------------------------
def main(maximo: int):
    grupos = {}
    ruta = RAIZ / "data" / "remisiones.csv"
    pac_csv = RAIZ / "data" / "pacientes.csv"
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
    candidatos = []
    for fila in cur.fetchall():
        m = fila["motivo"].lower()
        if any(k in m for k in CRANEO):
            candidatos.append((fila, "CT"))
        elif any(k in m for k in TORAX):
            candidatos.append((fila, "CR"))
    cur.close()
    conn.close()

    creadas = omitidas = 0
    for i, (p, modalidad) in enumerate(candidatos[:maximo]):
        if pacs.list_images(p["documento"]):
            omitidas += 1
            continue
        grupo = grupos.get(p["documento"], "borderline")
        severidad = {"efectiva": 0.2, "borderline": 0.5, "fallida": 0.85}[grupo]
        rng = random.Random(p["id"])
        severidad = min(max(severidad + rng.uniform(-0.15, 0.15), 0.05), 1)
        if modalidad == "CR":
            imagen, desc = rx_torax(1000 + p["id"], severidad), "Radiografía de tórax PA (sintética)"
        else:
            imagen, desc = tac_craneo(2000 + p["id"], severidad), "TAC de cráneo simple (sintética)"
        buf = io.BytesIO()
        imagen.save(buf, format="PNG")
        pacs.dicomize(buf.getvalue(), p, desc, modalidad, p["fecha_solicitud"].strftime("%Y%m%d"))
        creadas += 1
        print(f"  {p['documento']}  {p['nombre'][:28]:28s} {modalidad}  ({grupo})")
    print(f"[imágenes] {creadas} creadas en el PACS; {omitidas} pacientes ya tenían imágenes")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Base de imágenes sintéticas para el PACS")
    ap.add_argument("--max", type=int, default=60, help="máximo de pacientes a los que se les crea imagen (60)")
    main(ap.parse_args().max)
