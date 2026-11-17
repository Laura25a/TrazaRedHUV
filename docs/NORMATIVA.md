# Normativa colombiana y alcance de TrazaRed

Revisión técnica: 17/11/2026. Este mapa describe controles comprobables del prototipo académico. No es una certificación jurídica, una habilitación de IPS ni aceptación de RDA o FEV-RIPS por el Ministerio.

## Matriz N01–N05

| Punto | Implementación verificable | Límite vigente |
|---|---|---|
| N01 · Ley 1581/2012 | Roles y asignación clínica en historia, anexos, remisiones, libro, observaciones y consultas legadas de bitácora; respuestas API sin caché; cifrado AES-GCM de identificadores en PostgreSQL; saneamiento previo al LLM; consentimiento por institución/finalidad; Bundle del socio sin nombre, nacimiento ni contactos; aviso público `privacidad.html` | Texto libre puede identificar a terceros; el dictado del navegador puede usar servicios externos. HAPI conserva identidad en la red privada. Falta adopción institucional de la política y canal de derechos del titular antes de usar datos reales. |
| N02 · Res. 1995/1999 y 839/2017 | Bitácora acumulativa; DELETE de gestión devuelve 409, anulación/restauración no elimina contactos; historial de correcciones; auditoría encadenada; recuperación de imágenes y respaldo integral documentado | Falta nombrar custodio institucional, adoptar tabla de retención y ejecutar un simulacro integral de restauración con constancia. No hay eliminación automática de historia clínica. |
| N03 · Ley 2015/2020, Res. 866/2021 y 1888/2025 | Nueva descarga autorizada `GET /pacientes/{id}/historia.fhir.json`, Bundle **document** con Composition **preliminary**, Patient, Device ensamblador, Organization custodio académico, Encounter y Observation; revisiones aprobadas como secciones. `GET /pacientes/{id}/preparacion-rda` muestra mapeo y brechas | El documento es FHIR R4 de alcance académico; **no** declara `meta.profile` nacional ni validación RDA. No tiene todos los datos nacionales, firma clínica, acreditación institucional ni conexión a la plataforma. El contrato **collection** del socio se conserva por compatibilidad y no recibe documentos RDA. |
| N04 · Ley 1273/2009, arts. 269A y 269F | Incidentes de guardia con fecha, actor e IP; auditoría HMAC; acceso administrativo a incidentes | Registrar un incidente técnico no determina por sí mismo la comisión de un delito. La clasificación y actuación jurídica corresponden a los responsables competentes. |
| N05 · Habilitación y FEV-RIPS | Exportación contable identificada como **RIPS de demostración** en UI, nombre de archivo y JSON; `validacion_oficial=false`, norma de referencia y brechas explícitas. No se inventan códigos CUPS, REPS o datos de factura electrónica | No hay generador/validador oficial FEV-RIPS ni CUV. Conservar ruta `/contabilidad/rips.json` no significa soportar el esquema nacional. |

## Historia, conservación y recuperación

La Resolución 839/2017, art. 3, fija un mínimo de 15 años desde la última atención: 5 en archivo de gestión y 10 en archivo central. Prevé plazos especiales, incluido el doble para ciertas víctimas y conservación permanente en los supuestos de lesa humanidad. No debe aplicarse el plazo de las trazas técnicas del LLM a la historia clínica, sus revisiones o la bitácora.

La aplicación conserva los registros clínicos al anular y rechaza la eliminación de contactos. Esto no sustituye una política archivística. La disposición final requiere el procedimiento y condiciones de los arts. 3 y 4; no equivale a ejecutar una tarea periódica que borre registros por edad. Los permisos de los motores y el custodio operativo deben impedir cambios directos fuera de la aplicación.

Procedimiento de copias y recuperación: [RESPALDO.md](RESPALDO.md). Antes del uso asistencial, el responsable debe asignar custodio y suplente, ubicación y protección de copias, frecuencia y objetivos de recuperación, registrar restauración de PostgreSQL/Mongo/HAPI/Orthanc/ML y verificar identificadores cifrados, adjuntos, revisiones, contactos y cadena de auditoría. Sin ese ejercicio no se acredita recuperación integral operativa.

## Diferencias del RDA nacional

La guía consultada publica el perfil de consulta `CompositionAmbulatoryRDA` 1.0.3 STU1. Exige perfiles, cardinalidades, catálogos y datos adicionales: una exportación FHIR base no acredita cumplimiento nacional. El proyecto trabaja con remisiones; no convierte automáticamente un traslado en consulta u hospitalización ni inventa los actos clínicos ausentes. El documento preliminar usa como autor un Device ensamblador; las revisiones originales se muestran como evidencia, sin convertir al usuario que descarga en firmante clínico.

Se necesita seleccionar el escenario de atención real, registrar identificación institucional/profesional y datos territoriales, codificar diagnósticos y procedimientos cuando existan, validar con el paquete y terminologías nacionales y completar la aceptación de la plataforma. El endpoint de preparación muestra estas brechas; **no realiza validación de perfiles**. La descarga local autorizada sí contiene identidad del paciente para su uso clínico; la minimización federada del intercambio con el socio es otro contrato.

## Vigencia y fuentes oficiales

- [Ley 1581 de 2012](https://www1.funcionpublica.gov.co/eva/gestornormativo/norma.php?i=49981): finalidad, acceso restringido, seguridad, datos sensibles e información al titular. El aviso público no reemplaza autorización ni política adoptada por el responsable.
- [Resolución 1995 de 1999](https://www.minsalud.gov.co/Normatividad_Nuevo/RESOLUCI%C3%93N%201995%20DE%201999.pdf) y [Resolución 839 de 2017](https://www.minsalud.gov.co/sites/rid/Lists/BibliotecaDigital/RIDE/DE/DIJ/resolucion-839-de-2017.pdf): historia y gestión documental.
- [Portal oficial IHCE](https://www.minsalud.gov.co/IHCE/Paginas/default.aspx), [Res. 866/2021](https://normograma.supersalud.gov.co/compilacion/docs/resolucion_minsaludps_0866_2021.htm), [Res. 1888/2025](https://www.minsalud.gov.co/Normatividad_Nuevo/Resolucion%20No%201888%20de%202025.pdf) y [guía RDA](https://vulcano.ihcecol.gov.co/StructureDefinition-CompositionAmbulatoryRDA): intercambio nacional. Consultar versión y cronograma aplicable antes de integrar.
- [HL7 FHIR R4 Documents](https://hl7.org/fhir/R4/documents.html): Composition y Bundle document. El contrato de aula collection no es una transacción ni un documento clínico nacional.
- [Ley 1273/2009](https://www.funcionpublica.gov.co/eva/gestornormativo/norma.php?i=34492): delitos informáticos; ver modelo [STRIDE.md](STRIDE.md).
- [Resolución 2080 de 2026](https://www.minsalud.gov.co/sites/rid/Lists/BibliotecaDigital/RIDE/DE/DIJ/resolucion-2080-de-2026.pdf), del 8 de septiembre, revoca la 1732/2026. Su motivación aclara que la 1732 no se publicó en Diario Oficial y no adquirió obligatoriedad. Por eso no se adopta el anuncio de agosto como norma vigente. Mantener como referencia la 3100/2019 y sus modificaciones aplicables, con verificación institucional del régimen vigente.
- [Resolución 0948 de 2026](https://www.minsalud.gov.co/sites/rid/Lists/BibliotecaDigital/RIDE/DE/DIJ/resolucion-0948-de-2026.pdf): marco FEV-RIPS. La estructura simplificada de TrazaRed no acredita validación oficial. Confirmar el anexo, catálogos y reglas vigentes en [SISPRO](https://www.sispro.gov.co/central-financiamiento/Pages/facturacion-electronica.aspx) al implementar el generador oficial.

## Cómo comprobarlo

En Pacientes, abrir un caso asignado → Anexos clínicos → Datos y documentos → **Historia FHIR (preliminar)**. Comprobar Composition primera, referencias resueltas y estado preliminar. El médico ajeno recibe 404, contable 403 y anónimo 401 en la descarga y el diagnóstico de preparación.

En el login, abrir **Tratamiento de datos y alcance académico** sin sesión. En Libro contable, descargar **RIPS de demostración (JSON)** y revisar `uso`, `validacion_oficial` y `brechas`. Probar custodia consultando una bitácora propia, intento de lectura de otro médico, rechazo de DELETE y conservación al anular/restaurar.

Las pruebas usan identidades sintéticas y motores desechables; no se usan cuentas clínicas públicas para esta verificación.

Durante la verificación se reprodujo una carrera de arranque con sesión guardada: `/me` respondía antes de cargar `registro.js` y la vista quedaba vacía (`vistaInicioRegistro is not defined`). El arranque ahora espera `DOMContentLoaded`; la prueba del navegador incluye recarga del libro con sesión vigente y ausencia de errores JavaScript.

## Resultado de comprobación

- Integridad y perfil FHIR: **43 passed, 7 warnings in 133.33s**, salida 0. Comando: `TRAZARED_EVIDENCIA_DOCUMENTOS=/home/nicolas/Escritorio/academico/salud_digital/evidencia-trazared/normativa-2026-11-17 bash /tmp/trazared-reactividad-pytest.sh -q --tb=short --disable-warnings pruebas/test_integridad.py pruebas/test_fhir.py`. El script temporal crea PostgreSQL/Mongo desechables en loopback; el código de las pruebas queda versionado en `pruebas/test_integridad.py`. Las siete advertencias son de dependencias, no fallas de las pruebas.
- Regresiones reproducidas antes de corregir: bitácora ajena y remisión ajena devolvían 200; ahora las pruebas exigen 404. En Chromium se reprodujo el contenido vacío al restaurar sesión y quedó verificada su corrección.
- HAPI `/Bundle/$validate` con documento sintético: HTTP 200, cero errores y dos advertencias por la clase local `remision` (terminología del proyecto, sin equivalente nacional acreditado). No es una validación del paquete RDA.
- Chromium: descarga de PDF/JSON/FHIR desde la ficha sintética, enlace al aviso, operaciones contables, descargas y recarga con sesión vigente. En público se verificaron seis rutas con estilos, aviso en 1440 y 390 px, controles sin autenticación (401, no-store) y HAPI directo cerrado (404).
- Despliegue público: API reconstruida; los ocho módulos Python afectados y los tres archivos públicos de frontend coinciden por SHA-256 con los archivos comprobados. Salud de API, PostgreSQL, Mongo, PACS y FHIR en `ok`.

Las pruebas autenticadas se ejecutaron en el entorno desechable, no con cuentas clínicas de producción. No se ejecutó el tester completo público ni un intercambio con el socio real en esta tarea. Las nueve descargas previamente modificadas permanecen fuera del alcance de estos cambios.
