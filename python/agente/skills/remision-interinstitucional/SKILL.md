---
name: remision-interinstitucional
description: Procedimiento para decidir y proponer la remisión del paciente abierto a otra institución (prioridad, destino con convenio y motivo).
---
# Remisión interinstitucional desde el HUV

1. Llama a `consultar_paciente` para tener edad, EPS, signos vitales y la remisión actual.
2. Llama a `predecir_urgencia`. La prioridad sale de ahí y del criterio clínico:
   - probabilidad ≥ 0,9 → `critica` (remisión inmediata, con monitoreo durante el traslado);
   - entre 0,4 y 0,9 → `urgente`;
   - menor a 0,4 → `programada`, salvo que una guía indique lo contrario.
3. Llama a `buscar_guias` con el hallazgo concreto (por ejemplo "SpO2 menor de 90 remisión UCI" o
   "infarto sin hemodinamia remisión"). Remite solo si una fuente lo respalda.
4. Llama a `buscar_ips_con_convenio` con el servicio que necesita (UCI, hemodinamia, neurología...).
   Prefiere la primera institución con convenio vigente; si ninguna tiene convenio, dilo.
5. Escribe el motivo así: "<hallazgo> + <servicio requerido> + <criterio de la guía [n]>".
6. Llama a `proponer_remision` con la institución, el motivo y la prioridad. El médico decide.
7. Recuerda al médico que el contacto con la institución receptora debe quedar en la bitácora
   (hora, quién respondió, qué dijo), porque esa es la evidencia de trazabilidad.
