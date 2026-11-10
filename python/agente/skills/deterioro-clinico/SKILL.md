---
name: deterioro-clinico
description: Cómo valorar si el paciente abierto se está deteriorando (signos de alarma, qSOFA, NEWS2, modelo de urgencia) y qué reportar al clínico.
---
# Valoración de deterioro clínico

1. Obtén los signos vitales con `consultar_paciente`.
2. Revisa signos de alarma con `buscar_guias`:
   - SpO2 < 90 % o FR > 24 → "deterioro respiratorio NEWS2";
   - FR ≥ 22, sistólica ≤ 100 o alteración de conciencia (Glasgow < 15) → "qSOFA sepsis";
   - sistólica < 90 → "shock hipotensión".
3. Llama a `predecir_urgencia` y reporta la probabilidad con su nivel (no urgente / urgente / crítico).
4. Reporta en este orden: conclusión (estable, en riesgo o crítico), signos que la justifican, criterio
   de la guía con cita [n] y resultado del modelo.
5. Si el modelo y las guías no coinciden, dilo explícitamente: prevalece el criterio del clínico.
6. No propongas medicamentos ni dosis. Si el paciente está crítico, sugiere valorar la remisión
   (skill `remision-interinstitucional`).
