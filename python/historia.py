"""Historia trasladable: la misma lectura autorizada alimenta JSON y PDF."""
import io
from datetime import datetime

import cripto
import exportes
import libro
import pacs
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Response
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle

from auth import get_db, registrar_auditoria, ROLES_CLINICOS, requiere_rol
from validaciones import exigir_asignacion_clinica

router = APIRouter(tags=["Historia clínica"])


def obtener_historia(db, paciente_id: int, usuario: dict) -> dict:
    """No exporta borradores ni datos técnicos enviados al modelo.

    Los estudios son un inventario de instancias activas, sin inferir hallazgos
    radiológicos ni incrustar archivos DICOM. Un fallo del PACS queda explícito.
    """
    exigir_asignacion_clinica(db, paciente_id, usuario)
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT id, nombre, documento, genero, eps, fecha_nacimiento,
                              telefono, tipo_sangre, alergias
                       FROM pacientes WHERE id = %s;""", (paciente_id,))
        paciente = cripto.descifrar_paciente(cur.fetchone())
        if paciente is None:
            raise HTTPException(404, "Paciente no encontrado")
        cur.execute("""SELECT o.id, o.tipo, o.codigo_loinc, o.valor, o.unidad,
                              o.fecha_observacion AT TIME ZONE current_setting('TimeZone') AS fecha_observacion,
                              o.socio_id, o.profesional_fhir, o.remision_id
                       FROM observaciones o LEFT JOIN remisiones r ON r.id = o.remision_id
                       WHERE COALESCE(o.paciente_id, r.paciente_id) = %s
                       ORDER BY o.fecha_observacion DESC, o.id DESC;""", (paciente_id,))
        observaciones = cur.fetchall()
        cur.execute("""SELECT r.id, r.estado, r.remision_id, r.modelo,
                              r.creado_en AT TIME ZONE current_setting('TimeZone') AS fecha,
                              r.prediccion AS resultado, r.probabilidad_urgente AS probabilidad,
                              r.observacion_revision, r.revisado_por,
                              r.revisado_en AT TIME ZONE current_setting('TimeZone') AS revisado_en,
                              u.nombre AS revisor_nombre, u.rol AS revisor_rol
                       FROM reportes_ia r LEFT JOIN usuarios u ON u.id = r.revisado_por
                       WHERE r.paciente_id = %s AND r.estado = 'aprobado'
                       ORDER BY r.creado_en DESC, r.id DESC;""", (paciente_id,))
        reportes = cur.fetchall()
        cur.execute("""SELECT r.id, r.estado, r.respuesta, r.fuentes, r.revision_corpus,
                              r.creado_en, r.creado_por, r.revisado_en, r.revisado_por, r.motivo,
                              u.nombre AS revisor_nombre, u.rol AS revisor_rol
                       FROM revisiones_rag r LEFT JOIN usuarios u ON u.id = r.revisado_por
                       WHERE r.paciente_id = %s AND r.estado = 'aprobado'
                       ORDER BY r.id DESC;""", (paciente_id,))
        rag = cur.fetchall()
        cur.execute("""SELECT r.id, r.medico_id, m.nombre AS medico_nombre,
                              r.especialista_id, e.nombre AS especialista_nombre,
                              r.motivo, r.observaciones, r.estado, r.created_at, r.aceptada_en
                       FROM remisiones_medicas r JOIN usuarios m ON m.id = r.medico_id
                       JOIN usuarios e ON e.id = r.especialista_id
                       WHERE r.paciente_id = %s ORDER BY r.created_at DESC, r.id DESC;""", (paciente_id,))
        interconsultas = cur.fetchall()
        cur.execute("""SELECT id, institucion_origen, institucion_destino, estado, activo,
                              radicada_en AT TIME ZONE current_setting('TimeZone') AS radicada_en,
                              cerrada_en AT TIME ZONE current_setting('TimeZone') AS cerrada_en
                       FROM remisiones WHERE paciente_id = %s ORDER BY id DESC;""", (paciente_id,))
        remisiones = cur.fetchall()
        for r in remisiones:
            r["radicado"] = libro.radicado(r["id"], r["radicada_en"])
        cur.execute("SELECT instance_id FROM imagenes_eliminadas WHERE paciente_id = %s AND vigente;", (paciente_id,))
        ocultas = {r["instance_id"] for r in cur.fetchall()}
    disponible, advertencias = True, []
    try:
        estudios = [i for i in pacs.list_images(paciente["documento"]) if i["instance_id"] not in ocultas]
    except HTTPException as error:
        if error.status_code not in (502, 503, 504):
            raise
        disponible, estudios = False, []
        advertencias.append("No fue posible consultar el PACS. El inventario de estudios está incompleto.")
    return {"paciente": paciente, "observaciones": observaciones, "reportes_ia": reportes,
            "resultados_rag": rag, "interconsultas": interconsultas, "remisiones": remisiones,
            "estudios": estudios, "estudios_disponibles": disponible, "advertencias": advertencias,
            "generado_en": datetime.now(exportes.ZONA),
            "alcance_estudios": "Inventario de imágenes activas; los archivos DICOM se consultan en el visor autorizado."}


def _crear_pdf(historia: dict) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=1.7*cm, rightMargin=1.7*cm,
                            topMargin=1.5*cm, bottomMargin=1.6*cm, title="Historia clínica trasladable")
    story = []
    def par(texto, estilo=exportes.E_CELDA):
        story.append(exportes._p(texto, estilo))
    def seccion(texto):
        story.extend([Spacer(1, .4*cm), exportes._p(texto, exportes.E_CELDA_B), Spacer(1, .15*cm)])
    def tabla(columnas, filas, proporciones):
        ancho = A4[0] - doc.leftMargin - doc.rightMargin
        datos = [[exportes._p(c, exportes.E_CELDA_B) for c in columnas]]
        datos += [[exportes._p(v) for v in fila] for fila in filas]
        t = Table(datos, colWidths=[ancho*p/sum(proporciones) for p in proporciones], repeatRows=1)
        t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e7f1ef')),
                              ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                              ('LINEBELOW', (0, 0), (-1, -1), .4, exportes.LINEA),
                              ('BOTTOMPADDING', (0, 0), (-1, -1), 5)]))
        story.append(t)
    par("TrazaRed HUV", exportes.E_SUB)
    par("Historia clínica trasladable", exportes.E_TITULO)
    par(f"Generada: {exportes.texto(historia['generado_en'])}", exportes.E_SUB)
    seccion("Datos del paciente")
    p = historia["paciente"]
    tabla(["Dato", "Valor"], [[titulo, p.get(campo) or "Sin registro"] for campo, titulo in (
        ("nombre", "Nombre"), ("documento", "Documento"), ("genero", "Sexo registrado"),
        ("eps", "EPS"), ("fecha_nacimiento", "Nacimiento"), ("telefono", "Teléfono"),
        ("tipo_sangre", "Tipo de sangre"), ("alergias", "Alergias"))], [1, 3])
    seccion("Episodios y traslado")
    for r in historia["remisiones"]:
        par(f"{r['radicado']} · {r['estado']} · {'Vigente' if r['activo'] else 'Anulado'}", exportes.E_CELDA_B)
        par(f"{r['institucion_origen']} → {r['institucion_destino']}")
        par(f"Radicada: {exportes.texto(r['radicada_en'])} · Cierre: {exportes.texto(r['cerrada_en']) or 'Sin cierre'}")
    if not historia["remisiones"]:
        par("Sin episodios registrados.")
    seccion("Observaciones clínicas")
    if historia["observaciones"]:
        tabla(["Observación / código", "Valor", "Unidad", "Fecha / episodio"], [
            [f"{o['tipo']} / {o.get('codigo_loinc') or 'Sin código'}", o['valor'], exportes.unidad(o['unidad']),
             f"{exportes.texto(o['fecha_observacion'])} / {o['remision_id'] or 'Sin episodio'}"]
            for o in historia["observaciones"]], [3, 1, 1, 2])
    else:
        par("Sin observaciones registradas.")
    seccion("Interconsultas")
    for r in historia["interconsultas"]:
        par(f"Interconsulta #{r['id']} · {r['estado']}", exportes.E_CELDA_B)
        par(f"Remitente: {r['medico_nombre']} (#{r['medico_id']}) · Especialista: {r['especialista_nombre']} (#{r['especialista_id']})")
        par(f"Solicitada: {exportes.texto(r['created_at'])} · Aceptada: {exportes.texto(r['aceptada_en']) or 'Sin registro'}")
        par(f"Motivo: {r['motivo']}")
        par(f"Observaciones: {r['observaciones'] or 'Sin registro'}")
        story.append(Spacer(1, .2*cm))
    if not historia["interconsultas"]:
        par("Sin interconsultas registradas.")
    seccion("Estudios disponibles en PACS")
    par(historia["alcance_estudios"])
    for aviso in historia["advertencias"]:
        par(aviso, exportes.E_CELDA_B)
    for e in historia["estudios"]:
        par(f"{e.get('descripcion') or 'Estudio'} · {e.get('modalidad') or 'Sin modalidad'} · Fecha DICOM: {e.get('fecha') or 'Sin registro'}")
        par(f"Instancia: {e['instance_id']}", exportes.E_SUB)
    if historia["estudios_disponibles"] and not historia["estudios"]:
        par("Sin estudios activos registrados.")
    seccion("Reportes de IA aprobados y revisión clínica")
    for r in historia["reportes_ia"]:
        par(f"Reporte #{r['id']} · Episodio: {r['remision_id'] or 'Sin vínculo'} · {exportes.texto(r['fecha'])}", exportes.E_CELDA_B)
        par(f"Resultado: {r['resultado']} · Probabilidad: {r['probabilidad']}")
        par(f"Revisor: {r['revisor_nombre'] or 'Sin registro'} (#{r['revisado_por'] or '-'}, {r['revisor_rol'] or '-'}) · {exportes.texto(r['revisado_en']) or 'Fecha no registrada'}")
        par(f"Nota de revisión: {r['observacion_revision'] or 'No registrada en el reporte histórico'}")
    if not historia["reportes_ia"]:
        par("Sin reportes de IA aprobados.")
    seccion("Consultas RAG aprobadas y fuentes")
    for r in historia["resultados_rag"]:
        par(f"Consulta #{r['id']} · Revisor: {r['revisor_nombre'] or '-'} (#{r['revisado_por']}) · {exportes.texto(r['revisado_en'])}", exportes.E_CELDA_B)
        par(r['respuesta'])
        par(f"Nota de revisión: {r['motivo'] or 'Sin nota adicional'}")
        for f in r['fuentes']:
            par(f"[{f['n']}] {f['texto']} - {f['fuente']} ({f['chunk_id']})")
    if not historia["resultados_rag"]:
        par("Sin consultas RAG aprobadas.")
    doc.build(story, onFirstPage=exportes._pie, onLaterPages=exportes._pie)
    return buffer.getvalue()


@router.get("/pacientes/{paciente_id}/historia")
def historia_json(paciente_id: int, response: Response, db=Depends(get_db),
                  usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    historia = obtener_historia(db, paciente_id, usuario)
    registrar_auditoria(db, usuario["id"], "historia_consultada", "pacientes", paciente_id, "historia clínica (JSON)")
    response.headers["Cache-Control"] = "no-store"
    return historia


@router.get("/pacientes/{paciente_id}/historia.pdf")
def historia_pdf(paciente_id: int, db=Depends(get_db),
                 usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    historia = obtener_historia(db, paciente_id, usuario)
    contenido = _crear_pdf(historia)
    registrar_auditoria(db, usuario["id"], "historia_pdf_descargada", "pacientes", paciente_id, "historia clínica (PDF)")
    return exportes.archivo(contenido, f"historia_{paciente_id}.pdf", "pdf")


@router.get('/pacientes/{paciente_id}/preparacion-rda')
def preparacion_rda(paciente_id: int, db=Depends(get_db),
                    usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    import documento_clinico
    exigir_asignacion_clinica(db, paciente_id, usuario)
    with db.cursor() as cur:
        cur.execute('SELECT id FROM pacientes WHERE id = %s', (paciente_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, 'Paciente no encontrado')
    return documento_clinico.preparacion_rda()


@router.get('/pacientes/{paciente_id}/historia.fhir.json')
def historia_fhir(paciente_id: int, db=Depends(get_db),
                   usuario=Depends(requiere_rol(*ROLES_CLINICOS))):
    import json
    import documento_clinico
    contenido = documento_clinico.documento(obtener_historia(db, paciente_id, usuario))
    registrar_auditoria(db, usuario['id'], 'historia_fhir_descargada', 'pacientes', paciente_id,
                       f"documento preliminar={contenido['id']}; sin validación nacional RDA")
    return Response(content=json.dumps(contenido, ensure_ascii=False), media_type='application/fhir+json',
                    headers={'Cache-Control': 'no-store',
                             'Content-Disposition': f'attachment; filename="historia_{paciente_id}_fhir.json"'})
