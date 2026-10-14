"""
historia.py — Historia clínica en PDF (Corte 2, Persona 3: R11).

Genera un PDF con los datos del paciente, sus observaciones y los reportes
de IA aprobados. Solo admin, médico y especialista pueden descargarla.
"""
import io

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

from auth import get_db, get_usuario_actual, registrar_auditoria, ROLES_CLINICOS, requiere_rol

router = APIRouter(tags=["Historia clínica"])


def _obtener_reportes_ia(db, paciente_id: int) -> list:
    try:
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT id, estado, fecha, resultado, probabilidad
               FROM reportes_ia
               WHERE paciente_id = %s AND estado = 'aprobado'
               ORDER BY fecha DESC;""",
            (paciente_id,),
        )
        reportes = cur.fetchall()
        cur.close()
        return reportes
    except psycopg2.Error:
        db.rollback()
        return []


def _crear_pdf(paciente: dict, observaciones: list, reportes_ia: list) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    story = []

    titulo = ParagraphStyle("Titulo", parent=styles["Heading1"], fontSize=20,
                            textColor=colors.HexColor("#1a5490"), alignment=1,
                            spaceAfter=20)
    story.append(Paragraph("Historia Clinica", titulo))
    story.append(Paragraph("TrazaRed HUV", styles["Normal"]))
    story.append(Spacer(1, 0.5*cm))

    story.append(Paragraph("Datos del paciente", styles["Heading2"]))
    datos = [
        ["Nombre:", paciente.get("nombre") or "-"],
        ["Documento:", paciente.get("documento") or "-"],
        ["Genero:", paciente.get("genero") or "-"],
        ["EPS:", paciente.get("eps") or "-"],
        ["Fecha de nacimiento:", str(paciente.get("fecha_nacimiento") or "-")],
        ["Telefono:", paciente.get("telefono") or "-"],
        ["Tipo de sangre:", paciente.get("tipo_sangre") or "-"],
        ["Alergias:", paciente.get("alergias") or "-"],
    ]
    tabla_datos = Table(datos, colWidths=[4*cm, 12*cm])
    tabla_datos.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(tabla_datos)
    story.append(Spacer(1, 0.8*cm))

    story.append(Paragraph("Observaciones", styles["Heading2"]))
    if observaciones:
        filas = [["Tipo", "Valor", "Unidad", "Fecha"]]
        for o in observaciones:
            filas.append([str(o.get("tipo", "")), str(o.get("valor", "")),
                          str(o.get("unidad") or "-"),
                          str(o.get("fecha_observacion") or "")])
        tabla_obs = Table(filas, colWidths=[5*cm, 3*cm, 3*cm, 5*cm])
        tabla_obs.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a5490")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        story.append(tabla_obs)
    else:
        story.append(Paragraph("Sin observaciones registradas.", styles["Normal"]))
    story.append(Spacer(1, 0.8*cm))

    story.append(Paragraph("Reportes de IA aprobados", styles["Heading2"]))
    if reportes_ia:
        for r in reportes_ia:
            story.append(Paragraph(f"<b>Reporte #{r.get('id')}</b> - {r.get('fecha') or ''}",
                                   styles["Normal"]))
            if r.get("probabilidad") is not None:
                story.append(Paragraph(
                    f"Probabilidad: {r.get('probabilidad')} - Resultado: {r.get('resultado') or '-'}",
                    styles["BodyText"]))
            story.append(Spacer(1, 0.3*cm))
    else:
        story.append(Paragraph("Sin reportes de IA aprobados.", styles["Normal"]))

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


@router.get("/pacientes/{paciente_id}/historia.pdf")
def historia_pdf(paciente_id: int, db=Depends(get_db),
                 usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT id, nombre, documento, genero, eps, fecha_nacimiento,
                  telefono, tipo_sangre, alergias
           FROM pacientes WHERE id = %s;""", (paciente_id,))
    paciente = cur.fetchone()
    if paciente is None:
        cur.close()
        raise HTTPException(status_code=404, detail="Paciente no encontrado")

    cur.execute(
        """SELECT o.tipo, o.valor, o.unidad, o.fecha_observacion
           FROM observaciones o
           JOIN remisiones r ON r.id = o.remision_id
           WHERE r.paciente_id = %s
           ORDER BY o.fecha_observacion DESC;""", (paciente_id,))
    observaciones = cur.fetchall()
    cur.close()

    reportes_ia = _obtener_reportes_ia(db, paciente_id)

    registrar_auditoria(db, usuario["id"], "historia_pdf_descargada", "pacientes",
                        paciente_id, f"historia clinica de {paciente['nombre']}")

    pdf_bytes = _crear_pdf(paciente, observaciones, reportes_ia)

    nombre_archivo = f"historia_{paciente['documento']}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nombre_archivo}"'},
    )
