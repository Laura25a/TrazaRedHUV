// ==========================================================================
// TrazaRed HUV — Predicción de urgencia con el modelo (XGBoost)
// ==========================================================================
// Formulario con las variables clínicas que usa el modelo.
//   · Sin número de paciente  -> POST /ia/predecir : solo calcula, no guarda nada.
//   · Con número de paciente  -> POST /ia/analizar : crea el reporte PENDIENTE de aprobación
//                                 clínica (R10) y luego se lee con GET /ia/reportes/{id}.
// Los umbrales (urgente / crítico) se leen de GET /modelo para no escribirlos a mano.
"use strict";

const PRED_NUM = [
  { id: "edad", etq: "Edad (años)", min: 0, max: 120, step: 1, ej: "65" },
  { id: "pacientes_por_hora", etq: "Pacientes por hora en urgencias", min: 0, max: 200, step: 1, ej: "8" },
  { id: "sbp", etq: "Presión sistólica (mmHg)", min: 40, max: 300, step: 1, ej: "120" },
  { id: "dbp", etq: "Presión diastólica (mmHg)", min: 20, max: 200, step: 1, ej: "80" },
  { id: "fc", etq: "Frecuencia cardíaca (lpm)", min: 20, max: 250, step: 1, ej: "85" },
  { id: "fr", etq: "Frecuencia respiratoria (rpm)", min: 4, max: 80, step: 1, ej: "20" },
  { id: "temperatura", etq: "Temperatura (°C)", min: 30, max: 45, step: 0.1, ej: "36.6" },
  { id: "saturacion", etq: "Saturación de O₂ (%)", min: 0, max: 100, step: 1, ej: "98" },
];

const PRED_SELECT = [
  { id: "sexo", etq: "Sexo", opciones: [["", "Sin dato"], ["1", "Mujer"], ["2", "Hombre"]], vacio: true },
  { id: "modo_llegada", etq: "Modo de llegada", def: "3",
    opciones: [["1", "Caminando"], ["2", "Ambulancia pública"], ["3", "Vehículo particular"], ["4", "Ambulancia privada"], ["7", "Otro"]] },
  { id: "lesion", etq: "¿Es una lesión?", def: "1", opciones: [["1", "No"], ["2", "Sí"]] },
  { id: "mental", etq: "Estado mental", def: "1",
    opciones: [["1", "Alerta"], ["2", "Responde a la voz"], ["3", "Responde al dolor"], ["4", "Sin respuesta"]] },
  { id: "dolor", etq: "¿Tiene dolor?", def: "0", opciones: [["0", "No"], ["1", "Sí"]] },
];

async function vistaPrediccion() {
  const selects = PRED_SELECT.map(s => `<label>${esc(s.etq)}
      <select id="pred-${s.id}">${s.opciones.map(([v, t]) =>
        `<option value="${v}"${v === (s.def ?? "") ? " selected" : ""}>${esc(t)}</option>`).join("")}</select></label>`).join("");
  const numeros = PRED_NUM.map(c => `<label>${esc(c.etq)}
      <input type="number" id="pred-${c.id}" min="${c.min}" max="${c.max}" step="${c.step}" placeholder="p. ej. ${c.ej}"></label>`).join("");

  $("#contenido").innerHTML = `<div class="pagina">
    <div class="pagina-cabeza"><div><h2>Predicción de urgencia</h2>
      <p>Ingresa los datos del paciente y el modelo calcula la probabilidad de que sea urgente (KTAS 1 a 3).</p></div></div>
    <section class="tarjeta"><div class="agr-panel">
      <div class="agr-nota">${icono("info")}<p>
        <strong>¿Cómo funciona?</strong> Los campos vacíos se completan con la mediana de los pacientes de entrenamiento.
        Sin número de paciente solo se calcula (no se guarda nada). Con número de paciente se crea un reporte
        <em>pendiente</em> que un médico debe aprobar o rechazar. El resultado orienta, no decide.</p></div>
      <form id="pred-form" class="pred-form" novalidate>
        <div class="pred-grid">${selects}${numeros}
          <label>Dolor (escala 0–10)<input type="number" id="pred-nrs_dolor" min="0" max="10" step="0.5" placeholder="solo si tiene dolor"></label>
          <label>Número de paciente (opcional)<input type="number" id="pred-paciente" min="1" step="1" placeholder="para guardar el reporte"></label>
        </div>
        <div class="pred-acciones"><button type="submit" class="btn btn-primario" id="pred-enviar">Predecir</button>
          <button type="button" class="btn btn-secundario" id="pred-limpiar">Limpiar</button></div>
      </form>
      <div id="pred-resultado" aria-live="polite"></div>
    </div></section></div>`;

  let umbrales = { umbral: null, umbral_critico: null };
  try { const m = await API.get("/modelo"); umbrales = { umbral: m.umbral, umbral_critico: m.umbral_critico }; } catch { /* se muestra sin umbrales */ }

  const dolor = $("#pred-dolor"), nrs = $("#pred-nrs_dolor");
  const sincronizarDolor = () => { nrs.disabled = dolor.value === "0"; if (nrs.disabled) nrs.value = ""; };
  dolor.addEventListener("change", sincronizarDolor); sincronizarDolor();

  $("#pred-limpiar").addEventListener("click", () => {
    $("#pred-form").reset(); sincronizarDolor(); $("#pred-resultado").innerHTML = "";
  });
  $("#pred-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!$("#pred-form").reportValidity()) return;
    const num = (id) => { const v = $("#pred-" + id).value; return v === "" ? null : Number(v); };
    const datos = {
      sexo: num("sexo"), edad: num("edad"), pacientes_por_hora: num("pacientes_por_hora"),
      modo_llegada: num("modo_llegada"), lesion: num("lesion"), mental: num("mental"), dolor: num("dolor"),
      nrs_dolor: num("nrs_dolor"), sbp: num("sbp"), dbp: num("dbp"), fc: num("fc"), fr: num("fr"),
      temperatura: num("temperatura"), saturacion: num("saturacion"),
    };
    if (datos.sbp != null && datos.dbp != null && datos.dbp >= datos.sbp) {
      $("#pred-resultado").innerHTML = `<div class="vacio">La presión diastólica debe ser menor que la sistólica.</div>`;
      return;
    }
    const pid = num("paciente");
    const boton = $("#pred-enviar");
    boton.disabled = true;
    $("#pred-resultado").innerHTML = cargando("Calculando…");
    try {
      let r, guardado = null;
      if (pid) {
        const creado = await API.post("/ia/analizar", { paciente_id: pid, ...datos });
        r = await API.get(`/ia/reportes/${creado.id}`);
        guardado = creado.id;
      } else {
        r = await API.post("/ia/predecir", datos);
      }
      $("#pred-resultado").innerHTML = predResultado(r, umbrales, guardado, pid);
    } catch (e) {
      $("#pred-resultado").innerHTML = `<div class="vacio">${esc(
        e.status === 403 ? "Tu rol no puede realizar esta acción." : e.message)}</div>`;
    } finally {
      boton.disabled = false;
    }
  });
}

function predResultado(r, umbrales, guardado, pid) {
  const prob = Number(r.probabilidad);
  const critico = !!r.critico;
  const urgente = r.urgente ?? (critico || r.nivel === "urgente");
  const [clase, titulo, texto] = critico
    ? ["est-pendiente", "Caso crítico", "La probabilidad supera el umbral crítico: conviene priorizar la valoración clínica de inmediato."]
    : urgente
      ? ["est-pendiente", "Urgente", "El modelo clasifica al paciente como urgente (KTAS 1 a 3): conviene valoración prioritaria."]
      : ["est-activo", "No urgente", "El modelo no lo clasifica como urgente. Si algo en la valoración clínica no concuerda, prevalece el criterio del médico."];
  const u = umbrales.umbral != null ? `Umbral urgente: ${agrNum(umbrales.umbral, 2)}` : "";
  const c = umbrales.umbral_critico != null ? ` · umbral crítico: ${agrNum(umbrales.umbral_critico, 2)}` : "";
  return `<div class="pred-resultado">
      <div class="pred-prob"><span class="pred-valor">${agrNum(prob * 100, 1)} %</span>
        <span class="pildora ${clase}">${icono(clase === "est-activo" ? "check" : "alerta")}${titulo}</span></div>
      <div class="pred-barra" role="img" aria-label="Probabilidad de urgente ${agrNum(prob * 100, 1)} por ciento"><i style="width:${Math.min(prob * 100, 100)}%"></i></div>
      <p class="tenue">Probabilidad de que el paciente sea urgente. ${esc(u + c)}${r.version_modelo ? ` · modelo ${esc(String(r.version_modelo))}` : ""}</p>
      <p>${esc(texto)}</p>
      ${guardado ? `<div class="agr-nota">${icono("info")}<p>Se guardó el reporte <strong>#${guardado}</strong> del paciente ${pid}, en estado
        <strong>pendiente</strong> de aprobación clínica.</p></div>` : ""}
      <p class="tenue">Apoyo a la decisión clínica, no un diagnóstico.</p></div>`;
}

if (typeof SECCIONES !== "undefined" && typeof ICONOS !== "undefined") {
  ICONOS.prediccion = '<path d="M3 12h4l3-8 4 16 3-8h4"/>';
  SECCIONES.prediccion = {
    titulo: "Predicción de urgencia", icono: "prediccion",
    roles: ["admin", "medico", "especialista"],
    pintar: vistaPrediccion, grupo: "Inteligencia artificial",
  };
}
