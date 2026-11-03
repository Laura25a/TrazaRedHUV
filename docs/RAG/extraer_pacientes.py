#!/home/nicolas/venv_global/bin/python
"""Extrae pacientes REALES de la API de TrazaRed y los convierte en documentos
de paciente para el corpus del taller RAG.

Es la "llamada a base de datos" que el profe pidió mostrar: este script hace
login con el usuario demo del sistema, consulta la historia clínica y los
reportes de IA, y escribe documentos H## en docs/RAG/corpus/pacientes/reales/
con el MISMO formato que el notebook lee.

Requisitos: la API arriba (cd docker && docker compose up -d, puerto 8000).
Uso: /home/nicolas/venv_global/bin/python extraer_pacientes.py [id_paciente ...]
"""
import sys
from pathlib import Path

import httpx

BASE = "http://localhost:8000/api"
RAIZ = Path(__file__).resolve().parents[2]      # .../TrazaRedHUV
SALIDA = Path(__file__).resolve().parent / "corpus" / "pacientes" / "reales"
IDS = [int(a) for a in sys.argv[1:]] or [1, 2, 3]


def cargar_env(ruta: Path) -> dict:
    datos = {}
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            k, v = linea.split("=", 1)
            datos[k.strip()] = v.strip().strip('"').strip("'")
    return datos


def main() -> int:
    env = cargar_env(RAIZ / "pass.env")
    clave = env.get("DEMO_MEDICO_PASSWORD", "")
    if not clave:
        print("pass.env no tiene DEMO_MEDICO_PASSWORD; no sigo.")
        return 1

    r = httpx.post(f"{BASE}/login", data={"username": "medico@huv.gov.co", "password": clave}, timeout=15)
    r.raise_for_status()
    token = r.json().get("access_token") or r.json().get("token")
    headers = {"Authorization": f"Bearer {token}"}
    print("Login OK (token obtenido, no se imprime).")

    SALIDA.mkdir(parents=True, exist_ok=True)
    n = 0
    for pid in IDS:
        resp = httpx.get(f"{BASE}/pacientes/{pid}/historia", headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"paciente {pid}: HTTP {resp.status_code}, salto")
            continue
        datos = resp.json()
        # La historia llega como lista de eventos (atenciones, signos, reportes IA).
        # Se aplana a texto plano tal como lo indexaría el taller.
        lineas = [f"DOC_ID: R{pid:02d}", "TIPO: historia", "ESTADO: aprobado",
                  f"PACIENTE_ID: {pid}", "NOMBRE: (de la base, el taller lo seudonimiza)",
                  "FUENTE: API TrazaRed GET /pacientes/{pid}/historia", "---"]
        if isinstance(datos, list):
            for evento in datos:
                lineas.append(str(evento))
                lineas.append("")
        else:
            lineas.append(str(datos))
            lineas.append("")
        destino = SALIDA / f"R{pid:02d}.md"
        destino.write_text("\n".join(lineas), encoding="utf-8")
        print(f"paciente {pid}: -> {destino.name} ({len(lineas)} líneas)")
        n += 1

    print(f"{n} documentos reales en {SALIDA}. Re-ejecutar el notebook para indexarlos.")
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
