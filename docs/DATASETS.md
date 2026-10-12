# Datasets de TrazaRed HUV

Este documento describe los dos datasets reales que usa el sistema (imágenes y datos tabulares), cómo se agruparon los pacientes y cómo se entrenó y evaluó el modelo de IA.

## Imagen

- **Nombre:** NIH ChestX-ray14, del NIH Clinical Center.
- **Fuente:** https://nihcc.app.box.com/v/ChestXray-NIHCC (artículo original: Wang et al., CVPR 2017, "ChestX-ray8").
- **Licencia:** el NIH lo distribuye sin restricciones de uso, y solo pide reconocer al NIH Clinical Center como proveedor de los datos.
- **Número de imágenes cargadas:** 600, una por paciente, guardadas como DICOM en el PACS (Orthanc) y enlazadas con el paciente por su documento. El dataset completo tiene 112,120 radiografías de 30,805 pacientes; nosotros usamos el primer archivo (`images_001`, 4,999 imágenes).
- **Cómo las escogimos:** de esas 4,999 dejamos solo las de adultos (18 a 100 años), con vista PA y de la primera visita de cada paciente del NIH. Quedaron 1,156 candidatas: 359 con algún hallazgo y 797 sin hallazgo. A los pacientes del grupo "fallida" les asignamos una radiografía con hallazgo, a los "efectiva" una sin hallazgo y a los "borderline" cualquiera de las dos. Ninguna imagen se repite.
- **Por qué responde al problema:** el sistema sigue la remisión de pacientes entre instituciones, y para que el médico que recibe al paciente pueda decidir necesita ver sus estudios. La radiografía de tórax es de los estudios más frecuentes en urgencias, y este dataset son imágenes de práctica clínica real, de adultos, con casos normales y con hallazgos.
- **Limitaciones:** las etiquetas del NIH salen de un procesamiento automático de los informes radiológicos, no de una revisión imagen por imagen. Además, la radiografía que ve cada paciente en el sistema es ilustrativa: no es de esa persona, solo está enlazada por identificador. Las imágenes no entran al repositorio por su tamaño; para reproducirlas hay que descargar `images_001` y correr `sembrar_imagenes_reales.py`.

## Tabular

- **Nombre:** Emergency Service – Triage Application (datos de la Escala Coreana de Triage y Agudeza, KTAS).
- **Fuente:** https://www.kaggle.com/datasets/ilkeryildiz/emergency-service-triage-application. Viene del estudio de Moon et al., 2019, PLoS ONE (https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0216972), que publica los datos en su material suplementario.
- **Licencia:** CC BY 4.0 (la del artículo), con atribución a los autores.
- **Registros:** 1,267 adultos atendidos en dos servicios de urgencias de Corea entre octubre de 2016 y septiembre de 2017, con 24 variables.
- **Por qué responde al problema:** la remisión es más trazable si se sabe a quién atender primero. El KTAS es un triage de cinco niveles, y el modelo aprende a distinguir los pacientes urgentes (KTAS 1, 2 o 3) de los que no lo son, usando solo lo que se conoce al llegar el paciente.
- **Limpieza:** los valores `??` y `#BOÞ!` pasan a vacío; las columnas numéricas se convierten a número; si el paciente no tiene dolor, `NRS_pain` vale 0 (los 553 vacíos correspondían a pacientes sin dolor); los modos de llegada 5, 6 y 7 (14 pacientes en total) se juntan en uno.

### Agrupamiento de pacientes (R07)

Agrupamos los 1,267 pacientes reales con K-means, usando el mismo preprocesamiento del modelo (mediana para los vacíos, estandarización y one-hot).

**Cuántos grupos.** Probamos de 2 a 8 grupos. La silueta fue baja en todos los casos (entre 0.10 y 0.14), así que los grupos no están muy separados. El valor más alto fue con 2 grupos (0.143), pero con tan pocos casi no se podía interpretar nada clínicamente. Con 4 grupos la silueta fue 0.122 (similar a la de 3, que dio 0.119), el codo se suaviza ahí, y los grupos resultaron más descriptivos, incluido uno pequeño pero muy particular. Por eso nos quedamos con **k = 4**.

| Grupo | Reales | Edad media | Presión sistólica | Frec. cardíaca | Saturación | Dolor (NRS) | % urgentes |
|---|---|---|---|---|---|---|---|
| 0 | 309 | 56.7 | 160 | 86 | 98 | 2.3 | 58 % |
| 1 | 535 | 49.2 | 120 | 80 | 98 | 4.1 | 47 % |
| 2 | 417 | 59.2 | 120 | 83 | 98 | 0.0 | 71 % |
| 3 | 6 | 73.7 | 130 | 102 | 75 | 1.0 | 100 % |

(Presión, frecuencia y saturación son medianas.)

**Qué representa cada grupo:**
- **Grupo 0, hipertensos con poco dolor.** Pacientes de unos 57 años con la presión sistólica claramente elevada (mediana de 160) y dolor leve. Poco más de la mitad es urgente.
- **Grupo 1, el grupo "general".** Es el más grande y el más joven (49 años), con signos vitales normales y dolor moderado. Menos de la mitad es urgente, así que aquí urgentes y no urgentes se parecen mucho.
- **Grupo 2, mayores sin dolor.** Pacientes de unos 59 años que no refieren dolor y tienen signos vitales normales, pero el 71 % es urgente. Nuestra hipótesis es que son cuadros urgentes sin dolor (por ejemplo, respiratorios o neurológicos), pero no podemos comprobarlo, porque el modelo no usa el motivo de consulta.
- **Grupo 3, hipoxemia severa.** Solo 6 pacientes, mayores (74 años), con saturación mediana de 75 % y frecuencia cardíaca elevada. Todos son urgentes.

**Dónde se confunde el modelo.** Evaluamos el modelo con validación cruzada sobre los 1,267 pacientes, con el umbral de 0.4, y miramos el error por grupo:

| Grupo | Sensibilidad | Especificidad | Urgentes perdidos | Falsas alarmas | Error total |
|---|---|---|---|---|---|
| 0 | 0.866 | 0.485 | 24 | 67 | 29.4 % |
| 1 | 0.810 | 0.710 | 48 | 82 | 24.3 % |
| 2 | 0.973 | 0.165 | 8 | 101 | 26.1 % |
| 3 | 1.000 | sin no urgentes | 0 | 0 | 0 % |

El modelo se equivoca en los tres grupos grandes, pero de formas distintas. Su **zona de confusión principal es el grupo 1**: es donde pierde más urgentes (48, con la sensibilidad más baja, 0.81), y es lo más grave en un triage. Es también el grupo donde casi la mitad de los pacientes es urgente, con signos vitales normales, así que hay poco que los distinga. En el **grupo 2** pasa lo contrario: el modelo marca como urgente a casi todos (especificidad de 0.165), lo que genera muchas falsas alarmas. El grupo 0 es el que tiene el error total más alto (29.4 %).

**Pacientes sintéticos.** Generamos 60 en cada uno de los grupos 0, 1 y 2 y 10 en el grupo 3. Cada sintético sale de remuestrear un paciente real del mismo grupo y sumarle ruido (20 % de la desviación estándar de cada variable), respetando el mínimo y el máximo del grupo. Todos quedan marcados con `origen = sintetico` y con su cluster. El grupo 3 se genera a partir de solo 6 pacientes reales, así que sus 10 sintéticos tienen poca variedad. Los sintéticos sirven para poblar el sistema; **no se usan para entrenar ni para evaluar el modelo**.

## Modelo de IA (R22)

**Qué predice.** La probabilidad de que un paciente sea urgente (KTAS 1, 2 o 3), usando solo lo que se conoce al llegar: sexo, edad, pacientes por hora, modo de llegada, lesión, estado mental, dolor, escala de dolor y signos vitales (presión, frecuencia cardíaca, frecuencia respiratoria, temperatura y saturación). No usa el triage de la enfermera, el diagnóstico, el destino ni el motivo de consulta.

**Cómo se entrenó.** Pipeline de mediana, estandarización (para las variables numéricas), one-hot (para las categóricas) y regresión logística. Elegimos regresión logística porque es fácil de explicar a un médico y da probabilidades que se pueden usar para fijar umbrales. Separamos 80 % para entrenar (1,013 pacientes) y 20 % para probar (254), con `stratify` y `random_state=42`. Entrenamos y evaluamos solo con pacientes reales. En la validación cruzada de 5 particiones el AUC fue de 0.809 (entre 0.787 y 0.820).

**Resultados en la prueba (254 pacientes: 147 urgentes y 107 no urgentes).** AUC de 0.748. Con el umbral de decisión de 0.4:

| Sensibilidad | Especificidad | Precisión | F1 |
|---|---|---|---|
| 0.871 | 0.486 | 0.699 | 0.776 |

Matriz de confusión: 52 verdaderos negativos, 55 falsos positivos, 19 falsos negativos y 128 verdaderos positivos.

**Umbral de decisión: 0.4.** En un triage es peor dejar pasar a un urgente que dar una alarma de más. Comparamos tres umbrales:

| Umbral | Urgentes perdidos | Falsas alarmas | Sensibilidad | Especificidad | F1 |
|---|---|---|---|---|---|
| 0.5 | 34 | 44 | 0.769 | 0.589 | 0.743 |
| **0.4** | **19** | **55** | **0.871** | **0.486** | **0.776** |
| 0.3 | 8 | 79 | 0.946 | 0.262 | 0.762 |

Pasar de 0.5 a 0.4 nos ahorra 15 urgentes perdidos a cambio de 11 falsas alarmas más, y el F1 mejora. Bajar a 0.3 casi no deja de marcar a nadie como urgente (especificidad de 26 %), y el modelo deja de servir para priorizar.

**Umbral de caso crítico: 0.9.** Es el que dispara la alerta que interrumpe al médico (R20), así que tiene que ser rara para que le hagan caso. Probamos 0.8 y 0.9 en la prueba:

| Umbral | Pacientes marcados | Urgentes entre los marcados | No urgentes marcados | KTAS 1 o 2 entre los marcados |
|---|---|---|---|---|
| 0.8 | 62 de 254 | 83.9 % | 10 | 33.9 % |
| **0.9** | **26 de 254** | **88.5 %** | **3** | **50.0 %** |

Con 0.9 la alerta salta en 1 de cada 10 pacientes y casi todos son urgentes, aunque solo captura 13 de los 52 pacientes KTAS 1 o 2 de la prueba. Esos casos no se pierden: igual quedan marcados como urgentes por el umbral de 0.4. Elegimos 0.9 porque preferimos pocas alertas, bien fundadas, que una alerta que salte tanto que se ignore.

**Sesgo por subgrupo (umbral 0.4, conjunto de prueba):**

| Subgrupo | Pacientes | Sensibilidad | Especificidad | Urgentes perdidos |
|---|---|---|---|---|
| Sexo, código 1 | 119 | 0.897 | 0.490 | 7 |
| Sexo, código 2 | 135 | 0.848 | 0.482 | 12 |
| Menores de 65 años | 155 | 0.802 | 0.595 | 16 |
| Mayores de 65 años | 99 | 0.955 | 0.242 | 3 |
| Hospital 1 | 144 | 0.859 | 0.561 | 11 |
| Hospital 2 | 110 | 0.884 | 0.366 | 8 |

- **Edad:** es la diferencia que más importa. De los 19 urgentes que el modelo pierde, **16 son menores de 65 años**. En los mayores casi no pierde urgentes, pero marca como urgente a casi todos (especificidad de 0.242). Es probable que el modelo se apoye mucho en la edad como señal de urgencia, y por eso un paciente joven con un cuadro grave pasa más fácil desapercibido.
- **Sexo y hospital:** las diferencias son pequeñas y con tan pocos pacientes por subgrupo no se pueden distinguir de la casualidad. En el hospital 2 hay más falsas alarmas (especificidad de 0.366).
- No usamos umbrales distintos por grupo: con 254 pacientes de prueba habría un riesgo alto de sobreajustar. Queda como mejora futura.

**Limitaciones:**
- El modelo no usa el motivo de consulta, que es una de las variables más informativas en un triage real.
- La saturación de oxígeno está vacía en 697 de 1,267 registros (55 %) y se imputa con la mediana, así que para esos pacientes el modelo prácticamente no la usa.
- Los datos vienen de solo dos servicios de urgencias de Corea, lo que dificulta generalizar a un hospital colombiano.
- La prueba tiene 254 pacientes, por lo que las cifras cambian un poco con otra partición. El AUC de prueba (0.748) es menor que el de la validación cruzada (0.809).
- El modelo aprendió "urgente" (KTAS 1 a 3), no "crítico". Una probabilidad alta indica mucha confianza en que el paciente es urgente, pero no garantiza que sea de los más graves.
- No se comparó con otros modelos: usamos regresión logística por su interpretabilidad.

**Cómo se usa en el sistema.** La API es la única que habla con el servicio de IA. Antes de llamarlo, una capa quita nombre, documento, correo, teléfono y dirección: al modelo solo le llegan las variables clínicas y un identificador seudónimo, derivado del paciente con una clave secreta (HMAC) y que no se puede deshacer. El payload exacto que se envió queda guardado y se consulta en `GET /ia/reportes/{id}/payload`. Cada análisis crea un reporte en estado `pendiente`, que un médico o especialista aprueba o rechaza; solo los aprobados llegan a la historia clínica, y los rechazados se conservan como evidencia. Un admin puede ver la versión y las métricas del modelo (`GET /modelo`) y reentrenarlo (`POST /entrenar`).