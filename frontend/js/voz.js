// ==========================================================================
// TrazaRed HUV — Bitácora por voz con IA
// ==========================================================================
// El médico llama a la institución con el altavoz; TrazaRed transcribe la
// llamada y la IA propone la anotación de la bitácora (institución, quién
// respondió, qué dijo y la causa). El médico revisa, corrige y confirma: la
// anotación se guarda por la ruta normal de gestiones, firmada y con su hora.
//
// Transcripción, en este orden:
//   1. Dictado del navegador (Web Speech API: Chrome y Edge; Brave lo bloquea).
//   2. Grabar audio y transcribirlo en el servidor con Whisper (si hay GROQ_API_KEY).
//   3. Escribir o pegar lo que se dijo, o usar el ejemplo para la demostración.
// Extracción: POST /bitacora/voz/extraer (reglas siempre; LLM si hay llave).
"use strict";

const VOZ = { reconocedor: null, grabador: null, trozos: [], grabando: false };
const VOZ_SR = window.SpeechRecognition || window.webkitSpeechRecognition;

function vozEjemplo(f) {
  const r = f.remision;
  const inst = (f.pronostico && f.pronostico.instituciones_sugeridas && f.pronostico.instituciones_sugeridas[0]
    && f.pronostico.instituciones_sugeridas[0].institucion) || "Fundación Valle del Lili";
  return `Buenas tardes, le hablo de la Oficina de Referencia del Hospital Universitario del Valle. ` +
    `Necesito remitir un paciente: ${String(r.motivo || "requiere valoración por UCI").slice(0, 120)}. ` +
    `Sí, buenas tardes, habla la doctora Paola Muñoz, coordinadora de UCI de la ${inst}. ` +
    `En este momento no tenemos camas disponibles en UCI. Intente de nuevo en dos horas o comuníquese con el CRU.`;
}

function abrirDictado(f, recargar) {
  const r = f.remision;
  const fondo = document.createElement("div");
  fondo.className = "fondo-modal";
  fondo.innerHTML = `<div class="modal modal-voz" role="dialog" aria-label="Dictar llamada">
    <div class="modal-cabeza"><h3>${icono("microfono")} Dictar llamada · ${esc(r.radicado)}</h3>
      <button type="button" class="btn-icono" data-cerrar aria-label="Cerrar">${icono("x")}</button></div>
    <div class="modal-cuerpo">
      <ol class="voz-pasos"><li class="activo">Llama con el altavoz y graba</li><li>La IA llena la bitácora</li><li>Revisas y confirmas</li></ol>

      <section class="voz-paso" id="voz-1">
        <div class="voz-controles">
          <button type="button" class="btn btn-voz" id="voz-grabar">${icono("microfono")}<span>Empezar a escuchar</span></button>
          <span class="voz-estado" id="voz-estado" aria-live="polite">${VOZ_SR ? "Listo para escuchar (dictado del navegador)." : "Este navegador no transcribe en vivo: se grabará el audio."}</span>
        </div>
        <label class="voz-etiqueta" for="voz-texto">Lo que se dijo en la llamada <span class="tenue">(puedes corregirlo o pegarlo)</span></label>
        <textarea id="voz-texto" rows="6" maxlength="8000" placeholder="La transcripción aparece aquí mientras hablan…"></textarea>
        <div class="voz-acciones">
          <button type="button" class="btn btn-suave btn-sm" id="voz-ejemplo">Usar llamada de ejemplo</button>
          <button type="button" class="btn btn-primario" id="voz-extraer">${icono("prediccion")}Llenar la bitácora con IA</button>
        </div>
        <p class="tenue voz-priv">Antes de pasar por la IA se quitan el nombre, el documento y los teléfonos del paciente. La grabación no se guarda.</p>
      </section>

      <form class="voz-paso oculto" id="voz-2" novalidate>
        <div class="voz-ia" id="voz-ia"></div>
        <div class="campo"><label for="v-ins">Institución contactada</label><input id="v-ins" name="institucion_contactada" list="dl-v-ins" required>
          ${typeof datalist === "function" && typeof INSTITUCIONES !== "undefined" ? datalist("dl-v-ins", INSTITUCIONES) : ""}</div>
        <div class="fila"><div class="campo"><label for="v-med">Medio</label><select id="v-med" name="medio"><option value="telefono">Teléfono</option>
            <option value="plataforma">Plataforma de referencia</option><option value="correo">Correo</option><option value="presencial">Presencial</option></select></div>
          <div class="campo"><label for="v-por">Contactado por</label><input id="v-por" name="contactado_por" required></div></div>
        <div class="campo"><label for="v-res">Respuesta de la institución</label><textarea id="v-res" name="respuesta" rows="3" maxlength="500" required></textarea></div>
        <p class="prevista-causa" id="v-causa" aria-live="polite"></p>
        <details class="voz-detalle"><summary>Ver la transcripción que recibió la IA (sin datos del paciente)</summary><pre id="voz-seud"></pre></details>
        <p class="error-form oculto" id="voz-error"></p>
        <div class="voz-acciones">
          <button type="button" class="btn btn-secundario" id="voz-volver">Volver a la transcripción</button>
          <button type="submit" class="btn btn-coral" id="voz-guardar">Confirmar y anotar en la bitácora</button>
        </div>
      </form>
    </div></div>`;
  document.body.appendChild(fondo);
  const $m = (s) => $(s, fondo);
  const cerrar = () => { vozDetener(); fondo.remove(); };
  $$("[data-cerrar]", fondo).forEach(b => b.addEventListener("click", cerrar));
  fondo.addEventListener("mousedown", (e) => { if (e.target === fondo) cerrar(); });

  const paso = (n) => {
    $m("#voz-1").classList.toggle("oculto", n !== 1);
    $m("#voz-2").classList.toggle("oculto", n !== 2);
    $$(".voz-pasos li", fondo).forEach((li, i) => li.classList.toggle("activo", i < n));
  };

  $m("#voz-ejemplo").addEventListener("click", () => { $m("#voz-texto").value = vozEjemplo(f); $m("#voz-texto").focus(); });
  $m("#voz-grabar").addEventListener("click", () => (VOZ.grabando ? vozDetener() : vozEmpezar(fondo, r.id)));
  $m("#voz-volver").addEventListener("click", () => paso(1));

  $m("#voz-extraer").addEventListener("click", async () => {
    const texto = $m("#voz-texto").value.trim();
    if (texto.length < 10) { toast("Graba la llamada, pega lo que se dijo o usa el ejemplo.", "error"); return; }
    vozDetener();
    const b = $m("#voz-extraer");
    b.disabled = true; b.lastChild.textContent = "Analizando la llamada…";
    try {
      const x = await API.post("/bitacora/voz/extraer", { remision_id: r.id, transcripcion: texto });
      $m("#v-ins").value = x.campos.institucion_contactada || "";
      $m("#v-med").value = x.campos.medio || "telefono";
      $m("#v-por").value = x.campos.contactado_por || sesion.usuario.nombre;
      $m("#v-res").value = x.campos.respuesta || "";
      $m("#voz-seud").textContent = x.transcripcion_seudonimizada;
      const k = x.clasificacion || {};
      $m("#voz-ia").innerHTML = `<div class="agr-nota">${icono("info")}<p><strong>La IA propone esta anotación</strong>
        (${x.modo === "llm" ? "modelo de lenguaje" : "extracción por reglas"}). Revísala y corrige lo que haga falta: nada se guarda sin tu confirmación.
        ${x.faltan.length ? `<br><span class="voz-falta">No detectó: ${esc(x.faltan.join(", "))}. Complétalo a mano.</span>` : ""}</p></div>
        <div class="voz-chips">
          <span class="voz-chip ${x.institucion_detectada ? "ok" : "falta"}">Institución</span>
          <span class="voz-chip ${x.quien_respondio ? "ok" : "falta"}">${esc(x.quien_respondio || "Quién respondió")}${x.cargo ? ` · ${esc(x.cargo)}` : ""}</span>
          <span class="causa c-${esc(k.efecto || "neutra")} ${esc(k.causa || "otra")}">${esc(k.etiqueta || "Otra")}</span>
        </div>`;
      vozCausa(fondo);
      paso(2);
      $m("#v-res").focus();
    } catch (e) { toast(e.message, "error"); }
    finally { b.disabled = false; b.lastChild.textContent = "Llenar la bitácora con IA"; }
  });

  $m("#v-res").addEventListener("input", debounce(() => vozCausa(fondo), 350));

  $m("#voz-2").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $m("#voz-error"), b = $m("#voz-guardar");
    err.classList.add("oculto"); b.disabled = true;
    try {
      const d = Object.fromEntries(new FormData($m("#voz-2")));
      if (!d.institucion_contactada.trim() || !d.respuesta.trim()) throw new Error("Completa la institución y la respuesta.");
      const g = await API.post(`/registro/remisiones/${r.id}/gestiones`, {
        institucion_contactada: d.institucion_contactada.trim(), medio: d.medio,
        respuesta: d.respuesta.trim(), contactado_por: d.contactado_por.trim() });
      remisionesCache = null;
      toast(`Gestión anotada desde la llamada · ${g.clasificacion.etiqueta}${g.estado_cambio ? ` · el asiento pasó a ${ESTADOS[g.estado]}` : ""}`, "ok");
      fondo.remove();
      recargar();
    } catch (x) { err.textContent = x.message; err.classList.remove("oculto"); }
    finally { b.disabled = false; }
  });
}

async function vozCausa(fondo) {
  const t = $("#v-res", fondo).value.trim(), p = $("#v-causa", fondo);
  if (t.length < 3) { p.textContent = ""; return; }
  try {
    const k = await API.post("/registro/ia/clasificar", { texto: t });
    p.innerHTML = `Se anotará como <span class="causa c-${esc(k.efecto)} ${esc(k.causa)}">${esc(k.etiqueta)}</span>${k.efecto === "acepta" ? " · el asiento pasará a <strong>Aceptada</strong>" : ""}`;
  } catch { p.textContent = ""; }
}

function vozBoton(fondo, grabando, texto) {
  VOZ.grabando = grabando;
  const b = $("#voz-grabar", fondo);
  if (!b) return;
  b.classList.toggle("grabando", grabando);
  $("span", b).textContent = grabando ? "Detener" : "Empezar a escuchar";
  if (texto) $("#voz-estado", fondo).textContent = texto;
}

function vozEmpezar(fondo, remisionId) {
  const area = $("#voz-texto", fondo);
  if (VOZ_SR) {
    const sr = new VOZ_SR();
    sr.lang = "es-CO"; sr.continuous = true; sr.interimResults = true;
    let base = area.value ? area.value.trim() + " " : "";
    sr.onresult = (ev) => {
      let final = "", parcial = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const t = ev.results[i][0].transcript;
        if (ev.results[i].isFinal) final += t + ". "; else parcial += t;
      }
      base += final;
      area.value = base + parcial;
    };
    sr.onerror = (ev) => {
      const motivo = { "not-allowed": "No diste permiso al micrófono.", "network": "Tu navegador no permite el dictado (Brave lo bloquea): usa Chrome o Edge, o pega el texto.",
        "no-speech": "No se escuchó nada. Acerca el altavoz al micrófono." }[ev.error] || `Error del dictado: ${ev.error}`;
      vozBoton(fondo, false, motivo);
      if (ev.error === "network") vozGrabarAudio(fondo, remisionId);
    };
    sr.onend = () => { if (VOZ.grabando && VOZ.reconocedor === sr) { try { sr.start(); } catch { vozBoton(fondo, false); } } };
    VOZ.reconocedor = sr;
    try { sr.start(); vozBoton(fondo, true, "Escuchando… habla normal, el altavoz cerca del micrófono."); }
    catch { vozGrabarAudio(fondo, remisionId); }
  } else {
    vozGrabarAudio(fondo, remisionId);
  }
}

async function vozGrabarAudio(fondo, remisionId) {
  if (!navigator.mediaDevices || !window.MediaRecorder) { vozBoton(fondo, false, "Este navegador no puede grabar: pega el texto o usa el ejemplo."); return; }
  try {
    const flujo = await navigator.mediaDevices.getUserMedia({ audio: true });
    VOZ.trozos = [];
    const rec = new MediaRecorder(flujo);
    rec.ondataavailable = (e) => { if (e.data.size) VOZ.trozos.push(e.data); };
    rec.onstop = async () => {
      flujo.getTracks().forEach(t => t.stop());
      const audio = new Blob(VOZ.trozos, { type: rec.mimeType || "audio/webm" });
      vozBoton(fondo, false, "Transcribiendo el audio en el servidor…");
      const datos = new FormData();
      datos.append("audio", audio, "llamada.webm");
      datos.append("remision_id", remisionId);
      try {
        const t = await API.subir("/bitacora/voz/transcribir", datos);
        const area = $("#voz-texto", fondo);
        if (area) area.value = ((area.value || "") + " " + t.texto).trim();
        vozBoton(fondo, false, "Transcripción lista. Revísala y pulsa «Llenar la bitácora con IA».");
      } catch (e) { vozBoton(fondo, false, e.message); }
    };
    VOZ.grabador = rec;
    rec.start();
    vozBoton(fondo, true, "Grabando el audio de la llamada… pulsa Detener al terminar.");
  } catch { vozBoton(fondo, false, "No diste permiso al micrófono."); }
}

function vozDetener() {
  const sr = VOZ.reconocedor; VOZ.reconocedor = null;
  if (sr) { try { sr.stop(); } catch { /* ya estaba detenido */ } }
  if (VOZ.grabador && VOZ.grabador.state === "recording") VOZ.grabador.stop();
  VOZ.grabador = null;
  VOZ.grabando = false;
  $$(".modal-voz #voz-grabar").forEach(b => { b.classList.remove("grabando"); $("span", b).textContent = "Empezar a escuchar"; });
}

if (typeof ICONOS !== "undefined") {
  ICONOS.microfono = '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3M8 21h8"/>';
}
