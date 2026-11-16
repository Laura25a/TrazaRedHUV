"""Genera una copia privada del contrato del profesor."""
import argparse
import json
import os
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destino", type=Path)
    args = parser.parse_args()
    plantilla = (Path(__file__).resolve().parents[1] / "tester.yaml").read_text()
    patron = re.compile(r'"\$\{([A-Z][A-Z0-9_]*)\}"')
    faltantes = sorted({n for n in patron.findall(plantilla) if not os.getenv(n)})
    if faltantes:
        parser.exit(2, "Faltan variables: " + ", ".join(faltantes) + "\n")
    datos = patron.sub(lambda m: json.dumps(os.environ[m[1]], ensure_ascii=False), plantilla)
    with os.fdopen(os.open(args.destino, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as salida:
        salida.write(datos)


if __name__ == "__main__":
    main()
