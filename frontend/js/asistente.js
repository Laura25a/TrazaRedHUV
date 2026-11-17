// ==========================================================================
// TrazaRed HUV — Asistente clínico (semanas 12 y 13)
// ==========================================================================
// Dos modos en una sola pantalla:
//   · Agente     POST /agente/chat, /agente/decidir, GET /agente/historial
//                El agente usa herramientas del sistema (datos del paciente,
//                modelo de urgencia, guías, instituciones con convenio) y
//                PROPONE remisiones: la tarjeta de aprobación decide.
//   · Guías      POST /rag/preguntar · respuesta con citas y revisión clínica opcional.
// El paciente se fija en esta pantalla (no en el chat): el agente nunca puede
// pedir otro paciente. Se registra en SECCIONES como los demás módulos.
"use strict";

const AST = { modo: "agente", paciente: null, conversacion: null, revision: 0, ocupado: false };
const AST_HERRAMIENTAS = {
  consultar_paciente: "Consultó los datos clínicos del paciente",
  predecir_urgencia: "Estimó la urgencia con el modelo XGBoost",
  buscar_guias: "Buscó en las guías clínicas (RAG)",
  buscar_ips_con_convenio: "Buscó instituciones con convenio con la EPS",
  proponer_remision: "Propuso una remisión",
  cargar_skill: "Cargó un procedimiento (skill)",
  guardar_preferencia: "Guardó una preferencia",
};

function astClaveConversacion() {
  return "ast-conv-v2:" + (AST.paciente || "general");
}

function astConversacionNueva() {
  AST.revision++;
  AST.conversacion = "c-" + Date.now().toString(36);
  try { sessionStorage.setItem(astClaveConversacion(), AST.conversacion); } catch { /* sin storage */ }
}

function astSeleccionarConversacion() {
  AST.revision++;
  AST.conversacion = null;
  try { AST.conversacion = sessionStorage.getItem(astClaveConversacion()); } catch { /* sin storage */ }
  if (!AST.conversacion) astConversacionNueva();
}

async function vistaAsistente(params = {}) {
  if (params.paciente) AST.paciente = +params.paciente;

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
      <div id="ast-rag-revision" hidden>
        <label><input type="checkbox" id="ast-enviar-revision"> Enviar el resultado a revisión clínica</label>
        <div id="ast-rag-pendientes"></div>
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
      <p class="tenue ast-pie">Apoyo a la decisión clínica. El asistente trabaja con el seudónimo del paciente. Evita incluir datos personales en tus mensajes.</p>
    </section></div>`;

  $$("#ast-modo button").forEach(b => b.addEventListener("click", () => {
    AST.modo = b.dataset.modo;
    $$("#ast-modo button").forEach(x => x.classList.toggle("activa", x === b));
    astActualizarModo();
    astCargarRevisiones().catch(e => toast(e.message, "error"));
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
  astActualizarModo();
  await astAbrirPaciente(AST.paciente);
}

async function astAbrirPaciente(pid) {
  const chat = $("#ast-chat"), info = $("#ast-pinfo");
  chat.replaceChildren();
  $("#ast-texto").value = "";
  AST.paciente = pid;
  $("#ast-enviar-revision").checked = false;
  $("#ast-rag-pendientes").replaceChildren();
  astActualizarModo();
  astSeleccionarConversacion();
  const revision = AST.revision;
  const vigente = () => chat.isConnected && AST.revision === revision;
  $("#ast-pid").value = pid || "";
  $("#ast-cerrar").disabled = !pid;
  info.textContent = pid ? "Cargando paciente…" : "Sin paciente: solo preguntas generales.";
  if (pid) {
    try {
      const p = await API.get(`/pacientes/${pid}`);
      if (!vigente()) return;
      info.innerHTML = `<strong>${esc(p.nombre)}</strong> · ${p.fecha_nacimiento ? edad(p.fecha_nacimiento) + " años · " : ""}${esc(p.eps || "")}`;
    } catch (e) {
      if (vigente()) {
        await astAbrirPaciente(null);
        if (info.isConnected && AST.paciente === null) info.textContent = e.message;
      }
      return;
    }
  }
  if (vigente()) {
    await astCargarHistorial();
    if (vigente()) await astCargarRevisiones().catch(e => toast(e.message, "error"));
  }
}

function astBienvenida() {
  astMensaje("asistente", "Hola. Abre un paciente arriba y pregúntame por él. Te respondo corto: qué tan grave está, qué dicen las guías, a dónde conviene remitirlo. Si propongo una remisión, tú decides.");
}

async function astCargarHistorial() {
  const chat = $("#ast-chat"), pid = AST.paciente, conversacion = AST.conversacion, revision = AST.revision;
  const vigente = () => chat.isConnected && AST.revision === revision;
  try {
    const h = await API.get(`/agente/historial?conversacion_id=${encodeURIComponent(conversacion)}${pid ? "&paciente_id=" + pid : ""}`);
    if (!vigente()) return;
    if (!h.mensajes.length && !h.pendiente) { astBienvenida(); return; }
    h.mensajes.forEach(m => astMensaje(m.rol, m.texto));
    if (h.pendiente) astTarjetaAprobacion([{ herramienta: h.pendiente.herramienta, args: h.pendiente.args }]);
  } catch (e) {
    if (!vigente()) return;
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
  return `<details class="ast-pasos"><summary>Fuentes (${fuentes.length})</summary><ol>${fuentes.map(f => {
    const nombre = esc(f.fuente);
    const etiqueta = f.url?.startsWith("https://")
      ? `<a href="${esc(f.url)}" target="_blank" rel="noopener noreferrer">${nombre}</a>` : nombre;
    return `<li><span class="ast-cita">[${f.n}]</span> ${etiqueta} <code>${esc(f.chunk_id)}</code>
      ${f.seccion ? `<div>${esc(f.seccion)}</div>` : ""}
      <div class="ast-resultado">${esc(f.texto)}</div></li>`;
  }).join("")}</ol></details>`;
}

function astActualizarModo() {
  $("#ast-texto").maxLength = AST.modo === "rag" ? 500 : 1000;
  $("#ast-rag-revision").hidden = AST.modo !== "rag" || !AST.paciente
    || !["medico", "especialista"].includes(sesion.usuario.rol);
}

async function astCargarRevisiones() {
  const panel = $("#ast-rag-pendientes"), revision = AST.revision, pid = AST.paciente;
  if ($("#ast-rag-revision").hidden) return;
  const lista = await API.get(`/pacientes/${pid}/rag/pendientes`);
  if (!panel.isConnected || revision !== AST.revision) return;
  panel.innerHTML = lista.map(r => `<section class="ast-aprobar" data-rag-id="${r.id}">
    <h4>Resultado RAG #${r.id} pendiente de revisión</h4>
    <p>${astFormato(r.respuesta)}</p>${astFuentes(r.fuentes)}
    <label>Motivo del rechazo<textarea class="ast-motivo" rows="2" maxlength="300"></textarea></label>
    <div class="ast-botones">
      <button class="btn btn-ok" data-revisar="aprobado">Aprobar para la historia clínica</button>
      <button class="btn btn-peligro" data-revisar="rechazado">Rechazar resultado</button>
    </div></section>`).join("");
  $$("[data-revisar]", panel).forEach(boton => boton.addEventListener("click", async () => {
    const tarjeta = boton.closest("[data-rag-id]");
    const motivo = $(".ast-motivo", tarjeta).value.trim();
    const decision = boton.dataset.revisar;
    if (decision === "rechazado" && !motivo) {
      toast("Escribe el motivo del rechazo.", "error");
      $(".ast-motivo", tarjeta).focus();
      return;
    }
    $$("button", tarjeta).forEach(b => b.disabled = true);
    try {
      await API.post(`/pacientes/${pid}/rag/${tarjeta.dataset.ragId}/revisar`, { decision, motivo });
      if (!tarjeta.isConnected) return;
      tarjeta.remove();
      toast(decision === "aprobado" ? "Resultado incorporado a la historia clínica." : "Resultado rechazado.", "ok");
    } catch (e) {
      if (!tarjeta.isConnected) return;
      toast(e.message, "error");
      if (e.status === 409) tarjeta.remove();
      else $$("button", tarjeta).forEach(b => b.disabled = false);
    }
  }));
}

function astFuentesCortas(fuentes) {
  if (!fuentes || !fuentes.length) return "";
  return `<details class="ast-pasos"><summary>Fuentes (${fuentes.length})</summary><ol>${fuentes.map(f =>
    `<li><span class="ast-cita">[${f.n}]</span> ${esc(f.fuente)}<div class="ast-resultado">${esc(String(f.texto || "").slice(0, 260))}</div></li>`).join("")}</ol></details>`;
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
      const r = await API.post("/agente/decidir", { conversacion_id: AST.conversacion, paciente_id: AST.paciente, decision: b.dataset.d, motivo: motivo || null });
      if (!div.isConnected) return;
      if (r.estado !== "respondido") {
        espera.remove();
        astPintarRespuesta(r);
        $$("button", div).forEach(x => x.disabled = false);
        return;
      }
      $(".ast-motivo", div).remove();
      $(".ast-botones", div).innerHTML = `<span class="pildora ${b.dataset.d === "approve" ? "est-activo" : "est-pendiente"}">${b.dataset.d === "approve" ? "Aprobada por ti" : "Rechazada por ti"}</span>`;
      if (!espera.isConnected) return;
      espera.remove();
      astPintarRespuesta(r);
      if (b.dataset.d === "approve") { toast("Remisión radicada en el libro.", "ok"); remisionesCache = null; }
    } catch (e) {
      espera.remove();
      $$("button", div).forEach(x => x.disabled = false);
      if (div.isConnected) astMensaje("sistema", e.message);
    }
  }));
}

function astPintarRespuesta(r) {
  if (r.estado === "pendiente_aprobacion") {
    if (r.pasos && r.pasos.length) astMensaje("asistente", "Revisé el caso. Detalle técnico:", astPasos(r.pasos));
    else if (r.detalle) astMensaje("sistema", r.detalle);
    else if ((r.consulto || []).length) astMensaje("asistente", `Revisé ${r.consulto.join(", ")} y te propongo esto:`);
    astTarjetaAprobacion(r.acciones);
  } else if (r.estado === "respondido") {
    if (r.seguridad) { astMensaje("sistema", r.respuesta); return; }       // la guardia frenó el mensaje (R25)
    // Al médico: la respuesta, sus fuentes y qué se consultó. La traza técnica completa
    // (cada herramienta con sus resultados) solo le llega al administrador.
    const consulto = (r.consulto || []).length ? `<p class="tenue ast-consulto">Consulté: ${esc(r.consulto.join(" · "))}</p>` : "";
    astMensaje("asistente", r.respuesta || "(sin texto)", astFuentesCortas(r.fuentes) + consulto + astPasos(r.pasos || []));
  } else if (r.estado === "tiempo_agotado" || r.estado === "error") {
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
      if (!espera.isConnected) return;
      espera.remove();
      astPintarRespuesta(r);
    } else {
      const r = await API.post("/rag/preguntar", {
        pregunta: texto, paciente_id: AST.paciente,
        enviar_a_revision: !$("#ast-rag-revision").hidden && $("#ast-enviar-revision").checked,
      });
      if (!espera.isConnected) return;
      espera.remove();
      if (r.seguridad) { astMensaje("sistema", r.respuesta); return; }
      const modo = { llm: "con pasajes seleccionados por el modelo", extractivo: "extractiva (frases de las guías, sin LLM)", abstencion: "sin evidencia" }[r.modo] || r.modo;
      astMensaje("asistente", r.respuesta, `<p class="tenue ast-modo-rag">Respuesta ${esc(modo)}</p>${astFuentes(r.fuentes)}`);
      if (r.revision_id) {
        $("#ast-enviar-revision").checked = false;
        await astCargarRevisiones().catch(e => toast(e.message, "error"));
      }
    }
  } catch (e) {
    if (!espera.isConnected) return;
    espera.remove();
    astMensaje("sistema", e.status === 403 ? "Tu rol no puede usar el asistente." : e.message);
  } finally {
    AST.ocupado = false;
    if ($("#ast-enviar")) $("#ast-enviar").disabled = false;
  }
}

if (typeof SECCIONES !== "undefined" && typeof ICONOS !== "undefined") {
  ICONOS.asistente = '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/><path d="M8.5 11h.01M12 11h.01M15.5 11h.01"/>';
  SECCIONES.asistente = {
    titulo: "Asistente clínico", icono: "asistente",
    roles: ["medico", "especialista", "admin"],          // el admin entra en modo consulta y ve la traza completa
    pintar: vistaAsistente, grupo: "Inteligencia artificial",
  };
}
