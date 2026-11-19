"""Evita acreditar una entrega por un 200 degradado o un frontend de otra versión."""
from pathlib import Path
import sys
from types import SimpleNamespace

import yaml

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "scripts"))
from verificar_entrega_publica import estatico_correcto, salud_completa


def test_salud_200_degradada_no_acredita_disponibilidad():
    servicio = {k: "ok" for k in ("api", "postgres", "mongo", "pacs", "fhir")}
    r = SimpleNamespace(status_code=200, json=lambda: servicio)
    assert salud_completa(r)
    servicio["mongo"] = "error"
    assert not salud_completa(r)
    servicio.pop("mongo")
    assert not salud_completa(r)


def test_css_html_o_desactualizado_no_acredita_frontend(tmp_path):
    css = tmp_path / "estilos.css"
    css.write_text("body { color: black; }")
    r = SimpleNamespace(status_code=200, headers={"content-type": "text/css"}, content=css.read_bytes())
    assert estatico_correcto(r, css)
    r.content = b"body { color: red; }"
    assert not estatico_correcto(r, css)
    r.content = css.read_bytes()
    r.headers["content-type"] = "text/html"
    assert not estatico_correcto(r, css)


def test_contrato_declara_indicadores_y_r33_pendiente():
    contrato = yaml.safe_load((RAIZ / "tester.yaml").read_text())
    escenarios = contrato["escenarios_integridad"]
    assert set(escenarios) == {"fechas_imposibles", "gestiones_huerfanas", "catalogo_y_transiciones",
                               "semantica_fhir", "conciliacion_202"}
    assert "observacion_conservada_en_historia" in escenarios["conciliacion_202"]["indicadores"]
    assert contrato["verificacion_manual"]["L05_R33"]["estado"] == "pendiente"
