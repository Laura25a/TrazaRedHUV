// ==========================================================================
// TrazaRed HUV — Asistente clínico (semanas 12 y 13)
// ==========================================================================
// Dos modos en una sola pantalla:
//   · Agente     POST /agente/chat, /agente/decidir, GET /agente/historial
//                El agente usa herramientas del sistema (datos del paciente,
//                modelo de urgencia, guías, instituciones con convenio) y
//                PROPONE remisiones: la tarjeta de aprobación decide.
//   · Guías      POST /rag/preguntar · respuesta con citas, sin acciones.
// El paciente se fija en esta pantalla (no en el chat): el agente nunca puede
// pedir otro paciente. Se registra en SECCIONES como los demás módulos.
"use strict";

const AST = { modo: "agente", paciente: null, conversacion: null, ocupado: false };
const AST_HERRAMIENTAS = {
  consultar_paciente: "Consultó los datos clínicos del paciente",
  predecir_urgencia: "Estimó la urgencia con el modelo XGBoost",
  buscar_guias: "Buscó en las guías clínicas (RAG)",
  buscar_ips_con_convenio: "Buscó instituciones con convenio con la EPS",
  proponer_remision: "Propuso una remisión",
  cargar_skill: "Cargó un procedimiento (skill)",
  guardar_preferencia: "Guardó una preferencia",
};

function astConversacionNueva() {
  AST.conversacion = "c-" + Date.now().toString(36);
  try { sessionStorage.setItem("ast-conv", AST.conversacion); } catch { /* sin storage */ }
}

async function vistaAsistente(params = {}) {
  if (params.paciente) AST.paciente = +params.paciente;
  try { AST.conversacion = AST.conversacion || sessionStorage.getItem("ast-conv"); } catch { /* sin storage */ }
  if (!AST.conversacion) astConversacionNueva();

  $("#contenido").innerHTML = `<div class="pagina">
    <div class="pagina-cabeza"><div><h2>Asistente clínico</h2>
      <p>Pregunta por el paciente abierto: el asistente revisa sus datos, el modelo de urgencia, las guías y las
      instituciones con convenio, y <strong>propone</strong>. Tú decides.</p></div>
      <button class="btn btn-secundario" id="ast-nueva">Nueva conversación</button></div>
    <section class="tarjeta ast">
      <div class="pestanas" id="ast-modo">
        <button data-modo="agente" class="${AST.modo === "agente" ? "activa" : ""}">Agente (con acciones)</button>
        <button data-modo="rag" class="${AST.modo === "rag" ? "activa" : ""}">Consulta a guías (RAG)</button>
      </div>
      <div class="ast-barra">
        <label>Paciente abierto
          <span class="ast-paciente">
            <input type="number" id="ast-pid" min="1" placeholder="número" value="${AST.paciente || ""}">
            <button class="btn btn-suave btn-sm" id="ast-abrir">Abrir</button>
            <button class="btn btn-suave btn-sm" id="ast-cerrar" ${AST.paciente ? "" : "disabled"}>Sin paciente</button>
          </span></label>
        <span class="tenue" id="ast-pinfo">${AST.paciente ? "Cargando paciente…" : "Sin paciente: solo preguntas generales."}</span>
        <span class="tenue ast-modelo" id="ast-modelo"></span>
      </div>
      <div class="ast-chat" id="ast-chat" aria-live="polite"></div>
      <form class="ast-form" id="ast-form">
        <textarea id="ast-texto" rows="2" maxlength="1000" placeholder="Ej.: ¿Qué tan grave está este paciente?  ·  Remite este paciente a UCI  ·  ¿Qué dice la guía de qSOFA?"></textarea>
        <button class="btn btn-primario" id="ast-enviar" type="submit">Enviar</button>
      </form>
      <div class="ast-sugerencias" id="ast-sug">
        ${["¿Qué tan grave está este paciente?", "Remite este paciente a UCI", "¿Qué instituciones tienen convenio con su EPS?",
           "¿Cuáles son los criterios de qSOFA?", "Recuerda que prefiero respuestas breves"].map(s => `<button type="button" class="btn btn-suave btn-sm">${esc(s)}</button>`).join("")}
      </div>
      <p class="tenue ast-pie">Apoyo a la decisión clínica. El asistente nunca ve nombre, documento ni teléfono: solo el seudónimo del paciente.</p>
    </section></div>`;

  $$("#ast-modo button").forEach(b => b.addEventListener("click", () => {
    AST.modo = b.dataset.modo;
    $$("#ast-modo button").forEach(x => x.classList.toggle("activa", x === b));
    $("#ast-sug").classList.toggle("oculto", false);
  }));
  $("#ast-nueva").addEventListener("click", () => { astConversacionNueva(); $("#ast-chat").innerHTML = ""; astBienvenida(); });
  $("#ast-abrir").addEventListener("click", () => astAbrirPaciente(+$("#ast-pid").value || null));
  $("#ast-cerrar").addEventListener("click", () => astAbrirPaciente(null));
  $$("#ast-sug button").forEach(b => b.addEventListener("click", () => { $("#ast-texto").value = b.textContent; $("#ast-texto").focus(); }));
  $("#ast-texto").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#ast-form").requestSubmit(); } });
  $("#ast-form").addEventListener("submit", (e) => { e.preventDefault(); astEnviar(); });

  API.get("/agente/estado").then(e => {
    $("#ast-modelo").textContent = `Modelo: ${e.proveedor === "simulado" ? "simulado (sin llave de Groq)" : e.modelo} · memoria: ${e.memoria.split(" (")[0]}`;
  }).catch(() => {});
  if (AST.paciente) astAbrirPaciente(AST.paciente, true);
  await astCargarHistorial();
}

async function astAbrirPaciente(pid, silencioso = false) {
  AST.paciente = pid;
  $("#ast-pid").value = pid || "";
  $("#ast-cerrar").disabled = !pid;
  if (!pid) { $("#ast-pinfo").textContent = "Sin paciente: solo preguntas generales."; return; }
  try {
    const p = await API.get(`/pacientes/${pid}`);
    // En pantalla el médico sí ve el nombre (tiene permiso); al asistente solo le llega el seudónimo.
    $("#ast-pinfo").innerHTML = `<strong>${esc(p.nombre)}</strong> · ${p.fecha_nacimiento ? edad(p.fecha_nacimiento) + " años · " : ""}${esc(p.eps || "")}`;
    if (!silencioso) astMensaje("sistema", `Paciente #${pid} abierto. Las preguntas sobre "este paciente" se refieren a él.`);
  } catch (e) {
    AST.paciente = null;
    $("#ast-pinfo").textContent = e.status === 404 ? "Ese paciente no existe." : e.message;
  }
}

function astBienvenida() {
  astMensaje("asistente", "Hola. Abre un paciente arriba y pregúntame por él: puedo revisar sus signos, estimar su urgencia, buscar en las guías, sugerir instituciones con convenio y proponerte una remisión para que tú la apruebes.");
}

async function astCargarHistorial() {
  try {
    const h = await API.get(`/agente/historial?conversacion_id=${encodeURIComponent(AST.conversacion)}`);
    if (!h.mensajes.length && !h.pendiente) { astBienvenida(); return; }
    h.mensajes.forEach(m => astMensaje(m.rol, m.texto));
    if (h.pendiente) astTarjetaAprobacion([{ herramienta: h.pendiente.herramienta, args: h.pendiente.args }]);
  } catch (e) {
    astBienvenida();
    if (e.status === 403) astMensaje("sistema", "Tu rol no puede usar el asistente (solo médico y especialista).");
  }
}

function astFormato(texto) {
  // Texto seguro: se escapa todo y solo se resaltan las citas [n] y los saltos de línea
  return esc(texto).replace(/\[(\d+)\]/g, '<span class="ast-cita">[$1]</span>').replace(/\n/g, "<br>");
}

function astMensaje(rol, texto, extra = "") {
  const div = document.createElement("div");
  div.className = `ast-msg ast-${rol}`;
  div.innerHTML = `<div class="ast-burbuja">${astFormato(texto)}${extra}</div>`;
  $("#ast-chat").appendChild(div);
  div.scrollIntoView({ block: "nearest", behavior: "smooth" });
  return div;
}

function astPasos(pasos) {
  const llamadas = pasos.filter(p => p.tipo === "llamada");
  if (!llamadas.length) return "";
  const resultados = pasos.filter(p => p.tipo === "resultado");
  return `<details class="ast-pasos"><summary>Lo que hizo el asistente (${llamadas.length} ${llamadas.length === 1 ? "paso" : "pasos"})</summary><ol>
    ${llamadas.map(l => {
      const r = resultados.find(x => x.herramienta === l.herramienta);
      return `<li><strong>${esc(AST_HERRAMIENTAS[l.herramienta] || l.herramienta)}</strong> <code>${esc(l.herramienta)}</code>
        ${r ? `<div class="ast-resultado">${astFormato(r.texto)}</div>` : ""}</li>`;
    }).join("")}</ol></details>`;
}

function astFuentes(fuentes) {
  if (!fuentes || !fuentes.length) return "";
  return `<details class="ast-pasos" open><summary>Fuentes</summary><ol>${fuentes.map(f =>
    `<li><span class="ast-cita">[${f.n}]</span> ${esc(f.fuente)} <code>${esc(f.chunk_id)}</code></li>`).join("")}</ol></details>`;
}

function astTarjetaAprobacion(acciones) {
  const a = acciones[0];
  const args = a.args || {};
  const div = document.createElement("div");
  div.className = "ast-msg ast-asistente";
  div.innerHTML = `<div class="ast-aprobar">
    <h4>${icono("alerta")} El asistente propone una acción: necesita tu decisión</h4>
    ${a.herramienta === "proponer_remision" ? `<dl>
      <dt>Remitir a</dt><dd>${esc(args.institucion_destino || "—")}</dd>
      <dt>Prioridad</dt><dd><span class="pildora ${args.prioridad === "critica" ? "est-pendiente" : "est-activo"}">${esc(args.prioridad || "—")}</span></dd>
      <dt>Motivo</dt><dd>${esc(args.motivo || "—")}</dd></dl>`
      : `<pre>${esc(JSON.stringify(args, null, 2))}</pre>`}
    <p class="tenue">Si apruebas, la remisión se radica en el libro en estado pendiente y queda en la auditoría con tu usuario.
      Si rechazas, no se crea nada y la propuesta queda guardada como evidencia.</p>
    <textarea class="ast-motivo" rows="2" maxlength="300" placeholder="Motivo (obligatorio si rechazas)"></textarea>
    <div class="ast-botones">
      <button class="btn btn-ok" data-d="approve">Aprobar</button>
      <button class="btn btn-peligro" data-d="reject">Rechazar</button>
    </div></div>`;
  $("#ast-chat").appendChild(div);
  div.scrollIntoView({ block: "nearest", behavior: "smooth" });
  $$("button[data-d]", div).forEach(b => b.addEventListener("click", async () => {
    const motivo = $(".ast-motivo", div).value.trim();
    if (b.dataset.d === "reject" && !motivo) { toast("Escribe el motivo del rechazo.", "error"); $(".ast-motivo", div).focus(); return; }
    $$("button", div).forEach(x => x.disabled = true);
    const espera = astMensaje("asistente", "…");
    try {
      const r = await API.post("/agente/decidir", { conversacion_id: AST.conversacion, decision: b.dataset.d, motivo: motivo || null });
      $(".ast-motivo", div).remove();
      $(".ast-botones", div).innerHTML = `<span class="pildora ${b.dataset.d === "approve" ? "est-activo" : "est-pendiente"}">${b.dataset.d === "approve" ? "Aprobada por ti" : "Rechazada por ti"}</span>`;
      espera.remove();
      astPintarRespuesta(r);
      if (b.dataset.d === "approve") { toast("Remisión radicada en el libro.", "ok"); remisionesCache = null; }
    } catch (e) {
      espera.remove();
      $$("button", div).forEach(x => x.disabled = false);
      astMensaje("sistema", e.message);
    }
  }));
}

function astPintarRespuesta(r) {
  if (r.estado === "pendiente_aprobacion") {
    if (r.pasos && r.pasos.length) astMensaje("asistente", "Revisé el caso con las herramientas del sistema.", astPasos(r.pasos));
    else if (r.detalle) astMensaje("sistema", r.detalle);
    astTarjetaAprobacion(r.acciones);
  } else if (r.estado === "respondido") {
    astMensaje("asistente", r.respuesta || "(sin texto)", astPasos(r.pasos || []));
  } else if (r.estado === "tiempo_agotado") {
    astMensaje("sistema", r.detalle || "El asistente tardó demasiado. Intenta de nuevo.");
  } else if (r.estado === "sin_pendientes") {
    astMensaje("sistema", "No hay ninguna acción esperando decisión en esta conversación.");
  } else {
    astMensaje("sistema", JSON.stringify(r));
  }
}

async function astEnviar() {
  const texto = $("#ast-texto").value.trim();
  if (texto.length < 2 || AST.ocupado) return;
  AST.ocupado = true;
  $("#ast-enviar").disabled = true;
  $("#ast-texto").value = "";
  astMensaje("usuario", texto);
  const espera = astMensaje("asistente", AST.modo === "agente" ? "Revisando con las herramientas del sistema…" : "Buscando en las guías…");
  espera.classList.add("ast-esperando");
  try {
    if (AST.modo === "agente") {
      const r = await API.post("/agente/chat", { mensaje: texto, conversacion_id: AST.conversacion, paciente_id: AST.paciente });
      espera.remove();
      astPintarRespuesta(r);
    } else {
      const r = await API.post("/rag/preguntar", { pregunta: texto, paciente_id: AST.paciente });
      espera.remove();
      const modo = { llm: "redactada con el modelo de lenguaje", extractivo: "extractiva (frases de las guías, sin LLM)", abstencion: "sin evidencia" }[r.modo] || r.modo;
      astMensaje("asistente", r.respuesta, `<p class="tenue ast-modo-rag">Respuesta ${esc(modo)}</p>${astFuentes(r.fuentes)}`);
    }
  } catch (e) {
    espera.remove();
    astMensaje("sistema", e.status === 403 ? "Tu rol no puede usar el asistente." : e.message);
  } finally {
    AST.ocupado = false;
    $("#ast-enviar").disabled = false;
  }
}

if (typeof SECCIONES !== "undefined" && typeof ICONOS !== "undefined") {
  ICONOS.asistente = '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/><path d="M8.5 11h.01M12 11h.01M15.5 11h.01"/>';
  SECCIONES.asistente = {
    titulo: "Asistente clínico", icono: "asistente",
    roles: ["medico", "especialista"],
    pintar: vistaAsistente, grupo: "Inteligencia artificial",
  };
}
