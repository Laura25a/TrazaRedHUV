"""Ejercicio del 3 de noviembre con medidas sintéticas, fuera de la API clínica."""
import argparse
from typing import Annotated, Literal
from xml.etree import ElementTree as ET

from langchain.tools import tool
from pydantic import BaseModel, Field

Positivo = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Porcentaje = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]


class Medidas(BaseModel):
    peso_kg: Positivo
    altura_m: Positivo
    grasa_pct: Porcentaje


@tool
def calcular_imc(peso_kg: Positivo, altura_m: Positivo) -> float:
    """Calcula el IMC a partir del peso en kg y la altura en metros."""
    return round(peso_kg / altura_m / altura_m, 2)


@tool
def calcular_masa_grasa(peso_kg: Positivo, grasa_pct: Porcentaje) -> float:
    """Calcula la masa grasa en kg. Solicita el porcentaje medido si no lo tienes."""
    return round(peso_kg * (grasa_pct / 100), 2)


@tool
def formatear_medidas(imc: Positivo, grasa_kg: Annotated[float, Field(ge=0, allow_inf_nan=False)],
                     formato: Literal["markdown", "xml"]) -> str:
    """Presenta las dos medidas como tabla Markdown o como XML estructurado."""
    if formato == "markdown":
        return f"| Medida | Valor |\n| --- | ---: |\n| IMC (kg/m²) | {imc:.2f} |\n| Masa grasa (kg) | {grasa_kg:.2f} |"
    raiz = ET.Element("medidas")
    ET.SubElement(raiz, "imc", unidad="kg/m²").text = f"{imc:.2f}"
    ET.SubElement(raiz, "masa_grasa", unidad="kg").text = f"{grasa_kg:.2f}"
    return ET.tostring(raiz, encoding="unicode")


def ejecutar(medidas: Medidas, formato: Literal["markdown", "xml"]) -> str:
    imc = calcular_imc.invoke({"peso_kg": medidas.peso_kg, "altura_m": medidas.altura_m})
    grasa_kg = calcular_masa_grasa.invoke({"peso_kg": medidas.peso_kg, "grasa_pct": medidas.grasa_pct})
    return formatear_medidas.invoke({"imc": imc, "grasa_kg": grasa_kg, "formato": formato})


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--peso-kg", type=float, required=True)
    cli.add_argument("--altura-m", type=float, required=True)
    cli.add_argument("--grasa-pct", type=float, required=True)
    cli.add_argument("--formato", choices=("markdown", "xml"), default="markdown")
    argumentos = vars(cli.parse_args())
    formato = argumentos.pop("formato")
    print(ejecutar(Medidas(**argumentos), formato))
