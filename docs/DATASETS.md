# Datasets de TrazaRed HUV

Este documento describe los dos datasets reales que usa el sistema (imágenes y datos tabulares), cómo se agruparon los pacientes y cómo se eligió, entrenó y evaluó el modelo de IA. Todas las cifras salen del cuaderno `semana9_final.ipynb`.

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
- **Limpieza:**
  - Los valores `??` y `#BOÞ!` pasan a vacío y las columnas numéricas se convierten a número.
  - Si el paciente no tiene dolor, `NRS_pain` vale 0 (los 553 vacíos correspondían a pacientes sin dolor; quedan 3 vacíos reales).
  - Una saturación de 20 % (1 paciente) se trata como error de registro y pasa a vacío. Los demás valores extremos (por ejemplo, presión sistólica de 275 o temperatura de 41) se conservan porque son fisiológicamente posibles y clínicamente relevantes.
  - Los modos de llegada 5, 6 y 7 (14 pacientes en total) se juntan en uno (7, «otro»).
  - Los vacíos que quedan se imputan con la mediana dentro del `Pipeline`, después de separar entrenamiento y prueba, para no filtrar información.

### Agrupamiento de pacientes (R07)

Agrupamos los 1,267 pacientes reales usando el mismo preprocesamiento del modelo (mediana para los vacíos, estandarización y one-hot).

**Cuántos grupos.** Probamos K-means de 2 a 8 grupos:

| k | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|
| Silueta | 0.141 | 0.116 | 0.122 | 0.123 | 0.111 | 0.099 | 0.097 |
| Davies-Bouldin | 2.509 | 2.284 | 2.004 | 1.898 | 2.026 | 2.034 | 2.126 |
| Estabilidad (ARI con remuestreo) | 0.940 | 0.808 | 0.700 | 0.688 | 0.642 | 0.660 | 0.599 |

Ningún criterio señala un k óptimo: la silueta es máxima en k = 2, Davies-Bouldin en k = 5 y el BIC de GMM deja de mejorar de forma marcada a partir de k = 3. Todas las siluetas son bajas (menores de 0.15), así que los pacientes forman una nube continua más que grupos bien separados, y el agrupamiento es una **segmentación descriptiva**, no una prueba de grupos naturales.

Elegimos **k = 4** por estabilidad e interpretación clínica. Conserva los tres grupos grandes de k = 3 (ARI de 0.947 al compararlos sin el grupo nuevo) y separa un cuarto grupo de 32 pacientes con un perfil clínico definido. El costo es una estabilidad menor (0.700 frente a 0.808 de k = 3).

**Comparación de algoritmos (k = 4).**

| Algoritmo | Tamaños de los grupos | Silueta | Davies-Bouldin | ¿Asigna pacientes nuevos? |
|---|---|---|---|---|
| **K-means** | 526, 404, 305, 32 | 0.122 | 2.004 | Sí |
| Jerárquico (Ward) | 657, 532, 73, 5 | 0.136 | 1.869 | No |
| GMM (covarianza diagonal) | 499, 361, 340, 67 | 0.047 | 4.597 | Sí |
| DBSCAN | 1 solo grupo (ruido entre 1.7 % y 12.3 % según eps) | — | — | No |

Solo K-means y GMM cumplen R07 (al menos 2 grupos y poder asignar pacientes nuevos). K-means queda empatado en silueta con el mejor y es el más sencillo de explicar, así que es el elegido. La silueta y Davies-Bouldin favorecen a K-means por construcción, por eso orientan pero no deciden solas.

**Los cuatro grupos.** Los números 0 a 3 dependen de la inicialización de K-means; por eso el sistema nombra cada grupo por su **perfil** y no por su número. Los valores de presión, frecuencia cardíaca y saturación son medianas.

| Grupo | Perfil | Reales | Edad media | Presión sistólica | Frec. cardíaca | Saturación | Dolor (NRS) | % urgentes | % KTAS 1-2 |
|---|---|---|---|---|---|---|---|---|---|
| 0 | Hipoxemia severa | 32 | 68.3 | 110 | 100 | 90 | 0.8 | 100 % | 65.6 % |
| 1 | Presión arterial alta | 305 | 57.1 | 161 | 86 | 98 | 2.4 | 58.4 % | 19.7 % |
| 2 | Sin dolor | 404 | 58.6 | 121 | 82 | 98 | 0.0 | 69.1 % | 23.8 % |
| 3 | General | 526 | 48.9 | 120 | 80 | 98 | 4.1 | 46.4 % | 13.1 % |

- **Hipoxemia severa con frecuencia respiratoria alta (32 pacientes).** Mayores (mediana de 72.5 años) con la saturación muy por debajo del promedio (3.1 desviaciones estándar), frecuencia respiratoria y cardíaca altas y poco dolor. Todos son urgentes y todos tienen la saturación registrada, así que no es un efecto de la imputación. Es un grupo pequeño: sus conclusiones no se pueden generalizar.
- **Presión arterial alta (305).** Adultos de unos 57 años con la presión sistólica y diastólica claramente elevadas (alrededor de 1.2 desviaciones por encima del promedio) y dolor leve. Poco más de la mitad es urgente.
- **Sin dolor (404).** Pacientes de unos 59 años que no refieren dolor y tienen signos vitales normales, pero el 69 % es urgente. Nuestra hipótesis es que son cuadros urgentes sin dolor (por ejemplo, respiratorios o neurológicos), pero no podemos comprobarlo, porque el modelo no usa el motivo de consulta.
- **General (526).** Es el grupo más grande y el más joven (49 años), con signos vitales normales y dolor por encima del promedio. Menos de la mitad es urgente, así que aquí urgentes y no urgentes se parecen mucho.

**Dónde se confunde el modelo.** Evaluamos el modelo de urgencia (XGBoost, umbral 0.4) con validación cruzada de 5 particiones sobre los 1,267 pacientes. Globalmente tiene sensibilidad de 0.881, especificidad de 0.549 y error de 25.9 %.

| Grupo | Sensibilidad | Especificidad | Urgentes perdidos (esperados) | Falsas alarmas (esperadas) | Error total |
|---|---|---|---|---|---|
| Hipoxemia severa | 1.000 | sin no urgentes | 0 (3.8) | 0 | 0.0 % |
| Presión arterial alta | 0.865 | 0.488 | 24 (21.1) | 65 (57.3) | 29.2 % |
| Sin dolor | 0.939 | 0.224 | 17 (33.1) | 97 (56.4) | 28.2 % |
| General | 0.811 | 0.720 | 46 (29.0) | 79 (127.3) | 23.8 % |

Un grupo se declara **zona de confusión** solo si el exceso de errores no se explica por azar. Para eso se compara, con una prueba binomial, el número de errores del grupo con el esperado según el desempeño global (nivel 0.05 dividido entre 8 comparaciones, es decir, 0.00625):

- **General:** zona de confusión por **urgentes perdidos**. Pierde 46 urgentes cuando se esperaban 29 (p = 0.001). Es lo más grave en un triage, y es el grupo donde urgentes y no urgentes se parecen más.
- **Sin dolor:** zona de confusión por **falsas alarmas**. Genera 97 cuando se esperaban 56 (p < 0.0001): el modelo marca como urgente a casi todos (especificidad de 0.224).
- **Presión arterial alta** e **hipoxemia severa:** sin evidencia de exceso de errores (p = 0.28 y p = 0.10 en el grupo de presión alta).

Una regla más simple (sensibilidad menor que la global, o error mayor que el global) marcaría a tres de los cuatro grupos, incluido el de presión alta. La usamos solo como referencia, porque con grupos pequeños marca diferencias que pueden ser azar.

**Pacientes sintéticos.** Generamos 60 en cada uno de los grupos de hipoxemia, presión alta y sin dolor, y 10 en el grupo general. Cada sintético sale de remuestrear un paciente real del mismo grupo y sumarle ruido (20 % de la desviación estándar de cada variable), respetando el mínimo y el máximo del grupo. Todos quedan marcados con `origen = sintetico` y con su cluster. Controles de calidad:

- El mismo K-means reconoce al **96.8 %** de los sintéticos en su grupo de origen (90 % en hipoxemia y 100 % en los demás). Es una prueba optimista, porque son copias de reales con poco ruido.
- Ningún valor sintético queda fuera del rango observado en los reales (se cumple por construcción).
- Las medias se parecen a las reales. La excepción es la edad en el grupo de hipoxemia (73.5 años en sintéticos frente a 68.3 en reales): con 60 sintéticos remuestreados esa diferencia aparece en cerca del 1 % de las generaciones, y se comprobó que el generador no tiene sesgo (el promedio de 500 generaciones es 68.4).

Los sintéticos sirven para poblar el sistema; **no se usan para entrenar ni para evaluar el modelo**.

## Modelo de IA (R22)

**Qué predice.** La probabilidad de que un paciente sea urgente (KTAS 1, 2 o 3), usando solo lo que se conoce al llegar: sexo, edad, pacientes por hora, modo de llegada, lesión, estado mental, dolor, escala de dolor y signos vitales (presión, frecuencia cardíaca, frecuencia respiratoria, temperatura y saturación). No usa el triage de la enfermera, el diagnóstico, el destino ni el motivo de consulta. El hospital (`Group`) se reserva para el análisis de sesgo y no entra al modelo.

**Cómo se eligió el modelo.** Se compararon 11 algoritmos con las mismas reglas, fijadas antes de ver los resultados:

1. Todos reciben el mismo preprocesamiento (mediana, estandarización y one-hot) dentro de un `Pipeline`.
2. Todos usan las mismas 5 particiones de validación cruzada, calculadas sobre el conjunto de entrenamiento.
3. Se elige por AUC en validación cruzada, nunca por el rendimiento en entrenamiento (que premia memorizar). La prueba no se toca en esta etapa.
4. Si varios modelos quedan dentro de una desviación estándar del mejor, se consideran empatados y gana el más explicable.

| Modelo | AUC en validación cruzada | AUC en entrenamiento | Brecha |
|---|---|---|---|
| **XGBoost** | **0.833 ± 0.007** | 0.928 | 0.095 |
| CatBoost | 0.829 ± 0.012 | 0.971 | 0.142 |
| Gradient Boosting | 0.821 ± 0.012 | 0.948 | 0.127 |
| LightGBM | 0.810 ± 0.015 | 0.995 | 0.185 |
| Regresión logística | 0.809 ± 0.011 | 0.827 | 0.018 |
| SVM lineal | 0.808 ± 0.015 | 0.820 | 0.012 |
| MLP sin regularizar | 0.804 ± 0.019 | 0.936 | 0.132 |
| SVM rbf | 0.800 ± 0.009 | 0.887 | 0.088 |
| MLP regularizado | 0.797 ± 0.013 | 0.835 | 0.038 |
| KNN | 0.753 ± 0.012 | 0.816 | 0.063 |
| Árbol de decisión (profundidad 3) | 0.729 ± 0.025 | 0.767 | 0.038 |

- **Ganó XGBoost.** Tuvo el mejor AUC (0.833) y la menor variación entre particiones (0.007). Quedó empatado solo con CatBoost (el corte era 0.825), y entre los dos la regla de explicabilidad escoge XGBoost.
- **Los modelos lineales quedan cerca** (regresión logística 0.809, SVM lineal 0.808) y casi no memorizan (brecha de 0.02 o menos), pero están a unos 2.4 puntos de AUC del mejor.
- **La brecha delata la memorización:** LightGBM llega a 0.995 en entrenamiento y solo 0.810 en validación. Por eso la elección se hace únicamente con validación cruzada.
- Configuración final: XGBoost con 200 árboles, profundidad 3, tasa de aprendizaje 0.05 y `random_state=42`.

**Cómo se entrenó y evaluó.** Se separó 80 % para entrenar (1,013 pacientes) y 20 % para probar (254), con `stratify` y `random_state=42`. Se entrena y evalúa solo con pacientes reales. En validación cruzada de 5 particiones el AUC fue 0.833 (entre 0.819 y 0.840).

**Resultados en la prueba (254 pacientes: 147 urgentes y 107 no urgentes).** AUC de **0.765**. Con el umbral de decisión de 0.4:

| Sensibilidad | Especificidad | Precisión | F1 |
|---|---|---|---|
| 0.884 | 0.514 | 0.714 | 0.790 |

Matriz de confusión: 55 verdaderos negativos, 52 falsos positivos, 17 falsos negativos y 130 verdaderos positivos.

El AUC de prueba (0.765) es menor que el de todas las particiones de validación cruzada (0.819 a 0.840). Se lo atribuimos a dos cosas: las particiones comparten gran parte de los datos de entrenamiento (su desviación subestima la incertidumbre real) y la prueba es pequeña (su AUC tiene un margen de error de unos ±0.03). Por eso tomamos el **0.765 como la estimación más conservadora** del desempeño esperado.

**Comparación con un modelo lineal.** Una regresión logística de referencia alcanza un AUC de prueba de 0.748, frente a 0.765 de XGBoost. La mejora es modesta (unos 0.017), lo que indica que gran parte de la señal es aproximadamente lineal. Mantenemos XGBoost por su leve ventaja y porque captura interacciones, pero no afirmamos que sea mucho mejor.

**Umbral de decisión: 0.4.** En un triage es peor dejar pasar a un urgente que dar una alarma de más. Para no elegirlo mirando la prueba, comparamos tres umbrales con predicciones fuera de muestra sobre el entrenamiento (1,013 pacientes, 586 urgentes):

| Umbral | Sensibilidad | Especificidad | Urgentes perdidos | Falsas alarmas | F1 |
|---|---|---|---|---|---|
| 0.5 | 0.829 | 0.663 | 100 | 144 | 0.799 |
| **0.4** | **0.881** | **0.571** | **70** | **183** | **0.803** |
| 0.3 | 0.930 | 0.457 | 41 | 232 | 0.800 |

El F1 casi no cambia entre 0.3 y 0.5, así que la decisión no depende de una métrica global sino del costo clínico. De 0.5 a 0.4 se recuperan 30 urgentes a cambio de 39 falsas alarmas más; de 0.4 a 0.3 se recuperan 29 a cambio de 49 más, y casi la mitad de los no urgentes quedaría marcada como urgente. Con 0.4 se detecta a casi 9 de cada 10 urgentes. En la prueba, con 0.4, el modelo detecta a 46 de los 52 pacientes KTAS 1 o 2 (88.5 %).

**Umbral de caso crítico: 0.9.** Es el que dispara la alerta que interrumpe al médico (R20), así que tiene que ser rara para que le hagan caso. Se eligió con predicciones fuera de muestra (con 0.8 se marcan 292 pacientes y el 37.0 % es KTAS 1 o 2; con 0.9 se marcan 118 y el 50.8 %), y se verificó en la prueba sin ajustarlo:

| Umbral | Pacientes marcados | Urgentes entre los marcados | No urgentes marcados | KTAS 1 o 2 entre los marcados | % de los KTAS 1 o 2 que captura |
|---|---|---|---|---|---|
| 0.8 | 67 de 254 (26.4 %) | 82.1 % | 12 | 37.3 % | 48.1 % |
| **0.9** | **31 de 254 (12.2 %)** | **90.3 %** | **3** | **51.6 %** | **30.8 %** |

Con 0.9 la alerta salta en 1 de cada 8 pacientes y es más del doble de selectiva que el azar (la proporción base de KTAS 1 o 2 es 20.5 %). Con 0.8 se disparan 36 alertas más para encontrar unos 9 casos graves adicionales. Los graves que no alcanzan 0.9 no se pierden: siguen marcados como urgentes por el umbral de 0.4. Con solo 31 alertas, los porcentajes son aproximados.

**Sesgo por subgrupo (umbral 0.4, conjunto de prueba):**

| Subgrupo | Pacientes | Urgentes | Sensibilidad | Especificidad | Urgentes perdidos |
|---|---|---|---|---|---|
| Sexo, código 1 | 119 | 68 | 0.912 | 0.490 | 6 |
| Sexo, código 2 | 135 | 79 | 0.861 | 0.536 | 11 |
| Menores de 65 años | 155 | 81 | 0.815 | 0.581 | 15 |
| Mayores de 65 años | 99 | 66 | 0.970 | 0.364 | 2 |
| Hospital 1 | 144 | 78 | 0.897 | 0.576 | 8 |
| Hospital 2 | 110 | 69 | 0.870 | 0.415 | 9 |

- **Edad:** es la diferencia que más importa. De los 17 urgentes que el modelo pierde, **15 son menores de 65 años** (88 %). La sensibilidad es 0.815 en menores (IC 95 % aproximado 0.72 a 0.89) y 0.970 en mayores (0.90 a 0.99); los intervalos no se traslapan, así que la diferencia probablemente es real. En mayores casi no pierde urgentes, pero marca como urgente a muchos que no lo son (especificidad de 0.364). Un paciente joven con un cuadro grave pasa más fácil desapercibido. La edad no es la variable dominante del modelo (ocupa el puesto 8 de 24 por importancia), así que este patrón puede venir de varias variables a la vez.
- **Sexo y hospital:** las diferencias no son concluyentes con tan pocos pacientes por subgrupo. Conviene vigilar el hospital: la especificidad es menor en el 2 (0.415 frente a 0.576), y en el hospital 1 la saturación no se registra nunca, lo que podría explicar parte de la diferencia.
- No usamos umbrales distintos por grupo: con 254 pacientes de prueba habría un riesgo alto de sobreajustar. Queda como mejora futura.

**Qué variables pesan más.** Con la importancia del modelo, las cinco de mayor peso son la presencia de lesión (`Injury`), la escala de dolor (`NRS_pain`) y tres categorías del modo de llegada. La importancia indica cuánto contribuye cada variable, pero no hacia dónde empuja ni implica causalidad, y las variables correlacionadas (por ejemplo, presión sistólica y diastólica) se reparten el peso.

**Limitaciones:**
- El modelo no usa el motivo de consulta, que es una de las variables más informativas en un triage real. Agruparlo en categorías (dolor torácico, abdominal, cefalea...) sería la mejora con más potencial. No se usa el texto libre porque, con 1,267 pacientes, daría sobreajuste, y porque en el sistema real un campo libre no se puede garantizar libre de datos personales (R12).
- La saturación de oxígeno está vacía en 698 de 1,267 registros (55 %) y se imputa con la mediana. El hospital 1 no la registra en ninguno de sus pacientes, así que para esos casos el modelo prácticamente no la usa.
- Los datos vienen de solo dos servicios de urgencias de Corea, lo que dificulta generalizar a un hospital colombiano.
- La prueba tiene 254 pacientes, por lo que las cifras cambian un poco con otra partición. El AUC de prueba (0.765) es menor que el de validación cruzada (0.833).
- XGBoost es menos fácil de explicar que la regresión logística, y su ventaja sobre ella es modesta (0.765 frente a 0.748 en prueba).
- El modelo aprendió "urgente" (KTAS 1 a 3), no "crítico". Una probabilidad alta indica mucha confianza en que el paciente es urgente, pero no garantiza que sea de los más graves.
- Con k = 4 los grupos son poco separados (silueta de 0.122) y uno de ellos es pequeño (32 pacientes): los grupos orientan, no deciden.

**Cómo se usa en el sistema.** La API es la única que habla con el servicio de IA. Antes de llamarlo, una capa quita nombre, documento, correo, teléfono y dirección: al modelo solo le llegan las variables clínicas y un identificador seudónimo, derivado del paciente con una clave secreta (HMAC) y que no se puede deshacer. El payload exacto que se envió queda guardado y se consulta en `GET /ia/reportes/{id}/payload`. Cada análisis crea un reporte en estado `pendiente`, que un médico o especialista aprueba o rechaza; solo los aprobados llegan a la historia clínica, y los rechazados se conservan como evidencia. Un admin puede ver la versión y las métricas del modelo (`GET /modelo`) y reentrenarlo (`POST /entrenar`). El modelo se guarda con la versión 1.1 desde el cuaderno; al reentrenarlo desde el servicio la numeración pasa a 2.x.