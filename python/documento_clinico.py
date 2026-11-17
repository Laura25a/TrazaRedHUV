"""Documento clínico académico FHIR R4; no declara conformidad nacional RDA.

El contrato collection del socio permanece separado. No se inventan códigos
REPS, CUPS, CIE-10, identidad profesional nacional ni actos clínicos faltantes.
"""
from html import escape
import uuid

import fhir_sync

GUIA_RDA = 'https://vulcano.ihcecol.gov.co/'
PERFIL_RDA = 'https://fhir.minsalud.gov.co/rda/StructureDefinition/CompositionAmbulatoryRDA'
BRECHAS_RDA = [
    'Confirmar el tipo real de atención (consulta, urgencias u hospitalización); la remisión no lo acredita.',
    'Registrar identificación y habilitación institucional REPS y datos territoriales requeridos.',
    'Registrar identificación profesional nacional y autoría clínica; exportar no equivale a firmar un RDA.',
    'Completar catálogos nacionales, diagnósticos CIE-10, procedimientos CUPS y demás datos obligatorios del tipo de RDA.',
    'Validar los recursos contra la versión acordada del paquete nacional y sus terminologías.',
    'Completar credenciales, pruebas y aceptación de la plataforma nacional; el socio de aula no la sustituye.',
]


def preparacion_rda():
    """Brechas explícitas: no es un validador del perfil nacional."""
    return {
        'apto_envio_nacional': False, 'validacion_perfil_nacional': 'no_realizada',
        'guia': GUIA_RDA, 'perfil_consulta_referencia': PERFIL_RDA,
        'version_guia_consultada': '1.0.3 STU1 (consulta); confirmar antes de integrar',
        'mapeo': {'ficha': 'Patient', 'episodio_remision': 'Encounter',
                  'signos_codificados': 'Observation (LOINC/UCUM)',
                  'resumen_aprobado': 'Composition.section', 'custodio': 'Organization'},
        'documento_disponible': 'Bundle document FHIR R4 académico, Composition preliminary',
        'brechas': list(BRECHAS_RDA),
    }


def documento(historia):
    fecha = historia['generado_en'].isoformat()
    identificador = str(uuid.uuid4())
    entradas = []

    def agregar(recurso, clave):
        recurso['id'] = clave
        referencia = 'urn:uuid:' + str(uuid.uuid5(uuid.NAMESPACE_URL, f'trazared:{identificador}:{clave}'))
        entradas.append({'fullUrl': referencia, 'resource': recurso})
        return {'reference': referencia}

    def narrativa(lineas):
        return {'status': 'generated', 'div': '<div xmlns="http://www.w3.org/1999/xhtml">' +
                ''.join('<p>' + escape(str(linea)) + '</p>' for linea in lineas) + '</div>'}

    composicion = {'resourceType': 'Composition', 'status': 'preliminary',
                   'text': narrativa(['Resumen académico preliminar de registros clínicos aprobados. No acredita validación nacional RDA.']),
                   'type': {'text': 'Resumen académico de historia clínica para traslado'},
                   'date': fecha, 'title': 'Resumen clínico TrazaRed (preliminar)',
                   'confidentiality': 'R', 'section': []}
    agregar(composicion, 'composicion')  # primera entrada obligatoria en Bundle document
    p = historia['paciente']
    paciente = fhir_sync.paciente_a_fhir(p)
    paciente['text'] = narrativa([p['nombre'], f"Documento: {p['documento']}"])
    sujeto = agregar(paciente, f"paciente-{p['id']}")
    custodio = agregar({'resourceType': 'Organization', 'name': 'TrazaRed HUV · proyecto académico',
                        'text': narrativa(['Custodio del documento: proyecto académico TrazaRed HUV.'])}, 'custodio')
    autor = agregar({'resourceType': 'Device', 'deviceName': [
        {'name': 'TrazaRed · ensamblador de exportes clínicos', 'type': 'user-friendly-name'}],
        'text': narrativa(['Software ensamblador de registros existentes. No es un firmante clínico.'])}, 'ensamblador')
    composicion.update(subject=sujeto, custodian=custodio, author=[autor])
    secciones = composicion['section']
    secciones.append({'title': 'Alcance y pendientes', 'text': narrativa([
        'Documento académico preliminar; no es un RDA nacional validado ni acredita firma clínica.',
        'Solo contiene análisis con revisión aprobada. El exportador ensambla registros existentes.',
        *historia['advertencias'], *BRECHAS_RDA])})
    episodios = {}
    for r in historia['remisiones']:
        estado = {'pendiente': 'planned', 'en_gestion': 'arrived', 'aceptada': 'in-progress',
                  'rechazada': 'cancelled', 'completada': 'finished'}[r['estado']] if r['activo'] else 'entered-in-error'
        period = {'start': r['radicada_en'].isoformat()}
        if r['cerrada_en']:
            period['end'] = r['cerrada_en'].isoformat()
        episodios[r['id']] = agregar({'resourceType': 'Encounter', 'status': estado,
            'class': fhir_sync.CLASE_REMISION.copy(), 'subject': sujeto, 'period': period,
            'identifier': [{'system': 'urn:trazared:radicado', 'value': r['radicado']}],
            'text': narrativa([r['radicado'], f"{r['institucion_origen']} → {r['institucion_destino']}"])}, f"episodio-{r['id']}")
    if episodios:
        secciones.append({'title': 'Episodios de remisión', 'entry': list(episodios.values())})
    observaciones = []
    for o in historia['observaciones']:
        # No convertir texto libre en cantidades numéricas ni inventar terminología.
        recurso = {'resourceType': 'Observation', 'status': 'final', 'subject': sujeto,
                   'text': narrativa([f"{o['tipo']}: {o['valor']} {o['unidad'] or ''}"]),
                   'code': {'text': o['tipo']}, 'effectiveDateTime': o['fecha_observacion'].isoformat(),
                   'valueString': f"{o['valor']} {o['unidad'] or ''}".strip()}
        if o['remision_id'] in episodios:
            recurso['encounter'] = episodios[o['remision_id']]
        if o.get('codigo_loinc'):
            recurso['code']['coding'] = [{'system': 'http://loinc.org', 'code': o['codigo_loinc']}]
            mapeada = fhir_sync.observacion_a_fhir(o, '', 'temporal')
            recurso.pop('valueString')
            recurso['valueQuantity'] = mapeada['valueQuantity']
        observaciones.append(agregar(recurso, f"observacion-{o['id']}"))
    if observaciones:
        secciones.append({'title': 'Observaciones registradas', 'entry': observaciones})
    for titulo, lineas in (
        ('Interconsultas', [f"#{r['id']} · {r['estado']} · {r['medico_nombre']} → {r['especialista_nombre']} · {r['motivo']} · {r['observaciones'] or ''}" for r in historia['interconsultas']]),
        ('Análisis aprobados y revisión', [f"Reporte #{r['id']}: {r['resultado']}. Revisor: {r['revisor_nombre'] or 'Sin registro'}; fecha: {r['revisado_en'] or 'Sin registro'}; nota: {r['observacion_revision'] or 'No registrada en el reporte histórico'}" for r in historia['reportes_ia']]),
        ('RAG aprobado y revisión', [f"Consulta #{r['id']}: {r['respuesta']}. Revisor: {r['revisor_nombre'] or 'Sin registro'}; fecha: {r['revisado_en']}; nota: {r['motivo'] or ''}. Fuentes: {r['fuentes']}" for r in historia['resultados_rag']]),
        ('Estudios', [historia['alcance_estudios'], *[f"{e.get('descripcion') or 'Estudio'} · {e.get('modalidad') or ''} · {e.get('fecha') or 'Sin fecha'}" for e in historia['estudios']]]),
    ):
        secciones.append({'title': titulo, 'text': narrativa(lineas or ['Sin registros disponibles.'])})
    return {'resourceType': 'Bundle', 'type': 'document', 'id': identificador,
            'identifier': {'system': 'urn:trazared:documento-clinico', 'value': identificador},
            'timestamp': fecha, 'entry': entradas}
