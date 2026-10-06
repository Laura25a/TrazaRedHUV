"""
Cliente del PACS (Orthanc) de TrazaRed HUV — semana 8.

Regla de arquitectura (igual que en la clase): el navegador NUNCA habla con
Orthanc. Solo esta API lo hace, después de comprobar el token y el rol, y deja
cada acceso en la tabla auditoria. Orthanc vive dentro de la red de Docker.

Vínculo con nuestra base: el tag DICOM PatientID (0010,0020) guarda el número
de documento del paciente (pacientes.documento).
"""
import base64
import os
import re

import httpx
from fastapi import HTTPException

ORTHANC_URL = os.getenv("ORTHANC_URL", "http://orthanc:8042")
ORTHANC_AUTH = (os.getenv("ORTHANC_USER", "api"), os.getenv("ORTHANC_PASSWORD", "orthanc_dev_2026"))

# Los IDs de Orthanc son 5 grupos de 8 caracteres hexadecimales separados por guiones.
INSTANCE_ID = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{8}){4}$")

# Modalidades DICOM que usamos (código -> nombre para la interfaz)
MODALIDADES = {"CR": "Radiografía", "DX": "Radiografía digital", "CT": "Tomografía",
               "MR": "Resonancia", "US": "Ecografía", "OT": "Otra"}


def orthanc(method: str, path: str, **kwargs) -> httpx.Response:
    """Petición a Orthanc; traduce sus fallos a errores HTTP claros."""
    try:
        r = httpx.request(method, ORTHANC_URL + path, auth=ORTHANC_AUTH, timeout=30, **kwargs)
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="El servidor de imágenes (PACS) no está disponible")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="Imagen no encontrada")
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"El PACS respondió con error {r.status_code}")
    return r


def is_alive() -> bool:
    try:
        return httpx.get(ORTHANC_URL + "/system", auth=ORTHANC_AUTH, timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def check_instance_id(instance_id: str) -> None:
    """El ID llega en la URL y se pega en otra URL: se valida (evita rutas tipo ../../algo)."""
    if not INSTANCE_ID.match(instance_id):
        raise HTTPException(status_code=404, detail="Imagen no encontrada")


def list_images(documento: str) -> list:
    """Imágenes de un paciente (búsqueda por PatientID = documento), la más reciente primero."""
    ids = orthanc("POST", "/tools/find",
                  json={"Level": "Instance", "Query": {"PatientID": documento}}).json()
    imagenes = []
    for instance_id in ids:
        t = orthanc("GET", f"/instances/{instance_id}/simplified-tags").json()
        modalidad = t.get("Modality") or "OT"
        imagenes.append({
            "instance_id": instance_id,
            "descripcion": t.get("SeriesDescription") or t.get("StudyDescription") or "Imagen",
            "modalidad": modalidad,
            "modalidad_nombre": MODALIDADES.get(modalidad, modalidad),
            "fecha": t.get("StudyDate"),
            "hora": t.get("StudyTime") or "",
            "filas": int(t.get("Rows") or 0),
            "columnas": int(t.get("Columns") or 0),
        })
    imagenes.sort(key=lambda i: (i["fecha"] or "", i["hora"]), reverse=True)
    return imagenes


def instance_patient_id(instance_id: str) -> str:
    """¿De qué paciente (documento) es esta imagen?"""
    return orthanc("GET", f"/instances/{instance_id}/simplified-tags").json().get("PatientID", "")


def dicomize(png_bytes: bytes, paciente: dict, descripcion: str, modalidad: str = "OT",
             fecha: str = None) -> str:
    """Convierte un PNG en una imagen DICOM dentro de Orthanc y devuelve su ID."""
    partes = paciente["nombre"].replace("^", " ").split()
    nombre = partes[0] if partes else ""
    apellidos = " ".join(partes[1:]) or nombre
    descripcion = "".join(ch for ch in descripcion if ch.isprintable())[:64] or "Imagen clínica"
    tags = {
        "PatientID": paciente["documento"],
        "PatientName": f"{apellidos}^{nombre}"[:64],          # formato DICOM: Apellidos^Nombre
        "PatientSex": {"F": "F", "M": "M"}.get(paciente.get("genero"), "O"),
        "StudyDescription": descripcion,
        "SeriesDescription": descripcion,
        "Modality": modalidad if modalidad in MODALIDADES else "OT",
        "InstitutionName": "Hospital Universitario del Valle",
    }
    if paciente.get("fecha_nacimiento"):
        tags["PatientBirthDate"] = paciente["fecha_nacimiento"].strftime("%Y%m%d")
    if fecha:
        tags["StudyDate"] = fecha                              # AAAAMMDD
    body = {"Content": "data:image/png;base64," + base64.b64encode(png_bytes).decode(), "Tags": tags}
    return orthanc("POST", "/tools/create-dicom", json=body).json()["ID"]
